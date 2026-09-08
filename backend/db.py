"""
Database module for Celito Onboarding Platform.

SQLite with WAL mode, busy_timeout=60000.
Database file lives at PROJECT_DIR/app.db (one level above backend/).
"""

import json
import logging
import os
import secrets
import sqlite3
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB_PATH = os.path.join(PROJECT_DIR, "app.db")

# ──────────────────────────────────────────────────────────────────────
# Connection helper
# ──────────────────────────────────────────────────────────────────────

def get_db(path=None):
    """Return a new SQLite connection with WAL mode and row factory."""
    db_path = path or DB_PATH
    conn = sqlite3.connect(db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ──────────────────────────────────────────────────────────────────────
# Schema creation
# ──────────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT    NOT NULL UNIQUE,
    display_name    TEXT    NOT NULL DEFAULT '',
    role            TEXT    NOT NULL DEFAULT 'employee'
                        CHECK(role IN ('admin','manager','hr','employee','disabled')),
    department      TEXT    DEFAULT '',
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    last_login      TEXT
);

CREATE TABLE IF NOT EXISTS onboarding_cohorts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    start_week      TEXT,
    slack_channel   TEXT    DEFAULT '',
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS employees (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    first_name            TEXT    NOT NULL,
    last_name             TEXT    NOT NULL,
    email                 TEXT    NOT NULL,
    phone                 TEXT    DEFAULT '',
    role_title            TEXT    DEFAULT '',
    department            TEXT    DEFAULT '',
    manager_email         TEXT    DEFAULT '',
    start_date            TEXT,
    end_date              TEXT,
    location              TEXT    DEFAULT '',
    status                TEXT    NOT NULL DEFAULT 'pending'
                              CHECK(status IN ('pending','active','offboarding','departed')),
    hire_type             TEXT    DEFAULT 'full-time',
    created_at            TEXT    NOT NULL DEFAULT (datetime('now')),
    created_by            TEXT    DEFAULT '',
    salesforce_contact_id TEXT    DEFAULT '',
    salesforce_case_id    TEXT    DEFAULT '',
    buddy_email           TEXT    DEFAULT '',
    hr_owner_email        TEXT    DEFAULT '',
    cohort_id             INTEGER REFERENCES onboarding_cohorts(id),
    location_mode         TEXT    NOT NULL DEFAULT 'in-office'
                              CHECK(location_mode IN ('in-office','remote','hybrid')),
    ramped_at             TEXT,
    ramped_by             TEXT    DEFAULT '',
    shipping_address      TEXT    DEFAULT ''
);

CREATE TABLE IF NOT EXISTS checklists (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id     INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    name            TEXT    NOT NULL,
    checklist_type  TEXT    NOT NULL CHECK(checklist_type IN ('onboarding','offboarding')),
    status          TEXT    NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','completed','cancelled')),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    completed_at    TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    checklist_id        INTEGER NOT NULL REFERENCES checklists(id) ON DELETE CASCADE,
    employee_id         INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    title               TEXT    NOT NULL,
    description         TEXT    DEFAULT '',
    category            TEXT    NOT NULL DEFAULT 'hr'
                            CHECK(category IN ('it','hr','manager','facilities','training','voice','sales','employee')),
    assigned_to         TEXT    DEFAULT '',
    assigned_email      TEXT    DEFAULT '',
    due_date            TEXT,
    status              TEXT    NOT NULL DEFAULT 'pending'
                            CHECK(status IN ('pending','in_progress','completed','skipped','blocked','pending_review')),
    completed_at        TEXT,
    completed_by        TEXT    DEFAULT '',
    sort_order          INTEGER DEFAULT 0,
    salesforce_task_id  TEXT    DEFAULT '',
    parent_task_id      INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on_task_id  INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    phase               TEXT    DEFAULT 'company_onboarding'
                            CHECK(phase IN ('pre_boarding','company_onboarding',
                                            'department_onboarding','role_training','offboarding')),
    due_offset_days     INTEGER,
    conditions          TEXT    DEFAULT '',
    location_mode       TEXT    NOT NULL DEFAULT 'all'
                            CHECK(location_mode IN ('in-office','remote','hybrid','all')),
    is_acknowledgment   INTEGER NOT NULL DEFAULT 0,
    is_security_critical INTEGER NOT NULL DEFAULT 0,
    compliance_required INTEGER NOT NULL DEFAULT 0,
    urgency             TEXT    NOT NULL DEFAULT 'normal'
                            CHECK(urgency IN ('normal','urgent','immediate')),
    resource_url        TEXT    DEFAULT ''
);

