"""CLI entry point. `facetag --help` to explore."""
from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn
from rich.table import Table

from . import activity as _activity
from . import clip as _clip
from . import cluster as _cluster
from . import cut as _cut
from . import db as _db
from . import detect as _detect
from . import energy as _energy
from . import extract as _extract
from . import finder as _finder
from . import label as _label
from . import markers as _markers
from . import person_thumb as _person_thumb
from . import tag as _tag
from . import web as _web

app = typer.Typer(add_completion=False, help="Face-tag a video library and cut highlight reels.")
console = Console()

DEFAULT_DB = Path.home() / ".facetag" / "index.db"
DEFAULT_LABEL_DIR = Path.home() / ".facetag" / "label_thumbs"


def _emit(event: str, **fields) -> None:
    """Print a structured progress event for the Spotted shell to consume.

    Goes to stdout on its own line, prefixed so the parser can pick it out
    of rich's interleaved progress bars. Best-effort — never raises.
    """
    try:
        payload = json.dumps({"event": event, **fields}, separators=(",", ":"))
        print(f"__SPOTTED__ {payload}", flush=True)
    except Exception:
        pass


def _embed_only(conn, clip_encoder, video_path, video_id: int, sample_fps: float, console) -> int:
    """Compute + store CLIP frame embeddings for an already-face-scanned video.

    Backfill path for libraries built before activity detection shipped: those
    videos have faces but no frame embeddings, so activity-suggest never finds
    anything for them. We re-sample frames and run only the image encoder here
    (no face re-detection, no duplicate faces) so a re-drop of an existing
    folder lights up scene/activity tagging. Returns the number of embeddings
    written. Safe to call only when the video currently has zero embeddings.
    """
    written = 0
    pending: list[tuple[float, bytes]] = []
    for t, frame in _extract.iter_frames(video_path, sample_fps=sample_fps):
        try:
            emb = clip_encoder.encode_image(frame)
            pending.append((t, _clip.embedding_to_bytes(emb)))
        except Exception as e:
            if not getattr(_embed_only, "_warned", False):
                console.print(f"[yellow]Activity encoding failed on a frame: {e}[/yellow]")
                _embed_only._warned = True
        if len(pending) >= 200:
            _db.add_frame_embeddings_bulk(conn, video_id, pending)
            written += len(pending)
            pending.clear()
    if pending:
        _db.add_frame_embeddings_bulk(conn, video_id, pending)
        written += len(pending)
    return written


def _clip_thumb_datauri(path: str, cache: dict, *, width: int = 128) -> str | None:
    """Small base64 JPEG data-URI (a frame ~1s into the clip) so the activity
    review can SHOW which clips a tag matched, the way faces show a photo grid.

    Data URI (not a file path) so the webview can render it without any Tauri
    file-scope config. Cached per path; returns None if extraction fails and the
    review just omits that thumbnail.
    """
    if path in cache:
        return cache[path]
    uri = None
    for seek in ("1", "0"):  # ~1s in, then the very start for sub-1s clips
        try:
            out = subprocess.run(
                ["ffmpeg", "-v", "error", "-ss", seek, "-i", path, "-frames:v", "1",
                 "-vf", f"scale={width}:-2", "-c:v", "mjpeg", "-f", "image2pipe", "-"],
                capture_output=True, timeout=15,
            ).stdout
            if out:
                uri = "data:image/jpeg;base64," + base64.b64encode(out).decode("ascii")
                break
        except Exception:  # noqa: BLE001 - a bad clip just loses its thumbnail
            pass
    cache[path] = uri
    return uri


def _score_energy(conn, video_path, video_id: int, do_motion: bool):
    """Compute a clip's energy (audio + optional motion) and persist it.

    Independent of the face/CLIP frame loop — energy.score_clip runs its own
    lightweight ffmpeg audio pass and OpenCV motion pass over the raw file.
    Returns the EnergyResult so the caller can emit progress.
    """
    res = _energy.score_clip(video_path, motion=do_motion)
    _db.set_energy(conn, video_id, res.score, res.bucket, res.peaks)
    return res


