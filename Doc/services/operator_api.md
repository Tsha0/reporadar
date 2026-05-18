# Operator API

> *"The human's interface to the whole system."*

## Purpose

Operator API is the control plane — every way a human interacts with RepoRadar lives here. Today that means:

- A **CLI** (`python -m src <command>`) for running scans, evaluating, kicking off the full pipeline, manually submitting URLs, smoke-testing the environment, and starting the daemon or dashboard.
- A **read-only Flask dashboard** (`python -m src serve`) showing posts awaiting review, recent evaluations, today's scans, hackathon candidates, and recent runs.

Per v2 §2.1 this service is a true control plane: it never does the work itself, it just calls into the other services and reads from a denormalized view of their tables. The dashboard's eventual approve/reject/regenerate buttons (v2 §9) will plug in here without other services changing.

## Source layout

```
src/operator_api/
├── __init__.py
├── cli.py                          # argparse + command handlers
└── web/
    ├── __init__.py
    ├── app.py                      # Flask factory + routes
    ├── queries.py                  # all dashboard SQL — the read model
    ├── templates/
    │   └── dashboard.html          # single-page HTML
    └── static/
        └── style.css
```

`cli.py` and `web/` are the two surfaces. They share `Settings` and the Postgres connection layer, but they don't depend on each other.

## CLI surface

`python -m src` dispatches to one of these commands. The mapping is in `cli.main()`:

| Command | Handler | What it does |
|---|---|---|
| `scan-repos` | `cmd_scan_repos` | Run `source_adapters.github_discovery.scan_github` and print the table of new repos |
| `scan-hackathons` | `cmd_scan_hackathons` | Run `source_adapters.devpost_discovery.scan_devpost` |
| `evaluate` | `cmd_evaluate` | Run `candidate_intelligence.evaluate_pending_candidates` — score any unevaluated rows |
| `run` | `cmd_run` | Run the full pipeline via `orchestrator.run_pipeline` |
| `submit <url> [--channels=...]` | `cmd_submit` | Manual URL → enriched candidate → synthesized evaluation (no LLM scoring) → forced selection → Content Generation → Publishing, in one shot. Produces a ready-for-review post immediately, no LLM gate. |
| `publish <post_id> [--dry-run]` | `cmd_publish` | Push one already-exported `post_instance` to its channel via the channel adapter. Today only `linkedin` is supported — calls `publishing.publish_post_to_linkedin` which hits the LinkedIn Posts API and updates the post_instance to `status='published'`. `--dry-run` prints what would be posted but does not contact LinkedIn. |
| `serve` | `cmd_serve` | Start the Flask dashboard |
| `daemon` | `cmd_daemon` | Start the APScheduler daemon (`scheduler.daemon.run_forever`) |
| `verify-env` | `cmd_verify_env` | Smoke-test GitHub, OpenAI, output dir, Postgres — exit 1 if any fail |

Every handler follows the same pattern:

```python
def cmd_xxx(args, settings):
    with connect(settings) as conn:
        run_id = start_run(conn, run_type="xxx")          # 1. orchestrator-style run row
        log = get_logger("reporadar.xxx", run_id)
        try:
            result = call_into_service(conn, settings, run_id, ...)
            finish_run(conn, run_id)                       # 2. mark completed
            # 3. print human-readable summary
            return 0
        except Exception as exc:
            finish_run(conn, run_id, error=str(exc))
            log.error("xxx failed: %s", exc)
            return 1
```

That structure means every CLI command shows up in the dashboard's "recent runs" list with a `run_type` you can filter on (`scan_repos`, `scan_hackathons`, `evaluate`, `manual_submission`, `publish`, `verify_env`, `daemon`, plus `daily_discovery` from the scheduler).

### CLI workflow

