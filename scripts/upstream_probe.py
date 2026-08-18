"""Can upstream's DataFusion TableProvider read a DataCollections store, unmodified?

Answer, as of this writing: **yes.** `zarr-datafusion-search` 0.1.2 from PyPI opens
our `/meta` group and runs real SQL over it with no changes to either side. That is
the layout-convergence claim tested rather than argued.

What it does *not* pick up is exactly the two upstream changes PLAN.md wants: the
Arrow `Schema` metadata is empty (so our constraint never reaches the planner) and
`member_id` carries no field metadata (so `zarr.group_ref` is invisible). Both come
back `None` below, which is the point of printing them.

**One trap, and it is fatal rather than noisy:** the published wheel is built against
`datafusion == 53`, and running it with `datafusion 54` **segfaults** (SIGBUS) as
soon as the FFI table provider is touched — even to read the schema. Pin 53 until a
wheel is rebuilt.

    uv venv --python 3.11 /tmp/df53
    VIRTUAL_ENV=/tmp/df53 uv pip install "datafusion==53.*" zarr-datafusion-search icechunk==1.1.21
    /tmp/df53/bin/python scripts/upstream_probe.py examples/hst/store
"""

from __future__ import annotations

import asyncio
import sys
import warnings

warnings.filterwarnings("ignore")


async def probe(store_path: str, group: str = "/meta") -> None:
    import datafusion
    import icechunk
    from datafusion import SessionContext
    from zarr_datafusion_search import ZarrTable

    major = int(datafusion.__version__.split(".")[0])
    if major != 53:
        print(
            f"! datafusion {datafusion.__version__} — the published wheel is built against 53.\n"
            "! Anything other than 53 segfaults across the FFI boundary. Refusing to continue.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    repo = icechunk.Repository.open(icechunk.local_filesystem_storage(store_path))
    table = await ZarrTable.from_icechunk(repo.readonly_session("main"), group)

    ctx = SessionContext()
    ctx.register_table("members", table)
    schema = ctx.table("members").schema()

    print(f"opened {store_path}{group} with zarr-datafusion-search\n")
    print("columns it sees:")
    for field in schema:
        print(f"  {field.name:20} {field.type}")

    print("\nSQL over our table, through their provider:")
    rows = ctx.sql("SELECT * FROM members LIMIT 3").to_pydict()
    for name, values in list(rows.items())[:4]:
        print(f"  {name:20} {values}")

    print("\nWhat it does not pick up — the two upstream changes, as data:")
    print(f"  Schema metadata (our constraint):     {getattr(schema, 'metadata', None)}")
    member_id = next((f for f in schema if f.name == "member_id"), None)
    print(f"  member_id metadata (zarr.group_ref):  {getattr(member_id, 'metadata', None)}")


if __name__ == "__main__":
    store = sys.argv[1] if len(sys.argv) > 1 else "examples/hst/store"
    asyncio.run(probe(store))
