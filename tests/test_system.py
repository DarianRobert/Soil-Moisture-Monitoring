import base64
import csv
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pi"))
from app import create_app, load_config
from collector import Collector
from core import Engine, parse_frame, relative_index, utc_now
from store import Store


def packet(raw=300, seq=1, uptime=None, **overrides):
    frame = {
        "v": 1,
        "node": "SOIL-01",
        "seq": seq,
        "uptime_ms": seq * 5000 if uptime is None else uptime,
        "raw": raw,
        "min": max(0, raw - 1),
        "max": min(1023, raw + 1),
        "n": 9,
    }
    frame.update(overrides)
    return frame


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds=5):
        self.now += seconds


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.sqlite3")
        self.cfg = load_config(ROOT / "pi/config.example.json")
        self.clock = Clock()
        self.engine = Engine(self.store, self.cfg, clock=self.clock)
        self.engine.begin_stream("test")
        self.seq = 0

    def tearDown(self):
        self.temp.cleanup()

    def feed(self, raw, n=1, **kw):
        for _ in range(n):
            self.seq += 1
            self.engine.ingest(parse_frame(json.dumps(packet(raw, self.seq, **kw))))
            self.clock.advance()

    def calibrate(self):
        self.feed(100, 12)
        dry = self.engine.capture("dry", "Dry test soil")
        self.feed(600, 12)
        wet = self.engine.capture("wet", "Drained wet test soil")
        self.engine.activate(dry, wet, "Test profile")
        self.engine.configure(dict(self.engine.settings, thresholds_confirmed=True))

    def test_frame_validation(self):
        self.assertEqual(parse_frame(json.dumps(packet()))["raw"], 300)
        for field, value in [
            ("raw", True),
            ("seq", -1),
            ("raw", 1024),
            ("n", 8),
            ("v", 2),
            ("node", "wrong"),
        ]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                parse_frame(json.dumps(packet(**{field: value})))
        for content in ["x", "[]", "null", '{"raw":3}', "a" * 513]:
            with self.assertRaises(ValueError):
                parse_frame(content)

    def test_relative_index_is_correct_and_clipped(self):
        c = {"dry_raw": 100, "wet_raw": 600}
        self.assertEqual(relative_index(100, c), (0, 0))
        self.assertEqual(relative_index(350, c), (50, 50))
        self.assertEqual(relative_index(600, c), (100, 100))
        self.assertEqual(relative_index(650, c), (100, 110))
        self.assertEqual(relative_index(50, c), (0, -10))

    def test_new_install_requires_references_and_thresholds(self):
        self.feed(100)
        self.assertEqual(self.engine.state, "UNCALIBRATED")
        with self.assertRaises(ValueError):
            self.engine.configure(dict(self.engine.settings, thresholds_confirmed=True))
        with self.assertRaises(ValueError):
            self.engine.capture("dry", "Too few")
        self.calibrate()
        self.feed(350)
        self.assertEqual(self.engine.state, "OBSERVE")

    def test_hysteresis_debounce_and_duplicate_handling(self):
        self.calibrate()
        self.feed(200, 2)  # index=20, needs three distinct packets
        self.assertEqual(self.engine.state, "CHECKING_DRY")
        last = packet(200, self.seq)
        self.assertFalse(self.engine.ingest(last))
        self.assertEqual(self.engine.low_count, 2)
        self.feed(200)
        self.assertEqual(self.engine.state, "WATER_NEEDED")
        self.feed(290, 5)  # index=38, inside hysteresis band
        self.assertEqual(self.engine.state, "WATER_NEEDED")
        self.feed(350, 2)
        self.assertEqual(self.engine.state, "WATER_NEEDED")
        self.feed(350)
        self.assertEqual(self.engine.state, "OBSERVE")

    def test_stale_disconnect_and_recovery(self):
        self.calibrate()
        self.feed(200, 3)
        self.clock.advance(21)
        self.engine.tick()
        self.assertEqual(self.engine.state, "STALE_DATA")
        self.feed(200)
        self.assertEqual(self.engine.state, "CHECKING_DRY")
        self.engine.disconnect("Cable removed")
        self.assertEqual(self.engine.state, "STALE_DATA")
        self.engine.begin_stream("reconnected")
        self.engine.tick()
        self.assertEqual(self.engine.state, "NO_DATA")
        self.feed(200)
        self.assertEqual(self.engine.state, "CHECKING_DRY")

    def test_watering_measurement_and_soak_survive_restart(self):
        self.calibrate()
        self.feed(200, 3)
        data = {
            "volume_ml": 20,
            "operator": "AR",
            "note": "Measured trial",
            "already_applied": True,
        }
        for bad in [0, -1, float("nan"), float("inf"), True, "x", 10001]:
            with self.assertRaises(ValueError):
                self.engine.water(dict(data, volume_ml=bad))
        with self.assertRaises(ValueError):
            self.engine.water(dict(data, already_applied=False))
        self.engine.water(data)
        self.assertEqual(self.engine.state, "WAIT_SOAK")
        self.feed(200, 3)
        self.assertEqual(self.engine.state, "WAIT_SOAK")
        restarted = Engine(self.store, self.cfg, clock=self.clock)
        self.assertEqual(restarted.state, "NO_DATA")
        self.assertGreater(restarted.soak_until, self.clock())
        self.clock.advance(181)
        self.feed(200, 3)
        self.assertEqual(self.engine.state, "WATER_NEEDED")
        row = self.store.rows("SELECT * FROM watering")[0]
        self.assertEqual(row["volume_ml"], 20)
        self.assertAlmostEqual(row["index_before"], 20)

    def test_capture_rejects_noise_reversed_and_small_span(self):
        self.feed(100, 11)
        self.feed(190)
        with self.assertRaises(ValueError):
            self.engine.capture("dry", "Unstable")
        self.feed(300, 12)
        dry = self.engine.capture("dry", "Dry")
        for raw in [100, 320]:
            self.feed(raw, 12)
            wet = self.engine.capture("wet", "Wet")
            with self.assertRaises(ValueError):
                self.engine.activate(dry, wet, "Invalid pair")

    def test_recalibration_keeps_original_readings(self):
        self.calibrate()
        self.feed(350)
        first = self.store.history()[-1]
        self.calibrate()
        self.feed(350)
        rows = self.store.history()
        kept = next(r for r in rows if r["id"] == first["id"])
        self.assertEqual(first, kept)
        self.assertNotEqual(rows[-1]["calibration_id"], first["calibration_id"])
        self.assertEqual(
            len(self.store.rows("SELECT * FROM calibrations WHERE active=1")), 1
        )

    def test_node_reset_starts_a_new_stream(self):
        self.calibrate()
        self.feed(200, 2)
        old = self.engine.stream_id
        self.engine.ingest(packet(200, seq=0, uptime=50))
        self.assertNotEqual(old, self.engine.stream_id)
        self.assertEqual(self.engine.low_count, 1)

    def test_extreme_and_noisy_readings_request_inspection(self):
        self.calibrate()
        self.feed(0, 3)
        self.assertEqual(self.engine.state, "CHECK_SENSOR")
        self.feed(300, min=200, max=400)
        self.assertEqual(self.engine.state, "CHECK_SENSOR")
        self.feed(300, 3)
        self.assertEqual(self.engine.state, "OBSERVE")

    def test_settings_validation_and_audit(self):
        with self.assertRaises(ValueError):
            self.engine.configure(
                dict(self.engine.settings, low_index=60, clear_index=40)
            )
        self.calibrate()
        self.engine.configure(
            dict(self.engine.settings, low_index=20, clear_index=40, trial_step_ml=10)
        )
        self.assertEqual(self.store.settings()["low_index"], 20)
        self.assertTrue(
            any(
                r["type"] == "SETTINGS" for r in self.store.rows("SELECT * FROM events")
            )
        )

    def test_csv_rows_quotes_and_formula_protection(self):
        self.feed(100)
        self.engine.observation({"operator": "AR", "note": "=1+1"})
        out = list(
            csv.DictReader(io.StringIO("".join(self.store.export_csv("observations"))))
        )
        self.assertEqual(out[0]["note"], "'=1+1")
        reading = list(
            csv.DictReader(io.StringIO("".join(self.store.export_csv("readings"))))
        )[0]
        self.assertEqual(reading["source"], "hardware")
        self.assertEqual(reading["raw"], "100")
        with self.assertRaises(ValueError):
            list(self.store.export_csv("readings; DROP TABLE readings"))

    def test_web_auth_csrf_exports_and_validation(self):
        app = create_app(self.engine, "test-password-123")
        client = app.test_client()
        auth = {
            "Authorization": "Basic "
            + base64.b64encode(b"garden:test-password-123").decode()
        }
        self.assertEqual(client.get("/api/status").status_code, 401)
        page = client.get("/", headers=auth)
        self.assertEqual(page.status_code, 200)
        csrf = re.search(r'data-csrf="([a-f0-9]+)"', page.get_data(as_text=True)).group(
            1
        )
        self.assertEqual(
            client.post("/api/observation", headers=auth, json={}).status_code, 403
        )
        headers = dict(auth, **{"X-Garden-CSRF": csrf})
        self.assertEqual(
            client.post("/api/observation", headers=headers, json={}).status_code, 400
        )
        result = client.post(
            "/api/observation",
            headers=headers,
            json={"operator": "AR", "note": "Actual trial"},
        )
        self.assertEqual(result.status_code, 200)
        exported = client.get("/api/export/observations.csv", headers=auth)
        self.assertIn("Actual trial", exported.get_data(as_text=True))
        self.assertEqual(
            client.get("/api/export/invalid.csv", headers=auth).status_code, 400
        )
        self.assertEqual(client.get("/api/history", headers=auth).status_code, 200)
        self.assertIn("nosniff", page.headers["X-Content-Type-Options"])

    @unittest.skipUnless(os.name == "posix", "Uses Linux pseudo terminal")
    def test_real_pyserial_fragmented_frames(self):
        import pty

        master, slave = pty.openpty()
        self.cfg["serial_port"] = os.ttyname(slave)
        engine = Engine(self.store, self.cfg)  # Real monotonic clock for IO test.
        stop = threading.Event()
        collector = Collector(engine, stop)
        collector.start()
        try:
            deadline = time.monotonic() + 4
            while not engine.connected and time.monotonic() < deadline:
                time.sleep(0.02)
            time.sleep(2.2)  # Allow the collector's documented Uno reset delay.
            frame = json.dumps(packet(456, seq=11)).encode() + b"\n"
            os.write(master, frame[:25])
            time.sleep(1.1)
            os.write(master, frame[25:])
            deadline = time.monotonic() + 3
            while engine.last is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertIsNotNone(engine.last)
            self.assertEqual(engine.last["raw"], 456)
            os.write(master, b"invalid\n")
            deadline = time.monotonic() + 2
            while engine.bad_frames < 1 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(engine.bad_frames, 1)
        finally:
            stop.set()
            collector.join(3)
            os.close(master)
            os.close(slave)
        self.assertFalse(collector.is_alive())


if __name__ == "__main__":
    unittest.main(verbosity=2)
