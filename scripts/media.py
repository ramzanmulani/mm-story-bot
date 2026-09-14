"""Download a post's video and normalise it to Instagram Story spec."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path

import requests

from common import LOG

STORY_SAFE_ASPECT = (0.5, 0.7)  # w/h that Instagram shows without letterboxing


class MediaError(RuntimeError):
    pass


def ensure_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise MediaError(
                f"{tool} is not installed. The workflow installs it on the runner; "
                f"locally run `sudo apt install ffmpeg`."
            )


def download(url: str, dest: Path, *, max_mb: int = 400) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    LOG.info("Downloading source video ...")
    with requests.get(url, stream=True, timeout=180) as resp:
        resp.raise_for_status()
        written = 0
        limit = max_mb * 1024 * 1024
        with dest.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                if not chunk:
                    continue
                written += len(chunk)
                if written > limit:
                    raise MediaError(f"Source video exceeds {max_mb} MB; skipping this clip.")
                fh.write(chunk)
    LOG.info("Downloaded %.1f MB", dest.stat().st_size / 1e6)
    return dest


def probe(path: Path) -> dict:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ],
        capture_output=True, text=True, check=False,
    )
    if out.returncode != 0:
        raise MediaError(f"ffprobe failed: {out.stderr.strip()[:300]}")
    data = json.loads(out.stdout or "{}")

    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise MediaError("No video stream found in the downloaded file.")
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)

    duration = _to_float(data.get("format", {}).get("duration")) or _to_float(video.get("duration")) or 0.0
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    if width <= 0 or height <= 0:
        raise MediaError("Could not read video dimensions.")

    return {
        "duration": duration,
        "width": width,
        "height": height,
        "aspect": width / height,
        "vcodec": video.get("codec_name", ""),
        "acodec": (audio or {}).get("codec_name", ""),
        "has_audio": audio is not None,
        "size_mb": path.stat().st_size / 1e6,
    }


def meets_story_spec(info: dict, cfg_video: dict) -> bool:
    """True when the untouched original can go straight to Instagram."""
    return (
        float(cfg_video.get("min_duration_sec", 3)) <= info["duration"] <= cfg_video["max_duration_sec"]
        and info["size_mb"] <= cfg_video["max_filesize_mb"]
        and info["vcodec"] in ("h264", "avc1")
        and (not info["has_audio"] or info["acodec"] in ("aac", "mp4a"))
        and STORY_SAFE_ASPECT[0] <= info["aspect"] <= STORY_SAFE_ASPECT[1]
    )


def normalise(src: Path, dest: Path, info: dict, cfg_video: dict) -> Path:
    """Re-encode to a guaranteed-valid 1080x1920 story clip."""
    w = int(cfg_video["width"])
    h = int(cfg_video["height"])
    max_dur = float(cfg_video["max_duration_sec"])
    fit = str(cfg_video.get("fit", "blur")).lower()

    min_dur = float(cfg_video.get("min_duration_sec", 3))

    # Facebook rejects stories under 3s, Instagram rejects over 60s.
    # in_args go before -i; out_trim after the inputs.
    in_args: list[str] = []
    out_trim: list[str] = []
    target_dur = info["duration"]

    if info["duration"] > max_dur:
        start_at = 0.0
        if str(cfg_video.get("trim_from", "start")).lower() == "middle":
            start_at = max(0.0, (info["duration"] - max_dur) / 2.0)
        in_args = ["-ss", f"{start_at:.2f}"]
        out_trim = ["-t", f"{max_dur:.2f}"]
        target_dur = max_dur
        LOG.info("Clip is %.1fs - trimming %.1fs from %.1fs.", info["duration"], max_dur, start_at)
    elif 0 < info["duration"] < min_dur:
        loops = int(min_dur // max(info["duration"], 0.1)) + 1
        in_args = ["-stream_loop", str(loops)]
        out_trim = ["-t", f"{min_dur:.2f}"]
        target_dur = min_dur
        LOG.info(
            "Clip is only %.1fs - looping it to %.1fs (Facebook rejects stories under %.0fs).",
            info["duration"], min_dur, min_dur,
        )

    vf = _build_filter(fit, w, h, cfg_video.get("pad_color", "#0E0E11"))

    cmd = _encode_cmd(
        src, dest, info, cfg_video, vf, in_args, out_trim, target_dur,
        vbitrate=str(cfg_video["video_bitrate"]),
        abitrate=str(cfg_video["audio_bitrate"]),
    )

    LOG.info("Normalising to %dx%d (%s fit) ...", w, h, fit)
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0 or not dest.exists():
        raise MediaError(f"ffmpeg failed: {result.stderr.strip()[-500:]}")

    size_mb = dest.stat().st_size / 1e6
    LOG.info("Normalised: %.1f MB", size_mb)
    if size_mb > cfg_video["max_filesize_mb"]:
        dest = _shrink(src, dest, info, cfg_video, vf, in_args, out_trim, target_dur)
    return dest


def _encode_cmd(
    src: Path, dest: Path, info: dict, cfg_video: dict, vf: str,
    in_args: list[str], out_trim: list[str], target_dur: float,
    *, vbitrate: str, abitrate: str,
) -> list[str]:
    """All inputs must be declared before any output option."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *in_args, "-i", str(src)]
    if not info["has_audio"]:
        # Instagram and Facebook are both happier with a silent track than none.
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]

    cmd += ["-filter_complex", vf, "-map", "[v]"]
    if info["has_audio"]:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", abitrate, "-ar", "48000", "-ac", "2"]
    else:
        cmd += ["-map", "1:a", "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]

    # anullsrc is an endless stream: without an explicit duration ffmpeg
    # would never stop. Always give the silent path something to end on.
    if not info["has_audio"] and "-t" not in out_trim:
        out_trim = [*out_trim, "-t", f"{max(target_dur, 0.5):.2f}"]

    cmd += [
        *out_trim,
        "-c:v", "libx264", "-profile:v", "high", "-level", "4.1",
        "-pix_fmt", "yuv420p", "-r", str(cfg_video["fps"]),
        "-b:v", vbitrate, "-maxrate", vbitrate, "-bufsize", "9000k",
        "-preset", "veryfast", "-movflags", "+faststart", str(dest),
    ]
    return cmd


def _build_filter(fit: str, w: int, h: int, pad_color: str) -> str:
    """One filter_complex string producing a [v] output at exactly w x h."""
    if fit == "crop":
        return (
            f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},setsar=1[v]"
        )
    if fit == "pad":
        colour = pad_color if pad_color.startswith("#") else f"#{pad_color}"
        return (
            f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:{colour},setsar=1[v]"
        )
    # blur (default): the clip centred over a blurred, darkened copy of itself
    return (
        f"[0:v]split=2[bg][fg];"
        f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
        f"boxblur=luma_radius=40:luma_power=2,eq=brightness=-0.18[bgb];"
        f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1[v]"
    )


