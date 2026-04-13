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

from rich.logging import RichHandler
from core.history import load_config as _history_load_config, load_env as _history_load_env, HistoryError

LOGGER = logging.getLogger("pipeline")


class PipelineError(RuntimeError):
    """Raised when a pipeline stage fails and the run must be aborted."""


# ---------------------------------------------------------------------------
# Config + env loading (delegates to core.history — single source of truth)
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
    try:
        return _history_load_config(config_path)
    except HistoryError as exc:
        raise PipelineError(str(exc)) from exc


def load_env(env_path: str = ".env") -> dict[str, str]:
    """Load .env file into the process environment and return a snapshot.

    Args:
        env_path: Path to the .env file (optional — skipped if absent).

    Returns:
        Dictionary snapshot of the current environment after loading.
    """
    return _history_load_env(env_path)


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
# Performance cache helpers (analytics feedback loop)
# ---------------------------------------------------------------------------


def _get_logs_dir(config: dict[str, Any]) -> Path:
    return Path(str(config.get("paths", {}).get("logs_dir", "./logs"))).resolve()


def _load_performance_cache(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Load all entries from logs/performance_cache.jsonl. Returns [] if absent."""
    cache_path = _get_logs_dir(config) / "performance_cache.jsonl"
    if not cache_path.exists():
        return []
    entries: list[dict[str, Any]] = []
    try:
        with cache_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as exc:
        LOGGER.warning("Could not read performance_cache.jsonl: %s", exc)
    return entries


def _append_performance_cache(config: dict[str, Any], entry: dict[str, Any]) -> None:
    """Append one entry to logs/performance_cache.jsonl."""
    logs_dir = _get_logs_dir(config)
    cache_path = logs_dir / "performance_cache.jsonl"
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        with cache_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        LOGGER.warning("Could not write performance_cache.jsonl: %s", exc)


def _write_pending_analytics(
    config: dict[str, Any],
    run_id: str,
    video_id: str,
    template_used: str,
    topic: str,
    profile: str = "default",
    backend_used: str = "kokoro",
) -> None:
    """Schedule a deferred analytics fetch (48h after upload)."""
    from datetime import timedelta

    logs_dir = _get_logs_dir(config)
    pending_path = logs_dir / "analytics_pending.jsonl"
    fetch_after = (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat()
    entry = {
        "run_id": run_id,
        "youtube_video_id": video_id,
        "template_used": template_used,
        "topic": topic,
        "profile": profile or "default",
        "backend_used": backend_used or "kokoro",
        "fetch_after": fetch_after,
    }
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        with pending_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        LOGGER.debug("[analytics] Pending analytics entry scheduled for %s (video %s).", fetch_after, video_id)
    except Exception as exc:
        LOGGER.warning("Could not write analytics_pending.jsonl: %s", exc)


async def _process_pending_analytics(config: dict[str, Any], env: dict[str, str]) -> None:
    """Fetch analytics for pending entries whose 48h window has elapsed.

    Runs as a fire-and-forget background task at pipeline startup.
    Non-fatal: any failure is logged as a warning.
    """
    from core.analytics_reader import get_video_metrics, AnalyticsError

    logs_dir = _get_logs_dir(config)
    pending_path = logs_dir / "analytics_pending.jsonl"
    if not pending_path.exists():
        return

    now = datetime.now(timezone.utc)
    due: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []

    try:
        with pending_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    fetch_after = datetime.fromisoformat(
                        entry["fetch_after"].replace("Z", "+00:00")
                    )
                except (KeyError, ValueError):
                    continue
                (due if fetch_after <= now else remaining).append(entry)
    except Exception as exc:
        LOGGER.warning("Could not read analytics_pending.jsonl: %s", exc)
        return

    if not due:
        return

    LOGGER.info("[analytics] Processing %d deferred analytics fetch(es).", len(due))

    for entry in due:
        video_id = entry.get("youtube_video_id", "")
        if not video_id:
            continue
        try:
            metrics = await get_video_metrics(config=config, env=env, video_id=video_id)
            perf_entry: dict[str, Any] = {
                "run_id": entry.get("run_id", ""),
                "youtube_video_id": video_id,
                "template_used": entry.get("template_used", ""),
                "topic": entry.get("topic", ""),
                "profile": entry.get("profile", "default"),
                "backend_used": entry.get("backend_used", "kokoro"),
                "avg_view_percentage": metrics.get("avg_view_percentage", 0.0),
                "views": metrics.get("views", 0),
                "watch_time_minutes": metrics.get("watch_time_minutes", 0.0),
                "likes": metrics.get("likes", 0),
                "fetched_at": metrics.get("fetched_at", ""),
            }
            _append_performance_cache(config, perf_entry)
            LOGGER.info(
                "[analytics] Cached metrics for video %s: avg_view_pct=%.1f%%",
                video_id,
                perf_entry["avg_view_percentage"],
            )
        except AnalyticsError as exc:
            LOGGER.warning("[analytics] Failed to fetch metrics for %s: %s", video_id, exc)
            remaining.append(entry)  # retry on next run

    try:
        with pending_path.open("w", encoding="utf-8") as fh:
            for entry in remaining:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        LOGGER.warning("Could not rewrite analytics_pending.jsonl: %s", exc)


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
    performance_data: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from core.script_gen import generate_script_package

    LOGGER.info("[script] Generating script for topic: %s", topic)
    try:
        payload = await generate_script_package(
            config=config,
            env=env,
            topic=topic,
            trend_context=trend_context,
            performance_data=performance_data,
        )
    except Exception as exc:
        raise PipelineError(f"Script generation failed: {exc}") from exc
    run_ctx["scenes"] = len(payload.get("scenes", []))
    run_ctx["duration_sec"] = payload.get("duration_sec", 0)
    run_ctx["template_used"] = payload.get("template_used", "unknown")
    run_ctx["script_payload"] = payload
    LOGGER.info(
        "[script] Script ready: %d scenes, %ds (template: %s).",
        run_ctx["scenes"],
        run_ctx["duration_sec"],
        run_ctx["template_used"],
    )
    return payload


async def _stage_video(
    config: dict[str, Any],
    scenes: list[dict[str, Any]],
    run_id: str,
    run_ctx: dict[str, Any],
    progress_callback: Any | None = None,
) -> list[Path]:
    from core.video_gen import generate_video_clips

    LOGGER.info("[video] Generating %d clips…", len(scenes))
    try:
        clip_paths, degraded_ids = await generate_video_clips(
            config=config, scenes=scenes, run_id=run_id, progress_callback=progress_callback
        )
    except Exception as exc:
        raise PipelineError(f"Video generation failed: {exc}") from exc
    run_ctx["clip_paths"] = [str(p) for p in clip_paths]
    run_ctx["degraded_scenes"] = len(degraded_ids)
    LOGGER.info("[video] %d clips generated (%d degraded).", len(clip_paths), len(degraded_ids))
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
    scene_audio_paths: list[Path] | None = None,
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
            scene_audio_paths=scene_audio_paths,
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
    """Upload to all enabled platforms. A failure on one platform does not abort others."""
    from core.uploader import upload_to_youtube

    platforms_cfg = config.get("platforms", {})
    results: dict[str, Any] = {}

    # --- YouTube (primary, always attempted unless explicitly disabled) ---
    yt_enabled = platforms_cfg.get("youtube", {}).get("enabled", True)
    if yt_enabled:
        LOGGER.info("[upload] Uploading to YouTube…")
        try:
            response = await upload_to_youtube(
                config=config,
                env=env,
                video_path=video_path,
                script_payload=script_payload,
                publish_at=publish_at,
            )
            video_id = response.get("id", "")
            run_ctx["youtube_video_id"] = video_id
            run_ctx["youtube_url"] = f"https://youtu.be/{video_id}"
            results["youtube"] = response
            LOGGER.info("[upload] YouTube: published https://youtu.be/%s", video_id)
        except Exception as exc:
            LOGGER.error("[upload] YouTube upload failed: %s", exc)
            run_ctx["youtube_error"] = str(exc)
            raise PipelineError(f"YouTube upload failed: {exc}") from exc

    # --- TikTok ---
    tiktok_enabled = platforms_cfg.get("tiktok", {}).get("enabled", False)
    if tiktok_enabled:
        LOGGER.info("[upload] Uploading to TikTok…")
        try:
            from core.tiktok_uploader import upload_to_tiktok, TikTokUploaderError
            tiktok_resp = await upload_to_tiktok(
                config=config, env=env, video_path=video_path, script_payload=script_payload,
            )
            run_ctx["tiktok_publish_id"] = tiktok_resp.get("publish_id", "")
            results["tiktok"] = tiktok_resp
            LOGGER.info("[upload] TikTok: publish_id=%s", run_ctx["tiktok_publish_id"])
        except Exception as exc:
            LOGGER.warning("[upload] TikTok upload failed (non-fatal): %s", exc)
            run_ctx["tiktok_error"] = str(exc)

    # --- Instagram ---
    instagram_enabled = platforms_cfg.get("instagram", {}).get("enabled", False)
    if instagram_enabled:
        LOGGER.info("[upload] Uploading to Instagram…")
        try:
            from core.instagram_uploader import upload_to_instagram, InstagramUploaderError
            ig_resp = await upload_to_instagram(
                config=config, env=env, video_path=video_path, script_payload=script_payload,
            )
            run_ctx["instagram_media_id"] = ig_resp.get("media_id", "")
            results["instagram"] = ig_resp
            LOGGER.info("[upload] Instagram: media_id=%s", run_ctx["instagram_media_id"])
        except Exception as exc:
            LOGGER.warning("[upload] Instagram upload failed (non-fatal): %s", exc)
            run_ctx["instagram_error"] = str(exc)

    return results


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
    profile: str | None = None,
) -> dict[str, Any]:
    """Execute the full shorts-engine pipeline end-to-end.

    Args:
        config: Parsed config.yaml mapping. When *profile* is provided, the
            profile overrides are merged in automatically.
        env: Environment mapping with all credentials.
        topic: Pre-selected topic string. When None, the top trending topic is used.
        upload: Whether to upload the final video to YouTube.
        publish_at: UTC datetime for scheduled YouTube publication.
        run_id: Optional run identifier (UUID generated if not provided).
        progress_callback: Optional async callable(stage: str, data: dict) for
                           real-time SSE progress reporting from the dashboard.
        profile: Optional niche profile name (e.g. ``"ia_histoire"``).  When set,
            ``profiles/{profile}.yaml`` is deep-merged over *config* and outputs/
            logs directories are automatically scoped to prevent cross-niche
            collisions.

    Returns:
        Dictionary with run metadata, paths, and optional YouTube response.

    Raises:
        PipelineError: On any unrecoverable stage failure.
    """
    if profile:
        from core.history import load_config as _load_config_with_profile, HistoryError as _HE
        try:
            config = _load_config_with_profile(profile=profile)
        except _HE as exc:
            raise PipelineError(f"Could not load profile '{profile}': {exc}") from exc

    run_id = run_id or uuid.uuid4().hex[:12]
    log_file = _setup_logging(config, run_id)

    LOGGER.info("=== Pipeline run %s started ===", run_id)

    # Load performance cache once — shared by trends scoring and template selection.
    performance_data = _load_performance_cache(config)

    run_ctx: dict[str, Any] = {
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "topic": topic,
        "profile": profile,
        "log_file": str(log_file),
        "status": "running",
        "publish_at": publish_at.isoformat() if publish_at else None,
    }

    async def _progress(stage: str, data: dict[str, Any] | None = None) -> None:
        if progress_callback is not None:
            try:
                await progress_callback(stage, data or {})
            except Exception as cb_exc:
                LOGGER.debug("Progress callback error (non-fatal): %s", cb_exc)

    global_timeout = int(
        config.get("pipeline", {}).get("global_timeout_sec", 7200)
    )

    try:
        async with asyncio.timeout(global_timeout):
            # Stage 1 — Trends (+ fire deferred analytics in background)
            await _progress("trends_start", {})
            asyncio.create_task(_process_pending_analytics(config, env))
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
            # Filter performance data by profile for per-niche template selection.
            # Fall back to global data if fewer than 7 profile-specific entries exist.
            _profile_key = profile or "default"
            _profile_perf = [e for e in performance_data if e.get("profile") == _profile_key]
            perf_to_pass = _profile_perf if len(_profile_perf) >= 7 else performance_data

            await _progress("script_start", {"topic": topic})
            script_payload = await _stage_script(
                config, env, topic, trend_context, run_ctx, performance_data=perf_to_pass
            )
            await _progress("script_done", {"scenes": run_ctx.get("scenes", 0)})

            scenes = script_payload["scenes"]

            # Stage 3 — Video
            await _progress("video_start", {"scenes": len(scenes)})
            clip_paths = await _stage_video(config, scenes, run_id, run_ctx, progress_callback=_progress)
            await _progress("video_done", {"clips": len(clip_paths)})

            # Stage 4 — Audio
            await _progress("audio_start", {})
            audio_result = await _stage_audio(config, env, scenes, run_id, run_ctx)
            narration_path = Path(audio_result["narration_path"])
            scene_audio_paths = [Path(p) for p in audio_result.get("scene_paths", [])]
            await _progress("audio_done", {"narration": str(narration_path)})

            # Stage 5 — Post-production
            await _progress("postprod_start", {})
            postprod_result = await _stage_post_prod(
                config, clip_paths, narration_path, scenes, run_id, run_ctx,
                scene_audio_paths=scene_audio_paths,
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

                # Schedule deferred analytics fetch (48h after upload).
                video_id = run_ctx.get("youtube_video_id", "")
                if video_id:
                    _write_pending_analytics(
                        config,
                        run_id=run_id,
                        video_id=video_id,
                        template_used=run_ctx.get("template_used", ""),
                        topic=topic or "",
                        profile=profile or "default",
                        backend_used=str(
                            config.get("audio_generation", {}).get("active_backend", "kokoro")
                        ),
                    )

                # Stage 7 — Thumbnail (non-fatal, only when upload succeeded)
                if video_id:
                    await _stage_thumbnail(
                        config, env, clip_paths, script_payload, video_id, run_ctx
                    )
            else:
                LOGGER.info("[upload] Skipped (upload=False, AUTO_UPLOAD not set).")

            run_ctx["status"] = "success"
            run_ctx["finished_at"] = datetime.now(timezone.utc).isoformat()

    except asyncio.TimeoutError:
        run_ctx["status"] = "timeout"
        run_ctx["error"] = f"Pipeline timed out after {global_timeout}s"
        run_ctx["finished_at"] = datetime.now(timezone.utc).isoformat()
        LOGGER.error("[pipeline] Run %s timed out after %ds", run_id, global_timeout)
        await _progress("error", {"message": run_ctx["error"]})
        raise PipelineError(run_ctx["error"])

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
                "published_at": run_ctx.get("publish_at") or run_ctx.get("finished_at", ""),
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
