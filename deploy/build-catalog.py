#!/usr/bin/env python3
"""Build a Codex model catalog for the gateway from the installed Codex's bundled catalog.

Usage: python3 deploy/build-catalog.py OVERRIDES OUTPUT [--codex BINARY]

The bundled catalog of BINARY (default: codex) supplies the schema and the base entry, so the
output matches the Codex version that will read it. Generate with the same binary that reads
the catalog (the desktop app bundles its own). OUTPUT must not exist: personal files are never
overwritten. The result is checked by loading it back through `codex debug models`.
"""

import argparse
import copy
import json
import subprocess
import sys
from pathlib import Path


def fail(message: str) -> None:
    """Print a prefixed error message to stderr and exit with a non-zero status."""
    sys.exit(f"build-catalog: {message}")


def codex_models(codex: str, *args: str) -> list[dict]:
    """Return the model list printed by `codex debug models`, or stop on any malformed output."""
    try:
        out = subprocess.run([codex, "debug", "models", *args], capture_output=True, text=True, timeout=60, check=True).stdout
        models = json.loads(out)["models"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        fail(f"cannot read the catalog from {codex} debug models: {exc}")
    if not isinstance(models, list) or not all(isinstance(m, dict) and "slug" in m for m in models):
        fail("unexpected catalog schema: 'models' must be a list of objects with a slug")
    return models


def build(bundled: list[dict], spec: dict) -> dict:
    """Clone the base entry once per model and apply only keys that the base entry already has."""
    by_slug = {m["slug"]: m for m in bundled}
    base = by_slug.get(spec.get("base"))
    if base is None:
        fail(f"base model {spec.get('base')!r} is not in this Codex's bundled catalog; available: {', '.join(by_slug)}")
    entries = []
    for slug, fields in spec["models"].items():
        fields = dict(fields)
        changes = {**(spec.get("partner_defaults", {}) if fields.pop("partner", False) else {}), **fields, "slug": slug}
        unknown = sorted(set(changes) - set(base))
        if unknown:
            fail(f"{slug}: keys not in this Codex's catalog schema: {', '.join(unknown)}")
        entries.append({**copy.deepcopy(base), **changes})
    return {"models": entries}


def main() -> None:
    """Parse CLI arguments, build the catalog JSON, write it, and verify it with Codex."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("overrides", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--codex", default="codex", help="Codex binary whose bundled catalog is the base")
    args = parser.parse_args()
    if args.output.exists():
        fail(f"{args.output} exists; choose a new path (existing files are never overwritten)")
    catalog = build(codex_models(args.codex, "--bundled"), json.loads(args.overrides.read_text(encoding="utf-8")))
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(catalog, handle, indent=2)
    loaded = [m["slug"] for m in codex_models(args.codex, "-c", f"model_catalog_json={json.dumps(str(args.output.resolve()))}")]
    expected = [m["slug"] for m in catalog["models"]]
    if loaded != expected:
        fail(f"Codex did not load the new catalog (got {loaded}); {args.output} was kept for inspection")
    print(f"wrote {args.output}: {', '.join(expected)}")


if __name__ == "__main__":
    main()
