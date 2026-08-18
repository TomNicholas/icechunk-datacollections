# Progress

Task state only. [PLAN.md](./PLAN.md) holds the design and the reasoning — when the two
disagree, PLAN.md wins and this file is stale.

**Next up:** `spec/constraint-language.md`, then M1. Both are unblocked; everything from
M2 waits on the substrate question.

---

## Reserved for Tom

Not to be decided as a side effect of implementation work. If something below appears to
need an answer, reduce its scope instead.

- [ ] **Serialisation substrate** — Zarr-in-Icechunk, or Iceberg/Parquet tracked by
      Icechunk lower down? Upstream of layout decisions 1, 5 and 6, so it gates
      `spec/layout.md` and all of M2+.
- [ ] **Nullability** — narrowed: schema evolution no longer needs it, so only
      genuinely-missing *source* values remain.

---

## M0 — spec + fixtures

- [ ] `spec/constraint-language.md` — JSON encoding + lattice semantics — **unblocked**
- [ ] JSON Schema meta-schema for constraint documents
- [ ] Constraint-language fixtures — **unblocked**
- [ ] `spec/layout.md` — *blocked on substrate*
- [ ] Store fixtures — *blocked on substrate*

## M1 — `json-constraint` — unblocked, start here

Scope is deliberately minimal: literals, variables with domains, optionality, **flat
groups only**. No nesting, cardinality, `$expr`, or cohorts.

Domain language is tiny by design: `join` synthesises only literals, numeric ranges and
bare types. **No enums** (categorical variation is a cohort) and **no synthesised
patterns** — both would make "least generaliser" ambiguous. Authors may declare patterns;
`join` preserves or discards but never invents.

A group's description is **exactly its `zarr.json`, chunking included**, compared as-is
with no canonicalisation — sound only because every member is written by our own
`add_item`.

Still open before coding starts:

- [ ] **Presence columns are a third column kind.** An optionality flag is not a `$var`,
      so "variables ⊆ columns" does not cover it. Small spec gap in layout decision 6.
- [ ] **Does zarrs read and write Zarr v3 consolidated metadata?** Does VirtualiZarr
      write it? The one-document-per-group decision rests on both.

Implementation:

- [ ] JSON encoding: parse + serialise
- [ ] Leaf domains delegated to a JSON Schema validator
- [ ] `meet` / validate
- [ ] `join` (anti-unification)
- [ ] `substitute`
- [ ] `subsumes` — only as far as testing `join` needs

Property tests, which are as much the deliverable as the code:

- [ ] `join` commutative, associative, idempotent, absorbing
- [ ] `join` over a set of instances validates every one of them
- [ ] `join` yields the **least** generaliser, not merely a generaliser
- [ ] `substitute` inverts abstraction
- [ ] fold `join` over a few hundred real OME-Zarr and Sentinel-2 groups. Note this does
      **not** depend on M6: it needs only consolidated-metadata JSON pulled from public
      stores, which is exactly why `json-constraint` has no Zarr dependency.

## M2 — `zarr-collection` + Python API — blocked on substrate

- [ ] Store layout read/write
- [ ] Arrow schema from Zarr attributes, incl. `zarr.group_ref` extension type
- [ ] Constraint in `/meta` group attributes, under a cohort-keyed map
- [ ] Append path (member satisfies the constraint)
- [ ] Widening append: `join`, backfill new column by reading groups, one transaction
- [ ] Test: widening append leaves the store byte-equivalent to a from-scratch build
- [ ] `create_collection(store_or_session, constraint=None)`
- [ ] `add_item(ds: xr.Dataset, id, allow_schema_evolution=False)`
- [ ] `would_evolve(ds)`
- [ ] Cheap pre-check from the Dataset before writing the group
- [ ] Rejection message = the `join` diff

## M3 — query — blocked

- [ ] Wire to `zarr-datafusion-search`

## M4 — views + STAC mapping — blocked

- [ ] Projection mapping: constraint + row → target JSON
- [ ] STAC Item derivation
- [ ] Round-trip property test against real STAC Items
- [ ] Decide: true geometry, or bbox-approximate `intersects` declared in `/conformance`

## M5 — STAC API backend — blocked

- [ ] stac-fastapi backend over the Python bindings
- [ ] Keyset pagination; decide token contents vs the Sort extension
- [ ] pystac-client as the acceptance test

## M6 — four examples, flat, ~100 groups each — the real milestone

Breadth across domains at shallow depth. This is what proves the factoring.

- [ ] **OME-Zarr** — FOV at level 0 as the referenced unit (implement first)
- [ ] **Sentinel-2 L2A / STAC** — full-resolution level only
- [ ] **MAST-U** — ~100 shots
- [ ] **HST** — primary HDU, single instrument

## M7+ — depth, only after M6

Two independent tracks.

*Language depth:*

- [ ] Nested groups
- [ ] Variable cardinality (`$each` / `$count`) — unlocks multiscale and overviews
- [ ] `$expr` leaves; decide the minimum grammar
- [ ] Cohorts; decide whether they nest
- [ ] Optional declarations for extra columns

*Scale:*

- [ ] Read the existing Icechunk node-count analysis — cheap, and worth doing early
      since the bad outcome is "pause the project"
- [ ] Empirical benchmarking beyond ~100 groups
- [ ] Any resulting upstream Icechunk work

---

## Upstream (separate track)

- [ ] PR to `zarr-datafusion-search`: build Arrow extension types from array attributes,
      replacing the `if name == "bbox"` special case. Small, self-contained, useful to
      the maintainers regardless.
- [ ] Nullability upstream — **on hold** until the substrate question resolves, since the
      right ask depends on the answer.

## Unknowns to check — facts, not decisions

- [ ] MAST-U store structure: signals directly under the shot group, or per-diagnostic
      subgroups? Determines the flat referenced unit. Needed before that example.
- [ ] crates.io availability of `json-constraint` — needed before publishing
- [ ] FITS-via-kerchunk reader status in VirtualiZarr — needed before HST

---

## Done

- [x] Repo initialised
- [x] `PLAN.md` — design, six layout decisions, milestones, reserved questions
- [x] `README.md` — motivation, strictness spectrum, comparison table
