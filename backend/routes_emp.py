"""
Employee self-service routes for the Celito Onboarding & Offboarding Platform.
Allows employees to view their own onboarding progress, complete tasks,
manage meet-and-greets, respond to surveys, and access buddy/cohort features.
"""

import json
import logging
from datetime import datetime, date, timedelta

from flask import Blueprint, request, jsonify

from .auth import login_required, get_current_user
from .db import get_db, log_audit

logger = logging.getLogger(__name__)

emp_bp = Blueprint('emp', __name__, url_prefix='/api/me')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_employee(db, email):
    """Look up an employee record by email. Returns dict or None."""
    row = db.execute('SELECT * FROM employees WHERE email = ?', (email,)).fetchone()
    return dict(row) if row else None


def _calc_phase(start_date_str):
    """Return the current onboarding phase based on days since start."""
    if not start_date_str:
        return 'unknown'
    try:
        start = date.fromisoformat(start_date_str)
    except (ValueError, TypeError):
        return 'unknown'
    delta = (date.today() - start).days
    if delta < 0:
        return 'pre_boarding'
    if delta <= 5:
        return 'company_onboarding'
    if delta <= 28:
        return 'department_onboarding'
    if delta <= 90:
        return 'role_training'
    return 'completed'


def _week_bounds(ref=None):
    """Return (monday, sunday) ISO strings for the week containing *ref*."""
    ref = ref or date.today()
    monday = ref - timedelta(days=ref.weekday())
    sunday = monday + timedelta(days=6)
    return monday.isoformat(), sunday.isoformat()


def _user_info(db, email):
    """Fetch display_name / role / department for an email, or return stub."""
    if not email:
        return None
    row = db.execute(
        'SELECT display_name, email, role, department FROM users WHERE email = ?',
        (email,)
    ).fetchone()
    if row:
        return dict(row)
    return {'display_name': email, 'email': email, 'role': '', 'department': ''}


# ---------------------------------------------------------------------------
# GET /api/me/profile
# ---------------------------------------------------------------------------

@emp_bp.route('/profile', methods=['GET'])
@login_required
def get_profile():
    """Return the current user's profile, employee record, buddy, HR owner,
    cohort, and current onboarding phase."""
    user = get_current_user()
    db = get_db()
    try:
        # User account
        user_row = db.execute(
            'SELECT id, email, display_name, role, department, created_at, last_login, timezone '
            'FROM users WHERE email = ?',
            (user['email'],)
        ).fetchone()

        emp = _get_employee(db, user['email'])

        buddy_info = None
        hr_owner_info = None
        cohort_info = None
        current_phase = None

        if emp:
            buddy_info = _user_info(db, emp.get('buddy_email'))
            hr_owner_info = _user_info(db, emp.get('hr_owner_email'))
            current_phase = _calc_phase(emp.get('start_date'))

            # Cohort
            cid = emp.get('cohort_id')
            if cid:
                crow = db.execute(
                    'SELECT * FROM onboarding_cohorts WHERE id = ?', (cid,)
                ).fetchone()
                if crow:
                    members = db.execute(
                        "SELECT first_name, last_name, email, department, role_title, start_date "
                        "FROM employees WHERE cohort_id = ? AND id != ?",
                        (cid, emp['id'])
                    ).fetchall()
                    cohort_info = {**dict(crow), 'members': [dict(m) for m in members]}

        return jsonify({
            'user': dict(user_row) if user_row else {
                'email': user['email'],
                'display_name': user.get('name', '')
            },
            'employee': emp,
            'buddy': buddy_info,
            'hr_owner': hr_owner_info,
            'cohort': cohort_info,
            'current_phase': current_phase,
        })
    finally:
        db.close()


# ---------------------------------------------------------------------------
# PUT /api/me/profile — update user profile (timezone, etc.)
# ---------------------------------------------------------------------------

@emp_bp.route('/profile', methods=['PUT'])
@login_required
def update_profile():
    """Update the current user's editable profile fields (timezone)."""
    user = get_current_user()
    data = request.get_json(force=True)
    db = get_db()
    try:
        updates = []
        params = []

        # Timezone — validate against a known list
        VALID_TIMEZONES = {
            'America/New_York', 'America/Chicago', 'America/Denver',
            'America/Los_Angeles', 'America/Phoenix', 'America/Anchorage',
            'Pacific/Honolulu', 'Europe/London', 'Europe/Berlin',
            'Asia/Kolkata', 'Asia/Tokyo', 'Australia/Sydney',
        }
        tz = data.get('timezone')
        if tz and tz in VALID_TIMEZONES:
            updates.append('timezone = ?')
            params.append(tz)

        if not updates:
            return jsonify({'error': 'No valid fields to update'}), 400

        params.append(user['email'])
        db.execute(
            f"UPDATE users SET {', '.join(updates)} WHERE email = ?", params
        )
        # Also update employees table if the user has an employee record
        if tz:
            db.execute(
                "UPDATE employees SET timezone = ? WHERE email = ?",
                (tz, user['email'])
            )
        db.commit()
        return jsonify({'ok': True, 'message': 'Profile updated'})
    finally:
        db.close()


# ---------------------------------------------------------------------------
# GET /api/me/onboarding
# ---------------------------------------------------------------------------

