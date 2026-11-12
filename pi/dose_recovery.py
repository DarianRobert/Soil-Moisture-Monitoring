import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

RECOVERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS dose_reconstructions (
 group_key TEXT PRIMARY KEY, at TEXT NOT NULL,
 combined_run_id INTEGER NOT NULL UNIQUE,
 source_runs_json TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
 confirmation TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dose_run_sources (
 source_run_id INTEGER PRIMARY KEY, combined_run_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS dose_point_sources (
 combined_point_id INTEGER PRIMARY KEY, source_point_id INTEGER NOT NULL UNIQUE
);
"""


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def rows(db, sql, args=()):
    return [dict(row) for row in db.execute(sql, args)]


def load_manifest(path):
    manifest = json.loads(Path(path).read_text())
    if manifest.get("format") != 1:
        raise ValueError("Unsupported recovery manifest.")
    groups = manifest["confirmed_groups"]
    ids = [identity for group in groups for identity in group]
    if not groups or any(len(g) < 2 for g in groups) or len(ids) != len(set(ids)):
        raise ValueError("Recovery groups overlap or are incomplete.")
    return manifest


def build_plan(db, manifest):
    """Validate current source rows and ledger, returning an immutable plan."""
    if rows(db, "SELECT id FROM dose_runs WHERE status='open' LIMIT 1"):
        raise ValueError("Finish or end the open calibration run before recovery.")
    snapshots = {}
    for name, table in [
        ("profiles", "dose_profiles"),
        ("runs", "dose_runs"),
        ("points", "dose_points"),
    ]:
        snapshots[name] = {}
        for expected in manifest["expected"][name]:
            found = rows(db, f"SELECT * FROM {table} WHERE id=?", (expected["id"],))
            if not found:
                raise ValueError(
                    f"Missing original {name} record {expected['id']}. No recovery was applied."
                )
            row = found[0]
            source = {key: row[key] for key in expected["fields"]}
            if digest(source) != expected["sha256"]:
                raise ValueError(
                    f"Original {name} record {expected['id']} differs from the supplied evidence. No recovery was applied."
                )
            snapshots[name][row["id"]] = row
    plan = []
    for group in manifest["confirmed_groups"]:
        runs = [snapshots["runs"][identity] for identity in group]
        profile = snapshots["profiles"][runs[0]["profile_id"]]
        if any(
            r["profile_id"] != profile["id"]
            or r["kind"] != "training"
            or r["stream_id"] != runs[0]["stream_id"]
            or r["pending"]
            or r["status"] not in {"ended", "complete"}
            for r in runs
        ):
            raise ValueError(
                "The grouped runs have incompatible settings, recording streams or unfinished additions."
            )
        merged, skipped, ledger_ids = [], [], []
        offset, previous_last = 0.0, None
        for run in runs:
            points = sorted(
                (p for p in snapshots["points"].values() if p["run_id"] == run["id"]),
                key=lambda p: p["cumulative_ml"],
            )
            live_ids = [
                p["id"]
                for p in rows(
                    db,
                    "SELECT id FROM dose_points WHERE run_id=? ORDER BY cumulative_ml",
                    (run["id"],),
                )
            ]
            if live_ids != [p["id"] for p in points]:
                raise ValueError(
                    "Additional or missing source points were found. No recovery was applied."
                )
            if (
                len(points) < 2
                or points[0]["cumulative_ml"] != 0
                or abs(points[-1]["cumulative_ml"] - run["cumulative_ml"]) > 1e-9
            ):
                raise ValueError(
                    "A source run has incomplete cumulative-water records."
                )
            if previous_last and (
                points[0]["at"] < previous_last["at"]
                or abs(points[0]["index_value"] - previous_last["index_value"]) > 3
            ):
                raise ValueError(
                    "A restart baseline differs too much from its preceding capture to join automatically."
                )
            if not merged:
                merged.append({**points[0], "cumulative_ml": 0.0})
            else:
                skipped.append(points[0]["id"])
            for pos, (before, point) in enumerate(zip(points, points[1:])):
                amount = point["cumulative_ml"] - before["cumulative_ml"]
                units = amount / profile["resolution_ml"]
                if (
                    not math.isfinite(amount)
                    or amount <= 0
                    or amount > profile["max_portion_ml"] + 1e-9
                    or abs(units - round(units)) > 1e-6
                ):
                    raise ValueError(
                        "An addition does not match the original measuring settings."
                    )
                water_id = run["watermark_id"] - (len(points) - 2) + pos
                water = rows(
                    db, "SELECT id,volume_ml,note FROM watering WHERE id=?", (water_id,)
                )
                if (
                    not water
                    or abs(water[0]["volume_ml"] - amount) > 1e-9
                    or not water[0]["note"].startswith(
                        f"Water-amount training run #{run['id']}."
                    )
                ):
                    raise ValueError(
                        f"Water ledger entry {water_id} does not match run {run['id']}. No water is invented or entered again."
                    )
                ledger_ids.append(water_id)
                merged.append(
                    {**point, "cumulative_ml": offset + point["cumulative_ml"]}
                )
            previous_last = points[-1]
            offset += run["cumulative_ml"]
        if ledger_ids != list(range(ledger_ids[0], ledger_ids[-1] + 1)):
            raise ValueError(
                "Another recorded water entry occurred between the grouped additions."
            )
        if len(merged) < 4 or merged[-1]["index_value"] < profile["target_index"]:
            raise ValueError(
                "A combined trial lacks three additions or has not reached the target."
            )
        highest = merged[0]["index_value"]
        for point in merged:
            if (
                not math.isfinite(point["index_value"])
                or point["index_value"] < highest - 3 - 1e-9
            ):
                raise ValueError(
                    "A combined trial has a substantial falling response. Inspect it before fitting."
                )
            highest = max(highest, point["index_value"])
        plan.append(
            {
                "source_ids": group,
                "group_key": ",".join(map(str, group)),
                "points": merged,
                "repeated_baseline_point_ids": skipped,
                "first_run": runs[0],
                "last_run": runs[-1],
                "cumulative_ml": offset,
                "water_ids": ledger_ids,
            }
        )
    # All-or-nothing idempotency; source originals retain their exact values.
    tables = {
        r["name"] for r in rows(db, "SELECT name FROM sqlite_master WHERE type='table'")
    }
    existing = (
        rows(db, "SELECT * FROM dose_reconstructions")
        if "dose_reconstructions" in tables
        else []
    )
    matching = [r for r in existing if r["manifest_sha256"] == digest(manifest)]
    if matching:
        if {r["group_key"] for r in matching} != {p["group_key"] for p in plan}:
            raise ValueError(
                "The recovery audit is incomplete; no automatic changes were made."
            )
        for rec in matching:
            p = next(p for p in plan if p["group_key"] == rec["group_key"])
            actual_sources = rows(
                db,
                "SELECT source_run_id FROM dose_run_sources WHERE combined_run_id=? ORDER BY source_run_id",
                (rec["combined_run_id"],),
            )
            if [r["source_run_id"] for r in actual_sources] != p["source_ids"]:
                raise ValueError(
                    "The previous recovery links changed. No duplicate trials were created."
                )
            combined = rows(
                db, "SELECT * FROM dose_runs WHERE id=?", (rec["combined_run_id"],)
            )
            points = rows(
                db,
                "SELECT * FROM dose_points WHERE run_id=? ORDER BY cumulative_ml",
                (rec["combined_run_id"],),
            )
            if (
                not combined
                or combined[0]["status"] != "complete"
                or combined[0]["kind"] != "training"
                or combined[0]["profile_id"] != p["first_run"]["profile_id"]
                or combined[0]["stream_id"] != p["first_run"]["stream_id"]
                or combined[0]["watermark_id"] != p["last_run"]["watermark_id"]
                or abs(combined[0]["cumulative_ml"] - p["cumulative_ml"]) > 1e-9
                or len(points) != len(p["points"])
            ):
                raise ValueError(
                    "A previously reconstructed trial changed. No duplicate trials were created."
                )
            for actual, expected in zip(points, p["points"]):
                if any(
                    actual[k] != expected[k]
                    for k in (
                        "at",
                        "cumulative_ml",
                        "raw_median",
                        "index_value",
                        "spread_index",
                        "half_drift_index",
                        "reading_ids_json",
                    )
                ):
                    raise ValueError(
                        "A reconstructed point changed. No duplicate trials were created."
                    )
                links = rows(
                    db,
                    "SELECT source_point_id FROM dose_point_sources WHERE combined_point_id=?",
                    (actual["id"],),
                )
                if len(links) != 1 or links[0]["source_point_id"] != expected["id"]:
                    raise ValueError(
                        "A reconstructed point's provenance link changed. No duplicate trials were created."
                    )
            p["combined_run_id"] = rec["combined_run_id"]
        return plan, True
    if existing and any(
        r["group_key"] in {p["group_key"] for p in plan} for r in existing
    ):
        raise ValueError(
            "These groups were already reconstructed with a different manifest."
        )
    if "dose_run_sources" in tables:
        for p in plan:
            for identity in p["source_ids"]:
                if rows(
                    db,
                    "SELECT * FROM dose_run_sources WHERE source_run_id=?",
                    (identity,),
                ):
                    raise ValueError(
                        "A source run already belongs to another reconstruction."
                    )
    return plan, False


def preview(database, manifest_path):
    path = Path(database).expanduser().resolve()
    if not path.is_file():
        raise ValueError(
            "The original garden.sqlite3 was not found. Use the existing project folder."
        )
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        return build_plan(db, load_manifest(manifest_path))


def recover_database(database, manifest_path):
    """Caller must stop acquisition and make a consistent database backup first."""
    manifest = load_manifest(manifest_path)
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        plan, done = build_plan(db, manifest)
        if done:
            return plan, True
        for statement in RECOVERY_SCHEMA.split(";"):
            if statement.strip():
                db.execute(statement)
        at = (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        for p in plan:
            first, last = p["first_run"], p["last_run"]
            chain = "→".join(map(str, p["source_ids"]))
            cur = db.execute(
                """INSERT INTO dose_runs
                (at,profile_id,kind,status,stream_id,cumulative_ml,pending,watermark_id,operator,note)
                VALUES(?,?,'training','complete',?,?,0,?,?,?)""",
                (
                    first["at"],
                    first["profile_id"],
                    first["stream_id"],
                    p["cumulative_ml"],
                    last["watermark_id"],
                    first["operator"],
                    f"Reconstructed physical trial from software runs {chain}. Original records retained; no new watering.",
                ),
            )
            combined = cur.lastrowid
            p["combined_run_id"] = combined
            db.execute(
                "INSERT INTO dose_reconstructions VALUES(?,?,?,?,?,?)",
                (
                    p["group_key"],
                    at,
                    combined,
                    json.dumps(p["source_ids"]),
                    digest(manifest),
                    manifest["confirmation"],
                ),
            )
            for identity in p["source_ids"]:
                db.execute(
                    "INSERT INTO dose_run_sources VALUES(?,?)", (identity, combined)
                )
            for point in p["points"]:
                cur = db.execute(
                    """INSERT INTO dose_points
                    (run_id,at,cumulative_ml,raw_median,index_value,spread_index,half_drift_index,reading_ids_json)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        combined,
                        point["at"],
                        point["cumulative_ml"],
                        point["raw_median"],
                        point["index_value"],
                        point["spread_index"],
                        point["half_drift_index"],
                        point["reading_ids_json"],
                    ),
                )
                db.execute(
                    "INSERT INTO dose_point_sources VALUES(?,?)",
                    (cur.lastrowid, point["id"]),
                )
        return plan, False
