import json
import math
import statistics
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone


def utc_now():
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def number(value, label, minimum, maximum):
    if isinstance(value, bool):
        raise ValueError(f"{label}: enter a number")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}: enter a number") from None
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError(f"{label}: must be between {minimum} and {maximum}")
    return result


def text_field(value, label, maximum=500, required=False):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{label}: use at most {maximum} characters")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{label} is required")
    return value


def parse_frame(line, node="SOIL-01"):
    if len(line) > 512:
        raise ValueError("Frame too long")
    try:
        frame = json.loads(line)
    except (ValueError, UnicodeError):
        raise ValueError("Incomplete or invalid JSON frame") from None
    if not isinstance(frame, dict) or frame.get("node") != node:
        raise ValueError("Wrong node or frame format")
    for key, low, high in [
        ("v", 1, 1),
        ("seq", 0, 2**32 - 1),
        ("uptime_ms", 0, 2**32 - 1),
        ("raw", 0, 1023),
        ("min", 0, 1023),
        ("max", 0, 1023),
        ("n", 9, 9),
    ]:
        value = frame.get(key)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"Invalid frame field: {key}")
    if not frame["min"] <= frame["raw"] <= frame["max"]:
        raise ValueError("Invalid burst min/median/max")
    return frame


def relative_index(raw, calibration):
    value = (
        100.0
        * (raw - calibration["dry_raw"])
        / (calibration["wet_raw"] - calibration["dry_raw"])
    )
    return max(0.0, min(100.0, value)), value