```mermaid
stateDiagram-v2
    [*] --> Parsing : main(argv)
    Parsing --> ShowingHelp : --help / no command
    Parsing --> LoadingSettings : valid command
    LoadingSettings --> ConfigError : Settings.from_env raised
    LoadingSettings --> Dispatching : settings ok

    Dispatching --> CandidateIntelligenceCmd : scan-repos / scan-hackathons / evaluate / submit
    Dispatching --> OrchestratorCmd : run
    Dispatching --> PublishingCmd : publish
    Dispatching --> SchedulerCmd : daemon
    Dispatching --> DashboardCmd : serve
    Dispatching --> VerifyCmd : verify-env

    CandidateIntelligenceCmd --> PrintingSummary
    OrchestratorCmd --> PrintingSummary
    PublishingCmd --> PrintingSummary : publish_post_to_linkedin (or --dry-run)
    VerifyCmd --> PrintingSummary
    SchedulerCmd --> [*] : blocks until SIGTERM
    DashboardCmd --> [*] : blocks until Ctrl+C

    PrintingSummary --> Success
    ShowingHelp --> Success
    ConfigError --> Failure : exit 1
    Success --> [*] : exit 0
    Failure --> [*]
```

## Dashboard surface

Single page at `/` rendering six sections, plus a `/media/<filename>` route for serving rendered images and three JSON mutation endpoints called by the inline dashboard JS:

| Section | Source query | Filter |
|---|---|---|
| Posts awaiting review | `queries.get_recent_posts` | `post_instance.status` in (`exported`, `ready_for_review`, `drafted`) |
| Scheduled posts | `queries.get_scheduled_posts` | `post_instance.status='approved'`, sorted by `publication.scheduled_for` ASC |
| Recent evaluations | `queries.get_recent_evaluations` | Always includes `candidate_id` + `has_post` so the dashboard can hide the "Generate" button when a `posted_repositories` row already exists for the canonical repo key |
| Scanned repos today | `queries.get_todays_scans` | source_type=`github_discovery`, today |
| Recent hackathon candidates | `queries.get_recent_hackathons` | source_type=`devpost_discovery` |
| Recent runs | `queries.get_recent_runs` | `pipeline_runs` ordered by `started_at DESC` |

Each post returned by `get_recent_posts` / `get_scheduled_posts` includes `first_path_basename` so the template can `<img src="{{ url_for('media', filename=...) }}">` the rendered poster inline, plus the full caption, hashtags, source links, alt text, image dimensions, and any validation warnings.

The `/media/<path:filename>` route uses Flask's `send_from_directory(settings.output_dir, filename)`, which both serves the JPEG and refuses any `filename` that resolves outside the output dir (defense against path traversal).

### Mutation endpoints

JSON routes called from inline `fetch()` handlers in `dashboard.html`. All return small JSON bodies; the dashboard reloads on success.

**Per-post + per-evaluation actions** (operator review workflow):

| Endpoint | Body | Calls | Effect |
|---|---|---|---|
| `POST /api/posts/<post_id>/approve` | `{"scheduled_for": "<ISO datetime>"}` | `publishing.mark_post_approved` | Sets `post_instance.status='approved'`, writes `review.approved_by/at` and `publication.scheduled_for`. Card moves from "Posts awaiting review" → "Scheduled posts". |
| `POST /api/posts/<post_id>/reject` | `{"reason"?: str}` (optional) | `publishing.mark_post_rejected` | Sets `post_instance.status='rejected'`. The instance stays in the JSONB array (audit) but is filtered out of the review queue. |
| `POST /api/posts/<post_id>/publish-now` | `{}` | `publishing.publish_post_to_linkedin` | Uploads the post immediately via the LinkedIn Posts API (3-step image upload + post create). On success: `post_instance.status='published'`, `publication.external_post_url` + `external_post_id` filled, card moves to "Recently published". On failure: `status='failed'` with `publication.error_message`, card shows a "Retry upload" button. **LinkedIn only today.** Returns 400 on auth/scope/422 errors, 502 on upstream 5xx or wiring errors, 500 on unexpected exceptions. |
| `POST /api/evaluations/<candidate_id>/generate` | `{"channels"?: [str]}` (default `["instagram", "linkedin"]`) | `candidate_intelligence.repository.get_candidate_with_evaluation` → `orchestrator.generate_post_for_existing_candidate` | Skips re-evaluation; force-selects the candidate; runs Content Generation per channel; publishes. New `posted_repositories` row appears in "Posts awaiting review". |

