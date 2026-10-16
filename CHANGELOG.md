# Changelog

## 1.0.0.107 · Beta — 2026-10-16

- Clear automatically followed terminal activity after 30 seconds; keep deliberately opened job-history details available.
- Show the finish date/time on completed activity headers and stop pinning collapsed historical panels.
- Acknowledge terminal jobs when dismissed, including collapsed details.

## 1.0.0.106 · Beta — 2026-10-16

- Reduce matching CPU during health checks and automatic retries with cached text preparation and a conservative native title prefilter; retain full fuzzy fallback and recording safeguards.
- Rank review candidates using optimistic score bounds, preserving exhaustive ordering, duplicate groups and tie handling while avoiding unnecessary full scoring.
- Log inventory, ranking, Library preparation phases, thread CPU and slow SQLite statements/commits without SQL values or additional database writes.

## 1.0.0.105 · Beta — 2026-10-16

- Use a separately cached Library linking index for album linking and listening-history availability, without preparing catalog-only releases, Issues or artist profiles.
- Reuse the same album identity, selected artwork, availability and track rules as Library; serve existing-album covers without forcing a full Library rebuild.
- Keep linking caches scoped to account, permissions and credentials; invalidate for relevant album, artwork, track and inventory changes.
- Attribute MusicBrainz public fallback failures correctly and retry transient public failures once within the public request limiter.

## 1.0.0.104 · Beta — 2026-10-16

- Describe unreviewed historical job failures, interruptions and partial completions explicitly in navigation.
- Open the relevant job directly and mark only successfully loaded, expanded job details reviewed; keep review state account-scoped.
- Keep active jobs first, queued-only badge counts, and background preparation failures in Activity.

## 1.0.0.103 · Beta — 2026-10-16

- Add job runtime budgets and visible stopping status while preserving safe Plex writes and recovery.
- Separate source observations, match state, matching and sync workflow phases; retain the public Syncer interface.
- Require clean lint and formatting across all Python sources, scripts and tests.
- Route runtime messages through redacted logging with shared job and diagnostic severity.
- Replace unmaintained matching dependencies with RapidFuzz while preserving integer score behavior.
- Authenticate every pinned Python runtime artifact with SHA-256 hashes and replace a withdrawn Soup Sieve pin.
- Expand provider and sync tests, enforce coverage minimums in CI, and name tests by behavior.
- Consolidate the stylesheet cascade, remove duplicate icons, and document the compose template’s LAN port exposure.

## 1.0.0.102 · Beta — 2026-10-16

- Move page-read and refresh feedback into an expandable Background updates section in Activity; remove page-shifting banners and routine settings indicators.
- Poll compact job results and history without decoding full health/source previews; retain full job details and immediate per-playlist completion signals.
- Keep authentication database reads off the event loop, skip disabled debug polling logs, and read cancellation flags without loading full results.
- Reuse account-scoped saved matching links, invalidating them when inventory, path mappings or manual links change.
- Find the same strong song identities before fully scoring unrelated titles; preserve fuzzy fallback, recording/version guards and duration checks.
- Make automatic suggestions usable while alternate candidates continue loading.

## 1.0.0.101 · Beta — 2026-10-16

- Keep the full Library model cached across unrelated playlist and account updates, while preserving account, permission, credential, artwork and inventory invalidation.
- Load block rules once and index album track associations during Discover/listening-history preparation; avoid building an empty discovery feed.
- Keep inventory loading locks scoped to each account and prevent invalidated cache work from being republished.
- Show page loading and background refresh progress with elapsed time and queued preparation status; retain usable saved data and offer retry when refresh fails.
- Verify loading and refresh behavior at phone, desktop and ultrawide sizes under the production security policy.

## 1.0.0.100 · Beta — 2026-10-15

- Tolerate regular, unlinked SQLite WAL/SHM files after bounded rechecks; retain strict main database, symlink, hard-link and nonregular-file checks.

## 1.0.0.99 · Beta — 2026-10-15

- Recheck transient unlinked SQLite WAL/SHM files before rejecting a database connection, while retaining symlink and hard-link protections.
- Include the rejected database file and its attributes in safety-check errors.

## 1.0.0.98 · Beta — 2026-10-13

- Route service calls through one network layer with separate connection and read timeout limits, including MusicBrainz sessions and protected artwork downloads.
- Keep stream handling with its caller, redact request credentials from error logs, and prevent automatic redirects on authenticated calls.
- Report stalled response reads as timeouts and retain single-attempt playlist writes.

## 1.0.0.97 · Beta — 2026-10-13

- Keep Missing track details and actions correctly aligned when administrator selection controls are absent.
- Stack actions below track details on mobile and align the user selector with the toolbar.

## 1.0.0.96 · Beta — 2026-10-13

- Added read-only permission to view all users’ requests.
- Administrators can filter Missing tracks by user and manually match for their accounts.
- Removed the redundant More section from Account.

## 1.0.0.95 · Beta — 2026-10-13

- Preserve X in artist names when parsing Apple Music credits; recognize saved credits affected by the earlier parser.
- Include structured artist credits when evaluating automatic suggestions and identify the exact track and playlist when queued automatic validation fails.

## 1.0.0.94 · Beta — 2026-10-13

- Block inline stylesheet injection and external HTTP images while preserving dynamic themes, artwork, menus and modals. Add a headless browser CSP check to CI.
- Avoid repeated credential-file scans and unnecessary permission changes on database connections; retain link checks and expand the bounded repository cache.
- Expand sync outcome tests for duplicate warnings, missing tracks, write/readback failures and dry runs.
- Remove unused imports and extend formatting checks to storage, browser security and sync support modules.

## 1.0.0.93 · Beta — 2026-10-13

- Validate and pin notification destinations, disable webhook redirects, and keep delivery errors credential-safe. Local notification servers require explicit access.
- Avoid the API-key write lock when its usage timestamp is current; recheck account status and key validity before updating a due timestamp.
- Apply a default HTTP timeout and centralize configured-secret redaction.
- Allow update checks to be disabled in About and keep scheduled checks inactive while disabled.
- Add incremental lint and formatting checks to CI, source-fetching and sync safety tests, and frontend-independent routing tests.
- Correct Apple Music's fallback parser for unfamiliar page layouts.

## 1.0.0.92 · Beta — 2026-10-12

Initial beta release of Harmonia, with Plex playlist syncing, manual match review, library browsing, music discovery, album requests, and multiple-user support.