@app.command()
def scan(
    path: Path = typer.Argument(..., exists=True, help="Video file or directory to scan."),
    db_path: Path = typer.Option(DEFAULT_DB, "--db"),
    sample_fps: float = typer.Option(1.0, "--fps", help="Frames per second to sample for face detection."),
    rescan: bool = typer.Option(False, "--rescan", help="Re-scan videos already in the index."),
    min_score: float = typer.Option(0.5, "--min-score", help="Minimum face detection confidence."),
    tags: str = typer.Option("", "--tags", help="Comma-separated batch tags applied to every clip in this scan (e.g. 'baptism,kids')."),
    activities: bool = typer.Option(True, "--activities/--no-activities", help="Also run MobileCLIP image encoder on each sampled frame so the activity-suggest step can find scenes (kids, beach, wedding…). Disable to keep scans face-only."),
    energy: bool = typer.Option(True, "--energy/--no-energy", help="Score each clip's 'energy' (excitement) from audio loudness + camera-compensated motion, tag it high/medium/low, and mark its peak moments. On by default."),
    energy_motion: bool = typer.Option(True, "--energy-motion/--no-energy-motion", help="Include the optical-flow motion pass in energy scoring. Disable for audio-only energy (faster, but blind to silent action)."),
):
    """Walk a path, sample frames, detect faces, store embeddings."""
    videos = _extract.walk_videos(path)
    if not videos:
        console.print(f"[red]No videos found under {path}[/red]")
        raise typer.Exit(1)

    batch_tags = [t.strip().lower() for t in tags.split(",") if t.strip()] if tags else []

    conn = _db.connect(db_path)
    detector = _detect.Detector(min_score=min_score)

    # Activity encoder is optional + degrades gracefully. If the .mlpackages
    # aren't bundled (or coremltools is missing), we keep the scan running
    # in face-only mode rather than failing the whole batch.
    clip_encoder = None
    if activities:
        try:
            clip_encoder = _clip.ClipEncoder()
            # Trigger lazy load now so a missing-model error surfaces before
            # we've already sunk minutes into face detection.
            clip_encoder._load()
        except _clip.ClipUnavailable as e:
            console.print(f"[yellow]Activity detection disabled: {e}[/yellow]")
            _emit("activities-disabled", reason=str(e))
            clip_encoder = None

    console.print(f"Found [bold]{len(videos)}[/bold] video(s)")
    _emit("scan-start", total=len(videos), activities=bool(clip_encoder))
    total_faces = 0
    total_skipped = 0
    total_backfilled = 0

    for index, v in enumerate(videos, start=1):
        path_str = str(v.resolve())
        if not rescan and _db.is_scanned(conn, path_str):
            # Already face-scanned. A re-drop may still need two backfills:
            # (1) activity embeddings for libraries first scanned before CLIP
            # shipped (faces but zero frame embeddings → activity-suggest finds
            # nothing), and (2) energy for libraries scanned before energy
            # shipped. Do whichever is missing; only count as skipped if neither.
            vid = _db.video_id_for_path(conn, path_str)
            # Re-drop with new tags: update this clip's batch vocabulary so
            # activity-suggest re-matches the words she just typed instead of
            # silently keeping the original set (the clip is otherwise skipped).
            if batch_tags and vid is not None:
                _db.set_batch_tags(conn, vid, batch_tags)
            did_backfill = False
            if clip_encoder is not None and vid is not None and not _db.video_has_embeddings(conn, vid):
                _emit("video-backfill", name=v.name, index=index, total=len(videos))
                try:
                    n_emb = _embed_only(conn, clip_encoder, v, vid, sample_fps, console)
                    total_backfilled += 1
                    did_backfill = True
                    console.print(f"[cyan]Backfilled {n_emb} activity embedding(s) for {v.name}[/cyan]")
                except Exception as e:
                    console.print(f"[yellow]Embedding backfill failed for {v.name}: {e}[/yellow]")
                    _emit("video-skip", name=v.name, index=index, total=len(videos), reason="backfill-failed")
            if energy and vid is not None and not _db.video_has_energy(conn, vid):
                try:
                    res = _score_energy(conn, v, vid, energy_motion)
                    did_backfill = True
                    console.print(f"[cyan]Backfilled energy ({res.bucket}) for {v.name}[/cyan]")
                    _emit("video-energy", name=v.name, bucket=res.bucket, score=round(res.score, 3), peaks=len(res.peaks))
                except Exception as e:
                    console.print(f"[yellow]Energy backfill failed for {v.name}: {e}[/yellow]")
                    _emit("energy-skip", name=v.name, reason=str(e))
            if did_backfill:
                _emit("video-done", name=v.name, index=index, total=len(videos), faces=0)
            else:
                total_skipped += 1
                _emit("video-skip", name=v.name, index=index, total=len(videos))
            continue
        # The whole per-clip body is guarded: a probe error, a detector crash,
        # or any other failure skips THIS clip and moves on — it never aborts
        # the batch (clips after a bad one used to be lost). scan_complete is
        # only set at the very end, so a crash leaves the clip re-scannable.
        try:
            duration, _, _ = _extract.probe(v)
            _emit("video-start", name=v.name, index=index, total=len(videos), duration_sec=duration)

            video_id = _db.add_video(conn, path_str, duration)
            if batch_tags:
                _db.set_batch_tags(conn, video_id, batch_tags)
            # Reaching here means the clip isn't marked complete, so we're doing
            # a full (re-)scan: wipe any prior partial faces/embeddings first so
            # a resumed or repeated scan can't accumulate duplicate rows.
            _db.clear_video_faces(conn, video_id)
            _db.clear_video_embeddings(conn, video_id)

            rows: list = []
            emb_rows: list[tuple[float, bytes]] = []
            approx_frames = max(1, int(duration * sample_fps))
            with Progress(
                TextColumn("[bold cyan]{task.description}"),
                BarColumn(),
                TextColumn("{task.completed}/{task.total} frames"),
                TextColumn("[green]{task.fields[faces]} faces"),
                TimeElapsedColumn(),
                TimeRemainingColumn(),
                console=console,
            ) as prog:
                task = prog.add_task(v.name[:50], total=approx_frames, faces=0)
                face_count = 0
                for t, frame in _extract.iter_frames(v, sample_fps=sample_fps):
                    try:
                        faces = detector.detect(frame)
                    except Exception as e:  # noqa: BLE001 - one bad frame must not sink the clip
                        if not getattr(scan, "_detect_warned", False):
                            console.print(f"[yellow]Face detection failed on a frame: {e}[/yellow]")
                            scan._detect_warned = True
                        faces = []
                    for f in faces:
                        rows.append((t, f.bbox, f.embedding))
                    face_count += len(faces)
                    if clip_encoder is not None:
                        try:
                            emb = clip_encoder.encode_image(frame)
                            emb_rows.append((t, _clip.embedding_to_bytes(emb)))
                        except Exception as e:
                            # Don't fail the whole scan on one bad frame; log once.
                            if not getattr(scan, "_clip_warned", False):
                                console.print(f"[yellow]Activity encoding failed on a frame: {e}[/yellow]")
                                scan._clip_warned = True
                    prog.update(task, advance=1, faces=face_count)
                    if len(rows) >= 500:
                        _db.add_faces_bulk(conn, video_id, rows)
                        rows.clear()
                    if len(emb_rows) >= 200:
                        _db.add_frame_embeddings_bulk(conn, video_id, emb_rows)
                        emb_rows.clear()
                if rows:
                    _db.add_faces_bulk(conn, video_id, rows)
                if emb_rows:
                    _db.add_frame_embeddings_bulk(conn, video_id, emb_rows)
                total_faces += face_count
            # Energy runs its own ffmpeg audio + OpenCV motion pass over the raw
            # file, independent of the sampled frames. Non-fatal on its own.
            if energy:
                try:
                    res = _score_energy(conn, v, video_id, energy_motion)
                    console.print(f"[cyan]{v.name}: {res.bucket} energy[/cyan]")
                    _emit("video-energy", name=v.name, bucket=res.bucket, score=round(res.score, 3), peaks=len(res.peaks))
                except Exception as e:
                    console.print(f"[yellow]Energy scoring failed for {v.name}: {e}[/yellow]")
                    _emit("energy-skip", name=v.name, reason=str(e))
            # Faces, embeddings, and energy are all written — safe to mark done.
            _db.mark_scan_complete(conn, video_id)
            _emit("video-done", name=v.name, index=index, total=len(videos), faces=face_count)
        except Exception as e:  # noqa: BLE001 - a bad clip skips; it never aborts the batch
            console.print(f"[yellow]Skipping {v.name}: scan failed ({e})[/yellow]")
            _emit("video-skip", name=v.name, index=index, total=len(videos), reason="scan-failed")
            total_skipped += 1
            continue

    _emit("scan-complete", total_faces=total_faces, total_skipped=total_skipped, total_videos=len(videos), total_backfilled=total_backfilled)
    backfill_note = f", {total_backfilled} backfilled for activity tagging" if total_backfilled else ""
    console.print(f"\n[bold green]Done.[/bold green] {total_faces} faces indexed, {total_skipped} videos skipped (already scanned){backfill_note}.")


