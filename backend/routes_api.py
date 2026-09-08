"""
External REST API for Celito Onboarding Platform.

Provides endpoints for external tools (provisioning scripts, ITSM systems)
to query and update onboarding tasks programmatically.

Authentication: API key via X-API-Key header or ?api_key query parameter.
"""

import logging
from datetime import date, datetime

from flask import Blueprint, jsonify, request

from .db import get_db, log_audit

logger = logging.getLogger(__name__)

api_bp = Blueprint("external_api", __name__, url_prefix="/api/v1")


# ── Auth helper ─────────────────────────────────────────────────────────

def _require_api_key():
    """Validate API key from header or query param.

    Returns (api_key_row_dict, None) on success or
    (None, (response, status_code)) on failure.
    """
    key = request.headers.get("X-API-Key") or request.args.get("api_key")
    if not key:
        return None, (jsonify({
            "error": "Missing API key. Pass X-API-Key header or api_key query parameter."
        }), 401)

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM api_keys WHERE key_value = ? AND is_active = 1",
            (key,),
        ).fetchone()
        if row:
            # Update last_used timestamp
            conn.execute(
                "UPDATE api_keys SET last_used = ? WHERE id = ?",
                (datetime.utcnow().isoformat(), row["id"]),
            )
            conn.commit()
    finally:
        conn.close()

    if not row:
        return None, (jsonify({"error": "Invalid or inactive API key"}), 401)

    return dict(row), None


# ── Endpoints ───────────────────────────────────────────────────────────

@api_bp.route("/tasks", methods=["GET"])
def list_tasks():
    """List onboarding tasks with optional filters.

    Query params:
      employee_email – filter by employee email
      status         – filter by status (pending, completed, skipped, blocked…)
      phase          – filter by phase
      assigned_to    – filter by assignee email
      limit          – max results (default 100, max 500)
    """
    api_user, err = _require_api_key()
    if err:
        return err

    conn = get_db()
    try:
        query = (
            "SELECT t.*, e.email AS employee_email, "
            "e.first_name || ' ' || e.last_name AS employee_name "
            "FROM tasks t "
            "LEFT JOIN employees e ON t.employee_id = e.id "
            "WHERE 1=1"
        )
        params = []

        if request.args.get("employee_email"):
            query += " AND e.email = ?"
            params.append(request.args["employee_email"])
        if request.args.get("status"):
            query += " AND t.status = ?"
            params.append(request.args["status"])
        if request.args.get("phase"):
            query += " AND t.phase = ?"
            params.append(request.args["phase"])
        if request.args.get("assigned_to"):
            query += " AND t.assigned_email = ?"
            params.append(request.args["assigned_to"])

        limit = min(int(request.args.get("limit", 100)), 500)
        query += f" ORDER BY t.id DESC LIMIT {limit}"

        tasks = conn.execute(query, params).fetchall()
        return jsonify({"tasks": [dict(t) for t in tasks], "count": len(tasks)})
    finally:
        conn.close()


@api_bp.route("/tasks/<int:task_id>", methods=["GET"])
def get_task(task_id):
    """Get a single task by ID."""
    api_user, err = _require_api_key()
    if err:
        return err

    conn = get_db()
    try:
        task = conn.execute(
            "SELECT t.*, e.email AS employee_email "
            "FROM tasks t LEFT JOIN employees e ON t.employee_id = e.id "
            "WHERE t.id = ?",
            (task_id,),
        ).fetchone()
        if not task:
            return jsonify({"error": "Task not found"}), 404
        return jsonify(dict(task))
    finally:
        conn.close()


