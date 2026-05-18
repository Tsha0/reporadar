"""Data-access for `posted_repositories`.

Owned exclusively by the Publishing service. Other services read via API /
read model, never write here directly.
"""
from __future__ import annotations

from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Jsonb

from src.common.ids import posted_repo_id_for
from src.contracts.candidate import Candidate
from src.contracts.content import GeneratedContent
from src.contracts.evaluation import Evaluation
from src.contracts.media import MediaAsset
from src.contracts.package import PostPackage
from src.contracts.selection import SelectionDecision


def upsert_posted_repository(
    conn: psycopg.Connection,
    *,
    candidate: Candidate,
    evaluation: Evaluation,
    selection: SelectionDecision,
    packages: list[PostPackage],
) -> str:
    """Insert (or merge into existing) posted_repositories row + add post_instances.

    Returns the posted_repository row id.
    """
    posted_id = posted_repo_id_for(candidate.project_id)
    canonical_url = candidate.github.url if candidate.github else (
        candidate.hackathon.devpost_url if candidate.hackathon else ""
    )

    github_payload = candidate.github.model_dump(mode="json") if candidate.github else None
    hackathon_payload = candidate.hackathon.model_dump(mode="json") if candidate.hackathon else None

    project_description = {
        "ai_summary": evaluation.summary,
        "why_interesting": evaluation.why_interesting,
        "target_audience": [evaluation.audience],
        "tags": candidate.github.topics if candidate.github else [],
    }
    source_payload = {
        "original_source_type": candidate.source.source_type,
        "source_urls": [candidate.source.source_url],
        "discovery_run_id": candidate.run_id,
        "candidate_id": candidate.candidate_id,
        "evaluation_id": evaluation.evaluation_id,
        "selection_id": selection.selection_id,
    }
    evaluation_snapshot = evaluation.model_dump(mode="json")
    ranking_snapshot = {
        "ranking_version": selection.ranking_version,
        "ranking_score": selection.ranking_score,
        "rank_in_run": selection.rank_in_run,
        "total_candidates_in_run": selection.total_candidates_in_run,
        "ranked_at": (selection.selected_at or datetime.now(timezone.utc)).isoformat(),
        "selection_reason": "; ".join(selection.ranking_reasons),
    }
    post_instances_payload = [_post_instance_for(pkg) for pkg in packages]

    now = datetime.now(timezone.utc)
    posting_state = {
        "has_been_posted": False,
        "posted_platforms": [],
        "exported_platforms": [pkg.channel for pkg in packages],
        "first_posted_at": None,
        "last_posted_at": None,
        "do_not_repost": True,
    }
    audit = {
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "created_by": "publishing_service",
        "schema_version": 1,
    }

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO posted_repositories (
                id, project_id, canonical_repo_key, canonical_repo_url,
                github, hackathon, project_description, source,
                evaluation_snapshot, ranking_snapshot, post_instances,
                posting_state, audit
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (canonical_repo_key) DO UPDATE SET
                github               = EXCLUDED.github,
                hackathon            = EXCLUDED.hackathon,
                project_description  = EXCLUDED.project_description,
                evaluation_snapshot  = EXCLUDED.evaluation_snapshot,
                ranking_snapshot     = EXCLUDED.ranking_snapshot,
                post_instances       = posted_repositories.post_instances || EXCLUDED.post_instances,
                posting_state        = jsonb_set(
                    posted_repositories.posting_state,
                    '{exported_platforms}',
                    EXCLUDED.posting_state -> 'exported_platforms'
                )
            """,
            (
                posted_id,
                candidate.project_id,
                candidate.canonical_repo_key,
                canonical_url,
                Jsonb(github_payload) if github_payload is not None else None,
                Jsonb(hackathon_payload) if hackathon_payload is not None else None,
                Jsonb(project_description),
                Jsonb(source_payload),
                Jsonb(evaluation_snapshot),
                Jsonb(ranking_snapshot),
                Jsonb(post_instances_payload),
                Jsonb(posting_state),
                Jsonb(audit),
            ),
        )
    conn.commit()
    return posted_id


def mark_manually_posted(
    conn: psycopg.Connection,
    *,
    posted_id: str,
    channel: str,
    external_post_url: str | None,
    operator: str = "operator",
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE posted_repositories
            SET post_instances = (
                SELECT jsonb_agg(
                    CASE
                        WHEN inst ->> 'platform' = %s
                        THEN jsonb_set(
                            jsonb_set(
                                inst,
                                '{status}', '"manually_posted"'::jsonb
                            ),
                            '{publication}',
                            jsonb_build_object(
                                'publishing_mode', 'manual',
                                'posted_by', %s::text,
                                'posted_at', %s::text,
                                'external_post_url', %s::text,
                                'external_post_id', null
                            )
                        )
                        ELSE inst
                    END
                )
                FROM jsonb_array_elements(post_instances) inst
            ),
            posting_state = jsonb_set(
                jsonb_set(
                    jsonb_set(posting_state, '{has_been_posted}', 'true'::jsonb),
                    '{last_posted_at}', to_jsonb(%s::text)
                ),
                '{first_posted_at}',
                CASE WHEN posting_state ? 'first_posted_at' AND posting_state ->> 'first_posted_at' IS NOT NULL
                     THEN posting_state -> 'first_posted_at'
                     ELSE to_jsonb(%s::text) END
            )
            WHERE id = %s
            """,
            (channel, operator, now, external_post_url, now, now, posted_id),
        )
    conn.commit()


