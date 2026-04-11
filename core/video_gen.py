"""Local video clip generation using LTX-Video-2.3.

Each scene from the script package is rendered independently as a portrait MP4
(1080x1920 @ 24fps). Visual coherence between scenes is maintained via
image-to-video conditioning: the last frame of scene N seeds scene N+1.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import torch
from diffusers import LTXImageToVideoPipeline, LTXPipeline
from diffusers.utils import export_to_video
from PIL import Image

LOGGER = logging.getLogger(__name__)


class VideoGenerationError(RuntimeError):
    """Raised when clip generation fails unrecoverably."""


@dataclass(slots=True)
class VideoGenConfig:
    """All parameters needed by :class:`VideoGenerator`."""

    model_id: str
    device: str
    dtype: torch.dtype
    num_inference_steps: int
    guidance_scale: float
    seed: int
    fps: int
    width: int
    height: int
    max_scene_duration_sec: int
    negative_prompt: str
    enable_image_conditioning: bool
    conditioning_strength: float
    clips_dir: Path

    @classmethod
    def from_mapping(cls, config: dict[str, Any]) -> "VideoGenConfig":
        """Build a VideoGenConfig from the global app config mapping.

        Args:
            config: Parsed config.yaml mapping.

        Returns:
            A validated VideoGenConfig instance.

        Raises:
            VideoGenerationError: If required config sections are missing.
        """
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

        dtype_str = str(video_cfg.get("dtype", "float16")).lower()
        dtype_map: dict[str, torch.dtype] = {
            "float16": torch.float16,
            "bfloat16": torch.bfloat16,
            "float32": torch.float32,
        }
        dtype = dtype_map.get(dtype_str, torch.float16)

        clips_dir = Path(str(paths_cfg.get("clips_dir", "./outputs/clips"))).resolve()

        return cls(
            model_id=str(video_cfg.get("model_id", "Lightricks/LTX-Video-2.3")),
            device=str(video_cfg.get("device", "cuda")),
            dtype=dtype,
            num_inference_steps=int(gen_cfg.get("num_inference_steps", 30)),
            guidance_scale=float(gen_cfg.get("guidance_scale", 6.5)),
            seed=int(gen_cfg.get("seed", -1)),
            fps=int(pipeline_cfg.get("target_fps", 24)),
            width=int(pipeline_cfg.get("target_width", 1080)),
            height=int(pipeline_cfg.get("target_height", 1920)),
            max_scene_duration_sec=int(
                video_cfg.get("scenes", {}).get("max_scene_duration_sec", 10)
            ),
            negative_prompt=str(
                gen_cfg.get(
                    "negative_prompt",
                    "blurry, low resolution, watermark, text artifacts, flicker, frame jitter",
                )
            ),
            enable_image_conditioning=bool(
                coherence_cfg.get("enable_image_conditioning", True)
            ),
            conditioning_strength=float(coherence_cfg.get("conditioning_strength", 0.6)),
            clips_dir=clips_dir,
        )


class VideoGenerator:
    """LTX-Video-2.3 local pipeline wrapper with inter-scene conditioning."""

    def __init__(self, cfg: VideoGenConfig) -> None:
        """Initialize the video generator (pipelines loaded lazily on first use).

        Args:
            cfg: Validated video generation configuration.
        """
        self.cfg = cfg
        self._t2v: LTXPipeline | None = None
        self._i2v: LTXImageToVideoPipeline | None = None

    async def generate_clips(
        self, scenes: list[dict[str, Any]], run_id: str, progress_callback: Any | None = None
    ) -> list[Path]:
        """Generate one MP4 clip per scene and return ordered list of paths.

        Args:
            scenes: Validated scene list from the script package (dicts with
                    visual_prompt, duration_sec, and optional LTX fields).
            run_id: Unique run identifier used for output file naming.

        Returns:
            List of absolute Paths to generated MP4 clip files, in scene order.

        Raises:
            VideoGenerationError: If any scene fails and cannot be recovered.
        """
        if not scenes:
            raise VideoGenerationError("Scenes list is empty — nothing to generate.")

        self.cfg.clips_dir.mkdir(parents=True, exist_ok=True)

        # Compute effective seed: -1 means derive deterministically from run_id
        # so each run produces distinct visuals even for the same topic.
        if self.cfg.seed == -1:
            effective_seed = int(run_id[:8], 16) % (2 ** 32)
            LOGGER.debug("Seed derived from run_id '%s': %d", run_id, effective_seed)
        else:
            effective_seed = self.cfg.seed

        clip_paths: list[Path] = []
        conditioning_image: Image.Image | None = None

        degraded_scenes: list[int] = []

        # Capture the running event loop here (in the async context) so we can
        # post progress events from inside asyncio.to_thread workers.
        _loop = asyncio.get_event_loop()

        async def _emit_scene_done(scene_id: int, clip_path: Path, total: int) -> None:
            if progress_callback is not None:
                try:
                    await progress_callback(
                        "video_scene_done",
                        {"scene_id": scene_id, "total": total, "clip_path": str(clip_path)},
                    )
                except Exception as cb_exc:
                    LOGGER.debug("video progress callback error (non-fatal): %s", cb_exc)

        for scene in scenes:
            scene_id = int(scene.get("scene_id", len(clip_paths) + 1))
            use_conditioning = (
                self.cfg.enable_image_conditioning
                and conditioning_image is not None
            )
            LOGGER.info(
                "Generating scene %d/%d (conditioning=%s)",
                scene_id,
                len(scenes),
                use_conditioning,
            )

            output_path = self.cfg.clips_dir / f"{run_id}_scene{scene_id:02d}.mp4"

            try:
                if use_conditioning:
                    clip_path = await self._render_i2v(
                        scene=scene,
                        conditioning_image=conditioning_image,
                        output_path=output_path,
                        scene_id=scene_id,
                        seed=effective_seed,
                    )
                else:
                    clip_path = await self._render_t2v(
                        scene=scene,
                        output_path=output_path,
                        scene_id=scene_id,
                        seed=effective_seed,
                    )
            except VideoGenerationError as exc:
                LOGGER.warning(
                    "Scene %d generation failed (%s) — using static fallback clip.",
                    scene_id,
                    exc,
                )
                clip_path = await asyncio.to_thread(
                    _generate_static_fallback,
                    conditioning_image,
                    output_path,
                    int(scene.get("duration_sec", 8)),
                    self.cfg.fps,
                    self.cfg.width,
                    self.cfg.height,
                )
                degraded_scenes.append(scene_id)

            clip_paths.append(clip_path)
            await _emit_scene_done(scene_id, clip_path, len(scenes))
            conditioning_image = await asyncio.to_thread(
                _extract_last_frame, clip_path
            )

        if degraded_scenes:
            LOGGER.warning(
                "Run '%s' degraded: %d scene(s) used static fallback: %s",
                run_id,
                len(degraded_scenes),
                degraded_scenes,
            )
        LOGGER.info("All %d clips generated for run '%s'.", len(clip_paths), run_id)
        return clip_paths, degraded_scenes

    async def _render_t2v(
        self,
        scene: dict[str, Any],
        output_path: Path,
        scene_id: int,
        seed: int,
    ) -> Path:
        def _run() -> Path:
            pipeline = self._get_t2v_pipeline()
            prompt = _build_full_prompt(scene)
            num_frames = _scene_num_frames(
                duration_sec=int(scene.get("duration_sec", 8)),
                fps=self.cfg.fps,
                max_duration_sec=self.cfg.max_scene_duration_sec,
            )
            generator = torch.Generator(device=self.cfg.device).manual_seed(
                seed + scene_id
            )
            try:
                output = pipeline(
                    prompt=prompt,
                    negative_prompt=self.cfg.negative_prompt,
                    width=self.cfg.width,
                    height=self.cfg.height,
                    num_frames=num_frames,
                    num_inference_steps=self.cfg.num_inference_steps,
                    guidance_scale=self.cfg.guidance_scale,
                    generator=generator,
                )
            except Exception as exc:
                raise VideoGenerationError(
                    f"T2V generation failed for scene {scene_id}: {exc}"
                ) from exc

            frames = _unwrap_frames(output)
            export_to_video(frames, str(output_path), fps=self.cfg.fps)
            LOGGER.debug("Scene %d saved → %s", scene_id, output_path)
            return output_path

        return await asyncio.to_thread(_run)

    async def _render_i2v(
        self,
        scene: dict[str, Any],
        conditioning_image: Image.Image,
        output_path: Path,
        scene_id: int,
        seed: int,
    ) -> Path:
        def _run() -> Path:
            pipeline = self._get_i2v_pipeline()
            prompt = _build_full_prompt(scene)
            num_frames = _scene_num_frames(
                duration_sec=int(scene.get("duration_sec", 8)),
                fps=self.cfg.fps,
                max_duration_sec=self.cfg.max_scene_duration_sec,
            )
            generator = torch.Generator(device=self.cfg.device).manual_seed(
                seed + scene_id
            )
            resized_image = conditioning_image.resize(
                (self.cfg.width, self.cfg.height), Image.LANCZOS
            )
            try:
                output = pipeline(
                    image=resized_image,
                    prompt=prompt,
                    negative_prompt=self.cfg.negative_prompt,
                    width=self.cfg.width,
                    height=self.cfg.height,
                    num_frames=num_frames,
                    num_inference_steps=self.cfg.num_inference_steps,
                    guidance_scale=self.cfg.guidance_scale,
                    strength=self.cfg.conditioning_strength,
                    generator=generator,
                )
            except Exception as exc:
                raise VideoGenerationError(
                    f"I2V generation failed for scene {scene_id}: {exc}"
                ) from exc

            frames = _unwrap_frames(output)
            export_to_video(frames, str(output_path), fps=self.cfg.fps)
            LOGGER.debug("Scene %d (i2v) saved → %s", scene_id, output_path)
            return output_path

        return await asyncio.to_thread(_run)

    def _get_t2v_pipeline(self) -> LTXPipeline:
        if self._t2v is None:
            LOGGER.info("Loading LTX T2V pipeline from '%s'…", self.cfg.model_id)
            try:
                self._t2v = LTXPipeline.from_pretrained(
                    self.cfg.model_id, torch_dtype=self.cfg.dtype
                )
                self._t2v.enable_model_cpu_offload()
            except Exception as exc:
                raise VideoGenerationError(
                    f"Failed to load T2V pipeline: {exc}"
                ) from exc
            LOGGER.info("T2V pipeline loaded.")
        return self._t2v

    def _get_i2v_pipeline(self) -> LTXImageToVideoPipeline:
        if self._i2v is None:
            LOGGER.info("Loading LTX I2V pipeline from '%s'…", self.cfg.model_id)
            try:
                self._i2v = LTXImageToVideoPipeline.from_pretrained(
                    self.cfg.model_id, torch_dtype=self.cfg.dtype
                )
                self._i2v.enable_model_cpu_offload()
            except Exception as exc:
                raise VideoGenerationError(
                    f"Failed to load I2V pipeline: {exc}"
                ) from exc
            LOGGER.info("I2V pipeline loaded.")
        return self._i2v


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _build_full_prompt(scene: dict[str, Any]) -> str:
    """Assemble the richest possible LTX prompt from all scene fields.

    Args:
        scene: Scene dict from validated script payload.

    Returns:
        A single dense English prompt string.
    """
    parts: list[str] = []

    visual = str(scene.get("visual_prompt", "")).strip()
    if visual:
        parts.append(visual)

    for field_name in ("camera_movement", "lighting_mood", "historical_era", "cinematic_style"):
        value = str(scene.get(field_name, "")).strip()
        if value:
            parts.append(value)

    return ". ".join(p.rstrip(".") for p in parts if p)


def _scene_num_frames(
    duration_sec: int,
    fps: int,
    max_duration_sec: int,
) -> int:
    """Compute a valid num_frames (must satisfy 4k+1 constraint for LTX-Video).

    Args:
        duration_sec: Desired duration in seconds.
        fps: Target frames per second.
        max_duration_sec: Hard cap on scene duration.

    Returns:
        Number of frames satisfying frame_count ≡ 1 (mod 4).
    """
    clamped = min(duration_sec, max_duration_sec)
    raw = clamped * fps
    # Adjust to nearest value satisfying n = 4k + 1
    remainder = raw % 4
    if remainder == 0:
        adjusted = raw + 1
    elif remainder == 1:
        adjusted = raw
    else:
        adjusted = raw + (4 - remainder) + 1
    return max(5, int(adjusted))


def _unwrap_frames(output: Any) -> list[Image.Image]:
    """Extract frames list from a diffusers pipeline output.

    Args:
        output: Raw diffusers pipeline return value.

    Returns:
        Flat list of PIL Image frames for the first video.

    Raises:
        VideoGenerationError: If frames cannot be extracted.
    """
    frames_attr = getattr(output, "frames", None)
    if frames_attr is None:
        raise VideoGenerationError(
            "Pipeline output has no 'frames' attribute — unexpected diffusers API."
        )

    first = frames_attr[0] if frames_attr else []
    if not first:
        raise VideoGenerationError("Pipeline returned empty frames list.")
    return list(first)


def _extract_last_frame(clip_path: Path) -> Image.Image | None:
    """Read the last frame of an MP4 clip for i2v conditioning.

    Args:
        clip_path: Path to an existing MP4 file.

    Returns:
        The last frame as a PIL Image, or None if extraction fails.
    """
    cap = cv2.VideoCapture(str(clip_path))
    if not cap.isOpened():
        LOGGER.warning("Could not open clip for last-frame extraction: %s", clip_path)
        return None

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    target = max(0, total - 1)
    cap.set(cv2.CAP_PROP_POS_FRAMES, target)
    ret, frame_bgr = cap.read()
    cap.release()

    if not ret:
        LOGGER.warning("Could not read last frame from clip: %s", clip_path)
        return None

    frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    return Image.fromarray(frame_rgb)


def _sanitize_run_id(run_id: str) -> str:
    """Strip unsafe filesystem characters from a run ID.

    Args:
        run_id: Raw run identifier string.

    Returns:
        Alphanumeric-and-underscores-only run identifier.
    """
    return re.sub(r"[^\w\-]", "_", run_id)[:64]


def _generate_static_fallback(
    reference_image: "Image.Image | None",
    output_path: Path,
    duration_sec: int,
    fps: int,
    width: int,
    height: int,
) -> Path:
    """Generate a static fallback MP4 clip from a reference frame (or black).

    Used when LTX-Video generation fails for a scene. Produces a clip of the
    correct duration filled with the last available conditioning frame, or a
    solid black frame if none is available.

    Args:
        reference_image: PIL Image to use as static frame, or None for black.
        output_path: Target MP4 path to write.
        duration_sec: Desired clip duration in seconds.
        fps: Frames per second.
        width: Frame width in pixels.
        height: Frame height in pixels.

    Returns:
        Path to the written fallback MP4.

    Raises:
        VideoGenerationError: If writing fails.
    """
    import numpy as np

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


async def generate_video_clips(
    config: dict[str, Any],
    scenes: list[dict[str, Any]],
    run_id: str,
    progress_callback: Any | None = None,
) -> tuple[list[Path], list[int]]:
    """Public async entry point for video clip generation.

    Args:
        config: Full global config mapping loaded from config.yaml.
        scenes: Validated scene list from the script package output.
        run_id: Unique identifier for this pipeline run (used in filenames).
        progress_callback: Optional async callable(stage, data) for SSE progress
                           reporting. Called after each scene clip is rendered.

    Returns:
        Tuple of (clip_paths, degraded_scene_ids) where clip_paths is the ordered
        list of generated MP4 paths and degraded_scene_ids lists any scene IDs
        that fell back to a static frame due to GPU errors.

    Raises:
        VideoGenerationError: On configuration error or generation failure.
    """
    safe_id = _sanitize_run_id(run_id)
    cfg = VideoGenConfig.from_mapping(config)
    generator = VideoGenerator(cfg=cfg)
    clip_paths, degraded_scene_ids = await generator.generate_clips(
        scenes=scenes, run_id=safe_id, progress_callback=progress_callback
    )
    return clip_paths, degraded_scene_ids
