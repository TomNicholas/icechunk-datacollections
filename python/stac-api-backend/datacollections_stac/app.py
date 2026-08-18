"""Assemble the stac-fastapi application.

There are no routes here, and that is the point: stac-fastapi owns the landing page,
the conformance declaration, link relations, request models, error shapes and the
OpenAPI document. We supply a client and the extensions we actually implement.
"""

from __future__ import annotations

from stac_fastapi.api.app import StacApi
from stac_fastapi.api.models import create_get_request_model, create_post_request_model
from stac_fastapi.extensions import TokenPaginationExtension
from stac_fastapi.types.config import ApiSettings

from .backend import Backend
from .client import DataCollectionsClient


def make_app(backend: Backend, title: str | None = None, version: str = "0.1.0"):
    """A STAC API serving one DataCollections store.

    Only the extensions we genuinely implement are declared. `TokenPagination` is
    real — the token is a row ordinal plus the Icechunk snapshot the search ran
    against, so a page boundary is stable while the collection is appended to
    underneath. Sort and Filter are *not* declared, because we do not implement
    them and claiming conformance we do not have is worse than lacking it.
    """
    settings = ApiSettings(
        stac_fastapi_title=title or backend.title,
        stac_fastapi_description=backend.title,
        stac_fastapi_version=version,
        stac_fastapi_landing_id=backend.collection_id,
        # Validate what we emit against stac-pydantic. This is the main reason to
        # host on the reference implementation rather than hand-rolled routes: it
        # immediately caught a `bbox` being served as a JSON *string*, which our own
        # routes had been passing through happily.
        enable_response_models=True,
    )
    extensions = [TokenPaginationExtension()]

    # The landing page's identity lives on the *client* (stac-fastapi's
    # LandingPageMixin), not in the settings — pystac-client reads `id` from there.
    client = DataCollectionsClient(
        backend=backend,
        landing_page_id=backend.collection_id,
        title=title or backend.title,
        description=backend.title,
    )

    return StacApi(
        settings=settings,
        client=client,
        extensions=extensions,
        search_get_request_model=create_get_request_model(extensions),
        search_post_request_model=create_post_request_model(extensions),
    ).app
