# HTTPS deployment and browser security

Harmonia supports local HTTP for private development/LAN use. A public deployment must terminate TLS at a reverse proxy and block direct access to the application port.

## Public deployment settings

Set these in the Compose environment settings, then recreate the application container when deploying the next release:

```dotenv
HARMONIA_HTTPS_ONLY=true
# Replace with the exact source address your proxy uses to reach Harmonia.
HARMONIA_TRUSTED_PROXIES=10.0.0.2

```

For a same-host proxy, bind the published port to loopback. A proxy running on the same host can use the loopback port. For a proxy in a separate Docker container or machine, choose a reachable interface/address and configure Docker network isolation/firewall rules so only that proxy can connect. Trust the actual connecting proxy address, not a browser/client address. Docker NAT can change the address seen by the application. Do not trust arbitrary clients or broad untrusted networks. Wildcard and all-address CIDR proxy trust are rejected.

HTTPS-only mode rejects requests that the application sees as HTTP and always marks login/session cookies Secure. Forwarded scheme/client headers are consumed only from configured trusted addresses; this preserves real client addresses for login rate limiting. Leaving HTTPS-only false retains local HTTP compatibility. Changing the proxy trust environment does not itself configure the proxy or firewall.

For a single Nginx proxy, overwrite client-supplied forwarding headers:

```nginx
location / {
    proxy_pass http://127.0.0.1:8173;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Real-IP $remote_addr;
}
```

Configure the TLS listener/certificate separately. If there are multiple proxy hops or a CDN, configure that proxy's real-IP trust before deriving client addresses; never blindly trust incoming forwarding headers. Health probes in HTTPS-only mode must use the HTTPS endpoint or a correctly trusted proxy connection.

## Browser response policy

The application supplies an enforced CSP with same-origin scripts/connections, no plugins or framing, and Apple Music preview framing explicitly allowed. Inline styles remain allowed for React component styling. External image/audio origins remain allowed for existing artwork and previews; this is not a strict media-origin allowlist.

Responses have nosniff, DENY framing, referrer and camera/microphone/geolocation restrictions. HTTPS responses have one-year HSTS for this host, without includeSubDomains or preload. HTTP responses do not establish HSTS. API responses (including authentication errors and backup downloads) and responses setting cookies are no-store. Existing hashed static asset caching remains intact. Public Swagger/ReDoc/OpenAPI routes are disabled.

## Verification after deployment

Inspect the public HTTPS response and an unauthenticated API response:

```bash
curl -sS -D - -o /dev/null https://harmonia.example.com/
curl -sS -D - -o /dev/null https://harmonia.example.com/api/account
```

Confirm Content-Security-Policy, X-Frame-Options: DENY, X-Content-Type-Options: nosniff and Strict-Transport-Security; API responses should have Cache-Control: no-store. During normal sign-in, inspect cookie response headers and confirm Secure and HttpOnly. Confirm the direct application port is inaccessible from an external network, trusted forwarding identifies separate client IPs, and forged forwarded headers from an untrusted connection cannot establish HTTPS or change the rate-limit identity. Check Apple Music previews and normal navigation on the deployed browser/device.

Local regression tests verify the application controls. Actual TLS certificates, proxy addresses, forwarding rules, firewall reachability and browser behavior remain deployment checks until tested on the real host.

## Container startup and configuration permissions

The container briefly starts as root to prepare its dedicated configuration storage, default `/config`. It creates missing directories, repairs ownership to UID/GID 10001 and restricts directory/file modes to 0700/0600. It checks the tree on each startup but changes ownership or modes only when necessary. It then clears supplementary groups, switches to UID/GID 10001, prevents privilege escalation and executes Harmonia. The running application has no effective capabilities.

The startup process does not prepare the music library. Mount music read-only. Configuration symlinks, hard-linked files and nested filesystems are rejected to prevent permission repair from following references to unrelated storage. Existing file contents are preserved. Use a dedicated configuration directory, not a shared parent containing other applications or music.

The hardened Compose example retains a read-only root filesystem, dropped capabilities and bounded temporary storage. It grants only CHOWN, DAC_OVERRIDE, FOWNER, SETUID and SETGID for startup repair and privilege dropping. Do not specify `user: 10001:10001` when automatic ownership repair is required. An explicitly non-root deployment remains supported if its configuration storage is already prepared.

No manual mkdir/chown is required for normal local bind mounts. For NFS/root-squashed mounts or filesystems that reject chown/chmod, startup stops with a clear diagnostic. Prepare UID/GID 10001 access on the storage server in those environments. If upgrading an existing installation, keep its host storage folder mounted to `/config` rather than selecting a new empty folder. An explicit `HARMONIA_DATA_DIR=/data` with the existing `/data` mount remains supported.

Docker-exec commands run with the image's default root identity; the normal entrypoint always drops privileges before executing its command. Do not override the entrypoint for normal application startup.

## Redirect policies (R03)

External album/artist artwork must use HTTPS and resolve only to public IP addresses. Each redirect is checked again, service credentials are removed and the connection is pinned to the validated IP while TLS still verifies the original hostname; environment HTTP proxies are disabled for this transport. DNS rebinding cannot substitute a different connection address. External providers now cannot redirect artwork to a LAN/loopback/metadata-service address.

Configured Plex LAN image redirects are restricted to the original scheme/host/port. Other Plex/account/artwork service calls refuse automatic redirects, preventing tokens from crossing origins. An endpoint that requires a redirect must be configured with its final URL. Existing administrator-controlled LAN Plex/Lidarr access remains supported.

## Member defaults and capacity (R05)

New members and imported users require administrator approval for album requests by default. Explicit saved auto-approval choices are retained. Existing accounts without an explicit approval value now use approval-first requests. Members can continue managing their own playlists when permitted. Owners retain their existing administrative behavior.

Each member can have at most 25 requests awaiting approval; resubmitting an existing pending album joins that record. Pending approval checks/writes occur atomically. Each personal job store admits 25 queued/running/cancelling jobs for a member; owner/admin/background stores admit 200. Duplicate active jobs join before capacity checks, and cancelled lookup jobs release admission. Excess submissions return HTTP 429 with retry guidance. These bounds are per store, not a total disk-retention quota.

## Dependency and image maintenance (R06)

Docker installs requirements.lock, resolved from the tested Python 3.12 runtime; requirements.txt includes this lock for native installs. requirements.in retains direct dependency constraints for deliberate updates. Startup validation follows the include rather than silently skipping dependency checks. Runtime and frontend bases are pinned to multiarchitecture digests. The runtime uses a pinned Python 3.12 Alpine 3.23 base, upgrades its OS packages during the build, and requires zlib 1.3.2-r1 or newer. Update containers by rebuilding rather than installing packages inside them.

The included .github/workflows/security.yml builds/tests/scans the actual image on native AMD64 and ARM64 runners. It uses an immutable checkout commit and pinned Docker Scout image. Configure limited Docker Scout credentials in repository secrets if the scanner requires them; missing scanner access is a failed check, not a clean scan. Critical/high advisories fail the check, including unfixed findings. Repository secrets DOCKER_SCOUT_HUB_USER and DOCKER_SCOUT_HUB_PASSWORD are required for the Docker Scout step.

Maintain the lock in an isolated Python 3.12 environment, review changed packages and base/scanner digests, and rerun backend/frontend validation plus both runtime image scans before publication. Pinning improves repeatability; it does not freeze security knowledge or guarantee bit-identical wheel contents without artifact hashes. Review scanner results rather than equating every package finding with a remotely exploitable application defect.
