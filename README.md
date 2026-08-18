# DataCollections

**Manage heterogenous collections of multidimensional array data as a single consistent and self-describing Icechunk repository.**

Intended for sets (or "collections") of related but heterogenous multi-dimensional array data, such as:
- **Level 2 geospatial raster collections** - Satellite scenes sharing bands and dtypes but differing in projection and extent, such as Sentinel-2 L2A tiles spread across UTM zones.
- **Microscope image collections** - Fields of view sharing an axis and channel vocabulary but differing in Z-depth and channel count, such as OME-Zarr plates.
- **Fusion plasma physics experiment archives** - Diagnostic signals sharing dimension names but differing in length from shot to shot, such as MAST-U's magnetics.
- **Astronomical telescope imagery archives** - Exposures sharing detector geometry but differing in filter, target and exposure time, such as HST's WFC3/IR images.

Features:
- **Queryable** - SQL queries can scan tables of item-level metadata, before opening only the subset of data required for an analysis (e.g. with Xarray or GDAL).
- **Serverless** - All data and metadata is in the storage layer, so queries and updates can be performed without any need for a server.
- **Consistent** - Icechunk's ACID transactions ensure that the metadata about each item are updated consistently with the data.
- **Scalable** - Cloud-native properties of Icechunk and Zarr allow storing PB-scale datasets comprising millions of items.
- **General** - No domain-specific assumptions baked in - can hold any valid Zarr array data.
- **Constrainable** - Configurable schema definition means collections are self-describing, with enforcement at ingestion time.
- **Extensible** - Domain-agnostic core allows layering domain-specific conventions and query APIs on top (e.g. GeoZarr conventions + STAC API for collections of geospatial raster data).
- **Zero-copy** - Icechunk's "virtual chunks" feature allows for cataloging data in existing file formats (e.g. TIFF, COG, HDF5) without copying binary data at ingestion time.

See [PLAN.md](./PLAN.md) for the design and the reasoning behind it,
[IMPLEMENTATION.md](./IMPLEMENTATION.md) for what the code actually is and where it
departs from the plan, and [PROGRESS.md](./PROGRESS.md) for task state.

**Status: an MVP of every component runs.** The constraint language, the store
layout, both write paths, SQL query, the view layer, a STAC API, and four examples —
microscopy, geospatial, fusion and astronomy.

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

Collections of array data sit awkwardly between existing formats. 
No existing solution provides all the benefits of cloud-native data distribution in a generalizable way.

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

Notice that the two rows for "Heterogenous" and "Schema-constrained" are mutually exclusive:

- *Heterogeneous but undescribed* — Icechunk Zarr DataTree, Lance
  Blob V2. They will hold anything and tell you nothing about how the members relate.
- *Described but homogeneous* — Icechunk Zarr datacube, Iceberg + Arrow
  FixedShapeTensor, RaQuet. They describe the members precisely, by admitting only
  members that are already alike.

The closest thing (STAC + a cloud-optimized format like COG or Zarr) unecessarily bakes in domain-specific assumptions, and is not transactionally consistent, so no existing solution does it all. 
We can chart this:

```
  ├─────────────────── TOO STRICT ────────────────────┤ ├─ JUST RIGHT ──┤ ├─── TOO LOOSE ───┤
    every member identical         domain-specific       heterogenous but  no enforceable
                                                         constrained       structure

  RaQuet     Iceberg +  Icechunk   STAC +     STAC +     Icechunk          Icechunk   Lance
             Arrow FST  Zarr       COG        native     DataCollections   Zarr       Blob V2
                        datacube              Zarr                         DataTree
```

(For more details on this comparison see [COMPARISON.md](./COMPARISON.md).)

## License

Apache 2.0 — see [LICENSE](./LICENSE).
