# Docs & Website Update Guide

Audience: any agent or human editing this repo's documentation, README, or the
product site (`docs/index.html`). Read this **before** touching those surfaces.
Goal: every future update looks and reads like it was written by the same hand.

---

## 1. Non-negotiables

1. **Work on `main`.** Never push any `internal/*` branch. Verify with
   `git status --short --branch` before and after your work.
2. **Commit only what you touched.** Parallel sessions often leave
   work-in-progress in the tree. Always `git add <explicit paths>` — never
   `git add -A` / `git add .`.
3. **Run the public-hygiene scan before every commit** (§8). No internal
   machine paths, no private project names, no personal handles in examples.
4. **Never present unbuilt as built.** Every claim carries a status from the
   ladder in §4. When implementation and docs disagree, docs are wrong — fix
   the docs in the same change or don't ship the claim.
5. **The site stays one self-contained file.** `docs/index.html` makes zero
   external requests: no CDN scripts, fonts, analytics, or remote images.
   Inline everything; favicon is an inline SVG data URI.
6. **Verify before you report.** After pushing site changes, poll
   `https://phenomenoner.github.io/a2a-superhub/` until HTTP 200 and spot-check
   the changed text. "Pushed" is not "live".

## 2. Documentation map — who owns which truth

Higher layers are **more executable**; when layers conflict, the higher layer
wins and the lower layer must be corrected (or carry an explicit amendment
note, like the one at the top of `docs/DESIGN.md`).

| Layer | Files | Role |
|---|---|---|
| 1 · Executable | `schemas/*.json`, `tests/`, `src/` | The contract as code. Never edited to match prose — prose matches them. |
| 2 · Contract docs | `docs/M0_CONTRACT_DECISIONS.md`, `docs/MEMORY_API.md`, `docs/MEMORY_SECURITY.md`, `docs/A2A_COMPATIBILITY.md`, `docs/PACKAGING.md`, `docs/ext/` | Ratified, implementation-facing decisions. Status headers state exactly what is implemented vs absent. |
| 3 · Evidence | `docs/M*_EVIDENCE.md` | Frozen per-milestone proof (baseline commit, scope, test results). Append new files; never rewrite old ones. |
| 4 · Conceptual RFC | `docs/DESIGN.md` | The narrative design. Corrected by layers 1–3 via its amendment block. |
| 5 · Marketing summary | `README.md` | Compressed pitch + status table + quickstart. Everything here must be defensible from layers 1–4. |
| 6 · Product site | `docs/index.html` | The most compressed layer. Same claims as README, fewer words. |
| 7 · GitHub metadata | About, topics, milestones, pinned issues, labels | Positioning surface. Update via `gh` when layers 5–6 shift. |

Reference docs `docs/API.md`, `docs/ADAPTERS.md`, `docs/SECURITY.md` sit
between layers 2 and 5: factual, no marketing voice.

## 3. Status sync map — "X changed, update Y"

When a milestone lands (or scope changes), update **all** of these in one
change, in this order:

| # | Surface | Exact location |
|---|---|---|
| 1 | Contract doc status header | e.g. `docs/MEMORY_API.md` first paragraph — implemented vs absent list |
| 2 | Evidence file | new `docs/M<x>_EVIDENCE.md` with baseline commit + scope |
| 3 | `docs/DESIGN.md` | `Status:` line + amendment block at top |
| 4 | `README.md` | badges (line ~9–12) · "Two planes" status table · memory-plane section heading + intro · Roadmap list |
| 5 | `docs/index.html` | hero kicker (`.kicker`, "doc-first v2 rfc open") · status strip (`.strip`) · section tags (`.tag.ship` / `.tag.rfc`) · memory-plane `<h2>` label · roadmap cards (`.ms`) · terminal mock if the demoed API changed |
| 6 | GitHub | close/edit milestone, tick tracking-issue checklist, update About text if the one-liner changed |

A grep that finds most stale status strings:

```bash
grep -rniE "rfc open|design rfc|not implemented|remain(s)? absent|planned|shipped" \
  README.md docs/DESIGN.md docs/index.html docs/MEMORY_API.md
```

## 4. Status ladder (the only allowed labels)

| Label | Meaning | Visual |
|---|---|---|
| ✅ Shipped | Implemented, tested, on `main`, evidence file exists | green check / `.tag.ship` |
| 🧱 Foundation (opt-in) | Implemented behind a flag/extra; scope stated precisely | amber, named milestone (e.g. "M1B, opt-in") |
| 📐 Design RFC | Public design, not built | amber `.tag.rfc`, always links to `docs/DESIGN.md` |
| 🗺 Planned | Named on roadmap only | plain text, no badge |

