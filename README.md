# Harmonia

A self-hosted music companion for Plex, with Spotify and Apple Music playlist syncing, music discovery, and optional Lidarr album requests.

Bring your playlists into your own music library. Harmonia matches source tracks to music available in Plex, helps you review missing or ambiguous matches, and keeps your Plex playlists up to date.

[Releases](https://github.com/rstuke82/harmonia/releases) · [Report an issue](https://github.com/rstuke82/harmonia/issues)

## Features

- **Playlist syncing:** Import public Spotify and Apple Music playlists, or create playlists from text. Sync manually or on a schedule.
- **Match review:** Compare Plex candidates, including track durations, and save manual selections. Review missing tracks, manage artist aliases, and ignore tracks or block artists.
- **Discovery:** Browse recently added albums, recent requests, and optional Last.fm recommendations and listening history.
- **Album requests:** Find releases using MusicBrainz and request albums through an optional Lidarr integration.
- **Library review:** Browse saved library inventory, review availability and associations, and investigate library issues.
- **Multiple users:** Plex sign-in, personal playlists, administrator-managed permissions, and album request approval.
- **Background work:** Scheduled tasks, visible job progress, activity logs, and persistent job history. Interrupted bulk metadata refreshes resume after restart, retaining completed artists and retrying unfinished or failed artists.
- **Responsive interface:** Use Harmonia on desktop or mobile, with an installable web app.

Harmonia syncs playlists to music you can access in Plex. It does not download audio from Spotify or Apple Music. Optional Lidarr requests use your existing Lidarr download and import setup.

## Requirements

- Docker with the Docker Compose plugin.
- A Plex Media Server with a music library.
- A Plex account that owns the server for initial administrator setup.
- Network access from Harmonia to Plex and the services you enable.

Optional integrations:

| Integration | Purpose |
| --- | --- |
| Lidarr | Album requests, download/import status, and additional library and track identity information |
| Last.fm | Recommendations and listening-history features |
| Local music mount | File scanning and duplicate diagnostics |

Lidarr and local filesystem access are not required for basic Plex playlist syncing. MusicBrainz supplies release metadata; an optional metadata mirror can be configured in Settings.

## Docker installation

Create a folder for Harmonia and download the Compose template:

```bash
mkdir -p harmonia
cd harmonia
curl -fL https://raw.githubusercontent.com/rstuke82/harmonia/main/compose.yaml.example \
  -o compose.yaml
```

The repository stores the template as `compose.yaml.example`; the command saves it locally as `compose.yaml`, which Docker Compose reads automatically. Use this in a new installation folder: downloading again replaces an existing `compose.yaml`.

Review `compose.yaml` before starting. Set your timezone and optionally enable the read-only music mount. The template contains:

```yaml
services:
  harmonia:
    container_name: harmonia
    # Image tags: latest (stable), beta, or a published version.
    image: ghcr.io/rstuke82/harmonia:latest
    restart: unless-stopped
    ports:
      - "8173:8173"
    environment:
      HARMONIA_PORT: "8173"
      HARMONIA_DATA_DIR: /config
      HARMONIA_IMAGE: ghcr.io/rstuke82/harmonia:latest
      # Used for update notifications; does not automatically install updates.
      # Options: stable (image :latest) or beta (image :beta).
      HARMONIA_UPDATE_CHANNEL: stable
      # Change this to your timezone, for example America/Chicago.
      TZ: Etc/UTC
    volumes:
      - ./config:/config
      # Optional: local library scanning (read-only).
      # Replace the example host path with your music library location.
      # - /path/to/music:/music:ro
    stop_grace_period: 30s
```

The container prepares its dedicated `/config` storage at startup, then runs Harmonia as UID/GID `10001`. It does not change the music library.

Start Harmonia from the folder containing `compose.yaml`:

```bash
docker compose up -d
```

Open `http://SERVER-IP:8173`, or `http://localhost:8173` if Docker is running on your computer.

The example publishes port 8173 on the host's available interfaces. Use it on a trusted network. For public access, configure HTTPS and restrict access to the application port as described below.

## First-run setup

1. Open Harmonia in your browser.
2. Retrieve the one-time setup key from the container's startup logs:

   ```bash
   docker compose logs harmonia
   ```

   If needed, retrieve the key directly before setup is complete:

   ```bash
   docker compose exec harmonia cat /config/setup-key
   ```

3. Enter the setup key and sign in with the Plex account that owns your server.
4. Complete the Plex server and music-library selection. This establishes the initial administrator.
5. Allow the initial Plex inventory scan to finish. Follow its progress in Jobs.
6. Configure any optional services in Settings and review task schedules.

Keep the setup key private. It is consumed after administrator setup and is not required for normal sign-in.

Plex and Lidarr addresses must be reachable from inside the container. `localhost` in a service URL refers to the Harmonia container, not your Docker host or another container.

Administrators manage additional users and their permissions through Users. New members require approval for album requests by default.

## Using Harmonia

### Playlists

Open Playlists and add a public Spotify or Apple Music playlist URL, or choose Create from Text. Review the destination name and sync settings, then follow the background job in Jobs.

Harmonia matches source tracks to available Plex music. Missing or ambiguous tracks remain available for review. Choose a candidate to queue a manual match, then apply your match changes and sync. Tracks that cannot be matched are not replaced with arbitrary music.

Each playlist can follow the server schedule, use a custom schedule, or sync manually. Review ignored tracks and blocked artists in Blocklist.

### Discovery and library

Discover brings together recently added Plex albums, recent requests, and enabled recommendation features. Library provides album and track details from saved inventory.

Saved inventory keeps normal browsing from requiring a full live scan. After adding or changing music, check the inventory timestamp and refresh the relevant service scan in Settings → Jobs & Cache if needed.

Issues provides diagnostic findings for review. An association problem, partial import, or duplicate candidate is evidence to investigate rather than permission to remove music.

### Album requests

Configure Lidarr in Settings with its URL, API key, and request defaults. Test the connection and select the appropriate root folder and profiles.

Choose Request Album, review the release results, and select the album you want. Administrator approval may be required before an album is added or searched. Track progress in Requests and follow related work in Jobs.

**Imported into Lidarr and available in Plex are separate stages.** Harmonia shows Waiting for Plex while an imported album is awaiting Plex confirmation, Partially available when only part is available, and Available when full availability is confirmed. Needs attention indicates a failure or condition requiring review.

For timely import updates, configure Lidarr's webhook using the endpoint and authentication details provided by Harmonia. In Account → API Access, enable “Receive Lidarr webhooks” for the administrator key used by Lidarr. The endpoint must be reachable from Lidarr. Inventory scans and polling also contribute to saved availability; Plex still needs to discover imported files.

## Configuration and storage

Configure Plex, Lidarr, Last.fm, matching preferences, users, and scheduled tasks through the web interface. Update notifications can be disabled in Settings → About. Connection credentials belong in Harmonia Settings, not in a public Compose file.

| Setting | Purpose |
| --- | --- |
| `HARMONIA_PORT` | Port used by the application inside the container |
| `HARMONIA_DATA_DIR` | Persistent application storage; `/config` in the supplied template |
| `HARMONIA_UPDATE_CHANNEL` | Update notification channel: `stable` or `beta` |
| `TZ` | Timezone for the container and scheduled work |

The template's `HARMONIA_IMAGE` value records the selected image reference. Changing it alone does not change Compose's `image:` entry; keep the two references consistent.

In `./config:/config`, `./config` is the folder on your host and `/config` is its location inside Harmonia. This folder contains the database, settings, saved state, and other persistent application files. Keep it when updating or recreating the container.

### Optional local file scanning

Uncomment the example music volume and replace `/path/to/music` with your library's host path. Keep `:ro` to give Harmonia read-only access.

Configure the corresponding path mapping in Harmonia's library-management settings. A path reported by Lidarr or Plex may differ from the path visible inside Harmonia; for example, the same files may appear under `/data/media/music` in one service and `/music` in another.

Local scans and duplicate verification provide diagnostic evidence. They do not delete, move, or retag music files. Start scans from Settings → Jobs & Cache and review the results in Issues.

## Updates and backups

Before updating, create a backup in Settings → Backups and download it to a private location. Backups contain service credentials and should not be committed to Git or shared with bug reports. Harmonia backups do not include your music files or Plex's own database.

Update the selected Docker image and recreate the container:

```bash
docker compose pull
docker compose up -d
docker compose logs --tail=100 harmonia
```

Update notifications do not install updates automatically. Review [release notes](https://github.com/rstuke82/harmonia/releases) for changes requiring configuration updates.

For a complete filesystem backup, stop Harmonia, copy the entire host configuration folder to a private backup location, and start Harmonia again. Restoring app state through Settings → Backups replaces Harmonia state without rolling back Plex playlists or music files.

## Public access

Put Harmonia behind an HTTPS reverse proxy before exposing it to the internet. Restrict the application port so it can only be reached through the intended proxy.

For a proxy on the same host, use a loopback port mapping:

```yaml
ports:
  - "127.0.0.1:8173:8173"
```

Configure `HARMONIA_HTTPS_ONLY=true` and set `HARMONIA_TRUSTED_PROXIES` to the actual addresses of your trusted proxy. A proxy in another container or machine needs an appropriately reachable network path. Do not trust all client addresses.

See [deployment and security guidance](SECURITY-DEPLOYMENT.md) for forwarding headers, proxy networking, and verification. Protect configuration files, credentials, and backups as carefully as the application itself.

## API access

Personal API keys start read-only. Enable playlist or album-request permissions in Account → API Access when needed for automation. Only administrators can enable Lidarr webhook access. Existing keys without saved permissions become read-only; enable the required permissions before using those integrations. Changing permissions keeps the same key. Keys never expire; regenerating a key immediately disables its previous value. Administrator settings and key management require a browser session.

## Troubleshooting

| Problem | What to check |
| --- | --- |
| Container cannot write configuration | Confirm the host configuration folder is writable by UID/GID `10001`. Change ownership only on Harmonia's configuration folder. |
| Plex or Lidarr connection fails | Test from Settings. Confirm the URL is reachable from the container and includes any configured URL base. |
| Library is empty after setup | Check the initial scan in Jobs and retry failed scans from Settings → Jobs & Cache. |
| Imported album is waiting for Plex | Confirm Plex has scanned the files and that the selected library and path mappings are correct. Review the related job and refresh Plex inventory if needed. |
| Playlist track remains missing | Review its candidates, duration, source metadata, and availability in the signed-in user's Plex library. |
| Local scanning cannot see music | Check the read-only mount and the path mapping visible inside Harmonia. |

Use Jobs and Activity for application progress and errors. Container logs are available with:

```bash
docker compose logs --tail=200 harmonia
```

When [reporting an issue](https://github.com/rstuke82/harmonia/issues), include the Harmonia version and build, installation method, steps to reproduce, expected and actual behavior, and relevant sanitized logs. Remove tokens, API keys, setup keys, and personal information before sharing.

## License

Harmonia is distributed under the [GNU General Public License v2.0](LICENSE). Retain the included license and copyright notices when redistributing the source or modified versions.

Plex, Spotify, Apple Music, Lidarr, Last.fm, and MusicBrainz are independent services. Harmonia is not affiliated with or endorsed by them.
