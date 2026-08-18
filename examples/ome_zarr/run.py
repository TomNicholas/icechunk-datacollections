"""OME-Zarr microscopy — **no STAC anywhere in the stack**.

Implemented first, deliberately. OME-NGFF has the richest attribute vocabulary of
the four domains (`multiscales`, `axes`, `coordinateTransformations`, `omero`), so
doing it before any STAC code exists is the forcing function: if the core cannot
express the microscopy case with no geospatial vocabulary anywhere, the factoring is
wrong.

**Referenced unit: one field of view at resolution level 0.** Flat — a group plus
its child arrays. Plate/well structure and multiscale levels both need language
features deferred past M6 (`$each`/`$count` for varying level counts, nesting for
plate → well → FOV), and choosing a finer unit is what makes them unnecessary here.

What varies across FOVs: Z-depth and channel count. Both ordinary `$var` cases, and
both tied to the array's shape by the co-constraint — `nz` appears in the
acquisition attributes *and* in `shape[1]`, which asserts they agree within a member
and says nothing across members.

What is wildcarded: `multiscales` and `omero`. That is the whole point of the
wildcard — domain vocabulary we constrain the *position* of but decline to
interpret. The values are stored verbatim and reinstated exactly by `substitute`.

Run:  python examples/ome_zarr/run.py -n 40
"""

from __future__ import annotations

import pathlib
import sys

import numpy as np
import xarray as xr

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from _common import array_pointer, banner, fresh_repo, ingest, parse_args, show_table, substitute_leaf
from datacollections import (
    Constraint,
    ExtraColumn,
    View,
    column,
    create_collection,
    description,
    var,
    wild,
)

NY = NX = 256  # small on purpose; this is a metadata demo
PLATES = ["P01", "P02"]


def field_of_view(index: int) -> tuple[xr.Dataset, dict]:
    """One FOV at level 0. Z-depth and channel count vary; Y/X do not."""
    nz = 1 + index % 8
    nc = 1 + index % 3
    plate = PLATES[index % len(PLATES)]
    well = f"{chr(ord('A') + index % 4)}{1 + index % 6}"

    data = np.zeros((nc, nz, NY, NX), dtype="uint16")
    ds = xr.Dataset(
        {"0": (("c", "z", "y", "x"), data)},
        attrs={
            "ome": {
                "version": "0.5",
                "multiscales": [
                    {
                        "name": f"{plate}/{well}/{index}",
                        "axes": [
                            {"name": "c", "type": "channel"},
                            {"name": "z", "type": "space", "unit": "micrometer"},
                            {"name": "y", "type": "space", "unit": "micrometer"},
                            {"name": "x", "type": "space", "unit": "micrometer"},
                        ],
                        "datasets": [
                            {
                                "path": "0",
                                "coordinateTransformations": [
                                    {"type": "scale", "scale": [1.0, 0.5, 0.1625, 0.1625]}
                                ],
                            }
                        ],
                    }
                ],
                "omero": {
                    "channels": [
                        {"label": f"ch{i}", "color": "FFFFFF", "window": {"start": 0, "end": 65535}}
                        for i in range(nc)
                    ]
                },
            },
            "acquisition": {"nz": nz, "nchannels": nc},
        },
    )
    ds["0"].encoding["chunks"] = (1, 1, NY, NX)  # pin: chunk shape is part of the description
    return ds, {"plate": plate, "well": well, "fov": index}


def author_constraint(first_description: dict) -> Constraint:
    """Author the constraint from one member's description.

    This is authoring, not inference: we take the first member's `zarr.json`
    verbatim and then say, leaf by leaf, which parts are allowed to move. Nothing
    generalises anything automatically — the plan has no such operation.
    """
    nz = var("nz", type="integer", minimum=1, maximum=64)
    nc = var("nc", type="integer", minimum=1, maximum=8)
    doc = first_description
    for pointer, hole in [
        # the acquisition attributes...
        ("/attributes/acquisition/nz", nz),
        ("/attributes/acquisition/nchannels", nc),
        # ...and the array shape they must agree with, within a member
        (array_pointer("0", "shape", "0"), nc),
        (array_pointer("0", "shape", "1"), nz),
        # OME's own vocabulary: constrained in position, uninterpreted in content
        ("/attributes/ome/multiscales", wild("multiscales")),
        ("/attributes/ome/omero", wild("omero")),
    ]:
        doc = substitute_leaf(doc, pointer, hole)
    return Constraint(doc)


def main() -> None:
    args = parse_args("ome_zarr", default_n=40)
    banner(f"OME-Zarr: {args.members} fields of view, no STAC in the stack")

    repo = fresh_repo(args.store)
    coll = create_collection(
        repo,
        constraint=None,
        extra_columns=[
            ExtraColumn("plate", "string", "not derivable from the group; supplied at ingest"),
            ExtraColumn("well", "string"),
            ExtraColumn("fov", "int64"),
        ],
    )

    # The first member bootstraps an all-literal constraint...
    ds, extras = field_of_view(0)
    first = coll.add_item(ds, extras=extras)
    print(f"\nfirst member {first}: constraint taken verbatim, {len(coll.constraint.declarations)} holes")

    # ...and a second FOV with a different Z-depth is rejected, because add_item is
    # always strict. Loosening is an explicit, separate call.
    ds2, extras2 = field_of_view(1)
    problems = coll.check(ds2)
    print(f"second FOV rejected by the all-literal constraint: {problems[0]}")

    constraint = author_constraint(coll.constraint.document)
    report = coll.evolve_schema(constraint)
    print(f"\n{report}")
    print(f"holes: {[d['name'] for d in coll.constraint.declarations]}")

    ingest(coll, (field_of_view(i) for i in range(1, args.members)))
    print(f"\n{len(coll)} members, columns: {[c['name'] for c in coll.columns]}")

    banner("The table is queryable with SQL")
    show_table(coll, "SELECT plate, well, fov, nz, nc FROM members ORDER BY nz DESC")
    show_table(coll, "SELECT nz, COUNT(*) AS fovs FROM members GROUP BY nz ORDER BY nz")

    banner("A member's description is derivable from the constraint plus its row")
    member_id = coll.member_ids[3]
    reconstructed = coll.describe(member_id)
    print(f"  member {member_id}")
    print(f"  shape: {reconstructed['consolidated_metadata']['metadata']['0']['shape']}")
    print(f"  omero channels: {len(reconstructed['attributes']['ome']['omero']['channels'])}")
    print(f"  verify() over the whole collection: {coll.verify() or 'consistent'}")

    banner("A view with no STAC vocabulary in it at all")
    fov_view = View(
        {
            "name": "ome-fov-record",
            "template": {
                "id": {"$join": [column("plate"), "/", column("well"), "/", column("fov")]},
                "acquisition": {
                    "z_depth": column("nz"),
                    "channels": column("nc"),
                    "axes": description("/attributes/ome/multiscales/0/axes"),
                },
                "array": {
                    "shape": description(array_pointer("0", "shape")),
                    "dtype": description(array_pointer("0", "data_type")),
                },
            },
        }
    )
    from json import dumps

    print(dumps(coll.render(fov_view, member_id), indent=2)[:600])
    print(f"\nstore: {args.store}")


if __name__ == "__main__":
    main()
