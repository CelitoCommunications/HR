"""
Admin routes for the Celito Onboarding & Offboarding Platform.
Handles user management, settings, audit logs, integration testing,
checklist templates, cohorts, surveys, equipment, metrics, and bulk ops.
"""

import json
import logging
from datetime import datetime, date, timedelta

from flask import Blueprint, request, jsonify

from .auth import login_required, role_required, get_current_user
from .db import get_db, take_monthly_snapshot

logger = logging.getLogger(__name__)

admin_bp = Blueprint('admin', __name__, url_prefix='/api/admin')


def _audit(action, target_type=None, target_id=None, details=None):
    """Log an action to the audit trail."""
    user = get_current_user()
    db = get_db()
    try:
        db.execute(
            'INSERT INTO audit_log (user_email, action, target_type, target_id, details, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (user['email'], action, target_type, str(target_id) if target_id else None,
             json.dumps(details) if details else None, datetime.utcnow().isoformat())
        )
        db.commit()
    finally:
        db.close()


# ── User Management ──────────────────────────────────────────────────────────

@admin_bp.route('/users', methods=['GET'])
@login_required
@role_required(['admin'])
def list_users():
    """List all users."""
    db = get_db()
    try:
        rows = db.execute(
            'SELECT id, email, display_name, role, department, created_at, last_login '
            'FROM users ORDER BY display_name'
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        db.close()


@admin_bp.route('/users', methods=['POST'])
@login_required
@role_required(['admin'])
def create_user():
    """Create or invite a user."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Request body required'}), 400

    email = (data.get('email') or '').strip().lower()
    display_name = (data.get('display_name') or '').strip()
    role = (data.get('role') or 'employee').strip().lower()
    department = (data.get('department') or '').strip()

    if not email:
        return jsonify({'error': 'Email is required'}), 400
    if role not in ('admin', 'manager', 'hr', 'employee'):
        return jsonify({'error': 'Invalid role. Must be admin, manager, hr, or employee'}), 400

    db = get_db()
    try:
        existing = db.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone()
        if existing:
            return jsonify({'error': 'User with this email already exists'}), 409

        db.execute(
            'INSERT INTO users (email, display_name, role, department, created_at) VALUES (?, ?, ?, ?, ?)',
            (email, display_name, role, department, datetime.utcnow().isoformat())
        )
        db.commit()

        user_row = db.execute(
            'SELECT id, email, display_name, role, department, created_at FROM users WHERE email = ?',
            (email,)
        ).fetchone()
    finally:
        db.close()

    _audit('user_created', 'user', user_row['id'], {'email': email, 'role': role})
    logger.info(f"User created: {email} with role {role}")

    return jsonify(dict(user_row)), 201


@admin_bp.route('/users/<int:user_id>', methods=['PUT'])
@login_required
@role_required(['admin'])
def update_user(user_id):
    """Update a user's role or department."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Request body required'}), 400

    db = get_db()
    try:
        user_row = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        if not user_row:
            return jsonify({'error': 'User not found'}), 404

        updates = []
        params = []
        changes = {}

        if 'role' in data:
            role = data['role'].strip().lower()
            if role not in ('admin', 'manager', 'hr', 'employee', 'disabled'):
                return jsonify({'error': 'Invalid role'}), 400
            updates.append('role = ?')
            params.append(role)
            changes['role'] = {'from': user_row['role'], 'to': role}
            if role == 'disabled':
                _audit('user_disabled', 'user', user_id, {'email': user_row['email']})

        if 'department' in data:
            dept = data['department'].strip()
            updates.append('department = ?')
            params.append(dept)
            changes['department'] = {'from': user_row['department'], 'to': dept}

        if 'display_name' in data:
            name = data['display_name'].strip()
            updates.append('display_name = ?')
            params.append(name)
            changes['display_name'] = {'from': user_row['display_name'], 'to': name}

        if not updates:
            return jsonify({'error': 'No fields to update'}), 400

        params.append(user_id)
        db.execute(f'UPDATE users SET {", ".join(updates)} WHERE id = ?', params)
        db.commit()

        updated = db.execute(
            'SELECT id, email, display_name, role, department, created_at, last_login FROM users WHERE id = ?',
            (user_id,)
        ).fetchone()
        result = dict(updated)
    finally:
        db.close()

    _audit('user_updated', 'user', user_id, changes)
    return jsonify(result)


@admin_bp.route('/users/<int:user_id>', methods=['DELETE'])
@login_required
@role_required(['admin'])
def delete_user(user_id):
    """Soft-delete (deactivate) a user by setting role to 'disabled'."""
    db = get_db()
    try:
        user_row = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        if not user_row:
            return jsonify({'error': 'User not found'}), 404

        current_user = get_current_user()
        if user_row['email'] == current_user['email']:
            return jsonify({'error': 'Cannot deactivate yourself'}), 400

        db.execute('UPDATE users SET role = ? WHERE id = ?', ('disabled', user_id))
        db.commit()
    finally:
        db.close()

    _audit('user_deactivated', 'user', user_id, {'email': user_row['email']})
    return jsonify({'message': f"User {user_row['email']} deactivated"})


# ── App Settings ─────────────────────────────────────────────────────────────

@admin_bp.route('/settings', methods=['GET'])
@login_required
@role_required(['admin'])
def get_settings():
    """Get application settings (non-secret values only)."""
    db = get_db()
    try:
        rows = db.execute("SELECT key, value FROM app_settings WHERE key NOT LIKE '%secret%'").fetchall()
        settings = {r['key']: r['value'] for r in rows}
        return jsonify(settings)
    finally:
        db.close()


