# DataCollections — plan

A self-contained demo of queryable, self-describing collections of Zarr groups,
factored so each component can be extracted into its own repo later without a
refactor.

**Status:** planning. This document is the working design; edit freely.

## Goal

Store a large collection of references to Zarr groups (millions of rows) alongside
a machine-readable description of *what is consistent and what varies* across
those groups, such that:

- the collection is queryable with SQL (via DataFusion),
- the per-group description is derivable from the stored constraint plus per-row values,
- STAC is one optional *view* over the data, not the storage format,
- the same machinery works for domains with no STAC vocabulary at all
  (microscopy, genomics).

## Vocabulary

Used precisely throughout; worth fixing before reading further.

| term | meaning |
|---|---|
| **member** | one thing in the collection. The unit `add_item` adds, and one row of the table. |
| **group** | the Zarr group at `/groups/<id>` that a member's data lives in. One per member. |
| **row** | a member as seen from the metadata table. Same thing, storage-side view. |
| **description** | a member's complete `zarr.json`, consolidated, chunking included. |
| **constraint** | the JSON document describing a whole cohort: literals where members agree, variables where they differ. |
| **literal** | a leaf all members share. No column. |
| **variable** | `{"$var": "nt"}` — a leaf that differs between members. Gets a column. |
| **domain** | an optional restriction on a variable, declared inline. Numeric ranges only. |
| **wildcard** | a leaf we decline to describe. Matches anything; the value is stored verbatim. |
| **binding** | one member's actual value for one variable. |
| **extra column** | a column that is not a variable — provenance, pruning aids, view inputs. |
| **cohort** | a set of members sharing one constraint. v1 has exactly one per store. |
| **view** | a projection of constraint + bindings into another format, e.g. a STAC Item. |

Avoid "item" except in `add_item` and when referring to STAC Items specifically.

## Companion documents

- [`README.md`](./README.md) — motivation, the strictness spectrum, the comparison table.
  Start there for *why*; this document is *what* and *why-this-way*.
- [`COMPARISON.md`](./COMPARISON.md) — the long form of that comparison: how each
  existing approach falls short, and what gap is left.
- [`PROGRESS.md`](./PROGRESS.md) — task state. This document holds the reasoning; when the
  two disagree, this one wins.
- Upstream prototype: **`developmentseed/zarr-datafusion-search`** — every `src/…:NN`
  citation below refers to that repo. It implements the querying half already; this project
  builds on it rather than replacing it.

## Components

| | Component | Home |
|---|---|---|
| (a) | Zarr layout convention — root table + constraint documents + `/groups/<id>` | `spec/` + `crates/zarr-collection` |
| (b) | Constraint language — define, read, write, validate, substitute | `crates/json-constraint` |
| (c) | Query engine integration | `crates/zarr-collection-query` over upstream `zarr-datafusion-search` |
| (d) | STAC API backend + constraint→STAC mapping | `crates/constraint-views` + `python/stac-api-backend` |
| (e) | Examples, at least one non-STAC | `examples/` |

---

## Design decisions already made

### CUE is prior art, not a dependency

We implement the lattice ourselves. CUE is cited for the *representation* — values
and types in one lattice, JSON as a subset, unification — and nothing else. There
is no CUE crate, no FFI, no CUE authoring frontend.

Three reasons a dependency was considered and rejected:

1. **CUE cannot serialize a constraint to JSON.** `cue export` errors on any
   non-concrete value — non-concrete values are forbidden in JSON by design.
   CUE↔JSON Schema conversion exists both ways, but JSON Schema cannot express
   unification variables, which are the whole point. So **our JSON encoding is
   normative** and would have to exist regardless.
2. **"CUE in Rust" means linking Go.** There is no pure-Rust port; `cue-rs` and
   `cuengine` are FFI wrappers around `libcue` (the C interface to the Go runtime),
   statically linked as `libcue.a`. Tolerable in a binary; a real burden for a
   crate that also ships Python wheels via maturin (cgo static libs across
   manylinux / macos-arm / windows).
3. **Oracle-testing against CUE would have needed a translator anyway.** Validating our
   semantics against `cue` requires mapping CUE's value graph onto our `$var` encoding,
   which is a nontrivial Go-side component for a benefit our own property tests give
   more cheaply.

Authoring in CUE was also considered. CUE *can* express our co-constraint through
field references (`nt: int`, then `shape: [nt, 10980, 10980]` in two arrays), but
using it requires that same value-graph translator, in exchange for making small
`zarr.json`-shaped documents marginally nicer to type. Not worth it.

### The constraint language

A constraint document is a JSON document in the same shape as the thing it
describes, with named holes. Every concrete JSON document is a valid constraint
(all literals, no variables) — this is the "superset of JSON" property.

Leaves are one of:

- **Literal** — invariant across all groups in the cohort. No column.
- **Variable** (`{"$var": "nt"}`) — varies per group. Gets a column.
- **Invariant but unknown at authoring time** — not a separate mechanism; a
  variable whose derived domain collapses to one value, promotable to a literal
  (dropping the column) as a storage optimisation.

**Constraints are authored, not inferred.** This is the largest scope decision in the
project. There is no generalisation operation at all: when the schema must change, the
user passes the new constraint explicitly and the system checks it. That removes
anti-unification, leastness, widening policy and domain synthesis from the design
entirely.

What this trades: the constraint is no longer guaranteed to be the *tightest* description
of the members. What it keeps — and this is the half that matters — is that the
constraint is never *false*, because every member is `meet`-checked on write. **Authoring
loses tightness, not truth.**

There is no inference tool and none is planned. If it is ever wanted, pandera's schema
inference is the route rather than implementing anti-unification here — see the pandera
section below.

Consequently the operations needed in v1 are:

- **`meet`** / validate — essential, on every write
- **`substitute`** — essential, for derivability
- **`subsumes`** — a user-supplied constraint must *generalise* the current one, so
  evolution stays monotonic. Cheap: a structural comparison with no synthesis and no
  leastness proof.

**No enums. Categorical variation is a cohort, not a domain.** Retained as a language
restriction even without inference, because it keeps `subsumes` cheap and stops
constraints from growing with the data.

**Domains are declared inline, and repeated variables must declare identical domains.**
`{"$var": "nt", "minimum": 1}` carries its domain at the use site, which preserves the
property that a constraint document looks like the thing it describes. A variable used
twice must have byte-identical domains at both sites; disagreement is a malformed
document, caught by the meta-schema rather than silently resolved.

