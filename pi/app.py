#!/usr/bin/env python3

import argparse
import hmac
import json
import logging
import os
import secrets
import signal
import sys
import threading
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    stream_with_context,
)
from werkzeug.exceptions import HTTPException
from waitress import serve

from collector import Collector
from core import Engine, utc_now
from store import Store

BASE = Path(__file__).resolve().parent


def load_config(path):
    defaults = json.loads((BASE / "config.example.json").read_text())
    if path.exists():
        user = json.loads(path.read_text())
        if not isinstance(user, dict) or set(user) - set(defaults):
            raise ValueError(
                "Unknown configuration keys or invalid configuration object"
            )
        defaults.update(user)
    if defaults["baudrate"] != 115200 or defaults["node_id"] != "SOIL-01":
        raise ValueError("This firmware requires baudrate 115200 and node_id SOIL-01")
    if not isinstance(defaults["serial_port"], str) or not defaults["serial_port"]:
        raise ValueError("serial_port must be auto or an explicit device path")
    if defaults["host"] not in ("127.0.0.1", "0.0.0.0"):
        raise ValueError("host must be 127.0.0.1 or 0.0.0.0")
    ranges = {
        "port": (1024, 65535),
        "stale_seconds": (15, 120),
        "capture_samples": (12, 60),
        "capture_max_age_seconds": (90, 600),
        "capture_max_spread_adc": (1, 100),
        "minimum_calibration_span_adc": (50, 1023),
        "confirmation_samples": (3, 12),
        "soak_seconds": (60, 3600),
    }
    for key, (low, high) in ranges.items():
        if type(defaults[key]) is not int or not low <= defaults[key] <= high:
            raise ValueError(f"{key} must be an integer between {low} and {high}")
    if defaults["capture_max_age_seconds"] < 5 * defaults["capture_samples"]:
        raise ValueError(
            "capture_max_age_seconds must allow all capture samples at 5 second intervals"
        )
    return defaults


def create_app(engine, password):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 8192
    csrf = secrets.token_hex(24)

    @app.before_request
    def protect():
        auth = request.authorization
        if (
            not auth
            or auth.username != "garden"
            or not hmac.compare_digest(
                (auth.password or "").encode("utf-8"), password.encode("utf-8")
            )
        ):
            return Response(
                "Sign in with username garden and the password in pi/data/access_token.txt.\n",
                401,
                {"WWW-Authenticate": 'Basic realm="COMP717A Garden"'},
            )
        if request.method != "GET" and not hmac.compare_digest(
            request.headers.get("X-Garden-CSRF", ""), csrf
        ):
            return jsonify(
                error="Reload this dashboard before submitting the form."
            ), 403

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'"
        )
        return response

    @app.errorhandler(ValueError)
    def invalid(exc):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(Exception)
    def failed(exc):
        if isinstance(exc, HTTPException):
            return jsonify(error=exc.description), exc.code
        app.logger.exception("Request failed")
        return jsonify(
            error="Operation failed. Check the terminal/service log and disk space."
        ), 500

    def action(handler):
        @wraps(handler)
        def wrapped():
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object")
            with engine.lock:
                result = handler(data)
            return jsonify(ok=True, result=result)

        return wrapped

    @app.get("/")
    def index():
        return render_template("index.html", csrf=csrf)

    @app.get("/api/status")
    def status():
        return jsonify(engine.snapshot())

    @app.get("/api/history")
    def history():
        return jsonify(engine.store.history())

    @app.post("/api/capture")
    @action
    def capture(data):
        return engine.capture(data.get("kind"), data.get("note"))

    @app.post("/api/calibration")
    @action
    def calibration(data):
        engine.activate(data.get("dry_id"), data.get("wet_id"), data.get("note"))

    @app.post("/api/settings")
    @action
    def settings(data):
        engine.configure(data)

    @app.post("/api/watering")
    @action
    def watering(data):
        engine.water(data)

    @app.post("/api/observation")
    @action
    def observation(data):
        engine.observation(data)

    @app.get("/api/export/<name>")
    def export(name):
        if name == "configuration.json":
            with engine.lock:
                payload = {
                    "exported_at": utc_now(),
                    "source": engine.source,
                    "runtime": engine.config,
                    "settings": engine.settings,
                    "calibration": engine.calibration,
                }
            return Response(
                json.dumps(payload, indent=2),
                mimetype="application/json",
                headers={
                    "Content-Disposition": 'attachment; filename="configuration.json"'
                },
            )
        table = name.removesuffix(".csv")
        allowed = {
            "readings",
            "watering",
            "events",
            "observations",
            "calibrations",
            "captures",
        }
        if not name.endswith(".csv") or table not in allowed:
            raise ValueError("Unknown export")
        return Response(
            stream_with_context(engine.store.export_csv(table)),
            mimetype="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{engine.source}_{table}.csv"'
            },
        )

    # WATER_AMOUNT_SUPPORT v1
    from dose_amount import install_dose_support

    install_dose_support(app, engine, action)
    from analytics import install_analytics

    install_analytics(app, engine, csrf)
    return app


