"""Tests for the SQL emitted by mark_post_approved / mark_post_rejected.

These use a fake psycopg cursor that records the SQL + params, so we don't
need a real Postgres for unit-level coverage of the JSONB mutation logic.
Integration coverage happens via the dashboard API tests (`test_api_routes`).
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from src.publishing.repository import mark_post_approved, mark_post_rejected


def _fake_conn(rowcount: int):
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.execute = MagicMock()
    cur.rowcount = rowcount

    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)
    conn.commit = MagicMock()
    return conn, cur


def test_mark_post_approved_returns_true_on_hit():
    conn, cur = _fake_conn(rowcount=1)
    when = datetime(2026, 5, 17, 14, 0, tzinfo=timezone.utc)

    result = mark_post_approved(conn, post_id="post_linkedin_abc", scheduled_for=when)

    assert result is True
    conn.commit.assert_called_once()
    sql, params = cur.execute.call_args[0]
    assert "approved" in sql
    assert "scheduled_for" in sql
    assert params[0] == "post_linkedin_abc"  # first WHEN clause uses post_id
    assert params[3] == when.isoformat()      # scheduled_for ISO string


def test_mark_post_approved_returns_false_when_no_row_matches():
    conn, _ = _fake_conn(rowcount=0)
    result = mark_post_approved(
        conn, post_id="post_nope", scheduled_for=datetime.now(timezone.utc)
    )
    assert result is False


def test_mark_post_rejected_returns_true_on_hit():
    conn, cur = _fake_conn(rowcount=1)
    result = mark_post_rejected(conn, post_id="post_instagram_abc", reason="off-brand")
    assert result is True
    sql, _ = cur.execute.call_args[0]
    assert "rejected" in sql
    assert "rejected_by" in sql


def test_mark_post_rejected_works_without_reason():
    conn, cur = _fake_conn(rowcount=1)
    result = mark_post_rejected(conn, post_id="post_x_abc")
    assert result is True