**Wildcards, for leaves we decline to describe.** A wildcard leaf matches any JSON value,
and the member's actual value is stored verbatim in its column so `substitute` can
reinstate it. This is how v1 sidesteps variable-length lists inside `zarr.json`: a
`codecs` list that differs between members is replaced *in its entirety* by a wildcard,
rather than being aligned element-wise. Rank differences in `dimension_names` and `shape`
are likewise a whole-leaf wildcard, not a positional merge.

**A group's description is exactly the contents of its `zarr.json` — chunking
included.** No projection to a "logical" subset for the MVP. Four consequences worth
having deliberately:

- **`substitute` inverts exactly.** Because nothing is dropped, a constraint plus a
  row's bindings reconstructs the member's `zarr.json` in full. The derivability claim
  is therefore literal rather than approximate.
- **Re-chunking a member widens the constraint.** Accepted, not a bug — chunk shape is
  part of the description, so changing it is a change to the member.
- **Auto-chunking will manufacture variables you did not want.** Writers that pick chunk
  shape from array size — as dask and VirtualiZarr often do — will produce members whose
  `chunk_shape` differs, so a constraint admitting them all must make it a variable with
  its own column. Pin chunk shape explicitly at ingest unless that is genuinely what you
  want.
- **No canonicalisation, and that is only safe because we own the writer.** Since the
  description is compared as-is, any serialisation difference is significant: codec
  defaults written explicitly versus omitted, or `fill_value` encoded differently by
  different implementations. The existing `zarr-datafusion-search` already carries a
  patch for exactly this — zarrs and zarr-python disagree on bytes `fill_value`
  encoding (`ingest.rs:138`). Within one collection every member is written by
  `add_item`, so provenance is uniform and the comparison is sound. **Ingesting groups
  written by someone else is where this breaks**, and it is a v2 problem.

**One document per group, via consolidated metadata.** A referenced group's complete
description is its **consolidated `zarr.json`**, so constraint documents are shaped
like consolidated metadata and there is no hierarchy walking at validate time. This
holds for flat groups too — it is a "one document per group" decision, not a nesting
feature — so it stays in v1.

**Optionality is deferred**, along with cohorts and nesting — see deferred language
features. In v1 every member must have the same set of arrays and attribute keys.
Differing array sets is therefore not expressible at all: `add_item` rejects
such a member at all; the user would have to author a constraint permitting it, which
the v1 language cannot express.

**Choose a referenced unit fine enough that members are structurally uniform.** This is
what lets a language this small handle all four examples, and it is the same move in
each case: field of view rather than plate, primary HDU rather than whole FITS file,
and — the case that would otherwise need optionality — **(shot, diagnostic) rather than
shot** for MAST-U. At that granularity a missing diagnostic is simply a member that does
not exist, rather than an absent array inside a member. Optionality converts into member
absence, which needs no language support at all.

**Scoping rule.** A variable's *binding scope* is one referenced group (one row).
Its *storage* is a column of N bindings. Repeated use of a variable within one
document asserts those positions are equal **within a single group**, and says
nothing across groups. This co-constraint is what JSON Schema structurally cannot
express and is the main reason not to use it.

Operations required:

- `meet` / validate — is this member an instance of the constraint?
- `subsumes` — does constraint A generalise B? Gates `evolve_schema`, keeping evolution
  monotonic.
- `substitute` — constraint + row bindings → concrete member description. This is what
  makes STAC Item derivation mechanical.

That is the whole surface. No least-upper-bound operation, no generalisation, no
inference.

### Why not JSON Schema — and where we do use it

Asked and answered properly, because building a language is the expensive part of M1.

**Hard blocker: co-constraints.** Standard JSON Schema cannot express value-equality
between two locations — "these two arrays have the same time length, whatever it is".
`$ref` to a shared definition constrains both to the same *type*, not the same
*value* (it accepts 12 and 7). Ajv's `$data` references do what we want but are a
non-standard extension. Enumerating combinations via `oneOf` is combinatorial and
needs the values in advance. This is expressiveness, not verbosity.

**`subsumes` does not exist there.** We need to ask "does constraint A generalise B?" to
gate schema evolution, and JSON Schema defines no subsumption relation. Worse, over the
full language (`not`, `anyOf`, `$ref`, conditionals) subschema equivalence is not even
decidable in general. Restricting to a tractable subset means defining our own language
that merely *serialises as* JSON Schema.

**`substitute` does not exist there either.** JSON Schema is a validator, not a
template: no standard "schema + bindings → instance" operation, and no notion of
bindings. So **part of what we need is not validation at all** — `substitute` is
constructive, JSON Schema is purely declarative. Adopting it means
building the two hardest operations ourselves anyway, over a much larger language.

**We would also lose the shape property** — that a constraint document looks like the
consolidated `zarr.json` it describes, which is what makes constraints readable and
diffable.

**Where we *do* use it, and it reduces M1 scope:**

- **Per-variable domains.** A domain is a predicate over a scalar
  (`{"type":"integer","minimum":1}`, `{"enum":[…]}`). That is JSON Schema's sweet
  spot and the boring part — use an off-the-shelf validator rather than inventing
  range/enum/pattern ourselves.
- **The meta-schema.** Validating that a *constraint document* is well-formed is a
  perfect JSON Schema job. Ships in M0 with the spec.

So: JSON Schema for leaf domains and document well-formedness; our own structure for
the tree, the variables, and the operations.

**Residual risk of not adopting it:** we own a format nobody else reads. Mitigation
if it matters later — emit JSON Schema as a *lossy* export (dropping co-constraints)
for interop, which is what CUE does.

### pandera.xarray — authoring surface and export target, not the normative format

Pandera v0.31.0 has full xarray support: `DatasetSchema` and `DatasetModel`, covering
`DataArray`, `Dataset` and `DataTree`, plus encoding validation, CF-convention checks,
lazy validation with error reports, **schema inference from data**, and **YAML/JSON
serialisation**. Three ways it matters here:

**1. It is the obvious Python authoring surface.** Now that constraints are authored
rather than inferred, ergonomics of authoring went from a nicety to a load-bearing
concern — hand-writing JSON constraint documents is unpleasant. `constraint_from_pandera(schema)`
lets users express intent in idiomatic Python. This is a bigger deal than it would have
been under inference.

**2. Export lets users validate in their own code.** `constraint_to_pandera(c)` hands a
consumer a schema they can check their own Datasets against before attempting `add_item`,
using tooling they already know. Same shape as the STAC view: a projection of the
constraint, so it belongs in `constraint-views` (d) rather than in the core.