@emp_bp.route('/onboarding', methods=['GET'])
@login_required
def get_my_onboarding():
    """Return the employee's full onboarding data grouped by phase and
    category, with subtask nesting, progress percentages, dependency
    status, and a this-week focus section."""
    user = get_current_user()
    db = get_db()
    try:
        emp = _get_employee(db, user['email'])
        if not emp:
            return jsonify({'error': 'No employee record found', 'phases': {}}), 200

        checklists = db.execute(
            "SELECT * FROM checklists WHERE employee_id = ? AND checklist_type = 'onboarding' "
            "ORDER BY created_at DESC",
            (emp['id'],)
        ).fetchall()
        if not checklists:
            return jsonify({'employee': emp, 'phases': {}, 'progress': 0})

        cl = checklists[0]  # most recent onboarding checklist

        all_tasks = db.execute(
            'SELECT * FROM tasks WHERE checklist_id = ? ORDER BY sort_order, id',
            (cl['id'],)
        ).fetchall()
        all_tasks = [dict(t) for t in all_tasks]

        # Index by id for dependency lookup
        task_map = {t['id']: t for t in all_tasks}

        # Mark dependency status
        for t in all_tasks:
            dep_id = t.get('depends_on_task_id')
            if dep_id and dep_id in task_map:
                dep = task_map[dep_id]
                t['dependency_met'] = dep['status'] in ('completed', 'skipped')
                t['blocked_by'] = dep['title'] if not t['dependency_met'] else None
            else:
                t['dependency_met'] = True
                t['blocked_by'] = None

        # Separate parents and subtasks
        parents = [t for t in all_tasks if not t.get('parent_task_id')]
        subtask_map = {}
        for t in all_tasks:
            pid = t.get('parent_task_id')
            if pid:
                subtask_map.setdefault(pid, []).append(t)

        # Nest subtasks
        for p in parents:
            p['subtasks'] = subtask_map.get(p['id'], [])

        # Group by phase then category
        PHASES = ['pre_boarding', 'company_onboarding', 'department_onboarding', 'role_training']
        phases = {}
        for phase in PHASES:
            phase_tasks = [t for t in parents if (t.get('phase') or '') == phase]
            if not phase_tasks:
                continue
            categories = {}
            for t in phase_tasks:
                cat = t.get('category') or 'other'
                categories.setdefault(cat, []).append(t)

            total = sum(1 for t in all_tasks if (t.get('phase') or '') == phase)
            done = sum(1 for t in all_tasks
                       if (t.get('phase') or '') == phase
                       and t['status'] in ('completed', 'skipped'))
            phases[phase] = {
                'categories': categories,
                'total': total,
                'completed': done,
                'progress': round(done / total * 100, 1) if total else 0,
            }

        # Overall progress
        total_all = len(all_tasks)
        done_all = sum(1 for t in all_tasks if t['status'] in ('completed', 'skipped'))

        # Employee-specific progress (tasks they can act on: assigned to
        # them, acknowledgment tasks, or employee-category tasks)
        emp_email = user.get('email', '')
        my_tasks = [t for t in all_tasks
                    if t.get('is_acknowledgment')
                    or t.get('category') == 'employee'
                    or (t.get('assigned_email') and t['assigned_email'] == emp_email)]
        my_total = len(my_tasks)
        my_done = sum(1 for t in my_tasks if t['status'] in ('completed', 'skipped'))

        # Overdue
        today_str = date.today().isoformat()
        overdue = [t for t in parents
                   if t.get('due_date') and t['due_date'] < today_str
                   and t['status'] not in ('completed', 'skipped')]

        # This week's focus
        mon, sun = _week_bounds()
        this_week = [t for t in parents
                     if t.get('due_date') and mon <= t['due_date'] <= sun
                     and t['status'] not in ('completed', 'skipped')]

        return jsonify({
            'employee': emp,
            'checklist': dict(cl),
            'phases': phases,
            'tasks': all_tasks,
            'progress': round(done_all / total_all * 100, 1) if total_all else 0,
            'total_tasks': total_all,
            'completed_tasks': done_all,
            'my_progress': round(my_done / my_total * 100, 1) if my_total else 0,
            'my_total': my_total,
            'my_completed': my_done,
            'overdue': overdue,
            'this_week': this_week,
        })
    finally:
        db.close()


# ---------------------------------------------------------------------------
# PUT /api/me/tasks/<id>/complete
# ---------------------------------------------------------------------------

@emp_bp.route('/tasks/<int:task_id>/complete', methods=['PUT'])
@login_required
def complete_my_task(task_id):
    """Mark a task as completed. Enforces acknowledgment flag, dependencies,
    and auto-unblocks downstream tasks."""
    user = get_current_user()
    db = get_db()
    try:
        emp = _get_employee(db, user['email'])
        if not emp:
            return jsonify({'error': 'No employee record found'}), 404

        task = db.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if not task:
            return jsonify({'error': 'Task not found'}), 404

        if task['employee_id'] != emp['id']:
            return jsonify({'error': 'This task does not belong to you'}), 403

        # Only acknowledgment tasks or tasks assigned to the employee
        is_allowed = (
            task.get('is_acknowledgment')
            or task.get('category') == 'employee'
            or (task['assigned_email'] and task['assigned_email'] == user['email'])
            or not task['assigned_email']
        )
        if not is_allowed:
            return jsonify({'error': 'This task must be completed by the assigned team'}), 403

        # Check dependency
        dep_id = task.get('depends_on_task_id')
        if dep_id:
            dep = db.execute('SELECT id, title, status FROM tasks WHERE id = ?', (dep_id,)).fetchone()
            if dep and dep['status'] not in ('completed', 'skipped'):
                return jsonify({
                    'error': f"This task is blocked by: {dep['title']}"
                }), 403

        now = datetime.utcnow().isoformat()

        db.execute(
            'UPDATE tasks SET status = ?, completed_at = ?, completed_by = ? WHERE id = ?',
            ('completed', now, user['email'], task_id)
        )

        # Unblock dependent tasks
        db.execute(
            "UPDATE tasks SET status = 'pending' "
            "WHERE depends_on_task_id = ? AND status = 'blocked'",
            (task_id,)
        )

        # Check phase completion
        phase = task.get('phase')
        checklist_id = task['checklist_id']
        if phase:
            remaining_in_phase = db.execute(
                "SELECT COUNT(*) as cnt FROM tasks "
                "WHERE checklist_id = ? AND phase = ? AND status NOT IN ('completed','skipped')",
                (checklist_id, phase)
            ).fetchone()['cnt']
            # remaining includes this task pre-commit, so <=1
            if remaining_in_phase <= 1:
                logger.info("Phase '%s' completed for checklist %s", phase, checklist_id)

        # Check full checklist completion
        remaining = db.execute(
            "SELECT COUNT(*) as cnt FROM tasks "
            "WHERE checklist_id = ? AND status NOT IN ('completed','skipped')",
            (checklist_id,)
        ).fetchone()['cnt']
        if remaining <= 1:
            db.execute(
                "UPDATE checklists SET status = 'completed', completed_at = ? WHERE id = ?",
                (now, checklist_id)
            )

        db.commit()
        log_audit(user['email'], 'task_completed', 'task', task_id,
                  {'title': task['title']})

        updated = db.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
        return jsonify(dict(updated))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# PUT /api/me/tasks/<id>/already-completed  (rehire "I already did this")
# ---------------------------------------------------------------------------

@emp_bp.route('/tasks/<int:task_id>/already-completed', methods=['PUT'])
@login_required
def mark_already_completed(task_id):
    """Rehire employees can flag a task as already done from a previous stint.
    Sets status to 'pending_review' so HR can confirm."""
    user = get_current_user()
    db = get_db()
    try:
        emp = _get_employee(db, user['email'])
        if not emp:
            return jsonify({'error': 'No employee record found'}), 404
        if not emp.get('is_rehire'):
            return jsonify({'error': 'This feature is only available for rehire employees'}), 403

        task = db.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if not task:
            return jsonify({'error': 'Task not found'}), 404
        if task['employee_id'] != emp['id']:
            return jsonify({'error': 'This task does not belong to you'}), 403
        if task['status'] in ('completed', 'skipped'):
            return jsonify({'error': 'Task is already completed or skipped'}), 400

        now = datetime.utcnow().isoformat()
        db.execute(
            "UPDATE tasks SET status = 'pending_review', completed_at = ?, completed_by = ? WHERE id = ?",
            (now, user['email'] + ':already-done', task_id)
        )
        db.commit()

        log_audit(user['email'], 'task_already_completed', 'task', task_id,
                  {'title': task['title']})
        updated = db.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
        return jsonify(dict(updated))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# GET /api/me/contacts
# ---------------------------------------------------------------------------

