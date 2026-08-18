"""Zarr/Icechunk IO. The only module that touches a store.

Kept separate from `collection.py` so the interesting half — the transaction shape,
the two-phase check, the evolve plan — reads without Zarr API noise in the way.
"""

from __future__ import annotations

import contextlib
from typing import Any, Iterable

import numpy as np
import zarr

from . import _datacollections as _rs
from ._json import jsonable, loads
from .description import chunks_for

META_GROUP = "meta"
GROUPS_PREFIX = "groups"
#: `/meta` arrays are 1D and chunked along their single dimension.
COLUMN_CHUNK = 8192

_NUMPY_DTYPE = {"int64": "int64", "float64": "float64", "bool": "bool", "string": str}


def root_group(session) -> Any:
    return zarr.open_group(session.store, mode="a")


def read_root(session) -> Any:
    return zarr.open_group(session.store, mode="r")


# ------------------------------------------------------------------- member groups


def write_group(root, member_id: str, ds: Any) -> Any:
    """Write one member's group from an xarray Dataset.

    Chunk shape is **pinned** (see `description.chunks_for`), because chunk shape is
    part of the description and letting the writer pick it manufactures variables.

    A dataset carrying VirtualiZarr `ManifestArray`s is written through
    VirtualiZarr's own Icechunk writer, so virtual chunk references are stored rather
    than data copied — which is what keeps every example store small enough to host.
    """
    path = f"{GROUPS_PREFIX}/{member_id}"
    if is_virtual(ds):
        return _write_virtual_group(root, path, ds)

    group = root.create_group(path, attributes=jsonable(dict(ds.attrs)))
    for name, v in sorted({**ds.coords, **ds.data_vars}.items()):
        arr = group.create_array(
            str(name),
            shape=tuple(int(s) for s in v.shape),
            dtype=v.dtype if v.dtype.kind not in ("U", "S", "O") else str,
            chunks=chunks_for(v),
            dimension_names=[str(d) for d in v.dims],
            attributes=jsonable(dict(v.attrs)),
        )
        # `encoding["materialize"] = False` declares the member metadata-only: the
        # array exists with its full shape and dtype but no chunks are written. This
        # is what a search demo actually needs from a 10980x10980 band, and it is the
        # stopgap where VirtualiZarr has no parser for the source format — the real
        # path writes virtual chunk references instead.
        if v.encoding.get("materialize", True) and v.size:
            arr[...] = np.asarray(v.values)
    return group


def is_virtual(ds: Any) -> bool:
    """Does this Dataset contain VirtualiZarr ManifestArrays?"""
    try:
        from virtualizarr.manifests import ManifestArray  # type: ignore
    except ImportError:
        return False
    return any(isinstance(v.data, ManifestArray) for v in ds.variables.values())


def _write_virtual_group(root, path: str, ds: Any):
    """Delegate to VirtualiZarr's own Icechunk writer rather than reimplementing it.

    Only chunk *references* are written, so a member costs metadata alone — which is
    what keeps a whole example store small enough to host and version cheaply.

    Note the repository must have been created with a virtual chunk container
    covering the source URLs, and with `authorize_virtual_chunk_access` for it.
    That is the caller's business, not ours: it is a property of the repo, and the
    credentials involved are theirs.
    """
    accessor = getattr(ds, "vz", None) or getattr(ds, "virtualize", None)
    if accessor is None:  # pragma: no cover - depends on the installed version
        raise RuntimeError(
            "the Dataset holds ManifestArrays but VirtualiZarr exposes no writer "
            "accessor; upgrade virtualizarr"
        )
    accessor.to_icechunk(root.store, group=path)
    return zarr.open_group(root.store, path=path, mode="r")


def member_group(root, member_id: str) -> Any:
    return root[f"{GROUPS_PREFIX}/{member_id}"]


def member_ids(root) -> list[str]:
    col = column(root, "member_id")
    return [str(x) for x in col[:]] if col.shape[0] else []


# -------------------------------------------------------------------- the table


