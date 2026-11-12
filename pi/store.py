import csv
import io
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DEFAULT_SETTINGS = {
    "crop": "Potted plant / soil sample",
    "soil_notes": "Enter the actual soil and container details.",
    "low_index": 30.0,
    "clear_index": 45.0,
    "thresholds_confirmed": False,
    "trial_step_ml": None,
}

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS settings (
  id INTEGER PRIMARY KEY CHECK(id=1), value_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS captures (
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL
    CHECK(kind IN ('dry','wet')), raw REAL NOT NULL,
  spread INTEGER NOT NULL, sd REAL NOT NULL, samples_json TEXT NOT NULL,
  note TEXT NOT NULL, source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calibrations (
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, dry_raw REAL NOT NULL,
  wet_raw REAL NOT NULL CHECK(wet_raw>dry_raw),
  dry_capture_id INTEGER REFERENCES captures(id),
  wet_capture_id INTEGER REFERENCES captures(id),
  active INTEGER NOT NULL CHECK(active IN (0,1)),
  source TEXT NOT NULL, note TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_calibration
  ON calibrations(active) WHERE active=1;
CREATE TABLE IF NOT EXISTS readings (
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, stream_id TEXT NOT NULL,
  seq INTEGER NOT NULL, uptime_ms INTEGER NOT NULL,
  raw INTEGER NOT NULL CHECK(raw BETWEEN 0 AND 1023),
  burst_min INTEGER NOT NULL, burst_max INTEGER NOT NULL,
  calibration_id INTEGER REFERENCES calibrations(id),
  moisture_index REAL, index_unclamped REAL,
  state TEXT NOT NULL, flags TEXT NOT NULL, source TEXT NOT NULL,
  UNIQUE(stream_id,seq)
);
CREATE INDEX IF NOT EXISTS readings_at ON readings(at);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, type TEXT NOT NULL,
  message TEXT NOT NULL, source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watering (
  id INTEGER PRIMARY KEY, at TEXT NOT NULL,
  volume_ml REAL NOT NULL CHECK(volume_ml>0 AND volume_ml<=10000),
  operator TEXT NOT NULL, note TEXT NOT NULL,
  reading_id INTEGER REFERENCES readings(id),
  raw_before INTEGER, index_before REAL,
  calibration_id INTEGER REFERENCES calibrations(id), source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, note TEXT NOT NULL,
  operator TEXT NOT NULL, source TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            db.execute(
                "INSERT OR IGNORE INTO settings VALUES(1,?)",
                (json.dumps(DEFAULT_SETTINGS),),
            )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def rows(self, query, args=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute(query, args)]

    def insert(self, table, values):
        allowed = {"captures", "readings", "events", "watering", "observations"}
        if table not in allowed:
            raise ValueError("Unsupported table")
        keys = list(values)
        with self.connect() as db:
            cursor = db.execute(
                f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
                [values[k] for k in keys],
            )
            return cursor.lastrowid

    def settings(self):
        return json.loads(
            self.rows("SELECT value_json FROM settings WHERE id=1")[0]["value_json"]
        )

    def save_settings(self, value):
        with self.connect() as db:
            db.execute(
                "UPDATE settings SET value_json=? WHERE id=1", (json.dumps(value),)
            )

    def calibration(self):
        rows = self.rows("SELECT * FROM calibrations WHERE active=1")
        return rows[0] if rows else None

    def activate_calibration(self, at, dry, wet, source, note, settings):
        # Calibration and threshold invalidation must commit together.
        with self.connect() as db:
            db.execute("UPDATE calibrations SET active=0 WHERE active=1")
            cursor = db.execute(
                """INSERT INTO calibrations
                (at,dry_raw,wet_raw,dry_capture_id,wet_capture_id,active,source,note)
                VALUES(?,?,?,?,?,1,?,?)""",
                (at, dry["raw"], wet["raw"], dry["id"], wet["id"], source, note),
            )
            db.execute(
                "UPDATE settings SET value_json=? WHERE id=1", (json.dumps(settings),)
            )
            return cursor.lastrowid

    def latest_captures(self):
        return {
            kind: next(
                iter(
                    self.rows(
                        "SELECT * FROM captures WHERE kind=? ORDER BY id DESC LIMIT 1",
                        (kind,),
                    )
                ),
                None,
            )
            for kind in ("dry", "wet")
        }

    def history(self, limit=720):
        return list(
            reversed(
                self.rows("SELECT * FROM readings ORDER BY id DESC LIMIT ?", (limit,))
            )
        )

    def summary(self):
        with self.connect() as db:
            return {
                "reading_count": db.execute("SELECT count(*) FROM readings").fetchone()[
                    0
                ],
                "water_total_ml": db.execute(
                    "SELECT coalesce(sum(volume_ml),0) FROM watering"
                ).fetchone()[0],
                "water_event_count": db.execute(
                    "SELECT count(*) FROM watering"
                ).fetchone()[0],
            }

    def export_csv(self, table):
        if table not in {
            "readings",
            "watering",
            "events",
            "observations",
            "calibrations",
            "captures",
        }:
            raise ValueError("Unknown export")
        with self.connect() as db:
            cur = db.execute(f"SELECT * FROM {table} ORDER BY id")
            stream = io.StringIO()
            writer = csv.writer(stream)
            writer.writerow([d[0] for d in cur.description])
            yield stream.getvalue()
            for row in cur:
                stream.seek(0)
                stream.truncate(0)
                # Keep user notes from becoming formulas when opened in a spreadsheet.
                safe = [
                    "'" + v
                    if isinstance(v, str)
                    and v.lstrip().startswith(("=", "+", "-", "@"))
                    else v
                    for v in row
                ]
                writer.writerow(safe)
                yield stream.getvalue()
