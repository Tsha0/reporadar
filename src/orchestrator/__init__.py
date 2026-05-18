"""Workflow Orchestrator — coordinates the full pipeline run.

Owns: pipeline_runs table, run-level retry logic, idempotency. Calls every
other service via its public entry point — never reaches into another
service's internals.
"""
from src.orchestrator.manual import (
    generate_post_for_existing_candidate,
    submit_url_and_generate,
)
from src.orchestrator.pipeline import run_pipeline

__all__ = [
    "run_pipeline",
    "generate_post_for_existing_candidate",
    "submit_url_and_generate",
]
