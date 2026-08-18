# Sentinel-2 L2A — geospatial

A collection of Sentinel-2 scenes, catalogued from live STAC metadata with the pixels
left where they are. The only example that exercises the view layer and the STAC API.

```bash
python examples/sentinel2_stac/run.py -n 30      # live
python examples/sentinel2_stac/run.py -n 6 --offline
```

## The data

Sentinel-2 L2A scenes from the AWS Open Data COGs, discovered through Element 84's
[earth-search](https://earth-search.aws.element84.com/v1) STAC API. The example
queries five widely separated regions so the results land in **different UTM zones**,
which is the whole reason this is the motivating geospatial case.

Three 10 m bands are catalogued per scene — `blue`, `red`, `nir` (B02, B04, B08) —
each 10980 × 10980 `uint16`, tiled 1024 × 1024.

**The members are virtual.** Each band is read with
[`virtual-tiff`](https://pypi.org/project/virtual-tiff/)'s `VirtualTIFF` parser and
written as **chunk references** into the public COGs, so no pixel data is copied. At
30 scenes that is roughly **21.7 GB of imagery addressed by a 0.59 MB store**, and
the run finishes by reading real pixels back out through those references.

Reading a member needs `import virtual_tiff.codecs` first: without it zarr cannot
even parse the array metadata, because a codec it does not recognise is a hard
failure. That is the same must-understand argument that made `zarr.group_ref` an
Arrow extension type rather than a custom Zarr dtype, seen from the other side.

## The referenced unit: one scene, full-resolution level only

A COG's later IFDs are its overviews, and smaller scenes have fewer of them — a
variable-cardinality case that needs `$each` / `$count`. So `VirtualTIFF(ifd=0)` takes
level 0 and the pyramid waits.

## How it is cataloged

One member is a group of three arrays, all `(10980, 10980)` `uint16` with dimension
names `y, x`.

| hole | kind | where it appears | why |
|---|---|---|---|
| `proj_epsg` | variable, integer 1024–32766 | `/attributes/proj:epsg` | scenes sit in different UTM zones |
| `proj_transform` | wildcard | `/attributes/proj:transform` | the affine transform is a list, not a scalar, so it is stored whole |
| `codecs_{blue,red,nir}` | wildcard | each band's `codecs` | whatever compression the COG actually uses |
| `tags_{blue,red,nir}` | wildcard | each band's `attributes` | the GeoTIFF tags `virtual-tiff` carries through |

**`proj:epsg` shows the language's honest limit.** With enums excluded, its domain
can only say "an integer in the EPSG range" — imprecise rather than wrong. Saying
"one of these three zones" would need cohorts, which are deferred.

**The GeoTIFF tags are a third domain vocabulary** the language constrains in position
and declines to interpret, alongside OME's `multiscales` and MAST-U's IMAS block.
They genuinely differ per scene — `model_tiepoint` is the scene origin in projected
coordinates — and the pre-check caught that on the second member, before writing.

Four **extra columns** exist for query and view convenience: `granule` (the archive's
own id, since member ids are opaque hashes), `datetime`, `cloud_cover`, and
`bbox_wgs84`.

**Why `bbox_wgs84` and not `bbox`.** `zarr-datafusion-search`'s schema builder
*hard-errors* on a column named `bbox` that is not Zarr `bytes` — it refuses the
whole store, not just that column. Renaming costs nothing: the STAC Item still has a
`bbox` field, and bounding-box search still works. What is deferred is pushing that
filter into the scan, which needs a WKB geometry column and so a `binary` dtype the
layout does not have yet.

## The STAC view

STAC enters here and nowhere else, as a template rendered per member:

```python
stac_item_view(
    collection="sentinel-2-l2a",
    id=column("granule"),
    datetime=column("datetime"),
    bbox=column("bbox_wgs84"),
    properties={"proj:epsg": column("proj_epsg"), ...},
)
```

The Collection document's `summaries` are not authored separately — they fall out of
the variable domains, so `proj_epsg`'s declared range *is* the summary.

`python/stac-api-backend` serves these Items over stac-fastapi, and
`tests/test_stac_roundtrip.py` checks a derived Item against the earth-search Item it
came from.
