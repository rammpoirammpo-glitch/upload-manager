# Reaparr — Docker setup (server / Umbrel)

Reaparr is a Plex media downloader that also registers itself inside
**Sonarr** and **Radarr** as a Torznab indexer + a qBittorrent download client,
so those apps can search and import media that Reaparr downloads from other
Plex servers.

This folder contains a production-ready `docker-compose.yml` using the official
image from Docker Hub (`reaparr/reaparr`), pinned to v0.39.0.

---

## 1. Prerequisites

- A **Plex account** with access to at least one Plex server. This is how you
  log in — Reaparr has **no default username/password**; you authorize with your
  existing Plex account on first run.
- Docker with Docker Compose (`docker compose` / `docker-compose`).
- The host folders for config and media.

## 2. Create the folders and `.env`

```bash
cd <this folder>
cp .env.example .env          # then edit HOST_PORT, PUID, PGID, TZ
mkdir -p Config Downloads Movies TvShows
```

Set `PUID` / `PGID` to the user that owns those folders (run `id` to check).
The container runs **as that user, not root** — this avoids permission errors
like `UnauthorizedAccessException on /Config/ReaparrSettings.json`.

## 3. Start the container

```bash
docker compose up -d
docker compose ps            # verify it is running
docker compose logs -f       # optional: watch startup logs
```

The web UI is then at **http://<your-server-ip>:<HOST_PORT>** (default **7000**).

## 4. First-run login

Open the web UI. There is **no default username/password** — sign in with your
**Plex account**. You'll then be asked to pick / connect a Plex server.

## 5. Connect Sonarr and Radarr

1. In Reaparr, open **Settings → Integrations**.
2. Add a **Sonarr** and/or **Radarr** instance with their **URL** and **API key**.
3. Reaparr then configures itself inside those apps (as an indexer + a
   qBittorrent-style download client) with a single click.

### Which URL to use (networking)

- **Same machine, different compose project** (most common on a home server):
  use the *published host port*, e.g.

  ```text
  Radarr : http://127.0.0.1:7878      (API key from Radarr Settings->General)
  Sonarr : http://127.0.0.1:8989
  ```

- **Different machines on your LAN**: use the server's LAN IP, e.g.
  `http://192.168.1.50:7878`. Make sure Sonarr/Radarr bind to `0.0.0.0` and the
  port is open.

- **Same compose file / same bridge network**: you can use the service
  hostnames (`radarr`, `sonarr`) on their *internal* ports (7878 / 8989) instead
  of the host-port URLs — only if those services are in the same compose network
  as Reaparr.

## 6. Stop / update

```bash
docker compose down          # stop and remove the container (data is kept)
docker compose pull          # after updating the image version
docker compose up -d         # restart
```

---

## Volumes (do not relocate inside the image)

| Host folder | Container path | Purpose |
|---|---|---|
| `./Config` | `/Config` | configuration, logs, database (put on SSD) |
| `./Downloads` | `/Downloads` | temporary download staging |
| `./Movies` | `/Movies` | default destination for movies |
| `./TvShows` | `/TvShows` | default destination for TV shows |

All four must be writable by `PUID:PGID`.

## Troubleshooting

- **Can't open web UI**: check `docker compose ps`; confirm the firewall allows
  the chosen `HOST_PORT`.
- **Permission errors**: your `PUID`/`PGID` don't match the owner of the mounted
  folders — fix `.env` and `chown` the folders accordingly.
- **API-key / auth in logs**: leave `UNMASKED=false` (default) so secrets stay
  masked.