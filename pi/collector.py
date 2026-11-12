import json
import logging
import math
import os
import threading
import time
from pathlib import Path

import serial
from serial.tools import list_ports
from core import parse_frame

LOG = logging.getLogger(__name__)


def select_port(configured):
    if configured != "auto":
        return configured
    candidates = sorted(
        p.device
        for p in list_ports.comports()
        if p.device.startswith(("/dev/ttyACM", "/dev/ttyUSB"))
    )
    if len(candidates) != 1:
        raise ValueError(
            f"Found {len(candidates)} USB serial ports. Connect one Uno, or set serial_port in config.json. "
            "Use: python3 -m serial.tools.list_ports -v"
        )
    # Prefer a stable by-id link when the board provides one.
    by_id = Path("/dev/serial/by-id")
    if by_id.exists():
        links = sorted(
            p
            for p in by_id.iterdir()
            if os.path.realpath(p) == os.path.realpath(candidates[0])
        )
        if links:
            return str(links[0])
    return candidates[0]


class Collector(threading.Thread):
    def __init__(self, engine, stop, simulate=False):
        super().__init__(name="moisture-collector", daemon=True)
        self.engine, self.stop, self.simulate = engine, stop, simulate

    def run(self):
        if self.simulate:
            return self.run_simulation()
        while not self.stop.is_set():
            try:
                port = select_port(self.engine.config["serial_port"])
                options = dict(
                    port=port, baudrate=self.engine.config["baudrate"], timeout=1
                )
                if os.name == "posix":
                    options["exclusive"] = True
                with serial.Serial(**options) as connection:
                    self.engine.begin_stream(port)
                    # Many Unos reset when the USB serial port is opened.
                    if self.stop.wait(2):
                        return
                    connection.reset_input_buffer()
                    pending = bytearray()
                    dropping = False
                    while not self.stop.is_set():
                        part = connection.read_until(b"\n", 513)
                        if part:
                            if dropping:
                                if part.endswith(b"\n"):
                                    dropping = False
                                continue
                            pending.extend(part)
                            if len(pending) > 512:
                                pending.clear()
                                dropping = not part.endswith(b"\n")
                                self.bad_frame("Frame exceeded 512 bytes")
                            elif pending.endswith(b"\n"):
                                try:
                                    frame = parse_frame(
                                        bytes(pending), self.engine.config["node_id"]
                                    )
                                    self.engine.ingest(frame)
                                except ValueError as exc:
                                    self.bad_frame(str(exc))
                                pending.clear()
                        self.engine.tick()
            except (serial.SerialException, OSError, ValueError) as exc:
                self.engine.disconnect(str(exc))
                LOG.warning("Serial: %s", exc)
                self.stop.wait(3)
            except Exception:
                # A disk/DB error must not leave the web display apparently healthy.
                LOG.exception("Collector stopped due to an unexpected error")
                self.engine.connected = False
                self.engine.error = (
                    "Collector stopped. Check the terminal/service log and disk space."
                )
                return

    def bad_frame(self, message):
        with self.engine.lock:
            self.engine.bad_frames += 1
            self.engine.error = message
            # Avoid filling the database with the same malformed stream.
            if self.engine.bad_frames == 1 or self.engine.bad_frames % 100 == 0:
                self.engine.event("BAD_FRAME", message)

    def run_simulation(self):
        self.engine.begin_stream("SIMULATED - no Arduino")
        start = time.monotonic()
        seq = 0
        while not self.stop.is_set():
            elapsed = time.monotonic() - start
            raw = int(375 + 225 * math.cos(elapsed / 35))
            frame = parse_frame(
                json.dumps(
                    {
                        "v": 1,
                        "node": self.engine.config["node_id"],
                        "seq": seq,
                        "uptime_ms": int(elapsed * 1000) % 2**32,
                        "raw": raw,
                        "n": 9,
                        "min": raw - 2,
                        "max": raw + 2,
                    }
                ),
                self.engine.config["node_id"],
            )
            self.engine.ingest(frame)
            seq += 1
            self.stop.wait(5)
