# Upstream: `developmentseed/zarr-datafusion-search`

Reconnaissance only — nothing was raised, opened or pushed anywhere. Line numbers
are against the repo as of this reading; re-check before acting on them.

The headline: **their layout and ours are nearly the same store.** That is much
better news than the plan assumed, and it makes the two upstream changes PLAN.md
already wants into exactly the changes that would let their `TableProvider` read a
DataCollections store unmodified.

## What they assume, next to what we write

| | `zarr-datafusion-search` | DataCollections |
|---|---|---|
| table group | `/meta`, hardcoded (`src/ingest.rs:478`) | `/meta` |
| columns | one **1-D Zarr array per column**, node name = column name (`src/schema.rs:49-59`) | same |
| shape | `[nrows]`, chunk `[chunk_size]`, taken from the *first* child array (`src/ingest.rs:106-136`) | same, chunk 8192 |
| field order | sorted alphabetically for determinism (`src/schema.rs:44-46`) | our order is the constraint's; theirs is a sort of the same set |
| schema metadata | **none read from the store** (`src/schema.rs:46`) | `/meta` attributes hold the constraint |
| field metadata | **none read**, except one hardcoded case | `/meta/<col>` attributes hold `ARROW:extension:*` |
| extra structures | `/indexes/<column>` holds an R-tree per indexed column (`src/table_provider.rs:117-158`) | nothing yet; this is a good idea to adopt |
| nullability | every field `nullable: false` (`src/schema.rs:69,127`) | same, and by design |

So a DataCollections store is *already* shaped like the thing their provider opens.
What it carries that they ignore is precisely the self-description: the constraint in
group attributes and the extension type in array attributes.

## The two changes, precisely

**1. Build extension types from array attributes, not from a column named `bbox`.**

The special case is `src/schema.rs:63-78`: if the column is called `bbox` and is Zarr
`bytes`, it becomes `BinaryView` with a geoarrow `WkbType` whose CRS is hardcoded
`EPSG:4326`; any *other* WKB column gets no extension type, and a non-bytes column
named `bbox` is a hard error.

The change: `zarr_to_arrow_field` takes `(name, zarr_dtype)` (`src/schema.rs:62`),
called from `arrays_to_schema` (`src/schema.rs:42`). Pass the array's attributes too —
`zarrs::array::Array::attributes()` returns `&serde_json::Map<String, Value>` — and
copy `ARROW:extension:name` and `ARROW:extension:metadata` into the field's metadata
map. Those are Arrow's canonical keys, so `Field::extension_type::<WkbType>()` and
geoarrow's `try_extension_type` pick up `geoarrow.wkb` plus its CRS with no name
check at all. The `bbox` branch then deletes: Zarr `bytes` already maps to
`BinaryView` by default (`src/schema.rs:104-105`).

Deleting the hardcoded EPSG:4326 is the point — a differently-projected `bbox` then
works without a code change, which is exactly the Sentinel-2 heterogeneous-CRS case.

One thing to fix in the same pass: `evaluate_filters` rebuilds fields as
`Field::new(name, data_type, true)` (`src/table_provider.rs:1104`), which drops the
extension metadata for the filter-evaluation schema.

**2. Read group attributes into Arrow `Schema` metadata.** Currently nothing calls
`Group::attributes()`; the schema is built from array names and dtypes alone
(`src/schema.rs:35-47`), and ingest writes `GroupMetadataV3::default()`, i.e. empty
attributes (`src/ingest.rs:489-511`). `group_arrays_schema{,_async}` already hold the
`Group`, so threading `group.attributes()` through to `Schema::new_with_metadata` is
small. That is the change that would hand the planner our constraint for free — the
`/meta` attributes ↔ Arrow `Schema` metadata mapping that layout decision 5 rests on.

With both, their provider reads our store and gets the constraint as a planner input
without knowing anything about DataCollections.

## What blocks using them as a dependency today

**Version skew, and it is not small.** They pin `icechunk = "0.3.16"`,
`datafusion = "53.0"`, `arrow = "58.0.0"`, `zarrs = "0.23.10"` (`Cargo.toml:7-31`).
The Python stack this MVP runs on is `icechunk` 1.1.21. Depending on their crate
from a workspace that also links current Icechunk means either they bump — a real
piece of work, not a version-number edit — or we pin to an Icechunk two major
versions old. That is the concrete reason `zarr-collection-query` does not exist as a
crate yet, over and above "the MVP did it in Python".

## Their nullability behaviour, confirmed

Worth having in writing, because PLAN.md flags it as an eventual upstream
conversation. Every field is `nullable: false`. Nulls are destroyed on write: each
Arrow arm converts `is_null(i)` to the fill value — `0` / `false` / `""` / `vec![]`
(`src/ingest.rs:260-455`). A column added to an existing store is backfilled
implicitly by the Zarr fill value: the new array is created at
`existing_rows + new_rows` and written only at the tail, so earlier rows read back as
fill (`src/ingest.rs:182-186, 229-245`). And a column *missing* from a later batch is
simply not extended, so it ends up shorter than its siblings.

Two observations for that conversation:

- Backfilled and genuinely-zero are indistinguishable, which is the objection the
  plan already records.
- **Our `evolve_schema` does not have this problem and does not need nulls to avoid
  it** — it backfills real values by reading each member's group, because a variable
  is by construction a hole in the group description. That is a concrete alternative
  to offer rather than an abstract complaint, and it only works because the members
  live in the same store.

## Worth stealing

`/indexes/<column>` — one Zarr array per spatial index, read whole and used for
pruning (`src/table_provider.rs:117-158`). It is a clean precedent for "extra
structures beside the table", and if we adopt the same convention their pruning works
on our stores too. It also sharpens the extra-column question: an index is a derived
structure over a column, so "how was this column derived?" and "what indexes it?"
want answering together.
