"""A STAC API over a DataCollections store.

STAC is **one view**, added last and deliberately thin: this package holds no
knowledge of the constraint language beyond calling `stac_item_view` and rendering
it. Deleting it would leave the rest of the stack working, which is the property the
factoring is meant to have.

The host is **stac-fastapi**, the reference implementation, as PLAN.md specifies.
That is what makes the conformance claims mean anything: the landing page, link
relations, request models, `/queryables`, error shapes and the OpenAPI document are
all its, and only the data is ours. `DataCollectionsClient` is the six-method client
class it calls; `Backend` underneath it does the searching and knows nothing about
HTTP, so it stays testable without a server.

    from datacollections_stac import Backend, make_app
    app = make_app(Backend(collection, view, collection_id="mastu-amc"))
"""

from .backend import Backend, SearchResult
from .client import DataCollectionsClient
from .app import make_app

__all__ = ["Backend", "SearchResult", "DataCollectionsClient", "make_app"]
