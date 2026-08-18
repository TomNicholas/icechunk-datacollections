# Everything needs the venv's interpreter, and pyo3 needs it by absolute path.
PY := $(CURDIR)/.venv/bin/python
export PYO3_PYTHON = $(PY)

.PHONY: venv build test test-rust test-python fixtures examples fmt clean

venv:
	uv venv --python 3.11 .venv
	VIRTUAL_ENV=$(CURDIR)/.venv uv pip install -e "python/datacollections-py[query,stac,stac-test,pandera,virtual,dev]" \
		maturin httpx

build:
	cd python/datacollections-py && env -u CONDA_PREFIX VIRTUAL_ENV=$(CURDIR)/.venv $(CURDIR)/.venv/bin/maturin develop

test: test-rust test-python

test-rust:
	env -u VIRTUAL_ENV cargo test

test-python: build
	$(PY) -m pytest python/datacollections-py/tests python/stac-api-backend/tests examples -q

fixtures:
	$(PY) scripts/make_fixtures.py

examples:
	$(PY) examples/ome_zarr/run.py -n 40
	$(PY) examples/sentinel2_stac/run.py -n 30
	$(PY) examples/mastu/run.py -n 40
	$(PY) examples/hst/run.py -n 30

fmt:
	cargo fmt --all
	cargo clippy --workspace --all-targets -- -D warnings

clean:
	cargo clean
	rm -rf examples/*/store
