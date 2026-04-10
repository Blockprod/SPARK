"""Tests unitaires pour core/trend_hunter.py.

Teste uniquement la configuration et le scoring — aucun appel à Google Trends
ni à l'API Reddit.
"""

import pytest
from core.trend_hunter import TrendHunterConfig, TrendHunterError, TopicSignal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_config() -> dict:
    return {
        "trends": {
            "scoring_weights": {
                "google_volume": 0.35,
                "google_momentum": 0.30,
                "reddit_mentions": 0.20,
                "reddit_engagement": 0.15,
            },
            "google": {
                "geo": "FR",
                "hl": "fr-FR",
                "tz": 60,
                "cat": 0,
                "timeframe": "now 7-d",
                "max_topics": 20,
            },
            "reddit": {
                "subreddits": ["artificial", "MachineLearning", "france"],
                "lookback_hours": 72,
                "max_posts_per_subreddit": 25,
                "min_score": 5,
            },
        }
    }


# ---------------------------------------------------------------------------
# TrendHunterConfig.from_mapping
# ---------------------------------------------------------------------------


class TestTrendHunterConfigFromMapping:
    def test_valid_config_creates_instance(self) -> None:
        cfg = TrendHunterConfig.from_mapping(_base_config())
        assert cfg.google_geo == "FR"
        assert cfg.google_hl == "fr-FR"
        assert cfg.google_tz == 60
        assert cfg.google_max_topics == 20
        assert cfg.reddit_lookback_hours == 72
        assert cfg.reddit_min_score == 5
        assert "artificial" in cfg.reddit_subreddits

    def test_missing_trends_section_raises(self) -> None:
        with pytest.raises(TrendHunterError, match="trends"):
            TrendHunterConfig.from_mapping({})

    def test_missing_scoring_weight_raises(self) -> None:
        config = _base_config()
        del config["trends"]["scoring_weights"]["reddit_engagement"]
        with pytest.raises(TrendHunterError, match="scoring weights"):
            TrendHunterConfig.from_mapping(config)

    def test_empty_subreddits_raises(self) -> None:
        config = _base_config()
        config["trends"]["reddit"]["subreddits"] = []
        with pytest.raises(TrendHunterError, match="subreddits"):
            TrendHunterConfig.from_mapping(config)

    def test_subreddits_cast_to_strings(self) -> None:
        config = _base_config()
        config["trends"]["reddit"]["subreddits"] = [123, "france"]
        cfg = TrendHunterConfig.from_mapping(config)
        assert cfg.reddit_subreddits == ["123", "france"]

    def test_scoring_weights_sum_to_one(self) -> None:
        cfg = TrendHunterConfig.from_mapping(_base_config())
        total = sum(cfg.scoring_weights.values())
        assert abs(total - 1.0) < 1e-9

    def test_defaults_applied_when_google_section_absent(self) -> None:
        config = _base_config()
        del config["trends"]["google"]
        cfg = TrendHunterConfig.from_mapping(config)
        assert cfg.google_geo == "FR"
        assert cfg.google_max_topics == 20

    def test_all_four_weights_stored(self) -> None:
        cfg = TrendHunterConfig.from_mapping(_base_config())
        assert set(cfg.scoring_weights.keys()) == {
            "google_volume",
            "google_momentum",
            "reddit_mentions",
            "reddit_engagement",
        }


# ---------------------------------------------------------------------------
# TopicSignal dataclass
# ---------------------------------------------------------------------------


class TestTopicSignal:
    def test_default_values(self) -> None:
        signal = TopicSignal(topic="IA et l'Histoire")
        assert signal.google_volume == 0.0
        assert signal.google_momentum == 0.0
        assert signal.reddit_mentions == 0.0
        assert signal.reddit_engagement == 0.0
        assert signal.source_notes == []

    def test_custom_values(self) -> None:
        signal = TopicSignal(
            topic="Machine Learning",
            google_volume=0.8,
            reddit_mentions=0.5,
        )
        assert signal.topic == "Machine Learning"
        assert signal.google_volume == 0.8
        assert signal.reddit_mentions == 0.5

    def test_source_notes_initialised_empty(self) -> None:
        s1 = TopicSignal(topic="A")
        s2 = TopicSignal(topic="B")
        # Each instance must have its own list (no shared mutable default)
        s1.source_notes.append("note")
        assert s2.source_notes == []
