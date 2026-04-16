"""Narrative script generation using Gemini for the KORU universe.

This module transforms an episode dict into a strict JSON payload containing:
- a 50-60 second French narration script
- scene breakdown
- per-scene Wan2.1 cinematic prompts
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types as genai_types
from tenacity import AsyncRetrying, retry_if_not_exception_type, stop_after_attempt, wait_exponential

LOGGER = logging.getLogger(__name__)


class ScriptGenerationError(RuntimeError):
    """Raised when script generation or validation fails."""


class ScriptValidationError(ScriptGenerationError):
    """Raised when Gemini output fails schema or structural validation.

    These errors are deterministic and must NOT be retried by tenacity.
    """


@dataclass(slots=True)
class ScriptGenConfig:
    """Configuration contract for Gemini script generation."""

    model: str
    temperature: float
    top_p: float
    max_output_tokens: int
    min_duration_sec: int
    max_duration_sec: int
    max_scenes: int
    prompts_dir: Path
    acts: list[str] = field(default_factory=lambda: ["creation", "apogee", "chute", "ruines"])

    @classmethod
    def from_mapping(cls, config: dict[str, Any]) -> "ScriptGenConfig":
        """Create a validated ScriptGenConfig from global app config.

        Args:
            config: Parsed config.yaml mapping.

        Returns:
            A ScriptGenConfig instance.

        Raises:
            ScriptGenerationError: If required sections are missing.
        """
        script_cfg = config.get("script_generation")
        pipeline_cfg = config.get("pipeline")
        paths_cfg = config.get("paths")

        if not isinstance(script_cfg, dict):
            raise ScriptGenerationError("Missing 'script_generation' in configuration.")
        if not isinstance(pipeline_cfg, dict):
            raise ScriptGenerationError("Missing 'pipeline' in configuration.")
        if not isinstance(paths_cfg, dict):
            raise ScriptGenerationError("Missing 'paths' in configuration.")

        prompts_dir = Path(str(paths_cfg.get("prompts_dir", "./prompts"))).resolve()

        raw_acts = script_cfg.get("acts", [])
        acts = (
            [str(a) for a in raw_acts]
            if isinstance(raw_acts, list) and raw_acts
            else ["creation", "apogee", "chute", "ruines"]
        )

        return cls(
            model=str(script_cfg.get("model", "gemini-2.5-flash")),
            temperature=float(script_cfg.get("temperature", 0.8)),
            top_p=float(script_cfg.get("top_p", 0.95)),
            max_output_tokens=int(script_cfg.get("max_output_tokens", 4096)),
            min_duration_sec=int(pipeline_cfg.get("min_duration_sec", 50)),
            max_duration_sec=int(pipeline_cfg.get("max_duration_sec", 60)),
            max_scenes=int(pipeline_cfg.get("max_scenes", 8)),
            prompts_dir=prompts_dir,
            acts=acts,
        )


class ScriptGenerator:
    """Gemini-backed script generator with strict JSON validation."""

    def __init__(self, cfg: ScriptGenConfig, env: dict[str, str] | None = None) -> None:
        """Initialize script generator.

        Args:
            cfg: Script generation configuration.
            env: Environment mapping containing GEMINI_API_KEY.
        """
        self.cfg = cfg
        self.env = env or {}
        api_key = self.env.get("GEMINI_API_KEY", "")
        if not api_key:
            raise ScriptGenerationError("Missing GEMINI_API_KEY in environment.")
        self._request_timeout = float(self.env.get("GEMINI_REQUEST_TIMEOUT_SEC", "60"))

        self._client = genai.Client(api_key=api_key)

    async def generate_for_episode(
        self,
        episode: dict[str, Any],
    ) -> dict[str, Any]:
        """Generate a validated narrative package for a KORU episode.

        Args:
            episode: Episode dict with keys ``episode_number``, ``act``,
                ``revelation``, ``implied_parallel``.

        Returns:
            Strictly validated JSON payload ready for video and audio generation.

        Raises:
            ScriptGenerationError: On API, parsing, or schema validation failure.
        """
        if not episode or not isinstance(episode, dict):
            raise ScriptGenerationError("Episode must be a non-empty dict.")
        if "episode_number" not in episode:
            raise ScriptValidationError("Episode dict must contain 'episode_number'.")
        if "act" not in episode:
            raise ScriptValidationError("Episode dict must contain 'act'.")

        system_prompt = self._read_prompt_file("system_script.txt")
        try:
            video_guide = self._read_prompt_file("system_video.txt")
            system_prompt = (
                system_prompt
                + "\n\n---\nART DIRECTION & FEW-SHOT EXAMPLES FOR visual_prompt FIELDS\n"
                + video_guide
            )
        except ScriptGenerationError:
            LOGGER.warning(
                "system_video.txt not found — visual_prompt quality may be lower."
            )
        user_prompt = self._build_user_prompt(episode)

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            retry=retry_if_not_exception_type(ScriptValidationError),
            reraise=True,
        ):
            with attempt:
                raw = await self._generate_raw(system_prompt=system_prompt, user_prompt=user_prompt)
                payload = self._parse_json_response(raw)
                self._validate_payload(payload)
                return payload

        raise ScriptGenerationError("Unexpected script generation retry exhaustion.")

    async def _generate_raw(self, system_prompt: str, user_prompt: str) -> str:
        def _invoke() -> str:
            # Build config with system_instruction set to the KORU bible + art direction.
            # This uses Gemini's dedicated system instruction slot instead of sending
            # the prompt as a "user" message — the model treats it as authoritative context.
            config = genai_types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=self.cfg.temperature,
                top_p=self.cfg.top_p,
                max_output_tokens=self.cfg.max_output_tokens,
                response_mime_type="application/json",
            )
            try:
                response = self._client.models.generate_content(
                    model=self.cfg.model,
                    contents=user_prompt,
                    config=config,
                )
            except Exception as exc:
                raise ScriptGenerationError(f"Gemini request failed: {exc}") from exc

            text = getattr(response, "text", "")
            if not text:
                raise ScriptGenerationError("Gemini returned an empty response body.")
            return text.strip()

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_invoke),
                timeout=self._request_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise ScriptGenerationError(
                f"Gemini request timed out after {self._request_timeout}s"
            ) from exc

    def _read_prompt_file(self, filename: str) -> str:
        path = self.cfg.prompts_dir / filename
        if not path.exists():
            raise ScriptGenerationError(f"Prompt file not found: {path}")
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            raise ScriptGenerationError(f"Prompt file is empty: {path}")
        return content

    def _build_user_prompt(self, episode: dict[str, Any]) -> str:
        return json.dumps(episode, ensure_ascii=False)

    def _parse_json_response(self, raw: str) -> dict[str, Any]:
        candidate = raw.strip()

        # Handle occasional fenced blocks despite response_mime_type JSON contract.
        if candidate.startswith("```"):
            candidate = candidate.strip("`")
            candidate = candidate.replace("json\n", "", 1).strip()

        # Extract the outermost JSON object — guards against thinking-model preamble
        # or trailing commentary that wraps the actual payload.
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = candidate[start : end + 1]

        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise ScriptValidationError(f"Invalid JSON from Gemini: {exc}") from exc

        if not isinstance(payload, dict):
            raise ScriptValidationError("Gemini payload must be a JSON object.")
        return payload

    def _validate_payload(self, payload: dict[str, Any]) -> None:
        required_top = {
            "episode_number",
            "act",
            "title",
            "hook",
            "narration_text",
            "language",
            "duration_sec",
            "scenes",
            "metadata",
        }
        missing = sorted(required_top - set(payload.keys()))
        if missing:
            raise ScriptValidationError(
                f"Generated payload missing required fields: {', '.join(missing)}"
            )

        duration = payload.get("duration_sec")
        if not isinstance(duration, int):
            raise ScriptValidationError("'duration_sec' must be an integer.")
        if duration < self.cfg.min_duration_sec or duration > self.cfg.max_duration_sec:
            raise ScriptValidationError(
                f"duration_sec must be between {self.cfg.min_duration_sec} and "
                f"{self.cfg.max_duration_sec}. Got: {duration}"
            )

        language = payload.get("language")
        if language != "fr":
            raise ScriptValidationError("'language' must be exactly 'fr'.")

        scenes = payload.get("scenes")
        if not isinstance(scenes, list) or not scenes:
            raise ScriptValidationError("'scenes' must be a non-empty list.")
        if len(scenes) > self.cfg.max_scenes:
            raise ScriptValidationError(
                f"Number of scenes exceeds max_scenes={self.cfg.max_scenes}."
            )

        accumulated_duration = 0
        for idx, scene in enumerate(scenes, start=1):
            self._validate_scene(scene, idx)
            accumulated_duration += int(scene["duration_sec"])

        if abs(accumulated_duration - duration) > 2:
            # Gemini often returns a mismatched duration_sec vs scene sum.
            # Auto-correct if the actual sum is within the valid pipeline range,
            # rather than failing the entire pipeline run on a cosmetic mismatch.
            if self.cfg.min_duration_sec <= accumulated_duration <= self.cfg.max_duration_sec:
                LOGGER.warning(
                    "duration_sec mismatch (total=%ds vs declared=%ds) — "
                    "auto-correcting duration_sec to actual scene sum.",
                    accumulated_duration,
                    duration,
                )
                payload["duration_sec"] = accumulated_duration
            else:
                raise ScriptValidationError(
                    "Sum of scene durations must approximately match duration_sec "
                    f"(difference <= 2s). total={accumulated_duration}, duration={duration}"
                )

        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            raise ScriptValidationError("'metadata' must be a JSON object.")

        required_metadata = {"youtube_title", "youtube_description", "youtube_tags"}
        missing_meta = sorted(required_metadata - set(metadata.keys()))
        if missing_meta:
            raise ScriptValidationError(
                f"metadata missing required fields: {', '.join(missing_meta)}"
            )

        tags = metadata.get("youtube_tags")
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise ScriptValidationError("metadata.youtube_tags must be a list of strings.")

        if not (5 <= len(tags) <= 12):
            raise ScriptValidationError(
                f"metadata.youtube_tags must contain 5 to 12 tags. Got: {len(tags)}"
            )

    def _validate_scene(self, scene: Any, index: int) -> None:
        if not isinstance(scene, dict):
            raise ScriptValidationError(f"Scene #{index} must be an object.")

        required = {
            "scene_id",
            "duration_sec",
            "narration",
            "visual_prompt",
            "camera_movement",
            "lighting_mood",
            "historical_era",
            "cinematic_style",
        }
        missing = sorted(required - set(scene.keys()))
        if missing:
            raise ScriptValidationError(
                f"Scene #{index} missing fields: {', '.join(missing)}"
            )

        duration = scene.get("duration_sec")
        if not isinstance(duration, int) or duration <= 0:
            raise ScriptValidationError(f"Scene #{index} has invalid duration_sec.")

        for field in (
            "narration",
            "visual_prompt",
            "camera_movement",
            "lighting_mood",
            "historical_era",
            "cinematic_style",
        ):
            value = scene.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ScriptValidationError(f"Scene #{index} field '{field}' must be non-empty.")


async def generate_script_package(
    config: dict[str, Any],
    env: dict[str, str],
    episode: dict[str, Any],
) -> dict[str, Any]:
    """Public async API for KORU episode script generation.

    Args:
        config: Full project config mapping loaded from config.yaml.
        env: Environment mapping with Gemini credentials.
        episode: Episode dict with keys ``episode_number``, ``act``,
            ``revelation``, ``implied_parallel``.

    Returns:
        A validated JSON payload suitable for video/audio pipeline stages.
    """
    cfg = ScriptGenConfig.from_mapping(config)
    generator = ScriptGenerator(cfg=cfg, env=env)
    payload = await generator.generate_for_episode(episode=episode)
    payload["ai_generated"] = True  # internal traceability — all content is fully AI-generated
    LOGGER.info(
        "Generated script package for episode #%d (act: %s) with %d scenes.",
        episode.get("episode_number", 0),
        episode.get("act", ""),
        len(payload.get("scenes", [])),
    )
    return payload
