"""Manual single-candidate orchestration.

Used by both `operator_api.cli.cmd_submit` / the dashboard's
`POST /api/submit` endpoint (full submit-and-generate flow), and the
`POST /api/evaluations/<candidate_id>/generate` endpoint (skip submit,
just generate from an existing evaluation).

Two entry points:

    submit_url_and_generate(...) — full path: submit URL → enrich → synthesize
        evaluation → forced selection → content gen → publish.

    generate_post_for_existing_candidate(...) — partial: candidate + evaluation
        already exist; only forced selection → content gen → publish.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from src.ai_gateway.factory import get_llm_provider
from src.ai_gateway.llm.base import LLMProvider
from src.candidate_intelligence.enrichment import enrich_github_candidate
from src.candidate_intelligence.evaluation import synthesize_evaluation_for_manual
from src.candidate_intelligence.repository import set_evaluation, upsert_candidate
from src.candidate_intelligence.source_adapters.github_discovery.client import GithubClient
from src.candidate_intelligence.source_adapters.manual_submission import submit_manual
from src.common.config import Settings
from src.common.ids import selection_id
from src.content_generation import generate_post_package
from src.contracts.candidate import Candidate
from src.contracts.evaluation import Evaluation
from src.contracts.package import PostPackage
from src.contracts.selection import RankingBreakdown, SelectionDecision
from src.publishing import publish_packages

_log = logging.getLogger(__name__)

DEFAULT_CHANNELS = ["instagram", "linkedin"]


def generate_post_for_existing_candidate(
    conn: psycopg.Connection,
    settings: Settings,
    run_id: str,
    candidate: Candidate,
    evaluation: Evaluation,
    *,
    channels: list[str] | None = None,
    provider: LLMProvider,
    ranking_version: str = "manual_v1",
    ranking_reason: str = "Manually triggered by operator.",
) -> tuple[str, list[PostPackage], list[Path]]:
    """Force-select a candidate + generate content + publish, in one call.

    Returns (posted_id, packages, json_sidecar_paths). Raises if every channel
    failed; partial success returns whatever channels did succeed.
    """
    target_channels = channels or DEFAULT_CHANNELS

    selection = SelectionDecision(
        selection_id=selection_id(),
        candidate_id=candidate.candidate_id,
        project_id=candidate.project_id,
        run_id=run_id,
        ranking_version=ranking_version,
        ranking_score=float(evaluation.scores.overall),
        rank_in_run=1,
        total_candidates_in_run=1,
        score_breakdown=RankingBreakdown(
            evaluation_overall_score=float(evaluation.scores.overall)
        ),
        ranking_reasons=[ranking_reason],
        eligible=True,
        selected=True,
        selected_for_channels=target_channels,
        selected_at=datetime.now(timezone.utc),
    )

    packages: list[PostPackage] = []
    for channel in target_channels:
        try:
            package = generate_post_package(
                conn, settings, run_id, candidate, evaluation, provider, channel=channel
            )
            packages.append(package)
        except Exception as exc:
            _log.exception("Channel %s failed for candidate %s: %s", channel, candidate.candidate_id, exc)

    if not packages:
        raise RuntimeError("All channels failed to generate")

    posted_id, json_paths = publish_packages(
        conn,
        settings,
        candidate=candidate,
        evaluation=evaluation,
        selection=selection,
        packages=packages,
    )
    return posted_id, packages, json_paths


def submit_url_and_generate(
    conn: psycopg.Connection,
    settings: Settings,
    run_id: str,
    url: str,
    *,
    channels: list[str] | None = None,
    provider: LLMProvider | None = None,
    operator: str = "operator",
) -> tuple[Candidate, Evaluation, str, list[PostPackage], list[Path]]:
    """Full manual-submission flow for one URL → ready-for-review post(s).

    Steps (mirrors `cmd_submit`, now reused by the dashboard's /api/submit):

        1. submit_manual            — write candidate row from GitHub/Devpost
        2. enrich_github_candidate  — README + commits + issues (GitHub only)
        3. synthesize_evaluation    — placeholder Evaluation (no LLM scoring)
        4. generate_post_for_existing_candidate — forced selection → CG → publish

    Returns (candidate, evaluation, posted_id, packages, json_sidecar_paths).
    Raises if every channel fails to generate.
    """
    candidate = submit_manual(conn, settings, run_id, url, operator=operator)

    if candidate.github:
        gh_client = GithubClient(conn, run_id, settings.gh_token)
        enrichment = enrich_github_candidate(candidate.github.full_name, gh_client)
        candidate = candidate.model_copy(update={"enrichment": enrichment})
        upsert_candidate(conn, candidate)

    evaluation = synthesize_evaluation_for_manual(candidate)
    set_evaluation(
        conn,
        candidate_id=candidate.candidate_id,
        evaluation_payload=evaluation.model_dump(mode="json"),
        skip=False,
    )

    if provider is None:
        provider = get_llm_provider(settings, conn, run_id)

    posted_id, packages, json_paths = generate_post_for_existing_candidate(
        conn,
        settings,
        run_id,
        candidate,
        evaluation,
        channels=channels,
        provider=provider,
        ranking_reason=f"Manually submitted by {operator}.",
    )
    return candidate, evaluation, posted_id, packages, json_paths
