import json
import math

from core import text_field, utc_now

METHOD_SCHEMA = """
CREATE TABLE IF NOT EXISTS dose_watering_cycles (
 id INTEGER PRIMARY KEY, at TEXT NOT NULL, profile_id INTEGER NOT NULL,
 stream_id TEXT NOT NULL, model_signature TEXT NOT NULL, status TEXT NOT NULL,
 baseline_index REAL NOT NULL, target_index REAL NOT NULL,
 start_water_id INTEGER NOT NULL, end_water_id INTEGER,
 predicted_ml REAL NOT NULL, low_ml REAL NOT NULL, high_ml REAL NOT NULL,
 detail_json TEXT NOT NULL, closed_at TEXT, note TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_dose_watering_cycle
 ON dose_watering_cycles(status) WHERE status='open';
CREATE TABLE IF NOT EXISTS dose_prediction_replays (
 id INTEGER PRIMARY KEY, at TEXT NOT NULL, run_id INTEGER NOT NULL,
 original_model_signature TEXT NOT NULL, current_model_signature TEXT NOT NULL,
 old_prediction_ml REAL NOT NULL, replay_prediction_ml REAL NOT NULL,
 observed_ml REAL NOT NULL, actual_total_ml REAL NOT NULL,
 replay_error_ml REAL NOT NULL, detail_json TEXT NOT NULL,
 UNIQUE(run_id,current_model_signature)
);
"""


def baseline_amount_ranges(curves, starting_index, target, minimum=3):
    """Align each measured index RISE to a fresh zero-water starting point.

    Water at the requested rise is read from the complete cumulative curve.
    No volume associated with the new sample's absolute ADC is subtracted.
    This is an empirical response model, not a soil-water-content conversion.
    """
    from dose_amount import water_bounds

    if not math.isfinite(starting_index) or not math.isfinite(target):
        raise ValueError("Invalid starting reading or target.")
    if starting_index >= target:
        return [(i, 0.0, 0.0) for i in range(len(curves))]
    rise = target - starting_index
    amounts = []
    for i, curve in enumerate(curves):
        if not curve or abs(curve[0][1]) > 1e-9:
            continue
        try:
            low, high = water_bounds(curve, curve[0][0] + rise)
            amounts.append((i, low, high))
        except ValueError:
            continue
    if len(amounts) < minimum:
        raise ValueError(
            "Fewer than three training trials cover the required index rise. No amount is extrapolated."
        )
    return amounts