@api_bp.route("/tasks/<int:task_id>/complete", methods=["POST"])
def complete_task(task_id):
    """Mark a task as completed.

    Optional JSON body:
      notes – completion notes appended to the task
    """
    api_user, err = _require_api_key()
    if err:
        return err

    conn = get_db()
    try:
        task = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not task:
            return jsonify({"error": "Task not found"}), 404

        if task["status"] == "completed":
            return jsonify({"ok": True, "message": "Already completed"})

        # Check single dependency
        if task["depends_on_task_id"]:
            dep = conn.execute(
                "SELECT status FROM tasks WHERE id = ?",
                (task["depends_on_task_id"],),
            ).fetchone()
            if dep and dep["status"] not in ("completed", "skipped"):
                return jsonify({
                    "error": "Dependency not met",
                    "blocking_task_id": task["depends_on_task_id"],
                }), 409

        # Check multi-dependency
        if task.get("depends_on"):
            dep_ids = [int(d.strip()) for d in task["depends_on"].split(",") if d.strip()]
            if dep_ids:
                ph = ",".join("?" * len(dep_ids))
                incomplete = conn.execute(
                    f"SELECT id, title FROM tasks WHERE id IN ({ph}) "
                    f"AND status NOT IN ('completed', 'skipped')",
                    dep_ids,
                ).fetchall()
                if incomplete:
                    return jsonify({
                        "error": "Dependencies not met",
                        "blocking_tasks": [
                            {"id": t["id"], "title": t["title"]} for t in incomplete
                        ],
                    }), 409

        body = request.get_json(silent=True) or {}
        notes = body.get("notes", "")
        note_text = f"Completed via API ({api_user.get('name', 'external')})"
        if notes:
            note_text += f": {notes}"

        now = datetime.utcnow().isoformat()
        conn.execute(
            "UPDATE tasks SET status = 'completed', completed_at = ?, completed_by = ? "
            "WHERE id = ?",
            (now, f"api:{api_user.get('name', 'key')}", task_id),
        )
        # Add note as comment
        conn.execute(
            "INSERT INTO task_comments (task_id, user_email, comment, created_at) "
            "VALUES (?, ?, ?, ?)",
            (task_id, f"api:{api_user.get('name', 'key')}", note_text, now),
        )
        # Unblock dependent tasks
        conn.execute(
            "UPDATE tasks SET status = 'pending' "
            "WHERE depends_on_task_id = ? AND status = 'blocked'",
            (task_id,),
        )
        conn.commit()
        log_audit(
            f"api:{api_user.get('name', 'key')}",
            "complete_task_api", "task", str(task_id),
        )
    finally:
        conn.close()

    return jsonify({"ok": True, "message": f"Task {task_id} marked as completed"})


@api_bp.route("/tasks/<int:task_id>/skip", methods=["POST"])
def skip_task(task_id):
    """Mark a task as skipped (e.g. not applicable for this employee)."""
    api_user, err = _require_api_key()
    if err:
        return err

    conn = get_db()
    try:
        task = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not task:
            return jsonify({"error": "Task not found"}), 404
        if task["status"] in ("completed", "skipped"):
            return jsonify({"ok": True, "message": f"Already {task['status']}"})

        body = request.get_json(silent=True) or {}
        reason = body.get("reason", "Skipped via API")
        now = datetime.utcnow().isoformat()
        conn.execute(
            "UPDATE tasks SET status = 'skipped' WHERE id = ?", (task_id,)
        )
        conn.execute(
            "INSERT INTO task_comments (task_id, user_email, comment, created_at) "
            "VALUES (?, ?, ?, ?)",
            (task_id, f"api:{api_user.get('name', 'key')}", reason, now),
        )
        # Unblock dependent tasks
        conn.execute(
            "UPDATE tasks SET status = 'pending' "
            "WHERE depends_on_task_id = ? AND status = 'blocked'",
            (task_id,),
        )
        conn.commit()
        log_audit(
            f"api:{api_user.get('name', 'key')}",
            "skip_task_api", "task", str(task_id),
        )
    finally:
        conn.close()

    return jsonify({"ok": True, "message": f"Task {task_id} skipped"})


