"""Shared helpers: config loading, logging, HTTP with retry."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

_LOG_FORMAT = "%(asctime)s  %(levelname)-7s  %(message)s"


def get_logger(name: str = "mm-story-bot") -> logging.Logger:
    log = logging.getLogger(name)
    if not log.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt="%H:%M:%S"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False
    return log


LOG = get_logger()


class ConfigError(RuntimeError):
    """Raised when configuration or environment is unusable."""


def load_config(path: Path | None = None) -> dict[str, Any]:
    path = path or (REPO_ROOT / "config.yaml")
    if not path.exists():
        raise ConfigError(f"config.yaml not found at {path}")
    with path.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}

    cfg.setdefault("account", {})
    origin = str(cfg["account"].get("pages_origin", "") or "").strip()

    # Blank is the normal case: derive it from the repo we are running in, so
    # there is no URL to mistype and no step to forget after forking.
    if not origin:
        origin = _derive_pages_origin()

    if not origin.startswith("https://"):
        raise ConfigError(
            "Could not work out your GitHub Pages URL. Either run inside GitHub "
            "Actions, or set account.pages_origin in config.yaml to "
            "https://<user>.github.io/<repo>"
        )
    cfg["account"]["pages_origin"] = origin.rstrip("/")
    return cfg


def _derive_pages_origin() -> str:
    slug = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
    if "/" not in slug:
        return ""
    owner, repo = slug.split("/", 1)
    return f"https://{owner.lower()}.github.io/{repo}"


def require_env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise ConfigError(
            f"Missing required secret {name}. Add it under "
            f"Settings -> Secrets and variables -> Actions."
        )
    return value


def env_flag(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def http_request(
    method: str,
    url: str,
    *,
    attempts: int = 4,
    backoff: float = 3.0,
    timeout: int = 60,
    **kwargs: Any,
) -> requests.Response:
    """HTTP with bounded retry on transport errors and 5xx / 429 only.

    4xx (other than 429) are returned as-is: those are our bug or a bad
    token, and retrying them just burns the rate limit.
    """
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.request(method, url, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            last_error = exc
            if attempt == attempts:
                break
            LOG.warning("%s %s failed (%s); retry %d/%d", method, _safe(url), exc, attempt, attempts)
            time.sleep(backoff * attempt)
            continue

        if resp.status_code < 500 and resp.status_code != 429:
            return resp

        last_error = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        if attempt == attempts:
            return resp
        LOG.warning(
            "%s %s -> %d; retry %d/%d",
            method, _safe(url), resp.status_code, attempt, attempts,
        )
        time.sleep(backoff * attempt)

    raise RuntimeError(f"{method} {_safe(url)} failed after {attempts} attempts: {last_error}")


def _safe(url: str) -> str:
    """Strip query strings so access tokens never reach the log."""
    return url.split("?", 1)[0]


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        LOG.warning("Could not read %s (%s); starting from defaults.", path.name, exc)
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(path)
