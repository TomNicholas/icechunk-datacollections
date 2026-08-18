"""Query: the `/meta` table as Arrow, and SQL over it with DataFusion.

**Scope note.** The Rust home for this is upstream — `zarr-datafusion-search`
already implements a DataFusion `TableProvider` over Zarr-in-Icechunk, and
`zarr-collection-query` is designed to shrink toward zero as things upstream. What
is here is the MVP equivalent: read the columns, build an Arrow table with the
extension type and the constraint attached where a planner would look for them, and
hand it to DataFusion. It is honest about what it is — no predicate pushdown, no
lazy chunk reads — and it exercises the part of the design that matters, which is
that the constraint is a **planner input** available from the table's schema.

The one upstream fix worth carrying over: build Arrow extension types from **array
attributes** rather than special-casing a column named `bbox`.
"""

from __future__ import annotations

from typing import Any

from . import _datacollections as _rs
from . import store as _store
from ._json import dumps, loads

_ARROW_TYPES = {
    "int64": "int64",
    "float64": "float64",
    "bool": "bool",
    "string": "string",
}


def arrow_schema(collection) -> Any:
    """The Arrow schema, with `/meta` attributes as schema metadata and each
    column's Zarr attributes as field metadata.

    That mapping — `/meta` group attributes ↔ Arrow `Schema` metadata, `/meta/<col>`
    array attributes ↔ Arrow `Field` metadata — is why the constraint lives in group
    attributes: DataFusion then gets it with the table schema, for free.
    """
    import pyarrow as pa

    root = _store.read_root(collection._repo.readonly_session(collection._branch))
    fields = []
    for col in collection.columns:
        arrow_type = getattr(pa, _ARROW_TYPES[col["dtype"]])()
        attrs = dict(_store.column(root, col["name"]).attrs)
        metadata = loads(_rs.arrow_field_metadata(dumps(attrs))) if attrs else {}
        metadata["datacollections:role"] = col["role"]
        if col.get("encoding"):
            metadata["datacollections:encoding"] = col["encoding"]
        fields.append(pa.field(col["name"], arrow_type, nullable=False, metadata=metadata))

    schema_metadata = {
        "datacollections": dumps(collection.attributes["datacollections"]),
    }
    return pa.schema(fields, metadata=schema_metadata)


def to_arrow(collection) -> Any:
    """The whole table. Wildcard columns stay JSON-encoded strings — decoding them
    is `substitute`'s job, not the query engine's."""
    import pyarrow as pa

    root = _store.read_root(collection._repo.readonly_session(collection._branch))
    schema = arrow_schema(collection)
    arrays = [
        pa.array(_store.read_column(root, col["name"]), type=schema.field(col["name"]).type)
        for col in collection.columns
    ]
    return pa.Table.from_arrays(arrays, schema=schema)


def context(collection, name: str = "members"):
    """A DataFusion context with the collection registered."""
    from datafusion import SessionContext

    ctx = SessionContext()
    ctx.register_record_batches(name, [to_arrow(collection).to_batches()])
    return ctx


def sql(collection, query: str, name: str = "members"):
    """Run SQL over the table. Returns a pyarrow Table."""
    return context(collection, name).sql(query).to_arrow_table()