**Pipeline-control actions** (top-of-dashboard "Pipeline controls" section):

| Endpoint | Body | Calls | Effect |
|---|---|---|---|
| `POST /api/scan-repos` | `{}` | `candidate_intelligence.source_adapters.github_discovery.scan_github` | Runs the GitHub Search API queries, UPSERTs candidate rows. Returns `{eligible_count, sample[]}`. Free / fast (5-30s). |
| `POST /api/scan-hackathons` | `{}` | `candidate_intelligence.source_adapters.devpost_discovery.scan_devpost` | Polite scrape of Devpost (1.5s between requests). Returns `{eligible_count, sample[]}`. Free but slow (30-90s). |
| `POST /api/evaluate` | `{}` | `candidate_intelligence.evaluate_pending_candidates` | LLM-scores each pending candidate (capped at `max_evaluations_per_run × 2`). Returns `{evaluated_count, skipped_count, sample[]}`. **Costs LLM tokens.** |
| `POST /api/run` | `{"channels"?: [str]}` | `orchestrator.run_pipeline` | Full pipeline: discover → enrich → evaluate → select → generate → publish. Returns the full orchestrator summary `{run_id, posted_id, channels, image_paths, ...}` or `{status: "no_eligible_candidate"}`. **Most expensive.** |
| `POST /api/submit` | `{"url": str, "channels"?: [str]}` | `orchestrator.submit_url_and_generate` | Same as the CLI `submit` command: skip evaluation, submit URL → enrich → synthesize evaluation → content gen → publish. Returns `{candidate_id, canonical_repo_key, posted_id, channels}`. |

All pipeline-control endpoints wrap their work in `start_run`/`finish_run` so the action appears in the dashboard's "Recent runs" section even if the browser disconnects. The `run_type` distinguishes them: `scan_repos`, `scan_hackathons`, `evaluate`, `daily_discovery`, `manual_submission`.

The `Generate post →` button in the Recent Evaluations cards is hidden when `evaluation.has_post` is true. The `⤴ Upload to LinkedIn now` button is rendered on LinkedIn cards in "Posts awaiting review" and "Scheduled posts". Failed publishes get a `⤴ Retry upload` button in "Recently published".

`app.py` is a Flask factory with 11 routes:

```python
GET  /                                # render dashboard
GET  /media/<filename>                # serve rendered images

# per-post / per-evaluation
POST /api/posts/<post_id>/approve     # mark_post_approved + scheduled_for
POST /api/posts/<post_id>/reject      # mark_post_rejected
POST /api/posts/<post_id>/publish-now # publish_post_to_linkedin (LinkedIn only)
POST /api/evaluations/<cid>/generate  # generate_post_for_existing_candidate

# pipeline controls
POST /api/scan-repos                  # candidate_intelligence.scan_github
POST /api/scan-hackathons             # candidate_intelligence.scan_devpost
POST /api/evaluate                    # candidate_intelligence.evaluate_pending_candidates
POST /api/run                         # orchestrator.run_pipeline
POST /api/submit                      # orchestrator.submit_url_and_generate
```

All SQL lives in `queries.py`. All cross-service calls live in the endpoint handlers — `app.py` directly imports `candidate_intelligence` (scanners + repository), `orchestrator` (pipeline + manual helpers), and `publishing` (mark_post_* + publish_post_to_linkedin).

### Dashboard sections

