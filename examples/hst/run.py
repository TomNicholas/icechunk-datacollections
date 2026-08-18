"""Hubble Space Telescope — astronomy, no STAC.

**Scoped to WFC3/IR, and to the primary HDU.** Both restrictions are the same move:
HDU structure varies by instrument, so WFC3, ACS and COS are genuinely different
group *shapes*, which under the disjunction restriction must become separate
cohorts — and cohorts are deferred past M6. Multiple HDUs per file would likewise
mean multiple Zarr nodes, so v1 stays flat. HST is therefore the example that
motivates cohorts, and the first thing to revisit when they arrive.

Observation metadata comes live from the MAST CAOM API. What varies across members:
exposure time, filter, target, proposal — all ordinary `$var` cases, and all of them
attributes rather than array structure, which is the useful contrast with MAST-U
where the *shape* varies.

**Shortcut, recorded rather than hidden:** the FITS bytes are not virtualized here.
VirtualiZarr's FITS path goes through the kerchunk-backed reader, which upstream
describes as temporary, and `s3://stpubdata` is requester-pays — neither is a good
dependency for an example whose job is to test the factoring. The primary HDU is
declared at its true WFC3/IR shape (1024×1024, float32, contiguous — FITS images
are unchunked, so chunk shape tracks array shape) with no chunks written.

Run:  python examples/hst/run.py -n 30
"""

from __future__ import annotations

import json
import pathlib
import sys
import urllib.parse
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

MAST = "https://mast.stsci.edu/api/v0/invoke"
RECORDED = pathlib.Path(__file__).parent / "recorded_observations.json"
#: WFC3/IR detector. Real, and fixed for this instrument — which is exactly why it
#: is a literal in the constraint rather than a variable.
DETECTOR = (1024, 1024)


def fetch_observations(n: int) -> list[dict]:
    request = {
        "service": "Mast.Caom.Filtered",
        "format": "json",
        "params": {
            "columns": "obs_id,instrument_name,filters,t_exptime,target_name,proposal_id,t_min",
            "filters": [
                {"paramName": "obs_collection", "values": ["HST"]},
                {"paramName": "instrument_name", "values": ["WFC3/IR"]},
                {"paramName": "dataproduct_type", "values": ["image"]},
            ],
            "pagesize": n,
            "page": 1,
        },
    }
    body = urllib.parse.urlencode({"request": json.dumps(request)}).encode()
    with urllib.request.urlopen(urllib.request.Request(MAST, data=body), timeout=90) as response:
        payload = json.load(response)
    rows = [r for r in payload.get("data", []) if r.get("t_exptime")]
    return rows[:n]


def load_observations(n: int, offline: bool) -> list[dict]:
    if not offline:
        try:
            rows = fetch_observations(n)
            RECORDED.write_text(json.dumps(rows[:8], indent=1))
            return rows
        except Exception as e:
            print(f"  MAST unreachable ({e}); falling back to recorded observations")
    recorded = json.loads(RECORDED.read_text())
    return [recorded[i % len(recorded)] for i in range(n)]


def build_member(obs: dict) -> tuple[xr.Dataset, dict]:
    values = np.broadcast_to(np.zeros(1, dtype="float32"), DETECTOR)
    primary = xr.Variable(("y", "x"), values, attrs={"BUNIT": "ELECTRONS/S"})
    # FITS images are contiguous and unchunked, so the chunk shape *is* the shape
    primary.encoding["chunks"] = DETECTOR
    primary.encoding["materialize"] = False

    ds = xr.Dataset(
        {"PRIMARY": primary},
        attrs={
            "TELESCOP": "HST",
            "INSTRUME": "WFC3",
            "DETECTOR": "IR",
            "EQUINOX": 2000.0,
            "EXPTIME": float(obs["t_exptime"]),
            "FILTER": obs.get("filters") or "UNKNOWN",
            "TARGNAME": obs.get("target_name") or "ANY",
            "PROPOSID": str(obs.get("proposal_id") or ""),
        },
    )
    extras = {
        "obs_id": obs["obs_id"],
        "target": obs.get("target_name") or "ANY",
        "filter": obs.get("filters") or "UNKNOWN",
        "mjd_start": float(obs.get("t_min") or 0.0),
    }
    return ds, extras


def author_constraint(first: dict) -> Constraint:
    """Instrument identity stays literal; the observation parameters vary.

    `INSTRUME`, `DETECTOR`, `TELESCOP` and `EQUINOX` are literals precisely because
    the collection is scoped to one instrument. Admitting ACS would not be a matter
    of loosening these — the HDU structure differs, which is a different cohort.
    """
    doc = first
    for pointer, hole in [
        ("/attributes/EXPTIME", var("exptime", type="number", minimum=0)),
        ("/attributes/FILTER", var("filter_name", type="string")),
        ("/attributes/TARGNAME", var("target_name", type="string")),
        ("/attributes/PROPOSID", var("proposal_id", type="string")),
    ]:
        doc = substitute_leaf(doc, pointer, hole)
    return Constraint(doc)


def main() -> None:
    args = parse_args("hst", default_n=30)
    banner(f"HST WFC3/IR: {args.members} primary HDUs, single instrument, single cohort")

    observations = load_observations(args.members, args.offline)
    print(f"{len(observations)} observations; filters: "
          f"{sorted({o.get('filters') for o in observations})[:8]}")

    repo = fresh_repo(args.store)
    coll = create_collection(
        repo,
        constraint=None,
        extra_columns=[
            ExtraColumn("obs_id", "string", "the archive's own id"),
            ExtraColumn("target", "string"),
            ExtraColumn("filter", "string"),
            ExtraColumn("mjd_start", "float64"),
        ],
    )

    ds, extras = build_member(observations[0])
    coll.add_item(ds, extras=extras)
    report = coll.evolve_schema(author_constraint(coll.constraint.document))
    print(f"\n{report}")

    ingest(coll, (build_member(o) for o in observations[1:]))
    print(f"\n{len(coll)} members; holes: {[d['name'] for d in coll.constraint.declarations]}")

    banner("What a different instrument would do, and why it needs cohorts")
    acs = build_member(observations[0])[0]
    acs.attrs["INSTRUME"] = "ACS"
    problems = coll.check(acs)
    print(f"  {problems[0] if problems else 'accepted (unexpected)'}")
    print("  Loosening INSTRUME to a variable would admit the header but not the "
          "different HDU structure — that is what cohorts are for.")

    banner("SQL over the observation metadata")
    show_table(coll, "SELECT obs_id, target, filter_name, exptime FROM members ORDER BY exptime DESC")
    show_table(
        coll,
        "SELECT filter_name, COUNT(*) AS n, AVG(exptime) AS mean_exptime "
        "FROM members GROUP BY filter_name ORDER BY n DESC",
    )

    banner("An astronomy-shaped view")
    view = View(
        {
            "name": "hst-exposure-record",
            "template": {
                "id": column("obs_id"),
                "instrument": {
                    "telescope": description("/attributes/TELESCOP"),
                    "name": description("/attributes/INSTRUME"),
                    "detector": description("/attributes/DETECTOR"),
                },
                "exposure": {"seconds": column("exptime"), "filter": column("filter_name")},
                "image": {"shape": description(array_pointer("PRIMARY", "shape"))},
            },
        }
    )
    print(json.dumps(coll.render(view, coll.member_ids[0]), indent=2))
    print(f"\n  verify(): {coll.verify() or 'consistent'}")
    print(f"\nstore: {args.store}")


if __name__ == "__main__":
    main()
