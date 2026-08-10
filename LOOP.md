# Spotted — autonomous work queue

The 30-minute loop reads this file each tick, does ONE item, ships it, and
updates the queue. Ordered: tester feedback beats everything, then whatever is
at the top of the backlog.

Shipped through **v0.0.94** (2026-08-06). Tester: Ellie. Silent since v0.0.94.

---

## Every tick, in order

1. **Check for tester feedback.** `~/.claude/pm/inbox/imessage/*.jsonl` (today
   and yesterday) for anything about Spotted. If there is any, that is the
   tick's work. Nothing else.
2. Otherwise take the **top unchecked item** below.
3. Implement it. Small enough to finish and ship in one tick; if it isn't,
   split it here and do the first slice.
4. `bash scripts/preflight.sh` — must print ALL GREEN. Never tag otherwise.
5. Bump `app/package.json`, `app/src-tauri/tauri.conf.json`,
   `app/src-tauri/Cargo.toml` together (preflight checks they agree).
6. Commit, push, `git tag vX.Y.Z && git push origin vX.Y.Z`.
7. `bash scripts/watch_release.sh X.Y.Z` — must print SHIPPED.
8. Tick the item here, commit the queue update.
9. If the change is something Ellie would notice, text her (sign `-AI`).
   Silent internal work does not earn a text.

**Do not** touch the two-frame Resolve offset without a Resolve-proven source
mapping from Ellie's real artifact. **Do not** guess at R3D decoding; that is
blocked on her sample + REDCINE-X install.

---

## Security / hardening

- [x] **Lock the main window's CSP.** (v0.0.95) Was `"csp": null` — no policy
      at all. Now `script-src 'self'`, no eval, no wildcards, with `asset:`
      and `data:` in img-src for person and activity thumbnails,
      `ipc: http://ipc.localhost` for invoke, and `frame-src` pinned to the
      labeler origin. `tests/test_webview_csp.py` pins the policy against the
      frontend that needs it — including a guard that fails if `LABEL_PORT`
      and the `frame-src` port ever drift apart, since that would render the
      naming step as an empty iframe.
- [x] **Stop the sidecar capability accepting arbitrary args.** (v0.0.96)
      Removed the whole `shell:` grant rather than narrowing it to an argument
      allowlist, which the queue originally called for. The allowlist was
      unnecessary: every sidecar spawn happens in Rust via
      `app.shell().sidecar(...)`, which performs no scope check — the scope is
      only read by the plugin's `execute`/`spawn` IPC commands, and the
      frontend never imports the shell plugin at all. So the grant was pure
      surface. Verified against the compiled ACL, not just the source.
      `tests/test_capability_scope.py` pins it, including the premise: if the
      frontend ever does import the shell plugin, the test fails and says to
      scope the permission deliberately rather than restore `args: true`.
- [x] **Labeler needs Origin checking and a CSP.** (v0.0.97) The queue was out
      of date on one point: the labeler already had a per-session token, and
      that closes ordinary CSRF on its own. What it could not cover is the
      token leaking — it rides in the iframe URL's query string, because an
      `<img>` has no way to send a header. So: an `Origin` check on writes
      (browsers set it and will not let a page forge it, absent means a
      non-browser caller and the token stands alone), `Referrer-Policy:
      no-referrer` so that token-bearing URL never leaves in a `Referer`,
      `no-store` on the HTML that embeds the token, `compare_digest` on the
      token compare, and a nonce-based CSP with `default-src 'none'`.
      Deliberately no `frame-ancestors` and no `X-Frame-Options`: the parent
      is `tauri://localhost` and WebKit cannot be relied on to match a custom
      scheme, and a blank naming step is worse than what it would prevent.
      `tests/test_labeler_headers.py` covers it, including a rendered card,
      since `default-src 'none'` is only safe while the page loads nothing but
      its own images.