Rules: a surface may compress wording but never promote a label upward.
Scope creep in labels ("M1 done" when only M1A/B landed) is the #1 drift bug —
name the sub-milestone.

## 5. Website design system (`docs/index.html`)

### 5.1 Design tokens (CSS custom properties in `:root`)

| Token | Value | Semantic meaning — do not repurpose |
|---|---|---|
| `--bg` | `#07090e` | page background |
| `--panel` / `--panel2` | `#0d1117` / `#10161f` | section & card backgrounds |
| `--line` / `--line2` | white @ 9% / 16% | hairline borders |
| `--text` / `--muted` / `--dim` | `#e6edf3` / `#98a3b3` / `#6b7687` | text hierarchy |
| `--cool` | `#22d3ee` | **coordination plane, v1, shipped things** |
| `--warm` | `#f5b944` | **memory plane, RFC/design things, primary CTA** |
| `--violet` | `#a78bfa` | graph/federation accents, gradient partner |
| `--green` | `#4ade80` | shipped checkmarks only |

The cool/warm duality is the brand: *cool pipes, warm memory*. New sections
about coordination use `--cool` kickers; memory/design sections use `--warm`.
Gradients are always warm→violet (headline highlight) or cool→blue (buttons).

### 5.2 Typography & layout

- System font stacks only (`--sans`, `--mono`). No webfonts, ever.
- `--mono` is for: kickers, tags, terminal/code, tiny metadata. Kickers are
  lowercase-styled via `text-transform:uppercase` + `letter-spacing:.18em`.
- Display sizes use `clamp()`; body stays ~1rem/1.6.
- Section anatomy, in order: `.kicker` (eyebrow) → `h2` → `.lead` (max-width
  ~46rem) → content grid. Every section follows this; don't invent new anatomy.
- Container: `.wrap` (max-width 1080px). Breakpoints: 900px (3→2 col),
  620px (→1 col, nav links hide). Wide content must scroll inside its own
  `overflow-x:auto` box (see `.tbl`), never the page.

### 5.3 Component inventory (reuse before inventing)

| Class | Use |
|---|---|
| `.card` (+`.ing` top-border variant) | feature/ingredient cards in `.grid.g3` / `.g2` |
| `.tag.ship` / `.tag.rfc` | status chips beside `h2` |
| `.term` | terminal mock (bar + dots + `<pre>` with `.cmt/.day/.p/.s/.w/.g` spans) |
| `.codebox` | file sample with header path (`.hd`) and YAML highlight spans `.y-*` |
| `.strip` | horizontal status facts bar |
| `.tl` + `.ev` | the Monday/Thursday two-panel story |
| `.flow` | arrow-chip pipeline |
| `.prin` | 4-up aphorism grid |
| `.tbl` | comparison table (hero row = `.hero-row`) |
| `.road` + `.ms` | roadmap milestone cards |
| `.btn` / `.btn.primary` (warm) / `.btn.cool` | CTAs — primary action is always warm |

### 5.4 Motion contract (learned the hard way)

Reveal-on-scroll uses `.rv`, gated on `html.js` (set by an inline `<script>` in
`<head>`), with an IntersectionObserver **plus a ~1.2s safety timer** that
force-adds `.on` to everything. Invariants:

- No JS → everything visible (CSS hides nothing without `html.js`).
- Observer never fires → safety timer shows everything anyway.
- `prefers-reduced-motion` → no animation.

**Animation must never gate content visibility.** If you add animated
elements, they join the existing `.rv` mechanism; do not add new hide-by-default
styles.

### 5.5 Stability rules

- Keep the favicon, `<title>` shape, and og: meta stable across edits; update
  og:description only when positioning genuinely changes.
- `docs/.nojekyll` must remain (Pages serves files as-is).
- All internal links point at `github.com/phenomenoner/a2a-superhub/...`
  absolute URLs (the site and the repo render from different roots).

## 6. Voice & copy style

**Language:** public surfaces (repo docs, README, site, issues) are English.

**The formula: tension + honesty.** Open with the pain, sharp and concrete;
resolve with what exists; label what doesn't (§4). The reader should feel the
itch and trust the label.

Signature devices — reuse them, don't dilute them:

