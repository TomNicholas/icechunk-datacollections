# DataCollections

Queryable, self-describing collections of Zarr groups — where the collection itself
records what is consistent across its members and what is allowed to vary.

See [PLAN.md](./PLAN.md) for the design and the reasoning behind it,
[IMPLEMENTATION.md](./IMPLEMENTATION.md) for what the code actually is and where it
departs from the plan, and [PROGRESS.md](./PROGRESS.md) for task state.

**Status: an MVP of every component runs.** The constraint language, the store
layout, both write paths, SQL query, the view layer, a STAC API, and four examples —
microscopy, geospatial, fusion and astronomy, three of them with no STAC anywhere in
the stack.

```python
import icechunk
from datacollections import create_collection, var

repo = icechunk.Repository.create(icechunk.local_filesystem_storage("store"))
coll = create_collection(repo, constraint=None)   # first member sets the constraint
member_id = coll.add_item(ds)                     # one atomic transaction

# say explicitly what may vary, and the writer backfills the new column by reading
coll.evolve_schema(looser_constraint)
coll.sql("SELECT member_id, nt FROM members WHERE nt > 1000")
coll.describe(member_id)      # the member's zarr.json, reconstructed exactly
```

```bash
make venv build test      # 61 Rust tests, 64 Python tests
make examples             # all four domains, against live APIs
```


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

The rest of that argument — why each existing approach falls short, what the table's
closest columns tell you, and what exactly is missing — is in
[COMPARISON.md](./COMPARISON.md).
