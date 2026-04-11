"""TikTok Content Posting API v2 uploader for shorts-engine.

Uploads the final video to TikTok as a Creator Post using the FILE_UPLOAD
source type (chunked direct upload — no public URL required).

Required environment variables:
  TIKTOK_ACCESS_TOKEN   — OAuth2 user access token
  TIKTOK_OPEN_ID        — TikTok user open_id

Reference: https://developers.tiktok.com/doc/content-posting-api-post-video
"""

from __future__ import annotations

import json
import logging
import math
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
_CHUNK_SIZE = 10 * 1024 * 1024  # 10 MiB per chunk (TikTok minimum: 5 MiB)
_MAX_VIDEO_BYTES = 128 * 1024 * 1024  # 128 MiB TikTok limit for direct upload


class TikTokUploaderError(RuntimeError):
    """Raised when a TikTok upload operation fails unrecoverably."""


def _build_description(script_payload: dict[str, Any]) -> str:
    """Return a TikTok-safe caption (max 2200 chars, no links)."""
    meta = script_payload.get("metadata", {})
    title = str(script_payload.get("title", meta.get("youtube_title", "")))
    hook = str(script_payload.get("hook", ""))
    raw = f"{hook}\n\n{title}".strip() if hook else title
    # Append hashtags from tags (max 5 for TikTok SEO)
    tags = meta.get("youtube_tags", [])[:5]
    hashtags = " ".join(f"#{t.replace(' ', '')}" for t in tags if t.strip())
    caption = f"{raw}\n\n{hashtags}".strip() if hashtags else raw
    return caption[:2200]


def _json_post(url: str, payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json; charset=UTF-8",
            "Authorization": f"Bearer {access_token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_txt = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise TikTokUploaderError(
            f"TikTok API HTTP {exc.code} at {url}: {body_txt}"
        ) from exc
    except urllib.error.URLError as exc:
        raise TikTokUploaderError(f"TikTok API connection error: {exc.reason}") from exc


async def upload_to_tiktok(
    config: dict[str, Any],
    env: dict[str, str],
    video_path: Path,
    script_payload: dict[str, Any],
) -> dict[str, Any]:
    """Upload a video to TikTok using the Content Posting API v2.

    This function is async-friendly: it delegates blocking I/O to a thread
    via :func:`asyncio.to_thread`.

    Args:
        config: Global project config (not currently used, reserved for future
                per-platform overrides).
        env: Environment mapping — must contain TIKTOK_ACCESS_TOKEN.
        video_path: Path to the final MP4 to upload.
        script_payload: Script payload for metadata (title, description, tags).

    Returns:
        Dictionary with ``publish_id`` and ``platform``.

    Raises:
        TikTokUploaderError: On missing credentials, file issues, or API errors.
    """
    import asyncio

    return await asyncio.to_thread(
        _upload_sync, config=config, env=env,
        video_path=video_path, script_payload=script_payload,
    )


def _upload_sync(
    config: dict[str, Any],
    env: dict[str, str],
    video_path: Path,
    script_payload: dict[str, Any],
) -> dict[str, Any]:
    access_token = env.get("TIKTOK_ACCESS_TOKEN", "")
    if not access_token:
        raise TikTokUploaderError(
            "TIKTOK_ACCESS_TOKEN is absent — set it in .env to enable TikTok upload."
        )

    if not video_path.exists():
        raise TikTokUploaderError(f"Video file not found: {video_path}")

    total_bytes = video_path.stat().st_size
    if total_bytes > _MAX_VIDEO_BYTES:
        raise TikTokUploaderError(
            f"Video {video_path.name} is {total_bytes} bytes — exceeds TikTok 128 MiB limit."
        )

    chunk_size = min(_CHUNK_SIZE, total_bytes)
    chunk_count = math.ceil(total_bytes / chunk_size)
    description = _build_description(script_payload)

    # Step 1 — Initialize upload
    LOGGER.info("[tiktok] Initializing upload: %s (%d bytes, %d chunk(s))", video_path.name, total_bytes, chunk_count)
    init_payload = {
        "post_info": {
            "title": description,
            "privacy_level": "SELF_ONLY",  # safe default — user can change in TikTok app
            "disable_duet": False,
            "disable_comment": False,
            "disable_stitch": False,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": total_bytes,
            "chunk_size": chunk_size,
            "total_chunk_count": chunk_count,
        },
    }
    init_resp = _json_post(_INIT_URL, init_payload, access_token)
    error = init_resp.get("error", {})
    if error.get("code", "ok").lower() != "ok":
        raise TikTokUploaderError(
            f"TikTok init error {error.get('code')}: {error.get('message')}"
        )
    data = init_resp.get("data", {})
    publish_id: str = data.get("publish_id", "")
    upload_url: str = data.get("upload_url", "")

    if not publish_id or not upload_url:
        raise TikTokUploaderError(
            f"TikTok init response missing publish_id or upload_url: {init_resp}"
        )

    # Step 2 — Upload chunks
    LOGGER.info("[tiktok] Uploading chunks to %s…", upload_url)
    with video_path.open("rb") as fh:
        for chunk_index in range(chunk_count):
            chunk_data = fh.read(chunk_size)
            if not chunk_data:
                break
            start_byte = chunk_index * chunk_size
            end_byte = start_byte + len(chunk_data) - 1
            req = urllib.request.Request(
                upload_url,
                data=chunk_data,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Range": f"bytes {start_byte}-{end_byte}/{total_bytes}",
                    "Content-Length": str(len(chunk_data)),
                },
                method="PUT",
            )
            try:
                with urllib.request.urlopen(req, timeout=120):
                    pass
            except urllib.error.HTTPError as exc:
                if exc.code not in (200, 201, 206):
                    raise TikTokUploaderError(
                        f"TikTok chunk {chunk_index} upload failed: HTTP {exc.code}"
                    ) from exc
            LOGGER.debug("[tiktok] Chunk %d/%d uploaded.", chunk_index + 1, chunk_count)

    LOGGER.info("[tiktok] Upload complete. publish_id=%s", publish_id)
    return {"platform": "tiktok", "publish_id": publish_id}
