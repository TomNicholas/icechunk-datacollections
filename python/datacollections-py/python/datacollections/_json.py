"""JSON helpers.

The Rust core is JSON-in/JSON-out, so everything crossing that boundary passes
through here. Zarr metadata dicts contain numpy scalars and non-finite floats,
neither of which `json` handles, and both of which have a canonical Zarr v3 JSON
spelling — so the conversion is normative rather than cosmetic.
"""

from __future__ import annotations

import json
import math
from typing import Any


def jsonable(value: Any) -> Any:
    """Convert a Zarr metadata value to plain JSON.

    Non-finite floats take their Zarr v3 spelling (`"NaN"`, `"Infinity"`,
    `"-Infinity"`), which is also how they appear in a `zarr.json` on disk — so a
    description built here compares equal to one read back.
    """
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, (int, str)):
        return value
    # numpy scalars, np.dtype, and anything else that knows how to be a Python object
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return jsonable(item())
        except (ValueError, TypeError):
            pass
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        return jsonable(tolist())
    return str(value)


def dumps(value: Any) -> str:
    return json.dumps(jsonable(value), allow_nan=False)


def loads(text: str) -> Any:
    return json.loads(text)
