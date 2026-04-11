"""Tests unitaires pour core/uploader.py.

Teste UploaderConfig.from_mapping et la logique de construction des métadonnées.
Aucun appel à l'API YouTube réel.
"""

import pytest
from datetime import datetime, timezone
from pathlib import Path

from core.uploader import UploaderConfig, UploaderError, _build_video_metadata


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_config() -> dict:
    return {
        "uploader": {
            "provider": "youtube_data_api_v3",
            "default_privacy_status": "private",
            "notify_subscribers": False,
            "category_id": 28,
            "default_tags": ["IA", "Histoire", "Shorts"],
        }
    }


def _base_env(tmp_path: Path) -> dict:
    secrets = tmp_path / "secrets"
    secrets.mkdir(exist_ok=True)
    return {
        "YOUTUBE_CLIENT_SECRETS_FILE": str(secrets / "client_secret.json"),
        "YOUTUBE_TOKEN_FILE": str(secrets / "youtube_token.json"),
        "YOUTUBE_CHANNEL_ID": "UCtest123",
        "YOUTUBE_CATEGORY_ID": "28",
        "YOUTUBE_DEFAULT_PRIVACY": "private",
        "YOUTUBE_NOTIFY_SUBSCRIBERS": "false",
    }


def _script_payload(
    title: str = "Titre YouTube normal",
    description: str = "Description normale.",
    tags: list[str] | None = None,
) -> dict:
    return {
        "language": "fr",
        "hook": "Hook de fallback.",
        "metadata": {
            "youtube_title": title,
            "youtube_description": description,
            "youtube_tags": tags or ["ia", "histoire", "tech"],
        },
    }


# ---------------------------------------------------------------------------
# UploaderConfig.from_mapping
# ---------------------------------------------------------------------------


class TestUploaderConfigFromMapping:
    def test_valid_config_creates_instance(self, tmp_path: Path) -> None:
        cfg = UploaderConfig.from_mapping(_base_config(), _base_env(tmp_path))
        assert cfg.channel_id == "UCtest123"
        assert cfg.category_id == 28
        assert cfg.default_privacy_status == "private"
        assert cfg.notify_subscribers is False
        assert "IA" in cfg.default_tags

    def test_missing_uploader_section_raises(self, tmp_path: Path) -> None:
        with pytest.raises(UploaderError, match="uploader"):
            UploaderConfig.from_mapping({}, _base_env(tmp_path))

    def test_invalid_privacy_value_raises(self, tmp_path: Path) -> None:
        env = _base_env(tmp_path)
        env["YOUTUBE_DEFAULT_PRIVACY"] = "only_me"
        with pytest.raises(UploaderError, match="Invalid YOUTUBE_DEFAULT_PRIVACY"):
            UploaderConfig.from_mapping(_base_config(), env)

    def test_valid_privacy_values_accepted(self, tmp_path: Path) -> None:
        for privacy in ("private", "unlisted", "public"):
            env = _base_env(tmp_path)
            env["YOUTUBE_DEFAULT_PRIVACY"] = privacy
            cfg = UploaderConfig.from_mapping(_base_config(), env)
            assert cfg.default_privacy_status == privacy

    def test_notify_subscribers_true_from_env(self, tmp_path: Path) -> None:
        env = _base_env(tmp_path)
        env["YOUTUBE_NOTIFY_SUBSCRIBERS"] = "true"
        cfg = UploaderConfig.from_mapping(_base_config(), env)
        assert cfg.notify_subscribers is True

    def test_category_id_overridden_by_env(self, tmp_path: Path) -> None:
        env = _base_env(tmp_path)
        env["YOUTUBE_CATEGORY_ID"] = "22"
        cfg = UploaderConfig.from_mapping(_base_config(), env)
        assert cfg.category_id == 22

    def test_default_tags_empty_list_when_not_configured(self, tmp_path: Path) -> None:
        config = _base_config()
        config["uploader"]["default_tags"] = []
        cfg = UploaderConfig.from_mapping(config, _base_env(tmp_path))
        assert cfg.default_tags == []


# ---------------------------------------------------------------------------
# _build_video_metadata — titre
# ---------------------------------------------------------------------------


class TestBuildVideoMetadataTitle:
    def test_normal_title_preserved(self) -> None:
        result = _build_video_metadata(
            _script_payload(title="Titre court"),
            default_tags=[],
            category_id=28,
            privacy_status="private",
            notify_subscribers=False,
            publish_at=None,
        )
        assert result["snippet"]["title"] == "Titre court"

    def test_title_truncated_at_97_chars(self) -> None:
        long_title = "A" * 120
        result = _build_video_metadata(
            _script_payload(title=long_title),
            default_tags=[],
            category_id=28,
            privacy_status="private",
            notify_subscribers=False,
            publish_at=None,
        )
        title = result["snippet"]["title"]
        assert len(title) <= 100
        assert title.endswith("…")
        assert title == "A" * 97 + "…"

    def test_empty_title_raises(self) -> None:
        with pytest.raises(UploaderError, match="youtube_title is empty"):
            _build_video_metadata(
                _script_payload(title=""),
                default_tags=[],
                category_id=28,
                privacy_status="private",
                notify_subscribers=False,
                publish_at=None,
            )


