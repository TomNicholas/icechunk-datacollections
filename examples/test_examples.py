"""Run every example, small and offline.

Breadth across domains is the milestone that actually proves the factoring, so it
gets a test rather than a "it worked when I ran it". Each example runs against
recorded metadata so the suite needs no network; running the scripts directly hits
the live APIs.
"""

import contextlib
import importlib.util
import io
import pathlib
import sys

import pytest

EXAMPLES = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(EXAMPLES))


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"example_{name}", EXAMPLES / name / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(name: str, tmp_path, members: int = 4, extra: list[str] | None = None) -> str:
    module = load(name)
    argv = [
        name,
        "-n",
        str(members),
        "--store",
        str(tmp_path / name),
        "--offline",
        *(extra or []),
    ]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        old, sys.argv = sys.argv, argv
        try:
            module.main()
        finally:
            sys.argv = old
    return out.getvalue()


@pytest.mark.parametrize("name", ["ome_zarr", "sentinel2_stac", "mastu", "hst"])
def test_example_runs_and_stays_consistent(name, tmp_path):
    output = run(name, tmp_path)
    assert "consistent" in output or "verify" not in output
    assert "Traceback" not in output


def test_the_cap_on_members_is_enforced(tmp_path):
    """Going beyond ~100 groups per example needs the Icechunk node-count
    investigation first, so the examples refuse rather than discovering the limits
    by accident."""
    with pytest.raises(SystemExit):
        run("ome_zarr", tmp_path, members=101)


def test_three_of_the_four_examples_never_mention_stac():
    """The forcing function: if the core could not express the non-geospatial cases
    without STAC vocabulary somewhere in the stack, the factoring would be wrong."""
    import ast

    for name in ["ome_zarr", "mastu", "hst"]:
        tree = ast.parse((EXAMPLES / name / "run.py").read_text())
        used = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id.startswith("stac")
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if alias.name.startswith("stac")
        }
        assert not used, f"{name} reaches for STAC: {used}"
