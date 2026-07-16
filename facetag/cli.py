"""CLI entry point. `facetag --help` to explore."""
from __future__ import annotations

import json
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


@app.command()
def scan(
    path: Path = typer.Argument(..., exists=True, help="Video file or directory to scan."),
    db_path: Path = typer.Option(DEFAULT_DB, "--db"),
    sample_fps: float = typer.Option(1.0, "--fps", help="Frames per second to sample for face detection."),
    rescan: bool = typer.Option(False, "--rescan", help="Re-scan videos already in the index."),
    min_score: float = typer.Option(0.5, "--min-score", help="Minimum face detection confidence."),
    tags: str = typer.Option("", "--tags", help="Comma-separated batch tags applied to every clip in this scan (e.g. 'baptism,kids')."),
    activities: bool = typer.Option(True, "--activities/--no-activities", help="Also run MobileCLIP image encoder on each sampled frame so the activity-suggest step can find scenes (kids, beach, wedding…). Disable to keep scans face-only."),
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
            # Already face-scanned. But a video first indexed before activity
            # detection shipped has faces and ZERO frame embeddings, so the
            # activity-suggest step silently finds nothing for it no matter how
            # many times the user re-runs. Backfill the embeddings here (encoder
            # only — faces already exist, so no re-detection and no duplicates)
            # so re-dropping an existing library finally lights up scene tags.
            vid = _db.video_id_for_path(conn, path_str)
            if clip_encoder is not None and vid is not None and not _db.video_has_embeddings(conn, vid):
                _emit("video-backfill", name=v.name, index=index, total=len(videos))
                try:
                    n_emb = _embed_only(conn, clip_encoder, v, vid, sample_fps, console)
                    total_backfilled += 1
                    console.print(f"[cyan]Backfilled {n_emb} activity embedding(s) for {v.name}[/cyan]")
                    _emit("video-done", name=v.name, index=index, total=len(videos), faces=0, backfilled=n_emb)
                except Exception as e:
                    console.print(f"[yellow]Embedding backfill failed for {v.name}: {e}[/yellow]")
                    _emit("video-skip", name=v.name, index=index, total=len(videos), reason="backfill-failed")
            else:
                total_skipped += 1
                _emit("video-skip", name=v.name, index=index, total=len(videos))
            continue
        try:
            duration, _, _ = _extract.probe(v)
        except Exception as e:
            console.print(f"[yellow]Skipping {v.name}: probe failed ({e})[/yellow]")
            _emit("video-skip", name=v.name, index=index, total=len(videos), reason="probe-failed")
            continue
        _emit("video-start", name=v.name, index=index, total=len(videos), duration_sec=duration)

        video_id = _db.add_video(conn, path_str, duration)
        if batch_tags:
            _db.set_batch_tags(conn, video_id, batch_tags)
        if rescan:
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
                faces = detector.detect(frame)
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
        _emit("video-done", name=v.name, index=index, total=len(videos), faces=face_count)

    _emit("scan-complete", total_faces=total_faces, total_skipped=total_skipped, total_videos=len(videos), total_backfilled=total_backfilled)
    backfill_note = f", {total_backfilled} backfilled for activity tagging" if total_backfilled else ""
    console.print(f"\n[bold green]Done.[/bold green] {total_faces} faces indexed, {total_skipped} videos skipped (already scanned){backfill_note}.")


@app.command("activity-suggest")
def activity_suggest(
    db_path: Path = typer.Option(DEFAULT_DB, "--db"),
    threshold: float = typer.Option(0.10, "--threshold", help="Min cosine similarity (per-video MAX over frames) to apply a tag. Deliberately loose (below the old 0.13) because a missed tag is invisible and unfixable while an extra one is one click to drop on the review screen. Needs one pass on real footage to calibrate; this is the single knob to turn."),
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
        """Per-tag rollup the review screen reads: clip count, peak score, a sample."""
        agg: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for path, picks in results.items():
            for t, s in picks:
                agg[t].append((Path(path).name, s))
        return sorted(
            (
                {"tag": t, "clips": len(hits), "peak": round(max(s for _, s in hits), 3), "sample": hits[0][0]}
                for t, hits in agg.items()
            ),
            key=lambda m: (-m["clips"], -m["peak"]),
        )

    def _emit_complete(results: dict[str, list[tuple[str, float]]], total: int) -> None:
        sample = None
        if results:
            sp = next(iter(results.keys()))
            sample = {"file": Path(sp).name, "tags": [t for t, _ in results[sp]]}
        _emit("activity-complete", total=total, tagged=len(results), sample=sample, matched=_aggregate(results))

    videos = _db.videos_with_embeddings(conn)

    # Load the matcher eagerly so a present-but-unloadable model surfaces here
    # (the old lazy load inside apply_auto_tags was unguarded and crashed the
    # command). If the model can't load OR there are no frame embeddings to
    # score against, we CANNOT match per clip — but we must not silently drop
    # the tags the user typed. Fall back to applying every typed tag to every
    # clip (the pre-v0.0.38 behavior); the review screen still lets them prune.
    # Losing the user's tags outright is the worst outcome, so this is deliberate.
    encoder = None
    load_error = None
    if videos:
        try:
            encoder = _clip.ClipEncoder()
            encoder._load()
        except _clip.ClipUnavailable as e:
            load_error = str(e)
            encoder = None

    if encoder is None or not videos:
        reason = (
            "no frame embeddings (this library was scanned without scene analysis)"
            if not videos
            else f"the scene model could not load ({load_error})"
        )
        console.print(
            f"[yellow]Can't match per clip: {reason}. Applying your "
            f"{len(user_tags)} tag(s) to every clip instead — drop any wrong "
            f"ones on the review screen.[/yellow]"
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
        _emit("activity-fallback", reason=reason, tags=user_tags, clips=len(all_videos))
        _emit_complete(stamped, total=len(all_videos))
        return

    # Precise per-clip matching. Each user tag is its own subject and output
    # label; activity.py's templates wrap and ensemble it (the CLIP zero-shot trick).
    prompts = [(t, t) for t in user_tags]

    _emit("activity-start", total=len(videos))
    console.print(
        f"Looking for [bold]{len(user_tags)}[/bold] tag(s) "
        f"({', '.join(user_tags)}) across [bold]{len(videos)}[/bold] clip(s)…"
    )
    results = _activity.apply_auto_tags(
        conn,
        encoder,
        threshold=threshold,
        # A user's own tags: apply any that clear the absolute threshold,
        # independent of each other. No median-margin (that's for pruning a
        # big fixed list) and no top-K cap (if a clip genuinely shows 6 of
        # her tags, write all 6).
        max_tags_per_video=len(user_tags),
        use_median_margin=False,
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
    """Verify the packaged bundle can actually run: exiftool resolves and the
    MobileCLIP model loads + encodes. Run against the FROZEN binary in CI so a
    build that compiles but dies at runtime fails the release instead of
    auto-updating to every user. Exits non-zero on any failure.
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
            console.print("[red]No faces in index. Run `facetag scan` first.[/red]")
            raise typer.Exit(1)
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
                existing = _tag.read_keywords(Path(path_str))
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
):
    """Write per-face timeline markers (XMP-xmpDM:Markers) into each video.

    Premiere Pro and DaVinci Resolve render these as clip markers on the
    timeline scrubber. Each marker is one face detection: "Sarah at 0:03,
    Dad at 0:08…". Editors can click a marker to jump to that frame.
    """
    conn = _db.connect(db_path)
    videos = _markers.videos_with_named_faces(conn)
    if not videos:
        console.print("[yellow]No videos with named faces. Run `label-web` first.[/yellow]")
        raise typer.Exit(0)

    _emit("markers-start", total=len(videos))
    console.print(f"Writing markers to [bold]{len(videos)}[/bold] video(s)…")
    failed: list[tuple[str, str]] = []
    sidecar_failed: list[tuple[str, str]] = []
    last_written: tuple[Path, int] | None = None  # for the verification readback

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
            events = _markers.face_events_for_video(conn, vid)
            _emit("markers-video", name=short, count=len(events), index=idx, total=len(videos))
            try:
                _markers.write_markers(Path(path_str), events)
                last_written = (Path(path_str), len(events))
            except _markers.ExiftoolMissing as e:
                console.print(f"\n[red]{e}[/red]")
                _emit("error", stage="markers-write", message=str(e))
                raise typer.Exit(2)
            except Exception as e:
                failed.append((short, str(e)))
                _emit("markers-error", name=short, message=str(e))
            # Sidecar XMP — additive, non-fatal. DaVinci reads sidecars
            # more reliably than in-file XMP across versions; Premiere
            # users still get the in-file write above.
            try:
                _markers.write_markers_sidecar(Path(path_str), events)
            except Exception as e:
                sidecar_failed.append((short, str(e)))
                _emit("markers-sidecar-error", name=short, message=str(e))
            prog.update(task, advance=1)

    if failed:
        console.print(f"[red]{len(failed)} failure(s) writing in-file markers:[/red]")
        for n, err in failed:
            console.print(f"  [red]{n}[/red]  {err}")
    if sidecar_failed:
        console.print(f"[yellow]{len(sidecar_failed)} sidecar XMP failure(s) (DaVinci fallback):[/yellow]")
        for n, err in sidecar_failed:
            console.print(f"  [yellow]{n}[/yellow]  {err}")

    if not failed:
        # Read back markers from a sample clip and surface to the UI so
        # users see proof markers landed without opening the .mov in an
        # editor. Same shape as the tag-verified event.
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
                )
            except Exception as e:
                _emit("markers-verify-error", message=str(e))
        _emit("markers-complete", total=len(videos))
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
