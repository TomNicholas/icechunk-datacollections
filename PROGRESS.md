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

Scope is deliberately minimal: literals, variables with inline numeric-range domains,
wildcards, **flat groups only**, every member with the same set of arrays and attribute
keys.

**Constraints are authored, not inferred** — `join` is off the write path, so
anti-unification, leastness and domain synthesis are all out of v1. Inference survives as
an optional off-path `infer_constraint` tool. Authoring loses tightness, not truth: every
member is still `meet`-checked, so the constraint can never be false.

A member's description is **exactly its `zarr.json`, chunking included**, compared as-is
with no canonicalisation — sound only because every member is written by our own
`add_item`. Repeated variables must declare **identical** inline domains. Differing lists
are replaced **wholesale** by a wildcard rather than aligned element-wise, which is how v1
avoids variable-length `codecs` lists.

Still open before coding starts:

- [ ] **Does zarrs read and write Zarr v3 consolidated metadata?** Does VirtualiZarr
      write it? The one-document-per-group decision rests on both.
- [ ] **Do attribute key sets vary in practice?** v1 requires them identical across
      members. If real OME-NGFF or MAST-U data varies, the narrow fix is optionality for
      attribute keys specifically — worth knowing before M6 rather than during it.

Implementation:

- [ ] JSON encoding: parse + serialise
- [ ] Meta-schema (JSON Schema) for constraint documents, incl. identical-domain check
- [ ] `meet` / validate, with mismatch reporting good enough for user-facing errors
- [ ] `subsumes`
- [ ] `substitute`
- [ ] Wildcard leaves: match anything, store the value verbatim, reinstate on substitute

Property tests:

- [ ] `meet` accepts real members and rejects mutations of them — a few hundred actual
      OME-Zarr and Sentinel-2 `zarr.json` documents from public stores. Needs no
      DataCollections store, which is why this crate has no Zarr dependency.
- [ ] `subsumes` reflexive, transitive, antisymmetric up to equality
- [ ] `substitute` inverts **exactly** — full `zarr.json` reconstruction
- [ ] round-trip over every member: `substitute(c, bindings(m)) == description(m)`

## M2 — `zarr-collection` + Python API — blocked on substrate

- [ ] Store layout read/write
- [ ] Arrow schema from Zarr attributes, incl. `zarr.group_ref` extension type
- [ ] Constraint in `/meta` group attributes, under a cohort-keyed map
- [ ] `add_item(ds, id)` — always strict
- [ ] `evolve_schema(new_constraint)` — `subsumes` check, backfill new columns by reading
      members, one transaction
- [ ] Test: evolve-then-append leaves the store byte-equivalent to a from-scratch build
- [ ] `create_collection(store_or_session, constraint=None)` — None takes the first
      member's `zarr.json` verbatim as an all-literal constraint
- [ ] `check(ds)` — report mismatches without writing
- [ ] Cheap pre-check from the Dataset before writing the group
- [ ] Decide how extra column values are supplied — see unresolved design gaps

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
- [ ] **MAST-U** — ~100 members, unit is (shot, diagnostic) so optionality is not needed
- [ ] **HST** — primary HDU, single instrument

## M7+ — depth, only after M6

Two independent tracks.

*Language depth:*

- [ ] `infer_constraint(members)` — the optional inference tool, where `join` and
      anti-unification live, along with their leastness and domain-synthesis questions
- [ ] Optionality (`$present`) — a member may lack an array
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

## Unresolved design gaps

- [ ] **How do extra column values get supplied?** Decision 6 permits extra columns and
      the STAC view needs them (`bbox`, `datetime`), but nothing in the API passes or
      derives them. Also bears on identity: with (shot, diagnostic) as MAST-U's unit,
      querying "all diagnostics for shot 30420" wants `shot` and `diagnostic` as their
      own columns, which are extra columns rather than variables.

---

## Done

- [x] Repo initialised
- [x] `PLAN.md` — design, six layout decisions, milestones, reserved questions
- [x] `README.md` — motivation, strictness spectrum, comparison table
