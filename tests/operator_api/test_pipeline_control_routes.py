"""Flask test-client coverage of the 5 pipeline-control routes:

  POST /api/scan-repos       — runs scan_github
  POST /api/scan-hackathons  — runs scan_devpost
  POST /api/evaluate         — runs evaluate_pending_candidates
  POST /api/run              — runs orchestrator.run_pipeline
  POST /api/submit           — runs orchestrator.submit_url_and_generate

Each route is patched at the import site inside `operator_api.web.app`,
so we exercise the request/response wiring without touching Postgres,
GitHub, Devpost, or the LLM.
"""
from __future__ import annotations

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
    return create_app(settings).test_client()


def _stub_conn():
    fake_conn = MagicMock()
    fake_conn.__enter__.return_value = fake_conn
    fake_conn.__exit__.return_value = False
    return fake_conn


def _patch_runs():
    """Patch start_run + finish_run; their return values don't matter here."""
    return patch.multiple(
        "src.operator_api.web.app",
        start_run=MagicMock(return_value="run_test_001"),
        finish_run=MagicMock(),
    )


def _stub_candidate(key: str):
    c = MagicMock()
    c.canonical_repo_key = key
    return c


# ── /api/scan-repos ───────────────────────────────────────────────────

def test_scan_repos_returns_eligible_count(client):
    candidates = [_stub_candidate("github:owner/a"), _stub_candidate("github:owner/b")]
    with patch("src.operator_api.web.app.scan_github", return_value=candidates) as mock_scan, \
         patch("src.operator_api.web.app.connect", return_value=_stub_conn()), _patch_runs():
        resp = client.post("/api/scan-repos", json={})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["eligible_count"] == 2
    assert body["sample"] == ["github:owner/a", "github:owner/b"]
    mock_scan.assert_called_once()


def test_scan_repos_returns_500_on_failure(client):
    with patch(
        "src.operator_api.web.app.scan_github",
        side_effect=RuntimeError("GitHub rate-limited"),
    ), patch("src.operator_api.web.app.connect", return_value=_stub_conn()), _patch_runs():
        resp = client.post("/api/scan-repos", json={})
    assert resp.status_code == 500
    assert "GitHub rate-limited" in resp.get_json()["error"]


# ── /api/scan-hackathons ──────────────────────────────────────────────

def test_scan_hackathons_returns_eligible_count(client):
    with patch(
        "src.operator_api.web.app.scan_devpost",
        return_value=[_stub_candidate("devpost:foo")],
    ), patch("src.operator_api.web.app.connect", return_value=_stub_conn()), _patch_runs():
        resp = client.post("/api/scan-hackathons", json={})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["eligible_count"] == 1


# ── /api/evaluate ────────────────────────────────────────────────────

def test_evaluate_returns_counts_and_sample(client):
    e1 = MagicMock(candidate_id="cand_1", skip=False)
    e1.scores.overall = 8.5
    e2 = MagicMock(candidate_id="cand_2", skip=True)
    e2.scores.overall = 3.0
    with patch(
        "src.operator_api.web.app.evaluate_pending_candidates",
        return_value=[e1, e2],
    ), patch("src.operator_api.web.app.get_llm_provider", return_value=MagicMock()), \
         patch("src.operator_api.web.app.connect", return_value=_stub_conn()), _patch_runs():
        resp = client.post("/api/evaluate", json={})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["evaluated_count"] == 2
    assert body["skipped_count"] == 1
    assert len(body["sample"]) == 2


# ── /api/run ────────────────────────────────────────────────────────

def test_run_returns_pipeline_result(client):
    with patch(
        "src.operator_api.web.app.run_pipeline",
        return_value={
            "run_id": "run_xyz",
            "posted_id": "posted_proj_abc",
            "candidate_id": "cand_xx",
            "project_id": "proj_abc",
            "selection_score": 8.72,
            "channels": ["instagram", "linkedin"],
            "image_paths": ["output/ig.jpg"],
            "export_paths": ["output/ig.json"],
        },
    ), patch("src.operator_api.web.app.connect", return_value=_stub_conn()):
        resp = client.post("/api/run", json={"channels": ["linkedin"]})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["posted_id"] == "posted_proj_abc"
    assert body["channels"] == ["instagram", "linkedin"]


def test_run_handles_no_eligible_candidate(client):
    with patch(
        "src.operator_api.web.app.run_pipeline", return_value=None
    ), patch("src.operator_api.web.app.connect", return_value=_stub_conn()):
        resp = client.post("/api/run", json={})
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "no_eligible_candidate"


def test_run_returns_500_on_pipeline_exception(client):
    with patch(
        "src.operator_api.web.app.run_pipeline", side_effect=RuntimeError("DB down")
    ), patch("src.operator_api.web.app.connect", return_value=_stub_conn()):
        resp = client.post("/api/run", json={})
    assert resp.status_code == 500
    assert "DB down" in resp.get_json()["error"]


# ── /api/submit ─────────────────────────────────────────────────────

def test_submit_requires_url(client):
    resp = client.post("/api/submit", json={})
    assert resp.status_code == 400
    assert "url is required" in resp.get_json()["error"]


def test_submit_returns_posted_summary(client):
    cand = MagicMock(candidate_id="cand_1", canonical_repo_key="github:owner/x")
    eval_ = MagicMock()
    pkg_ig = MagicMock(channel="instagram")
    pkg_li = MagicMock(channel="linkedin")
    with patch(
        "src.operator_api.web.app.submit_url_and_generate",
        return_value=(cand, eval_, "posted_proj_x", [pkg_ig, pkg_li], [Path("/tmp/a.json")]),
    ), patch("src.operator_api.web.app.connect", return_value=_stub_conn()), _patch_runs():
        resp = client.post(
            "/api/submit",
            json={"url": "https://github.com/owner/x"},
        )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["candidate_id"] == "cand_1"
    assert body["canonical_repo_key"] == "github:owner/x"
    assert body["posted_id"] == "posted_proj_x"
    assert body["channels"] == ["instagram", "linkedin"]


def test_submit_returns_400_on_bad_url(client):
    with patch(
        "src.operator_api.web.app.submit_url_and_generate",
        side_effect=ValueError("Unsupported submission host: example.com"),
    ), patch("src.operator_api.web.app.connect", return_value=_stub_conn()), _patch_runs():
        resp = client.post("/api/submit", json={"url": "https://example.com"})
    assert resp.status_code == 400
    assert "Unsupported submission host" in resp.get_json()["error"]


def test_submit_returns_502_when_all_channels_fail(client):
    with patch(
        "src.operator_api.web.app.submit_url_and_generate",
        side_effect=RuntimeError("All channels failed to generate"),
    ), patch("src.operator_api.web.app.connect", return_value=_stub_conn()), _patch_runs():
        resp = client.post("/api/submit", json={"url": "https://github.com/owner/x"})
    assert resp.status_code == 502
    assert "channels failed" in resp.get_json()["error"]