@api_bp.route("/employees", methods=["GET"])
def list_employees():
    """List employees with optional status/department filter.

    Query params:
      status     – pending, active, offboarding, departed
      department – filter by department name
      limit      – max results (default 100, max 500)
    """
    api_user, err = _require_api_key()
    if err:
        return err

    conn = get_db()
    try:
        query = (
            "SELECT id, first_name, last_name, email, department, "
            "role_title, start_date, status, location_mode "
            "FROM employees WHERE 1=1"
        )
        params = []

        if request.args.get("status"):
            query += " AND status = ?"
            params.append(request.args["status"])
        if request.args.get("department"):
            query += " AND department = ?"
            params.append(request.args["department"])

        limit = min(int(request.args.get("limit", 100)), 500)
        query += f" ORDER BY id DESC LIMIT {limit}"

        emps = conn.execute(query, params).fetchall()
        return jsonify({"employees": [dict(e) for e in emps], "count": len(emps)})
    finally:
        conn.close()


@api_bp.route("/employees/<int:emp_id>/tasks", methods=["GET"])
def employee_tasks(emp_id):
    """List all tasks for a specific employee."""
    api_user, err = _require_api_key()
    if err:
        return err

    conn = get_db()
    try:
        emp = conn.execute(
            "SELECT id FROM employees WHERE id = ?", (emp_id,)
        ).fetchone()
        if not emp:
            return jsonify({"error": "Employee not found"}), 404

        tasks = conn.execute(
            "SELECT * FROM tasks WHERE employee_id = ? ORDER BY sort_order, id",
            (emp_id,),
        ).fetchall()
        return jsonify({"tasks": [dict(t) for t in tasks], "count": len(tasks)})
    finally:
        conn.close()


@api_bp.route("/docs", methods=["GET"])
def api_docs():
    """Return API documentation as JSON."""
    api_user, err = _require_api_key()
    # Allow unauthenticated access to docs
    return jsonify({
        "name": "Celito Onboarding Platform API",
        "version": "1.0",
        "authentication": {
            "method": "API Key",
            "header": "X-API-Key",
            "query_param": "api_key",
            "description": "Pass your API key via the X-API-Key header (preferred) or api_key query parameter.",
        },
        "base_url": "/api/v1",
        "endpoints": [
            {
                "method": "GET",
                "path": "/tasks",
                "description": "List tasks with optional filters",
                "query_params": {
                    "employee_email": "Filter by employee email",
                    "status": "Filter by status (pending, in_progress, completed, skipped, blocked)",
                    "phase": "Filter by phase (pre_boarding, company_onboarding, department_onboarding, role_training)",
                    "assigned_to": "Filter by assignee email",
                    "limit": "Max results (default 100, max 500)",
                },
            },
            {
                "method": "GET",
                "path": "/tasks/:id",
                "description": "Get a single task by ID",
            },
            {
                "method": "POST",
                "path": "/tasks/:id/complete",
                "description": "Mark a task as completed",
                "body": {"notes": "(optional) Completion notes"},
            },
            {
                "method": "POST",
                "path": "/tasks/:id/skip",
                "description": "Mark a task as skipped",
                "body": {"reason": "(optional) Reason for skipping"},
            },
            {
                "method": "GET",
                "path": "/employees",
                "description": "List employees",
                "query_params": {
                    "status": "pending, active, offboarding, departed",
                    "department": "Filter by department name",
                    "limit": "Max results (default 100, max 500)",
                },
            },
            {
                "method": "GET",
                "path": "/employees/:id/tasks",
                "description": "List all tasks for a specific employee",
            },
            {
                "method": "GET",
                "path": "/docs",
                "description": "This documentation (no auth required)",
            },
        ],
        "examples": {
            "curl_list_pending": 'curl -H "X-API-Key: YOUR_KEY" https://your-domain/api/v1/tasks?status=pending',
            "curl_complete": 'curl -X POST -H "X-API-Key: YOUR_KEY" -H "Content-Type: application/json" -d \'{"notes":"Laptop provisioned"}\' https://your-domain/api/v1/tasks/42/complete',
        },
    })
