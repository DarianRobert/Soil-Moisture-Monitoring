import json
import math
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import jsonify, render_template, request

SAST = ZoneInfo("Africa/Johannesburg")
UTC = timezone.utc
WATER_NOTE = re.compile(r"^Water-amount (training|validation) run #(\d+)(?:\.|\s|$)")


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("A recorded timestamp has no timezone.")
    return result.astimezone(UTC)


def iso(value):
    return (
        value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def local_midnight(day):
    return datetime.combine(day, time.min, SAST)


def rows(db, query, args=()):
    return [dict(row) for row in db.execute(query, args)]


def water_kind(note):
    match = WATER_NOTE.match(note or "")
    if match:
        return "training" if match[1] == "training" else "checks"
    return "references" if (note or "").strip().lower() == "calibration" else "other"


def blank_day(day):
    return dict(
        date=day.isoformat(),
        volume_ml=0.0,
        watering_count=0,
        reading_count=0,
        recorded_seconds=0.0,
        dry_warning_seconds=0.0,
        categories=dict(training=0.0, checks=0.0, references=0.0, other=0.0),
    )


def split_interval(buckets, start, end, key):
    while start < end:
        day = start.astimezone(SAST).date()
        stop = min(end, local_midnight(day + timedelta(days=1)).astimezone(UTC))
        bucket = buckets.get(day.isoformat())
        if bucket is not None:
            bucket[key] += (stop - start).total_seconds()
        start = stop


def summarize_readings(records, buckets, max_gap):

    previous = None
    previous_at = None
    for row in records:
        at = timestamp(row["at"])
        bucket = buckets.get(at.astimezone(SAST).date().isoformat())
        if bucket is not None:
            bucket["reading_count"] += 1
        if previous is not None:
            elapsed = (at - previous_at).total_seconds()
            continuous = (
                0 < elapsed <= max_gap
                and row["stream_id"] == previous["stream_id"]
                and row["calibration_id"] == previous["calibration_id"]
                and row["seq"] == previous["seq"] + 1
            )
            if continuous:
                split_interval(buckets, previous_at, at, "recorded_seconds")
                if (
                    previous["state"] == "WATER_NEEDED"
                    and not previous["flags"]
                    and previous["moisture_index"] is not None
                ):
                    split_interval(buckets, previous_at, at, "dry_warning_seconds")
        previous, previous_at = row, at


def reduced_segments(records, max_gap, budget=1600):

    groups, group, previous = [], [], None
    for row in records:
        valid = row["moisture_index"] is not None
        continuous = (
            previous is not None
            and valid
            and previous["moisture_index"] is not None
            and previous["stream_id"] == row["stream_id"]
            and previous["calibration_id"] == row["calibration_id"]
            and row["seq"] == previous["seq"] + 1
            and 0
            < (timestamp(row["at"]) - timestamp(previous["at"])).total_seconds()
            <= max_gap
        )
        if not continuous and group:
            groups.append(group)
            group = []
        if valid:
            group.append(row)
        previous = row
    if group:
        groups.append(group)
    total = sum(map(len, groups))
    result = []
    for group in groups:
        width = max(1, math.ceil(total / budget) * 4)
        if total > budget:
            selected = []
            for offset in range(0, len(group), width):
                block = group[offset : offset + width]
                keep = {
                    0,
                    len(block) - 1,
                    min(range(len(block)), key=lambda i: block[i]["moisture_index"]),
                    max(range(len(block)), key=lambda i: block[i]["moisture_index"]),
                }
                selected.extend(block[i] for i in sorted(keep))
            group = selected
        result.append(
            [
                dict(
                    id=r["id"],
                    at=r["at"],
                    index=r["moisture_index"],
                    raw=r["raw"],
                    flags=r["flags"],
                )
                for r in group
            ]
        )
    return result


class Analytics:
    def __init__(self, store, source, max_gap=20):
        self.store, self.source, self.max_gap = store, source, max_gap

    def run_options(self, db, tables):
        if not {"dose_runs", "dose_profiles", "dose_points"} <= tables:
            return []
        superseded = ""
        if "dose_run_sources" in tables:
            superseded = "AND r.id NOT IN (SELECT source_run_id FROM dose_run_sources)"
        return rows(
            db,
            """SELECT r.id,r.at,r.kind,r.status,r.cumulative_ml,
            r.profile_id,p.name AS profile_name,p.target_index,p.low_index,
            p.calibration_id,COUNT(q.id) AS point_count
            FROM dose_runs r JOIN dose_profiles p ON p.id=r.profile_id
            JOIN dose_points q ON q.run_id=r.id
            WHERE p.source=? """
            + superseded
            + """
            GROUP BY r.id ORDER BY r.at,r.id""",
            (self.source,),
        )

    def predictions(self, db, tables, scope):
        result = []
        if not {"dose_runs", "dose_profiles", "dose_points"} <= tables:
            return result
        originals = rows(
            db,
            """SELECT r.*,p.name AS profile_name,p.target_index
            FROM dose_runs r JOIN dose_profiles p ON p.id=r.profile_id
            WHERE p.source=? AND r.kind='validation' AND r.status='complete'
            AND r.predicted_ml IS NOT NULL ORDER BY r.at,r.id""",
            (self.source,),
        )
        replays = []
        if "dose_prediction_replays" in tables:
            replays = rows(
                db,
                """SELECT a.* FROM dose_prediction_replays a
                JOIN dose_runs r ON r.id=a.run_id
                JOIN dose_profiles p ON p.id=r.profile_id
                WHERE p.source=? AND r.kind='validation' AND r.status='complete'
                ORDER BY a.at,a.id""",
                (self.source,),
            )
        for run in originals:
            captures = rows(
                db,
                "SELECT * FROM dose_points WHERE run_id=? ORDER BY cumulative_ml,id",
                (run["id"],),
            )
            if not captures:
                continue
            matches = (
                [r for r in replays if r["run_id"] == run["id"]]
                if scope == "recalculated"
                else [None]
            )
            for replay in matches:
                predicted = (
                    replay["replay_prediction_ml"] if replay else run["predicted_ml"]
                )
                actual = replay["actual_total_ml"] if replay else run["cumulative_ml"]
                if not all(
                    isinstance(n, (int, float)) and math.isfinite(n) and n >= 0
                    for n in (predicted, actual)
                ):
                    continue
                result.append(
                    dict(
                        run_id=run["id"],
                        replay_id=replay["id"] if replay else None,
                        profile_name=run["profile_name"],
                        at=replay["at"] if replay else run["at"],
                        starting_index=captures[0]["index_value"],
                        target_index=run["target_index"],
                        final_index=captures[-1]["index_value"],
                        predicted_ml=predicted,
                        actual_total_ml=actual,
                        difference_ml=abs(predicted - actual),
                        signed_difference_ml=predicted - actual,
                        target_observed_ml=replay["observed_ml"]
                        if replay
                        else run["observed_ml"],
                        stored_target_error_ml=replay["replay_error_ml"]
                        if replay
                        else run["absolute_error_ml"],
                        original_passed=run["passed"],
                        scope=scope,
                        model_signature=replay["current_model_signature"]
                        if replay
                        else run["model_signature"],
                    )
                )
        return result

    def watering_graph(self, db, tables, options, selected, anchor):
        run = next((r for r in options if str(r["id"]) == selected), None)
        if selected != "day" and run is None:
            raise ValueError("Choose an available watering run.")
        points = []
        if run is not None:
            points = rows(
                db,
                "SELECT * FROM dose_points WHERE run_id=? ORDER BY cumulative_ml,id",
                (run["id"],),
            )
            ids = {run["id"]}
            if "dose_run_sources" in tables:
                ids.update(
                    r[0]
                    for r in db.execute(
                        "SELECT source_run_id FROM dose_run_sources WHERE combined_run_id=?",
                        (run["id"],),
                    )
                )
            water = []
            for item in rows(
                db,
                "SELECT * FROM watering WHERE source=? ORDER BY at,id",
                (self.source,),
            ):
                match = WATER_NOTE.match(item["note"] or "")
                if match and int(match[2]) in ids:
                    water.append(item)
            first_ids = json.loads(points[0]["reading_ids_json"])
            start = timestamp(points[0]["at"])
            if first_ids:
                first = db.execute(
                    "SELECT MIN(at) FROM readings WHERE id IN ("
                    + ",".join("?" for _ in first_ids)
                    + ") AND source=?",
                    (*first_ids, self.source),
                ).fetchone()[0]
                if first:
                    start = min(start, timestamp(first))
            end = max(
                [timestamp(p["at"]) for p in points]
                + [timestamp(w["at"]) for w in water]
            )
            if run["status"] == "open":
                latest = db.execute(
                    "SELECT MAX(at) FROM readings WHERE source=?", (self.source,)
                ).fetchone()[0]
                if latest:
                    end = max(end, timestamp(latest))
            calibration_id = run["calibration_id"]
            readings = rows(
                db,
                """SELECT id,at,raw,moisture_index,flags,stream_id,seq,calibration_id
                FROM readings WHERE source=? AND at>=? AND at<=? AND calibration_id=? ORDER BY at,id""",
                (self.source, iso(start), iso(end), calibration_id),
            )
        else:
            start, end = (
                local_midnight(anchor),
                local_midnight(anchor + timedelta(days=1)),
            )
            water = rows(
                db,
                "SELECT * FROM watering WHERE source=? AND at>=? AND at<? ORDER BY at,id",
                (self.source, iso(start), iso(end)),
            )
            readings = rows(
                db,
                """SELECT id,at,raw,moisture_index,flags,stream_id,seq,calibration_id
                FROM readings WHERE source=? AND at>=? AND at<? ORDER BY at,id""",
                (self.source, iso(start), iso(end)),
            )
        additions = [
            dict(
                id=w["id"],
                at=w["at"],
                volume_ml=w["volume_ml"],
                index=w["index_before"],
                raw=w["raw_before"],
                category=water_kind(w["note"]),
            )
            for w in water
        ]
        captures = [
            dict(
                id=p["id"],
                at=p["at"],
                index=p["index_value"],
                raw=p["raw_median"],
                cumulative_ml=p["cumulative_ml"],
            )
            for p in points
        ]
        return dict(
            run=run,
            selected=selected,
            start=iso(start),
            end=iso(end),
            segments=reduced_segments(readings, self.max_gap),
            reading_count=len(readings),
            additions=additions,
            captures=captures,
            water_total_ml=sum(w["volume_ml"] for w in water),
            target_index=run["target_index"] if run else None,
        )

    def payload(self, selected_date=None, selected_run=None, scope=None):
        with self.store.connect() as db:
            db.execute("BEGIN")
            tables = {
                r[0]
                for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            latest = db.execute(
                """SELECT MAX(at) FROM (
                SELECT MAX(at) AS at FROM readings WHERE source=?
                UNION ALL SELECT MAX(at) AS at FROM watering WHERE source=?)""",
                (self.source, self.source),
            ).fetchone()[0]
            try:
                anchor = (
                    date.fromisoformat(selected_date)
                    if selected_date
                    else (
                        timestamp(latest).astimezone(SAST).date()
                        if latest
                        else datetime.now(SAST).date()
                    )
                )
            except (ValueError, TypeError):
                raise ValueError("Choose a valid summary date (YYYY-MM-DD).") from None
            monday = anchor - timedelta(days=anchor.weekday())
            start, end = (
                local_midnight(monday),
                local_midnight(monday + timedelta(days=7)),
            )
            buckets = {
                d.isoformat(): blank_day(d)
                for d in [monday + timedelta(days=i) for i in range(7)]
            }
            readings = rows(
                db,
                """SELECT id,at,stream_id,seq,calibration_id,moisture_index,state,flags
                FROM readings WHERE source=? AND at>=? AND at<? ORDER BY at,id""",
                (self.source, iso(start), iso(end)),
            )
            before = rows(
                db,
                """SELECT id,at,stream_id,seq,calibration_id,moisture_index,state,flags
                FROM readings WHERE source=? AND at<? ORDER BY at DESC,id DESC LIMIT 1""",
                (self.source, iso(start)),
            )
            after = rows(
                db,
                """SELECT id,at,stream_id,seq,calibration_id,moisture_index,state,flags
                FROM readings WHERE source=? AND at>=? ORDER BY at,id LIMIT 1""",
                (self.source, iso(end)),
            )
            summarize_readings(before + readings + after, buckets, self.max_gap)
            for water in rows(
                db,
                "SELECT at,volume_ml,note FROM watering WHERE source=? AND at>=? AND at<? ORDER BY at,id",
                (self.source, iso(start), iso(end)),
            ):
                bucket = buckets[
                    timestamp(water["at"]).astimezone(SAST).date().isoformat()
                ]
                bucket["volume_ml"] += water["volume_ml"]
                bucket["watering_count"] += 1
                bucket["categories"][water_kind(water["note"])] += water["volume_ml"]
            week = dict(
                volume_ml=0.0,
                watering_count=0,
                reading_count=0,
                recorded_seconds=0.0,
                dry_warning_seconds=0.0,
            )
            for bucket in buckets.values():
                for key in week:
                    week[key] += bucket[key]
            options = self.run_options(db, tables)
            if not selected_run:
                candidates = [
                    r
                    for r in options
                    if r["kind"] == "validation" and r["status"] == "complete"
                ]
                if not candidates:
                    candidates = [r for r in options if r["status"] == "complete"]
                selected_run = str(candidates[-1]["id"]) if candidates else "day"
            available_replays = (
                db.execute(
                    """SELECT COUNT(*) FROM dose_prediction_replays a
                JOIN dose_runs r ON r.id=a.run_id JOIN dose_profiles p ON p.id=r.profile_id
                WHERE p.source=? AND r.kind='validation' AND r.status='complete'""",
                    (self.source,),
                ).fetchone()[0]
                if "dose_prediction_replays" in tables
                else 0
            )
            scope = scope or ("recalculated" if available_replays else "recorded")
            if scope not in {"recorded", "recalculated"}:
                raise ValueError("Choose recorded checks or saved recalculations.")
            predictions = self.predictions(db, tables, scope)
            return dict(
                source=self.source,
                timezone="Africa/Johannesburg",
                latest_recorded_at=latest,
                selected_date=anchor.isoformat(),
                week_start=monday.isoformat(),
                week_end=(monday + timedelta(days=6)).isoformat(),
                day=buckets[anchor.isoformat()],
                week=week,
                days=list(buckets.values()),
                run_options=options,
                watering=self.watering_graph(db, tables, options, selected_run, anchor),
                prediction_scope=scope,
                predictions=predictions,
                prediction_summary=dict(
                    count=len(predictions),
                    mean_difference_ml=sum(r["difference_ml"] for r in predictions)
                    / len(predictions)
                    if predictions
                    else None,
                    maximum_difference_ml=max(
                        (r["difference_ml"] for r in predictions), default=None
                    ),
                ),
            )


def install_analytics(app, engine, csrf):
    service = Analytics(engine.store, engine.source, engine.config["stale_seconds"])
    app.extensions["garden_analytics"] = service

    @app.get("/history")
    def garden_history():
        return render_template("history.html", csrf=csrf)

    @app.get("/api/analytics")
    def garden_analytics():
        return jsonify(
            service.payload(
                request.args.get("date"),
                request.args.get("run"),
                request.args.get("scope"),
            )
        )
