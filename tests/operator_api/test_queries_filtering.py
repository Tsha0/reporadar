"""get_recent_posts must filter by review-status; get_scheduled_posts must
return only approved instances. Both must extract scheduled_for for display.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from src.operator_api.web import queries


def _conn_returning(rows: list[dict]):
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.execute = MagicMock()
    cur.fetchall = MagicMock(return_value=rows)
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    return conn


def _row_with_instances(instances: list[dict]) -> dict:
    return {
        "id": "posted_proj_1",
        "canonical_repo_url": "https://github.com/example/x",
        "github": {"full_name": "example/x", "url": "https://github.com/example/x"},
        "hackathon": None,
        "project_description": {"ai_summary": "x"},
        "post_instances": instances,
        "posting_state": {"has_been_posted": False},
        "updated_at": None,
    }


def _inst(post_id: str, status: str, scheduled_for: str | None = None) -> dict:
    publication = {"external_post_url": None}
    if scheduled_for:
        publication["scheduled_for"] = scheduled_for
    return {
        "post_id": post_id,
        "platform": "linkedin",
        "status": status,
        "content": {"text": f"caption for {post_id}", "character_count": 10},
        "media": [{"local_path": f"output/{post_id}.jpg", "alt_text": "x", "width": 1024, "height": 1024}],
        "source_links": [],
        "publication": publication,
        "review": {},
    }


def test_get_recent_posts_returns_only_review_statuses():
    conn = _conn_returning([
        _row_with_instances([
            _inst("post_a", "exported"),
            _inst("post_b", "approved", scheduled_for="2026-05-17T14:00:00"),
            _inst("post_c", "rejected"),
            _inst("post_d", "manually_posted"),
            _inst("post_e", "ready_for_review"),
        ])
    ])

    posts = queries.get_recent_posts(conn)

    ids = {p["id"] for p in posts}
    assert ids == {"post_a", "post_e"}


def test_get_recent_published_returns_published_and_failed_and_manually_posted():
    conn = _conn_returning([
        _row_with_instances([
            _inst("post_published", "published"),
            _inst("post_failed", "failed"),
            _inst("post_manual", "manually_posted"),
            _inst("post_exported", "exported"),
            _inst("post_approved", "approved"),
            _inst("post_rejected", "rejected"),
        ])
    ])

    posts = queries.get_recent_published_posts(conn)
    ids = {p["id"] for p in posts}
    assert ids == {"post_published", "post_failed", "post_manual"}


def test_get_scheduled_posts_returns_only_approved_and_sorts_by_time():
    conn = _conn_returning([
        _row_with_instances([
            _inst("post_late", "approved", scheduled_for="2026-12-01T10:00:00"),
            _inst("post_early", "approved", scheduled_for="2026-05-17T08:00:00"),
            _inst("post_unscheduled", "approved"),  # no scheduled_for
            _inst("post_other", "exported"),
        ])
    ])

    posts = queries.get_scheduled_posts(conn)

    ids = [p["id"] for p in posts]
    # Approved only, sorted ascending by scheduled_for (unscheduled sinks last)
    assert ids == ["post_early", "post_late", "post_unscheduled"]
    # scheduled_for is formatted for display
    assert posts[0]["scheduled_for"] == "2026-05-17 08:00"
