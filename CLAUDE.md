# CLAUDE.md — working rules for this repo

A2A Superhub: durable A2A coordination hub + shared memory plane for
heterogeneous AI agents. Public repo; product site deploys from `main` `/docs`
to https://phenomenoner.github.io/a2a-superhub/.

## Before editing docs, README, or the website

**Read [`ops-rules/docs-and-website-update-guide.md`](ops-rules/docs-and-website-update-guide.md) first.**
It defines the design tokens, voice, status ladder, truth hierarchy, and the
"X changed → update Y" sync map. Docs/site edits that skip it will drift.

## Hard rules (apply to every change)

1. Work on `main`. Never push any `internal/*` branch or its content.
2. `git add` explicit paths only — parallel sessions leave WIP in the tree.
3. Run the public-hygiene grep (guide §8) before every commit. Zero hits.
4. Never label unbuilt work as shipped; use the status ladder (guide §4) and
   keep all surfaces in sync (guide §3).
5. `docs/index.html` stays a single self-contained file: no external requests,
   animations must never gate content visibility.
6. After pushing site changes, verify the live URL returns 200 and shows the
   new copy before reporting done.

## Verify

```bash
python -m unittest discover -s tests    # core suite
python -m http.server 8899 --directory docs   # site QA (guide §7C)
```

Truth order when docs disagree: `schemas/` + `tests/` > contract docs
(`docs/MEMORY_API.md` etc.) > `docs/DESIGN.md` (RFC + amendment block) >
`README.md` > `docs/index.html` > GitHub About/metadata.
