# -*- coding: utf-8 -*-
"""LTX-2.3 video clip generation via official ltx-pipelines.

Each scene from the script package is rendered independently using the
DistilledPipeline (8+4 steps two-stage pipeline with 2x spatial upscaling).
Visual coherence between scenes is maintained via image conditioning:
the last frame of scene N seeds scene N+1.

Required models (downloaded by Colab cell 5a):
  - ltx-2.3-22b-distilled-1.1.safetensors  (22B DiT checkpoint)
  - ltx-2.3-spatial-upscaler-x2-1.1.safetensors
  - google/gemma-3-12b-it-qat-q4_0-unquantized  (text encoder)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image

LOGGER = logging.getLogger(__name__)

# Required for FP8 quantization memory management on CUDA
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")


class VideoGenerationError(RuntimeError):
    """Raised when clip generation fails unrecoverably."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class VideoGenConfig:
    """All parameters needed by :class:`VideoGenerator`."""

    checkpoint_path: str
    spatial_upsampler_path: str
    gemma_root: str
    device: str
    fp8: bool
    seed: int
    fps: float
    width: int       # final output width  (after 2x spatial upscale)
    height: int      # final output height (after 2x spatial upscale)
    max_scene_duration_sec: int
    enable_image_conditioning: bool
    conditioning_strength: float
    clips_dir: Path
    use_static_fallback: bool = False

    @classmethod
    def from_mapping(cls, config: dict[str, Any]) -> "VideoGenConfig":
        video_cfg = config.get("video_generation")
        paths_cfg = config.get("paths")
        pipeline_cfg = config.get("pipeline")

        if not isinstance(video_cfg, dict):
            raise VideoGenerationError("Missing 'video_generation' in configuration.")
        if not isinstance(paths_cfg, dict):
            raise VideoGenerationError("Missing 'paths' in configuration.")
        if not isinstance(pipeline_cfg, dict):
            raise VideoGenerationError("Missing 'pipeline' in configuration.")

        gen_cfg = video_cfg.get("generation", {})
        coherence_cfg = video_cfg.get("coherence", {})
        clips_dir = Path(str(paths_cfg.get("clips_dir", "./outputs/clips"))).resolve()

        return cls(
            checkpoint_path=str(video_cfg.get("checkpoint_path", "")),
            spatial_upsampler_path=str(video_cfg.get("spatial_upsampler_path", "")),
            gemma_root=str(video_cfg.get("gemma_root", "")),
            device=str(video_cfg.get("device", "cuda")),
            fp8=bool(video_cfg.get("fp8", True)),
            seed=int(gen_cfg.get("seed", -1)),
            fps=float(pipeline_cfg.get("target_fps", 25)),
            width=int(pipeline_cfg.get("target_width", 576)),
            height=int(pipeline_cfg.get("target_height", 1024)),
            max_scene_duration_sec=int(
                video_cfg.get("scenes", {}).get("max_scene_duration_sec", 10)
            ),
            enable_image_conditioning=bool(
                coherence_cfg.get("enable_image_conditioning", True)
            ),
            conditioning_strength=float(
                coherence_cfg.get("conditioning_strength", 0.6)
            ),
            clips_dir=clips_dir,
            use_static_fallback=str(
                video_cfg.get("provider", "ltx-2")
            ).lower() == "static",
        )


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


