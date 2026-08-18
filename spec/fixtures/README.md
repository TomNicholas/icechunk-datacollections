# Fixtures

Conformance data, shared by every crate and by the Python package. Fixtures are the
contract between components and the thing that keeps them honest.

Each `constraints/*.json` file is:

```json
{
  "name": "…",
  "why": "what this fixture is testing, in one line",
  "constraint": { … a constraint document … },
  "members":     [ { "description": { … }, "bindings": { … } } ],
  "non_members": [ { "description": { … }, "why": "which leaf should fail" } ],
  "subsumes":    [ { "loosened": { … }, "holds": true } ]
}
```

Every member must satisfy, for the fixture's constraint `c`:

- `meet(c, description) == bindings`
- `substitute(c, bindings) == description` — exactly, as JSON

Every non-member must fail `meet` with at least one mismatch. Every `subsumes` entry
asserts `subsumes(loosened, constraint) == holds`.

Descriptions are shaped like **consolidated `zarr.json`** — one document per group,
so there is no hierarchy walking at validate time (PLAN.md). The hand-written ones
are deliberately small; `ome-fov-generated.json` is emitted by the OME-Zarr example
from a real zarr-python write, and is the one that catches encoding drift.

Run the conformance suite with `cargo test -p json-constraint` and
`pytest python/datacollections-py/tests`.