@emp_bp.route('/contacts', methods=['GET'])
@login_required
def get_my_contacts():
    """Return key contacts: manager, HR rep, IT, buddy, and cohort members."""
    user = get_current_user()
    db = get_db()
    try:
        emp = _get_employee(db, user['email'])
        if not emp:
            return jsonify({'contacts': [], 'cohort_members': []}), 200

        contacts = []

        # Manager
        if emp.get('manager_email'):
            mgr = _user_info(db, emp['manager_email'])
            contacts.append({
                'role': 'Manager',
                'name': mgr['display_name'],
                'email': mgr['email'],
                'department': mgr.get('department', ''),
            })

        # HR owner
        if emp.get('hr_owner_email'):
            hr = _user_info(db, emp['hr_owner_email'])
            contacts.append({
                'role': 'HR Representative',
                'name': hr['display_name'],
                'email': hr['email'],
                'department': hr.get('department', 'Human Resources'),
            })
        else:
            # Fallback: any HR-role user
            hr_rows = db.execute(
                "SELECT display_name, email, department FROM users WHERE role = 'hr' LIMIT 2"
            ).fetchall()
            for h in hr_rows:
                contacts.append({
                    'role': 'HR',
                    'name': h['display_name'],
                    'email': h['email'],
                    'department': h['department'] or 'Human Resources',
                })

        # IT contact
        it_rows = db.execute(
            "SELECT display_name, email, department FROM users "
            "WHERE department LIKE '%IT%' OR department LIKE '%Technology%' OR role = 'admin' "
            "LIMIT 2"
        ).fetchall()
        for it in it_rows:
            contacts.append({
                'role': 'IT Support',
                'name': it['display_name'],
                'email': it['email'],
                'department': it['department'] or 'IT',
            })

        # Buddy
        if emp.get('buddy_email'):
            buddy = _user_info(db, emp['buddy_email'])
            contacts.append({
                'role': 'Onboarding Buddy',
                'name': buddy['display_name'],
                'email': buddy['email'],
                'department': buddy.get('department', ''),
            })

        # Cohort members
        cohort_members = []
        cid = emp.get('cohort_id')
        if cid:
            rows = db.execute(
                "SELECT first_name, last_name, email, department, role_title "
                "FROM employees WHERE cohort_id = ? AND id != ?",
                (cid, emp['id'])
            ).fetchall()
            cohort_members = [dict(r) for r in rows]

        return jsonify({'contacts': contacts, 'cohort_members': cohort_members})
    finally:
        db.close()


# ---------------------------------------------------------------------------
# GET /api/me/this-week
# ---------------------------------------------------------------------------

@emp_bp.route('/this-week', methods=['GET'])
@login_required
def get_this_week():
    """Return only tasks due the current calendar week, grouped by day,
    plus overdue items from prior weeks."""
    user = get_current_user()
    db = get_db()
    try:
        emp = _get_employee(db, user['email'])
        if not emp:
            return jsonify({'days': [], 'overdue': [], 'message': ''}), 200

        checklist = db.execute(
            "SELECT id FROM checklists WHERE employee_id = ? AND checklist_type = 'onboarding' "
            "ORDER BY created_at DESC LIMIT 1",
            (emp['id'],)
        ).fetchone()
        if not checklist:
            return jsonify({'days': [], 'overdue': [], 'message': 'No active onboarding'}), 200

        cl_id = checklist['id']
        mon, sun = _week_bounds()
        today_str = date.today().isoformat()

        # This week's tasks
        rows = db.execute(
            "SELECT * FROM tasks WHERE checklist_id = ? "
            "AND due_date >= ? AND due_date <= ? "
            "ORDER BY due_date, sort_order",
            (cl_id, mon, sun)
        ).fetchall()

        days = {}
        for r in rows:
            d = r['due_date']
            days.setdefault(d, []).append(dict(r))
        days_list = [{'date': d, 'tasks': t} for d, t in sorted(days.items())]

        # Overdue from earlier weeks
        overdue = db.execute(
            "SELECT * FROM tasks WHERE checklist_id = ? "
            "AND due_date < ? AND status NOT IN ('completed','skipped') "
            "ORDER BY due_date",
            (cl_id, mon)
        ).fetchall()

        # Progress message
        total = db.execute(
            "SELECT COUNT(*) as cnt FROM tasks WHERE checklist_id = ?", (cl_id,)
        ).fetchone()['cnt']
        done = db.execute(
            "SELECT COUNT(*) as cnt FROM tasks WHERE checklist_id = ? "
            "AND status IN ('completed','skipped')", (cl_id,)
        ).fetchone()['cnt']
        pct = round(done / total * 100) if total else 0
        week_count = sum(1 for r in rows if r['status'] not in ('completed', 'skipped'))
        message = (
            f"You're {pct}% through your onboarding! "
            f"{week_count} task{'s' if week_count != 1 else ''} this week."
        )

        return jsonify({
            'days': days_list,
            'overdue': [dict(o) for o in overdue],
            'message': message,
            'progress_pct': pct,
        })
    finally:
        db.close()


# ---------------------------------------------------------------------------
# GET /api/me/milestones
# ---------------------------------------------------------------------------

@emp_bp.route('/milestones', methods=['GET'])
@login_required
def get_my_milestones():
    """Return 30/60/90-day milestones with days remaining and current marker."""
    user = get_current_user()
    db = get_db()
    try:
        emp = _get_employee(db, user['email'])
        if not emp:
            return jsonify({'milestones': []}), 200

        rows = db.execute(
            'SELECT * FROM milestones WHERE employee_id = ? ORDER BY day_marker',
            (emp['id'],)
        ).fetchall()

        milestones = []
        start = None
        try:
            start = date.fromisoformat(emp['start_date'])
        except (ValueError, TypeError):
            pass

        days_since_start = (date.today() - start).days if start else 0
        current_marker = None

        for r in rows:
            m = dict(r)
            if start:
                target = start + timedelta(days=r['day_marker'])
                m['target_date'] = target.isoformat()
                m['days_remaining'] = max(0, (target - date.today()).days)
            else:
                m['target_date'] = None
                m['days_remaining'] = None
            milestones.append(m)

        # Determine current milestone
        for marker in [30, 60, 90]:
            if days_since_start <= marker:
                current_marker = marker
                break

        return jsonify({
            'milestones': milestones,
            'current_marker': current_marker,
            'days_since_start': days_since_start,
        })
    finally:
        db.close()


# ---------------------------------------------------------------------------
# GET /api/me/meet-greets
# ---------------------------------------------------------------------------

@emp_bp.route('/meet-greets', methods=['GET'])
@login_required
def get_my_meet_greets():
    """Return meet-and-greet assignments grouped by week."""
    user = get_current_user()
    db = get_db()
    try:
        emp = _get_employee(db, user['email'])
        if not emp:
            return jsonify({'meet_greets': {}, 'total': 0, 'completed_count': 0}), 200

        rows = db.execute(
            'SELECT * FROM meet_greets WHERE employee_id = ? ORDER BY week_number, id',
            (emp['id'],)
        ).fetchall()

        by_week = {}
        total = 0
        completed_count = 0
        for r in rows:
            m = dict(r)
            wk = m.get('week_number') or 1
            by_week.setdefault(wk, []).append(m)
            total += 1
            if m.get('completed'):
                completed_count += 1

        return jsonify({
            'meet_greets': by_week,
            'total': total,
            'completed_count': completed_count,
            'summary': f"Met {completed_count} of {total} people",
        })
    finally:
        db.close()


