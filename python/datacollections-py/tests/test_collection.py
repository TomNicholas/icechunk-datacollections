"""The store: both write paths, in one transaction each."""

import copy

import pytest

from conftest import make_repo, shot
from datacollections import (
    Constraint,
    ConstraintError,
    ExtraColumn,
    create_collection,
    open_collection,
    var,
)
from datacollections import store as _store


def loosen_nt(constraint: Constraint) -> dict:
    """Turn the two arrays' time length into one variable — the co-constraint that
    says they are equal within a member and says nothing across members."""
    doc = copy.deepcopy(constraint.document)
    arrays = doc["consolidated_metadata"]["metadata"]
    nt = var("nt", type="integer", minimum=1)
    for name in ("time", "data"):
        arrays[name]["shape"][0] = nt
        arrays[name]["chunk_grid"]["configuration"]["chunk_shape"][0] = nt
    return doc


def test_first_member_sets_the_constraint_verbatim(repo):
    coll = create_collection(repo, constraint=None)
    assert coll.constraint is None
    member_id = coll.add_item(shot(10))

    assert len(coll) == 1
    assert coll.constraint.declarations == []  # all-literal: no inference happened
    assert len(member_id) == 32  # 128 bits, hex


def test_add_item_is_always_strict(repo):
    coll = create_collection(repo, constraint=None)
    coll.add_item(shot(10))
    with pytest.raises(ConstraintError) as e:
        coll.add_item(shot(11))
    # the message names the leaf, not just "constraint violation"
    assert "/consolidated_metadata/metadata/data/shape/0" in str(e.value)
    assert "expected 10, found 11" in str(e.value)
    assert len(coll) == 1


def test_a_rejected_member_writes_nothing(repo):
    coll = create_collection(repo, constraint=None)
    coll.add_item(shot(10))
    with pytest.raises(ConstraintError):
        coll.add_item(shot(11))
    root = _store.read_root(repo.readonly_session("main"))
    assert len(list(root["groups"].members())) == 1


def test_describe_reconstructs_the_members_zarr_json_exactly(repo):
    coll = create_collection(repo, constraint=None)
    member_id = coll.add_item(shot(10))
    coll.evolve_schema(loosen_nt(coll.constraint))
    second = coll.add_item(shot(4096))

    root = _store.read_root(repo.readonly_session("main"))
    from datacollections import description_of_group

    for mid in (member_id, second):
        actual = description_of_group(root[f"groups/{mid}"])
        assert coll.describe(mid) == actual


def test_evolve_schema_backfills_by_reading_no_nulls_involved(repo):
    coll = create_collection(repo, constraint=None)
    ids = [coll.add_item(shot(10)) for _ in range(3)]
    report = coll.evolve_schema(loosen_nt(coll.constraint))

    assert report.created == ["nt"]
    assert report.backfilled == ["nt"]
    assert report.rows_read == 3  # O(N), and the report says so
    assert not report.was_cheap
    assert [r["nt"] for r in coll.rows()] == [10, 10, 10]
    assert [r["member_id"] for r in coll.rows()] == ids


def test_evolve_schema_refuses_to_tighten(repo):
    coll = create_collection(repo, constraint=None)
    coll.add_item(shot(10))
    tightened = copy.deepcopy(coll.constraint.document)
    tightened["attributes"]["campaign"] = "M08"
    with pytest.raises(ValueError, match="does not generalise"):
        coll.evolve_schema(tightened)


def test_an_evolution_that_creates_no_column_is_cheap(repo):
    coll = create_collection(repo, constraint=None)
    coll.add_item(shot(10))
    coll.evolve_schema(loosen_nt(coll.constraint))
    # widen the domain only: the column already exists, so nothing is read
    wider = copy.deepcopy(coll.constraint.document)
    report = coll.evolve_schema(wider)
    assert report.was_cheap
    assert report.rows_read == 0


