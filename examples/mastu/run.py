"""MAST-U tokamak — fusion.

The strongest example, because FAIR-MAST is a *live instance of the problem*: it
publishes a separate JSON REST API for shot metadata alongside the Zarr data, which
is exactly the disconnected metadata store this project objects to. Here the two
become one store, and the metadata is queryable with SQL.

**Two findings from looking at the real data, which PLAN.md flagged as unverified:**

1. **Signals sit under per-diagnostic subgroups.** `s3://mast/level1/shots/<shot>.zarr`
   is **Zarr v2**, with `/<source>/<signal>` — e.g. `/amc/plasma_current`. So the
   plan's guess was right, and a shot is not a flat referenced unit.
2. **Signal availability varies within a diagnostic too**, not only across
   diagnostics. Taking (shot, diagnostic) as the unit would therefore still hit
   optionality — some shots have `p4l_coil_current`, others do not.

So this example takes **(shot, diagnostic, signal)** as the referenced unit: one
member is one signal of one shot. That is the plan's own technique applied one level
deeper than it predicted — *choose a finer referenced unit so members are
structurally uniform* — and it makes optionality unnecessary again rather than
motivating a language feature. A missing signal is simply a member that does not
exist, and "which signals does shot 30420 have?" becomes a SQL query rather than a
schema question.

Per-shot time-series length is then the textbook `{"$var": "nt"}` case: `nt`
constrains the array shape, the chunk shape, and the time coordinate at once.

Metadata comes live from <https://mastapp.site>. Arrays are declared but not
materialised (`encoding["materialize"] = False`); virtual chunk references via
VirtualiZarr's Zarr parser are the natural next step and need no change here.

Run:  python examples/mastu/run.py -n 40 --source amc
"""

from __future__ import annotations

import json
import pathlib
import sys
import urllib.request

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
)

API = "https://mastapp.site/json/signals"
RECORDED = pathlib.Path(__file__).parent / "recorded_signals.json"


def fetch_signals(source: str, n: int) -> list[dict]:
    """Page until we have n rank-1 time series.

    Filtering to `dimensions == ["time"]` is what keeps members structurally
    uniform: a diagnostic's signals include profiles and 2-D data too, and mixing
    ranks in one collection is exactly what v1 cannot express. Another finer-unit
    decision, made by the query rather than by the language.
    """
    signals: list[dict] = []
    for page in range(1, 12):
        url = f"{API}?filters=source$eq:{source}&per_page=200&page={page}"
        with urllib.request.urlopen(url, timeout=60) as response:
            payload = json.load(response)
        items = payload["items"] if isinstance(payload, dict) and "items" in payload else payload
        if not items:
            break
        signals += [s for s in items if s.get("dimensions") == ["time"]]
        if len(signals) >= n:
            break
    return signals[:n]


def load_signals(source: str, n: int, offline: bool) -> list[dict]:
    if not offline:
        try:
            signals = fetch_signals(source, n)
            RECORDED.write_text(json.dumps(signals[:8], indent=1))
            return signals
        except Exception as e:
            print(f"  mastapp.site unreachable ({e}); falling back to recorded signals")
    recorded = json.loads(RECORDED.read_text())
    return [recorded[i % len(recorded)] for i in range(n)]


def build_member(signal: dict) -> tuple[xr.Dataset, dict]:
    nt = int(signal["shape"][0])
    values = np.broadcast_to(np.zeros(1, dtype="float32"), (nt,))
    data = xr.Variable(("time",), values, attrs={"units": signal.get("units") or ""})
    time = xr.Variable(("time",), np.broadcast_to(np.zeros(1, dtype="float64"), (nt,)))
    for v in (data, time):
        v.encoding["chunks"] = (30000,)  # pinned, as the source store chunks it
        v.encoding["materialize"] = False

    ds = xr.Dataset(
        {"data": data},
        coords={"time": time},
        attrs={
            "diagnostic": signal["source"],
            "signal": signal["name"],
            "imas": {"ids": signal.get("imas") or "", "homogeneous_time": 1},
            "quality": signal.get("quality") or "Not Checked",
            "description": signal.get("description") or "",
            "source_url": signal.get("url") or "",
        },
    )
    extras = {
        "shot": int(signal["shot_id"]),
        "diagnostic": signal["source"],
        "signal": signal["name"],
    }
    return ds, extras


