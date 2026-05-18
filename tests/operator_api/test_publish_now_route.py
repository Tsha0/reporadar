"""Flask test-client coverage of POST /api/posts/<post_id>/publish-now.

Patches publish_post_to_linkedin / publish_post_to_instagram so we exercise
the request/response wiring without making real network calls. The route
first calls `find_post_by_id` to read `platform` and dispatch to the right
adapter — tests stub that out too.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.operator_api.web.app import create_app
from src.publishing import InstagramPublishError, LinkedInPublishError


class _StubSettings:
    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        self.database_url = "postgresql://stub"


@pytest.fixture
def client(tmp_path: Path):
    settings = _StubSettings(str(tmp_path))
    app = create_app(settings)
    return app.test_client()


def _stub_conn():
    fake_conn = MagicMock()
    fake_conn.__enter__.return_value = fake_conn
    fake_conn.__exit__.return_value = False
    return fake_conn


def _post_row(platform: str = "linkedin"):
    """Mimic the (posted_id, instance) tuple find_post_by_id returns."""
    return ("posted_1", {"platform": platform, "post_id": "post_x"})


def test_publish_now_routes_linkedin_post_to_linkedin_adapter(client):
    with patch(
        "src.operator_api.web.app.find_post_by_id", return_value=_post_row("linkedin")
    ), patch(
        "src.operator_api.web.app.publish_post_to_linkedin",
        return_value=(
            "urn:li:share:7234567890123456789",
            "https://www.linkedin.com/feed/update/urn:li:share:7234567890123456789/",
        ),
    ) as mock_pub, patch(
        "src.operator_api.web.app.publish_post_to_instagram"
    ) as mock_ig, patch(
        "src.operator_api.web.app.connect", return_value=_stub_conn()
    ), patch(
        "src.operator_api.web.app.start_run", return_value="run_abc"
    ), patch(
        "src.operator_api.web.app.finish_run"
    ):
        resp = client.post("/api/posts/post_linkedin_abc/publish-now", json={})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "published"
    assert body["post_id"] == "post_linkedin_abc"
    assert "linkedin.com" in body["external_post_url"]
    assert body["external_post_id"] == "urn:li:share:7234567890123456789"
    mock_pub.assert_called_once()
    assert mock_pub.call_args.kwargs["post_id"] == "post_linkedin_abc"
    assert mock_pub.call_args.kwargs["operator"] == "dashboard"
    mock_ig.assert_not_called()


def test_publish_now_routes_instagram_post_to_instagram_adapter(client):
    with patch(
        "src.operator_api.web.app.find_post_by_id",
        return_value=_post_row("instagram"),
    ), patch(
        "src.operator_api.web.app.publish_post_to_instagram",
        return_value=("17909999999999999", "https://www.instagram.com/p/CxYzAbC/"),
    ) as mock_ig, patch(
        "src.operator_api.web.app.publish_post_to_linkedin"
    ) as mock_li, patch(
        "src.operator_api.web.app.connect", return_value=_stub_conn()
    ), patch(
        "src.operator_api.web.app.start_run", return_value="run_abc"
    ), patch(
        "src.operator_api.web.app.finish_run"
    ):
        resp = client.post("/api/posts/post_ig_abc/publish-now", json={})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "published"
    assert body["post_id"] == "post_ig_abc"
    assert "instagram.com" in body["external_post_url"]
    assert body["external_post_id"] == "17909999999999999"
    mock_ig.assert_called_once()
    mock_li.assert_not_called()


def test_publish_now_returns_404_when_post_missing(client):
    with patch(
        "src.operator_api.web.app.find_post_by_id", return_value=None
    ), patch(
        "src.operator_api.web.app.connect", return_value=_stub_conn()
    ), patch(
        "src.operator_api.web.app.start_run", return_value="run_x"
    ), patch(
        "src.operator_api.web.app.finish_run"
    ):
        resp = client.post("/api/posts/post_nope/publish-now", json={})

    assert resp.status_code == 404
    assert "not found" in resp.get_json()["error"]


def test_publish_now_returns_400_on_unsupported_channel(client):
    with patch(
        "src.operator_api.web.app.find_post_by_id",
        return_value=_post_row("tiktok"),
    ), patch(
        "src.operator_api.web.app.connect", return_value=_stub_conn()
    ), patch(
        "src.operator_api.web.app.start_run", return_value="run_x"
    ), patch(
        "src.operator_api.web.app.finish_run"
    ):
        resp = client.post("/api/posts/post_x/publish-now", json={})

    assert resp.status_code == 400
    assert "tiktok" in resp.get_json()["error"]


def test_publish_now_returns_4xx_on_linkedin_auth_error(client):
    err = LinkedInPublishError(
        "createPost: 401 Unauthorized — LINKEDIN_ACCESS_TOKEN is invalid or expired",
        status_code=401,
        body='{"message": "Invalid access token"}',
    )
    with patch(
        "src.operator_api.web.app.find_post_by_id", return_value=_post_row("linkedin")
    ), patch(
        "src.operator_api.web.app.publish_post_to_linkedin", side_effect=err,
    ), patch(
        "src.operator_api.web.app.connect", return_value=_stub_conn()
    ), patch(
        "src.operator_api.web.app.start_run", return_value="run_abc"
    ), patch(
        "src.operator_api.web.app.finish_run"
    ) as mock_finish:
        resp = client.post("/api/posts/post_x/publish-now", json={})

    assert resp.status_code == 400
    body = resp.get_json()
    assert body["status_code"] == 401
    assert "401 Unauthorized" in body["error"]
    assert "Invalid access token" in body["body"]
    mock_finish.assert_called_once()


def test_publish_now_returns_4xx_on_instagram_auth_error(client):
    err = InstagramPublishError(
        "mediaPublish: 401 Unauthorized — IG_ACCESS_TOKEN is invalid",
        status_code=401,
        body='{"error":{"message":"Invalid OAuth"}}',
    )
    with patch(
        "src.operator_api.web.app.find_post_by_id",
        return_value=_post_row("instagram"),
    ), patch(
        "src.operator_api.web.app.publish_post_to_instagram", side_effect=err,
    ), patch(
        "src.operator_api.web.app.connect", return_value=_stub_conn()
    ), patch(
        "src.operator_api.web.app.start_run", return_value="run_abc"
    ), patch(
        "src.operator_api.web.app.finish_run"
    ):
        resp = client.post("/api/posts/post_ig/publish-now", json={})

    assert resp.status_code == 400
    body = resp.get_json()
    assert body["status_code"] == 401
    assert "401 Unauthorized" in body["error"]


def test_publish_now_returns_502_on_upstream_5xx(client):
    err = LinkedInPublishError(
        "createPost: HTTP 500", status_code=500, body="oops"
    )
    with patch(
        "src.operator_api.web.app.find_post_by_id", return_value=_post_row("linkedin")
    ), patch(
        "src.operator_api.web.app.publish_post_to_linkedin", side_effect=err
    ), patch(
        "src.operator_api.web.app.connect", return_value=_stub_conn()
    ), patch(
        "src.operator_api.web.app.start_run", return_value="run_x"
    ), patch(
        "src.operator_api.web.app.finish_run"
    ):
        resp = client.post("/api/posts/post_x/publish-now", json={})
    assert resp.status_code == 502


def test_publish_now_returns_500_on_unexpected_exception(client):
    with patch(
        "src.operator_api.web.app.find_post_by_id", return_value=_post_row("linkedin")
    ), patch(
        "src.operator_api.web.app.publish_post_to_linkedin",
        side_effect=RuntimeError("DB down"),
    ), patch(
        "src.operator_api.web.app.connect", return_value=_stub_conn()
    ), patch(
        "src.operator_api.web.app.start_run", return_value="run_x"
    ), patch(
        "src.operator_api.web.app.finish_run"
    ):
        resp = client.post("/api/posts/post_x/publish-now", json={})
    assert resp.status_code == 500
    assert "DB down" in resp.get_json()["error"]
