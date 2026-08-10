"""Library search: filenames count, and the result cap admits itself.

In-app search already existed and is good — it matches people by name, matches
clips by keyword, and cross-references so typing "wedding" surfaces the people
who appear in wedding-tagged clips. Two things were missing.

The clip index carries each clip's filename and the search only looked at
keywords, so someone who knew a clip was IMG_0042.mov and wanted to see what
Spotted had put on it got nothing back, with the answer sitting in memory.

And the panel drew at most 200 rows under a header reporting the true total, so
a common tag on a large library read as "847 clips" above a list of 200 with
nothing saying the rest existed. Same defect as the review screen's clip cap,
in a different place.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN_TS = (ROOT / "app/src/main.ts").read_text()
INDEX_HTML = (ROOT / "app/index.html").read_text()
STYLES = (ROOT / "app/src/styles.css").read_text()


def _fn(name: str) -> str:
    start = MAIN_TS.index(f"function {name}(")
    return MAIN_TS[start:MAIN_TS.index("\n}", start)]


def test_a_filename_is_searchable():
    fn = _fn("matchingClipsForQuery")
    assert "c.name.toLowerCase().includes(query)" in fn
    assert "c.keywords.some" in fn, "keyword search must still work"


def test_the_index_already_carried_the_filename():
    """The data was there; only the filter ignored it. If the shape changes,
    this stops being true and the search silently narrows again."""
    assert "type LibraryClipIndexEntry = { path: string; name: string; keywords: string[] }" in MAIN_TS


def test_the_result_cap_is_disclosed():
    fn = _fn("renderLibraryClipSearchResults")
    assert "CLIP_SEARCH_LIMIT" in fn
    assert "showing the first" in fn
    assert "matches.length > shown" in fn


def test_the_drawn_rows_and_the_reported_count_use_the_same_limit():
    """They were 200 and matches.length, which is how the header came to
    describe a list it was not showing."""
    fn = _fn("renderLibraryClipSearchResults")
    assert "matches.slice(0, CLIP_SEARCH_LIMIT)" in fn
    assert "Math.min(matches.length, CLIP_SEARCH_LIMIT)" in fn
    assert "matches.slice(0, 200)" not in MAIN_TS, "a second literal cap would drift"


def test_a_filename_hit_shows_why_it_matched():
    """A filename match highlights no keyword chip, so without this the row
    appears in the list with nothing on it explaining why."""
    fn = _fn("renderLibraryClipSearchResults")
    assert 'c.name.toLowerCase().includes(query) ? " is-match" : ""' in fn
    assert ".library-clip-search__name.is-match" in STYLES


def test_the_header_no_longer_claims_everything_is_a_tag_match():
    """"tagged with" stopped being true the moment filenames were searched.

    Scoped to the assignment rather than the whole function: the first version
    of this test read the function body and failed on a comment that quotes the
    old wording, which is a test reporting prose as a defect.
    """
    fn = _fn("renderLibraryClipSearchResults")
    stmt = fn[fn.index("header.textContent ="):fn.index(";", fn.index("header.textContent ="))]
    assert "tagged with" not in stmt
    assert 'matching "${query}"' in stmt


def test_the_empty_state_and_placeholder_mention_filenames():
    """A search box that quietly supports something is barely better than one
    that does not."""
    fn = _fn("renderLibraryClipSearchResults")
    assert "filename" in fn
    assert "filenames" in INDEX_HTML
