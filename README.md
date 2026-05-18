# facetag

Face-tag a local video library and cut temporal highlight reels per person. Runs entirely on your Mac, no cloud calls, no per-minute fees.

## How it works

1. **Scan**: walks a folder, samples frames at 1 fps, detects faces with InsightFace (RetinaFace + ArcFace), stores 512-d embeddings + bounding boxes in SQLite.
2. **Cluster**: HDBSCAN groups embeddings into per-person clusters.
3. **Label**: opens a 3x3 grid of representative faces per cluster, you name them once.
4. **Search / Cut**: query "videos with Sarah", or render a highlight reel concatenating every clip where Sarah appears.

## Setup

```bash
brew install ffmpeg python@3.13
cd ~/Documents/face-tagger
python3.13 -m venv .venv
.venv/bin/pip install -e .
```

The first scan downloads the InsightFace `buffalo_l` model (~280 MB) into `~/.insightface/`. After that everything is local.

Activate the venv or call the CLI directly:

```bash
source ~/Documents/face-tagger/.venv/bin/activate
# or
~/Documents/face-tagger/.venv/bin/facetag <command>
```

## Workflow

```bash
# 1. Index your library (point at any folder; recursive).
facetag scan ~/Movies

# 2. Cluster faces into people.
facetag cluster

# 3. Label each cluster.
#    Easiest: web UI — one page with all clusters, type names, hit Save All.
facetag label-web

#    Or terminal-only: opens a face grid in Preview, prompts for each cluster.
facetag label

# 4. Find videos with someone.
facetag search "Sarah"

# 5. Build a highlight reel of that person across the whole library.
facetag cut "Sarah" -o ~/Desktop/sarah_reel.mp4

# Or limit to specific videos.
facetag cut "Sarah" --video ~/Movies/birthday.mov --video ~/Movies/wedding.mov -o sarah.mp4
```

## Performance on Apple Silicon

InsightFace runs with the CoreML execution provider on M-series Macs. Expect ~1-3 minutes of processing per hour of video at default 1 fps sampling. CPU-only Macs are 5-10x slower but still works. Re-running `scan` skips already-indexed videos (use `--rescan` to redo).

## Tuning

- `facetag scan ... --fps 0.5` halves the sample rate (faster, but may miss brief appearances).
- `facetag cluster --eps 0.45` is stricter (fewer, purer clusters); `--eps 0.65` is more lenient.
- `facetag cut ... --gap 3 --pad 1.5` merges detections up to 3s apart and pads each clip by 1.5s on both sides.

## Where things live

- Index DB: `~/.facetag/index.db`
- Label thumbnails: `~/.facetag/label_thumbs/`
- Model cache: `~/.insightface/`

## Limitations

- **Frame sampling** at 1 fps means appearances under ~1 second can be missed. Increase `--fps` if needed.
- **HDBSCAN** sometimes splits one person across multiple clusters (e.g. very different lighting). You can name both clusters the same name; they'll merge in search and cut.
- **No spatial cropping** in this version; clips keep the original frame. Spatial reframing (crop to follow the subject) is a separate feature.
- **No face grouping by hugging/crowd shots** — every face in frame is detected independently.
