"""Audio voice-over generation for shorts-engine.

Kokoro runs fully locally via ONNX inference.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
import wave
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
from tenacity import Retrying, stop_after_attempt, wait_exponential

LOGGER = logging.getLogger(__name__)


class AudioGenerationError(RuntimeError):
    """Raised when voice-over generation fails."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AudioGenConfig:
    """Configuration for the audio generation stage."""

    language: str

    # Kokoro
    kokoro_model_path: str
    kokoro_voices_path: str
    kokoro_voice: str
    kokoro_speed: float
    kokoro_sample_rate: int
    kokoro_output_format: str

    # Outputs
    audio_dir: Path

    @classmethod
    def from_mapping(cls, config: dict[str, Any]) -> "AudioGenConfig":
        """Build AudioGenConfig from global app config.

        Args:
            config: Parsed config.yaml mapping.

        Returns:
            Validated AudioGenConfig instance.

        Raises:
            AudioGenerationError: If required sections are absent.
        """
        audio_cfg = config.get("audio_generation")
        paths_cfg = config.get("paths")

        if not isinstance(audio_cfg, dict):
            raise AudioGenerationError("Missing 'audio_generation' in configuration.")
        if not isinstance(paths_cfg, dict):
            raise AudioGenerationError("Missing 'paths' in configuration.")

        kokoro_cfg = audio_cfg.get("kokoro", {})
        audio_dir = Path(str(paths_cfg.get("audio_dir", "./outputs/audio"))).resolve()

        return cls(
            language=str(audio_cfg.get("language", "fr-fr")),
            kokoro_model_path=str(kokoro_cfg.get("model_path", "./models/kokoro-v1.0.fp16.onnx")),
            kokoro_voices_path=str(kokoro_cfg.get("voices_path", "./models/voices-v1.0.bin")),
            kokoro_voice=str(kokoro_cfg.get("voice", "ff_siwis")),
            kokoro_speed=float(kokoro_cfg.get("speed", 1.0)),
            kokoro_sample_rate=int(kokoro_cfg.get("sample_rate", 24000)),
            kokoro_output_format=str(kokoro_cfg.get("output_format", "wav")),
            audio_dir=audio_dir,
        )


# ---------------------------------------------------------------------------
# Backend ABC
# ---------------------------------------------------------------------------


class TTSBackend(ABC):
    """Abstract TTS backend. All backends must implement synthesize()."""

    @abstractmethod
    async def synthesize(self, text: str, output_path: Path) -> Path:
        """Synthesize speech for the given text and write it to output_path.

        Args:
            text: Narration text to synthesize (plain UTF-8, no SSML).
            output_path: Absolute path for the output audio file.

        Returns:
            Path to the written audio file.

        Raises:
            AudioGenerationError: On synthesis or I/O failure.
        """


# ---------------------------------------------------------------------------
# Kokoro backend (local ONNX)
# ---------------------------------------------------------------------------


class KokoroBackend(TTSBackend):
    """Kokoro TTS via kokoro-onnx, fully local."""

    def __init__(self, cfg: AudioGenConfig) -> None:
        """Initialize Kokoro backend.

        Args:
            cfg: Audio generation configuration.
        """
        self.cfg = cfg
        self._kokoro: Any = None

    def _get_kokoro(self) -> Any:
        if self._kokoro is None:
            try:
                from kokoro_onnx import Kokoro  # type: ignore[import]
            except ImportError as exc:
                raise AudioGenerationError(
                    "kokoro-onnx is not installed. Run: pip install kokoro-onnx"
                ) from exc
            LOGGER.info("Loading Kokoro ONNX model (voice=%s)…", self.cfg.kokoro_voice)
            try:
                self._kokoro = Kokoro(self.cfg.kokoro_model_path, self.cfg.kokoro_voices_path)
            except Exception as exc:
                raise AudioGenerationError(
                    f"Failed to load Kokoro model: {exc}"
                ) from exc
        return self._kokoro

    async def synthesize(self, text: str, output_path: Path) -> Path:
        """Synthesize narration text with Kokoro and write WAV to output_path.

        Args:
            text: Plain French narration text.
            output_path: Target WAV file path.

        Returns:
            Path to the written WAV file.

        Raises:
            AudioGenerationError: If synthesis or write fails.
        """
        if not text or not text.strip():
            raise AudioGenerationError("Cannot synthesize empty narration text.")

        def _run() -> Path:
            kokoro = self._get_kokoro()
            try:
                samples, sample_rate = kokoro.create(
                    text=text.strip(),
                    voice=self.cfg.kokoro_voice,
                    speed=self.cfg.kokoro_speed,
                    lang=self.cfg.language,
                )
            except Exception as exc:
                raise AudioGenerationError(
                    f"Kokoro inference failed: {exc}"
                ) from exc

            if samples is None or len(samples) == 0:
                raise AudioGenerationError("Kokoro returned empty audio samples.")

            audio_array = np.array(samples, dtype=np.float32)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                sf.write(str(output_path), audio_array, samplerate=sample_rate)
            except Exception as exc:
                raise AudioGenerationError(
                    f"Failed to write audio file {output_path}: {exc}"
                ) from exc

            LOGGER.debug("Kokoro wrote %s (%.2fs)", output_path, len(samples) / sample_rate)
            return output_path

        return await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# AudioGenerator orchestrator
