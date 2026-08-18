# Constraint language — normative encoding and semantics

Version `0.1`. Status: MVP. This document is normative for the JSON encoding and for
the three operations; [`PLAN.md`](../PLAN.md) holds the reasoning behind the
restrictions and the rejection trails.

A **constraint** is a JSON document in the same shape as the document it describes,
with named holes. The described document is a member's **description**: its complete
consolidated `zarr.json`, chunking included.

Every concrete JSON document is a valid constraint (all literals, no holes). That is
the "superset of JSON" property, and it is what makes
`create_collection(constraint=None)` possible — the first member's description is
taken verbatim as an all-literal constraint.

## 1. Leaves

A constraint node is one of:

| kind | encoding | column | matches |
|---|---|---|---|
| literal (scalar) | `1`, `"f4"`, `true`, `null` | no | that exact JSON value |
| literal (object/array) | `{...}`, `[...]` | structural — see §2 | recursively |
| escaped literal | `{"$literal": <any JSON>}` | no | that exact JSON value |
| variable | `{"$var": "nt", "type": "integer", "minimum": 1}` | yes | any scalar in the domain |
| wildcard | `{"$wild": "codecs"}` | yes | any JSON value whatsoever |

An object is a **variable leaf** iff it has a `$var` key, a **wildcard leaf** iff it
has a `$wild` key, and an **escaped literal** iff it has a `$literal` key. An object
carrying more than one of those keys is malformed. Any other object is structural.

`$literal` exists so that a description which genuinely contains a `$var` key (Zarr
attributes are free-form JSON) can still be described. It is rare; nothing in the
four v1 examples needs it.

### 1.1 Variables

`$var` names a hole. The name claims a column of the same name in `/meta`
(layout decision 6, [`layout.md`](./layout.md)), and it is the name under which the
member's value appears in a **binding set**.

A variable matches **scalars only** — string, number, integer, boolean, null. A leaf
that varies and is not a scalar must be a wildcard. This is what keeps
`subsumes` cheap and is why a `codecs` list that differs between members is replaced
*in its entirety* by a wildcard rather than aligned element-wise.

**Domains are declared inline** as sibling keys of `$var`, using a deliberately small
subset of JSON Schema:

```
type                one of "integer" | "number" | "string" | "boolean"
minimum, maximum    inclusive bounds; numeric types only
```

No `enum`. Categorical variation is a cohort, not a domain — see PLAN.md. No
`pattern`, no `multipleOf`, no composition keywords. Unknown sibling keys are
malformed rather than ignored, so that a later version can add domain keywords
without silently weakening old readers.

**A variable used more than once must declare byte-identical domains at every use
site.** Disagreement is a malformed document, caught by the meta-schema, not silently
resolved.

**Scoping.** A variable's *binding scope* is one member. Repeated use of a variable
within one document asserts those positions hold equal values **within a single
member**, and says nothing across members. This co-constraint is the thing JSON
Schema structurally cannot express, and the main reason not to use it.

### 1.2 Wildcards

`{"$wild": "name"}` matches any JSON value, including objects and arrays. The
member's actual value is stored **verbatim** in the named column so `substitute` can
reinstate it exactly.

Wildcards carry no domain and assert nothing. **A wildcard name must not be reused**
within a document: two occurrences would assert an equality the wildcard is
explicitly declining to assert.

Wildcard columns are JSON-encoded strings in `/meta` (see layout.md §4).

## 2. Structure

Objects match by key set: **the constraint's key set and the description's key set
must be equal.** No optionality in v1 — a member that lacks a key the constraint
declares, or carries one it does not, is rejected. If members legitimately differ in
which keys they have, choose a finer referenced unit (PLAN.md) or wildcard the whole
subtree.

Arrays match **positionally and by length**. A constraint array of length 3 matches
only descriptions of length 3, element-wise. Rank differences in `shape` or
`dimension_names` are therefore not expressible element-wise, and must be a
whole-leaf wildcard.

## 3. Operations

Three, and only three. There is no least-upper-bound, no anti-unification and no
inference anywhere in the design: **constraints are authored, not inferred.**

### 3.1 `meet(constraint, description) -> Bindings | [Mismatch]`

Unification of a constraint with a concrete document. Walks both in parallel:

