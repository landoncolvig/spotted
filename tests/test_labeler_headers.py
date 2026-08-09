"""Response headers and the second CSRF gate on the labeler.

The token guard already refuses anything that cannot present the per-session
token, which is most of what matters. What it cannot cover is the token
leaking: it rides in the iframe URL's query string because an <img> has no way
to send a header. These tests cover what was added for that case — an Origin
check the browser will not let a page forge, `Referrer-Policy: no-referrer` so
the token-bearing URL never leaves in a Referer, and a nonce CSP so injected
markup cannot execute even if escaping is missed somewhere.

The one that matters most is the last test in this file. The labeler is loaded
in an iframe by a parent on the `tauri://localhost` scheme. If a future change
adds `frame-ancestors` or `X-Frame-Options`, WebKit may refuse to render the
frame, and the naming step — the step where the whole product happens — goes
blank with nothing in the UI to explain it.
"""

from __future__ import annotations

import pytest

from facetag import db as _db
from facetag import web as _web


@pytest.fixture
def client(tmp_path):
    _db.connect(tmp_path / "t.db").close()
    app = _web.create_app(tmp_path / "t.db", tmp_path / "thumbs")
    _web._install_guard(app, "TOK")
    return app.test_client()


def _csp(resp) -> dict[str, str]:
    raw = resp.headers["Content-Security-Policy"]
    return {c.split()[0]: " ".join(c.split()[1:]) for c in raw.split(";") if c.split()}


def test_a_forged_origin_cannot_write_even_with_the_token():
    """The case the token alone does not cover: token leaked, then replayed
    from a page the user has open. Browsers set Origin and will not let script
    override it."""
    import pathlib
    tmp = pathlib.Path(__import__("tempfile").mkdtemp())
    _db.connect(tmp / "t.db").close()
    app = _web.create_app(tmp / "t.db", tmp / "thumbs")
    _web._install_guard(app, "TOK")
    c = app.test_client()
    assert c.post("/hide/1?k=TOK", headers={"Origin": "https://evil.example"}).status_code == 403


def test_the_pages_own_origin_still_writes(client):
    resp = client.post("/hide/1?k=TOK", headers={"Origin": "http://localhost"})
    assert resp.status_code != 403


def test_a_non_browser_caller_is_governed_by_the_token_alone(client):
    """No Origin means not a browser. Requiring one would break the CLI and
    every test, and would buy nothing: a non-browser can set any header."""
    assert client.post("/hide/1?k=TOK").status_code != 403


def test_a_non_ascii_token_is_refused_not_a_crash(client):
    """hmac.compare_digest raises TypeError on non-ASCII str, so comparing as
    str would turn a hostile query string into a 500."""
    assert client.get("/?k=é").status_code == 403


def test_the_token_url_never_leaves_in_a_referer(client):
    assert client.get("/?k=TOK").headers["Referrer-Policy"] == "no-referrer"


def test_the_html_does_not_reach_the_disk_cache(client):
    """The page embeds the session token in a JS constant."""
    assert "no-store" in client.get("/?k=TOK").headers["Cache-Control"]


def test_script_runs_by_nonce_not_by_being_inline(client):
    resp = client.get("/?k=TOK")
    csp = _csp(resp)
    assert csp["default-src"] == "'none'"
    assert "'unsafe-inline'" not in csp["script-src"]
    assert csp["script-src"].startswith("'nonce-")


def test_the_pages_own_script_and_style_carry_that_nonce(client):
    """A nonce policy that does not match the page is just a blank labeler."""
    resp = client.get("/?k=TOK")
    nonce = _csp(resp)["script-src"].removeprefix("'nonce-").removesuffix("'")
    body = resp.get_data(as_text=True)
    assert nonce and "__NONCE__" not in body
    assert f'<script nonce="{nonce}">' in body
    assert f'<style nonce="{nonce}">' in body


def test_each_response_gets_a_fresh_nonce(client):
    first = _csp(client.get("/?k=TOK"))["script-src"]
    second = _csp(client.get("/?k=TOK"))["script-src"]
    assert first != second


def test_nothing_stops_the_app_from_framing_the_labeler(client):
    """See this module's docstring. Removing this test is not the fix."""
    resp = client.get("/?k=TOK")
    assert "X-Frame-Options" not in resp.headers
    assert "frame-ancestors" not in resp.headers["Content-Security-Policy"]


def test_a_real_card_loads_nothing_the_policy_would_block(tmp_path):
    """`default-src 'none'` blocks every resource type not named explicitly, so
    the policy is only safe while the page loads nothing but its own images.
    Rendered with an actual cluster, because the empty library renders no cards
    and would make this vacuous."""
    import re

    db_path = tmp_path / "t.db"
    conn = _db.connect(db_path)
    clip = tmp_path / "clip.mov"
    clip.write_bytes(b"")
    vid = _db.add_video(conn, str(clip), 2.0)
    conn.execute(
        "INSERT INTO faces(video_id,timestamp_sec,embedding,cluster_id) "
        "VALUES(?,?,X'00',7)", (vid, 0.0),
    )
    conn.commit()
    conn.close()
    thumbs = tmp_path / "thumbs"
    thumbs.mkdir()
    (thumbs / "cluster_0007.jpg").write_bytes(
        bytes.fromhex("ffd8ffdb0043000806") + b"\x00" * 32 + bytes.fromhex("ffd9")
    )

    app = _web.create_app(db_path, thumbs)
    _web._install_guard(app, "tok")
    body = app.test_client().get("/?k=tok&view=all").get_data(as_text=True)

    srcs = re.findall(r'<img[^>]+src="([^"]+)"', body)
    assert srcs, "no cards rendered; this check would prove nothing"
    for src in srcs:
        assert src.startswith("/"), f"img-src 'self' would block {src}"

    # A nonce covers <script> and <style> elements. It does not cover style=""
    # or onclick="" attributes — those need 'unsafe-inline', which would defeat
    # the policy. So the page must not use them.
    assert not re.search(r'\sstyle="', body), "inline style attribute would be blocked"
    assert not re.search(r'\son[a-z]+="', body), "inline handler would be blocked"
    for tag in ("<link", "<form", "@import", "url(", "<iframe", "srcset"):
        assert tag not in body, f"{tag} needs a directive the policy does not grant"
