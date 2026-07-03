"""Storage layer using Microsoft SQL Server (pyodbc)."""

import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pyodbc

from config import (
    APP_ROOT,
    APPLICATION_LOG_FILE,
    QUESTION_MEMORY_FILE,
    STATUS_FILE,
    get_daily_application_target,
    get_candidate_profile,
)


def _get_conn() -> pyodbc.Connection:
    conn_str = os.getenv("MSSQL_CONNECTION_STRING")
    if not conn_str:
        server = os.getenv("MSSQL_SERVER", "localhost")
        database = os.getenv("MSSQL_DATABASE", "auto_apply")
        driver = os.getenv("MSSQL_DRIVER", "ODBC Driver 17 for SQL Server")
        username = os.getenv("MSSQL_USERNAME", "")
        password = os.getenv("MSSQL_PASSWORD", "")
        if username and password:
            conn_str = (
                f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};"
                f"UID={username};PWD={password}"
            )
        else:
            conn_str = (
                f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};"
                f"Trusted_Connection=yes"
            )
    return pyodbc.connect(conn_str, autocommit=False)


# =============================================================================
# JSON File Helpers
# =============================================================================


def read_json_file(path: Path, default_value):
    if not path.exists():
        return default_value
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_value


def write_json_file(path: Path, payload):
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# =============================================================================
# Status Records
# =============================================================================


def load_status_record() -> dict:
    return read_json_file(STATUS_FILE, {})


def save_status_record(status: dict) -> None:
    write_json_file(STATUS_FILE, status)


def clear_status_record() -> None:
    try:
        STATUS_FILE.unlink(missing_ok=True)
    except OSError:
        pass


# =============================================================================
# Application History
# =============================================================================


def load_application_history() -> dict:
    return read_json_file(APPLICATION_LOG_FILE, {"applications": []})


def record_application_event(
    job_context: dict,
    status: str,
    reason: Optional[str] = None,
    artifacts: Optional[dict] = None,
) -> None:
    event = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "status": status,
        "reason": reason,
        "url": job_context.get("url"),
        "company_name": job_context.get("company_name"),
        "job_title": job_context.get("job_title"),
        "verified": job_context.get("verified"),
        "verification_source": job_context.get("verification_source"),
        "artifacts": artifacts or {},
    }

    history = load_application_history()
    history.setdefault("applications", []).append(event)
    write_json_file(APPLICATION_LOG_FILE, history)

    try:
        conn = _get_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO applications (
                timestamp, status, reason, url, company_name, job_title, verified,
                verification_source, score, source, artifacts_json, gap_analysis_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event["timestamp"],
                event["status"],
                event["reason"],
                event["url"],
                event["company_name"],
                event["job_title"],
                1 if event["verified"] else 0,
                event["verification_source"],
                job_context.get("score"),
                job_context.get("source", "runtime"),
                json.dumps(event["artifacts"]),
                json.dumps((artifacts or {}).get("gap_analysis"))
                if (artifacts or {}).get("gap_analysis")
                else None,
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def count_successful_applications_today() -> int:
    today = date.today().isoformat()
    try:
        conn = _get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM applications WHERE status = 'success' AND timestamp >= ?",
            (f"{today}T00:00:00",),
        )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception:
        pass

    history = load_application_history()
    return sum(
        1
        for entry in history.get("applications", [])
        if entry.get("status") == "success"
        and str(entry.get("timestamp", "")).startswith(today)
    )