class WaterCycle:
    def __init__(self, dose):
        self.d, self.e = dose, dose.e
        self.ready_at = 0.0

    def open(self):
        rows = self.d.rows(
            "SELECT * FROM dose_watering_cycles WHERE status='open' ORDER BY id DESC LIMIT 1"
        )
        return rows[0] if rows else None

    def applied(self, cycle):
        end = cycle["end_water_id"]
        rows = self.d.rows(
            "SELECT * FROM watering WHERE id>? AND (? IS NULL OR id<=?) ORDER BY id",
            (cycle["start_water_id"], end, end),
        )
        return rows, sum(float(r["volume_ml"]) for r in rows)

    def start(self, data):
        p = self.d.require_profile()
        if self.d.open_run():
            raise ValueError(
                "Finish or end the calibration run before starting normal watering."
            )
        if self.open():
            raise ValueError(
                "A watering cycle is already open. Continue it or end it before changing soil."
            )
        if data.get("fresh_start") is not True:
            raise ValueError(
                "Confirm the matching setup and start before the first water addition."
            )
        if self.e.clock() < self.e.soak_until:
            raise ValueError("Wait for the current soak pause to finish.")
        point = self.d.stable()
        if point["index_value"] > p["low_index"] or self.e.state != "WATER_NEEDED":
            raise ValueError("Start a cycle only after a confirmed dry-soil warning.")
        model = self.d.model(p)
        details = self.d.amount_details(p, model, point["index_value"])
        details = {
            **details,
            "baseline_reading_ids_json": point["reading_ids_json"],
            "starting_water_ml": 0.0,
        }
        with self.e.store.connect() as db:
            cursor = db.execute(
                """INSERT INTO dose_watering_cycles
                (at,profile_id,stream_id,model_signature,status,baseline_index,target_index,
                 start_water_id,predicted_ml,low_ml,high_ml,detail_json)
                VALUES(?,?,?,?,'open',?,?,?,?,?,?,?)""",
                (
                    utc_now(),
                    p["id"],
                    self.e.stream_id,
                    model["signature"],
                    point["index_value"],
                    p["target_index"],
                    self.d.latest_water(),
                    details["predicted_ml"],
                    details["range_ml"][0],
                    details["range_ml"][1],
                    json.dumps(details, sort_keys=True),
                ),
            )
            identity = cursor.lastrowid
        self.d.confirmed = (p["id"], self.e.stream_id)
        self.e.event(
            "DOSE_CYCLE", f"Started watering cycle #{identity} at zero recorded water."
        )
        return {
            "cycle_id": identity,
            "notice": "Cycle started. Add only the displayed next portion, then record it once.",
        }

    def close(self, cycle, status, note):
        with self.e.store.connect() as db:
            db.execute(
                "UPDATE dose_watering_cycles SET status=?,end_water_id=?,closed_at=?,note=? WHERE id=? AND status='open'",
                (status, self.d.latest_water(), utc_now(), note, cycle["id"]),
            )

    def end(self, data):
        cycle = self.open()
        if not cycle:
            raise ValueError("No watering cycle is open.")
        note = text_field(
            data.get("note", "Operator ended this watering cycle."), "Note", 300
        )
        self.close(cycle, "ended", note)
        self.e.event("DOSE_CYCLE", f"Ended watering cycle #{cycle['id']}.")
        return {"notice": "Watering cycle ended. Its recorded additions remain saved."}

    def new_sample(self, data):
        p = self.d.require_profile()
        if self.d.open_run():
            raise ValueError(
                "Finish or end the calibration run before changing the soil sample."
            )
        cycle = self.open()
        if cycle:
            self.close(cycle, "new_sample", "Operator selected a new soil sample.")
        self.d.confirmed = (p["id"], self.e.stream_id)
        self.ready_at = self.e.clock() + 30
        return {
            "notice": "New soil sample selected. Allow 30 seconds for readings to settle."
        }

    def snapshot(self, p, model, settled):
        cycle = self.open()
        if not cycle:
            return None
        water, applied = self.applied(cycle)
        problem = None
        if (
            not p
            or cycle["profile_id"] != p["id"]
            or cycle["model_signature"] != model["signature"]
        ):
            problem = "The calibration changed. Select New soil sample when the new sample is ready."
        elif cycle["stream_id"] != self.e.stream_id:
            problem = "Confirm setup to continue with this soil sample."
        elif any(
            r["source"] != p["source"] or r.get("calibration_id") != p["calibration_id"]
            for r in water
        ):
            problem = "Water was recorded under different settings. Check the setup and select New soil sample."
        reached = bool(
            not problem
            and settled
            and settled["index_value"] >= cycle["target_index"]
            and self.e.clock() >= self.e.soak_until
        )
        if reached:
            self.close(
                cycle,
                "target_reached",
                "The settled sensor reading reached the target.",
            )
            cycle = {
                **cycle,
                "status": "target_reached",
                "end_water_id": self.d.latest_water(),
            }
        return {
            **cycle,
            "actual_added_ml": applied,
            "remaining_ml": 0.0
            if reached
            else max(0.0, cycle["predicted_ml"] - applied),
            "remaining_range_ml": [
                max(0.0, cycle["low_ml"] - applied),
                max(0.0, cycle["high_ml"] - applied),
            ],
            "problem": problem,
            "target_reached": reached,
        }