| Section | Source query | What it shows |
|---|---|---|
| Posts awaiting review | `queries.get_recent_posts` | status in (exported, ready_for_review, drafted) — Approve / Deny / Upload now (linkedin) buttons |
| Scheduled posts | `queries.get_scheduled_posts` | status=approved, sorted by scheduled_for ASC — Upload now (linkedin) button |
| Recently published | `queries.get_recent_published_posts` | status in (published, manually_posted, failed) — clickable external_post_url; failed posts show error + Retry button |
| Recent evaluations | `queries.get_recent_evaluations` | All evaluations + `Generate post →` button when no posted_repositories row exists yet |
| Scanned repos today | `queries.get_todays_scans` | github_discovery, today |
| Recent hackathon candidates | `queries.get_recent_hackathons` | devpost_discovery |
| Recent runs | `queries.get_recent_runs` | pipeline_runs |

```python
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
            runs=queries.get_recent_runs(conn),
            today=date.today().isoformat(),
        )
    finally:
        conn.close()
```

### Read-model isolation

`queries.py` is RepoRadar's denormalized read model. It is the **only** place in the codebase that reads across multiple JSONB sections of multiple service tables in a single query. The dashboard never imports a service's repository module directly.

Why this matters: when (not if) services move to separate processes / databases, the read-model layer is the easiest part to swap. Today it's `psycopg.connect()` against the same DB; tomorrow it could be a materialized view, an event-sourced projection, or per-service HTTP calls behind an aggregator.

### `verify-env` deep-dive

A useful pattern worth calling out: `verify-env` is the only command that exercises every external dependency *without* doing real work.

```
OPENAI_MODEL=gpt-5.4-mini
GitHub OK — 4987/5000 core requests remaining
OpenAI LLM OK — sample: 'OK'
Output dir OK — /Users/.../reporadar/output
Postgres OK

All checks passed.
```

It catches each subsystem's failure independently so you get one report of everything wrong (not just the first failure). Issues are collected in a list; the command exits 1 if any failed.

## Data ownership

Operator API owns **nothing** in the database. It writes only `pipeline_runs` rows for its own CLI command invocations (using the shared `orchestrator.runs.start_run` / `finish_run`).

| Operation | Where it goes |
|---|---|
| Each CLI command's lifecycle | `pipeline_runs` (via orchestrator helpers) |
| All reads | `queries.py` against multiple service tables |

The dashboard reads from `posted_repositories`, `candidate_repository_evaluations`, and `pipeline_runs` — it never reads `api_calls`, the AI Gateway's table.

## Cross-service interactions

```mermaid
flowchart LR
    User[Operator]
    CLI[operator_api/cli.py]
    Web[operator_api/web/app.py]

    User -->|python -m src ...| CLI
    User -->|http://...| Web

    CLI -->|scan-repos / scan-hackathons / evaluate / submit| CI[Candidate Intelligence]
    CLI -->|run| Orch[Orchestrator]
    CLI -->|daemon| Sched[Scheduler]
    CLI -->|verify-env| AI[AI Gateway]
    CLI -->|serve| Web

    Web -->|read-only queries| DB[(Postgres)]
    CI --> DB
    Orch --> DB
```

| From | Calls | Why |
|---|---|---|
| `cli.cmd_scan_repos` | `candidate_intelligence.source_adapters.github_discovery.scan_github` | CLI scan |
| `cli.cmd_scan_hackathons` | `candidate_intelligence.source_adapters.devpost_discovery.scan_devpost` | CLI scan |
| `cli.cmd_evaluate` | `candidate_intelligence.evaluate_pending_candidates` | Catch up evaluations |
| `cli.cmd_submit` | `manual_submission.submit_manual` → `enrich_github_candidate` → `synthesize_evaluation_for_manual` → forced `SelectionDecision` → `content_generation.generate_post_package` (per channel) → `publishing.publish_packages` | Operator URL paste produces a post directly — no LLM evaluation gate |
| `cli.cmd_run` | `orchestrator.run_pipeline` | Full daily pipeline manually |
| `cli.cmd_publish` | `publishing.find_post_by_id` + `publishing.publish_post_to_linkedin` | Push one exported post to LinkedIn via the Posts API |
| `cli.cmd_serve` | starts Flask app from `web.app.create_app` | Dashboard |
| `cli.cmd_daemon` | `scheduler.daemon.run_forever` | Long-running scheduler |
| `cli.cmd_verify_env` | `GithubClient`, OpenAI LLM provider, Postgres `SELECT 1` | Health checks |
| `web.app.dashboard` route | `web.queries.*` | Read-only dashboard rendering |

