"""Post-production pipeline for shorts-engine.

Responsibilities:
  1. Concatenate per-scene MP4 clips into a single timeline.
  2. Mix voice-over WAV with LTX-native ambient audio track.
  3. Generate a timed SRT subtitle file from scene narrations.
  4. Burn soft subtitles and export the final MP4 (H264/AAC).

All heavy lifting is delegated to FFmpeg via ffmpeg-python.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ffmpeg
import pysubs2

LOGGER = logging.getLogger(__name__)


class PostProductionError(RuntimeError):
    """Raised when a post-production step fails."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PostProdConfig:
    """All FFmpeg and subtitle parameters for the post-production stage."""

    ffmpeg_bin: str
    ffprobe_bin: str
    video_codec: str
    audio_codec: str
    crf: int
    preset: str
    audio_bitrate: str
    pix_fmt: str
    threads: int

    # Subtitle style
    subtitles_enabled: bool
    max_chars_per_line: int
    max_lines: int
    line_padding_ms: int
    font_name: str
    font_size: int
    primary_color: str
    outline_color: str
    back_color: str
    outline: int
    shadow: int
    alignment: int
    margin_v: int

    # Output paths
    renders_dir: Path
    temp_dir: Path

    @classmethod
    def from_mapping(cls, config: dict[str, Any]) -> "PostProdConfig":
        """Build PostProdConfig from global app config.

        Args:
            config: Parsed config.yaml mapping.

        Returns:
            Validated PostProdConfig instance.

        Raises:
            PostProductionError: If required config sections are missing.
        """
        pp_cfg = config.get("post_production")
        paths_cfg = config.get("paths")

        if not isinstance(pp_cfg, dict):
            raise PostProductionError("Missing 'post_production' in configuration.")
        if not isinstance(paths_cfg, dict):
            raise PostProductionError("Missing 'paths' in configuration.")

        ff_cfg = pp_cfg.get("ffmpeg", {})
        sub_cfg = pp_cfg.get("subtitles", {})
        style_cfg = sub_cfg.get("style", {})

        renders_dir = Path(str(paths_cfg.get("renders_dir", "./outputs/renders"))).resolve()
        temp_dir = Path(str(paths_cfg.get("temp_dir", "./outputs/tmp"))).resolve()

        return cls(
            ffmpeg_bin=str(ff_cfg.get("ffmpeg_bin", "ffmpeg")),
            ffprobe_bin=str(ff_cfg.get("ffprobe_bin", "ffprobe")),
            video_codec=str(ff_cfg.get("video_codec", "libx264")),
            audio_codec=str(ff_cfg.get("audio_codec", "aac")),
            crf=int(ff_cfg.get("crf", 20)),
            preset=str(ff_cfg.get("preset", "medium")),
            audio_bitrate=str(ff_cfg.get("audio_bitrate", "192k")),
            pix_fmt=str(ff_cfg.get("pix_fmt", "yuv420p")),
            threads=int(ff_cfg.get("threads", 0)),
            subtitles_enabled=bool(sub_cfg.get("enabled", True)),
            max_chars_per_line=int(sub_cfg.get("max_chars_per_line", 42)),
            max_lines=int(sub_cfg.get("max_lines", 2)),
            line_padding_ms=int(sub_cfg.get("line_padding_ms", 120)),
            font_name=str(style_cfg.get("font_name", "Arial")),
            font_size=int(style_cfg.get("font_size", 54)),
            primary_color=str(style_cfg.get("primary_color", "&H00FFFFFF")),
            outline_color=str(style_cfg.get("outline_color", "&H00000000")),
            back_color=str(style_cfg.get("back_color", "&H64000000")),
            outline=int(style_cfg.get("outline", 2)),
            shadow=int(style_cfg.get("shadow", 0)),
            alignment=int(style_cfg.get("alignment", 2)),
            margin_v=int(style_cfg.get("margin_v", 120)),
            renders_dir=renders_dir,
            temp_dir=temp_dir,
        )


# ---------------------------------------------------------------------------
# Core post-production class
# ---------------------------------------------------------------------------


