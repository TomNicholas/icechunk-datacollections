# DataCollections

Queryable, self-describing collections of Zarr groups — where the collection itself
records what is consistent across its members and what is allowed to vary.

See [PLAN.md](./PLAN.md) for the design and the reasoning behind it, and
[PROGRESS.md](./PROGRESS.md) for task state.

## Motivation

Collections of array data sit awkwardly between existing formats. The useful way to
see why is to ask how much structure each format **fixes in advance**.

```
  MORE STRICT ◄─────────────────────────────────────────────► LESS STRICT

  RaQuet     Iceberg +  Icechunk   STAC +     STAC +     Icechunk   Lance
             Arrow FST  Zarr       COG        native     Zarr       Blob V2
                        datacube              Zarr       DataTree

  ├─────────────────────── Icechunk DataCollections ────────────────────────┤
     strictness declared per collection, not fixed by the format
```

| | Icechunk Zarr datacube | Icechunk Zarr DataTree | STAC + COG | STAC + native Zarr | Iceberg + Arrow FixedShapeTensor | RaQuet | Lance Blob V2 | Icechunk DataCollections |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| Consistency | ✅ | ✅ | ❌ | ❌ | ✅ | ❌ | ✅ | ✅ |
| Scalable data | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ |
| Scalable index | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Uncoordinated writes | ✅ | ✅ | ❌ | ✅ | ❌ | ❌ | ❌ | ✅ |
| Heterogeneous | ❌ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ✅ |
| Schema-constrained | ✅ | ❌ | ❌ | ❌ | ✅ | ✅ | ❌ | ✅ |
| Domain-agnostic | ✅ | ✅ | ❌ | ❌ | ✅ | ❌ | ✅ | ✅ |
| N-dimensional | ✅ | ✅ | ❌ | ✅ | ✅ | ❌ | ❌ | ✅ |

- **Consistency** — metadata and data stay in sync, transactionally.
- **Scalable data** — large arrays can be read in parts, without materialising a whole
  member to get at a slice of it.
- **Scalable index** — members matching a predicate can be found without reading all of
  them.
- **Uncoordinated writes** — many workers can write different parts of a *single
  member's* data in parallel, without coordinating with one another.
- **Heterogeneous** — members may differ in structure: shapes, dtypes, which variables
  are present.
- **Schema-constrained** — records and enforces what members have in common.
- **Domain-agnostic** — no baked-in domain vocabulary.
- **N-dimensional** — native n-d arrays, rather than tables, fixed-grid rasters, or
  opaque blobs.

**The two middle rows are the point.** Read them together and every other column falls
on one side or the other:

- *Heterogeneous but undescribed* — Icechunk Zarr DataTree, both STAC variants, Lance
  Blob V2. They will hold anything and tell you nothing about how the members relate.
- *Described but homogeneous* — Icechunk Zarr datacube, Iceberg + Arrow
  FixedShapeTensor, RaQuet. They describe the members precisely, by admitting only
  members that are already alike.

Nothing existing does both, and doing both is the thesis.

Every approach is given its best available substrate, so the comparison is between
designs rather than between storage layers. That levelling is what makes the two
closest columns informative:

- An **Icechunk Zarr datacube fails on one row only: heterogeneity.** That single cell
  is the entire motivation for this project.
- An **Icechunk Zarr DataTree fails on exactly two: scalable index and
  schema-constrained.** Which is the table restating the equation above —
  DataCollections is a DataTree plus an index plus a derived constraint.

Further notes, since a table this compact hides its reasoning:

- **Consistency, scalable data and uncoordinated writes are inherited** from Icechunk
  and Zarr rather than invented here — which is why all three Icechunk columns share
  them. What distinguishes those three columns from each other is the index, the
  heterogeneity, and the constraint.
- **Splitting scalability in two isolates one precise failure each.** Iceberg + Arrow
  FixedShapeTensor has a scalable index but not scalable data — a tensor value is read
  whole, so there is no slicing into a member. A Zarr DataTree is the mirror image: the
  data is Zarr and scales fine, but there is no index at all, so any question means
  walking every node.
- **Iceberg + Arrow FixedShapeTensor is the strongest alternative here**, and worth
  taking seriously: Iceberg supplies transactional consistency, and the tensor extension
  type supplies genuine n-dimensionality with an enforced schema. It fails on exactly
  three things — it cannot slice into a member, it needs a catalog service to commit,
  and every tensor in a column must be *identically* shaped. That last one is the
  motivating case: "same dimension names, differing lengths" is inexpressible.
- **Both STAC rows are unconstrained** because STAC describes assets as opaque and its
  `summaries` are advisory rather than derived or enforced. Pointing STAC at native Zarr
  buys n-dimensionality, and nothing else.
