"""CLI entry point. `facetag --help` to explore."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn
from rich.table import Table

from . import cluster as _cluster
from . import cut as _cut
from . import db as _db
from . import detect as _detect
from . import extract as _extract
from . import label as _label
from . import markers as _markers
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


@app.command()
def scan(
    path: Path = typer.Argument(..., exists=True, help="Video file or directory to scan."),
    db_path: Path = typer.Option(DEFAULT_DB, "--db"),
    sample_fps: float = typer.Option(1.0, "--fps", help="Frames per second to sample for face detection."),
    rescan: bool = typer.Option(False, "--rescan", help="Re-scan videos already in the index."),
    min_score: float = typer.Option(0.5, "--min-score", help="Minimum face detection confidence."),
    tags: str = typer.Option("", "--tags", help="Comma-separated batch tags applied to every clip in this scan (e.g. 'baptism,kids')."),
):
    """Walk a path, sample frames, detect faces, store embeddings."""
    videos = _extract.walk_videos(path)
    if not videos:
        console.print(f"[red]No videos found under {path}[/red]")
        raise typer.Exit(1)

    batch_tags = [t.strip().lower() for t in tags.split(",") if t.strip()] if tags else []

    conn = _db.connect(db_path)
    detector = _detect.Detector(min_score=min_score)

    console.print(f"Found [bold]{len(videos)}[/bold] video(s)")
    _emit("scan-start", total=len(videos))
    total_faces = 0
    total_skipped = 0

    for index, v in enumerate(videos, start=1):
        path_str = str(v.resolve())
        if not rescan and _db.is_scanned(conn, path_str):
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

        rows: list = []
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
                prog.update(task, advance=1, faces=face_count)
                if len(rows) >= 500:
                    _db.add_faces_bulk(conn, video_id, rows)
                    rows.clear()
            if rows:
                _db.add_faces_bulk(conn, video_id, rows)
            total_faces += face_count
        _emit("video-done", name=v.name, index=index, total=len(videos), faces=face_count)

    _emit("scan-complete", total_faces=total_faces, total_skipped=total_skipped, total_videos=len(videos))
    console.print(f"\n[bold green]Done.[/bold green] {total_faces} faces indexed, {total_skipped} videos skipped (already scanned).")


@app.command()
def cluster(
    db_path: Path = typer.Option(DEFAULT_DB, "--db"),
    epsilon: float = typer.Option(0.55, "--eps", help="Cluster selection epsilon (lower = stricter)."),
    min_size: int = typer.Option(5, "--min-size", help="Min faces per cluster."),
):
    """Group all indexed faces into person clusters."""
    conn = _db.connect(db_path)
    face_ids, embs = _db.all_embeddings(conn)
    if not face_ids:
        console.print("[red]No faces in index. Run `facetag scan` first.[/red]")
        raise typer.Exit(1)

    _emit("cluster-start", faces=len(face_ids))
    console.print(f"Clustering {len(face_ids)} faces…")
    labels = _cluster.cluster_embeddings(embs, min_cluster_size=min_size, epsilon=epsilon)
    assignments = {fid: int(lbl) for fid, lbl in zip(face_ids, labels)}
    _db.set_clusters(conn, assignments)

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
):
    """Open a one-page web labeler. See every cluster, type names, hit Save All."""
    _web.serve(db_path, thumb_dir, port=port, open_browser=not no_browser)


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


@app.command("tag-write")
def tag_write(
    db_path: Path = typer.Option(DEFAULT_DB, "--db"),
    replace: bool = typer.Option(True, "--replace/--append", help="Replace existing keywords (default) vs append."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print what would be written, change nothing."),
):
    """Write per-video person keywords into each .mov via exiftool.

    Premiere reads these as the Keywords column. DaVinci Resolve surfaces
    them in the Media Pool. After running this, an editor can search by
    person name across the whole library.
    """
    conn = _db.connect(db_path)
    mapping = _tag.videos_with_keywords(conn)
    if not mapping:
        console.print("[yellow]Nothing to tag — no named people and no batch tags. Run `label-web` first.[/yellow]")
        raise typer.Exit(0)

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
            table.add_row(short, ", ".join(names))
            _emit("tag-video", name=short, names=names, index=idx, total=len(mapping))
            if not dry_run:
                try:
                    _tag.write_keywords(Path(path_str), names, replace=replace)
                except _tag.ExiftoolMissing as e:
                    console.print(f"\n[red]{e}[/red]")
                    _emit("error", stage="tag-write", message=str(e))
                    raise typer.Exit(2)
                except Exception as e:
                    failed.append((short, str(e)))
                    _emit("tag-error", name=short, message=str(e))
            prog.update(task, advance=1)

    console.print(table)
    if dry_run:
        console.print(f"[dim]Dry run. No files changed.[/dim]")
    elif failed:
        console.print(f"[red]{len(failed)} failure(s):[/red]")
        for name, err in failed:
            console.print(f"  [red]{name}[/red]  {err}")
    else:
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
            except _markers.ExiftoolMissing as e:
                console.print(f"\n[red]{e}[/red]")
                _emit("error", stage="markers-write", message=str(e))
                raise typer.Exit(2)
            except Exception as e:
                failed.append((short, str(e)))
                _emit("markers-error", name=short, message=str(e))
            prog.update(task, advance=1)

    if failed:
        console.print(f"[red]{len(failed)} failure(s):[/red]")
        for n, err in failed:
            console.print(f"  [red]{n}[/red]  {err}")
    else:
        _emit("markers-complete", total=len(videos))
        console.print(f"[bold green]Done.[/bold green] Wrote markers to {len(videos)} clips.")


@app.command()
def status(
    db_path: Path = typer.Option(DEFAULT_DB, "--db"),
):
    """Show index summary (videos, faces, clusters, per-person clip counts)."""
    conn = _db.connect(db_path)
    n_videos = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    n_faces = conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0]
    n_clusters = conn.execute(
        "SELECT COUNT(DISTINCT cluster_id) FROM faces WHERE cluster_id IS NOT NULL AND cluster_id >= 0"
    ).fetchone()[0]
    n_named = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]

    # Per-person counts: how many distinct videos each named person appears in.
    per_person_rows = conn.execute(
        "SELECT p.name, "
        "       COUNT(DISTINCT f.video_id) AS clips, "
        "       COUNT(f.id) AS faces "
        "FROM people p "
        "JOIN faces f ON f.cluster_id = p.cluster_id "
        "WHERE p.name IS NOT NULL AND p.name != '' "
        "GROUP BY p.name "
        "ORDER BY clips DESC, p.name ASC"
    ).fetchall()
    people = [{"name": n, "clips": c, "faces": fc} for n, c, fc in per_person_rows]

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


if __name__ == "__main__":
    app()
