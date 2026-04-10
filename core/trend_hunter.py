"""Trend discovery module for shorts-engine.

This module fetches signals from Google Trends and Reddit, merges candidate topics,
computes a weighted score, and returns ranked topics for downstream script generation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pytrends.request import TrendReq
import praw
from prawcore.exceptions import PrawcoreException

LOGGER = logging.getLogger(__name__)


class TrendHunterError(RuntimeError):
    """Raised when trend discovery cannot proceed safely."""


@dataclass(slots=True)
class TopicSignal:
    """Represents merged trend signals for a single topic candidate."""

    topic: str
    google_volume: float = 0.0
    google_momentum: float = 0.0
    reddit_mentions: float = 0.0
    reddit_engagement: float = 0.0
    source_notes: list[str] | None = None

    def __post_init__(self) -> None:
        if self.source_notes is None:
            self.source_notes = []


@dataclass(slots=True)
class TrendHunterConfig:
    """Configuration used by :class:`TrendHunter`."""

    google_geo: str
    google_hl: str
    google_tz: int
    google_cat: int
    google_timeframe: str
    google_max_topics: int

    reddit_subreddits: list[str]
    reddit_lookback_hours: int
    reddit_max_posts_per_subreddit: int
    reddit_min_score: int

    scoring_weights: dict[str, float]

    @classmethod
    def from_mapping(cls, config: dict[str, Any]) -> "TrendHunterConfig":
        """Build a validated config object from global config mapping.

        Args:
            config: The global app config dictionary loaded from config.yaml.

        Returns:
            A validated :class:`TrendHunterConfig` instance.

        Raises:
            TrendHunterError: If required config fields are missing.
        """
        trends_cfg = config.get("trends")
        if not isinstance(trends_cfg, dict):
            raise TrendHunterError("Missing 'trends' section in configuration.")

        google_cfg = trends_cfg.get("google", {})
        reddit_cfg = trends_cfg.get("reddit", {})
        weights = trends_cfg.get("scoring_weights", {})

        required_weight_keys = {
            "google_volume",
            "google_momentum",
            "reddit_mentions",
            "reddit_engagement",
        }
        if not required_weight_keys.issubset(weights.keys()):
            missing = sorted(required_weight_keys - set(weights.keys()))
            raise TrendHunterError(
                f"Missing scoring weights in config: {', '.join(missing)}"
            )

        subreddits = reddit_cfg.get("subreddits", [])
        if not isinstance(subreddits, list) or not subreddits:
            raise TrendHunterError(
                "Config 'trends.reddit.subreddits' must be a non-empty list."
            )

        return cls(
            google_geo=str(google_cfg.get("geo", "FR")),
            google_hl=str(google_cfg.get("hl", "fr-FR")),
            google_tz=int(google_cfg.get("tz", 60)),
            google_cat=int(google_cfg.get("cat", 0)),
            google_timeframe=str(google_cfg.get("timeframe", "now 7-d")),
            google_max_topics=int(google_cfg.get("max_topics", 20)),
            reddit_subreddits=[str(item) for item in subreddits],
            reddit_lookback_hours=int(reddit_cfg.get("lookback_hours", 72)),
            reddit_max_posts_per_subreddit=int(
                reddit_cfg.get("max_posts_per_subreddit", 25)
            ),
            reddit_min_score=int(reddit_cfg.get("min_score", 5)),
            scoring_weights={
                "google_volume": float(weights["google_volume"]),
                "google_momentum": float(weights["google_momentum"]),
                "reddit_mentions": float(weights["reddit_mentions"]),
                "reddit_engagement": float(weights["reddit_engagement"]),
            },
        )


class TrendHunter:
    """Fetches and ranks candidate topics from Google Trends and Reddit."""

    _MIN_TOPIC_LEN = 4
    _MAX_TOPIC_LEN = 80

    def __init__(self, cfg: TrendHunterConfig, env: dict[str, str] | None = None, logs_dir: Path | None = None) -> None:
        """Initialize the trend hunter.

        Args:
            cfg: Parsed trend configuration.
            env: Environment mapping, typically `os.environ` or a test dictionary.
            logs_dir: Optional override for the directory containing
                      ``publish_history.jsonl``.  When provided, takes precedence
                      over the ``LOGS_DIR`` environment variable.  Useful for
                      injecting a temporary directory in unit tests.
        """
        self.cfg = cfg
        self.env = env or {}
        self._logs_dir = logs_dir

    async def discover_topics(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return ranked topic candidates.

        Args:
            limit: Maximum number of ranked topics to return.

        Returns:
            A sorted list of topic dictionaries with scores and source metrics.

        Raises:
            TrendHunterError: If all upstream sources fail.
        """
        google_task = asyncio.create_task(self.fetch_google_trends())
        reddit_task = asyncio.create_task(self.fetch_reddit_topics())

        google_result, reddit_result = await asyncio.gather(
            google_task, reddit_task, return_exceptions=True
        )

        google_signals: dict[str, TopicSignal] = {}
        reddit_signals: dict[str, TopicSignal] = {}

        google_failed = isinstance(google_result, Exception)
        reddit_failed = isinstance(reddit_result, Exception)

        if google_failed:
            LOGGER.error("Google Trends fetch failed: %s", google_result)
        else:
            google_signals = google_result

        if reddit_failed:
            LOGGER.error("Reddit fetch failed: %s", reddit_result)
        else:
            reddit_signals = reddit_result

        if google_failed and reddit_failed:
            raise TrendHunterError(
                "Unable to discover topics: Google Trends and Reddit both failed."
            )

        merged = self._merge_signals(google_signals, reddit_signals)
        ranked = self._rank_signals(merged)
        ranked = self._exclude_recent_topics(ranked, self.env)
        return ranked[: max(1, limit)]

    def _exclude_recent_topics(
        self,
        ranked: list[dict[str, Any]],
        env: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Remove topics already produced in the last 14 days from the ranking.

        Reads logs/publish_history.jsonl (path derived from env LOGS_DIR or default).
        If the file is absent or unreadable, returns ranked unchanged.

        Args:
            ranked: Sorted topic list from _rank_signals.
            env: Environment mapping (may contain LOGS_DIR override).

        Returns:
            Filtered ranked list with recently produced topics removed.
        """
        from core.history import read_publish_history

        logs_dir = (
            self._logs_dir
            if self._logs_dir is not None
            else Path(env.get("LOGS_DIR", "./logs")).resolve()
        )
        cutoff = datetime.now(timezone.utc) - timedelta(days=14)
        recent_topics: set[str] = set()

        for entry in read_publish_history(logs_dir):
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
                topic = entry.get("topic", "")
                if topic:
                    recent_topics.add(self._canonical_topic(topic))

        if not recent_topics:
            return ranked

        filtered = [
            t for t in ranked
            if self._canonical_topic(t.get("topic", "")) not in recent_topics
        ]
        excluded = len(ranked) - len(filtered)
        if excluded:
            LOGGER.info(
                "Deduplication: excluded %d topic(s) produced in the last 14 days.",
                excluded,
            )
        return filtered

    async def fetch_google_trends(self) -> dict[str, TopicSignal]:
        """Fetch candidate topics and metrics from Google Trends.

        Returns:
            A mapping from normalized topic key to topic signal.
        """

        return await asyncio.to_thread(self._fetch_google_trends_sync)

    async def fetch_reddit_topics(self) -> dict[str, TopicSignal]:
        """Fetch topic candidates from configured Reddit communities.

        Returns:
            A mapping from normalized topic key to topic signal.

        Raises:
            TrendHunterError: If Reddit credentials are missing or invalid.
        """

        return await asyncio.to_thread(self._fetch_reddit_topics_sync)

    def _fetch_google_trends_sync(self) -> dict[str, TopicSignal]:
        pytrends = TrendReq(hl=self.cfg.google_hl, tz=self.cfg.google_tz)

        signals: dict[str, TopicSignal] = {}
        try:
            trending_df = pytrends.trending_searches(pn="france")
        except Exception as exc:  # explicit catch from upstream package internals
            raise TrendHunterError(f"Google Trends request failed: {exc}") from exc

        if trending_df.empty:
            LOGGER.warning("Google Trends returned no trending searches for France.")
            return signals

        raw_topics = [str(value).strip() for value in trending_df[0].tolist()]
        deduped_topics: list[str] = []
        seen: set[str] = set()

        for topic in raw_topics:
            key = self._canonical_topic(topic)
            if not key or key in seen:
                continue
            seen.add(key)
            deduped_topics.append(topic)
            if len(deduped_topics) >= self.cfg.google_max_topics:
                break

        for topic in deduped_topics:
            key = self._canonical_topic(topic)
            if not key:
                continue

            volume, momentum = self._safe_interest_metrics(pytrends, topic)
            if key not in signals:
                signals[key] = TopicSignal(topic=topic)
            signals[key].google_volume = max(signals[key].google_volume, volume)
            signals[key].google_momentum = max(signals[key].google_momentum, momentum)
            signals[key].source_notes.append("google_trends")

        return signals

    def _safe_interest_metrics(self, pytrends: TrendReq, topic: str) -> tuple[float, float]:
        try:
            pytrends.build_payload(
                kw_list=[topic],
                cat=self.cfg.google_cat,
                timeframe=self.cfg.google_timeframe,
                geo=self.cfg.google_geo,
            )
            data = pytrends.interest_over_time()
        except Exception as exc:
            LOGGER.warning("Interest metrics unavailable for topic '%s': %s", topic, exc)
            return 0.0, 0.0

        if data.empty or topic not in data.columns:
            return 0.0, 0.0

        series = data[topic].astype(float)
        if series.empty:
            return 0.0, 0.0

        latest = float(series.iloc[-1])
        baseline = float(series.iloc[:-1].mean()) if len(series) > 1 else latest
        momentum = max(0.0, latest - baseline)
        return latest, momentum

    def _fetch_reddit_topics_sync(self) -> dict[str, TopicSignal]:
        client_id = self.env.get("REDDIT_CLIENT_ID", "")
        client_secret = self.env.get("REDDIT_CLIENT_SECRET", "")
        user_agent = self.env.get("REDDIT_USER_AGENT", "")

        if not (client_id and client_secret and user_agent):
            raise TrendHunterError(
                "Missing Reddit credentials: REDDIT_CLIENT_ID, "
                "REDDIT_CLIENT_SECRET, REDDIT_USER_AGENT."
            )

        try:
            reddit = praw.Reddit(
                client_id=client_id,
                client_secret=client_secret,
                user_agent=user_agent,
            )
        except Exception as exc:
            raise TrendHunterError(f"Failed to initialize Reddit client: {exc}") from exc

        threshold = datetime.now(timezone.utc) - timedelta(
            hours=self.cfg.reddit_lookback_hours
        )
        signals: dict[str, TopicSignal] = {}

        for sub_name in self.cfg.reddit_subreddits:
            try:
                subreddit = reddit.subreddit(sub_name)
                posts = subreddit.hot(limit=self.cfg.reddit_max_posts_per_subreddit)
                for post in posts:
                    created = datetime.fromtimestamp(float(post.created_utc), tz=timezone.utc)
                    if created < threshold:
                        continue
                    if int(post.score) < self.cfg.reddit_min_score:
                        continue
                    self._accumulate_post_signals(signals, post.title, int(post.score), int(post.num_comments))
            except PrawcoreException as exc:
                LOGGER.warning("Reddit fetch warning for r/%s: %s", sub_name, exc)
            except Exception as exc:
                LOGGER.warning("Unexpected Reddit error for r/%s: %s", sub_name, exc)

        return signals

    def _accumulate_post_signals(
        self,
        signals: dict[str, TopicSignal],
        title: str,
        score: int,
        comments: int,
    ) -> None:
        topic_candidates = self._extract_topic_candidates(title)
        if not topic_candidates:
            return

        engagement = float(score + comments)
        for candidate in topic_candidates:
            key = self._canonical_topic(candidate)
            if not key:
                continue
            signal = signals.setdefault(key, TopicSignal(topic=candidate))
            signal.reddit_mentions += 1.0
            signal.reddit_engagement += engagement
            signal.source_notes.append("reddit")

    def _extract_topic_candidates(self, title: str) -> list[str]:
        clean = re.sub(r"\s+", " ", title).strip()
        if not clean:
            return []

        terms = re.split(r"[^\w\-]+", clean.lower())
        stopwords = {
            "the",
            "and",
            "for",
            "with",
            "that",
            "this",
            "from",
            "about",
            "dans",
            "avec",
            "pour",
            "mais",
            "plus",
            "moins",
            "what",
            "when",
            "how",
            "why",
        }

        selected_terms = [
            term
            for term in terms
            if len(term) >= 4 and term not in stopwords and not term.isdigit()
        ]

        candidates = [clean]
        candidates.extend(selected_terms[:5])
        return candidates

    def _canonical_topic(self, topic: str) -> str:
        cleaned = re.sub(r"\s+", " ", topic.strip().lower())
        cleaned = re.sub(r"[^\w\s\-]", "", cleaned)
        if len(cleaned) < self._MIN_TOPIC_LEN or len(cleaned) > self._MAX_TOPIC_LEN:
            return ""
        return cleaned

    def _merge_signals(
        self,
        google_signals: dict[str, TopicSignal],
        reddit_signals: dict[str, TopicSignal],
    ) -> dict[str, TopicSignal]:
        merged: dict[str, TopicSignal] = {}

        for key, signal in google_signals.items():
            merged[key] = TopicSignal(
                topic=signal.topic,
                google_volume=signal.google_volume,
                google_momentum=signal.google_momentum,
                reddit_mentions=signal.reddit_mentions,
                reddit_engagement=signal.reddit_engagement,
                source_notes=list(signal.source_notes or []),
            )

        for key, signal in reddit_signals.items():
            if key not in merged:
                merged[key] = TopicSignal(topic=signal.topic)
            merged[key].reddit_mentions += signal.reddit_mentions
            merged[key].reddit_engagement += signal.reddit_engagement
            merged[key].source_notes.extend(signal.source_notes or [])

        return merged

    def _rank_signals(self, signals: dict[str, TopicSignal]) -> list[dict[str, Any]]:
        if not signals:
            return []

        topics = list(signals.values())
        normalized_google_volume = self._normalize([item.google_volume for item in topics])
        normalized_google_momentum = self._normalize([item.google_momentum for item in topics])
        normalized_reddit_mentions = self._normalize([item.reddit_mentions for item in topics])
        normalized_reddit_engagement = self._normalize([item.reddit_engagement for item in topics])

        weights = self.cfg.scoring_weights
        # Load historical performance bonus map (topic_keyword → bonus 0.0–0.15)
        perf_bonus = self._load_performance_bonus()

        ranked: list[dict[str, Any]] = []

        for index, item in enumerate(topics):
            score = (
                normalized_google_volume[index] * weights["google_volume"]
                + normalized_google_momentum[index] * weights["google_momentum"]
                + normalized_reddit_mentions[index] * weights["reddit_mentions"]
                + normalized_reddit_engagement[index] * weights["reddit_engagement"]
            )

            # Apply performance bonus: reward topics whose keyword segments
            # appeared in historically high-retention videos.
            bonus = self._compute_topic_bonus(item.topic, perf_bonus)
            score = round(score + bonus, 4)

            ranked.append(
                {
                    "topic": item.topic,
                    "score": score,
                    "signals": {
                        "google_volume": round(item.google_volume, 3),
                        "google_momentum": round(item.google_momentum, 3),
                        "reddit_mentions": round(item.reddit_mentions, 3),
                        "reddit_engagement": round(item.reddit_engagement, 3),
                    },
                    "sources": sorted(set(item.source_notes or [])),
                }
            )

        ranked.sort(key=lambda x: x["score"], reverse=True)
        return ranked

    def _load_performance_bonus(self) -> dict[str, float]:
        """Read analytics_cache.jsonl and build a keyword → performance bonus map.

        The bonus is a value in [0.0, 0.15] derived from ``avg_view_percentage``
        (0 % → bonus 0.0, 100 % → bonus 0.15).  Topics with no analytics data
        receive no bonus.

        Returns:
            Mapping from lowercased keyword to float bonus value.
        """
        logs_dir = Path(self.env.get("LOGS_DIR", "./logs")).resolve()
        cache_path = logs_dir / "analytics_cache.jsonl"
        if not cache_path.exists():
            return {}

        keyword_stats: dict[str, list[float]] = {}
        try:
            with cache_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    avg_vp = entry.get("avg_view_percentage")
                    topic = entry.get("topic", "")
                    if avg_vp is None or not topic:
                        continue
                    try:
                        score_val = float(avg_vp)
                    except (TypeError, ValueError):
                        continue
                    # Tokenise topic into keywords for fuzzy matching
                    for kw in re.split(r"[\s\-_]+", topic.lower()):
                        kw = kw.strip()
                        if len(kw) >= 4:
                            keyword_stats.setdefault(kw, []).append(score_val)
        except Exception as exc:
            LOGGER.warning("Could not read analytics_cache.jsonl for perf bonus: %s", exc)
            return {}

        # Aggregate: average avg_view_percentage per keyword, map to [0, 0.15] bonus
        bonus_map: dict[str, float] = {}
        for kw, values in keyword_stats.items():
            mean_vp = sum(values) / len(values)
            bonus_map[kw] = round(min(0.15, mean_vp / 100.0 * 0.15), 5)

        return bonus_map

    def _compute_topic_bonus(self, topic: str, perf_bonus: dict[str, float]) -> float:
        """Return the maximum performance bonus applicable to a topic string.

        Args:
            topic: Raw topic string.
            perf_bonus: Keyword → bonus mapping from _load_performance_bonus.

        Returns:
            Maximum bonus value across all matching keywords (0.0 if none).
        """
        if not perf_bonus:
            return 0.0
        best = 0.0
        for kw in re.split(r"[\s\-_]+", topic.lower()):
            kw = kw.strip()
            if kw in perf_bonus:
                best = max(best, perf_bonus[kw])
        return best

    def _normalize(self, values: list[float]) -> list[float]:
        if not values:
            return []

        finite_values = [v if math.isfinite(v) else 0.0 for v in values]
        min_v = min(finite_values)
        max_v = max(finite_values)

        if math.isclose(min_v, max_v):
            return [0.5 if max_v > 0 else 0.0 for _ in finite_values]

        scale = max_v - min_v
        return [(v - min_v) / scale for v in finite_values]


async def get_ranked_topics(
    config: dict[str, Any],
    env: dict[str, str] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Public async helper to fetch and rank topics.

    Args:
        config: Global configuration mapping loaded from config.yaml.
        env: Environment mapping containing Reddit credentials.
        limit: Maximum number of topics in output.

    Returns:
        Ranked list of topics with per-source metrics.
    """
    cfg = TrendHunterConfig.from_mapping(config)
    hunter = TrendHunter(cfg=cfg, env=env)
    return await hunter.discover_topics(limit=limit)
