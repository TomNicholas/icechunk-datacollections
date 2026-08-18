"""Sentinel-2 L2A — geospatial, and the only example that exercises the view layer.

Heterogeneous CRS across tiles is the motivating case: with enums excluded from the
language, `proj:epsg` can only be `{"$var": "proj_epsg", "type": "integer"}`, which
is *imprecise rather than wrong*. Scoping a collection to one UTM zone tightens it;
the honest v1 answer is that a tighter statement needs cohorts, which are deferred.

Metadata comes from the Element84 `earth-search` STAC API over the AWS Open Data
COGs. **v1 virtualizes the full-resolution level only** — the COGs' internal
overviews are a variable-cardinality case (`$each`/`$count`) and wait for M7.

**The members are genuinely virtual.** The COGs are read through
[`virtual-tiff`](https://pypi.org/project/virtual-tiff/)'s `VirtualTIFF` parser, so
each member's group holds chunk *references* into the public COGs on S3 — no pixels
are copied, and the whole store is metadata plus manifests. That also makes the
example a real test of the wildcard: a COG's `codecs` list is whatever the file
uses, and the constraint declines to describe it rather than aligning it
element-wise.

`--offline` falls back to recorded item metadata and metadata-only members, so the
example and the test suite run with no network.

Run:  python examples/sentinel2_stac/run.py -n 30
      python examples/sentinel2_stac/run.py -n 6 --offline
"""

from __future__ import annotations

import json
import pathlib
import shutil
import sys
import urllib.request

import icechunk

import numpy as np
import xarray as xr

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from _common import array_pointer, banner, fresh_repo, ingest, parse_args, show_table, substitute_leaf
from datacollections import (
    Constraint,
    ExtraColumn,
    column,
    create_collection,
    stac_collection,
    stac_item_view,
    var,
    wild,
)

SEARCH = "https://earth-search.aws.element84.com/v1/search"
BANDS = ["blue", "red", "nir"]  # B02, B04, B08 — all 10 m, so all one shape
COG_HOST = "https://sentinel-cogs.s3.us-west-2.amazonaws.com"
RECORDED = pathlib.Path(__file__).parent / "recorded_items.json"


#: Deliberately far apart, so the tiles land in different UTM zones — heterogeneous
#: CRS is the whole reason this is the motivating geospatial case.
REGIONS = [
    [-9.5, 51.0, -8.5, 52.0],   # Ireland, zone 29
    [-3.5, 48.0, -2.5, 49.0],   # Brittany, zone 30
    [4.5, 45.0, 5.5, 46.0],     # Rhône-Alpes, zone 31
    [13.5, 41.5, 14.5, 42.5],   # Lazio, zone 33
    [24.5, 59.0, 25.5, 60.0],   # Estonia, zone 35
]


