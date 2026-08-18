"""Cache real, publicly-published Zarr metadata documents to test the language on.

PLAN.md asks for `meet` to be checked against "a few hundred real OME-Zarr and
Sentinel-2 `zarr.json` documents pulled from public stores". This fetches such a
corpus and caches it in `spec/fixtures/real/`, where a Rust test runs the three laws
over every document. It needs no DataCollections store, which is exactly why
`json-constraint` has no Zarr dependency.

Two sources, both anonymous HTTP:

- **MAST-U** — `s3://mast/level1/shots/<shot>.zarr/<source>/<signal>/{.zarray,.zattrs}`,
  hundreds of real array-metadata documents with real float attributes. It was one
  of these that would have caught the one-ULP float bug had the corpus existed
  first.
- **IDR OME-Zarr** — the image-data-resource public bucket, whose `.zattrs` carry
  the full `multiscales` / `omero` vocabulary. The richest attribute documents of
  the four domains, and the best test of "constrain the position, do not interpret
  the content".

These are Zarr **v2** documents, and that is fine: the constraint language is a
language over JSON. Using them makes the point that nothing in `json-constraint` is
Zarr-version-specific, let alone Zarr-specific.

    python scripts/fetch_real_documents.py --limit 200
"""

from __future__ import annotations

import argparse
import json
import pathlib
import urllib.error
import urllib.request

OUT = pathlib.Path(__file__).resolve().parents[1] / "spec" / "fixtures" / "real"
MAST_API = "https://mastapp.site/json/signals"
MAST_S3 = "https://s3.echo.stfc.ac.uk/mast/level1/shots"
IDR = "https://uk1s3.embassy.ebi.ac.uk/idr/zarr/v0.4"
IDR_IMAGES = [
    "idr0062A/6001240.zarr",
    "idr0079A/9836839.zarr",
    "idr0094A/9846151.zarr",
    "idr0101A/13457537.zarr",
]


def get_json(url: str, timeout: int = 30):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def mast_documents(limit: int) -> list[dict]:
    signals = get_json(f"{MAST_API}?per_page=200", timeout=60)
    signals = signals["items"] if isinstance(signals, dict) and "items" in signals else signals
    out = []
    for s in signals:
        if len(out) >= limit:
            break
        base = f"{MAST_S3}/{s['shot_id']}.zarr/{s['source']}/{s['name']}"
        for suffix, kind in ((".zarray", "array"), (".zattrs", "attributes")):
            try:
                doc = get_json(f"{base}{suffix}", timeout=20)
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
                continue
            if doc:
                out.append({"source": "mastu", "kind": kind, "url": f"{base}{suffix}", "document": doc})
    return out


def idr_documents(limit: int) -> list[dict]:
    out = []
    for image in IDR_IMAGES:
        if len(out) >= limit:
            break
        for path, kind in ((".zattrs", "attributes"), ("0/.zarray", "array")):
            url = f"{IDR}/{image}/{path}"
            try:
                doc = get_json(url, timeout=30)
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
                print(f"  skipped {url} ({e})")
                continue
            out.append({"source": "ome-zarr", "kind": kind, "url": url, "document": doc})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200, help="documents per source")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    corpus = []
    for name, fetch in (("MAST-U", mast_documents), ("IDR OME-Zarr", idr_documents)):
        print(f"fetching {name}…")
        try:
            got = fetch(args.limit)
        except Exception as e:
            print(f"  {name} unreachable: {e}")
            got = []
        print(f"  {len(got)} documents")
        corpus += got

    path = OUT / "documents.json"
    path.write_text(json.dumps(corpus, indent=1) + "\n")
    print(f"\nwrote {len(corpus)} documents to {path} ({path.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