class Engine:
    def __init__(self, store, config, source="hardware", clock=time.monotonic):
        self.store, self.config, self.source, self.clock = store, config, source, clock
        self.lock = threading.RLock()
        self.settings = store.settings()
        self.calibration = store.calibration()
        self.recent = deque(maxlen=120)
        self.stream_id = str(uuid.uuid4())
        self.last = None
        self.last_seen = None
        self.last_frame = None
        self.connected = False
        self.port = None
        self.error = "Waiting for the Arduino."
        self.state = "NO_DATA"
        self.low_count = self.high_count = self.rail_count = 0
        self.water_needed = False
        self.soak_until = 0.0
        self.bad_frames = 0
        previous = store.rows("SELECT at FROM watering ORDER BY id DESC LIMIT 1")
        if previous:
            elapsed = (
                datetime.now(timezone.utc)
                - datetime.fromisoformat(previous[0]["at"].replace("Z", "+00:00"))
            ).total_seconds()
            if 0 <= elapsed < config["soak_seconds"]:
                self.soak_until = self.clock() + config["soak_seconds"] - elapsed
        self.event("START", "Application started; waiting for a fresh reading.")

    def event(self, kind, message):
        self.store.insert(
            "events",
            {"at": utc_now(), "type": kind, "message": message, "source": self.source},
        )

    def reset_decision(self):
        self.low_count = self.high_count = self.rail_count = 0
        self.water_needed = False

    def begin_stream(self, port):
        with self.lock:
            self.stream_id = str(uuid.uuid4())
            self.last_frame = None
            self.last = None
            self.last_seen = None
            self.recent.clear()
            self.reset_decision()
            self.connected, self.port, self.error = True, port, ""
            self.event("SERIAL_CONNECTED", f"Serial stream opened: {port}")

    def disconnect(self, message):
        with self.lock:
            if self.connected:
                self.event("SERIAL_DISCONNECTED", message)
            self.connected, self.error = False, message
            self.recent.clear()
            self.reset_decision()
            self.tick()

    def age(self):
        return (
            None if self.last_seen is None else max(0.0, self.clock() - self.last_seen)
        )

    def fresh(self):
        age = self.age()
        return (
            self.connected and age is not None and age <= self.config["stale_seconds"]
        )

    def transition(self, state):
        if state != self.state:
            self.event("STATE", f"{self.state} -> {state}")
            self.state = state

    def evaluate(self, new_sample=False):
        if self.last is None:
            return "NO_DATA"
        if not self.fresh():
            self.reset_decision()
            return "STALE_DATA"
        if self.rail_count >= self.config["confirmation_samples"]:
            self.low_count = self.high_count = 0
            self.water_needed = False
            return "CHECK_SENSOR"
        if "NOISY_BURST" in self.last["flags"]:
            self.low_count = self.high_count = 0
            self.water_needed = False
            return "CHECK_SENSOR"
        if self.calibration is None:
            return "UNCALIBRATED"
        if not self.settings["thresholds_confirmed"]:
            return "SET_THRESHOLDS"
        if self.clock() < self.soak_until:
            return "WAIT_SOAK"
        if not new_sample:
            if self.state == "WAIT_SOAK":
                return "WATER_NEEDED" if self.water_needed else "OBSERVE"
            return self.state
        index = self.last["moisture_index"]
        if index <= self.settings["low_index"]:
            self.low_count += 1
            self.high_count = 0
        elif index >= self.settings["clear_index"]:
            self.high_count += 1
            self.low_count = 0
        else:
            self.low_count = self.high_count = 0
        count = self.config["confirmation_samples"]
        if self.low_count >= count:
            self.water_needed = True
        elif self.high_count >= count:
            self.water_needed = False
        if self.water_needed:
            return "WATER_NEEDED"
        return "CHECKING_DRY" if self.low_count else "OBSERVE"

    def tick(self):
        with self.lock:
            self.transition(self.evaluate())

    def ingest(self, frame):
        with self.lock:
            # The caller passes a validated frame. Duplicate frames do not confirm dryness.
            key = (frame["seq"], frame["uptime_ms"])
            if self.last_frame == key:
                return False
            if self.last_frame and (
                key[0] <= self.last_frame[0] or key[1] < self.last_frame[1]
            ):
                self.begin_stream(self.port or "reset")
                self.event(
                    "NODE_RESET", "Sequence/uptime reset; cleared confirmation history."
                )
            if (
                self.last_seen is not None
                and self.clock() - self.last_seen > self.config["stale_seconds"]
            ):
                self.recent.clear()
                self.reset_decision()
            self.last_frame = key
            self.last_seen = self.clock()
            self.error = ""
            self.connected = True
            raw = frame["raw"]
            self.rail_count = self.rail_count + 1 if raw <= 2 or raw >= 1021 else 0
            index, unclamped = (
                relative_index(raw, self.calibration)
                if self.calibration
                else (None, None)
            )
            flags = []
            if raw <= 2 or raw >= 1021:
                flags.append("ADC_NEAR_RAIL_CHECK_PLACEMENT_AND_WIRING")
            if frame["max"] - frame["min"] > 40:
                flags.append("NOISY_BURST")
            if unclamped is not None and not 0 <= unclamped <= 100:
                flags.append("OUTSIDE_CALIBRATION_REFERENCES")
            row = {
                "at": utc_now(),
                "stream_id": self.stream_id,
                "seq": frame["seq"],
                "uptime_ms": frame["uptime_ms"],
                "raw": raw,
                "burst_min": frame["min"],
                "burst_max": frame["max"],
                "calibration_id": self.calibration["id"] if self.calibration else None,
                "moisture_index": index,
                "index_unclamped": unclamped,
                "state": "",
                "flags": ";".join(flags),
                "source": self.source,
            }
            self.last = row
            self.recent.append((self.last_seen, raw))
            self.transition(self.evaluate(new_sample=True))
            row["state"] = self.state
            row["id"] = self.store.insert("readings", row)
            return True

    def capture(self, kind, note):
        with self.lock:
            if kind not in {"dry", "wet"}:
                raise ValueError("Choose dry or wet")
            note = text_field(note, "Reference description", required=True)
            count = self.config["capture_samples"]
            samples = list(self.recent)[-count:]
            if not self.fresh() or len(samples) != count:
                raise ValueError(
                    f"Need {count} fresh samples. Leave the probe still for at least 60 seconds."
                )
            if self.clock() - samples[0][0] > self.config["capture_max_age_seconds"]:
                raise ValueError(
                    "Samples are too old; wait for a continuous minute of fresh data"
                )
            values = [v for _, v in samples]
            spread = max(values) - min(values)
            if spread > self.config["capture_max_spread_adc"]:
                raise ValueError(
                    "Reference is unstable. Keep depth/contact fixed and wait another minute."
                )
            identity = self.store.insert(
                "captures",
                {
                    "at": utc_now(),
                    "kind": kind,
                    "raw": statistics.median(values),
                    "spread": spread,
                    "sd": statistics.stdev(values),
                    "samples_json": json.dumps(values),
                    "note": note,
                    "source": self.source,
                },
            )
            self.event(
                "CAPTURE",
                f"{kind} reference #{identity}; median={statistics.median(values)} ADC",
            )
            return identity

    def activate(self, dry_id, wet_id, note):
        with self.lock:
            note = text_field(note, "Calibration description", required=True)
            ids = (
                number(dry_id, "Dry reference ID", 1, 1e12),
                number(wet_id, "Wet reference ID", 1, 1e12),
            )
            if any(not x.is_integer() for x in ids):
                raise ValueError("Reference IDs must be integers")
            rows = self.store.rows("SELECT * FROM captures WHERE id IN (?,?)", ids)
            selected = {r["id"]: r for r in rows}
            dry, wet = selected.get(int(ids[0])), selected.get(int(ids[1]))
            if not dry or not wet or dry["kind"] != "dry" or wet["kind"] != "wet":
                raise ValueError(
                    "Select one captured dry reference and one captured wet reference"
                )
            if dry["source"] != self.source or wet["source"] != self.source:
                raise ValueError("Reference source mismatch")
            if wet["raw"] - dry["raw"] < self.config["minimum_calibration_span_adc"]:
                raise ValueError(
                    "For Grove v1.4 the wet reference must exceed dry by at least 50 ADC counts. Check the sensor, soil and wiring."
                )
            settings = dict(self.settings, thresholds_confirmed=False)
            self.store.activate_calibration(
                utc_now(), dry, wet, self.source, note, settings
            )
            self.settings, self.calibration = settings, self.store.calibration()
            # Retain historic readings unchanged; wait for a new calibrated sample.
            self.last = None
            self.last_seen = None
            self.recent.clear()
            self.reset_decision()
            self.event(
                "CALIBRATION",
                f"Activated profile #{self.calibration['id']}; thresholds require confirmation.",
            )
            self.transition("NO_DATA")

    def configure(self, data):
        with self.lock:
            if type(data.get("thresholds_confirmed")) is not bool:
                raise ValueError("Threshold confirmation must be true or false")
            settings = {
                "crop": text_field(data.get("crop"), "Crop/container", 120, True),
                "soil_notes": text_field(data.get("soil_notes"), "Soil details", 500),
                "low_index": number(data.get("low_index"), "Dry trigger index", 0, 99),
                "clear_index": number(data.get("clear_index"), "Clear index", 1, 100),
                "thresholds_confirmed": data["thresholds_confirmed"],
                "trial_step_ml": None
                if data.get("trial_step_ml") in (None, "")
                else number(data["trial_step_ml"], "Trial portion (mL)", 0.1, 10000),
            }
            if settings["low_index"] >= settings["clear_index"]:
                raise ValueError("Clear index must be above the dry trigger index")
            if settings["thresholds_confirmed"] and self.calibration is None:
                raise ValueError("Activate a calibration before confirming thresholds")
            self.store.save_settings(settings)
            self.settings = settings
            self.reset_decision()
            self.event("SETTINGS", json.dumps(settings, sort_keys=True))
            # Reset the status immediately; confirmation resumes on new samples.
            self.transition(
                "OBSERVE"
                if self.fresh()
                and self.calibration
                and settings["thresholds_confirmed"]
                else self.evaluate()
            )

    def water(self, data):
        with self.lock:
            if data.get("already_applied") is not True:
                raise ValueError(
                    "Confirm that you already measured and applied this water"
                )
            volume = number(data.get("volume_ml"), "Measured water (mL)", 0.1, 10000)
            operator = text_field(data.get("operator"), "Operator", 80, True)
            note = text_field(data.get("note", ""), "Watering note", 500)
            latest = self.last if self.fresh() else None
            self.store.insert(
                "watering",
                {
                    "at": utc_now(),
                    "volume_ml": volume,
                    "operator": operator,
                    "note": note,
                    "reading_id": latest.get("id") if latest else None,
                    "raw_before": latest["raw"] if latest else None,
                    "index_before": latest["moisture_index"] if latest else None,
                    "calibration_id": latest["calibration_id"] if latest else None,
                    "source": self.source,
                },
            )
            self.soak_until = self.clock() + self.config["soak_seconds"]
            self.low_count = self.high_count = 0
            self.event(
                "MANUAL_WATER",
                f"Operator reported {volume:g} mL applied; start soak interval.",
            )
            self.tick()

    def observation(self, data):
        with self.lock:
            self.store.insert(
                "observations",
                {
                    "at": utc_now(),
                    "note": text_field(data.get("note"), "Observation", 1000, True),
                    "operator": text_field(data.get("operator"), "Operator", 80, True),
                    "source": self.source,
                },
            )

    def snapshot(self):
        with self.lock:
            self.tick()
            captures = self.store.latest_captures()
            return {
                "source": self.source,
                "state": self.state,
                "connected": self.connected,
                "serial_port": self.port,
                "error": self.error,
                "age_seconds": self.age(),
                "latest": dict(self.last) if self.last else None,
                "calibration": self.calibration,
                "captures": captures,
                "settings": self.settings,
                "bad_frames": self.bad_frames,
                "fresh_capture_samples": len(self.recent),
                "capture_samples_needed": self.config["capture_samples"],
                "soak_remaining_seconds": max(
                    0, math.ceil(self.soak_until - self.clock())
                ),
                "summary": self.store.summary(),
                "recent_watering": self.store.rows(
                    "SELECT * FROM watering ORDER BY id DESC LIMIT 10"
                ),
                "recent_events": self.store.rows(
                    "SELECT * FROM events ORDER BY id DESC LIMIT 12"
                ),
                "recent_observations": self.store.rows(
                    "SELECT * FROM observations ORDER BY id DESC LIMIT 10"
                ),
            }
