"""Shuffle-bag rotation.

Guarantees: every eligible video is posted once before any of them is posted
a second time, and a clip from the last `no_repeat_window` posts is never
picked at the moment the bag is refilled.

State lives in state/history.json and is committed back to main after each
successful run, so the rotation survives across GitHub Actions runs.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common import LOG, read_json, write_json

STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "history.json"

_EMPTY: dict[str, Any] = {"bag": [], "recent": [], "posted": [], "cycles": 0}


def load_state(path: Path = STATE_PATH) -> dict[str, Any]:
    state = read_json(path, dict(_EMPTY))
    for key, default in _EMPTY.items():
        state.setdefault(key, default if not isinstance(default, list) else [])
    return state


def save_state(state: dict[str, Any], path: Path = STATE_PATH) -> None:
    # Keep the log readable and the file small.
    state["posted"] = state.get("posted", [])[-300:]
    write_json(path, state)


def pick(state: dict[str, Any], library_ids: list[str], *, no_repeat_window: int) -> str | None:
    """Return the next media id to post, mutating `state` in place.

    `library_ids` is newest-first and is the single source of truth about
    what still exists on the account - anything deleted on Instagram silently
    drops out of the rotation here.
    """
    if not library_ids:
        return None

    valid = set(library_ids)

    # Drop ids that no longer exist on the account (deleted / archived posts).
    bag = [mid for mid in state.get("bag", []) if mid in valid]
    recent = [mid for mid in state.get("recent", []) if mid in valid]

    if not bag:
        bag = _refill(library_ids, recent, no_repeat_window)
        state["cycles"] = int(state.get("cycles", 0)) + 1
        LOG.info(
            "Rotation cycle %d starting - %d clip(s) in the new bag.",
            state["cycles"], len(bag),
        )

    chosen = bag.pop()

    recent.append(chosen)
    state["bag"] = bag
    state["recent"] = recent[-max(no_repeat_window, 1) :]
    LOG.info("Picked %s  (%d left in this cycle)", chosen, len(bag))
    return chosen


def _refill(library_ids: list[str], recent: list[str], no_repeat_window: int) -> list[str]:
    """Fresh shuffled bag, arranged so the last-posted clips come out last."""
    pool = list(library_ids)
    random.shuffle(pool)

    if no_repeat_window > 0 and recent:
        blocked = set(recent[-no_repeat_window:])
        # Only hold clips back if doing so still leaves something to post.
        if len(pool) > len(blocked):
            head = [m for m in pool if m not in blocked]
            tail = [m for m in pool if m in blocked]
            # pop() takes from the end, so put the blocked ones at the front.
            pool = tail + head
    return pool


def record_posted(
    state: dict[str, Any],
    *,
    media_id: str,
    story_id: str,
    permalink: str,
    facebook_post_id: str = "",
    facebook_feed_id: str = "",
) -> None:
    entry = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "media_id": media_id,
        "permalink": permalink,
    }
    if story_id:
        entry["instagram_story_id"] = story_id
    if facebook_post_id:
        entry["facebook_post_id"] = facebook_post_id
    if facebook_feed_id:
        entry["facebook_feed_id"] = facebook_feed_id
    state.setdefault("posted", []).append(entry)


def feed_posts_in_last_24h(state: dict[str, Any]) -> int:
    """How many Page Reels went out in the rolling day."""
    return _count_last_24h(state, key="facebook_feed_id")


def posted_in_last_24h(state: dict[str, Any]) -> int:
    return _count_last_24h(state)


def _count_last_24h(state: dict[str, Any], *, key: str | None = None) -> int:
    now = datetime.now(timezone.utc)
    count = 0
    for entry in reversed(state.get("posted", [])):
        try:
            when = datetime.fromisoformat(entry["at"])
        except (KeyError, ValueError):
            continue
        if (now - when).total_seconds() > 86400:
            break
        if key is None or entry.get(key):
            count += 1
    return count
