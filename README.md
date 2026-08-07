# Upload Manager — umbrelOS Community App Store

Clean, ready-to-install Community App Store for [umbrelOS](https://umbrel.com).
The app watches the shared `/downloads` folder used by the whole *arr family
(Radarr, Sonarr, Readarr, Lidarr, Whisparr, ...) and uploads completed movies,
shows, books and music to your cloud file hosts (EarnVids-style, WebDAV, S3,
SFTP, local) in parallel — with a web dashboard and Telegram notifications.

## Repository layout (Umbrel-standard)

```
umbrel-upload-manager-store/
├── umbrel-app-store.yml            # Store definition: id + name ONLY (no array)
├── uploadmgr-upload-manager/       # App folder — name MUST equal the app id
│   ├── umbrel-app.yml              # App manifest (Umbrel reads this, not .json)
│   └── docker-compose.yml          # Compose with app_proxy + storage mounts
├── .github/workflows/build-upload-manager.yml
├── Dockerfile
├── requirements.txt
└── app/                            # App source (used to build the image)
```

## Notes (why this repo will NOT throw `map undefined`)

- `umbrel-app-store.yml` follows the official format — only `id` and `name`.
  umbrelOS discovers every subdirectory automatically; there is **no** array to add.
- Every subdirectory of the repo root is treated as an app, so this repo has exactly
  one app folder (`uploadmgr-upload-manager`) containing a **valid** `umbrel-app.yml`.
- The app folder name and `umbrel-app.yml` `id` both equal `uploadmgr-upload-manager`,
  and the id starts with the store id (`uploadmgr`).
- Umbrel uses a **YAML** manifest (`umbrel-app.yml`). There is no `umbrel-app.json`.

## Install steps

1. Replace `REPLACE_WITH_YOUR_GITHUB_USERNAME` in:
   - `uploadmgr-upload-manager/umbrel-app.yml`
   - `uploadmgr-upload-manager/docker-compose.yml`
2. Push this repository to GitHub.
3. Build & publish the image (once):
   - GitHub → Actions → **Build and publish Docker image** → **Run workflow**
   - This pushes `ghcr.io/<your-username>/upload-manager:latest` (amd64 + arm64).
4. On your umbrelOS device:
   - App Store → top-right `⋮` → **Community App Stores** → paste the repo URL → **Add**.
   - Open the store → **Upload Manager** → **Install**.
5. In the app dashboard:
   - Add providers (file hosts / WebDAV / S3 / SFTP / local) → Test.
   - Add watch paths (e.g. `/downloads/movies`, `/downloads/tv`).
   - Optionally set Telegram token/chat id, then press **Test Telegram**.

## CasaOS?

umbrelOS and CasaOS are different platforms with different, incompatible store
formats (Umbrel = `umbrel-app.yml` + `app_proxy`; CasaOS = its own manifest +
`casaos.*` labels). They cannot share one manifest file. This repo targets
**umbrelOS**. For CasaOS, use a separate CasaOS package.
