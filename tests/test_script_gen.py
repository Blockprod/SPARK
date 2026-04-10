"""Tests unitaires pour core/script_gen.py.

Seule la logique de parsing/validation est testée — aucun appel API Gemini réel.
"""

import asyncio
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.script_gen import (
    ScriptGenConfig,
    ScriptGenerator,
    ScriptGenerationError,
    ScriptValidationError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_config(prompts_dir: Path) -> dict:
    return {
        "script_generation": {
            "model": "gemini-2.0-flash",
            "temperature": 0.8,
            "top_p": 0.95,
            "max_output_tokens": 4096,
            "template_pool": ["revelation", "parallele_inverse", "countdown"],
        },
        "pipeline": {
            "min_duration_sec": 50,
            "max_duration_sec": 60,
            "max_scenes": 8,
        },
        "paths": {
            "prompts_dir": str(prompts_dir),
        },
    }


def _make_generator(tmp_path: Path) -> ScriptGenerator:
    cfg = ScriptGenConfig.from_mapping(_base_config(tmp_path))
    with (
        patch("google.generativeai.configure"),
        patch("google.generativeai.GenerativeModel", return_value=MagicMock()),
    ):
        return ScriptGenerator(cfg=cfg, env={"GEMINI_API_KEY": "fake-key-for-tests"})


def _valid_payload(duration: int = 55) -> dict:
    return {
        "topic": "IA et l'Histoire",
        "title": "Titre de test",
        "hook": "Hook captivant",
        "narration_text": "Texte de narration complet pour le test.",
        "language": "fr",
        "duration_sec": duration,
        "scenes": [
            {
                "scene_id": 1,
                "duration_sec": duration,
                "narration": "Narration de la scène principale.",
                "visual_prompt": "Prompt visuel cinématographique détaillé.",
                "camera_movement": "slow pan right",
                "lighting_mood": "golden hour",
                "historical_era": "XIXe siècle industriel",
                "cinematic_style": "épique dramatique",
            }
        ],
        "metadata": {
            "youtube_title": "Titre YouTube complet",
            "youtube_description": "Description complète pour YouTube.",
            "youtube_tags": ["ia", "histoire", "tech", "science", "france"],
        },
    }


# ---------------------------------------------------------------------------
# ScriptGenConfig.from_mapping
# ---------------------------------------------------------------------------


class TestScriptGenConfigFromMapping:
    def test_valid_config_creates_instance(self, tmp_path: Path) -> None:
        cfg = ScriptGenConfig.from_mapping(_base_config(tmp_path))
        assert cfg.model == "gemini-2.0-flash"
        assert cfg.temperature == 0.8
        assert cfg.min_duration_sec == 50
        assert cfg.max_scenes == 8

    def test_missing_script_generation_raises(self, tmp_path: Path) -> None:
        config = _base_config(tmp_path)
        del config["script_generation"]
        with pytest.raises(ScriptGenerationError, match="script_generation"):
            ScriptGenConfig.from_mapping(config)

    def test_missing_pipeline_raises(self, tmp_path: Path) -> None:
        config = _base_config(tmp_path)
        del config["pipeline"]
        with pytest.raises(ScriptGenerationError, match="pipeline"):
            ScriptGenConfig.from_mapping(config)

    def test_missing_paths_raises(self, tmp_path: Path) -> None:
        config = _base_config(tmp_path)
        del config["paths"]
        with pytest.raises(ScriptGenerationError, match="paths"):
            ScriptGenConfig.from_mapping(config)

    def test_empty_template_pool_uses_defaults(self, tmp_path: Path) -> None:
        config = _base_config(tmp_path)
        config["script_generation"]["template_pool"] = []
        cfg = ScriptGenConfig.from_mapping(config)
        assert set(cfg.template_pool) == {"revelation", "parallele_inverse", "countdown"}

    def test_custom_template_pool_preserved(self, tmp_path: Path) -> None:
        config = _base_config(tmp_path)
        config["script_generation"]["template_pool"] = ["custom_a", "custom_b"]
        cfg = ScriptGenConfig.from_mapping(config)
        assert cfg.template_pool == ["custom_a", "custom_b"]


# ---------------------------------------------------------------------------
# ScriptGenerator.__init__
# ---------------------------------------------------------------------------


class TestScriptGeneratorInit:
    def test_missing_api_key_raises_before_genai(self, tmp_path: Path) -> None:
        cfg = ScriptGenConfig.from_mapping(_base_config(tmp_path))
        with pytest.raises(ScriptGenerationError, match="GEMINI_API_KEY"):
            ScriptGenerator(cfg=cfg, env={})

    def test_empty_api_key_raises(self, tmp_path: Path) -> None:
        cfg = ScriptGenConfig.from_mapping(_base_config(tmp_path))
        with pytest.raises(ScriptGenerationError, match="GEMINI_API_KEY"):
            ScriptGenerator(cfg=cfg, env={"GEMINI_API_KEY": ""})

    def test_custom_timeout_loaded(self, tmp_path: Path) -> None:
        cfg = ScriptGenConfig.from_mapping(_base_config(tmp_path))
        with (
            patch("google.generativeai.configure"),
            patch("google.generativeai.GenerativeModel", return_value=MagicMock()),
        ):
            gen = ScriptGenerator(
                cfg=cfg,
                env={"GEMINI_API_KEY": "k", "GEMINI_REQUEST_TIMEOUT_SEC": "120"},
            )
        assert gen._request_timeout == 120.0


# ---------------------------------------------------------------------------
# _parse_json_response
# ---------------------------------------------------------------------------


class TestParseJsonResponse:
    def test_valid_json_object(self, tmp_path: Path) -> None:
        gen = _make_generator(tmp_path)
        result = gen._parse_json_response('{"key": "value", "num": 42}')
        assert result == {"key": "value", "num": 42}

    def test_strips_fenced_code_block(self, tmp_path: Path) -> None:
        gen = _make_generator(tmp_path)
        raw = "```json\n{\"topic\": \"test\"}\n```"
        result = gen._parse_json_response(raw)
        assert result["topic"] == "test"

    def test_invalid_json_raises_script_validation_error(self, tmp_path: Path) -> None:
        gen = _make_generator(tmp_path)
        with pytest.raises(ScriptValidationError, match="Invalid JSON"):
            gen._parse_json_response("definitely not json {{{")

    def test_json_array_raises_script_validation_error(self, tmp_path: Path) -> None:
        gen = _make_generator(tmp_path)
        with pytest.raises(ScriptValidationError, match="JSON object"):
            gen._parse_json_response("[1, 2, 3]")

    def test_json_string_raises_script_validation_error(self, tmp_path: Path) -> None:
        gen = _make_generator(tmp_path)
        with pytest.raises(ScriptValidationError, match="JSON object"):
            gen._parse_json_response('"just a string"')


# ---------------------------------------------------------------------------
# _validate_payload
# ---------------------------------------------------------------------------


class TestValidatePayload:
    def test_valid_payload_passes(self, tmp_path: Path) -> None:
        gen = _make_generator(tmp_path)
        gen._validate_payload(_valid_payload())  # no exception

    def test_missing_top_level_field_raises(self, tmp_path: Path) -> None:
        gen = _make_generator(tmp_path)
        payload = _valid_payload()
        del payload["hook"]
        with pytest.raises(ScriptValidationError, match="hook"):
            gen._validate_payload(payload)

    def test_duration_too_short_raises(self, tmp_path: Path) -> None:
        gen = _make_generator(tmp_path)
        payload = _valid_payload(duration=30)
        payload["scenes"][0]["duration_sec"] = 30
        with pytest.raises(ScriptValidationError, match="duration_sec"):
            gen._validate_payload(payload)

    def test_duration_too_long_raises(self, tmp_path: Path) -> None:
        gen = _make_generator(tmp_path)
        payload = _valid_payload(duration=90)
        payload["scenes"][0]["duration_sec"] = 90
        with pytest.raises(ScriptValidationError, match="duration_sec"):
            gen._validate_payload(payload)

    def test_non_integer_duration_raises(self, tmp_path: Path) -> None:
        gen = _make_generator(tmp_path)
        payload = _valid_payload()
        payload["duration_sec"] = 55.5
        with pytest.raises(ScriptValidationError, match="duration_sec"):
            gen._validate_payload(payload)

    def test_wrong_language_raises(self, tmp_path: Path) -> None:
        gen = _make_generator(tmp_path)
        payload = _valid_payload()
        payload["language"] = "en"
        with pytest.raises(ScriptValidationError, match="language"):
            gen._validate_payload(payload)

    def test_empty_scenes_list_raises(self, tmp_path: Path) -> None:
        gen = _make_generator(tmp_path)
        payload = _valid_payload()
        payload["scenes"] = []
        with pytest.raises(ScriptValidationError, match="scenes"):
            gen._validate_payload(payload)

    def test_too_many_scenes_raises(self, tmp_path: Path) -> None:
        gen = _make_generator(tmp_path)
        payload = _valid_payload()
        scene = payload["scenes"][0].copy()
        scene["duration_sec"] = 6
        payload["scenes"] = [scene] * 9  # exceeds max_scenes=8
        payload["duration_sec"] = 54
        with pytest.raises(ScriptValidationError, match="max_scenes"):
            gen._validate_payload(payload)

    def test_scene_duration_mismatch_raises(self, tmp_path: Path) -> None:
        gen = _make_generator(tmp_path)
        payload = _valid_payload()
        payload["scenes"][0]["duration_sec"] = 10  # 10 != 55, diff > 2
        with pytest.raises(ScriptValidationError, match="Sum of scene durations"):
            gen._validate_payload(payload)

    def test_missing_metadata_field_raises(self, tmp_path: Path) -> None:
        gen = _make_generator(tmp_path)
        payload = _valid_payload()
        del payload["metadata"]["youtube_title"]
        with pytest.raises(ScriptValidationError, match="youtube_title"):
            gen._validate_payload(payload)

    def test_too_few_tags_raises(self, tmp_path: Path) -> None:
        gen = _make_generator(tmp_path)
        payload = _valid_payload()
        payload["metadata"]["youtube_tags"] = ["a", "b", "c"]  # < 5
        with pytest.raises(ScriptValidationError, match="youtube_tags"):
            gen._validate_payload(payload)

    def test_too_many_tags_raises(self, tmp_path: Path) -> None:
        gen = _make_generator(tmp_path)
        payload = _valid_payload()
        payload["metadata"]["youtube_tags"] = [f"tag{i}" for i in range(13)]  # > 12
        with pytest.raises(ScriptValidationError, match="youtube_tags"):
            gen._validate_payload(payload)

    def test_non_string_tags_raises(self, tmp_path: Path) -> None:
        gen = _make_generator(tmp_path)
        payload = _valid_payload()
        payload["metadata"]["youtube_tags"] = [1, 2, 3, 4, 5]
        with pytest.raises(ScriptValidationError, match="youtube_tags"):
            gen._validate_payload(payload)


# ---------------------------------------------------------------------------
# _validate_scene
# ---------------------------------------------------------------------------


class TestValidateScene:
    def test_valid_scene_passes(self, tmp_path: Path) -> None:
        gen = _make_generator(tmp_path)
        gen._validate_scene(_valid_payload()["scenes"][0], 1)  # no exception

    def test_non_dict_raises(self, tmp_path: Path) -> None:
        gen = _make_generator(tmp_path)
        with pytest.raises(ScriptValidationError, match="must be an object"):
            gen._validate_scene("not_a_dict", 1)

    def test_missing_required_field_raises(self, tmp_path: Path) -> None:
        gen = _make_generator(tmp_path)
        scene = _valid_payload()["scenes"][0].copy()
        del scene["narration"]
        with pytest.raises(ScriptValidationError, match="narration"):
            gen._validate_scene(scene, 1)

    def test_zero_duration_raises(self, tmp_path: Path) -> None:
        gen = _make_generator(tmp_path)
        scene = _valid_payload()["scenes"][0].copy()
        scene["duration_sec"] = 0
        with pytest.raises(ScriptValidationError, match="duration_sec"):
            gen._validate_scene(scene, 1)

    def test_negative_duration_raises(self, tmp_path: Path) -> None:
        gen = _make_generator(tmp_path)
        scene = _valid_payload()["scenes"][0].copy()
        scene["duration_sec"] = -5
        with pytest.raises(ScriptValidationError, match="duration_sec"):
            gen._validate_scene(scene, 1)

    def test_whitespace_only_narration_raises(self, tmp_path: Path) -> None:
        gen = _make_generator(tmp_path)
        scene = _valid_payload()["scenes"][0].copy()
        scene["narration"] = "   "
        with pytest.raises(ScriptValidationError, match="narration"):
            gen._validate_scene(scene, 1)

    def test_whitespace_only_visual_prompt_raises(self, tmp_path: Path) -> None:
        gen = _make_generator(tmp_path)
        scene = _valid_payload()["scenes"][0].copy()
        scene["visual_prompt"] = ""
        with pytest.raises(ScriptValidationError, match="visual_prompt"):
            gen._validate_scene(scene, 1)


# ---------------------------------------------------------------------------
# generate_for_topic — early validation (no API call)
# ---------------------------------------------------------------------------


class TestTopicValidation:
    def test_empty_topic_raises_before_api(self, tmp_path: Path) -> None:
        gen = _make_generator(tmp_path)
        with pytest.raises(ScriptGenerationError, match="non-empty"):
            asyncio.run(gen.generate_for_topic(""))

    def test_whitespace_only_topic_raises(self, tmp_path: Path) -> None:
        gen = _make_generator(tmp_path)
        with pytest.raises(ScriptGenerationError, match="non-empty"):
            asyncio.run(gen.generate_for_topic("    "))

    def test_topic_over_500_chars_raises_validation_error(self, tmp_path: Path) -> None:
        gen = _make_generator(tmp_path)
        with pytest.raises(ScriptValidationError, match="500 characters"):
            asyncio.run(gen.generate_for_topic("x" * 501))

    def test_topic_exactly_500_chars_passes_validation(self, tmp_path: Path) -> None:
        """Topic of exactly 500 chars should not raise the length guard."""
        gen = _make_generator(tmp_path)
        # The function will fail later (prompt file missing) — not at the length guard
        with pytest.raises(ScriptGenerationError, match="Prompt file not found"):
            asyncio.run(gen.generate_for_topic("a" * 500))
