"""Tests unitaires pour core/audio_gen.py.

Teste AudioGenConfig.from_mapping, la sélection de backend Kokoro,
et la synthèse audio (ONNX mocké).
Aucun appel réel au modèle Kokoro ONNX.
"""

from __future__ import annotations

import io
import struct
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.audio_gen import (
    AudioGenConfig,
    AudioGenerationError,
    AudioGenerator,
    EdgeTTSBackend,
    KokoroBackend,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_config() -> dict:
    return {
        "audio_generation": {
            "language": "fr-fr",
            "kokoro": {
                "model_path": "./models/kokoro.onnx",
                "voices_path": "./models/voices.bin",
                "voice": "ff_siwis",
                "speed": 1.0,
                "sample_rate": 24000,
                "output_format": "wav",
            },
        },
        "paths": {
            "audio_dir": "./outputs/audio",
        },
    }


def _make_wav_bytes(sample_rate: int = 24000, n_samples: int = 100) -> bytes:
    """Build a minimal valid PCM WAV in memory for testing."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack(f"<{n_samples}h", *([0] * n_samples)))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# AudioGenConfig.from_mapping
# ---------------------------------------------------------------------------


class TestAudioGenConfigFromMapping:
    def test_valid_kokoro_config(self) -> None:
        cfg = AudioGenConfig.from_mapping(_base_config())
        assert cfg.kokoro_voice == "ff_siwis"
        assert cfg.language == "fr-fr"

    def test_missing_audio_generation_raises(self) -> None:
        with pytest.raises(AudioGenerationError, match="audio_generation"):
            AudioGenConfig.from_mapping({"paths": {"audio_dir": "./x"}})

    def test_missing_paths_raises(self) -> None:
        with pytest.raises(AudioGenerationError, match="paths"):
            AudioGenConfig.from_mapping({"audio_generation": {"language": "fr-fr"}})

    def test_defaults_applied_when_kokoro_section_absent(self) -> None:
        config: dict = {
            "audio_generation": {"language": "fr-fr"},
            "paths": {"audio_dir": "./x"},
        }
        cfg = AudioGenConfig.from_mapping(config)
        assert cfg.kokoro_voice == "ff_siwis"
        assert cfg.kokoro_speed == 1.0


# ---------------------------------------------------------------------------
# AudioGenerator — sélection backend + fallback gracieux
# ---------------------------------------------------------------------------


class TestAudioGeneratorBackendSelection:
    def test_kokoro_backend_selected(self) -> None:
        config = _base_config()
        config["audio_generation"]["active_backend"] = "kokoro"
        cfg = AudioGenConfig.from_mapping(config)
        gen = AudioGenerator(cfg=cfg)
        assert isinstance(gen._backend, KokoroBackend)

    def test_edge_tts_backend_selected_by_default(self) -> None:
        cfg = AudioGenConfig.from_mapping(_base_config())
        gen = AudioGenerator(cfg=cfg)
        assert isinstance(gen._backend, EdgeTTSBackend)

    def test_edge_tts_backend_selected_explicitly(self) -> None:
        config = _base_config()
        config["audio_generation"]["active_backend"] = "edge_tts"
        cfg = AudioGenConfig.from_mapping(config)
        gen = AudioGenerator(cfg=cfg)
        assert isinstance(gen._backend, EdgeTTSBackend)



