"""YouTube Data API v3 uploader for shorts-engine.

Handles OAuth2 token management, resumable video upload,
automatic metadata population from the script package,
and optional scheduled publishing via publishAt.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from cryptography.fernet import Fernet, InvalidToken as _FernetInvalidToken
    _FERNET_AVAILABLE = True
except ImportError:  # cryptography not installed — encryption disabled
    _FERNET_AVAILABLE = False
    _FernetInvalidToken = Exception  # type: ignore[assignment,misc]

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

LOGGER = logging.getLogger(__name__)

YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
_UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB resumable chunks


class UploaderError(RuntimeError):
    """Raised when upload preparation or execution fails."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class UploaderConfig:
    """Configuration for the YouTube uploader stage."""

    client_secrets_file: Path
    token_file: Path
    channel_id: str
    category_id: int
    default_privacy_status: str
    notify_subscribers: bool
    default_tags: list[str] = field(default_factory=list)

    @classmethod
    def from_mapping(
        cls, config: dict[str, Any], env: dict[str, str]
    ) -> "UploaderConfig":
        """Build UploaderConfig from global app config and environment.

        Args:
            config: Parsed config.yaml mapping.
            env: Environment mapping with YouTube credential paths.

        Returns:
            Validated UploaderConfig instance.

        Raises:
            UploaderError: If required config sections are missing.
        """
        uploader_cfg = config.get("uploader")
        if not isinstance(uploader_cfg, dict):
            raise UploaderError("Missing 'uploader' in configuration.")

        secrets_path = Path(
            env.get(
                "YOUTUBE_CLIENT_SECRETS_FILE",
                "./secrets/client_secret.json",
            )
        ).resolve()
        token_path = Path(
            env.get("YOUTUBE_TOKEN_FILE", "./secrets/youtube_token.json")
        ).resolve()
        channel_id = env.get("YOUTUBE_CHANNEL_ID", "")
        category_id = int(
            env.get("YOUTUBE_CATEGORY_ID", str(uploader_cfg.get("category_id", 27)))
        )
        privacy = env.get(
            "YOUTUBE_DEFAULT_PRIVACY",
            str(uploader_cfg.get("default_privacy_status", "private")),
        ).lower()
        if privacy not in {"private", "unlisted", "public"}:
            raise UploaderError(
                f"Invalid YOUTUBE_DEFAULT_PRIVACY value '{privacy}'. "
                "Must be private, unlisted, or public."
            )
        notify = env.get(
            "YOUTUBE_NOTIFY_SUBSCRIBERS",
            str(uploader_cfg.get("notify_subscribers", False)),
        ).lower() in {"true", "1", "yes"}

        raw_tags = uploader_cfg.get("default_tags", [])
        default_tags = [str(t) for t in raw_tags] if isinstance(raw_tags, list) else []

        return cls(
            client_secrets_file=secrets_path,
            token_file=token_path,
            channel_id=channel_id,
            category_id=category_id,
            default_privacy_status=privacy,
            notify_subscribers=notify,
            default_tags=default_tags,
        )


# ---------------------------------------------------------------------------
# YouTube uploader
# ---------------------------------------------------------------------------


