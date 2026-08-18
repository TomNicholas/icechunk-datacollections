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
examples/                   microscopy, geospatial, fusion, astronomy
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

Test suites: **61 Rust tests** (`make test-rust`) including the fixture conformance
suite and property tests for the three laws, and **64 Python tests**
(`make test-python`) across the bindings, the store, the virtual ingest path, the
query layer, the pandera translation, the STAC API and the four examples. `make
test` runs both.

## Measured costs, at the ~100-member cap

`python scripts/timings.py -n 100`, on a laptop, with metadata-only members. The
point is the *shape*, which is what the design predicts, not the absolute numbers:

```
100 members, 7 columns, store 0.9 MB

append (O(1) expected):        median 14 ms, first 10 ms, last 17 ms
evolve_schema (O(N) expected):
  at  25 members:   35 ms, 25 groups read   (1.4 ms/member)
  at  50 members:   63 ms, 50 groups read   (1.3 ms/member)
  at  75 members:   89 ms, 75 groups read   (1.2 ms/member)
  at 100 members:  112 ms, 100 groups read  (1.1 ms/member)

verify() over 100 members: 0.25 s
SQL over the whole table:  263 ms (cold DataFusion context, whole table materialised)
```

Appends are flat and evolutions are linear in existing members, at about
1.2 ms per member read. So the bimodal latency the plan predicts is real and
visible, and at this size an O(N) widening is still cheap in absolute terms. This is
**not** the Icechunk node-count investigation — it stays inside the cap and says
nothing about 10⁴ members.

Running the examples at their full 100 takes seconds to a minute each, dominated by
the source APIs rather than by anything here — the ingest itself is ~14 ms a member.

One thing the full-size runs did expose: `verify()` was O(N²), because each
`describe` re-read the whole table to find one row. Reading the columns once
instead took it from ~30 s to 0.25 s at 100 members. Worth noting *because* it only
showed up at the cap: at the 20–40 members the test suite uses, it looked fine.

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

**2. `zarr-collection-query` is not a crate — it is `query.py` over upstream's
provider.** Since revised: `query.py` now registers **`zarr-datafusion-search`'s own
`ZarrTableProvider`**, through their published Python bindings, so the scan is theirs
— lazy chunk reads and projection pushdown included. A DataCollections store turned
out to be readable by them unmodified, which is what made the swap a dozen lines
rather than a port. `EXPLAIN` shows `FFI_ExecutionPlan: ZarrExec` with the projection
narrowed to the columns the query names.

It is not a Rust crate because it does not need to be: they publish wheels, so
adopting them costs this workspace no Rust dependency at all.

Three things remain ours, each with a note saying when it goes:

- **The self-description.** Their schema builder reads array names and dtypes only,
  so the constraint and `zarr.group_ref` are dropped in transit;
  `attach_self_description` puts them back. Deletable the day the two upstream PRs
  land — there is a test that should keep passing when it goes.
- **A fallback**, now unused by anything here. Upstream *hard-errors* on a column
  named `bbox` that is not Zarr bytes — it does not merely skip the extension type.
  Our Sentinel-2 example had exactly that, so **all four example stores now name the
  column `bbox_wgs84`** and every one of them reads through upstream. The STAC Item
  still has a `bbox` field; only the column name changed. `create_collection` warns
  (`UnreadableByUpstream`) if anyone declares `bbox` again, at the point where
  renaming is free. The fallback stays for stores we did not write.
- **The DataFusion pin.** `datafusion==53.*`, because their wheel is built against 53
  and any other major **segfaults** across the FFI boundary instead of raising.

**What renaming defers, precisely.** Bounding-box *search* still works — the STAC API
filters on the column, so `pystac-client.search(bbox=…)` returns the right Items.
What is deferred is pushing that filter into the scan: upstream indexes a WKB
geometry column via `/indexes/<column>`, and writing one needs a **`binary` dtype in
our layout**, which does not exist (`Dtype` is int64/float64/bool/string). Adding it
plus the geoarrow extension metadata is the real fix, and it is a layout change
rather than a rename — hence deferred rather than done in passing.

**3. ~~The STAC API is plain FastAPI, not stac-fastapi.~~ Withdrawn — it is
stac-fastapi.** The first version hand-rolled the routes, justified by dependency
surface. That justification did not survive being questioned: `stac-fastapi.api`,
`.types` and `.extensions` install in seconds, and the six-method `BaseCoreClient`
maps straight onto the `Backend` that was already there.

Hosting on the reference implementation was worth it immediately, in two concrete
ways rather than in principle:

- **It validates our responses** (`enable_response_models=True`), which caught a
  `bbox` being served as a JSON *string* — the hand-rolled routes had been passing
  it through happily, and a real client would have choked. `ExtraColumn` now takes
  `encoding="json"` so such a column decodes to a real list, the same mechanism a
  wildcard's column uses.
- **It knows the spec better than I do.** Validation rejected an `/items` page for
  lacking a `collection` link at the FeatureCollection level — a requirement the
  hand-rolled version did not know existed.

The acceptance test is now the one PLAN.md asks for: **pystac-client against a live
uvicorn server**, opening the API, checking conformance for itself, paging through a
search, and filtering by datetime, bbox and ids.

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