class VideoGenerator:
    """LTX-2.3 DistilledPipeline wrapper with inter-scene conditioning."""

    def __init__(self, cfg: VideoGenConfig) -> None:
        self.cfg = cfg
        self._pipeline: Any = None

    # -- public API --

    async def generate_clips(
        self,
        scenes: list[dict[str, Any]],
        run_id: str,
        progress_callback: Any | None = None,
    ) -> tuple[list[Path], list[int]]:
        if not scenes:
            raise VideoGenerationError("Scenes list is empty -- nothing to generate.")

        self.cfg.clips_dir.mkdir(parents=True, exist_ok=True)

        if self.cfg.seed == -1:
            effective_seed = int(run_id[:8], 16) % (2**32)
            LOGGER.debug("Seed derived from run_id '%s': %d", run_id, effective_seed)
        else:
            effective_seed = self.cfg.seed

        clip_paths: list[Path] = []
        degraded_scenes: list[int] = []
        last_frame_path: str | None = None

        for scene in scenes:
            scene_id = int(scene.get("scene_id", len(clip_paths) + 1))
            output_path = self.cfg.clips_dir / f"{run_id}_scene{scene_id:02d}.mp4"

            use_cond = (
                last_frame_path is not None
                and self.cfg.enable_image_conditioning
            )
            LOGGER.info(
                "Generating scene %d/%d (conditioning=%s)",
                scene_id, len(scenes), use_cond,
            )

            if self.cfg.use_static_fallback:
                clip_path = await asyncio.to_thread(
                    _generate_static_fallback,
                    None, output_path,
                    int(scene.get("duration_sec", 8)),
                    int(self.cfg.fps), self.cfg.width, self.cfg.height,
                )
                degraded_scenes.append(scene_id)
            else:
                try:
                    clip_path = await self._render_scene(
                        scene=scene,
                        output_path=output_path,
                        scene_id=scene_id,
                        seed=effective_seed,
                        conditioning_image_path=last_frame_path if use_cond else None,
                    )
                except Exception as exc:
                    # Log full error so user can see the REAL reason in Colab output
                    LOGGER.error(
                        "SCENE %d FAILED [%s: %s] -- falling back to black placeholder.",
                        scene_id, type(exc).__name__, exc,
                    )
                    LOGGER.debug("Full traceback for scene %d:", scene_id, exc_info=True)
                    clip_path = await asyncio.to_thread(
                        _generate_static_fallback,
                        None, output_path,
                        int(scene.get("duration_sec", 8)),
                        int(self.cfg.fps), self.cfg.width, self.cfg.height,
                    )
                    degraded_scenes.append(scene_id)

            clip_paths.append(clip_path)

            # Extract last frame for inter-scene conditioning
            frame_save = (
                self.cfg.clips_dir / f"{run_id}_scene{scene_id:02d}_lastframe.png"
            )
            last_frame_path = await asyncio.to_thread(
                _extract_and_save_last_frame, clip_path, frame_save,
            )

            if progress_callback is not None:
                try:
                    await progress_callback(
                        "video_scene_done",
                        {
                            "scene_id": scene_id,
                            "total": len(scenes),
                            "clip_path": str(clip_path),
                        },
                    )
                except Exception as cb_exc:
                    LOGGER.debug("Progress callback error (non-fatal): %s", cb_exc)

        if degraded_scenes:
            LOGGER.warning(
                "Run '%s': %d/%d scene(s) fell back to black placeholder: %s\n"
                "If ALL scenes are black: check model paths and LTX-2 package install.",
                run_id, len(degraded_scenes), len(scenes), degraded_scenes,
            )
        LOGGER.info("Video generation complete: %d clips for run '%s'.", len(clip_paths), run_id)
        return clip_paths, degraded_scenes

    # -- private --

    async def _render_scene(
        self,
        scene: dict[str, Any],
        output_path: Path,
        scene_id: int,
        seed: int,
        conditioning_image_path: str | None = None,
    ) -> Path:
        def _run() -> Path:
            # --- Import LTX-2 packages (lazy; fails fast with useful message) ---
            try:
                from ltx_core.model.video_vae import TilingConfig, get_video_chunks_number
                from ltx_pipelines.utils.media_io import encode_video
            except ImportError as exc:
                raise VideoGenerationError(
                    f"LTX-2 packages not installed: {exc}. "
                    "In Colab: pip install -e /content/LTX-2/packages/ltx-core "
                    "-e /content/LTX-2/packages/ltx-pipelines"
                ) from exc

            # --- Load pipeline (cached after first call) ---
            try:
                pipeline = self._get_pipeline()
            except Exception as exc:
                raise VideoGenerationError(
                    f"Pipeline load failed for scene {scene_id}: {exc}"
                ) from exc

            prompt = str(scene.get("visual_prompt", "")).strip()
            if not prompt:
                raise VideoGenerationError(f"Scene {scene_id}: empty visual_prompt")

            duration = min(
                int(scene.get("duration_sec", 8)),
                self.cfg.max_scene_duration_sec,
            )
            num_frames = _compute_num_frames(duration, self.cfg.fps)

            tiling_config = TilingConfig.default()
            video_chunks_number = get_video_chunks_number(num_frames, tiling_config)

            # --- Image conditioning for scene-to-scene visual continuity ---
            images: list = []
            if conditioning_image_path:
                try:
                    # Format: (image_path, start_frame, strength, end_frame)
                    images = [
                        (
                            conditioning_image_path,
                            0,
                            float(self.cfg.conditioning_strength),
                            num_frames - 1,
                        )
                    ]
                    LOGGER.debug(
                        "Conditioning: %s (strength=%.2f)",
                        conditioning_image_path, self.cfg.conditioning_strength,
                    )
                except Exception as cond_exc:
                    LOGGER.warning("Image conditioning skipped: %s", cond_exc)
                    images = []

            LOGGER.info(
                "LTX-2 scene %d: %dx%d | %d frames @%dfps | seed=%d | cond=%s",
                scene_id, self.cfg.width, self.cfg.height,
                num_frames, int(self.cfg.fps), seed + scene_id, bool(images),
            )

            # --- Inference ---
            try:
                video, ltx_audio = pipeline(
                    prompt=prompt,
                    seed=seed + scene_id,
                    height=self.cfg.height,
                    width=self.cfg.width,
                    num_frames=num_frames,
                    frame_rate=self.cfg.fps,
                    images=images,
                    tiling_config=tiling_config,
                )
            except Exception as exc:
                raise VideoGenerationError(
                    f"LTX-2 inference failed for scene {scene_id}: {exc}"
                ) from exc

            # --- Encode to MP4 ---
            try:
                encode_video(
                    video=video,
                    fps=int(self.cfg.fps),
                    audio=ltx_audio,
                    output_path=str(output_path),
                    video_chunks_number=video_chunks_number,
                )
            except Exception as exc:
                raise VideoGenerationError(
                    f"encode_video failed for scene {scene_id}: {exc}"
                ) from exc

            if not output_path.exists():
                raise VideoGenerationError(
                    f"LTX-2 did not produce output file: {output_path}"
                )

            LOGGER.info("Scene %d OK -> %s", scene_id, output_path.name)
            return output_path

        return await asyncio.to_thread(_run)

    def _get_pipeline(self) -> Any:
        """Lazily load the LTX-2 DistilledPipeline (cached for re-use across scenes)."""
        if self._pipeline is None:
            LOGGER.info("Loading LTX-2.3 DistilledPipeline (fp8=%s)...", self.cfg.fp8)

            # Validate model paths before attempting load
            for label, path in [
                ("distilled_checkpoint", self.cfg.checkpoint_path),
                ("spatial_upsampler", self.cfg.spatial_upsampler_path),
                ("gemma_root", self.cfg.gemma_root),
            ]:
                if not path or not Path(path).exists():
                    raise VideoGenerationError(
                        f"LTX-2 {label} not found at: {path!r}. "
                        "Run Colab cell 5a to download models."
                    )

            try:
                from ltx_pipelines.distilled import DistilledPipeline
                from ltx_core.quantization import QuantizationPolicy

                quant = QuantizationPolicy.fp8_cast() if self.cfg.fp8 else None

                self._pipeline = DistilledPipeline(
                    distilled_checkpoint_path=self.cfg.checkpoint_path,
                    spatial_upsampler_path=self.cfg.spatial_upsampler_path,
                    gemma_root=self.cfg.gemma_root,
                    loras=[],
                    quantization=quant,
                )
            except Exception as exc:
                raise VideoGenerationError(
                    f"Failed to load LTX-2 DistilledPipeline: {exc}"
                ) from exc
            LOGGER.info("LTX-2.3 DistilledPipeline loaded successfully.")
        return self._pipeline


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _compute_num_frames(duration_sec: int, fps: float) -> int:
    """Compute frame count satisfying the 8k+1 constraint required by LTX-2."""
    raw = int(duration_sec * fps)
    k = round((raw - 1) / 8)
    adjusted = 8 * k + 1
    return max(9, adjusted)


