"""Facebook Page Stories - three-phase video upload.

Unlike Instagram, Facebook takes the file directly, so no public URL is
needed here. The Page token is derived at runtime from the same long-lived
user token Instagram uses, so there is no second secret to keep alive.

Facebook's own limits: 3-90s source (stories play up to 60s), 9:16,
1080x1920 recommended, H.264/H.265, AAC 48kHz stereo.
"""

from __future__ import annotations

import time
from pathlib import Path

from common import LOG, http_request

GRAPH_VERSION = "v21.0"
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"


class FacebookError(RuntimeError):
    """Facebook refused something we cannot fix inside one run."""


def _payload(resp, what: str) -> dict:
    try:
        data = resp.json()
    except ValueError:
        raise FacebookError(f"{what}: non-JSON response ({resp.status_code}): {resp.text[:300]}")

    if resp.status_code >= 400 or "error" in data:
        err = data.get("error", {})
        code = err.get("code")
        msg = err.get("message", resp.text[:300])
        hint = ""
        if code in (190, 102):
            hint = "  -> The token has expired. Re-run setup.sh with a fresh long-lived token."
        elif code == 200:
            hint = (
                "  -> The token is missing pages_manage_posts / pages_read_engagement, "
                "or you do not have CREATE_CONTENT rights on that Page."
            )
        raise FacebookError(f"{what}: [{code}] {msg}{hint}")
    return data


