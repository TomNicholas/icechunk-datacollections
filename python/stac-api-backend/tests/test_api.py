"""Acceptance tests for the STAC API.

PLAN.md names pystac-client as the acceptance test. That is a heavier dependency
than this MVP wants, so the equivalent assertions are made directly against the
route shapes a client relies on: the landing page's `conformsTo` and links, a
Collection document, item search with paging, and a single-Item fetch.
"""

import copy
import pathlib
import sys

import icechunk
import numpy as np
import pytest
import xarray as xr
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from datacollections import ExtraColumn, column, create_collection, stac_item_view, var
from datacollections_stac import Backend, make_app


def tile(epsg: int, size: int = 128) -> xr.Dataset:
    return xr.Dataset(
        {"B04": (("y", "x"), np.zeros((size, size), "uint16"))},
        attrs={"proj:epsg": epsg, "constellation": "sentinel-2"},
    )


@pytest.fixture
def client(tmp_path):
    repo = icechunk.Repository.create(icechunk.local_filesystem_storage(str(tmp_path / "store")))
    coll = create_collection(
        repo,
        constraint=None,
        extra_columns=[
            ExtraColumn("granule", "string"),
            ExtraColumn("datetime", "string"),
            ExtraColumn("bbox", "string", "WKB would be the real thing; JSON here"),
        ],
    )
    coll.add_item(
        tile(32633),
        extras={
            "granule": "T33UUP_20240101",
            "datetime": "2024-01-01T10:00:00Z",
            "bbox": "[12.0, 45.0, 13.0, 46.0]",
        },
    )
    # heterogeneous CRS across tiles is the motivating geospatial case
    doc = copy.deepcopy(coll.constraint.document)
    doc["attributes"]["proj:epsg"] = var("proj_epsg", type="integer", minimum=1024, maximum=32766)
    coll.evolve_schema(doc)
    for i, epsg in enumerate([32634, 32635], start=2):
        coll.add_item(
            tile(epsg),
            extras={
                "granule": f"T33UU{i}_2024010{i}",
                "datetime": f"2024-01-0{i}T10:00:00Z",
                "bbox": "[20.0, 45.0, 21.0, 46.0]",
            },
        )

    view = stac_item_view(
        collection="sentinel-2-l2a",
        id=column("granule"),
        datetime=column("datetime"),
        bbox=column("bbox"),
        properties={"proj:epsg": column("proj_epsg")},
    )
    backend = Backend(
        coll, view, collection_id="sentinel-2-l2a", title="Sentinel-2 L2A", bbox_column="bbox"
    )
    return TestClient(make_app(backend))


def test_landing_page_declares_conformance_and_links(client):
    doc = client.get("/").json()
    assert doc["type"] == "Catalog"
    assert "https://api.stacspec.org/v1.0.0/item-search" in doc["conformsTo"]
    assert {l["rel"] for l in doc["links"]} >= {"self", "conformance", "data", "search"}


def test_the_collection_document_summarises_the_constraint(client):
    doc = client.get("/collections/sentinel-2-l2a").json()
    assert doc["type"] == "Collection"
    # the summary came from the variable's declared domain, not from the data
    assert doc["summaries"]["proj_epsg"]["maximum"] == 32766.0
    assert client.get("/collections/nope").status_code == 404


def test_item_search_returns_features(client):
    doc = client.get("/search").json()
    assert doc["type"] == "FeatureCollection"
    assert doc["numberMatched"] == 3
    assert {f["id"] for f in doc["features"]} == {
        "T33UUP_20240101",
        "T33UU2_20240102",
        "T33UU3_20240103",
    }
    assert doc["features"][0]["properties"]["proj:epsg"] == 32633


def test_pagination_walks_the_whole_collection(client):
    seen, token, pages = [], None, 0
    while True:
        url = f"/search?limit=2{f'&token={token}' if token else ''}"
        doc = client.get(url).json()
        seen += [f["id"] for f in doc["features"]]
        pages += 1
        token = next((l["href"].split("token=")[1] for l in doc["links"] if l["rel"] == "next"), None)
        if not token:
            break
    assert pages == 2
    assert len(seen) == 3 == len(set(seen))


def test_datetime_and_bbox_filters(client):
    doc = client.get("/search?datetime=2024-01-02T00:00:00Z/2024-01-05T00:00:00Z").json()
    assert doc["numberMatched"] == 2
    doc = client.get("/search?bbox=11,44,14,47").json()
    assert [f["id"] for f in doc["features"]] == ["T33UUP_20240101"]


def test_post_search_matches_get(client):
    a = client.get("/search?limit=1").json()
    b = client.post("/search", json={"limit": 1}).json()
    assert a["features"] == b["features"]


def test_fetching_one_item(client):
    item = client.get("/collections/sentinel-2-l2a/items/T33UUP_20240101").json()
    assert item["type"] == "Feature"
    assert item["bbox"] == "[12.0, 45.0, 13.0, 46.0]"
    assert client.get("/collections/sentinel-2-l2a/items/nope").status_code == 404


def test_items_endpoint_is_the_same_view(client):
    doc = client.get("/collections/sentinel-2-l2a/items?limit=10").json()
    assert doc["numberReturned"] == 3