**COGs virtualize through `virtual-tiff`, not through VirtualiZarr itself.**
`virtualizarr.parsers` ships FITS, HDF, NetCDF3, Zarr, DMRPP and kerchunk parsers but
no TIFF one; the separate `virtual-tiff` package provides `VirtualTIFF`, and it works
on the Sentinel-2 COGs. **The Sentinel-2 example is therefore genuinely virtual**:
each member's group holds chunk references into the public COGs, real pixels read
back out of it, and **4.3 GB of imagery is addressed by a 0.10 MB store**. That is
the ingest path PLAN.md wants, on a real archive.

Three things it taught us, all recorded in the example:

- `virtual-tiff` carries the TIFF/GeoTIFF tags through as array attributes, and they
  genuinely differ per tile (`model_tiepoint` is the scene origin). A **third**
  domain vocabulary the language constrains in position and declines to interpret,
  alongside OME's and IMAS's — and the pre-check caught it correctly on the second
  member.
- A COG's `codecs` list is whatever the file uses, which is precisely the wildcard's
  job: replaced wholesale, stored verbatim, reinstated exactly.
- **Reading a member requires its codecs to be registered.** Without
  `import virtual_tiff.codecs`, zarr cannot parse the array metadata at all —
  `UnknownCodecError`. That is the same must-understand argument that made
  `zarr.group_ref` an Arrow extension type rather than a Zarr dtype, seen from the
  other side.

The metadata-only path (`encoding["materialize"] = False`) remains for MAST-U and
HST, whose sources are Zarr v2 and requester-pays FITS respectively, and for the
offline test runs.

**MAST-U's store, confirmed and then some.**
`s3://mast/level1/shots/<shot>.zarr` is **Zarr v2** with per-diagnostic subgroups
(`/amc/plasma_current`). The plan's guess was right that a shot is not flat — and
one step further: **signal availability varies within a diagnostic too**, so
(shot, diagnostic) would still have needed optionality. The example takes
**(shot, diagnostic, signal)**. This is the plan's own most transferable lesson,
applied one level deeper than it predicted, and it is the single most useful thing
this implementation learned: *the fix for apparent inexpressiveness was again a finer
referenced unit, not a language feature.*

**Writers add their own attributes, and that sharpens the "we own the writer"
argument.** Writing a member through VirtualiZarr's Icechunk writer produces a group
carrying `coordinates: time` and a `_FillValue` on the coordinate array — keys the
source Dataset never had. Our own `write_group` adds neither. Two consequences:

- The **pre-check had to be made conservative**. It originally trusted attribute key
  sets, and so *false-rejected* virtual members: it refused a member the
  authoritative post-write check would have accepted. A pre-check that produces false
  rejections is worse than no pre-check, so it now keeps only value mismatches on
  structural leaves plus a key-set mismatch on the *array* set, which the Dataset does
  determine.
- More importantly, this is the `zarr.json`-as-is risk made concrete. Two writers
  disagree on attributes for the same logical member, so **a collection whose members
  came from different writers would need `evolve_schema` for a difference that is not
  a difference in the data at all.** PLAN.md files "ingesting groups written by
  someone else" as a v2 problem; this is what it will look like in practice, and it
  shows up between two writers we already use rather than at some foreign store.

The virtual path itself works: `add_item` on a Dataset of `ManifestArray`s writes
chunk references through VirtualiZarr, and there is a test for it
(`tests/test_virtual.py`) using the Zarr parser over a local source store. It also
surfaced one ergonomic sharp edge worth knowing: Icechunk's virtual chunk container
*name* must be a prefix of the reference URLs VirtualiZarr writes (`file:/…`), not
merely a descriptive label.

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

## Three bugs found by running against real things, all fixed

Recorded because both are the kind that only appear on real data, and one of them
was a genuine threat to the project's central claim.

**0. Our writer skipped all-fill chunks, and upstream's reader requires them.**
zarr-python does not write a chunk whose values are all the fill value. A MAST-U
collection whose `units` column is empty for every member therefore produced
`chunk cannot be found for key meta/units/c/0` when read through upstream's
provider — an interop failure invisible to every test we had, because our own reader
is happy with the fill value. `/meta` writes now set `write_empty_chunks`, which is
what upstream's own ingest does. Note it has to be set at *write* time: it is a
runtime array config, not part of `zarr.json`, so it does not survive reopening the
array to append to it.

**1. serde_json's float parser was one ULP out.** Three HST members failed
`verify()`: an `EXPTIME` attribute of `1305.8754880000001` came back from the store
as `1305.875488` — adjacent doubles. serde_json's default parser is permitted that;
the `float_roundtrip` feature is what forbids it, and it is now on. For a project
whose claim is that a constraint plus a row reconstructs the member's `zarr.json`
*exactly*, a ULP is not a rounding detail — it is the claim failing, silently, on
about 3% of real archive metadata. There is a regression test with the awkward
values, and `spec/constraint-language.md` §3.1 now states that equality is
JSON-*value* equality and that implementations must parse floats exactly.

**2. A demoted column could not be filled, so appends stopped working.** Evolving a
constraint so that it no longer declares a hole demotes its column to an extra
(layout decision 6: tightening is free, the column is kept). But extras are
caller-supplied, so the *next* `add_item` demanded a value for a column the caller
never chose and could not know. The fix keeps the plan's intent and closes the trap:
a demoted column records a **`source_pointer`** — the position in the description its
value came from — and stays recomputable. This is the "optional declarations for
extra columns" idea PLAN.md files as a likely later addition, arriving early because
the alternative was either data loss or a null.

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
- **The Python `Collection` reads the whole table for `rows()` and `verify()`.** One
  pass, not N passes — but still no streaming and no projection pushdown, so both are
  O(N) in memory as well as time.
