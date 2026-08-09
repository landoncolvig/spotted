"""Clips a cloud provider has evicted, caught before the scan instead of during.

iCloud Drive, Dropbox and OneDrive all remove a file's contents while leaving
something that still looks like a file. Spotted used to meet this one clip at a
time, deep inside a scan, as a per-clip decode failure — which reads as "my
footage is broken" rather than "these have not downloaded yet". The user's next
move is completely different in each case.

Two signals, because the providers differ. `st_blocks == 0` with a non-zero
size means the file occupies no space on this disk, whatever the directory
entry claims. A sibling `.<name>.icloud` is what iCloud leaves when it evicts a
file outright, and in that case the video path does not exist at all, so there
is nothing to stat.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from facetag import cli as _cli
from facetag import extract as _extract

runner = CliRunner()


def _real(p: Path) -> Path:
    p.write_bytes(b"\x00" * 4096)
    return p


def _dataless(p: Path) -> Path:
    """A file with a size and no blocks, the way APFS represents evicted data."""
    with open(p, "wb") as fh:
        fh.truncate(50 * 1024 * 1024)
    assert p.stat().st_blocks == 0 and p.stat().st_size > 0
    return p


def test_a_downloaded_clip_is_not_flagged(tmp_path):
    clip = _real(tmp_path / "here.mov")
    assert _extract.not_downloaded([clip]) == []


def test_an_evicted_clip_is_flagged(tmp_path):
    clip = _dataless(tmp_path / "away.mov")
    assert _extract.not_downloaded([clip]) == [clip]


def test_an_icloud_placeholder_is_flagged_even_though_the_clip_is_absent(tmp_path):
    """iCloud replaces the file with `.name.icloud`, so stat() raises and the
    naive check would call it simply missing."""
    clip = tmp_path / "evicted.mov"
    (tmp_path / ".evicted.mov.icloud").write_bytes(b"placeholder")
    assert _extract.not_downloaded([clip]) == [clip]


def test_a_genuinely_missing_clip_is_not_called_undownloaded(tmp_path):
    """Deleted and not-yet-downloaded need different advice."""
    assert _extract.not_downloaded([tmp_path / "never-existed.mov"]) == []


def test_an_empty_file_is_not_mistaken_for_a_placeholder(tmp_path):
    """A zero-byte file also has zero blocks. It is corrupt or still copying,
    which is a different problem with different advice."""
    empty = tmp_path / "empty.mov"
    empty.touch()
    assert _extract.not_downloaded([empty]) == []


def test_a_partly_downloaded_folder_still_scans_what_is_there(tmp_path):
    """The normal case. Refusing the whole drop because some clips are on a
    server helps nobody."""
    _real(tmp_path / "a.mov")
    _dataless(tmp_path / "b.mov")
    res = runner.invoke(
        _cli.app,
        ["scan", "--db", str(tmp_path / "t.db"), "--no-energy", "--", str(tmp_path)],
    )
    assert "scan-not-downloaded" in res.output
    assert '"fatal":false' in res.output
    assert "b.mov" in res.output
    # a.mov still reached the index.
    assert res.exit_code == 0, res.output


def test_a_folder_with_nothing_downloaded_stops_with_the_reason(tmp_path):
    """Running a whole scan that can only fail wastes minutes and explains
    nothing."""
    _dataless(tmp_path / "a.mov")
    _dataless(tmp_path / "b.mov")
    res = runner.invoke(
        _cli.app,
        ["scan", "--db", str(tmp_path / "t.db"), "--no-energy", "--", str(tmp_path)],
    )
    assert res.exit_code == 1
    assert '"fatal":true' in res.output
    assert "Download Now" in res.output


def test_the_message_names_clips_and_discloses_the_rest(tmp_path):
    """Tested on the partial path, which is the one that puts names in the
    message; the fatal path says "none of these N" and needs no list."""
    _real(tmp_path / "here.mov")
    for i in range(7):
        _dataless(tmp_path / f"c{i}.mov")
    res = runner.invoke(
        _cli.app,
        ["scan", "--db", str(tmp_path / "t.db"), "--no-energy", "--", str(tmp_path)],
    )
    assert '"clips":7' in res.output
    assert "and 2 more" in res.output, "a truncated list must say it is truncated"
    # The payload carries the same cap, so the Done screen can disclose it too.
    assert '"names":["c0.mov","c1.mov","c2.mov","c3.mov","c4.mov"]' in res.output


def test_the_done_screen_can_name_them():
    main_ts = (Path(__file__).resolve().parent.parent / "app/src/main.ts").read_text()
    row = main_ts[main_ts.index('label: "Not downloaded"'):]
    row = row[:row.index("},")]
    assert "lastNotDownloaded" in row
    assert "more`" in row, "truncation must announce itself"
    assert "bad: true" in row, "an empty row would warn on every clean run"
