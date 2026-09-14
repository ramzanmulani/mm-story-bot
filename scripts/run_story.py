"""Post ONE story to Instagram and Facebook. One daily slot = one run.

Flow:
  1. read rotation state          -> state/history.json
  2. fetch the video library      -> Instagram Graph API
  3. pick the next clip           -> shuffle bag
  4. download + normalise once    -> ffmpeg, 1080x1920, 3-59s
  5. Instagram: host on Pages, create container, poll, publish
  6. Facebook:  upload the same file straight to the Page story
  7. write state back             -> committed by the workflow

The two platforms are independent. If one fails the other still goes out,
and the rotation only advances when at least one story actually published -
so a bad slot retries the same clip instead of burning it.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import notify  # noqa: E402
from common import LOG, ConfigError, env_flag, load_config, require_env  # noqa: E402
from fb_api import FacebookClient, FacebookError  # noqa: E402
from host import HostError, publish_file, repo_slug  # noqa: E402
from ig_api import InstagramClient, InstagramError, Media  # noqa: E402
from media import (  # noqa: E402
    MediaError, download, ensure_ffmpeg, meets_story_spec, normalise, probe,
)
from picker import (  # noqa: E402
    feed_posts_in_last_24h, load_state, pick, posted_in_last_24h, record_posted, save_state,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Post one story to Instagram and Facebook.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Do everything except the final publish calls.")
    parser.add_argument("--slot", default="?", help="Slot label, used in logs only.")
    args = parser.parse_args()

    dry_run = args.dry_run or env_flag("DRY_RUN")
    started = time.time()

    try:
        cfg = load_config()
        if not cfg["publish"].get("enabled", True):
            LOG.warning("publish.enabled is false - running as a dry run.")
            dry_run = True

        want_ig = bool(cfg.get("targets", {}).get("instagram", True))
        want_fb = bool(cfg.get("targets", {}).get("facebook", False))
        want_feed = bool(cfg.get("targets", {}).get("facebook_feed", False))
        if not (want_ig or want_fb or want_feed):
            raise ConfigError("Every target is switched off in config.yaml - nothing to do.")

        ensure_ffmpeg()
        user_token = require_env("IG_ACCESS_TOKEN")
        ig_user_id = require_env("IG_USER_ID")
        client = InstagramClient(ig_user_id, user_token)

        expiry, days_left = client.token_expiry()
        if days_left is None:
            LOG.info("Token expires: %s", expiry)
        elif days_left < 0:
            LOG.error("Token expired on %s - nothing will post until it is replaced.", expiry)
        elif days_left <= 7:
            LOG.warning(
                "TOKEN EXPIRES IN %.1f DAYS (%s). Replace it before then or the "
                "bot goes quiet - see 'The token' in README.md.", days_left, expiry,
            )
            notify.send(
                "Story bot token is about to expire",
                f"{days_left:.1f} days left (expires {expiry}).\n"
                f"Generate a permanent Page token and re-run setup.",
                ok=False,
            )
        else:
            LOG.info("Token expires: %s  (%.0f days left)", expiry, days_left)

        used, total = client.publishing_quota()
        LOG.info("Instagram publishing quota: %d/%d used in the last 24h.", used, total)
        if want_ig and used >= total - 1:
            LOG.warning(
                "Instagram's 24h quota is nearly exhausted (%d/%d) - skipping Instagram "
                "this slot so the account is not rate limited.", used, total,
            )
            want_ig = False
            if not want_fb:
                raise InstagramError("Quota exhausted and Facebook is off - nothing to post.")

        library = client.fetch_library(
            media_types=cfg["source"]["media_types"],
            product_types=cfg["source"]["product_types"],
            max_age_days=int(cfg["source"].get("max_age_days", 0)),
            max_items=int(cfg["source"].get("max_library_size", 300)),
            exclude_ids=cfg["source"].get("exclude_media_ids", []),
        )
        if not library:
            raise ConfigError(
                "No eligible videos found on the account. Check that the token belongs to "
                "@mythicalmotions and that source.media_types matches the posts you have."
            )

        by_id = {m.id: m for m in library}
        state = load_state()
        LOG.info("Stories posted by this bot in the last 24h: %d", posted_in_last_24h(state))

        if want_feed:
            allowed = int(cfg.get("facebook", {}).get("feed_posts_per_day", 1))
            already = feed_posts_in_last_24h(state)
            if already >= max(allowed, 0):
                LOG.info(
                    "Facebook feed: %d/%d Page Reel(s) already posted today - "
                    "this slot posts stories only.", already, allowed,
                )
                want_feed = False
            else:
                LOG.info("Facebook feed: %d/%d posted today - this slot also posts a Reel.",
                         already, allowed)

        media_id = pick(state, [m.id for m in library],
                        no_repeat_window=int(cfg["rotation"].get("no_repeat_window", 10)))
        if media_id is None:
            raise ConfigError("Rotation returned nothing to post.")
        chosen: Media = by_id[media_id]
        LOG.info("Clip: %s  |  %s", chosen.permalink or chosen.id,
                 chosen.short_caption or "(no caption)")

        # Facebook is resolved before any encoding so a permissions problem
        # surfaces in seconds rather than after a two-minute transcode.
        fb: FacebookClient | None = None
        if want_fb or want_feed:
            try:
                fb = FacebookClient.resolve(
                    user_token,
                    page_id=str(cfg.get("facebook", {}).get("page_id", "") or ""),
                    prefer_ig_user_id=ig_user_id,
                )
            except FacebookError as exc:
                LOG.error("Facebook is not usable this run: %s", exc)
                if not want_ig:
                    raise
                want_fb = want_feed = False

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            src = download(chosen.media_url, tmpdir / "source.mp4")
            info = probe(src)
            LOG.info("Source: %dx%d  %.1fs  %s/%s  %.1f MB",
                     info["width"], info["height"], info["duration"],
                     info["vcodec"], info["acodec"] or "no-audio", info["size_mb"])

            clip = normalise(
                src, tmpdir / f"story_{chosen.id}_{int(time.time())}.mp4", info, cfg["video"]
            )

            results: dict[str, str] = {}
            failures: dict[str, str] = {}

            if want_ig:
                try:
                    url = _host_for_instagram(clip, chosen, info, cfg)
                    if dry_run:
                        LOG.info("DRY RUN - Instagram would publish: %s", url)
                        results["instagram"] = "dry run"
                    else:
                        container = client.create_story_container(url)
                        client.wait_for_container(
                            container,
                            poll_sec=int(cfg["publish"]["container_poll_sec"]),
                            timeout_sec=int(cfg["publish"]["container_timeout_sec"]),
                        )
                        results["instagram"] = client.publish(container)
                except (InstagramError, HostError, MediaError) as exc:
                    LOG.error("Instagram failed: %s", exc)
                    failures["instagram"] = str(exc)

            if want_fb and fb is not None:
                try:
                    if dry_run:
                        LOG.info("DRY RUN - Facebook would post %.1f MB to Page %s",
                                 clip.stat().st_size / 1e6, fb.page_id)
                        results["facebook"] = "dry run"
                    else:
                        results["facebook"] = fb.publish_video_story(
                            clip,
                            poll_sec=int(cfg["publish"]["container_poll_sec"]),
                            timeout_sec=int(cfg["publish"]["container_timeout_sec"]),
                        )
                except FacebookError as exc:
                    LOG.error("Facebook failed: %s", exc)
                    failures["facebook"] = str(exc)

            if want_feed and fb is not None:
                try:
                    caption = _feed_caption(chosen, cfg)
                    if dry_run:
                        LOG.info("DRY RUN - Facebook Page Reel caption would be:\n%s", caption)
                        results["facebook_feed"] = "dry run"
                    else:
                        results["facebook_feed"] = fb.publish_reel(
                            clip,
                            caption=caption,
                            poll_sec=int(cfg["publish"]["container_poll_sec"]),
                            timeout_sec=int(cfg["publish"]["container_timeout_sec"]),
                        )
                except FacebookError as exc:
                    LOG.error("Facebook Page feed failed: %s", exc)
                    failures["facebook_feed"] = str(exc)

        if not results:
            raise ConfigError(
                "Nothing published. " + " | ".join(f"{k}: {v}" for k, v in failures.items())
            )

        if not dry_run:
            record_posted(
                state,
                media_id=chosen.id,
                story_id=results.get("instagram", ""),
                permalink=chosen.permalink,
                facebook_post_id=results.get("facebook", ""),
                facebook_feed_id=results.get("facebook_feed", ""),
            )
            save_state(state)

        _report(args.slot, chosen, results, failures, started, cfg)
        return 0

    except (ConfigError, InstagramError, FacebookError, MediaError, HostError) as exc:
        LOG.error("%s", exc)
        _fail(args.slot, str(exc))
        return 1
    except Exception as exc:
        LOG.exception("Unexpected failure: %s", exc)
        _fail(args.slot, f"Unexpected failure: {exc}")
        return 1


def _host_for_instagram(clip: Path, chosen: Media, info: dict, cfg: dict) -> str:
    """Instagram will not take an upload - it fetches a public https url."""
    try:
        return publish_file(
            clip,
            repo_slug=repo_slug(),
            token=require_env("GITHUB_TOKEN"),
            pages_origin=cfg["account"]["pages_origin"],
            retention_hours=int(cfg["hosting"]["retention_hours"]),
            wait_sec=int(cfg["hosting"]["pages_wait_sec"]),
        )
    except HostError as exc:
        if cfg["publish"].get("fallback_to_source_url") and meets_story_spec(info, cfg["video"]):
            LOG.warning("%s\nFalling back to the original Instagram url for this run.", exc)
            return chosen.media_url
        raise


def _feed_caption(chosen: Media, cfg: dict) -> str:
    """Caption for the Page Reel, built from config.yaml."""
    fb_cfg = cfg.get("facebook", {}) or {}
    template = str(fb_cfg.get("feed_caption", "{caption}") or "{caption}")
    footer = str(fb_cfg.get("feed_caption_footer", "") or "")

    body = template.format(
        caption=(chosen.caption or "").strip(),
        permalink=chosen.permalink or "",
    ).strip()

    if footer.strip():
        body = f"{body}\n{footer.rstrip()}" if body else footer.strip()
    return body.strip()


def _report(slot: str, chosen: Media, results: dict, failures: dict,
            started: float, cfg: dict) -> None:
    took = time.time() - started
    line = ", ".join(f"{k}: {v}" for k, v in results.items())
    LOG.info("Done in %.0fs - %s", took, line)
    if failures:
        LOG.warning("Partial: %s", " | ".join(f"{k} failed" for k in failures))

    if failures and cfg["notify"].get("on_failure", True):
        notify.send(
            f"Story partly failed (slot {slot})",
            f"{chosen.permalink or chosen.id}\nPosted: {line or 'nothing'}\n\n"
            + "\n".join(f"{k}: {v}" for k, v in failures.items()),
            ok=False,
        )
    elif cfg["notify"].get("on_success"):
        notify.send(f"Story posted (slot {slot})",
                    f"{chosen.permalink or chosen.id}\n{line} - {took:.0f}s", ok=True)


def _fail(slot: str, reason: str) -> None:
    try:
        gated = load_config()["notify"].get("on_failure", True)
    except Exception:
        gated = True  # config itself may be what broke - still tell him
    if gated:
        notify.send(f"Story bot failed (slot {slot})", reason, ok=False)


if __name__ == "__main__":
    raise SystemExit(main())