@admin_bp.route('/settings', methods=['PUT'])
@login_required
@role_required(['admin'])
def update_settings():
    """Update application settings."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Request body required'}), 400

    db = get_db()
    try:
        for key, value in data.items():
            existing = db.execute('SELECT key FROM app_settings WHERE key = ?', (key,)).fetchone()
            if existing:
                db.execute('UPDATE app_settings SET value = ? WHERE key = ?', (str(value), key))
            else:
                db.execute('INSERT INTO app_settings (key, value) VALUES (?, ?)', (key, str(value)))
        db.commit()
    finally:
        db.close()

    _audit('settings_updated', 'settings', None, {'keys': list(data.keys())})
    return jsonify({'message': 'Settings updated', 'updated_keys': list(data.keys())})


# ── Audit Log ────────────────────────────────────────────────────────────────

@admin_bp.route('/audit-log', methods=['GET'])
@login_required
@role_required(['admin'])
def get_audit_log():
    """Get paginated audit log entries with filtering."""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    per_page = min(per_page, 200)
    offset = (page - 1) * per_page

    filters = []
    params = []

    action_filter = request.args.get('action')
    if action_filter:
        filters.append('action = ?')
        params.append(action_filter)

    email_filter = request.args.get('user_email')
    if email_filter:
        filters.append('user_email LIKE ?')
        params.append(f'%{email_filter}%')

    date_from = request.args.get('from')
    if date_from:
        filters.append('created_at >= ?')
        params.append(date_from)

    date_to = request.args.get('to')
    if date_to:
        filters.append('created_at <= ?')
        params.append(date_to + ' 23:59:59')

    q = request.args.get('q', '').strip()
    if q:
        filters.append('(user_email LIKE ? OR action LIKE ? OR details LIKE ?)')
        q_param = f'%{q}%'
        params.extend([q_param, q_param, q_param])

    where = ''
    if filters:
        where = 'WHERE ' + ' AND '.join(filters)

    db = get_db()
    try:
        total = db.execute(f'SELECT COUNT(*) as cnt FROM audit_log {where}', params).fetchone()['cnt']

        params_with_pagination = params + [per_page, offset]
        rows = db.execute(
            f'SELECT * FROM audit_log {where} ORDER BY created_at DESC LIMIT ? OFFSET ?',
            params_with_pagination
        ).fetchall()

        entries = []
        for r in rows:
            entry = dict(r)
            if entry.get('details'):
                try:
                    entry['details'] = json.loads(entry['details'])
                except (json.JSONDecodeError, TypeError):
                    pass
            entries.append(entry)
    finally:
        db.close()

    return jsonify({
        'entries': entries,
        'page': page,
        'per_page': per_page,
        'total': total,
        'pages': (total + per_page - 1) // per_page
    })


@admin_bp.route('/audit-log/actions', methods=['GET'])
@login_required
@role_required(['admin'])
def get_audit_actions():
    """Get distinct action types for the audit log filter dropdown."""
    db = get_db()
    try:
        rows = db.execute(
            'SELECT DISTINCT action FROM audit_log ORDER BY action'
        ).fetchall()
    finally:
        db.close()
    return jsonify({'actions': [r['action'] for r in rows]})


# ── Entra ID User Directory ─────────────────────────────────────────────────

@admin_bp.route('/entra-users', methods=['GET'])
@login_required
@role_required(['admin', 'hr'])
def list_entra_users():
    """
    Fetch celito.net users from Microsoft Entra ID via Graph API.

    Uses the Client Credentials flow (app-level permission User.Read.All)
    to list all users whose mail ends with @celito.net, excluding
    resource accounts (meeting rooms, shared mailboxes without a surname).
    Results are cached for 10 minutes.
    """
    import time
    import requests as req
    from .config import config

    # Simple in-memory cache
    cache = getattr(list_entra_users, '_cache', None)
    if cache and time.time() - cache['ts'] < 600:
        return jsonify(cache['data'])

    try:
        tenant_id = config.get('entra.tenant_id', '')
        client_id = config.get('entra.client_id', '')
        client_secret = config.get('entra.client_secret', '')

        # Get app-level token via Client Credentials
        token_resp = req.post(
            f'https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token',
            data={
                'grant_type': 'client_credentials',
                'client_id': client_id,
                'client_secret': client_secret,
                'scope': 'https://graph.microsoft.com/.default',
            },
            timeout=15,
        )
        token_resp.raise_for_status()
        access_token = token_resp.json()['access_token']

        # Query Graph for celito.net users — filter out rooms/resources
        # accountEnabled eq true filters out disabled accounts
        graph_url = (
            "https://graph.microsoft.com/v1.0/users"
            "?$filter=accountEnabled eq true and endsWith(mail,'@celito.net')"
            "&$select=id,displayName,mail,jobTitle,department"
            "&$top=999"
            "&$orderby=displayName"
            "&$count=true"
        )
        graph_resp = req.get(
            graph_url,
            headers={
                'Authorization': f'Bearer {access_token}',
                'ConsistencyLevel': 'eventual',
            },
            timeout=15,
        )
        graph_resp.raise_for_status()
        raw_users = graph_resp.json().get('value', [])

        # Filter out meeting rooms and resource accounts:
        # - Must have a displayName with a space (first + last name)
        # - Must have a mail address
        users = [
            {
                'email': u['mail'].lower(),
                'display_name': u['displayName'],
                'job_title': u.get('jobTitle') or '',
                'department': u.get('department') or '',
            }
            for u in raw_users
            if u.get('mail')
            and ' ' in (u.get('displayName') or '')
        ]

        # Cache the results
        list_entra_users._cache = {'ts': time.time(), 'data': users}

        return jsonify(users)

    except Exception as e:
        logger.error(f"Failed to fetch Entra users: {e}")
        return jsonify({'error': f'Could not load Entra users: {str(e)}'}), 500


# ── Integration Tests ────────────────────────────────────────────────────────

@admin_bp.route('/test-salesforce', methods=['POST'])
@login_required
@role_required(['admin'])
def test_salesforce():
    """Test the Salesforce connection."""
    try:
        from .salesforce_client import SalesforceClient
        from .config import config

        sf = SalesforceClient()
        sf.authenticate()
        result = sf.query("SELECT Id, Name FROM Account LIMIT 1")
        record_count = len(result) if isinstance(result, list) else 0

        _audit('test_salesforce', 'integration', None, {'status': 'success', 'records': record_count})
        return jsonify({
            'status': 'connected',
            'connected': True,
            'message': f'Successfully connected to Salesforce. Found {record_count} test record(s).',
            'instance_url': sf._instance_url
        })
    except Exception as e:
        logger.error(f"Salesforce connection test failed: {e}")
        _audit('test_salesforce', 'integration', None, {'status': 'failed', 'error': str(e)})
        return jsonify({'status': 'error', 'message': f'Connection failed: {str(e)}'}), 500


@admin_bp.route('/test-slack', methods=['POST'])
@login_required
@role_required(['admin'])
def test_slack():
    """Test the Slack connection."""
    try:
        from .slack_client import SlackClient
        from .config import config

        slack = SlackClient()
        result = slack._call('GET', 'auth.test')

        _audit('test_slack', 'integration', None, {'status': 'success', 'team': result.get('team')})
        return jsonify({
            'status': 'connected',
            'connected': True,
            'message': f"Connected to Slack workspace: {result.get('team', 'Unknown')}",
            'team': result.get('team'),
            'bot_user': result.get('user')
        })
    except Exception as e:
        logger.error(f"Slack connection test failed: {e}")
        _audit('test_slack', 'integration', None, {'status': 'failed', 'error': str(e)})
        return jsonify({'status': 'error', 'message': f'Connection failed: {str(e)}'}), 500


@admin_bp.route('/test-anthropic', methods=['POST'])
@login_required
@role_required(['admin'])
def test_anthropic():
    """Test the Anthropic/Claude AI connection."""
    try:
        from .config import config
        import requests

        api_key = config.get('anthropic.api_key')
        if not api_key or api_key.startswith('YOUR_'):
            return jsonify({'status': 'error', 'message': 'Anthropic API key not configured'}), 500

        model = config.get('anthropic.model', 'claude-sonnet-4-6')
        resp = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key': api_key,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            },
            json={
                'model': model,
                'max_tokens': 10,
                'messages': [{'role': 'user', 'content': 'Reply with OK'}],
            },
            timeout=15,
        )
        if resp.status_code == 200:
            _audit('test_anthropic', 'integration', None, {'status': 'success', 'model': model})
            return jsonify({'status': 'connected', 'connected': True, 'message': f'Claude AI ({model}) is connected', 'model': model})
        else:
            error_msg = resp.json().get('error', {}).get('message', resp.text[:200])
            _audit('test_anthropic', 'integration', None, {'status': 'failed', 'error': error_msg})
            return jsonify({'status': 'error', 'message': f'API returned {resp.status_code}: {error_msg}'}), 500
    except Exception as e:
        logger.error(f"Anthropic connection test failed: {e}")
        _audit('test_anthropic', 'integration', None, {'status': 'failed', 'error': str(e)})
        return jsonify({'status': 'error', 'message': f'Connection failed: {str(e)}'}), 500


# ── Dashboard Stats (Enhanced) ───────────────────────────────────────────────

@admin_bp.route('/stats', methods=['GET'])
@login_required
@role_required(['admin', 'hr'])
def get_stats():
    """Get comprehensive dashboard statistics for admin view."""
    db = get_db()
    try:
        today = date.today().isoformat()
        month_start = date.today().replace(day=1).isoformat()
        year_ago = (date.today() - timedelta(days=365)).isoformat()

        # ── Core counts ──
        total_employees = db.execute(
            "SELECT COUNT(*) as cnt FROM employees WHERE status != 'departed'"
        ).fetchone()['cnt']

        active_onboardings = db.execute(
            "SELECT COUNT(*) as cnt FROM checklists WHERE checklist_type = 'onboarding' AND status = 'active'"
        ).fetchone()['cnt']

        active_offboardings = db.execute(
            "SELECT COUNT(*) as cnt FROM checklists WHERE checklist_type = 'offboarding' AND status = 'active'"
        ).fetchone()['cnt']

        overdue_tasks = db.execute(
            "SELECT COUNT(*) as cnt FROM tasks WHERE status NOT IN ('completed', 'skipped') AND due_date < ?",
            (today,)
        ).fetchone()['cnt']

        # ── Monthly counts ──
        monthly_hires = db.execute(
            "SELECT COUNT(*) as cnt FROM employees WHERE start_date >= ? AND start_date <= ?",
            (month_start, today)
        ).fetchone()['cnt']

        monthly_departures = db.execute(
            "SELECT COUNT(*) as cnt FROM employees WHERE end_date >= ? AND end_date <= ?",
            (month_start, today)
        ).fetchone()['cnt']

        # ── Pre-boarding readiness ──
        thirty_days_out = (date.today() + timedelta(days=30)).isoformat()
        upcoming_rows = db.execute(
            "SELECT id, first_name, last_name, department, start_date, location_mode "
            "FROM employees WHERE status = 'pending' AND start_date >= ? AND start_date <= ? "
            "ORDER BY start_date",
            (today, thirty_days_out)
        ).fetchall()

        pre_boarding_readiness = []
        for emp in upcoming_rows:
            eid = emp['id']
            total_pre = db.execute(
                "SELECT COUNT(*) as cnt FROM tasks t JOIN checklists c ON t.checklist_id = c.id "
                "WHERE c.employee_id = ? AND t.phase = 'pre_boarding'", (eid,)
            ).fetchone()['cnt']
            done_pre = db.execute(
                "SELECT COUNT(*) as cnt FROM tasks t JOIN checklists c ON t.checklist_id = c.id "
                "WHERE c.employee_id = ? AND t.phase = 'pre_boarding' AND t.status = 'completed'",
                (eid,)
            ).fetchone()['cnt']
            pct = round(done_pre / total_pre * 100) if total_pre else 0
            pre_boarding_readiness.append({
                'id': eid,
                'name': f"{emp['first_name']} {emp['last_name']}",
                'department': emp['department'],
                'start_date': emp['start_date'],
                'location_mode': emp['location_mode'],
                'readiness_pct': pct,
                'tasks_done': done_pre,
                'tasks_total': total_pre
            })

        # ── Time-to-ramp (last 12 months) ──
        ramp_row = db.execute(
            "SELECT AVG(julianday(ramped_at) - julianday(start_date)) as avg_days "
            "FROM employees WHERE ramped_at IS NOT NULL AND ramped_at >= ?",
            (year_ago,)
        ).fetchone()
        avg_time_to_ramp = round(ramp_row['avg_days'], 1) if ramp_row['avg_days'] else None

        # ── 90-day retention (last 12 months) ──
        eligible = db.execute(
            "SELECT COUNT(*) as cnt FROM employees "
            "WHERE start_date >= ? AND start_date <= date(?, '-90 days')",
            (year_ago, today)
        ).fetchone()['cnt']
        retained = db.execute(
            "SELECT COUNT(*) as cnt FROM employees "
            "WHERE start_date >= ? AND start_date <= date(?, '-90 days') "
            "AND (status = 'active' OR (end_date IS NOT NULL AND julianday(end_date) - julianday(start_date) >= 90))",
            (year_ago, today)
        ).fetchone()['cnt']
        retention_90_day = round(retained / eligible * 100, 1) if eligible else None

        # ── New-hire NPS (last 12 months) ──
        nps_row = db.execute(
            "SELECT AVG(sr.rating) as avg_rating FROM survey_responses sr "
            "JOIN surveys s ON sr.survey_id = s.id "
            "WHERE s.survey_type IN ('day_30', 'day_90') AND sr.rating IS NOT NULL "
            "AND sr.created_at >= ?",
            (year_ago,)
        ).fetchone()
        new_hire_nps = round(nps_row['avg_rating'], 1) if nps_row['avg_rating'] else None

        # ── Tasks on-time % (last 12 months) ──
        completed_total = db.execute(
            "SELECT COUNT(*) as cnt FROM tasks WHERE status = 'completed' AND completed_at >= ?",
            (year_ago,)
        ).fetchone()['cnt']
        completed_on_time = db.execute(
            "SELECT COUNT(*) as cnt FROM tasks "
            "WHERE status = 'completed' AND completed_at >= ? "
            "AND (due_date IS NULL OR completed_at <= due_date)",
            (year_ago,)
        ).fetchone()['cnt']
        tasks_on_time_pct = round(completed_on_time / completed_total * 100, 1) if completed_total else None

        # ── Phase breakdown ──
        phase_rows = db.execute(
            "SELECT t.phase, COUNT(DISTINCT c.employee_id) as cnt "
            "FROM tasks t JOIN checklists c ON t.checklist_id = c.id "
            "WHERE c.status = 'active' AND c.checklist_type = 'onboarding' AND t.phase IS NOT NULL "
            "GROUP BY t.phase"
        ).fetchall()
        phase_breakdown = {r['phase']: r['cnt'] for r in phase_rows}

        # ── Overdue by category ──
        overdue_cat_rows = db.execute(
            "SELECT category, COUNT(*) as cnt FROM tasks "
            "WHERE status NOT IN ('completed', 'skipped') AND due_date < ? "
            "GROUP BY category ORDER BY cnt DESC",
            (today,)
        ).fetchall()
        overdue_by_category = {r['category']: r['cnt'] for r in overdue_cat_rows}

        # ── Active cohorts ──
        cohort_rows = db.execute(
            "SELECT oc.id, oc.name, oc.start_week, oc.slack_channel, "
            "COUNT(e.id) as member_count "
            "FROM onboarding_cohorts oc "
            "LEFT JOIN employees e ON e.cohort_id = oc.id AND e.status IN ('pending', 'active') "
            "GROUP BY oc.id ORDER BY oc.start_week DESC LIMIT 10"
        ).fetchall()
        active_cohorts = [dict(r) for r in cohort_rows]

    finally:
        db.close()

    return jsonify({
        'total_employees': total_employees,
        'active_onboardings': active_onboardings,
        'active_offboardings': active_offboardings,
        'overdue_tasks': overdue_tasks,
        'monthly_hires': monthly_hires,
        'monthly_departures': monthly_departures,
        'pre_boarding_readiness': pre_boarding_readiness,
        'avg_time_to_ramp': avg_time_to_ramp,
        'retention_90_day': retention_90_day,
        'new_hire_nps': new_hire_nps,
        'tasks_on_time_pct': tasks_on_time_pct,
        'phase_breakdown': phase_breakdown,
        'overdue_by_category': overdue_by_category,
        'active_cohorts': active_cohorts
    })


# ── Checklist Templates ──────────────────────────────────────────────────────

@admin_bp.route('/templates', methods=['GET'])
@login_required
@role_required(['admin'])
def list_templates():
    """List all checklist templates."""
    db = get_db()
    try:
        rows = db.execute(
            "SELECT * FROM checklist_templates WHERE is_active = 1 ORDER BY department, checklist_type, phase"
        ).fetchall()
        templates = []
        for r in rows:
            t = dict(r)
            try:
                tasks = json.loads(t['tasks_json']) if t['tasks_json'] else []
                t['task_count'] = len(tasks)
            except (json.JSONDecodeError, TypeError):
                t['task_count'] = 0
            templates.append(t)
        return jsonify(templates)
    finally:
        db.close()


@admin_bp.route('/templates/defaults', methods=['GET'])
@login_required
@role_required(['admin', 'hr'])
def get_template_defaults():
    """Return the built-in default onboarding tasks as a flat JSON list.

    Accepts optional query params to tailor the task list:
      ?hire_type=full-time|part-time|contractor|intern
      ?location_mode=in-office|remote|hybrid
      ?department=Sales|IT|...
    Defaults to full-time / in-office / General if not specified.
    """
    from .routes_mgr import _get_onboarding_tasks

    hire_type = request.args.get('hire_type', 'full-time')
    location_mode = request.args.get('location_mode', 'in-office')
    department = request.args.get('department', 'General')

    dummy_employee = {
        'email': 'template@celito.net',
        'manager_email': '',
        'department': department,
        'location_mode': location_mode,
        'hire_type': hire_type,
    }

    raw_tasks = _get_onboarding_tasks(dummy_employee)

    # Flatten to the format the template system expects
    template_tasks = []
    for t in raw_tasks:
        task_entry = {
            'title': t['title'],
            'description': t.get('description', ''),
            'category': t.get('category', 'hr'),
            'assigned_to': t.get('assigned_to', ''),
            'due_offset_days': t.get('due_offset_days', 0),
            'phase': t.get('phase', 'company_onboarding'),
        }
        if t.get('is_security_critical'):
            task_entry['is_security_critical'] = True
        if t.get('is_acknowledgment'):
            task_entry['is_acknowledgment'] = True
        if t.get('compliance_required'):
            task_entry['compliance_required'] = True
        if t.get('resource_url'):
            task_entry['resource_url'] = t['resource_url']
        if t.get('conditions'):
            task_entry['conditions'] = t['conditions']

        template_tasks.append(task_entry)

        # Also include subtasks as indented entries
        for st in t.get('subtasks', []):
            st_entry = {
                'title': f"  ↳ {st['title']}",
                'description': st.get('description', ''),
                'category': st.get('category', t.get('category', 'it')),
                'assigned_to': st.get('assigned_to', ''),
                'due_offset_days': st.get('due_offset_days', 0),
                'phase': st.get('phase', t.get('phase', '')),
                'parent_title': t['title'],
            }
            if st.get('is_security_critical'):
                st_entry['is_security_critical'] = True
            template_tasks.append(st_entry)

    return jsonify({
        'tasks': template_tasks,
        'hire_type': hire_type,
        'location_mode': location_mode,
        'department': department,
        'total': len(template_tasks),
    })


@admin_bp.route('/templates', methods=['POST'])
@login_required
@role_required(['admin'])
def create_template():
    """Create a new checklist template."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Request body required'}), 400

    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Template name is required'}), 400

    tasks_json = data.get('tasks_json')
    if tasks_json:
        if isinstance(tasks_json, list):
            tasks_json = json.dumps(tasks_json)
        else:
            try:
                json.loads(tasks_json)
            except (json.JSONDecodeError, TypeError):
                return jsonify({'error': 'tasks_json must be a valid JSON array'}), 400
    else:
        tasks_json = '[]'

    now = datetime.utcnow().isoformat()
    db = get_db()
    try:
        db.execute(
            "INSERT INTO checklist_templates (name, department, checklist_type, phase, tasks_json, location_mode, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name, data.get('department'), data.get('checklist_type', 'onboarding'),
             data.get('phase'), tasks_json, data.get('location_mode', 'all'), now, now)
        )
        db.commit()
        tid = db.execute("SELECT last_insert_rowid() as id").fetchone()['id']
        row = db.execute("SELECT * FROM checklist_templates WHERE id = ?", (tid,)).fetchone()
        result = dict(row)
    finally:
        db.close()

    _audit('template_created', 'checklist_template', tid, {'name': name})
    return jsonify(result), 201


