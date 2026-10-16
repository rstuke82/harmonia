# Audit status — build 1.0.0.108

The code changes for the tracked findings are implemented. A new vulnerability scan of this exact build remains pending: local Docker Scout requires a Docker login. This is not a clean-scan result.

## Completed implementation

| Finding | Resolution and evidence |
| --- | --- |
| B1 | MusicBrainz connection errors use the imported HTTPException; connection tests exercise safe service errors. |
| B2 | Dependencies are untracked and ignored; clean frontend installation/build is tested in CI. |
| B3 | Repository writes preserve unrelated keys; API-key activity updates use targeted SQL without restoring stale account state. |
| B4 | Plex writes retain a durable ordered snapshot, destination lock, verification and restoration path. Duplicate-only omissions are warnings. |
| S1 | API keys have explicit scopes and fail closed on unscoped administrative access. Keys do not expire. |
| S2 | Route access defaults to administrator-only; capability and account-isolation tests exercise member access. |
| S3 | Sign-in has per-source limits without the globally exhaustible starts bucket. |
| S4 | Session/account reads use targeted lookups and API-key hash indexing. |
| S5 | CSP rejects inline stylesheet attributes and external HTTP images; real React components pass production-policy browser checks. |
| S6 | Plex sharing XML uses defusedxml. |
| N1 | API-key activity updates avoid a write transaction when an update is not due. |
| N2 | Notification destinations are validated and DNS-pinned, redirects are refused, and service errors are redacted. Private destinations require explicit opt-in. |
| N3 | Per-lane runtime budgets, visible stopping state, checkpoint deadlines, remaining HTTP budgets and unexpected worker-exit detection are implemented. See execution boundary below. |
| N4 | Configured secrets use shared redaction, including runtime events and upstream diagnostics. |
| N5 | Compose explicitly documents all-interface LAN publishing and the loopback alternative for a local reverse proxy. The default intentionally continues to support LAN access. |
| N6 | Update/notification traffic uses bounded transports; update checks can be disabled. |
| M1 | All Python runtime sources, scripts and tests pass strict Ruff lint and formatting. The lint backlog/baseline exemption is removed. |
| M2 | Syncer remains the public facade. Saved decisions, source observations, matching and workflow phases now have separate modules; destination verification, dry-run reporting, verification/persistence and summaries have explicit boundaries. sync_playlist complexity falls from 63 to 17. |
| M3 | HTTP calls use the named shared network module rather than a misleading requests alias. Specialized pinned transports retain their security boundaries. |
| M4 | Shared HTTP calls have finite connection/read bounds, safe retry rules and credential-aware redirect restrictions. |
| M5 | Routine connections avoid the full secure-files traversal; repository instances use the bounded 128-entry cache. |
| M6 | Revision-trigger behavior has regression coverage. |
| M7 | Runtime services emit levelled, redacted events instead of shadowing print. Job capture is retained, events are not duplicated, and console verbosity is preserved. Launcher version/help and early setup/dependency diagnostics retain intentional terminal output. |
| T1 | Provider parsing, artwork fallbacks, failures and sync safety have expanded coverage. CI enforces overall/provider/sync coverage minimums and reports the measurements. |
| T2 | The backend suite passes without requiring a built frontend artifact. |
| T3 | Tests are named by behavior rather than build/release numbers; shared test imports are updated. |
| H1 | Root documentation describes current installation and operation. |
| H2 | Seven byte-identical PNG copies are removed. One app.css preserves the full stylesheet cascade; build/release-named stylesheets are removed. |
| H3 | The dependency lock documents the current runtime target without a stale build reference. |
| H4 | RapidFuzz replaces fuzzywuzzy and its Levenshtein accelerators. A 900-pair golden corpus preserves integer, punctuation, Unicode, credit and token score behavior. |
| H5 | Every runtime pin has permitted SHA-256 artifact hashes; pip enforces them. Python 3.12 AMD64 and ARM64 Alpine wheels were downloaded and authenticated. The withdrawn Soup Sieve 3.0.1 pin is replaced by 3.0.3. |
| H6 | Strict lint, formatting and coverage checks are required in CI. Both architecture Scout steps passed on the published revision; a fresh scan is required after pushing this build. |

## Validation

Build 108: 992 backend tests pass locally and in the Python 3.12 runtime container, with 74.7% overall coverage; strict lint/formatting, 29 frontend checks and the production frontend build pass. Account retrieval, cross-account isolation, browser-session requirements, legacy hash-only keys, regeneration, revocation and bulk alert acknowledgement have regression coverage.

Build 103 validation baseline:

- Strict lint and formatting pass for the entire Python tree.
- Final backend suite: 968 tests pass; coverage is 74.3% overall, Spotify 96.6%, Apple Music 92.5%, matching 79.1% and sync workflow 67.0%. All 968 tests also pass in the hardened Python 3.12 container.
- Frontend regression scripts and the production build pass.
- Production CSP and Activity-only read feedback pass headless browser checks at phone, desktop and ultrawide sizes.
- Container startup repairs nested configuration ownership, drops privileges, rejects unsafe links, preserves music files and reaches healthy state.
- The runtime verifies zlib 1.3.2-r1, native artwork codecs, RapidFuzz, SQLite, timezone data and TLS trust.
- Existing GitHub Scout success: [AMD64 and ARM64 workflow](https://github.com/rstuke82/harmonia/actions/runs/38305258820), published revision e6f80ee. This does not certify the new image.

## Execution boundary and deployment checks

Job cancellation is cooperative. A destructive Plex replacement retains its lock and recovery snapshot until the current safe unit finishes or recovery is attempted. The scheduler never frees a live worker merely because its deadline elapsed. A blocked native call cannot be forcibly killed safely inside a Python thread. Restarting the container marks unfinished work interrupted, and the next replacement checks durable recovery first. A hard process kill is not a safe substitute for playlist recovery.

The compose template publishes the application for LAN access. Apply the documented loopback binding when using a reverse proxy on the same host; use the deployment guide for internet exposure.

After pushing the source, the new GitHub quality/browser and both architecture runtime scans must pass. Local Scout can be retried after Docker authentication. Live Plex behavior and production latency still require validation on the deployment; local mocks and container tests do not establish those results.

## Deployment observations through build 107

A live health check completed successfully, but foreground latency remains load-sensitive. The supplied deployment log shows a full Library preparation taking 45.07s (8.23s thread CPU), a linking preparation taking 18.43s (1.49s CPU), and job polling dropping from seconds to 0.09s after health completion. Slow SQLite statement wall times alone do not distinguish database waiting from CPU scheduling/GIL contention. Profiling the unsuccessful matching fallback and remaining Library contention is still pending.

Build 108 retains newly generated personal API-key values in private account records for later retrieval by the owning browser session. Hash-based authentication, scopes and revocation remain in place; public account responses omit the value and API-key authentication cannot access key-management routes. Previous hash-only keys require manual regeneration to become retrievable.