CREATE TABLE IF NOT EXISTS task_comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    user_email  TEXT    NOT NULL,
    comment     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_email  TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    target_type TEXT    DEFAULT '',
    target_id   TEXT    DEFAULT '',
    details     TEXT    DEFAULT '{}',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS equipment (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id     INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    item_type       TEXT    NOT NULL
                        CHECK(item_type IN ('laptop','desktop','mac','phone','monitors',
                                            'headphones','camera','desk','chair',
                                            'stationery','other')),
    model           TEXT    DEFAULT '',
    serial_number   TEXT    DEFAULT '',
    notes           TEXT    DEFAULT '',
    issued_date     TEXT,
    returned_date   TEXT,
    tracking_number TEXT    DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','ordered','shipped','delivered',
                                         'issued','returned')),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS milestones (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id     INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    day_marker      INTEGER NOT NULL CHECK(day_marker > 0),
    title           TEXT    NOT NULL,
    expectations    TEXT    DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','in_progress','achieved','missed')),
    notes           TEXT    DEFAULT '',
    reviewed_by     TEXT    DEFAULT '',
    reviewed_at     TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS checklist_templates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    department      TEXT,
    checklist_type  TEXT    NOT NULL CHECK(checklist_type IN ('onboarding','offboarding')),
    phase           TEXT    CHECK(phase IN ('pre_boarding','company_onboarding',
                                           'department_onboarding','role_training')),
    tasks_json      TEXT    NOT NULL DEFAULT '[]',
    location_mode   TEXT    NOT NULL DEFAULT 'all'
                        CHECK(location_mode IN ('in-office','remote','hybrid','all')),
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS surveys (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id     INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    survey_type     TEXT    NOT NULL
                        CHECK(survey_type IN ('day_7','day_30','day_90','exit_program')),
    sent_at         TEXT,
    completed_at    TEXT,
    status          TEXT    NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','scheduled','sent','completed','skipped'))
);

CREATE TABLE IF NOT EXISTS survey_responses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    survey_id       INTEGER NOT NULL REFERENCES surveys(id) ON DELETE CASCADE,
    question        TEXT    NOT NULL,
    answer          TEXT    DEFAULT '',
    rating          INTEGER CHECK(rating IS NULL OR (rating >= 1 AND rating <= 5)),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meet_greets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id     INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    contact_name    TEXT    NOT NULL,
    contact_email   TEXT    NOT NULL,
    contact_role    TEXT    DEFAULT '',
    reason          TEXT    DEFAULT '',
    scheduled_date  TEXT,
    completed       INTEGER NOT NULL DEFAULT 0,
    completed_at    TEXT,
    week_number     INTEGER CHECK(week_number IS NULL OR (week_number >= 1 AND week_number <= 4))
);

CREATE TABLE IF NOT EXISTS buddy_tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id     INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    buddy_email     TEXT    NOT NULL,
    title           TEXT    NOT NULL,
    description     TEXT    DEFAULT '',
    due_offset_days INTEGER,
    status          TEXT    NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','in_progress','completed','skipped')),
    completed_at    TEXT
);

CREATE TABLE IF NOT EXISTS metric_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date   TEXT    NOT NULL,
    metric_name     TEXT    NOT NULL,
    metric_value    REAL    NOT NULL,
    metadata        TEXT    DEFAULT '{}',
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(snapshot_date, metric_name)
);
"""

# Indexes for common queries
INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_employees_status       ON employees(status);
CREATE INDEX IF NOT EXISTS idx_employees_email        ON employees(email);
CREATE INDEX IF NOT EXISTS idx_employees_manager      ON employees(manager_email);
CREATE INDEX IF NOT EXISTS idx_employees_cohort       ON employees(cohort_id);
CREATE INDEX IF NOT EXISTS idx_checklists_employee    ON checklists(employee_id);
CREATE INDEX IF NOT EXISTS idx_tasks_checklist        ON tasks(checklist_id);
CREATE INDEX IF NOT EXISTS idx_tasks_employee         ON tasks(employee_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status           ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_assigned         ON tasks(assigned_email);
CREATE INDEX IF NOT EXISTS idx_tasks_parent           ON tasks(parent_task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_depends          ON tasks(depends_on_task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_phase            ON tasks(phase);
CREATE INDEX IF NOT EXISTS idx_audit_log_action       ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_audit_log_created      ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_equipment_employee     ON equipment(employee_id);
CREATE INDEX IF NOT EXISTS idx_milestones_employee    ON milestones(employee_id);
CREATE INDEX IF NOT EXISTS idx_surveys_employee       ON surveys(employee_id);
CREATE INDEX IF NOT EXISTS idx_meet_greets_employee   ON meet_greets(employee_id);
CREATE INDEX IF NOT EXISTS idx_buddy_tasks_employee   ON buddy_tasks(employee_id);
CREATE INDEX IF NOT EXISTS idx_buddy_tasks_buddy      ON buddy_tasks(buddy_email);
CREATE INDEX IF NOT EXISTS idx_checklist_templates_dept ON checklist_templates(department);
CREATE INDEX IF NOT EXISTS idx_metric_snapshots_date   ON metric_snapshots(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_metric_snapshots_name   ON metric_snapshots(metric_name);
"""


