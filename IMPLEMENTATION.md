# Implementation notes — what was built, and where it departs from PLAN.md

Written for whoever reads this next. [`PLAN.md`](./PLAN.md) is the design and still
wins on reasoning; this file records **what the MVP actually is**, every deliberate
deviation, and the facts that turned up when the design met real data and real
libraries.

Nothing here reopens a closed decision. Where an MVP shortcut was taken, it is a
scope reduction with a note on what the full version needs, not a redesign.

## What exists

```
spec/                       constraint-language.md · layout.md · meta-schema.json · fixtures/
crates/json-constraint/     meet · subsumes · substitute · the JSON encoding
crates/zarr-collection/     layout, /meta attributes, zarr.group_ref, append and evolve plans
crates/constraint-views/    the view mapping language, and STAC as one instance of it
python/datacollections-py/  pyo3 bindings + the Python API (create_collection, add_item, …)
python/stac-api-backend/    a STAC API over a collection
examples/                   four domains, three with no STAC anywhere
```

Dependencies run exactly one way, as planned:

```
                    ┌── zarr-collection ◄─────────────────────────┐
json-constraint ◄───┤                                             ├── datacollections-py
                    └── constraint-views ◄───────────────────────-┘
```

`json-constraint` depends on serde and indexmap and nothing else — no zarrs, no
arrow, no DataFusion. That rule held without strain and is the extractability
guarantee.

Test suites: **55 Rust tests** (`make test-rust`) including the fixture conformance
suite and property tests for the three laws, and **53 Python tests**
(`make test-python`) across the bindings, the store, the query layer, the pandera
translation, the STAC API and the four examples. `make test` runs both.

## Deliberate deviations from PLAN.md

**1. The Rust crates do no IO; the storage driver is Python.**
PLAN.md puts `add_item` and `evolve_schema` in `zarr-collection` over zarrs and
icechunk. Here `zarr-collection` computes *plans* — which columns must exist, what a
row contains, which columns a widening must backfill — and `python/datacollections`
executes them over zarr-python and icechunk-python.

Why: the ingest ecosystem the plan itself points at (xarray, VirtualiZarr) is
Python, and the interesting half of both write paths is the plan, not the store
calls. The layout decisions stay in Rust where they can be tested without a store,
and the transaction shape stays in one readable Python function. The cost is that a
Rust-only consumer gets the layout logic but not a writer; adding one later is
additive, since `AppendPlan` and `EvolvePlan` are exactly what it would consume.

**2. `zarr-collection-query` does not exist as a crate.** Query is
`python/datacollections/query.py`: read the columns, build an Arrow table carrying
the extension type and the constraint where a planner looks for them, hand it to
DataFusion. The upstream `zarr-datafusion-search` remains the right home for a real
`TableProvider`, and the plan already says this layer should shrink toward zero. What
is here exercises the claim that matters — the constraint is a **planner input read
off the table's schema** — without vendoring a git dependency into an MVP. No
predicate pushdown, no lazy chunk reads; it materialises the table.

**3. The STAC API is plain FastAPI, not stac-fastapi.** Same routes, same response
shapes, a tenth of the dependency surface. `Backend` is written so that hosting it
under stac-fastapi later means passing it as the client class. pystac-client as the
acceptance test was likewise replaced by direct assertions on the response shapes a
client depends on.

**4. Bindings pass JSON text across the FFI boundary.** No pythonize/serde-pyobject
dependency; `serde_json::Value` in, `serde_json::Value` out, `json.dumps` on the
Python side. Negligible at these document sizes, and it keeps the binding layer from
having to track two object models. If profiling ever says otherwise, the change is
local to `python/datacollections-py/src/lib.rs`.

**5. Property tests use a seeded xorshift, not proptest.** The crate's selling point
is that it has almost no dependencies. A deterministic seed sweep is reproducible in
a way shrinking is not, and the generators here (abstract a random document at three
levels of looseness) produce exactly the related-constraint ladders the subsumption
laws need.

