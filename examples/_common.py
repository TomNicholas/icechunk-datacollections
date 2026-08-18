"""Shared plumbing for the examples.

Every example does the same five things, and the point of having four of them is
that the *only* thing that differs is the domain vocabulary:

1. build members as `xarray.Dataset`s,
2. author a constraint saying what is invariant and what varies,
3. `add_item` each member into one Icechunk store,
4. query the table with SQL,
5. project members through a view — STAC for one domain, something else for three.

If any of that needed domain-specific machinery in the core, the factoring would be
wrong. That is what these examples are for.
"""

from __future__ import annotations

import argparse
import copy
import pathlib
import shutil
from typing import Any, Callable, Iterable

import icechunk

#: Hard cap until the Icechunk node-count investigation happens. Icechunk's scaling
#: in *number of nodes* (not rows) is the plan's main structural risk, so every
#: example stays deliberately small rather than discovering the limits by accident.
MAX_MEMBERS = 100

EXAMPLES = pathlib.Path(__file__).resolve().parent


def fresh_repo(path: str | pathlib.Path) -> icechunk.Repository:
    path = pathlib.Path(path)
    if path.exists():
        shutil.rmtree(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return icechunk.Repository.create(icechunk.local_filesystem_storage(str(path)))


def parse_args(name: str, default_n: int = 20, **flags: Any) -> argparse.Namespace:
    """Shared flags, plus any example-specific ones as `flag=default`."""
    p = argparse.ArgumentParser(description=f"DataCollections example: {name}")
    p.add_argument("-n", "--members", type=int, default=default_n)
    for flag, default in flags.items():
        p.add_argument(f"--{flag.replace('_', '-')}", default=default)
    p.add_argument("--store", default=str(EXAMPLES / name / "store"))
    p.add_argument("--offline", action="store_true", help="use recorded metadata, no network")
    args = p.parse_args()
    if args.members > MAX_MEMBERS:
        raise SystemExit(
            f"{args.members} members exceeds the {MAX_MEMBERS} cap; going beyond it needs the "
            "Icechunk node-count investigation first (PLAN.md)"
        )
    return args


def substitute_leaf(document: dict, pointer: str, value: Any) -> dict:
    """Replace one leaf of a description by a hole, addressed by JSON Pointer.

    Authoring helper only. It is not inference: the caller says exactly which leaf
    varies and what its domain is.
    """
    document = copy.deepcopy(document)
    parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer.split("/")[1:]]
    cur = document
    for p in parts[:-1]:
        cur = cur[int(p)] if isinstance(cur, list) else cur[p]
    last = parts[-1]
    if isinstance(cur, list):
        cur[int(last)] = value
    else:
        cur[last] = value
    return document


def array_pointer(array: str, *rest: str) -> str:
    """The pointer to one array's metadata inside a consolidated description."""
    return "/".join(["/consolidated_metadata/metadata", array, *rest])


def ingest(coll, members: Iterable[tuple[Any, dict]], report: Callable[[str], None] = print) -> list[str]:
    """Add members, reporting which appends were cheap and which were rejected."""
    ids = []
    for i, (ds, extras) in enumerate(members):
        ids.append(coll.add_item(ds, extras=extras))
        if i == 0 or (i + 1) % 25 == 0:
            report(f"  … {i + 1} members")
    return ids


def show_table(coll, query: str, limit: int = 5) -> None:
    table = coll.sql(query).to_pydict()
    n = len(next(iter(table.values()), []))
    print(f"\n  {query}")
    for i in range(min(n, limit)):
        row = {k: v[i] for k, v in table.items()}
        print(f"    {row}")
    if n > limit:
        print(f"    … {n - limit} more rows")


def banner(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")
