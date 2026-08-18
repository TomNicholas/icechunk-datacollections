"""`create_collection`, `add_item`, `evolve_schema`, `check`.

The whole point is that a member and its row land in **one Icechunk transaction**.
Nothing partially lands: if `meet` rejects a member after its group was written, the
session is discarded and the group write goes with it.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Iterable, Sequence

from . import _datacollections as _rs
from . import description as _description
from . import store as _store
from ._json import dumps, loads
from .constraint import Constraint, ConstraintError

__all__ = ["create_collection", "open_collection", "Collection", "EvolveReport", "ExtraColumn"]


@dataclasses.dataclass(frozen=True)
class ExtraColumn:
    """A column that is not a variable.

    Extra columns exist for real reasons: query convenience (a WKB `bbox`, an
    extracted `datetime`), ingest provenance, index support, and view-only fields
    with no counterpart in the Zarr group at all. They are **not recomputable** from
    a member's group, which is why their values must be passed to `add_item` — and
    why they are marked with a distinct role in the schema.

    Since member ids are opaque random hashes, extra columns are also the only way
    to address a member meaningfully: "shot 30420, diagnostic amc" is an extra-column
    query, and a derived STAC Item wants its human-meaningful `id` from one.
    """

    name: str
    dtype: str = "string"
    description: str | None = None

    def to_json(self) -> dict:
        d = {"name": self.name, "dtype": self.dtype}
        if self.description:
            d["description"] = self.description
        return d


@dataclasses.dataclass(frozen=True)
class EvolveReport:
    """What `evolve_schema` did. Cost is reported rather than hidden: an ordinary
    append is O(1), an evolution that creates a column is **O(N) in existing
    collection size**, and writers see that bimodal latency."""

    created: list[str]
    backfilled: list[str]
    demoted: list[str]
    rows_read: int

    @property
    def was_cheap(self) -> bool:
        return not self.backfilled

    def __str__(self) -> str:
        if self.was_cheap:
            return "evolve_schema: constraint loosened, no columns created (O(1))"
        return (
            f"evolve_schema: created {self.created}, backfilled by reading "
            f"{self.rows_read} member groups (O(N))"
        )


class Collection:
    """A DataCollections store: a `/meta` table plus the `/groups/*` it describes."""

    def __init__(self, repo, branch: str = "main", id_seed: int | None = None):
        self._repo = repo
        self._branch = branch
        self._id_seed = id_seed
        self._id_counter = 0
        self._attributes = self._read_attributes()

    def _read_attributes(self) -> dict | None:
        """`None` for a repository that has no `/meta` yet — a collection created
        with `constraint=None` and no members."""
        from zarr.errors import GroupNotFoundError

        try:
            root = _store.read_root(self._repo.readonly_session(self._branch))
            attrs = _store.read_meta_attributes(root)
        except (GroupNotFoundError, KeyError):
            return None
        return attrs or None

    # ------------------------------------------------------------------ opening

    @property
    def constraint(self) -> Constraint | None:
        """`None` only for an empty collection created with `constraint=None`, which
        takes its constraint from the first member's `zarr.json` verbatim."""
        if self._attributes is None:
            return None
        return Constraint(loads(_rs.metadata_read(dumps(self._attributes)))["constraint"])

    @property
    def columns(self) -> list[dict]:
        if self._attributes is None:
            return []
        return loads(_rs.metadata_read(dumps(self._attributes)))["columns"]

    @property
    def extra_columns(self) -> list[str]:
        return [c["name"] for c in self.columns if c["role"] == "extra"]

    def __len__(self) -> int:
        if self._attributes is None:
            return 0
        return _store.num_rows(_store.read_root(self._repo.readonly_session(self._branch)))

    def __repr__(self) -> str:
        return f"<Collection {len(self)} members, {len(self.columns)} columns>"

    @property
    def member_ids(self) -> list[str]:
        if self._attributes is None:
            return []
        return _store.member_ids(_store.read_root(self._repo.readonly_session(self._branch)))

    # -------------------------------------------------------------------- write

    def _next_id(self) -> str:
        if self._id_seed is None:
            return _rs.generate_ids(1, None)[0]
        # A seedable generator, so a test can assert that an incremental build is
        # byte-equivalent to a from-scratch one rather than "equivalent modulo ids".
        self._id_counter += 1
        return _rs.generate_ids(self._id_counter, self._id_seed)[-1]

    def add_item(self, ds: Any, extras: dict | None = None, message: str | None = None) -> str:
        """Add one member. Always strict — changing the schema is `evolve_schema`.

        One transaction covering: generate an id, write `/groups/<id>`, derive that
        group's description, `meet` it against the constraint, append the row,
        commit. Rejection at the `meet` step discards the group write too.
        """
        extras = dict(extras or {})
        constraint = self.constraint

        # Phase one: the cheap pre-check. Dims, shapes, dtypes and attributes are all
        # available before anything is written, so most rejections cost nothing.
        if constraint is not None:
            early = _description.precheck_mismatches(constraint, ds)
            if early:
                raise ConstraintError(
                    "member rejected before writing (pre-check): "
                    + "; ".join(str(m) for m in early)
                )

        session = self._repo.writable_session(self._branch)
        root = _store.root_group(session)
        member_id = self._next_id()
        _store.write_group(root, member_id, ds)

        # Phase two, authoritative: the description of what was actually written.
        written = _store.member_group(root, member_id)
        description = _description.of_group(written)

        if constraint is None:
            # Bootstrapping needs no inference: the first member's zarr.json is the
            # constraint, verbatim, and every later member must match it exactly.
            constraint = Constraint.from_description(description)
            attributes = loads(
                _rs.metadata_new(
                    dumps(constraint.document),
                    dumps([e.to_json() for e in self._pending_extras]),
                )
            )
            _store.create_meta(root, attributes)
            self._attributes = attributes

        try:
            bindings = constraint.meet(description)
        except ConstraintError as e:
            # Nothing commits, the group write included.
            raise ConstraintError(f"member rejected, nothing written: {e}") from None

        plan = loads(
            _rs.plan_append(
                dumps(self._attributes), member_id, dumps(bindings), dumps(extras)
            )
        )
        _store.append_row(root, plan["row"])
        session.commit(message or f"add member {member_id}")
        return member_id

    def evolve_schema(self, new_constraint: Constraint | dict, message: str | None = None) -> EvolveReport:
        """Loosen the constraint, explicitly and in its own transaction.

        Monotonic: `subsumes(new, current)` must hold. Any column the new constraint
        requires and the table does not have is **backfilled by reading every
        existing member's group metadata** — real values, no nulls, because a
        variable is by construction a hole in the group description, so its value for
        row i was never unknown, merely not yet materialised.
        """
        if self._attributes is None:
            raise ValueError("this collection has no constraint yet; add a member first")
        new_constraint = (
            new_constraint if isinstance(new_constraint, Constraint) else Constraint(new_constraint)
        )
        plan = loads(_rs.plan_evolve(dumps(self._attributes), dumps(new_constraint.document)))

        session = self._repo.writable_session(self._branch)
        root = _store.root_group(session)
        ids = _store.member_ids(root)

        for col in plan["created"]:
            _store.create_column(root, col)

        if plan["backfill"]:
            values: dict[str, list] = {name: [] for name in plan["backfill"]}
            for member_id in ids:
                description = _description.of_group(_store.member_group(root, member_id))
                bindings = new_constraint.meet(description)
                for name in plan["backfill"]:
                    cell = loads(
                        _rs.encode_cell(dumps(plan["attributes"]), name, dumps(bindings[name]))
                    )
                    values[name].append(cell)
            for name, cells in values.items():
                _store.write_cells(root, name, cells)

        _store.write_meta_attributes(root, plan["attributes"])
        session.commit(message or "evolve_schema")
        self._attributes = plan["attributes"]
        return EvolveReport(
            created=[c["name"] for c in plan["created"]],
            backfilled=list(plan["backfill"]),
            demoted=list(plan["demoted"]),
            rows_read=len(ids) if plan["backfill"] else 0,
        )

    # --------------------------------------------------------------------- read

    def check(self, ds: Any) -> list:
        """Would this Dataset be accepted? Reports the specific mismatches; writes
        nothing. Approximate in the same way the pre-check is — the authoritative
        answer needs the group to have been written."""
        constraint = self.constraint
        if constraint is None:
            return []
        return _description.precheck_mismatches(constraint, ds)

    def row(self, member_id: str) -> dict:
        """One member's row, decoded."""
        root = _store.read_root(self._repo.readonly_session(self._branch))
        ids = _store.member_ids(root)
        i = ids.index(member_id)
        names = [c["name"] for c in self.columns]
        raw = _store.read_row(root, names, i)
        return {
            name: loads(_rs.decode_cell(dumps(self._attributes), name, dumps(cell)))
            for name, cell in raw.items()
        }

    def describe(self, member_id: str) -> dict:
        """A member's description, reconstructed from the constraint and its row.

        This is the derivability claim: nothing is dropped, so the reconstruction is
        the member's `zarr.json` in full — not an approximation of it.
        """
        root = _store.read_root(self._repo.readonly_session(self._branch))
        ids = _store.member_ids(root)
        i = ids.index(member_id)
        names = [c["name"] for c in self.columns]
        row = _store.read_row(root, names, i)
        return loads(_rs.describe(dumps(self._attributes), dumps(row)))

    def rows(self) -> list[dict]:
        root = _store.read_root(self._repo.readonly_session(self._branch))
        names = [c["name"] for c in self.columns]
        columns = {name: _store.read_column(root, name) for name in names}
        n = len(columns["member_id"])
        return [
            {
                name: loads(_rs.decode_cell(dumps(self._attributes), name, dumps(columns[name][i])))
                for name in names
            }
            for i in range(n)
        ]

    def verify(self) -> list[str]:
        """Recompute every variable column from the groups and compare.

        `/meta` is a materialised view over `/groups/*`, so this is a free integrity
        check — and the same read a repair would use. Extra columns are skipped:
        they are the one thing that is not recomputable.
        """
        problems = []
        root = _store.read_root(self._repo.readonly_session(self._branch))
        constraint = self.constraint
        if constraint is None:
            return problems
        for member_id in _store.member_ids(root):
            actual = _description.of_group(_store.member_group(root, member_id))
            if self.describe(member_id) != actual:
                problems.append(f"{member_id}: reconstructed description differs from the group")
            try:
                constraint.meet(actual)
            except ConstraintError as e:
                problems.append(f"{member_id}: no longer matches the constraint: {e}")
        return problems

    # -------------------------------------------------------------------- views

    def to_arrow(self):
        """The table as an Arrow table, with `zarr.group_ref` on `member_id`."""
        from .query import to_arrow

        return to_arrow(self)

    def sql(self, query: str):
        from .query import sql

        return sql(self, query)

    def render(self, view, member_id: str) -> dict:
        """Project one member through a view mapping."""
        from .views import View

        view = view if isinstance(view, View) else View(view)
        return view.render(self.describe(member_id), self.row(member_id))

    @property
    def attributes(self) -> dict | None:
        """The raw `/meta` attributes, for anyone who wants to look."""
        return self._attributes


