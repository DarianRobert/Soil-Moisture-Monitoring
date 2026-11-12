import csv
import hashlib
import io
import json
import math
import statistics

from flask import Response, jsonify

from core import number, relative_index, text_field, utc_now
from dose_recovery import RECOVERY_SCHEMA
from dose_error_policy import POLICY_SCHEMA, review_for
from dose_cycle import METHOD_SCHEMA, WaterCycle, baseline_amount_ranges

MIN_TRAINING_RUNS = 3
MIN_TRAINING_POINTS = 4
WINDOW_SECONDS = 30
MIN_WINDOW_SPAN = 25
MAX_GAP_SECONDS = 10
MAX_SPREAD_INDEX = 3.0
MAX_HALF_DRIFT_INDEX = 0.75
MIN_POINT_RISE = 2.0
MAX_RESPONSE_FALL = 3.0
MODEL_VERSION = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS dose_profiles (
 id INTEGER PRIMARY KEY, at TEXT NOT NULL, name TEXT NOT NULL,
 setup_json TEXT NOT NULL, fingerprint TEXT NOT NULL, source TEXT NOT NULL,
 resolution_ml REAL NOT NULL, max_portion_ml REAL NOT NULL,
 allowed_error_ml REAL NOT NULL, target_index REAL NOT NULL,
 low_index REAL NOT NULL, calibration_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS dose_selection (
 id INTEGER PRIMARY KEY CHECK(id=1), profile_id INTEGER
);
INSERT OR IGNORE INTO dose_selection VALUES(1,NULL);
CREATE TABLE IF NOT EXISTS dose_runs (
 id INTEGER PRIMARY KEY, at TEXT NOT NULL, profile_id INTEGER NOT NULL,
 kind TEXT NOT NULL, status TEXT NOT NULL, stream_id TEXT NOT NULL,
 cumulative_ml REAL NOT NULL DEFAULT 0, pending INTEGER NOT NULL DEFAULT 0,
 watermark_id INTEGER NOT NULL, predicted_ml REAL, observed_ml REAL,
 absolute_error_ml REAL, passed INTEGER, model_signature TEXT,
 operator TEXT NOT NULL, note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS dose_points (
 id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, at TEXT NOT NULL,
 cumulative_ml REAL NOT NULL, raw_median REAL NOT NULL,
 index_value REAL NOT NULL, spread_index REAL NOT NULL,
 half_drift_index REAL NOT NULL, reading_ids_json TEXT NOT NULL,
 UNIQUE(run_id,cumulative_ml)
);
CREATE INDEX IF NOT EXISTS dose_points_run ON dose_points(run_id);
CREATE TABLE IF NOT EXISTS dose_check_context (
 run_id INTEGER PRIMARY KEY, at TEXT NOT NULL, detail_json TEXT NOT NULL
);
"""
SCHEMA += RECOVERY_SCHEMA
SCHEMA += POLICY_SCHEMA
SCHEMA += METHOD_SCHEMA


def water_bounds(points, index):

    if len(points) < 2 or not math.isfinite(index):
        raise ValueError("Not enough valid points for a water estimate.")
    if any(not math.isfinite(x) or not math.isfinite(v) for x, v in points):
        raise ValueError("The water-response curve contains invalid values.")
    if any(
        x1 < x0 - 1e-9 or v1 <= v0 for (x0, v0), (x1, v1) in zip(points, points[1:])
    ):
        raise ValueError("The water-response curve is not increasing.")
    if index < points[0][0] - 1e-9 or index > points[-1][0] + 1e-9:
        raise ValueError("Reading is outside the measured water-calibration range.")
    index = min(points[-1][0], max(points[0][0], index))
    candidates = []
    for (x0, v0), (x1, v1) in zip(points, points[1:]):
        if abs(x1 - x0) <= 1e-9:
            if abs(index - x0) <= 1e-9:
                candidates.extend([v0, v1])
        elif x0 - 1e-9 <= index <= x1 + 1e-9:
            fraction = min(1.0, max(0.0, (index - x0) / (x1 - x0)))
            candidates.append(v0 + fraction * (v1 - v0))
    if not candidates:
        raise ValueError("Reading is outside the measured water-calibration range.")
    return min(candidates), max(candidates)


def interpolate(points, index):
    low, high = water_bounds(points, index)
    if high - low > 1e-9:
        raise ValueError("This flat response does not identify one water amount.")
    return (low + high) / 2


def covered_amount_ranges(curves, current, target):
    if not math.isfinite(current) or not math.isfinite(target):
        raise ValueError("Invalid moisture reading or target.")
    if current >= target:
        return [(i, 0.0, 0.0) for i in range(len(curves))]
    amounts = []
    for i, curve in enumerate(curves):
        try:
            target_low, target_high = water_bounds(curve, target)
            current_low, current_high = water_bounds(curve, current)
            amounts.append(
                (
                    i,
                    max(0.0, target_low - current_high),
                    max(0.0, target_high - current_low),
                )
            )
        except ValueError:
            continue
    if len(amounts) < MIN_TRAINING_RUNS:
        raise ValueError(
            "Fewer than three training runs cover this reading and the target. No amount is extrapolated."
        )
    return amounts


def curve_amount_ranges(curves, current, target):
    return [
        (low, high) for _, low, high in covered_amount_ranges(curves, current, target)
    ]


def curve_amounts(curves, current, target):
    """Candidate midpoints. DoseService also checks their full interval spread."""
    return [
        (low + high) / 2 for low, high in curve_amount_ranges(curves, current, target)
    ]


def next_portion(estimated, resolution, maximum, tested_maximum):
    if estimated < resolution - 1e-9:
        return 0.0
    limit = min(maximum, tested_maximum, estimated)
    half = max(resolution, estimated / 2)
    units = math.floor((min(half, limit) + 1e-9) / resolution)
    return round(units * resolution, 6)


def response_problem(points):
    highest = None
    for point in points:
        value = point["index_value"]
        if not math.isfinite(value):
            return "The response contains an invalid reading. End this run and check the sensor."
        if highest is not None and value < highest - MAX_RESPONSE_FALL - 1e-9:
            return "The settled response fell substantially after watering. End this run and inspect the soil, probe and watering method."
        highest = value if highest is None else max(highest, value)
    return None


def fitted_curve(points):
    issue = response_problem(points)
    if issue:
        raise ValueError(issue)
    result = []
    highest = None
    for point in points:
        value = point["index_value"]
        highest = value if highest is None else max(highest, value)
        result.append((highest, point["cumulative_ml"]))
    return result


class DoseService:
    def __init__(self, engine):
        self.e = engine
        self.confirmed = None  # Deliberately reset after restart/reconnection.
        with self.e.store.connect() as db:
            db.executescript(SCHEMA)
        self.cycles = WaterCycle(self)

    def rows(self, sql, args=()):
        return self.e.store.rows(sql, args)

    def selected(self):
        rows = self.rows(
            "SELECT p.* FROM dose_profiles p JOIN dose_selection s ON s.profile_id=p.id WHERE s.id=1"
        )
        return rows[0] if rows else None

    def setup(self):
        e = self.e
        c = e.calibration or {}
        return {
            "calibration_id": c.get("id"),
            "dry_raw": c.get("dry_raw"),
            "wet_raw": c.get("wet_raw"),
            "crop": e.settings["crop"],
            "soil_notes": e.settings["soil_notes"],
            "low_index": e.settings["low_index"],
            "clear_index": e.settings["clear_index"],
            "soak_seconds": e.config["soak_seconds"],
            "confirmation_samples": e.config["confirmation_samples"],
            "source": e.source,
        }

    def fingerprint(self):
        return json.dumps(self.setup(), sort_keys=True, separators=(",", ":"))

    def compatible(self, p):
        return p and p["fingerprint"] == self.fingerprint()

    def require_profile(self):
        p = self.selected()
        if not self.compatible(p):
            raise ValueError(
                "Select a matching water profile or create a new one for these settings and soil."
            )
        return p

    def latest_water(self):
        r = self.rows("SELECT id FROM watering ORDER BY id DESC LIMIT 1")
        return r[0]["id"] if r else 0

    def open_run(self):
        r = self.rows(
            "SELECT * FROM dose_runs WHERE status='open' ORDER BY id DESC LIMIT 1"
        )
        return r[0] if r else None

    def points(self, identity):
        return self.rows(
            "SELECT * FROM dose_points WHERE run_id=? ORDER BY cumulative_ml",
            (identity,),
        )

    def run_problem(self, r, p):
        if not self.compatible(p) or r["profile_id"] != p["id"]:
            return "The settings or selected profile changed. End this run and start a new matching run."
        if r["stream_id"] != self.e.stream_id or not self.e.connected:
            return "Recording was interrupted. End this run and repeat it with a fresh soil sample."
        if r["watermark_id"] != self.latest_water():
            return "Another water entry was recorded outside this run. End the run; its water history cannot be fitted safely."
        return None

    def stable(self):
        e = self.e
        if hasattr(self, "cycles") and e.clock() < self.cycles.ready_at:
            raise ValueError("Wait for the new soil sample reading to settle.")
        e.tick()
        if not e.fresh() or not e.last or not e.calibration:
            raise ValueError("Wait for fresh calibrated readings from the sensor.")
        if e.state in {
            "CHECK_SENSOR",
            "UNCALIBRATED",
            "SET_THRESHOLDS",
            "STALE_DATA",
            "NO_DATA",
        }:
            raise ValueError(
                "Resolve the sensor or calibration message before estimating water."
            )
        now = e.clock()
        samples = [(t, raw) for t, raw in e.recent if 0 <= now - t <= WINDOW_SECONDS]
        if len(samples) < 6 or samples[-1][0] - samples[0][0] < MIN_WINDOW_SPAN:
            raise ValueError(
                "Keep the probe still and wait for about 30 seconds of continuous readings."
            )
        if any(b[0] - a[0] > MAX_GAP_SECONDS for a, b in zip(samples, samples[1:])):
            raise ValueError(
                "A gap interrupted the stable-reading window. Wait for continuous readings."
            )
        records = list(
            reversed(
                self.rows(
                    "SELECT id,raw,flags,calibration_id FROM readings WHERE stream_id=? ORDER BY id DESC LIMIT ?",
                    (e.stream_id, len(samples)),
                )
            )
        )
        if len(records) != len(samples) or [r["raw"] for r in records] != [
            raw for _, raw in samples
        ]:
            raise ValueError("The reading window changed. Wait for the next update.")
        if any(
            r["flags"] or r["calibration_id"] != e.calibration["id"] for r in records
        ):
            raise ValueError(
                "The recent readings include a sensor or range warning. Check the setup and wait."
            )
        indexes = [relative_index(raw, e.calibration)[1] for _, raw in samples]
        half = len(indexes) // 2
        spread = max(indexes) - min(indexes)
        drift = abs(
            statistics.median(indexes[:half]) - statistics.median(indexes[half:])
        )
        if spread > MAX_SPREAD_INDEX or drift > MAX_HALF_DRIFT_INDEX:
            raise ValueError(
                "The reading is still changing. Wait longer without adding more water."
            )
        return {
            "raw_median": statistics.median(raw for _, raw in samples),
            "index_value": statistics.median(indexes),
            "spread_index": spread,
            "half_drift_index": drift,
            "reading_ids_json": json.dumps([r["id"] for r in records]),
        }

    def create_profile(self, data):
        if self.open_run():
            raise ValueError(
                "Finish or end the open run before creating another profile."
            )
        if not self.e.calibration or not self.e.settings["thresholds_confirmed"]:
            raise ValueError(
                "First activate the ordinary dry/wet calibration and confirm the warning thresholds."
            )
        resolution = number(
            data.get("resolution_ml"), "Measuring graduation (mL)", 0.1, 1000
        )
        maximum = number(
            data.get("max_portion_ml"), "Largest portion (mL)", resolution, 10000
        )
        error = number(
            data.get("allowed_error_ml"),
            "Allowed check error (mL)",
            resolution,
            maximum,
        )
        setup = {
            **self.setup(),
            "soil_quantity": text_field(
                data.get("soil_quantity"), "Soil amount", 160, True
            ),
            "container": text_field(data.get("container"), "Container", 160, True),
            "depth_cm": number(data.get("depth_cm"), "Probe depth (cm)", 0.1, 20),
            "measuring_tool": text_field(
                data.get("measuring_tool"), "Measuring tool", 160, True
            ),
        }
        with self.e.store.connect() as db:
            cur = db.execute(
                """INSERT INTO dose_profiles
                (at,name,setup_json,fingerprint,source,resolution_ml,max_portion_ml,
                 allowed_error_ml,target_index,low_index,calibration_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    utc_now(),
                    text_field(data.get("name"), "Profile name", 120, True),
                    json.dumps(setup, sort_keys=True),
                    self.fingerprint(),
                    self.e.source,
                    resolution,
                    maximum,
                    error,
                    self.e.settings["clear_index"],
                    self.e.settings["low_index"],
                    self.e.calibration["id"],
                ),
            )
            identity = cur.lastrowid
            db.execute("UPDATE dose_selection SET profile_id=? WHERE id=1", (identity,))
        self.confirmed = None
        self.e.event("DOSE_PROFILE", f"Created water-amount profile #{identity}.")
        return identity

    def select(self, data):
        if self.open_run():
            raise ValueError("Finish or end the open run before switching profiles.")
        identity = number(data.get("profile_id"), "Profile", 1, 1e12)
        if not identity.is_integer():
            raise ValueError("Choose a whole profile ID.")
        identity = int(identity)
        if not self.rows("SELECT id FROM dose_profiles WHERE id=?", (identity,)):
            raise ValueError("Unknown profile.")
        with self.e.store.connect() as db:
            db.execute("UPDATE dose_selection SET profile_id=? WHERE id=1", (identity,))
        self.confirmed = None

    def confirm(self, data):
        p = self.require_profile()
        if data.get("same_setup") is not True:
            raise ValueError(
                "Confirm the same soil amount, container, water, probe position and measuring method."
            )
        if not self.e.connected:
            raise ValueError("Connect the sensor before confirming this setup.")
        self.confirmed = (p["id"], self.e.stream_id)
        cycle = self.cycles.open()
        if (
            cycle
            and cycle["profile_id"] == p["id"]
            and cycle["model_signature"] == self.model(p)["signature"]
        ):
            with self.e.store.connect() as db:
                db.execute(
                    "UPDATE dose_watering_cycles SET stream_id=? WHERE id=?",
                    (self.e.stream_id, cycle["id"]),
                )

    def model(self, p):
        runs = self.rows(
            """SELECT r.* FROM dose_runs r
            WHERE r.profile_id=? AND r.kind='training' AND r.status='complete'
            AND NOT EXISTS (SELECT 1 FROM dose_run_sources s WHERE s.source_run_id=r.id)
            ORDER BY r.id""",
            (p["id"],),
        )
        curves = []
        largest_steps = []
        for r in runs:
            points = self.points(r["id"])
            curves.append(fitted_curve(points))
            largest_steps.append(
                max(
                    b["cumulative_ml"] - a["cumulative_ml"]
                    for a, b in zip(points, points[1:])
                )
            )
        signature = hashlib.sha256(
            json.dumps(
                {
                    "version": MODEL_VERSION,
                    "profile": p,
                    "runs": [r["id"] for r in runs],
                    "curves": curves,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return {
            "curves": curves,
            "run_ids": [r["id"] for r in runs],
            "signature": signature,
            "runs": len(runs),
            "tested_maximum_ml": min(largest_steps) if largest_steps else 0,
        }

    def amounts(self, p, model, current):
        return self.amount_details(p, model, current)["amounts"]

    def amount_details(self, p, model, current):
        details = self.candidate_details(p, model, current)
        if not details["agreement_ok"]:
            low, high = details["range_ml"]
            raise ValueError(
                f"Training estimates at index {current:.1f} range from {low:.1f} to {high:.1f} mL. Their spread is {details['spread_ml']:.1f} mL; the limit is {details['spread_limit_ml']:.1f} mL. Normal quantity advice is unavailable here. A separate check run can still measure the prediction error."
            )
        return details

    def candidate_details(self, p, model, current):
        """Freeze an experimental prediction without declaring it reliable."""
        ranges = baseline_amount_ranges(
            model["curves"], current, p["target_index"], MIN_TRAINING_RUNS
        )
        low = min(a for _, a, _ in ranges)
        high = max(b for _, _, b in ranges)
        amounts = [(a + b) / 2 for _, a, b in ranges]
        run_ids = model.get("run_ids", [None] * len(model["curves"]))
        limit = 2 * p["allowed_error_ml"]
        return {
            "method": "baseline_aligned_index_rise",
            "model_version": MODEL_VERSION,
            "current_index": current,
            "target_index": p["target_index"],
            "amounts": amounts,
            "predicted_ml": statistics.median(amounts),
            "range_ml": [low, high],
            "spread_ml": high - low,
            "spread_limit_ml": limit,
            "agreement_ok": high - low <= limit + 1e-9,
            "allowed_error_ml": p["allowed_error_ml"],
            "training_runs_used": len(ranges),
            "candidates": [
                {"run_id": run_ids[i], "low_ml": a, "high_ml": b} for i, a, b in ranges
            ],
        }

    def check_context(self, identity):
        rows = self.rows(
            "SELECT detail_json FROM dose_check_context WHERE run_id=?", (identity,)
        )
        return json.loads(rows[0]["detail_json"]) if rows else None

    def begin(self, data):
        p = self.require_profile()
        if self.open_run():
            raise ValueError("A run is already open.")
        if data.get("same_setup") is not True:
            raise ValueError("Confirm that this fresh soil sample matches the profile.")
        kind = data.get("kind")
        if kind not in {"training", "validation"}:
            raise ValueError("Choose a training run or a check run.")
        if self.e.clock() < self.e.soak_until:
            raise ValueError("Wait until the current soak pause ends.")
        point = self.stable()
        if point["index_value"] > p["low_index"]:
            raise ValueError(
                "Start a run with a representative dry soil sample below the warning limit. Keep the electronics and probe contact correct."
            )
        predicted = signature = check_details = None
        if kind == "validation":
            model = self.model(p)
            check_details = self.candidate_details(p, model, point["index_value"])
            predicted, signature = check_details["predicted_ml"], model["signature"]
        operator = text_field(data.get("operator"), "Your initials", 80, True)
        with self.e.store.connect() as db:
            db.execute(
                "UPDATE dose_watering_cycles SET status='calibration_started',end_water_id=?,closed_at=?,note=? WHERE status='open'",
                (
                    self.latest_water(),
                    utc_now(),
                    "Operator started a fresh calibration run.",
                ),
            )
            cur = db.execute(
                """INSERT INTO dose_runs
                (at,profile_id,kind,status,stream_id,watermark_id,predicted_ml,model_signature,operator)
                VALUES(?,?,?,'open',?,?,?,?,?)""",
                (
                    utc_now(),
                    p["id"],
                    kind,
                    self.e.stream_id,
                    self.latest_water(),
                    predicted,
                    signature,
                    operator,
                ),
            )
            identity = cur.lastrowid
            self._save_point(db, identity, 0, point)
            if check_details is not None:
                db.execute(
                    "INSERT INTO dose_check_context VALUES(?,?,?)",
                    (identity, utc_now(), json.dumps(check_details, sort_keys=True)),
                )
        self.confirmed = None
        self.e.event(
            "DOSE_RUN", f"Started {kind} run #{identity}; fresh sample confirmed."
        )
        return identity

    def _save_point(self, db, run_id, volume, point):
        db.execute(
            """INSERT INTO dose_points
            (run_id,at,cumulative_ml,raw_median,index_value,spread_index,half_drift_index,reading_ids_json)
            VALUES(?,?,?,?,?,?,?,?)""",
            (
                run_id,
                utc_now(),
                volume,
                point["raw_median"],
                point["index_value"],
                point["spread_index"],
                point["half_drift_index"],
                point["reading_ids_json"],
            ),
        )

    def require_run(self):
        p = self.require_profile()
        r = self.open_run()
        if not r:
            raise ValueError("Start a run first.")
        issue = self.run_problem(r, p)
        if issue:
            raise ValueError(issue)
        return p, r

    def add_water(self, data):
        p, r = self.require_run()
        issue = response_problem(self.points(r["id"]))
        if issue:
            raise ValueError(issue)
        if r["pending"]:
            raise ValueError(
                "That addition is already recorded. Wait and capture its settled reading before adding more."
            )
        if self.points(r["id"])[-1]["index_value"] >= p["target_index"]:
            raise ValueError(
                "The last captured reading reached the target. Finish this run without adding more water."
            )
        if data.get("already_applied") is not True:
            raise ValueError(
                "Measure and add the water by hand, then confirm that it was applied."
            )
        amount = number(
            data.get("volume_ml"),
            "Actual addition (mL)",
            p["resolution_ml"],
            p["max_portion_ml"],
        )
        units = amount / p["resolution_ml"]
        if abs(units - round(units)) > 1e-6:
            raise ValueError(
                f"Use multiples of {p['resolution_ml']:g} mL, matching your measuring tool's graduation."
            )
        note = text_field(data.get("note", ""), "Watering note", 300)
        # Use the existing ledger and soak implementation. If interrupted between
        # these commits, the watermark check prevents further fitting of this run.
        self.e.water(
            {
                "volume_ml": amount,
                "operator": r["operator"],
                "already_applied": True,
                "note": f"Water-amount {r['kind']} run #{r['id']}. {note}",
            }
        )
        watermark = self.latest_water()
        with self.e.store.connect() as db:
            db.execute(
                "UPDATE dose_runs SET pending=1,cumulative_ml=cumulative_ml+?,watermark_id=? WHERE id=?",
                (amount, watermark, r["id"]),
            )
        return watermark

    def capture(self, data):
        p, r = self.require_run()
        if not r["pending"]:
            raise ValueError("The baseline or last addition has already been captured.")
        if self.e.clock() < self.e.soak_until:
            raise ValueError(
                "Wait until the soak pause finishes, then wait longer if the reading is still moving."
            )
        if data.get("setup_unchanged") is not True:
            raise ValueError(
                "Confirm no probe movement, unrecorded water, puddling or drainage during this step. Otherwise end the run."
            )
        point = self.stable()
        with self.e.store.connect() as db:
            self._save_point(db, r["id"], r["cumulative_ml"], point)
            db.execute("UPDATE dose_runs SET pending=0 WHERE id=?", (r["id"],))
        points = self.points(r["id"])
        little_change = (
            len(points) > 1
            and points[-1]["index_value"] - points[-2]["index_value"] < MIN_POINT_RISE
        )
        return {
            "index_value": point["index_value"],
            "excluded_reason": response_problem(points),
            "notice": (
                "Water and reading saved. The index changed very little. Wait longer and inspect the soil. If another small addition is needed, record it in THIS SAME RUN; the original baseline and all water remain saved."
                if little_change
                else None
            ),
        }

    def finish(self, data):
        p, r = self.require_run()
        points = self.points(r["id"])
        if r["pending"]:
            raise ValueError(
                "Capture the settled reading after the last addition first."
            )
        issue = response_problem(points)
        if not issue:
            minimum = MIN_TRAINING_POINTS if r["kind"] == "training" else 2
            if len(points) < minimum:
                raise ValueError(
                    "A training run needs a baseline and at least three additions. If you reached the target sooner, end this run and use smaller portions in the next fresh-soil run."
                )
            if points[-1]["index_value"] < p["target_index"]:
                raise ValueError(
                    "The target has not yet been reached. Continue only if the soil needs more water and there is no puddling or drainage."
                )
        observed = error = passed = None
        if not issue and r["kind"] == "validation":
            if r["model_signature"] != self.model(p)["signature"]:
                raise ValueError(
                    "The training model changed. Use a new independent check run."
                )
            curve = fitted_curve(points)
            observed = interpolate(curve, p["target_index"])
            error = abs(r["predicted_ml"] - observed)
            passed = int(error <= p["allowed_error_ml"] + 1e-9)
        with self.e.store.connect() as db:
            db.execute(
                """UPDATE dose_runs SET status=?,observed_ml=?,absolute_error_ml=?,passed=?,note=? WHERE id=?""",
                (
                    "rejected" if issue else "complete",
                    observed,
                    error,
                    passed,
                    issue or "",
                    r["id"],
                ),
            )
        self.e.event(
            "DOSE_RUN",
            f"Finished {r['kind']} run #{r['id']}; " + (issue or "record retained."),
        )
        return {
            "excluded_reason": issue,
            "predicted_ml": r["predicted_ml"],
            "observed_ml": observed,
            "absolute_error_ml": error,
            "check_passed": None if passed is None else bool(passed),
            "training_agreement_ok": (self.check_context(r["id"]) or {}).get(
                "agreement_ok"
            ),
        }

    def end_run(self, data):
        r = self.open_run()
        if not r:
            raise ValueError("No run is open.")
        reason = text_field(data.get("reason"), "Reason for ending this run", 500, True)
        with self.e.store.connect() as db:
            db.execute(
                "UPDATE dose_runs SET status='ended',note=? WHERE id=?",
                (reason, r["id"]),
            )
        self.confirmed = None
        self.e.event("DOSE_RUN", f"Ended run #{r['id']}: {reason}")

    def checked_model(self, p, model, validation):
        review = review_for(self, p, model, validation) if validation else None
        return bool(
            validation
            and (
                (
                    validation["model_signature"] == model["signature"]
                    and validation["passed"]
                )
                or (review and review["accepted"])
            )
        )

    def recommendation(self, p, model, validation, current, cycle=None):
        result = {
            "available": False,
            "estimated_ml": None,
            "next_ml": None,
            "range_ml": None,
            "target_index": p["target_index"] if p else None,
            "experimental": not bool(p and self.checked_model(p, model, validation)),
        }

        def wait(message, **values):
            return {**result, **values, "message": message}

        if not p:
            return wait("Create a water profile.")
        if not self.compatible(p):
            return wait("Select a profile matching the current setup.")
        if self.open_run():
            return wait("Use the active calibration run to record water and readings.")
        if model["runs"] < MIN_TRAINING_RUNS:
            return wait("Complete at least three training trials.")
        if cycle is None:
            cycle = self.cycles.snapshot(p, model, current)
        if cycle and cycle["problem"]:
            return wait(cycle["problem"])
        if cycle and cycle["target_reached"]:
            return {
                **result,
                "available": True,
                "estimated_ml": 0.0,
                "next_ml": 0.0,
                "message": "Target reached. Stop watering.",
            }
        remaining = cycle["remaining_ml"] if cycle else None
        interval = cycle["remaining_range_ml"] if cycle else None
        if not self.e.fresh() or self.e.state not in {
            "WATER_NEEDED",
            "OBSERVE",
            "CHECKING_DRY",
            "WAIT_SOAK",
        }:
            return wait("Wait for a healthy sensor reading.")
        if self.e.state == "WAIT_SOAK":
            seconds = max(0, math.ceil(self.e.soak_until - self.e.clock()))
            return wait(
                f"Wait for water to spread: {seconds} seconds remaining.",
                estimated_ml=remaining,
                range_ml=interval,
            )
        if not current:
            return wait(
                "Wait for the reading to settle.",
                estimated_ml=remaining,
                range_ml=interval,
            )
        if (
            self.e.state != "WATER_NEEDED"
            or current["index_value"] >= p["target_index"]
        ):
            return {
                **result,
                "available": True,
                "estimated_ml": 0.0,
                "next_ml": 0.0,
                "message": "No water is suggested now.",
            }
        if not cycle:
            if current["index_value"] > p["low_index"]:
                return wait("Start the next cycle from a settled dry-soil reading.")
            try:
                details = self.amount_details(p, model, current["index_value"])
            except ValueError as exc:
                return wait(str(exc))
            if self.confirmed != (p["id"], self.e.stream_id):
                return wait(
                    "Confirm the soil setup below.",
                    estimated_ml=details["predicted_ml"],
                    range_ml=details["range_ml"],
                    training_runs_used=details["training_runs_used"],
                    preview=True,
                )
            self.cycles.start({"fresh_start": True})
            cycle = self.cycles.snapshot(p, model, current)
            remaining = cycle["remaining_ml"]
            interval = cycle["remaining_range_ml"]
        if remaining <= 1e-9:
            return wait(
                "The recorded water has reached the estimate. Inspect the soil before any further addition.",
                estimated_ml=0.0,
                range_ml=interval,
                prediction_exhausted=True,
            )
        portion = next_portion(
            remaining,
            p["resolution_ml"],
            p["max_portion_ml"],
            model["tested_maximum_ml"],
        )
        return {
            **result,
            "available": True,
            "estimated_ml": remaining,
            "next_ml": portion,
            "range_ml": interval,
            "current_index": current["index_value"],
            "message": (
                "The remainder is below one measuring graduation. Inspect the soil."
                if portion == 0
                else "Add only the next portion, record it once, then wait and recheck."
            ),
        }

    def status(self):
        self.e.tick()
        p = self.selected()
        r = self.open_run()
        model = (
            self.model(p)
            if p
            else {"curves": [], "signature": None, "runs": 0, "tested_maximum_ml": 0}
        )
        checks = (
            self.rows(
                "SELECT * FROM dose_runs WHERE profile_id=? AND kind='validation' ORDER BY id DESC LIMIT 1",
                (p["id"],),
            )
            if p
            else []
        )
        validation = checks[0] if checks else None
        if validation:
            validation = {
                **validation,
                "context": self.check_context(validation["id"]),
                "policy_review": review_for(self, p, model, validation),
            }
        try:
            stable = self.stable()
            stable_message = (
                "Reading settled: ready to capture when the soak pause has ended."
            )
        except ValueError as exc:
            stable, stable_message = None, str(exc)
        preview = {
            "available": False,
            "message": "Prepare a fresh matching soil sample for the separate check.",
        }
        if (
            not r
            and p
            and self.compatible(p)
            and stable
            and self.e.clock() >= self.e.soak_until
        ):
            if stable["index_value"] <= p["low_index"]:
                try:
                    preview = {
                        "available": True,
                        **self.candidate_details(p, model, stable["index_value"]),
                    }
                except ValueError as exc:
                    preview = {"available": False, "message": str(exc)}
        cycle = self.cycles.snapshot(p, model, stable)
        if r:
            points = self.points(r["id"])
            little_change = (
                len(points) > 1
                and points[-1]["index_value"] - points[-2]["index_value"]
                < MIN_POINT_RISE
            )
            r = {
                **r,
                "points": points,
                "context": self.check_context(r["id"]),
                "problem": self.run_problem(r, p) or response_problem(points),
                "notice": (
                    "The latest addition produced little change. Its water is recorded. Wait longer, inspect the soil and continue this same run only if another small addition is appropriate."
                    if little_change
                    else None
                ),
            }
        recommendation = self.recommendation(p, model, validation, stable, cycle)
        cycle = self.cycles.snapshot(p, model, stable) if self.cycles.open() else cycle
        return {
            "version": MODEL_VERSION,
            "source": self.e.source,
            "profiles": self.rows("SELECT id,name FROM dose_profiles ORDER BY id DESC"),
            "profile": {**p, "setup": json.loads(p["setup_json"])} if p else None,
            "compatible": bool(self.compatible(p)),
            "run": r,
            "training_runs": model["runs"],
            "validation": validation,
            "check_preview": preview,
            "cycle": cycle,
            "calibration_state": "checked"
            if p and self.checked_model(p, model, validation)
            else "experimental",
            "setup_confirmed": bool(
                p and self.confirmed == (p["id"], self.e.stream_id)
            ),
            "stable": stable,
            "stable_message": stable_message,
            "soak_remaining": max(0, math.ceil(self.e.soak_until - self.e.clock())),
            "recent_runs": self.rows("""SELECT r.*,s.combined_run_id AS included_in_run
                    FROM dose_runs r LEFT JOIN dose_run_sources s ON s.source_run_id=r.id
                    ORDER BY r.id DESC LIMIT 20"""),
            "recommendation": recommendation,
        }

    def export(self, name):
        tables = {
            "profiles": "dose_profiles",
            "runs": "dose_runs",
            "points": "dose_points",
            "error_limit_changes": "dose_error_limit_changes",
            "check_reassessments": "dose_check_reassessments",
            "watering_cycles": "dose_watering_cycles",
            "prediction_replays": "dose_prediction_replays",
        }
        if name == "all.json":
            return Response(
                json.dumps(
                    {
                        "exported_at": utc_now(),
                        "source": self.e.source,
                        "model_version": MODEL_VERSION,
                        "error_policy_version": 1,
                        "reconstructions": self.rows(
                            "SELECT * FROM dose_reconstructions ORDER BY combined_run_id"
                        ),
                        "run_sources": self.rows(
                            "SELECT * FROM dose_run_sources ORDER BY source_run_id"
                        ),
                        "point_sources": self.rows(
                            "SELECT * FROM dose_point_sources ORDER BY combined_point_id"
                        ),
                        "check_context": self.rows(
                            "SELECT * FROM dose_check_context ORDER BY run_id"
                        ),
                        **{
                            key: self.rows(f"SELECT * FROM {table} ORDER BY id")
                            for key, table in tables.items()
                        },
                    },
                    indent=2,
                ),
                mimetype="application/json",
                headers={
                    "Content-Disposition": 'attachment; filename="water_amount_evidence.json"'
                },
            )
        key = name.removesuffix(".csv")
        if not name.endswith(".csv") or key not in tables:
            raise ValueError("Unknown water-amount export.")
        with self.e.store.connect() as db:
            cur = db.execute(f"SELECT * FROM {tables[key]} ORDER BY id")
            out = io.StringIO()
            writer = csv.writer(out)
            writer.writerow([d[0] for d in cur.description])
            for row in cur:
                writer.writerow(
                    [
                        "'" + v
                        if isinstance(v, str)
                        and v.lstrip().startswith(("=", "+", "-", "@"))
                        else v
                        for v in row
                    ]
                )
        return Response(
            out.getvalue(),
            mimetype="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="water_amount_{key}.csv"'
            },
        )


def install_dose_support(app, engine, action):
    service = DoseService(engine)
    app.extensions["water_amount"] = service

    @app.get("/api/dose/status")
    def dose_status():
        with engine.lock:
            return jsonify(service.status())

    def register(path, name, handler):
        app.add_url_rule(path, name, action(handler), methods=["POST"])

    for path, handler in [
        ("new_sample", service.cycles.new_sample),
        ("end_cycle", service.cycles.end),
        ("profile", service.create_profile),
        ("select", service.select),
        ("confirm", service.confirm),
        ("begin", service.begin),
        ("water", service.add_water),
        ("capture", service.capture),
        ("finish", service.finish),
        ("end", service.end_run),
    ]:
        register("/api/dose/" + path, "dose_" + path, handler)

    @app.get("/api/dose/export/<name>")
    def dose_export(name):
        with engine.lock:
            return service.export(name)
