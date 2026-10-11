# Changelog

## 1.0.0.76

- Replace the Debian runtime with digest-pinned Python 3.12 on Alpine 3.23 and require patched zlib 1.3.2-r1 or newer.
- Keep locked Python dependencies on binary wheels, retain timezone support and UID/GID 10001 startup protection.
- Verify patched zlib, native artwork codecs, SQLite, matching, validation, timezones and TLS trust in both architecture CI jobs.
- Runtime-only update; queued application changes are excluded.

## 1.0.0.75

- Choose the Plex music library during first-time setup before the administrator account and initial scan are created.
- Use Request Album labels consistently for all accounts.
- Restore Activity to mobile bottom navigation and use compact chevron collapse controls.
- Use temporary popup confirmations for artwork updates and user-initiated jobs, including direct request helpers.
- Add API-key copy confirmation and a link to create a Last.fm API key.
- Add library-selection security and browser-flow regressions, and shared activity-confirmation tests.

## 1.0.0.74

Initial Harmonia repository release, with four-part version numbering, /config storage and automatic configuration permission handling.