def list_recent_application_events(limit: int = 100) -> list[dict]:
    try:
        conn = _get_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT TOP (?) timestamp, status, reason, url, company_name, job_title, verified,
                   verification_source, score, source, artifacts_json, gap_analysis_json
            FROM applications
            ORDER BY timestamp DESC
            """,
            (limit,),
        )
        rows = cursor.fetchall()
        columns = [col[0] for col in cursor.description]
        conn.close()

        return [
            {
                "timestamp": row[columns.index("timestamp")],
                "status": row[columns.index("status")],
                "reason": row[columns.index("reason")],
                "url": row[columns.index("url")],
                "company_name": row[columns.index("company_name")],
                "job_title": row[columns.index("job_title")],
                "verified": bool(row[columns.index("verified")]),
                "verification_source": row[columns.index("verification_source")],
                "score": row[columns.index("score")],
                "source": row[columns.index("source")],
                "artifacts": json.loads(row[columns.index("artifacts_json")] or "{}"),
                "gap_analysis": json.loads(row[columns.index("gap_analysis_json")] or "null"),
            }
            for row in rows
        ]
    except Exception:
        return []


# =============================================================================
# Question Memory
# =============================================================================


def normalize_question_text(question_text: str) -> str:
    return re.sub(r"\s+", " ", (question_text or "").strip().lower())


def load_question_memory() -> dict:
    memory_by_question: dict = {}

    file_memory = read_json_file(QUESTION_MEMORY_FILE, {"questions": []})
    for entry in file_memory.get("questions", []):
        normalized = normalize_question_text(entry.get("question"))
        if not normalized or not entry.get("answer"):
            continue
        memory_by_question[normalized] = {
            "question": entry.get("question", "").strip(),
            "answer": entry.get("answer", "").strip(),
            "source": entry.get("source", "file"),
            "updated_at": entry.get("updated_at", ""),
        }

    try:
        conn = _get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT normalized_question, question_text, answer_text, source, updated_at FROM question_memory"
        )
        rows = cursor.fetchall()
        conn.close()
        for row in rows:
            memory_by_question[row[0]] = {
                "question": row[1],
                "answer": row[2],
                "source": row[3],
                "updated_at": row[4],
            }
    except Exception:
        pass

    return memory_by_question


def save_question_memory(question_text: str, answer_text: str, source: str = "manual") -> None:
    normalized = normalize_question_text(question_text)
    if not normalized or not (answer_text or "").strip():
        return

    timestamp = datetime.now().isoformat(timespec="seconds")

    try:
        conn = _get_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            MERGE question_memory AS target
            USING (VALUES (?, ?, ?, ?, ?))
                AS source (normalized_question, question_text, answer_text, source, updated_at)
            ON target.normalized_question = source.normalized_question
            WHEN MATCHED THEN
                UPDATE SET
                    question_text = source.question_text,
                    answer_text   = source.answer_text,
                    source        = source.source,
                    updated_at    = source.updated_at
            WHEN NOT MATCHED THEN
                INSERT (normalized_question, question_text, answer_text, source, updated_at)
                VALUES (source.normalized_question, source.question_text,
                        source.answer_text, source.source, source.updated_at);
            """,
            (normalized, question_text.strip(), answer_text.strip(), source, timestamp),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

    memory = load_question_memory()
    questions = sorted(memory.values(), key=lambda item: item["question"].lower())
    write_json_file(QUESTION_MEMORY_FILE, {"questions": questions})


def build_question_memory_context() -> str:
    memory = load_question_memory()
    if not memory:
        return ""
    lines = ["Known reusable screening answers:"]
    for entry in sorted(memory.values(), key=lambda item: item["question"].lower()):
        lines.append(f"- {entry['question']}: {entry['answer']}")
    return "\n".join(lines)


# =============================================================================
# Initialization
# =============================================================================


def initialize_application_store() -> None:
    conn = _get_conn()
    cursor = conn.cursor()

    cursor.execute(
        """
        IF OBJECT_ID('dbo.applications', 'U') IS NULL
        CREATE TABLE applications (
            id                  INT IDENTITY(1,1) PRIMARY KEY,
            timestamp           NVARCHAR(50)  NOT NULL,
            status              NVARCHAR(50)  NOT NULL,
            reason              NVARCHAR(MAX),
            url                 NVARCHAR(MAX),
            company_name        NVARCHAR(500),
            job_title           NVARCHAR(500),
            verified            BIT,
            verification_source NVARCHAR(500),
            score               INT,
            source              NVARCHAR(100),
            artifacts_json      NVARCHAR(MAX) NOT NULL DEFAULT '{}',
            gap_analysis_json   NVARCHAR(MAX)
        )
        """
    )

    cursor.execute(
        """
        IF OBJECT_ID('dbo.question_memory', 'U') IS NULL
        CREATE TABLE question_memory (
            normalized_question NVARCHAR(1000) PRIMARY KEY,
            question_text       NVARCHAR(MAX)  NOT NULL,
            answer_text         NVARCHAR(MAX)  NOT NULL,
            source              NVARCHAR(100)  NOT NULL,
            updated_at          NVARCHAR(50)   NOT NULL
        )
        """
    )

    conn.commit()
    conn.close()


def print_daily_progress() -> None:
    successes = count_successful_applications_today()
    target = get_daily_application_target()
    print(f"[*] Daily verified application progress: {successes}/{target}")
