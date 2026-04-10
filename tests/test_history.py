"""Tests unitaires pour core/history.py."""

import json
import pytest
from pathlib import Path

from core.history import (
    read_publish_history,
    append_publish_history,
    load_config,
    HistoryError,
)


# ---------------------------------------------------------------------------
# read_publish_history
# ---------------------------------------------------------------------------


class TestReadPublishHistory:
    def test_missing_file_returns_empty_list(self, tmp_path: Path) -> None:
        result = read_publish_history(tmp_path)
        assert result == []

    def test_reads_valid_entries(self, tmp_path: Path) -> None:
        history = tmp_path / "publish_history.jsonl"
        history.write_text(
            '{"run_id": "abc", "video_id": "v1"}\n'
            '{"run_id": "def", "video_id": "v2"}\n',
            encoding="utf-8",
        )
        result = read_publish_history(tmp_path)
        assert len(result) == 2
        assert result[0]["run_id"] == "abc"
        assert result[1]["video_id"] == "v2"

    def test_skips_malformed_lines(self, tmp_path: Path) -> None:
        history = tmp_path / "publish_history.jsonl"
        history.write_text(
            '{"run_id": "ok1"}\nnot_valid_json\n{"run_id": "ok2"}\n',
            encoding="utf-8",
        )
        result = read_publish_history(tmp_path)
        assert len(result) == 2
        assert result[0]["run_id"] == "ok1"
        assert result[1]["run_id"] == "ok2"

    def test_skips_empty_lines(self, tmp_path: Path) -> None:
        history = tmp_path / "publish_history.jsonl"
        history.write_text(
            '{"run_id": "x"}\n\n\n{"run_id": "y"}\n',
            encoding="utf-8",
        )
        result = read_publish_history(tmp_path)
        assert len(result) == 2

    def test_preserves_entry_order(self, tmp_path: Path) -> None:
        history = tmp_path / "publish_history.jsonl"
        lines = [json.dumps({"i": i}) for i in range(5)]
        history.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = read_publish_history(tmp_path)
        assert [r["i"] for r in result] == list(range(5))


# ---------------------------------------------------------------------------
# append_publish_history
# ---------------------------------------------------------------------------


class TestAppendPublishHistory:
    def test_creates_file_on_first_append(self, tmp_path: Path) -> None:
        append_publish_history(tmp_path, {"run_id": "abc"})
        result = read_publish_history(tmp_path)
        assert len(result) == 1
        assert result[0]["run_id"] == "abc"

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        deep = tmp_path / "a" / "b" / "c"
        append_publish_history(deep, {"key": "value"})
        assert (deep / "publish_history.jsonl").exists()

    def test_appends_multiple_entries_in_order(self, tmp_path: Path) -> None:
        for i in range(4):
            append_publish_history(tmp_path, {"index": i})
        result = read_publish_history(tmp_path)
        assert len(result) == 4
        assert [r["index"] for r in result] == [0, 1, 2, 3]

    def test_preserves_unicode(self, tmp_path: Path) -> None:
        append_publish_history(tmp_path, {"topic": "L'IA expliquée par l'Histoire"})
        result = read_publish_history(tmp_path)
        assert result[0]["topic"] == "L'IA expliquée par l'Histoire"

    def test_nested_dict_serialised(self, tmp_path: Path) -> None:
        entry = {"meta": {"a": 1, "b": [2, 3]}}
        append_publish_history(tmp_path, entry)
        result = read_publish_history(tmp_path)
        assert result[0]["meta"]["b"] == [2, 3]


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_loads_valid_yaml(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("project:\n  name: spark\n", encoding="utf-8")
        result = load_config(cfg_file)
        assert result["project"]["name"] == "spark"

    def test_raises_history_error_on_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(HistoryError, match="not found"):
            load_config(tmp_path / "absent.yaml")

    def test_raises_on_non_dict_yaml(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("- item1\n- item2\n", encoding="utf-8")
        with pytest.raises(HistoryError, match="mapping"):
            load_config(cfg_file)

    def test_raises_on_invalid_yaml(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("key: [\nunclosed", encoding="utf-8")
        with pytest.raises(HistoryError):
            load_config(cfg_file)

    def test_nested_values_accessible(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            "pipeline:\n  max_scenes: 8\n  min_duration_sec: 50\n",
            encoding="utf-8",
        )
        result = load_config(cfg_file)
        assert result["pipeline"]["max_scenes"] == 8