def local_secret(path):
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write(secrets.token_urlsafe(18) + "\n")
    os.chmod(path, 0o600)
    secret = path.read_text().strip()
    if len(secret) < 12:
        raise ValueError("Password file must contain at least 12 characters")
    return secret


def main():
    parser = argparse.ArgumentParser(
        description="COMP717A moisture monitoring and manual watering log"
    )
    parser.add_argument(
        "--simulate", action="store_true", help="SIMULATED data in a separate database"
    )
    parser.add_argument("--config", type=Path, default=BASE / "config.json")
    parser.add_argument("--host", choices=["127.0.0.1", "0.0.0.0"])
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    config = load_config(args.config)
    if args.host:
        config["host"] = args.host
    if args.port is not None:
        if not 1024 <= args.port <= 65535:
            parser.error("port must be between 1024 and 65535")
        config["port"] = args.port
    data = BASE / "data"
    data.mkdir(mode=0o700, exist_ok=True)
    source = "simulated" if args.simulate else "hardware"
    # One process per mode; no duplicate collectors writing to the same database.
    lock_file = (data / f"{source}.lock").open("w")
    if os.name == "posix":
        import fcntl

        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(
                "This mode is already running. Stop the other app/service first."
            )
    store = Store(data / ("demo.sqlite3" if args.simulate else "garden.sqlite3"))
    engine = Engine(store, config, source=source)
    if args.simulate and engine.calibration is None:
        for kind, raw in [("dry", 150), ("wet", 650)]:
            store.insert(
                "captures",
                {
                    "at": utc_now(),
                    "kind": kind,
                    "raw": raw,
                    "spread": 0,
                    "sd": 0,
                    "samples_json": json.dumps([raw] * 12),
                    "note": "SIMULATED reference for software practice only",
                    "source": source,
                },
            )
        refs = store.latest_captures()
        engine.activate(
            refs["dry"]["id"], refs["wet"]["id"], "SIMULATED software test profile"
        )
        engine.configure(dict(engine.settings, thresholds_confirmed=True))
    password = local_secret(data / "access_token.txt")
    stop = threading.Event()
    collector = Collector(engine, stop, simulate=args.simulate)
    app = create_app(engine, password)

    def shutdown(signum=None, frame=None):
        stop.set()
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    collector.start()
    print(f"Mode: {source.upper()}. Database: {store.path}", flush=True)
    print(
        f"Open http://127.0.0.1:{config['port']} on the Pi. Username: garden",
        flush=True,
    )
    print(f"Password: read {data / 'access_token.txt'}", flush=True)
    if config["host"] == "0.0.0.0":
        print(
            "LAN access enabled. Use the Pi's IP address on your trusted local network.",
            flush=True,
        )
    try:
        serve(app, host=config["host"], port=config["port"], threads=4)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        collector.join(timeout=4)
        lock_file.close()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        sys.exit(f"Setup error: {exc}")
