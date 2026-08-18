"""Query: the table as Arrow, and SQL over it."""

import copy

import pytest

from conftest import shot
from datacollections import ExtraColumn, create_collection, var

pa = pytest.importorskip("pyarrow")
pytest.importorskip("datafusion")


def collection_with_rows(repo):
    coll = create_collection(
        repo, constraint=None, extra_columns=[ExtraColumn("shot", "int64")]
    )
    coll.add_item(shot(10), extras={"shot": 30420})

    doc = copy.deepcopy(coll.constraint.document)
    nt = var("nt", type="integer", minimum=1)
    for name in ("time", "data"):
        doc["consolidated_metadata"]["metadata"][name]["shape"][0] = nt
        doc["consolidated_metadata"]["metadata"][name]["chunk_grid"]["configuration"][
            "chunk_shape"
        ][0] = nt
    coll.evolve_schema(doc)
    coll.add_item(shot(4096), extras={"shot": 30421})
    coll.add_item(shot(1), extras={"shot": 30422})
    return coll


def test_the_arrow_schema_carries_the_constraint_and_the_extension_type(repo):
    coll = collection_with_rows(repo)
    table = coll.to_arrow()

    # /meta group attributes -> Arrow Schema metadata: this is why the constraint
    # lives in group attributes, and it is what makes it a planner input.
    assert b"datacollections" in table.schema.metadata
    import json

    dc = json.loads(table.schema.metadata[b"datacollections"])
    assert dc["cohorts"]["default"]["constraint"]

    # /meta/<field> array attributes -> Arrow Field metadata
    field = table.schema.field("member_id")
    assert field.metadata[b"ARROW:extension:name"] == b"zarr.group_ref"
    # ARROW:extension:metadata is a *string* in Arrow, whatever it looks like in Zarr
    assert json.loads(field.metadata[b"ARROW:extension:metadata"])["resolve"] == "/groups/{id}"
    assert table.schema.field("nt").metadata[b"datacollections:role"] == b"variable"
    assert table.schema.field("shot").metadata[b"datacollections:role"] == b"extra"


def test_sql_over_the_table(repo):
    coll = collection_with_rows(repo)
    out = coll.sql("SELECT shot, nt FROM members WHERE nt > 5 ORDER BY nt").to_pydict()
    assert out["nt"] == [10, 4096]
    assert out["shot"] == [30420, 30421]


def test_the_table_is_a_materialised_view_over_the_groups(repo):
    """Every variable column is recomputable from the group it describes — which is
    a free consistency check and, if it ever fails, a repair path."""
    coll = collection_with_rows(repo)
    assert coll.verify() == []
    table = coll.to_arrow().to_pydict()
    assert table["nt"] == [row["nt"] for row in coll.rows()]
