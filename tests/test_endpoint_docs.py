#!/usr/bin/env python3
"""Fail when a frontend API path is absent from the public API contract docs."""
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "apps" / "ashat-hub-web" / "src"
DOCS = ROOT / "docs" / "python-api-contracts.md"

# These are finite values selected by the frontend before interpolation.
DYNAMIC_VALUES = {
    "mode": ("login", "register"),
    "action": ("refresh", "clear"),
}


def frontend_paths():
    paths = set()
    for source in SRC.rglob("*.ts"):
        paths.update(extract(source.read_text(encoding="utf-8")))
    for source in SRC.rglob("*.tsx"):
        paths.update(extract(source.read_text(encoding="utf-8")))
    return paths


def extract(text):
    # This intentionally scans both quoted strings and template literals. It
    # catches direct fetches as well as request(`${API}/...`) calls.
    found = set()
    normalized = text.replace("${API}", "/api")
    normalized = re.sub(r"\$\{encodeURIComponent\([^}]+\)\}", ":id", normalized)
    normalized = normalized.replace("${search}", "")
    for match in re.finditer(r"/api/[A-Za-z0-9_./${}:-]+", normalized):
        path = match.group(0).split("?", 1)[0].rstrip("/")
        path = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", r":\1", path)
        found.update(expand(path))
    return found


def expand(path):
    dynamic = re.search(r":([A-Za-z_][A-Za-z0-9_]*)", path)
    if not dynamic:
        return {path}
    name = dynamic.group(1)
    values = DYNAMIC_VALUES.get(name)
    if not values:
        return {path}
    return {path.replace(f":{name}", value, 1) for value in values}


def documented_paths():
    text = DOCS.read_text(encoding="utf-8")
    paths = set()
    for match in re.finditer(r"`(/api/[^`?\s]+)", text):
        path = match.group(1).rstrip("/")
        paths.add(path)
    # OIDC endpoints are documented relative to the issuer section.
    oidc = re.search(r"## OIDC issuer(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    if oidc:
        for endpoint in re.findall(r"`(GET|POST) ([^`]+)`", oidc.group(1)):
            paths.add("/api/oauth/" + endpoint[1].lstrip("/"))
    return paths


def matches(path, documented):
    if path in documented:
        return True
    # Route parameter names are not contractually significant.
    pattern = re.compile(r"^" + re.sub(r":([^/]+)", r"[^/]+", path) + r"$")
    return any(pattern.match(candidate) for candidate in documented)


def main():
    frontend = frontend_paths()
    documented = documented_paths()
    missing = sorted(path for path in frontend if not matches(path, documented))
    if missing:
        print("Frontend API paths missing from docs/python-api-contracts.md:")
        for path in missing:
            print(f"  - {path}")
        return 1
    print(f"frontend endpoint documentation check: PASS ({len(frontend)} paths)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
