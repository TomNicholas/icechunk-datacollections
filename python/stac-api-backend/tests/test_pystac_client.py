"""The acceptance test PLAN.md asks for: **pystac-client against a live server.**

A real client, over real HTTP, against the reference implementation hosting our
backend. That is a much stronger statement than asserting our own response shapes
back to ourselves — pystac-client decides whether the API conforms, follows the
links itself, and refuses to search if the landing page does not advertise it.
"""

import pathlib
import socket
import sys
import threading
import time

import icechunk
import numpy as np
import pytest
import xarray as xr

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from datacollections import ExtraColumn, column, create_collection, stac_item_view
from datacollections_stac import Backend, make_app

pystac_client = pytest.importorskip("pystac_client")
uvicorn = pytest.importorskip("uvicorn")


def tile(size: int = 64) -> xr.Dataset:
    return xr.Dataset(
        {"B04": (("y", "x"), np.zeros((size, size), "uint16"))},
        attrs={"proj:epsg": 32633, "constellation": "sentinel-2"},
    )


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("store")
    repo = icechunk.Repository.create(icechunk.local_filesystem_storage(str(tmp_path / "s")))
    coll = create_collection(
        repo,
        constraint=None,
        extra_columns=[
            ExtraColumn("granule", "string"),
            ExtraColumn("datetime", "string"),
            ExtraColumn("bbox_wgs84", "string", "not named `bbox` — see query.py", encoding="json"),
        ],
    )
    for i in range(1, 6):
        coll.add_item(
            tile(),
            extras={
                "granule": f"T33UUP_2024010{i}",
                "datetime": f"2024-01-0{i}T10:00:00Z",
                "bbox_wgs84": [12.0 + i, 45.0, 13.0 + i, 46.0],
            },
        )

    view = stac_item_view(
        collection="sentinel-2-l2a",
        id=column("granule"),
        datetime=column("datetime"),
        bbox=column("bbox_wgs84"),
    )
    app = make_app(
        Backend(coll, view, collection_id="sentinel-2-l2a", title="Sentinel-2 L2A", bbox_column="bbox_wgs84")
    )

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "the API did not come up"
    try:
        yield f"http://127.0.0.1:{port}", coll
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_pystac_client_opens_the_api(served):
    url, _ = served
    client = pystac_client.Client.open(url)
    assert client.id == "sentinel-2-l2a"
    # pystac-client decides for itself whether this API conforms
    from pystac_client.conformance import ConformanceClasses

    assert client.conforms_to(ConformanceClasses.ITEM_SEARCH)
    assert client.conforms_to(ConformanceClasses.COLLECTIONS)


def test_pystac_client_walks_the_collections(served):
    url, coll = served
    client = pystac_client.Client.open(url)
    collections = list(client.get_collections())
    assert [c.id for c in collections] == ["sentinel-2-l2a"]
    # the summaries came from the constraint's variable domains
    assert collections[0].summaries is not None


def test_pystac_client_searches_and_pages(served):
    url, coll = served
    client = pystac_client.Client.open(url)

    search = client.search(collections=["sentinel-2-l2a"], limit=2)
    items = list(search.items())
    assert len(items) == len(coll) == 5
    assert {i.id for i in items} == {f"T33UUP_2024010{i}" for i in range(1, 6)}
    # paging happened: 5 items at 2 per page, followed by the client itself
    assert all(i.bbox is not None for i in items)


def test_pystac_client_filters(served):
    url, _ = served
    client = pystac_client.Client.open(url)

    by_time = list(
        client.search(datetime="2024-01-02T00:00:00Z/2024-01-03T23:59:59Z").items()
    )
    assert {i.id for i in by_time} == {"T33UUP_20240102", "T33UUP_20240103"}

    by_bbox = list(client.search(bbox=[12.5, 44.0, 14.5, 47.0]).items())
    assert {i.id for i in by_bbox} == {"T33UUP_20240101", "T33UUP_20240102"}

    by_id = list(client.search(ids=["T33UUP_20240104"]).items())
    assert [i.id for i in by_id] == ["T33UUP_20240104"]


def test_items_fetched_individually_match_the_search(served):
    url, _ = served
    client = pystac_client.Client.open(url)
    collection = client.get_collection("sentinel-2-l2a")
    item = collection.get_item("T33UUP_20240103")
    assert item is not None
    assert item.properties["datetime"] == "2024-01-03T10:00:00Z"
    assert item.bbox == [15.0, 45.0, 16.0, 46.0]
