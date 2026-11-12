import base64
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pi"))
from analytics import Analytics, reduced_segments
from app import create_app, load_config
from core import Engine
from store import Store


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "garden.sqlite3")
        self.service = Analytics(self.store, "hardware", 20)

    def tearDown(self):
        self.temp.cleanup()

    def reading(
        self,
        at,
        seq,
        state="WATER_NEEDED",
        stream="stream-a",
        source="hardware",
        index=10,
        flags="",
    ):
        return self.store.insert(
            "readings",
            dict(
                at=at,
                stream_id=stream,
                seq=seq,
                uptime_ms=seq * 1000,
                raw=64,
                burst_min=63,
                burst_max=65,
                calibration_id=None,
                moisture_index=index,
                index_unclamped=index,
                state=state,
                flags=flags,
                source=source,
            ),
        )

    def water(self, at, volume, note="", source="hardware"):
        return self.store.insert(
            "watering",
            dict(
                at=at,
                volume_ml=volume,
                operator="test",
                note=note,
                reading_id=None,
                raw_before=None,
                index_before=None,
                calibration_id=None,
                source=source,
            ),
        )

    def test_sast_midnight_splits_observed_durations(self):
        self.reading("2026-10-20T21:59:58.000Z", 1)
        self.reading("2026-10-20T22:00:03.000Z", 2)
        d = self.service.payload("2026-10-21")
        days = {r["date"]: r for r in d["days"]}
        self.assertEqual(days["2026-10-20"]["recorded_seconds"], 2)
        self.assertEqual(days["2026-10-21"]["recorded_seconds"], 3)
        self.assertEqual(d["day"]["dry_warning_seconds"], 3)

    def test_gaps_missing_sequences_and_stream_changes_do_not_count(self):
        self.reading("2026-10-20T10:00:00.000Z", 1)
        self.reading("2026-10-20T10:00:01.000Z", 2)
        self.reading("2026-10-20T10:01:00.000Z", 3)
        self.reading("2026-10-20T10:01:01.000Z", 5)
        self.reading("2026-10-20T10:01:02.000Z", 6, stream="stream-b")
        d = self.service.payload("2026-10-20")
        self.assertEqual(d["day"]["recorded_seconds"], 1)
        self.assertEqual(d["day"]["dry_warning_seconds"], 1)

    def test_noisy_and_soak_intervals_do_not_count_as_dry_warning(self):
        self.reading("2026-10-20T10:00:00.000Z", 1, flags="NOISY_BURST")
        self.reading("2026-10-20T10:00:01.000Z", 2, state="WAIT_SOAK")
        self.reading("2026-10-20T10:00:02.000Z", 3, state="OBSERVE")
        d = self.service.payload("2026-10-20")
        self.assertEqual(d["day"]["recorded_seconds"], 2)
        self.assertEqual(d["day"]["dry_warning_seconds"], 0)

    def test_week_uses_sast_calendar_and_no_double_counting(self):
        self.water("2026-10-18T21:59:59.000Z", 300, "calibration")
        self.water("2026-10-18T22:00:00.000Z", 2, "Water-amount training run #1.")
        self.water("2026-10-20T10:00:00.000Z", 0.5, "Water-amount validation run #2.")
        self.water("2026-10-20T10:01:00.000Z", 1.25)
        d = self.service.payload("2026-10-20")
        self.assertEqual(d["week_start"], "2026-10-19")
        self.assertEqual(d["week"]["volume_ml"], 3.75)
        self.assertEqual(d["week"]["watering_count"], 3)
        self.assertEqual(d["day"]["categories"]["checks"], 0.5)
        self.assertEqual(d["day"]["categories"]["other"], 1.25)
        self.assertEqual(sum(x["volume_ml"] for x in d["days"]), 3.75)

    def test_simulated_records_are_separate(self):
        self.water("2026-10-20T10:00:00.000Z", 2)
        self.water("2026-10-20T10:01:00.000Z", 999, source="simulated")
        self.reading("2026-10-20T10:00:00.000Z", 1)
        self.reading("2026-10-20T10:00:01.000Z", 2, source="simulated", stream="demo")
        d = self.service.payload("2026-10-20")
        self.assertEqual(d["week"]["volume_ml"], 2)
        self.assertEqual(d["week"]["reading_count"], 1)

    def test_empty_database_has_explicit_empty_views(self):
        d = self.service.payload("2026-10-20")
        self.assertIsNone(d["latest_recorded_at"])
        self.assertEqual(d["watering"]["selected"], "day")
        self.assertEqual(d["watering"]["segments"], [])
        self.assertEqual(d["predictions"], [])
        self.assertIsNone(d["prediction_summary"]["mean_difference_ml"])

    def test_invalid_parameters_are_rejected(self):
        for kwargs in [
            dict(selected_date="bad"),
            dict(scope="other"),
            dict(selected_run="999"),
        ]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.service.payload(**kwargs)

    def test_http_views_keep_auth_and_do_not_write_evidence(self):
        engine = Engine(self.store, load_config(ROOT / "pi/config.example.json"))
        app = create_app(engine, "analytics-test-password")
        client = app.test_client()
        self.assertEqual(client.get("/history").status_code, 401)
        auth = {
            "Authorization": "Basic "
            + base64.b64encode(b"garden:analytics-test-password").decode()
        }
        with self.store.connect() as db:
            before = list(db.iterdump())
        response = client.get("/api/analytics?date=2026-10-20", headers=auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get("/history", headers=auth).status_code, 200)
        self.assertEqual(
            client.get("/api/analytics?date=bad", headers=auth).status_code, 400
        )
        with self.store.connect() as db:
            self.assertEqual(before, list(db.iterdump()))
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
        self.assertEqual(
            client.post("/api/settings", json={}, headers=auth).status_code, 403
        )


if __name__ == "__main__":
    unittest.main()
