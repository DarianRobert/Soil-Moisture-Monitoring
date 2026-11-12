import json
import math

from core import utc_now

POLICY_SCHEMA = """
CREATE TABLE IF NOT EXISTS dose_error_limit_changes (
 id INTEGER PRIMARY KEY, at TEXT NOT NULL, profile_id INTEGER NOT NULL,
 old_limit_ml REAL NOT NULL, new_limit_ml REAL NOT NULL,
 old_model_signature TEXT NOT NULL, new_model_signature TEXT NOT NULL,
 reason TEXT NOT NULL, backup_path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dose_check_reassessments (
 id INTEGER PRIMARY KEY, at TEXT NOT NULL, change_id INTEGER NOT NULL,
 profile_id INTEGER NOT NULL, run_id INTEGER NOT NULL,
 original_model_signature TEXT NOT NULL, current_model_signature TEXT NOT NULL,
 original_limit_ml REAL NOT NULL, current_limit_ml REAL NOT NULL,
 predicted_ml REAL NOT NULL, observed_ml REAL NOT NULL,
 absolute_error_ml REAL NOT NULL, original_passed INTEGER NOT NULL,
 accepted INTEGER NOT NULL, detail_json TEXT NOT NULL,
 UNIQUE(change_id,run_id)
);
"""

REASON = (
    "User requested a 2 mL allowed check error after reviewing the existing "
    "experimental results. Historical measurements and original check "
    "outcomes are retained. Reassessment is retrospective, not a new test."
)


def close(a, b):
    return (
        a is not None
        and b is not None
        and math.isclose(float(a), float(b), rel_tol=0, abs_tol=1e-8)
    )


def review_for(service, profile, model, check):
    if not check or check["status"] != "complete":
        return None
    rows = service.rows(
        """SELECT * FROM dose_check_reassessments
        WHERE profile_id=? AND run_id=? AND current_model_signature=?
        ORDER BY id DESC LIMIT 1""",
        (profile["id"], check["id"], model["signature"]),
    )
    if not rows:
        return None
    review = rows[0]
    if (
        review["original_model_signature"] != check["model_signature"]
        or not close(review["current_limit_ml"], profile["allowed_error_ml"])
        or any(
            not close(review[k], check[k])
            for k in ["predicted_ml", "observed_ml", "absolute_error_ml"]
        )
        or review["original_passed"] != check["passed"]
    ):
        return None
    return review


def assess_historical_check(service, profile, model, check):
    """Accept transfer only when the numerical predictor and saved evidence match."""
    from dose_amount import fitted_curve, interpolate

    if not check or check["status"] != "complete" or check["kind"] != "validation":
        return None, "No completed separate check is available to reassess."
    context = service.check_context(check["id"])
    if not context or not isinstance(context.get("allowed_error_ml"), (int, float)):
        return (
            None,
            "The old check has no saved original error criterion; a new check is needed.",
        )
    old_limit = context["allowed_error_ml"]
    if not math.isfinite(old_limit) or old_limit <= 0:
        return None, "The saved original error criterion is invalid."
    original_profile = {**profile, "allowed_error_ml": old_limit}
    original_model = service.model(original_profile)
    if original_model["signature"] != check["model_signature"]:
        return (
            None,
            "The training data or other profile settings changed; a new check is needed.",
        )
    points = service.points(check["id"])
    if (
        len(points) < 2
        or not close(points[0]["cumulative_ml"], 0)
        or not close(points[0]["index_value"], context.get("current_index"))
        or not close(profile["target_index"], context.get("target_index"))
    ):
        return None, "The saved baseline or target does not match the check evidence."
    try:
        candidate = service.candidate_details(profile, model, points[0]["index_value"])
        observed = interpolate(fitted_curve(points), profile["target_index"])
    except ValueError as exc:
        return None, str(exc)
    error = abs(candidate["predicted_ml"] - observed)
    if (
        not close(candidate["predicted_ml"], check["predicted_ml"])
        or not close(context.get("predicted_ml"), check["predicted_ml"])
        or not close(observed, check["observed_ml"])
        or not close(error, check["absolute_error_ml"])
        or check["passed"] != int(error <= old_limit + 1e-9)
    ):
        return (
            None,
            "The stored prediction or result does not reproduce; a new check is needed.",
        )
    return {
        "original_limit_ml": old_limit,
        "current_limit_ml": profile["allowed_error_ml"],
        "predicted_ml": candidate["predicted_ml"],
        "observed_ml": observed,
        "absolute_error_ml": error,
        "original_passed": check["passed"],
        "accepted": int(error <= profile["allowed_error_ml"] + 1e-9),
        "detail": {
            "reason": REASON,
            "training_run_ids": model["run_ids"],
            "numerical_prediction_unchanged": True,
            "candidate_details": candidate,
        },
    }, None


