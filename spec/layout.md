# Store layout — normative convention

Version `0.1`. Substrate: **Zarr v3 in Icechunk** (PLAN.md reserved question 1 is
settled by assumption for v1).

A DataCollections store is one Icechunk repository containing a metadata table and
the referenced groups it describes, **in the same store**, so that a member and its
row land in one atomic commit. That is the thesis; see PLAN.md layout decision 1.

## 1. Tree

```
/                          root group
    attributes: {"datacollections": {"version": "0.1"}}

/meta                      group — the table. Maps 1:1 to an Arrow Schema.
    attributes: {"datacollections": { ... see §2 ... }}

/meta/member_id            1D string array, one element per member. The join key.
/meta/<column>             1D array, one per column of the table. Same length, same order.

/groups/<member_id>        one group per member, with consolidated metadata.
```

Row `i` of every `/meta/*` array describes the group named by `/meta/member_id[i]`.
Order is append order and is not otherwise meaningful.

Member ids are **generated, not supplied**: 128 random bits, hex-encoded (32 chars).
See PLAN.md — uniqueness without coordination, and deliberately not content-addressed.
A seedable generator exists so tests can assert byte-equivalence between an
incremental build and a from-scratch one.

## 2. `/meta` attributes

All DataCollections state lives under a single `datacollections` key, so the
namespace does not collide with anything a user puts in group attributes.

```json
{
  "datacollections": {
    "version": "0.1",
    "cohorts": {
      "default": { "constraint": { "…": "a constraint document" } }
    },
    "columns": {
      "member_id": { "role": "member_id", "dtype": "string" },
      "nt":        { "role": "variable", "dtype": "int64",  "cohort": "default" },
      "codecs":    { "role": "wildcard", "dtype": "string", "cohort": "default",
                     "encoding": "json" },
      "shot":      { "role": "extra",    "dtype": "int64" }
    }
  }
}
```

**The constraint lives here, not in the extension type** (layout decision 5): it
specifies which columns must exist, so it belongs at schema level. `/meta`'s
attributes map 1:1 onto Arrow `Schema` metadata, which is how DataFusion gets the
constraint as a planner input for free.

**Cohorts are a keyed map from day one** even though v1 has exactly one, named
`default` (layout decision 3). Adding a second cohort later is then additive rather
than a breaking attribute-shape change.

**Every variable and wildcard of the constraint must have a column; extra columns are
permitted** (layout decision 6). The check is mechanical and runs on open and on
every write.

## 3. `zarr.group_ref` — the Arrow extension type

Declared in the **array** attributes of `/meta/member_id`, mirroring Arrow `Field`
metadata:

```json
{
  "ARROW:extension:name": "zarr.group_ref",
  "ARROW:extension:metadata": {
    "version": "0.1",
    "storage_types": ["utf8_view", "utf8", "large_utf8"],
    "resolve": "/groups/{id}"
  }
}
```

Stored as **real JSON** in Zarr attributes — readable in `zarr.json`, no double
escaping — and stringified when the Arrow `Field` is constructed, because Arrow
requires `ARROW:extension:metadata` to be a string.

It carries only genuinely column-scoped things: the supported storage types, the
id→location resolution rule, and the spec version. Nothing that changes per commit,
so a routine append never mutates the column's type identity.

An implementation that does not recognise the extension name degrades to the storage
type — a plain string column — with every value still readable. That graceful
degradation is why this is an Arrow extension type rather than a custom Zarr dtype.

`resolve` is `/groups/{id}` for the same-store layout. A store may instead declare
`{"resolve": "uri"}`, meaning the column holds absolute URIs; that variant is
recorded in the spec because it is useful to other people, and is explicitly **not**
our fallback (PLAN.md layout decision 1).

## 4. Column types

| constraint leaf | `dtype` | encoding |
|---|---|---|
| `$var` with `"type": "integer"` | `int64` | native |
| `$var` with `"type": "number"` | `float64` | native |
| `$var` with `"type": "boolean"` | `bool` | native |
| `$var` with `"type": "string"` or no type | `string` | native |
| `$wild` | `string` | **JSON-encoded value**, `"encoding": "json"` |
| extra column | declared by the user | native |

Wildcard columns hold `json.dumps` of the member's value, which is what lets
`substitute` reinstate a whole `codecs` list verbatim.

All `/meta` arrays are 1D, chunked along the single dimension (8192 by default), and
resized by append.

## 5. Write paths

Both are **one Icechunk transaction**. Nothing partially lands.

### `add_item` — always strict

1. cheap pre-check of the Dataset against the constraint (dims, shapes, dtypes,
   attrs are all available before writing) — catches most rejections early;
2. generate an id, write `/groups/<id>`, consolidate its metadata;
3. read back the group's consolidated `zarr.json` — the authoritative description;
4. `meet` it against the cohort constraint; on failure, **abort the whole
   transaction**, group write included;
5. append the bindings, the id and any extra column values as one row;
6. commit.

The pre-check cannot be authoritative because the exact `zarr.json` depends on the
writer's encoding choices, so the real check happens post-write and pre-commit. Two
phase, by design.

### `evolve_schema` — explicit, separate, monotonic

1. `subsumes(new, current)` must hold — evolution may only loosen;
2. create any column the new constraint requires and does not yet have, and
   **backfill it for every existing member by reading that member's group metadata**
   (layout decision 2). Real values, no nulls. O(N) in collection size, and the API
   says so rather than hiding it;
3. commit the new constraint and the new columns together.

Columns for variables that the new constraint no longer has are **kept**, as extra
columns. Tightening therefore costs no data migration.

## 6. Consistency

`/meta` is a **materialised view** over `/groups/*`: every variable and wildcard
column is recomputable by re-reading the member's `zarr.json` and re-running `meet`.
That gives a free integrity check (`collection.verify()`), a repair path, and the
guarantee that a widening backfill can always be satisfied. Extra columns are the
exception — not recomputable, which is why they are marked with a distinct role.