def _extract_and_save_last_frame(
    clip_path: Path, save_path: Path,
) -> str | None:
    """Extract last frame from MP4 and save as PNG for next-scene conditioning."""
    cap = cv2.VideoCapture(str(clip_path))
    if not cap.isOpened():
        LOGGER.warning("Cannot open clip for frame extraction: %s", clip_path)
        return None
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, total - 1))
    ret, frame_bgr = cap.read()
    cap.release()
    if not ret:
        LOGGER.warning("Cannot read last frame from: %s", clip_path)
        return None
    save_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(save_path), frame_bgr)
    return str(save_path)


def _generate_static_fallback(
    reference_image: Image.Image | None,
    output_path: Path,
    duration_sec: int,
    fps: int,
    width: int,
    height: int,
) -> Path:
    """Generate a static fallback MP4 (solid black or reference frame)."""
    num_frames = max(1, duration_sec * fps)

    if reference_image is not None:
        try:
            frame = reference_image.resize((width, height)).convert("RGB")
            frame_array = np.array(frame, dtype=np.uint8)
        except Exception:
            frame_array = np.zeros((height, width, 3), dtype=np.uint8)
    else:
        frame_array = np.zeros((height, width, 3), dtype=np.uint8)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(output_path), fourcc, float(fps), (width, height))
    if not writer.isOpened():
        raise VideoGenerationError(
            f"Could not open VideoWriter for fallback clip: {output_path}"
        )
    try:
        frame_bgr = cv2.cvtColor(frame_array, cv2.COLOR_RGB2BGR)
        for _ in range(num_frames):
            writer.write(frame_bgr)
    finally:
        writer.release()
    return output_path


def _sanitize_run_id(run_id: str) -> str:
    return re.sub(r"[^\w\-]", "_", run_id)[:64]


# ---------------------------------------------------------------------------
# Public API  (called by pipeline.py)
# ---------------------------------------------------------------------------


async def generate_video_clips(
    config: dict[str, Any],
    scenes: list[dict[str, Any]],
    run_id: str,
    progress_callback: Any | None = None,
) -> tuple[list[Path], list[int]]:
    """Public async entry point for video clip generation."""
    cfg = VideoGenConfig.from_mapping(config)
    generator = VideoGenerator(cfg=cfg)
    return await generator.generate_clips(
        scenes=scenes, run_id=run_id, progress_callback=progress_callback,
    )
