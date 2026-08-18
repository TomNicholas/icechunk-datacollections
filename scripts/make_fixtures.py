"""Emit the hand-written conformance fixtures.

Kept as a script rather than raw JSON so the four domain fixtures share one
description-builder and stay structurally comparable — the point of the fixture set
is breadth across domains at identical depth.

    python scripts/make_fixtures.py
"""

from __future__ import annotations

import copy
import json
import pathlib

OUT = pathlib.Path(__file__).resolve().parents[1] / "spec" / "fixtures" / "constraints"

ZSTD = [
    {"name": "bytes", "configuration": {"endian": "little"}},
    {"name": "zstd", "configuration": {"level": 3, "checksum": False}},
]


def array(shape, chunks, dtype, dims, attrs=None, codecs=None, fill_value=0):
    """A Zarr v3 array metadata document, as zarr-python writes it."""
    return {
        "zarr_format": 3,
        "node_type": "array",
        "shape": list(shape),
        "data_type": dtype,
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": list(chunks)}},
        "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
        "fill_value": fill_value,
        "codecs": ZSTD if codecs is None else codecs,
        "attributes": attrs or {},
        "dimension_names": list(dims),
        "storage_transformers": [],
    }


def group(attrs, arrays):
    """A group's *consolidated* zarr.json — the whole description of one member."""
    return {
        "zarr_format": 3,
        "node_type": "group",
        "attributes": attrs,
        "consolidated_metadata": {
            "kind": "inline",
            "must_understand": False,
            "metadata": arrays,
        },
    }


def var(name, **domain):
    return {"$var": name, **domain}


def wild(name):
    return {"$wild": name}


def at(doc, path, value):
    """Set a value at a slash-separated path, returning a modified copy."""
    doc = copy.deepcopy(doc)
    cur = doc
    parts = path.split("/")
    for p in parts[:-1]:
        cur = cur[int(p)] if isinstance(cur, list) else cur[p]
    last = parts[-1]
    if isinstance(cur, list):
        cur[int(last)] = value
    else:
        cur[last] = value
    return doc


# --------------------------------------------------------------------- OME-Zarr
# Referenced unit: one field of view at resolution level 0. Flat: a group plus its
# child arrays. The richest attribute vocabulary of the four domains, which is the
# point — the language constrains `multiscales` without interpreting it.


