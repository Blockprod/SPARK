"""APScheduler-based scheduler for shorts-engine.

Triggers automated pipeline runs at peak FR YouTube Shorts hours.
Runs as a standalone process alongside the dashboard or independently.

Usage:
    python scheduler.py
    python scheduler.py --config config.yaml --env .env
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from pipeline import load_config, load_env, run_pipeline

LOGGER = logging.getLogger("scheduler")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)


class SchedulerError(RuntimeError):
    """Raised when scheduler configuration or startup fails."""


# ---------------------------------------------------------------------------
# Scheduled job
# ---------------------------------------------------------------------------


async def _scheduled_pipeline_job(
    config: dict[str, Any],
    env: dict[str, str],
    slot_label: str,
    publish_at_str: str | None = None,
    profile: str | None = None,
) -> None:
    """Execute a full pipeline run triggered by the scheduler.

    Args:
        config: Parsed config.yaml mapping.
        env: Environment mapping with credentials.
        slot_label: Human-readable label for this time slot (for logs).
        publish_at_str: Optional ISO-8601 publish time string, or None for no scheduling.
        profile: Optional niche profile name (e.g. ``"ia_histoire"``).
    """
    publish_at: datetime | None = None
    if publish_at_str:
        try:
            publish_at = datetime.fromisoformat(publish_at_str)
        except ValueError:
            LOGGER.warning(
                "Invalid publish_at string '%s' — uploading immediately.", publish_at_str
            )

    auto_upload = env.get("AUTO_UPLOAD", "false").lower() in {"true", "1"}

    # Weekly quota guard: enforce max_shorts_per_week from config.
    sched_cfg = config.get("scheduler", {})
    max_per_week = int(sched_cfg.get("max_shorts_per_week", 7))
    current_week_count = _count_shorts_this_week(config)
    if current_week_count >= max_per_week:
        LOGGER.warning(
            "Weekly quota reached (%d/%d Shorts) — scheduled job for slot '%s' skipped.",
            current_week_count,
            max_per_week,
            slot_label,
        )
        return

    LOGGER.info(
        "Scheduled run triggered for slot '%s' (auto_upload=%s).",
        slot_label,
        auto_upload,
    )

    try:
        result = await run_pipeline(
            config=config,
            env=env,
            topic=None,
            upload=auto_upload,
            publish_at=publish_at,
            profile=profile,
        )
        LOGGER.info(
            "Scheduled run completed (status=%s, run_id=%s).",
            result.get("status"),
            result.get("run_id"),
        )
    except Exception as exc:
        LOGGER.error("Scheduled pipeline run failed: %s", exc)


# ---------------------------------------------------------------------------
# Weekly quota helper
# ---------------------------------------------------------------------------


def _count_shorts_this_week(config: dict[str, Any]) -> int:
    """Count successful pipeline runs published in the last 7 days.

    Reads logs/publish_history.jsonl. Returns 0 if the file is absent or unreadable.

    Args:
        config: Global app config (for logs dir path).

    Returns:
        Number of successful Shorts published within the past 7 days.
    """
    from core.history import read_publish_history

    logs_dir = Path(
        str(config.get("paths", {}).get("logs_dir", "./logs"))
    ).resolve()
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    count = 0
    for entry in read_publish_history(logs_dir):
        if entry.get("status") != "success":
            continue
        published_at_str = entry.get("published_at", "")
        if not published_at_str:
            continue
        try:
            published_at = datetime.fromisoformat(
                published_at_str.replace("Z", "+00:00")
            )
        except ValueError:
            continue
        if published_at >= cutoff:
            count += 1
    return count


# ---------------------------------------------------------------------------
# Scheduler builder
# ---------------------------------------------------------------------------


def build_scheduler(
    config: dict[str, Any],
    env: dict[str, str],
) -> AsyncIOScheduler:
    """Build and configure an AsyncIOScheduler from config.yaml.

    Args:
        config: Parsed config.yaml mapping.
        env: Environment mapping with credentials.

    Returns:
        Configured (but not yet started) AsyncIOScheduler.

    Raises:
        SchedulerError: If scheduler config is invalid.
    """
    sched_cfg = config.get("scheduler", {})
    if not isinstance(sched_cfg, dict):
        raise SchedulerError("Missing 'scheduler' section in config.yaml.")

    if not sched_cfg.get("enabled", True):
        raise SchedulerError(
            "Scheduler is disabled in config.yaml (scheduler.enabled=false). "
            "Set scheduler.enabled=true to activate."
        )

    timezone_str = str(sched_cfg.get("timezone", "Europe/Paris"))
    misfire = int(sched_cfg.get("misfire_grace_time_sec", 300))
    coalesce = bool(sched_cfg.get("coalesce", True))

    raw_slots = sched_cfg.get("default_publish_slots", ["12:30", "18:30", "21:00"])
    if not isinstance(raw_slots, list) or not raw_slots:
        raise SchedulerError(
            "scheduler.default_publish_slots must be a non-empty list of 'HH:MM' strings."
        )

    scheduler = AsyncIOScheduler(
        timezone=timezone_str,
        job_defaults={
            "coalesce": coalesce,
            "misfire_grace_time": misfire,
            "max_instances": 1,
        },
        executors={"default": {"type": "asyncio"}},
    )

    for slot in raw_slots:
        try:
            hour_str, minute_str = str(slot).split(":")
            hour = int(hour_str)
            minute = int(minute_str)
        except (ValueError, AttributeError) as exc:
            raise SchedulerError(
                f"Invalid publish slot format '{slot}'. Expected 'HH:MM'."
            ) from exc

        slot_label = f"{hour:02d}:{minute:02d}"

        scheduler.add_job(
            _scheduled_pipeline_job,
            trigger=CronTrigger(
                hour=hour,
                minute=minute,
                timezone=timezone_str,
            ),
            kwargs={
                "config": config,
                "env": env,
                "slot_label": slot_label,
                "publish_at_str": None,
                "profile": None,
            },
            id=f"pipeline_slot_{hour:02d}{minute:02d}",
            name=f"Pipeline run @ {slot_label} ({timezone_str})",
            replace_existing=True,
        )
        LOGGER.info("Job registered: pipeline run @ %s %s", slot_label, timezone_str)

    # Per-profile scheduled jobs (overrides or supplements default slots).
    profiles_cfg = sched_cfg.get("profiles", {})
    if isinstance(profiles_cfg, dict):
        for profile_name, profile_sched in profiles_cfg.items():
            if not isinstance(profile_sched, dict):
                continue
            if not profile_sched.get("enabled", False):
                LOGGER.info("Profile '%s' scheduler is disabled — skipping.", profile_name)
                continue
            profile_slots = profile_sched.get("publish_slots", [])
            if not isinstance(profile_slots, list) or not profile_slots:
                LOGGER.warning("Profile '%s' publish_slots is empty or missing — skipped.", profile_name)
                continue
            for slot in profile_slots:
                try:
                    h_str, m_str = str(slot).split(":")
                    h, m = int(h_str), int(m_str)
                except (ValueError, AttributeError) as exc:
                    raise SchedulerError(
                        f"Invalid publish slot '{slot}' for profile '{profile_name}'."
                    ) from exc
                lbl = f"{h:02d}:{m:02d}"
                scheduler.add_job(
                    _scheduled_pipeline_job,
                    trigger=CronTrigger(hour=h, minute=m, timezone=timezone_str),
                    kwargs={
                        "config": config,
                        "env": env,
                        "slot_label": f"{profile_name}@{lbl}",
                        "publish_at_str": None,
                        "profile": profile_name,
                    },
                    id=f"pipeline_profile_{profile_name}_{h:02d}{m:02d}",
                    name=f"Pipeline run [{profile_name}] @ {lbl} ({timezone_str})",
                    replace_existing=True,
                )
                LOGGER.info("Job registered: [%s] @ %s %s", profile_name, lbl, timezone_str)

    return scheduler


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------


def _install_signal_handlers(
    scheduler: AsyncIOScheduler, loop: asyncio.AbstractEventLoop
) -> None:
    """Register SIGTERM / SIGINT handlers for graceful shutdown.

    Args:
        scheduler: The running APScheduler instance.
        loop: The running asyncio event loop.
    """

    def _handle_stop(signame: str) -> None:
        LOGGER.info("Received %s — shutting down scheduler…", signame)
        scheduler.shutdown(wait=True)
        loop.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig.name: _handle_stop(s))
        except (NotImplementedError, RuntimeError):
            # Windows does not support add_signal_handler for all signals
            signal.signal(sig, lambda *_: _handle_stop(sig.name))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def _run_scheduler(config_path: str, env_path: str) -> None:
    config = load_config(config_path)
    env = load_env(env_path)

    scheduler = build_scheduler(config=config, env=env)
    loop = asyncio.get_running_loop()
    _install_signal_handlers(scheduler, loop)

    scheduler.start()
    LOGGER.info(
        "Scheduler started. %d job(s) registered. Waiting for triggers…",
        len(scheduler.get_jobs()),
    )

    # Print next fire times for visibility
    for job in scheduler.get_jobs():
        next_run = job.next_run_time
        LOGGER.info(
            "  • %s — next run: %s",
            job.name,
            next_run.isoformat() if next_run else "N/A",
        )

    # Keep alive until signal
    try:
        while True:
            await asyncio.sleep(60)
    except asyncio.CancelledError:
        pass
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=True)
        LOGGER.info("Scheduler stopped cleanly.")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="shorts-engine scheduler — triggers pipeline at peak FR hours",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml.")
    parser.add_argument("--env", default=".env", help="Path to .env file.")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    try:
        asyncio.run(_run_scheduler(args.config, args.env))
    except KeyboardInterrupt:
        LOGGER.info("Scheduler interrupted by user.")
        sys.exit(0)
