"""Do the Items we derive agree with the Items they came from?

PLAN.md asks for Item derivation to be "verified by round-tripping real STAC
Items". This is that test, against the cached earth-search items the Sentinel-2
example fetched — so it needs no network.

What it can and cannot claim, stated precisely, because the difference is the whole
point of the view layer:

- **Extra columns round-trip exactly.** `id`, `datetime`, `bbox`, `eo:cloud_cover`
  are carried through ingest and reappear in the derived Item unchanged.
- **Properties read from the description round-trip exactly**, which is the stronger
  claim: `proj:epsg` goes source Item → group attributes → constraint binding →
  column → `substitute` → view, and comes back equal.
- **The derived Item is not the source Item.** It carries what was ingested, not
  everything earth-search publishes. A DataCollections store is not a STAC mirror,
  and a view that reproduced every source field would only be proving that we copied
  a JSON blob around.
"""

import json
import pathlib
import sys

import icechunk
import numpy as np
import pytest
import xarray as xr

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from datacollections import ExtraColumn, column, create_collection, stac_item_view, var
from datacollections_stac import Backend

EXAMPLE = pathlib.Path(__file__).resolve().parents[3] / "examples" / "sentinel2_stac"
RECORDED = EXAMPLE / "recorded_items.json"


@pytest.fixture(scope="module")
def source_items() -> list[dict]:
    if not RECORDED.exists():
        pytest.skip("no recorded STAC items; run examples/sentinel2_stac/run.py once")
    return json.loads(RECORDED.read_text())


@pytest.fixture
def collection(tmp_path, source_items):
    sys.path.insert(0, str(EXAMPLE))
    from run import build_member  # the example's own offline builder

    repo = icechunk.Repository.create(
        icechunk.local_filesystem_storage(str(tmp_path / "store"))
    )
    coll = create_collection(
        repo,
        constraint=None,
        extra_columns=[
            ExtraColumn("granule", "string"),
            ExtraColumn("datetime", "string"),
            ExtraColumn("cloud_cover", "float64"),
            ExtraColumn("bbox", "string", encoding="json"),
        ],
    )
    ds, extras = build_member(source_items[0])
    coll.add_item(ds, extras=extras)

    doc = json.loads(json.dumps(coll.constraint.document))
    doc["attributes"]["proj:epsg"] = var("proj_epsg", type="integer", minimum=1024, maximum=32766)
    doc["attributes"]["proj:transform"] = {"$wild": "proj_transform"}
    coll.evolve_schema(doc)
    for item in source_items[1:]:
        ds, extras = build_member(item)
        coll.add_item(ds, extras=extras)
    return coll, source_items


def item_view():
    return stac_item_view(
        collection="sentinel-2-l2a",
        id=column("granule"),
        datetime=column("datetime"),
        bbox=column("bbox"),
        properties={
            "proj:epsg": column("proj_epsg"),
            "eo:cloud_cover": column("cloud_cover"),
        },
    )


def test_derived_items_agree_with_their_sources(collection):
    coll, sources = collection
    view = item_view()
    by_id = {s["id"]: s for s in sources}

    for member_id in coll.member_ids:
        derived = coll.render(view, member_id)
        source = by_id[derived["id"]]

        assert derived["type"] == source["type"] == "Feature"
        assert derived["properties"]["datetime"] == source["properties"]["datetime"]
        assert derived["bbox"] == pytest.approx(source["bbox"])
        assert derived["properties"]["eo:cloud_cover"] == pytest.approx(
            source["properties"]["eo:cloud_cover"]
        )
        # the interesting one: this came back through the constraint, not a column copy
        assert derived["properties"]["proj:epsg"] == source["properties"]["proj:epsg"]


def test_the_epsg_really_did_go_through_the_description(collection):
    """`proj:epsg` is a *variable*, so its value is reconstructed by `substitute`
    from the member's row — the derived Item is a projection of the store, not a
    cached copy of the source Item."""
    coll, sources = collection
    member_id = coll.member_ids[0]
    described = coll.describe(member_id)
    assert described["attributes"]["proj:epsg"] == coll.row(member_id)["proj_epsg"]

    view = stac_item_view(
        collection="c",
        id=column("granule"),
        datetime=column("datetime"),
        properties={"proj:epsg": {"$from": "description:/attributes/proj:epsg"}},
    )
    # read from the description or read from the column — same answer, by construction
    assert coll.render(view, member_id)["properties"]["proj:epsg"] == (
        coll.render(item_view(), member_id)["properties"]["proj:epsg"]
    )


def test_a_derived_item_carries_what_was_ingested_and_no_more(collection):
    """The honest limit: a DataCollections store is not a STAC mirror."""
    coll, sources = collection
    derived = coll.render(item_view(), coll.member_ids[0])
    source = sources[0]
    missing = set(source["properties"]) - set(derived["properties"])
    assert missing, "the view is not expected to reproduce every source property"
    assert "eo:cloud_cover" not in missing


def test_the_api_serves_the_same_items(collection):
    coll, sources = collection
    backend = Backend(coll, item_view(), collection_id="sentinel-2-l2a", bbox_column="bbox")
    result = backend.search(limit=100)
    assert result.matched == len(coll)
    assert {i["id"] for i in result.items} == {s["id"] for s in sources[: len(coll)]}
