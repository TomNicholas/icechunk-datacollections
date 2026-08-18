"""A STAC API over a DataCollections store.

STAC is **one view**, added last and deliberately thin: this package holds no
knowledge of the constraint language beyond calling `stac_item_view` and rendering
it. Deleting it would leave the rest of the stack working, which is the property the
factoring is meant to have.

**Scope note.** PLAN.md names `stac-fastapi` as the host. This MVP implements the
core and item-search endpoints on plain FastAPI instead — the same routes and the
same response shapes, a tenth of the dependency surface, and the interesting part
(pagination over an immutable snapshot, search pushed into DataFusion) is identical.
Swapping in stac-fastapi later means reusing `Backend` as its client class.

    from datacollections_stac import Backend, make_app
    app = make_app(Backend(collection, view, collection_id="mastu-amc"))
"""

from .backend import Backend, SearchResult
from .app import make_app

__all__ = ["Backend", "SearchResult", "make_app"]
