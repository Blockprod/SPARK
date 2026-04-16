# -*- coding: utf-8 -*-
"""Wan2.1 1.3B text-to-video generation via HuggingFace Diffusers.

Each scene from the script package is rendered independently using WanPipeline
(T2V-1.3B). The model requires ~8.2 GB VRAM — compatible with T4 (Colab free
tier, 16 GB VRAM) and any A-series GPU.

The model is downloaded automatically by Diffusers on first run (~8 GB, ~5 min
on Colab's fast network), and cached at /root/.cache/huggingface/.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

LOGGER = logging.getLogger(__name__)

os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")


class VideoGenerationError(RuntimeError):
    """Raised when clip generation fails unrecoverably."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_NEGATIVE_PROMPT = (
    "Bright tones, overexposed, static, blurred details, subtitles, "
    "style, works, paintings, images, static, overall gray, worst quality, "
    "low quality, JPEG compression residue, ugly, incomplete, deformed, "
    "disfigured, misshapen limbs, fused fingers, still picture, "
    "messy background, walking backwards"
)


@dataclass(slots=True)
class VideoGenConfig:
    """All parameters needed by :class:`VideoGenerator`."""

    model_id: str
    device: str
    height: int
    width: int
    num_frames: int
    num_inference_steps: int
    guidance_scale: float
    fps: float
    seed: int
    clips_dir: Path
    use_cpu_offload: bool
    max_scene_duration_sec: int
    negative_prompt: str

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
        scenes_cfg = video_cfg.get("scenes", {})
        clips_dir = Path(str(paths_cfg.get("clips_dir", "./outputs/clips"))).resolve()

        return cls(
            model_id=str(video_cfg.get("model_id", "Wan-AI/Wan2.1-T2V-1.3B-Diffusers")),
            device=str(video_cfg.get("device", "cuda")),
            height=int(pipeline_cfg.get("target_height", 832)),
            width=int(pipeline_cfg.get("target_width", 480)),
            num_frames=int(video_cfg.get("num_frames", 81)),
            num_inference_steps=int(video_cfg.get("num_inference_steps", 25)),
            guidance_scale=float(video_cfg.get("guidance_scale", 5.0)),
            fps=float(pipeline_cfg.get("target_fps", 16)),
            seed=int(gen_cfg.get("seed", -1)),
            clips_dir=clips_dir,
            use_cpu_offload=bool(video_cfg.get("use_cpu_offload", True)),
            max_scene_duration_sec=int(scenes_cfg.get("max_scene_duration_sec", 5)),
            negative_prompt=str(
                video_cfg.get("negative_prompt", _DEFAULT_NEGATIVE_PROMPT)
            ),
        )


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


