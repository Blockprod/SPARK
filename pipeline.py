"""Async end-to-end pipeline orchestrator for shorts-engine.

Usage (standalone):
    python pipeline.py --topic "Algorithmes de recommandation" --upload

Usage (from scheduler or dashboard):
    from pipeline import run_pipeline
    result = await run_pipeline(config, env, topic="...", upload=False)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from rich.logging import RichHandler

LOGGER = logging.getLogger("pipeline")


class PipelineError(RuntimeError):
    """Raised when a pipeline stage fails and the run must be aborted."""


# ---------------------------------------------------------------------------
# Config + env loading
# ---------------------------------------------------------------------------


def load_config(config_path: str = "config.yaml") -> dict[str, Any]:
    """Load and return the parsed config.yaml mapping.

    Args:
        config_path: Relative or absolute path to config.yaml.

    Returns:
        Parsed configuration dictionary.

    Raises:
        PipelineError: If the file is missing or unparseable.
    """
    path = Path(config_path).resolve()
    if not path.exists():
        raise PipelineError(f"Configuration file not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
    except Exception as exc:
        raise PipelineError(f"Failed to parse config.yaml: {exc}") from exc
    if not isinstance(cfg, dict):
        raise PipelineError("config.yaml must be a YAML mapping at the top level.")
    return cfg


def load_env(env_path: str = ".env") -> dict[str, str]:
    """Load .env file into the process environment and return a snapshot.

    Args:
        env_path: Path to the .env file (optional — skipped if absent).

    Returns:
        Dictionary snapshot of the current environment after loading.
    """
    path = Path(env_path)
    if path.exists():
        load_dotenv(dotenv_path=str(path), override=False)
    return dict(os.environ)


# ---------------------------------------------------------------------------
# JSON run logger
# ---------------------------------------------------------------------------


def _setup_logging(config: dict[str, Any], run_id: str) -> Path:
    """Configure structured JSON logging to file and rich console.

    Args:
        config: Global app config.
        run_id: Unique run ID used for the log filename.

    Returns:
        Path to the JSON log file.
    """
    logs_dir = Path(str(config.get("paths", {}).get("logs_dir", "./logs"))).resolve()
    logs_dir.mkdir(parents=True, exist_ok=True)

    level_str = str(config.get("logging", {}).get("level", "INFO")).upper()
    level = getattr(logging, level_str, logging.INFO)

    log_file = logs_dir / f"run_{run_id}.jsonl"

    class _JsonlHandler(logging.FileHandler):
        def emit(self, record: logging.LogRecord) -> None:
            entry = {
                "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
                "run_id": run_id,
            }
            if record.exc_info:
                entry["exc"] = self.format(record)
            try:
                self.stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
                self.flush()
            except Exception:
                self.handleError(record)

    logging.basicConfig(
        level=level,
        handlers=[
            RichHandler(rich_tracebacks=True, show_path=False),
            _JsonlHandler(str(log_file), encoding="utf-8"),
        ],
        format="%(message)s",
        datefmt="[%X]",
        force=True,
    )
    return log_file


# ---------------------------------------------------------------------------
# Pipeline stages (thin wrappers with structured logging)
# ---------------------------------------------------------------------------


async def _stage_trends(
    config: dict[str, Any],
    env: dict[str, str],
    run_ctx: dict[str, Any],
) -> list[dict[str, Any]]:
    from core.trend_hunter import get_ranked_topics

    LOGGER.info("[trends] Fetching ranked topics…")
    try:
        topics = await get_ranked_topics(config=config, env=env, limit=20)
    except Exception as exc:
        raise PipelineError(f"Trend discovery failed: {exc}") from exc
    run_ctx["topics_fetched"] = len(topics)
    LOGGER.info("[trends] %d topics ranked.", len(topics))
    return topics


async def _stage_script(
    config: dict[str, Any],
    env: dict[str, str],
    topic: str,
    trend_context: dict[str, Any],
    run_ctx: dict[str, Any],
) -> dict[str, Any]:
    from core.script_gen import generate_script_package

    LOGGER.info("[script] Generating script for topic: %s", topic)
    try:
        payload = await generate_script_package(
            config=config,
            env=env,
            topic=topic,
            trend_context=trend_context,
        )
    except Exception as exc:
        raise PipelineError(f"Script generation failed: {exc}") from exc
    run_ctx["scenes"] = len(payload.get("scenes", []))
    run_ctx["duration_sec"] = payload.get("duration_sec", 0)
    run_ctx["script_payload"] = payload
    LOGGER.info(
        "[script] Script ready: %d scenes, %ds.",
        run_ctx["scenes"],
        run_ctx["duration_sec"],
    )
    return payload


async def _stage_video(
    config: dict[str, Any],
    scenes: list[dict[str, Any]],
    run_id: str,
    run_ctx: dict[str, Any],
) -> list[Path]:
    from core.video_gen import generate_video_clips

    LOGGER.info("[video] Generating %d clips…", len(scenes))
    try:
        clip_paths = await generate_video_clips(
            config=config, scenes=scenes, run_id=run_id
        )
    except Exception as exc:
        raise PipelineError(f"Video generation failed: {exc}") from exc
    run_ctx["clip_paths"] = [str(p) for p in clip_paths]
    LOGGER.info("[video] %d clips generated.", len(clip_paths))
    return clip_paths


async def _stage_audio(
    config: dict[str, Any],
    env: dict[str, str],
    scenes: list[dict[str, Any]],
    run_id: str,
    run_ctx: dict[str, Any],
) -> dict[str, Any]:
    from core.audio_gen import generate_audio

    LOGGER.info("[audio] Synthesizing narration for %d scenes…", len(scenes))
    try:
        audio_result = await generate_audio(
            config=config, scenes=scenes, run_id=run_id, env=env
        )
    except Exception as exc:
        raise PipelineError(f"Audio generation failed: {exc}") from exc
    run_ctx["narration_path"] = audio_result["narration_path"]
    LOGGER.info("[audio] Narration ready: %s", audio_result["narration_path"])
    return audio_result


async def _stage_post_prod(
    config: dict[str, Any],
    clip_paths: list[Path],
    narration_path: Path,
    scenes: list[dict[str, Any]],
    run_id: str,
    run_ctx: dict[str, Any],
) -> dict[str, str]:
    from core.post_prod import run_post_production

    LOGGER.info("[postprod] Assembling final video…")
    try:
        result = await run_post_production(
            config=config,
            clip_paths=clip_paths,
            narration_path=narration_path,
            scenes=scenes,
            run_id=run_id,
        )
    except Exception as exc:
        raise PipelineError(f"Post-production failed: {exc}") from exc
    run_ctx["final_video"] = result["final_video"]
    run_ctx["srt_file"] = result.get("srt_file", "")
    LOGGER.info("[postprod] Final video: %s", result["final_video"])
    return result


async def _stage_upload(
    config: dict[str, Any],
    env: dict[str, str],
    video_path: Path,
    script_payload: dict[str, Any],
    publish_at: datetime | None,
    run_ctx: dict[str, Any],
) -> dict[str, Any]:
    from core.uploader import upload_to_youtube

    LOGGER.info("[upload] Uploading to YouTube…")
    try:
        response = await upload_to_youtube(
            config=config,
            env=env,
            video_path=video_path,
            script_payload=script_payload,
            publish_at=publish_at,
        )
    except Exception as exc:
        raise PipelineError(f"YouTube upload failed: {exc}") from exc
    video_id = response.get("id", "")
    run_ctx["youtube_video_id"] = video_id
    run_ctx["youtube_url"] = f"https://youtu.be/{video_id}"
    LOGGER.info("[upload] Published: https://youtu.be/%s", video_id)
    return response


async def _stage_thumbnail(
    config: dict[str, Any],
    env: dict[str, str],
    clip_paths: list[Path],
    script_payload: dict[str, Any],
    video_id: str,
    run_ctx: dict[str, Any],
) -> None:
    from core.thumbnail_gen import generate_and_upload_thumbnail, ThumbnailError

    LOGGER.info("[thumbnail] Generating and uploading thumbnail…")
    try:
        jpeg_path = await generate_and_upload_thumbnail(
            config=config,
            env=env,
            clip_paths=clip_paths,
            script_payload=script_payload,
            video_id=video_id,
        )
        run_ctx["thumbnail_path"] = jpeg_path
    except ThumbnailError as exc:
        LOGGER.warning("[thumbnail] Skipped (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


async def run_pipeline(
    config: dict[str, Any],
    env: dict[str, str],
    topic: str | None = None,
    upload: bool = False,
    publish_at: datetime | None = None,
    run_id: str | None = None,
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    """Execute the full shorts-engine pipeline end-to-end.

    Args:
        config: Parsed config.yaml mapping.
        env: Environment mapping with all credentials.
        topic: Pre-selected topic string. When None, the top trending topic is used.
        upload: Whether to upload the final video to YouTube.
        publish_at: UTC datetime for scheduled YouTube publication.
        run_id: Optional run identifier (UUID generated if not provided).
        progress_callback: Optional async callable(stage: str, data: dict) for
                           real-time SSE progress reporting from the dashboard.

    Returns:
        Dictionary with run metadata, paths, and optional YouTube response.

    Raises:
        PipelineError: On any unrecoverable stage failure.
    """
    run_id = run_id or uuid.uuid4().hex[:12]
    log_file = _setup_logging(config, run_id)

    LOGGER.info("=== Pipeline run %s started ===", run_id)

    run_ctx: dict[str, Any] = {
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "topic": topic,
        "log_file": str(log_file),
        "status": "running",
    }

    async def _progress(stage: str, data: dict[str, Any] | None = None) -> None:
        if progress_callback is not None:
            try:
                await progress_callback(stage, data or {})
            except Exception as cb_exc:
                LOGGER.debug("Progress callback error (non-fatal): %s", cb_exc)

    try:
        # Stage 1 — Trends
        await _progress("trends_start", {})
        topics = await _stage_trends(config, env, run_ctx)
        await _progress("trends_done", {"count": len(topics)})

        if not topic:
            if not topics:
                raise PipelineError("No trending topics found and no topic was provided.")
            topic = topics[0]["topic"]
            LOGGER.info("Auto-selected top topic: %s", topic)
        run_ctx["topic"] = topic

        trend_context = next(
            (t for t in topics if t.get("topic", "").lower() == topic.lower()),
            {},
        )

        # Stage 2 — Script
        await _progress("script_start", {"topic": topic})
        script_payload = await _stage_script(config, env, topic, trend_context, run_ctx)
        await _progress("script_done", {"scenes": run_ctx.get("scenes", 0)})

        scenes = script_payload["scenes"]

        # Stage 3 — Video
        await _progress("video_start", {"scenes": len(scenes)})
        clip_paths = await _stage_video(config, scenes, run_id, run_ctx)
        await _progress("video_done", {"clips": len(clip_paths)})

        # Stage 4 — Audio
        await _progress("audio_start", {})
        audio_result = await _stage_audio(config, env, scenes, run_id, run_ctx)
        narration_path = Path(audio_result["narration_path"])
        await _progress("audio_done", {"narration": str(narration_path)})

        # Stage 5 — Post-production
        await _progress("postprod_start", {})
        postprod_result = await _stage_post_prod(
            config, clip_paths, narration_path, scenes, run_id, run_ctx
        )
        final_video_path = Path(postprod_result["final_video"])
        await _progress("postprod_done", {"final_video": str(final_video_path)})

        # Stage 6 — Upload (optional)
        if upload or env.get("AUTO_UPLOAD", "false").lower() in {"true", "1"}:
            await _progress("upload_start", {})
            upload_response = await _stage_upload(
                config, env, final_video_path, script_payload, publish_at, run_ctx
            )
            run_ctx["upload_response"] = upload_response
            await _progress("upload_done", {"youtube_url": run_ctx.get("youtube_url", "")})

            # Stage 7 — Thumbnail (non-fatal, only when upload succeeded)
            video_id = run_ctx.get("youtube_video_id", "")
            if video_id:
                await _stage_thumbnail(
                    config, env, clip_paths, script_payload, video_id, run_ctx
                )
        else:
            LOGGER.info("[upload] Skipped (upload=False, AUTO_UPLOAD not set).")

        run_ctx["status"] = "success"
        run_ctx["finished_at"] = datetime.now(timezone.utc).isoformat()

    except PipelineError as exc:
        run_ctx["status"] = "error"
        run_ctx["error"] = str(exc)
        run_ctx["finished_at"] = datetime.now(timezone.utc).isoformat()
        LOGGER.error("Pipeline failed: %s", exc)
        await _progress("error", {"message": str(exc)})
        raise

    finally:
        _write_run_manifest(config, run_id, run_ctx)
        keep = config.get("pipeline", {}).get("keep_intermediate_files", False)
        if not keep:
            _cleanup_intermediate_files(run_id, config)

    LOGGER.info("=== Pipeline run %s completed (%s) ===", run_id, run_ctx["status"])
    return run_ctx


def _cleanup_intermediate_files(run_id: str, config: dict[str, Any]) -> None:
    """Delete partial clip and audio files generated during a run.

    Called from the finally block when keep_intermediate_files is False.
    """
    outputs_dir = Path(
        str(config.get("paths", {}).get("outputs_dir", "./outputs"))
    ).resolve()
    patterns = [
        f"{run_id}_scene*.mp4",
        f"{run_id}_scene*.wav",
        f"{run_id}_narration_full.wav",
    ]
    for pattern in patterns:
        for f in outputs_dir.glob(pattern):
            try:
                f.unlink(missing_ok=True)
            except Exception as exc:
                LOGGER.debug("Could not remove intermediate file %s: %s", f, exc)


def _write_run_manifest(
    config: dict[str, Any], run_id: str, run_ctx: dict[str, Any]
) -> None:
    """Persist the run context as a JSON manifest in the logs directory.

    Also appends a compact entry to publish_history.jsonl for deduplication
    and quota tracking by downstream modules.

    Args:
        config: Global app config (for logs dir path).
        run_id: Unique run identifier.
        run_ctx: Accumulated run metadata dictionary.
    """
    try:
        logs_dir = Path(
            str(config.get("paths", {}).get("logs_dir", "./logs"))
        ).resolve()
        logs_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = logs_dir / f"run_{run_id}_manifest.json"
        manifest_path.write_text(
            json.dumps(run_ctx, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except Exception as exc:
        LOGGER.warning("Could not write run manifest: %s", exc)

    # Append to publish_history.jsonl only for successful uploads.
    if run_ctx.get("status") == "success":
        try:
            logs_dir = Path(
                str(config.get("paths", {}).get("logs_dir", "./logs"))
            ).resolve()
            history_path = logs_dir / "publish_history.jsonl"
            entry = {
                "run_id": run_id,
                "topic": run_ctx.get("topic", ""),
                "video_id": run_ctx.get("youtube_video_id", ""),
                "published_at": run_ctx.get("finished_at", ""),
                "status": "success",
                "status_checked": False,
            }
            with history_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:
            LOGGER.warning("Could not append to publish_history.jsonl: %s", exc)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="shorts-engine pipeline CLI",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--topic",
        type=str,
        default=None,
        help="Topic to generate. If omitted, top trending topic is used.",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        default=False,
        help="Upload the final video to YouTube after generation.",
    )
    parser.add_argument(
        "--publish-at",
        type=str,
        default=None,
        help="ISO-8601 UTC datetime for scheduled publication (e.g. 2026-04-05T18:30:00Z).",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config.yaml",
        help="Path to config.yaml.",
    )
    parser.add_argument(
        "--env",
        type=str,
        default=".env",
        help="Path to .env file.",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="Optional custom run ID.",
    )
    return parser.parse_args()


async def _main() -> None:
    args = _parse_args()
    config = load_config(args.config)
    env = load_env(args.env)

    publish_at: datetime | None = None
    if args.publish_at:
        try:
            publish_at = datetime.fromisoformat(
                args.publish_at.replace("Z", "+00:00")
            )
        except ValueError as exc:
            print(f"ERROR: Invalid --publish-at format: {exc}", file=sys.stderr)
            sys.exit(1)

    result = await run_pipeline(
        config=config,
        env=env,
        topic=args.topic,
        upload=args.upload,
        publish_at=publish_at,
        run_id=args.run_id,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    asyncio.run(_main())
