"""Tests unitaires pour core/trend_hunter.py.

Teste la configuration, le scoring, le backend SerpAPI et la logique de bascule
Google Trends — aucun appel réseau réel.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from core.trend_hunter import TrendHunter, TrendHunterConfig, TrendHunterError, TopicSignal


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


# ---------------------------------------------------------------------------
# Helpers for TrendHunter network tests
# ---------------------------------------------------------------------------


def _make_hunter(serpapi_key: str = "") -> TrendHunter:
    cfg = TrendHunterConfig.from_mapping(
        {
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
                    "max_topics": 5,
                },
                "reddit": {
                    "subreddits": ["france"],
                    "lookback_hours": 24,
                    "max_posts_per_subreddit": 5,
                    "min_score": 1,
                },
            }
        }
    )
    env: dict[str, str] = {}
    if serpapi_key:
        env["SERPAPI_KEY"] = serpapi_key
    return TrendHunter(cfg=cfg, env=env)


def _serpapi_response(topics: list[str]) -> bytes:
    payload = {
        "trending_searches": [
            {"query": t, "search_volume": 80.0 - i * 5, "increase_percentage": 10.0}
            for i, t in enumerate(topics)
        ]
    }
    return json.dumps(payload).encode("utf-8")


# ---------------------------------------------------------------------------
# _fetch_google_trends_serpapi
# ---------------------------------------------------------------------------


class TestFetchGoogleTrendsSerpapi:
    def test_returns_topic_signals_from_serpapi(self) -> None:
        hunter = _make_hunter(serpapi_key="test-key")
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = _serpapi_response(["Intelligence Artificielle", "Machine Learning"])

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = hunter._fetch_google_trends_serpapi()

        assert len(result) == 2
        keys = list(result.keys())
        first_signal = result[keys[0]]
        assert first_signal.google_volume > 0.0
        assert "serpapi_trends" in first_signal.source_notes

    def test_empty_trending_searches_returns_empty_dict(self) -> None:
        hunter = _make_hunter(serpapi_key="test-key")
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = json.dumps({"trending_searches": []}).encode()

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = hunter._fetch_google_trends_serpapi()

        assert result == {}

    def test_missing_api_key_raises_trend_hunter_error(self) -> None:
        hunter = _make_hunter(serpapi_key="")
        with pytest.raises(TrendHunterError, match="SERPAPI_KEY"):
            hunter._fetch_google_trends_serpapi()

    def test_http_error_non_retryable_raises_trend_hunter_error(self) -> None:
        import urllib.error

        hunter = _make_hunter(serpapi_key="test-key")
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(url="", code=401, msg="Unauthorized", hdrs=None, fp=None),
        ):
            with pytest.raises(TrendHunterError, match="401"):
                hunter._fetch_google_trends_serpapi()

    def test_respect_max_topics_limit(self) -> None:
        hunter = _make_hunter(serpapi_key="test-key")  # max_topics=5
        topics = [f"Topic {i}" for i in range(20)]
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = _serpapi_response(topics)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = hunter._fetch_google_trends_serpapi()

        assert len(result) <= 5


# ---------------------------------------------------------------------------
# fetch_google_trends — bascule SerpAPI / pytrends
# ---------------------------------------------------------------------------


class TestFetchGoogleTrendsBascule:
    @pytest.mark.anyio
    async def test_uses_serpapi_when_key_present(self) -> None:
        hunter = _make_hunter(serpapi_key="test-key")
        expected = {"intelligence artificielle": TopicSignal(topic="Intelligence Artificielle")}

        with patch.object(hunter, "_fetch_google_trends_serpapi", return_value=expected) as mock_serp:
            result = await hunter.fetch_google_trends()

        mock_serp.assert_called_once()
        assert result is expected

    @pytest.mark.anyio
    async def test_falls_back_to_pytrends_when_key_absent(self) -> None:
        hunter = _make_hunter(serpapi_key="")
        expected = {"ia": TopicSignal(topic="IA")}

        with patch.object(hunter, "_fetch_google_trends_sync", return_value=expected) as mock_pytrends:
            result = await hunter.fetch_google_trends()

        mock_pytrends.assert_called_once()
        assert result is expected

    @pytest.mark.anyio
    async def test_pytrends_fallback_emits_deprecation_warning(self) -> None:
        hunter = _make_hunter(serpapi_key="")

        with patch.object(hunter, "_fetch_google_trends_sync", return_value={}):
            with patch("core.trend_hunter.LOGGER") as mock_logger:
                await hunter.fetch_google_trends()
                warning_calls = [str(call) for call in mock_logger.warning.call_args_list]
                assert any("SERPAPI_KEY" in c for c in warning_calls)