def init_db():
    """Create all tables and indexes if they don't exist."""
    conn = get_db()
    try:
        conn.executescript(SCHEMA_SQL)
        conn.executescript(INDEX_SQL)
        conn.commit()
        logger.info("Database initialized at %s", DB_PATH)
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────
# Migrations (for adding columns to existing databases)
# ──────────────────────────────────────────────────────────────────────

MIGRATIONS = [
    # employees table additions
    ("001_emp_buddy_email",     "ALTER TABLE employees ADD COLUMN buddy_email TEXT DEFAULT ''"),
    ("002_emp_hr_owner_email",  "ALTER TABLE employees ADD COLUMN hr_owner_email TEXT DEFAULT ''"),
    ("003_emp_cohort_id",       "ALTER TABLE employees ADD COLUMN cohort_id INTEGER REFERENCES onboarding_cohorts(id)"),
    ("004_emp_location_mode",   "ALTER TABLE employees ADD COLUMN location_mode TEXT NOT NULL DEFAULT 'in-office'"),
    ("005_emp_ramped_at",       "ALTER TABLE employees ADD COLUMN ramped_at TEXT"),
    ("006_emp_ramped_by",       "ALTER TABLE employees ADD COLUMN ramped_by TEXT DEFAULT ''"),
    # tasks table additions
    ("010_task_parent_id",      "ALTER TABLE tasks ADD COLUMN parent_task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE"),
    ("011_task_depends_on",     "ALTER TABLE tasks ADD COLUMN depends_on_task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL"),
    ("012_task_phase",          "ALTER TABLE tasks ADD COLUMN phase TEXT DEFAULT 'company_onboarding'"),
    ("013_task_due_offset",     "ALTER TABLE tasks ADD COLUMN due_offset_days INTEGER"),
    ("014_task_conditions",     "ALTER TABLE tasks ADD COLUMN conditions TEXT DEFAULT ''"),
    ("015_task_location_mode",  "ALTER TABLE tasks ADD COLUMN location_mode TEXT NOT NULL DEFAULT 'all'"),
    ("016_task_is_ack",         "ALTER TABLE tasks ADD COLUMN is_acknowledgment INTEGER NOT NULL DEFAULT 0"),
    ("017_task_security",       "ALTER TABLE tasks ADD COLUMN is_security_critical INTEGER NOT NULL DEFAULT 0"),
    ("018_task_compliance",     "ALTER TABLE tasks ADD COLUMN compliance_required INTEGER NOT NULL DEFAULT 0"),
    ("019_task_urgency",        "ALTER TABLE tasks ADD COLUMN urgency TEXT NOT NULL DEFAULT 'normal'"),
    ("020_task_resource_url",   "ALTER TABLE tasks ADD COLUMN resource_url TEXT DEFAULT ''"),
    ("021_emp_shipping_addr",   "ALTER TABLE employees ADD COLUMN shipping_address TEXT DEFAULT ''"),
    # Relax milestones day_marker CHECK from IN (30,60,90) to > 0
    # Timezone support for users and employees
    ("023_users_timezone",     "ALTER TABLE users ADD COLUMN timezone TEXT DEFAULT 'America/Los_Angeles'"),
    ("024_employees_timezone", "ALTER TABLE employees ADD COLUMN timezone TEXT DEFAULT 'America/Los_Angeles'"),
    # Add suggested_time to meet_greets for timezone-aware scheduling
    ("025_meet_greets_suggested_time", "ALTER TABLE meet_greets ADD COLUMN suggested_time TEXT DEFAULT ''"),
    # Rehire / boomerang workflow
    ("026_emp_is_rehire",          "ALTER TABLE employees ADD COLUMN is_rehire INTEGER NOT NULL DEFAULT 0"),
    ("027_emp_previous_end_date",  "ALTER TABLE employees ADD COLUMN previous_end_date TEXT DEFAULT ''"),
    # Expand tasks.status CHECK to include 'pending_review' for rehire workflow
    ("028_tasks_pending_review_status", """
        CREATE TABLE IF NOT EXISTS tasks_new (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            checklist_id        INTEGER NOT NULL REFERENCES checklists(id) ON DELETE CASCADE,
            employee_id         INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
            title               TEXT    NOT NULL,
            description         TEXT    DEFAULT '',
            category            TEXT    NOT NULL DEFAULT 'hr'
                                    CHECK(category IN ('it','hr','manager','facilities','training','voice','sales','employee')),
            assigned_to         TEXT    DEFAULT '',
            assigned_email      TEXT    DEFAULT '',
            due_date            TEXT,
            status              TEXT    NOT NULL DEFAULT 'pending'
                                    CHECK(status IN ('pending','in_progress','completed','skipped','blocked','pending_review')),
            completed_at        TEXT,
            completed_by        TEXT    DEFAULT '',
            sort_order          INTEGER DEFAULT 0,
            salesforce_task_id  TEXT    DEFAULT '',
            parent_task_id      INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
            depends_on_task_id  INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
            phase               TEXT    DEFAULT 'company_onboarding'
                                    CHECK(phase IN ('pre_boarding','company_onboarding',
                                                    'department_onboarding','role_training','offboarding')),
            due_offset_days     INTEGER,
            conditions          TEXT    DEFAULT '',
            location_mode       TEXT    NOT NULL DEFAULT 'all'
                                    CHECK(location_mode IN ('in-office','remote','hybrid','all')),
            is_acknowledgment   INTEGER NOT NULL DEFAULT 0,
            is_security_critical INTEGER NOT NULL DEFAULT 0,
            compliance_required INTEGER NOT NULL DEFAULT 0,
            urgency             TEXT    NOT NULL DEFAULT 'normal'
                                    CHECK(urgency IN ('normal','urgent','immediate')),
            resource_url        TEXT    DEFAULT ''
        );
        INSERT OR IGNORE INTO tasks_new SELECT * FROM tasks;
        DROP TABLE tasks;
        ALTER TABLE tasks_new RENAME TO tasks;
    """),
    # Task multi-dependency support (comma-separated IDs, supplements depends_on_task_id)
    ("028_task_depends_on_multi",  "ALTER TABLE tasks ADD COLUMN depends_on TEXT DEFAULT ''"),
    # External API key table
    ("029_api_keys_table", """
        CREATE TABLE IF NOT EXISTS api_keys (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            key_value   TEXT    UNIQUE NOT NULL,
            name        TEXT    NOT NULL,
            created_by  TEXT    NOT NULL,
            created_at  TEXT    DEFAULT (datetime('now')),
            is_active   INTEGER DEFAULT 1,
            last_used   TEXT
        )
    """),
    ("022_milestones_relax_check", """
        CREATE TABLE IF NOT EXISTS milestones_new (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id     INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
            day_marker      INTEGER NOT NULL CHECK(day_marker > 0),
            title           TEXT    NOT NULL,
            expectations    TEXT    DEFAULT '',
            status          TEXT    NOT NULL DEFAULT 'pending'
                                CHECK(status IN ('pending','in_progress','achieved','missed')),
            notes           TEXT    DEFAULT '',
            reviewed_by     TEXT    DEFAULT '',
            reviewed_at     TEXT,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        INSERT OR IGNORE INTO milestones_new SELECT * FROM milestones;
        DROP TABLE milestones;
        ALTER TABLE milestones_new RENAME TO milestones;
    """),
]


