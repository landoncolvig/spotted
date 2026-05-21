# Spotted — Product Roadmap

This is the strategic anchor for Spotted's path from one-user beta to SaaS.
Every PR-sized decision should be traceable to a phase below; if it isn't,
either the work is wrong-sized or this doc needs updating.

Last meaningful update: 2026-05-20 (transition planning).

---

## TL;DR

You've built the technical foundation. The risk now is *not* technical — it's
optimizing the engine without deciding what car you're building. The next two
weeks are about getting visibility (telemetry, customer interviews, error
reporting) so every decision after is informed rather than guessed.

Architecture pick: **local-first, cloud-optional**. Face embeddings never
leave the user's Mac unless they opt in to a paid sync feature. This is the
defensible differentiator vs Adobe / Frame.io / cloud-first competitors and
sidesteps the worst of BIPA/GDPR/CCPA biometric data exposure.

---

## Where the product is today

- Mac desktop app (Tauri 2 + Python sidecar via PyInstaller)
- Apple Silicon only
- Face detection (InsightFace + HDBSCAN clustering)
- Activity tagging (MobileCLIP-S2 via Core ML, zero-shot)
- Metadata write: XMP-dc:Subject, Keys:Keywords, Spotlight Comment, Finder
  Tags, XMP-xmpDM:Markers (in-file + sidecar .xmp for DaVinci)
- Auto-updater via GitHub releases (visible dialog flow from v0.0.27)
- One real user (Ellie) + Landon for testing
- 31 releases shipped during ~24 hours of iteration

---

## Blind spots that constrain everything

1. **No usage data.** Zero telemetry. We don't know where users drop off,
   what features they touch, or whether scans complete. Every decision is a
   guess until this lands.
2. **No customer development.** Sample size of one (Ellie, family
   videographer). Wedding, corporate L&D, newsrooms, real-estate
   videographers all have face-tag needs with different willingness to pay.
3. **macOS + Apple Silicon only.** Caps TAM at ~25% of editors before we
   start. Windows port is 2-4 weeks of work; the *decision* about whether to
   do it is 1 hour.
4. **Bundle is 720MB and growing.** Each ML feature adds weight. At ~1GB the
   download friction starts killing first-install conversions.
5. **No backup, no undo.** One bad Reset Library click = lost work = lost
   user. Acceptable in beta, unacceptable post-billing.
6. **Biometric data law exposure** once anything syncs to the cloud. BIPA
   fines run $1K-$5K per face print without consent. Staying local-only
   sidesteps this entirely.
7. **Zero automated tests.** Regressions get caught by Ellie reporting them.
   Works for one user, breaks at fifty.
8. **No marketing, no waitlist, no pricing tested.** Required before billing
   can land.

---

## Ranked improvements

Ranking heuristic: commercial-viability impact × confidence ÷ reversibility.
Low-reversibility decisions ranked higher because they constrain everything
downstream.

| #  | Item                                            | Cost      | Reversibility |
|----|-------------------------------------------------|-----------|---------------|
| 1  | Pick and commit to a target customer            | Low       | High          |
| 2  | Opt-in telemetry + error reporting              | 2-3 days  | Easy          |
| 3  | 5-10 customer interviews with non-Ellie users   | 1 week    | N/A           |
| 4  | Backup + undo for Reset Library                 | Half day  | Cheap         |
| 5  | In-app library search                           | 1-2 days  | Easy          |
| 6  | Keyboard-driven labeler                         | 1-2 days  | Easy          |
| 7  | Cluster merge UI                                | 2-3 days  | Easy          |
| 8  | Bundle diet (drop transformers, slim tokenizer) | 1 day     | Cheap         |
| 9  | Perf benchmarks on 5,000-clip library + fixes   | 2-3 days  | N/A           |
| 10 | Integration tests for metadata write path       | 1-2 days  | Cheap         |
| 11 | Windows port — yes/no/later decision            | 1h-4wk    | Hard          |
| 12 | Account system scaffold (dormant, no billing)   | 1 week    | Hard          |
| 13 | Marketing site + public messaging               | 1-2 weeks | Cheap         |
| 14 | Premiere/DaVinci/FCP companion panels           | 4-8 weeks | Worth it      |
| 15 | Cloud-optional sync (first paid feature)        | 4-6 weeks | Very hard     |
| 16 | AI clip descriptions (second paid feature)      | 2-3 weeks | Easy          |
| 17 | Shared team libraries                           | 6-10 weeks| Schema-heavy  |
| 18 | iOS companion app                               | 8+ weeks  | Defer-able    |