class YouTubeUploader:
    """Manages OAuth2 tokens and uploads videos via YouTube Data API v3."""

    def __init__(self, cfg: UploaderConfig) -> None:
        """Initialize uploader.

        Args:
            cfg: Validated uploader configuration.
        """
        self.cfg = cfg
        self._service: Any = None

    async def upload(
        self,
        video_path: Path,
        script_payload: dict[str, Any],
        publish_at: datetime | None = None,
        privacy_override: str | None = None,
    ) -> dict[str, Any]:
        """Upload a video to YouTube and return the API response.

        Args:
            video_path: Absolute path to the final MP4 file.
            script_payload: Validated script package (provides metadata).
            publish_at: Optional UTC datetime for scheduled publishing.
                        Forces privacy=scheduled when provided.
            privacy_override: Override default privacy status for this upload.

        Returns:
            YouTube API insert response dict containing at least 'id' and 'status'.

        Raises:
            UploaderError: On credential, file, or API error.
        """
        if not video_path.exists():
            raise UploaderError(f"Video file not found: {video_path}")

        metadata = _build_video_metadata(
            script_payload=script_payload,
            default_tags=self.cfg.default_tags,
            category_id=self.cfg.category_id,
            privacy_status=privacy_override or self.cfg.default_privacy_status,
            notify_subscribers=self.cfg.notify_subscribers,
            publish_at=publish_at,
        )

        LOGGER.info(
            "Uploading '%s' to YouTube (privacy=%s, scheduled=%s)…",
            video_path.name,
            metadata["status"]["privacyStatus"],
            publish_at.isoformat() if publish_at else "none",
        )

        return await asyncio.to_thread(
            self._upload_sync, video_path, metadata
        )

    def _upload_sync(
        self, video_path: Path, metadata: dict[str, Any]
    ) -> dict[str, Any]:
        service = self._get_service()

        media = MediaFileUpload(
            str(video_path),
            mimetype="video/mp4",
            resumable=True,
            chunksize=_UPLOAD_CHUNK_SIZE,
        )

        request = service.videos().insert(
            part="snippet,status",
            body=metadata,
            media_body=media,
            notifySubscribers=self.cfg.notify_subscribers,
        )

        response: dict[str, Any] | None = None
        while response is None:
            try:
                status, response = request.next_chunk()
                if status:
                    progress_pct = int(status.progress() * 100)
                    LOGGER.debug("Upload progress: %d%%", progress_pct)
            except HttpError as exc:
                if exc.resp.status in {500, 502, 503, 504}:
                    LOGGER.warning(
                        "Transient HTTP %d during upload — retrying.", exc.resp.status
                    )
                    continue
                if exc.resp.status == 403 and "quotaExceeded" in str(exc):
                    raise UploaderError(
                        "QUOTA_EXCEEDED — limite YouTube 10k unités/jour atteinte. "
                        "Réessai possible après 00:00 UTC."
                    ) from exc
                raise UploaderError(
                    f"YouTube API error during upload: {exc}"
                ) from exc
            except Exception as exc:
                raise UploaderError(
                    f"Unexpected error during upload chunk: {exc}"
                ) from exc

        video_id = response.get("id", "")
        LOGGER.info(
            "Upload complete. Video ID: %s | URL: https://youtu.be/%s",
            video_id,
            video_id,
        )
        return response

    def _get_service(self) -> Any:
        if self._service is not None:
            return self._service

        creds = self._load_or_refresh_credentials()
        try:
            self._service = build("youtube", "v3", credentials=creds)
        except Exception as exc:
            raise UploaderError(
                f"Failed to build YouTube API service: {exc}"
            ) from exc
        return self._service

    def _load_or_refresh_credentials(self) -> Credentials:
        creds: Credentials | None = None

        if self.cfg.token_file.exists():
            try:
                raw = self.cfg.token_file.read_bytes()
                fernet = self._get_fernet()
                if fernet:
                    try:
                        raw = fernet.decrypt(raw)
                    except _FernetInvalidToken:
                        LOGGER.warning(
                            "Token at %s could not be decrypted — will re-authenticate.",
                            self.cfg.token_file,
                        )
                        raw = b""
                if raw:
                    creds = Credentials.from_authorized_user_info(
                        json.loads(raw.decode("utf-8")), YOUTUBE_SCOPES
                    )
            except Exception as exc:
                LOGGER.warning(
                    "Could not load token from %s: %s — will re-authenticate.",
                    self.cfg.token_file,
                    exc,
                )
                creds = None

        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                LOGGER.info("OAuth2 token refreshed.")
                self._save_token(creds)
            except Exception as exc:
                LOGGER.warning("Token refresh failed: %s — re-authenticating.", exc)
                creds = None

        if not creds or not creds.valid:
            if not self.cfg.client_secrets_file.exists():
                raise UploaderError(
                    f"OAuth client secrets file not found: {self.cfg.client_secrets_file}. "
                    "Download it from Google Cloud Console and set YOUTUBE_CLIENT_SECRETS_FILE."
                )
            try:
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self.cfg.client_secrets_file), YOUTUBE_SCOPES
                )
                creds = flow.run_local_server(port=0)
            except Exception as exc:
                raise UploaderError(
                    f"OAuth2 authorization flow failed: {exc}"
                ) from exc
            self._save_token(creds)

        return creds

    def _get_fernet(self) -> "Fernet | None":
        """Return a Fernet instance if TOKEN_ENCRYPTION_KEY is set, else None."""
        if not _FERNET_AVAILABLE:
            return None
        key = os.environ.get("TOKEN_ENCRYPTION_KEY", "").strip()
        if not key:
            return None
        try:
            from cryptography.fernet import Fernet
            return Fernet(key.encode())
        except Exception as exc:
            LOGGER.warning("Invalid TOKEN_ENCRYPTION_KEY — token stored unencrypted: %s", exc)
            return None

    def _save_token(self, creds: Credentials) -> None:
        try:
            self.cfg.token_file.parent.mkdir(parents=True, exist_ok=True)
            data: bytes = creds.to_json().encode("utf-8")
            fernet = self._get_fernet()
            if fernet:
                data = fernet.encrypt(data)
            self.cfg.token_file.write_bytes(data)
            import stat
            try:
                self.cfg.token_file.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0o600
            except NotImplementedError:
                pass  # Windows — no-op
            LOGGER.debug("OAuth2 token saved → %s", self.cfg.token_file)
        except Exception as exc:
            LOGGER.warning("Could not persist token to %s: %s", self.cfg.token_file, exc)