def migrate_db():
    """Run any pending migrations. Each migration runs at most once.
    ALTER TABLE ADD COLUMN is idempotent-safe via try/except per migration."""
    conn = get_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS migrations (
                name       TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()

        applied = {row["name"] for row in
                   conn.execute("SELECT name FROM migrations").fetchall()}

        for name, sql in MIGRATIONS:
            if name not in applied:
                try:
                    logger.info("Applying migration: %s", name)
                    # Use executescript for multi-statement migrations
                    if ';' in sql.strip().rstrip(';'):
                        conn.executescript(sql)
                    else:
                        conn.execute(sql)
                    conn.execute(
                        "INSERT INTO migrations (name) VALUES (?)", (name,))
                    conn.commit()
                    logger.info("Migration %s applied", name)
                except sqlite3.OperationalError as exc:
                    # Column/table may already exist from a fresh init_db()
                    if "duplicate column" in str(exc).lower() or "already exists" in str(exc).lower():
                        conn.execute(
                            "INSERT OR IGNORE INTO migrations (name) VALUES (?)",
                            (name,))
                        conn.commit()
                        logger.debug("Migration %s skipped (already present): %s",
                                     name, exc)
                    else:
                        conn.rollback()
                        raise
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────
# Session secret management
# ──────────────────────────────────────────────────────────────────────

def get_or_create_session_secret():
    """Retrieve the Flask session secret from app_settings.
    Generate and persist a new one if it doesn't exist."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = 'session_secret'"
        ).fetchone()

        if row:
            return row["value"]

        secret = secrets.token_hex(32)
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('session_secret', ?)",
            (secret,),
        )
        conn.commit()
        logger.info("Generated new session secret and stored in database")
        return secret
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────
# Audit logging helper
# ──────────────────────────────────────────────────────────────────────

def log_audit(user_email, action, target_type="", target_id="", details=None):
    """Write an entry to the audit log."""
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO audit_log (user_email, action, target_type, target_id, details) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_email, action, target_type, str(target_id),
             json.dumps(details) if details else "{}"),
        )
        conn.commit()
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────
# Task helpers — subtasks, dependencies, phases
# ──────────────────────────────────────────────────────────────────────

def get_tasks_with_subtasks(checklist_id):
    """Return tasks for a checklist, organized with subtasks nested
    under their parent tasks.

    Returns a list of dicts.  Each top-level task has a ``subtasks`` key
    containing a (possibly empty) list of child task dicts.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE checklist_id = ? ORDER BY sort_order, id",
            (checklist_id,),
        ).fetchall()
    finally:
        conn.close()

    tasks_by_id = {}
    top_level = []

    for row in rows:
        task = dict(row)
        task["subtasks"] = []
        tasks_by_id[task["id"]] = task

    for task in tasks_by_id.values():
        parent_id = task.get("parent_task_id")
        if parent_id and parent_id in tasks_by_id:
            tasks_by_id[parent_id]["subtasks"].append(task)
        else:
            top_level.append(task)

    return top_level


