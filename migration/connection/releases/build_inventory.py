"""Build the minimal deterministic inventory zipapp without pip dependencies."""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path


def build(destination: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "inventory"
    files = {
        "migration_connection/__init__.py": b"",
        "__main__.py": b"from migration_connection.inventory.__main__ import main\nraise SystemExit(main())\n",
    }
    for name in (
        "__init__.py",
        "__main__.py",
        "cloud.py",
        "collector.py",
        "observation.py",
        "schema.py",
    ):
        files["migration_connection/inventory/" + name] = (source / name).read_bytes()
    with (
        destination.open("xb") as output,
        zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive,
    ):
        for name, contents in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100600 << 16
            archive.writestr(info, contents)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    build(parser.parse_args().output)
