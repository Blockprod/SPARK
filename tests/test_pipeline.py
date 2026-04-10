"""Tests unitaires pour pipeline.py.

Teste load_config, load_env et _cleanup_intermediate_files
sans exécuter le pipeline complet.
"""

import pytest
from pathlib import Path

from pipeline import load_config, load_env, PipelineError, _cleanup_intermediate_files


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_loads_valid_yaml(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("pipeline:\n  max_scenes: 6\n", encoding="utf-8")
        result = load_config(str(cfg_file))
        assert result["pipeline"]["max_scenes"] == 6

    def test_missing_file_raises_pipeline_error(self, tmp_path: Path) -> None:
        with pytest.raises(PipelineError, match="not found"):
            load_config(str(tmp_path / "absent.yaml"))

    def test_non_dict_yaml_raises_pipeline_error(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("- list_item\n", encoding="utf-8")
        with pytest.raises(PipelineError, match="mapping"):
            load_config(str(cfg_file))

    def test_invalid_yaml_raises_pipeline_error(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("key: [\nbroken", encoding="utf-8")
        with pytest.raises(PipelineError):
            load_config(str(cfg_file))


# ---------------------------------------------------------------------------
# load_env
# ---------------------------------------------------------------------------


class TestLoadEnv:
    def test_returns_dict(self) -> None:
        result = load_env()
        assert isinstance(result, dict)

    def test_missing_env_file_still_returns_dict(self, tmp_path: Path) -> None:
        result = load_env(str(tmp_path / ".nonexistent_env"))
        assert isinstance(result, dict)

    def test_env_file_values_loaded(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("TEST_VAR_SPARK=hello_test\n", encoding="utf-8")
        result = load_env(str(env_file))
        assert result.get("TEST_VAR_SPARK") == "hello_test"


# ---------------------------------------------------------------------------
# _cleanup_intermediate_files
# ---------------------------------------------------------------------------


class TestCleanupIntermediateFiles:
    def _make_run_files(self, outputs_dir: Path, run_id: str) -> list[Path]:
        outputs_dir.mkdir(parents=True, exist_ok=True)
        files = [
            outputs_dir / f"{run_id}_scene01.mp4",
            outputs_dir / f"{run_id}_scene02.mp4",
            outputs_dir / f"{run_id}_scene01.wav",
            outputs_dir / f"{run_id}_narration_full.wav",
        ]
        for f in files:
            f.write_text("dummy", encoding="utf-8")
        return files

    def _make_config(self, outputs_dir: Path) -> dict:
        return {"paths": {"outputs_dir": str(outputs_dir)}}

    def test_removes_clips_and_audio(self, tmp_path: Path) -> None:
        run_id = "abc123test01"
        outputs_dir = tmp_path / "outputs"
        files = self._make_run_files(outputs_dir, run_id)

        _cleanup_intermediate_files(run_id, self._make_config(outputs_dir))

        for f in files:
            assert not f.exists(), f"{f.name} should have been deleted"

    def test_does_not_remove_other_run_files(self, tmp_path: Path) -> None:
        run_id = "abc123test01"
        other_run_id = "xyz999test99"
        outputs_dir = tmp_path / "outputs"
        self._make_run_files(outputs_dir, run_id)
        other_files = self._make_run_files(outputs_dir, other_run_id)

        _cleanup_intermediate_files(run_id, self._make_config(outputs_dir))

        for f in other_files:
            assert f.exists(), f"{f.name} from other run must be preserved"

    def test_does_not_raise_if_outputs_dir_absent(self, tmp_path: Path) -> None:
        absent_dir = tmp_path / "nonexistent_outputs"
        config = {"paths": {"outputs_dir": str(absent_dir)}}
        # Should not raise even if the directory doesn't exist
        _cleanup_intermediate_files("abc123test01", config)

    def test_does_not_raise_if_files_already_gone(self, tmp_path: Path) -> None:
        outputs_dir = tmp_path / "outputs"
        outputs_dir.mkdir()
        config = self._make_config(outputs_dir)
        # No files created — cleanup on empty dir should be a no-op
        _cleanup_intermediate_files("abc123test01", config)