def check_task_dependencies(task_id):
    """Return True if all dependency tasks (recursive chain) are completed.

    A task with no ``depends_on_task_id`` is always ready.
    """
    conn = get_db()
    try:
        seen = set()
        current_id = task_id

        while current_id is not None:
            row = conn.execute(
                "SELECT depends_on_task_id, status FROM tasks WHERE id = ?",
                (current_id,),
            ).fetchone()
            if row is None:
                break

            dep_id = row["depends_on_task_id"]
            if dep_id is None:
                return True  # no further dependency — chain is satisfied

            if dep_id in seen:
                # circular dependency guard
                logger.warning("Circular dependency detected at task %s", dep_id)
                return False
            seen.add(dep_id)

            dep_row = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (dep_id,)
            ).fetchone()
            if dep_row is None:
                return True  # dependency task was deleted; treat as clear
            if dep_row["status"] not in ("completed", "skipped"):
                return False

            current_id = dep_id

        return True
    finally:
        conn.close()


def get_phase_progress(employee_id, phase):
    """Return completion percentage (0–100) for a specific phase of an
    employee's onboarding/offboarding tasks."""
    conn = get_db()
    try:
        total = conn.execute(
            "SELECT COUNT(*) AS cnt FROM tasks "
            "WHERE employee_id = ? AND phase = ? AND status != 'skipped'",
            (employee_id, phase),
        ).fetchone()["cnt"]

        if total == 0:
            return 100  # no tasks in this phase ⇒ 100 %

        done = conn.execute(
            "SELECT COUNT(*) AS cnt FROM tasks "
            "WHERE employee_id = ? AND phase = ? AND status = 'completed'",
            (employee_id, phase),
        ).fetchone()["cnt"]

        return round(done / total * 100)
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────
# Cohort helpers
# ──────────────────────────────────────────────────────────────────────