# ---------------------------------------------------------------------------


class AudioGenerator:
    """Orchestrates TTS synthesis for all scenes in a script package."""

    def __init__(self, cfg: AudioGenConfig) -> None:
        self.cfg = cfg
        LOGGER.info("Audio backend: Kokoro local ONNX (voice=%s)", cfg.kokoro_voice)
        self._backend: TTSBackend = KokoroBackend(cfg=cfg)

    async def generate_scene_audio(
        self, scenes: list[dict[str, Any]], run_id: str
    ) -> list[Path]:
        """Generate one WAV file per scene narration.

        Args:
            scenes: Validated scene list from script package.
            run_id: Unique run identifier for file naming.

        Returns:
            Ordered list of absolute paths to per-scene WAV files.

        Raises:
            AudioGenerationError: If any scene synthesis fails.
        """
        if not scenes:
            raise AudioGenerationError("Scenes list is empty — nothing to synthesize.")

        self.cfg.audio_dir.mkdir(parents=True, exist_ok=True)
        audio_paths: list[Path] = []

        for scene in scenes:
            scene_id = int(scene.get("scene_id", len(audio_paths) + 1))
            narration = str(scene.get("narration", "")).strip()

            if not narration:
                raise AudioGenerationError(
                    f"Scene {scene_id} has an empty narration field."
                )

            output_path = (
                self.cfg.audio_dir / f"{run_id}_scene{scene_id:02d}.wav"
            )
            LOGGER.info(
                "Synthesizing scene %d/%d audio…", scene_id, len(scenes)
            )
            written_path = await self._backend.synthesize(
                text=narration, output_path=output_path
            )
            audio_paths.append(written_path)

        return audio_paths

    async def generate_full_narration(
        self, scenes: list[dict[str, Any]], run_id: str
    ) -> Path:
        """Concatenate all scene narrations into a single continuous WAV.

        Args:
            scenes: Validated scene list from script package.
            run_id: Unique run identifier for file naming.

        Returns:
            Absolute path to the concatenated narration WAV file.

        Raises:
            AudioGenerationError: If synthesis or concatenation fails.
        """
        scene_paths = await self.generate_scene_audio(scenes=scenes, run_id=run_id)
        concat_path = self.cfg.audio_dir / f"{run_id}_narration_full.wav"

        def _concat() -> Path:
            segments: list[np.ndarray] = []
            sample_rate: int | None = None

            for path in scene_paths:
                try:
                    data, sr = sf.read(str(path), dtype="float32")
                except Exception as exc:
                    raise AudioGenerationError(
                        f"Failed to read audio segment {path}: {exc}"
                    ) from exc

                if sample_rate is None:
                    sample_rate = sr
                elif sr != sample_rate:
                    raise AudioGenerationError(
                        f"Inconsistent sample rates across segments: "
                        f"{sr} != {sample_rate} in {path}"
                    )
                segments.append(data)

            if not segments or sample_rate is None:
                raise AudioGenerationError("No audio segments to concatenate.")

            combined = np.concatenate(segments, axis=0)
            concat_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                sf.write(str(concat_path), combined, samplerate=sample_rate)
            except Exception as exc:
                raise AudioGenerationError(
                    f"Failed to write concatenated narration: {exc}"
                ) from exc

            LOGGER.info(
                "Concatenated narration written → %s (%.2fs)",
                concat_path,
                len(combined) / sample_rate,
            )
            return concat_path

        return await asyncio.to_thread(_concat)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def generate_audio(
    config: dict[str, Any],
    scenes: list[dict[str, Any]],
    run_id: str,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Public async entry point for the audio generation stage.

    Args:
        config: Full global config mapping loaded from config.yaml.
        scenes: Validated scene list from the script package output.
        run_id: Unique identifier for this pipeline run.
        env: Environment mapping (unused, kept for API compatibility).

    Returns:
        Dictionary with keys:
          - "scene_paths": list of per-scene WAV paths (strings)
          - "narration_path": path to concatenated narration WAV (string)

    Raises:
        AudioGenerationError: On configuration or synthesis failure.
    """
    cfg = AudioGenConfig.from_mapping(config)
    generator = AudioGenerator(cfg=cfg)

    # generate_full_narration internally calls generate_scene_audio.
    # Previously generate_scene_audio was called separately here, doubling
    # synthesis time.  Removed — scene paths are reconstructed from the
    # deterministic naming convention used by generate_scene_audio.
    narration_path = await generator.generate_full_narration(
        scenes=scenes, run_id=run_id
    )
    scene_paths = [
        cfg.audio_dir / f"{run_id}_scene{int(s.get('scene_id', i + 1)):02d}.wav"
        for i, s in enumerate(scenes)
    ]

    return {
        "scene_paths": [str(p) for p in scene_paths],
        "narration_path": str(narration_path),
    }
