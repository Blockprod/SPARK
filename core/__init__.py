"""shorts-engine core modules package."""

from core.trend_hunter import get_ranked_topics
from core.script_gen import generate_script_package
from core.video_gen import generate_video_clips
from core.audio_gen import generate_audio
from core.post_prod import run_post_production
from core.uploader import upload_to_youtube, get_youtube_service
from core.thumbnail_gen import generate_and_upload_thumbnail
from core.analytics_reader import AnalyticsReader
from core.history import (
    read_publish_history,
    append_publish_history,
    load_config,
    load_env,
)

__all__ = [
    "get_ranked_topics",
    "generate_script_package",
    "generate_video_clips",
    "generate_audio",
    "run_post_production",
    "upload_to_youtube",
    "get_youtube_service",
    "generate_and_upload_thumbnail",
    "AnalyticsReader",
    "read_publish_history",
    "append_publish_history",
    "load_config",
    "load_env",
]
