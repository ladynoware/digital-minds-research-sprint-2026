#!/usr/bin/env python3
"""Stamp every CSS and JS reference with a hash of the file it points at.

GitHub Pages serves assets with a cache header that lets browsers hold on to
them, so an edited stylesheet can keep serving from cache long after it was
deployed — you reload, nothing changes, and the deploy looks broken when it is
not. That happened repeatedly while building this.

The fix is a version string in the URL. Keyed to a hash of the file rather than
to a number someone has to remember to bump, so it changes exactly when the
file changes and never otherwise:

    <link rel="stylesheet" href="assets/site.css?v=8f2c1a9d" />

Run it after editing anything in assets/ and before committing:

    python site/stamp_assets.py

Idempotent — running it twice in a row rewrites nothing. The JSON data files
need no stamp: they are fetched with `cache: "no-store"` already.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

SITE_DIR = Path(__file__).resolve().parent

# href="assets/x.css"  /  src="assets/x.js"  — with or without an existing ?v=
REFERENCE = re.compile(r'((?:href|src)=")(assets/[\w.-]+\.(?:css|js))(?:\?v=[0-9a-f]+)?(")')


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:8]


def main() -> int:
    cache: dict[str, str] = {}
    missing: list[str] = []
    changed: list[str] = []

    def replace(match: re.Match[str]) -> str:
        prefix, rel, suffix = match.groups()
        if rel not in cache:
            target = SITE_DIR / rel
            if not target.exists():
                missing.append(rel)
                cache[rel] = ""
            else:
                cache[rel] = digest(target)
        stamp = cache[rel]
        return f"{prefix}{rel}{'?v=' + stamp if stamp else ''}{suffix}"

    for page in sorted(SITE_DIR.glob("*.html")):
        original = page.read_text(encoding="utf-8")
        updated = REFERENCE.sub(replace, original)
        if updated != original:
            page.write_text(updated, encoding="utf-8")
            changed.append(page.name)

    for name in changed:
        print(f"  stamped {name}")
    if not changed:
        print("  already up to date")
    if missing:
        # A reference to a file that is not there is a broken page, not a
        # caching problem — say so rather than silently leaving it unstamped.
        print(f"\n  WARNING: referenced but not found: {', '.join(sorted(set(missing)))}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
