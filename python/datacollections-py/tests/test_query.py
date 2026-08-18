"""Query: SQL over the table, through upstream's DataFusion provider.

The reading is `zarr-datafusion-search`'s. These tests check three things: that a
DataCollections store really is readable by it unmodified, that the self-description
survives the trip, and that when upstream refuses a store we say so rather than
silently doing something slower.
"""

import copy

import pytest

from conftest import shot
from datacollections import ExtraColumn, create_collection, var

pa = pytest.importorskip("pyarrow")
pytest.importorskip("datafusion")
pytest.importorskip("zarr_datafusion_search")

from datacollections.query import (  # noqa: E402
    UpstreamRefused,
    explain,
    reads_through_upstream,
)


def collection_with_rows(repo, extra_columns=None):
    coll = create_collection(
        repo,
        constraint=None,
        extra_columns=extra_columns or [ExtraColumn("shot", "int64")],
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


def test_a_datacollections_store_is_readable_by_upstream_unmodified(repo):
    """The layout-convergence claim, as a test: we write it, they read it."""
    coll = collection_with_rows(repo)
    assert reads_through_upstream(coll)

    out = coll.sql("SELECT shot, nt FROM members WHERE nt > 5 ORDER BY nt").to_pydict()
    assert out["nt"] == [10, 4096]
    assert out["shot"] == [30420, 30421]


def test_the_scan_is_upstreams_and_the_projection_is_pushed_down(repo):
    """Not just "it returns rows" — check the plan.

    `ZarrExec` is upstream's operator, and the `TableScan` projection lists only the
    columns the query needs, so unread columns are never touched. The filter still
    appears *above* the scan, so predicate pushdown is not happening for this
    predicate — stated here rather than claimed away.
    """
    coll = collection_with_rows(repo)
    plan = explain(coll, "SELECT shot FROM members WHERE nt > 5")

    assert "ZarrExec" in plan
    assert "projection=[nt, shot]" in plan
    assert "FilterExec" in plan


def test_the_self_description_survives_the_round_trip(repo):
    """Upstream drops both halves of it, so we re-attach them — see query.py.

    When the two upstream PRs land, this test should keep passing with
    `attach_self_description` deleted.
    """
    coll = collection_with_rows(repo)
    table = coll.to_arrow()

    import json

    dc = json.loads(table.schema.metadata[b"datacollections"])
    assert dc["cohorts"]["default"]["constraint"]

    field = table.schema.field("member_id")
    assert field.metadata[b"ARROW:extension:name"] == b"zarr.group_ref"
    assert json.loads(field.metadata[b"ARROW:extension:metadata"])["resolve"] == "/groups/{id}"
    assert table.schema.field("nt").metadata[b"datacollections:role"] == b"variable"
    assert table.schema.field("shot").metadata[b"datacollections:role"] == b"extra"


def test_an_all_fill_column_is_still_readable(repo):
    """zarr-python skips writing all-fill chunks; upstream's reader requires them.

    A column that is empty for every member — MAST-U's `units` is, in real data —
    produced "chunk cannot be found" until the writer started materialising empty
    chunks. Pinned here because nothing else would notice.
    """
    coll = create_collection(
        repo, constraint=None, extra_columns=[ExtraColumn("units", "string")]
    )
    for _ in range(3):
        coll.add_item(shot(10), extras={"units": ""})

    assert reads_through_upstream(coll)
    assert coll.sql("SELECT units FROM members").to_pydict() == {"units": ["", "", ""]}


def test_a_store_upstream_refuses_falls_back_and_says_why(repo):
    """Their schema builder hard-errors on a column named `bbox` that is not Zarr
    `bytes` — the special case the first upstream PR deletes. Until then such a
    collection is still queryable, but loudly and slowly."""
    coll = create_collection(
        repo, constraint=None, extra_columns=[ExtraColumn("bbox", "string", encoding="json")]
    )
    coll.add_item(shot(10), extras={"bbox": [1.0, 2.0, 3.0, 4.0]})

    with pytest.warns(UpstreamRefused, match="bbox"):
        out = coll.sql("SELECT member_id FROM members").to_pydict()
    assert len(out["member_id"]) == 1

    # `reads_through_upstream` is a question, not a query, so it stays quiet
    assert not reads_through_upstream(coll)


def test_the_table_is_a_materialised_view_over_the_groups(repo):
    coll = collection_with_rows(repo)
    assert coll.verify() == []
    assert coll.to_arrow().to_pydict()["nt"] == [row["nt"] for row in coll.rows()]
