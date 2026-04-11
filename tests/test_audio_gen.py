"""Tests unitaires pour core/audio_gen.py.

Teste AudioGenConfig.from_mapping, la sélection de backend,
le fallback Kokoro si MISTRAL_API_KEY absent, et VoxtralBackend (API mockée).
Aucun appel réel à l'API Mistral ni au modèle Kokoro ONNX.
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
    KokoroBackend,
    VoxtralBackend,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_config(backend: str = "kokoro") -> dict:
    return {
        "audio_generation": {
            "active_backend": backend,
            "language": "fr-fr",
            "kokoro": {
                "model_path": "./models/kokoro.onnx",
                "voices_path": "./models/voices.bin",
                "voice": "ff_siwis",
                "speed": 1.0,
                "sample_rate": 24000,
                "output_format": "wav",
            },
            "voxtral": {
                "model": "mistral-tts-latest",
                "voice": "fr_female",
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
        cfg = AudioGenConfig.from_mapping(_base_config("kokoro"))
        assert cfg.active_backend == "kokoro"
        assert cfg.kokoro_voice == "ff_siwis"
        assert cfg.language == "fr-fr"

    def test_valid_voxtral_config(self) -> None:
        cfg = AudioGenConfig.from_mapping(_base_config("voxtral"))
        assert cfg.active_backend == "voxtral"
        assert cfg.voxtral_voice == "fr_female"

    def test_missing_audio_generation_raises(self) -> None:
        with pytest.raises(AudioGenerationError, match="audio_generation"):
            AudioGenConfig.from_mapping({"paths": {"audio_dir": "./x"}})

    def test_missing_paths_raises(self) -> None:
        with pytest.raises(AudioGenerationError, match="paths"):
            AudioGenConfig.from_mapping({"audio_generation": {"active_backend": "kokoro"}})

    def test_invalid_backend_raises(self) -> None:
        config = _base_config("elevenlabs")
        with pytest.raises(AudioGenerationError, match="Unsupported audio backend"):
            AudioGenConfig.from_mapping(config)

    def test_defaults_applied_when_kokoro_section_absent(self) -> None:
        config: dict = {
            "audio_generation": {"active_backend": "kokoro", "language": "fr-fr"},
            "paths": {"audio_dir": "./x"},
        }
        cfg = AudioGenConfig.from_mapping(config)
        assert cfg.kokoro_voice == "ff_siwis"
        assert cfg.kokoro_speed == 1.0


# ---------------------------------------------------------------------------
# AudioGenerator — sélection backend + fallback gracieux
# ---------------------------------------------------------------------------


class TestAudioGeneratorBackendSelection:
    def test_kokoro_backend_selected_when_active(self) -> None:
        cfg = AudioGenConfig.from_mapping(_base_config("kokoro"))
        gen = AudioGenerator(cfg=cfg, env={})
        assert isinstance(gen._backend, KokoroBackend)

    def test_voxtral_backend_selected_when_key_present(self) -> None:
        cfg = AudioGenConfig.from_mapping(_base_config("voxtral"))
        gen = AudioGenerator(cfg=cfg, env={"MISTRAL_API_KEY": "sk-test-key"})
        assert isinstance(gen._backend, VoxtralBackend)

    def test_fallback_to_kokoro_when_mistral_key_absent(self) -> None:
        """Si active_backend=voxtral mais MISTRAL_API_KEY absent → Kokoro silencieusement."""
        cfg = AudioGenConfig.from_mapping(_base_config("voxtral"))
        gen = AudioGenerator(cfg=cfg, env={})
        assert isinstance(gen._backend, KokoroBackend)

    def test_voxtral_init_raises_without_key_when_instantiated_directly(self) -> None:
        cfg = AudioGenConfig.from_mapping(_base_config("voxtral"))
        with pytest.raises(AudioGenerationError, match="MISTRAL_API_KEY"):
            VoxtralBackend(cfg=cfg, env={})


# ---------------------------------------------------------------------------
# VoxtralBackend.synthesize — mock API Mistral
# ---------------------------------------------------------------------------


class TestVoxtralBackendSynthesize:
    def _make_backend(self, tmp_path: Path) -> VoxtralBackend:
        cfg = AudioGenConfig.from_mapping(_base_config("voxtral"))
        cfg = AudioGenConfig(
            active_backend="voxtral",
            language="fr-fr",
            kokoro_model_path="./x.onnx",
            kokoro_voices_path="./x.bin",
            kokoro_voice="ff_siwis",
            kokoro_speed=1.0,
            kokoro_sample_rate=24000,
            kokoro_output_format="wav",
            voxtral_enabled=True,
            voxtral_model="mistral-tts-latest",
            voxtral_voice="fr_female",
            audio_dir=tmp_path,
        )
        return VoxtralBackend(cfg=cfg, env={"MISTRAL_API_KEY": "sk-test"})

    @pytest.mark.anyio
    async def test_synthesize_writes_wav_on_success(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)
        pcm_bytes = _make_wav_bytes()

        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = pcm_bytes

        output = tmp_path / "test_scene.wav"
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = await backend.synthesize(text="Bonjour le monde.", output_path=output)

        assert result == output
        assert output.exists()

    @pytest.mark.anyio
    async def test_synthesize_raises_on_404(self, tmp_path: Path) -> None:
        import urllib.error

        backend = self._make_backend(tmp_path)
        output = tmp_path / "test.wav"
        http_err = urllib.error.HTTPError(url="", code=404, msg="Not Found", hdrs=MagicMock(), fp=None)  # type: ignore[arg-type]

        with patch("urllib.request.urlopen", side_effect=http_err):
            with pytest.raises(AudioGenerationError, match="404"):
                await backend.synthesize(text="test", output_path=output)

    @pytest.mark.anyio
    async def test_synthesize_retries_on_429(self, tmp_path: Path) -> None:
        """429 doit être retenté par tenacity jusqu'à 4 fois avant erreur finale."""
        import urllib.error

        backend = self._make_backend(tmp_path)
        output = tmp_path / "test.wav"
        http_err = urllib.error.HTTPError(url="", code=429, msg="Too Many Requests", hdrs=MagicMock(), fp=None)  # type: ignore[arg-type]

        call_count = 0

        def _side_effect(*args: object, **kwargs: object) -> object:
            nonlocal call_count
            call_count += 1
            raise http_err

        with patch("urllib.request.urlopen", side_effect=_side_effect), \
             patch("time.sleep"):  # bypass tenacity wait_exponential delays
            with pytest.raises(AudioGenerationError, match="429"):
                await backend.synthesize(text="test", output_path=output)

        assert call_count == 4  # 4 tentatives avant abandon

    @pytest.mark.anyio
    async def test_synthesize_raises_on_empty_text(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)
        output = tmp_path / "test.wav"
        with pytest.raises(AudioGenerationError, match="empty narration"):
            await backend.synthesize(text="", output_path=output)
