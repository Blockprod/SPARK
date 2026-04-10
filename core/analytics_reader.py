"""YouTube Analytics API reader for shorts-engine.

Reads post-publication metrics (views, watch time, average view percentage)
for uploaded Shorts via the YouTube Analytics API v2.

Requires the OAuth2 token to include the additional scope:
  https://www.googleapis.com/auth/yt-analytics.readonly

The token file used is the same as the uploader (YOUTUBE_TOKEN_FILE env var).
Results are cached in logs/analytics_cache.jsonl to avoid redundant API calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

LOGGER = logging.getLogger(__name__)

_ANALYTICS_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
_CACHE_TTL_HOURS = 6


class AnalyticsError(RuntimeError):
    """Raised when analytics retrieval fails unrecoverably."""


class AnalyticsReader:
    """Reads YouTube Analytics metrics for a given video ID."""

    def __init__(self, token_file: Path, cache_path: Path) -> None:
        """Initialize the analytics reader.

        Args:
            token_file: Path to the OAuth2 token JSON file.
            cache_path: Path to the JSONL cache file for analytics results.
        """
        self.token_file = token_file
        self.cache_path = cache_path
        self._service: Any = None

    async def get_video_metrics(self, video_id: str) -> dict[str, Any]:
        """Return analytics metrics for a single video.

        Checks the local cache first. If the cache entry is absent or stale
        (older than _CACHE_TTL_HOURS), fetches fresh data from the API.

        Args:
            video_id: YouTube video ID (e.g. "dQw4w9WgXcQ").

        Returns:
            Dictionary with keys:
              - video_id (str)
              - views (int)
              - watch_time_minutes (float)
              - avg_view_percentage (float)
              - likes (int)
              - fetched_at (ISO-8601 UTC string)

        Raises:
            AnalyticsError: If the API call fails and no cache entry exists.
        """
        if not video_id or not video_id.strip():
            raise AnalyticsError("video_id must be a non-empty string.")

        cached = self._read_cache(video_id)
        if cached is not None:
            return cached

        result = await asyncio.to_thread(self._fetch_metrics_sync, video_id)
        self._write_cache(result)
        return result

    def _fetch_metrics_sync(self, video_id: str) -> dict[str, Any]:
        service = self._get_service()
        today = date.today()
        start_date = (today - timedelta(days=90)).isoformat()
        end_date = today.isoformat()

        try:
            response = (
                service.reports()
                .query(
                    ids="channel==MINE",
                    startDate=start_date,
                    endDate=end_date,
                    metrics="views,estimatedMinutesWatched,averageViewPercentage,likes",
                    filters=f"video=={video_id}",
                    dimensions="video",
                )
                .execute()
            )
        except HttpError as exc:
            raise AnalyticsError(
                f"YouTube Analytics API error for video {video_id}: {exc}"
            ) from exc
        except Exception as exc:
            raise AnalyticsError(
                f"Unexpected error fetching analytics for video {video_id}: {exc}"
            ) from exc

        rows = response.get("rows", [])
        if not rows:
            LOGGER.warning(
                "No analytics data returned for video %s (too recent or no views yet).",
                video_id,
            )
            return {
                "video_id": video_id,
                "views": 0,
                "watch_time_minutes": 0.0,
                "avg_view_percentage": 0.0,
                "likes": 0,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }

        row = rows[0]
        # columns: video, views, estimatedMinutesWatched, averageViewPercentage, likes
        return {
            "video_id": video_id,
            "views": int(row[1]) if len(row) > 1 else 0,
            "watch_time_minutes": float(row[2]) if len(row) > 2 else 0.0,
            "avg_view_percentage": float(row[3]) if len(row) > 3 else 0.0,
            "likes": int(row[4]) if len(row) > 4 else 0,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    def _get_service(self) -> Any:
        if self._service is not None:
            return self._service

        creds = self._load_credentials()
        try:
            self._service = build("youtubeAnalytics", "v2", credentials=creds)
        except Exception as exc:
            raise AnalyticsError(
                f"Failed to build YouTube Analytics service: {exc}"
            ) from exc
        return self._service

    def _load_credentials(self) -> Credentials:
        if not self.token_file.exists():
            raise AnalyticsError(
                f"OAuth2 token file not found: {self.token_file}. "
                "Run an upload first to generate the token with the required scopes."
            )
        try:
            creds = Credentials.from_authorized_user_file(
                str(self.token_file), _ANALYTICS_SCOPES
            )
        except Exception as exc:
            raise AnalyticsError(
                f"Could not load OAuth2 credentials from {self.token_file}: {exc}"
            ) from exc

        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:
                raise AnalyticsError(
                    f"Could not refresh OAuth2 token: {exc}"
                ) from exc

        if not creds.valid:
            raise AnalyticsError(
                "OAuth2 credentials are invalid or expired and could not be refreshed. "
                "Re-run the upload flow to generate a fresh token."
            )
        return creds

    def _read_cache(self, video_id: str) -> dict[str, Any] | None:
        if not self.cache_path.exists():
            return None

        cutoff = datetime.now(timezone.utc) - timedelta(hours=_CACHE_TTL_HOURS)
        try:
            with self.cache_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("video_id") != video_id:
                        continue
                    fetched_at_str = entry.get("fetched_at", "")
                    if not fetched_at_str:
                        continue
                    try:
                        fetched_at = datetime.fromisoformat(
                            fetched_at_str.replace("Z", "+00:00")
                        )
                    except ValueError:
                        continue
                    if fetched_at >= cutoff:
                        LOGGER.debug("Cache hit for video %s.", video_id)
                        return entry
        except Exception as exc:
            LOGGER.warning("Could not read analytics cache: %s", exc)
        return None

    def _write_cache(self, entry: dict[str, Any]) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with self.cache_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:
            LOGGER.warning("Could not write analytics cache: %s", exc)


# ---------------------------------------------------------------------------
# Public async entry point
# ---------------------------------------------------------------------------


async def get_video_metrics(
    config: dict[str, Any],
    env: dict[str, str],
    video_id: str,
) -> dict[str, Any]:
    """Public async helper to fetch analytics metrics for a video.

    Args:
        config: Global configuration mapping loaded from config.yaml.
        env: Environment mapping with credential paths.
        video_id: YouTube video ID to query.

    Returns:
        Metrics dictionary (views, watch_time_minutes, avg_view_percentage, likes).

    Raises:
        AnalyticsError: On API or credential failure.
    """
    token_file = Path(
        env.get("YOUTUBE_TOKEN_FILE", "./secrets/youtube_token.json")
    ).resolve()
    logs_dir = Path(
        str(config.get("paths", {}).get("logs_dir", "./logs"))
    ).resolve()
    cache_path = logs_dir / "analytics_cache.jsonl"

    reader = AnalyticsReader(token_file=token_file, cache_path=cache_path)
    return await reader.get_video_metrics(video_id=video_id)