@emp_bp.route('/meet-greets/<int:mg_id>/complete', methods=['PUT'])
@login_required
def complete_meet_greet(mg_id):
    """Mark a meet-and-greet as completed."""
    user = get_current_user()
    db = get_db()
    try:
        emp = _get_employee(db, user['email'])
        if not emp:
            return jsonify({'error': 'No employee record found'}), 404

        mg = db.execute('SELECT * FROM meet_greets WHERE id = ?', (mg_id,)).fetchone()
        if not mg:
            return jsonify({'error': 'Meet-and-greet not found'}), 404
        if mg['employee_id'] != emp['id']:
            return jsonify({'error': 'Not your meet-and-greet'}), 403

        now = datetime.utcnow().isoformat()
        db.execute(
            'UPDATE meet_greets SET completed = 1, completed_at = ? WHERE id = ?',
            (now, mg_id)
        )
        db.commit()
        log_audit(user['email'], 'meet_greet_completed', 'meet_greet', mg_id,
                  {'contact': mg['contact_name']})

        return jsonify({'success': True})
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Surveys
# ---------------------------------------------------------------------------

@emp_bp.route('/surveys', methods=['GET'])
@login_required
def get_my_surveys():
    """Return surveys (pending and completed) for the current employee."""
    user = get_current_user()
    db = get_db()
    try:
        emp = _get_employee(db, user['email'])
        if not emp:
            return jsonify({'surveys': []}), 200

        rows = db.execute(
            'SELECT * FROM surveys WHERE employee_id = ? ORDER BY id',
            (emp['id'],)
        ).fetchall()

        surveys = []
        for s in rows:
            sd = dict(s)
            # Include existing responses
            if sd['status'] == 'completed':
                responses = db.execute(
                    'SELECT question, answer, rating FROM survey_responses WHERE survey_id = ?',
                    (s['id'],)
                ).fetchall()
                sd['responses'] = [dict(r) for r in responses]
            else:
                # Provide default questions for pending surveys
                sd['questions'] = _survey_questions(sd['survey_type'])
            surveys.append(sd)

        return jsonify({'surveys': surveys})
    finally:
        db.close()


def _survey_questions(survey_type):
    """Return the standard questions for a survey type."""
    base = [
        {'question': 'How is your onboarding experience going?', 'type': 'rating'},
        {'question': 'What has been most helpful so far?', 'type': 'text'},
        {'question': 'What could be improved?', 'type': 'text'},
        {'question': 'Do you feel welcomed and supported?', 'type': 'rating'},
    ]
    if survey_type == 'day_7':
        base.append({'question': 'Is there anything you need that you do not have yet?', 'type': 'text'})
    elif survey_type == 'day_30':
        base.append({'question': 'Do you feel confident in your understanding of your role?', 'type': 'rating'})
        base.append({'question': 'How effective is your manager support?', 'type': 'rating'})
    elif survey_type == 'day_90':
        base.append({'question': 'Do you feel fully productive in your role?', 'type': 'rating'})
        base.append({'question': 'Would you recommend Celito as a great place to work?', 'type': 'rating'})
        base.append({'question': 'Any final suggestions for improving onboarding?', 'type': 'text'})
    elif survey_type == 'exit_program':
        base = [
            {'question': 'Overall, how would you rate the onboarding program?', 'type': 'rating'},
            {'question': 'What was the best part of onboarding?', 'type': 'text'},
            {'question': 'What would you change?', 'type': 'text'},
            {'question': 'Were the tools and resources provided adequate?', 'type': 'rating'},
        ]
    return base


@emp_bp.route('/surveys/<int:survey_id>/respond', methods=['POST'])
@login_required
def respond_to_survey(survey_id):
    """Submit responses for a survey."""
    user = get_current_user()
    db = get_db()
    try:
        emp = _get_employee(db, user['email'])
        if not emp:
            return jsonify({'error': 'No employee record found'}), 404

        survey = db.execute('SELECT * FROM surveys WHERE id = ?', (survey_id,)).fetchone()
        if not survey:
            return jsonify({'error': 'Survey not found'}), 404
        if survey['employee_id'] != emp['id']:
            return jsonify({'error': 'This survey does not belong to you'}), 403
        if survey['status'] == 'completed':
            return jsonify({'error': 'Survey already completed'}), 400

        data = request.get_json() or {}
        responses = data.get('responses', [])
        if not responses:
            return jsonify({'error': 'No responses provided'}), 400

        now = datetime.utcnow().isoformat()
        for resp in responses:
            db.execute(
                'INSERT INTO survey_responses (survey_id, question, answer, rating, created_at) '
                'VALUES (?, ?, ?, ?, ?)',
                (survey_id, resp.get('question', ''), resp.get('answer', ''),
                 resp.get('rating'), now)
            )

        db.execute(
            "UPDATE surveys SET status = 'completed', completed_at = ? WHERE id = ?",
            (now, survey_id)
        )
        db.commit()
        log_audit(user['email'], 'survey_completed', 'survey', survey_id,
                  {'type': survey['survey_type']})

        return jsonify({'success': True, 'message': 'Thank you for your feedback!'})
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Buddy
# ---------------------------------------------------------------------------

@emp_bp.route('/buddy', methods=['GET'])
@login_required
def get_my_buddy():
    """Return buddy profile and buddy-task progress."""
    user = get_current_user()
    db = get_db()
    try:
        emp = _get_employee(db, user['email'])
        if not emp:
            return jsonify({'buddy': None, 'tasks': []}), 200

        buddy = _user_info(db, emp.get('buddy_email'))

        tasks = db.execute(
            'SELECT * FROM buddy_tasks WHERE employee_id = ? ORDER BY due_offset_days',
            (emp['id'],)
        ).fetchall()

        return jsonify({
            'buddy': buddy,
            'tasks': [dict(t) for t in tasks],
        })
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Cohort
# ---------------------------------------------------------------------------

@emp_bp.route('/cohort', methods=['GET'])
@login_required
def get_my_cohort():
    """Return cohort info, members, and shared tasks/events."""
    user = get_current_user()
    db = get_db()
    try:
        emp = _get_employee(db, user['email'])
        if not emp or not emp.get('cohort_id'):
            return jsonify({'cohort': None}), 200

        cohort = db.execute(
            'SELECT * FROM onboarding_cohorts WHERE id = ?', (emp['cohort_id'],)
        ).fetchone()
        if not cohort:
            return jsonify({'cohort': None}), 200

        members = db.execute(
            'SELECT first_name, last_name, email, department, role_title, start_date '
            'FROM employees WHERE cohort_id = ? ORDER BY start_date',
            (emp['cohort_id'],)
        ).fetchall()

        return jsonify({
            'cohort': dict(cohort),
            'members': [dict(m) for m in members],
        })
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Navigation guide
# ---------------------------------------------------------------------------