def create_meta(root, attributes: dict) -> Any:
    """Create `/meta` and one array per column, all empty."""
    meta = root.require_group(META_GROUP)
    meta.attrs.update(attributes)
    for col in loads(_rs.metadata_read(_dumps(attributes)))["columns"]:
        create_column(root, col)
    return meta


def write_meta_attributes(root, attributes: dict) -> None:
    meta = root.require_group(META_GROUP)
    # Replace wholesale: the constraint and the column list are one document, and a
    # partial update would let them disagree.
    for key in list(meta.attrs.keys()):
        del meta.attrs[key]
    meta.attrs.update(attributes)


def read_meta_attributes(root) -> dict:
    return dict(root[META_GROUP].attrs)


@contextlib.contextmanager
def writing():
    """Materialise chunks even when every value in them is the fill value.

    Interoperability, not correctness for us: zarr-python skips writing an all-fill
    chunk, and a reader that expects every chunk to exist then fails outright.
    `zarr-datafusion-search`'s DataFusion provider is such a reader — a MAST-U
    collection whose `units` column is empty for every member produced
    "chunk cannot be found for key `meta/units/c/0`". Upstream sets the same flag in
    its own ingest, for the same reason.

    It has to be set at *write* time rather than at creation: `write_empty_chunks` is
    a runtime array config, not part of `zarr.json`, so it does not survive reopening
    the array to append to it.
    """
    with zarr.config.set({"array.write_empty_chunks": True}):
        yield


def create_column(root, col: dict) -> Any:
    """One 1D array per column, resized by append.

    See `writing()` for why every write to these arrays materialises empty chunks.
    """
    name = col["name"]
    path = f"{META_GROUP}/{name}"
    dtype = _NUMPY_DTYPE[col["dtype"]]
    arr = root.create_array(
        path,
        shape=(0,),
        dtype=dtype,
        chunks=(COLUMN_CHUNK,),
        dimension_names=["row"],
        overwrite=True,
    )
    if col["role"] == "member_id":
        # The Arrow extension type, declared where Arrow Field metadata lives.
        arr.attrs.update(loads(_rs.group_ref_attributes()))
    return arr


def column(root, name: str) -> Any:
    return root[f"{META_GROUP}/{name}"]


def append_row(root, row: dict) -> int:
    """Extend every column by one. Ordering is append order; row i of every column
    describes the group named by `member_id[i]`."""
    n = None
    with writing():
        for name, cell in row.items():
            arr = column(root, name)
            i = arr.shape[0]
            n = i if n is None else n
            if i != n:
                raise RuntimeError(f"column `{name}` has {i} rows, expected {n} — table is torn")
            arr.resize((i + 1,))
            arr[i] = _cell_value(arr, cell)
    return n if n is not None else 0


def write_cells(root, name: str, values: Iterable[Any]) -> None:
    """Overwrite a whole column — the backfill path."""
    arr = column(root, name)
    values = list(values)
    arr.resize((len(values),))
    if values:
        with writing():
            arr[:] = np.array([_cell_value(arr, v) for v in values], dtype=arr.dtype)


def _cell_value(arr, cell):
    if arr.dtype.kind in ("U", "S", "O", "T"):
        return str(cell)
    return cell


def read_row(root, columns: Iterable[str], i: int) -> dict:
    out = {}
    for name in columns:
        v = column(root, name)[i]
        out[name] = _scalar(v)
    return out


def read_column(root, name: str) -> list:
    arr = column(root, name)
    return [_scalar(v) for v in arr[:]] if arr.shape[0] else []


def _scalar(v):
    if isinstance(v, np.generic):
        return v.item()
    if isinstance(v, np.str_):  # pragma: no cover - numpy version dependent
        return str(v)
    if isinstance(v, str):
        return v
    return jsonable(v)


def num_rows(root) -> int:
    return int(column(root, "member_id").shape[0])


def _dumps(value) -> str:
    from ._json import dumps

    return dumps(value)