@admin_bp.route('/templates/<int:template_id>', methods=['PUT'])
@login_required
@role_required(['admin'])
def update_template(template_id):
    """Update a checklist template."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Request body required'}), 400

    db = get_db()
    try:
        existing = db.execute("SELECT * FROM checklist_templates WHERE id = ? AND is_active = 1", (template_id,)).fetchone()
        if not existing:
            return jsonify({'error': 'Template not found'}), 404

        updates = []
        params = []
        for field in ('name', 'department', 'checklist_type', 'phase', 'location_mode'):
            if field in data:
                updates.append(f'{field} = ?')
                params.append(data[field])
        if 'tasks_json' in data:
            tj = data['tasks_json']
            if isinstance(tj, list):
                tj = json.dumps(tj)
            else:
                try:
                    json.loads(tj)
                except (json.JSONDecodeError, TypeError):
                    return jsonify({'error': 'tasks_json must be a valid JSON array'}), 400
            updates.append('tasks_json = ?')
            params.append(tj)

        if not updates:
            return jsonify({'error': 'No fields to update'}), 400

        updates.append('updated_at = ?')
        params.append(datetime.utcnow().isoformat())
        params.append(template_id)

        db.execute(f"UPDATE checklist_templates SET {', '.join(updates)} WHERE id = ?", params)
        db.commit()
        row = db.execute("SELECT * FROM checklist_templates WHERE id = ?", (template_id,)).fetchone()
        result = dict(row)
    finally:
        db.close()

    _audit('template_updated', 'checklist_template', template_id, {'fields': list(data.keys())})
    return jsonify(result)


@admin_bp.route('/templates/<int:template_id>', methods=['DELETE'])
@login_required
@role_required(['admin'])
def delete_template(template_id):
    """Soft-delete a checklist template."""
    db = get_db()
    try:
        existing = db.execute("SELECT * FROM checklist_templates WHERE id = ? AND is_active = 1", (template_id,)).fetchone()
        if not existing:
            return jsonify({'error': 'Template not found'}), 404
        db.execute("UPDATE checklist_templates SET is_active = 0, updated_at = ? WHERE id = ?",
                   (datetime.utcnow().isoformat(), template_id))
        db.commit()
    finally:
        db.close()

    _audit('template_deleted', 'checklist_template', template_id, {'name': existing['name']})
    return jsonify({'message': f"Template '{existing['name']}' deactivated"})


@admin_bp.route('/templates/<int:template_id>/duplicate', methods=['POST'])
@login_required
@role_required(['admin'])
def duplicate_template(template_id):
    """Duplicate a checklist template with a new name."""
    db = get_db()
    try:
        existing = db.execute("SELECT * FROM checklist_templates WHERE id = ? AND is_active = 1", (template_id,)).fetchone()
        if not existing:
            return jsonify({'error': 'Template not found'}), 404

        data = request.get_json(silent=True) or {}
        new_name = (data.get('name') or f"{existing['name']} (Copy)").strip()
        now = datetime.utcnow().isoformat()

        db.execute(
            "INSERT INTO checklist_templates (name, department, checklist_type, phase, tasks_json, location_mode, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (new_name, existing['department'], existing['checklist_type'],
             existing['phase'], existing['tasks_json'], existing['location_mode'], now, now)
        )
        db.commit()
        new_id = db.execute("SELECT last_insert_rowid() as id").fetchone()['id']
        row = db.execute("SELECT * FROM checklist_templates WHERE id = ?", (new_id,)).fetchone()
        result = dict(row)
    finally:
        db.close()

    _audit('template_duplicated', 'checklist_template', new_id,
           {'source_id': template_id, 'new_name': new_name})
    return jsonify(result), 201


# ── Cohort Management ────────────────────────────────────────────────────────

@admin_bp.route('/cohorts', methods=['GET'])
@login_required
@role_required(['admin'])
def list_cohorts():
    """List all cohorts with member count and avg progress."""
    db = get_db()
    try:
        rows = db.execute(
            "SELECT oc.*, COUNT(e.id) as member_count "
            "FROM onboarding_cohorts oc "
            "LEFT JOIN employees e ON e.cohort_id = oc.id "
            "GROUP BY oc.id ORDER BY oc.start_week DESC"
        ).fetchall()
        cohorts = []
        for r in rows:
            c = dict(r)
            # Compute avg progress for active members
            members = db.execute(
                "SELECT e.id FROM employees e WHERE e.cohort_id = ? AND e.status IN ('pending', 'active')",
                (c['id'],)
            ).fetchall()
            if members:
                total_tasks = 0
                done_tasks = 0
                for m in members:
                    tt = db.execute(
                        "SELECT COUNT(*) as cnt FROM tasks t JOIN checklists cl ON t.checklist_id = cl.id "
                        "WHERE cl.employee_id = ?", (m['id'],)
                    ).fetchone()['cnt']
                    dt = db.execute(
                        "SELECT COUNT(*) as cnt FROM tasks t JOIN checklists cl ON t.checklist_id = cl.id "
                        "WHERE cl.employee_id = ? AND t.status = 'completed'", (m['id'],)
                    ).fetchone()['cnt']
                    total_tasks += tt
                    done_tasks += dt
                c['avg_progress'] = round(done_tasks / total_tasks * 100, 1) if total_tasks else 0
            else:
                c['avg_progress'] = 0
            cohorts.append(c)
        return jsonify(cohorts)
    finally:
        db.close()


@admin_bp.route('/cohorts', methods=['POST'])
@login_required
@role_required(['admin'])
def create_cohort():
    """Create a new onboarding cohort."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Request body required'}), 400

    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Cohort name is required'}), 400

    db = get_db()
    try:
        db.execute(
            "INSERT INTO onboarding_cohorts (name, start_week, created_at) VALUES (?, ?, ?)",
            (name, data.get('start_week'), datetime.utcnow().isoformat())
        )
        db.commit()
        cid = db.execute("SELECT last_insert_rowid() as id").fetchone()['id']
        row = db.execute("SELECT * FROM onboarding_cohorts WHERE id = ?", (cid,)).fetchone()
        result = dict(row)
    finally:
        db.close()

    _audit('cohort_created', 'cohort', cid, {'name': name})
    return jsonify(result), 201