**3. It is the answer if inference is ever wanted.** We do not plan to build inference.
Should it be asked for, inferring a pandera schema and translating is the route, rather
than implementing anti-unification here. It would be partial — pandera cannot know about
our cross-member variables, so it would cover dtypes, dims and coords and leave the
variable/column decisions to the user — which is a good reason to leave it out until
someone actually needs it.

**Why it cannot be the normative format**, which is the same analysis as the JSON Schema
one and worth stating because it is a much closer relative:

- **No cross-member notion.** A pandera schema validates *one* Dataset. Ours describes a
  *collection*, marking what varies with named variables that become columns. Pandera has
  no way to say "this dimension's length varies across members, and this column records
  it" — the co-constraint machinery has no counterpart.
- **Checks are arbitrary Python.** They are not generally serialisable, and `subsumes`
  could not reason about them, so a pandera schema cannot be the thing we store and
  compare.

So a pandera schema corresponds roughly to one of our *all-literal* constraints plus
checks. Useful in both directions, normative in neither.

Its `DataTree` support is also worth revisiting when nesting arrives.

### Deferred language features

**Breadth before depth.** The risk in this project is the *factoring* being wrong —
STAC or geospatial assumptions leaking into the core. Breadth across domains tests
that; language depth does not. So v1 keeps the language deliberately shallow and
scopes each example to a **flat referenced group** (a group plus its child arrays,
one level), then deepens the language only once all four domains work.

Everything below is designed but out of v1 scope.

**Nested groups.** Anti-unification over a deeper tree is the same algorithm, so
this costs the language little — but it forces per-example decisions we would rather
make with four working examples in hand. Note nesting is **not** cohorts: nesting is
structure *within* one referenced group, cohorts are different constraint documents
for different *subsets of rows*.

**Variable-cardinality children.** Overview counts vary with image size; well counts
vary per plate. Neither a variable value nor a cohort — a variable number of
like-shaped children. Sketch:

```json
{ "$each": { "pattern": "^[0-9]+$", "constraint": { … } },
  "$count": { "$var": "nlevels" } }
```

**Arithmetic relations (`$expr`).** Variable cardinality rules out binding each
multiscale level's shape as its own variable — there is no fixed set of variables
when the count varies. Three options were considered:

1. *Ragged per-row storage* — a list column of level shapes. Zarr has no ragged
   type, so this means offsets arrays or a JSON-encoded string column. Rejected:
   fights the substrate.
2. *Loosely constrained levels* — bind `nlevels` and level 0 only. Rejected:
   `substitute` can then not reconstruct the group, so **derivability breaks** for
   every multiscale example.
3. **Chosen when we get to it:** a leaf may be a simple expression over bound
   variables, e.g. `{"$expr": "ceil(level0_y / 2**n)"}`. Derivability stays total,
   no ragged columns, and it stores *less* — `nlevels` plus the level-0 shape
   reconstructs every level.

**Keep `$expr` small.** Minimum grammar is probably integer arithmetic plus
`ceil`/`floor`; keeping it that small is what stops it becoming a general expression
language, and it is what keeps `subsumes` decidable over expressions.

**Optionality.** "This array may be absent from a member", which needs a boolean-valued
`$present` variable per optional subtree, and a contract that variables scoped inside an
absent subtree are undefined. Deferred because
a finer referenced unit sidesteps it — see above. The accepted v1 limitation is that one
collection cannot hold members that legitimately differ in *which* arrays they have.

**Cohorts.** See layout decision 3.

### Dimension description uses core Zarr metadata, not a domain vocabulary

Constrain over `dimension_names` and `shape` — both core Zarr v3 array metadata,
so it works identically for satellite tiles and microscope stacks. Richer axis
semantics live in attributes as **opaque JSON the language constrains but does not
interpret**: `cube:dimensions` (STAC datacube) for geospatial, `multiscales`/`axes`
(OME-NGFF) for microscopy. Both are "named axes with types and units" in
incompatible spellings; we pick neither.

### Arrow extension type, not a custom Zarr dtype

A Zarr v3 `data_type` tells the reader how to decode bytes and carries
must-understand semantics — an unknown dtype is a hard read failure in every Zarr
implementation. An Arrow extension type is a storage type plus an annotation, and
an unrecognised name degrades to the storage type with all values still readable.

So: `data_type: "string"`, with the extension declaration in **Zarr attributes**.

| Arrow | Zarr |
|---|---|
| `Schema` metadata | `/meta` **group** attributes |
| `Field` metadata (incl. `ARROW:extension:*`) | `/meta/<field>` **array** attributes |
| storage type | `data_type` |

Attributes are free-form JSON that implementations ignore when unrecognised — the
same graceful-degradation contract Arrow field metadata has. They are also mutable
in place and updatable atomically with the data in one Icechunk commit, which
makes evolving the constraint far cheaper than the Parquet-footer equivalent.

Store extension metadata as real JSON in attributes (readable in `zarr.json`, no
double-escaping) and stringify when constructing the Arrow `Field`, since Arrow
requires `ARROW:extension:metadata` to be a string.

`zarr.group_ref` should declare `Utf8View`, `Utf8` and `LargeUtf8` as supported
storage types — Zarr's single `string` dtype maps to whichever we choose, and
permitting the set now avoids a breaking type change later (as `arrow.json` does).

### Six layout decisions to settle before fixtures exist

1. **Referenced groups live in the same store** (`/groups/<id>` alongside the root
   table). One Icechunk repo, one atomic commit — the strongest form of the
   synchronisation argument, and what makes the demo self-contained.

   **This is architectural and not conditional.** One store, one atomic commit *is*
   the thesis — it is the whole answer to the synchronisation problem that motivates
   the project. Without it this reduces to "stac-geoparquet but Zarr", which is much
   less interesting. So we do not have an architectural fallback, by choice.

   **The associated risk:** Icechunk may not scale well to large *numbers of nodes*.
   Two axes must not be conflated:

   - **rows** — references in the `/meta` table. Millions is fine; these are
     ordinary chunked 1D arrays.
   - **nodes** — referenced subtrees in the same store. Untested, and plausibly
     expensive: snapshot metadata enumerates every node, and consolidated metadata
     at multiple levels risks duplicating it.

   Same-store coupling is what ties these together. If node-count scaling turns out
   to be poor, the response is **to make Icechunk scale better**, not to change this
   layout. If that proves too hard, the honest response is to **pause the project**
   rather than retreat to a weaker architecture. Recording that now so it is a
   decision rather than a late scramble.

   External URI references stay in the spec as a declared variant — useful to other
   people's use cases — but they are explicitly *not* our fallback.
