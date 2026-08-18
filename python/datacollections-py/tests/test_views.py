"""Views, including STAC — which is one view among others, not the storage format."""

import pytest

from conftest import shot
from datacollections import (
    ExtraColumn,
    View,
    column,
    create_collection,
    description,
    stac_collection,
    stac_item_view,
    stac_items,
)


@pytest.fixture
def coll(repo):
    c = create_collection(
        repo,
        constraint=None,
        extra_columns=[
            ExtraColumn("shot", "int64"),
            ExtraColumn("datetime", "string", "extracted at ingest for query convenience"),
        ],
    )
    c.add_item(shot(10), extras={"shot": 30420, "datetime": "2024-03-01T12:00:00Z"})
    return c


def test_a_stac_item_is_a_rendered_template(coll):
    view = stac_item_view(
        collection="mastu-amc",
        id={"$join": ["mastu-", column("shot"), "-", description("/attributes/diagnostic")]},
        datetime=column("datetime"),
        properties={"campaign": description("/attributes/campaign")},
    )
    item = coll.render(view, coll.member_ids[0])

    assert item["type"] == "Feature"
    assert item["id"] == "mastu-30420-amc"  # human-meaningful, from an extra column
    assert item["collection"] == "mastu-amc"
    assert item["properties"]["datetime"] == "2024-03-01T12:00:00Z"
    assert item["properties"]["campaign"] == "M09"
    assert item["geometry"] is None


def test_a_view_declares_which_columns_a_search_must_fetch(coll):
    view = stac_item_view("c", column("shot"), column("datetime"))
    assert view.columns_read == ["shot", "datetime"]


def test_views_read_columns_and_the_reconstructed_description(coll):
    """The two directions are different: `substitute` reads only variable columns, so
    derivability is unaffected by extra columns, while a view may read anything."""
    view = View(
        {
            "name": "custom",
            "template": {
                "id": column("member_id"),
                "shot": column("shot"),
                "nchannels": description("/consolidated_metadata/metadata/data/shape/1"),
            },
        }
    )
    out = coll.render(view, coll.member_ids[0])
    assert out["shot"] == 30420
    assert out["nchannels"] == 8


def test_a_stac_collection_falls_out_of_the_variable_domains():
    from datacollections import Constraint, var, wild

    c = Constraint(
        {
            "attributes": {
                "proj:epsg": var("proj_epsg", type="integer", minimum=1024, maximum=32766),
                "cube:dimensions": wild("cube_dimensions"),
            }
        }
    )
    doc = stac_collection("sentinel-2-l2a", "Sentinel-2 L2A tiles", c)
    assert doc["type"] == "Collection"
    assert doc["summaries"]["proj_epsg"] == {
        "type": "integer",
        "minimum": 1024.0,
        "maximum": 32766.0,
    }
    # a wildcard declined to describe its leaf, so it summarises nothing
    assert "cube_dimensions" not in doc["summaries"]


def test_stac_items_over_the_whole_collection(coll):
    coll.add_item(shot(10), extras={"shot": 30421, "datetime": "2024-03-02T12:00:00Z"})
    view = stac_item_view("mastu-amc", column("shot"), column("datetime"))
    items = stac_items(coll, view)
    assert [i["id"] for i in items] == [30420, 30421]


def test_a_domain_with_no_stac_vocabulary_uses_the_same_machinery(coll):
    """The forcing function: if the view layer only worked for STAC, the factoring
    would be wrong."""
    ome = View(
        {
            "name": "ome-fov-record",
            "template": {
                "fov_id": column("member_id"),
                "acquisition": {"campaign": description("/attributes/campaign")},
            },
        }
    )
    out = coll.render(ome, coll.member_ids[0])
    assert out["acquisition"]["campaign"] == "M09"
    assert "stac_version" not in out
