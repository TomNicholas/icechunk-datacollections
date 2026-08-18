"""Query: SQL over the `/meta` table, through upstream's DataFusion TableProvider.

The reading is **`zarr-datafusion-search`'s**, not ours. It already implements a
DataFusion `TableProvider` over Zarr-in-Icechunk — lazy chunk reads, predicate
pushdown, spatial indexes — and its store layout turned out to be the same one we
write: `/meta`, one 1-D array per column, node name as column name. So a
DataCollections store is queryable by it unmodified, and this module is the thin
layer PLAN.md says should "shrink toward zero as things upstream".

What is still ours, and only until two upstream PRs land:

- **The self-description.** Their schema builder reads array *names* and *dtypes*
  only, so the Arrow `Schema` metadata comes back empty and `member_id` carries no
  field metadata — our constraint never reaches the planner, and `zarr.group_ref` is
  invisible. [`attach_self_description`](#) puts both back after the fact. When
  upstream reads group and array attributes, that function deletes and the constraint
  arrives as a planner input for free. See `docs/upstream-zarr-datafusion-search.md`.

**Version pin, and it is not optional.** Their published wheel is built against
`datafusion == 53`. Running it under 54 does not raise — it **segfaults** (SIGBUS) as
soon as the FFI table provider is touched, even to read the schema. Hence the `==53`
pin in `pyproject.toml`, and the explicit check below, which turns a crash into a
message.
"""

from __future__ import annotations

import asyncio
import threading
import warnings
from typing import Any

from . import _datacollections as _rs
from . import store as _store
from ._json import dumps, loads

#: The group upstream's provider opens, and the one we write.
META_GROUP = "/meta"

#: The only DataFusion major their published wheel is ABI-compatible with.
REQUIRED_DATAFUSION_MAJOR = 53

_ARROW_TYPES = {
    "int64": "int64",
    "float64": "float64",
    "bool": "bool",
    "string": "string",
}


class QueryUnavailable(RuntimeError):
    """The query stack is missing or mismatched, with what to do about it."""


def _check_stack() -> None:
    try:
        import datafusion
        import zarr_datafusion_search  # noqa: F401
    except ImportError as e:
        raise QueryUnavailable(
            "the query extra is not installed: "
            "pip install 'datacollections[query]' (it pins datafusion==53)"
        ) from e

    major = int(datafusion.__version__.split(".")[0])
    if major != REQUIRED_DATAFUSION_MAJOR:
        raise QueryUnavailable(
            f"datafusion {datafusion.__version__} is installed, but the published "
            f"zarr-datafusion-search wheel is built against {REQUIRED_DATAFUSION_MAJOR}. "
            "Anything else segfaults across the FFI boundary rather than raising. "
            f"Pin datafusion=={REQUIRED_DATAFUSION_MAJOR}.*"
        )


def _run(make_coroutine):
    """Run an async factory from sync code, whether or not a loop is already running.

    Takes a *factory* rather than a coroutine because `ZarrTable.from_icechunk`
    needs a running loop at the moment it is called, not merely when awaited —
    building the future outside the loop raises `no running event loop`.

    Callers here are ordinary functions, and one of them may already be inside a loop
    (a STAC request handler, say), where `asyncio.run` refuses; hence the thread.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(make_coroutine())

    result: dict[str, Any] = {}

    def target():
        try:
            result["value"] = asyncio.run(make_coroutine())
        except BaseException as e:  # noqa: BLE001 - re-raised on the calling thread
            result["error"] = e

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result["value"]


def table_provider(collection):
    """Upstream's `ZarrTableProvider` over this collection's `/meta` group."""
    _check_stack()
    from zarr_datafusion_search import ZarrTable

    session = collection._repo.readonly_session(collection._branch)

    async def open_table():
        return await ZarrTable.from_icechunk(session, META_GROUP)

    return _run(open_table)


def context(collection, name: str = "members", upstream: bool = True):
    """A DataFusion context with the collection registered.

    The scan is upstream's, so predicates and projections reach the store rather
    than being applied after materialising every column.

    If upstream refuses the store, this falls back to materialising the table here
    and says why. That is not politeness: it keeps a collection queryable when the
    obstacle is an upstream limitation rather than anything wrong with the data —
    see `_materialise` — and the warning names the limitation so it stays visible.
    """
    from datafusion import SessionContext

    ctx = SessionContext()
    if upstream:
        try:
            provider = table_provider(collection)
            ctx.register_table(name, provider)
            # Pin the provider's lifetime to the context. Registering it hands
            # DataFusion an FFI handle but not a Python reference, so letting the
            # ZarrTable be collected fails at execution time with "TaskContextProvider
            # went out of scope over FFI boundary" — a long way from its cause.
            ctx._datacollections_provider = provider
            ctx._datacollections_upstream = True
            return ctx
        except QueryUnavailable:
            raise
        except BaseException as e:  # upstream panics come through as BaseException
            warnings.warn(
                "zarr-datafusion-search refused this store, so the table was "
                "materialised locally instead — no predicate pushdown, no lazy chunk "
                f"reads. {_likely_reason(collection, e)}",
                UpstreamRefused,
                stacklevel=2,
            )

    ctx.register_record_batches(name, [_materialise(collection).to_batches()])
    ctx._datacollections_upstream = False
    return ctx


class UpstreamRefused(UserWarning):
    """Upstream could not read this store, so the table was materialised locally.

    Known cause, and the reason PLAN.md wants the first upstream PR: a column named
    `bbox` that is not Zarr `bytes` is a hard error in their schema builder, not a
    column without an extension type. Our Sentinel-2 example has exactly that — a
    JSON-encoded `bbox` extra column — so it takes this path today.
    """


def _likely_reason(collection, error: BaseException) -> str:
    """Explain the refusal, since upstream's own message usually cannot be read.

    A Rust panic crossing the FFI boundary arrives as "rust future panicked: unknown
    error" — the real text ("Expected 'bbox' field to be of Zarr Bytes data type")
    goes to stderr and is lost. So where we know the cause, we name it ourselves.
    """
    names = {c["name"] for c in collection.columns}
    if "bbox" in names and collection.attributes:
        bbox = next(c for c in collection.columns if c["name"] == "bbox")
        if bbox["dtype"] != "binary":
            return (
                "Almost certainly the `bbox` special case: upstream requires a column "
                f"named `bbox` to be Zarr bytes, and this one is {bbox['dtype']}. "
                "Deleting that special case is the first upstream PR — see "
                "docs/upstream-zarr-datafusion-search.md. "
                f"(Upstream reported: {str(error)[:120]})"
            )
    return f"Reason: {str(error)[:200]}"


def _materialise(collection):
    """Read every column here and build the Arrow table ourselves.

    The fallback, and what this module did in full before upstream's provider was
    wired in. Fine at a hundred members, wrong at a million.
    """
    import pyarrow as pa

    root = _store.read_root(collection._repo.readonly_session(collection._branch))
    per_field = field_metadata(collection)
    fields, arrays = [], []
    for col in collection.columns:
        arrow_type = getattr(pa, _ARROW_TYPES[col["dtype"]])()
        fields.append(
            pa.field(col["name"], arrow_type, nullable=False, metadata=per_field[col["name"]])
        )
        arrays.append(pa.array(_store.read_column(root, col["name"]), type=arrow_type))
    schema = pa.schema(fields, metadata=schema_metadata(collection))
    return pa.Table.from_arrays(arrays, schema=schema)


def reads_through_upstream(collection) -> bool:
    """Does this collection query through upstream's provider, or the fallback?"""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UpstreamRefused)
        return bool(getattr(context(collection), "_datacollections_upstream", False))


