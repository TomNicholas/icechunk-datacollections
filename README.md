# datacollections

Queryable, self-describing collections of Zarr groups — where the collection itself
records what is consistent across its members and what is allowed to vary.

See [PLAN.md](./PLAN.md) for the design.

## Motivation

Collections of array data sit awkwardly between existing formats. The useful way to
see why is to ask how much structure each format **fixes in advance**.

```
    MORE STRICT                                              LESS STRICT
    ├───────────┬───────────┬──────────────┬─────────────────┬────────────┤
    single       RaQuet      STAC + COG     datacollections   Lance blob
    Zarr
    datacube
```

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

### Lance blob is not strict enough

Lance's blob columns give you a table of references to large out-of-line objects with
queryable metadata alongside — structurally close to what we want. But the referent is
an **opaque byte sequence**. Nothing describes what is inside it, so nothing is
verifiable and nothing is derivable. A blob has no schema to be consistent about.

### The gap

Every one of these bakes its strictness in at design time. What is missing is a
collection format where **strictness is a parameter of the collection, not of the
format** — where you can say "all these groups have the same dimension names, the same
dtypes, and the same variables, but their time axes are of differing lengths", and
have that statement be machine-checkable.

Two properties make this more than a schema file sitting next to the data:

**It is derived, not authored.** The description of what varies is computed from the
members themselves, by generalising over them. So it is always true of the data rather
than aspirational, and it can be recomputed to verify or repair.

**It is expressed in domain-neutral terms.** Constraints are stated over core Zarr
metadata — dimension names, shapes, dtypes — which means the same machinery works for
satellite tiles, plasma diagnostics, microscopy and astronomy. Domain vocabularies
(STAC's datacube extension, OME-NGFF axes, IMAS) ride along as opaque attributes that
can be constrained without being interpreted.

The payoff is that formats like STAC become *views* derived from the collection rather
than the way it is stored — one projection among several, and an optional one.