- literal: must be **exactly equal** as JSON. No canonicalisation — see PLAN.md;
  this is sound only because every member is written by our own `add_item`.
  Equality is **JSON-value equality, not byte equality**: object key order is
  insignificant, and a number is compared as the value it denotes, so
  `1.0e308` and `1.0e+308` are the same literal. Implementations must parse floats
  to their nearest double *exactly* — a one-ULP parser breaks the round-trip law,
  and did, on real archive metadata.
- variable: the description leaf must be a scalar and satisfy the domain; the value
  is recorded as a binding. If the name is already bound in this member, the two
  values must be equal (the co-constraint).
- wildcard: any value; recorded verbatim.
- object: key sets must be equal; recurse per key.
- array: lengths must be equal; recurse per index.

Success yields a **binding set**: `name -> JSON value`, one entry per variable and
wildcard in the document. Failure yields a list of mismatches, each carrying a JSON
Pointer to the offending location, what was expected and what was found. Rejection
messages are user-facing: `add_item` refusing a member must say *which leaf* failed.

`meet` is total, deterministic, and does no IO.

**`meet` decomposes; it does not also validate as a separate concern.** It looks like
two operations — decide membership, extract bindings — but the deciding half is
already `subsumes` (§3.3), asked against the all-literal constraint admitting exactly
that document:

```
matches(c, d)  ≡  subsumes(c, from_description(d))
```

That equivalence is normative, and property-tested. What is irreducible about `meet`
is therefore the decomposition: splitting a document into the part the constraint
fixes and the bindings that vary, which §3.2 puts back together. A caller wanting only
the verdict may use either; a caller wanting the mismatches uses `meet`, which reports
all of them rather than stopping at the first.

### 3.2 `substitute(constraint, bindings) -> description`

The inverse. Replaces each variable and wildcard leaf by its bound value and returns
a concrete JSON document. Errors if a name is unbound, or if a bound value violates
its declared domain.

**Round-trip law (normative):** for every member `m` of a cohort with constraint `c`,

```
substitute(c, meet(c, description(m))) == description(m)
```

exactly, as JSON. Because a description is the whole `zarr.json` with nothing
dropped, derivability is literal rather than approximate. This law is the property
test that guards the whole design.

### 3.3 `subsumes(a, b) -> bool`

"Does `a` generalise `b`?" — every document matching `b` also matches `a`. It gates
`evolve_schema`, keeping schema evolution monotonic.

Deliberately a **cheap structural comparison** — no synthesis, no leastness proof, no
decision procedure over a general language. Position-wise:

| `a` | `b` | `a` subsumes `b`? |
|---|---|---|
| wildcard | anything | yes |
| variable | literal scalar | yes, iff the literal is in `a`'s domain |
| variable | variable | yes, iff `b`'s domain is contained in `a`'s |
| variable | wildcard | no |
| literal | literal | yes, iff exactly equal |
| literal | variable or wildcard | no |
| object | object | yes, iff key sets are equal and every value subsumes |
| array | array | yes, iff lengths are equal and every element subsumes |
| any | different structural kind | no |

Plus one non-local check: for each variable of `a` occurring at more than one
position, the corresponding positions in `b` must be **forced equal** — all the same
literal, or all the same variable name. Otherwise `a` asserts an equality `b` does
not, and `a` would reject documents `b` accepts.

Domain containment is the obvious interval test; a domain with no `type` and no
bounds contains every scalar domain.

**Laws** (property-tested): reflexive, transitive, and antisymmetric up to document
equality.

## 4. Well-formedness

A constraint document is well-formed iff:

1. every `$var` object has a `$var` string and only recognised domain keys;
2. every `$wild` object has exactly the one key `$wild`, a string;
3. no object carries two of `$var` / `$wild` / `$literal`;
4. all occurrences of one variable name declare identical domains;
5. no wildcard name occurs twice;
6. variable and wildcard namespaces are disjoint;
7. names match `^[A-Za-z_][A-Za-z0-9_]*$` — they become column names.

Rules 1–3 and 7 are expressible in JSON Schema and ship as
[`meta-schema.json`](./meta-schema.json). Rules 4–6 are cross-document and are
checked by `json_constraint::validate`.

## 5. Deferred syntax

Reserved, unimplemented, and rejected by the meta-schema in 0.1 so that adding them
later is not a breaking change: `$present` (optionality), `$each` / `$count`
(variable cardinality), `$expr` (arithmetic over bound variables). See PLAN.md for
the designs.
