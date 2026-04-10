"""FastAPI dashboard backend for shorts-engine.

Endpoints:
  GET  /trends           — return latest scraped trend topics
  POST /generate         — start pipeline run (background task)
  GET  /status/{run_id}  — SSE stream of pipeline progress
  GET  /preview/{run_id} — video metadata + paths for player
  POST /upload/{run_id}  — trigger YouTube upload for a completed run

Static files:
  GET /  → dashboard/ui/index.html
  GET /player → dashboard/ui/player.html

Usage:
    uvicorn dashboard.app:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import yaml
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Path as APIPath, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

LOGGER = logging.getLogger("dashboard")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)

# ---------------------------------------------------------------------------
# App factory + shared state
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
UI_DIR = BASE_DIR / "ui"
ROOT_DIR = BASE_DIR.parent

@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:  # noqa: ARG001
    global _pipeline_semaphore
    try:
        max_concurrent = int(
            _load_config().get("pipeline", {}).get("max_concurrent_runs", 1)
        )
    except Exception:
        max_concurrent = 1
    _pipeline_semaphore = asyncio.Semaphore(max_concurrent)
    yield


app = FastAPI(
    title="shorts-engine dashboard",
    description="Pipeline control plane for automated YouTube Shorts generation.",
    version="1.0.0",
    lifespan=_lifespan,
)

_cors_origins = [
    o.strip()
    for o in os.environ.get("DASHBOARD_CORS_ORIGINS", "http://localhost:7860").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["X-API-Key", "Content-Type"],
    allow_credentials=False,
)

# In-process SSE event queues: run_id → asyncio.Queue of event dicts
_sse_queues: dict[str, asyncio.Queue] = {}

# Completed run manifests: run_id → run_ctx dict
_run_results: dict[str, dict[str, Any]] = {}

# Cached trend results
_cached_trends: list[dict[str, Any]] = []
_cached_trends_at: datetime | None = None

# Concurrency semaphore — initialised on startup from config pipeline.max_concurrent_runs
_pipeline_semaphore: asyncio.Semaphore | None = None



def _load_config() -> dict[str, Any]:
    from core.history import load_config
    return load_config(ROOT_DIR / "config.yaml")


def _load_env() -> dict[str, str]:
    from core.history import load_env
    return load_env(ROOT_DIR / ".env")


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def _get_dashboard_api_key() -> str:
    key = os.environ.get("DASHBOARD_API_KEY", "")
    if not key:
        raise RuntimeError(
            "DASHBOARD_API_KEY is not set. Set it in .env to protect the dashboard."
        )
    return key


async def _verify_api_key(
    x_api_key: str = Header(..., alias="X-API-Key"),
    expected: str = Depends(_get_dashboard_api_key),
) -> None:
    if x_api_key != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class GenerateRequest(BaseModel):
    topic: str | None = Field(
        default=None,
        max_length=500,
        pattern=r"^[\w\s\-'\",.!?()\u00e0\u00e2\u00e7\u00e9\u00e8\u00ea\u00eb\u00ee\u00ef\u00f4\u00f9\u00fb\u00fc\u00c0\u00c2\u00c7\u00c9\u00c8\u00ca\u00cb\u00ce\u00cf\u00d4\u00d9\u00db\u00dc]+$",
        description="Topic to generate (max 500 chars). Omit to use top trending topic.",
    )
    upload: bool = Field(default=False, description="Upload result to YouTube.")
    publish_at: str | None = Field(
        default=None,
        description="ISO-8601 UTC datetime for scheduled YouTube publication.",
    )


class TrendResponse(BaseModel):
    topics: list[dict[str, Any]]
    fetched_at: str


class GenerateResponse(BaseModel):
    run_id: str
    status: str
    message: str


class PreviewResponse(BaseModel):
    run_id: str
    status: str
    topic: str | None
    final_video: str | None
    srt_file: str | None
    youtube_url: str | None
    duration_sec: int | None
    scenes: int | None
    started_at: str | None
    finished_at: str | None


class UploadResponse(BaseModel):
    run_id: str
    youtube_video_id: str
    youtube_url: str


# ---------------------------------------------------------------------------
# SSE progress callback
# ---------------------------------------------------------------------------


async def _make_progress_callback(run_id: str):
    """Return an async callback that pushes SSE events into the run queue."""

    queue = _sse_queues.setdefault(run_id, asyncio.Queue())

    async def _callback(stage: str, data: dict[str, Any]) -> None:
        event = {
            "stage": stage,
            "data": data,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        await queue.put(event)

    return _callback


# ---------------------------------------------------------------------------
# Background pipeline task
# ---------------------------------------------------------------------------


async def _run_pipeline_task(
    run_id: str,
    config: dict[str, Any],
    env: dict[str, str],
    topic: str | None,
    upload: bool,
    publish_at: datetime | None,
) -> None:
    from pipeline import run_pipeline

    progress_cb = await _make_progress_callback(run_id)
    queue = _sse_queues.setdefault(run_id, asyncio.Queue())

    semaphore = _pipeline_semaphore if _pipeline_semaphore is not None else asyncio.Semaphore(1)
    async with semaphore:
        try:
            result = await run_pipeline(
                config=config,
                env=env,
                topic=topic,
                upload=upload,
                publish_at=publish_at,
                run_id=run_id,
                progress_callback=progress_cb,
            )
            _run_results[run_id] = result
            await queue.put({"stage": "done", "data": result, "ts": datetime.now(timezone.utc).isoformat()})
        except Exception as exc:
            error_event = {
                "stage": "error",
                "data": {"message": str(exc)},
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            _run_results[run_id] = {"run_id": run_id, "status": "error", "error": str(exc)}
            await queue.put(error_event)
            LOGGER.error("Pipeline run %s failed: %s", run_id, exc)
        finally:
            # Sentinel: tell SSE generator to close stream
            await queue.put(None)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root():
    """Serve the main dashboard UI."""
    index = UI_DIR / "index.html"
    if not index.exists():
        raise HTTPException(status_code=404, detail="index.html not found.")
    return HTMLResponse(content=index.read_text(encoding="utf-8"))


@app.get("/player", response_class=HTMLResponse, include_in_schema=False)
async def player():
    """Serve the video player UI."""
    player_html = UI_DIR / "player.html"
    if not player_html.exists():
        raise HTTPException(status_code=404, detail="player.html not found.")
    return HTMLResponse(content=player_html.read_text(encoding="utf-8"))


@app.get("/trends", response_model=TrendResponse)
async def get_trends(force_refresh: bool = False):
    """Return ranked trending topics.

    Uses cached results if available and less than 10 minutes old.
    Add ?force_refresh=true to bypass cache.
    """
    global _cached_trends, _cached_trends_at

    cache_age_minutes = (
        (datetime.now(timezone.utc) - _cached_trends_at).total_seconds() / 60
        if _cached_trends_at
        else 9999
    )

    if not force_refresh and _cached_trends and cache_age_minutes < _load_config().get("dashboard", {}).get("trends_cache_ttl_minutes", 10):
        return TrendResponse(
            topics=_cached_trends,
            fetched_at=_cached_trends_at.isoformat(),
        )

    try:
        from core.trend_hunter import get_ranked_topics

        config = _load_config()
        env = _load_env()
        topics = await get_ranked_topics(config=config, env=env, limit=20)
        _cached_trends = topics
        _cached_trends_at = datetime.now(timezone.utc)
        return TrendResponse(topics=topics, fetched_at=_cached_trends_at.isoformat())
    except Exception as exc:
        LOGGER.error("Trend fetch failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Trend fetch failed: {exc}")


@app.post("/generate", response_model=GenerateResponse, status_code=202, dependencies=[Depends(_verify_api_key)])
async def generate(req: GenerateRequest, background_tasks: BackgroundTasks):
    """Start a pipeline run in background. Returns run_id immediately."""
    run_id = uuid.uuid4().hex[:12]

    publish_at: datetime | None = None
    if req.publish_at:
        try:
            publish_at = datetime.fromisoformat(req.publish_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid publish_at format: {exc}",
            )

    config = _load_config()
    env = _load_env()

    _sse_queues[run_id] = asyncio.Queue()

    background_tasks.add_task(
        _run_pipeline_task,
        run_id=run_id,
        config=config,
        env=env,
        topic=req.topic,
        upload=req.upload,
        publish_at=publish_at,
    )

    LOGGER.info("Pipeline run %s started for topic '%s'.", run_id, req.topic or "auto")
    return GenerateResponse(
        run_id=run_id,
        status="started",
        message=f"Pipeline run {run_id} started. Stream progress at /status/{run_id}",
    )


@app.get("/status/{run_id}")
async def status_stream(run_id: str, request: Request):
    """SSE stream of pipeline progress events for a given run_id.

    Events are JSON objects with fields: stage, data, ts.
    Stream closes automatically when the pipeline finishes or errors.
    """
    if run_id not in _sse_queues and run_id not in _run_results:
        raise HTTPException(status_code=404, detail=f"Run ID '{run_id}' not found.")

    queue = _sse_queues.setdefault(run_id, asyncio.Queue())

    async def _event_generator() -> AsyncIterator[dict]:
        heartbeat_interval = 5
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=heartbeat_interval)
            except asyncio.TimeoutError:
                yield {"event": "heartbeat", "data": json.dumps({"ts": datetime.now(timezone.utc).isoformat()})}
                continue

            if event is None:
                # Sentinel — stream closed
                break

            yield {"event": event.get("stage", "update"), "data": json.dumps(event)}

    return EventSourceResponse(_event_generator())


@app.get("/preview/{run_id}", response_model=PreviewResponse)
async def preview(run_id: str = APIPath(..., pattern=r"^[0-9a-f]{12}$")):
    """Return structured metadata for a completed pipeline run."""
    result = _run_results.get(run_id)
    if result is None:
        # Try loading from manifest on disk
        try:
            config = _load_config()
            logs_dir = Path(
                str(config.get("paths", {}).get("logs_dir", "./logs"))
            ).resolve()
            manifest = logs_dir / f"run_{run_id}_manifest.json"
            if manifest.exists():
                result = json.loads(manifest.read_text(encoding="utf-8"))
                _run_results[run_id] = result
        except Exception:
            pass

    if result is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")

    return PreviewResponse(
        run_id=run_id,
        status=str(result.get("status", "unknown")),
        topic=result.get("topic"),
        final_video=result.get("final_video"),
        srt_file=result.get("srt_file"),
        youtube_url=result.get("youtube_url"),
        duration_sec=result.get("duration_sec"),
        scenes=result.get("scenes"),
        started_at=result.get("started_at"),
        finished_at=result.get("finished_at"),
    )


@app.post("/upload/{run_id}", response_model=UploadResponse, dependencies=[Depends(_verify_api_key)])
async def upload_run(run_id: str = APIPath(..., pattern=r"^[0-9a-f]{12}$")):
    """Trigger YouTube upload for a completed pipeline run."""
    result = _run_results.get(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")
    if result.get("status") != "success":
        raise HTTPException(
            status_code=409,
            detail=f"Run '{run_id}' is not in 'success' state (current: {result.get('status')}).",
        )
    final_video = result.get("final_video")
    if not final_video or not Path(final_video).exists():
        raise HTTPException(
            status_code=404,
            detail=f"Final video file not found for run '{run_id}'.",
        )

    # Load original script payload from manifest if available
    try:
        config = _load_config()
        logs_dir = Path(
            str(config.get("paths", {}).get("logs_dir", "./logs"))
        ).resolve()
        manifest = logs_dir / f"run_{run_id}_manifest.json"
        if manifest.exists():
            manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
        else:
            manifest_data = result
    except Exception:
        manifest_data = result

    try:
        from core.uploader import upload_to_youtube

        env = _load_env()
        script_payload = manifest_data.get("script_payload", manifest_data)
        response = await upload_to_youtube(
            config=config,
            env=env,
            video_path=Path(final_video),
            script_payload=script_payload,
        )
        video_id = response.get("id", "")
        _run_results[run_id]["youtube_video_id"] = video_id
        _run_results[run_id]["youtube_url"] = f"https://youtu.be/{video_id}"

        # Write to publish_history.jsonl so deduplication and quota guards apply
        try:
            from core.history import append_publish_history
            entry = {
                "run_id": run_id,
                "topic": str(manifest_data.get("topic", "")),
                "video_id": video_id,
                "published_at": datetime.now(timezone.utc).isoformat(),
                "status": "success",
                "status_checked": False,
            }
            append_publish_history(logs_dir, entry)
        except Exception as hist_exc:
            LOGGER.warning("Could not append to publish_history.jsonl: %s", hist_exc)

        return UploadResponse(
            run_id=run_id,
            youtube_video_id=video_id,
            youtube_url=f"https://youtu.be/{video_id}",
        )
    except Exception as exc:
        LOGGER.error("Upload for run %s failed: %s", run_id, exc)
        raise HTTPException(status_code=502, detail=f"Upload failed: {exc}")


@app.get("/analytics/{run_id}", dependencies=[Depends(_verify_api_key)])
async def get_analytics(run_id: str = APIPath(..., pattern=r"^[0-9a-f]{12}$")):
    """Return YouTube Analytics metrics for a completed and uploaded run.

    Reads the video_id from the run manifest or publish_history.jsonl,
    then calls the YouTube Analytics API via core/analytics_reader.py.

    Returns metrics: views, watch_time_minutes, avg_view_percentage, likes.
    """
    # Resolve video_id from in-memory results or disk manifest
    video_id: str = ""

    result = _run_results.get(run_id)
    if result:
        video_id = str(result.get("youtube_video_id", ""))

    if not video_id:
        try:
            config = _load_config()
            logs_dir = Path(
                str(config.get("paths", {}).get("logs_dir", "./logs"))
            ).resolve()
            manifest = logs_dir / f"run_{run_id}_manifest.json"
            if manifest.exists():
                data = json.loads(manifest.read_text(encoding="utf-8"))
                video_id = str(data.get("youtube_video_id", ""))
        except Exception:
            pass

    if not video_id:
        # Fallback: scan publish_history.jsonl
        try:
            config = _load_config()
            logs_dir = Path(
                str(config.get("paths", {}).get("logs_dir", "./logs"))
            ).resolve()
            from core.history import read_publish_history
            for entry in read_publish_history(logs_dir):
                if entry.get("run_id") == run_id:
                    video_id = str(entry.get("video_id", ""))
                    break
        except Exception:
            pass

    if not video_id:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No YouTube video_id found for run '{run_id}'. "
                "Ensure the run completed with an upload."
            ),
        )

    try:
        from core.analytics_reader import get_video_metrics

        config = _load_config()
        env = _load_env()
        metrics = await get_video_metrics(config=config, env=env, video_id=video_id)
        return metrics
    except Exception as exc:
        LOGGER.error("Analytics fetch for run %s failed: %s", run_id, exc)
        raise HTTPException(status_code=502, detail=f"Analytics fetch failed: {exc}")


@app.get("/video/{run_id}")
async def serve_video(run_id: str):
    """Stream the final MP4 video for preview in the browser."""
    result = _run_results.get(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")
    final_video = result.get("final_video")
    if not final_video or not Path(final_video).exists():
        raise HTTPException(status_code=404, detail="Video file not available.")
    return FileResponse(
        path=final_video,
        media_type="video/mp4",
        filename=Path(final_video).name,
    )


@app.get("/runs", response_model=list[dict])
async def list_runs():
    """Return summary of all known pipeline runs (in-memory + disk manifests)."""
    summaries: list[dict] = []

    # Load from disk manifests first
    try:
        config = _load_config()
        logs_dir = Path(
            str(config.get("paths", {}).get("logs_dir", "./logs"))
        ).resolve()
        if logs_dir.exists():
            for manifest_file in sorted(logs_dir.glob("run_*_manifest.json"), reverse=True):
                try:
                    data = json.loads(manifest_file.read_text(encoding="utf-8"))
                    summaries.append(
                        {
                            "run_id": data.get("run_id"),
                            "topic": data.get("topic"),
                            "status": data.get("status"),
                            "started_at": data.get("started_at"),
                            "finished_at": data.get("finished_at"),
                            "youtube_url": data.get("youtube_url"),
                        }
                    )
                except Exception:
                    continue
    except Exception:
        pass

    # Merge in-memory results (may be more up to date)
    known_ids = {s["run_id"] for s in summaries}
    for run_id, result in _run_results.items():
        if run_id not in known_ids:
            summaries.append(
                {
                    "run_id": run_id,
                    "topic": result.get("topic"),
                    "status": result.get("status"),
                    "started_at": result.get("started_at"),
                    "finished_at": result.get("finished_at"),
                    "youtube_url": result.get("youtube_url"),
                }
            )

    return summaries
