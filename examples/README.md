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
| [`ome_zarr`](./ome_zarr/) | field of view, level 0 | Z-depth, channel count | generated locally |
| [`sentinel2_stac`](./sentinel2_stac/) | tile, full-res level | CRS, transform, GeoTIFF tags, codecs | earth-search STAC + **virtual COGs** |
| [`mastu`](./mastu/) | **(shot, diagnostic, signal)** | time-series length, units | mastapp.site, live |
| [`hst`](./hst/) | primary HDU, WFC3/IR | exposure, filter, target | MAST CAOM API, live |

Each has its own README describing the data and how it is catalogued — what one
member is, which leaves are holes, and why.

**~100 members per example, hard cap**, enforced in `_common.py`. Icechunk's scaling
in *number of nodes* (not rows) is untested and is the plan's main structural risk;
going beyond needs that investigation first rather than discovering the limits by
accident.

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
3. **COGs virtualize through the separate `virtual-tiff` package** — VirtualiZarr
   itself ships no TIFF parser. So the Sentinel-2 members are **genuinely virtual**:
   chunk references into the public COGs, real pixels readable back, and 4.3 GB of
   imagery addressed by a 0.10 MB store. Two consequences worth seeing: the GeoTIFF
   tags arrive as array attributes and differ per tile (a third opaque domain
   vocabulary), and reading a member needs `import virtual_tiff.codecs` or zarr
   cannot parse its metadata at all. MAST-U and HST stay metadata-only
   (`encoding["materialize"] = False`) because their sources are Zarr v2 and
   requester-pays FITS.
4. **Icechunk does not support Zarr consolidated metadata at all** —
   `zarr.consolidate_metadata` raises on an `IcechunkStore`. A member's description
   is therefore *derived* from the group's children rather than read from a stored
   consolidated key. The one-document-per-group decision is unaffected: it is still
   one document, with no hierarchy walking at validate time.
