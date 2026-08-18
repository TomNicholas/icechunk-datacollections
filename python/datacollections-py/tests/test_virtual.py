"""The virtual ingest path: `add_item` on a Dataset of VirtualiZarr ManifestArrays.

This is the path PLAN.md wants for every example — source format → VirtualiZarr →
virtual Zarr → our layout, with only chunk *references* written, so a whole demo
store stays small enough to host and version cheaply. It is exercised here with
VirtualiZarr's Zarr parser over a local Zarr store, which needs no cloud credentials
and no extra format readers.

What this shows that the other tests do not: a member written by someone else's
writer still `meet`s a constraint authored from a member written the same way. The
`zarr.json`-as-is comparison is sound exactly as far as writer provenance is
uniform, and no further.
"""

import numpy as np
import pytest
import xarray as xr

from datacollections import Constraint, create_collection, var

icechunk = pytest.importorskip("icechunk")
virtualizarr = pytest.importorskip("virtualizarr")
obstore = pytest.importorskip("obstore")
pytest.importorskip("arro3.core")

from obspec_utils.registry import ObjectStoreRegistry  # noqa: E402
from virtualizarr import open_virtual_dataset  # noqa: E402
from virtualizarr.parsers import ZarrParser  # noqa: E402


def write_source(path, nt: int) -> str:
    """A source Zarr store standing in for whatever the real archive holds."""
    xr.Dataset(
        {"data": (("time", "channel"), np.zeros((nt, 8), "float32"))},
        coords={"time": ("time", np.arange(nt, dtype="float64"))},
        attrs={"diagnostic": "amc"},
    ).to_zarr(str(path), zarr_format=3, mode="w", consolidated=False)
    return f"file://{path}"


def virtual_repo(path, source_root):
    """A repo that may hold virtual references into `source_root`.

    The container and its authorization are the *caller's* business — they are a
    property of the repository and involve their credentials — which is why nothing
    in `datacollections` configures them.
    """
    # The container's *name* must be a prefix of the chunk references VirtualiZarr
    # writes ("file:/…"), which is why it is called "file" rather than something
    # descriptive.
    config = icechunk.RepositoryConfig(
        virtual_chunk_containers={
            "file": icechunk.VirtualChunkContainer(
                url_prefix=f"file://{source_root}/",
                store=icechunk.local_filesystem_store("/"),
            )
        }
    )
    return icechunk.Repository.create(
        icechunk.local_filesystem_storage(str(path)),
        config=config,
        authorize_virtual_chunk_access={"file": None},
    )


@pytest.fixture
def virtual_datasets(tmp_path):
    registry = ObjectStoreRegistry({"file://": obstore.store.LocalStore()})
    sources = tmp_path / "sources"
    sources.mkdir()
    out = []
    for nt in (100, 250):
        url = write_source(sources / f"shot{nt}.zarr", nt)
        out.append(open_virtual_dataset(url, parser=ZarrParser(), registry=registry))
    return sources, out


def test_add_item_writes_virtual_chunk_references(tmp_path, virtual_datasets):
    sources, datasets = virtual_datasets
    from datacollections import store as _store

    assert _store.is_virtual(datasets[0])

    repo = virtual_repo(tmp_path / "repo", sources)
    coll = create_collection(repo, constraint=None)
    first = coll.add_item(datasets[0])

    assert len(coll) == 1
    description = coll.describe(first)
    arrays = description["consolidated_metadata"]["metadata"]
    assert arrays["data"]["shape"] == [100, 8]
    assert description["attributes"]["diagnostic"] == "amc"

    # ...and the store holds references, not data: a virtual member's group is
    # metadata plus a chunk manifest.
    assert coll.verify() == []


def test_a_virtual_member_meets_a_constraint_and_a_second_one_widens_it(tmp_path, virtual_datasets):
    sources, datasets = virtual_datasets
    repo = virtual_repo(tmp_path / "repo", sources)
    coll = create_collection(repo, constraint=None)
    coll.add_item(datasets[0])

    # a different time length is rejected until the user says it may vary
    from datacollections import ConstraintError

    with pytest.raises(ConstraintError):
        coll.add_item(datasets[1])

    import copy

    doc = copy.deepcopy(coll.constraint.document)
    nt = var("nt", type="integer", minimum=1)
    for name in ("time", "data"):
        doc["consolidated_metadata"]["metadata"][name]["shape"][0] = nt
        # The source store is contiguous in time, so its chunk shape tracks its array
        # shape — and chunk shape is part of the description, so the *same* variable
        # has to appear there too. Loosening the shape alone is not enough, which is
        # the "re-chunking a member widens the constraint" rule seen from the other
        # side.
        doc["consolidated_metadata"]["metadata"][name]["chunk_grid"]["configuration"][
            "chunk_shape"
        ][0] = nt
    report = coll.evolve_schema(Constraint(doc))
    assert report.backfilled == ["nt"]

    coll.add_item(datasets[1])
    assert [row["nt"] for row in coll.rows()] == [100, 250]
    assert coll.verify() == []