class VideoGenerator:
    """Wan2.1 1.3B WanPipeline wrapper for per-scene video generation."""

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

        for scene in scenes:
            scene_id = int(scene.get("scene_id", len(clip_paths) + 1))
            output_path = self.cfg.clips_dir / f"{run_id}_scene{scene_id:02d}.mp4"

            LOGGER.info("Generating scene %d/%d", scene_id, len(scenes))

            try:
                clip_path = await self._render_scene(
                    scene=scene,
                    output_path=output_path,
                    scene_id=scene_id,
                    seed=effective_seed,
                )
                clip_paths.append(clip_path)
                LOGGER.info("Scene %d OK -> %s", scene_id, clip_path.name)
            except Exception as exc:
                LOGGER.error(
                    "SCENE %d FAILED [%s: %s] -- using placeholder.",
                    scene_id, type(exc).__name__, exc,
                )
                LOGGER.debug(
                    "Full traceback for scene %d:", scene_id, exc_info=True
                )
                fallback = await asyncio.to_thread(
                    _generate_static_fallback,
                    output_path,
                    int(scene.get("duration_sec", 5)),
                    int(self.cfg.fps),
                    self.cfg.width,
                    self.cfg.height,
                )
                clip_paths.append(fallback)
                degraded_scenes.append(scene_id)

            if progress_callback is not None:
                try:
                    await progress_callback(
                        "video_scene_done",
                        {
                            "scene_id": scene_id,
                            "total": len(scenes),
                            "clip_path": str(clip_paths[-1]),
                        },
                    )
                except Exception as cb_exc:
                    LOGGER.debug("Progress callback error (non-fatal): %s", cb_exc)

        if degraded_scenes:
            ratio = len(degraded_scenes) / len(scenes)
            LOGGER.warning(
                "Run '%s': %d/%d scene(s) used placeholder: %s",
                run_id, len(degraded_scenes), len(scenes), degraded_scenes,
            )
            if ratio > 0.5:
                raise VideoGenerationError(
                    f"Too many degraded scenes ({len(degraded_scenes)}/{len(scenes)}). "
                    f"Aborting — the output would be unusable. "
                    f"Check GPU memory (nvidia-smi) and model loading logs."
                )

        LOGGER.info(
            "Video generation complete: %d clips for run '%s'.",
            len(clip_paths), run_id,
        )
        return clip_paths, degraded_scenes

    # -- private --

    async def _render_scene(
        self,
        scene: dict[str, Any],
        output_path: Path,
        scene_id: int,
        seed: int,
    ) -> Path:
        """Render a single scene using WanPipeline in a background thread."""

        def _run() -> Path:
            try:
                import torch
            except ImportError as exc:
                raise VideoGenerationError(
                    "PyTorch is not installed. Run: pip install torch"
                ) from exc

            try:
                pipeline = self._get_pipeline()
            except VideoGenerationError:
                raise
            except Exception as exc:
                raise VideoGenerationError(
                    f"Pipeline load failed for scene {scene_id}: {exc}"
                ) from exc

            prompt = str(
                scene.get("visual_prompt", scene.get("narration", ""))
            ).strip()
            if not prompt:
                raise VideoGenerationError(f"Scene {scene_id}: empty visual_prompt")

            duration = min(
                int(scene.get("duration_sec", 5)),
                self.cfg.max_scene_duration_sec,
            )
            num_frames = _compute_num_frames(duration, self.cfg.fps)

            LOGGER.info(
                "Scene %d: %dx%d | %d frames @%dfps | seed=%d | prompt=%r",
                scene_id,
                self.cfg.width,
                self.cfg.height,
                num_frames,
                int(self.cfg.fps),
                seed + scene_id,
                prompt[:80],
            )

            try:
                generator = torch.Generator(device="cpu").manual_seed(
                    seed + scene_id
                )
                result = pipeline(
                    prompt=prompt,
                    negative_prompt=self.cfg.negative_prompt,
                    height=self.cfg.height,
                    width=self.cfg.width,
                    num_frames=num_frames,
                    num_inference_steps=self.cfg.num_inference_steps,
                    guidance_scale=self.cfg.guidance_scale,
                    generator=generator,
                )
                frames = result.frames[0]
            except Exception as exc:
                # If inference hit a CUDA error, clear GPU state so the
                # next scene can attempt inference with a clean context.
                import gc
                if torch.cuda.is_available():
                    gc.collect()
                    torch.cuda.empty_cache()
                raise VideoGenerationError(
                    f"Wan2.1 inference failed for scene {scene_id}: {exc}"
                ) from exc

            try:
                _save_frames_to_mp4(frames, output_path, self.cfg.fps)
            except Exception as exc:
                raise VideoGenerationError(
                    f"Video encoding failed for scene {scene_id}: {exc}"
                ) from exc

            if not output_path.exists() or output_path.stat().st_size < 1024:
                raise VideoGenerationError(
                    f"Output file missing or empty after encoding: {output_path}"
                )

            return output_path

        return await asyncio.to_thread(_run)

    def _get_pipeline(self) -> Any:
        """Lazily load WanPipeline once and cache it across all scenes.

        Memory strategy (enable_model_cpu_offload — Kaggle T4):
          - All sub-models loaded to CPU RAM in bfloat16 (0 VRAM during load).
          - During inference each sub-module auto-migrates to GPU just before
            its forward() call and returns to CPU immediately after.
          - Peak VRAM ≈ 2.6 GB (transformer only). Fits on T4 (14.56 GB).
          - Requires ~25 GB CPU RAM (Kaggle T4 provides ~30 GB). ✓
        """
        if self._pipeline is not None:
            return self._pipeline

        import gc
        import torch
        from diffusers import WanPipeline
        from diffusers.schedulers.scheduling_unipc_multistep import (
            UniPCMultistepScheduler,
        )

        model_id = self.cfg.model_id
        LOGGER.info(
            "Loading Wan2.1 pipeline: %s (sequential CPU offload)…",
            model_id,
        )
        try:
            # Warm up CUDA context so cuBLAS can initialise cleanly before
            # any model weights touch the GPU.
            if torch.cuda.is_available():
                _w = torch.zeros(1, device="cuda")
                del _w
                torch.cuda.empty_cache()

            # Load all sub-models to CPU RAM in bfloat16.
            # low_cpu_mem_usage=True avoids double-buffering during weight
            # copying, keeping peak CPU RAM ≈ total model size (~25 GB).
            pipe = WanPipeline.from_pretrained(
                model_id,
                torch_dtype=torch.bfloat16,
                low_cpu_mem_usage=True,
            )
            # flow_shift: 3.0 for 480 P, 5.0 for 720 P
            pipe.scheduler = UniPCMultistepScheduler.from_config(
                pipe.scheduler.config,
                flow_shift=3.0,
            )
            # Sequential offload: PyTorch hooks move each individual layer
            # to GPU just before its forward() call, then back to CPU.
            # The UMT5-XXL text encoder is 22.7 GB bfloat16 — it cannot fit
            # on the T4 (14.56 GB) as a whole sub-model, but its individual
            # layers (~940 MB each) fit easily.  Peak VRAM < 1 GB.
            # Slower than model_cpu_offload but the only option that avoids
            # CUBLAS_STATUS_ALLOC_FAILED caused by whole-encoder OOM.
            pipe.enable_sequential_cpu_offload()

        except Exception as exc:
            gc.collect()
            torch.cuda.empty_cache()
            raise VideoGenerationError(
                f"Failed to load Wan2.1 pipeline '{model_id}': {exc}"
            ) from exc

        LOGGER.info("Wan2.1 pipeline loaded (sequential_cpu_offload).")
        self._pipeline = pipe
        return self._pipeline


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _compute_num_frames(duration_sec: int, fps: float) -> int:
    """Frame count satisfying the 4k+1 constraint required by Wan2.1."""
    raw = int(duration_sec * fps)
    k = max(1, round((raw - 1) / 4))
    adjusted = 4 * k + 1
    # Cap at 129 frames (~8 sec at 16 fps) to stay within T4 VRAM budget
    return max(17, min(adjusted, 129))