@admin_bp.route('/cohorts/<int:cohort_id>', methods=['PUT'])
@login_required
@role_required(['admin'])
def update_cohort(cohort_id):
    """Update a cohort."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Request body required'}), 400

    db = get_db()
    try:
        existing = db.execute("SELECT * FROM onboarding_cohorts WHERE id = ?", (cohort_id,)).fetchone()
        if not existing:
            return jsonify({'error': 'Cohort not found'}), 404

        updates = []
        params = []
        for field in ('name', 'slack_channel', 'start_week'):
            if field in data:
                updates.append(f'{field} = ?')
                params.append(data[field])
        if not updates:
            return jsonify({'error': 'No fields to update'}), 400

        params.append(cohort_id)
        db.execute(f"UPDATE onboarding_cohorts SET {', '.join(updates)} WHERE id = ?", params)
        db.commit()
        row = db.execute("SELECT * FROM onboarding_cohorts WHERE id = ?", (cohort_id,)).fetchone()
        result = dict(row)
    finally:
        db.close()

    _audit('cohort_updated', 'cohort', cohort_id, {'fields': list(data.keys())})
    return jsonify(result)


@admin_bp.route('/cohorts/<int:cohort_id>', methods=['DELETE'])
@login_required
@role_required(['admin'])
def delete_cohort(cohort_id):
    """Delete a cohort (only if empty)."""
    db = get_db()
    try:
        existing = db.execute("SELECT * FROM onboarding_cohorts WHERE id = ?", (cohort_id,)).fetchone()
        if not existing:
            return jsonify({'error': 'Cohort not found'}), 404

        member_count = db.execute(
            "SELECT COUNT(*) as cnt FROM employees WHERE cohort_id = ?", (cohort_id,)
        ).fetchone()['cnt']
        if member_count > 0:
            return jsonify({'error': f'Cannot delete cohort with {member_count} member(s). Reassign them first.'}), 400

        db.execute("DELETE FROM onboarding_cohorts WHERE id = ?", (cohort_id,))
        db.commit()
    finally:
        db.close()

    _audit('cohort_deleted', 'cohort', cohort_id, {'name': existing['name']})
    return jsonify({'message': f"Cohort '{existing['name']}' deleted"})


@admin_bp.route('/cohorts/<int:cohort_id>/slack', methods=['POST'])
@login_required
@role_required(['admin'])
def create_cohort_slack_channel(cohort_id):
    """Create a Slack channel for the cohort."""
    db = get_db()
    try:
        cohort = db.execute("SELECT * FROM onboarding_cohorts WHERE id = ?", (cohort_id,)).fetchone()
        if not cohort:
            return jsonify({'error': 'Cohort not found'}), 404

        if cohort['slack_channel']:
            return jsonify({'error': f"Slack channel already exists: {cohort['slack_channel']}"}), 409
    finally:
        db.close()

    try:
        from .slack_client import SlackClient
        from .config import config
        slack = SlackClient()

        # Sanitize name for Slack (lowercase, hyphens, no spaces)
        channel_name = 'cohort-' + cohort['name'].lower().replace(' ', '-').replace('_', '-')
        channel_name = ''.join(c for c in channel_name if c.isalnum() or c == '-')[:80]

        result = slack.create_channel(channel_name)
        channel_id = result.get('id', channel_name)

        db2 = get_db()
        try:
            db2.execute("UPDATE onboarding_cohorts SET slack_channel = ? WHERE id = ?",
                        (channel_name, cohort_id))
            db2.commit()
        finally:
            db2.close()

        _audit('cohort_slack_created', 'cohort', cohort_id, {'channel': channel_name})
        return jsonify({'message': f'Slack channel #{channel_name} created', 'channel': channel_name})
    except Exception as e:
        logger.error(f"Failed to create Slack channel for cohort {cohort_id}: {e}")
        return jsonify({'error': f'Failed to create Slack channel: {str(e)}'}), 500


# ── Survey Management ────────────────────────────────────────────────────────

@admin_bp.route('/surveys', methods=['GET'])
@login_required
@role_required(['admin'])
def list_surveys():
    """List all surveys with employee name and status."""
    survey_type = request.args.get('survey_type')
    status = request.args.get('status')

    filters = []
    params = []
    if survey_type:
        filters.append('s.survey_type = ?')
        params.append(survey_type)
    if status:
        filters.append('s.status = ?')
        params.append(status)

    where = ''
    if filters:
        where = 'AND ' + ' AND '.join(filters)

    db = get_db()
    try:
        rows = db.execute(
            f"SELECT s.*, e.first_name, e.last_name, e.email AS employee_email, e.department "
            f"FROM surveys s JOIN employees e ON s.employee_id = e.id "
            f"WHERE 1=1 {where} ORDER BY s.sent_at DESC",
            params
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        db.close()


@admin_bp.route('/surveys/metrics', methods=['GET'])
@login_required
@role_required(['admin', 'hr'])
def survey_metrics():
    """Aggregate NPS scores, response rates, and trends."""
    db = get_db()
    try:
        year_ago = (date.today() - timedelta(days=365)).isoformat()

        # Overall averages by survey type
        type_avgs = db.execute(
            "SELECT s.survey_type, AVG(sr.rating) as avg_rating, COUNT(DISTINCT s.id) as count "
            "FROM surveys s JOIN survey_responses sr ON sr.survey_id = s.id "
            "WHERE sr.rating IS NOT NULL AND sr.created_at >= ? "
            "GROUP BY s.survey_type",
            (year_ago,)
        ).fetchall()

        # Response rate
        total_sent = db.execute(
            "SELECT COUNT(*) as cnt FROM surveys WHERE status IN ('sent', 'completed') AND sent_at >= ?",
            (year_ago,)
        ).fetchone()['cnt']
        total_completed = db.execute(
            "SELECT COUNT(*) as cnt FROM surveys WHERE status = 'completed' AND completed_at >= ?",
            (year_ago,)
        ).fetchone()['cnt']
        response_rate = round(total_completed / total_sent * 100, 1) if total_sent else None

        # Monthly NPS trend
        monthly_nps = db.execute(
            "SELECT strftime('%Y-%m', sr.created_at) as month, AVG(sr.rating) as avg_rating, "
            "COUNT(*) as responses "
            "FROM survey_responses sr JOIN surveys s ON sr.survey_id = s.id "
            "WHERE sr.rating IS NOT NULL AND sr.created_at >= ? "
            "GROUP BY month ORDER BY month",
            (year_ago,)
        ).fetchall()

        return jsonify({
            'by_type': [dict(r) for r in type_avgs],
            'response_rate': response_rate,
            'total_sent': total_sent,
            'total_completed': total_completed,
            'monthly_trend': [dict(r) for r in monthly_nps]
        })
    finally:
        db.close()


@admin_bp.route('/surveys/bulk-send', methods=['POST'])
@login_required
@role_required(['admin'])
def bulk_send_surveys():
    """Send surveys to all eligible employees based on days since start."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Request body required'}), 400

    survey_type = data.get('survey_type')
    if survey_type not in ('day_7', 'day_30', 'day_90', 'exit_program'):
        return jsonify({'error': 'Invalid survey_type. Must be day_7, day_30, day_90, or exit_program'}), 400

    day_map = {'day_7': 7, 'day_30': 30, 'day_90': 90}
    target_days = day_map.get(survey_type)

    db = get_db()
    try:
        now = datetime.utcnow().isoformat()

        if target_days:
            # Find employees at roughly the right point ± 3 days
            target_date = (date.today() - timedelta(days=target_days)).isoformat()
            window_start = (date.today() - timedelta(days=target_days + 3)).isoformat()
            window_end = (date.today() - timedelta(days=target_days - 3)).isoformat()
            eligible = db.execute(
                "SELECT id, email, first_name, last_name FROM employees "
                "WHERE status = 'active' AND start_date >= ? AND start_date <= ? "
                "AND id NOT IN (SELECT employee_id FROM surveys WHERE survey_type = ?)",
                (window_start, window_end, survey_type)
            ).fetchall()
        else:
            # exit_program — employees finishing offboarding
            eligible = db.execute(
                "SELECT e.id, e.email, e.first_name, e.last_name FROM employees e "
                "JOIN checklists c ON c.employee_id = e.id "
                "WHERE c.checklist_type = 'offboarding' AND c.status = 'completed' "
                "AND e.id NOT IN (SELECT employee_id FROM surveys WHERE survey_type = 'exit_program')"
            ).fetchall()

        sent_count = 0
        for emp in eligible:
            db.execute(
                "INSERT INTO surveys (employee_id, survey_type, sent_at, status) VALUES (?, ?, ?, 'sent')",
                (emp['id'], survey_type, now)
            )
            sent_count += 1

        db.commit()
    finally:
        db.close()

    # Attempt Slack notification for each
    if sent_count > 0:
        try:
            from .slack_client import SlackClient
            from .config import config
            slack = SlackClient()
            for emp in eligible:
                try:
                    slack.send_dm(
                        emp['email'],
                        f"Hi {emp['first_name']}! We'd love your feedback on your onboarding experience. "
                        f"Please take a moment to complete your {survey_type.replace('_', ' ')} survey "
                        f"in the Onboarding Portal."
                    )
                except Exception:
                    pass
        except Exception:
            pass

    _audit('surveys_bulk_sent', 'survey', None,
           {'survey_type': survey_type, 'count': sent_count})
    return jsonify({'message': f'Sent {sent_count} {survey_type} survey(s)', 'count': sent_count})