class PostProducer:
    """Assembles clips, mixes audio, generates SRT, and exports final MP4."""

    def __init__(self, cfg: PostProdConfig) -> None:
        """Initialize post-producer.

        Args:
            cfg: Validated post-production configuration.
        """
        self.cfg = cfg

    async def produce(
        self,
        clip_paths: list[Path],
        narration_path: Path,
        scenes: list[dict[str, Any]],
        run_id: str,
    ) -> dict[str, str]:
        """Run the full post-production pipeline.

        Args:
            clip_paths: Ordered list of per-scene MP4 clip files.
            narration_path: Concatenated narration WAV file.
            scenes: Validated scene list from script package (for SRT generation).
            run_id: Unique run identifier for output filenames.

        Returns:
            Dictionary with keys:
              - "final_video": absolute path to the exported MP4
              - "srt_file": absolute path to the generated SRT file (or "" if disabled)

        Raises:
            PostProductionError: On any FFmpeg or I/O failure.
        """
        self.cfg.renders_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.temp_dir.mkdir(parents=True, exist_ok=True)

        if not clip_paths:
            raise PostProductionError("clip_paths is empty — nothing to assemble.")
        if not narration_path.exists():
            raise PostProductionError(
                f"Narration file not found: {narration_path}"
            )

        concat_path = await self._concatenate_clips(clip_paths, run_id)
        srt_path = await self._generate_srt(scenes, run_id)
        final_path = await self._mix_and_export(
            video_path=concat_path,
            narration_path=narration_path,
            srt_path=srt_path if self.cfg.subtitles_enabled else None,
            run_id=run_id,
        )

        return {
            "final_video": str(final_path),
            "srt_file": str(srt_path) if self.cfg.subtitles_enabled else "",
        }

    async def _concatenate_clips(
        self, clip_paths: list[Path], run_id: str
    ) -> Path:
        """Concatenate multiple MP4 clips using FFmpeg concat demuxer.

        Args:
            clip_paths: Ordered list of clip file paths.
            run_id: Run identifier for temp file naming.

        Returns:
            Path to the concatenated (silent) MP4 file.

        Raises:
            PostProductionError: If any clip is missing or FFmpeg fails.
        """
        for path in clip_paths:
            if not path.exists():
                raise PostProductionError(f"Clip file not found: {path}")

        concat_list_path = self.cfg.temp_dir / f"{run_id}_concat.txt"
        concat_video_path = self.cfg.temp_dir / f"{run_id}_concat.mp4"

        list_content = "\n".join(
            f"file '{str(p).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'"
            for p in clip_paths
        )
        concat_list_path.write_text(list_content, encoding="utf-8")

        LOGGER.info("Concatenating %d clips…", len(clip_paths))

        def _run() -> Path:
            try:
                (
                    ffmpeg
                    .input(str(concat_list_path), format="concat", safe=0)
                    .output(
                        str(concat_video_path),
                        vcodec="copy",
                        an=None,
                    )
                    .overwrite_output()
                    .run(
                        cmd=self.cfg.ffmpeg_bin,
                        quiet=True,
                        capture_stdout=True,
                        capture_stderr=True,
                    )
                )
            except ffmpeg.Error as exc:
                stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
                raise PostProductionError(
                    f"FFmpeg concat failed: {stderr}"
                ) from exc
            return concat_video_path

        return await asyncio.to_thread(_run)

    async def _generate_srt(
        self, scenes: list[dict[str, Any]], run_id: str
    ) -> Path:
        """Build a timed SRT file from scene narration texts.

        Args:
            scenes: Scene list with 'narration' and 'duration_sec' fields.
            run_id: Run identifier for output filename.

        Returns:
            Path to the written SRT file.

        Raises:
            PostProductionError: If scene data is malformed.
        """
        srt_path = self.cfg.temp_dir / f"{run_id}_subtitles.srt"

        def _build() -> Path:
            subs = pysubs2.SSAFile()
            subs.info["WrapStyle"] = "0"

            current_ms = 0
            for scene in scenes:
                duration_sec = int(scene.get("duration_sec", 0))
                narration = str(scene.get("narration", "")).strip()

                if not narration or duration_sec <= 0:
                    current_ms += duration_sec * 1000
                    continue

                end_ms = current_ms + duration_sec * 1000
                lines = _wrap_text(
                    narration,
                    max_chars=self.cfg.max_chars_per_line,
                    max_lines=self.cfg.max_lines,
                )

                for chunk_idx, chunk in enumerate(lines):
                    chunk_start = current_ms + chunk_idx * (
                        (duration_sec * 1000) // max(len(lines), 1)
                    )
                    chunk_end = min(
                        chunk_start
                        + (duration_sec * 1000) // max(len(lines), 1)
                        - self.cfg.line_padding_ms,
                        end_ms,
                    )
                    if chunk_start >= chunk_end:
                        continue

                    event = pysubs2.SSAEvent(
                        start=chunk_start,
                        end=chunk_end,
                        text=chunk,
                    )
                    subs.append(event)

                current_ms = end_ms

            _apply_ass_style(subs, self.cfg)
            srt_path.parent.mkdir(parents=True, exist_ok=True)
            subs.save(str(srt_path), format_="srt")
            LOGGER.debug("SRT written → %s", srt_path)
            return srt_path

        return await asyncio.to_thread(_build)

    async def _mix_and_export(
        self,
        video_path: Path,
        narration_path: Path,
        srt_path: Path | None,
        run_id: str,
    ) -> Path:
        """Mix voice-over with ambient video audio, add soft subtitles, export MP4.

        Strategy:
          - Keep LTX-native ambient audio at -18 dBFS.
          - Mix narration voice-over at -3 dBFS.
          - If SRT is provided, burn subtitles via the `subtitles` filter.

        Args:
            video_path: Concatenated silent/ambient MP4.
            narration_path: Continuous narration WAV.
            srt_path: Optional SRT file path. None means subtitles are disabled.
            run_id: Run identifier for output naming.

        Returns:
            Path to the final exported MP4.

        Raises:
            PostProductionError: If FFmpeg fails.
        """
        final_path = self.cfg.renders_dir / f"{run_id}_final.mp4"

        def _run() -> Path:
            video_stream = ffmpeg.input(str(video_path))
            narration_stream = ffmpeg.input(str(narration_path))

            # Video — optionally burn subtitles
            v = video_stream.video
            if srt_path is not None and srt_path.exists():
                srt_escaped = str(srt_path).replace("\\", "/").replace(":", "\\:")
                v = v.filter(
                    "subtitles",
                    filename=srt_escaped,
                    force_style=(
                        f"FontName={self.cfg.font_name},"
                        f"FontSize={self.cfg.font_size},"
                        f"PrimaryColour={self.cfg.primary_color},"
                        f"OutlineColour={self.cfg.outline_color},"
                        f"BackColour={self.cfg.back_color},"
                        f"Outline={self.cfg.outline},"
                        f"Shadow={self.cfg.shadow},"
                        f"Alignment={self.cfg.alignment},"
                        f"MarginV={self.cfg.margin_v}"
                    ),
                )

            # Audio mixing: ambient @ -18 dBFS + narration @ -3 dBFS
            has_audio = _probe_has_audio(str(video_path))
            if has_audio:
                ambient = video_stream.audio.filter("volume", volume="-18dB")
                narration = narration_stream.audio.filter("volume", volume="-3dB")
                mixed_audio = ffmpeg.filter(
                    [ambient, narration],
                    "amix",
                    inputs=2,
                    duration="first",
                    dropout_transition=2,
                )
            else:
                mixed_audio = narration_stream.audio.filter(
                    "volume", volume="-3dB"
                )

            output_kwargs = {
                "vcodec": self.cfg.video_codec,
                "acodec": self.cfg.audio_codec,
                "crf": str(self.cfg.crf),
                "preset": self.cfg.preset,
                "audio_bitrate": self.cfg.audio_bitrate,
                "pix_fmt": self.cfg.pix_fmt,
            }
            if self.cfg.threads > 0:
                output_kwargs["threads"] = str(self.cfg.threads)

            try:
                (
                    ffmpeg
                    .output(v, mixed_audio, str(final_path), **output_kwargs)
                    .overwrite_output()
                    .run(
                        cmd=self.cfg.ffmpeg_bin,
                        quiet=True,
                        capture_stdout=True,
                        capture_stderr=True,
                    )
                )
            except ffmpeg.Error as exc:
                stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
                raise PostProductionError(
                    f"FFmpeg final export failed: {stderr}"
                ) from exc

            LOGGER.info("Final video exported → %s", final_path)
            return final_path

        return await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wrap_text(text: str, max_chars: int, max_lines: int) -> list[str]:
    """Split narration text into subtitle chunks respecting line length.

    Args:
        text: Full narration text for a scene.
        max_chars: Maximum characters per subtitle line.
        max_lines: Maximum lines per subtitle event.

    Returns:
        List of subtitle chunks (preserves word boundaries).
    """
    words = re.split(r"\s+", text.strip())
    chunks: list[str] = []
    current_line_words: list[str] = []
    current_line_len = 0
    lines_in_current_chunk: list[str] = []

    for word in words:
        if current_line_len + len(word) + (1 if current_line_words else 0) > max_chars:
            lines_in_current_chunk.append(" ".join(current_line_words))
            current_line_words = [word]
            current_line_len = len(word)
            if len(lines_in_current_chunk) >= max_lines:
                chunks.append("\\N".join(lines_in_current_chunk))
                lines_in_current_chunk = []
        else:
            current_line_words.append(word)
            current_line_len += len(word) + (1 if len(current_line_words) > 1 else 0)

    if current_line_words:
        lines_in_current_chunk.append(" ".join(current_line_words))
    if lines_in_current_chunk:
        chunks.append("\\N".join(lines_in_current_chunk))

    return chunks if chunks else [text]