@app.command("activity-suggest")
def activity_suggest(
    db_path: Path = typer.Option(DEFAULT_DB, "--db"),
    threshold: float = typer.Option(0.15, "--threshold", help="Absolute floor (min cosine, per-video MAX over frames) below which a tag is never applied. Selection is otherwise relative — a tag must also stand out for that tag and on that clip — so this is a noise floor, not the primary knob. Calibrated on real footage; raising it trims more aggressively."),
):
    """Look for each of the user's typed tags in every clip and apply it only
    where it actually appears.

    The vocabulary is exactly the tags the user entered on the welcome screen
    (stored as batch_tags at scan time), NOT a built-in prompt list. For each
    tag we CLIP-score it against each clip's frame embeddings and apply it to
    the clips where it crosses `threshold` — the tag equivalent of how a face
    name only attaches to the clips that person is in.

    Run AFTER `scan` (which produced the embeddings) and AFTER `cluster`+labeler.
    Writes matches into the `auto_tags` table; they merge into the editor's
    Keywords column via tag.videos_with_keywords().
    """
    from collections import defaultdict

    conn = _db.connect(db_path)

    user_tags = _db.all_batch_tags(conn)
    if not user_tags:
        console.print("[yellow]No tags to look for — the user didn't enter any on the welcome screen.[/yellow]")
        _emit("activity-empty", message="no user tags entered")
        raise typer.Exit(0)

    def _aggregate(results: dict[str, list[tuple[str, float]]]) -> list[dict]:
        """Per-tag rollup the review screen reads: clip count, peak score, a
        sample name, and up to a few clip thumbnails so the user can SEE what
        each tag matched instead of trusting a bare count."""
        agg: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for path, picks in results.items():
            for t, s in picks:
                agg[t].append((path, s))
        thumb_cache: dict = {}
        rollup: list[dict] = []
        for t, hits in agg.items():
            ordered = sorted(hits, key=lambda x: -x[1])
            # Cap thumbnails per tag: enough to preview what matched (the count
            # carries the total) while keeping the base64 payload on the emit
            # line well within the shell reader's per-line budget.
            thumbs = [u for p, _ in ordered[:4] if (u := _clip_thumb_datauri(p, thumb_cache))]
            rollup.append({
                "tag": t,
                "clips": len(hits),
                "peak": round(max(s for _, s in hits), 3),
                "sample": Path(ordered[0][0]).name,
                "thumbs": thumbs,
            })
        return sorted(rollup, key=lambda m: (-m["clips"], -m["peak"]))

    def _emit_complete(results: dict[str, list[tuple[str, float]]], total: int) -> None:
        sample = None
        if results:
            sp = next(iter(results.keys()))
            sample = {"file": Path(sp).name, "tags": [t for t, _ in results[sp]]}
        _emit("activity-complete", total=total, tagged=len(results), sample=sample, matched=_aggregate(results))

    # Load the matcher eagerly so a present-but-unloadable model surfaces here
    # (the old lazy load inside apply_auto_tags was unguarded and crashed the
    # command).
    encoder = None
    load_error = None
    try:
        encoder = _clip.ClipEncoder()
        encoder._load()
    except _clip.ClipUnavailable as e:
        load_error = str(e)
        encoder = None

    # Self-heal missing embeddings. Matching needs frame embeddings; older
    # libraries (or any clip scanned face-only) have none. The old behavior was
    # to blanket-stamp every typed tag onto every clip, which bakes wrong
    # keywords into files (a "casino, bingo" tag on a nursery shot — the exact
    # thing users report). Instead, if the scene encoder loaded, compute
    # embeddings on demand for any indexed clip that lacks them and is still on
    # disk, then match per clip like normal.
    if encoder is not None:
        needs = [
            (vid, p)
            for vid, p in conn.execute("SELECT id, path FROM videos").fetchall()
            if not _db.video_has_embeddings(conn, vid) and Path(p).exists()
        ]
        if needs:
            console.print(f"[cyan]Computing scene embeddings for {len(needs)} clip(s) that lack them…[/cyan]")
            _emit("activity-backfill-start", total=len(needs))
            with Progress(
                TextColumn("[bold cyan]{task.description}"),
                BarColumn(),
                TextColumn("{task.completed}/{task.total}"),
                TimeElapsedColumn(),
                console=console,
            ) as prog:
                task = prog.add_task("scene embeddings", total=len(needs))
                for i, (vid, p) in enumerate(needs, start=1):
                    try:
                        n = _embed_only(conn, encoder, Path(p), vid, 1.0, console)
                        _emit("activity-backfill", name=Path(p).name, index=i, total=len(needs), embeddings=n)
                    except Exception as e:  # noqa: BLE001 - one bad clip shouldn't sink the batch
                        console.print(f"[yellow]Scene embedding failed for {Path(p).name}: {e}[/yellow]")
                    prog.update(task, advance=1)

    videos = _db.videos_with_embeddings(conn)

    if not videos:
        # We couldn't get embeddings for anything. Two very different causes:
        if encoder is None:
            # The scene model genuinely can't load. This is the ONE case where
            # blanket-stamping is defensible — we can't match at all, and losing
            # the user's typed tags outright is worse. Kept deliberately.
            console.print(
                f"[yellow]Scene model unavailable ({load_error}). Applying your "
                f"{len(user_tags)} tag(s) to every clip so they aren't lost — "
                f"drop wrong ones on the review screen.[/yellow]"
            )
            all_videos = conn.execute("SELECT id, path FROM videos").fetchall()
            if not all_videos:
                _emit("activity-empty", message="no videos in index")
                raise typer.Exit(0)
            stamped: dict[str, list[tuple[str, float]]] = {}
            for vid, path in all_videos:
                picks = [(t, 1.0) for t in user_tags]
                _db.replace_auto_tags(conn, vid, picks)
                stamped[path] = picks
            _emit("activity-fallback", reason=f"scene model could not load ({load_error})", tags=user_tags, clips=len(all_videos))
            _emit_complete(stamped, total=len(all_videos))
            return
        # The model loaded but no clip could be analyzed — the indexed files
        # aren't on disk (moved/renamed/in iCloud). Blanket-stamping here would
        # just bake garbage; say so plainly instead.
        console.print(
            "[yellow]No clips available to analyze — none of the indexed files "
            "are on disk (moved, renamed, or not downloaded from iCloud). Nothing tagged.[/yellow]"
        )
        _emit("activity-empty", message="no clips on disk to analyze")
        raise typer.Exit(0)

    # Precise per-clip matching. Encode each tag with the most-specific subject
    # we have (curated phrase when known, e.g. "pool" -> "a swimming pool"), but
    # keep the user's word as the written label; activity.py's templates wrap and
    # ensemble it (the CLIP zero-shot trick).
    prompts = [(_activity.enrich_tag(t), t) for t in user_tags]

    _emit("activity-start", total=len(videos))
    console.print(
        f"Looking for [bold]{len(user_tags)}[/bold] tag(s) "
        f"({', '.join(user_tags)}) across [bold]{len(videos)}[/bold] clip(s)…"
    )
    results = _activity.apply_auto_tags(
        conn,
        encoder,
        # Relative selection for the user's own tags: a tag lands on a clip only
        # if it stands out for that tag AND on that clip, above an absolute
        # floor. A flat threshold over-tags badly here — CLIP scores are low and
        # uncalibrated, so e.g. "casino" scores ~0.135 on footage with no casino
        # and any threshold that catches real matches also stamps it everywhere.
        relative=True,
        floor=threshold,
        # Cap tags per clip so a single clip can't collect the whole vocabulary
        # even if many tags score high on it — keep its strongest few.
        max_tags_per_video=min(len(user_tags), 6),
        prompts=prompts,
    )

    if not results:
        console.print(f"[yellow]No tag scored above {threshold:.2f} on any clip.[/yellow]")
        _emit("activity-complete", total=len(videos), tagged=0, sample=None, matched=[])
        return

    table = Table("video", "tags (score)")
    for path, picks in list(results.items())[:30]:
        chips = ", ".join(f"{t} ({s:.2f})" for t, s in picks)
        table.add_row(Path(path).name, chips)
    console.print(table)
    console.print(f"[bold green]Done.[/bold green] {len(results)} of {len(videos)} videos got auto-tags.")
    _emit_complete(results, total=len(videos))


