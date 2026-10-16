# To do

- [x] **M2 — Refactor the sync engine.** Split saved match state, source observations, matching and sync workflow into focused modules. Separate identity repair, dry-run reporting, verification and output phases; retain the Syncer API, destination locks and recovery. Implemented in build 103.

- [x] **Remaining audit code changes.** Job budgets, strict quality gates, runtime logging, provider/sync coverage, descriptive tests, asset cleanup, RapidFuzz and authenticated runtime pins are implemented in build 103. See AUDIT.md for validation and operational limits.

- [x] **M3/M4 — Network consistency and timeouts.** Route application HTTP calls through the shared transport, enforce finite connection/read bounds, and preserve authenticated redirect, artwork, and playlist-write protections. Implemented in build 98.

- [x] **Page performance and visible loading.** Reduce repeated Library rebuilding and history lookup work; show nonblocking loading, background refresh, preparation progress and retry feedback. Implemented in build 101.

- [x] **Activity-only refresh feedback and responsive reads.** Remove page-shifting banners, compact job polling/history, reuse saved matching links and avoid unrelated scoring on strong identity matches. Implemented in build 102.


- [x] **Sidebar failure wording.** Describe previous failures, interruptions and partial completion explicitly; link directly to the relevant job and acknowledge only loaded, expanded details. Implemented in build 104.
- [ ] **Deployment validation.** Confirm current foreground latency and live Plex playlist behavior on the deployment; scan the exact new runtime image after Docker authentication or source push.