def _save_frames_to_mp4(
    frames: list,
    output_path: Path,
    fps: float,
) -> None:
    """Write a list of PIL images (or numpy arrays) to an MP4 file via OpenCV."""
    if not frames:
        raise VideoGenerationError("No frames to save.")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    first = frames[0]
    if hasattr(first, "size"):       # PIL Image: .size = (width, height)
        w, h = first.size
    else:
        arr = np.array(first)
        h, w = arr.shape[:2]

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(output_path), fourcc, fps, (w, h))
    if not writer.isOpened():
        raise VideoGenerationError(
            f"cv2.VideoWriter failed to open: {output_path}"
        )

    for frame in frames:
        if hasattr(frame, "size"):
            arr = np.array(frame)
        else:
            arr = frame
        if arr.dtype != np.uint8:
            arr = (arr * 255).clip(0, 255).astype(np.uint8)
        bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        writer.write(bgr)

    writer.release()


def _generate_static_fallback(
    output_path: Path,
    duration_sec: int,
    fps: int,
    width: int,
    height: int,
) -> Path:
    """Generate a solid dark-grey fallback clip when inference fails."""
    num_frames = max(1, duration_sec * fps)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(output_path), fourcc, fps, (width, height))
    frame = np.full((height, width, 3), 30, dtype=np.uint8)
    for _ in range(num_frames):
        writer.write(frame)
    writer.release()
    return output_path


# ---------------------------------------------------------------------------
# Public module-level entry point (called by pipeline.py)
# ---------------------------------------------------------------------------


async def generate_video_clips(
    config: dict[str, Any],
    scenes: list[dict[str, Any]],
    run_id: str,
    progress_callback: Any | None = None,
) -> tuple[list[Path], list[int]]:
    """Module-level wrapper expected by pipeline.py _stage_video."""
    cfg = VideoGenConfig.from_mapping(config)
    generator = VideoGenerator(cfg)
    return await generator.generate_clips(
        scenes=scenes,
        run_id=run_id,
        progress_callback=progress_callback,
    )