- [ ] **Signing key passphrase is empty.** `~/.tauri/spotted.key` and the
      `TAURI_PRIVATE_KEY` Actions secret have no passphrase, so the key file
      alone is enough to sign an update for the whole fleet. Needs a
      passphrase + `TAURI_KEY_PASSWORD` secret. **Ask Landon before doing
      this** — it rotates the update-signing key and mis-sequencing it breaks
      the auto-updater for anyone on an older build.
- [ ] **Ad-hoc signing + dyld entitlements.** BLOCKED on disk space, see the
      Blocked section. Partly answered: `disable-library-validation` is
      definitively required and must stay. Whether the other two can go is
      still unproven, so do not ship a reduction on the strength of the note
      below.

## Deferred features

- [x] **Partial-failure Done screen.** (v0.0.98) `tag-write` exits non-zero if
      any single clip fails, so 1 bad clip in 107 rendered identically to all
      107 failing — "Couldn't finish" — which sends someone back to re-run a
      batch that was fine. The per-clip detail was already on the wire and the
      frontend was discarding it in a `console.warn`. Now kept and shown:
      "Tagged 106 of 107 clips", plus the failing filenames (capped at 8, and
      the cap announces itself). `tag-skip` was emitted by the sidecar and
      missing from the TS union entirely, so it fell through the switch; a
      test now compares emitted `tag-*` events against declared ones. The
      partial count comes from the sidecar's own tally, never from counting
      the per-clip events, which would understate damage if the sidecar died
      partway.
- [x] **Energy tagging opt-out.** (v0.0.99) A checkbox on the tags screen, on
      by default. The trap: `scan --no-energy` alone looks like a complete
      opt-out and is not, because `energy_bucket` and the peak rows persist in
      the index, so a clip scored on an earlier drop keeps them. Wired to the
      scan only, the checkbox would appear to work on a fresh folder and do
      nothing on a re-drop, which is the state a real library is usually in.
      So it acts at all three stages: `--no-energy` (stop computing),
      `--no-energy-keywords` (stop writing the keyword), `--no-energy-markers`
      (stop the peak cues, and skip the peak lookup that decides which clips
      are in the marker set at all). Kept off `--exclude-tags` deliberately —
      exclusions are persisted as review rejections and would have deleted a
      user's own tag if they had typed "high energy".
- [x] **Energy REVIEW step.** (v0.0.100) The review screen now has an Energy
      section: a row per level with clip count, a sample name and thumbnails,
      each unchecked individually. `activity-suggest` emits `energy-summary`
      alongside `activity-complete`, read off the same captured stdout.
      Two things that would have made it nearly invisible: `activity-suggest`
      returns early when the user typed no tags, so the emit had to go BEFORE
      that bail; and the frontend skipped the review whenever no tags matched,
      which is the normal state for someone who never typed any. Unchecking a
      level drops its keyword AND its peak cues, which needs gating in two
      places, because a clip can enter the marker set through its named faces
      and would otherwise collect energy cues on the way past.