# ---------------------------------------------------------------------------
# Metadata builder
# ---------------------------------------------------------------------------


def _build_video_metadata(
    script_payload: dict[str, Any],
    default_tags: list[str],
    category_id: int,
    privacy_status: str,
    notify_subscribers: bool,
    publish_at: datetime | None,
) -> dict[str, Any]:
    """Construct the YouTube API video insert body from script payload.

    Args:
        script_payload: Validated script package containing metadata subtree.
        default_tags: Fallback tags from config when payload tags are sparse.
        category_id: YouTube category ID (27 = Education).
        privacy_status: One of private / unlisted / public.
        notify_subscribers: Whether to notify channel subscribers.
        publish_at: UTC datetime for scheduled publishing.

    Returns:
        YouTube API-compatible video body dict.

    Raises:
        UploaderError: If critical metadata fields are absent.
    """
    meta = script_payload.get("metadata")
    if not isinstance(meta, dict):
        raise UploaderError(
            "Script payload missing 'metadata' object. "
            "Ensure script_gen produced a valid payload."
        )

    title = str(meta.get("youtube_title", "")).strip()
    if not title:
        raise UploaderError("Script payload metadata.youtube_title is empty.")
    if len(title) > 100:
        title = title[:97] + "…"

    description = str(meta.get("youtube_description", "")).strip()[:5000]
    if not description:
        description = str(script_payload.get("hook", "")).strip()

    payload_tags = meta.get("youtube_tags", [])
    if isinstance(payload_tags, list):
        merged_tags = list(dict.fromkeys(payload_tags + default_tags))
    else:
        merged_tags = list(default_tags)

    _truncated_tags: list[str] = []
    _tags_total_chars = 0
    for _tag in merged_tags:
        _sep = 1 if _truncated_tags else 0
        if _tags_total_chars + len(_tag) + _sep > 500:
            break
        _tags_total_chars += len(_tag) + _sep
        _truncated_tags.append(_tag)

    actual_privacy = privacy_status
    status_body: dict[str, Any] = {
        "privacyStatus": actual_privacy,
        "selfDeclaredMadeForKids": False,
    }

    if publish_at is not None:
        if publish_at.tzinfo is None:
            publish_at = publish_at.replace(tzinfo=timezone.utc)
        status_body["privacyStatus"] = "private"
        status_body["publishAt"] = publish_at.strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "snippet": {
            "title": title,
            "description": description,
            "tags": _truncated_tags,
            "categoryId": str(category_id),
            "defaultLanguage": str(script_payload.get("language", "fr")),
            "defaultAudioLanguage": str(script_payload.get("language", "fr")),
        },
        "status": status_body,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def check_video_status(
    config: dict[str, Any],
    env: dict[str, str],
    video_id: str,
) -> dict[str, Any]:
    """Check the upload status and content rating of a published video.

    Intended to be called ~24h after upload to detect copyright claims or
    processing failures. Logs a critical warning if a claim is detected.

    Args:
        config: Full global config mapping loaded from config.yaml.
        env: Environment mapping with YouTube credentials.
        video_id: YouTube video ID to check.

    Returns:
        Dictionary with keys:
          - video_id (str)
          - upload_status (str)
          - privacy_status (str)
          - content_rating (dict)
          - has_claim (bool)

    Raises:
        UploaderError: On credential or API failure.
    """
    if not video_id or not video_id.strip():
        raise UploaderError("video_id must be a non-empty string.")

    cfg = UploaderConfig.from_mapping(config, env)
    uploader = YouTubeUploader(cfg=cfg)

    def _check_sync() -> dict[str, Any]:
        service = uploader._get_service()
        try:
            response = (
                service.videos()
                .list(part="status,contentDetails", id=video_id)
                .execute()
            )
        except HttpError as exc:
            raise UploaderError(
                f"YouTube API error checking video {video_id}: {exc}"
            ) from exc

        items = response.get("items", [])
        if not items:
            LOGGER.warning("Video %s not found in YouTube API response.", video_id)
            return {
                "video_id": video_id,
                "upload_status": "not_found",
                "privacy_status": "",
                "content_rating": {},
                "has_claim": False,
            }

        item = items[0]
        status = item.get("status", {})
        content_details = item.get("contentDetails", {})
        content_rating = content_details.get("contentRating", {})
        upload_status = status.get("uploadStatus", "")
        privacy_status = status.get("privacyStatus", "")
        has_claim = bool(content_rating)

        if upload_status == "rejected":
            LOGGER.critical(
                "⚠️ Video %s was REJECTED by YouTube (uploadStatus=rejected). "
                "Check YouTube Studio for details.",
                video_id,
            )
        if has_claim:
            LOGGER.critical(
                "⚠️ Video %s has a CONTENT RATING / COPYRIGHT CLAIM: %s",
                video_id,
                content_rating,
            )

        return {
            "video_id": video_id,
            "upload_status": upload_status,
            "privacy_status": privacy_status,
            "content_rating": content_rating,
            "has_claim": has_claim,
        }

    return await asyncio.to_thread(_check_sync)