2. **Widening backfills by reading; it does not write nulls.** Transactional
   incremental writes are the point of the project, so write-once is not acceptable.
   Instead, when the user evolves the schema, the writer **pays the cost of reading
   whatever group metadata it needs to compute the new column's value for every existing
   row**, and commits the new constraint plus the fully-populated new column in one
   transaction.

   This works because a variable is by construction a hole in the *group* description,
   so its value for row `i` is always derivable from group `i`. The values were never
   unknown — merely not yet materialised. **No nulls are involved.**

   *This strengthens decision 1.* Backfill is a local read because the groups are
   colocated. With external URI references it would be N reads across the internet,
   committed non-atomically. Same-store is what makes widening tractable.

   Costs, recorded honestly:

   - **Widening is O(N) in existing collection size**, not O(1) in the append. Adding
     one non-conforming group means recomputing a column over every existing row. At
     ~100 groups this is nothing; at millions it is a heavy transaction.
   - So writers see **bimodal latency**: ordinary appends are cheap, widening appends
     are proportional to the collection. The API should report which happened rather
     than hiding it.
   - Consolidated metadata at the store root may make backfill a *single* read rather
     than N — but that makes the root metadata object large, which pushes on the
     node-count risk. Worth measuring alongside the Icechunk investigation.

   **What this leaves for the reserved nullability question: nothing in v1.** Schema
   evolution backfills real values, and optionality — the other case that would have
   needed a validity mask — is deferred. What remains is only genuinely-missing *source*
   values: a member whose own metadata lacks a measurement. That is a domain problem,
   and no v1 work depends on it.

3. **Cohorts are deferred as long as possible.** v1 is **one store = one constraint
   document = one implicit cohort**, which is also what `zarr-datafusion-search`
   does today (one table = one `/meta` group). No discriminator column, no keyed
   map, no nesting.

   Pay one line of forward compatibility now: store the single constraint **under a
   map keyed by one cohort ID** from day one, so that adding a second cohort later
   is additive rather than a breaking attribute-shape change. A keyed map is also
   naturally schema-level rather than column-level, which reinforces decision 5. Following
   stac-geoparquet's `collections` map — which lets multiple cohorts share one
   physical schema — is the eventual target, not the v1 scope.

   Deferring is cheap because no example is *blocked* without cohorts, though two are
   left loose. MAST-U's varying diagnostics are handled by taking (shot, diagnostic) as
   the referenced unit. Sentinel-2 tiles in differing UTM zones give an uninformative
   `{"$var": "crs", "type": "string"}` — since enums are excluded, the domain cannot
   say anything tighter — which is merely imprecise rather than wrong; scoping the v1
   example to one UTM zone tightens it if desired. HST is the only case that genuinely
   needs cohorts, and it is implemented last.
4. **Disjunction restricted to variable domains** — an enum on `crs`, not a choice
   between two whole group shapes. Keeps `subsumes` cheap and stops constraints growing
   with the data. Note this restriction is what *creates* the need for
   cohorts, since it pushes genuinely different shapes out of the language — so
   deferring cohorts means v1 simply cannot describe a collection of mixed shapes.
   That is an accepted v1 limitation, not an oversight.