@emp_bp.route('/navigation-guide', methods=['GET'])
@login_required
def get_navigation_guide():
    """Location-aware 'where to go for what' guide based on department and work mode."""
    user = get_current_user()
    db = get_db()
    try:
        emp = _get_employee(db, user['email'])
        dept = (emp.get('department') or '').lower() if emp else ''
        location_mode = (emp.get('location_mode') or 'office').lower() if emp else 'office'

        # Systems map — core tools everyone uses
        systems = [
            {'name': 'Microsoft Teams', 'description': 'Instant messaging, video calls, team channels'},
            {'name': 'Outlook / O365', 'description': 'Email, calendar, contacts'},
            {'name': 'SharePoint', 'description': 'Document storage, team sites, intranet'},
            {'name': 'Salesforce', 'description': 'CRM — customer records, cases, opportunities'},
            {'name': 'IT Glue', 'description': 'IT documentation and knowledge base'},
            {'name': 'Insperity', 'description': 'Payroll, benefits, PTO requests'},
            {'name': 'Ramp', 'description': 'Corporate card, expense management'},
            {'name': 'Alarm.com', 'description': 'Building access and security'},
        ]

        # Department-specific additions
        if 'sales' in dept:
            systems.extend([
                {'name': 'Bloom Growth', 'description': 'Sales meeting management, L10s'},
                {'name': 'Levitate', 'description': 'Client engagement and outreach'},
                {'name': 'DocuSign', 'description': 'Contract signing and e-signatures'},
            ])
        if 'accounting' in dept or 'finance' in dept:
            systems.append({'name': 'NetSuite', 'description': 'Accounting and financial management'})
        if 'it' in dept or 'engineering' in dept:
            systems.extend([
                {'name': 'PSA / Autotask', 'description': 'Ticketing and project management'},
                {'name': 'Datto', 'description': 'Backup and disaster recovery'},
                {'name': 'PRTG', 'description': 'Network monitoring'},
                {'name': 'Automate', 'description': 'IT automation and RMM'},
            ])

        # Location-specific system additions
        if location_mode == 'remote':
            systems.extend([
                {'name': 'Cisco AnyConnect VPN', 'description': 'Secure connection to company network — required for all remote work', 'badge': '🏠'},
                {'name': 'Zoom / Teams Meetings', 'description': 'All meetings are virtual — keep your setup tested and ready', 'badge': '🏠'},
                {'name': 'Expensify', 'description': 'Home office supply and equipment reimbursements', 'badge': '🏠'},
            ])
        elif location_mode == 'hybrid':
            systems.extend([
                {'name': 'Room Booking System', 'description': 'Reserve conference rooms and desks for your in-office days', 'badge': '🔄'},
                {'name': 'Badge Access App', 'description': 'Mobile badge for building entry — activate before your first office day', 'badge': '🔄'},
                {'name': 'Cisco AnyConnect VPN', 'description': 'Secure connection for remote work days', 'badge': '🔄'},
            ])

        # Who to ask — base entries
        who_to_ask = [
            {'role': 'Service Desk', 'topics': 'Computer, email, VPN, software — submit a ticket or message IT'},
            {'role': 'HR Team', 'topics': 'PTO, payroll, benefits, policies, onboarding questions'},
            {'role': 'Office Manager', 'topics': 'Office access, desk, parking, supplies'},
            {'role': 'Finance', 'topics': 'Ramp card, expense reports, reimbursements'},
            {'role': 'Your Manager', 'topics': 'Projects, priorities, team norms, career growth'},
        ]

        # Location-specific who-to-ask additions
        if location_mode == 'remote':
            who_to_ask.extend([
                {'role': 'IT + Facilities', 'topics': 'Home office setup, equipment shipping, ergonomic requests', 'badge': '🏠'},
                {'role': 'Facilities', 'topics': 'Equipment shipping and returns, supply orders', 'badge': '🏠'},
                {'role': 'Your Manager', 'topics': 'Timezone conflicts, async vs sync decisions, workload balancing', 'badge': '🏠'},
                {'role': 'Your Buddy + HR', 'topics': 'Feeling isolated or disconnected — reach out, it\'s completely normal', 'badge': '🏠'},
            ])
        elif location_mode == 'hybrid':
            who_to_ask.extend([
                {'role': 'Your Manager', 'topics': 'Office schedule questions, which days to come in, team anchor days', 'badge': '🔄'},
                {'role': 'Facilities', 'topics': 'Desk and room booking, hot desking questions, parking', 'badge': '🔄'},
                {'role': 'Team Lead', 'topics': 'In-office vs remote norms, collaboration expectations', 'badge': '🔄'},
            ])

        # Org chart — team hierarchy
        org = []
        if emp:
            team = db.execute(
                'SELECT first_name, last_name, role_title, department, email '
                'FROM employees WHERE department = ? AND status = ? ORDER BY first_name',
                (emp.get('department'), 'active')
            ).fetchall()
            org = [dict(t) for t in team]

        # Common acronyms — base glossary
        glossary = [
            {'term': 'CSM', 'definition': 'Customer Success Manager'},
            {'term': 'SOW', 'definition': 'Statement of Work'},
            {'term': 'MSP', 'definition': 'Managed Service Provider'},
            {'term': 'NOC', 'definition': 'Network Operations Center'},
            {'term': 'SLA', 'definition': 'Service Level Agreement'},
            {'term': 'MFA', 'definition': 'Multi-Factor Authentication'},
            {'term': 'VPN', 'definition': 'Virtual Private Network'},
            {'term': 'PTO', 'definition': 'Paid Time Off'},
            {'term': 'GxP', 'definition': 'Good Practice (e.g. GMP, GLP, GCP)'},
            {'term': 'AD', 'definition': 'Active Directory'},
            {'term': 'O365', 'definition': 'Office 365 (Microsoft 365)'},
            {'term': 'OWA', 'definition': 'Outlook Web Access'},
            {'term': 'PSA', 'definition': 'Professional Services Automation'},
            {'term': 'RMM', 'definition': 'Remote Monitoring and Management'},
            {'term': 'PRTG', 'definition': 'Paessler Router Traffic Grapher (network monitor)'},
            {'term': 'EOD', 'definition': 'End of Day'},
            {'term': '1:1', 'definition': 'One-on-one meeting (usually with your manager)'},
            {'term': 'OKR', 'definition': 'Objectives and Key Results'},
            {'term': 'QBR', 'definition': 'Quarterly Business Review'},
        ]

        # Location-specific glossary additions
        if location_mode == 'remote':
            glossary.extend([
                {'term': 'Async', 'definition': 'Asynchronous communication — messages you respond to on your own schedule', 'badge': '🏠'},
                {'term': 'Sync', 'definition': 'Synchronous communication — real-time calls or meetings', 'badge': '🏠'},
                {'term': 'Core Hours', 'definition': 'Overlap hours when all team members are expected online (check with your manager)', 'badge': '🏠'},
                {'term': 'Camera-On Culture', 'definition': 'Team norm about when video is expected — varies by meeting type', 'badge': '🏠'},
                {'term': 'Virtual Water Cooler', 'definition': 'Casual Slack channels (#random, #watercooler) for non-work chat', 'badge': '🏠'},
                {'term': 'Digital First', 'definition': 'Default to written communication so remote team members aren\'t excluded', 'badge': '🏠'},
            ])
        elif location_mode == 'hybrid':
            glossary.extend([
                {'term': 'Anchor Days', 'definition': 'Designated days when the whole team is in-office together', 'badge': '🔄'},
                {'term': 'Flex Days', 'definition': 'Days you choose whether to work from office or home', 'badge': '🔄'},
                {'term': 'Hot Desking', 'definition': 'Using any available desk rather than an assigned one', 'badge': '🔄'},
                {'term': 'Neighborhood Seating', 'definition': 'Desks grouped by team so you sit near your teammates on office days', 'badge': '🔄'},
            ])

        # Quick tips — location-specific guidance
        quick_tips = []
        if location_mode == 'office':
            quick_tips = [
                'Badge in by 9 AM — the front door locks automatically',
                'Kitchen is on the 2nd floor — coffee, snacks, and a fridge for your lunch',
                'Parking validation is at the front desk',
                'Need a quiet space? Book a focus room in the room booking system',
                'Friday afternoons often have team social events — check the #social channel',
            ]
        elif location_mode == 'remote':
            quick_tips = [
                'Set your Slack status with your timezone so teammates know when you\'re online',
                'Camera on for your first 2 weeks minimum — it helps people learn your face',
                'Don\'t hesitate to ping anyone — remote doesn\'t mean alone',
                'Take a real lunch break away from your desk — you\'ll be more productive',
                'Schedule virtual coffee chats proactively — relationships don\'t build themselves remotely',
                'Block "focus time" on your calendar so meetings don\'t eat your whole day',
                'Share your working hours in your Slack profile and calendar',
            ]
        elif location_mode == 'hybrid':
            quick_tips = [
                'Prioritize in-office days for collaborative work — save deep focus for remote days',
                'Book your desk the day before so you\'re guaranteed a spot',
                'Keep your home setup as a mirror of your office setup for seamless transitions',
                'When in-office, don\'t just sit at your desk — use the time for face-to-face connections',
                'Always dial into meetings from your own device, even in-office, so remote teammates can hear you',
                'Check your team\'s anchor days so you overlap with the right people',
            ]

        return jsonify({
            'systems': systems,
            'who_to_ask': who_to_ask,
            'team': org,
            'glossary': glossary,
            'quick_tips': quick_tips,
            'location_mode': location_mode,
        })
    finally:
        db.close()


