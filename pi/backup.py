#!/usr/bin/env python3
"""Create a consistent database backup without copying dashboard credentials."""

import argparse
import json
import sqlite3
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory


def create_backup(base, simulate=False):
    base = Path(base).resolve()
    mode = "demo" if simulate else "garden"
    source = base / "data" / f"{mode}.sqlite3"
    if not source.is_file():
        raise FileNotFoundError("No database yet. Start the application first.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = base / "backups" / f"{mode}_{stamp}.zip"
    destination.parent.mkdir(parents=True, exist_ok=True)

    with TemporaryDirectory(dir=destination.parent) as temp:
        snapshot = Path(temp) / source.name
        # SQLite's backup API includes committed WAL data without stopping collection.
        with closing(
            sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        ) as original:
            with closing(sqlite3.connect(snapshot)) as copied:
                original.backup(copied)
        pending = Path(temp) / destination.name
        with zipfile.ZipFile(pending, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(snapshot, snapshot.name)
            config = base / "config.json"
            if config.is_file():
                archive.write(config, "config.json")
            archive.writestr(
                "BACKUP_INFO.json",
                json.dumps(
                    {
                        "created_utc": stamp,
                        "mode": mode,
                        "note": "Consistent SQLite snapshot. Dashboard password excluded.",
                    },
                    indent=2,
                ),
            )
        # Publish only a complete archive, on the same filesystem as its temporary file.
        pending.replace(destination)
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulate", action="store_true")
    args = parser.parse_args(argv)
    try:
        destination = create_backup(Path(__file__).resolve().parent, args.simulate)
    except (OSError, sqlite3.Error) as exc:
        parser.exit(1, f"Backup failed: {exc}\n")
    print(destination)


if __name__ == "__main__":
    main()
