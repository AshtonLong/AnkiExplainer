"""Zip ``addon/`` into ``anki_explainer.ankiaddon``.

The archive has the add-on files at its root (as AnkiWeb and "Install from
file" expect), with ``manifest.json``. Caches, local config (``meta.json``)
and anything in ``user_files/`` except its README are excluded.

    python scripts/build_addon.py [--out dist/anki_explainer.ankiaddon]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDON = os.path.join(ROOT, "addon")
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache"}
EXCLUDE_FILES = {"meta.json", ".DS_Store", "Thumbs.db"}


def included(rel: str) -> bool:
    parts = rel.replace("\\", "/").split("/")
    if any(p in EXCLUDE_DIRS for p in parts):
        return False
    if parts[-1] in EXCLUDE_FILES or parts[-1].endswith((".pyc", ".pyo")):
        return False
    if parts[0] == "user_files" and parts[-1] != "README.txt":
        return False
    return True


def build(out: str) -> list[str]:
    with open(os.path.join(ADDON, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    assert manifest.get("package") and manifest.get("min_point_version"), "manifest incomplete"
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    names: list[str] = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(ADDON):
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, ADDON).replace("\\", "/")
                if included(rel):
                    zf.write(full, rel)
                    names.append(rel)
    return names


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "dist", "anki_explainer.ankiaddon"))
    args = ap.parse_args()
    names = build(args.out)
    size = os.path.getsize(args.out)
    print(f"Wrote {args.out} ({len(names)} files, {size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
