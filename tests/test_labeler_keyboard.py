"""Naming clusters without reaching for the mouse.

Naming a few hundred clusters is the slowest thing anyone does in Spotted, and
it was mouse-driven. The header advertised "Tab to advance" and Tab did not
even work properly: the hide button sits before the name field inside each
card, so Tab from one name landed on the NEXT card's "×" — two presses per
card, the first parked on a control that discards the cluster if you hit Space
or Enter.

So the button leaves the tab order, Tab goes name to name as advertised, Enter
does the same without leaving the home row, and ⌘⌫ replaces the button for the
keyboard.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from facetag import db as _db
from facetag import web as _web

ROOT = Path(__file__).resolve().parent.parent
WEB_PY = (ROOT / "facetag/web.py").read_text()


@pytest.fixture
def page(tmp_path):
    """A rendered labeler page with two clusters, so tab order is observable."""
    conn = _db.connect(tmp_path / "t.db")
    clip = tmp_path / "clip.mov"
    clip.write_bytes(b"")
    vid = _db.add_video(conn, str(clip), 2.0)
    for cid in (1, 2):
        for i in range(3):
            conn.execute(
                "INSERT INTO faces(video_id,timestamp_sec,embedding,cluster_id) "
                "VALUES(?,?,X'00',?)", (vid, float(i), cid),
            )
    conn.commit()
    conn.close()
    app = _web.create_app(tmp_path / "t.db", tmp_path / "thumbs")
    _web._install_guard(app, "TOK")
    return app.test_client().get("/?k=TOK&view=all").get_data(as_text=True)


def test_tab_reaches_the_name_field_not_the_discard_button(page):
    """The whole defect in one assertion: the button is rendered before the
    input, so without this Tab stops on it first."""
    assert page.count('class="hide-btn"') >= 2, "fixture should render 2 cards"
    for btn in re.findall(r"<button[^>]*hide-btn[^>]*>", page):
        assert 'tabindex="-1"' in btn, "hide button is still in the tab order"


def test_the_button_still_works_for_the_mouse(page):
    """Out of the tab order, not disabled."""
    for btn in re.findall(r"<button[^>]*hide-btn[^>]*>", page):
        assert "disabled" not in btn
        assert "data-cluster=" in btn


def test_the_hint_describes_what_the_keyboard_actually_does(page):
    """It advertised Tab while Tab landed on the wrong control."""
    assert "Enter" in page
    assert "to hide" in page


def test_enter_saves_before_moving_on():
    """Advancing without flushing the pending debounced save loses the name
    someone just typed."""
    handler = WEB_PY[WEB_PY.index('if (e.key === "Enter")'):]
    handler = handler[:handler.index("return;")]
    assert "clearTimeout(saveTimers.get(" in handler
    assert "saveOne(card)" in handler
    assert "focusRelative(input" in handler


def test_shift_enter_goes_back():
    handler = WEB_PY[WEB_PY.index('if (e.key === "Enter")'):]
    handler = handler[:handler.index("return;")]
    assert "e.shiftKey ? -1 : 1" in handler


def test_the_last_card_does_not_trap_focus():
    """focusRelative returns false at the end; blurring flushes the save and
    lets the user reach Done."""
    handler = WEB_PY[WEB_PY.index('if (e.key === "Enter")'):]
    handler = handler[:handler.index("return;")]
    assert "input.blur()" in handler


def test_navigation_skips_cards_the_filter_has_hidden():
    """Typing in the filter box hides cards with style.display. Tabbing into
    one the user cannot see would be worse than not moving at all."""
    fn = WEB_PY[WEB_PY.index("function nameInputs()"):]
    fn = fn[:fn.index("\n}")]
    assert 'style.display !== "none"' in fn


def test_hiding_by_keyboard_replaces_the_button_it_removed():
    """Taking the button out of the tab order without this would leave
    keyboard users no way to discard a noise cluster."""
    handler = WEB_PY[WEB_PY.index('e.key === "Backspace"'):]
    handler = handler[:handler.index("\n  }")]
    assert "hideCard(card.dataset.cluster, card)" in handler
    assert "after.focus()" in handler, "focus must survive the card disappearing"


def test_the_helpers_are_defined_before_the_handler_runs():
    """saveTimers is a const, so calling it from a handler defined earlier in
    the file would throw at the first keystroke rather than at load."""
    for name in ("async function hideCard", "const saveTimers", "async function saveOne"):
        assert WEB_PY.index(name) < WEB_PY.index("// Naming a few hundred clusters")


def test_the_filter_box_is_left_alone():
    """It is an input too, and hijacking Enter there would break filtering."""
    handler = WEB_PY[WEB_PY.index("// Naming a few hundred clusters"):]
    guard = handler[:handler.index('if (e.key === "Enter")')]
    assert 'classList.contains("filter")' in guard