---

## Roadmap

### Phase A — Stop flying blind (next 4-6 weeks)

Goal: get the data needed to make every other decision, without committing to
SaaS architecture.

- [ ] **#1** Target customer pick — written one-pager
- [ ] **#2** Opt-in telemetry (PostHog, Plausible, or roll-your-own) + Sentry
- [ ] **#3** 5-10 customer interviews (recruit via /r/editors, Twitter,
      IndieHackers; offer free Spotted Pro for life as incentive)
- [x] **#4** Backup + undo for Reset Library *(in progress)*
- [x] **#8** Bundle diet (drop transformers) *(in progress)*
- [ ] **#10** Three metadata-write integration tests
- [ ] **#11** Windows port — *decision*, not work (yes/no/later)

**Phase A exit criteria**: clear customer thesis on paper, real usage data
flowing, error stream you can read, smaller bundles, regression net. Now
operating with eyes open.

### Phase B — Make the existing thing actually good (weeks 6-14)

Goal: take the product from "works for Ellie" to "works for 200 users in
production."

- [ ] **#5** In-app library search
- [ ] **#6** Keyboard-driven labeler
- [ ] **#7** Cluster merge UI
- [ ] **#9** Perf benchmark + top-3 fixes
- [ ] **#12** Account system scaffold (no billing yet)
- [ ] **#13** Marketing site + public landing page
- [ ] **#14 (start)** Premiere/DaVinci panel work (long-lead-time)

**Phase B exit criteria**: polished local product, public face, accounts that
can later become paid, editor-panel integration underway.

### Phase C — SaaS transition (weeks 14-24)

Goal: launch paid tier with one differentiated cloud feature.

- [ ] **#15** Cloud-optional sync (first paid feature)
- [ ] **#16** AI clip descriptions (second paid feature, opt-in cloud)
- [ ] Stripe integration, pricing page, free tier limits, paid tier unlock
- [ ] Legal: ToS, Privacy Policy, DPA, BIPA-safe consent flow for any face
      data going to the cloud
- [ ] Support channel (Intercom/Crisp or a thoughtful email address)
- [ ] **#17** Team libraries if Phase B customer signal points there

**Phase C exit criteria**: real SaaS with 50-200 paying users and revenue
signal that informs Phase D.

---

## SaaS transition strategy

### Architecture choice

**Pick: local-first, cloud-optional.**

Desktop stays the core. Cloud unlocks sync, backup, sharing, team. Face
**embeddings** stay local by default; cloud syncs only **names** and
**clip-to-name mappings** (much smaller compliance footprint).

Why not cloud-first:

- Your privacy story is the most defensible differentiator. Adobe and Frame.io
  can't credibly say "your face data never leaves your Mac" because their
  architecture won't allow it.
- BIPA/GDPR/CCPA exposure jumps from "trivial" to "needs a written policy,
  consent flow, deletion endpoint, DPO contact, ideally SOC 2" the moment
  face embeddings sync.
- Cloud-first puts you in a fight against Frame.io's resources. Local-first
  puts you in a category they can't enter.

### Pricing sketch (stress-test in customer interviews)