def _shrink(
    src: Path, dest: Path, info: dict, cfg_video: dict, vf: str,
    in_args: list[str], out_trim: list[str], target_dur: float,
) -> Path:
    """Second pass at a lower bitrate when the first pass overshot the cap."""
    target_mb = float(cfg_video["max_filesize_mb"]) * 0.85
    duration = max(target_dur, 1.0)
    kbps = max(1200, int((target_mb * 8192) / duration) - 128)
    LOG.warning("Over the size cap - re-encoding at %dk.", kbps)

    shrunk = dest.with_name(dest.stem + "_s.mp4")
    cmd = _encode_cmd(src, shrunk, info, cfg_video, vf, in_args, out_trim, target_dur,
                      vbitrate=f"{kbps}k", abitrate="96k")
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0 or not shrunk.exists():
        raise MediaError(f"ffmpeg shrink pass failed: {result.stderr.strip()[-400:]}")
    if shrunk.stat().st_size / 1e6 > cfg_video["max_filesize_mb"]:
        raise MediaError("Clip is still over the story size cap after two passes.")
    dest.unlink(missing_ok=True)
    return shrunk


def _to_float(value) -> float:
    try:
        out = float(value)
        return 0.0 if math.isnan(out) or math.isinf(out) else out
    except (TypeError, ValueError):
        return 0.0