async def upload_to_youtube(
    config: dict[str, Any],
    env: dict[str, str],
    video_path: Path,
    script_payload: dict[str, Any],
    publish_at: datetime | None = None,
) -> dict[str, Any]:
    """Public async entry point for YouTube upload.

    Args:
        config: Full global config mapping loaded from config.yaml.
        env: Environment mapping with YouTube credentials and paths.
        video_path: Absolute path to the final MP4 to upload.
        script_payload: Validated script package (provides title, description, tags).
        publish_at: Optional UTC datetime for scheduled publication.
                    When provided, video is created as private and scheduled.

    Returns:
        YouTube API insert response dict (contains 'id', 'kind', 'status', etc.).

    Raises:
        UploaderError: On any credential, file, or API failure.
    """
    cfg = UploaderConfig.from_mapping(config, env)
    uploader = YouTubeUploader(cfg=cfg)
    response = await uploader.upload(
        video_path=video_path,
        script_payload=script_payload,
        publish_at=publish_at,
    )
    return response


def get_youtube_service(config: dict[str, Any], env: dict[str, str]) -> Any:
    """Return an authenticated YouTube API service object.

    Convenience function for callers that need the service without running
    a full upload (e.g. thumbnail upload).

    Args:
        config: Full global config mapping loaded from config.yaml.
        env: Environment mapping with YouTube credential paths.

    Returns:
        Authenticated googleapiclient.discovery Resource for YouTube v3.

    Raises:
        UploaderError: On credential or API failure.
    """
    cfg = UploaderConfig.from_mapping(config, env)
    uploader = YouTubeUploader(cfg=cfg)
    return uploader._get_service()
