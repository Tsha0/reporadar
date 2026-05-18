from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import psycopg

from src.candidate_intelligence.repository import set_post_link
from src.common.config import Settings
from src.contracts.candidate import Candidate
from src.contracts.content import GeneratedContent
from src.contracts.evaluation import Evaluation
from src.contracts.media import MediaAsset
from src.contracts.package import PostPackage
from src.contracts.selection import SelectionDecision
from src.publishing.adapters.instagram_graph import (
    InstagramPublishError,
    publish_to_instagram,
)
from src.publishing.adapters.linkedin_api import (
    LinkedInPublishError,
    publish_to_linkedin,
)
from src.publishing.adapters.manual_export import export_to_disk
from src.publishing.repository import (
    find_post_by_id,
    mark_post_failed,
    mark_post_published,
    upsert_posted_repository,
)

_log = logging.getLogger(__name__)


def publish_packages(
    conn: psycopg.Connection,
    settings: Settings,
    *,
    candidate: Candidate,
    evaluation: Evaluation,
    selection: SelectionDecision,
    packages: list[PostPackage],
) -> tuple[str, list[Path]]:
    """Default manual-export publishing flow.

    1. Write a posted_repositories row (snapshot of repo + evaluation + ranking +
       one post_instance per package).
    2. Write a sidecar JSON per package in `output_dir` for the operator.
    3. Backfill the candidate row's `post_link` so the dashboard can join.

    Returns (posted_id, [json_paths]).
    """
    posted_id = upsert_posted_repository(
        conn,
        candidate=candidate,
        evaluation=evaluation,
        selection=selection,
        packages=packages,
    )

    json_paths: list[Path] = []
    for package in packages:
        json_path = export_to_disk(package, settings.output_dir)
        json_paths.append(json_path)
        _log.info("Exported %s package to %s", package.channel, json_path)

    set_post_link(
        conn,
        candidate_id=candidate.candidate_id,
        post_link_payload={
            "posted": False,
            "exported": True,
            "posted_project_id": posted_id,
            "post_ids": [pkg.post_id for pkg in packages],
            "exported_at": packages[0].created_at.isoformat() if packages else None,
        },
    )

    return posted_id, json_paths


def publish_post_to_linkedin(
    conn: psycopg.Connection,
    settings: Settings,
    *,
    post_id: str,
    operator: str = "publishing_service",
) -> tuple[str, str]:
    """Push one already-exported LinkedIn `post_instance` to LinkedIn's feed.

    1. Load the post_instance JSONB blob for `post_id`.
    2. Rehydrate it into a `PostPackage` (just enough fields for the adapter).
    3. Call `publish_to_linkedin(...)` — the LinkedIn Posts API adapter.
    4. On success: `mark_post_published(...)` flips status to 'published' and
       fills the publication block.
       On failure: `mark_post_failed(...)` records the error message.

    Returns `(external_post_id, external_post_url)` on success.
    Raises `LinkedInPublishError` on failure (after marking the row as failed).
    """
    row = find_post_by_id(conn, post_id)
    if row is None:
        raise LinkedInPublishError(f"No post_instance found with post_id={post_id!r}")
    _, instance = row

    if instance.get("platform") != "linkedin":
        raise LinkedInPublishError(
            f"post_id={post_id!r} is on channel {instance.get('platform')!r}, not linkedin"
        )

    package = _rehydrate_package(post_id, instance)

    try:
        post_urn, permalink = publish_to_linkedin(package, settings)
    except LinkedInPublishError as exc:
        detail = f"{exc} (status={exc.status_code})" if exc.status_code else str(exc)
        mark_post_failed(conn, post_id=post_id, error_message=detail)
        _log.exception("LinkedIn publish failed for %s: %s", post_id, detail)
        raise

    mark_post_published(
        conn,
        post_id=post_id,
        external_post_url=permalink,
        external_post_id=post_urn,
        operator=operator,
    )
    _log.info("Published %s to LinkedIn: %s", post_id, permalink)
    return post_urn, permalink


def publish_post_to_instagram(
    conn: psycopg.Connection,
    settings: Settings,
    *,
    post_id: str,
    operator: str = "publishing_service",
) -> tuple[str, str]:
    """Push one already-exported Instagram `post_instance` to IG via the Graph API.

    1. Load the post_instance JSONB blob for `post_id`.
    2. Rehydrate it into a `PostPackage`.
    3. Call `publish_to_instagram(...)` — uploads the local JPEG to the
       configured image host, then runs the 3-step Graph API container flow.
    4. On success: `mark_post_published(...)` flips status to 'published' and
       fills the publication block.
       On failure: `mark_post_failed(...)` records the error message.

    Returns `(media_id, permalink)` on success.
    Raises `InstagramPublishError` on failure (after marking the row failed).
    """
    row = find_post_by_id(conn, post_id)
    if row is None:
        raise InstagramPublishError(f"No post_instance found with post_id={post_id!r}")
    _, instance = row

    if instance.get("platform") != "instagram":
        raise InstagramPublishError(
            f"post_id={post_id!r} is on channel {instance.get('platform')!r}, not instagram"
        )

    package = _rehydrate_package(post_id, instance)

    try:
        media_id, permalink = publish_to_instagram(package, settings)
    except InstagramPublishError as exc:
        detail = f"{exc} (status={exc.status_code})" if exc.status_code else str(exc)
        mark_post_failed(conn, post_id=post_id, error_message=detail)
        _log.exception("Instagram publish failed for %s: %s", post_id, detail)
        raise

    mark_post_published(
        conn,
        post_id=post_id,
        external_post_url=permalink,
        external_post_id=media_id,
        operator=operator,
    )
    _log.info("Published %s to Instagram: %s", post_id, permalink)
    return media_id, permalink


def _rehydrate_package(post_id: str, instance: dict) -> PostPackage:
    """Reconstruct a PostPackage from a stored post_instance JSONB blob.

    Only the fields the LinkedIn adapter touches need to be exact — but we
    fill every required Pydantic field for completeness.
    """
    content_data = dict(instance.get("content") or {})
    if "generated_at" in content_data and isinstance(content_data["generated_at"], str):
        content_data["generated_at"] = datetime.fromisoformat(content_data["generated_at"])
    content = GeneratedContent(**content_data)

    media_assets: list[MediaAsset] = []
    for asset_data in instance.get("media") or []:
        ad = dict(asset_data)
        if "generated_at" in ad and isinstance(ad["generated_at"], str):
            ad["generated_at"] = datetime.fromisoformat(ad["generated_at"])
        media_assets.append(MediaAsset(**ad))

    return PostPackage(
        post_id=post_id,
        project_id=instance.get("project_id", ""),
        candidate_id=instance.get("candidate_id", ""),
        run_id=instance.get("run_id", ""),
        channel=instance.get("platform", "linkedin"),
        status=instance.get("status", "ready_for_review"),
        content=content,
        media=media_assets,
        source_links=list(instance.get("source_links") or []),
        review_notes=(instance.get("review") or {}).get("review_notes"),
        created_at=content.generated_at,
    )
