"""A `stac-fastapi` backend over a DataCollections store.

This is the client class stac-fastapi calls: six methods, each one a thin adapter
onto [`Backend`](./backend.py), which is where the searching actually happens.
Keeping the two apart is deliberate — `Backend` knows about collections, views and
pagination and nothing about HTTP, so it stays testable without a server and
reusable if the host framework ever changes again.

Using stac-fastapi rather than hand-rolled routes is what makes the conformance
claims mean something: the landing page, link relations, request models,
`/queryables`, error shapes and OpenAPI docs are the reference implementation's,
not ours. All we supply is the data.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import attr
from stac_fastapi.types import stac
from stac_fastapi.types.core import BaseCoreClient
from stac_fastapi.types.search import BaseSearchPostRequest

from .backend import DEFAULT_LIMIT, Backend


@attr.s
class DataCollectionsClient(BaseCoreClient):
    """Serves one cohort as one STAC Collection.

    v1 stores are single-cohort, so that is 1:1 by construction. When cohorts arrive
    it becomes one Collection per cohort, and only `all_collections` and the
    collection lookup change.
    """

    backend: Backend = attr.ib(default=None)

    # ------------------------------------------------------------- collections

    def all_collections(self, **kwargs) -> stac.Collections:
        request = kwargs.get("request")
        collection = self._collection(request)
        return stac.Collections(
            collections=[collection],
            links=[
                self._link(request, "root", "/"),
                self._link(request, "self", "/collections"),
            ],
        )

    def get_collection(self, collection_id: str, **kwargs) -> stac.Collection:
        self._check_collection(collection_id)
        return self._collection(kwargs.get("request"))

    def _collection(self, request) -> stac.Collection:
        document = self.backend.collection_document()
        document["links"] = [
            self._link(request, "root", "/"),
            self._link(request, "self", f"/collections/{self.backend.collection_id}"),
            self._link(request, "items", f"/collections/{self.backend.collection_id}/items"),
        ]
        return stac.Collection(**document)

    # -------------------------------------------------------------------- items

    def get_item(self, item_id: str, collection_id: str, **kwargs) -> stac.Item:
        from stac_fastapi.types.errors import NotFoundError

        self._check_collection(collection_id)
        found = self.backend.item(item_id)
        if found is None:
            raise NotFoundError(f"no item `{item_id}` in collection `{collection_id}`")
        return stac.Item(**self._with_links(found, kwargs.get("request")))

    def item_collection(
        self,
        collection_id: str,
        bbox=None,
        datetime: str | None = None,
        limit: int = DEFAULT_LIMIT,
        token: str | None = None,
        **kwargs,
    ) -> stac.ItemCollection:
        self._check_collection(collection_id)
        result = self.backend.search(
            collections=[collection_id],
            bbox=bbox,
            datetime=datetime,
            limit=limit,
            token=token,
        )
        # An OGC-Features Items page must carry a `collection` link at the
        # FeatureCollection level, not only on each Item. Caught by stac-fastapi's
        # response validation; the hand-rolled routes this replaced did not know it.
        return self._feature_collection(
            result,
            kwargs.get("request"),
            f"/collections/{collection_id}/items",
            extra_links=[
                self._link(kwargs.get("request"), "collection", f"/collections/{collection_id}"),
                self._link(kwargs.get("request"), "parent", f"/collections/{collection_id}"),
            ],
        )

    # ------------------------------------------------------------------ search

    def get_search(
        self,
        collections: list[str] | None = None,
        ids: list[str] | None = None,
        bbox=None,
        intersects: Any = None,
        datetime: str | None = None,
        limit: int | None = DEFAULT_LIMIT,
        **kwargs,
    ) -> stac.ItemCollection:
        result = self.backend.search(
            ids=ids,
            collections=collections,
            bbox=bbox or self._bbox_of(intersects),
            datetime=datetime,
            limit=limit or DEFAULT_LIMIT,
            token=kwargs.get("token"),
        )
        return self._feature_collection(result, kwargs.get("request"), "/search")

    def post_search(self, search_request: BaseSearchPostRequest, **kwargs) -> stac.ItemCollection:
        body = search_request.model_dump(exclude_none=True)
        result = self.backend.search(
            ids=body.get("ids"),
            collections=body.get("collections"),
            bbox=body.get("bbox") or self._bbox_of(body.get("intersects")),
            datetime=self._datetime_of(body.get("datetime")),
            limit=body.get("limit") or DEFAULT_LIMIT,
            token=body.get("token"),
        )
        return self._feature_collection(result, kwargs.get("request"), "/search")

    # ------------------------------------------------------------------ helpers

    def _bbox_of(self, intersects: Any):
        """`intersects` is answered from the geometry's envelope.

        Declared honestly rather than quietly: with only a bbox column there is no
        true geometry to test against, so `intersects` is bbox-approximate. That is
        the open conformance question PLAN.md flags for M4, and the
        `#query`-style claim is only made when a bbox column exists.
        """
        if intersects is None:
            return None
        geometry = intersects if isinstance(intersects, dict) else intersects.model_dump()
        coordinates: list[float] = []

        def walk(node):
            if isinstance(node, (int, float)):
                coordinates.append(float(node))
            elif isinstance(node, (list, tuple)):
                for child in node:
                    walk(child)

        walk(geometry.get("coordinates", []))
        xs, ys = coordinates[0::2], coordinates[1::2]
        return [min(xs), min(ys), max(xs), max(ys)] if xs and ys else None

    def _datetime_of(self, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        # stac-pydantic hands back an interval of datetimes
        start, end = (value if isinstance(value, (list, tuple)) else (value, value))[:2]
        fmt = lambda d: d.isoformat().replace("+00:00", "Z") if d else ".."  # noqa: E731
        return f"{fmt(start)}/{fmt(end)}"

    def _check_collection(self, collection_id: str) -> None:
        from stac_fastapi.types.errors import NotFoundError

        if collection_id != self.backend.collection_id:
            raise NotFoundError(f"no collection `{collection_id}`")

    def _feature_collection(
        self, result, request, path: str, extra_links: list[dict] | None = None
    ) -> stac.ItemCollection:
        links = [self._link(request, "root", "/"), self._link(request, "self", path)]
        links += extra_links or []
        if result.next_token:
            links.append(self._link(request, "next", f"{path}?token={result.next_token}"))
        return stac.ItemCollection(
            type="FeatureCollection",
            features=[stac.Item(**self._with_links(i, request)) for i in result.items],
            links=links,
            numberMatched=result.matched,
            numberReturned=len(result.items),
        )

    def _with_links(self, item: dict, request) -> dict:
        collection = item.get("collection", self.backend.collection_id)
        item = dict(item)
        item["links"] = [
            self._link(request, "root", "/"),
            self._link(request, "collection", f"/collections/{collection}"),
            self._link(request, "self", f"/collections/{collection}/items/{item['id']}"),
        ]
        return item

    def _link(self, request, rel: str, path: str) -> dict:
        base = str(request.base_url) if request is not None else "/"
        return {"rel": rel, "type": "application/json", "href": urljoin(base, path.lstrip("/"))}