# ---------------------------------------------------------------------------
# GET /api/me/documents (enhanced)
# ---------------------------------------------------------------------------

@emp_bp.route('/documents', methods=['GET'])
@login_required
def get_my_documents():
    """List documents available to the employee."""
    user = get_current_user()
    db = get_db()
    try:
        emp = _get_employee(db, user['email'])
        if not emp:
            return jsonify({'documents': []}), 200

        doc_tasks = db.execute(
            "SELECT t.id, t.title, t.description, t.status, t.category, t.is_acknowledgment "
            "FROM tasks t JOIN checklists c ON t.checklist_id = c.id "
            "WHERE c.employee_id = ? AND ("
            "  t.title LIKE '%document%' OR t.title LIKE '%policy%' OR "
            "  t.title LIKE '%handbook%' OR t.title LIKE '%agreement%' OR "
            "  t.title LIKE '%sign%' OR t.title LIKE '%review%' OR "
            "  t.title LIKE '%acknowledge%' OR t.title LIKE '%NDA%' OR "
            "  t.title LIKE '%values%' OR t.title LIKE '%SOP%'"
            ") ORDER BY t.sort_order",
            (emp['id'],)
        ).fetchall()

        documents = []
        for dt in doc_tasks:
            documents.append({
                'id': dt['id'],
                'title': dt['title'],
                'description': dt['description'],
                'status': 'Completed' if dt['status'] == 'completed' else 'Pending Review',
                'category': dt['category'],
                'action_required': dt['status'] != 'completed',
                'is_acknowledgment': bool(dt.get('is_acknowledgment')),
            })

        return jsonify({'documents': documents})
    finally:
        db.close()


# ---------------------------------------------------------------------------
# GET /api/me/schedule (enhanced)
# ---------------------------------------------------------------------------

@emp_bp.route('/schedule', methods=['GET'])
@login_required
def get_my_schedule():
    """Return the employee's first-week schedule, built from onboarding tasks
    and enriched with suggested time slots."""
    user = get_current_user()
    db = get_db()
    try:
        emp = _get_employee(db, user['email'])
        if not emp:
            return jsonify({'schedule': [], 'start_date': None}), 200

        start = emp.get('start_date')
        if not start:
            return jsonify({'schedule': [], 'start_date': None}), 200

        # Tasks due in the first 7 days
        tasks = db.execute(
            "SELECT t.title, t.description, t.due_date, t.category, t.status, "
            "  t.assigned_to, t.phase "
            "FROM tasks t JOIN checklists c ON t.checklist_id = c.id "
            "WHERE c.employee_id = ? AND c.checklist_type = 'onboarding' "
            "AND t.due_date >= ? AND t.due_date <= date(?, '+6 days') "
            "ORDER BY t.due_date, t.sort_order",
            (emp['id'], start, start)
        ).fetchall()

        # Suggested time-slot assignments for common task types
        time_hints = {
            'orientation': '9:00 AM',
            'welcome': '9:00 AM',
            'CEO': '10:00 AM',
            'leadership': '10:00 AM',
            'lunch': '12:00 PM',
            'coffee': '10:30 AM',
            'SOP': '2:00 PM',
            'training': '2:00 PM',
            'policy': '3:00 PM',
            '1:1': '11:00 AM',
            'check-in': '11:00 AM',
            'buddy': '10:30 AM',
            'shadow': '1:00 PM',
        }

        schedule = {}
        for t in tasks:
            d = t['due_date']
            title_lower = (t['title'] or '').lower()
            suggested_time = None
            for keyword, time in time_hints.items():
                if keyword.lower() in title_lower:
                    suggested_time = time
                    break
            item = {
                'title': t['title'],
                'description': t['description'],
                'category': t['category'],
                'status': t['status'],
                'assigned_to': t['assigned_to'],
                'suggested_time': suggested_time,
            }
            schedule.setdefault(d, []).append(item)

        # Sort items within each day by suggested time
        for d in schedule:
            schedule[d].sort(key=lambda x: x.get('suggested_time') or '23:59')

        schedule_list = [
            {'date': d, 'day_name': date.fromisoformat(d).strftime('%A'), 'items': items}
            for d, items in sorted(schedule.items())
        ]

        return jsonify({
            'start_date': start,
            'schedule': schedule_list,
        })
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Buddy self-service (for people who ARE a buddy)
# ---------------------------------------------------------------------------