def find_post_by_id(conn: psycopg.Connection, post_id: str) -> tuple[str, dict] | None:
    """Locate which posted_repositories row a given post_instance.post_id belongs to.

    Returns (posted_id, post_instance_dict) or None if not found. Used by the
    dashboard's approve/reject endpoints which only know the per-channel
    `post_id`, not the parent canonical row.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, inst
            FROM posted_repositories,
                 jsonb_array_elements(post_instances) inst
            WHERE inst ->> 'post_id' = %s
            LIMIT 1
            """,
            (post_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return row[0], row[1]


def mark_post_approved(
    conn: psycopg.Connection,
    *,
    post_id: str,
    scheduled_for: datetime,
    operator: str = "operator",
) -> bool:
    """Approve one post_instance for upload at a scheduled time.

    Sets `post_instance.status='approved'`, fills `review.approved_by/at`, and
    stamps `publication.scheduled_for`. Returns True if a row was updated,
    False if no matching post_id exists.
    """
    now = datetime.now(timezone.utc).isoformat()
    scheduled_iso = scheduled_for.isoformat()
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE posted_repositories
            SET post_instances = (
                SELECT jsonb_agg(
                    CASE
                        WHEN inst ->> 'post_id' = %s
                        THEN jsonb_set(
                            jsonb_set(
                                jsonb_set(inst, '{status}', '"approved"'::jsonb),
                                '{review}',
                                jsonb_build_object(
                                    'approved_by', %s::text,
                                    'approved_at', %s::text,
                                    'review_notes', inst -> 'review' -> 'review_notes'
                                )
                            ),
                            '{publication}',
                            (COALESCE(inst -> 'publication', '{}'::jsonb))
                                || jsonb_build_object('scheduled_for', %s::text)
                        )
                        ELSE inst
                    END
                )
                FROM jsonb_array_elements(post_instances) inst
            )
            WHERE post_instances @> jsonb_build_array(jsonb_build_object('post_id', %s::text))
            """,
            (post_id, operator, now, scheduled_iso, post_id),
        )
        affected = cur.rowcount
    conn.commit()
    return affected > 0


def mark_post_published(
    conn: psycopg.Connection,
    *,
    post_id: str,
    external_post_url: str,
    external_post_id: str | None = None,
    operator: str = "publishing_service",
) -> bool:
    """Mark one post_instance as actually published via the API adapter.

    Transitions:
        post_instance.status                  → 'published'
        post_instance.publication.publishing_mode → 'api'
        post_instance.publication.posted_by       → operator (default: service name)
        post_instance.publication.posted_at       → NOW()
        post_instance.publication.external_post_url → permalink
        post_instance.publication.external_post_id  → URN
        posting_state.has_been_posted         → true
        posting_state.first_posted_at         → preserved or NOW()
        posting_state.last_posted_at          → NOW()
        posting_state.posted_platforms        → existing array ∪ [channel]

    Returns True if a row was updated.
    """
    now = datetime.now(timezone.utc).isoformat()
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE posted_repositories
            SET post_instances = (
                SELECT jsonb_agg(
                    CASE
                        WHEN inst ->> 'post_id' = %s
                        THEN jsonb_set(
                            jsonb_set(inst, '{status}', '"published"'::jsonb),
                            '{publication}',
                            jsonb_build_object(
                                'publishing_mode', 'api',
                                'posted_by', %s::text,
                                'posted_at', %s::text,
                                'external_post_url', %s::text,
                                'external_post_id', %s::text
                            )
                        )
                        ELSE inst
                    END
                )
                FROM jsonb_array_elements(post_instances) inst
            ),
            posting_state = jsonb_set(
                jsonb_set(
                    jsonb_set(posting_state, '{has_been_posted}', 'true'::jsonb),
                    '{last_posted_at}', to_jsonb(%s::text)
                ),
                '{first_posted_at}',
                CASE WHEN posting_state ? 'first_posted_at' AND posting_state ->> 'first_posted_at' IS NOT NULL
                     THEN posting_state -> 'first_posted_at'
                     ELSE to_jsonb(%s::text) END
            )
            WHERE post_instances @> jsonb_build_array(jsonb_build_object('post_id', %s::text))
            """,
            (
                post_id,
                operator,
                now,
                external_post_url,
                external_post_id,
                now,
                now,
                post_id,
            ),
        )
        affected = cur.rowcount
    conn.commit()
    return affected > 0


