"""Shared pytest fixtures.

Generates tiny throwaway .mov files via ffmpeg so the metadata tests
don't need binary fixtures committed to git. Skips ffmpeg-dependent
tests cleanly if ffmpeg isn't on PATH (e.g. in a stripped CI image).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


@pytest.fixture(scope="session")
def have_ffmpeg() -> bool:
    return _have("ffmpeg") and _have("ffprobe")


@pytest.fixture(scope="session")
def have_exiftool() -> bool:
    return _have("exiftool")


@pytest.fixture
def test_mov(tmp_path: Path, have_ffmpeg: bool) -> Path:
    """1-second 320x240 QuickTime testsrc clip. ffmpeg generates it on the
    fly so we don't bundle a binary blob. Use this as the input to any
    test that needs a real .mov for exiftool/xattr to write into."""
    if not have_ffmpeg:
        pytest.skip("ffmpeg not on PATH")
    out = tmp_path / "test.mov"
    subprocess.check_call(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi",
            "-i", "testsrc=duration=1:size=320x240:rate=30",
            "-pix_fmt", "yuv420p",
            "-c:v", "libx264",
            str(out),
        ],
        stderr=subprocess.DEVNULL,
    )
    assert out.is_file() and out.stat().st_size > 1000
    return out