def _apply_ass_style(subs: pysubs2.SSAFile, cfg: PostProdConfig) -> None:
    """Set default ASS subtitle style on a pysubs2 SSAFile.

    Args:
        subs: The SSAFile to modify in place.
        cfg: Post-production configuration with style fields.
    """
    style = pysubs2.SSAStyle(
        fontname=cfg.font_name,
        fontsize=cfg.font_size,
        primarycolor=pysubs2.Color(255, 255, 255, 0),
        outlinecolor=pysubs2.Color(0, 0, 0, 0),
        backcolor=pysubs2.Color(0, 0, 0, 100),
        outline=cfg.outline,
        shadow=cfg.shadow,
        alignment=cfg.alignment,
        marginv=cfg.margin_v,
        bold=False,
    )
    subs.styles["Default"] = style


def _probe_has_audio(file_path: str) -> bool:
    """Check whether a media file contains an audio stream.

    Args:
        file_path: Absolute path to the media file.

    Returns:
        True if at least one audio stream is present, False otherwise.
    """
    try:
        probe = ffmpeg.probe(file_path)
        streams = probe.get("streams", [])
        return any(s.get("codec_type") == "audio" for s in streams)
    except ffmpeg.Error:
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_post_production(
    config: dict[str, Any],
    clip_paths: list[Path],
    narration_path: Path,
    scenes: list[dict[str, Any]],
    run_id: str,
) -> dict[str, str]:
    """Public async entry point for post-production.

    Args:
        config: Full global config mapping loaded from config.yaml.
        clip_paths: Ordered list of per-scene MP4 files from video_gen.
        narration_path: Full narration WAV from audio_gen.
        scenes: Validated scene list from script package (for SRT timing).
        run_id: Unique identifier for this pipeline run.

    Returns:
        Dictionary with:
          - "final_video": absolute path to the final MP4
          - "srt_file": absolute path to the SRT file (empty string if disabled)

    Raises:
        PostProductionError: On configuration error or FFmpeg failure.
    """
    cfg = PostProdConfig.from_mapping(config)
    producer = PostProducer(cfg=cfg)
    result = await producer.produce(
        clip_paths=clip_paths,
        narration_path=narration_path,
        scenes=scenes,
        run_id=run_id,
    )
    return result