def mark_post_failed(
    conn: psycopg.Connection,
    *,
    post_id: str,
    error_message: str,
) -> bool:
    """Flag a post_instance as having failed to publish via the API adapter.

    Sets status='failed' and stuffs the error into publication.error_message
    so the dashboard can surface it. The post stays in the array (audit).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE posted_repositories
            SET post_instances = (
                SELECT jsonb_agg(
                    CASE
                        WHEN inst ->> 'post_id' = %s
                        THEN jsonb_set(
                            jsonb_set(inst, '{status}', '"failed"'::jsonb),
                            '{publication, error_message}',
                            to_jsonb(%s::text),
                            true
                        )
                        ELSE inst
                    END
                )
                FROM jsonb_array_elements(post_instances) inst
            )
            WHERE post_instances @> jsonb_build_array(jsonb_build_object('post_id', %s::text))
            """,
            (post_id, error_message[:500], post_id),
        )
        affected = cur.rowcount
    conn.commit()
    return affected > 0


def mark_post_rejected(
    conn: psycopg.Connection,
    *,
    post_id: str,
    operator: str = "operator",
    reason: str | None = None,
) -> bool:
    """Reject one post_instance.

    The post_instance stays in the JSONB array (audit trail), but its status
    becomes 'rejected' so the dashboard filters it out of the review queue.
    Returns True if a row was updated.
    """
    now = datetime.now(timezone.utc).isoformat()
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE posted_repositories
            SET post_instances = (
                SELECT jsonb_agg(
                    CASE
                        WHEN inst ->> 'post_id' = %s
                        THEN jsonb_set(
                            jsonb_set(inst, '{status}', '"rejected"'::jsonb),
                            '{review}',
                            jsonb_build_object(
                                'approved_by', null,
                                'approved_at', null,
                                'rejected_by', %s::text,
                                'rejected_at', %s::text,
                                'review_notes', COALESCE(%s::jsonb, inst -> 'review' -> 'review_notes')
                            )
                        )
                        ELSE inst
                    END
                )
                FROM jsonb_array_elements(post_instances) inst
            )
            WHERE post_instances @> jsonb_build_array(jsonb_build_object('post_id', %s::text))
            """,
            (
                post_id,
                operator,
                now,
                Jsonb(reason) if reason is not None else None,
                post_id,
            ),
        )
        affected = cur.rowcount
    conn.commit()
    return affected > 0


def _post_instance_for(package: PostPackage) -> dict:
    return {
        "post_id": package.post_id,
        "platform": package.channel,
        "status": "exported",
        "content": package.content.model_dump(mode="json"),
        "media": [_media_dict(asset) for asset in package.media],
        "source_links": package.source_links,
        "review": {"approved_by": None, "approved_at": None, "review_notes": package.review_notes},
        "publication": {
            "publishing_mode": "manual",
            "posted_by": None,
            "posted_at": None,
            "external_post_url": None,
            "external_post_id": None,
        },
    }


def _media_dict(asset: MediaAsset) -> dict:
    return asset.model_dump(mode="json")
