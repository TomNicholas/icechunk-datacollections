"""The constraint language, from Python.

Constraints are **authored, not inferred**. There is no generalisation operation
anywhere in the design, so this module is about making authoring pleasant: `var`,
`wild` and `like` build the document, and `Constraint` runs the three operations.

    from datacollections import Constraint, var, wild

    c = Constraint({
        "zarr_format": 3,
        "node_type": "group",
        "attributes": {"campaign": var("campaign", type="string")},
        "consolidated_metadata": {"kind": "inline", "must_understand": False,
                                  "metadata": {"time": {..., "shape": [var("nt", type="integer", minimum=1)]}}},
    })
"""

from __future__ import annotations

from typing import Any, Iterable

from . import _datacollections as _rs
from ._json import dumps, loads

__all__ = ["Constraint", "var", "wild", "literal", "Mismatch", "ConstraintError"]


class ConstraintError(ValueError):
    """A member did not match, or a constraint document is malformed."""


def var(name: str, **domain: Any) -> dict:
    """A named hole matching scalars in a domain.

    Domains are the deliberately small JSON Schema subset: `type` (integer, number,
    string, boolean), `minimum`, `maximum`. There is no `enum` — categorical
    variation is a cohort, not a domain.

    A variable used at more than one position asserts those positions are **equal
    within a single member**, and says nothing across members. That co-constraint is
    the thing JSON Schema structurally cannot express.
    """
    return {"$var": name, **domain}


def wild(name: str) -> dict:
    """A leaf we decline to describe: matches anything, stored verbatim.

    This is how a `codecs` list that differs between members is handled — replaced
    *in its entirety*, rather than aligned element-wise.
    """
    return {"$wild": name}


def literal(value: Any) -> dict:
    """An escaped literal, for a description that genuinely contains a `$var` key."""
    return {"$literal": value}


class Mismatch:
    """One reason a member was rejected. Carries the leaf, not just a verdict."""

    __slots__ = ("pointer", "kind", "expected", "found", "message")

    def __init__(self, d: dict):
        self.pointer = d["pointer"]
        self.kind = d["kind"]
        self.expected = d["expected"]
        self.found = d["found"]
        self.message = d["message"]

    def __repr__(self) -> str:
        return f"Mismatch({self.message})"

    def __str__(self) -> str:
        return self.message


class Constraint:
    """A well-formed constraint document."""

    __slots__ = ("_json", "_text")

    def __init__(self, document: dict | str | "Constraint"):
        if isinstance(document, Constraint):
            document = document.document
        text = document if isinstance(document, str) else dumps(document)
        try:
            self._text = _rs.constraint_check(text)
        except ValueError as e:
            raise ConstraintError(str(e)) from None
        self._json = loads(self._text)

    # ---------------------------------------------------------------- authoring

    @classmethod
    def from_description(cls, description: dict) -> "Constraint":
        """The all-literal constraint admitting exactly this description.

        This is `create_collection(constraint=None)`: the first member's `zarr.json`
        taken verbatim, with every later member having to match it exactly until the
        user says explicitly, via `evolve_schema`, what is allowed to vary.
        """
        return cls(_rs.constraint_from_description(dumps(description)))

    @property
    def document(self) -> dict:
        return self._json

    @property
    def declarations(self) -> list[dict]:
        """Every variable and wildcard, with its domain and use sites.

        Each one claims a column: "which columns must this table have?" is
        answerable from the constraint alone, without reading a row.
        """
        return loads(_rs.constraint_declarations(self._text))

    @property
    def variables(self) -> list[str]:
        return [d["name"] for d in self.declarations if d["kind"] == "variable"]

    @property
    def wildcards(self) -> list[str]:
        return [d["name"] for d in self.declarations if d["kind"] == "wildcard"]

    # --------------------------------------------------------------- operations

    def meet(self, description: dict) -> dict:
        """Is this a member? If so, its bindings. Raises with the offending leaf."""
        try:
            return loads(_rs.meet(self._text, dumps(description)))
        except ValueError as e:
            raise ConstraintError(str(e)) from None

    def mismatches(self, description: dict) -> list[Mismatch]:
        """The mismatches, as data. Empty means the description is a member."""
        return [Mismatch(m) for m in loads(_rs.mismatches(self._text, dumps(description)))]

    def matches(self, description: dict) -> bool:
        return not self.mismatches(description)

    def substitute(self, bindings: dict) -> dict:
        """Bindings back to the member's description — exactly, nothing dropped."""
        try:
            return loads(_rs.substitute(self._text, dumps(bindings)))
        except ValueError as e:
            raise ConstraintError(str(e)) from None

    def subsumes(self, other: "Constraint | dict") -> bool:
        """Does this constraint generalise `other`? Gates `evolve_schema`."""
        return self.explain_subsumes(other) is None

    def explain_subsumes(self, other: "Constraint | dict") -> str | None:
        """`None` if it subsumes, else why not."""
        other = other if isinstance(other, Constraint) else Constraint(other)
        return _rs.subsumes_explain(self._text, other._text)

    # ------------------------------------------------------------------- dunder

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Constraint) and other._json == self._json

    def __repr__(self) -> str:
        names = ", ".join(d["name"] for d in self.declarations) or "no holes"
        return f"<Constraint {names}>"


def like(description: dict, vary: Iterable[str] = (), **_: Any) -> Constraint:
    """Convenience: take a description verbatim, then punch wildcards at pointers.

    Deliberately *not* inference — the caller says which leaves vary, by JSON
    Pointer. It is a typing aid for the common "this document, but these bits move"
    case, and it never invents a domain.
    """
    import copy

    doc = copy.deepcopy(description)
    for pointer in vary:
        parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer.split("/")[1:]]
        cur = doc
        for p in parts[:-1]:
            cur = cur[int(p)] if isinstance(cur, list) else cur[p]
        name = "_".join(parts).replace(":", "_").replace("-", "_")
        if isinstance(cur, list):
            cur[int(parts[-1])] = wild(name)
        else:
            cur[parts[-1]] = wild(name)
    return Constraint(doc)
