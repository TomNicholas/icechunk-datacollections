"""Sentinel-2 L2A — geospatial, and the only example that exercises the view layer.

Heterogeneous CRS across tiles is the motivating case: with enums excluded from the
language, `proj:epsg` can only be `{"$var": "proj_epsg", "type": "integer"}`, which
is *imprecise rather than wrong*. Scoping a collection to one UTM zone tightens it;
the honest v1 answer is that a tighter statement needs cohorts, which are deferred.

Metadata comes from the Element84 `earth-search` STAC API over the AWS Open Data
COGs. **v1 virtualizes the full-resolution level only** — the COGs' internal
overviews are a variable-cardinality case (`$each`/`$count`) and wait for M7.

Two honest shortcuts, both recorded rather than hidden:

- **VirtualiZarr 2.5.1 ships no TIFF/COG parser** (`virtualizarr.parsers` has FITS,
  HDF, NetCDF3, Zarr, DMRPP and Kerchunk). So the bands are written metadata-only —
  full shape, dtype and dimension names, no chunks — rather than as virtual chunk
  references. When a TIFF parser lands, only `build_member` changes.
- `--offline` uses recorded item metadata so the example runs with no network.

Run:  python examples/sentinel2_stac/run.py -n 30
      python examples/sentinel2_stac/run.py -n 6 --offline
"""

from __future__ import annotations

import json
import pathlib
import sys
import urllib.request

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


def build_member(item: dict) -> tuple[xr.Dataset, dict]:
    """One tile: three 10 m bands, described but not materialised."""
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

    ds = xr.Dataset(
        arrays,
        attrs={
            "proj:epsg": int(props.get("proj:epsg") or 4326),
            "proj:transform": list(transform) if transform else [],
            "constellation": props.get("constellation", "sentinel-2"),
            "cube:dimensions": {
                "x": {"type": "spatial", "axis": "x"},
                "y": {"type": "spatial", "axis": "y"},
            },
        },
    )
    extras = {
        "granule": item["id"],
        "datetime": props["datetime"],
        "cloud_cover": float(props.get("eo:cloud_cover") or 0.0),
        "bbox": json.dumps([float(v) for v in item["bbox"]]),
    }
    return ds, extras


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
    return Constraint(doc)


def main() -> None:
    args = parse_args("sentinel2_stac", default_n=30)
    banner(f"Sentinel-2 L2A: {args.members} tiles, the one example that uses STAC")

    items = load_items(args.members, args.offline)
    print(f"got {len(items)} STAC items; UTM zones present: "
          f"{sorted({i['properties'].get('proj:epsg') for i in items})}")

    repo = fresh_repo(args.store)
    coll = create_collection(
        repo,
        constraint=None,
        extra_columns=[
            ExtraColumn("granule", "string", "human-meaningful id; member ids are opaque"),
            ExtraColumn("datetime", "string", "extracted at ingest for query and for the view"),
            ExtraColumn("cloud_cover", "float64"),
            ExtraColumn("bbox", "string", "WKB in a real store; JSON text here"),
        ],
    )

    ds, extras = build_member(items[0])
    coll.add_item(ds, extras=extras)
    report = coll.evolve_schema(author_constraint(coll.constraint.document))
    print(f"\n{report}")

    ingest(coll, (build_member(item) for item in items[1:]))
    print(f"\n{len(coll)} members; holes: {[d['name'] for d in coll.constraint.declarations]}")

    banner("SQL over the table")
    show_table(coll, "SELECT granule, proj_epsg, cloud_cover FROM members ORDER BY cloud_cover")
    show_table(coll, "SELECT proj_epsg, COUNT(*) AS tiles FROM members GROUP BY proj_epsg")

    banner("STAC is a view over the table, not the storage format")
    view = stac_item_view(
        collection="sentinel-2-l2a",
        id=column("granule"),
        datetime=column("datetime"),
        bbox=column("bbox"),
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
    print(f"\nstore: {args.store}")


if __name__ == "__main__":
    main()
