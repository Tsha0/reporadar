# AGENTS.md

RepoRadar discovers promising GitHub and Devpost projects, evaluates them with OpenAI, generates channel-specific copy and images, and saves reviewable post packages locally. It does not auto-publish.

## Commands

Assume `.venv/` is active and `.env` is populated.

```bash
.venv/bin/python migrations/apply.py
python -m src scan-repos
python -m src scan-hackathons
python -m src evaluate
python -m src run
python -m src submit <url>
python -m src serve
python -m src daemon
python -m src verify-env
pytest -q
```

Use `.venv/bin/python` if bare `python` is not on PATH.

## Architecture

The v2 code is split into synchronous service modules under `src/`; the async event-bus split is future work.

- `common/`: settings, Postgres, logging, IDs
- `contracts/`: frozen Pydantic models; use `.model_copy(update=...)` instead of mutation
- `ai_gateway/`: OpenAI text and image adapters; OpenAI is the only supported AI provider
- `candidate_intelligence/`: discovery, enrichment, evaluation, deduplication, and `candidate_repository_evaluations` writes
- `selection/`: ranking and winner selection
- `content_generation/`: per-channel text generation
- `media_rendering/`: per-channel image profiles and prompt builders
- `post_packaging/`: channel package validation
- `publishing/`: manual export and `posted_repositories` writes
- `orchestrator/`: workflow coordination only; no business logic
- `scheduler/`: APScheduler daemon
- `operator_api/`: CLI and read-only Flask dashboard

## Rules

- `OPENAI_API_KEY`, `GH_TOKEN`, and `DATABASE_URL` are required at config load.
- The schema is `migrations/0001_initial_v2.sql`; `migrations/apply.py` is idempotent.
- `candidate_intelligence/repository.py` is the only writer of `candidate_repository_evaluations`.
- `publishing/repository.py` is the only writer of `posted_repositories`.
- Dashboard reads go through `operator_api/web/queries.py`.
- The orchestrator should only call service entry points such as discovery/evaluation, selection, content/media generation, packaging, and publishing.
- Discovery upserts every search hit so later runs can calculate deltas.
- `canonical_repo_key` is the cross-source identity (`github:owner/repo` or `devpost:<slug>`); `project_id` is deterministically derived from it.

## Channels

Current channels: `instagram` and `linkedin`.

To add a channel, add the content template, media profile, and package validator in the corresponding service folders, then route through the existing service entry points.

## Out Of Scope

- Async event bus
- Dockerized per-service deployment
- Dashboard approve/reject/regenerate endpoints
- Automatic Instagram or LinkedIn publishing
