import json
import pathlib

import icechunk
import numpy as np
import pytest
import xarray as xr

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "spec" / "fixtures" / "constraints"


@pytest.fixture(scope="session")
def fixtures() -> list[dict]:
    """The shared conformance fixtures — the same files the Rust suite reads."""
    files = sorted(FIXTURES.glob("*.json"))
    assert files, "run `python scripts/make_fixtures.py`"
    return [json.loads(p.read_text()) for p in files]


@pytest.fixture(scope="session")
def meta_schema() -> dict:
    return json.loads((REPO_ROOT / "spec" / "meta-schema.json").read_text())


@pytest.fixture
def repo(tmp_path):
    return icechunk.Repository.create(icechunk.local_filesystem_storage(str(tmp_path / "store")))


def make_repo(path) -> icechunk.Repository:
    return icechunk.Repository.create(icechunk.local_filesystem_storage(str(path)))


def shot(nt: int, campaign: str = "M09", diagnostic: str = "amc") -> xr.Dataset:
    """A MAST-U-shaped member: (shot, diagnostic) as the referenced unit, so a
    missing diagnostic is a member that does not exist rather than an absent array."""
    return xr.Dataset(
        {"data": (("time", "channel"), np.zeros((nt, 8), "float32"))},
        coords={"time": ("time", np.arange(nt, dtype="float64"))},
        attrs={"diagnostic": diagnostic, "campaign": campaign},
    )
