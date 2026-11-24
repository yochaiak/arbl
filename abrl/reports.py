import csv
import html
import io
import json
import math
import statistics
from collections import Counter
from hashlib import sha256

from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import select

from abrl.db import Attempt, Run, Trial
from abrl.session_metadata import redact_session_metadata, redact_session_text


def trial_view(trial: Trial) -> dict:
    return {
        "id": trial.id,
        "ordinal": trial.ordinal,
        "status": trial.status,
        "outcome": trial.outcome,
        "elapsed_ms": trial.elapsed_ms,
        "error": redact_session_text(trial.error),
        "attempts": trial.attempts,
        "verification_status": "not_collected",
        "verdict": None,
        "environment": redact_session_metadata(trial.metadata_json),
    }


def wilson(successes: int, total: int) -> dict:
    if total == 0:
        return dict(successes=successes, total=total, rate=None, low=None, high=None)
    z = 1.959963984540054
    rate = successes / total
    scale = 1 + z * z / total
    center = (rate + z * z / (2 * total)) / scale
    margin = z * math.sqrt(rate * (1 - rate) / total + z * z / (4 * total**2)) / scale
    return dict(
        successes=successes,
        total=total,
        rate=rate,
        low=max(0.0, center - margin),
        high=min(1.0, center + margin),
    )


def run_view(run: Run) -> dict:
    return {
        "id": run.id,
        "target_id": run.target_id,
        "workflow_id": run.workflow_id,
        "target_version": run.target_version,
        "workflow_version": run.workflow_version,
        "target_name": run.target["name"],
        "workflow_name": run.workflow["name"],
        "target_mode": run.target["mode"],
        "mode": run.mode,
        "phase": run.phase,
        "status": run.status,
        "repetitions": run.repetitions,
        "seed": run.seed,
        "created_at": run.created_at,
        "finished_at": run.finished_at,
        "reliability_target": run.reliability_target,
    }


def report(session, run: Run) -> dict:
    trials = session.scalars(select(Trial).where(Trial.run_id == run.id).order_by(Trial.ordinal)).all()
    first = dict(
        session.execute(
            select(Attempt.trial_id, Attempt.outcome)
            .join(Trial, Trial.id == Attempt.trial_id)
            .where(Trial.run_id == run.id, Attempt.number == 1)
        ).all()
    )
    outcomes = Counter(trial.outcome for trial in trials)
    attempted = sum(t.status == "finished" for t in trials)
    successful = outcomes["completed"]
    success = wilson(successful, attempted)
    times = sorted(t.elapsed_ms for t in trials if t.elapsed_ms is not None and t.status == "finished")
    if run.mode == "demo":
        status, message = "simulated", "Simulated results cannot qualify a live workflow."
    elif run.status != "completed":
        status, message = "incomplete", "Finish the run before drawing a conclusion."
    elif run.phase != "confirmation":
        status, message = "pilot", "Use this pilot to plan a separate confirmation run."
    elif run.target["session_policy"] == "repeat_within_trial":
        status, message = (
            "insufficient",
            "Repeat visits may be correlated. Treat this interval as descriptive.",
        )
    elif success["low"] is not None and success["low"] >= run.reliability_target:
        status, message = (
            "qualified",
            "Observed workflow reliability meets the configured confidence threshold.",
        )
    else:
        status, message = (
            "insufficient",
            "The lower confidence bound falls short of the reliability target.",
        )
    snapshot = {
        "target": run.target,
        "workflow": run.workflow,
        "seed": run.seed,
        "phase": run.phase,
        "mode": run.mode,
        "repetitions": run.repetitions,
        "reliability_target": run.reliability_target,
    }
    return {
        "run_id": run.id,
        **run_view(run),
        "counts": dict(
            planned=run.repetitions,
            completed=successful,
            failed=outcomes["failed"],
            blocked=outcomes["blocked"],
            cancelled=outcomes["cancelled"],
            pending=outcomes[None],
            verified=0,
        ),
        "completion": success,
        "first_attempt": wilson(
            sum(first.get(t.id) == "completed" for t in trials if t.status == "finished"), attempted
        ),
        "latency_ms": {
            "p50": statistics.median(times) if times else None,
            "p95": times[math.ceil(0.95 * len(times)) - 1] if times else None,
        },
        "verdicts": {},
        "verification": {"not_collected": attempted},
        "cost_per_success": None,
        "recommendation": {"status": status, "message": message},
        "limitations": [
            "Workflow runs contain no attributed AdScore verdicts.",
            "Interval assumes independent logical trials.",
            "Costs have not been supplied.",
        ],
        "config_sha256": sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
        "snapshot": snapshot,
        "trials": [trial_view(t) for t in trials],
    }


def csv_export(data: dict) -> bytes:
    fields = (
        "run_id",
        "mode",
        "target_mode",
        "id",
        "ordinal",
        "status",
        "outcome",
        "elapsed_ms",
        "verification_status",
        "verdict",
        "error",
        "attempts",
    )
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for item in data["trials"]:
        row = {"run_id": data["run_id"], "mode": data["mode"], "target_mode": data["target_mode"], **item}
        writer.writerow(
            {
                key: (
                    "'" + value
                    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r")
                    else value
                )
                for key, value in row.items()
            }
        )
    return output.getvalue().encode()


def report_lines(data: dict) -> list[tuple[str, str]]:
    completion = data["completion"]
    interval = (
        f"{completion['low']:.1%}–{completion['high']:.1%}"
        if completion["low"] is not None
        else "Unavailable"
    )
    return [
        ("Source", "Simulated demo" if data["mode"] == "demo" else "Observed browser workflow"),
        ("Target / workflow", f"{data['target_name']} / {data['workflow_name']}"),
        ("Run", f"{data['run_id']} ({data['status']}, {data['phase']})"),
        ("Completion", f"{completion['successes']} / {completion['total']}"),
        ("95% Wilson interval", interval),
        ("Latency p50 / p95", f"{data['latency_ms']['p50']} / {data['latency_ms']['p95']} ms"),
        ("Recommendation", data["recommendation"]["message"]),
        ("AdScore", "No verdict is attributed to automated workflow trials."),
        ("Run configuration SHA-256", data["config_sha256"]),
    ]


def html_export(data: dict) -> bytes:
    rows = "".join(
        f"<tr><th>{html.escape(key)}</th><td>{html.escape(value)}</td></tr>"
        for key, value in report_lines(data)
    )
    return (
        '<!doctype html><meta charset="utf-8"><title>ABRL workflow report</title>'
        "<style>body{font:16px system-ui;margin:4rem;max-width:70rem}th,td{padding:.6rem;text-align:left;border-bottom:1px solid #bbb}</style>"
        f"<h1>ABRL workflow report</h1><table>{rows}</table>"
    ).encode()


def pdf_export(data: dict) -> bytes:
    buffer = io.BytesIO()
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(buffer, title="ABRL workflow report")
    rows = [
        [Paragraph(html.escape(k), styles["BodyText"]), Paragraph(html.escape(v), styles["BodyText"])]
        for k, v in report_lines(data)
    ]
    table = Table(rows, colWidths=[148, 300])
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.whitesmoke, colors.white]),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    doc.build([Paragraph("ABRL workflow report", styles["Title"]), Spacer(1, 12), table])
    return buffer.getvalue()