def inspect_profile(service, limit=2.0):
    profile = service.selected()
    if not profile or profile["source"] != "hardware":
        raise ValueError(
            "Select your hardware water-amount profile first. No settings changed."
        )
    if service.open_run():
        raise ValueError(
            "A training/check run is still open. Finish or end it before changing the limit."
        )
    if (
        not math.isfinite(limit)
        or not profile["resolution_ml"] <= limit <= profile["max_portion_ml"]
    ):
        raise ValueError(
            "The check-error limit must be between the measuring graduation and maximum portion."
        )
    return profile


def set_error_limit(service, backup_path, limit=2.0):
    """Called with the dashboard stopped and hardware process lock held."""
    profile = inspect_profile(service, limit)
    old_model = service.model(profile)
    updated = {**profile, "allowed_error_ml": limit}
    model = service.model(updated)
    checks = service.rows(
        "SELECT * FROM dose_runs WHERE profile_id=? AND kind='validation' ORDER BY id DESC LIMIT 1",
        (profile["id"],),
    )
    check = checks[0] if checks else None
    same_limit = close(profile["allowed_error_ml"], limit)
    has_review_table = bool(
        service.rows(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dose_check_reassessments'"
        )
    )
    existing = (
        review_for(service, updated, model, check)
        if same_limit and has_review_table
        else None
    )
    if same_limit and existing:
        return {
            "changed": False,
            "review": existing,
            "message": "The 2 mL limit and reassessment are already installed.",
        }
    reassessment, reason = assess_historical_check(service, updated, model, check)
    with service.e.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM dose_runs WHERE status='open' LIMIT 1").fetchone():
            raise ValueError(
                "A run opened during preparation. No limit change was made."
            )
        current = db.execute(
            "SELECT p.* FROM dose_profiles p JOIN dose_selection s ON s.profile_id=p.id WHERE s.id=1"
        ).fetchone()
        if not current or dict(current) != profile:
            raise ValueError(
                "The selected profile changed during preparation. No limit change was made."
            )
        for statement in POLICY_SCHEMA.split(";"):
            if statement.strip():
                db.execute(statement)
        # Never update historical dose_runs, dose_points or dose_check_context.
        db.execute(
            "UPDATE dose_profiles SET allowed_error_ml=? WHERE id=?",
            (limit, profile["id"]),
        )
        at = utc_now()
        cursor = db.execute(
            """INSERT INTO dose_error_limit_changes
            (at,profile_id,old_limit_ml,new_limit_ml,old_model_signature,new_model_signature,reason,backup_path)
            VALUES(?,?,?,?,?,?,?,?)""",
            (
                at,
                profile["id"],
                profile["allowed_error_ml"],
                limit,
                old_model["signature"],
                model["signature"],
                REASON,
                str(backup_path),
            ),
        )
        change_id = cursor.lastrowid
        if reassessment:
            db.execute(
                """INSERT INTO dose_check_reassessments
                (at,change_id,profile_id,run_id,original_model_signature,current_model_signature,
                 original_limit_ml,current_limit_ml,predicted_ml,observed_ml,absolute_error_ml,
                 original_passed,accepted,detail_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    at,
                    change_id,
                    profile["id"],
                    check["id"],
                    check["model_signature"],
                    model["signature"],
                    reassessment["original_limit_ml"],
                    limit,
                    reassessment["predicted_ml"],
                    reassessment["observed_ml"],
                    reassessment["absolute_error_ml"],
                    reassessment["original_passed"],
                    reassessment["accepted"],
                    json.dumps(reassessment["detail"], sort_keys=True),
                ),
            )
        db.execute(
            "INSERT INTO events(at,type,message,source) VALUES(?,?,?,?)",
            (
                at,
                "DOSE_ERROR_LIMIT",
                json.dumps(
                    {
                        "profile_id": profile["id"],
                        "old_limit_ml": profile["allowed_error_ml"],
                        "new_limit_ml": limit,
                        "reason": REASON,
                        "reviewed_run_id": check["id"] if reassessment else None,
                        "reassessment_accepted": bool(
                            reassessment and reassessment["accepted"]
                        ),
                    },
                    sort_keys=True,
                ),
                "hardware",
            ),
        )
    return {
        "changed": True,
        "review": review_for(service, updated, model, check),
        "message": reason
        or "Existing check reassessed; its original prediction and result are retained.",
    }
