"""A member's **description**: its complete `zarr.json`, consolidated, chunking
included.

Two functions, and the difference between them is the two-phase check in
`add_item`:

- `of_group` reads the group that was actually written. **Authoritative.**
- `predicted` guesses it from an `xarray.Dataset` before anything is written, so
  most rejections are caught before a byte is committed. It cannot be authoritative,
  because the exact `zarr.json` depends on the writer's encoding choices.

**Finding, recorded here because it changed the design slightly:** Icechunk does not
support Zarr consolidated metadata at all — `zarr.consolidate_metadata` raises
`TypeError: The Zarr Store in use (IcechunkStore) doesn't support consolidated
metadata`, because Icechunk's own manifest already makes a group's children cheap to
enumerate. So the consolidated document is *derived* here rather than read from a
stored `consolidated_metadata` key. The one-document-per-group decision survives
intact — a member's description is still one document with no hierarchy walking at
validate time — it is simply assembled by the reader. v1 groups are flat (one level),
so the assembly is a single `members()` call.
"""

from __future__ import annotations

from typing import Any

from ._json import jsonable

#: v1 scopes every example to a flat referenced group: a group plus its child arrays.
MAX_DEPTH = 1


def of_group(group: Any) -> dict:
    """The consolidated description of a written Zarr group."""
    arrays = {}
    for name, member in sorted(group.members(), key=lambda kv: kv[0]):
        if getattr(member, "metadata", None) is None:
            continue
        meta = member.metadata.to_dict()
        if meta.get("node_type") != "array":
            raise ValueError(
                f"`{name}` is a nested group; v1 referenced groups are flat. "
                "Choose a finer referenced unit so members are structurally uniform."
            )
        arrays[name] = jsonable(meta)
    return {
        "zarr_format": 3,
        "node_type": "group",
        "attributes": jsonable(dict(group.attrs)),
        "consolidated_metadata": {
            "kind": "inline",
            "must_understand": False,
            "metadata": arrays,
        },
    }


# Zarr's defaults, as zarr-python writes them. We pin these explicitly at ingest
# rather than letting them vary: auto-chunking in particular will otherwise
# manufacture variables nobody asked for, since chunk shape is part of the
# description.
_DEFAULT_CODECS = [
    {"name": "bytes", "configuration": {"endian": "little"}},
    {"name": "zstd", "configuration": {"level": 0, "checksum": False}},
]
_STRING_CODECS = [
    {"name": "vlen-utf8", "configuration": {}},
    {"name": "zstd", "configuration": {"level": 0, "checksum": False}},
]


def _data_type(dtype) -> str:
    import numpy as np

    if dtype.kind in ("U", "S", "O", "T"):
        return "string"
    return np.dtype(dtype).name


def _fill_value(dtype) -> Any:
    if _data_type(dtype) == "string":
        return ""
    if dtype.kind == "b":
        return False
    if dtype.kind == "f":
        return 0.0
    return 0


def chunks_for(variable) -> tuple:
    """The chunk shape a variable will be written with.

    Pinned to the full array unless the caller set `encoding["chunks"]`. Pinning is
    deliberate: chunk shape is part of the description, so letting a writer pick it
    from array size turns it into a variable with its own column.
    """
    declared = variable.encoding.get("chunks")
    if declared:
        return tuple(declared)
    return tuple(variable.shape) if variable.shape else (1,)


def predicted(ds: Any) -> dict:
    """The description `write_group` is expected to produce for this Dataset.

    Used only for the cheap pre-check; `of_group` is what actually decides.
    """
    arrays = {}
    for name, v in sorted({**ds.coords, **ds.data_vars}.items()):
        dtype = v.dtype
        arrays[str(name)] = {
            "shape": [int(s) for s in v.shape],
            "data_type": _data_type(dtype),
            "chunk_grid": {
                "name": "regular",
                "configuration": {"chunk_shape": [int(c) for c in chunks_for(v)]},
            },
            "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
            "fill_value": _fill_value(dtype),
            "codecs": _STRING_CODECS if _data_type(dtype) == "string" else _DEFAULT_CODECS,
            "attributes": jsonable(dict(v.attrs)),
            "dimension_names": [str(d) for d in v.dims],
            "zarr_format": 3,
            "node_type": "array",
            "storage_transformers": [],
        }
    return {
        "zarr_format": 3,
        "node_type": "group",
        "attributes": jsonable(dict(ds.attrs)),
        "consolidated_metadata": {
            "kind": "inline",
            "must_understand": False,
            "metadata": arrays,
        },
    }


#: Leaves the pre-check trusts. Everything else in a predicted description depends
#: on encoding choices the writer makes, so a mismatch there is not evidence of a
#: bad member — only the post-write check is authoritative.
PRECHECK_LEAVES = ("/shape", "/data_type", "/dimension_names", "/attributes")

#: The one place a *key set* mismatch is trustworthy before writing: which arrays a
#: member has. Attribute key sets are not — writers add their own keys (xarray adds
#: `coordinates` to a group and `_FillValue` to a coordinate array, and VirtualiZarr's
#: Icechunk writer does the same), so a missing attribute key in the prediction is
#: evidence about the writer, not about the member.
ARRAY_SET_POINTER = "/consolidated_metadata/metadata"


def precheck_mismatches(constraint, ds) -> list:
    """Rejections we can be confident about before writing anything.

    Deliberately conservative: a pre-check that produces false rejections is worse
    than no pre-check, because it refuses members the authoritative check would
    accept. So it keeps only value mismatches on structural leaves — dims, shapes,
    dtypes, attribute values — plus a key-set mismatch on the array set itself.
    """
    kept = []
    for m in constraint.mismatches(predicted(ds)):
        if m.kind == "keyset":
            if m.pointer == ARRAY_SET_POINTER:
                kept.append(m)
            continue
        if not m.pointer or any(leaf in m.pointer for leaf in PRECHECK_LEAVES):
            kept.append(m)
    return kept