- The hook: *"Your agents collaborate. Then they forget everything."*
- The scenario: **Monday 09:12 / Thursday 03:40** — one concrete story
  (Agent A learns about Agent B's gateway; offline B catches up). Extend this
  story rather than inventing parallel ones.
- The feedback ask: *"the most useful replies start with 'this breaks when…'"*
- Aphorisms (principles strip): "Markdown is the truth — burn the index, keep
  the memory." / "Verbatim in, intelligence out." / "Offline is a feature." /
  "Local-first, zero API keys." New aphorisms need the same shape: ≤6 words of
  claim, one line of consequence.
- Positioning line: "Memory frameworks remember *users*. Superhub gives peer
  agents a shared *past*." / "peers, not children".

Claims discipline:

- Numbers are **budgets or measured evidence** — say which ("target", "budget",
  "measured in `M1A_EVIDENCE.md`"). No adopted benchmark numbers from other
  projects without attribution.
- Competitors: factual, linked, respectful. Keep the courtesy line under the
  comparison table ("Respect to all of the above…"). Compare stated scope,
  never quality.
- Em-dash-heavy, short-sentence rhythm; second person for the reader's pain,
  first person plural sparingly.

## 7. Update recipes

### A. Milestone landed (most common)

1. Read the milestone's evidence + contract doc headers to learn exact scope.
2. Walk the sync map (§3) top to bottom. Small diffs, no rewrites.
3. Site: usually only strip/tags/roadmap-cards/kicker change. Keep the hero
   hook unless positioning changed.
4. QA (recipe C), hygiene scan (§8), commit, push, verify live.
5. `gh`: tick checklist boxes on the tracking issue, close the milestone if
   complete, adjust About text if the one-liner moved.

### B. New documentation page

1. Place it in the map (§2): contract doc? evidence? reference?
2. Match the header pattern of its layer (contract docs open with a `Status:`
   scope paragraph; evidence opens with baseline commit + date + scope).
3. Link it from `README.md` docs row and/or `docs/DESIGN.md` amendment block
   if it corrects the RFC.

### C. Site edit QA (before any push touching `docs/index.html`)

```bash
python -m http.server 8899 --directory docs   # then open localhost:8899
```

- [ ] Full scroll-through at desktop width, then ~800px and ~375px
- [ ] All 28+ `.rv` elements visible after load (check with JS disabled too,
      or temporarily remove the `html.js` script and confirm nothing hides)
- [ ] No horizontal page scroll at any width; tables scroll inside `.tbl`
- [ ] No console errors; no external network requests (DevTools network tab)
- [ ] Links resolve (RFC, issues, GitHub)
- After push: `curl -s -o /dev/null -w "%{http_code}" https://phenomenoner.github.io/a2a-superhub/`
  until 200, then spot-check the changed copy on the live page.

### D. Positioning change (hero copy, About, topics)

Rare and owner-visible. Propose the new one-liner in the PR/commit message,
change README hero + site hero + og meta + GitHub About together, and keep the
old hook unless explicitly replacing it.

## 8. Public hygiene scan

Run from repo root before every commit that touches public files:

```bash
grep -rniE "wareh[o]use|goal-n[o]tes|agent.h[a]rness|opencl[a]w|fub[o]n|phenomenoner[@]|user\.phenomen[o]ner|[A-Z]:[\\/](Users|Warehou)" \
  README.md CLAUDE.md docs/*.md docs/index.html ops-rules/ skills/ schemas/ 2>/dev/null
```

(The bracketed letters keep this command from matching its own text, so a
clean tree greps to zero hits.)

Zero hits required. Additional rules:

- Example humans are `user.jane`; example agents are neutral ids like
  `agent.alpha` / `agent.beta`. Never real instance names.
- No absolute local paths, no personal machine details, no private repo names.
- `internal/*` branches and their files never appear in public content.

## 9. Git & deploy mechanics

- Commit messages: imperative summary + short "what changed at which layer"
  body. One logical surface-sync per commit.
- GitHub Pages deploys automatically from `main` `/docs` (legacy build,
  `.nojekyll` present). No action needed beyond push; first build ≈ 1 min.
- Repo metadata changes go through `gh repo edit` / `gh api` — never hand-edit
  claims into the site that GitHub metadata contradicts (About text, topics).

## 10. Pre-push checklist (copy-paste)

```text
[ ] on main, no internal/* involved
[ ] git add used explicit paths only; diff reviewed
[ ] status labels consistent across README / DESIGN / MEMORY_API / site (§3 grep)
[ ] no claim exceeds its evidence layer (§4)
[ ] site: single-file, no external requests, .rv safe, .nojekyll intact (if touched)
[ ] hygiene scan clean (§8)
[ ] after push: live site 200 + spot-check (if site touched)
[ ] gh metadata synced: milestones / tracking issues / About (if status moved)
```
