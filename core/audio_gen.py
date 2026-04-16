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
    active_backend: str

    # Kokoro
    kokoro_model_path: str
    kokoro_voices_path: str
    kokoro_voice: str
    kokoro_speed: float
    kokoro_sample_rate: int
    kokoro_output_format: str

    # Edge TTS
    edge_tts_voice: str
    edge_tts_rate: str
    edge_tts_pitch: str

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
        edge_cfg = audio_cfg.get("edge_tts", {})
        audio_dir = Path(str(paths_cfg.get("audio_dir", "./outputs/audio"))).resolve()

        return cls(
            language=str(audio_cfg.get("language", "fr-fr")),
            active_backend=str(audio_cfg.get("active_backend", "edge_tts")),
            kokoro_model_path=str(kokoro_cfg.get("model_path", "./models/kokoro-v1.0.fp16.onnx")),
            kokoro_voices_path=str(kokoro_cfg.get("voices_path", "./models/voices-v1.0.bin")),
            kokoro_voice=str(kokoro_cfg.get("voice", "ff_siwis")),
            kokoro_speed=float(kokoro_cfg.get("speed", 1.0)),
            kokoro_sample_rate=int(kokoro_cfg.get("sample_rate", 24000)),
            kokoro_output_format=str(kokoro_cfg.get("output_format", "wav")),
            edge_tts_voice=str(edge_cfg.get("voice", "fr-FR-DeniseNeural")),
            edge_tts_rate=str(edge_cfg.get("rate", "+0%")),
            edge_tts_pitch=str(edge_cfg.get("pitch", "+0Hz")),
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
            text: Narration text to synthesize (plain UTF-8).
            output_path: Absolute path for the output audio file.

        Returns:
            Path to the written audio file.

        Raises:
            AudioGenerationError: On synthesis or I/O failure.
        """


def _add_ssml_pauses(text: str) -> str:
    """Add SSML-style pauses after sentence-ending punctuation for Edge TTS.

    Edge TTS supports inline SSML breaks. This inserts short pauses after
    periods, exclamation marks, and question marks to give a more natural
    narration rhythm instead of a flat machine-gun delivery.
    """
    import re
    # Add 350ms pause after sentence-ending punctuation followed by a space
    result = re.sub(
        r'([.!?…])\s+',
        r'\1 <break time="350ms"/> ',
        text,
    )
    return result


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
# Edge TTS backend (Microsoft neural TTS — free, requires internet)
# ---------------------------------------------------------------------------


class EdgeTTSBackend(TTSBackend):
    """Edge TTS via the `edge-tts` package. Excellent French voices.

    Voices recommended for French:
      - fr-FR-DeniseNeural  (female, warm, clear)
      - fr-FR-HenriNeural   (male, professional)
      - fr-FR-VivienneMultilingualNeural (female, expressive, multilingual)
    """

    def __init__(self, cfg: AudioGenConfig) -> None:
        self.cfg = cfg

    async def synthesize(self, text: str, output_path: Path) -> Path:
        """Synthesize speech via Edge TTS and write MP3 to output_path.

        Args:
            text: Plain French narration text.
            output_path: Target audio file path (will use .mp3 extension).

        Returns:
            Path to the written audio file.

        Raises:
            AudioGenerationError: If synthesis or write fails.
        """
        if not text or not text.strip():
            raise AudioGenerationError("Cannot synthesize empty narration text.")

        try:
            import edge_tts
        except ImportError as exc:
            raise AudioGenerationError(
                "edge-tts is not installed. Run: pip install edge-tts"
            ) from exc

        # Edge TTS always produces MP3 — use .mp3 extension regardless of config
        mp3_path = output_path.with_suffix(".mp3")
        mp3_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            communicate = edge_tts.Communicate(
                text=_add_ssml_pauses(text.strip()),
                voice=self.cfg.edge_tts_voice,
                rate=self.cfg.edge_tts_rate,
                pitch=self.cfg.edge_tts_pitch,
            )
            await communicate.save(str(mp3_path))
        except Exception as exc:
            raise AudioGenerationError(
                f"Edge TTS synthesis failed: {exc}"
            ) from exc

        if not mp3_path.exists() or mp3_path.stat().st_size < 100:
            raise AudioGenerationError(
                f"Edge TTS produced empty or missing file: {mp3_path}"
            )

        LOGGER.debug(
            "Edge TTS wrote %s (voice=%s)", mp3_path, self.cfg.edge_tts_voice
        )
        return mp3_path

    async def synthesize_with_timestamps(
        self, text: str, output_path: Path,
    ) -> tuple[Path, list[dict[str, Any]]]:
        """Synthesize speech and capture WordBoundary events for subtitle alignment.

        Returns:
            Tuple of (audio_path, word_boundaries) where word_boundaries is a list
            of dicts with keys: offset_ms, duration_ms, text.
        """
        if not text or not text.strip():
            raise AudioGenerationError("Cannot synthesize empty narration text.")

        try:
            import edge_tts
        except ImportError as exc:
            raise AudioGenerationError(
                "edge-tts is not installed. Run: pip install edge-tts"
            ) from exc

        mp3_path = output_path.with_suffix(".mp3")
        mp3_path.parent.mkdir(parents=True, exist_ok=True)

        word_boundaries: list[dict[str, Any]] = []

        communicate = edge_tts.Communicate(
            text=_add_ssml_pauses(text.strip()),
            voice=self.cfg.edge_tts_voice,
            rate=self.cfg.edge_tts_rate,
            pitch=self.cfg.edge_tts_pitch,
        )

        try:
            with open(str(mp3_path), "wb") as f:
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        f.write(chunk["data"])
                    elif chunk["type"] == "WordBoundary":
                        word_boundaries.append({
                            "offset_ms": chunk["offset"] // 10_000,  # 100ns ticks → ms
                            "duration_ms": chunk["duration"] // 10_000,
                            "text": chunk["text"],
                        })
        except Exception as exc:
            raise AudioGenerationError(
                f"Edge TTS streaming synthesis failed: {exc}"
            ) from exc

        if not mp3_path.exists() or mp3_path.stat().st_size < 100:
            raise AudioGenerationError(
                f"Edge TTS produced empty or missing file: {mp3_path}"
            )

        LOGGER.debug(
            "Edge TTS wrote %s with %d word boundaries", mp3_path, len(word_boundaries)
        )
        return mp3_path, word_boundaries


# ---------------------------------------------------------------------------
# AudioGenerator orchestrator
# ---------------------------------------------------------------------------


class AudioGenerator:
    """Orchestrates TTS synthesis for all scenes in a script package."""

    def __init__(self, cfg: AudioGenConfig) -> None:
        self.cfg = cfg
        backend_name = cfg.active_backend.lower().strip()
        if backend_name == "edge_tts":
            LOGGER.info(
                "Audio backend: Edge TTS (voice=%s rate=%s)",
                cfg.edge_tts_voice, cfg.edge_tts_rate,
            )
            self._backend: TTSBackend = EdgeTTSBackend(cfg=cfg)
        else:
            LOGGER.info("Audio backend: Kokoro local ONNX (voice=%s)", cfg.kokoro_voice)
            self._backend = KokoroBackend(cfg=cfg)

    async def generate_scene_audio(
        self, scenes: list[dict[str, Any]], run_id: str
    ) -> tuple[list[Path], list[list[dict[str, Any]]]]:
        """Generate one audio file per scene narration.

        Args:
            scenes: Validated scene list from script package.
            run_id: Unique run identifier for file naming.

        Returns:
            Tuple of (audio_paths, word_boundaries_per_scene).
            word_boundaries_per_scene is a list of word boundary lists (one per scene).
            Each boundary dict has keys: offset_ms, duration_ms, text.
            Empty lists for non-Edge-TTS backends.

        Raises:
            AudioGenerationError: If any scene synthesis fails.
        """
        if not scenes:
            raise AudioGenerationError("Scenes list is empty — nothing to synthesize.")

        self.cfg.audio_dir.mkdir(parents=True, exist_ok=True)
        audio_paths: list[Path] = []
        all_word_boundaries: list[list[dict[str, Any]]] = []

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

            if isinstance(self._backend, EdgeTTSBackend):
                written_path, boundaries = await self._backend.synthesize_with_timestamps(
                    text=narration, output_path=output_path
                )
                all_word_boundaries.append(boundaries)
            else:
                written_path = await self._backend.synthesize(
                    text=narration, output_path=output_path
                )
                all_word_boundaries.append([])

            audio_paths.append(written_path)

        return audio_paths, all_word_boundaries

    async def generate_full_narration(
        self, scenes: list[dict[str, Any]], run_id: str
    ) -> tuple[Path, list[Path], list[list[dict[str, Any]]]]:
        """Concatenate all scene narrations into a single continuous audio file.

        Args:
            scenes: Validated scene list from script package.
            run_id: Unique run identifier for file naming.

        Returns:
            Tuple of (narration_path, scene_paths, word_boundaries_per_scene).

        Raises:
            AudioGenerationError: If synthesis or concatenation fails.
        """
        scene_paths, word_boundaries = await self.generate_scene_audio(scenes=scenes, run_id=run_id)
        # Detect output extension from actual files produced (wav or mp3)
        out_ext = scene_paths[0].suffix if scene_paths else ".wav"
        concat_path = self.cfg.audio_dir / f"{run_id}_narration_full{out_ext}"

        def _concat() -> Path:
            concat_path.parent.mkdir(parents=True, exist_ok=True)
            if out_ext == ".wav":
                # Kokoro path: read float32 arrays and concatenate with soundfile
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
                            f"Inconsistent sample rates: {sr} vs {sample_rate} in {path}"
                        )
                    segments.append(data)
                if not segments or sample_rate is None:
                    raise AudioGenerationError("No audio segments to concatenate.")
                combined = np.concatenate(segments, axis=0)
                try:
                    sf.write(str(concat_path), combined, samplerate=sample_rate)
                except Exception as exc:
                    raise AudioGenerationError(
                        f"Failed to write concatenated narration: {exc}"
                    ) from exc
                dur = len(combined) / sample_rate
            else:
                # Edge TTS / MP3 path: use ffmpeg concat demuxer (handles any format)
                import subprocess
                list_file = self.cfg.audio_dir / f"{run_id}_concat_list.txt"
                list_file.write_text(
                    "".join(f"file '{p.as_posix()}'\n" for p in scene_paths),
                    encoding="utf-8",
                )
                try:
                    result = subprocess.run(
                        [
                            "ffmpeg", "-y",
                            "-f", "concat", "-safe", "0",
                            "-i", str(list_file),
                            "-c", "copy",
                            str(concat_path),
                        ],
                        check=True,
                        capture_output=True,
                    )
                except subprocess.CalledProcessError as exc:
                    stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
                    raise AudioGenerationError(
                        f"FFmpeg audio concat failed: {stderr}"
                    ) from exc
                finally:
                    list_file.unlink(missing_ok=True)
                dur = 0.0  # duration computed later by post_prod ffprobe

            LOGGER.info("Full narration written -> %s (%.1fs)", concat_path, dur)
            return concat_path

        narration_path = await asyncio.to_thread(_concat)
        return narration_path, scene_paths, word_boundaries


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
          - "scene_paths": list of per-scene audio paths (strings)
          - "narration_path": path to concatenated narration audio (string)
          - "word_boundaries": list of per-scene word boundary lists

    Raises:
        AudioGenerationError: On configuration or synthesis failure.
    """
    cfg = AudioGenConfig.from_mapping(config)
    generator = AudioGenerator(cfg=cfg)

    narration_path, scene_paths, word_boundaries = await generator.generate_full_narration(
        scenes=scenes, run_id=run_id
    )

    return {
        "scene_paths": [str(p) for p in scene_paths],
        "narration_path": str(narration_path),
        "word_boundaries": word_boundaries,
    }
