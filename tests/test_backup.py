import json
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pi"))
from backup import create_backup


class BackupTests(unittest.TestCase):
    def test_missing_database_does_not_create_one(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            with self.assertRaisesRegex(FileNotFoundError, "No database yet"):
                create_backup(base)
            self.assertFalse((base / "data").exists())
            self.assertFalse((base / "backups").exists())

    def test_backup_includes_committed_wal_data_and_config_only(self):
        for simulate, mode in ((False, "garden"), (True, "demo")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                (base / "data").mkdir()
                (base / "config.json").write_text(
                    '{"node_id":"SOIL-01"}', encoding="utf-8"
                )
                (base / "dashboard-password.txt").write_text(
                    "not-for-backup", encoding="utf-8"
                )
                connection = sqlite3.connect(base / "data" / f"{mode}.sqlite3")
                try:
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.execute("CREATE TABLE samples (raw INTEGER)")
                    connection.execute("INSERT INTO samples VALUES (375)")
                    connection.commit()
                    destination = create_backup(base, simulate)
                    with zipfile.ZipFile(destination) as archive:
                        self.assertIsNone(archive.testzip())
                        self.assertEqual(
                            set(archive.namelist()),
                            {f"{mode}.sqlite3", "config.json", "BACKUP_INFO.json"},
                        )
                        self.assertEqual(
                            json.loads(archive.read("BACKUP_INFO.json"))["mode"], mode
                        )
                        extracted = base / "restored.sqlite3"
                        extracted.write_bytes(archive.read(f"{mode}.sqlite3"))
                    with sqlite3.connect(extracted) as restored:
                        self.assertEqual(
                            restored.execute("SELECT raw FROM samples").fetchall(),
                            [(375,)],
                        )
                    self.assertEqual(
                        connection.execute("SELECT raw FROM samples").fetchall(),
                        [(375,)],
                    )
                    self.assertEqual(
                        [p.name for p in destination.parent.iterdir()],
                        [destination.name],
                    )
                finally:
                    connection.close()

    def test_backup_succeeds_without_optional_config(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            (base / "data").mkdir()
            with sqlite3.connect(base / "data" / "garden.sqlite3") as connection:
                connection.execute("CREATE TABLE samples (raw INTEGER)")
            with zipfile.ZipFile(create_backup(base)) as archive:
                self.assertNotIn("config.json", archive.namelist())


if __name__ == "__main__":
    unittest.main()
