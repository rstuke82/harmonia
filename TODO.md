# To do

- [ ] **M2 — Refactor the sync engine.** Split `harmonia/sync_engine.py` into smaller modules with clear responsibilities for source loading, matching, Plex playlist updates, verification, and saved state. Preserve existing matching rules, account isolation, duplicate handling, and playlist recovery. Add regression coverage before moving behavior, and migrate incrementally. Queued for future work; no build assigned.

- [x] **M3/M4 — Network consistency and timeouts.** Route application HTTP calls through the shared transport, enforce finite connection/read bounds, and preserve authenticated redirect, artwork, and playlist-write protections. Implemented in build 98.

- [x] **Page performance and visible loading.** Reduce repeated Library rebuilding and history lookup work; show nonblocking loading, background refresh, preparation progress and retry feedback. Implemented in build 101.
