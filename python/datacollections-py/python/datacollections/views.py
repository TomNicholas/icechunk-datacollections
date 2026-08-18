"""Views — projections of constraint + bindings into some other format.

STAC is one of them, and nothing about it is privileged. A view is a template of the
target format with two kinds of hole:

- `{"$from": "column:<name>"}` — any column: variable, wildcard, **or extra**
- `{"$from": "description:/json/pointer"}` — into the reconstructed description

plus `{"$join": [...]}` for building a human-meaningful id out of extra columns,
which is what opaque member ids force you to do.

Do not conflate the two directions: `substitute` reads only variable columns, so
derivability is unaffected by extra columns, while a *view* may read anything.
"""

from __future__ import annotations

from typing import Any, Iterable

from . import _datacollections as _rs
from ._json import dumps, loads

__all__ = ["View", "column", "description", "stac_item_view", "stac_collection", "stac_items"]


def column(name: str) -> dict:
    """A view hole reading a column."""
    return {"$from": f"column:{name}"}


def description(pointer: str) -> dict:
    """A view hole reading a JSON Pointer into the member's description."""
    return {"$from": f"description:{pointer}"}


class View:
    def __init__(self, mapping: dict | str | "View"):
        if isinstance(mapping, View):
            mapping = mapping.document
        self._json = loads(mapping) if isinstance(mapping, str) else mapping
        if "name" not in self._json or "template" not in self._json:
            raise ValueError("a view is {'name': ..., 'template': ...}")
        self._text = dumps(self._json)

    @property
    def document(self) -> dict:
        return self._json

    @property
    def columns_read(self) -> list[str]:
        """Which columns this view needs — the projection a search should fetch."""
        return _rs.view_columns_read(self._text)

    def render(self, member_description: dict, columns: dict) -> dict:
        return loads(_rs.render_view(self._text, dumps(member_description), dumps(columns)))

    def __repr__(self) -> str:
        return f"<View {self._json['name']} reading {self.columns_read}>"


def stac_item_view(
    collection: str,
    id: dict,
    datetime: dict,
    bbox: dict | None = None,
    geometry: dict | None = None,
    properties: dict | None = None,
    assets: dict | None = None,
) -> View:
    """The STAC Item view, as an ordinary mapping document.

    `id` almost always reads an extra column: member ids are opaque 128-bit hashes,
    so a human-meaningful Item id has to come from somewhere else. `datetime` and
    `bbox` are usually extra columns too — derived at ingest for query convenience.
    """
    config = {"collection": collection, "id": id, "datetime": datetime}
    if bbox is not None:
        config["bbox"] = bbox
    if geometry is not None:
        config["geometry"] = geometry
    if properties:
        config["properties"] = properties
    if assets is not None:
        config["assets"] = assets
    return View(loads(_rs.stac_item_mapping(dumps(config))))


def stac_collection(collection_id: str, text: str, constraint) -> dict:
    """A STAC Collection derived from the constraint.

    The `summaries` fall straight out of the variable domains — a declared domain
    *is* a summary — so the collection-level description of what varies needs no
    separate authoring. Wildcards contribute nothing, correctly: a wildcard is a leaf
    we declined to describe.
    """
    doc = constraint.document if hasattr(constraint, "document") else constraint
    return loads(_rs.stac_collection(collection_id, text, dumps(doc)))


def stac_items(coll, view: View, member_ids: Iterable[str] | None = None) -> list[dict]:
    ids = list(member_ids) if member_ids is not None else coll.member_ids
    return [coll.render(view, member_id) for member_id in ids]