@emp_bp.route('/buddy-assignments', methods=['GET'])
@login_required
def get_buddy_assignments():
    """Return the list of new hires this user is a buddy for."""
    user = get_current_user()
    db = get_db()
    try:
        # Find employees where buddy_email = current user
        employees = db.execute(
            "SELECT id, first_name, last_name, email, department, role_title, start_date, status "
            "FROM employees WHERE buddy_email = ? AND status IN ('pending', 'active')",
            (user['email'],)
        ).fetchall()

        result = []
        for emp in employees:
            ed = dict(emp)
            tasks = db.execute(
                'SELECT * FROM buddy_tasks WHERE employee_id = ? AND buddy_email = ? '
                'ORDER BY due_offset_days',
                (emp['id'], user['email'])
            ).fetchall()
            ed['buddy_tasks'] = [dict(t) for t in tasks]
            result.append(ed)

        return jsonify({'assignments': result})
    finally:
        db.close()


@emp_bp.route('/buddy-assignee', methods=['GET'])
@login_required
def get_buddy_assignee_progress():
    """
    Return the onboarding progress of the employee(s) this user is buddied with.

    Buddies can see: phase progress, task completion %, this week's tasks,
    flagged blockers, and milestones — so they can help proactively instead
    of flying blind.
    """
    user = get_current_user()
    email = user['email']
    db = get_db()
    try:
        # Find the employee this user is buddy for (active onboarding)
        assignee = db.execute(
            "SELECT id, first_name, last_name, email, department, role_title, "
            "start_date, status, location_mode "
            "FROM employees WHERE buddy_email = ? AND status IN ('pending', 'active')",
            (email,)
        ).fetchone()

        if not assignee:
            return jsonify({'assignee': None, 'message': 'No active buddy assignment found'})

        emp_id = assignee['id']

        # Get task progress
        tasks = db.execute(
            "SELECT id, title, phase, category, status, due_date, assigned_email "
            "FROM tasks WHERE employee_id = ? ORDER BY due_date",
            (emp_id,)
        ).fetchall()

        total = len(tasks)
        completed = sum(1 for t in tasks if t['status'] == 'completed')

        # Get tasks due this week
        today = date.today().isoformat()
        week_end = (date.today() + timedelta(days=7)).isoformat()
        this_week = [dict(t) for t in tasks
                     if t['due_date'] and t['due_date'] <= week_end
                     and t['status'] not in ('completed', 'skipped')]

        # Phase breakdown
        phases = {}
        for t in tasks:
            p = t['phase'] or 'general'
            if p not in phases:
                phases[p] = {'total': 0, 'completed': 0}
            phases[p]['total'] += 1
            if t['status'] == 'completed':
                phases[p]['completed'] += 1

        # Blocked tasks — status = 'blocked'
        blocked_tasks = [dict(t) for t in tasks if t['status'] == 'blocked']

        # Recent blocker comments (marked with [BLOCKER] prefix)
        try:
            blockers = db.execute(
                "SELECT tc.task_id, tc.comment, tc.created_at, tc.user_email, t.title "
                "FROM task_comments tc JOIN tasks t ON tc.task_id = t.id "
                "WHERE t.employee_id = ? AND tc.comment LIKE '%[BLOCKER]%' "
                "ORDER BY tc.created_at DESC LIMIT 5",
                (emp_id,)
            ).fetchall()
            blockers = [dict(b) for b in blockers]
        except Exception:
            blockers = []

        return jsonify({
            'assignee': {
                'id': emp_id,
                'name': f"{assignee['first_name']} {assignee['last_name']}",
                'email': assignee['email'],
                'department': assignee['department'] or '',
                'role_title': assignee['role_title'] or '',
                'start_date': assignee['start_date'],
                'location_mode': assignee['location_mode'] or '',
            },
            'progress': {
                'total_tasks': total,
                'completed_tasks': completed,
                'percent': round(completed / total * 100) if total else 0,
                'phases': phases,
            },
            'this_week': this_week[:10],
            'blockers': blockers,
            'blocked_tasks': blocked_tasks[:10],
        })
    finally:
        db.close()


@emp_bp.route('/tasks/<int:task_id>/comments', methods=['GET'])
@login_required
def get_my_task_comments(task_id):
    """Get comments on an employee's own onboarding task."""
    user = get_current_user()
    db = get_db()
    try:
        emp = _get_employee(db, user['email'])
        if not emp:
            return jsonify({'error': 'Employee record not found'}), 404

        task = db.execute(
            'SELECT t.id, t.employee_id FROM tasks t WHERE t.id = ? AND t.employee_id = ?',
            (task_id, emp['id'])
        ).fetchone()
        if not task:
            return jsonify({'error': 'Task not found'}), 404

        comments = db.execute(
            'SELECT * FROM task_comments WHERE task_id = ? ORDER BY created_at ASC',
            (task_id,)
        ).fetchall()
        return jsonify([dict(c) for c in comments])
    finally:
        db.close()


@emp_bp.route('/tasks/<int:task_id>/comment', methods=['POST'])
@login_required
def add_my_task_comment(task_id):
    """Employee can add a comment or flag a blocker on their own onboarding tasks."""
    user = get_current_user()
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Request body required'}), 400

    comment_text = (data.get('comment') or '').strip()
    is_blocker = data.get('is_blocker', False)

    if not comment_text:
        return jsonify({'error': 'Comment cannot be empty'}), 400
    if len(comment_text) > 2000:
        return jsonify({'error': 'Comment too long (max 2000 characters)'}), 400

    db = get_db()
    try:
        emp = _get_employee(db, user['email'])
        if not emp:
            return jsonify({'error': 'Employee record not found'}), 404

        task = db.execute(
            'SELECT t.id, t.employee_id, t.title FROM tasks t WHERE t.id = ? AND t.employee_id = ?',
            (task_id, emp['id'])
        ).fetchone()
        if not task:
            return jsonify({'error': 'Task not found'}), 404

        if is_blocker:
            comment_text = '[BLOCKER] ' + comment_text

        now = datetime.utcnow().isoformat()
        db.execute(
            'INSERT INTO task_comments (task_id, user_email, comment, created_at) VALUES (?, ?, ?, ?)',
            (task_id, user['email'], comment_text, now)
        )
        db.commit()

        log_audit(user['email'], 'task_comment', 'task', task_id,
                  {'is_blocker': is_blocker, 'task_title': task['title']})

        return jsonify({'success': True, 'message': 'Comment added'})
    finally:
        db.close()


@emp_bp.route('/buddy-tasks/<int:task_id>/complete', methods=['PUT'])
@login_required
def complete_buddy_task(task_id):
    """Mark a buddy task as completed. Only the assigned buddy can do this."""
    user = get_current_user()
    db = get_db()
    try:
        task = db.execute('SELECT * FROM buddy_tasks WHERE id = ?', (task_id,)).fetchone()
        if not task:
            return jsonify({'error': 'Buddy task not found'}), 404
        if task['buddy_email'] != user['email']:
            return jsonify({'error': 'This task is not assigned to you'}), 403

        now = datetime.utcnow().isoformat()
        db.execute(
            "UPDATE buddy_tasks SET status = 'completed', completed_at = ? WHERE id = ?",
            (now, task_id)
        )
        db.commit()
        log_audit(user['email'], 'buddy_task_completed', 'buddy_task', task_id,
                  {'title': task['title']})

        return jsonify({'success': True})
    finally:
        db.close()