def create_collection(
    repo,
    constraint: Constraint | dict | None = None,
    extra_columns: Sequence[ExtraColumn | dict] | None = None,
    branch: str = "main",
    id_seed: int | None = None,
) -> Collection:
    """Create a collection in an Icechunk repository.

    `constraint=None` defers: the first member's `zarr.json` becomes an all-literal
    constraint, and every later member must match it exactly until `evolve_schema`
    says what may vary. That is the whole of bootstrapping — no inference is
    involved, here or anywhere else.
    """
    extras = [e if isinstance(e, ExtraColumn) else ExtraColumn(**e) for e in (extra_columns or [])]
    coll = Collection(repo, branch=branch, id_seed=id_seed)
    coll._pending_extras = extras

    if constraint is not None:
        constraint = constraint if isinstance(constraint, Constraint) else Constraint(constraint)
        attributes = loads(
            _rs.metadata_new(dumps(constraint.document), dumps([e.to_json() for e in extras]))
        )
        session = repo.writable_session(branch)
        root = _store.root_group(session)
        root.attrs.update({"datacollections": {"version": _rs.SPEC_VERSION}})
        _store.create_meta(root, attributes)
        session.commit("create collection")
        coll._attributes = attributes
    return coll


def open_collection(repo, branch: str = "main") -> Collection:
    coll = Collection(repo, branch=branch)
    coll._pending_extras = []
    if coll._attributes is None:
        raise ValueError("no /meta group in this repository — not a DataCollections store")
    return coll


# `_pending_extras` only matters between `create_collection` and the first
# `add_item`, and only when the constraint was deferred.
Collection._pending_extras: Iterable[ExtraColumn] = ()
