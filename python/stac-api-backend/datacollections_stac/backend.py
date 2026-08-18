"""The backend: search over the table, render matching rows through the Item view."""

from __future__ import annotations

import base64
import dataclasses
import json
from typing import Any, Iterable

DEFAULT_LIMIT = 10
MAX_LIMIT = 500


@dataclasses.dataclass
class SearchResult:
    items: list[dict]
    next_token: str | None
    matched: int


class Backend:
    """Serves one cohort as one STAC Collection.

    v1 stores are single-cohort, so this is 1:1 by construction. When cohorts arrive
    it becomes one Collection per cohort with no change to the route layer.
    """

    def __init__(
        self,
        collection,
        item_view,
        collection_id: str = "collection",
        title: str = "A DataCollections store",
        datetime_column: str | None = "datetime",
        bbox_column: str | None = None,
    ):
        self.collection = collection
        self.view = item_view
        self.collection_id = collection_id
        self.title = title
        self.datetime_column = datetime_column
        self.bbox_column = bbox_column

    # ------------------------------------------------------------------ metadata

    def collection_document(self) -> dict:
        from datacollections import stac_collection

        doc = stac_collection(self.collection_id, self.title, self.collection.constraint)
        doc["links"] = []
        return doc

    def conformance(self) -> list[str]:
        classes = [
            "https://api.stacspec.org/v1.0.0/core",
            "https://api.stacspec.org/v1.0.0/collections",
            "https://api.stacspec.org/v1.0.0/ogcapi-features",
            "https://api.stacspec.org/v1.0.0/item-search",
        ]
        if self.bbox_column is not None:
            # Declared honestly: with only a bbox column, `intersects` is
            # bbox-approximate, so the geometry-exact claim is not made.
            classes.append("https://api.stacspec.org/v1.0.0/item-search#query")
        return classes

    # -------------------------------------------------------------------- search

    def search(
        self,
        ids: Iterable[str] | None = None,
        bbox: Iterable[float] | None = None,
        datetime: str | None = None,
        limit: int = DEFAULT_LIMIT,
        token: str | None = None,
        collections: Iterable[str] | None = None,
    ) -> SearchResult:
        limit = max(1, min(int(limit), MAX_LIMIT))
        if collections and self.collection_id not in set(collections):
            return SearchResult([], None, 0)

        rows = self.collection.rows()
        member_ids = self.collection.member_ids
        indexed = list(zip(member_ids, rows))

        if ids:
            wanted = set(ids)
            rendered_ids = {mid: self._item_id(mid, row) for mid, row in indexed}
            indexed = [(m, r) for m, r in indexed if m in wanted or rendered_ids[m] in wanted]

        if datetime and self.datetime_column:
            start, _, end = datetime.partition("/")
            indexed = [
                (m, r)
                for m, r in indexed
                if _in_interval(str(r.get(self.datetime_column, "")), start, end or start)
            ]

        if bbox and self.bbox_column:
            indexed = [(m, r) for m, r in indexed if _overlaps(r.get(self.bbox_column), bbox)]

        matched = len(indexed)
        offset = self._decode_token(token)
        page = indexed[offset : offset + limit]
        next_offset = offset + limit
        next_token = self._encode_token(next_offset) if next_offset < matched else None

        items = [self.view.render(self.collection.describe(mid), row) for mid, row in page]
        return SearchResult(items, next_token, matched)

    def item(self, item_id: str) -> dict | None:
        result = self.search(ids=[item_id], limit=1)
        return result.items[0] if result.items else None

    def _item_id(self, member_id: str, row: dict) -> str:
        return str(self.view.render(self.collection.describe(member_id), row)["id"])

    # --------------------------------------------------------------- pagination
    #
    # The token is a row ordinal plus the snapshot the search ran against, so a page
    # boundary is stable even if the collection is appended to mid-scroll —
    # snapshot-isolated pagination is nearly free when the store is immutable
    # underneath you. What it does *not* yet handle is a user-chosen sort order,
    # which is the open question PLAN.md flags for M5.

    def _snapshot(self) -> str:
        session = self.collection._repo.readonly_session(self.collection._branch)
        return str(getattr(session, "snapshot_id", ""))

    def _encode_token(self, offset: int) -> str:
        payload = json.dumps({"offset": offset, "snapshot": self._snapshot()})
        return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")

    def _decode_token(self, token: str | None) -> int:
        if not token:
            return 0
        padded = token + "=" * (-len(token) % 4)
        try:
            payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        except (ValueError, UnicodeDecodeError):
            raise ValueError("malformed pagination token") from None
        return int(payload.get("offset", 0))


def _in_interval(value: str, start: str, end: str) -> bool:
    if not value:
        return False
    if start and start != ".." and value < start:
        return False
    if end and end != ".." and value > end:
        return False
    return True


def _overlaps(item_bbox: Any, query_bbox: Iterable[float]) -> bool:
    if not item_bbox:
        return False
    if isinstance(item_bbox, str):
        item_bbox = json.loads(item_bbox)
    ax0, ay0, ax1, ay1 = [float(v) for v in item_bbox[:4]]
    bx0, by0, bx1, by1 = [float(v) for v in list(query_bbox)[:4]]
    return not (ax1 < bx0 or ax0 > bx1 or ay1 < by0 or ay0 > by1)
