# MAST-U tokamak — fusion

A collection of plasma diagnostic signals from the MAST-U tokamak.

```bash
python examples/mastu/run.py -n 40 --source amc
python examples/mastu/run.py -n 12 --offline
```

## The data

[FAIR-MAST](https://mastapp.site) publishes the MAST and MAST-U campaigns openly:
signal metadata through a JSON REST API, and the data itself as Zarr on a public S3
bucket at `s3://mast/level1/shots/<shot>.zarr`.

**This is a live instance of the problem the project is about.** The metadata lives
in one system and the arrays in another, and keeping them in step is somebody's
manual job. Here they become one store, and "which shots have this signal, and how
long is it?" is a SQL query rather than a call to a separate service.

The example reads signal metadata from the API — name, source, shape, dimensions,
units, quality, IMAS id — and writes each member metadata-only, with the array
declared at its true length but no chunks stored. Virtual chunk references through
VirtualiZarr's Zarr parser are the natural next step and need no change here.

## The referenced unit: (shot, diagnostic, signal)

Two things were checked against the real store, and both pushed the unit finer than
PLAN.md predicted:

1. **The store is Zarr v2, with per-diagnostic subgroups** — `/<source>/<signal>`,
   e.g. `/amc/plasma_current`. So a shot is not a flat referenced unit, as suspected.
2. **Signal availability varies *within* a diagnostic too.** Some shots have
   `p4l_coil_current`, others do not — so (shot, diagnostic) would still have needed
   optionality.

Taking **(shot, diagnostic, signal)** makes members structurally uniform again: one
member is one signal of one shot. A missing signal becomes a member that does not
exist, which needs no language support at all, and "which signals does shot 30420
have?" becomes a query rather than a schema question.

This is the plan's own most transferable lesson — *choose a finer referenced unit so
members are uniform* — applied one level deeper than it was written.

The fetch also filters to rank-1 time series (`dimensions == ["time"]`), since a
diagnostic's signals include profiles and 2-D data whose mixed ranks v1 cannot
express in one collection. Another finer-unit decision, made by the query rather than
by the language.

## How it is cataloged

One member is a group of two arrays: `data` (`float32`, dimension `time`) and `time`
(`float64`), both of length `nt`.

| hole | kind | where it appears | why |
|---|---|---|---|
| `nt` | variable, integer 1–10⁷ | `data`'s `shape[0]` **and** `time`'s `shape[0]` | the textbook case: time-series length varies per shot, and the two arrays must agree within a member |
| `signal_name` | variable, string | `/attributes/signal` | which signal this member is |
| `units` | variable, string | `data`'s `attributes/units` | varies by signal; often empty |
| `quality` | variable, string | `/attributes/quality` | FAIR-MAST's own quality flag |
| `signal_description` | variable, string | `/attributes/description` | free text from the API |
| `source_url` | variable, string | `/attributes/source_url` | which shot store this came from |

`nt` at two positions is the co-constraint: `data` and `time` are the same length as
each other within a member, and any length across members.

Literal — and so enforced on every write — are the diagnostic name, the IMAS block
(`{"ids": …, "homogeneous_time": 1}`), the dtypes, the dimension names and the chunk
shape. The IMAS block is fusion's own vocabulary, constrained in position and left
uninterpreted, exactly as OME's `multiscales` is.

Three **extra columns** make members addressable, since member ids are opaque
128-bit hashes: `shot`, `diagnostic`, `signal`.

## A caveat this example turned up

`units` is empty for every member in a typical run, and zarr-python does not write a
chunk whose values are all the fill value. A reader that requires every chunk to
exist then fails — `zarr-datafusion-search`'s provider did, with `chunk cannot be
found for key meta/units/c/0`. `/meta` writes now materialise empty chunks, which is
what upstream's own ingest does. Nothing else in the suite would have caught it.
