"""Instagram Graph API Reels uploader for shorts-engine.

Publishes the final video to Instagram as a Reel using the two-step container
+ publish flow.

Step 1 — Create a media container (POST /{ig-user-id}/media).
Step 2 — Publish the container  (POST /{ig-user-id}/media_publish).

Required environment variables:
  INSTAGRAM_ACCESS_TOKEN    — Facebook Graph API long-lived token
  INSTAGRAM_USER_ID         — Instagram Business / Creator Account user ID
  INSTAGRAM_VIDEO_HOST_URL  — Base HTTPS URL where the MP4 is publicly reachable
                              (e.g. https://static.myserver.com/outputs/).
                              Instagram must be able to fetch the file by URL.

When INSTAGRAM_VIDEO_HOST_URL is absent, the upload is skipped with a warning.

Reference:
  https://developers.facebook.com/docs/instagram-api/guides/reels
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

_GRAPH_BASE = "https://graph.instagram.com/v21.0"
_POLL_INTERVAL_SEC = 5
_POLL_MAX_ATTEMPTS = 24  # 2 minutes total


class InstagramUploaderError(RuntimeError):
    """Raised when an Instagram upload operation fails unrecoverably."""


def _graph_post(url: str, params: dict[str, str]) -> dict[str, Any]:
    body = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_txt = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise InstagramUploaderError(
            f"Instagram API HTTP {exc.code} at {url}: {body_txt}"
        ) from exc
    except urllib.error.URLError as exc:
        raise InstagramUploaderError(
            f"Instagram API connection error: {exc.reason}"
        ) from exc


def _graph_get(url: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_txt = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise InstagramUploaderError(
            f"Instagram API HTTP {exc.code} at {url}: {body_txt}"
        ) from exc
    except urllib.error.URLError as exc:
        raise InstagramUploaderError(
            f"Instagram API connection error: {exc.reason}"
        ) from exc


def _build_caption(script_payload: dict[str, Any]) -> str:
    """Return an Instagram-safe caption (max 2200 chars)."""
    meta = script_payload.get("metadata", {})
    hook = str(script_payload.get("hook", ""))
    desc = str(meta.get("youtube_description", ""))
    tags = meta.get("youtube_tags", [])[:15]
    hashtags = " ".join(f"#{t.replace(' ', '')}" for t in tags if t.strip())
    caption = f"{hook}\n\n{desc}\n\n{hashtags}".strip()
    return caption[:2200]


async def upload_to_instagram(
    config: dict[str, Any],
    env: dict[str, str],
    video_path: Path,
    script_payload: dict[str, Any],
) -> dict[str, Any]:
    """Publish a Reel to Instagram via the Graph API.

    Args:
        config: Global project config (reserved).
        env: Environment mapping — must contain INSTAGRAM_ACCESS_TOKEN,
             INSTAGRAM_USER_ID, and INSTAGRAM_VIDEO_HOST_URL.
        video_path: Local path to the final MP4.
        script_payload: Script payload for caption and tags.

    Returns:
        Dictionary with ``media_id`` and ``platform``.

    Raises:
        InstagramUploaderError: On missing credentials or API errors.
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
    access_token = env.get("INSTAGRAM_ACCESS_TOKEN", "")
    user_id = env.get("INSTAGRAM_USER_ID", "")
    video_host_url = env.get("INSTAGRAM_VIDEO_HOST_URL", "").rstrip("/")

    if not access_token or not user_id:
        raise InstagramUploaderError(
            "INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_USER_ID are required for Instagram upload."
        )
    if not video_host_url:
        raise InstagramUploaderError(
            "INSTAGRAM_VIDEO_HOST_URL is absent — Instagram requires a publicly reachable video URL. "
            "Set INSTAGRAM_VIDEO_HOST_URL in .env to a hosting base URL (e.g. https://cdn.myhost.com/videos)."
        )

    video_url = f"{video_host_url}/{video_path.name}"
    caption = _build_caption(script_payload)

    # Step 1 — Create Reel container
    LOGGER.info("[instagram] Creating Reel container for %s…", video_path.name)
    container_url = f"{_GRAPH_BASE}/{user_id}/media"
    container_resp = _graph_post(container_url, {
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption,
        "share_to_feed": "true",
        "access_token": access_token,
    })
    creation_id = container_resp.get("id", "")
    if not creation_id:
        raise InstagramUploaderError(
            f"Instagram media container creation failed, no id in response: {container_resp}"
        )
    LOGGER.info("[instagram] Container created: %s", creation_id)

    # Step 2 — Wait for container to finish processing
    status_url = (
        f"{_GRAPH_BASE}/{creation_id}"
        f"?fields=status_code&access_token={urllib.parse.quote(access_token)}"
    )
    for attempt in range(1, _POLL_MAX_ATTEMPTS + 1):
        status_data = _graph_get(status_url)
        status_code = status_data.get("status_code", "")
        LOGGER.debug("[instagram] Container %s status: %s (attempt %d)", creation_id, status_code, attempt)
        if status_code == "FINISHED":
            break
        if status_code == "ERROR":
            raise InstagramUploaderError(
                f"Instagram container {creation_id} processing failed: {status_data}"
            )
        if attempt == _POLL_MAX_ATTEMPTS:
            raise InstagramUploaderError(
                f"Instagram container {creation_id} did not finish processing within "
                f"{_POLL_MAX_ATTEMPTS * _POLL_INTERVAL_SEC}s."
            )
        time.sleep(_POLL_INTERVAL_SEC)

    # Step 3 — Publish the container
    publish_url = f"{_GRAPH_BASE}/{user_id}/media_publish"
    publish_resp = _graph_post(publish_url, {
        "creation_id": creation_id,
        "access_token": access_token,
    })
    media_id = publish_resp.get("id", "")
    if not media_id:
        raise InstagramUploaderError(
            f"Instagram publish failed, no media id in response: {publish_resp}"
        )

    LOGGER.info("[instagram] Reel published. media_id=%s", media_id)
    return {"platform": "instagram", "media_id": media_id}
