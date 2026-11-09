"""Build a bounded integration-only release ZIP; never include household or tool data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "custom_components" / "solar_ev_planner"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default="")
    args = parser.parse_args()
    manifest = json.loads((SOURCE / "manifest.json").read_text())
    if args.tag and args.tag.removeprefix("v") != manifest["version"]:
        parser.error("release tag must match the integration manifest version")
    integrations = [p.name for p in SOURCE.parent.iterdir() if (p / "manifest.json").is_file()]
    if integrations != ["solar_ev_planner"]:
        parser.error("the repository must contain exactly one installable integration")
    output = ROOT / "dist" / "solar_ev_planner.zip"
    output.parent.mkdir(exist_ok=True)
    allowed = {".py", ".json", ".yaml", ".js", ".png", ".woff2"}
    files = sorted(
        p
        for p in SOURCE.rglob("*")
        if p.is_file()
        and not p.is_symlink()
        and p.suffix in allowed
        and "__pycache__" not in p.parts
    )
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(SOURCE).as_posix())
    with ZipFile(output) as archive:
        assert archive.testzip() is None
        required = {"manifest.json", "__init__.py", "rivian/__init__.py", "www/solarsage-logo.png"}
        assert required.issubset(archive.namelist())
    print(f"Built {output.name}: {len(files)} files, version {manifest['version']}")


if __name__ == "__main__":
    main()