**6. Domains are checked by hand-written code, not a JSON Schema validator.** The
v1 domain language is `type` + `minimum` + `maximum` with no enums — about forty
lines. Pulling in a validator crate to evaluate three keywords was not worth it. The
**meta-schema is** real JSON Schema (`spec/meta-schema.json`) and is exercised from
Python with `jsonschema`, which is where the plan wanted it.

## Facts that turned up, which PLAN.md could not have known

**Icechunk does not support Zarr consolidated metadata.**
`zarr.consolidate_metadata(store)` raises `TypeError` on an `IcechunkStore` —
Icechunk's own manifest already makes children cheap to enumerate. So a member's
description is *derived* by walking the group's children (flat, one level in v1)
rather than read from a stored consolidated key. **The one-document-per-group
decision survives intact** — still one document, still no hierarchy walking at
validate time — it is simply assembled by the reader. This also answers the M1 open
question "does zarrs/VirtualiZarr write consolidated metadata?" for the Icechunk
substrate: the question is moot there.

**VirtualiZarr 2.5.1 has no TIFF/COG parser.** `virtualizarr.parsers` ships
`FITSParser`, `HDFParser`, `NetCDF3Parser`, `ZarrParser`, `DMRPPParser` and the
kerchunk parsers. So the "one ingest path for all examples via VirtualiZarr" plan
does not yet reach Sentinel-2. Members are written **metadata-only** instead — the
array exists at full shape and dtype with no chunks — through
`encoding["materialize"] = False`. `add_item` already routes a Dataset containing
`ManifestArray`s to VirtualiZarr's Icechunk writer, so the virtual path is wired,
just unused by the COG example.

**MAST-U's store, confirmed and then some.**
`s3://mast/level1/shots/<shot>.zarr` is **Zarr v2** with per-diagnostic subgroups
(`/amc/plasma_current`). The plan's guess was right that a shot is not flat — and
one step further: **signal availability varies within a diagnostic too**, so
(shot, diagnostic) would still have needed optionality. The example takes
**(shot, diagnostic, signal)**. This is the plan's own most transferable lesson,
applied one level deeper than it predicted, and it is the single most useful thing
this implementation learned: *the fix for apparent inexpressiveness was again a finer
referenced unit, not a language feature.*

## The one design gap that had to be filled to build anything

**How extra column values are supplied.** PLAN.md marks this "blocks M2's API" and
reserves the decision. The MVP answer, which is **provisional and flagged rather than
decided**:

```python
coll = create_collection(repo, constraint=None, extra_columns=[ExtraColumn("shot", "int64")])
coll.add_item(ds, extras={"shot": 30420})
```

Declared at creation with a name and a dtype, supplied per member at `add_item`.
Chosen because it is the minimum that works and because extra columns are by
definition **not** recomputable from the member's group, so nothing else *could*
supply them. What it does not do: derive an extra column from the member (a `bbox`
computed from the attributes, say), which is the obvious next ask and would want a
declared derivation rather than a caller-supplied value. Left open on purpose.

## Known gaps, in the order they would matter

- **Backfill reads every member serially.** Correct, and O(N) as designed, but a
  single-threaded loop. The plan's note about consolidated metadata at the store root
  making backfill one read rather than N is untested — and moot until the
  node-count investigation, since Icechunk has no consolidated metadata anyway.
- **`add_item` does one commit per member.** Fine at 100 members; a bulk path that
  batches N members into one transaction is the obvious optimisation and does not
  change any layout decision.
- **No concurrent-writer testing.** Member ids need no coordination by construction,
  but two writers appending rows to the same columns will conflict in Icechunk, and
  nothing here explores that.
- **`check(ds)` is the pre-check, so it is approximate.** It reports mismatches on
  dims, shapes, dtypes and attributes only. The authoritative check needs the group
  written, which is exactly what the two-phase design says.
- **Wildcard columns are JSON text.** Fine, and honest, but it means a query engine
  cannot look inside them. That is the deliberate meaning of a wildcard.
- **The Python `Collection` reads the whole table for `rows()` and `verify()`.** No
  streaming, no projection pushdown.
