"""Automatic thumbnail generation and YouTube upload for shorts-engine.

Extracts the central frame of the second or third rendered clip, overlays
the video title with Pillow, and uploads the result via thumbnails.set().

Requirements: Pillow, opencv-python (cv2)
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


class ThumbnailError(RuntimeError):
    """Raised when thumbnail generation or upload fails."""


# ---------------------------------------------------------------------------
# Frame extraction
# ---------------------------------------------------------------------------


def _extract_central_frame(video_path: Path) -> "Any":
    """Extract the frame at the midpoint of a video file.

    Args:
        video_path: Path to an MP4 clip.

    Returns:
        OpenCV image array (BGR, uint8).

    Raises:
        ThumbnailError: If the file cannot be opened or has no frames.
    """
    try:
        import cv2  # type: ignore[import]
    except ImportError as exc:
        raise ThumbnailError(
            "opencv-python is required for thumbnail generation: pip install opencv-python"
        ) from exc

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise ThumbnailError(f"Cannot open video for thumbnail extraction: {video_path}")

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total_frames <= 0:
        cap.release()
        raise ThumbnailError(f"Video has no frames: {video_path}")

    mid = max(0, total_frames // 2)
    cap.set(cv2.CAP_PROP_POS_FRAMES, mid)
    ret, frame = cap.read()
    cap.release()

    if not ret or frame is None:
        raise ThumbnailError(
            f"Could not read frame {mid} from {video_path}. "
            "Falling back is handled by caller."
        )
    return frame


# ---------------------------------------------------------------------------
# Overlay
# ---------------------------------------------------------------------------

_THUMBNAIL_WIDTH = 1080
_THUMBNAIL_HEIGHT = 1920
_JPEG_QUALITY = 90


def _build_thumbnail_image(frame: "Any", title: str) -> "Any":
    """Resize frame to 1080×1920 and overlay title text with Pillow.

    Args:
        frame: OpenCV BGR image array.
        title: Title string to overlay (truncated to 80 chars).

    Returns:
        Pillow Image ready for JPEG export.

    Raises:
        ThumbnailError: If Pillow is not installed.
    """
    try:
        import cv2  # type: ignore[import]
        from PIL import Image, ImageDraw, ImageFont  # type: ignore[import]
    except ImportError as exc:
        raise ThumbnailError(
            "Pillow is required for thumbnail generation: pip install Pillow"
        ) from exc

    # Resize to 1080×1920 (keep aspect, fill black bars)
    h, w = frame.shape[:2]
    scale = min(_THUMBNAIL_WIDTH / w, _THUMBNAIL_HEIGHT / h)
    new_w = int(w * scale)
    new_h = int(h * scale)
    resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)

    canvas_bgr = __import__("numpy").zeros(
        (_THUMBNAIL_HEIGHT, _THUMBNAIL_WIDTH, 3), dtype=__import__("numpy").uint8
    )
    x_off = (_THUMBNAIL_WIDTH - new_w) // 2
    y_off = (_THUMBNAIL_HEIGHT - new_h) // 2
    canvas_bgr[y_off : y_off + new_h, x_off : x_off + new_w] = resized

    # Convert BGR → RGB for Pillow
    canvas_rgb = cv2.cvtColor(canvas_bgr, cv2.COLOR_BGR2RGB)
    img = Image.fromarray(canvas_rgb)

    title_text = title[:80]
    draw = ImageDraw.Draw(img)

    # Use default font — no external font file needed
    try:
        font = ImageFont.truetype("arial.ttf", size=72)
    except (IOError, OSError):
        font = ImageFont.load_default()

    # Semi-transparent black band at bottom
    band_top = _THUMBNAIL_HEIGHT - 220
    band_height = 200
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    band_draw = ImageDraw.Draw(overlay)
    band_draw.rectangle(
        [(0, band_top), (_THUMBNAIL_WIDTH, band_top + band_height)],
        fill=(0, 0, 0, 160),
    )
    img = img.convert("RGBA")
    img = Image.alpha_composite(img, overlay).convert("RGB")

    draw = ImageDraw.Draw(img)
    draw.text(
        (30, band_top + 20),
        title_text,
        fill=(255, 255, 255),
        font=font,
    )
    return img


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


def _upload_thumbnail_sync(
    service: "Any",
    video_id: str,
    jpeg_path: str,
) -> None:
    """Call thumbnails.set() on the YouTube Data API.

    Args:
        service: Built YouTube API service object.
        video_id: YouTube video ID.
        jpeg_path: Absolute path to the JPEG thumbnail file.

    Raises:
        ThumbnailError: If the API call fails.
    """
    from googleapiclient.http import MediaFileUpload  # type: ignore[import]
    from googleapiclient.errors import HttpError  # type: ignore[import]

    media = MediaFileUpload(jpeg_path, mimetype="image/jpeg")
    try:
        service.thumbnails().set(
            videoId=video_id,
            media_body=media,
        ).execute()
    except HttpError as exc:
        raise ThumbnailError(
            f"thumbnails.set() failed (HTTP {exc.resp.status}): {exc}"
        ) from exc
    except Exception as exc:
        raise ThumbnailError(f"thumbnails.set() unexpected error: {exc}") from exc


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def generate_and_upload_thumbnail(
    config: dict[str, Any],
    env: dict[str, str],
    clip_paths: list[Path],
    script_payload: dict[str, Any],
    video_id: str,
) -> str:
    """Generate a thumbnail from clips and upload it to YouTube.

    Attempts to use clip index 1 (scene 2) or index 2 (scene 3); falls back
    to index 0 if neither exists.  Returns the local JPEG path on success.

    Args:
        config: Global app config.
        env: Environment mapping with YouTube credential paths.
        clip_paths: Ordered list of rendered clip Paths.
        script_payload: Validated script package (provides title).
        video_id: YouTube video ID for thumbnails.set().

    Returns:
        Absolute path to the generated JPEG thumbnail.

    Raises:
        ThumbnailError: If no clips are available or all fallbacks fail.
    """
    if not clip_paths:
        raise ThumbnailError("No clip paths provided — cannot generate thumbnail.")

    # Pick source clip: prefer scene 2 (index 1), then scene 3 (index 2), else scene 1
    preferred_indices = [1, 2, 0]
    source_clip: Path | None = None
    for idx in preferred_indices:
        if idx < len(clip_paths):
            source_clip = clip_paths[idx]
            break

    if source_clip is None:
        raise ThumbnailError("clip_paths is empty after index resolution.")

    title = (
        str(script_payload.get("metadata", {}).get("youtube_title", ""))
        or str(script_payload.get("title", ""))
    ).strip()

    def _build_sync() -> str:
        frame = _extract_central_frame(source_clip)
        img = _build_thumbnail_image(frame, title)
        # Write to temp JPEG
        tmp = tempfile.NamedTemporaryFile(
            suffix=".jpg", delete=False, dir=str(source_clip.parent)
        )
        tmp.close()
        img.save(tmp.name, "JPEG", quality=_JPEG_QUALITY)
        return tmp.name

    try:
        jpeg_path = await asyncio.to_thread(_build_sync)
    except ThumbnailError:
        raise
    except Exception as exc:
        raise ThumbnailError(f"Thumbnail image build failed: {exc}") from exc

    LOGGER.info("Thumbnail generated: %s", jpeg_path)

    from core.uploader import get_youtube_service
    try:
        youtube_service = get_youtube_service(config, env)
    except Exception as exc:
        LOGGER.warning("Thumbnail upload skipped — could not build YouTube service: %s", exc)
        return jpeg_path

    try:
        await asyncio.to_thread(_upload_thumbnail_sync, youtube_service, video_id, jpeg_path)
        LOGGER.info("Thumbnail uploaded for video %s.", video_id)
    except ThumbnailError as exc:
        LOGGER.warning("Thumbnail upload failed (non-fatal): %s", exc)

    return jpeg_path
