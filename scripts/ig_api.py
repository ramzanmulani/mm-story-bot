"""Instagram Graph API client - library fetch and Story publishing."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from common import LOG, ConfigError, http_request

GRAPH_VERSION = "v21.0"
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"

MEDIA_FIELDS = (
    "id,media_type,media_product_type,media_url,thumbnail_url,"
    "permalink,caption,timestamp"
)


@dataclass(frozen=True)
class Media:
    id: str
    media_type: str
    product_type: str
    media_url: str
    permalink: str
    caption: str
    timestamp: str

    @property
    def short_caption(self) -> str:
        first = (self.caption or "").strip().splitlines()
        return (first[0][:70] + "...") if first and len(first[0]) > 70 else (first[0] if first else "")


class InstagramError(RuntimeError):
    """Instagram returned an error we cannot recover from inside one run."""


def _raise_for_graph_error(resp: Any, what: str) -> dict:
    try:
        payload = resp.json()
    except ValueError:
        raise InstagramError(f"{what}: non-JSON response ({resp.status_code}): {resp.text[:300]}")

    if resp.status_code >= 400 or "error" in payload:
        err = payload.get("error", {})
        msg = err.get("message", resp.text[:300])
        code = err.get("code")
        sub = err.get("error_subcode")
        hint = ""
        if code in (190, 102):
            hint = (
                "  -> Your IG_ACCESS_TOKEN has expired or been invalidated. "
                "Generate a new long-lived token and update the secret."
            )
        elif code == 200:
            hint = (
                "  -> The token is missing the instagram_content_publish "
                "permission, or the account is not a Business/Creator account."
            )
        elif code == 4 or code == 17:
            hint = "  -> Rate limited by Instagram. This run will be skipped; the next slot will retry."
        raise InstagramError(f"{what}: [{code}/{sub}] {msg}{hint}")
    return payload


class InstagramClient:
    def __init__(self, ig_user_id: str, access_token: str) -> None:
        if not ig_user_id.isdigit():
            raise ConfigError("IG_USER_ID must be the numeric Instagram Business account id.")
        self.ig_user_id = ig_user_id
        self._token = access_token

    # -- library ----------------------------------------------------------

    def fetch_library(
        self,
        *,
        media_types: Iterable[str],
        product_types: Iterable[str],
        max_age_days: int,
        max_items: int,
        exclude_ids: Iterable[str],
    ) -> list[Media]:
        """Page through /me/media and return eligible videos, newest first."""
        wanted_media = {m.upper() for m in media_types}
        wanted_product = {p.upper() for p in product_types}
        excluded = {str(i) for i in exclude_ids}
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max_age_days)
            if max_age_days and max_age_days > 0
            else None
        )

        url = f"{GRAPH}/{self.ig_user_id}/media"
        params = {"fields": MEDIA_FIELDS, "limit": 50, "access_token": self._token}
        out: list[Media] = []
        seen_pages = 0

        while url and len(out) < max_items and seen_pages < 20:
            resp = http_request("GET", url, params=params)
            payload = _raise_for_graph_error(resp, "Fetching your media library")
            params = None  # subsequent 'next' urls already carry everything

            for item in payload.get("data", []):
                media = self._to_media(item)
                if media is None:
                    continue
                if media.id in excluded:
                    continue
                if wanted_media and media.media_type not in wanted_media:
                    continue
                if wanted_product and media.product_type and media.product_type not in wanted_product:
                    continue
                if cutoff and _parse_ts(media.timestamp) and _parse_ts(media.timestamp) < cutoff:
                    continue
                out.append(media)
                if len(out) >= max_items:
                    break

            url = payload.get("paging", {}).get("next")
            seen_pages += 1

        LOG.info("Library: %d eligible video post(s) found on the account.", len(out))
        return out

    @staticmethod
    def _to_media(item: dict) -> Media | None:
        media_url = item.get("media_url")
        if not media_url:
            # Carousels and some copyright-restricted items have no media_url.
            return None
        return Media(
            id=str(item.get("id", "")),
            media_type=str(item.get("media_type", "")).upper(),
            product_type=str(item.get("media_product_type", "")).upper(),
            media_url=media_url,
            permalink=item.get("permalink", ""),
            caption=item.get("caption", "") or "",
            timestamp=item.get("timestamp", ""),
        )

    # -- publishing -------------------------------------------------------

    def create_story_container(self, video_url: str) -> str:
        resp = http_request(
            "POST",
            f"{GRAPH}/{self.ig_user_id}/media",
            data={
                "media_type": "STORIES",
                "video_url": video_url,
                "access_token": self._token,
            },
        )
        payload = _raise_for_graph_error(resp, "Creating the story container")
        container_id = payload.get("id")
        if not container_id:
            raise InstagramError(f"Instagram did not return a container id: {payload}")
        LOG.info("Container created: %s", container_id)
        return str(container_id)

    def wait_for_container(self, container_id: str, *, poll_sec: int, timeout_sec: int) -> None:
        deadline = time.time() + timeout_sec
        last_status = "UNKNOWN"
        while time.time() < deadline:
            resp = http_request(
                "GET",
                f"{GRAPH}/{container_id}",
                params={"fields": "status_code,status", "access_token": self._token},
            )
            payload = _raise_for_graph_error(resp, "Checking container status")
            last_status = payload.get("status_code", "UNKNOWN")

            if last_status == "FINISHED":
                LOG.info("Container %s is ready.", container_id)
                return
            if last_status in ("ERROR", "EXPIRED"):
                raise InstagramError(
                    f"Instagram could not process the video "
                    f"(status={last_status}): {payload.get('status', '')}"
                )
            LOG.info("Container %s: %s - waiting %ds", container_id, last_status, poll_sec)
            time.sleep(poll_sec)

        raise InstagramError(
            f"Container {container_id} stuck at {last_status} after {timeout_sec}s."
        )

    def publish(self, container_id: str) -> str:
        resp = http_request(
            "POST",
            f"{GRAPH}/{self.ig_user_id}/media_publish",
            data={"creation_id": container_id, "access_token": self._token},
        )
        payload = _raise_for_graph_error(resp, "Publishing the story")
        story_id = str(payload.get("id", ""))
        LOG.info("Story published. Media id: %s", story_id)
        return story_id

    def token_expiry(self) -> tuple[str, float | None]:
        """(description, days_left) for the token in use.

        A Meta token is allowed to debug itself, so this needs no app secret
        and no second credential. expires_at == 0 means the token never
        expires - which is what a Page access token minted from a long-lived
        user token gives you.
        """
        try:
            resp = http_request(
                "GET",
                f"{GRAPH}/debug_token",
                params={"input_token": self._token, "access_token": self._token},
                attempts=2,
            )
            data = resp.json().get("data", {})
        except Exception:
            return ("unknown", None)

        if not data:
            return ("unknown", None)

        expires_at = data.get("expires_at")
        token_type = str(data.get("type", "")).lower()
        if expires_at in (0, None):
            return (f"never ({token_type or 'token'})", None)

        try:
            when = datetime.fromtimestamp(int(expires_at), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return ("unknown", None)

        days = (when - datetime.now(timezone.utc)).total_seconds() / 86400
        return (f"{when:%Y-%m-%d %H:%M UTC} ({token_type or 'token'})", days)

    def publishing_quota(self) -> tuple[int, int]:
        """(used, total) API-published posts in the rolling 24h window."""
        try:
            resp = http_request(
                "GET",
                f"{GRAPH}/{self.ig_user_id}/content_publishing_limit",
                params={"fields": "quota_usage,config", "access_token": self._token},
                attempts=2,
            )
            data = resp.json().get("data", [{}])[0]
            return int(data.get("quota_usage", 0)), int(data.get("config", {}).get("quota_total", 100))
        except Exception:  # quota check is advisory only
            return 0, 100


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None
