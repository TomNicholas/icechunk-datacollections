# Hubble Space Telescope — astronomy

A collection of HST exposures, catalogued from the archive's own observation
metadata.

```bash
python examples/hst/run.py -n 30
python examples/hst/run.py -n 8 --offline
```

## The data

Observation records for **WFC3/IR** from the [MAST](https://mast.stsci.edu) CAOM API:
observation id, filter, target, exposure time, proposal, start time. Each member is
one exposure's **primary HDU**, declared at the detector's true 1024 × 1024
`float32` and contiguous — FITS images are unchunked, so the chunk shape is the array
shape.

The FITS bytes are **not** virtualized here, and that is a deliberate shortcut rather
than an oversight: VirtualiZarr's FITS path goes through the kerchunk-backed reader,
which upstream calls temporary, and `s3://stpubdata` is requester-pays. Neither is a
good dependency for an example whose job is to test the factoring. Members are
written metadata-only.

## The referenced unit: one primary HDU, one instrument

A FITS file's multiple HDUs would become multiple Zarr nodes, so v1 takes the primary
HDU to stay flat.

**Scoping to a single instrument matters more.** HDU structure varies by instrument,
so WFC3, ACS and COS are genuinely different group *shapes* — not different values in
the same shape. Under the disjunction restriction those are separate **cohorts**, and
cohorts are deferred, so this collection holds WFC3/IR alone.

That makes HST the example that motivates cohorts, and the first thing to revisit
when they arrive. The run demonstrates the boundary rather than papering over it: it
builds a member with `INSTRUME = "ACS"` and shows it being rejected, with the note
that loosening `INSTRUME` to a variable would admit the header but not the different
HDU structure.

## How it is cataloged

One member is a group holding one array, `PRIMARY`, of shape `(1024, 1024)` and dtype
`float32`, with dimension names `y, x`.

| hole | kind | where it appears | why |
|---|---|---|---|
| `exptime` | variable, number ≥ 0 | `/attributes/EXPTIME` | exposure duration, per observation |
| `filter_name` | variable, string | `/attributes/FILTER` | F125W, G141, … |
| `target_name` | variable, string | `/attributes/TARGNAME` | what was pointed at |
| `proposal_id` | variable, string | `/attributes/PROPOSID` | which programme |

Note what this example does **not** vary, in contrast to MAST-U: the array shape is a
literal. Every WFC3/IR exposure has the same detector geometry, so what differs
between members is entirely in the header. Two domains, the same three operations,
opposite halves of the description doing the varying.

`TELESCOP`, `INSTRUME`, `DETECTOR` and `EQUINOX` are literals precisely *because* the
collection is scoped to one instrument — they are what defines the cohort, so a
member disagreeing on any of them is not a member.

Four **extra columns** carry what the group cannot know: `obs_id` (the archive's own
identifier, since member ids are opaque), `target`, `filter` and `mjd_start`.

## A bug this example found

Running at the full 100 members made three of them fail `verify()`: an `EXPTIME` of
`1305.8754880000001` came back from the store as `1305.875488` — adjacent doubles.
serde_json's default float parser is permitted that; `float_roundtrip` forbids it and
is now enabled, with the value pinned in a regression test.

For a project whose central claim is that a constraint plus a row reconstructs a
member's `zarr.json` *exactly*, a one-ULP drift is not a rounding detail. It is the
claim failing silently, on about 3% of real archive metadata.
