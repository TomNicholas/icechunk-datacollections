"""DataCollections — queryable, self-describing collections of Zarr groups.

    import icechunk, xarray as xr
    from datacollections import create_collection, Constraint, var, wild

    repo = icechunk.Repository.create(icechunk.local_filesystem_storage("store"))
    coll = create_collection(repo, constraint=None)     # first member sets it
    member_id = coll.add_item(ds)                       # one atomic transaction

    coll.describe(member_id)     # the member's zarr.json, reconstructed exactly
    coll.sql("SELECT member_id, nt FROM members WHERE nt > 1000")

The store holds a metadata table **and** the groups it describes, in one Icechunk
repository, so a member and its row commit together. That same-store coupling is the
thesis; STAC is one optional view over the result, not the storage format.
"""

from ._datacollections import SPEC_VERSION, __version__
from .collection import (
    Collection,
    EvolveReport,
    ExtraColumn,
    create_collection,
    open_collection,
)
from .constraint import Constraint, ConstraintError, Mismatch, like, literal, var, wild
from .description import of_group as description_of_group
from .description import predicted as predicted_description
from .views import View, column, description, stac_collection, stac_item_view, stac_items

__all__ = [
    "SPEC_VERSION",
    "__version__",
    # authoring
    "Constraint",
    "ConstraintError",
    "Mismatch",
    "var",
    "wild",
    "literal",
    "like",
    # storage
    "create_collection",
    "open_collection",
    "Collection",
    "ExtraColumn",
    "EvolveReport",
    "description_of_group",
    "predicted_description",
    # views
    "View",
    "column",
    "description",
    "stac_item_view",
    "stac_collection",
    "stac_items",
]


def __getattr__(name):
    # pandera is an optional dependency, so its translation loads on first use.
    if name in ("constraint_to_pandera", "constraint_from_pandera", "constraint_to_pandera_dict"):
        from . import pandera_view

        return getattr(pandera_view, name)
    raise AttributeError(name)