5. **The constraint lives in `/meta` group attributes, not in the extension type.**
   Decisive argument: **the constraint defines which columns must exist** (every
   variable implies a column), and a document specifying the table's whole column set
   cannot live inside *one column's* type annotation — that is a layering inversion.
   Test: if you cannot answer "which columns must this table have?" without reading
   it, it belongs at schema level.

   The `zarr.group_ref` extension type therefore carries only small, genuinely
   column-scoped things: supported storage types, the ID→URI resolution rule, and the
   spec version. That split also keeps the extension type **stable while the
   constraint evolves per commit** — important because changing a field's extension
   metadata changes the field's *type identity*, with the concat/union equality
   problems that implies. A routine append must not mutate the column's type.

   Objection considered: field metadata survives projection, schema metadata often
   does not. It does not bite here — the constraint is a **planner input** (read off
   the registered table's schema for pruning and query validation), not a row payload
   that must ride through aggregates into output batches.

   `/meta` attributes rather than **store root**, even though the constraint describes
   `/groups/*`: `/meta` maps 1:1 to Arrow `Schema` metadata, so DataFusion gets it for
   free with the table schema. Discoverability is not lost, because consolidated
   metadata at the store root exposes `/meta`'s attributes anyway — a reader wanting
   only "what do these groups look like" still gets it in one read.
6. **Variables ⊆ columns.** Every variable in the constraint **must** have a column;
   extra columns beyond those are permitted. The constraint therefore specifies a
   *lower bound* on the table schema, not the whole thing.

   Extra columns exist for real reasons: derived values for query convenience (WKB
   `bbox` for the R-tree index, extracted `datetime`), ingest provenance (source URI,
   checksum, ingest time), index support, and view-only fields with no counterpart in
   the Zarr group at all.

   Consequences:

   - **`substitute` reads only variable columns**, so derivability is unaffected by
     extra columns. *View* projections may read any column — a STAC Item's `datetime`
     or `bbox` may well be an extra column rather than a variable. Do not conflate
     these two directions.
   - **Tightening is free.** Promoting a variable to a literal (all bindings agree)
     no longer requires dropping its column — it simply becomes an extra column. So
     the constraint can be tightened with no data migration.
   - **Loosening has a data cost, paid by reading.** Turning a literal into a variable
     requires a new column populated for every existing row, which `evolve_schema`
     backfills by reading those members' metadata (decision 2). Real values, no nulls —
     but O(N) in collection size.
   - **The `/meta` table is a materialised view over the referenced groups.** Every
     variable column is recomputable from the groups it describes. That gives a free
     consistency check (recompute and compare), a repair path, and the guarantee that
     widening can always be satisfied. Extra columns are the exception — they are not
     recomputable, which is a further reason to keep them clearly distinguished.
   - **Mechanically checkable:** "every variable has a column" is a cheap
     well-formedness check between constraint and table schema. Ships with M2.
   - **Naming:** variable names claim the column namespace; extra columns must not
     collide with them.

   v1 declares nothing about extra columns and does not validate them. Noting the
   tension honestly: a project whose thesis is self-description having undocumented
   columns is a little awkward, so optional declarations (name, type, how it was
   derived, whether it is index support) are a likely later addition. Not v1 scope.

---

## Repo shape

One Cargo workspace, with crate boundaries drawn where the *eventual* repo splits
would be, so extraction later is a path change rather than a refactor.

```
datacollections/
├── spec/                          (a) normative convention
│   ├── constraint-language.md         JSON encoding + meet/subsumes/substitute semantics
│   ├── layout.md                      table + constraint doc + /groups/<id>
│   └── fixtures/                      conformance data, shared by every crate
├── crates/
│   ├── json-constraint/           (b) meet · subsumes · substitute
│   ├── zarr-collection/           (a) layout, group attrs, zarr.group_ref, write paths
│   ├── zarr-collection-query/     (c) thin layer over zarr-datafusion-search
│   └── constraint-views/          (d) constraint + bindings → target JSON (STAC Items)
├── python/
│   ├── datacollections-py/            pyo3 bindings; create_collection · add_item ·
│   │                                  evolve_schema · check; pandera translation
│   └── stac-api-backend/          (d) stac-fastapi backend, pure Python
└── examples/                      (e) one per domain, Python
```

Dependencies run one way only:

```
                    ┌── zarr-collection ◄── zarr-collection-query ◄─┐
json-constraint ◄───┤                                               ├── datacollections-py
                    └── constraint-views ◄──────────────────────────┘
```

**Two crates depend on `json-constraint` and neither on the other.** `zarr-collection` is
*storage* — layout, group attributes, `zarr.group_ref`, `add_item`, `evolve_schema` — and
depends on zarrs, Icechunk and arrow. `constraint-views` is *projection* — constraint plus
one member's bindings out to some other format — and depends on none of them, because by
the time it runs everything it needs is plain JSON. It calls `substitute` to obtain the
member description, then maps that through a declared view mapping.

Hence the name: it is not `zarr-collection-views`, because it has no Zarr dependency and
would serve anyone deriving STAC Items from a constraint with no Zarr stack at all. Same
reasoning that made the core crate `json-constraint` rather than `zarr-constraint`.
`datacollections-py` is what wires storage and projection together.

**pandera translation is Python-side.** Pandera is a Python library, so producing *live*
`DatasetSchema` objects cannot happen in a Rust views crate. Two options: emit pandera's
serialised YAML/JSON from `constraint-views`, or build the objects in
`datacollections-py`. Prefer the latter — round-tripping live objects is the point, and
the serialised form is a lossy intermediary. Noted because it is the one place where the
"(d) views" boundary does not fall cleanly on the Rust/Python line.

**The one hard dependency rule:** `json-constraint` depends on neither zarrs, nor
arrow, nor DataFusion. JSON in, JSON out. That rule is the extractability
guarantee, and it is also what makes the crate useful to people who never touch a
query engine.

For (c), depend on `zarr-datafusion-search` as a git dependency rather than
vendoring, with `[patch]` to a branch while upstream PRs are in flight.
`zarr-collection-query` is explicitly designed to shrink toward zero as things
upstream.

---

## Milestones

Ordered by risk, not by dependency.

**M0 — spec + fixtures, before any code.** The convention doc, the JSON encoding,
and three or four tiny hand-built fixture stores. Fixtures are the contract between
crates and the thing that keeps them honest.

**M0 splits on the reserved substrate question.** `spec/constraint-language.md` is
substrate-independent and can be written now; `spec/layout.md` and the store fixtures
cannot. See "What is still to decide".

**M1 — `json-constraint`, deliberately minimal.** Highest risk, zero dependencies,
so it goes first and in isolation. Scope is exactly:

- literals, variables with inline domains (numeric ranges), and wildcards
- **flat groups only** — a group plus its child arrays, one level
- every member has the same set of arrays and attribute keys
- `meet`, `subsumes`, `substitute`

Explicitly **not** in M1, or anywhere in v1: inference of any kind. Deferred to M7:
optionality, nesting, variable cardinality, `$expr`, cohorts. Excluded permanently: enums. See deferred language features.

Property tests:

- `meet` accepts every member the constraint was authored for, and rejects mutations of
  them — checked against a few hundred real OME-Zarr and Sentinel-2 `zarr.json`
  documents pulled from public stores. This needs no DataCollections store, which is
  exactly why `json-constraint` has no Zarr dependency.
- `subsumes` is reflexive, transitive, and antisymmetric up to equality
- `substitute` inverts abstraction **exactly** — since the description is the whole
  `zarr.json`, a constraint plus bindings reconstructs it in full
- round-trip: `substitute(constraint, bindings_of(m)) == description_of(m)` for every
  member `m`

**M2 — `zarr-collection` + the Python creation API.** Write a store from a set of
groups against an authored constraint; read it back; surface the Arrow
schema with extension types built from attributes. Round-trip against fixtures. This
is the milestone where the first example (OME-Zarr) actually gets built, so the
Python API below is part of it rather than a later wrapper.

**Includes both write paths**, since transactional incremental writes are the point:

- *append* — group satisfies the existing constraint; O(1), just extend the columns
- *`evolve_schema`* — the user supplies a looser constraint; `subsumes` checks it
  generalises the current one, the writer backfills any new column for every existing
  member by reading their metadata, and constraint plus columns land in one transaction
  (decision 2)

Test that a widening append leaves the store byte-equivalent to building the whole
collection from scratch — that is the property that makes incremental writes
trustworthy.

#### Python API — a first-class M2 deliverable, not a wrapper added later

This is how the examples get built, so it cannot be deferred to the end. It lands in
`python/datacollections-py`, **not** in `json-constraint`, whose no-zarrs/no-arrow
rule is what keeps it extractable.

Division of labour: the Rust crates do the constraint algebra and the store layout;
**Python drives ingest**, because that is where the ecosystem is — xarray,
VirtualiZarr, `ome-zarr-py`, astropy.

Two entry points:

```python
coll = create_collection(store_or_session, constraint=None)   # constraint optional
member_id = coll.add_item(ds)                                 # ds: xr.Dataset
```

`add_item` takes an **xarray Dataset, potentially virtual** (i.e. containing
`ManifestArray`s from `open_virtual_dataset`). That single input type is what makes
ingest uniform across all four examples, given everything is virtual. Writing the
group's virtual chunk references delegates to VirtualiZarr's Icechunk writer rather
than reimplementing it.

`add_item` must be **one Icechunk transaction** covering all of:

1. generate a random id and write `/groups/<id>` from the Dataset
2. derive that group's description
3. `meet` against the current constraint; reject on failure
4. append the row to `/meta`
5. commit

#### Member ids are generated, not supplied

`add_item` does not accept an id; it generates a random one (128 bits is ample) and
returns it. Rationale:

- **Uniqueness needs no coordination.** User-supplied ids would require some authority to
  guarantee they do not collide across concurrent writers, which sits badly with
  uncoordinated parallel writes. Independently generated random ids collide with
  negligible probability and need no central sequence.
- **Random, not content-addressed.** Hashing the member's description would be tempting
  but is wrong twice over: two members can legitimately have identical metadata and must
  stay distinct, and it would reintroduce the canonicalisation problem the `zarr.json`-as-is
  decision avoids.

Two consequences:

- **Extra columns become the only way to address a member meaningfully.** With opaque
  ids, "shot 30420, diagnostic amc" is answerable *only* via extra columns, and a derived
  STAC Item wants a human-meaningful `id` from one too. This promotes "how are extra
  column values supplied?" from an open gap to a **blocker for M6** — MAST-U cannot be
  built without it.
- **Byte-equivalence tests need restating.** Building the same collection twice yields
  different ids, so "byte-equivalent to a from-scratch build" is only achievable modulo
  ids — or with a seedable id generator injected for tests. Prefer the latter; it keeps
  the assertion strong.

Minor cost: `/groups/<hash>` is not human-browsable, so eyeballing the store no longer
finds a member. That is what the table is for.

Rejection at step 3 must roll back step 1 — nothing commits, including the group
write. Icechunk gives this, but note the ordering is wasteful: prefer a **cheap
pre-check** deriving an approximate description straight from the Dataset (dims,
shapes, dtypes, attrs are all there) before writing anything. That catches most
rejections early. It cannot be authoritative, because the exact `zarr.json` depends on
the writer's encoding choices (codecs, chunk shapes), so the real check still happens
post-write and pre-commit. Two-phase.

#### Schema evolution is an explicit, separate call

Because constraints are authored rather than inferred, there is no flag on ingest.
`add_item` is always strict; changing the schema is its own operation:

```python
coll.add_item(ds: xr.Dataset) -> str            # strict; returns the generated id
coll.evolve_schema(new_constraint)              # explicit, deliberate, own transaction
```

`evolve_schema` must:

1. check `subsumes(new_constraint, current_constraint)` — evolution is monotonic, so a
   new constraint may only loosen. Tightening requires re-validating every existing
   member and is a separate operation if we ever want it.
2. create any column the new constraint requires and does not yet have, backfilling it
   for every existing member by reading their group metadata (decision 2)
3. commit constraint and columns together

This is strictly simpler than the earlier `allow_schema_evolution` flag: evolution is
visible in the call site rather than hidden in a boolean, it gets its own transaction,
and its cost — O(N) in existing members — is attached to an operation the user chose
rather than to a routine append.

**Rejection messages still matter most.** `add_item` refusing a member should say *what*
failed to match — the specific leaf, expected versus actual — not "constraint violation".
`meet` knows this, so it costs nothing to report.

Companion, no writes:

```python
coll.check(ds)   # -> None, or the specific mismatches against the current constraint
```

Bootstrapping needs no inference: `create_collection(constraint=None)` takes the first
member's `zarr.json` **verbatim** as an all-literal constraint. Every subsequent member
must then match exactly until the user calls `evolve_schema`, which is the point at which
they say explicitly what is allowed to vary.

Inspiration (deliberately not read yet, to avoid anchoring):
<https://github.com/earth-mover/icc-prototype>

**M3 — query.** Wire to `zarr-datafusion-search`.

**M4 — views + STAC mapping.** Projection mapping, then Item derivation verified
by round-tripping real STAC Items.

**M5 — stac-fastapi backend,** with pystac-client as the acceptance test.

**M6 — all four examples working flat, at ~100 groups each.** This is the real
milestone: breadth across domains at deliberately shallow depth. It is what proves
the factoring, which is the thing most likely to be wrong.

**M7+ — deepen, only after M6.** Two independent tracks, both gated on having four
working examples:

- *Language depth* — nesting, then variable cardinality and `$expr` (which unlocks
  overviews and multiscale), then cohorts.
- *Scale* — see the Icechunk investigation below.

M1–M3 is already a working demo — constrain, store, query — and everything STAC is
the tail. So **OME-Zarr lands on M1–M3, before any STAC code exists at all.** That
is the right order for a project whose thesis is that STAC is one optional view, and
it is a useful forcing function: if the core cannot express the microscopy case
without STAC vocabulary anywhere in the stack, the factoring is wrong. Sentinel-2
then arrives with M4–M5, so the view layer is designed against a real target rather
than retrofitted; MAST-U and HST follow.

### Scale is gated on an Icechunk investigation

**Hard cap of ~100 groups per example until then.** Going beyond that requires first
investigating how Icechunk scales with node count — snapshot size, commit cost,
store-open time — rather than discovering the limits by accident.

Starting point: **"Icechunk Big-O Scaling: Code Analysis"** — a code-level analysis
of the Icechunk Rust implementation against the scaling questions in
`Icechunk_big-O_scaling.md` — <https://hackmd.io/Bq-2qekGRImYawdajUyC-A>

**This does not gate the architecture, only the scale.** Per layout decision 1,
poor node-count scaling means upstream work on Icechunk, or pausing — not a
different layout. So the possible outcomes are: scale up, do Icechunk work first,
or stop. Not "redesign".

Split the work by cost, since the two halves have different urgency:

- **Reading the existing analysis is cheap and can happen at any time.** Worth doing
  early precisely *because* the bad outcome is "pause the project" — that is
  information you would rather have before building four examples than after.
- **Empirical benchmarking, and any Icechunk fixes, wait until after M6.** v1 never
  exceeds ~100 groups, so nothing in M0–M6 depends on the answer.

**Cohorts are out of scope for M0–M6** (layout decision 3), which is what keeps
every example single-cohort and defers the hardest design question past the point
where breadth has been proven.

---

## Examples

Four domains, three of them with no STAC in the stack anywhere.

**Implementation order: OME-Zarr → Sentinel-2/STAC → MAST-U → HST.** Rationale for
leading with microscopy:

- OME-NGFF has the **richest attribute vocabulary** of the four, so it is the
  hardest test of "domain vocabulary as opaque JSON we constrain but do not
  interpret". Doing it first means the core cannot accidentally get built around
  geospatial assumptions.
- Fixtures are **easy to make locally** — sample data or generated with
  `ome-zarr-py` — so M0 needs no S3 access and no large FITS downloads.
- It validates the factoring **before the STAC view exists at all**, which is the
  forcing function the milestone ordering wants.

Doing STAC second rather than last is deliberate: it is the only example exercising
(d), so it should land early enough that the view layer gets designed against a
real target rather than retrofitted.

**Every example store is fully virtual — only metadata is materialized.**
VirtualiZarr can virtually ingest native Zarr as well as FITS and COG/JP2, so
MAST-U's existing Zarr is virtualized like everything else rather than referenced
as a special case. Consequences worth having on purpose:

- **One ingest path** for all three examples: source format → VirtualiZarr →
  virtual Zarr → our layout. No per-example plumbing.
- The `/groups/<id>` subtrees are pure metadata plus chunk manifests, so the whole
  demo store is small enough to host and version cheaply.
- Icechunk supports virtual chunk references natively, so this is a supported path
  rather than a workaround.
- It makes scale claims testable without petabytes of storage — though see the
  node-count risk in layout decision 1: cheap *bytes* does not imply cheap
  *node counts*.

**~100 groups per example, hard cap.** Icechunk's scaling in node count is unknown
and is the plan's main structural risk, so every example stays deliberately small.
Going beyond requires the Icechunk investigation first — see Milestones.

### Sentinel-2 L2A — geospatial, STAC

Heterogeneous CRS and shapes across tiles, which is the motivating case in the
`zarr-datafusion-search` README. Can reuse the existing STAC ingest path. This is
the only example that exercises (d).

Use the Element84 `sentinel-2-l2a` COGs on AWS Open Data (11.4M scenes available,
converted from JP2K), with `earth-search.aws.element84.com/v1` as the STAC API.
~100 scenes in v1.

These COGs have internal overviews, and VirtualiZarr's TIFF path surfaces them as
separate groups. So multiscale is a cross-domain pattern rather than an OME-Zarr
quirk — both use power-of-2 downsampling, and both have **variable level counts**
(smaller images have fewer overviews), which is what motivates `$each`/`$count` and
`$expr`. **v1 virtualizes the full-resolution level only**, per breadth-before-depth;
overviews arrive with the M7 language-depth track and need no new ingest work.

### MAST-U tokamak — fusion, non-STAC

[FAIR-MAST](https://github.com/ukaea/fair-mast) publishes 11,573 shots from MAST
campaigns M05–M09 as Zarr on a public S3 bucket, which we virtually ingest like the
other sources. Why it is the strongest example:

- **It is a live instance of the problem.** FAIR-MAST ships a separate JSON REST
  API for shot metadata *alongside* the Zarr data store — exactly the disconnected
  metadata store the `zarr-datafusion-search` README objects to.
- Shot numbers are natural unique group IDs, and 11,573 shots are available — a
  realistic non-toy corpus to scale *into* once the Icechunk investigation allows it.
  v1 uses ~100 shots like every other example.
- **To check:** whether a shot's signals sit directly under the shot group or under
  per-diagnostic subgroups. If the latter, v1 takes a **single diagnostic** as the
  referenced unit to stay flat, and shot-level grouping waits for nesting.
- Per-shot time-series lengths vary while dimension names are fixed — the textbook
  `{"$var": "nt"}` case.
- **Diagnostic availability varies per shot**, which is why the referenced unit is
  **(shot, diagnostic)** rather than shot: a missing diagnostic becomes a member that
  does not exist, so optionality is not needed.
- Fusion has its own metadata vocabulary (IMAS/IDS), giving a second test of
  "domain vocabulary as opaque JSON we constrain but do not interpret".

### Hubble Space Telescope — astronomy, non-STAC

FITS in the AWS Open Data Registry, virtualized with VirtualiZarr. Notes:

- VirtualiZarr's FITS reader was fixed and **tested on HST data** specifically, so
  this is a trodden path. Caveat: FITS currently goes through the kerchunk-backed
  reader rather than a dedicated VirtualiZarr reader, which upstream describes as
  temporary.
- FITS images are typically contiguous and unchunked, so virtual chunk granularity
  is coarse (whole array or row blocks). Fine for a metadata-search demo.
- Multiple HDUs per file become multiple Zarr nodes, so **v1 scopes to the primary
  HDU** to stay flat.
- HDU structure varies by instrument (WFC3 / ACS / COS), so different instruments
  are genuinely different group *shapes* — which under the disjunction restriction
  must become **separate cohorts**. Since cohorts are deferred, **v1 scopes HST to a
  single instrument** (e.g. WFC3/IR) and stays single-cohort like the others. HST is
  therefore the example that motivates cohorts and the first thing to revisit when
  they arrive — which is why it is implemented last.

### OME-Zarr microscopy — bioimaging, non-STAC — **implement first**

- **Richest domain vocabulary** of the four: `multiscales`, `axes` (name, type
  `space|time|channel`, unit), `coordinateTransformations`, `omero` rendering
  metadata. The best available test that our language constrains attribute subtrees
  without interpreting them.
- **Natively hierarchical** — plate → well → field of view → multiscale levels.
  **v1 takes the field of view at resolution level 0 as the referenced unit**: one
  flat group, no multiscale levels, no plate/well structure. Both arrive with the
  M7 language-depth track.
- **Correction to an earlier framing:** plate/well was described as "the first
  customer for cohorts". That was wrong. Varying *numbers* of wells is
  variable-cardinality (`$each`/`$count`), not cohorts. Cohorts would only be needed
  if different wells had structurally different *shapes*.
- Varying Z-depth and channel counts per FOV are ordinary `$var` cases; varying presence
  of channels is an optionality case and varying multiscale level counts a cardinality
  case, so both wait for M7.

---

## Upstream work (separate track)

Independent of this repo, and worth doing regardless of how the rest lands:

- **PR to `zarr-datafusion-search`:** read array attributes in `schema.rs` to build
  Arrow extension types, replacing the `if name == "bbox"` special case. Deletes the
  hardcoded EPSG:4326 as a side effect, so a differently-projected `bbox` works
  without a code change. Small, contained, useful to the maintainers regardless.
- **Not yet a conversation:** nullability upstream. Every field there is
  `nullable: false` with fill-value backfill for columns added to an existing store,
  so absent and `0`/`""` are indistinguishable. Worth raising eventually, but hold
  until the reserved questions are settled — the right upstream ask depends on which
  substrate we land on.

Note this is cross-org — the repo is under DevelopmentSeed, the concept is
Earthmover's — so the larger direction is a conversation before it is a PR.

---

## Reserved questions — do not decide these incidentally

Two design questions are hard enough that they are **reserved for Tom** and should
not be resolved as a side effect of implementation work. If v1 appears to need an
answer, that is a signal to reduce v1 scope instead.

**They are coupled, and the substrate question comes first.** Parquet has nulls
natively, so resolving the substrate toward Parquet/Iceberg does not *solve* the
nullability question — it **dissolves** it. Effort spent designing mask arrays before
the substrate is settled may therefore be wasted.

Note the nullability question has since narrowed considerably — see below — so the
substrate question is now much the larger of the two.

### 1. Serialisation substrate

**v1 assumes Icechunk-Zarr. Nothing is blocked on this.** The question is recorded because
it is worth revisiting later, not because anything waits for it.

If it is ever reopened: metadata serialised as **Zarr arrays inside Icechunk** — the
assumption — versus **Iceberg/Parquet tracked by Icechunk at a lower level**, or Lance.
Rough shape of the trade:

- *Zarr-in-Icechunk (assumed):* one format for everything, metadata and data at the same
  abstraction level in the same atomic commit, "the meta arrays are just Zarr arrays"
  elegance, and it is what `zarr-datafusion-search` already implements.
- *Parquet/Iceberg:* nulls natively, DataFusion reads it without a custom TableProvider,
  row-group and column statistics with predicate pushdown for free, and Iceberg brings
  schema evolution plus snapshots. Costs a second format in the stack and moves metadata
  to a different abstraction level from the array data.

Reopening it would reach layout decisions 1, 5 and 6, so it gets more expensive the later
it happens. That is the argument for revisiting deliberately at some point rather than
never.

### 2. Nullability

How to represent "value unknown" when Zarr's `fill_value` guarantees every position
holds a real value, and no sentinel is safe (`0` is a real cloud cover, `""` a legal
string, NaN float-only). Parallel `_valid` mask arrays are one candidate; Parquet
native nulls are another; the Arrow-IPC chunk codec (zarr-python#2031, draft and
Python-only) is a third.

**Nothing in v1 depends on it.** Schema evolution backfills real values by reading the
groups (decision 2), and optionality — the other case that would have needed a validity
mask — is itself deferred. What remains is only genuinely-missing **source** values: a
member whose own metadata lacks a measurement. That is a domain problem, and it affects
mainly faithful absent-vs-null round-tripping in derived STAC Items for sources with such
gaps.

---

## What is still to decide

Organised by what each blocks, so this section answers "what is left?" directly. The
two hardest are in **Reserved questions** above.

### Blocks work now

**Nothing.** The substrate question is settled by assumption (Icechunk-Zarr), so both spec
files and every milestone are open for work.

One live gap on the critical path, though it blocks design rather than starting:

- **How are extra column values supplied?** Decision 6 permits extra columns; nothing in
  the API passes or derives them. Since member ids are opaque random hashes, extra columns
  are the *only* way to address a member meaningfully — MAST-U needs `shot` and
  `diagnostic`, and a derived STAC Item needs a human-meaningful `id`. Wants deciding
  before M2 fixes the Python API, not left until M6.

### Blocks nothing yet — decide when the milestone arrives

- **STAC pagination tokens vs the Sort extension** (M5). Row ordinal plus Icechunk
  snapshot ID gives snapshot-isolated pagination, but interacts awkwardly with
  user-chosen sort orders.
- **True geometry, or bbox-approximate `intersects`?** (M4). More a
  conformance-claim honesty question than a technical one.
- **Minimum `$expr` grammar** (M7). Integer arithmetic plus `ceil`/`floor` is probably
  enough; keeping it that small is what stops it becoming a general expression
  language.
- **Should extra columns be declarable** — documented but not validated? (post-v1)
- **How cohorts nest**, if at all (post-M6).

### Unknowns to check — facts, not decisions

- **MAST-U store structure** — do signals sit directly under the shot group, or under
  per-diagnostic subgroups? Determines the flat referenced unit for that example.
  Needed before the MAST-U example, not before M0.
- **crates.io availability of `json-constraint`** — their API refused our request.
  Needed before publishing.
- **FITS-via-kerchunk reader status** — upstream calls the arrangement temporary.
  Needed before HST.

### Closed — do not reopen incidentally

Recorded so they are not relitigated as a side effect of implementation work.

- **Same-store layout is architectural, not conditional** — decision 1. Poor Icechunk
  node-count scaling means improving Icechunk, or pausing; not a different layout.
- **CUE is prior art, not a dependency** — it cannot serialise non-concrete values,
  Rust means linking Go, and oracle-testing it would need a Go-side translator.
- **JSON Schema for leaf domains and the meta-schema only** — it cannot express
  co-constraints, and has neither `subsumes` nor `substitute`.
- **Variables ⊆ columns** — decision 6. Extra columns permitted.
- **The constraint lives in `/meta` group attributes**, not in the extension type —
  decision 5.
- **Schema evolution backfills real values by reading the groups** — decision 2. No
  nulls, and no write-once limitation.
- **Constraints are authored, not inferred.** There is no `join`, no anti-unification and
  no inference tool anywhere in the design; evolution is an explicit `evolve_schema` call
  supplying the new constraint. If inference is ever wanted, pandera's is the route.
- **Cohorts deferred to post-M6**; v1 is single-cohort — decision 3.
- **Crate naming:** `json-constraint` for the substrate-independent crate; `zarr-`
  prefixes only where there is a genuine zarrs dependency.

---

## Notes for whoever picks this up

**Read the rejection trails before re-proposing anything.** Most of this document is
"considered X, rejected it because Y" — CUE, JSON Schema, pandera-as-format, custom Zarr
dtypes, enums, inference, write-once, intent-derived defaults. That is the document's main
value and also its main trap: it is long, and the temptation is to skim to the milestones.
The "Closed — do not reopen incidentally" list is the index to it.

**One technique did most of the work, and it generalises.** Whenever something seemed
inexpressible, the fix was almost never to extend the language — it was to **choose a finer
referenced unit so that members are structurally uniform**. Field of view rather than plate.
Primary HDU rather than whole FITS file. (shot, diagnostic) rather than shot. Reach for that
before adding syntax; it is why a language with three operations and two leaf kinds covers
four unrelated scientific domains.

**Know which decisions are load-bearing.** Same-store atomicity (decision 1) is the thesis —
without it the project reduces to "stac-geoparquet but Zarr" and is not worth doing. The
deliberate smallness of the language is a scope choice that will be under constant pressure;
defend it. Breadth-before-depth is a forcing function, not scheduling convenience: if the
core cannot express the microscopy case with no STAC vocabulary anywhere in the stack, the
factoring is wrong and that is worth discovering early.

**Where this document is most likely to be stale.** The per-example scoping decisions were
made before anyone looked at the actual data — MAST-U's structure in particular is flagged
as unverified. Treat the examples section as intent rather than fact.

**The reserved questions are genuinely reserved.** If v1 appears to need one answered,
that is a signal to reduce v1 scope, not to decide it.
