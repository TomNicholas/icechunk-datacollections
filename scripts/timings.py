"""Measure what a collection costs to build, at the ~100-member cap.

Not a benchmark of Icechunk's node-count scaling — that is the investigation PLAN.md
gates M7 on, and this deliberately stays inside the cap. What it does give is the
shape of the two costs the design predicts:

- an ordinary append is O(1), so per-member time should be flat;
- `evolve_schema` that creates a column is O(N) in existing members, because it
  backfills by reading every one of them.

    python scripts/timings.py -n 100
"""

from __future__ import annotations

import argparse
import copy
import pathlib
import shutil
import statistics
import sys
import time

import numpy as np
import xarray as xr

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "examples"))

import icechunk  # noqa: E402

from datacollections import Constraint, ExtraColumn, create_collection, var  # noqa: E402


def member(i: int) -> tuple[xr.Dataset, dict]:
    nt = 100 + i
    values = np.broadcast_to(np.zeros(1, dtype="float32"), (nt, 8))
    v = xr.Variable(("time", "channel"), values)
    v.encoding["chunks"] = (4096, 8)
    v.encoding["materialize"] = False
    ds = xr.Dataset({"data": v}, attrs={"diagnostic": "amc", "campaign": "M09"})
    return ds, {"shot": 30000 + i}


def loosen(constraint: Constraint) -> Constraint:
    doc = copy.deepcopy(constraint.document)
    doc["consolidated_metadata"]["metadata"]["data"]["shape"][0] = var(
        "nt", type="integer", minimum=1
    )
    return Constraint(doc)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--members", type=int, default=100)
    ap.add_argument("--store", default="/tmp/datacollections-timings")
    args = ap.parse_args()

    path = pathlib.Path(args.store)
    if path.exists():
        shutil.rmtree(path)
    repo = icechunk.Repository.create(icechunk.local_filesystem_storage(str(path)))
    coll = create_collection(
        repo, constraint=None, extra_columns=[ExtraColumn("shot", "int64")]
    )

    ds, extras = member(0)
    coll.add_item(ds, extras=extras)
    coll.evolve_schema(loosen(coll.constraint))

    appends = []
    evolutions = []
    for i in range(1, args.members):
        ds, extras = member(i)
        t0 = time.perf_counter()
        coll.add_item(ds, extras=extras)
        appends.append(time.perf_counter() - t0)

        # every 25 members, pay for a widening so the O(N) cost is visible
        n_now = len(coll)
        if n_now % 25 == 0:
            doc = copy.deepcopy(coll.constraint.document)
            doc["attributes"]["campaign"] = var(f"campaign_{n_now}", type="string")
            t0 = time.perf_counter()
            report = coll.evolve_schema(Constraint(doc))
            evolutions.append((n_now, time.perf_counter() - t0, report.rows_read))

    t0 = time.perf_counter()
    problems = coll.verify()
    verify_s = time.perf_counter() - t0

    size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    print(f"\n{len(coll)} members, {len(coll.columns)} columns, store {size / 1e6:.1f} MB")
    print(f"\nappend (O(1) expected): median {statistics.median(appends) * 1e3:.0f} ms, "
          f"first {appends[0] * 1e3:.0f} ms, last {appends[-1] * 1e3:.0f} ms")
    print("\nevolve_schema (O(N) expected):")
    for members, seconds, rows in evolutions:
        print(f"  at {members:>3} members: {seconds * 1e3:>6.0f} ms, {rows} groups read"
              f"  ({seconds / max(rows, 1) * 1e3:.1f} ms/member)")
    print(f"\nverify() over {len(coll)} members: {verify_s:.2f} s — {problems or 'consistent'}")
    t0 = time.perf_counter()
    rows = coll.sql("SELECT COUNT(*) AS n, MAX(nt) AS max_nt FROM members").to_pydict()
    print(f"SQL over the whole table: {(time.perf_counter() - t0) * 1e3:.0f} ms -> {rows}")


if __name__ == "__main__":
    main()
