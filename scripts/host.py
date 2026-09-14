"""Publish one normalised video to a public URL via GitHub Pages.

Instagram will not accept a file upload - it fetches the video from a public
https url. The `gh-pages` branch is rewritten as a single orphan commit on
every run, so the repository never grows no matter how many months this runs.
Files older than `retention_hours` are dropped at the same time.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common import LOG, http_request, read_json, write_json

BRANCH = "gh-pages"
MANIFEST = "manifest.json"


class HostError(RuntimeError):
    pass


def _git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if check and result.returncode != 0:
        raise HostError(f"git {' '.join(args)} failed: {result.stderr.strip()[:400]}")
    return result


def publish_file(
    local_file: Path,
    *,
    repo_slug: str,
    token: str,
    pages_origin: str,
    retention_hours: int,
    wait_sec: int,
    actor: str = "mm-story-bot",
) -> str:
    """Push `local_file` to gh-pages and return the public url once it serves."""
    remote = f"https://x-access-token:{token}@github.com/{repo_slug}.git"
    filename = local_file.name

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "pages"
        work.mkdir()
        _git("init", "-q", cwd=work)
        _git("config", "user.name", actor, cwd=work)
        _git("config", "user.email", f"{actor}@users.noreply.github.com", cwd=work)
        _git("remote", "add", "origin", remote, cwd=work)

        fetched = _git("fetch", "--depth", "1", "origin", BRANCH, cwd=work, check=False)
        if fetched.returncode == 0:
            _git("checkout", "-q", "FETCH_HEAD", cwd=work, check=False)
            LOG.info("Existing %s branch pulled.", BRANCH)
        else:
            LOG.info("No %s branch yet - creating it.", BRANCH)

        manifest_path = work / MANIFEST
        manifest: dict[str, str] = read_json(manifest_path, {})
        manifest = _prune(work, manifest, retention_hours)

        (work / filename).write_bytes(local_file.read_bytes())
        manifest[filename] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        write_json(manifest_path, manifest)
        (work / ".nojekyll").write_text("", encoding="utf-8")
        (work / "index.html").write_text(_INDEX_HTML, encoding="utf-8")

        # Single-commit history: the branch is replaced, never appended to.
        _git("checkout", "-q", "--orphan", "publish", cwd=work)
        _git("add", "-A", cwd=work)
        _git("-c", "commit.gpgsign=false", "commit", "-q", "-m",
             f"media {filename}", cwd=work)
        _git("push", "-q", "--force", "origin", f"publish:{BRANCH}", cwd=work)
        LOG.info("Pushed %s to %s.", filename, BRANCH)

    url = f"{pages_origin.rstrip('/')}/{filename}"
    _wait_until_live(url, wait_sec)
    return url


def _prune(work: Path, manifest: dict[str, str], retention_hours: int) -> dict[str, str]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(retention_hours, 1))
    kept: dict[str, str] = {}
    for name, stamp in manifest.items():
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
            when = cutoff - timedelta(seconds=1)
        if when >= cutoff and (work / name).exists():
            kept[name] = stamp
        else:
            (work / name).unlink(missing_ok=True)

    # Anything on the branch that the manifest forgot about goes too.
    for stray in work.glob("*.mp4"):
        if stray.name not in kept:
            stray.unlink(missing_ok=True)

    dropped = len(manifest) - len(kept)
    if dropped > 0:
        LOG.info("Pruned %d expired file(s) from %s.", dropped, BRANCH)
    return kept


def _wait_until_live(url: str, wait_sec: int) -> None:
    LOG.info("Waiting for GitHub Pages to serve the file ...")
    deadline = time.time() + wait_sec
    delay = 5
    while time.time() < deadline:
        try:
            resp = http_request("HEAD", url, attempts=1, timeout=20, allow_redirects=True)
            if resp.status_code == 200:
                LOG.info("Live: %s", url)
                return
            status = resp.status_code
        except Exception as exc:  # network blip - keep waiting
            status = f"error ({exc})"
        LOG.info("Not live yet (%s); retrying in %ds", status, delay)
        time.sleep(delay)
        delay = min(delay + 5, 20)

    raise HostError(
        f"GitHub Pages did not serve {url} within {wait_sec}s.\n"
        f"  -> Check Settings -> Pages: source must be 'Deploy from a branch', "
        f"branch '{BRANCH}', folder '/ (root)', and the repository must be public."
    )


def repo_slug() -> str:
    slug = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not slug or "/" not in slug:
        raise HostError(
            "GITHUB_REPOSITORY is not set. Running outside Actions? "
            "Export it as <user>/<repo> first."
        )
    return slug


_INDEX_HTML = (
    "<!doctype html><meta charset=utf-8><title>mm-story-bot media</title>"
    "<style>body{background:#0E0E11;color:#D9AE6B;font:14px system-ui;"
    "display:grid;place-items:center;height:100vh;margin:0}</style>"
    "<p>Temporary media host for mm-story-bot.</p>"
)
