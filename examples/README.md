# Examples

Four domains, three of them with **no STAC in the stack anywhere**. That is the
point: if the core could not express the microscopy, fusion or astronomy cases
without geospatial vocabulary somewhere, the factoring would be wrong. There is a
test asserting the three non-geospatial examples never reach for a `stac_*` symbol.

```bash
python examples/ome_zarr/run.py       -n 40          # microscopy   — implemented first
python examples/sentinel2_stac/run.py -n 30          # geospatial   — the only STAC one
python examples/mastu/run.py          -n 40          # fusion
python examples/hst/run.py            -n 30          # astronomy
```

Add `--offline` to run from recorded metadata with no network. `pytest examples`
runs all four that way.

| example | referenced unit | what varies | source of metadata |
|---|---|---|---|
| `ome_zarr` | field of view, level 0 | Z-depth, channel count | generated locally |
| `sentinel2_stac` | tile, full-res level | CRS, affine transform | earth-search STAC API, live |
| `mastu` | **(shot, diagnostic, signal)** | time-series length, units | mastapp.site, live |
| `hst` | primary HDU, WFC3/IR | exposure, filter, target | MAST CAOM API, live |

**~100 members per example, hard cap**, enforced in `_common.py`. Icechunk's scaling
in *number of nodes* (not rows) is untested and is the plan's main structural risk;
going beyond needs that investigation first rather than discovering the limits by
accident.

## What each example is for

**OME-Zarr — the hardest test of "domain vocabulary as opaque JSON".** `multiscales`
and `omero` are wildcards: constrained in position, uninterpreted in content, stored
verbatim, reinstated exactly. Implemented before any STAC code existed.

**Sentinel-2 — the only example exercising the view layer.** Heterogeneous CRS is
the motivating case, and it shows the language's honest limit: with enums excluded,
`proj:epsg` can only be "an integer", which is *imprecise rather than wrong*.

**MAST-U — a live instance of the problem.** FAIR-MAST publishes a JSON REST API for
shot metadata *alongside* the Zarr store; here they are one queryable thing.

**HST — the example that motivates cohorts.** Scoped to WFC3/IR because different
instruments are structurally different groups, which the v1 language cannot express
in one collection.

## Findings from running against real data

Recorded here because PLAN.md flags the examples section as intent rather than fact.

1. **MAST-U structure, confirmed.** `s3://mast/level1/shots/<shot>.zarr` is **Zarr
   v2**, with per-diagnostic subgroups: `/<source>/<signal>`, e.g.
   `/amc/plasma_current`. A shot is therefore not a flat referenced unit, as the plan
   suspected.
2. **Signal availability varies *within* a diagnostic**, not only across
   diagnostics — so (shot, diagnostic) would still hit optionality. The example takes
   **(shot, diagnostic, signal)**, which is the plan's own technique applied one level
   deeper than it predicted: choose a finer referenced unit and optionality becomes
   member absence again.
3. **VirtualiZarr 2.5.1 has no TIFF/COG parser** (FITS, HDF, NetCDF3, Zarr, DMRPP,
   Kerchunk only), so the Sentinel-2 example cannot virtualize the COGs. Members are
   written **metadata-only** — full shape, dtype and dimension names, no chunks —
   via `encoding["materialize"] = False`. Same stopgap in MAST-U and HST. When a TIFF
   parser lands, only each example's `build_member` changes; `add_item` already
   routes a Dataset of `ManifestArray`s through VirtualiZarr's Icechunk writer.
4. **Icechunk does not support Zarr consolidated metadata at all** —
   `zarr.consolidate_metadata` raises on an `IcechunkStore`. A member's description
   is therefore *derived* from the group's children rather than read from a stored
   consolidated key. The one-document-per-group decision is unaffected: it is still
   one document, with no hierarchy walking at validate time.
