"""Tests unitaires pour dashboard/app.py.

Teste la validation Pydantic des schémas de requête et la logique
d'authentification — aucun appel HTTP réel.
"""

import os
import pytest
from unittest.mock import patch
from pydantic import ValidationError


# ---------------------------------------------------------------------------
# GenerateRequest — validation Pydantic
# ---------------------------------------------------------------------------


class TestGenerateRequest:
    def _import(self):
        from dashboard.app import GenerateRequest
        return GenerateRequest

    def test_valid_minimal_request(self) -> None:
        GenerateRequest = self._import()
        req = GenerateRequest()
        assert req.topic is None
        assert req.upload is False
        assert req.publish_at is None

    def test_valid_topic_accepted(self) -> None:
        GenerateRequest = self._import()
        req = GenerateRequest(topic="IA et l'Histoire de France")
        assert req.topic == "IA et l'Histoire de France"

    def test_topic_exactly_500_chars_accepted(self) -> None:
        GenerateRequest = self._import()
        topic = "a" * 500
        req = GenerateRequest(topic=topic)
        assert len(req.topic) == 500

    def test_topic_over_500_chars_raises_validation_error(self) -> None:
        GenerateRequest = self._import()
        with pytest.raises(ValidationError):
            GenerateRequest(topic="a" * 501)

    def test_topic_with_forbidden_chars_raises_validation_error(self) -> None:
        GenerateRequest = self._import()
        with pytest.raises(ValidationError):
            GenerateRequest(topic="Topic avec <html> injection")

    def test_topic_with_curly_braces_raises_validation_error(self) -> None:
        GenerateRequest = self._import()
        with pytest.raises(ValidationError):
            GenerateRequest(topic="Topic {injection: true}")

    def test_topic_none_accepted(self) -> None:
        GenerateRequest = self._import()
        req = GenerateRequest(topic=None)
        assert req.topic is None

    def test_upload_defaults_to_false(self) -> None:
        GenerateRequest = self._import()
        req = GenerateRequest()
        assert req.upload is False

    def test_upload_true_accepted(self) -> None:
        GenerateRequest = self._import()
        req = GenerateRequest(upload=True)
        assert req.upload is True

    def test_publish_at_iso_string_accepted(self) -> None:
        GenerateRequest = self._import()
        req = GenerateRequest(publish_at="2026-05-01T12:00:00Z")
        assert req.publish_at == "2026-05-01T12:00:00Z"


# ---------------------------------------------------------------------------
# _get_dashboard_api_key — guard logique
# ---------------------------------------------------------------------------


class TestGetDashboardApiKey:
    def test_raises_runtime_error_when_key_not_set(self) -> None:
        from dashboard.app import _get_dashboard_api_key
        with patch.dict(os.environ, {}, clear=False):
            # Ensure DASHBOARD_API_KEY is absent
            env_backup = os.environ.pop("DASHBOARD_API_KEY", None)
            try:
                with pytest.raises(RuntimeError, match="DASHBOARD_API_KEY"):
                    _get_dashboard_api_key()
            finally:
                if env_backup is not None:
                    os.environ["DASHBOARD_API_KEY"] = env_backup

    def test_returns_key_when_set(self) -> None:
        from dashboard.app import _get_dashboard_api_key
        with patch.dict(os.environ, {"DASHBOARD_API_KEY": "super-secret"}):
            result = _get_dashboard_api_key()
        assert result == "super-secret"