def ome_attrs(nz, nc):
    return {
        "ome": {
            "version": "0.5",
            "multiscales": [
                {
                    "name": "fov",
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
            "omero": {"channels": [{"label": f"ch{i}"} for i in range(nc)]},
        },
        "acquisition": {"nz": nz, "nchannels": nc},
    }


def ome_member(nz, nc, ny=2048, nx=2048):
    return group(
        ome_attrs(nz, nc),
        {"0": array([nc, nz, ny, nx], [1, 1, 512, 512], "uint16", ["c", "z", "y", "x"])},
    )


ome = {
    "name": "ome-fov",
    "why": "OME-Zarr field of view at level 0. Z-depth and channel count vary per FOV; "
    "the co-constraint ties the array's shape to the acquisition attributes.",
    "constraint": group(
        {
            "ome": {
                "version": "0.5",
                "multiscales": wild("multiscales"),
                "omero": wild("omero"),
            },
            "acquisition": {
                "nz": var("nz", type="integer", minimum=1),
                "nchannels": var("nc", type="integer", minimum=1),
            },
        },
        {
            "0": array(
                [var("nc", type="integer", minimum=1), var("nz", type="integer", minimum=1), 2048, 2048],
                [1, 1, 512, 512],
                "uint16",
                ["c", "z", "y", "x"],
            )
        },
    ),
    "members": [
        {
            "description": ome_member(nz, nc),
            "bindings": {
                "multiscales": ome_attrs(nz, nc)["ome"]["multiscales"],
                "omero": ome_attrs(nz, nc)["ome"]["omero"],
                "nz": nz,
                "nc": nc,
            },
        }
        for nz, nc in [(1, 2), (17, 4), (64, 1)]
    ],
    "non_members": [
        {
            "description": at(ome_member(8, 3), "consolidated_metadata/metadata/0/shape/1", 9),
            "why": "shape[1] is 9 but acquisition.nz says 8 — the co-constraint",
        },
        {
            "description": at(ome_member(8, 3), "consolidated_metadata/metadata/0/data_type", "uint8"),
            "why": "dtype is a literal in this cohort",
        },
        {
            "description": at(
                ome_member(8, 3),
                "consolidated_metadata/metadata/0/chunk_grid",
                {"name": "regular", "configuration": {"chunk_shape": [1, 1, 256, 256]}},
            ),
            "why": "chunking is part of the description; re-chunking a member widens the constraint",
        },
        {"description": at(ome_member(8, 3), "attributes/acquisition/nz", 0), "why": "nz below its minimum"},
    ],
}

# ------------------------------------------------------------------ Sentinel-2
# Geospatial, and the only fixture that feeds the STAC view. CRS varies across UTM
# zones; with enums excluded the domain can say nothing tighter than "an integer".

S2_BANDS = ["B02", "B04", "B08"]


def s2_member(epsg, ulx, uly, codecs=None):
    attrs = {
        "cube:dimensions": {
            "x": {"type": "spatial", "axis": "x", "extent": [ulx, ulx + 109800]},
            "y": {"type": "spatial", "axis": "y", "extent": [uly - 109800, uly]},
        },
        "proj:epsg": epsg,
        "proj:transform": [10, 0, ulx, 0, -10, uly, 0, 0, 1],
    }
    return group(
        attrs,
        {
            b: array([10980, 10980], [1024, 1024], "uint16", ["y", "x"], codecs=codecs)
            for b in S2_BANDS
        },
    )


sentinel2 = {
    "name": "sentinel2-l2a-tile",
    "why": "Sentinel-2 L2A tile, full-resolution level only. Heterogeneous CRS across "
    "tiles; codecs wildcarded wholesale rather than aligned element-wise.",
    "constraint": group(
        {
            "cube:dimensions": wild("cube_dimensions"),
            "proj:epsg": var("proj_epsg", type="integer", minimum=1024, maximum=32766),
            "proj:transform": wild("proj_transform"),
        },
        {
            b: array([10980, 10980], [1024, 1024], "uint16", ["y", "x"], codecs=wild(f"codecs_{b}"))
            for b in S2_BANDS
        },
    ),
    "members": [
        {
            "description": s2_member(epsg, ulx, uly),
            "bindings": {
                "cube_dimensions": s2_member(epsg, ulx, uly)["attributes"]["cube:dimensions"],
                "proj_epsg": epsg,
                "proj_transform": [10, 0, ulx, 0, -10, uly, 0, 0, 1],
                **{f"codecs_{b}": ZSTD for b in S2_BANDS},
            },
        }
        for epsg, ulx, uly in [(32633, 399960, 5000040), (32634, 499980, 4900020)]
    ],
    "non_members": [
        {
            "description": at(s2_member(32633, 399960, 5000040), "consolidated_metadata/metadata/B04/shape", [5490, 5490]),
            "why": "an overview level, not the full-resolution one; v1 virtualizes level 0 only",
        },
        {
            "description": at(s2_member(32633, 399960, 5000040), "attributes/proj:epsg", "EPSG:32633"),
            "why": "proj:epsg declared integer",
        },
    ],
}

# ---------------------------------------------------------------------- MAST-U
# The referenced unit is (shot, diagnostic), not shot — so a missing diagnostic is a
# member that does not exist, and optionality is not needed. nt is the textbook case.


def mastu_member(nt, nchan=8):
    return group(
        {
            "diagnostic": "amc",
            "imas": {"ids": "magnetics", "homogeneous_time": 1},
            "campaign": "M09",
        },
        {
            "time": array([nt], [4096], "float64", ["time"]),
            "data": array([nt, nchan], [4096, 8], "float32", ["time", "channel"]),
        },
    )


NT = {"type": "integer", "minimum": 1, "maximum": 10000000}

mastu = {
    "name": "mastu-shot-diagnostic",
    "why": "MAST-U (shot, diagnostic) as the referenced unit. Per-shot time-series "
    "lengths vary while dimension names are fixed: the co-constraint across two arrays.",
    "constraint": group(
        {
            "diagnostic": "amc",
            "imas": {"ids": "magnetics", "homogeneous_time": 1},
            "campaign": var("campaign", type="string"),
        },
        {
            "time": array([var("nt", **NT)], [4096], "float64", ["time"]),
            "data": array([var("nt", **NT), 8], [4096, 8], "float32", ["time", "channel"]),
        },
    ),
    "members": [
        {"description": mastu_member(nt), "bindings": {"campaign": "M09", "nt": nt}}
        for nt in [1, 4096, 240001]
    ],
    "non_members": [
        {
            "description": at(mastu_member(1000), "consolidated_metadata/metadata/data/shape/0", 999),
            "why": "time and data disagree on nt within one member",
        },
        {
            "description": at(mastu_member(1000), "attributes/diagnostic", "xsx"),
            "why": "a different diagnostic is a different cohort, not a member of this one",
        },
    ],
}

# ------------------------------------------------------------------------- HST
# Primary HDU only, single instrument, so the store stays single-cohort. HST is the
# example that motivates cohorts and the first thing to revisit when they arrive.


def hst_member(ny, nx, exptime):
    return group(
        {
            "INSTRUME": "WFC3",
            "DETECTOR": "IR",
            "TELESCOP": "HST",
            "EXPTIME": exptime,
            "EQUINOX": 2000.0,
        },
        {
            "PRIMARY": array(
                [ny, nx], [ny, nx], "float32", ["y", "x"], fill_value="NaN", codecs=[{"name": "bytes", "configuration": {"endian": "little"}}]
            )
        },
    )


hst = {
    "name": "hst-wfc3-ir-primary",
    "why": "HST primary HDU, WFC3/IR only. FITS images are contiguous, so chunk shape "
    "tracks the array shape — the repeated variable spans shape and chunk_grid.",
    "constraint": group(
        {
            "INSTRUME": "WFC3",
            "DETECTOR": "IR",
            "TELESCOP": "HST",
            "EXPTIME": var("exptime", type="number", minimum=0),
            "EQUINOX": 2000.0,
        },
        {
            "PRIMARY": {
                **array(
                    [var("ny", type="integer", minimum=1), var("nx", type="integer", minimum=1)],
                    [var("ny", type="integer", minimum=1), var("nx", type="integer", minimum=1)],
                    "float32",
                    ["y", "x"],
                    fill_value="NaN",
                    codecs=[{"name": "bytes", "configuration": {"endian": "little"}}],
                )
            }
        },
    ),
    "members": [
        {"description": hst_member(ny, nx, t), "bindings": {"exptime": t, "ny": ny, "nx": nx}}
        for ny, nx, t in [(1024, 1024, 1402.9), (256, 256, 22.3)]
    ],
    "non_members": [
        {
            "description": at(hst_member(1024, 1024, 100.0), "consolidated_metadata/metadata/PRIMARY/chunk_grid/configuration/chunk_shape/0", 512),
            "why": "chunk shape no longer equals the array shape — the repeated variable",
        },
        {
            "description": at(hst_member(1024, 1024, 100.0), "attributes/INSTRUME", "ACS"),
            "why": "another instrument is another cohort; cohorts are deferred past M6",
        },
    ],
}

# ------------------------------------------------------------------- subsumption
# subsumes() gates evolve_schema, so its table gets its own fixture.

base = group({"a": 3, "b": "x"}, {"d": array([10], [10], "int32", ["t"])})

subsumption = {
    "name": "subsumption",
    "why": "the subsumes table from spec/constraint-language.md §3.3, as data",
    "constraint": Ellipsis,  # filled below
    "members": [],
    "non_members": [],
}

tight = group(
    {"a": 3, "b": "x"},
    {"d": array([10], [10], "int32", ["t"])},
)
loose_var = group(
    {"a": var("a", type="integer"), "b": "x"},
    {"d": array([10], [10], "int32", ["t"])},
)
loose_wild = group(
    {"a": wild("a"), "b": "x"},
    {"d": array([10], [10], "int32", ["t"])},
)
different_literal = group(
    {"a": 4, "b": "x"},
    {"d": array([10], [10], "int32", ["t"])},
)
narrow_var = group(
    {"a": var("a", type="integer", minimum=0, maximum=5), "b": "x"},
    {"d": array([10], [10], "int32", ["t"])},
)

subsumption = {
    "name": "subsumption",
    "why": "the subsumes table from spec/constraint-language.md §3.3, as data. "
    "`subsumes[i].loosened` is asked to generalise `constraint`.",
    "constraint": tight,
    "members": [{"description": tight, "bindings": {}}],
    "non_members": [{"description": different_literal, "why": "a differs"}],
    "subsumes": [
        {"loosened": tight, "holds": True, "why": "reflexive"},
        {"loosened": loose_var, "holds": True, "why": "a variable generalises a literal in its domain"},
        {"loosened": loose_wild, "holds": True, "why": "a wildcard generalises everything"},
        {"loosened": different_literal, "holds": False, "why": "literals must be equal"},
        {"loosened": narrow_var, "holds": True, "why": "3 is inside [0, 5]"},
        {
            "loosened": group(
                {"a": var("a", type="integer", minimum=4), "b": "x"},
                {"d": array([10], [10], "int32", ["t"])},
            ),
            "holds": False,
            "why": "3 is outside [4, inf)",
        },
    ],
}

for extra in (ome, sentinel2, mastu, hst):
    tighter = json.loads(json.dumps(extra["constraint"]))
    extra["subsumes"] = [
        {"loosened": tighter, "holds": True, "why": "reflexive"},
        {
            "loosened": {"$wild": "everything"},
            "holds": True,
            "why": "a wildcard at the root generalises any constraint",
        },
    ]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for fixture in (ome, sentinel2, mastu, hst, subsumption):
        path = OUT / f"{fixture['name']}.json"
        path.write_text(json.dumps(fixture, indent=2) + "\n")
        print(f"wrote {path.relative_to(OUT.parents[2])}")


if __name__ == "__main__":
    main()