@emp_bp.route('/notifications', methods=['GET'])
@login_required
def get_notifications():
    """Get notifications for the current user by aggregating recent activity.

    Returns items that need attention: overdue tasks, new task assignments,
    blocker comments on tasks, upcoming milestones, pending surveys,
    and buddy tasks (if the user is a buddy).
    No new database table required — this queries existing data.
    """
    user = get_current_user()
    db = get_db()
    try:
        email = user['email']
        notifications = []
        now = datetime.utcnow()
        today_str = date.today().isoformat()

        # 1. Overdue tasks assigned to this user
        overdue = db.execute(
            "SELECT t.id, t.title, t.due_date, t.employee_id, "
            "  e.first_name || ' ' || e.last_name AS employee_name "
            "FROM tasks t "
            "JOIN employees e ON t.employee_id = e.id "
            "WHERE t.assigned_email = ? AND t.status NOT IN ('completed','skipped') "
            "  AND t.due_date < ? "
            "ORDER BY t.due_date ASC",
            (email, today_str)
        ).fetchall()
        for t in overdue:
            notifications.append({
                'type': 'overdue_task',
                'icon': '⚠️',
                'title': f'Overdue: {t["title"]}',
                'subtitle': f'For {t["employee_name"]} · Due {t["due_date"]}',
                'task_id': t['id'],
                'employee_id': t['employee_id'],
                'priority': 'high',
                'date': t['due_date'],
            })

        # 2. Tasks due this week assigned to this user
        week_end = (date.today() + timedelta(days=7)).isoformat()
        upcoming = db.execute(
            "SELECT t.id, t.title, t.due_date, t.employee_id, "
            "  e.first_name || ' ' || e.last_name AS employee_name "
            "FROM tasks t "
            "JOIN employees e ON t.employee_id = e.id "
            "WHERE t.assigned_email = ? AND t.status NOT IN ('completed','skipped') "
            "  AND t.due_date >= ? AND t.due_date <= ? "
            "ORDER BY t.due_date ASC",
            (email, today_str, week_end)
        ).fetchall()
        for t in upcoming:
            notifications.append({
                'type': 'upcoming_task',
                'icon': '📋',
                'title': t['title'],
                'subtitle': f'For {t["employee_name"]} · Due {t["due_date"]}',
                'task_id': t['id'],
                'employee_id': t['employee_id'],
                'priority': 'medium',
                'date': t['due_date'],
            })

        # 3. Blocker comments on tasks the user is responsible for (last 7 days)
        week_ago = (now - timedelta(days=7)).isoformat()
        blockers = db.execute(
            "SELECT tc.id, tc.comment, tc.created_at, tc.task_id, t.title AS task_title, "
            "  tc.user_email, e.first_name || ' ' || e.last_name AS employee_name, t.employee_id "
            "FROM task_comments tc "
            "JOIN tasks t ON tc.task_id = t.id "
            "JOIN employees e ON t.employee_id = e.id "
            "WHERE t.assigned_email = ? AND tc.comment LIKE '[BLOCKER]%' "
            "  AND tc.created_at >= ? AND tc.user_email != ? "
            "ORDER BY tc.created_at DESC",
            (email, week_ago, email)
        ).fetchall()
        for b in blockers:
            notifications.append({
                'type': 'blocker',
                'icon': '🚫',
                'title': f'Blocker on: {b["task_title"]}',
                'subtitle': f'{b["employee_name"]} flagged: {b["comment"][:80]}',
                'task_id': b['task_id'],
                'employee_id': b['employee_id'],
                'priority': 'high',
                'date': b['created_at'],
            })

        # 4. Pending surveys for employees (employee self-service)
        emp = _get_employee(db, email)
        if emp:
            surveys = db.execute(
                "SELECT id, survey_type, sent_at FROM surveys "
                "WHERE employee_id = ? AND status IN ('sent', 'pending') "
                "ORDER BY sent_at",
                (emp['id'],)
            ).fetchall()
            for s in surveys:
                notifications.append({
                    'type': 'survey',
                    'icon': '📝',
                    'title': f'Pulse survey ready: {s["survey_type"].replace("_", " ").title()}',
                    'subtitle': 'Share your onboarding feedback',
                    'priority': 'low',
                    'date': s['sent_at'] or today_str,
                })

        # 5. Overdue buddy tasks (if user is a buddy)
        buddy_overdue = db.execute(
            "SELECT bt.id, bt.title, bt.due_offset_days, bt.employee_id, "
            "  e.first_name || ' ' || e.last_name AS employee_name, e.start_date "
            "FROM buddy_tasks bt "
            "JOIN employees e ON bt.employee_id = e.id "
            "WHERE bt.buddy_email = ? AND bt.status NOT IN ('completed','skipped')",
            (email,)
        ).fetchall()
        for bt in buddy_overdue:
            if bt['start_date'] and bt['due_offset_days'] is not None:
                try:
                    start = date.fromisoformat(bt['start_date'])
                    due = (start + timedelta(days=bt['due_offset_days'])).isoformat()
                    if due < today_str:
                        notifications.append({
                            'type': 'buddy_task',
                            'icon': '🤝',
                            'title': f'Buddy task overdue: {bt["title"]}',
                            'subtitle': f'For {bt["employee_name"]}',
                            'priority': 'medium',
                            'date': due,
                        })
                except (ValueError, TypeError):
                    pass

        # 6. Upcoming milestones for manager's direct reports (next 14 days)
        milestone_end = (date.today() + timedelta(days=14)).isoformat()
        milestones = db.execute(
            "SELECT m.id, m.title, m.day_marker, m.employee_id, "
            "  e.first_name || ' ' || e.last_name AS employee_name, e.start_date "
            "FROM milestones m "
            "JOIN employees e ON m.employee_id = e.id "
            "WHERE e.manager_email = ? AND m.status NOT IN ('achieved','missed') ",
            (email,)
        ).fetchall()
        for m in milestones:
            if m['start_date']:
                try:
                    start = date.fromisoformat(m['start_date'])
                    target = (start + timedelta(days=m['day_marker'])).isoformat()
                    if today_str <= target <= milestone_end:
                        notifications.append({
                            'type': 'milestone',
                            'icon': '🎯',
                            'title': f'Milestone: {m["title"]}',
                            'subtitle': f'{m["employee_name"]} · Day {m["day_marker"]} · Target {target}',
                            'employee_id': m['employee_id'],
                            'priority': 'medium',
                            'date': target,
                        })
                except (ValueError, TypeError):
                    pass

        # Sort: high priority first, then by date
        priority_order = {'high': 0, 'medium': 1, 'low': 2}
        notifications.sort(key=lambda n: (priority_order.get(n.get('priority', 'low'), 2), n.get('date', '')))

        return jsonify({
            'notifications': notifications,
            'count': len(notifications),
            'high_count': sum(1 for n in notifications if n.get('priority') == 'high'),
        })
    finally:
        db.close()
