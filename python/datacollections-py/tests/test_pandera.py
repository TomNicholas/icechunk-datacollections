"""pandera.xarray translation — useful in both directions, normative in neither."""

import copy

import numpy as np
import pytest
import xarray as xr

from conftest import shot
from datacollections import Constraint, create_collection, var
from datacollections.pandera_view import (
    constraint_from_pandera,
    constraint_to_pandera,
    constraint_to_pandera_dict,
)

px = pytest.importorskip("pandera.xarray")


@pytest.fixture
def constraint(repo) -> Constraint:
    coll = create_collection(repo, constraint=None)
    coll.add_item(shot(10))
    doc = copy.deepcopy(coll.constraint.document)
    nt = var("nt", type="integer", minimum=1)
    for name in ("time", "data"):
        doc["consolidated_metadata"]["metadata"][name]["shape"][0] = nt
        doc["consolidated_metadata"]["metadata"][name]["chunk_grid"]["configuration"][
            "chunk_shape"
        ][0] = nt
    return Constraint(doc)


def test_a_variable_dimension_becomes_pandera_s_any_length(constraint):
    """The information loss, made visible: pandera can say the length is free, but
    not that a column records it per member. That is why it cannot be the normative
    format."""
    spec = constraint_to_pandera_dict(constraint)
    assert spec["data"]["dtype"] == "float32"
    assert spec["data"]["dims"] == ["time", "channel"]
    assert spec["data"]["shape"] == [None, 8]  # None where `nt` was


def test_export_produces_a_schema_a_consumer_can_validate_against(constraint):
    """Zarr metadata has no coordinate/data-variable distinction — that is an xarray
    notion — so the export applies xarray's own rule: an array named after its single
    dimension becomes a coordinate. One more reason the pandera form is a projection,
    not the format."""
    schema = constraint_to_pandera(constraint)

    def dataset(nt, nchannel):
        return xr.Dataset(
            {"data": (("time", "channel"), np.zeros((nt, nchannel), "float32"))},
            coords={"time": ("time", np.arange(nt, dtype="float64"))},
        )

    schema.validate(dataset(37, 8))  # any time length, as the constraint says
    schema.validate(dataset(4096, 8))

    with pytest.raises(Exception):
        schema.validate(dataset(37, 9))  # the channel count is a literal


def test_authoring_from_pandera_leaves_the_variable_decisions_to_the_user():
    """pandera cannot know about cross-member variables, so the import names one per
    dimension and wildcards the encoding — the user then says which are real."""
    schema = px.DatasetSchema(
        data_vars={"data": px.DataArraySchema(dtype="float32", dims=("time", "channel"))}
    )
    c = constraint_from_pandera(schema, attributes={"instrument": "amc"})
    names = {d["name"] for d in c.declarations}
    assert {"ntime", "nchannel"} <= names
    assert c.document["consolidated_metadata"]["metadata"]["data"]["data_type"] == "float32"