def sql(collection, query: str, name: str = "members"):
    """Run SQL over the table. Returns a pyarrow Table.

    Note the context is held in a local until execution finishes. Letting it become
    a temporary — `context(...).sql(q).to_arrow_table()` — collects it mid-flight and
    fails with "TaskContextProvider went out of scope over FFI boundary", which is a
    long way from its cause.
    """
    ctx = context(collection, name)
    dataframe = ctx.sql(query)
    return dataframe.to_arrow_table()


def explain(collection, query: str, name: str = "members") -> str:
    """The physical plan, for checking that predicates actually reach the scan."""
    ctx = context(collection, name)
    rows = ctx.sql(f"EXPLAIN {query}").to_pydict()
    return "\n".join(rows.get("plan", []))


# ---------------------------------------------------------- the self-description
#
# Everything below exists because upstream does not read Zarr attributes yet. It is
# deliberately separable: when it does, delete this section, not the module.


def field_metadata(collection) -> dict[str, dict[str, str]]:
    """Per-column Arrow field metadata, read from each `/meta/<col>`'s attributes.

    This is where `zarr.group_ref` lives — `ARROW:extension:name` and
    `ARROW:extension:metadata`, the latter stored as real JSON in Zarr and
    stringified here because Arrow requires a string.
    """
    root = _store.read_root(collection._repo.readonly_session(collection._branch))
    out: dict[str, dict[str, str]] = {}
    for col in collection.columns:
        attrs = dict(_store.column(root, col["name"]).attrs)
        metadata = loads(_rs.arrow_field_metadata(dumps(attrs))) if attrs else {}
        metadata["datacollections:role"] = col["role"]
        if col.get("encoding"):
            metadata["datacollections:encoding"] = col["encoding"]
        out[col["name"]] = metadata
    return out


def schema_metadata(collection) -> dict[str, str]:
    """Schema-level metadata: the constraint, from `/meta`'s group attributes.

    `/meta` attributes map 1:1 onto Arrow `Schema` metadata, which is the whole
    reason layout decision 5 puts the constraint in group attributes — a planner
    that reads the table's schema gets it for free.
    """
    return {"datacollections": dumps(collection.attributes["datacollections"])}


def attach_self_description(collection, table):
    """Put our metadata back onto a table read through upstream's provider.

    Upstream builds fields from array names and dtypes alone, so both the constraint
    and the extension type are dropped on the way through. Re-attaching them here
    keeps the claim — that a DataCollections table is self-describing at the Arrow
    level — true today, and marks precisely what the upstream PRs would remove.
    """
    import pyarrow as pa

    per_field = field_metadata(collection)
    fields = [
        f.with_metadata(per_field[f.name]) if f.name in per_field else f
        for f in table.schema
    ]
    schema = pa.schema(fields, metadata=schema_metadata(collection))
    return table.cast(schema)


def arrow_schema(collection):
    """The table's Arrow schema, self-description included."""
    return attach_self_description(collection, to_arrow(collection, described=False)).schema


def to_arrow(collection, described: bool = True):
    """The whole table as an Arrow table.

    Materialises everything, so it is for inspection and small collections; `sql`
    is the path that scales, because the predicate reaches upstream's scan.
    """
    table = sql(collection, "SELECT * FROM members")
    return attach_self_description(collection, table) if described else table