| Tier        | Price                | What's in it                                                                                                |
|-------------|----------------------|-------------------------------------------------------------------------------------------------------------|
| Free        | $0 forever           | Single Mac. Unlimited library. All current features. Watermarked exports (TBD). "Forever free for personal use." |
| Pro         | $12/mo or $120/yr    | Multi-Mac sync. Encrypted cloud backup. AI clip descriptions. Priority email support. Early access.         |
| Team        | $20/seat/mo, 3 min   | Shared library. Multi-user labeling. Audit log. SSO add-on. Dedicated Slack channel.                        |
| Enterprise  | Call us              | On-prem cloud option. SOC 2. BAA. Custom integrations. NLE-vendor partnerships.                             |

These numbers are **placeholders**. Lock them after Phase A customer
interviews.

### Compliance considerations

- BIPA (Illinois) is the strictest US law. Penalties $1K-$5K per face print
  collected/stored/used without written consent.
- Texas (CUBI) and Washington also have biometric laws; California CCPA
  covers biometrics indirectly.
- EU GDPR Article 9 classifies biometric data as "special category" with
  explicit consent + DPO + DPIA requirements.
- **As long as Spotted is local-only, none of this binds us** (the user is the
  controller; we're a tool).
- The day any face data syncs to our servers, we need:
  - Written consent flow with model & purpose disclosure
  - Deletion endpoint (account → cloud → all face data wiped within 30 days)
  - Privacy policy + DPA template
  - Eventually SOC 2 if any enterprise asks

---

## Open questions only the founder can answer

These constrain everything downstream and can't be answered from inside the
codebase. Lock them before Phase B starts.

1. **Who is the target customer at scale?**
   Wedding/event videographers? Family historians? Corporate L&D editors?
   News producers? Real-estate videographers? Pick one for the first year;
   let everyone else self-serve. Different choice = different roadmap order,
   different pricing, different marketing copy.

2. **Mac-only or cross-platform?**
   Mac-only is a brand and a price floor (Mac users pay more, expect more
   polish). Cross-platform is 4x the engineering and a different brand. Pick
   before you build sync (the sync architecture is platform-aware).

3. **How much of your time is this getting?**
   "Build SaaS on the side" and "SaaS is the day job" demand different
   roadmaps. Side-project: cut #14 (NLE panels), #17 (team), #18 (mobile)
   from Phase C and slow everything else by 2x.

4. **What's the budget for cloud + tools?**
   Sentry, Stripe, marketing site hosting, customer support tooling, GPU/
   storage for cloud features — call it $200-500/mo at launch and growing
   with adoption. If funding from cash flow, this constrains growth rate.

---

## Out-of-scope (deliberately not on the list)

Things I considered and intentionally left off. Documenting the misses keeps
them from sneaking back in:

- **Browser-based version** — undermines the local-first positioning, large
  rewrite, and we've not validated demand.
- **Custom face-detection model training** — InsightFace is good enough for
  90% of use cases; model improvement is a research project, not a product
  one.
- **Object/scene segmentation beyond classification** — MobileCLIP gives us
  "is there a dog in this video"; segmentation ("where in the frame is the
  dog") is much heavier and not asked for.
- **Audio analysis / transcription** — interesting adjacent space but a
  whole separate product. Note for later: if customer interviews surface
  "find clips where someone says X," revisit.
- **Real-time tagging** — Spotted is batch-oriented. Live processing during
  shoots is a different product.

---

## Pointer index for current state

- Architecture: `app/` (Tauri 2 shell), `facetag/` (Python pipeline), `sidecar/` (PyInstaller bundling)
- Auto-updater: `app/src-tauri/src/lib.rs:run` and `app/src/main.ts:wireUpdaterEvents`
- Activity detection: `facetag/clip.py`, `facetag/activity.py`
- Metadata write path: `facetag/tag.py` (XMP/Keys), `facetag/finder.py` (xattrs), `facetag/markers.py` (timeline markers)
- Schema: `facetag/db.py`
- Labeler UI: `facetag/web.py`
- Release pipeline: `.github/workflows/release.yml`