- [x] **Name autocomplete in the labeler.** (v0.0.101) Two halves, because
      autocomplete alone only lowers the odds. The labeler now suggests every
      name already in the library via a native `<datalist>` (no script, which
      matters under the page's nonce CSP), and `merge_clusters_by_name` groups
      case- and whitespace-insensitively, so a second spelling stops mattering
      when someone types it anyway. The surviving row takes the spelling of
      the largest cluster; without settling that, the merge would be invisible
      in the keyword written into the file, which is the only place the user
      sees the name. Suggestions are library-wide even though the labeler is
      batch-scoped, since reusing a name from an earlier drop is the point.
- [x] **Scan ETA.** The queue was stale: the scan already had one
      (`etaSuffix`, "12 of 107 · about 3 min left"). The complaint behind it
      was still true one phase later, so (v0.0.102) the WRITE phase got the
      same treatment — it showed a filename over a moving bar with no position
      and no estimate, and it runs exiftool twice plus xattr twice per clip, so
      it is the long phase where a hang would matter most because it is the one
      touching the user's files. Both phases now share one estimator. The write
      phase times itself rather than reusing the scan's clock, which starts
      before the labeler and would date from whenever the user began typing
      names. `tag-skip` also gained a position, so a run of containers that
      cannot hold keywords stops leaving the bar parked.
- [x] **Per-clip pruning in the activity review.** (v0.0.103) Each shown clip
      is now its own reject button. The selection rule is the part worth
      remembering: the clips offered are the tag's WEAKEST matches, not its
      strongest. Showing top scorers is right for a preview and wrong for
      pruning, since the strongest matches are the ones most likely correct
      and someone opening the row is hunting the ones to remove. Capped at 6
      with the row saying how many it is not showing, because a user who
      prunes the six shown and sees the tag still land on 40 clips would
      reasonably conclude pruning does not work. Rejections travel as a JSON
      file rather than argv: the values are filesystem paths and there is no
      separator they cannot legally contain.
- [ ] **Prune more than the six shown** (second slice of the item above). No
      way yet to reach the clips past the cap without dropping the tag
      entirely.
- [x] **Report export.** (v0.0.104) "Save report" on the Done screen writes a
      CSV: clip, folder, people, tags, energy, peak count, keywords written,
      still-on-disk. It reports `videos.spotted_keywords` — what Spotted last
      actually WROTE into the file — not what it intended to write. Those
      differ whenever a clip failed or its container could not hold keywords,
      and a report showing intent would be worse than none, since being
      checkable after the fact is the entire point. Scoped to the batch the
      Done screen just described, except after Re-tag Library.
- [x] **13 sidecar events reached nobody.** (v0.0.105) All triaged, allowlist
      now empty and must stay that way. Four were failures the user needed and
      now show on the Done screen: `finder-error` (the important one — the
      keyword write can succeed while the Finder tag and Spotlight comment
      fail, so the run reported clean while Finder search quietly could not
      find those clips), `markers-skip`, `energy-skip`, `index-prune-error`.
      Two were progress for the embedding backfill, which runs on any library
      first scanned before activity tagging existed and showed nothing at all
      while it ran. The other seven are console-only deliberately, and
      declared so that being console-only is a decision rather than an
      oversight.
- [x] **iCloud pre-check.** (v0.0.106) Checked once, up front, instead of
      meeting evicted clips one at a time inside the scan as per-clip decode
      failures — which reads as "my footage is broken" rather than "these have
      not downloaded yet", and the user's next move differs completely. Two
      signals, because providers differ: `st_blocks == 0` with a non-zero size
      (an APFS dataless placeholder), and a sibling `.<name>.icloud`, which is
      what iCloud leaves when it evicts a file outright — the video path then
      does not exist at all, so there is nothing to stat. A partly-downloaded
      folder scans what is there and names the rest; a folder with nothing
      downloaded stops with the reason rather than running a pass that can
      only fail. Guarded against two false positives: a genuinely deleted clip
      and a zero-byte file both need different advice.

## Roadmap (multi-tick — split before starting)

- [x] **Backup + undo for Reset Library.** Already existed — the third stale
      queue item. Reset renames `~/.facetag` aside rather than deleting, keeps
      3, and Restore Last Backup rolls it back behind a confirm. What was
      actually missing (v0.0.107): the restore dialog named the backup
      `.facetag.backup-1754774400`, so at the one moment that matters — about
      to overwrite the current library — the user could not tell whether it
      was from five minutes or three weeks ago. Now it says the date and the
      age. Two latent problems fixed alongside: backups were sorted as TEXT on
      a Unix-seconds suffix, so "newest" was resting on every stamp having the
      same digit count (true until 2286, so never live, but not something
      restore should rest on); and `.facetag.pre-restore-*` copies were
      excluded from the backup listing by design, which also meant nothing
      ever deleted them — a library's worth of thumbnails per restore.
- [ ] **Opt-in telemetry + error reporting.** Ranked #2. Blind on where users
      drop off. Local-first product, so this must be opt-in and say what it
      sends.
- [x] **In-app library search.** Already existed — the FOURTH stale item. It
      matches people by name, clips by keyword, and cross-references so
      "wedding" surfaces the people in wedding-tagged clips. Two real gaps
      fixed (v0.0.108): the clip index carries each filename and the search
      ignored it, so someone who knew a clip was IMG_0042.mov got nothing back
      with the answer already in memory; and the panel drew 200 rows under a
      header reporting the true total, so a common tag on a large library read
      as "847 clips" above a list of 200 with nothing saying the rest existed
      — the same cap-masquerading-as-the-whole-set defect as the review
      screen. A filename hit now highlights the filename, since it lights up
      no keyword chip and the row would otherwise appear unexplained.
- [x] **Keyboard-driven labeler.** (v0.0.109) This one was NOT already built,
      and it was worse than missing. The header advertised "Tab to advance"
      while the hide button sits before the name field inside each card, so Tab
      from one name landed on the NEXT card's "×" — two presses per card, the
      first parked on a control that discards the cluster if you hit Space or
      Enter. The button is out of the tab order now (still mouse-clickable),
      Tab goes name to name as advertised, Enter does the same without leaving
      the home row, Shift+Enter goes back, and ⌘⌫ replaces the button for the
      keyboard. Enter flushes the pending debounced save first, or advancing
      would lose the name just typed. Navigation skips cards the filter has
      hidden.
- [x] **Bundle diet.** (v0.0.110) The ROADMAP's stated action — drop
      transformers, slim the tokenizer — was already done (fifth stale item),
      so the weight was elsewhere. buffalo_l has five models and `detect.py`
      loads two: `allowed_modules=["detection","recognition"]`. The other three
      were downloaded by every user and skipped at load with "model ignore" —
      `1k3d68.onnx` 143.6MB, `2d106det.onnx` 5.0MB, `genderage.onnx` 1.3MB, so
      ~150MB of a ~682MB download for nothing. v0.0.41 stopped them being
      LOADED; it never stopped them being SHIPPED. The spec now bundles the two
      by name. `tests/test_bundle_contents.py` fails if `allowed_modules` and
      the bundled list ever drift — the reverse mistake (asking for a module
      whose model is absent) is the one that breaks face detection outright.

## Blocked

- **R3D / RED direct support** (DAYTA-23) — needs a real .R3D sample from
  Ellie and confirmation REDCINE-X is installed on her machine.
  `scripts/redline_probe.sh` reports what REDline can do once she has it.
- **Two-frame Resolve offset** — needs a Resolve-proven source mapping from
  her real artifact.
- **Trimming the dyld entitlements** — needs disk space on this machine. The
  only honest way to test is against the real frozen sidecar, and the way to
  get one without CI is to download the shipped `Spotted.app.tar.gz`, re-sign
  the sidecar with a reduced entitlement set, and run its `selftest`. That
  needs roughly 3GB free: 690MB for the bundle and about a gigabyte more for
  PyInstaller's unpack plus CoreML's model compilation scratch. The volume sat
  at 99% (2-3GB free) and the runs became unreproducible — the same config
  passed at 3.0GB free and failed at 2.4GB with
  `Error compiling model: I/O error`, which reads like an entitlement problem
  and is not one.

  What the runs did establish, because it fails instantly and identically
  regardless of disk: **`disable-library-validation` is required.** Without it
  the sidecar dies before any Python runs, at
  `dlopen(Python.framework)` → *"mapping process and mapped file
  (non-platform) have different Team IDs"*. That is the entitlement the file's
  comment actually justifies.

  Unproven: `allow-unsigned-executable-memory` and
  `allow-dyld-environment-variables`. One clean run passed the full selftest
  with only `disable-library-validation`, and it could not be reproduced under
  disk pressure. One observation is not enough to change what the whole fleet
  auto-installs. Note also that `selftest` exercises the sidecar only — the
  Tauri parent shares the same entitlements file and cannot be tested here
  without launching the GUI.

## Waiting on Ellie

v0.0.94 fixes she has not confirmed: face thumbnails in the naming step,
dead frames on 24+30fps batches (timeline now 120fps, deliberately), squashed
portrait frames degrading detection, labeler "failed to start" at 200+ clips.

---

## Log

- 2026-08-09 — queue created, loop restarted after a 2-day gap.
- 2026-08-09 — v0.0.95: main-window CSP locked down. No tester text; nothing
  she can see changed, and a CSP is only news if it broke something.
- 2026-08-09 — v0.0.96: dropped the webview's shell grant entirely. No tester
  text, same reason.
- 2026-08-09 — v0.0.110: bundle diet, ~150MB off the download. Last ROADMAP
  item. Backlog is now empty except the two things parked on Landon.
- 2026-08-09 — v0.0.109: keyboard labeler. First ROADMAP item in five that
  was genuinely missing, and the advertised shortcut actively led somewhere
  destructive.
- 2026-08-09 — v0.0.108: library search was already built; fixed filenames
  and the silent 200-row cap. FOURTH stale item. My own test failed on my own
  comment — it asserted a phrase was absent from the function and the comment
  explaining the change quoted it. Scoped the assertion to the statement.
- 2026-08-09 — v0.0.107: backup/undo was already built; fixed what was
  missing around it. Third stale queue item, so the remaining ROADMAP entries
  are worth verifying before trusting them.
- 2026-08-09 — v0.0.106: iCloud pre-check. Three of my own tests asserted
  pretty-printed JSON; `_emit` uses compact separators. Fixed the assertions,
  not the emit.
- 2026-08-09 — v0.0.105: triaged all 13 silent events. TypeScript caught me
  collecting `index-prune-error` without surfacing it, which is the same
  mistake in miniature — the compiler noticed a value that reached nobody.
- 2026-08-09 — v0.0.104: report export. Generalised the event-declaration
  guard from `tag-*` to ALL events, which immediately surfaced 13 pre-existing
  events the frontend silently drops. Pinned them in an allowlist that cannot
  grow, and queued the triage rather than widening this tick.
- 2026-08-09 — v0.0.103: per-clip pruning. The event-declaration test
  strengthened last tick immediately earned it: it caught two new emits
  (`tag-pruned`, `tag-prune-error`) that I had added without declaring in the
  TS union — the exact v0.0.98 `tag-skip` bug repeating in my own work.
  `tag-prune-error` now surfaces on the Done screen, since a prune that
  silently failed means the user's unchecked tags went into their files.
- 2026-08-09 — v0.0.102: write-phase position + ETA. Preflight caught a test
  of mine from v0.0.98 that anchored on `_emit("tag-skip"` as one line, so
  wrapping the call read as the sidecar having stopped emitting it. Second
  time a literal-anchored test has mis-reported a formatting change as a
  defect; both are now whitespace-tolerant, and fixing it also un-weakened a
  neighbouring regex test that had silently stopped covering tag-skip.
- 2026-08-09 — v0.0.101: name autocomplete + case-insensitive person merge.
  This one retroactively repairs existing libraries: any already-split person
  is consolidated the next time the labeler's Done button runs the merge.
- 2026-08-09 — v0.0.100: energy review screen. Closes the asymmetry — nothing
  Spotted decides about someone's footage now reaches their files unseen.
- 2026-08-09 — v0.0.99: energy opt-out. Split the queue item: the opt-out
  shipped, the review screen is still open and re-queued above.
- 2026-08-09 — entitlement trimming attempted and NOT shipped. Proved
  `disable-library-validation` is required; could not get a reproducible
  result on the other two because this machine's disk is at 99%. Moved to
  Blocked with the method written down. No release.
- 2026-08-09 — v0.0.98: partial-failure Done screen. First tick whose change
  Ellie would actually see, but not texting her: she has not run a batch since
  2026-08-03, and this only shows up when a clip fails.
- 2026-08-09 — v0.0.97: labeler response headers + Origin gate. Preflight
  earned its keep here: the CSP nonce attribute broke the test that
  node-syntax-checks the labeler's JavaScript, which anchored on a literal
  `<script>`. Fixed the anchor, then re-broke the JS on purpose to confirm the
  test still catches what it was written for. No tester text.
