"""Optional Discord webhook notifications. Silent no-op when unset."""

from __future__ import annotations

import os

from common import LOG, http_request

GOLD = 0xD9AE6B
RED = 0xE05252


def send(title: str, description: str, *, ok: bool = True) -> None:
    webhook = (os.environ.get("DISCORD_WEBHOOK") or "").strip()
    if not webhook:
        return
    try:
        http_request(
            "POST",
            webhook,
            attempts=2,
            json={
                "username": "MM Story Bot",
                "embeds": [
                    {
                        "title": title,
                        "description": description[:3800],
                        "color": GOLD if ok else RED,
                    }
                ],
            },
        )
    except Exception as exc:  # notification must never fail the run
        LOG.warning("Discord notification failed: %s", exc)