def test_widening_incrementally_matches_building_from_scratch(tmp_path):
    """The property that makes incremental writes trustworthy.

    Built two ways — appended-then-widened, versus created with the final constraint
    — the stores must agree on everything: the `/meta` attributes, every column, and
    every member's description. Ids are seeded so this is an equality, not an
    equality "modulo ids".
    """
    incremental = create_collection(make_repo(tmp_path / "a"), constraint=None, id_seed=7)
    incremental.add_item(shot(10))
    final_constraint = Constraint(loosen_nt(incremental.constraint))
    incremental.evolve_schema(final_constraint)
    incremental.add_item(shot(4096))

    scratch = create_collection(make_repo(tmp_path / "b"), constraint=final_constraint, id_seed=7)
    scratch.add_item(shot(10))
    scratch.add_item(shot(4096))

    assert incremental.attributes == scratch.attributes
    assert incremental.rows() == scratch.rows()
    assert [incremental.describe(m) for m in incremental.member_ids] == [
        scratch.describe(m) for m in scratch.member_ids
    ]


def test_extra_columns_are_supplied_by_the_caller(repo):
    """Extra columns are not recomputable from a member's group, so nothing else
    could supply them — and with opaque member ids they are the only way to address
    a member meaningfully."""
    coll = create_collection(
        repo,
        constraint=None,
        extra_columns=[
            ExtraColumn("shot", "int64", "MAST-U shot number"),
            ExtraColumn("diagnostic", "string"),
        ],
    )
    member_id = coll.add_item(shot(10), extras={"shot": 30420, "diagnostic": "amc"})
    assert coll.row(member_id)["shot"] == 30420
    assert coll.extra_columns == ["shot", "diagnostic"]

    with pytest.raises(ValueError, match="no value supplied for extra column"):
        coll.add_item(shot(10), extras={"shot": 30421})


def test_extra_columns_do_not_affect_derivability(repo):
    coll = create_collection(repo, constraint=None, extra_columns=[ExtraColumn("shot", "int64")])
    member_id = coll.add_item(shot(10), extras={"shot": 1})
    root = _store.read_root(repo.readonly_session("main"))
    from datacollections import description_of_group

    assert coll.describe(member_id) == description_of_group(root[f"groups/{member_id}"])


def test_check_reports_mismatches_without_writing(repo):
    coll = create_collection(repo, constraint=None)
    coll.add_item(shot(10))
    assert coll.check(shot(10)) == []
    problems = coll.check(shot(11))
    assert problems and "/shape/0" in problems[0].pointer
    assert len(coll) == 1


def test_verify_recomputes_the_table_from_the_groups(repo):
    coll = create_collection(repo, constraint=None)
    coll.add_item(shot(10))
    coll.evolve_schema(loosen_nt(coll.constraint))
    coll.add_item(shot(20))
    assert coll.verify() == []


def test_the_store_layout_is_what_the_spec_says(repo):
    coll = create_collection(repo, constraint=None)
    member_id = coll.add_item(shot(10))
    root = _store.read_root(repo.readonly_session("main"))

    assert set(dict(root["meta"].attrs)) == {"datacollections"}
    dc = dict(root["meta"].attrs)["datacollections"]
    assert dc["version"] == "0.1"
    assert set(dc["cohorts"]) == {"default"}  # keyed from day one
    assert "constraint" in dc["cohorts"]["default"]

    member_col = root["meta/member_id"]
    assert dict(member_col.attrs)["ARROW:extension:name"] == "zarr.group_ref"
    # stored as real JSON in Zarr, stringified only when the Arrow Field is built
    assert isinstance(dict(member_col.attrs)["ARROW:extension:metadata"], dict)
    assert root[f"groups/{member_id}"] is not None


def test_open_collection_round_trips(repo):
    coll = create_collection(repo, constraint=None)
    member_id = coll.add_item(shot(10))
    reopened = open_collection(repo)
    assert reopened.constraint == coll.constraint
    assert reopened.member_ids == [member_id]
    assert len(reopened) == 1


def test_opening_checks_that_every_variable_has_a_column(repo):
    """Variables ⊆ columns is mechanically checkable, and checked on open."""
    coll = create_collection(repo, constraint=None)
    coll.add_item(shot(10))
    session = repo.writable_session("main")
    root = _store.root_group(session)
    broken = copy.deepcopy(coll.attributes)
    broken["datacollections"]["cohorts"]["default"]["constraint"]["attributes"]["campaign"] = {
        "$var": "campaign",
        "type": "string",
    }
    _store.write_meta_attributes(root, broken)
    session.commit("break the invariant")

    with pytest.raises(ValueError, match="campaign"):
        open_collection(repo).columns