@app.command()
def selftest():
    """Verify the packaged bundle can actually run: exiftool resolves, the
    MobileCLIP model loads + encodes, and InsightFace detects a face and returns
    an embedding. Run against the FROZEN binary in CI so a build that compiles
    but dies at runtime fails the release instead of auto-updating to every
    user. Exits non-zero on any failure.
    """
    import shutil

    ok = True

    exe = shutil.which("exiftool")
    if exe:
        console.print(f"[green]exiftool OK[/green] ({exe})")
    else:
        console.print("[red]exiftool NOT on PATH[/red]")
        ok = False

    try:
        enc = _clip.ClipEncoder()
        if not enc.available:
            console.print("[red]MobileCLIP model NOT bundled/resolvable[/red]")
            ok = False
        else:
            enc._load()
            vec = enc.encode_texts(["a photo of a test scene"])
            if getattr(vec, "shape", (0,))[-1] > 0:
                console.print("[green]MobileCLIP OK[/green] (loaded + encoded)")
            else:
                console.print("[red]MobileCLIP returned an empty embedding[/red]")
                ok = False
    except Exception as e:  # noqa: BLE001 - surface any load/encode failure
        console.print(f"[red]MobileCLIP FAILED: {e}[/red]")
        ok = False

    # Real face-detect pass on a known face image. This is the check that would
    # have caught the 1k3d68 pose crash (None meanshape -> NoneType .shape)
    # before it auto-updated to the fleet: it runs the actual bundled Detector
    # and asserts a normalized 512-d embedding comes back.
    try:
        from insightface.data import get_image as ins_get_image

        img = ins_get_image("t1")  # bundled 2-face sample
        if img is None:
            console.print("[red]InsightFace sample image NOT bundled[/red]")
            ok = False
        else:
            faces = _detect.Detector().detect(img)
            dim = int(getattr(faces[0].embedding, "shape", (0,))[-1]) if faces else 0
            if not faces:
                console.print("[red]face detect returned no faces on the sample[/red]")
                ok = False
            elif dim != 512:
                console.print(f"[red]face embedding is {dim}-d, expected 512[/red]")
                ok = False
            else:
                console.print(
                    f"[green]face detect OK[/green] ({len(faces)} faces, {dim}-d embedding)"
                )
    except Exception as e:  # noqa: BLE001 - surface any detect/load failure
        console.print(f"[red]face detect FAILED: {e}[/red]")
        ok = False

    # Energy engine: synthesize a tiny clip (moving pattern + tone) and confirm
    # the audio pass (ffmpeg decode) and motion pass (OpenCV optical flow) both
    # run and yield a bucket. Catches a broken ffmpeg-audio or cv2-flow path in
    # the frozen build the same way the face check catches a broken detector.
    try:
        import subprocess
        import tempfile

        ff = shutil.which("ffmpeg")
        if not ff:
            console.print("[red]ffmpeg NOT on PATH (energy needs it)[/red]")
            ok = False
        else:
            with tempfile.TemporaryDirectory() as td:
                clip = str(Path(td) / "energy_selftest.mp4")
                subprocess.run(
                    [ff, "-v", "error", "-y",
                     "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=15:duration=2",
                     "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                     "-pix_fmt", "yuv420p", "-shortest", clip],
                    check=True, capture_output=True,
                )
                res = _energy.score_clip(clip)
                if res.bucket in _energy.BUCKETS and res.series.size > 0:
                    console.print(
                        f"[green]energy OK[/green] ({res.bucket}; audio={res.have_audio}, motion={res.have_motion})"
                    )
                else:
                    console.print("[red]energy scoring returned nothing usable[/red]")
                    ok = False
    except Exception as e:  # noqa: BLE001 - surface any energy failure
        console.print(f"[red]energy FAILED: {e}[/red]")
        ok = False

    if not ok:
        console.print("[bold red]selftest FAILED[/bold red]")
        raise typer.Exit(1)
    console.print("[bold green]selftest passed[/bold green]")


@app.command()
def cluster(
    db_path: Path = typer.Option(DEFAULT_DB, "--db"),
    epsilon: float = typer.Option(0.68, "--eps", help="Cluster selection epsilon (lower = stricter, more clusters; higher = looser, fewer clusters)."),
    min_size: int = typer.Option(5, "--min-size", help="Min faces per cluster."),
    recluster: bool = typer.Option(False, "--recluster", help="Force re-clustering even if clusters already exist. Will reshuffle cluster IDs and may break already-saved labels."),
):
    """Group all indexed faces into person clusters.

    Behavior depends on what's already in the DB:

    - **Cold start (no clusters yet):** runs HDBSCAN over every face. This
      is the original full-library cluster pass.
    - **Incremental (clusters exist + new unclustered faces):** runs the
      two-stage `incremental_assign` (centroid match + HDBSCAN on leftover)
      so a newly-dropped clip's faces either join existing clusters
      (inheriting saved labels) or form new ones, without reshuffling
      existing cluster IDs. This is the path that fires when a user drops
      a second batch into a library that already has labeled people.
    - **Already clustered + nothing new:** no-op.
    - **--recluster:** force the full pass; will reshuffle IDs and may
      break already-saved labels (kept for diagnostic / manual repair).
    """
    conn = _db.connect(db_path)

    if recluster or not _db.has_clusters(conn):
        face_ids, embs = _db.all_embeddings(conn)
        if not face_ids:
            # Not an error: a folder of faceless footage (b-roll, landscape,
            # drone) is legitimate, and the user may still want scene/activity
            # tags. Exit 0 so the flow continues to activity-suggest instead of
            # showing a hard error that blocks the second headline feature.
            console.print("[yellow]No faces detected — skipping face clustering.[/yellow]")
            _emit("cluster-empty")
            raise typer.Exit(0)
        _emit("cluster-start", faces=len(face_ids))
        console.print(f"Clustering {len(face_ids)} faces…")
        labels = _cluster.cluster_embeddings(embs, min_cluster_size=min_size, epsilon=epsilon)
        assignments = {fid: int(lbl) for fid, lbl in zip(face_ids, labels)}
        _db.set_clusters(conn, assignments)
    else:
        new_ids, new_embs = _db.unclustered_embeddings(conn)
        if not new_ids:
            summary = _db.cluster_summary(conn)
            console.print(f"[yellow]Already clustered ({len(summary)} clusters) and no new faces to assign.[/yellow]")
            _emit("cluster-skipped", clusters=len(summary))
            _emit("cluster-complete", clusters=len(summary))
            return
        centroids = _db.cluster_centroids(conn)
        next_cid = _db.max_cluster_id(conn) + 1
        _emit("cluster-start", faces=len(new_ids))
        console.print(
            f"Incrementally clustering {len(new_ids)} new face(s) "
            f"against {len(centroids)} existing cluster(s)…"
        )
        new_labels = _cluster.incremental_assign(
            new_embs,
            centroids,
            next_cid,
            min_cluster_size=min_size,
            epsilon=epsilon,
        )
        assignments = {fid: lbl for fid, lbl in zip(new_ids, new_labels)}
        _db.set_clusters(conn, assignments)
        matched = sum(1 for l in new_labels if l in centroids)
        new_formed = len({l for l in new_labels if l >= 0 and l not in centroids})
        noise = sum(1 for l in new_labels if l < 0)
        console.print(
            f"[bold green]{matched}[/bold green] face(s) joined existing clusters, "
            f"[bold green]{new_formed}[/bold green] new cluster(s) formed, "
            f"{noise} marked as noise."
        )

    summary = _db.cluster_summary(conn)
    _emit("cluster-complete", clusters=len(summary))
    console.print(f"[bold green]{len(summary)}[/bold green] clusters formed.")
    table = Table("cluster", "faces", "name")
    for cid, n, name in summary[:20]:
        table.add_row(str(cid), str(n), name or "[dim](unnamed)[/dim]")
    console.print(table)
    if len(summary) > 20:
        console.print(f"[dim]…and {len(summary) - 20} more[/dim]")


@app.command()
def label(
    db_path: Path = typer.Option(DEFAULT_DB, "--db"),
    work_dir: Path = typer.Option(DEFAULT_LABEL_DIR, "--thumbs"),
    all_clusters: bool = typer.Option(False, "--all", help="Re-label clusters that already have names too."),
):
    """Interactively name each cluster. Opens a face grid in Preview, prompts in terminal."""
    conn = _db.connect(db_path)
    n = _label.label_clusters(conn, work_dir, only_unnamed=not all_clusters)
    console.print(f"\n[bold green]{n}[/bold green] cluster(s) named.")


@app.command("label-web")
def label_web(
    db_path: Path = typer.Option(DEFAULT_DB, "--db"),
    thumb_dir: Path = typer.Option(DEFAULT_LABEL_DIR, "--thumbs"),
    port: int = typer.Option(8765, "--port"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't auto-open browser."),
    scope_path: list[str] = typer.Option(
        None,
        "--scope-path",
        help=(
            "Restrict the labeler to clusters that contain at least one face "
            "from a video matching this path (file) or under this directory. "
            "Repeatable. Users can toggle 'show all' from the chip in the UI."
        ),
    ),
):
    """Open a one-page web labeler. See every cluster, type names, hit Save All."""
    _web.serve(
        db_path,
        thumb_dir,
        port=port,
        open_browser=not no_browser,
        scope_paths=list(scope_path) if scope_path else None,
    )


@app.command()
def search(
    name: str = typer.Argument(..., help="Person name to search for."),
    db_path: Path = typer.Option(DEFAULT_DB, "--db"),
):
    """List videos containing a named person."""
    conn = _db.connect(db_path)
    rows = _db.videos_with_person(conn, name)
    if not rows:
        console.print(f"[yellow]No videos found with {name!r}[/yellow]")
        raise typer.Exit(0)
    table = Table("hits", "video")
    for _id, path, hits in rows:
        table.add_row(str(hits), path)
    console.print(table)


@app.command()
def cut(
    name: str = typer.Argument(..., help="Person name."),
    output: Path = typer.Option(..., "--out", "-o", help="Output file (e.g. sarah_reel.mp4)."),
    video: list[Path] = typer.Option(None, "--video", help="Limit to specific video file(s); repeatable. Omit for all."),
    db_path: Path = typer.Option(DEFAULT_DB, "--db"),
    gap: float = typer.Option(1.5, "--gap", help="Merge detections within this many seconds."),
    pad: float = typer.Option(0.75, "--pad", help="Pad each clip start/end by this many seconds."),
    min_clip: float = typer.Option(1.0, "--min-clip", help="Drop clips shorter than this."),
    width: int = typer.Option(1280, "--width"),
    height: int = typer.Option(720, "--height"),
):
    """Cut a temporal highlight reel of a named person."""
    conn = _db.connect(db_path)
    clips = _cut.collect_clips(conn, name, video, gap_sec=gap, pad_sec=pad, min_clip_sec=min_clip)
    if not clips:
        console.print(f"[yellow]No clips for {name!r}.[/yellow]")
        raise typer.Exit(1)
    total = sum(c.end - c.start for c in clips)
    console.print(f"Rendering [bold]{len(clips)}[/bold] clip(s) totaling {total:.1f}s → {output}")
    _cut.render_reel(clips, output, width=width, height=height)
    console.print(f"[bold green]Done.[/bold green] {output}")


@app.command()
def merge(
    db_path: Path = typer.Option(DEFAULT_DB, "--db"),
):
    """Manually consolidate clusters sharing a name.

    Usually you don't need to run this — `label-web` auto-merges on Save.
    But if you renamed clusters via the SQL DB directly or imported
    labels from elsewhere, this CLI command does the same merge.
    """
    conn = _db.connect(db_path)
    merged = _db.merge_clusters_by_name(conn)
    if not merged:
        console.print("[yellow]Nothing to merge.[/yellow]")
        return
    total = 0
    for name, cids in merged.items():
        n = len(cids) - 1
        total += n
        console.print(f"  {name}: merged {n} extra cluster(s) into cluster {cids[0]}")
    console.print(f"[bold green]Done.[/bold green] Merged {total} duplicate cluster(s).")


@app.command("rename-person")
def rename_person(
    old: str = typer.Argument(..., help="Current name."),
    new: str = typer.Argument(..., help="New name."),
    db_path: Path = typer.Option(DEFAULT_DB, "--db"),
):
    """Rename a person across all their clusters."""
    conn = _db.connect(db_path)
    rows = conn.execute(
        "UPDATE people SET name = ? WHERE name = ? RETURNING cluster_id",
        (new.strip(), old.strip()),
    ).fetchall()
    conn.commit()
    if not rows:
        console.print(f"[yellow]No person named {old!r}.[/yellow]")
        raise typer.Exit(1)
    console.print(f"Renamed {len(rows)} cluster(s) from {old!r} to {new!r}.")
    _emit("person-renamed", old=old, new=new, clusters=len(rows))


@app.command("delete-person")
def delete_person(
    name: str = typer.Argument(..., help="Person name to delete."),
    db_path: Path = typer.Option(DEFAULT_DB, "--db"),
):
    """Remove a person from the library. Faces stay in DB but are unnamed.

    The clips already on disk keep their existing XMP keywords until
    `tag-write` is re-run. To strip them too, run `tag-write` after this.
    """
    conn = _db.connect(db_path)
    rows = conn.execute(
        "DELETE FROM people WHERE name = ? RETURNING cluster_id",
        (name.strip(),),
    ).fetchall()
    conn.commit()
    if not rows:
        console.print(f"[yellow]No person named {name!r}.[/yellow]")
        raise typer.Exit(1)
    console.print(f"Deleted {len(rows)} cluster(s) named {name!r}.")
    _emit("person-deleted", name=name, clusters=len(rows))


@app.command("tag-write")
def tag_write(
    db_path: Path = typer.Option(DEFAULT_DB, "--db"),
    merge: bool = typer.Option(True, "--merge/--overwrite", help="Merge Spotted's keywords with any already in the file (default), preserving keywords set by other tools (Premiere, Photos, Bridge). --overwrite replaces the file's entire keyword set."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print what would be written, change nothing."),
    exclude_tags: str = typer.Option("", "--exclude-tags", help="Comma-separated matched tags to leave out (the ones the user unchecked on the review screen)."),
):
    """Write per-video person keywords into each .mov via exiftool.

    Premiere reads these as the Keywords column. DaVinci Resolve surfaces
    them in the Media Pool. After running this, an editor can search by
    person name across the whole library.

    By default keywords are MERGED with whatever is already in the file, and
    Spotted replaces only the keywords it wrote on a previous run (tracked in
    videos.spotted_keywords) so renames/removals take effect without clobbering
    another tool's keywords. `--overwrite` restores replace-everything behavior.
    """
    conn = _db.connect(db_path)
    exclude = {t.strip() for t in exclude_tags.split(",") if t.strip()}
    # Persist review-screen rejections: drop unchecked tags from auto_tags so
    # they also stop surfacing in in-app search and can't sneak back into a
    # later write. Not on a dry run — that must change nothing.
    if exclude and not dry_run:
        _db.delete_auto_tags_by_name(conn, exclude)
    mapping = _tag.videos_with_keywords(conn, exclude_tags=exclude)
    if not mapping:
        # Treat as a hard error so the Tauri shell shows it via showError()
        # instead of silently jumping to the Done screen. Otherwise the user
        # sees "Done" with nothing actually written to any file.
        msg = (
            "Nothing to tag: no face clusters were named, and none of the tags "
            "you entered were found in any clip."
        )
        console.print(f"[red]{msg}[/red]")
        _emit("tag-empty", message=msg)
        raise typer.Exit(2)

    _emit("tag-start", total=len(mapping))
    console.print(f"Writing keywords to [bold]{len(mapping)}[/bold] video(s)…")
    table = Table("video", "names")
    failed: list[tuple[str, str]] = []

    with Progress(
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as prog:
        task = prog.add_task("tagging", total=len(mapping))
        for idx, (path_str, names) in enumerate(mapping.items(), start=1):
            short = Path(path_str).name
            vid = _db.video_id_for_path(conn, path_str)
            # Merge mode: keep keywords already in the file that Spotted didn't
            # write, drop Spotted's own previous set (so a rename/removal sticks),
            # add the current set. Overwrite mode: write exactly the current set.
            if merge:
                try:
                    existing = _tag.read_keywords(Path(path_str))
                except Exception as e:
                    # Cannot see what's already in the file, so writing would
                    # replace the user's keywords with only Spotted's. Skip the
                    # clip and report it instead of silently destroying them.
                    failed.append((short, f"couldn't read existing keywords: {e}"))
                    _emit("tag-error", name=short, message=str(e))
                    prog.update(task, advance=1)
                    continue
                in_file = set(existing.get("xmp", [])) | set(existing.get("keys", []))
                prior_spotted = set(_db.get_spotted_keywords(conn, vid)) if vid else set()
                final = sorted((in_file - prior_spotted) | set(names))
            else:
                final = list(names)
            table.add_row(short, ", ".join(names))
            _emit("tag-video", name=short, names=names, index=idx, total=len(mapping))
            if not dry_run:
                try:
                    _tag.write_keywords(Path(path_str), final, replace=True)
                    # Remember exactly what Spotted put here for the next run's diff.
                    if vid:
                        _db.set_spotted_keywords(conn, vid, names)
                except _tag.ExiftoolMissing as e:
                    console.print(f"\n[red]{e}[/red]")
                    _emit("error", stage="tag-write", message=str(e))
                    raise typer.Exit(2)
                except Exception as e:
                    failed.append((short, str(e)))
                    _emit("tag-error", name=short, message=str(e))
                # Also write to Spotlight Comment so macOS Finder search
                # finds the clip by name/tag. Non-fatal — XMP write is the
                # critical path for Premiere/DaVinci.
                try:
                    _finder.write_finder_comment(Path(path_str), final)
                except Exception as e:
                    _emit("finder-error", name=short, message=str(e))
            prog.update(task, advance=1)

    console.print(table)
    if dry_run:
        console.print(f"[dim]Dry run. No files changed.[/dim]")
    elif failed:
        # Same surface logic as the empty-mapping case: bubble up to the
        # Tauri shell with a non-zero exit so the user sees what broke,
        # instead of a green "Done" while every file silently failed.
        console.print(f"[red]{len(failed)} of {len(mapping)} clip(s) failed to tag:[/red]")
        for name, err in failed:
            console.print(f"  [red]{name}[/red]  {err}")
        # First failure's message is usually the diagnostic signal (most
        # failures here have the same root cause — exiftool config, perms,
        # quarantine xattr). Surface it as a one-liner the UI can show.
        first_name, first_err = failed[0]
        summary = f"exiftool failed on {len(failed)}/{len(mapping)} clip(s). First: {first_name}: {first_err}"
        console.print(f"[red]{summary}[/red]")
        _emit("tag-failed", failed=len(failed), total=len(mapping), first=summary)
        raise typer.Exit(3)
    else:
        # Verify on a sample clip so the UI can show concrete proof of
        # what landed in each metadata namespace.
        if mapping:
            sample_path_str = next(iter(mapping.keys()))
            sample_path = Path(sample_path_str)
            try:
                kw = _tag.read_keywords(sample_path)
                comment = _finder.read_finder_comment(sample_path) or ""
                _emit(
                    "tag-verified",
                    file=sample_path.name,
                    xmp=kw.get("xmp", []),
                    keys=kw.get("keys", []),
                    comment=comment,
                )
            except Exception as e:
                _emit("tag-verify-error", message=str(e))
        _emit("tag-complete", total=len(mapping))
        console.print(f"[bold green]Done.[/bold green] {len(mapping)} videos tagged.")


@app.command("markers-write")
def markers_write(
    db_path: Path = typer.Option(DEFAULT_DB, "--db"),
    sidecar: bool = typer.Option(
        False, "--sidecar/--no-sidecar",
        help="Also drop a .xmp sidecar next to each clip (a DaVinci fallback). Off by default: sidecars leave a file beside every clip, which clutters a folder of thousands, and the markers are also written in-file. When off, any sidecar Spotted previously left is cleaned up.",
    ),
    resolve: bool = typer.Option(
        True, "--resolve/--no-resolve",
        help="Also emit a 'Spotted Markers' DaVinci Resolve script (into Resolve's Scripts folder, else next to the footage). DaVinci ignores in-file XMP markers, so this is the only way markers show there — the user runs it from Workspace > Scripts after importing.",
    ),
):
    """Write per-face timeline markers (XMP-xmpDM:Markers) into each video.

    Premiere Pro and DaVinci Resolve render these as clip markers on the
    timeline scrubber. Each marker is one face detection: "Sarah at 0:03,
    Dad at 0:08…". Editors can click a marker to jump to that frame.

    Markers are written in-file into each .mov. By default no .xmp sidecar is
    left behind (and any Spotted wrote before is removed) so tagged folders stay
    clean; pass --sidecar if you specifically need DaVinci's sidecar-file path.
    """
    conn = _db.connect(db_path)
    # Markers come from two sources: named-face appearances and energy peaks.
    # Union them by video so a clip with no named faces still gets its energy
    # markers, and a clip with both gets one merged, time-sorted set.
    by_id: dict[int, str] = {}
    for vid, path_str in _markers.videos_with_named_faces(conn):
        by_id[vid] = path_str
    for vid, path_str in _db.videos_with_energy_peaks(conn):
        by_id.setdefault(vid, path_str)
    videos = sorted(by_id.items())
    if not videos:
        console.print("[yellow]No markers to write (no named faces or energy peaks).[/yellow]")
        raise typer.Exit(0)

    # Every name Spotted may claim. Markers on a clip whose name isn't in here
    # were put there by a human in Premiere, and write_markers preserves them.
    known_names = {
        r[0] for r in conn.execute(
            "SELECT DISTINCT name FROM people WHERE name IS NOT NULL AND name != ''"
        ).fetchall()
    }

    _emit("markers-start", total=len(videos))
    console.print(f"Writing markers to [bold]{len(videos)}[/bold] video(s)…")
    failed: list[tuple[str, str]] = []
    sidecar_failed: list[tuple[str, str]] = []
    last_written: tuple[Path, int] | None = None  # for the verification readback
    video_markers: dict[str, list[tuple[float, str]]] = {}  # for the Resolve script

    with Progress(
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as prog:
        task = prog.add_task("markers", total=len(videos))
        for idx, (vid, path_str) in enumerate(videos, start=1):
            short = Path(path_str).name
            # One marker per appearance, not one per sampled second. A person
            # on screen for a minute was producing 60 stacked markers.
            events = _markers.collapse_to_appearances(
                _markers.face_events_for_video(conn, vid)
            )
            events += [(t, "Energy peak") for t in _db.energy_peaks_for_video(conn, vid)]
            events.sort()
            if events:
                video_markers[path_str] = events
            _emit("markers-video", name=short, count=len(events), index=idx, total=len(videos))
            try:
                _markers.write_markers(Path(path_str), events, known_names=known_names)
                last_written = (Path(path_str), len(events))
            except _markers.ExiftoolMissing as e:
                console.print(f"\n[red]{e}[/red]")
                _emit("error", stage="markers-write", message=str(e))
                raise typer.Exit(2)
            except Exception as e:
                failed.append((short, str(e)))
                _emit("markers-error", name=short, message=str(e))
            # Sidecar XMP. Opt-in (--sidecar) as a DaVinci fallback; otherwise
            # we clean up any sidecar Spotted left before, so tagged folders
            # don't accumulate a .xmp beside every clip. In-file markers above
            # cover Premiere and DaVinci setups that read in-file.
            try:
                if sidecar:
                    _markers.write_markers_sidecar(Path(path_str), events)
                else:
                    _markers.delete_sidecar_if_spotted(Path(path_str))
            except Exception as e:
                sidecar_failed.append((short, str(e)))
                _emit("markers-sidecar-error", name=short, message=str(e))
            prog.update(task, advance=1)

    # DaVinci Resolve marker script. DaVinci ignores the in-file XMP markers, so
    # this script (matching clips by filename and calling the Resolve API) is how
    # markers reach it. Written once for the whole batch.
    if resolve and video_markers:
        import os
        try:
            common = os.path.commonpath(list(video_markers.keys()))
            fallback_dir = Path(common if os.path.isdir(common) else os.path.dirname(common))

            # Primary path: an FCPXML timeline sitting next to the footage.
            # DaVinci imports it with File > Import > Timeline — no scripting,
            # no preference to enable, nothing hidden in ~/Library. Tested
            # against a clean Resolve 21 install, where the Scripts menu
            # enumerated nothing from any documented location, which is what
            # made the script route unusable for real users.
            xml_out = _markers.write_fcpxml(video_markers, fallback_dir)
            if xml_out:
                console.print(f"[cyan]DaVinci timeline → {xml_out}[/cyan]")
                _emit("resolve-timeline", path=str(xml_out), clips=len(video_markers))

            # The markers themselves ride in a companion EDL. Resolve's FCPXML
            # import drops <marker> elements (verified on Resolve 21), but it
            # does import markers via Timelines > Import > Timeline Markers
            # from EDL. Both files come from one shared layout so the EDL
            # timecodes line up with the imported timeline frame for frame.
            edl_out = _markers.write_edl(video_markers, fallback_dir)
            if edl_out:
                console.print(f"[cyan]DaVinci markers → {edl_out}[/cyan]")
                _emit("resolve-edl", path=str(edl_out), clips=len(video_markers))

            # Secondary: keep emitting the script for anyone whose Resolve does
            # pick scripts up. It costs one small file and needs no user setup.
            out = _markers.write_resolve_script(video_markers, fallback_dir)
            if out:
                console.print(f"[cyan]DaVinci marker script → {out}[/cyan]")
                _emit("resolve-script", path=str(out), clips=len(video_markers))
        except Exception as e:  # noqa: BLE001 - never let the script step fail markers
            _emit("resolve-script-error", message=str(e))

    if failed:
        console.print(f"[red]{len(failed)} failure(s) writing in-file markers:[/red]")
        for n, err in failed:
            console.print(f"  [red]{n}[/red]  {err}")
    if sidecar_failed:
        console.print(f"[yellow]{len(sidecar_failed)} sidecar XMP failure(s) (DaVinci fallback):[/yellow]")
        for n, err in sidecar_failed:
            console.print(f"  [yellow]{n}[/yellow]  {err}")

    # Read back markers from a sample clip and surface to the UI so users see
    # proof markers landed without opening the .mov in an editor. Same shape as
    # the tag-verified event.
    #
    # This is deliberately NOT gated on `failed` being empty. It used to be, and
    # that meant a single bad clip out of hundreds suppressed the verification
    # for the whole batch — the UI then rendered "Markers (timeline): empty",
    # which reads as "markers didn't work" even when every other clip was
    # marked correctly. Report what actually landed, and report the failures
    # alongside it instead of hiding both.
    if last_written is not None:
        sample_path, event_count = last_written
        try:
            in_file = _markers.read_markers(sample_path)
            sidecar = _markers.read_markers_sidecar(sample_path)
            _emit(
                "markers-verified",
                file=sample_path.name,
                event_count=event_count,
                in_file_present=bool(in_file),
                sidecar_present=bool(sidecar),
                failed=len(failed),
                written=len(videos) - len(failed),
            )
        except Exception as e:
            _emit("markers-verify-error", message=str(e))
    _emit("markers-complete", total=len(videos), failed=len(failed))
    if failed:
        console.print(
            f"[bold yellow]Done.[/bold yellow] Wrote markers to "
            f"{len(videos) - len(failed)} of {len(videos)} clips "
            f"({len(failed)} failed)."
        )
    else:
        console.print(f"[bold green]Done.[/bold green] Wrote markers to {len(videos)} clips.")


@app.command("person-thumbs")
def person_thumbs(
    db_path: Path = typer.Option(DEFAULT_DB, "--db"),
    force: bool = typer.Option(False, "--force", help="Regenerate even if a thumb already exists on disk."),
):
    """Generate a 128x128 face thumbnail per named person.

    Library view's sidebar reads these from ~/.facetag/person_thumbs/.
    Idempotent — only generates what's missing unless --force.
    """
    conn = _db.connect(db_path)
    cids = _person_thumb.generate_person_thumbs(conn, force=force)
    _emit("person-thumbs-complete", count=len(cids), dir=str(_person_thumb.PERSON_THUMB_DIR))
    console.print(f"[bold green]Done.[/bold green] {len(cids)} thumbnail(s) at {_person_thumb.PERSON_THUMB_DIR}")


@app.command()
def status(
    db_path: Path = typer.Option(DEFAULT_DB, "--db"),
    detail: bool = typer.Option(False, "--detail", help="Include per-person clip lists (heavier query, used by Library view)."),
):
    """Show index summary (videos, faces, clusters, per-person clip counts)."""
    conn = _db.connect(db_path)
    n_videos = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    n_faces = conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0]
    n_clusters = conn.execute(
        "SELECT COUNT(DISTINCT cluster_id) FROM faces WHERE cluster_id IS NOT NULL AND cluster_id >= 0"
    ).fetchone()[0]
    n_named = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]

    per_person_rows = conn.execute(
        "SELECT p.name, "
        "       p.cluster_id, "
        "       COUNT(DISTINCT f.video_id) AS clips, "
        "       COUNT(f.id) AS faces "
        "FROM people p "
        "JOIN faces f ON f.cluster_id = p.cluster_id "
        "WHERE p.name IS NOT NULL AND p.name != '' "
        "GROUP BY p.name "
        "ORDER BY clips DESC, p.name ASC"
    ).fetchall()
    people = [
        {"name": n, "cluster_id": int(cid), "clips": c, "faces": fc}
        for n, cid, c, fc in per_person_rows
    ]

    table = Table("metric", "value")
    table.add_row("videos", str(n_videos))
    table.add_row("faces", str(n_faces))
    table.add_row("clusters", str(n_clusters))
    table.add_row("named people", str(n_named))
    console.print(table)
    if people:
        pt = Table("person", "clips", "faces")
        for p in people:
            pt.add_row(p["name"], str(p["clips"]), str(p["faces"]))
        console.print(pt)

    _emit(
        "library-stats",
        videos=n_videos,
        faces=n_faces,
        clusters=n_clusters,
        named=n_named,
        people=people,
    )

    if detail:
        # Per-person clip list with timestamps. One event per person so
        # very large libraries stream incrementally and we don't blow up
        # the JSON parser at the receiving end.
        for p in people:
            rows = conn.execute(
                "SELECT v.path, GROUP_CONCAT(f.timestamp_sec, ',') AS times "
                "FROM faces f "
                "JOIN videos v ON v.id = f.video_id "
                "JOIN people pp ON pp.cluster_id = f.cluster_id "
                "WHERE pp.name = ? "
                "GROUP BY v.id "
                "ORDER BY v.path",
                (p["name"],),
            ).fetchall()
            clips = [
                {
                    "path": path,
                    "name": Path(path).name,
                    "times": sorted([float(t) for t in times.split(",")]) if times else [],
                }
                for path, times in rows
            ]
            _emit("library-person", name=p["name"], clips=clips)

        # Library-wide clip → keywords index for in-app search. Merges all
        # three keyword sources (named people, batch tags, auto-tags) into
        # one map so the frontend can search any keyword and find clips
        # without bouncing out to Finder / DaVinci. Sent as one event
        # because <10k clip rows is trivial JSON.
        kw_map = _tag.videos_with_keywords(conn)
        clips_with_keywords = [
            {"path": path, "name": Path(path).name, "keywords": kws}
            for path, kws in sorted(kw_map.items())
        ]
        _emit("library-clip-index", clips=clips_with_keywords)
        _emit("library-detail-complete")


if __name__ == "__main__":
    app()