def create_cohort(name, start_week):
    """Create a new onboarding cohort or return the existing one for
    the given ``start_week`` (ISO date string for the Monday of that week)."""
    conn = get_db()
    try:
        existing = conn.execute(
            "SELECT * FROM onboarding_cohorts WHERE start_week = ?",
            (start_week,),
        ).fetchone()
        if existing:
            return dict(existing)

        cur = conn.execute(
            "INSERT INTO onboarding_cohorts (name, start_week) VALUES (?, ?)",
            (name, start_week),
        )
        conn.commit()
        return {"id": cur.lastrowid, "name": name, "start_week": start_week}
    finally:
        conn.close()


def get_or_assign_cohort(start_date, employee_id=None):
    """Auto-assign an employee to a cohort based on the Monday of their
    start-date week.  Creates the cohort if it doesn't exist yet.

    ``start_date`` should be an ISO date string (YYYY-MM-DD).
    ``employee_id`` is optional — when provided the employee's cohort_id
    is updated in the database.  When omitted (e.g. before the employee
    row exists), only the cohort is created/fetched and returned.
    Returns the cohort dict.
    """
    dt = datetime.strptime(start_date, "%Y-%m-%d")
    monday = dt - timedelta(days=dt.weekday())
    start_week = monday.strftime("%Y-%m-%d")
    month_name = monday.strftime("%B %Y")
    cohort_name = f"{month_name} Cohort"

    cohort = create_cohort(cohort_name, start_week)

    if employee_id is not None:
        conn = get_db()
        try:
            conn.execute(
                "UPDATE employees SET cohort_id = ? WHERE id = ?",
                (cohort["id"], employee_id),
            )
            conn.commit()
        finally:
            conn.close()

    return cohort


# ──────────────────────────────────────────────────────────────────────
# Metrics helpers
# ──────────────────────────────────────────────────────────────────────

def get_employee_metrics():
    """Return aggregate onboarding metrics.

    Returns a dict with:
      - avg_time_to_ramp_days : float or None
      - retention_90_day_pct  : float or None
      - onboarding_nps        : float or None  (average rating from day_90 surveys)
      - total_onboarded       : int
      - total_departed        : int
    """
    conn = get_db()
    try:
        # Average time-to-ramp: days between start_date and ramped_at
        ramp_row = conn.execute("""
            SELECT AVG(julianday(ramped_at) - julianday(start_date)) AS avg_days
            FROM employees
            WHERE ramped_at IS NOT NULL AND start_date IS NOT NULL
        """).fetchone()
        avg_ramp = round(ramp_row["avg_days"], 1) if ramp_row["avg_days"] else None

        # 90-day retention: employees who started >= 90 days ago and are
        # still active (not departed)
        cutoff = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d")
        eligible = conn.execute(
            "SELECT COUNT(*) AS cnt FROM employees WHERE start_date <= ?",
            (cutoff,),
        ).fetchone()["cnt"]
        retained = conn.execute(
            "SELECT COUNT(*) AS cnt FROM employees "
            "WHERE start_date <= ? AND status != 'departed'",
            (cutoff,),
        ).fetchone()["cnt"]
        retention = round(retained / eligible * 100, 1) if eligible else None

        # Onboarding NPS: average rating across day_90 survey responses
        nps_row = conn.execute("""
            SELECT AVG(sr.rating) AS avg_nps
            FROM survey_responses sr
            JOIN surveys s ON sr.survey_id = s.id
            WHERE s.survey_type = 'day_90' AND sr.rating IS NOT NULL
        """).fetchone()
        nps = round(nps_row["avg_nps"], 1) if nps_row["avg_nps"] else None

        total_onboarded = conn.execute(
            "SELECT COUNT(*) AS cnt FROM employees WHERE status = 'active'"
        ).fetchone()["cnt"]

        total_departed = conn.execute(
            "SELECT COUNT(*) AS cnt FROM employees WHERE status = 'departed'"
        ).fetchone()["cnt"]

        return {
            "avg_time_to_ramp_days": avg_ramp,
            "retention_90_day_pct": retention,
            "onboarding_nps": nps,
            "total_onboarded": total_onboarded,
            "total_departed": total_departed,
        }
    finally:
        conn.close()