Nothing calls into Operator API. It is strictly an *initiating* and *displaying* service.

## State of one CLI command

```mermaid
stateDiagram-v2
    [*] --> ParsingArgs
    ParsingArgs --> InvalidArgs : argparse error
    ParsingArgs --> LoadingSettings : valid
    LoadingSettings --> ConfigError : missing env var / bad OpenAI key
    LoadingSettings --> OpeningConn
    OpeningConn --> StartingRun : INSERT pipeline_runs
    StartingRun --> CallingService
    CallingService --> FinishingClean : success
    CallingService --> FinishingError : exception
    FinishingClean --> PrintingResult
    PrintingResult --> [*] : exit 0
    FinishingError --> PrintingError
    PrintingError --> [*] : exit 1
    InvalidArgs --> [*] : exit 2 (argparse)
    ConfigError --> [*] : exit 1
```

## Configuration knobs

The Operator API doesn't have its own settings beyond what every service uses (`Settings.from_env()`). The dashboard's host/port/debug are CLI args:

```bash
python -m src serve --host 0.0.0.0 --port 8080 --debug
```

## Failure modes

| Symptom | Cause | Effect |
|---|---|---|
| `Configuration error: ...` to stderr | `Settings.from_env` raised (missing env var or missing API key) | Exit 1 before any DB activity |
| CLI command raises mid-flight | Downstream service failure | `finish_run(error=...)` and exit 1; the dashboard shows the failed run with the error message |
| Dashboard query fails | Postgres unreachable, schema not applied | 500 error; visible in the Flask log; one bad section does not affect others (each is queried independently) |
| `serve` port in use | Another process on the port | Flask raises `OSError`; exit immediately |

## Future: approve / reject / regenerate

v2 §9 lays out the post-MVP workflow:

```http
POST /posts/{post_id}/approve
POST /posts/{post_id}/reject
POST /posts/{post_id}/regenerate
POST /posts/{post_id}/mark-posted
POST /projects/submit
POST /runs
```

These slot in cleanly:

- **`approve` / `reject`** → update `posted_repositories.post_instances[].status` (already supported by `publishing.repository.mark_manually_posted`'s pattern).
- **`regenerate`** → call `content_generation.generate_post_package` again for a specific channel; replace the matching `post_instance`.
- **`mark-posted`** → exactly `publishing.repository.mark_manually_posted`.
- **`projects/submit`** → exactly `manual_submission.submit_manual` (already CLI-exposed as `submit`).
- **`runs`** → exactly `orchestrator.run_pipeline` (already CLI-exposed as `run`).

Adding them means writing a few Flask route handlers in `web/app.py` (or a new `web/routes.py`). No other service changes.

## Out of scope today

- **Authentication.** Dashboard is open. Fine for local-only use; needs auth before any public exposure.
- **WebSockets / live updates.** The dashboard is request-response; refresh to see new data.
- **Per-user audit trail.** Every CLI run is `requested_by="manual"` or `"operator"` — no per-user identity.
- **Operator UI for approve/reject.** Listed above as the next likely addition; today's dashboard is read-only.
- **Pagination on dashboard tables.** Each section is hard-capped at 8–25 rows.
- **CSV / JSON export from the dashboard.** Sidecar JSONs in `output/` exist per-post; dashboard-level export does not.
