"""Integration tests for the keyword-write path.

These guard against the v0.0.25 regression where exiftool was silently
failing to write XMP-dc:Subject + Keys:Keywords on testers' Macs and
the orchestrator swallowed every per-clip error. Now any regression
that breaks the roundtrip surfaces here.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from facetag import tag as _tag


def test_write_keywords_roundtrip_xmp_and_keys(test_mov: Path, have_exiftool: bool) -> None:
    if not have_exiftool:
        pytest.skip("exiftool not on PATH")
    names = ["Sarah", "Tom", "wedding"]
    _tag.write_keywords(test_mov, names)
    kw = _tag.read_keywords(test_mov)
    assert sorted(kw["xmp"]) == sorted(names), f"XMP-dc:Subject mismatch: {kw['xmp']}"
    assert sorted(kw["keys"]) == sorted(names), f"Keys:Keywords mismatch: {kw['keys']}"


def test_write_keywords_replace_is_idempotent(test_mov: Path, have_exiftool: bool) -> None:
    """Running write_keywords twice with the same names must not duplicate
    entries. This is the v0.0.22 bug class — exiftool's `=` + `+=` mix
    can silently dupe if the sep handling is wrong."""
    if not have_exiftool:
        pytest.skip("exiftool not on PATH")
    names = ["Sarah", "Tom"]
    _tag.write_keywords(test_mov, names)
    _tag.write_keywords(test_mov, names)
    kw = _tag.read_keywords(test_mov)
    assert kw["xmp"] == ["Sarah", "Tom"]
    assert kw["keys"] == ["Sarah", "Tom"]


def test_write_keywords_replace_then_smaller_set(test_mov: Path, have_exiftool: bool) -> None:
    """When replace=True, a smaller subsequent set must overwrite, not
    union with the previous set."""
    if not have_exiftool:
        pytest.skip("exiftool not on PATH")
    _tag.write_keywords(test_mov, ["Sarah", "Tom", "Ellie"])
    _tag.write_keywords(test_mov, ["Sarah"])
    kw = _tag.read_keywords(test_mov)
    assert kw["xmp"] == ["Sarah"]
    assert kw["keys"] == ["Sarah"]


def test_write_keywords_empty_list_is_noop(test_mov: Path, have_exiftool: bool) -> None:
    """Empty names list shouldn't crash; should just not touch the file."""
    if not have_exiftool:
        pytest.skip("exiftool not on PATH")
    _tag.write_keywords(test_mov, [])  # must not raise
    kw = _tag.read_keywords(test_mov)
    assert kw["xmp"] == []
    assert kw["keys"] == []