class FacebookClient:
    """Publishes video stories to one Facebook Page."""

    def __init__(self, page_id: str, page_token: str, page_name: str = "") -> None:
        self.page_id = page_id
        self._token = page_token
        self.page_name = page_name

    # -- discovery --------------------------------------------------------

    @classmethod
    def resolve(
        cls,
        token: str,
        *,
        page_id: str = "",
        prefer_ig_user_id: str = "",
    ) -> "FacebookClient":
        """Find the Page to post to and get a Page token for it.

        Accepts either kind of token:
          * a long-lived USER token - the Page and its token are looked up
            through /me/accounts (the Page token that comes back never expires);
          * a PAGE token - used directly, since /me is then the Page itself.

        Preference order for picking a Page from a user token: an explicit
        page_id from config, then the Page that owns the same Instagram
        account the stories go to, then the only Page on the token.
        """
        pages: list[dict] = []
        try:
            resp = http_request(
                "GET",
                f"{GRAPH}/me/accounts",
                params={
                    "fields": "id,name,access_token,instagram_business_account{id}",
                    "limit": 100,
                    "access_token": token,
                },
            )
            pages = _payload(resp, "Listing your Facebook Pages").get("data", [])
        except FacebookError as exc:
            LOG.info("Could not list Pages (%s)", exc)

        if not pages:
            # /me/accounts is empty or refused for a Page token - try that.
            direct = cls._as_page_token(token, page_id)
            if direct is not None:
                return direct
            raise FacebookError(
                "No Facebook Page is available on this token. Facebook stories need "
                "pages_show_list and pages_manage_posts, and you must manage the Page."
            )

        chosen = None
        if page_id:
            chosen = next((p for p in pages if str(p.get("id")) == str(page_id)), None)
            if chosen is None:
                raise FacebookError(
                    f"Page {page_id} (facebook.page_id in config.yaml) is not on this token. "
                    f"Pages found: {', '.join(str(p.get('id')) for p in pages)}"
                )
        if chosen is None and prefer_ig_user_id:
            chosen = next(
                (p for p in pages
                 if str((p.get("instagram_business_account") or {}).get("id")) == str(prefer_ig_user_id)),
                None,
            )
        if chosen is None and len(pages) == 1:
            chosen = pages[0]
        if chosen is None:
            raise FacebookError(
                "More than one Facebook Page on this token and none is linked to the "
                "Instagram account. Set facebook.page_id in config.yaml. Pages: "
                + ", ".join(f"{p.get('name')} ({p.get('id')})" for p in pages)
            )

        page_token = chosen.get("access_token")
        if not page_token:
            raise FacebookError(
                f"No Page access token returned for {chosen.get('name')}. "
                f"The token needs pages_show_list and pages_read_engagement."
            )
        LOG.info("Facebook Page: %s (%s)", chosen.get("name"), chosen.get("id"))
        return cls(str(chosen["id"]), page_token, str(chosen.get("name", "")))

    @classmethod
    def _as_page_token(cls, token: str, page_id_hint: str) -> "FacebookClient | None":
        """Treat `token` as a Page access token, if /me looks like a Page."""
        try:
            resp = http_request(
                "GET",
                f"{GRAPH}/me",
                params={"fields": "id,name,category", "access_token": token},
                attempts=2,
            )
            data = _payload(resp, "Identifying what this token belongs to")
        except FacebookError:
            return None

        page_id = str(data.get("id", ""))
        # Only a Page carries a category; a personal profile does not.
        if not page_id or "category" not in data:
            return None

        if page_id_hint and str(page_id_hint) != page_id:
            raise FacebookError(
                f"This Page token belongs to Page {page_id}, but facebook.page_id "
                f"in config.yaml says {page_id_hint}. Fix one of them."
            )

        LOG.info("Facebook Page (via a Page access token): %s (%s)", data.get("name"), page_id)
        return cls(page_id, token, str(data.get("name", "")))

    # -- publishing -------------------------------------------------------

    def publish_video_story(self, video: Path, *, poll_sec: int = 8, timeout_sec: int = 300) -> str:
        video_id, upload_url = self._start()
        self._upload(upload_url, video)
        post_id = self._finish(video_id)
        self._wait(video_id, poll_sec=poll_sec, timeout_sec=timeout_sec)
        LOG.info("Facebook story published. Post id: %s", post_id or video_id)
        return post_id or video_id

    def _start(self) -> tuple[str, str]:
        resp = http_request(
            "POST",
            f"{GRAPH}/{self.page_id}/video_stories",
            data={"upload_phase": "start", "access_token": self._token},
        )
        data = _payload(resp, "Starting the Facebook upload")
        video_id, upload_url = data.get("video_id"), data.get("upload_url")
        if not video_id or not upload_url:
            raise FacebookError(f"Facebook did not return an upload session: {data}")
        return str(video_id), str(upload_url)

    def _upload(self, upload_url: str, video: Path, what: str = "Facebook") -> None:
        size = video.stat().st_size
        LOG.info("Uploading %.1f MB to %s ...", size / 1e6, what)
        with video.open("rb") as fh:
            resp = http_request(
                "POST",
                upload_url,
                headers={
                    "Authorization": f"OAuth {self._token}",
                    "offset": "0",
                    "file_size": str(size),
                    "Content-Type": "application/octet-stream",
                },
                data=fh,
                timeout=300,
                attempts=2,
            )
        _payload(resp, "Uploading the video to Facebook")

    def _finish(self, video_id: str) -> str:
        resp = http_request(
            "POST",
            f"{GRAPH}/{self.page_id}/video_stories",
            data={
                "upload_phase": "finish",
                "video_id": video_id,
                "video_state": "PUBLISHED",
                "access_token": self._token,
            },
        )
        data = _payload(resp, "Publishing the Facebook story")
        if data.get("success") is False:
            raise FacebookError(f"Facebook reported failure on finish: {data}")
        return str(data.get("post_id", ""))

    def publish_reel(
        self,
        video: Path,
        *,
        caption: str = "",
        poll_sec: int = 8,
        timeout_sec: int = 300,
    ) -> str:
        """Post the clip to the Page's feed as a Reel.

        Facebook now treats every Page video as a Reel, so this is the
        endpoint that puts something permanent on the Page - unlike a story,
        which is gone in 24 hours.
        """
        resp = http_request(
            "POST",
            f"{GRAPH}/{self.page_id}/video_reels",
            data={"upload_phase": "start", "access_token": self._token},
        )
        data = _payload(resp, "Starting the Facebook Reel upload")
        video_id, upload_url = data.get("video_id"), data.get("upload_url")
        if not video_id or not upload_url:
            raise FacebookError(f"Facebook did not return a Reel upload session: {data}")

        self._upload(str(upload_url), video, what="the Facebook Page feed")

        finish = http_request(
            "POST",
            f"{GRAPH}/{self.page_id}/video_reels",
            data={
                "upload_phase": "finish",
                "video_id": str(video_id),
                "video_state": "PUBLISHED",
                "description": caption[:2000],
                "access_token": self._token,
            },
        )
        result = _payload(finish, "Publishing the Facebook Reel")
        if result.get("success") is False:
            raise FacebookError(f"Facebook reported failure publishing the Reel: {result}")

        self._wait(str(video_id), poll_sec=poll_sec, timeout_sec=timeout_sec)
        post_id = str(result.get("post_id", "") or video_id)
        LOG.info("Facebook Page Reel published. Id: %s", post_id)
        return post_id

    def _wait(self, video_id: str, *, poll_sec: int, timeout_sec: int) -> None:
        """Best effort - the story is already live once finish succeeds."""
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            try:
                resp = http_request(
                    "GET",
                    f"{GRAPH}/{video_id}",
                    params={"fields": "status", "access_token": self._token},
                    attempts=2,
                )
                status = resp.json().get("status", {})
            except Exception:
                return

            publish = (status.get("publishing_phase") or {}).get("status", "")
            processing = (status.get("processing_phase") or {}).get("status", "")
            if publish in ("complete", "published") or status.get("video_status") == "ready":
                return
            if publish == "error" or processing == "error":
                raise FacebookError(f"Facebook could not process the video: {status}")
            LOG.info("Facebook processing (%s/%s) - waiting %ds", processing or "?", publish or "?", poll_sec)
            time.sleep(poll_sec)
