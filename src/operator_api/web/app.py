from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory

from src.ai_gateway.factory import get_llm_provider
from src.candidate_intelligence.repository import get_candidate_with_evaluation
from src.candidate_intelligence.service import evaluate_pending_candidates
from src.candidate_intelligence.source_adapters.devpost_discovery.scanner import scan_devpost
from src.candidate_intelligence.source_adapters.github_discovery.scanner import scan_github
from src.common.config import Settings
from src.common.db import connect, open_connection
from src.operator_api.web import queries
from src.orchestrator.manual import (
    generate_post_for_existing_candidate,
    submit_url_and_generate,
)
from src.orchestrator.pipeline import run_pipeline
from src.orchestrator.runs import finish_run, start_run
from src.publishing import (
    InstagramPublishError,
    LinkedInPublishError,
    find_post_by_id,
    mark_post_approved,
    mark_post_rejected,
    publish_post_to_instagram,
    publish_post_to_linkedin,
)


def create_app(settings: Settings) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    output_dir_abs = str(Path(settings.output_dir).resolve())

    @app.template_global()
    def score_class(score: float | None) -> str:
        if score is None:
            return "score-none"
        if score >= 7.5:
            return "score-high"
        if score >= 5.0:
            return "score-mid"
        return "score-low"

    @app.route("/media/<path:filename>")
    def media(filename):
        """Serve rendered images from settings.output_dir.

        `send_from_directory` defends against path traversal: it refuses any
        `filename` that resolves outside `output_dir_abs`.
        """
        return send_from_directory(output_dir_abs, filename)

    @app.route("/")
    def dashboard():
        conn = open_connection(settings)
        try:
            return render_template(
                "dashboard.html",
                scans=queries.get_todays_scans(conn),
                hackathons=queries.get_recent_hackathons(conn),
                evaluations=queries.get_recent_evaluations(conn),
                posts=queries.get_recent_posts(conn),
                scheduled=queries.get_scheduled_posts(conn),
                published=queries.get_recent_published_posts(conn),
                runs=queries.get_recent_runs(conn),
                today=date.today().isoformat(),
            )
        finally:
            conn.close()

    # ── Mutation endpoints (JSON, called by the dashboard's inline JS) ────

    @app.route("/api/posts/<post_id>/approve", methods=["POST"])
    def api_approve_post(post_id):
        body = request.get_json(silent=True) or {}
        scheduled_for_raw = body.get("scheduled_for")
        if not scheduled_for_raw:
            return jsonify(error="scheduled_for is required"), 400
        try:
            scheduled_dt = datetime.fromisoformat(str(scheduled_for_raw).replace("Z", "+00:00"))
        except ValueError:
            return jsonify(error=f"invalid datetime: {scheduled_for_raw!r}"), 400

        with connect(settings) as conn:
            ok = mark_post_approved(conn, post_id=post_id, scheduled_for=scheduled_dt)
        if not ok:
            return jsonify(error=f"post {post_id} not found"), 404
        return jsonify(status="approved", post_id=post_id, scheduled_for=scheduled_dt.isoformat())

    @app.route("/api/posts/<post_id>/publish-now", methods=["POST"])
    def api_publish_post_now(post_id):
        """Upload one post_instance to its channel right now.

        Routes by `post_instance.platform`: `linkedin` → Posts API,
        `instagram` → Graph API. On success the post_instance flips to
        status='published' and the publication block is filled with the
        external_post_url + URN/media_id. On failure the post_instance
        is marked status='failed' with the error message — the operator
        can retry from the dashboard.
        """
        with connect(settings) as conn:
            run_id = start_run(conn, run_type="publish_now", requested_by="operator")
            try:
                row = find_post_by_id(conn, post_id)
                if row is None:
                    finish_run(conn, run_id, error="post not found")
                    return jsonify(error=f"post {post_id} not found"), 404
                _, instance = row
                channel = instance.get("platform")

                if channel == "linkedin":
                    external_id, permalink = publish_post_to_linkedin(
                        conn, settings, post_id=post_id, operator="dashboard"
                    )
                elif channel == "instagram":
                    external_id, permalink = publish_post_to_instagram(
                        conn, settings, post_id=post_id, operator="dashboard"
                    )
                else:
                    finish_run(conn, run_id, error=f"unsupported channel: {channel}")
                    return (
                        jsonify(
                            error=f"channel {channel!r} is not supported by publish-now "
                            "(supported: linkedin, instagram)"
                        ),
                        400,
                    )
            except (LinkedInPublishError, InstagramPublishError) as exc:
                # The publish_post_to_* helper already wrote mark_post_failed
                finish_run(conn, run_id, error=str(exc))
                status = 400 if exc.status_code in (400, 401, 403, 404, 422) else 502
                return (
                    jsonify(
                        error=str(exc),
                        status_code=exc.status_code,
                        body=(exc.body or "")[:500],
                    ),
                    status,
                )
            except ValueError as exc:
                finish_run(conn, run_id, error=str(exc))
                return jsonify(error=str(exc)), 404
            except Exception as exc:
                finish_run(conn, run_id, error=str(exc))
                return jsonify(error=str(exc)), 500
            finish_run(conn, run_id)

        return jsonify(
            status="published",
            post_id=post_id,
            external_post_url=permalink,
            external_post_id=external_id,
        )

    @app.route("/api/posts/<post_id>/reject", methods=["POST"])
    def api_reject_post(post_id):
        body = request.get_json(silent=True) or {}
        reason = body.get("reason")
        with connect(settings) as conn:
            ok = mark_post_rejected(conn, post_id=post_id, reason=reason)
        if not ok:
            return jsonify(error=f"post {post_id} not found"), 404
        return jsonify(status="rejected", post_id=post_id)

    @app.route("/api/evaluations/<candidate_id>/generate", methods=["POST"])
    def api_generate_from_evaluation(candidate_id):
        """Push an already-evaluated candidate straight into Content Generation.

        Skips re-evaluation. The existing Evaluation on the candidate row is
        reused. Same flow as `cmd_submit` after the synthesize step.
        """
        body = request.get_json(silent=True) or {}
        channels = body.get("channels") or ["instagram", "linkedin"]

        with connect(settings) as conn:
            candidate, evaluation = get_candidate_with_evaluation(conn, candidate_id)
            if candidate is None or evaluation is None:
                return jsonify(error=f"candidate {candidate_id} has no evaluation"), 404

            run_id = start_run(conn, run_type="manual_generate", requested_by="operator")
            try:
                provider = get_llm_provider(settings, conn, run_id)
                posted_id, packages, json_paths = generate_post_for_existing_candidate(
                    conn, settings, run_id, candidate, evaluation,
                    channels=channels, provider=provider,
                    ranking_reason="Operator clicked 'Generate post' on evaluation.",
                )
                finish_run(conn, run_id)
            except Exception as exc:
                finish_run(conn, run_id, error=str(exc))
                return jsonify(error=str(exc)), 500

        return jsonify(
            posted_id=posted_id,
            channels=[pkg.channel for pkg in packages],
            json_paths=[str(p) for p in json_paths],
        )

    # ── Candidate Intelligence pipeline controls ──────────────────────────
    # All five endpoints below wrap a single candidate_intelligence /
    # orchestrator entry point. Each one starts a pipeline_runs row so the
    # operator can see the action in the "Recent runs" section even if the
    # body of the response gets lost.

    @app.route("/api/scan-repos", methods=["POST"])
    def api_scan_repos():
        """Run `scan_github` once. Returns the count of newly-eligible candidates.

        Note: every search hit is UPSERTed to `candidate_repository_evaluations`
        even when below thresholds (baseline tracking). The count returned is
        only the *eligible* slice — those that passed the velocity filter.
        """
        with connect(settings) as conn:
            run_id = start_run(conn, run_type="scan_repos", requested_by="dashboard")
            try:
                candidates = scan_github(conn, settings, run_id)
                finish_run(conn, run_id)
            except Exception as exc:
                finish_run(conn, run_id, error=str(exc))
                return jsonify(error=str(exc)), 500
        return jsonify(
            status="ok",
            run_id=run_id,
            eligible_count=len(candidates),
            sample=[c.canonical_repo_key for c in candidates[:5]],
        )

    @app.route("/api/scan-hackathons", methods=["POST"])
    def api_scan_hackathons():
        """Run `scan_devpost` once. Polite scraper — can take 30-60 seconds."""
        with connect(settings) as conn:
            run_id = start_run(conn, run_type="scan_hackathons", requested_by="dashboard")
            try:
                candidates = scan_devpost(conn, settings, run_id)
                finish_run(conn, run_id)
            except Exception as exc:
                finish_run(conn, run_id, error=str(exc))
                return jsonify(error=str(exc)), 500
        return jsonify(
            status="ok",
            run_id=run_id,
            eligible_count=len(candidates),
            sample=[c.canonical_repo_key for c in candidates[:5]],
        )

    @app.route("/api/evaluate", methods=["POST"])
    def api_evaluate():
        """LLM-score pending (discovered-but-un-evaluated) candidates.

        Capped at `settings.max_evaluations_per_run * 3` lookups per call.
        """
        with connect(settings) as conn:
            run_id = start_run(conn, run_type="evaluate", requested_by="dashboard")
            try:
                provider = get_llm_provider(settings, conn, run_id)
                evaluations = evaluate_pending_candidates(conn, settings, run_id, provider)
                finish_run(conn, run_id)
            except Exception as exc:
                finish_run(conn, run_id, error=str(exc))
                return jsonify(error=str(exc)), 500
        return jsonify(
            status="ok",
            run_id=run_id,
            evaluated_count=len(evaluations),
            skipped_count=sum(1 for e in evaluations if e.skip),
            sample=[
                {
                    "candidate_id": e.candidate_id,
                    "overall": e.scores.overall,
                    "skip": e.skip,
                }
                for e in evaluations[:5]
            ],
        )

    @app.route("/api/run", methods=["POST"])
    def api_run_full_pipeline():
        """Run the full daily pipeline once.

        Body: {"channels"?: [str]}  (defaults to instagram + linkedin)

        End-to-end: discover → enrich → evaluate → select → content gen → publish.
        Expensive: LLM + image API calls. Can take 1-3 minutes.
        """
        body = request.get_json(silent=True) or {}
        channels = body.get("channels") or None
        with connect(settings) as conn:
            try:
                result = run_pipeline(
                    conn, settings, channels=channels, requested_by="dashboard"
                )
            except Exception as exc:
                # run_pipeline already wrote finish_run(error=...)
                return jsonify(error=str(exc)), 500
        if result is None:
            return jsonify(status="no_eligible_candidate", run_id=None)
        return jsonify(status="ok", **result)

    @app.route("/api/submit", methods=["POST"])
    def api_submit_url():
        """Manually submit a project URL → ready-for-review posts.

        Body: {"url": str, "channels"?: [str]}
        Skips LLM evaluation. Same flow as `python -m src submit`.
        """
        body = request.get_json(silent=True) or {}
        url = body.get("url")
        if not url:
            return jsonify(error="url is required"), 400
        channels = body.get("channels") or None

        with connect(settings) as conn:
            run_id = start_run(
                conn, run_type="manual_submission", requested_by="dashboard"
            )
            try:
                candidate, _evaluation, posted_id, packages, _ = submit_url_and_generate(
                    conn, settings, run_id, url,
                    channels=channels, operator="dashboard",
                )
                finish_run(conn, run_id)
            except ValueError as exc:
                # Unsupported host, malformed URL, missing project page, etc.
                finish_run(conn, run_id, error=str(exc))
                return jsonify(error=str(exc)), 400
            except RuntimeError as exc:
                # All channels failed inside generate_post_for_existing_candidate
                finish_run(conn, run_id, error=str(exc))
                return jsonify(error=str(exc)), 502
            except Exception as exc:
                finish_run(conn, run_id, error=str(exc))
                return jsonify(error=str(exc)), 500
        return jsonify(
            status="ok",
            run_id=run_id,
            candidate_id=candidate.candidate_id,
            canonical_repo_key=candidate.canonical_repo_key,
            posted_id=posted_id,
            channels=[pkg.channel for pkg in packages],
        )

    return app