def fetch_items(n: int) -> list[dict]:
    per_region = max(1, n // len(REGIONS) + 1)
    items: list[dict] = []
    for bbox in REGIONS:
        query = {
            "collections": ["sentinel-2-l2a"],
            "limit": per_region,
            "bbox": bbox,
            "query": {"eo:cloud_cover": {"lt": 20}},
        }
        request = urllib.request.Request(
            SEARCH,
            data=json.dumps(query).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            items += json.load(response)["features"]
        if len(items) >= n:
            break
    return items[:n]


def load_items(n: int, offline: bool) -> list[dict]:
    if not offline:
        try:
            items = fetch_items(n)
            RECORDED.write_text(json.dumps(items[:6], indent=1))
            return items
        except Exception as e:  # network is not the point of this example
            print(f"  earth-search unreachable ({e}); falling back to recorded items")
    items = json.loads(RECORDED.read_text())
    return [items[i % len(items)] for i in range(n)]


def registry():
    """An object-store registry for the public COG bucket, read anonymously."""
    import obstore
    from obspec_utils.registry import ObjectStoreRegistry

    return ObjectStoreRegistry({COG_HOST: obstore.store.HTTPStore.from_url(COG_HOST)})


def virtual_repo(path):
    """A repo allowed to hold references into the COG bucket.

    The container name must be a *prefix* of the chunk reference URLs, which is why
    it is the bucket URL rather than a label. Authorization is the caller's business:
    it is a property of the repository, and in general involves their credentials.
    """
    config = icechunk.RepositoryConfig(
        virtual_chunk_containers={
            COG_HOST: icechunk.VirtualChunkContainer(
                url_prefix=f"{COG_HOST}/", store=icechunk.http_store()
            )
        }
    )
    path = pathlib.Path(path)
    if path.exists():
        shutil.rmtree(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return icechunk.Repository.create(
        icechunk.local_filesystem_storage(str(path)),
        config=config,
        authorize_virtual_chunk_access={COG_HOST: None},
    )


def build_virtual_member(item: dict, reg) -> tuple[xr.Dataset, dict]:
    """One tile, as virtual chunk references into the COGs.

    `ifd=0` is the full-resolution image. A COG's later IFDs are its overviews, which
    are a variable-cardinality case — smaller scenes have fewer — so v1 takes level 0
    only and leaves `$each`/`$count` to M7.
    """
    from virtualizarr import open_virtual_dataset
    from virtual_tiff import VirtualTIFF

    arrays = {}
    for band in BANDS:
        vds = open_virtual_dataset(
            item["assets"][band]["href"], parser=VirtualTIFF(ifd=0), registry=reg
        )
        variable = vds[next(iter(vds.data_vars))]
        arrays[band] = variable.rename({d: n for d, n in zip(variable.dims, ("y", "x"))})

    ds = xr.Dataset(arrays, attrs=_attributes(item))
    return ds, _extras(item)


def _attributes(item: dict) -> dict:
    props = item["properties"]
    assets = item["assets"]
    transform = assets[BANDS[0]].get("proj:transform") or props.get("proj:transform")
    return {
        "proj:epsg": int(props.get("proj:epsg") or 4326),
        "proj:transform": list(transform) if transform else [],
        "constellation": props.get("constellation", "sentinel-2"),
        "cube:dimensions": {
            "x": {"type": "spatial", "axis": "x"},
            "y": {"type": "spatial", "axis": "y"},
        },
    }


def _extras(item: dict) -> dict:
    props = item["properties"]
    return {
        "granule": item["id"],
        "datetime": props["datetime"],
        "cloud_cover": float(props.get("eo:cloud_cover") or 0.0),
        "bbox_wgs84": [float(v) for v in item["bbox"]],
    }


def build_member(item: dict) -> tuple[xr.Dataset, dict]:
    """The offline fallback: three bands described but not materialised."""
    props = item["properties"]
    assets = item["assets"]
    shape = tuple(assets[BANDS[0]].get("proj:shape") or props.get("proj:shape") or (10980, 10980))
    transform = assets[BANDS[0]].get("proj:transform") or props.get("proj:transform")

    arrays = {}
    for band in BANDS:
        # broadcast_to is a view, so declaring a 10980x10980 band costs nothing
        values = np.broadcast_to(np.zeros(1, dtype="uint16"), shape)
        v = xr.Variable(("y", "x"), values, attrs={"band": band})
        v.encoding["chunks"] = (1024, 1024)
        v.encoding["materialize"] = False
        arrays[band] = v

    return xr.Dataset(arrays, attrs=_attributes(item)), _extras(item)


def author_constraint(first: dict) -> Constraint:
    """What varies across tiles, said explicitly."""
    doc = first
    doc = substitute_leaf(
        doc,
        "/attributes/proj:epsg",
        var("proj_epsg", type="integer", minimum=1024, maximum=32766),
    )
    # the affine transform differs per tile and is not a scalar, so it is a wildcard
    # stored verbatim — not aligned element-wise
    doc = substitute_leaf(doc, "/attributes/proj:transform", wild("proj_transform"))
    for band in BANDS:
        arrays = doc["consolidated_metadata"]["metadata"]
        # A real COG's codecs are whatever the file uses, so the wildcard declines to
        # describe the list rather than aligning it element-wise.
        if isinstance(arrays[band]["codecs"], list):
            doc = substitute_leaf(doc, array_pointer(band, "codecs"), wild(f"codecs_{band}"))
        # virtual-tiff carries the TIFF/GeoTIFF tags through as array attributes, and
        # they genuinely differ per tile — `model_tiepoint` is the scene's origin in
        # projected coordinates. A third domain vocabulary the language constrains in
        # position and declines to interpret, alongside OME's and IMAS's.
        if isinstance(arrays[band]["attributes"], dict):
            doc = substitute_leaf(doc, array_pointer(band, "attributes"), wild(f"tags_{band}"))
    return Constraint(doc)


def main() -> None:
    args = parse_args("sentinel2_stac", default_n=30)
    banner(f"Sentinel-2 L2A: {args.members} tiles, the one example that uses STAC")

    items = load_items(args.members, args.offline)
    print(f"got {len(items)} STAC items; UTM zones present: "
          f"{sorted({i['properties'].get('proj:epsg') for i in items})}")

    virtual = not args.offline
    if virtual:
        try:
            import virtual_tiff  # noqa: F401
        except ImportError:
            print("  virtual-tiff not installed; falling back to metadata-only members")
            virtual = False

    if virtual:
        reg = registry()
        build = lambda item: build_virtual_member(item, reg)  # noqa: E731
        repo = virtual_repo(args.store)
        print("  members are virtual: chunk references into the public COGs, no pixels copied")
    else:
        build = build_member
        repo = fresh_repo(args.store)
        print("  members are metadata-only (offline)")
    coll = create_collection(
        repo,
        constraint=None,
        extra_columns=[
            ExtraColumn("granule", "string", "human-meaningful id; member ids are opaque"),
            ExtraColumn("datetime", "string", "extracted at ingest for query and for the view"),
            ExtraColumn("cloud_cover", "float64"),
            ExtraColumn(
                "bbox_wgs84",
                "string",
                "the Item's bbox. NOT named `bbox`: upstream's schema builder hard-errors "
                "on a column of that name unless it is Zarr bytes, which we cannot yet "
                "write. See docs/upstream-zarr-datafusion-search.md",
                encoding="json",
            ),
        ],
    )

    ds, extras = build(items[0])
    coll.add_item(ds, extras=extras)
    report = coll.evolve_schema(author_constraint(coll.constraint.document))
    print(f"\n{report}")

    ingest(coll, (build(item) for item in items[1:]))
    print(f"\n{len(coll)} members; holes: {[d['name'] for d in coll.constraint.declarations]}")

    banner("SQL over the table")
    show_table(coll, "SELECT granule, proj_epsg, cloud_cover FROM members ORDER BY cloud_cover")
    show_table(coll, "SELECT proj_epsg, COUNT(*) AS tiles FROM members GROUP BY proj_epsg")

    banner("STAC is a view over the table, not the storage format")
    view = stac_item_view(
        collection="sentinel-2-l2a",
        id=column("granule"),
        datetime=column("datetime"),
        bbox=column("bbox_wgs84"),
        properties={
            "proj:epsg": column("proj_epsg"),
            "eo:cloud_cover": column("cloud_cover"),
            "constellation": {"$from": "description:/attributes/constellation"},
        },
    )
    item = coll.render(view, coll.member_ids[0])
    print(json.dumps(item, indent=2)[:700])

    doc = stac_collection("sentinel-2-l2a", "Sentinel-2 L2A tiles", coll.constraint)
    print("\nCollection summaries, derived from the variable domains:")
    print(json.dumps(doc["summaries"], indent=2))

    if virtual:
        banner("The references resolve: real pixels, out of a store with no pixels in it")
        # Registering the TIFF codecs is a *reader* requirement — without it zarr
        # cannot even parse the array metadata, since a codec it does not know is a
        # hard failure. Worth seeing: it is the same must-understand argument that
        # made `zarr.group_ref` an Arrow extension type rather than a Zarr dtype.
        import virtual_tiff.codecs  # noqa: F401
        import zarr

        root = zarr.open_group(repo.readonly_session("main").store, mode="r")
        band = root[f"groups/{coll.member_ids[0]}/red"]
        print(f"  {band.shape} {band.dtype}, chunks {band.chunks}")
        print(f"  pixels at [5000:5002, 5000:5002]: {band[5000:5002, 5000:5002].tolist()}")
        pixels = len(coll) * len(BANDS) * band.shape[0] * band.shape[1] * 2
        size = sum(f.stat().st_size for f in pathlib.Path(args.store).rglob("*") if f.is_file())
        print(f"  {pixels / 1e9:.1f} GB of imagery addressed by a {size / 1e6:.2f} MB store")

    print(f"\nstore: {args.store}")


if __name__ == "__main__":
    main()