# ---------------------------------------------------------------------------
# _build_video_metadata — description
# ---------------------------------------------------------------------------


class TestBuildVideoMetadataDescription:
    def test_normal_description_preserved(self) -> None:
        result = _build_video_metadata(
            _script_payload(description="Description courte."),
            default_tags=[],
            category_id=28,
            privacy_status="private",
            notify_subscribers=False,
            publish_at=None,
        )
        assert result["snippet"]["description"] == "Description courte."

    def test_description_truncated_at_5000_chars(self) -> None:
        long_desc = "D" * 6000
        result = _build_video_metadata(
            _script_payload(description=long_desc),
            default_tags=[],
            category_id=28,
            privacy_status="private",
            notify_subscribers=False,
            publish_at=None,
        )
        assert len(result["snippet"]["description"]) == 5000

    def test_empty_description_falls_back_to_hook(self) -> None:
        payload = _script_payload(description="")
        payload["hook"] = "Hook de remplacement."
        result = _build_video_metadata(
            payload,
            default_tags=[],
            category_id=28,
            privacy_status="private",
            notify_subscribers=False,
            publish_at=None,
        )
        assert result["snippet"]["description"] == "Hook de remplacement."


# ---------------------------------------------------------------------------
# _build_video_metadata — tags (limite 500 chars cumulés)
# ---------------------------------------------------------------------------


class TestBuildVideoMetadataTags:
    def test_short_tags_all_included(self) -> None:
        tags = ["ia", "hist", "tech", "fr", "shorts"]
        result = _build_video_metadata(
            _script_payload(tags=tags),
            default_tags=[],
            category_id=28,
            privacy_status="private",
            notify_subscribers=False,
            publish_at=None,
        )
        assert result["snippet"]["tags"] == tags

    def test_tags_truncated_at_500_cumulative_chars(self) -> None:
        # 52 tags of 10 chars would be 10 + 51*(10+1) = 571 chars > 500
        many_tags = [f"tag{i:04d}xxx" for i in range(52)]  # each 10 chars
        result = _build_video_metadata(
            _script_payload(tags=many_tags),
            default_tags=[],
            category_id=28,
            privacy_status="private",
            notify_subscribers=False,
            publish_at=None,
        )
        kept = result["snippet"]["tags"]
        total_chars = sum(len(t) for t in kept) + max(0, len(kept) - 1)
        assert total_chars <= 500
        assert len(kept) < 52  # some were dropped

    def test_default_tags_merged_with_payload_tags(self) -> None:
        result = _build_video_metadata(
            _script_payload(tags=["ia", "science"]),
            default_tags=["shorts", "histoire"],
            category_id=28,
            privacy_status="private",
            notify_subscribers=False,
            publish_at=None,
        )
        tags = result["snippet"]["tags"]
        assert "ia" in tags
        assert "shorts" in tags
        assert "histoire" in tags

    def test_duplicate_tags_deduplicated(self) -> None:
        result = _build_video_metadata(
            _script_payload(tags=["ia", "ia", "histoire"]),
            default_tags=["ia"],
            category_id=28,
            privacy_status="private",
            notify_subscribers=False,
            publish_at=None,
        )
        tags = result["snippet"]["tags"]
        assert tags.count("ia") == 1


# ---------------------------------------------------------------------------
# _build_video_metadata — privacy / publish_at
# ---------------------------------------------------------------------------


class TestBuildVideoMetadataPrivacy:
    def test_public_privacy_set(self) -> None:
        result = _build_video_metadata(
            _script_payload(),
            default_tags=[],
            category_id=28,
            privacy_status="public",
            notify_subscribers=False,
            publish_at=None,
        )
        assert result["status"]["privacyStatus"] == "public"

    def test_publish_at_overrides_privacy_to_private(self) -> None:
        publish_time = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = _build_video_metadata(
            _script_payload(),
            default_tags=[],
            category_id=28,
            privacy_status="public",
            notify_subscribers=False,
            publish_at=publish_time,
        )
        assert result["status"]["privacyStatus"] == "private"
        assert result["status"]["publishAt"] == "2026-05-01T12:00:00Z"

    def test_category_id_in_snippet(self) -> None:
        result = _build_video_metadata(
            _script_payload(),
            default_tags=[],
            category_id=22,
            privacy_status="private",
            notify_subscribers=False,
            publish_at=None,
        )
        assert result["snippet"]["categoryId"] == "22"

    def test_language_from_payload(self) -> None:
        result = _build_video_metadata(
            _script_payload(),
            default_tags=[],
            category_id=28,
            privacy_status="private",
            notify_subscribers=False,
            publish_at=None,
        )
        assert result["snippet"]["defaultLanguage"] == "fr"
        assert result["snippet"]["defaultAudioLanguage"] == "fr"

    def test_contains_synthetic_media_true(self) -> None:
        """YouTube 2024 policy: AI-generated content must declare containsSyntheticMedia."""
        result = _build_video_metadata(
            _script_payload(),
            default_tags=[],
            category_id=28,
            privacy_status="private",
            notify_subscribers=False,
            publish_at=None,
        )
        assert result["status"]["containsSyntheticMedia"] is True