def take_monthly_snapshot(db=None):
    """Calculate and store metric snapshots for the current month.

    Uses INSERT OR REPLACE so it's safe to call multiple times in the
    same month — later calls update the existing snapshot.
    ``db`` is an optional existing connection; when *None* a fresh one
    is opened and closed internally.
    """
    close_conn = db is None
    if db is None:
        db = get_db()
    try:
        today = datetime.utcnow()
        snapshot_date = today.strftime("%Y-%m")
        month_start = today.replace(day=1).strftime("%Y-%m-%d")
        # Last day of month
        if today.month == 12:
            next_month_start = today.replace(year=today.year + 1, month=1, day=1)
        else:
            next_month_start = today.replace(month=today.month + 1, day=1)
        month_end = (next_month_start - timedelta(days=1)).strftime("%Y-%m-%d")

        metrics = {}

        # monthly_hires — employees with start_date in this month
        row = db.execute(
            "SELECT COUNT(*) AS cnt FROM employees "
            "WHERE start_date >= ? AND start_date <= ?",
            (month_start, month_end),
        ).fetchone()
        metrics["monthly_hires"] = row["cnt"]

        # monthly_departures — employees with end_date in this month
        row = db.execute(
            "SELECT COUNT(*) AS cnt FROM employees "
            "WHERE end_date >= ? AND end_date <= ?",
            (month_start, month_end),
        ).fetchone()
        metrics["monthly_departures"] = row["cnt"]

        # avg_time_to_ramp — avg days for employees ramped this month
        row = db.execute(
            "SELECT AVG(julianday(ramped_at) - julianday(start_date)) AS avg_days "
            "FROM employees "
            "WHERE ramped_at >= ? AND ramped_at <= ? "
            "AND start_date IS NOT NULL",
            (month_start, month_end),
        ).fetchone()
        metrics["avg_time_to_ramp"] = round(row["avg_days"], 1) if row["avg_days"] else 0

        # retention_90day — % of employees started >=90 days ago who
        # are still active (not departed)
        cutoff = (today - timedelta(days=90)).strftime("%Y-%m-%d")
        elig = db.execute(
            "SELECT COUNT(*) AS cnt FROM employees WHERE start_date <= ?",
            (cutoff,),
        ).fetchone()["cnt"]
        retained = db.execute(
            "SELECT COUNT(*) AS cnt FROM employees "
            "WHERE start_date <= ? AND status != 'departed'",
            (cutoff,),
        ).fetchone()["cnt"]
        metrics["retention_90day"] = round(retained / elig * 100, 1) if elig else 0

        # avg_task_completion_rate — avg % of tasks completed across
        # active onboardings
        active_checklists = db.execute(
            "SELECT c.id FROM checklists c "
            "WHERE c.checklist_type = 'onboarding' AND c.status = 'active'"
        ).fetchall()
        if active_checklists:
            pcts = []
            for cl in active_checklists:
                total = db.execute(
                    "SELECT COUNT(*) AS cnt FROM tasks WHERE checklist_id = ?",
                    (cl["id"],),
                ).fetchone()["cnt"]
                done = db.execute(
                    "SELECT COUNT(*) AS cnt FROM tasks "
                    "WHERE checklist_id = ? AND status = 'completed'",
                    (cl["id"],),
                ).fetchone()["cnt"]
                pcts.append(round(done / total * 100, 1) if total else 100)
            metrics["avg_task_completion_rate"] = round(sum(pcts) / len(pcts), 1)
        else:
            metrics["avg_task_completion_rate"] = 0

        # avg_onboarding_satisfaction — avg survey rating this month
        row = db.execute(
            "SELECT AVG(sr.rating) AS avg_rating "
            "FROM survey_responses sr "
            "JOIN surveys s ON sr.survey_id = s.id "
            "WHERE sr.rating IS NOT NULL AND sr.created_at >= ? AND sr.created_at <= ?",
            (month_start, month_end),
        ).fetchone()
        metrics["avg_onboarding_satisfaction"] = (
            round(row["avg_rating"], 1) if row["avg_rating"] else 0
        )

        # Write all metrics
        for name, value in metrics.items():
            db.execute(
                "INSERT OR REPLACE INTO metric_snapshots "
                "(snapshot_date, metric_name, metric_value) VALUES (?, ?, ?)",
                (snapshot_date, name, value),
            )
        db.commit()
        logger.info("Monthly snapshot taken for %s: %s", snapshot_date, metrics)
        return metrics
    finally:
        if close_conn:
            db.close()
