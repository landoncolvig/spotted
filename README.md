# Spotted

Spot people in your video footage. Local face tagging for editors.

Drop a folder of `.mov` / `.mp4` files into Spotted. It reads frames, detects faces, groups them into people, lets you name each person once, and writes XMP keywords back into each clip's metadata. After that, Premiere and DaVinci can search your library by name.

No cloud. No subscription. Runs entirely on your Mac.

## Install

Download the latest `Spotted.dmg` from [Releases](https://github.com/landoncolvig/spotted/releases/latest), open it, drag Spotted to Applications.

The app is unsigned for now — first launch, right-click → Open, then "Open Anyway" in System Settings → Privacy & Security.

Spotted checks for updates on launch and applies them automatically.

## How it works

1. **Scan** — samples one frame per second, detects faces with InsightFace.
2. **Cluster** — HDBSCAN groups similar faces into per-person clusters.
3. **Label** — one screen with all clusters. Type names. Save.
4. **Tag** — writes XMP `Subject` (Keywords) into each `.mov`. Premiere and DaVinci read this natively.

## Repository layout

```
.
├── facetag/              # Python CLI library (the engine)
├── app/                  # Tauri desktop shell (the product)
│   ├── src/              # Vanilla TS + HTML frontend
│   └── src-tauri/        # Rust app + bundler config
└── .github/workflows/    # CI: tag-driven release builds
```

## For power users

The underlying CLI is installable separately:

```bash
brew install ffmpeg python@3.13
git clone https://github.com/landoncolvig/spotted
cd spotted
python3.13 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/facetag scan ~/Movies
```

See [facetag/](facetag/) for the full CLI.

## Releasing (maintainer notes)

```bash
# Bump version in app/package.json, app/src-tauri/tauri.conf.json, app/src-tauri/Cargo.toml
git tag v0.0.2
git push --tags
# GitHub Actions builds + publishes; users auto-update on next launch.
```