# ── Equipment Tracking ───────────────────────────────────────────────────────

@admin_bp.route('/equipment', methods=['GET'])
@login_required
@role_required(['admin'])
def list_equipment():
    """List all equipment across employees."""
    status_filter = request.args.get('status')
    type_filter = request.args.get('item_type')

    filters = []
    params = []
    if status_filter:
        filters.append('eq.status = ?')
        params.append(status_filter)
    if type_filter:
        filters.append('eq.item_type = ?')
        params.append(type_filter)

    where = ''
    if filters:
        where = 'AND ' + ' AND '.join(filters)

    db = get_db()
    try:
        rows = db.execute(
            f"SELECT eq.*, e.first_name, e.last_name, e.email AS employee_email, e.department "
            f"FROM equipment eq JOIN employees e ON eq.employee_id = e.id "
            f"WHERE 1=1 {where} ORDER BY eq.created_at DESC",
            params
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        db.close()


@admin_bp.route('/equipment/summary', methods=['GET'])
@login_required
@role_required(['admin', 'hr'])
def equipment_summary():
    """Equipment counts by status and by item type."""
    db = get_db()
    try:
        by_status = db.execute(
            "SELECT status, COUNT(*) as cnt FROM equipment GROUP BY status ORDER BY cnt DESC"
        ).fetchall()
        by_type = db.execute(
            "SELECT item_type, COUNT(*) as cnt FROM equipment GROUP BY item_type ORDER BY cnt DESC"
        ).fetchall()
        return jsonify({
            'by_status': {r['status']: r['cnt'] for r in by_status},
            'by_type': {r['item_type']: r['cnt'] for r in by_type}
        })
    finally:
        db.close()


# ── Comprehensive Metrics ────────────────────────────────────────────────────

@admin_bp.route('/metrics', methods=['GET'])
@login_required
@role_required(['admin', 'hr'])
def get_metrics():
    """Comprehensive metrics endpoint for the admin metrics dashboard."""
    db = get_db()
    try:
        today = date.today().isoformat()
        year_ago = (date.today() - timedelta(days=365)).isoformat()

        # ── Time-to-ramp histogram ──
        ramp_rows = db.execute(
            "SELECT julianday(ramped_at) - julianday(start_date) as days "
            "FROM employees WHERE ramped_at IS NOT NULL AND ramped_at >= ?",
            (year_ago,)
        ).fetchall()
        ramp_buckets = {'under_30': 0, '30_60': 0, '60_90': 0, 'over_90': 0}
        for r in ramp_rows:
            d = r['days']
            if d < 30:
                ramp_buckets['under_30'] += 1
            elif d < 60:
                ramp_buckets['30_60'] += 1
            elif d < 90:
                ramp_buckets['60_90'] += 1
            else:
                ramp_buckets['over_90'] += 1

        # ── Monthly retention (last 12 months) ──
        retention = []
        for i in range(12):
            m_start = (date.today().replace(day=1) - timedelta(days=30 * i)).replace(day=1)
            m_end = (m_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
            ms, me = m_start.isoformat(), m_end.isoformat()
            # Employees who started 90+ days before this month
            cutoff = (m_start - timedelta(days=90)).isoformat()
            cutoff_end = (m_end - timedelta(days=90)).isoformat()
            elig = db.execute(
                "SELECT COUNT(*) as cnt FROM employees WHERE start_date <= ? AND start_date >= ?",
                (cutoff_end, (date(m_start.year - 1, m_start.month, 1)).isoformat() if i == 11 else cutoff)
            ).fetchone()['cnt']
            still_active = db.execute(
                "SELECT COUNT(*) as cnt FROM employees "
                "WHERE start_date <= ? AND start_date >= ? "
                "AND (status = 'active' OR (end_date IS NOT NULL AND end_date > ?))",
                (cutoff_end, cutoff, ms)
            ).fetchone()['cnt']
            retention.append({
                'month': m_start.strftime('%Y-%m'),
                'eligible': elig,
                'retained': still_active,
                'rate': round(still_active / elig * 100, 1) if elig else None
            })
        retention.reverse()

        # ── Monthly NPS trend ──
        nps_trend = db.execute(
            "SELECT strftime('%Y-%m', sr.created_at) as month, "
            "AVG(sr.rating) as avg_rating, COUNT(*) as responses "
            "FROM survey_responses sr JOIN surveys s ON sr.survey_id = s.id "
            "WHERE sr.rating IS NOT NULL AND sr.created_at >= ? "
            "GROUP BY month ORDER BY month",
            (year_ago,)
        ).fetchall()

        # ── Task completion by category ──
        task_completion = db.execute(
            "SELECT category, "
            "SUM(CASE WHEN status = 'completed' AND (due_date IS NULL OR completed_at <= due_date) THEN 1 ELSE 0 END) as on_time, "
            "SUM(CASE WHEN status = 'completed' AND due_date IS NOT NULL AND completed_at > due_date THEN 1 ELSE 0 END) as late, "
            "COUNT(*) as total "
            "FROM tasks WHERE status = 'completed' AND completed_at >= ? "
            "GROUP BY category",
            (year_ago,)
        ).fetchall()

        # ── Department comparison ──
        dept_comparison = db.execute(
            "SELECT e.department, "
            "AVG(julianday(COALESCE(e.ramped_at, c.completed_at)) - julianday(e.start_date)) as avg_days, "
            "COUNT(*) as employee_count "
            "FROM employees e LEFT JOIN checklists c ON c.employee_id = e.id AND c.checklist_type = 'onboarding' "
            "WHERE (e.ramped_at IS NOT NULL OR c.status = 'completed') AND e.start_date >= ? "
            "GROUP BY e.department",
            (year_ago,)
        ).fetchall()

        # ── Phase bottlenecks ──
        phase_bottlenecks = db.execute(
            "SELECT phase, COUNT(*) as overdue_count "
            "FROM tasks WHERE status NOT IN ('completed', 'skipped') AND due_date < ? AND phase IS NOT NULL "
            "GROUP BY phase ORDER BY overdue_count DESC",
            (today,)
        ).fetchall()

        # ── Buddy effectiveness ──
        with_buddy = db.execute(
            "SELECT AVG(sr.rating) as avg_rating, COUNT(DISTINCT s.employee_id) as emp_count "
            "FROM survey_responses sr JOIN surveys s ON sr.survey_id = s.id "
            "JOIN employees e ON s.employee_id = e.id "
            "WHERE sr.rating IS NOT NULL AND e.buddy_email IS NOT NULL AND e.buddy_email != '' "
            "AND sr.created_at >= ?",
            (year_ago,)
        ).fetchone()
        without_buddy = db.execute(
            "SELECT AVG(sr.rating) as avg_rating, COUNT(DISTINCT s.employee_id) as emp_count "
            "FROM survey_responses sr JOIN surveys s ON sr.survey_id = s.id "
            "JOIN employees e ON s.employee_id = e.id "
            "WHERE sr.rating IS NOT NULL AND (e.buddy_email IS NULL OR e.buddy_email = '') "
            "AND sr.created_at >= ?",
            (year_ago,)
        ).fetchone()

        # ── Top overdue assignees ──
        top_overdue = db.execute(
            "SELECT assigned_email, assigned_to, COUNT(*) as overdue_count "
            "FROM tasks WHERE status NOT IN ('completed', 'skipped') AND due_date < ? "
            "AND assigned_email IS NOT NULL "
            "GROUP BY assigned_email ORDER BY overdue_count DESC LIMIT 10",
            (today,)
        ).fetchall()

    finally:
        db.close()

    return jsonify({
        'time_to_ramp': ramp_buckets,
        'retention': retention,
        'nps_trend': [dict(r) for r in nps_trend],
        'task_completion': [dict(r) for r in task_completion],
        'department_comparison': [dict(r) for r in dept_comparison],
        'phase_bottlenecks': [dict(r) for r in phase_bottlenecks],
        'buddy_effectiveness': {
            'with_buddy': {
                'avg_rating': round(with_buddy['avg_rating'], 2) if with_buddy['avg_rating'] else None,
                'employee_count': with_buddy['emp_count']
            },
            'without_buddy': {
                'avg_rating': round(without_buddy['avg_rating'], 2) if without_buddy['avg_rating'] else None,
                'employee_count': without_buddy['emp_count']
            }
        },
        'top_overdue_assignees': [dict(r) for r in top_overdue]
    })


# ── Bulk Operations ──────────────────────────────────────────────────────────

@admin_bp.route('/tasks/bulk-remind', methods=['POST'])
@login_required
@role_required(['admin'])
def bulk_remind():
    """Send Slack reminders for all overdue tasks, grouped by assignee."""
    db = get_db()
    try:
        today = date.today().isoformat()
        overdue = db.execute(
            "SELECT t.*, e.first_name, e.last_name "
            "FROM tasks t JOIN checklists c ON t.checklist_id = c.id "
            "JOIN employees e ON c.employee_id = e.id "
            "WHERE t.status NOT IN ('completed', 'skipped') AND t.due_date < ? "
            "AND t.assigned_email IS NOT NULL "
            "ORDER BY t.assigned_email, t.due_date",
            (today,)
        ).fetchall()

        # Group by assignee
        by_assignee = {}
        for task in overdue:
            email = task['assigned_email']
            if email not in by_assignee:
                by_assignee[email] = []
            by_assignee[email].append(task)
    finally:
        db.close()

    sent_count = 0
    failed = []
    try:
        from .slack_client import SlackClient
        from .config import config
        slack = SlackClient()

        for email, tasks in by_assignee.items():
            task_lines = []
            for t in tasks:
                days_overdue = (date.today() - date.fromisoformat(t['due_date'])).days
                task_lines.append(
                    f"• *{t['title']}* (for {t['first_name']} {t['last_name']}) — "
                    f"{days_overdue} day(s) overdue"
                )
            message = (
                f"⚠️ *Overdue Task Reminder*\n\n"
                f"You have {len(tasks)} overdue onboarding/offboarding task(s):\n\n"
                + "\n".join(task_lines) +
                "\n\nPlease complete these as soon as possible in the Onboarding Portal."
            )
            try:
                slack.send_dm(email, message)
                sent_count += 1
            except Exception as e:
                failed.append({'email': email, 'error': str(e)})
    except Exception as e:
        return jsonify({'error': f'Slack not configured: {str(e)}'}), 500

    _audit('bulk_remind', 'tasks', None, {
        'sent': sent_count, 'failed': len(failed),
        'total_overdue_tasks': len(overdue)
    })
    return jsonify({
        'message': f'Sent reminders to {sent_count} assignee(s) for {len(overdue)} overdue task(s)',
        'sent': sent_count,
        'failed': failed
    })


@admin_bp.route('/tasks/daily-digest', methods=['POST'])
@login_required
@role_required(['admin'])
def send_daily_digest():
    """
    Send a Slack DM digest to every user who has overdue or due-today tasks.

    Intended to be triggered by a daily cron job or manually by an admin
    from the Admin panel.  Groups by assignee so each person gets one DM.
    """
    try:
        from .slack_client import SlackClient
        slack = SlackClient()
    except Exception as e:
        return jsonify({'error': f'Slack not configured: {str(e)}'}), 503

    db = get_db()
    try:
        today = date.today().isoformat()
        rows = db.execute(
            "SELECT assigned_email, "
            "SUM(CASE WHEN due_date < ? THEN 1 ELSE 0 END) as overdue, "
            "SUM(CASE WHEN due_date = ? THEN 1 ELSE 0 END) as due_today "
            "FROM tasks "
            "WHERE status NOT IN ('completed', 'skipped') "
            "AND due_date <= ? AND assigned_email IS NOT NULL AND assigned_email != '' "
            "GROUP BY assigned_email",
            (today, today, today),
        ).fetchall()
    finally:
        db.close()

    sent = 0
    failed = []
    for row in rows:
        parts = []
        if row['overdue'] and row['overdue'] > 0:
            parts.append(f"🔴 *{row['overdue']} overdue*")
        if row['due_today'] and row['due_today'] > 0:
            parts.append(f"🟡 *{row['due_today']} due today*")
        if not parts:
            continue
        try:
            slack.send_dm(
                row['assigned_email'],
                f"📋 *Daily Task Digest*\n\n"
                f"You have {' and '.join(parts)} in the onboarding portal.\n\n"
                f"👉 https://onboard.celito.net/#tasks",
            )
            sent += 1
        except Exception as e:
            failed.append({'email': row['assigned_email'], 'error': str(e)})

    _audit('daily_digest', 'tasks', None, {'sent': sent, 'failed': len(failed)})
    return jsonify({
        'message': f'Sent daily digest to {sent} user(s)',
        'sent': sent,
        'failed': failed,
    })


@admin_bp.route('/slack/manager-synopsis', methods=['POST'])
@login_required
@role_required(['admin', 'hr'])
def slack_manager_synopsis():
    """Send each manager a Slack DM summarizing their direct reports' onboarding progress.

    Optional JSON body:
        manager_email: send only to this manager (otherwise all managers)
    """
    try:
        from .slack_client import SlackClient
        slack = SlackClient()
    except Exception as e:
        return jsonify({'error': f'Slack not configured: {str(e)}'}), 503

    data = request.get_json(silent=True) or {}
    single_manager = (data.get('manager_email') or '').strip().lower()

    db = get_db()
    try:
        today = date.today().isoformat()

        # Get active onboardings with progress
        query = """
            SELECT e.id, e.first_name, e.last_name, e.email, e.department,
                   e.role_title, e.start_date, e.manager_email, e.hire_type,
                   c.id as checklist_id, c.current_phase,
                   (SELECT COUNT(*) FROM tasks WHERE checklist_id = c.id) as total_tasks,
                   (SELECT COUNT(*) FROM tasks WHERE checklist_id = c.id AND status = 'completed') as done_tasks,
                   (SELECT COUNT(*) FROM tasks WHERE checklist_id = c.id
                    AND status NOT IN ('completed','skipped') AND due_date < ?) as overdue_tasks
            FROM checklists c
            JOIN employees e ON c.employee_id = e.id
            WHERE c.checklist_type = 'onboarding' AND c.status = 'active'
        """
        params = [today]

        if single_manager:
            query += " AND LOWER(e.manager_email) = ?"
            params.append(single_manager)

        query += " ORDER BY e.manager_email, e.start_date"
        rows = db.execute(query, params).fetchall()
    finally:
        db.close()

    if not rows:
        return jsonify({'message': 'No active onboardings found', 'sent': 0})

    # Group by manager
    by_manager = {}
    for r in rows:
        mgr = (r['manager_email'] or '').lower()
        if not mgr:
            continue
        if mgr not in by_manager:
            by_manager[mgr] = []
        by_manager[mgr].append(dict(r))

    PHASE_LABELS = {
        'pre_boarding': 'Pre-Boarding',
        'company_onboarding': 'Week 1',
        'department_onboarding': 'Dept Onboarding',
        'role_training': 'Role Training',
        'completed': 'Complete',
    }

    sent = 0
    failed = []
    for mgr_email, employees in by_manager.items():
        lines = []
        total_overdue = 0
        for emp in employees:
            total = emp['total_tasks'] or 0
            done = emp['done_tasks'] or 0
            overdue = emp['overdue_tasks'] or 0
            total_overdue += overdue
            pct = round((done / total * 100) if total > 0 else 0)
            phase_label = PHASE_LABELS.get(emp['current_phase'] or '', emp['current_phase'] or '—')

            bar_fill = pct // 10
            bar = '▓' * bar_fill + '░' * (10 - bar_fill)

            line = f"*{emp['first_name']} {emp['last_name']}* — {emp['role_title'] or emp['department']}\n"
            line += f"    `{bar}` {pct}%  ·  Phase: {phase_label}  ·  {done}/{total} tasks"
            if overdue > 0:
                line += f"  ·  🔴 {overdue} overdue"
            lines.append(line)

        header = f"📊 *Onboarding Synopsis* — {len(employees)} active hire{'s' if len(employees) != 1 else ''}\n"
        if total_overdue > 0:
            header += f"⚠️ {total_overdue} total overdue task(s) across your team\n"
        header += "\n"

        message = header + "\n\n".join(lines)
        message += f"\n\n👉 <https://dash.celito.net/onboard/#dashboard|Open Onboarding Portal>"

        try:
            slack.send_dm(mgr_email, message)
            sent += 1
        except Exception as e:
            failed.append({'email': mgr_email, 'error': str(e)})

    _audit('slack_manager_synopsis', 'slack', None, {
        'sent': sent, 'failed': len(failed),
        'employees': len(rows), 'managers': len(by_manager),
    })

    return jsonify({
        'message': f'Sent synopsis to {sent} manager(s) covering {len(rows)} employee(s)',
        'sent': sent,
        'managers': list(by_manager.keys()),
        'failed': failed,
    })


@admin_bp.route('/slack/employee-updates', methods=['POST'])
@login_required
@role_required(['admin', 'hr'])
def slack_employee_updates():
    """Send each onboarding employee a Slack DM with their personal progress.

    Optional JSON body:
        employee_id: send only to this employee (otherwise all active)
    """
    try:
        from .slack_client import SlackClient
        slack = SlackClient()
    except Exception as e:
        return jsonify({'error': f'Slack not configured: {str(e)}'}), 503

    data = request.get_json(silent=True) or {}
    single_emp_id = data.get('employee_id')

    db = get_db()
    try:
        today = date.today().isoformat()

        query = """
            SELECT e.id, e.first_name, e.last_name, e.email, e.department,
                   e.start_date, e.manager_email,
                   c.id as checklist_id, c.current_phase
            FROM checklists c
            JOIN employees e ON c.employee_id = e.id
            WHERE c.checklist_type = 'onboarding' AND c.status = 'active'
        """
        params = []
        if single_emp_id:
            query += " AND e.id = ?"
            params.append(single_emp_id)

        employees = db.execute(query, params).fetchall()

        messages = []
        for emp in employees:
            if not emp['email']:
                continue

            # Get task breakdown
            tasks = db.execute("""
                SELECT t.title, t.status, t.due_date, t.phase,
                       t.category, t.assigned_to
                FROM tasks t
                WHERE t.checklist_id = ?
                ORDER BY t.due_date, t.sort_order
            """, (emp['checklist_id'],)).fetchall()

            total = len(tasks)
            completed = sum(1 for t in tasks if t['status'] == 'completed')
            overdue = [t for t in tasks if t['status'] not in ('completed', 'skipped')
                       and t['due_date'] and t['due_date'] < today]
            upcoming = [t for t in tasks if t['status'] not in ('completed', 'skipped')
                        and t['due_date'] and t['due_date'] >= today][:5]

            pct = round((completed / total * 100) if total > 0 else 0)
            bar_fill = pct // 10
            bar = '▓' * bar_fill + '░' * (10 - bar_fill)

            PHASE_LABELS = {
                'pre_boarding': 'Pre-Boarding',
                'company_onboarding': 'Week 1',
                'department_onboarding': 'Dept Onboarding',
                'role_training': 'Role Training',
            }
            phase_label = PHASE_LABELS.get(emp['current_phase'] or '', emp['current_phase'] or '—')

            # Build message
            msg = f"👋 Hi {emp['first_name']}! Here's your onboarding progress:\n\n"
            msg += f"`{bar}` *{pct}% complete* ({completed}/{total} tasks)\n"
            msg += f"📍 Current phase: *{phase_label}*\n"

            if overdue:
                msg += f"\n🔴 *{len(overdue)} overdue task(s):*\n"
                for t in overdue[:5]:
                    days_late = (date.fromisoformat(today) - date.fromisoformat(t['due_date'])).days
                    msg += f"  • {t['title']} — {days_late}d overdue\n"
                if len(overdue) > 5:
                    msg += f"  _...and {len(overdue) - 5} more_\n"

            if upcoming:
                msg += f"\n📋 *Coming up next:*\n"
                for t in upcoming:
                    due_str = t['due_date']
                    msg += f"  • {t['title']} — due {due_str}\n"

            if not overdue and not upcoming:
                msg += "\n✅ No pending tasks right now — great job!\n"

            msg += f"\n👉 <https://dash.celito.net/onboard/#tasks|View your tasks>"

            messages.append({'email': emp['email'], 'message': msg, 'emp_id': emp['id']})
    finally:
        db.close()

    sent = 0
    failed = []
    for m in messages:
        try:
            slack.send_dm(m['email'], m['message'])
            sent += 1
        except Exception as e:
            failed.append({'email': m['email'], 'error': str(e)})

    _audit('slack_employee_updates', 'slack', None, {
        'sent': sent, 'failed': len(failed),
        'employees': len(messages),
    })

    return jsonify({
        'message': f'Sent progress updates to {sent} employee(s)',
        'sent': sent,
        'failed': failed,
    })


@admin_bp.route('/tasks/bulk-reassign', methods=['POST'])
@login_required
@role_required(['admin'])
def bulk_reassign():
    """Reassign all tasks from one person to another."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Request body required'}), 400

    from_email = (data.get('from_email') or '').strip().lower()
    to_email = (data.get('to_email') or '').strip().lower()
    to_name = (data.get('to_name') or '').strip()

    if not from_email or not to_email:
        return jsonify({'error': 'Both from_email and to_email are required'}), 400

    db = get_db()
    try:
        # Only reassign non-completed tasks
        affected = db.execute(
            "SELECT COUNT(*) as cnt FROM tasks "
            "WHERE assigned_email = ? AND status NOT IN ('completed', 'skipped')",
            (from_email,)
        ).fetchone()['cnt']

        if affected == 0:
            return jsonify({'message': f'No pending tasks found for {from_email}', 'count': 0})

        updates = {'assigned_email': to_email}
        if to_name:
            db.execute(
                "UPDATE tasks SET assigned_email = ?, assigned_to = ? "
                "WHERE assigned_email = ? AND status NOT IN ('completed', 'skipped')",
                (to_email, to_name, from_email)
            )
        else:
            db.execute(
                "UPDATE tasks SET assigned_email = ? "
                "WHERE assigned_email = ? AND status NOT IN ('completed', 'skipped')",
                (to_email, from_email)
            )
        db.commit()
    finally:
        db.close()

    _audit('bulk_reassign', 'tasks', None, {
        'from': from_email, 'to': to_email, 'count': affected
    })
    return jsonify({
        'message': f'Reassigned {affected} task(s) from {from_email} to {to_email}',
        'count': affected
    })


# ── Trend Metrics ──────────────────────────────────────────────────────────

@admin_bp.route('/executive-dashboard', methods=['GET'])
@login_required
@role_required(['admin', 'hr'])
def executive_dashboard():
    """Return executive-level KPIs with quarter-over-quarter comparison,
    department benchmarks, and a 6-month hiring trend."""
    db = get_db()
    try:
        today = date.today()
        q_month = ((today.month - 1) // 3) * 3 + 1
        q_start = date(today.year, q_month, 1)
        # Previous quarter start/end
        if q_month == 1:
            pq_start = date(today.year - 1, 10, 1)
        else:
            pq_start = date(today.year, q_month - 3, 1)
        pq_end = q_start - timedelta(days=1)

        # ── KPI 1: Active onboardings ────────────────────────────────
        active = db.execute(
            "SELECT COUNT(*) as cnt FROM checklists "
            "WHERE checklist_type = 'onboarding' AND status = 'active'"
        ).fetchone()['cnt']

        # ── KPI 2: Avg time to ramp (this quarter vs previous) ───────
        ramp_q = db.execute(
            "SELECT AVG(julianday(ramped_at) - julianday(start_date)) as avg_days "
            "FROM employees WHERE ramped_at IS NOT NULL AND ramped_at >= ?",
            (q_start.isoformat(),)
        ).fetchone()
        ramp_current = round(ramp_q['avg_days'], 1) if ramp_q and ramp_q['avg_days'] else None

        ramp_pq = db.execute(
            "SELECT AVG(julianday(ramped_at) - julianday(start_date)) as avg_days "
            "FROM employees WHERE ramped_at IS NOT NULL AND ramped_at >= ? AND ramped_at <= ?",
            (pq_start.isoformat(), pq_end.isoformat())
        ).fetchone()
        ramp_prev = round(ramp_pq['avg_days'], 1) if ramp_pq and ramp_pq['avg_days'] else None

        # ── KPI 3: Onboarding volume this quarter vs previous ────────
        volume_current = db.execute(
            "SELECT COUNT(*) as cnt FROM employees WHERE start_date >= ?",
            (q_start.isoformat(),)
        ).fetchone()['cnt']
        volume_prev = db.execute(
            "SELECT COUNT(*) as cnt FROM employees WHERE start_date >= ? AND start_date <= ?",
            (pq_start.isoformat(), pq_end.isoformat())
        ).fetchone()['cnt']

        # ── KPI 4: Task completion rate (compliance) ─────────────────
        compliance = db.execute(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN t.status IN ('completed','skipped') THEN 1 ELSE 0 END) as done "
            "FROM tasks t JOIN checklists c ON t.checklist_id = c.id "
            "JOIN employees e ON c.employee_id = e.id "
            "WHERE e.ramped_at IS NOT NULL AND e.ramped_at >= ?",
            (q_start.isoformat(),)
        ).fetchone()
        compliance_rate = round(compliance['done'] / compliance['total'] * 100) if compliance['total'] else None

        # ── KPI 5: Satisfaction (from survey_responses) ──────────────
        sat = db.execute(
            "SELECT AVG(sr.rating) as avg_rating FROM survey_responses sr "
            "JOIN surveys s ON sr.survey_id = s.id "
            "WHERE sr.rating IS NOT NULL AND sr.created_at >= ?",
            (q_start.isoformat(),)
        ).fetchone()
        satisfaction = round(sat['avg_rating'], 1) if sat and sat['avg_rating'] else None

        sat_prev = db.execute(
            "SELECT AVG(sr.rating) as avg_rating FROM survey_responses sr "
            "JOIN surveys s ON sr.survey_id = s.id "
            "WHERE sr.rating IS NOT NULL AND sr.created_at >= ? AND sr.created_at <= ?",
            (pq_start.isoformat(), pq_end.isoformat())
        ).fetchone()
        satisfaction_prev = round(sat_prev['avg_rating'], 1) if sat_prev and sat_prev['avg_rating'] else None

        # ── KPI 6: 90-day retention ──────────────────────────────────
        cutoff = (today - timedelta(days=90)).isoformat()
        eligible = db.execute(
            "SELECT COUNT(*) as cnt FROM employees WHERE start_date <= ?",
            (cutoff,)
        ).fetchone()['cnt']
        retained = db.execute(
            "SELECT COUNT(*) as cnt FROM employees "
            "WHERE start_date <= ? AND status != 'departed'",
            (cutoff,)
        ).fetchone()['cnt']
        retention = round(retained / eligible * 100, 1) if eligible else None

        # ── Department breakdown ─────────────────────────────────────
        dept_stats = db.execute(
            "SELECT e.department, COUNT(*) as total, "
            "AVG(CASE WHEN e.ramped_at IS NOT NULL "
            "    THEN julianday(e.ramped_at) - julianday(e.start_date) END) as avg_ramp "
            "FROM employees e WHERE e.start_date >= ? "
            "GROUP BY e.department ORDER BY total DESC",
            (q_start.isoformat(),)
        ).fetchall()

        # ── Monthly trend (last 6 months) ────────────────────────────
        monthly = []
        for i in range(5, -1, -1):
            m_start = today.replace(day=1)
            for _ in range(i):
                m_start = (m_start - timedelta(days=1)).replace(day=1)
            m_end = (m_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
            m_count = db.execute(
                "SELECT COUNT(*) as cnt FROM employees WHERE start_date >= ? AND start_date <= ?",
                (m_start.isoformat(), m_end.isoformat())
            ).fetchone()['cnt']
            monthly.append({'month': m_start.strftime('%b %Y'), 'hires': m_count})

        # ── Tasks on-time % ──────────────────────────────────────────
        completed_total = db.execute(
            "SELECT COUNT(*) as cnt FROM tasks WHERE status = 'completed' AND completed_at >= ?",
            (q_start.isoformat(),)
        ).fetchone()['cnt']
        completed_on_time = db.execute(
            "SELECT COUNT(*) as cnt FROM tasks "
            "WHERE status = 'completed' AND completed_at >= ? "
            "AND (due_date IS NULL OR completed_at <= due_date)",
            (q_start.isoformat(),)
        ).fetchone()['cnt']
        on_time_pct = round(completed_on_time / completed_total * 100) if completed_total else None

        return jsonify({
            'kpis': {
                'active_onboardings': active,
                'avg_ramp_days': ramp_current,
                'avg_ramp_days_prev': ramp_prev,
                'volume_current': volume_current,
                'volume_prev': volume_prev,
                'compliance_rate': compliance_rate,
                'satisfaction': satisfaction,
                'satisfaction_prev': satisfaction_prev,
                'retention_90day': retention,
                'on_time_pct': on_time_pct,
            },
            'departments': [dict(d) for d in dept_stats],
            'monthly_trend': monthly,
            'quarter': f"Q{(q_month - 1) // 3 + 1} {q_start.year}",
            'prev_quarter': f"Q{(pq_start.month - 1) // 3 + 1} {pq_start.year}",
        })
    finally:
        db.close()


@admin_bp.route('/metrics/trends', methods=['GET'])
@login_required
@role_required(['admin', 'hr'])
def get_metric_trends():
    """Return metric snapshots for the last 6 months (or all available)."""
    db = get_db()
    try:
        # Auto-take snapshot for current month if none exists
        current_month = date.today().strftime('%Y-%m')
        existing = db.execute(
            "SELECT COUNT(*) AS cnt FROM metric_snapshots WHERE snapshot_date = ?",
            (current_month,)
        ).fetchone()['cnt']
        if existing == 0:
            try:
                take_monthly_snapshot(db)
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "Auto-snapshot failed: %s", exc)

        rows = db.execute(
            "SELECT snapshot_date, metric_name, metric_value "
            "FROM metric_snapshots "
            "ORDER BY snapshot_date DESC "
            "LIMIT 60"  # up to 10 metrics × 6 months
        ).fetchall()
    finally:
        db.close()

    # Pivot rows into {months: [...], metrics: {name: [values]}}
    month_set = sorted({r['snapshot_date'] for r in rows})
    # Keep at most 6 most recent months
    months = month_set[-6:]
    metric_names = sorted({r['metric_name'] for r in rows})

    # Build lookup: (month, metric) -> value
    lookup = {}
    for r in rows:
        lookup[(r['snapshot_date'], r['metric_name'])] = r['metric_value']

    metrics = {}
    for name in metric_names:
        metrics[name] = [lookup.get((m, name), 0) for m in months]

    return jsonify({'months': months, 'metrics': metrics})


# ── API Key Management ──────────────────────────────────────────────────────

@admin_bp.route('/api-keys', methods=['GET'])
@login_required
@role_required(['admin'])
def list_api_keys():
    """List all API keys (active and revoked)."""
    conn = get_db()
    try:
        keys = conn.execute(
            "SELECT id, name, key_value, created_by, created_at, is_active, last_used "
            "FROM api_keys ORDER BY created_at DESC"
        ).fetchall()
        return jsonify({"keys": [dict(k) for k in keys]})
    finally:
        conn.close()


@admin_bp.route('/api-keys', methods=['POST'])
@login_required
@role_required(['admin'])
def create_api_key():
    """Generate a new API key."""
    import secrets as _secrets
    user = get_current_user()
    body = request.get_json(silent=True) or {}
    name = body.get("name", "Unnamed Key").strip()
    if not name:
        return jsonify({"error": "Key name is required"}), 400

    key_value = f"celito_{_secrets.token_hex(24)}"

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO api_keys (key_value, name, created_by) VALUES (?, ?, ?)",
            (key_value, name, user["email"]),
        )
        conn.commit()
    finally:
        conn.close()

    _audit("create_api_key", "api_key", name)
    return jsonify({"ok": True, "key": key_value, "name": name})


@admin_bp.route('/api-keys/<int:key_id>', methods=['DELETE'])
@login_required
@role_required(['admin'])
def revoke_api_key(key_id):
    """Revoke (deactivate) an API key."""
    conn = get_db()
    try:
        conn.execute("UPDATE api_keys SET is_active = 0 WHERE id = ?", (key_id,))
        conn.commit()
    finally:
        conn.close()

    _audit("revoke_api_key", "api_key", str(key_id))
    return jsonify({"ok": True})


@admin_bp.route('/metrics/snapshot', methods=['POST'])
@login_required
@role_required(['admin'])
def trigger_snapshot():
    """Manually trigger a metric snapshot for the current month."""
    db = get_db()
    try:
        result = take_monthly_snapshot(db)
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500
    finally:
        db.close()

    _audit('snapshot_taken', 'metrics', None, result)
    return jsonify({
        'message': 'Snapshot taken successfully',
        'snapshot': result
    })
