# OME-Zarr microscopy

A collection of microscope fields of view. **No STAC anywhere in the stack** — this
example was built before any STAC code existed, deliberately, as the check that the
core carries no geospatial assumptions.

```bash
python examples/ome_zarr/run.py -n 40
```

## The data

OME-NGFF (OME-Zarr) 0.5 images, generated locally by `run.py` rather than downloaded,
so the example needs no network and no credentials. Each field of view is a small
`uint16` image with the attribute vocabulary the format actually uses: `multiscales`
with named `axes` (`c`, `z`, `y`, `x`, each with a type and unit),
`coordinateTransformations`, and an `omero` rendering block listing the channels.

That vocabulary is the point. OME-NGFF has the richest attributes of the four
examples, so it is the hardest test of "constrain a domain vocabulary without
interpreting it".

## The referenced unit: one field of view, at resolution level 0

A real OME-Zarr plate nests plate → well → field of view → multiscale levels. A
member here is **one field of view at level 0** — a flat group holding a single array.

Everything above that level is deferred, not lost: varying numbers of wells is
variable cardinality (`$each` / `$count`), and the multiscale pyramid needs nesting.
Choosing the finer unit is what lets v1's language describe this domain at all.

## How it is cataloged

One member is a group holding one array, `0`, of shape `(nc, nz, 256, 256)` and dtype
`uint16`, with dimension names `c, z, y, x`.

| hole | kind | where it appears | why |
|---|---|---|---|
| `nz` | variable, integer 1–64 | `/attributes/acquisition/nz` **and** `shape[1]` | Z-depth varies per FOV, and the attribute must agree with the array |
| `nc` | variable, integer 1–8 | `/attributes/acquisition/nchannels` **and** `shape[0]` | channel count, likewise |
| `multiscales` | wildcard | `/attributes/ome/multiscales` | OME's own vocabulary: constrained in position, uninterpreted in content |
| `omero` | wildcard | `/attributes/ome/omero` | rendering metadata, likewise |

`nz` and `nc` each appear **twice**, which is the co-constraint: within one member the
acquisition metadata and the array shape must agree, and across members they may
differ freely. That statement is the thing JSON Schema cannot express, and it is why
this project has its own language.

Everything else is a literal — dtype, chunk shape, the Y and X extents, the axis
names — so a member that differs in any of them is rejected until someone says
explicitly that it may vary.

Three **extra columns** carry what the group itself does not know: `plate`, `well`
and `fov`. They are not recomputable from the member, which is why they are supplied
to `add_item` rather than derived.

## What the run shows

The first member bootstraps an all-literal constraint, so the *second* field of view
is rejected — its Z-depth differs. `evolve_schema` then loosens exactly the four
leaves above and backfills their columns by re-reading the members already written.
After that the collection is queryable:

```sql
SELECT nz, COUNT(*) AS fovs FROM members GROUP BY nz ORDER BY nz
```

and each member's full `zarr.json` is reconstructible from its row alone — including
the `omero` block, verbatim, out of a wildcard column.

The run ends by projecting a member through a view with no STAC vocabulary in it at
all, which is the same machinery the STAC example uses with a different template.
