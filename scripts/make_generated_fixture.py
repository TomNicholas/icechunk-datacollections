"""Emit a fixture from a **real write**, so encoding drift cannot pass unnoticed.

The hand-written fixtures encode what we *believe* zarr-python writes. This one
records what it actually wrote: build a small OME-Zarr-shaped collection, read the
members' descriptions back out of the store, and save them alongside the constraint
that was authored for them.

Why it matters: the descriptions are compared as-is, with no canonicalisation, which
is sound only because every member is written by our own `add_item`. If zarr-python
changes a default — the zstd level, a codec's spelling, how `fill_value` is encoded —
that assumption quietly stops holding, and this fixture is what fails.

    python scripts/make_generated_fixture.py
"""

from __future__ import annotations

import json
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "examples"))

import icechunk  # noqa: E402
import zarr  # noqa: E402

from datacollections import create_collection, description_of_group  # noqa: E402
from datacollections import store as _store  # noqa: E402

OUT = pathlib.Path(__file__).resolve().parents[1] / "spec" / "fixtures" / "constraints"


def main() -> None:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "examples" / "ome_zarr"))
    from run import author_constraint, field_of_view  # noqa: E402

    tmp = pathlib.Path(tempfile.mkdtemp()) / "store"
    repo = icechunk.Repository.create(icechunk.local_filesystem_storage(str(tmp)))
    coll = create_collection(repo, constraint=None, id_seed=1)

    ds, _ = field_of_view(0)
    coll.add_item(ds)
    constraint = author_constraint(coll.constraint.document)
    coll.evolve_schema(constraint)
    for i in (1, 2, 5):
        coll.add_item(field_of_view(i)[0])

    root = _store.read_root(repo.readonly_session("main"))
    members = []
    for member_id in coll.member_ids:
        description = description_of_group(root[f"groups/{member_id}"])
        members.append({"description": description, "bindings": constraint.meet(description)})

    # a non-member with one leaf changed, so the fixture also pins rejection
    broken = json.loads(json.dumps(members[0]["description"]))
    broken["consolidated_metadata"]["metadata"]["0"]["data_type"] = "uint8"

    fixture = {
        "name": "ome-fov-generated",
        "why": (
            "Emitted by scripts/make_generated_fixture.py from a real zarr-python write "
            f"(zarr {zarr.__version__}, icechunk {icechunk.__version__}). Records what the "
            "writer actually produced rather than what we believe it produces, so a change "
            "to a codec default or a fill_value encoding fails here instead of silently "
            "invalidating the no-canonicalisation assumption."
        ),
        "constraint": constraint.document,
        "members": members,
        "non_members": [{"description": broken, "why": "dtype is a literal in this cohort"}],
        "subsumes": [
            {"loosened": constraint.document, "holds": True, "why": "reflexive"},
            {"loosened": {"$wild": "everything"}, "holds": True, "why": "a wildcard root generalises anything"},
        ],
    }

    path = OUT / "ome-fov-generated.json"
    path.write_text(json.dumps(fixture, indent=2) + "\n")
    shutil.rmtree(tmp.parent, ignore_errors=True)
    print(f"wrote {path} ({len(members)} members, {path.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
