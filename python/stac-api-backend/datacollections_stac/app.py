"""The routes. Thin on purpose — every interesting decision is in `backend.py`."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request

from .backend import DEFAULT_LIMIT, Backend


def make_app(backend: Backend, title: str = "DataCollections STAC API") -> FastAPI:
    app = FastAPI(title=title)

    def link(request: Request, rel: str, path: str, **extra) -> dict:
        return {"rel": rel, "type": "application/json", "href": str(request.base_url).rstrip("/") + path, **extra}

    @app.get("/")
    def landing(request: Request) -> dict:
        return {
            "type": "Catalog",
            "stac_version": "1.1.0",
            "id": backend.collection_id,
            "description": backend.title,
            "conformsTo": backend.conformance(),
            "links": [
                link(request, "self", "/"),
                link(request, "conformance", "/conformance"),
                link(request, "data", "/collections"),
                link(request, "search", "/search"),
            ],
        }

    @app.get("/conformance")
    def conformance() -> dict:
        return {"conformsTo": backend.conformance()}

    @app.get("/collections")
    def collections() -> dict:
        return {"collections": [backend.collection_document()], "links": []}

    @app.get("/collections/{collection_id}")
    def collection(collection_id: str) -> dict:
        _check(collection_id, backend)
        return backend.collection_document()

    @app.get("/collections/{collection_id}/items")
    def items(
        request: Request,
        collection_id: str,
        limit: int = Query(DEFAULT_LIMIT, ge=1),
        token: str | None = None,
        datetime: str | None = None,
        bbox: str | None = None,
    ) -> dict:
        _check(collection_id, backend)
        result = backend.search(
            limit=limit,
            token=token,
            datetime=datetime,
            bbox=_parse_bbox(bbox),
            collections=[collection_id],
        )
        return _feature_collection(request, result, "/collections/{}/items".format(collection_id))

    @app.get("/collections/{collection_id}/items/{item_id}")
    def item(collection_id: str, item_id: str) -> dict:
        _check(collection_id, backend)
        found = backend.item(item_id)
        if found is None:
            raise HTTPException(status_code=404, detail=f"no item `{item_id}`")
        return found

    @app.get("/search")
    def search_get(
        request: Request,
        limit: int = Query(DEFAULT_LIMIT, ge=1),
        token: str | None = None,
        ids: str | None = None,
        collections: str | None = None,
        datetime: str | None = None,
        bbox: str | None = None,
    ) -> dict:
        result = backend.search(
            ids=ids.split(",") if ids else None,
            collections=collections.split(",") if collections else None,
            datetime=datetime,
            bbox=_parse_bbox(bbox),
            limit=limit,
            token=token,
        )
        return _feature_collection(request, result, "/search")

    @app.post("/search")
    async def search_post(request: Request) -> dict:
        body: dict[str, Any] = await request.json()
        result = backend.search(
            ids=body.get("ids"),
            collections=body.get("collections"),
            datetime=body.get("datetime"),
            bbox=body.get("bbox"),
            limit=body.get("limit", DEFAULT_LIMIT),
            token=body.get("token"),
        )
        return _feature_collection(request, result, "/search")

    def _feature_collection(request: Request, result, path: str) -> dict:
        links = [link(request, "self", path)]
        if result.next_token:
            links.append(link(request, "next", f"{path}?token={result.next_token}"))
        return {
            "type": "FeatureCollection",
            "features": result.items,
            "numberMatched": result.matched,
            "numberReturned": len(result.items),
            "links": links,
        }

    return app


def _check(collection_id: str, backend: Backend) -> None:
    if collection_id != backend.collection_id:
        raise HTTPException(status_code=404, detail=f"no collection `{collection_id}`")


def _parse_bbox(raw: str | None):
    if not raw:
        return None
    return [float(v) for v in raw.split(",")]