- **Uncoordinated writes is a Zarr property, not an Icechunk one.** It separates
  approaches whose members are internally chunked from those where a member is a single
  file or a single value. Any Zarr-backed member — including STAC pointing at native
  Zarr — lets N workers write disjoint chunks concurrently with no coordination, which is
  what makes distributed ingest of one large member possible. A COG is written by one
  process, a tensor is one Parquet value, a RaQuet tile is one row, and a Lance blob is
  one contiguous byte range: none of them can be filled in parallel from independent
  workers. (Under Icechunk the chunk writes stay uncoordinated; only the final commit
  gathers the workers' results.)
- **This is also the one row where STAC + native Zarr beats STAC + COG on something
  other than dimensionality**, which is worth knowing if you are choosing between them
  for reasons unrelated to this project.

## How each approach falls short

### A single Zarr datacube is too strict

A datacube requires every array to align on shared dimensions. One schema governs
everything, and items must be concatenable to belong at all.

That works beautifully for analysis-ready data and fails for Level-2-like data. Once
granules come from source files you do not control — as they do with virtual Zarr —
`dtype`, codecs, CRS and shape vary between them. Such arrays cannot be concatenated
into one cube, so the cube offers no way to hold them together at all.

### RaQuet is the datacube's strictness moved to a different substrate

RaQuet stores raster data in Parquet: each tile is a row, each band a column, pixels
packed as row-major binary blobs. Tiles are identified by a QUADBIN cell — which means
the format hardcodes not just "geospatial" but **one projection and one tiling
scheme**, Web Mercator on a fixed grid.

So it is at least as strict as a datacube, and in one respect stricter. It is worth
naming separately because of what it demonstrates: moving raster data from Zarr into
Parquet did not make it any more tolerant of heterogeneity. **Substrate and strictness
are independent axes.** Choosing a tabular container does not, by itself, buy you
anything on the axis that matters here.

### STAC + COG is too strict in the wrong dimension

STAC loosens the constraint on the data — assets are external files, and each may have
its own CRS, shape and dtype — while tightening a different one. It mandates a
geospatial vocabulary: geometry, bounding box, datetime. If your items are tokamak
shots or microscope fields of view, that vocabulary is not merely unnecessary but
actively wrong.

And the strictness lands where it is least useful. STAC is rigid about *geospatial
semantics* and entirely silent about *array structure* — an Item describes its assets
as opaque files, saying nothing about their dimensions, dtypes, or how they differ
from the item next to it. Rigid where you do not want it, silent where you do.

### xarray.DataTree constrains vertically, not horizontally

A `DataTree` will happily hold a heterogeneous collection of groups, and its nodes are
fully self-describing — dimensions, dtypes, coordinates and all. It is the natural
in-memory shape for what we are storing.

What it does not do is say anything about *siblings*. `DataTree`'s structure runs
**vertically**: children inherit coordinates from their parent and must align with
them. A collection needs the **horizontal** statement — that these thousands of
sibling nodes resemble one another in specified ways and differ in others. Nothing in
a tree records that, so there is nothing to check a new member against, and no way to
answer "which nodes have more than 100 timesteps" without walking every node.

The relationship is additive rather than competitive:

> **DataCollections ≈ DataTree + a derived description of what siblings share + a
> queryable index over what they do not.**

### Lance Blob V2 is not strict enough

Lance's blob columns give you a table of references to large out-of-line objects with
queryable metadata alongside — structurally close to what we want. But the referent is
an **opaque byte sequence**. Nothing describes what is inside it, so nothing is
verifiable and nothing is derivable. A blob has no schema to be consistent about.

Blob V2 is genuinely sophisticated about *placement* — it negotiates inline, packed,
dedicated or external storage per column per batch according to value size. But that
decides where the bytes live, not whether anything knows what they mean, so it moves
nothing on the axis that matters here.

Note that `DataTree` and Lance blobs are too loose in *opposite* directions, which is
what makes the pair instructive. Lance has the queryable table and an opaque referent;
`DataTree` has fully self-describing referents and no table. Neither has the piece in
between: a description of how the members relate to each other.

## The gap

Every one of these bakes its strictness in at design time. What is missing is a
collection format where **strictness is a parameter of the collection, not of the
format** — where you can say "all these groups have the same dimension names, the same
dtypes, and the same variables, but their time axes are of differing lengths", and
have that statement be machine-checkable.

Two properties make this more than a schema file sitting next to the data:

**It is enforced, not aspirational.** Every member is validated against the description in
the same transaction that writes it, so the two cannot drift apart. A schema file sitting
beside the data makes a claim; this makes a guarantee.

**It is expressed in domain-neutral terms.** Constraints are stated over core Zarr
metadata — dimension names, shapes, dtypes — which means the same machinery works for
satellite tiles, plasma diagnostics, microscopy and astronomy. Domain vocabularies
(STAC's datacube extension, OME-NGFF axes, IMAS) ride along as opaque attributes that
can be constrained without being interpreted.

The payoff is that formats like STAC become *views* derived from the collection rather
than the way it is stored — one projection among several, and an optional one.

