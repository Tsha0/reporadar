"""Flask test-client coverage of the dashboard's mutation endpoints.

Patches the publishing + orchestrator helpers so we exercise the request /
response wiring without a real Postgres or LLM.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.operator_api.web.app import create_app


class _StubSettings:
    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        self.database_url = "postgresql://stub"


@pytest.fixture
def client(tmp_path: Path):
    settings = _StubSettings(str(tmp_path))
    app = create_app(settings)
    return app.test_client()


# ── /api/posts/<post_id>/approve ──────────────────────────────────────

def test_approve_requires_scheduled_for(client):
    resp = client.post("/api/posts/post_x/approve", json={})
    assert resp.status_code == 400
    assert "scheduled_for" in resp.get_json()["error"]


def test_approve_rejects_bad_datetime(client):
    resp = client.post("/api/posts/post_x/approve", json={"scheduled_for": "not-a-date"})
    assert resp.status_code == 400


def test_approve_returns_404_when_post_missing(client):
    with patch("src.operator_api.web.app.mark_post_approved", return_value=False) as mock_app:
        # mark_post_approved is also imported via `connect`; use a context manager
        # over the connect helper so the route runs.
        with patch("src.operator_api.web.app.connect") as mock_connect:
            mock_connect.return_value.__enter__.return_value = MagicMock()
            mock_connect.return_value.__exit__.return_value = False
            resp = client.post(
                "/api/posts/post_nope/approve",
                json={"scheduled_for": "2026-05-17T14:00:00"},
            )
    assert resp.status_code == 404
    mock_app.assert_called_once()


def test_approve_succeeds_and_passes_parsed_datetime(client):
    with patch("src.operator_api.web.app.mark_post_approved", return_value=True) as mock_app:
        with patch("src.operator_api.web.app.connect") as mock_connect:
            mock_connect.return_value.__enter__.return_value = MagicMock()
            mock_connect.return_value.__exit__.return_value = False
            resp = client.post(
                "/api/posts/post_linkedin_abc/approve",
                json={"scheduled_for": "2026-05-17T14:00:00"},
            )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "approved"
    assert body["post_id"] == "post_linkedin_abc"
    # Helper got the parsed datetime as kwarg
    call_kwargs = mock_app.call_args.kwargs
    assert call_kwargs["post_id"] == "post_linkedin_abc"
    assert isinstance(call_kwargs["scheduled_for"], datetime)


# ── /api/posts/<post_id>/reject ───────────────────────────────────────

def test_reject_succeeds(client):
    with patch("src.operator_api.web.app.mark_post_rejected", return_value=True) as mock_app:
        with patch("src.operator_api.web.app.connect") as mock_connect:
            mock_connect.return_value.__enter__.return_value = MagicMock()
            mock_connect.return_value.__exit__.return_value = False
            resp = client.post("/api/posts/post_ig_abc/reject", json={})
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "rejected", "post_id": "post_ig_abc"}
    mock_app.assert_called_once()


def test_reject_404_when_missing(client):
    with patch("src.operator_api.web.app.mark_post_rejected", return_value=False):
        with patch("src.operator_api.web.app.connect") as mock_connect:
            mock_connect.return_value.__enter__.return_value = MagicMock()
            mock_connect.return_value.__exit__.return_value = False
            resp = client.post("/api/posts/post_nope/reject", json={})
    assert resp.status_code == 404


# ── /api/evaluations/<candidate_id>/generate ──────────────────────────

def test_generate_returns_404_when_no_evaluation(client):
    with patch(
        "src.operator_api.web.app.get_candidate_with_evaluation",
        return_value=(None, None),
    ):
        with patch("src.operator_api.web.app.connect") as mock_connect:
            mock_connect.return_value.__enter__.return_value = MagicMock()
            mock_connect.return_value.__exit__.return_value = False
            resp = client.post("/api/evaluations/cand_x/generate", json={})
    assert resp.status_code == 404


def test_generate_runs_helper_and_returns_summary(client):
    fake_candidate = MagicMock(candidate_id="cand_1", project_id="proj_1")
    fake_evaluation = MagicMock()
    fake_package_a = MagicMock(channel="instagram")
    fake_package_b = MagicMock(channel="linkedin")
    fake_json_paths = [Path("/tmp/a.json"), Path("/tmp/b.json")]

    with patch(
        "src.operator_api.web.app.get_candidate_with_evaluation",
        return_value=(fake_candidate, fake_evaluation),
    ), patch(
        "src.operator_api.web.app.generate_post_for_existing_candidate",
        return_value=("posted_proj_xyz", [fake_package_a, fake_package_b], fake_json_paths),
    ) as mock_helper, patch(
        "src.operator_api.web.app.get_llm_provider", return_value=MagicMock()
    ), patch(
        "src.operator_api.web.app.start_run", return_value="run_abc"
    ), patch(
        "src.operator_api.web.app.finish_run"
    ), patch(
        "src.operator_api.web.app.connect"
    ) as mock_connect:
        mock_connect.return_value.__enter__.return_value = MagicMock()
        mock_connect.return_value.__exit__.return_value = False
        resp = client.post("/api/evaluations/cand_1/generate", json={})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["posted_id"] == "posted_proj_xyz"
    assert body["channels"] == ["instagram", "linkedin"]
    # Helper called with the expected channels and reason
    helper_kwargs = mock_helper.call_args.kwargs
    assert helper_kwargs["channels"] == ["instagram", "linkedin"]
    assert "Generate post" in helper_kwargs["ranking_reason"]


def test_generate_returns_500_on_helper_failure(client):
    fake_candidate = MagicMock()
    fake_evaluation = MagicMock()
    with patch(
        "src.operator_api.web.app.get_candidate_with_evaluation",
        return_value=(fake_candidate, fake_evaluation),
    ), patch(
        "src.operator_api.web.app.generate_post_for_existing_candidate",
        side_effect=RuntimeError("LLM unavailable"),
    ), patch(
        "src.operator_api.web.app.get_llm_provider", return_value=MagicMock()
    ), patch(
        "src.operator_api.web.app.start_run", return_value="run_abc"
    ), patch(
        "src.operator_api.web.app.finish_run"
    ) as mock_finish, patch(
        "src.operator_api.web.app.connect"
    ) as mock_connect:
        mock_connect.return_value.__enter__.return_value = MagicMock()
        mock_connect.return_value.__exit__.return_value = False
        resp = client.post("/api/evaluations/cand_1/generate", json={})
    assert resp.status_code == 500
    assert "LLM unavailable" in resp.get_json()["error"]
    # finish_run was called with the error so the pipeline_runs row reflects failure
    mock_finish.assert_called_once()
    assert mock_finish.call_args.kwargs.get("error") == "LLM unavailable" or \
           "LLM" in str(mock_finish.call_args)