def author_constraint(first: dict) -> Constraint:
    """`nt` at four positions: the two arrays' shapes and their chunk shapes.

    Repeating one variable is the co-constraint — it asserts those positions are
    equal *within a member*, and says nothing across members. Note the chunk shape is
    part of the description, so it has to be described too; pinning it at ingest is
    what stops auto-chunking from manufacturing a second variable.
    """
    nt = var("nt", type="integer", minimum=1, maximum=10_000_000)
    doc = first
    for array in ("data", "time"):
        doc = substitute_leaf(doc, array_pointer(array, "shape", "0"), nt)
    # what differs between signals but says nothing structural
    for pointer, hole in [
        ("/attributes/signal", var("signal_name", type="string")),
        ("/attributes/quality", var("quality", type="string")),
        ("/attributes/description", var("signal_description", type="string")),
        ("/attributes/source_url", var("source_url", type="string")),
        (array_pointer("data", "attributes", "units"), var("units", type="string")),
    ]:
        doc = substitute_leaf(doc, pointer, hole)
    return Constraint(doc)


def main() -> None:
    args = parse_args("mastu", default_n=40, source="amc")
    source = args.source
    banner(f"MAST-U: {args.members} (shot, diagnostic, signal) members from FAIR-MAST")

    signals = load_signals(source, args.members, args.offline)
    print(f"{len(signals)} signals; shots {sorted({s['shot_id'] for s in signals})[:6]}…")
    print(f"time lengths present: {sorted({s['shape'][0] for s in signals})[:8]}…")

    repo = fresh_repo(args.store)
    coll = create_collection(
        repo,
        constraint=None,
        extra_columns=[
            ExtraColumn("shot", "int64", "the natural key; member ids are opaque"),
            ExtraColumn("diagnostic", "string"),
            ExtraColumn("signal", "string"),
        ],
    )

    ds, extras = build_member(signals[0])
    coll.add_item(ds, extras=extras)
    report = coll.evolve_schema(author_constraint(coll.constraint.document))
    print(f"\n{report}")

    ingest(coll, (build_member(s) for s in signals[1:]))
    print(f"\n{len(coll)} members; holes: {[d['name'] for d in coll.constraint.declarations]}")

    banner("The REST API and the array store are now one queryable thing")
    show_table(coll, "SELECT shot, signal, nt FROM members ORDER BY nt DESC")
    show_table(
        coll,
        "SELECT nt, COUNT(*) AS signals FROM members GROUP BY nt ORDER BY signals DESC",
    )
    show_table(
        coll,
        "SELECT shot, COUNT(*) AS signals_present FROM members GROUP BY shot ORDER BY shot",
    )

    banner("Derivability: constraint + row -> the member's zarr.json, exactly")
    member_id = coll.member_ids[0]
    reconstructed = coll.describe(member_id)
    print(f"  shape {reconstructed['consolidated_metadata']['metadata']['data']['shape']}, "
          f"chunks {reconstructed['consolidated_metadata']['metadata']['data']['chunk_grid']['configuration']['chunk_shape']}")
    print(f"  verify(): {coll.verify() or 'consistent'}")

    banner("A fusion-shaped view")
    view = View(
        {
            "name": "mastu-signal-record",
            "template": {
                "id": {"$join": [column("shot"), "/", column("diagnostic"), "/", column("signal")]},
                "shot": column("shot"),
                "diagnostic": column("diagnostic"),
                "samples": column("nt"),
                "units": column("units"),
                "imas": description("/attributes/imas"),
                "source": description("/attributes/source_url"),
            },
        }
    )
    print(json.dumps(coll.render(view, member_id), indent=2))
    print(f"\nstore: {args.store}")


if __name__ == "__main__":
    main()
