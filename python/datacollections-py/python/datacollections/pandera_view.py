"""pandera.xarray translation — an authoring surface and an export target, and
normative in neither direction.

A pandera schema validates **one** Dataset. A constraint describes a **collection**,
marking what varies with named variables that become columns; pandera has no way to
say "this dimension's length varies across members, and this column records it".
So a pandera schema corresponds roughly to one of our *all-literal* constraints plus
checks — useful both ways, normative neither way.

`constraint_to_pandera` therefore drops the cross-member half deliberately, and says
so: it projects one member's shape, dtypes and dims, leaving variables as unbounded
dimensions. `constraint_from_pandera` goes the other way for authoring, which
matters more now that constraints are authored rather than inferred.

Both are best-effort and importable without pandera installed; they raise only when
called.
"""

from __future__ import annotations

from typing import Any

from .constraint import Constraint, var, wild


def _require_pandera():
    try:
        import pandera  # noqa: F401
    except ImportError as e:  # pragma: no cover - depends on the environment
        raise ImportError(
            "pandera>=0.31 is needed for the pandera translation: pip install 'datacollections[pandera]'"
        ) from e


def _is_hole(node: Any) -> str | None:
    if isinstance(node, dict):
        if "$var" in node:
            return "var"
        if "$wild" in node:
            return "wild"
    return None


def constraint_to_pandera_dict(constraint: Constraint) -> dict:
    """The serialisable middle ground: `{array: {dtype, dims, shape}}`.

    A dimension whose length is a variable comes out as `None` — pandera's "any
    length" — which is exactly the information loss described above, made visible.
    """
    doc = constraint.document
    arrays = doc.get("consolidated_metadata", {}).get("metadata", {})
    out = {}
    for name, meta in arrays.items():
        if _is_hole(meta):
            continue
        shape = meta.get("shape", [])
        dims = meta.get("dimension_names", [])
        out[name] = {
            "dtype": meta.get("data_type") if not _is_hole(meta.get("data_type")) else None,
            "dims": [None if _is_hole(d) else d for d in dims] if not _is_hole(dims) else None,
            "shape": [None if _is_hole(s) else s for s in shape] if not _is_hole(shape) else None,
        }
    return out


def constraint_to_pandera(constraint: Constraint):
    """A live `pandera.xarray.DatasetSchema` a consumer can validate their own
    Dataset against before attempting `add_item`, in tooling they already know."""
    _require_pandera()
    import pandera.xarray as px  # type: ignore

    spec = constraint_to_pandera_dict(constraint)
    arrays: dict[str, Any] = {}
    coords: list[str] = []
    for name, s in spec.items():
        kwargs: dict[str, Any] = {}
        if s["dtype"]:
            kwargs["dtype"] = s["dtype"]
        if s["dims"] and all(d is not None for d in s["dims"]):
            kwargs["dims"] = tuple(s["dims"])
        if s["shape"]:
            # pandera spells "any length" as None in `shape`, which is exactly what
            # a variable dimension length becomes on the way out. That is the
            # information loss made visible: pandera can say the length is free, but
            # not that a *column* records it per member.
            kwargs["shape"] = tuple(s["shape"])
        # xarray promotes an array whose name is its own single dimension to a
        # coordinate. Zarr metadata has no such distinction — coordinate versus data
        # variable is an xarray notion, not a Zarr one — so the export applies
        # xarray's own rule rather than inventing one.
        # pandera takes coordinates as names only, so a coordinate's dtype and
        # length are dropped here — another place the projection loses information.
        if s["dims"] == [name]:
            coords.append(name)
        else:
            arrays[name] = px.DataArraySchema(**kwargs)
    return px.DatasetSchema(data_vars=arrays, coords=coords)


def constraint_from_pandera(schema, attributes: dict | None = None) -> Constraint:
    """Author a constraint from a pandera schema.

    Anything pandera leaves open — an unconstrained dimension length — becomes a
    variable named after the dimension, which is precisely the decision pandera
    cannot make for you and the user must therefore confirm.
    """
    _require_pandera()
    arrays = {}
    for name, array_schema in (getattr(schema, "data_vars", None) or {}).items():
        dims = list(getattr(array_schema, "dims", []) or [])
        shape = [var(f"n{d}", type="integer", minimum=0) for d in dims]
        dtype = getattr(array_schema, "dtype", None)
        arrays[str(name)] = {
            "zarr_format": 3,
            "node_type": "array",
            "shape": shape,
            "data_type": str(dtype) if dtype is not None else wild(f"{name}_dtype"),
            "chunk_grid": wild(f"{name}_chunk_grid"),
            "chunk_key_encoding": wild(f"{name}_chunk_key_encoding"),
            "fill_value": wild(f"{name}_fill_value"),
            "codecs": wild(f"{name}_codecs"),
            "attributes": wild(f"{name}_attributes"),
            "dimension_names": [str(d) for d in dims],
            "storage_transformers": [],
        }
    return Constraint(
        {
            "zarr_format": 3,
            "node_type": "group",
            "attributes": attributes if attributes is not None else wild("group_attributes"),
            "consolidated_metadata": {
                "kind": "inline",
                "must_understand": False,
                "metadata": arrays,
            },
        }
    )
