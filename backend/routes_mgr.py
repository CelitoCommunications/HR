"""
Manager and HR routes for the Celito Onboarding & Offboarding Platform.
Handles employee management, phased checklists, tasks, equipment, milestones,
cohorts, surveys, meet-and-greets, buddy tasks, templates, documents, and integrations.
"""

import io
import json
import logging
from datetime import datetime, date, timedelta

import requests as http_requests
from flask import Blueprint, request, jsonify, send_file

from .auth import login_required, role_required, get_current_user
from .db import get_db, get_or_assign_cohort
from .config import config

from zoneinfo import ZoneInfo
from datetime import time as dt_time

logger = logging.getLogger(__name__)

mgr_bp = Blueprint('mgr', __name__, url_prefix='/api')


def _get_overlap_window(tz1_name, tz2_name, ref_date):
    """Find overlapping business hours (9 AM–5 PM) between two timezones.

    Returns a (start_hour_utc, end_hour_utc) tuple in the *new-hire's*
    local time, or (10, 14) as a safe default when there's no overlap.
    Also returns a human-readable note about the suggested window.
    """
    tz1 = ZoneInfo(tz1_name or 'America/Los_Angeles')
    tz2 = ZoneInfo(tz2_name or 'America/Los_Angeles')

    start1 = datetime.combine(ref_date, dt_time(9, 0), tzinfo=tz1)
    end1 = datetime.combine(ref_date, dt_time(17, 0), tzinfo=tz1)
    start2 = datetime.combine(ref_date, dt_time(9, 0), tzinfo=tz2)
    end2 = datetime.combine(ref_date, dt_time(17, 0), tzinfo=tz2)

    overlap_start = max(start1, start2)
    overlap_end = min(end1, end2)

    if overlap_start < overlap_end:
        # Express the overlap in tz1's local hours (the new hire)
        local_start = overlap_start.astimezone(tz1)
        local_end = overlap_end.astimezone(tz1)
        return (
            local_start.hour,
            local_end.hour,
            f"{local_start.strftime('%-I %p')}–{local_end.strftime('%-I %p')} your time",
        )
    # No overlap — suggest midday for new hire, note it for them
    return (10, 14, "Limited overlap — 10 AM–2 PM your time (may be early/late for the other person)")


# ── Helpers ─────────────────────────────────────────────────────────────────

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


def _can_access_employee(user, employee):
    """Check if the user can access this employee's records."""
    if user['role'] in ('admin', 'hr'):
        return True
    if user['role'] == 'manager' and employee['manager_email'] == user['email']:
        return True
    if user['email'] == employee['email']:
        return True
    return False


def _offset_date(start_date, offset_days):
    """Calculate a due date from start_date + offset_days."""
    try:
        start = datetime.strptime(start_date, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        start = date.today()
    return (start + timedelta(days=offset_days)).isoformat()


def _check_condition(condition_json, employee):
    """Check whether a task's conditions match the employee. Returns True if task applies."""
    if not condition_json:
        return True
    try:
        conditions = json.loads(condition_json) if isinstance(condition_json, str) else condition_json
    except (json.JSONDecodeError, TypeError):
        return True

    for key, value in conditions.items():
        emp_val = employee.get(key, '') or ''
        if isinstance(value, str) and value.startswith('!'):
            # Negation: condition passes if employee value does NOT match
            if emp_val.lower() == value[1:].lower():
                return False
        else:
            if emp_val.lower() != str(value).lower():
                return False
    return True


def _insert_task(db, checklist_id, employee_id, task_def, start_date, sort_order, parent_task_id=None, depends_on_task_id=None):
    """Insert a task from a task definition dict. Returns the new task id."""
    due = _offset_date(start_date, task_def.get('due_offset_days', 0))
    urgency = task_def.get('urgency', 'normal')
    status = 'pending'
    if depends_on_task_id:
        status = 'blocked'

    db.execute(
        'INSERT INTO tasks (checklist_id, employee_id, title, description, category, '
        'assigned_to, assigned_email, due_date, status, sort_order, parent_task_id, '
        'depends_on_task_id, phase, due_offset_days, conditions, location_mode, '
        'is_acknowledgment, is_security_critical, compliance_required, urgency, resource_url) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (
            checklist_id, employee_id,
            task_def.get('title', ''),
            task_def.get('description', ''),
            task_def.get('category', 'hr'),
            task_def.get('assigned_to', ''),
            task_def.get('assigned_email', ''),
            due, status, sort_order,
            parent_task_id,
            depends_on_task_id,
            task_def.get('phase', 'company_onboarding'),
            task_def.get('due_offset_days', 0),
            json.dumps(task_def.get('conditions')) if task_def.get('conditions') else None,
            task_def.get('location_mode', 'all'),
            1 if task_def.get('is_acknowledgment') else 0,
            1 if task_def.get('is_security_critical') else 0,
            1 if task_def.get('compliance_required') else 0,
            urgency,
            task_def.get('resource_url', ''),
        )
    )
    return db.execute('SELECT last_insert_rowid()').fetchone()[0]


def _generate_buddy_tasks(db, employee_id, buddy_email, start_date):
    """Create buddy checklist tasks."""
    buddy_items = [
        {'title': 'Reach out to new hire before Day 1', 'description': 'Send an informal welcome message — introduce yourself and offer to answer questions.', 'due_offset_days': -3},
        {'title': 'Coffee or lunch in Week 1', 'description': 'Schedule an informal coffee or lunch (in-person or virtual) during the first week.', 'due_offset_days': 3},
        {'title': 'Weekly check-in — Week 2', 'description': 'Quick 15-minute check-in. How is the new hire settling in? Any questions or concerns?', 'due_offset_days': 10},
        {'title': 'Weekly check-in — Week 3', 'description': 'Continue regular check-ins. Help connect the new hire with other people and resources.', 'due_offset_days': 17},
        {'title': 'Weekly check-in — Week 4', 'description': 'Month-end check-in. Is the new hire feeling integrated? Any blockers?', 'due_offset_days': 24},
        {'title': 'Check in at Day 30', 'description': 'One-month milestone check-in. Provide feedback to manager on how onboarding is going from the buddy perspective.', 'due_offset_days': 30},
    ]
    for item in buddy_items:
        due = _offset_date(start_date, item['due_offset_days'])
        db.execute(
            'INSERT INTO buddy_tasks (employee_id, buddy_email, title, description, due_offset_days, status) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (employee_id, buddy_email, item['title'], item['description'], item['due_offset_days'], 'pending')
        )


def _generate_milestones(db, employee_id, role_title, department, hire_type='full-time'):
    """Create milestones tailored to department and hire_type.
    Full-time/part-time: standard 30/60/90. Contractors: deliverable-focused.
    Interns: learning-focused with shorter timeline."""
    dept_lower = (department or '').lower()
    ht = (hire_type or 'full-time').strip().lower()

    # Base milestones vary by hire_type
    if ht == 'contractor':
        milestones = [
            (7, 'First Week Complete',
             'Environment set up, access verified. Reviewed SOW deliverables with manager. '
             'Met key stakeholders and understands project context.'),
            (30, 'Mid-Contract Checkpoint',
             'Making steady progress on SOW deliverables. Communication cadence established. '
             'Any scope or timeline adjustments documented.'),
            (60, 'Contract Wrap-Up',
             'All deliverables complete or on track for completion. Knowledge transfer documented. '
             'Handoff plan in place for ongoing work.'),
        ]
    elif ht == 'intern':
        milestones = [
            (7, 'First Week Complete',
             'Completed orientation and met team. Workstation set up. '
             'Understands internship project goals and has a working Kanban board.'),
            (21, 'Intern Ramp-Up Complete',
             'Comfortable with tools and processes. Made first contribution to the project. '
             'Building relationships with mentor and team.'),
            (45, 'Mid-Internship Review',
             'Significant progress on project deliverables. Demonstrated learning in key skill areas. '
             'Received and incorporated feedback from mentor.'),
            (60, 'Internship Complete',
             'Presented project outcomes to team. Documented work for handoff. '
             'Received final performance feedback and career guidance.'),
        ]
    else:
        # Full-time and part-time
        milestones = [
            (7, 'First Week Complete',
             'Completed orientation and met key team members. Workstation fully set up. '
             'Reviewed team norms and communication channels. Completed all Day 1-5 onboarding tasks.'),
            (30, '30-Day Check-in',
             'Understands team workflows and tools. Completed initial training modules. '
             'Made first contribution to team work. Building relationships with cross-functional partners.'),
            (60, '60-Day Assessment',
             'Working independently on assigned tasks with minimal guidance. '
             'Building cross-functional relationships. Taking ownership of recurring responsibilities.'),
            (90, 'Fully Ramped',
             'Operating at full capacity across core responsibilities. Completed all training and certifications. '
             'Ready for independent work and client-facing interactions. Considered fully onboarded.'),
        ]

    # Department-specific milestones (full-time and part-time only;
    # contractors and interns have project-focused milestones above)
    if ht not in ('contractor', 'intern'):
        if dept_lower in ('sales', 'business development', 'account management'):
            milestones.extend([
                (14, 'First Client Shadowing',
                 'Shadowed at least 2 client calls or demos. Understands Celito pitch and value proposition. '
                 'Can articulate key differentiators and target customer profiles.'),
                (45, 'First Solo Demo',
                 'Delivered first solo product demo or client presentation. Received feedback from manager. '
                 'Demonstrated command of solution portfolio and competitive positioning.'),
                (60, 'Pipeline Building',
                 'Actively prospecting and building personal pipeline. Understands territory and target accounts. '
                 'Using CRM effectively for deal tracking and forecasting.'),
            ])
        elif dept_lower in ('engineering', 'it', 'sysadmin', 'systems', 'technology', 'devops'):
            milestones.extend([
                (14, 'Dev Environment Mastered',
                 'Development environment fully configured and functional. Familiar with code repos, CI/CD pipelines, '
                 'and deployment processes. Completed access requests for all required systems.'),
                (30, 'First PR Merged',
                 'Submitted and merged first pull request or completed first system change. '
                 'Understands code review process and team coding standards.'),
                (60, 'On-Call Ready',
                 'Comfortable with monitoring tools and alert escalation. Can handle Tier 1/2 incidents independently. '
                 'Has completed incident response training and knows escalation paths.'),
            ])
        elif dept_lower in ('customer success', 'support', 'csm', 'service', 'customer service'):
            milestones.extend([
                (14, 'First Ticket Resolved',
                 'Resolved first customer support ticket independently. Understands ticketing system workflow, '
                 'SLA requirements, and escalation procedures.'),
                (30, 'Handling Cases Solo',
                 'Managing a partial case queue independently. Comfortable with product knowledge base. '
                 'Can troubleshoot common issues without escalation.'),
                (60, 'Client Portfolio Assigned',
                 'Assigned and managing own client portfolio or case volume. Building direct relationships '
                 'with assigned clients. Tracking renewals and satisfaction metrics.'),
            ])
        elif dept_lower in ('marketing', 'communications', 'creative'):
            milestones.extend([
                (14, 'Brand Guidelines Mastered',
                 'Reviewed and understands Celito brand guidelines, tone, and visual identity. '
                 'Familiar with content calendar and campaign planning tools.'),
                (30, 'First Campaign Contribution',
                 'Contributed to first marketing campaign or content piece. Understands approval workflow '
                 'and stakeholder review process.'),
                (60, 'Campaign Ownership',
                 'Owns and executes campaigns or content streams independently. '
                 'Tracks metrics and reports on campaign performance.'),
            ])
        elif dept_lower in ('hr', 'human resources', 'operations', 'people', 'admin'):
            milestones.extend([
                (14, 'Process Documentation Review',
                 'Reviewed all critical process documents and SOPs. Understands compliance requirements '
                 'and key HR/operations deadlines.'),
                (30, 'First Process Improvement',
                 'Identified and proposed or implemented first process improvement. '
                 'Comfortable with HRIS, payroll, or operations tools.'),
                (60, 'Cross-Dept Liaison',
                 'Serving as point of contact for cross-departmental initiatives. '
                 'Building relationships with department heads and key stakeholders.'),
            ])
        elif dept_lower in ('finance', 'accounting', 'billing'):
            milestones.extend([
                (14, 'Systems Access Verified',
                 'All financial systems access verified and functional. Understands chart of accounts, '
                 'cost centers, and approval hierarchies.'),
                (30, 'First Reporting Cycle',
                 'Completed first full reporting cycle (monthly close, AP/AR, or budgeting). '
                 'Understands data sources and reconciliation processes.'),
                (60, 'Audit Preparation',
                 'Can prepare audit documentation independently. Understands compliance requirements '
                 'and internal controls framework.'),
            ])

    # Sort by day_marker and insert
    milestones.sort(key=lambda x: x[0])
    now = datetime.utcnow().isoformat()
    for day, title, expectations in milestones:
        db.execute(
            'INSERT INTO milestones (employee_id, day_marker, title, expectations, status, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (employee_id, day, title, expectations, 'pending', now)
        )


def _ai_generate_milestones(db, emp_id, emp, context=''):
    """Generate milestones using Claude API. Returns True on success, False on failure."""
    api_key = config.get('anthropic.api_key')
    model = config.get('anthropic.model', 'claude-sonnet-4-6')
    if not api_key:
        logger.error("AI milestone generation skipped: no API key")
        return False

    context_line = f"\nAdditional context from manager: {context}" if context else ""
    prompt = (
        f"Generate a comprehensive onboarding milestone plan for a new {emp['role_title']} "
        f"in the {emp['department']} department at Celito Communications, a managed IT services "
        f"and telecom company.{context_line}\n\n"
        f"Create 5-8 milestones spanning Day 7 through Day 90. Include:\n"
        f"- Day 7: First week completion milestone\n"
        f"- Day 14: Early department-specific milestone\n"
        f"- Day 30: One-month check-in\n"
        f"- Day 45-60: Mid-point department-specific milestones\n"
        f"- Day 90: Fully ramped milestone\n\n"
        f"For each milestone, provide:\n"
        f"- day_marker: integer (7, 14, 30, 45, 60, or 90)\n"
        f"- title: short, specific title (not generic — tailored to the role)\n"
        f"- expectations: 2-3 specific, measurable goals for this milestone\n\n"
        f"Return a JSON array of objects with: day_marker, title, expectations.\n"
        f"No markdown, no explanation — only the JSON array."
    )

    try:
        resp = http_requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key': api_key,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            },
            json={
                'model': model,
                'max_tokens': 2048,
                'messages': [{'role': 'user', 'content': prompt}],
                'system': 'You are an HR specialist at a managed IT services and telecom company. Return valid JSON only — no markdown fences, no explanation.',
            },
            timeout=30
        )
        resp.raise_for_status()
        result = resp.json()
        text = ''
        for block in result.get('content', []):
            if block.get('type') == 'text':
                text += block['text']
        text = text.strip()
        # Strip markdown fences if present
        if text.startswith('```'):
            lines = text.split('\n')
            text = '\n'.join(lines[1:])
            if text.endswith('```'):
                text = text[:-3]
            text = text.strip()

        milestones = json.loads(text)
        now = datetime.utcnow().isoformat()
        for m in milestones:
            db.execute(
                'INSERT INTO milestones (employee_id, day_marker, title, expectations, status, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (emp_id, m['day_marker'], m['title'], m['expectations'], 'pending', now)
            )
        db.commit()
        _audit('milestones_ai_generated', 'employee', emp_id, {
            'model': model, 'count': len(milestones), 'context': context[:200] if context else ''
        })
        return True

    except Exception as e:
        logger.error(f"AI milestone generation failed: {e}")
        return False


# ── Onboarding Task Templates ──────────────────────────────────────────────

def _get_onboarding_tasks(employee):
    """Return the full phased onboarding task list for an employee.
    Each item is a dict with task fields + optional 'subtasks' list.
    Conditions are checked against the employee before returning.
    Task list is hire_type-aware: full-time, part-time, contractor, and intern
    each receive a tailored set of tasks."""

    mgr_email = employee.get('manager_email', '')
    dept = employee.get('department', '')
    loc = employee.get('location_mode', 'in-office')
    hire_type = (employee.get('hire_type') or 'full-time').strip().lower()

    # Convenience flags for hire_type filtering
    is_contractor = hire_type == 'contractor'
    is_intern = hire_type == 'intern'
    is_part_time = hire_type == 'part-time'
    is_full_time = hire_type == 'full-time'
    is_temp = is_contractor or is_intern  # short-tenure types

    # Team email assignments from config — tasks show up in "My Tasks" for the right people
    sysadmin_email = config.get('team_assignments.sysadmin_email', '')
    servicedesk_email = config.get('team_assignments.servicedesk_email', '')
    voice_dept_email = config.get('team_assignments.voice_dept_email', '')
    hr_email = config.get('team_assignments.hr_email', '')
    facilities_email = config.get('team_assignments.facilities_email', '')

    all_tasks = []

    # ── Phase 0: Pre-Boarding ───────────────────────────────────────────────
    phase = 'pre_boarding'

    all_tasks.append({
        'title': 'Send personal welcome note to new hire',
        'description': 'Write a personal welcome message (not just HR paperwork). Mention why you are excited to have them join and what to expect in Week 1.',
        'category': 'manager', 'assigned_to': 'Manager', 'assigned_email': mgr_email,
        'phase': phase, 'due_offset_days': -14,
    })
    if not is_contractor:
        all_tasks.append({
            'title': 'Assign buddy/mentor',
            'description': 'Designate a peer outside the reporting line to help the new hire with day-to-day questions.',
            'category': 'manager', 'assigned_to': 'Manager', 'assigned_email': mgr_email,
            'phase': phase, 'due_offset_days': -10,
        })
    else:
        all_tasks.append({
            'title': 'Assign technical point of contact',
            'description': 'Designate a team member as the contractor\'s go-to for technical questions, codebase access, and process guidance.',
            'category': 'manager', 'assigned_to': 'Manager', 'assigned_email': mgr_email,
            'phase': phase, 'due_offset_days': -10,
        })
    if is_intern:
        all_tasks.append({
            'title': 'Assign internship mentor',
            'description': 'Assign a senior team member as the intern\'s dedicated mentor for career guidance, project feedback, and professional development throughout the internship.',
            'category': 'manager', 'assigned_to': 'Manager', 'assigned_email': mgr_email,
            'phase': phase, 'due_offset_days': -10,
        })
    if not is_temp:
        all_tasks.append({
            'title': 'Draft 30/60/90 day plan',
            'description': 'Create explicit expectations: 30 = learn, 60 = contribute with support, 90 = own something. Include an engineered early win in weeks 2-3.',
            'category': 'manager', 'assigned_to': 'Manager', 'assigned_email': mgr_email,
            'phase': phase, 'due_offset_days': -7,
        })
    if is_contractor:
        all_tasks.append({
            'title': 'Define contract deliverables and timeline',
            'description': 'Document the specific deliverables, milestones, and timeline for this contract engagement. Share with the contractor before Day 1.',
            'category': 'manager', 'assigned_to': 'Manager', 'assigned_email': mgr_email,
            'phase': phase, 'due_offset_days': -7,
        })
    if is_intern:
        all_tasks.append({
            'title': 'Define internship project and learning goals',
            'description': 'Outline the intern\'s primary project, learning objectives, and what success looks like by the end of the internship.',
            'category': 'manager', 'assigned_to': 'Manager', 'assigned_email': mgr_email,
            'phase': phase, 'due_offset_days': -7,
        })
    if not is_contractor:
        all_tasks.append({
            'title': 'Ship SWAG box to new hire',
            'description': 'Mail a welcome SWAG box to the new hire\'s home address so it arrives before Day 1.',
            'category': 'hr', 'assigned_to': 'HR', 'assigned_email': hr_email, 'phase': phase, 'due_offset_days': -7,
            'conditions': {'location_mode': 'remote'},
        })
        all_tasks.append({
            'title': 'Prepare SWAG on desk for Day 1',
            'description': 'Place welcome SWAG kit on the new hire\'s desk before their arrival.',
            'category': 'facilities', 'assigned_to': 'Facilities', 'assigned_email': facilities_email, 'phase': phase, 'due_offset_days': -1,
            'conditions': {'location_mode': '!remote'},
        })

    all_tasks.append({
        'title': 'Equipment overview — what is needed by the employee',
        'description': 'Review equipment needs with the new hire and hiring manager. Determine what hardware, software, and peripherals are required for their role.',
        'category': 'hr', 'assigned_to': 'Hiring Manager', 'assigned_email': mgr_email,
        'phase': phase, 'due_offset_days': -10,
    })

    # IT — SysAdmin
    domain_task = {
        'title': 'Create domain user',
        'description': 'Create Active Directory / Entra ID account for the new hire. This unblocks all other IT provisioning.',
        'category': 'it', 'assigned_to': 'SysAdmin', 'assigned_email': sysadmin_email,
        'phase': phase, 'due_offset_days': -5,
        'is_security_critical': True,
    }
    all_tasks.append(domain_task)

    o365_parent = {
        'title': 'Set up Office 365',
        'description': 'Full O365 provisioning — assign license, configure email, distribution lists, calendar, and signature.',
        'category': 'it', 'assigned_to': 'SysAdmin', 'assigned_email': sysadmin_email, 'phase': phase, 'due_offset_days': -3,
        '_depends_on_title': 'Create domain user',
        'subtasks': [
            {'title': 'Assign O365 license and sync to Mimecast', 'description': 'Provision the O365 license and ensure Mimecast email security sync is active.', 'category': 'it', 'assigned_to': 'SysAdmin', 'phase': phase, 'due_offset_days': -3},
            {'title': 'Add to distribution lists', 'description': 'Add the new hire to all relevant department and company-wide distribution lists.', 'category': 'it', 'assigned_to': 'SysAdmin', 'phase': phase, 'due_offset_days': -3},
            {'title': 'Make calendar items visible to all', 'description': 'Configure calendar sharing so the new hire\'s availability is visible to colleagues.', 'category': 'it', 'assigned_to': 'SysAdmin', 'phase': phase, 'due_offset_days': -3},
            {'title': 'Add to \'Resolved\' email group', 'description': 'Add the new hire to the Resolved email distribution group.', 'category': 'it', 'assigned_to': 'SysAdmin', 'phase': phase, 'due_offset_days': -3},
            {'title': 'Set up email signature', 'description': 'Configure the standard Celito email signature with the new hire\'s name, title, and contact information.', 'category': 'it', 'assigned_to': 'SysAdmin', 'phase': phase, 'due_offset_days': -3},
            {'title': 'Notify SF admin for Salesforce setup', 'description': 'Let the Salesforce admin know that the email is set up and ready for SF provisioning.', 'category': 'it', 'assigned_to': 'SysAdmin', 'assigned_email': sysadmin_email, 'phase': phase, 'due_offset_days': -2},
        ],
    }
    all_tasks.append(o365_parent)

    # IT — ServiceDesk computer setup
    computer_parent = {
        'title': 'Computer setup',
        'description': 'Full workstation setup — email, SharePoint, bookmarks, Chrome, Teams, 2FA, Wi-Fi.',
        'category': 'it', 'assigned_to': 'ServiceDesk', 'assigned_email': servicedesk_email, 'phase': phase, 'due_offset_days': -5,
        'subtasks': [
            {'title': 'Email setup (test on employee\'s computer)', 'description': 'Configure and test email client on the new hire\'s workstation.', 'category': 'it', 'assigned_to': 'ServiceDesk', 'assigned_email': servicedesk_email, 'phase': phase, 'due_offset_days': -3},
            {'title': 'Access to applicable SharePoint folders', 'description': 'Grant access to department-specific SharePoint sites and document libraries.', 'category': 'it', 'assigned_to': 'ServiceDesk', 'assigned_email': servicedesk_email, 'phase': phase, 'due_offset_days': -3},
            {'title': 'Wiki login working and bookmarked', 'description': 'Verify wiki/knowledge base access and add to browser bookmarks.', 'category': 'it', 'assigned_to': 'ServiceDesk', 'assigned_email': servicedesk_email, 'phase': phase, 'due_offset_days': -3},
            {'title': 'Download/Install Chrome for Salesforce', 'description': 'Install Google Chrome and configure it for Salesforce access.', 'category': 'it', 'assigned_to': 'ServiceDesk', 'assigned_email': servicedesk_email, 'phase': phase, 'due_offset_days': -3},
            {'title': 'Import all relevant bookmarks', 'description': 'Import the standard Celito bookmarks file for the new hire\'s role.', 'category': 'it', 'assigned_to': 'ServiceDesk', 'assigned_email': servicedesk_email, 'phase': phase, 'due_offset_days': -3},
            {'title': 'Download/Install Teams', 'description': 'Install Microsoft Teams desktop app and verify login.', 'category': 'it', 'assigned_to': 'ServiceDesk', 'assigned_email': servicedesk_email, 'phase': phase, 'due_offset_days': -3},
            {'title': 'WIKI login', 'description': 'Verify wiki access credentials work correctly.', 'category': 'it', 'assigned_to': 'ServiceDesk', 'assigned_email': servicedesk_email, 'phase': phase, 'due_offset_days': -3},
            {'title': 'Share applicable folders', 'description': 'Share network and cloud folders relevant to the new hire\'s department.', 'category': 'it', 'assigned_to': 'ServiceDesk', 'assigned_email': servicedesk_email, 'phase': phase, 'due_offset_days': -3},
            {'title': 'Setup 2-factor authentication', 'description': 'Enable MFA after computer is fully configured. Security-critical step.', 'category': 'it', 'assigned_to': 'ServiceDesk', 'assigned_email': servicedesk_email, 'phase': phase, 'due_offset_days': -2, 'is_security_critical': True},
            {'title': 'Wi-Fi setup — Celito internal network', 'description': 'Connect to the Celito internal network (not guest Wi-Fi).', 'category': 'it', 'assigned_to': 'ServiceDesk', 'assigned_email': servicedesk_email, 'phase': phase, 'due_offset_days': -2, 'conditions': {'location_mode': '!remote'}},
        ],
    }
    all_tasks.append(computer_parent)

    # Ship laptop for remote
    all_tasks.append({
        'title': 'Ship laptop to new hire',
        'description': 'Ship the configured laptop to the new hire\'s home address. Record the tracking number.',
        'category': 'it', 'assigned_to': 'IT', 'phase': phase, 'due_offset_days': -10,
        'conditions': {'location_mode': 'remote'},
    })

    # Facilities
    all_tasks.append({
        'title': 'Badge/keycard ready',
        'description': 'Create building access badge and configure door access permissions.',
        'category': 'facilities', 'assigned_to': 'Facilities', 'assigned_email': facilities_email, 'phase': phase, 'due_offset_days': -3,
        'conditions': {'location_mode': '!remote'},
    })
    all_tasks.append({
        'title': 'Desk/office setup',
        'description': 'Prepare workspace — desk, chair, monitors, peripherals.',
        'category': 'facilities', 'assigned_to': 'Facilities', 'assigned_email': facilities_email, 'phase': phase, 'due_offset_days': -3,
        'conditions': {'location_mode': '!remote'},
    })

    # More manager pre-boarding
    all_tasks.append({
        'title': 'Prepare Week 1 schedule',
        'description': 'Plan the new hire\'s first week: orientation, team intros, training sessions, 1:1s, buddy meeting.',
        'category': 'manager', 'assigned_to': 'Manager', 'assigned_email': mgr_email,
        'phase': phase, 'due_offset_days': -5,
    })
    all_tasks.append({
        'title': 'Send buddy intro email',
        'description': 'Send an email connecting the new hire with their assigned buddy before Day 1.',
        'category': 'manager', 'assigned_to': 'Manager', 'assigned_email': mgr_email,
        'phase': phase, 'due_offset_days': -5,
    })
    all_tasks.append({
        'title': 'Pre-populate Week 1 calendar',
        'description': 'Add orientation sessions, team meetings, 1:1s, and training to the new hire\'s calendar.',
        'category': 'hr', 'assigned_to': 'HR', 'assigned_email': hr_email, 'phase': phase, 'due_offset_days': -3,
    })

    # Employee pre-boarding tasks — things the new hire does before Day 1
    emp_email = employee.get('email', '')
    all_tasks.append({
        'title': 'Watch welcome video from leadership',
        'description': 'Watch a short welcome message from Celito leadership to learn about our mission, values, and what makes Celito special.',
        'category': 'employee', 'assigned_to': 'Employee', 'assigned_email': emp_email,
        'phase': phase, 'due_offset_days': -5, 'is_acknowledgment': True,
        'resource_url': 'https://celito.sharepoint.com/onboarding/welcome-video',
    })
    all_tasks.append({
        'title': 'Fill out your team bio and intro',
        'description': 'Tell us about yourself! Fill out a short bio that will be shared with your new team. Include your background, interests, and a fun fact.',
        'category': 'employee', 'assigned_to': 'Employee', 'assigned_email': emp_email,
        'phase': phase, 'due_offset_days': -5, 'is_acknowledgment': True,
    })
    all_tasks.append({
        'title': 'Review your Day 1 logistics guide',
        'description': 'Everything you need to know for your first day: where to go, what to bring, parking info, dress code, and your first-week schedule overview.',
        'category': 'employee', 'assigned_to': 'Employee', 'assigned_email': emp_email,
        'phase': phase, 'due_offset_days': -3, 'is_acknowledgment': True,
        'resource_url': 'https://celito.sharepoint.com/onboarding/day-1-guide',
    })
    all_tasks.append({
        'title': 'Pre-read: How Celito Makes Money',
        'description': 'A 10-minute overview of Celito\'s business model, customers, products, and competitive landscape. You\'ll discuss this with leadership during Week 1.',
        'category': 'employee', 'assigned_to': 'Employee', 'assigned_email': emp_email,
        'phase': phase, 'due_offset_days': -3, 'is_acknowledgment': True,
        'resource_url': 'https://celito.sharepoint.com/onboarding/business-overview',
    })
    all_tasks.append({
        'title': 'Set up your profile photo',
        'description': 'Upload a professional photo for your Celito accounts (email, Teams, Slack). This helps your new teammates recognize you!',
        'category': 'employee', 'assigned_to': 'Employee', 'assigned_email': emp_email,
        'phase': phase, 'due_offset_days': -2, 'is_acknowledgment': True,
    })

    # ── Phase 1: Company Onboarding (Week 1) ────────────────────────────────
    phase = 'company_onboarding'

    all_tasks.append({
        'title': 'Complete NDA',
        'description': 'Review and sign the Non-Disclosure Agreement.',
        'category': 'employee', 'assigned_to': 'Employee', 'phase': phase,
        'due_offset_days': 0, 'is_acknowledgment': True, 'compliance_required': True,
        'resource_url': 'https://celito.sharepoint.com/hr/nda-form',
    })
    all_tasks.append({
        'title': 'Background check complete',
        'description': 'HR verifies that the background check has cleared.',
        'category': 'hr', 'assigned_to': 'HR', 'assigned_email': hr_email, 'phase': phase, 'due_offset_days': 0,
    })
    all_tasks.append({
        'title': 'Enroll in alarm.com and create mobile credentials',
        'description': 'Set up alarm.com account and configure mobile access credentials for building security.',
        'category': 'hr', 'assigned_to': 'HR', 'assigned_email': hr_email, 'phase': phase, 'due_offset_days': 0,
        'conditions': {'location_mode': '!remote'},
    })
    if not is_contractor:
        all_tasks.append({
            'title': 'Enroll in payroll & benefits (Insperity)',
            'description': 'Set up the new hire in Insperity for payroll processing, health insurance, and benefits enrollment.' if not is_intern
                else 'Set up the intern in Insperity for payroll processing (limited benefits for intern position).',
            'category': 'hr', 'assigned_to': 'HR', 'assigned_email': hr_email, 'phase': phase, 'due_offset_days': 0,
        })
    else:
        all_tasks.append({
            'title': 'Verify contractor payment setup',
            'description': 'Confirm contractor invoicing details, payment terms, and W-9 on file. Contractors are not enrolled in Insperity — payments go through Accounts Payable.',
            'category': 'hr', 'assigned_to': 'HR', 'assigned_email': hr_email, 'phase': phase, 'due_offset_days': 0,
        })
    if not is_temp:
        all_tasks.append({
            'title': 'Set up corporate expense card (Ramp)',
            'description': 'Create a Ramp account for the employee\'s corporate expense card. Used for business purchases and expense tracking.',
            'category': 'hr', 'assigned_to': 'HR', 'assigned_email': hr_email, 'phase': phase, 'due_offset_days': 1,
            'conditions': {'department': 'Sales'},
        })
        all_tasks.append({
            'title': 'Create pin for Gas card',
            'description': 'Set up gas card PIN for field employees.',
            'category': 'hr', 'assigned_to': 'HR', 'assigned_email': hr_email, 'phase': phase, 'due_offset_days': 1,
            'conditions': {'department': 'Sales'},
        })
    all_tasks.append({
        'title': 'Review and acknowledge Celito Policies',
        'description': 'Read all company policies (acceptable use, PTO, code of conduct, etc.) and confirm acknowledgment.',
        'category': 'employee', 'assigned_to': 'Employee', 'phase': phase,
        'due_offset_days': 1, 'is_acknowledgment': True, 'compliance_required': True,
        'resource_url': 'https://celito.sharepoint.com/hr/company-policies',
    })
    all_tasks.append({
        'title': 'Attend CEO/Leadership presentation',
        'description': 'Attend the live session with senior leadership on Celito\'s mission, history, and strategy.',
        'category': 'employee', 'assigned_to': 'Employee', 'phase': phase,
        'due_offset_days': 3, 'is_acknowledgment': True,
        'resource_url': 'https://celito.sharepoint.com/onboarding/leadership-session',
    })
    all_tasks.append({
        'title': 'Review "Values in Practice" stories',
        'description': 'Read real stories of decisions Celito made because of its values, including hard tradeoffs.',
        'category': 'employee', 'assigned_to': 'Employee', 'phase': phase,
        'due_offset_days': 3, 'is_acknowledgment': True,
        'resource_url': 'https://celito.sharepoint.com/onboarding/values-in-practice',
    })
    all_tasks.append({
        'title': 'Review "How the Business Makes Money"',
        'description': 'Learn about Celito\'s customers, products, services, and competitive landscape.',
        'category': 'employee', 'assigned_to': 'Employee', 'phase': phase,
        'due_offset_days': 3, 'is_acknowledgment': True,
        'resource_url': 'https://celito.sharepoint.com/onboarding/business-model',
    })
    all_tasks.append({
        'title': 'Post welcome announcement to Slack',
        'description': 'Post a welcome message to #general and the team channel announcing the new hire.',
        'category': 'hr', 'assigned_to': 'HR', 'assigned_email': hr_email, 'phase': phase, 'due_offset_days': 0,
    })
    all_tasks.append({
        'title': 'Team lunch or virtual coffee',
        'description': 'Organize a team lunch (in-office) or virtual coffee roulette (remote) to welcome the new hire.',
        'category': 'manager', 'assigned_to': 'Manager', 'assigned_email': mgr_email,
        'phase': phase, 'due_offset_days': 2,
    })
    all_tasks.append({
        'title': 'Standard procedures training (SOP)',
        'description': 'Schedule a training session to walk through Celito\'s standard operating procedures — how we handle common tasks, escalations, and workflows.',
        'category': 'hr', 'assigned_to': 'HR', 'assigned_email': hr_email, 'phase': phase, 'due_offset_days': 3,
    })

    # ── Contractor-specific tasks ────────────────────────────────────────────
    if is_contractor:
        all_tasks.append({
            'title': 'Review and sign contractor agreement / SOW',
            'description': 'Review your Statement of Work, contractor agreement, and IP assignment terms. Sign and return to HR.',
            'category': 'employee', 'assigned_to': 'Employee', 'phase': phase,
            'due_offset_days': 0, 'is_acknowledgment': True, 'compliance_required': True,
            'resource_url': 'https://celito.sharepoint.com/hr/contractor-agreement',
        })
        all_tasks.append({
            'title': 'Set system access expiration dates',
            'description': 'Configure all contractor accounts with automatic expiration aligned to the contract end date. Document in IT Glue.',
            'category': 'it', 'assigned_to': 'SysAdmin', 'assigned_email': sysadmin_email,
            'phase': phase, 'due_offset_days': 1,
        })
        all_tasks.append({
            'title': 'Review contractor billing and time-tracking process',
            'description': 'Learn how to submit timesheets/invoices, the billing cycle, and who to contact about payment questions.',
            'category': 'employee', 'assigned_to': 'Employee', 'phase': phase,
            'due_offset_days': 1, 'is_acknowledgment': True,
        })

    # ── Intern-specific tasks ─────────────────────────────────────────────
    if is_intern:
        all_tasks.append({
            'title': 'Attend intern orientation session',
            'description': 'Join the intern cohort orientation covering: the intern program structure, expectations, how to get the most out of your internship, and meet fellow interns.',
            'category': 'employee', 'assigned_to': 'Employee', 'phase': phase,
            'due_offset_days': 0, 'is_acknowledgment': True,
        })
        all_tasks.append({
            'title': 'Set up internship project Kanban board',
            'description': 'Create a project board (Teams Planner or Trello) to track your internship deliverables and weekly goals. Share with your mentor and manager.',
            'category': 'employee', 'assigned_to': 'Employee', 'phase': phase,
            'due_offset_days': 3, 'is_acknowledgment': True,
        })

    # Remote/Hybrid-specific onboarding tasks
    all_tasks.append({
        'title': 'Set up VPN access',
        'description': 'Download and configure the Celito VPN client for secure remote access to company resources.',
        'category': 'it', 'assigned_to': 'ServiceDesk', 'assigned_email': servicedesk_email,
        'phase': phase, 'due_offset_days': 1,
        'conditions': {'location_mode': 'remote'},
        'resource_url': 'https://celito.sharepoint.com/it/vpn-setup-guide',
    })
    all_tasks.append({
        'title': 'Set up VPN access',
        'description': 'Download and configure the Celito VPN client for secure remote access when working from home.',
        'category': 'it', 'assigned_to': 'ServiceDesk', 'assigned_email': servicedesk_email,
        'phase': phase, 'due_offset_days': 3,
        'conditions': {'location_mode': 'hybrid'},
        'resource_url': 'https://celito.sharepoint.com/it/vpn-setup-guide',
    })
    all_tasks.append({
        'title': 'Laptop unboxing and initial setup',
        'description': 'Follow the setup guide to unbox your Celito laptop, complete initial configuration, install required software, and verify all logins work.',
        'category': 'it', 'assigned_to': 'Employee', 'assigned_email': emp_email,
        'phase': phase, 'due_offset_days': 0, 'is_acknowledgment': True,
        'conditions': {'location_mode': 'remote'},
        'resource_url': 'https://celito.sharepoint.com/it/laptop-setup-guide',
    })
    all_tasks.append({
        'title': 'Complete home office setup checklist',
        'description': 'Verify your home office meets Celito ergonomic standards: proper desk height, monitor at eye level, good lighting, stable internet. Submit the checklist for a home office stipend if needed.',
        'category': 'employee', 'assigned_to': 'Employee', 'assigned_email': emp_email,
        'phase': phase, 'due_offset_days': 3, 'is_acknowledgment': True,
        'conditions': {'location_mode': 'remote'},
        'resource_url': 'https://celito.sharepoint.com/hr/home-office-checklist',
    })
    all_tasks.append({
        'title': 'Test video and audio setup',
        'description': 'Before your first team meeting, test your camera, microphone, and speaker. Make sure your background is professional or use a virtual background. Join a test meeting with your buddy.',
        'category': 'employee', 'assigned_to': 'Employee', 'assigned_email': emp_email,
        'phase': phase, 'due_offset_days': 1, 'is_acknowledgment': True,
        'conditions': {'location_mode': 'remote'},
    })
    all_tasks.append({
        'title': 'Share your working hours and timezone with the team',
        'description': 'Post your typical working hours and timezone in your team Slack channel and update your Teams/Slack status. This helps your teammates know when to reach you.',
        'category': 'employee', 'assigned_to': 'Employee', 'assigned_email': emp_email,
        'phase': phase, 'due_offset_days': 2, 'is_acknowledgment': True,
        'conditions': {'location_mode': 'remote'},
    })
    all_tasks.append({
        'title': 'Review async communication norms',
        'description': 'At Celito, we balance sync and async communication. Review our guide: when to Slack vs email vs meet, expected response times, timezone etiquette, and how to keep distributed teammates in the loop.',
        'category': 'employee', 'assigned_to': 'Employee', 'assigned_email': emp_email,
        'phase': phase, 'due_offset_days': 3, 'is_acknowledgment': True,
        'conditions': {'location_mode': 'remote'},
        'resource_url': 'https://celito.sharepoint.com/onboarding/async-norms',
    })
    # Hybrid employees also get a subset of remote tasks
    all_tasks.append({
        'title': 'Complete home office setup checklist',
        'description': 'Verify your home workspace meets Celito ergonomic standards for your work-from-home days.',
        'category': 'employee', 'assigned_to': 'Employee', 'assigned_email': emp_email,
        'phase': phase, 'due_offset_days': 5, 'is_acknowledgment': True,
        'conditions': {'location_mode': 'hybrid'},
        'resource_url': 'https://celito.sharepoint.com/hr/home-office-checklist',
    })
    all_tasks.append({
        'title': 'Review async communication norms',
        'description': 'Review our guide on balancing in-office and remote communication: when to Slack vs email vs meet, and how to keep remote teammates in the loop.',
        'category': 'employee', 'assigned_to': 'Employee', 'assigned_email': emp_email,
        'phase': phase, 'due_offset_days': 5, 'is_acknowledgment': True,
        'conditions': {'location_mode': 'hybrid'},
        'resource_url': 'https://celito.sharepoint.com/onboarding/async-norms',
    })

    # Logins Required — verification checklist
    all_tasks.append({
        'title': 'Verify all logins are working',
        'description': 'Confirm the new employee can successfully log into all required systems after accounts are provisioned.',
        'category': 'it', 'assigned_to': 'ServiceDesk', 'assigned_email': servicedesk_email, 'phase': phase, 'due_offset_days': 1,
        '_depends_on_title': 'Set up Office 365',
        'subtasks': [
            {'title': 'Celito domain login verified', 'description': 'Verify Active Directory / domain credentials work.', 'category': 'it', 'assigned_to': 'ServiceDesk', 'assigned_email': servicedesk_email, 'phase': phase, 'due_offset_days': 1},
            {'title': 'Office 365 login verified', 'description': 'Verify O365 email and portal access.', 'category': 'it', 'assigned_to': 'ServiceDesk', 'assigned_email': servicedesk_email, 'phase': phase, 'due_offset_days': 1},
            {'title': 'Teams login verified', 'description': 'Verify Microsoft Teams desktop and web login.', 'category': 'it', 'assigned_to': 'ServiceDesk', 'assigned_email': servicedesk_email, 'phase': phase, 'due_offset_days': 1},
            {'title': 'Mimecast login verified', 'description': 'Verify Mimecast email security portal access.', 'category': 'it', 'assigned_to': 'ServiceDesk', 'assigned_email': servicedesk_email, 'phase': phase, 'due_offset_days': 1},
            {'title': 'Salesforce login verified', 'description': 'Verify Salesforce CRM access.', 'category': 'it', 'assigned_to': 'ServiceDesk', 'assigned_email': servicedesk_email, 'phase': phase, 'due_offset_days': 1},
            {'title': 'TractionTools login verified', 'description': 'Verify TractionTools / Bloom Growth login.', 'category': 'it', 'assigned_to': 'ServiceDesk', 'assigned_email': servicedesk_email, 'phase': phase, 'due_offset_days': 1},
            {'title': 'Celito Voice login verified', 'description': 'Verify phone system / voicemail login.', 'category': 'it', 'assigned_to': 'ServiceDesk', 'assigned_email': servicedesk_email, 'phase': phase, 'due_offset_days': 1, 'conditions': {'department': 'Sales'}},
            {'title': 'IT Glue login verified', 'description': 'Verify IT Glue documentation platform access.', 'category': 'it', 'assigned_to': 'ServiceDesk', 'assigned_email': servicedesk_email, 'phase': phase, 'due_offset_days': 1, 'conditions': {'department': 'IT'}},
            {'title': 'ScreenConnect login verified', 'description': 'Verify ScreenConnect remote access tool login.', 'category': 'it', 'assigned_to': 'ServiceDesk', 'assigned_email': servicedesk_email, 'phase': phase, 'due_offset_days': 1, 'conditions': {'department': 'IT'}},
            {'title': 'Automate login verified', 'description': 'Verify ConnectWise Automate login.', 'category': 'it', 'assigned_to': 'ServiceDesk', 'assigned_email': servicedesk_email, 'phase': phase, 'due_offset_days': 1, 'conditions': {'department': 'IT'}},
            {'title': 'Spy/PRTG login verified', 'description': 'Verify PRTG network monitoring login.', 'category': 'it', 'assigned_to': 'ServiceDesk', 'assigned_email': servicedesk_email, 'phase': phase, 'due_offset_days': 1, 'conditions': {'department': 'IT'}},
        ],
    })

    # ── Phase 2: Department Onboarding (Weeks 1-4) ──────────────────────────
    phase = 'department_onboarding'

    all_tasks.append({
        'title': 'Department strategy overview',
        'description': 'Department leader presents: how this department fits the company strategy, its goals for the year, and how success is measured.',
        'category': 'manager', 'assigned_to': 'Dept Leader', 'phase': phase, 'due_offset_days': 5,
    })
    all_tasks.append({
        'title': 'Identify and assign early win deliverable',
        'description': 'Give the new hire a real, scoped deliverable in the first 2-3 weeks that builds confidence and visibility.',
        'category': 'manager', 'assigned_to': 'Manager', 'assigned_email': mgr_email,
        'phase': phase, 'due_offset_days': 5,
    })
    all_tasks.append({
        'title': 'Schedule shadowing sessions (3 meetings)',
        'description': 'Set up the new hire to sit in on 3 real meetings, calls, or processes in weeks 1-2 — even before they can contribute.',
        'category': 'manager', 'assigned_to': 'Manager', 'assigned_email': mgr_email,
        'phase': phase, 'due_offset_days': 7,
    })
    all_tasks.append({
        'title': 'Review "How We Work" department guide',
        'description': 'Read the department\'s guide covering: decision-making, work tracking, meeting cadences, and communication norms (when to Slack vs email vs meet).',
        'category': 'employee', 'assigned_to': 'Employee', 'phase': phase,
        'due_offset_days': 7, 'is_acknowledgment': True,
        'resource_url': f'https://celito.sharepoint.com/departments/{dept.lower().replace(" ", "-") if dept else "general"}/how-we-work',
    })

    # Voice dept — conditional
    all_tasks.append({
        'title': 'Phone setup',
        'description': 'Configure desk phone or softphone, assign extension.',
        'category': 'it', 'assigned_to': 'Voice Dept', 'assigned_email': voice_dept_email, 'phase': phase, 'due_offset_days': 3,
        'conditions': {'location_mode': '!remote'},
    })
    all_tasks.append({
        'title': 'Add to salesvm@celito.net',
        'description': 'Add Sales team employee to the sales voicemail distribution.',
        'category': 'it', 'assigned_to': 'Voice Dept', 'assigned_email': voice_dept_email, 'phase': phase, 'due_offset_days': 3,
        'conditions': {'department': 'Sales'},
    })

    # Training on conference room
    all_tasks.append({
        'title': 'Training on conference room setup',
        'description': 'Show the new hire how to use conference room AV equipment.',
        'category': 'it', 'assigned_to': 'ServiceDesk', 'assigned_email': servicedesk_email, 'phase': phase, 'due_offset_days': 5,
        'conditions': {'location_mode': '!remote'},
    })
    all_tasks.append({
        'title': 'Populate calendar with training appointments',
        'description': 'Add all scheduled training sessions to the new hire\'s calendar.',
        'category': 'hr', 'assigned_to': 'HR', 'assigned_email': hr_email, 'phase': phase, 'due_offset_days': 5,
    })

    # Meet-and-greet tasks — tailored by hire_type
    # Full-time/part-time: meet all department leads (8 meetings over 4 weeks)
    # Contractors: meet only direct stakeholders (3 meetings in week 1-2)
    # Interns: meet key leads + intern program coordinator (5 meetings)
    if is_contractor:
        meet_greet_roles = [
            ('HR Lead', 7), ('IT Lead', 8),
            ('Direct Team Lead', 10),
        ]
    elif is_intern:
        meet_greet_roles = [
            ('CEO / Founder', 7), ('HR Lead', 7), ('IT Lead', 8),
            ('Intern Program Coordinator', 10), ('Direct Team Lead', 14),
        ]
    else:
        meet_greet_roles = [
            ('CEO / Founder', 7), ('HR Lead', 7), ('IT Lead', 8),
            ('Sales Leader', 10), ('Operations Lead', 14), ('Finance Lead', 14),
            ('Customer Success Lead', 21), ('Engineering Lead', 21),
        ]
    for role_name, offset in meet_greet_roles:
        all_tasks.append({
            'title': f'Meet & greet: {role_name}',
            'description': f'30-minute introductory meeting with {role_name}. Learn about their team and how you\'ll work together.',
            'category': 'employee', 'assigned_to': 'Employee', 'phase': phase,
            'due_offset_days': offset, 'is_acknowledgment': True,
        })

    # ── Phase 3: Role-Specific Training (Days 1-90) ─────────────────────────
    phase = 'role_training'

    # Sales training — conditional on department
    sales_modules = [
        ('Sales Training: Internet/celitoFiber', 5, 'https://celito.sharepoint.com/sales/training/internet-celitofiber'),
        ('Sales Training: Data Center', 7, 'https://celito.sharepoint.com/sales/training/data-center'),
        ('Sales Training: Carrier Training', 9, 'https://celito.sharepoint.com/sales/training/carrier'),
        ('Sales Training: Consulting', 11, 'https://celito.sharepoint.com/sales/training/consulting'),
        ('Sales Training: Voice', 13, 'https://celito.sharepoint.com/sales/training/voice'),
        ('Sales Training: Equipment', 15, 'https://celito.sharepoint.com/sales/training/equipment'),
        ('Sales Training: Third Party Solutions (O365, Mimecast, etc.)', 17, 'https://celito.sharepoint.com/sales/training/third-party'),
        ('Set weekly sales meeting', 5, ''),
        ('Sales Training: Contract Training', 19, 'https://celito.sharepoint.com/sales/training/contracts'),
        ('Sales Training: SOW Training', 21, 'https://celito.sharepoint.com/sales/training/sow'),
        ('Sales Training: Client List', 14, 'https://celito.sharepoint.com/sales/training/client-list'),
        ('Sales Training: Salesforce Overview', 10, 'https://celito.sharepoint.com/sales/training/salesforce'),
        ('Sales Training: Billing Review', 23, 'https://celito.sharepoint.com/sales/training/billing'),
        ('Sales Training: Lit Building Process', 25, 'https://celito.sharepoint.com/sales/training/lit-building'),
        ('Sales Training: Vendor Overview', 27, 'https://celito.sharepoint.com/sales/training/vendors'),
        ('Sales Training: Engineer Usage', 29, 'https://celito.sharepoint.com/sales/training/engineer-usage'),
    ]
    for title, offset, url in sales_modules:
        task = {
            'title': title,
            'description': f'Complete the {title.replace("Sales Training: ", "")} training module.',
            'category': 'training', 'assigned_to': 'Sales Trainer', 'phase': phase,
            'due_offset_days': offset, 'is_acknowledgment': True,
            'conditions': {'department': 'Sales'},
        }
        if url:
            task['resource_url'] = url
        all_tasks.append(task)

    # IT training for everyone
    it_training = [
        ('Training: Ticketing System', 7, 'https://celito.sharepoint.com/it/training/ticketing'),
        ('Training: IT Glue', 10, 'https://celito.sharepoint.com/it/training/it-glue'),
        ('Training: Salesforce', 12, 'https://celito.sharepoint.com/it/training/salesforce'),
        ('Training: Teams', 5, 'https://celito.sharepoint.com/it/training/teams'),
        ('Training: SharePoint', 7, 'https://celito.sharepoint.com/it/training/sharepoint'),
    ]
    for title, offset, url in it_training:
        all_tasks.append({
            'title': title,
            'description': f'Complete {title.replace("Training: ", "")} training module.',
            'category': 'training', 'assigned_to': 'Employee', 'phase': phase,
            'due_offset_days': offset, 'is_acknowledgment': True,
            'resource_url': url,
        })

    # Manager check-ins — consolidated cadence tasks (tailored by hire_type)
    all_tasks.append({
        'title': 'Daily check-ins — Week 1',
        'description': 'Hold brief 15-minute daily check-ins with your new hire Monday through Friday of their first week. Cover: How are you settling in? Any blockers? What do you need?',
        'category': 'manager', 'assigned_to': 'Manager', 'assigned_email': mgr_email,
        'phase': 'company_onboarding', 'due_offset_days': 5,
    })
    if not is_contractor:
        all_tasks.append({
            'title': 'Weekly 1:1s — Weeks 2-4',
            'description': 'Hold weekly 30-minute 1:1s during the first month. Discuss onboarding progress, answer questions, and ensure they have what they need to succeed.',
            'category': 'manager', 'assigned_to': 'Manager', 'assigned_email': mgr_email,
            'phase': 'department_onboarding', 'due_offset_days': 28,
        })
    if not is_temp:
        all_tasks.append({
            'title': 'Weekly 1:1s — Weeks 5-12',
            'description': 'Continue weekly 30-minute 1:1s through the end of the 90-day onboarding period. Shift focus from onboarding support to performance development.',
            'category': 'manager', 'assigned_to': 'Manager', 'assigned_email': mgr_email,
            'phase': phase, 'due_offset_days': 84,
        })

    # Reviews — tailored by hire_type
    if is_contractor:
        # Contractors get contract milestone check-ins instead of 30/60/90
        all_tasks.append({
            'title': 'Mid-contract deliverable review',
            'description': 'Review contractor progress against SOW deliverables. Confirm timeline, address blockers, and adjust scope if needed.',
            'category': 'manager', 'assigned_to': 'Manager', 'assigned_email': mgr_email,
            'phase': phase, 'due_offset_days': 30,
        })
        all_tasks.append({
            'title': 'Contract completion review',
            'description': 'Final review of all contract deliverables. Document outcomes, knowledge transfer status, and whether to extend or conclude the engagement.',
            'category': 'manager', 'assigned_to': 'Manager', 'assigned_email': mgr_email,
            'phase': phase, 'due_offset_days': 60,
        })
    elif is_intern:
        # Interns get mid-internship and end-of-internship reviews
        all_tasks.append({
            'title': 'Mid-internship review',
            'description': 'Formal check-in at the midpoint: review project progress, discuss what the intern has learned, and adjust goals for the second half.',
            'category': 'manager', 'assigned_to': 'Manager', 'assigned_email': mgr_email,
            'phase': phase, 'due_offset_days': 30,
        })
        all_tasks.append({
            'title': 'End-of-internship presentation',
            'description': 'The intern presents their project and key learnings to the team and leadership. Schedule a 30-minute slot and invite stakeholders.',
            'category': 'employee', 'assigned_to': 'Employee', 'phase': phase,
            'due_offset_days': 55, 'is_acknowledgment': True,
        })
        all_tasks.append({
            'title': 'Final internship review and return offer discussion',
            'description': 'Final review: assess intern performance, discuss career interests, and determine if a return offer (full-time or next summer) is appropriate.',
            'category': 'manager', 'assigned_to': 'Manager', 'assigned_email': mgr_email,
            'phase': phase, 'due_offset_days': 60,
        })
    else:
        # Full-time and part-time get the standard 30/60/90 reviews
        all_tasks.append({
            'title': '30-day review',
            'description': 'Formal 30-day milestone review. Assess: Has the new hire learned the role, team, and processes? Review 30-day plan goals.',
            'category': 'manager', 'assigned_to': 'Manager', 'assigned_email': mgr_email,
            'phase': phase, 'due_offset_days': 30,
        })
        all_tasks.append({
            'title': '60-day review',
            'description': 'Formal 60-day milestone review. Assess: Is the new hire contributing with support? Review 60-day plan goals.',
            'category': 'manager', 'assigned_to': 'Manager', 'assigned_email': mgr_email,
            'phase': phase, 'due_offset_days': 60,
        })
        all_tasks.append({
            'title': '90-day "Fully Ramped" review',
            'description': 'Final onboarding review. Determine if the new hire owns their responsibilities independently. Mark as fully ramped or extend onboarding.',
            'category': 'manager', 'assigned_to': 'Manager', 'assigned_email': mgr_email,
            'phase': phase, 'due_offset_days': 90,
        })

    # Filter by conditions
    filtered = []
    for task in all_tasks:
        if _check_condition(task.get('conditions'), employee):
            # Also filter subtasks
            if 'subtasks' in task:
                task['subtasks'] = [st for st in task['subtasks'] if _check_condition(st.get('conditions'), employee)]
            filtered.append(task)
    return filtered


def _get_offboarding_tasks(employee, term_type, last_day):
    """Return the full offboarding task list matching actual Celito checklists."""
    mgr_email = employee.get('manager_email', '')
    dept = employee.get('department', '')
    is_involuntary = term_type == 'involuntary'
    today_str = date.today().isoformat()

    # Team email assignments from config
    sysadmin_email = config.get('team_assignments.sysadmin_email', '')
    servicedesk_email = config.get('team_assignments.servicedesk_email', '')
    hr_email = config.get('team_assignments.hr_email', '')
    facilities_email = config.get('team_assignments.facilities_email', '')

    all_tasks = []

    # ── HR Tasks ────────────────────────────────────────────────────────────
    all_tasks.append({
        'title': 'Open Employee Separation Ticket in Salesforce',
        'description': 'Create the separation Case in Salesforce. (Auto-triggered by the platform.)',
        'category': 'hr', 'assigned_to': 'HR', 'assigned_email': hr_email, 'due_date': today_str, 'due_offset_days': 0,
    })
    # Equipment return — sub-items generated dynamically from equipment table
    all_tasks.append({
        'title': 'Return Celito equipment (laptop/desk phone/etc.)',
        'description': 'Collect and verify return of all company equipment. Check against equipment records.',
        'category': 'hr', 'assigned_to': 'HR', 'assigned_email': hr_email, 'due_date': last_day,
        'subtasks': [],  # populated dynamically from equipment table
    })
    all_tasks.append({
        'title': 'Return Celito access badge/fob',
        'description': 'Collect building access badge and fob from departing employee.',
        'category': 'hr', 'assigned_to': 'HR', 'assigned_email': hr_email, 'due_date': last_day,
    })
    all_tasks.append({
        'title': 'Remove from Bloom Growth',
        'description': 'Remove the employee from Bloom Growth platform.',
        'category': 'hr', 'assigned_to': 'HR', 'assigned_email': hr_email, 'due_date': last_day,
    })
    all_tasks.append({
        'title': 'Remove from Alarm.com for building access',
        'description': 'Revoke alarm.com building access credentials.',
        'category': 'hr', 'assigned_to': 'HR', 'assigned_email': hr_email,
        'due_date': today_str if is_involuntary else last_day,
        'is_security_critical': is_involuntary,
        'urgency': 'immediate' if is_involuntary else 'normal',
    })
    all_tasks.append({
        'title': 'Insperity termination completed',
        'description': 'Complete the termination process in Insperity (payroll, benefits cutoff, COBRA).',
        'category': 'hr', 'assigned_to': 'HR', 'assigned_email': hr_email, 'due_date': last_day,
    })
    all_tasks.append({
        'title': 'Remove from NetSuite (Accounting only)',
        'description': 'Remove user access from NetSuite accounting system.',
        'category': 'hr', 'assigned_to': 'HR', 'assigned_email': hr_email, 'due_date': last_day,
        'conditions': {'department': 'Accounting'},
    })
    all_tasks.append({
        'title': 'Disable Salesforce account',
        'description': 'Deactivate (do not delete) the employee\'s Salesforce account to preserve data.',
        'category': 'hr', 'assigned_to': 'HR', 'assigned_email': hr_email, 'due_date': last_day,
    })
    all_tasks.append({
        'title': 'Disable DocuSign account',
        'description': 'Deactivate DocuSign access.',
        'category': 'hr', 'assigned_to': 'HR', 'assigned_email': hr_email, 'due_date': last_day,
    })
    all_tasks.append({
        'title': 'Disable Levitate account',
        'description': 'Deactivate Levitate platform access.',
        'category': 'hr', 'assigned_to': 'HR', 'assigned_email': hr_email, 'due_date': last_day,
    })
    all_tasks.append({
        'title': 'Remove from vendor accounts (TD Synnex, Dell, etc.)',
        'description': 'Remove the employee from all third-party vendor portals and partner access.',
        'category': 'hr', 'assigned_to': 'HR', 'assigned_email': hr_email, 'due_date': last_day,
    })
    # Exit interview — 3 days before last day
    try:
        exit_interview_date = (date.fromisoformat(last_day) - timedelta(days=3)).isoformat()
    except (ValueError, TypeError):
        exit_interview_date = last_day
    all_tasks.append({
        'title': 'Schedule exit interview',
        'description': 'Schedule a 30-minute exit interview with the departing employee. '
                       'Use the standard exit interview question set. Document feedback for quarterly review.',
        'category': 'hr', 'assigned_to': 'HR', 'assigned_email': hr_email,
        'due_date': exit_interview_date,
    })

    # ── System Admin Tasks ──────────────────────────────────────────────────
    sysadmin_due = today_str if is_involuntary else last_day
    sysadmin_urgency = 'immediate' if is_involuntary else 'normal'

    sysadmin_tasks = [
        ('Change alternative domain administrator password', True,
         'Change the secondary domain admin password to prevent continued administrative access.'),
        ('Change Domain Admin password', True,
         'Change the primary domain administrator password. Critical for preventing unauthorized access to domain-level resources.'),
        ('Delete CV3 System Access', True,
         'Remove access to the CV3 client management portal.'),
        ('Delete Automate account', True,
         'Remove the ConnectWise Automate (remote monitoring & management) account.'),
        ('Delete PRTG account', True,
         'Remove access to PRTG network monitoring — prevents visibility into client network infrastructure.'),
        ('Remove Datto account', True,
         'Remove Datto backup & disaster recovery platform access.'),
        ('Disable Active Directory account', True,
         'Disable the network login account. This blocks Windows sign-in, VPN, and all domain-joined services.'),
        ('Disable PSA account', True,
         'Disable the ConnectWise PSA (Professional Services Automation) ticketing and billing account.'),
        ('Disable Appriver account', True,
         'Disable the AppRiver email security and filtering account.'),
        ('Disable IT Glue account', True,
         'Disable access to IT Glue — the documentation platform containing client passwords and configurations.'),
        ('Disable Office 365 and SharePoint Access', True,
         'Disable Microsoft 365 email, OneDrive, and SharePoint access. Data is retained per policy.'),
        ('Disable ActiveSync, OWA mobile and OWA', True,
         'Disable mobile email sync and Outlook Web Access so email cannot be read from personal devices.'),
        ('Check mobile phone MFA associations with client accounts', True,
         'Verify and remove any multi-factor authentication (MFA) tokens on the employee\'s phone that are linked to client accounts.'),
        ('Revoke VPN access', True,
         'Remove VPN credentials to prevent remote network access to internal and client environments.'),
        ('Disable Mimecast account', True,
         'Disable the Mimecast email security gateway account.'),
        ('Remove from Slack workspace', False,
         'Remove the employee from all Slack channels and deactivate their workspace account.'),
        ('Remove from Microsoft Teams', False,
         'Remove the employee from all Teams channels and groups.'),
    ]
    # Track password-change task titles for dependency
    password_change_titles = [
        'Change alternative domain administrator password',
        'Change Domain Admin password',
    ]

    for title, security_critical, desc in sysadmin_tasks:
        all_tasks.append({
            'title': title,
            'description': f'{desc} {"⚠️ IMMEDIATE — involuntary termination." if is_involuntary else "Complete before last day."}',
            'category': 'it', 'assigned_to': 'System Admin', 'assigned_email': sysadmin_email,
            'due_date': sysadmin_due,
            'is_security_critical': security_critical,
            'urgency': sysadmin_urgency,
        })

    # Password notification tasks (depend on password changes)
    all_tasks.append({
        'title': 'Inform employees of new passwords',
        'description': 'After changing admin passwords, notify affected employees of the new credentials.',
        'category': 'it', 'assigned_to': 'System Admin', 'assigned_email': sysadmin_email,
        'due_date': sysadmin_due,
        '_depends_on_title': 'Change Domain Admin password',
    })
    all_tasks.append({
        'title': 'Inform employees to change passwords',
        'description': 'Notify employees to change any shared or related passwords.',
        'category': 'it', 'assigned_to': 'System Admin', 'assigned_email': sysadmin_email,
        'due_date': sysadmin_due,
        '_depends_on_title': 'Change Domain Admin password',
    })

    # ── Technical Services Manager Tasks ────────────────────────────────────
    property_parent = {
        'title': 'Client property in possession of exiting employee',
        'description': 'Collect all client property items from the departing employee.',
        'category': 'facilities', 'assigned_to': 'Technical Services Manager',
        'due_date': last_day,
        'subtasks': [
            {'title': 'Collect: Keys', 'description': 'Retrieve all keys in the employee\'s possession.', 'category': 'facilities', 'assigned_to': 'Technical Services Manager', 'assigned_email': facilities_email, 'due_date': last_day},
            {'title': 'Collect: Pass keys', 'description': 'Retrieve all pass keys.', 'category': 'facilities', 'assigned_to': 'Technical Services Manager', 'assigned_email': facilities_email, 'due_date': last_day},
            {'title': 'Collect: Parking passes', 'description': 'Retrieve parking pass and revoke access.', 'category': 'facilities', 'assigned_to': 'Technical Services Manager', 'assigned_email': facilities_email, 'due_date': last_day},
            {'title': 'Collect: Passwords/combinations', 'description': 'Document and change any shared passwords or lock combinations the employee had access to.', 'category': 'facilities', 'assigned_to': 'Technical Services Manager', 'assigned_email': facilities_email, 'due_date': last_day, 'is_security_critical': True},
            {'title': 'Collect: Other items', 'description': 'Check for any additional company or client property.', 'category': 'facilities', 'assigned_to': 'Technical Services Manager', 'assigned_email': facilities_email, 'due_date': last_day},
        ],
    }
    all_tasks.append(property_parent)

    # ── CSM Tasks ───────────────────────────────────────────────────────────
    all_tasks.append({
        'title': 'Notify Account Executive of staff change',
        'description': 'Have the assigned Account Executive / CSM notify clients of the staffing change and introduce the replacement contact.',
        'category': 'manager', 'assigned_to': 'CSM', 'due_date': last_day,
    })

    # ── Final Account Disposition ───────────────────────────────────────────
    try:
        last_dt = datetime.strptime(last_day, '%Y-%m-%d').date()
        disposition_date = (last_dt + timedelta(days=1)).isoformat()
    except (ValueError, TypeError):
        disposition_date = last_day

    all_tasks.append({
        'title': 'Account disposition: Delete account OR Create alias/forward',
        'description': 'Decision required: (A) Delete the email account entirely, OR (B) Create an alias on a colleague\'s account and set up forwarding for 1 month (create alias, forward for 2 months). Manager makes the call.',
        'category': 'it', 'assigned_to': 'Manager', 'assigned_email': mgr_email,
        'due_date': disposition_date,
    })

    # Filter by conditions
    filtered = []
    for task in all_tasks:
        if _check_condition(task.get('conditions'), employee):
            if 'subtasks' in task:
                task['subtasks'] = [st for st in task['subtasks'] if _check_condition(st.get('conditions'), employee)]
            filtered.append(task)
    return filtered


# ── Employees ────────────────────────────────────────────────────────────────

@mgr_bp.route('/employees', methods=['GET'])
@login_required
def list_employees():
    """List employees with optional filters."""
    user = get_current_user()
    db = get_db()
    try:
        status = request.args.get('status')
        department = request.args.get('department')
        search = request.args.get('search')
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 25, type=int)
        per_page = min(per_page, 100)
        offset = (page - 1) * per_page

        filters = []
        params = []

        if user['role'] == 'manager':
            filters.append('e.manager_email = ?')
            params.append(user['email'])
        elif user['role'] == 'employee':
            filters.append('e.email = ?')
            params.append(user['email'])

        if status:
            filters.append('e.status = ?')
            params.append(status)
        if department:
            filters.append('e.department = ?')
            params.append(department)
        if search:
            filters.append("(e.first_name || ' ' || e.last_name LIKE ? OR e.email LIKE ?)")
            params.extend([f'%{search}%', f'%{search}%'])

        where = ''
        if filters:
            where = 'WHERE ' + ' AND '.join(filters)

        total = db.execute(f'SELECT COUNT(*) as cnt FROM employees e {where}', params).fetchone()['cnt']

        query_params = params + [per_page, offset]
        rows = db.execute(
            f'SELECT e.*, '
            f'(SELECT COUNT(*) FROM tasks t JOIN checklists c ON t.checklist_id = c.id '
            f'WHERE c.employee_id = e.id AND c.status = "active") as total_tasks, '
            f'(SELECT COUNT(*) FROM tasks t JOIN checklists c ON t.checklist_id = c.id '
            f'WHERE c.employee_id = e.id AND c.status = "active" AND t.status = "completed") as completed_tasks '
            f'FROM employees e {where} ORDER BY e.created_at DESC LIMIT ? OFFSET ?',
            query_params
        ).fetchall()

        employees = []
        for r in rows:
            emp = dict(r)
            total_t = emp.pop('total_tasks', 0)
            completed_t = emp.pop('completed_tasks', 0)
            emp['progress'] = round((completed_t / total_t * 100) if total_t > 0 else 0, 1)
            employees.append(emp)

        return jsonify({
            'employees': employees,
            'page': page,
            'per_page': per_page,
            'total': total,
            'pages': (total + per_page - 1) // per_page
        })
    finally:
        db.close()


@mgr_bp.route('/employees/<int:emp_id>', methods=['GET'])
@login_required
def get_employee(emp_id):
    """Get employee detail with checklists, tasks (nested subtasks), equipment, milestones."""
    user = get_current_user()
    db = get_db()
    try:
        emp = db.execute('SELECT * FROM employees WHERE id = ?', (emp_id,)).fetchone()
        if not emp:
            return jsonify({'error': 'Employee not found'}), 404
        if not _can_access_employee(user, emp):
            return jsonify({'error': 'Access denied'}), 403

        emp_dict = dict(emp)

        # Checklists with tasks (nested)
        checklists = db.execute(
            'SELECT * FROM checklists WHERE employee_id = ? ORDER BY created_at DESC', (emp_id,)
        ).fetchall()

        checklists_data = []
        for cl in checklists:
            cl_dict = dict(cl)
            tasks = db.execute(
                'SELECT * FROM tasks WHERE checklist_id = ? ORDER BY sort_order, id', (cl['id'],)
            ).fetchall()

            # Organize tasks with subtasks
            task_map = {}
            top_level = []
            for t in tasks:
                td = dict(t)
                td['subtasks'] = []
                task_map[td['id']] = td

            for t in tasks:
                td = task_map[t['id']]
                if td.get('parent_task_id') and td['parent_task_id'] in task_map:
                    task_map[td['parent_task_id']]['subtasks'].append(td)
                else:
                    top_level.append(td)

            cl_dict['tasks'] = top_level

            all_tasks = [dict(t) for t in tasks]
            total = len(all_tasks)
            completed = sum(1 for t in all_tasks if t['status'] == 'completed')
            cl_dict['progress'] = round((completed / total * 100) if total > 0 else 0, 1)

            # Phase progress
            phases = {}
            for t in all_tasks:
                p = t.get('phase') or 'other'
                if p not in phases:
                    phases[p] = {'total': 0, 'completed': 0}
                phases[p]['total'] += 1
                if t['status'] == 'completed':
                    phases[p]['completed'] += 1
            for p in phases:
                phases[p]['progress'] = round(
                    (phases[p]['completed'] / phases[p]['total'] * 100) if phases[p]['total'] > 0 else 0, 1
                )
            cl_dict['phase_progress'] = phases

            # Employee-specific progress (tasks the employee can act on)
            emp_email = emp_dict.get('email', '')
            my_tasks_list = [t for t in all_tasks
                             if t.get('is_acknowledgment')
                             or t.get('category') == 'employee'
                             or (t.get('assigned_email') and t['assigned_email'] == emp_email)]
            my_t = len(my_tasks_list)
            my_d = sum(1 for t in my_tasks_list if t['status'] == 'completed')
            cl_dict['my_progress'] = round((my_d / my_t * 100) if my_t > 0 else 0, 1)
            cl_dict['my_total'] = my_t
            cl_dict['my_completed'] = my_d

            checklists_data.append(cl_dict)

        emp_dict['checklists'] = checklists_data

        # Equipment
        equipment = db.execute(
            'SELECT * FROM equipment WHERE employee_id = ? ORDER BY created_at', (emp_id,)
        ).fetchall()
        emp_dict['equipment'] = [dict(e) for e in equipment]

        # Milestones
        milestones = db.execute(
            'SELECT * FROM milestones WHERE employee_id = ? ORDER BY day_marker', (emp_id,)
        ).fetchall()
        emp_dict['milestones'] = [dict(m) for m in milestones]

        # Buddy tasks
        buddy_tasks = db.execute(
            'SELECT * FROM buddy_tasks WHERE employee_id = ? ORDER BY due_offset_days', (emp_id,)
        ).fetchall()
        emp_dict['buddy_tasks'] = [dict(bt) for bt in buddy_tasks]

        # Meet and greets
        meet_greets = db.execute(
            'SELECT * FROM meet_greets WHERE employee_id = ? ORDER BY week_number, id', (emp_id,)
        ).fetchall()
        emp_dict['meet_greets'] = [dict(mg) for mg in meet_greets]

        return jsonify(emp_dict)
    finally:
        db.close()


@mgr_bp.route('/employees/bulk', methods=['POST'])
@login_required
@role_required(['admin', 'hr'])
def bulk_create_employees():
    """Create multiple employees at once from a JSON array.
    Body: {employees: [{first_name, last_name, email, ...}, ...], auto_onboard: false}
    """
    data = request.get_json(silent=True)
    if not data or not data.get('employees'):
        return jsonify({'error': 'No employees provided'}), 400

    employees_data = data['employees']
    if len(employees_data) > 50:
        return jsonify({'error': 'Maximum 50 employees per batch'}), 400

    auto_onboard = data.get('auto_onboard', False)
    user = get_current_user()
    results = []
    db = get_db()
    try:
        for emp_data in employees_data:
            if not emp_data.get('first_name') or not emp_data.get('last_name') or not emp_data.get('email'):
                results.append({'email': emp_data.get('email', ''), 'status': 'error',
                                'message': 'Missing required fields (first_name, last_name, email)'})
                continue

            email = emp_data['email'].strip().lower()
            existing = db.execute('SELECT id FROM employees WHERE email = ?', (email,)).fetchone()
            if existing:
                results.append({'email': email, 'status': 'skipped',
                                'message': 'Employee already exists', 'id': existing['id']})
                continue

            cohort_id = emp_data.get('cohort_id')
            start_date = emp_data.get('start_date', date.today().isoformat())
            if not cohort_id:
                try:
                    cohort_id = get_or_assign_cohort(start_date)['id']
                except Exception:
                    cohort_id = None

            db.execute(
                'INSERT INTO employees (first_name, last_name, email, phone, role_title, department, '
                'manager_email, start_date, location, status, hire_type, created_at, created_by, '
                'buddy_email, hr_owner_email, cohort_id, location_mode, shipping_address) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    emp_data.get('first_name', '').strip(),
                    emp_data.get('last_name', '').strip(),
                    email,
                    (emp_data.get('phone') or '').strip(),
                    (emp_data.get('role_title') or '').strip(),
                    (emp_data.get('department') or '').strip(),
                    (emp_data.get('manager_email') or '').strip().lower(),
                    start_date,
                    (emp_data.get('location') or '').strip(),
                    'pending',
                    (emp_data.get('hire_type') or 'full-time').strip(),
                    datetime.utcnow().isoformat(),
                    user['email'],
                    (emp_data.get('buddy_email') or '').strip().lower() or None,
                    (emp_data.get('hr_owner_email') or user['email']).strip().lower(),
                    cohort_id,
                    emp_data.get('location_mode', 'in-office'),
                    (emp_data.get('shipping_address') or '').strip() or None,
                )
            )
            new_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
            results.append({'email': email, 'status': 'created', 'id': new_id})

        db.commit()

        created_count = len([r for r in results if r['status'] == 'created'])
        _audit('bulk_create_employees', 'employee', None, {'count': created_count})

        return jsonify({
            'results': results,
            'created': created_count,
            'skipped': len([r for r in results if r['status'] == 'skipped']),
            'errors': len([r for r in results if r['status'] == 'error']),
        }), 201
    finally:
        db.close()


@mgr_bp.route('/employees', methods=['POST'])
@login_required
@role_required(['admin', 'hr'])
def create_employee():
    """Create a new employee record with enhanced fields."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Request body required'}), 400

    required = ['first_name', 'last_name', 'email', 'role_title', 'department', 'start_date']
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({'error': f'Missing required fields: {", ".join(missing)}'}), 400

    user = get_current_user()
    db = get_db()
    try:
        email = data['email'].strip().lower()
        existing = db.execute('SELECT id FROM employees WHERE email = ?', (email,)).fetchone()
        if existing:
            return jsonify({'error': 'An employee with this email already exists'}), 409

        # Auto-assign cohort
        cohort_id = data.get('cohort_id')
        if not cohort_id:
            cohort_id = get_or_assign_cohort(data['start_date'])['id']

        db.execute(
            'INSERT INTO employees (first_name, last_name, email, phone, role_title, department, '
            'manager_email, start_date, location, status, hire_type, created_at, created_by, '
            'buddy_email, hr_owner_email, cohort_id, location_mode, is_rehire, previous_end_date) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                data['first_name'].strip(),
                data['last_name'].strip(),
                email,
                (data.get('phone') or '').strip(),
                data['role_title'].strip(),
                data['department'].strip(),
                (data.get('manager_email') or '').strip().lower(),
                data['start_date'],
                (data.get('location') or '').strip(),
                'pending',
                (data.get('hire_type') or 'full-time').strip(),
                datetime.utcnow().isoformat(),
                user['email'],
                (data.get('buddy_email') or '').strip().lower() or None,
                (data.get('hr_owner_email') or user['email']).strip().lower(),
                cohort_id,
                data.get('location_mode', 'in-office'),
                1 if data.get('is_rehire') else 0,
                (data.get('previous_end_date') or '').strip(),
            )
        )
        db.commit()

        emp = db.execute('SELECT * FROM employees WHERE email = ?', (email,)).fetchone()
        _audit('employee_created', 'employee', emp['id'],
               {'name': f"{data['first_name']} {data['last_name']}", 'email': email})

        return jsonify(dict(emp)), 201
    finally:
        db.close()


@mgr_bp.route('/employees/<int:emp_id>', methods=['PUT'])
@login_required
@role_required(['admin', 'hr'])
def update_employee(emp_id):
    """Update an employee record."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Request body required'}), 400

    db = get_db()
    try:
        emp = db.execute('SELECT * FROM employees WHERE id = ?', (emp_id,)).fetchone()
        if not emp:
            return jsonify({'error': 'Employee not found'}), 404

        allowed_fields = [
            'first_name', 'last_name', 'email', 'phone', 'role_title', 'department',
            'manager_email', 'start_date', 'end_date', 'location', 'status', 'hire_type',
            'buddy_email', 'hr_owner_email', 'location_mode', 'cohort_id',
            'is_rehire', 'previous_end_date',
        ]
        updates = []
        params = []
        changes = {}
        for field in allowed_fields:
            if field in data:
                updates.append(f'{field} = ?')
                val = data[field].strip() if isinstance(data[field], str) else data[field]
                params.append(val)
                changes[field] = {'from': emp[field], 'to': val}

        if not updates:
            return jsonify({'error': 'No valid fields to update'}), 400

        params.append(emp_id)
        db.execute(f'UPDATE employees SET {", ".join(updates)} WHERE id = ?', params)
        db.commit()

        # If buddy was assigned, generate buddy tasks and notify via Slack
        if 'buddy_email' in data and data['buddy_email']:
            new_buddy = data['buddy_email'].strip().lower()
            old_buddy = (emp['buddy_email'] or '').strip().lower()
            existing_buddy = db.execute(
                'SELECT id FROM buddy_tasks WHERE employee_id = ?', (emp_id,)
            ).fetchone()
            if not existing_buddy:
                _generate_buddy_tasks(db, emp_id, new_buddy, emp['start_date'])
                db.commit()

            # Slack DM to newly assigned buddy
            if new_buddy and new_buddy != old_buddy:
                try:
                    from .slack_client import SlackClient
                    slack = SlackClient()
                    emp_name = f"{emp['first_name']} {emp['last_name']}"
                    start = emp['start_date'] or 'TBD'
                    slack.send_dm(
                        new_buddy,
                        f"👋 You've been assigned as a buddy for *{emp_name}* starting on {start}!\n\n"
                        f"As their buddy, you'll help them navigate their first month at Celito. "
                        f"Check the onboarding portal for your buddy tasks: introductions, coffee chats, and weekly check-ins.\n\n"
                        f"Please reach out to them before their start date to introduce yourself!"
                    )
                except Exception as e:
                    logger.warning(f"Failed to notify buddy via Slack: {e}")

        _audit('employee_updated', 'employee', emp_id, changes)

        updated = db.execute('SELECT * FROM employees WHERE id = ?', (emp_id,)).fetchone()
        return jsonify(dict(updated))
    finally:
        db.close()


# ── Onboarding ───────────────────────────────────────────────────────────────

@mgr_bp.route('/employees/<int:emp_id>/onboard/preview', methods=['GET', 'POST'])
@login_required
@role_required(['admin', 'hr', 'manager'])
def preview_onboarding(emp_id):
    """Preview the tasks that would be generated for this employee's onboarding.

    Returns the full task list without inserting anything into the database.
    HR can review, then POST to /onboard to actually create them.
    """
    db = get_db()
    try:
        emp = db.execute('SELECT * FROM employees WHERE id = ?', (emp_id,)).fetchone()
        if not emp:
            return jsonify({'error': 'Employee not found'}), 404

        emp_dict = dict(emp)
        start_date = emp['start_date'] or date.today().isoformat()

        # Check for custom templates first (same logic as start_onboarding)
        templates = db.execute(
            "SELECT * FROM checklist_templates WHERE checklist_type = 'onboarding' AND is_active = 1 "
            "AND (department IS NULL OR department = ?) ORDER BY department DESC",
            (emp['department'],)
        ).fetchall()

        preview_tasks = []
        if templates:
            sort_order = 0
            for tmpl in templates:
                try:
                    tmpl_tasks = json.loads(tmpl['tasks_json'])
                except (json.JSONDecodeError, TypeError):
                    continue
                for task_def in tmpl_tasks:
                    if _check_condition(task_def.get('conditions'), emp_dict):
                        phase = task_def.get('phase', tmpl['phase'] or 'company_onboarding')
                        due = _offset_date(start_date, task_def.get('due_offset_days', 0))
                        preview_tasks.append({
                            'title': task_def.get('title', ''),
                            'description': task_def.get('description', ''),
                            'category': task_def.get('category', 'hr'),
                            'phase': phase,
                            'assigned_to': task_def.get('assigned_to', ''),
                            'due_date': due,
                            'is_acknowledgment': task_def.get('is_acknowledgment', False),
                            'is_security_critical': task_def.get('is_security_critical', False),
                            'location_mode': task_def.get('location_mode', 'all'),
                            'sort_order': sort_order,
                            'source': 'template',
                            'template_name': tmpl['name'],
                        })
                        sort_order += 1
            source = 'templates'
        else:
            tasks = _get_onboarding_tasks(emp_dict)
            sort_order = 0
            for task_def in tasks:
                subtasks = task_def.get('subtasks', [])
                due = _offset_date(start_date, task_def.get('due_offset_days', 0))
                preview_tasks.append({
                    'title': task_def.get('title', ''),
                    'description': task_def.get('description', ''),
                    'category': task_def.get('category', 'hr'),
                    'phase': task_def.get('phase', 'company_onboarding'),
                    'assigned_to': task_def.get('assigned_to', ''),
                    'due_date': due,
                    'is_acknowledgment': task_def.get('is_acknowledgment', False),
                    'is_security_critical': task_def.get('is_security_critical', False),
                    'location_mode': task_def.get('location_mode', 'all'),
                    'sort_order': sort_order,
                    'source': 'built-in',
                    'has_subtasks': len(subtasks) > 0,
                    'subtask_count': len(subtasks),
                })
                sort_order += 1
                for st in subtasks:
                    st_due = _offset_date(start_date, st.get('due_offset_days', 0))
                    preview_tasks.append({
                        'title': st.get('title', ''),
                        'description': st.get('description', ''),
                        'category': st.get('category', task_def.get('category', 'hr')),
                        'phase': st.get('phase', task_def.get('phase', 'company_onboarding')),
                        'assigned_to': st.get('assigned_to', ''),
                        'due_date': st_due,
                        'is_acknowledgment': st.get('is_acknowledgment', False),
                        'is_security_critical': st.get('is_security_critical', False),
                        'location_mode': st.get('location_mode', 'all'),
                        'sort_order': sort_order,
                        'source': 'built-in',
                        'is_subtask': True,
                    })
                    sort_order += 1
            source = 'built-in'

        # Group by phase for the frontend
        by_phase = {}
        for t in preview_tasks:
            phase = t.get('phase', 'company_onboarding')
            if phase not in by_phase:
                by_phase[phase] = []
            by_phase[phase].append(t)

        # Group by category for stats
        by_category = {}
        for t in preview_tasks:
            cat = t.get('category', 'other')
            by_category[cat] = by_category.get(cat, 0) + 1

        # Counts per phase
        phase_counts = {phase: len(tasks_list) for phase, tasks_list in by_phase.items()}

        return jsonify({
            'employee': {
                'id': emp_id,
                'name': f"{emp['first_name']} {emp['last_name']}",
                'department': emp['department'],
                'location_mode': emp['location_mode'],
                'start_date': emp['start_date'],
            },
            'source': source,
            'total_tasks': len(preview_tasks),
            'phase_counts': phase_counts,
            'tasks': preview_tasks,
            'tasks_by_phase': by_phase,
            'tasks_by_category': by_category,
        })
    finally:
        db.close()


@mgr_bp.route('/employees/<int:emp_id>/onboard', methods=['POST'])
@login_required
@role_required(['admin', 'hr'])
def start_onboarding(emp_id):
    """Start the phased onboarding process for an employee."""
    db = get_db()
    try:
        emp = db.execute('SELECT * FROM employees WHERE id = ?', (emp_id,)).fetchone()
        if not emp:
            return jsonify({'error': 'Employee not found'}), 404

        existing = db.execute(
            "SELECT id FROM checklists WHERE employee_id = ? AND checklist_type = 'onboarding' AND status = 'active'",
            (emp_id,)
        ).fetchone()
        if existing:
            return jsonify({'error': 'Employee already has an active onboarding checklist'}), 409

        emp_dict = dict(emp)

        # Create checklist
        db.execute(
            'INSERT INTO checklists (employee_id, name, checklist_type, status, created_at) VALUES (?, ?, ?, ?, ?)',
            (emp_id, f"Onboarding: {emp['first_name']} {emp['last_name']}", 'onboarding', 'active',
             datetime.utcnow().isoformat())
        )
        checklist_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]

        start_date = emp['start_date'] or date.today().isoformat()

        # Check for custom templates first
        templates = db.execute(
            "SELECT * FROM checklist_templates WHERE checklist_type = 'onboarding' AND is_active = 1 "
            "AND (department IS NULL OR department = ?) ORDER BY department DESC",
            (emp['department'],)
        ).fetchall()

        if templates:
            # Use template-based tasks
            sort_order = 0
            for tmpl in templates:
                try:
                    tmpl_tasks = json.loads(tmpl['tasks_json'])
                except (json.JSONDecodeError, TypeError):
                    continue
                for task_def in tmpl_tasks:
                    if _check_condition(task_def.get('conditions'), emp_dict):
                        task_def.setdefault('phase', tmpl['phase'] or 'company_onboarding')
                        _insert_task(db, checklist_id, emp_id, task_def, start_date, sort_order)
                        sort_order += 1
        else:
            # Use the built-in phased task list
            tasks = _get_onboarding_tasks(emp_dict)
            sort_order = 0
            title_to_id = {}  # for resolving dependencies

            for task_def in tasks:
                # Resolve dependency by title
                depends_id = None
                dep_title = task_def.pop('_depends_on_title', None)
                if dep_title and dep_title in title_to_id:
                    depends_id = title_to_id[dep_title]

                subtasks = task_def.pop('subtasks', [])

                task_id = _insert_task(db, checklist_id, emp_id, task_def, start_date, sort_order, depends_on_task_id=depends_id)
                title_to_id[task_def['title']] = task_id
                sort_order += 1

                # Insert subtasks
                for st in subtasks:
                    st_dep_title = st.pop('_depends_on_title', None)
                    st_dep_id = title_to_id.get(st_dep_title) if st_dep_title else None
                    st_id = _insert_task(db, checklist_id, emp_id, st, start_date, sort_order,
                                         parent_task_id=task_id, depends_on_task_id=st_dep_id)
                    title_to_id[st['title']] = st_id
                    sort_order += 1

        # ── Rehire shortcut: auto-skip pre_boarding + company_onboarding tasks ──
        if emp.get('is_rehire'):
            skip_phases = ('pre_boarding', 'company_onboarding')
            now_ts = datetime.utcnow().isoformat()
            db.execute(
                "UPDATE tasks SET status = 'skipped', completed_at = ?, completed_by = 'system-rehire' "
                "WHERE checklist_id = ? AND phase IN (?, ?) AND status NOT IN ('completed', 'skipped')",
                (now_ts, checklist_id, *skip_phases)
            )
            # Unblock tasks that depended on any auto-skipped task
            db.execute(
                "UPDATE tasks SET status = 'pending' "
                "WHERE checklist_id = ? AND status = 'blocked' AND depends_on_task_id IN ("
                "  SELECT id FROM tasks WHERE checklist_id = ? AND status = 'skipped' AND completed_by = 'system-rehire'"
                ")", (checklist_id, checklist_id)
            )

        # Auto-populate meet_greets table (the primary tracking system)
        # Resolve real contact emails from employee record + team config
        _mgr_email = emp.get('manager_email', '')
        _hr_email = emp.get('hr_owner_email', '') or config.get('team_assignments.hr_email', '')
        _buddy_email = emp.get('buddy_email', '')
        _sysadmin_email = config.get('team_assignments.sysadmin_email', '')
        _servicedesk_email = config.get('team_assignments.servicedesk_email', '')
        _voice_email = config.get('team_assignments.voice_dept_email', '')
        _facilities_email = config.get('team_assignments.facilities_email', '')

        default_meets = [
            {'contact_name': 'CEO / Founder', 'contact_email': '', 'contact_role': 'Executive', 'reason': 'Understand company vision, mission, and strategy', 'week_number': 1},
            {'contact_name': 'Your Manager', 'contact_email': _mgr_email, 'contact_role': 'Manager', 'reason': 'Your direct manager — goals, expectations, and support', 'week_number': 1},
            {'contact_name': 'HR Lead', 'contact_email': _hr_email, 'contact_role': 'HR', 'reason': 'Your benefits, policies, and support resource', 'week_number': 1},
            {'contact_name': 'IT Lead', 'contact_email': _sysadmin_email, 'contact_role': 'IT', 'reason': 'Your technology setup and ongoing support contact', 'week_number': 1},
            {'contact_name': 'Sales Leader', 'contact_email': '', 'contact_role': 'Sales', 'reason': 'Understand how Celito wins and retains customers', 'week_number': 2},
            {'contact_name': 'Operations Lead', 'contact_email': '', 'contact_role': 'Operations', 'reason': 'How delivery and operations work day-to-day', 'week_number': 2},
            {'contact_name': 'Finance Lead', 'contact_email': '', 'contact_role': 'Finance', 'reason': 'Expense reports, purchasing, and reimbursements', 'week_number': 2},
            {'contact_name': 'Customer Success Lead', 'contact_email': '', 'contact_role': 'CSM', 'reason': 'How we support and grow client relationships', 'week_number': 3},
            {'contact_name': 'Engineering Lead', 'contact_email': '', 'contact_role': 'Engineering', 'reason': 'Technical capabilities and project delivery', 'week_number': 3},
        ]
        # Add buddy meet-and-greet if assigned
        if _buddy_email:
            default_meets.insert(2, {
                'contact_name': 'Your Onboarding Buddy',
                'contact_email': _buddy_email,
                'contact_role': 'Buddy',
                'reason': 'Your go-to person for questions, culture tips, and getting settled',
                'week_number': 1,
            })
        # Compute timezone-aware suggested meeting windows
        emp_tz = emp.get('timezone', 'America/Los_Angeles')
        for mg in default_meets:
            suggested_time = ''
            contact_email = mg.get('contact_email', '')
            if contact_email:
                # Look up the contact's timezone
                contact_tz_row = db.execute(
                    'SELECT timezone FROM users WHERE email = ?', (contact_email,)
                ).fetchone()
                contact_tz = contact_tz_row['timezone'] if contact_tz_row and contact_tz_row['timezone'] else 'America/Los_Angeles'
                try:
                    _, _, suggested_time = _get_overlap_window(emp_tz, contact_tz, date.today())
                except Exception:
                    suggested_time = ''
            db.execute(
                'INSERT INTO meet_greets (employee_id, contact_name, contact_email, contact_role, reason, week_number, suggested_time) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (emp_id, mg['contact_name'], contact_email, mg['contact_role'], mg['reason'], mg['week_number'], suggested_time)
            )

        # Generate milestones
        _generate_milestones(db, emp_id, emp['role_title'], emp['department'], emp.get('hire_type', 'full-time'))

        # Generate buddy tasks if buddy assigned
        if emp.get('buddy_email'):
            _generate_buddy_tasks(db, emp_id, emp['buddy_email'], start_date)

        # Auto-schedule pulse surveys at day 7, 30, 90
        try:
            start_dt = datetime.strptime(start_date, '%Y-%m-%d')
        except (ValueError, TypeError):
            start_dt = datetime.utcnow()
        for survey_type, offset in [('day_7', 7), ('day_30', 30), ('day_90', 90)]:
            send_date = (start_dt + timedelta(days=offset)).strftime('%Y-%m-%d')
            db.execute(
                "INSERT INTO surveys (employee_id, survey_type, status, sent_at) VALUES (?, ?, 'scheduled', ?)",
                (emp_id, survey_type, send_date)
            )

        # Update employee status
        db.execute("UPDATE employees SET status = 'active' WHERE id = ?", (emp_id,))
        db.commit()

        _audit('onboarding_started', 'employee', emp_id,
               {'checklist_id': checklist_id, 'name': f"{emp['first_name']} {emp['last_name']}"})

        # Notify the new hire via Slack
        employee_email = emp.get('email', '')
        if employee_email:
            try:
                from .slack_client import SlackClient
                _slack = SlackClient()
                _slack.send_dm(
                    employee_email,
                    f"🎉 Welcome to Celito, {emp['first_name']}! Your onboarding has been set up.\n"
                    f"Visit https://onboard.celito.net to view your tasks and get started.\n"
                    f"Your buddy and team will reach out shortly!",
                )
            except Exception:
                pass  # Slack is best-effort

        # Return result
        checklist = db.execute('SELECT * FROM checklists WHERE id = ?', (checklist_id,)).fetchone()
        tasks_rows = db.execute('SELECT * FROM tasks WHERE checklist_id = ? ORDER BY sort_order', (checklist_id,)).fetchall()

        result = dict(checklist)
        result['tasks'] = [dict(t) for t in tasks_rows]
        result['task_count'] = len(result['tasks'])
        return jsonify(result), 201
    finally:
        db.close()


@mgr_bp.route('/employees/<int:emp_id>/checklist', methods=['GET'])
@login_required
def get_checklist(emp_id):
    """Get checklist with all tasks for an employee, organized by phase."""
    user = get_current_user()
    db = get_db()
    try:
        emp = db.execute('SELECT * FROM employees WHERE id = ?', (emp_id,)).fetchone()
        if not emp:
            return jsonify({'error': 'Employee not found'}), 404
        if not _can_access_employee(user, emp):
            return jsonify({'error': 'Access denied'}), 403

        checklist_type = request.args.get('type')
        query = 'SELECT * FROM checklists WHERE employee_id = ?'
        params = [emp_id]
        if checklist_type:
            query += ' AND checklist_type = ?'
            params.append(checklist_type)
        query += ' ORDER BY created_at DESC'

        checklists = db.execute(query, params).fetchall()

        result = []
        for cl in checklists:
            cl_dict = dict(cl)
            tasks = db.execute(
                'SELECT * FROM tasks WHERE checklist_id = ? ORDER BY sort_order, id', (cl['id'],)
            ).fetchall()

            # Organize by phase
            by_phase = {}
            all_task_dicts = []
            for t in tasks:
                td = dict(t)
                all_task_dicts.append(td)
                p = td.get('phase') or 'other'
                if p not in by_phase:
                    by_phase[p] = []
                by_phase[p].append(td)

            cl_dict['tasks'] = all_task_dicts
            cl_dict['tasks_by_phase'] = by_phase

            total = len(all_task_dicts)
            completed = sum(1 for t in all_task_dicts if t['status'] == 'completed')
            cl_dict['progress'] = round((completed / total * 100) if total > 0 else 0, 1)
            result.append(cl_dict)

        return jsonify(result)
    finally:
        db.close()


@mgr_bp.route('/employees/<int:emp_id>/generate-checklist', methods=['POST'])
@login_required
@role_required(['admin', 'hr'])
def generate_checklist(emp_id):
    """Use Claude API to generate a customized onboarding checklist."""
    db = get_db()
    try:
        emp = db.execute('SELECT * FROM employees WHERE id = ?', (emp_id,)).fetchone()
        if not emp:
            return jsonify({'error': 'Employee not found'}), 404

        api_key = config.get('anthropic.api_key')
        model = config.get('anthropic.model', 'claude-sonnet-4-6')

        if not api_key:
            return jsonify({'error': 'Anthropic API key not configured'}), 500

        prompt = (
            f"Generate a detailed, phased onboarding checklist for a new employee:\n"
            f"- Name: {emp['first_name']} {emp['last_name']}\n"
            f"- Role: {emp['role_title']}\n"
            f"- Department: {emp['department']}\n"
            f"- Location: {emp['location'] or 'Not specified'}\n"
            f"- Location Mode: {emp.get('location_mode', 'in-office')}\n"
            f"- Start Date: {emp['start_date']}\n"
            f"- Hire Type: {emp['hire_type'] or 'Full-time'}\n\n"
            f"Organize tasks into four phases:\n"
            f"1. pre_boarding (before Day 1, negative day offsets)\n"
            f"2. company_onboarding (Week 1, days 0-5)\n"
            f"3. department_onboarding (Weeks 1-4, days 5-28)\n"
            f"4. role_training (Days 1-90)\n\n"
            f"Return a JSON array of task objects. Each must have:\n"
            f"- \"title\": string\n"
            f"- \"description\": string\n"
            f"- \"category\": one of \"it\", \"hr\", \"manager\", \"facilities\", \"employee\", \"training\"\n"
            f"- \"phase\": one of the four phases above\n"
            f"- \"due_offset_days\": integer (relative to start date)\n"
            f"- \"is_acknowledgment\": boolean (true if employee self-completes)\n"
            f"- \"is_security_critical\": boolean\n\n"
            f"Tailor tasks to the role, department, and location mode. Include 25-40 tasks across all phases. "
            f"Return ONLY the JSON array."
        )

        resp = http_requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key': api_key,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            },
            json={
                'model': model,
                'max_tokens': 4096,
                'messages': [{'role': 'user', 'content': prompt}],
                'system': (
                    'You are an HR onboarding specialist at a managed IT services and telecom company. '
                    'Generate comprehensive, role-appropriate, phased onboarding checklists. '
                    'Always respond with valid JSON only.'
                ),
            },
            timeout=60
        )
        resp.raise_for_status()
        result = resp.json()

        content_text = ''
        for block in result.get('content', []):
            if block.get('type') == 'text':
                content_text += block['text']

        text = content_text.strip()
        if text.startswith('```'):
            lines = text.split('\n')
            text = '\n'.join(lines[1:])
            if text.endswith('```'):
                text = text[:-3]
            text = text.strip()

        tasks = json.loads(text)

        _audit('checklist_generated', 'employee', emp_id,
               {'task_count': len(tasks), 'model': model})

        return jsonify({'tasks': tasks, 'model': model})

    except http_requests.exceptions.RequestException as e:
        logger.error(f"Claude API request failed: {e}")
        return jsonify({'error': f'AI service request failed: {str(e)}'}), 502
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse Claude response as JSON: {e}")
        return jsonify({'error': 'Failed to parse AI-generated checklist. Try again.'}), 500
    except Exception as e:
        logger.error(f"Checklist generation failed: {e}")
        return jsonify({'error': f'Checklist generation failed: {str(e)}'}), 500
    finally:
        db.close()


# ── Offboarding ──────────────────────────────────────────────────────────────

@mgr_bp.route('/employees/<int:emp_id>/offboard/preview', methods=['GET', 'POST'])
@login_required
@role_required(['admin', 'hr'])
def preview_offboarding(emp_id):
    """Preview offboarding tasks before starting. Returns the full task list
    grouped by urgency and category without inserting anything into the DB."""
    db = get_db()
    try:
        emp = db.execute('SELECT * FROM employees WHERE id = ?', (emp_id,)).fetchone()
        if not emp:
            return jsonify({'error': 'Employee not found'}), 404

        emp_dict = dict(emp)

        # Accept params from query string (GET) or body (POST)
        if request.method == 'POST':
            data = request.get_json(silent=True) or {}
        else:
            data = request.args

        last_day = data.get('last_day', emp.get('end_date') or date.today().isoformat())
        term_type = data.get('type', 'voluntary')
        if term_type not in ('voluntary', 'involuntary'):
            term_type = 'voluntary'

        tasks = _get_offboarding_tasks(emp_dict, term_type, last_day)

        # Add dynamic equipment return subtasks
        equip_rows = db.execute(
            'SELECT * FROM equipment WHERE employee_id = ? AND returned_date IS NULL', (emp_id,)
        ).fetchall()

        preview_tasks = []
        security_critical_tasks = []
        immediate_tasks = []
        sort_order = 0

        for task_def in tasks:
            subtasks = task_def.get('subtasks', [])

            # Populate equipment subtasks dynamically
            if 'Return Celito equipment' in task_def.get('title', '') and equip_rows:
                for eq in equip_rows:
                    subtasks.append({
                        'title': f"Return: {eq['item_type']} — {eq['model'] or 'N/A'} (S/N: {eq['serial_number'] or 'N/A'})",
                        'description': f"Collect {eq['item_type']} from departing employee.",
                        'category': 'hr', 'assigned_to': 'HR',
                        'due_date': last_day,
                    })

            pt = {
                'title': task_def.get('title', ''),
                'description': task_def.get('description', ''),
                'category': task_def.get('category', 'hr'),
                'assigned_to': task_def.get('assigned_to', ''),
                'due_date': task_def.get('due_date', last_day),
                'is_security_critical': bool(task_def.get('is_security_critical')),
                'urgency': task_def.get('urgency', 'normal'),
                'sort_order': sort_order,
                'has_subtasks': len(subtasks) > 0,
                'subtask_count': len(subtasks),
            }
            preview_tasks.append(pt)
            if pt['is_security_critical']:
                security_critical_tasks.append(pt)
            if pt['urgency'] == 'immediate':
                immediate_tasks.append(pt)
            sort_order += 1

            for st in subtasks:
                spt = {
                    'title': st.get('title', ''),
                    'description': st.get('description', ''),
                    'category': st.get('category', task_def.get('category', 'hr')),
                    'assigned_to': st.get('assigned_to', ''),
                    'due_date': st.get('due_date', last_day),
                    'is_security_critical': bool(st.get('is_security_critical')),
                    'urgency': st.get('urgency', 'normal'),
                    'sort_order': sort_order,
                    'is_subtask': True,
                }
                preview_tasks.append(spt)
                if spt['is_security_critical']:
                    security_critical_tasks.append(spt)
                if spt['urgency'] == 'immediate':
                    immediate_tasks.append(spt)
                sort_order += 1

        # Group by category
        by_category = {}
        for t in preview_tasks:
            cat = t.get('category', 'other')
            by_category[cat] = by_category.get(cat, 0) + 1

        # Group by urgency
        by_urgency = {'immediate': 0, 'normal': 0}
        for t in preview_tasks:
            urg = t.get('urgency', 'normal')
            by_urgency[urg] = by_urgency.get(urg, 0) + 1

        return jsonify({
            'employee': {
                'id': emp_id,
                'name': f"{emp['first_name']} {emp['last_name']}",
                'department': emp['department'],
                'email': emp.get('email', ''),
            },
            'term_type': term_type,
            'last_day': last_day,
            'total_tasks': len(preview_tasks),
            'security_critical_count': len(security_critical_tasks),
            'immediate_count': len(immediate_tasks),
            'equipment_count': len(equip_rows),
            'tasks': preview_tasks,
            'security_critical_tasks': security_critical_tasks,
            'immediate_tasks': immediate_tasks,
            'tasks_by_category': by_category,
            'tasks_by_urgency': by_urgency,
        })
    finally:
        db.close()


@mgr_bp.route('/employees/<int:emp_id>/offboard', methods=['POST'])
@login_required
@role_required(['admin', 'hr'])
def start_offboarding(emp_id):
    """Start the offboarding process with real Celito checklist items."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Request body required'}), 400

    last_day = data.get('last_day')
    term_type = data.get('type', 'voluntary')
    reason = data.get('reason', '')

    if not last_day:
        return jsonify({'error': 'last_day is required'}), 400
    if term_type not in ('voluntary', 'involuntary'):
        return jsonify({'error': 'type must be "voluntary" or "involuntary"'}), 400

    db = get_db()
    try:
        emp = db.execute('SELECT * FROM employees WHERE id = ?', (emp_id,)).fetchone()
        if not emp:
            return jsonify({'error': 'Employee not found'}), 404

        existing = db.execute(
            "SELECT id FROM checklists WHERE employee_id = ? AND checklist_type = 'offboarding' AND status = 'active'",
            (emp_id,)
        ).fetchone()
        if existing:
            return jsonify({'error': 'Employee already has an active offboarding checklist'}), 409

        emp_dict = dict(emp)

        # Create checklist
        db.execute(
            'INSERT INTO checklists (employee_id, name, checklist_type, status, created_at) VALUES (?, ?, ?, ?, ?)',
            (emp_id, f"Offboarding: {emp['first_name']} {emp['last_name']}", 'offboarding', 'active',
             datetime.utcnow().isoformat())
        )
        checklist_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]

        # Get offboarding tasks
        tasks = _get_offboarding_tasks(emp_dict, term_type, last_day)

        # Populate equipment return subtasks from the employee's equipment records
        equip_rows = db.execute(
            'SELECT * FROM equipment WHERE employee_id = ? AND returned_date IS NULL', (emp_id,)
        ).fetchall()

        sort_order = 0
        title_to_id = {}

        for task_def in tasks:
            subtasks = task_def.pop('subtasks', [])
            dep_title = task_def.pop('_depends_on_title', None)
            depends_id = title_to_id.get(dep_title) if dep_title else None

            # For the equipment return parent, add sub-items from equipment table
            if 'Return Celito equipment' in task_def.get('title', '') and equip_rows:
                for eq in equip_rows:
                    subtasks.append({
                        'title': f"Return: {eq['item_type']} — {eq['model'] or 'N/A'} (S/N: {eq['serial_number'] or 'N/A'})",
                        'description': f"Collect {eq['item_type']} from departing employee. Serial: {eq['serial_number'] or 'unknown'}.",
                        'category': 'hr', 'assigned_to': 'HR', 'assigned_email': hr_email, 'due_date': last_day,
                    })

            # Use explicit due_date if set (offboarding tasks have pre-calculated dates)
            if 'due_date' in task_def:
                due = task_def['due_date']
                status = 'pending'
                if depends_id:
                    status = 'blocked'

                db.execute(
                    'INSERT INTO tasks (checklist_id, employee_id, title, description, category, '
                    'assigned_to, assigned_email, due_date, status, sort_order, parent_task_id, '
                    'depends_on_task_id, phase, is_acknowledgment, is_security_critical, urgency) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (
                        checklist_id, emp_id,
                        task_def.get('title', ''),
                        task_def.get('description', ''),
                        task_def.get('category', 'hr'),
                        task_def.get('assigned_to', ''),
                        task_def.get('assigned_email', ''),
                        due, status, sort_order,
                        None, depends_id,
                        'offboarding',
                        1 if task_def.get('is_acknowledgment') else 0,
                        1 if task_def.get('is_security_critical') else 0,
                        task_def.get('urgency', 'normal'),
                    )
                )
                parent_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
            else:
                parent_id = _insert_task(db, checklist_id, emp_id, task_def, last_day, sort_order, depends_on_task_id=depends_id)

            title_to_id[task_def['title']] = parent_id
            sort_order += 1

            # Insert subtasks
            for st in subtasks:
                st_due = st.get('due_date', last_day)
                db.execute(
                    'INSERT INTO tasks (checklist_id, employee_id, title, description, category, '
                    'assigned_to, assigned_email, due_date, status, sort_order, parent_task_id, '
                    'phase, is_security_critical, urgency) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (
                        checklist_id, emp_id,
                        st.get('title', ''),
                        st.get('description', ''),
                        st.get('category', 'hr'),
                        st.get('assigned_to', ''),
                        st.get('assigned_email', ''),
                        st_due, 'pending', sort_order,
                        parent_id,
                        'offboarding',
                        1 if st.get('is_security_critical') else 0,
                        st.get('urgency', 'normal'),
                    )
                )
                sort_order += 1

        # Update employee status
        db.execute("UPDATE employees SET status = 'offboarding', end_date = ? WHERE id = ?",
                   (last_day, emp_id))
        db.commit()

        _audit('offboarding_started', 'employee', emp_id, {
            'checklist_id': checklist_id,
            'name': f"{emp['first_name']} {emp['last_name']}",
            'type': term_type,
            'last_day': last_day,
            'reason': reason
        })

        checklist = db.execute('SELECT * FROM checklists WHERE id = ?', (checklist_id,)).fetchone()
        tasks_rows = db.execute('SELECT * FROM tasks WHERE checklist_id = ? ORDER BY sort_order',
                           (checklist_id,)).fetchall()

        result = dict(checklist)
        result['tasks'] = [dict(t) for t in tasks_rows]
        result['task_count'] = len(result['tasks'])
        return jsonify(result), 201
    finally:
        db.close()


# ── Tasks ────────────────────────────────────────────────────────────────────

@mgr_bp.route('/tasks', methods=['GET'])
@login_required
def list_tasks():
    """List tasks with filters including phase."""
    user = get_current_user()
    db = get_db()
    try:
        filters = []
        params = []

        if user['role'] == 'employee':
            filters.append('(t.assigned_email = ? OR t.assigned_email = "")')
            params.append(user['email'])
        elif user['role'] == 'manager':
            filters.append(
                '(t.assigned_email = ? OR t.employee_id IN '
                '(SELECT id FROM employees WHERE manager_email = ?))'
            )
            params.extend([user['email'], user['email']])

        status = request.args.get('status')
        if status:
            filters.append('t.status = ?')
            params.append(status)

        category = request.args.get('category')
        if category:
            filters.append('t.category = ?')
            params.append(category)

        phase = request.args.get('phase')
        if phase:
            filters.append('t.phase = ?')
            params.append(phase)

        overdue = request.args.get('overdue')
        if overdue == 'true':
            filters.append("t.status NOT IN ('completed', 'skipped') AND t.due_date < ?")
            params.append(date.today().isoformat())

        employee_id = request.args.get('employee_id', type=int)
        if employee_id:
            filters.append('t.employee_id = ?')
            params.append(employee_id)

        # Exclude subtasks from top-level listing by default
        if request.args.get('include_subtasks') != 'true':
            filters.append('t.parent_task_id IS NULL')

        where = ''
        if filters:
            where = 'WHERE ' + ' AND '.join(filters)

        rows = db.execute(
            f'SELECT t.*, e.first_name || " " || e.last_name as employee_name, '
            f'c.checklist_type, c.name as checklist_name '
            f'FROM tasks t '
            f'JOIN employees e ON t.employee_id = e.id '
            f'JOIN checklists c ON t.checklist_id = c.id '
            f'{where} ORDER BY t.due_date, t.sort_order',
            params
        ).fetchall()

        return jsonify([dict(r) for r in rows])
    finally:
        db.close()


@mgr_bp.route('/tasks/<int:task_id>', methods=['PUT'])
@login_required
def update_task(task_id):
    """Update task status. Handles dependencies and phase completion checks."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Request body required'}), 400

    user = get_current_user()
    db = get_db()
    try:
        task = db.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if not task:
            return jsonify({'error': 'Task not found'}), 404

        emp = db.execute('SELECT * FROM employees WHERE id = ?', (task['employee_id'],)).fetchone()
        if not _can_access_employee(user, emp):
            return jsonify({'error': 'Access denied'}), 403

        # Check dependency — can't complete if dependency not met
        if data.get('status') == 'completed' and task['depends_on_task_id']:
            dep = db.execute('SELECT status FROM tasks WHERE id = ?', (task['depends_on_task_id'],)).fetchone()
            if dep and dep['status'] not in ('completed', 'skipped'):
                return jsonify({'error': 'Cannot complete: dependency task is not yet completed'}), 409

        # Check multi-dependency (depends_on comma-separated IDs)
        if data.get('status') == 'completed' and task.get('depends_on'):
            dep_ids = [int(d.strip()) for d in (task['depends_on'] or '').split(',') if d.strip()]
            if dep_ids:
                placeholders = ','.join('?' * len(dep_ids))
                incomplete = db.execute(
                    f"SELECT id, title, status FROM tasks WHERE id IN ({placeholders}) "
                    f"AND status NOT IN ('completed', 'skipped')", dep_ids
                ).fetchall()
                if incomplete:
                    return jsonify({
                        'error': 'Dependencies not met',
                        'blocking_tasks': [{'id': t['id'], 'title': t['title'], 'status': t['status']} for t in incomplete]
                    }), 409

        updates = []
        params = []

        if 'status' in data:
            new_status = data['status']
            if new_status not in ('pending', 'in_progress', 'completed', 'skipped', 'blocked'):
                return jsonify({'error': 'Invalid status'}), 400
            updates.append('status = ?')
            params.append(new_status)
            if new_status == 'completed':
                updates.append('completed_at = ?')
                params.append(datetime.utcnow().isoformat())
                updates.append('completed_by = ?')
                params.append(user['email'])

        old_assignee = task['assigned_to'] or ''
        old_assignee_email = task['assigned_email'] or ''

        if 'assigned_email' in data:
            updates.append('assigned_email = ?')
            params.append(data['assigned_email'])

        if 'assigned_to' in data:
            updates.append('assigned_to = ?')
            params.append(data['assigned_to'])

        if 'due_date' in data:
            updates.append('due_date = ?')
            params.append(data['due_date'])

        if not updates:
            return jsonify({'error': 'No valid fields to update'}), 400

        params.append(task_id)
        db.execute(f'UPDATE tasks SET {", ".join(updates)} WHERE id = ?', params)

        # Add note as comment if provided
        if data.get('notes'):
            db.execute(
                'INSERT INTO task_comments (task_id, user_email, comment, created_at) VALUES (?, ?, ?, ?)',
                (task_id, user['email'], data['notes'], datetime.utcnow().isoformat())
            )

        # If task completed, unblock dependent tasks
        if data.get('status') in ('completed', 'skipped'):
            blocked = db.execute(
                "SELECT id FROM tasks WHERE depends_on_task_id = ? AND status = 'blocked'",
                (task_id,)
            ).fetchall()
            for b in blocked:
                db.execute("UPDATE tasks SET status = 'pending' WHERE id = ?", (b['id'],))

        # Check if all tasks in checklist are done
        checklist_id = task['checklist_id']
        remaining = db.execute(
            "SELECT COUNT(*) as cnt FROM tasks WHERE checklist_id = ? AND status NOT IN ('completed', 'skipped')",
            (checklist_id,)
        ).fetchone()['cnt']

        if remaining <= (1 if data.get('status') in ('completed', 'skipped') else 0):
            db.execute(
                "UPDATE checklists SET status = 'completed', completed_at = ? WHERE id = ?",
                (datetime.utcnow().isoformat(), checklist_id)
            )
            # If it's an onboarding checklist completion, check 90-day ramp
            cl = db.execute('SELECT checklist_type FROM checklists WHERE id = ?', (checklist_id,)).fetchone()
            if cl and cl['checklist_type'] == 'offboarding':
                db.execute("UPDATE employees SET status = 'departed' WHERE id = ?", (task['employee_id'],))

        db.commit()

        # Auto-sync: if an offboarding equipment-return task is completed, mark equipment as returned
        if data.get('status') == 'completed' and task['phase'] == 'offboarding':
            task_title = (task['title'] or '').lower()
            # Check if task title contains equipment-related keywords
            has_equipment_keyword = any(kw in task_title for kw in EQUIPMENT_KEYWORDS)
            if 'return' in task_title or has_equipment_keyword:
                _sync_equipment_task_status(
                    db, task['employee_id'], task['title'] or '', 'completed', source=user['email']
                )
                logger.info("Offboarding task %s completed - synced equipment returns for employee %s",
                            task_id, task['employee_id'])

        # Specific audit + Slack DM for reassignment or initial assignment
        new_assignee = data.get('assigned_to', '')
        new_assignee_email = data.get('assigned_email', '')
        if new_assignee and new_assignee != old_assignee:
            is_reassignment = bool(old_assignee)
            _audit('task_reassigned' if is_reassignment else 'task_assigned', 'task', task_id, {
                'task_title': task['title'],
                'from': old_assignee,
                'to': new_assignee,
                'employee_id': task['employee_id']
            })
            # Notify new assignee via Slack
            if new_assignee_email:
                try:
                    from .slack_client import SlackClient
                    slack = SlackClient()
                    emp_name = emp['first_name'] + ' ' + emp['last_name'] if emp else 'an employee'
                    if is_reassignment:
                        msg = (
                            f"📋 *Task reassigned to you*\n"
                            f"*Task:* {task['title']}\n"
                            f"*Employee:* {emp_name}\n"
                            f"*Due:* {task['due_date'] or 'No due date'}\n"
                            f"_Reassigned by {user['email']}_"
                        )
                    else:
                        msg = (
                            f"📋 *New task assigned to you*\n"
                            f"*Task:* {task['title']}\n"
                            f"*Employee:* {emp_name}\n"
                            f"*Due:* {task['due_date'] or 'No due date'}\n"
                            f"View it at https://onboard.celito.net/#tasks"
                        )
                    slack.send_dm(new_assignee_email, msg)
                except Exception:
                    pass  # Slack is best-effort
        else:
            _audit('task_updated', 'task', task_id, {
                'changes': {k: v for k, v in data.items() if k != 'notes'},
                'employee_id': task['employee_id']
            })

        updated = db.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
        return jsonify(dict(updated))
    finally:
        db.close()


@mgr_bp.route('/tasks/bulk', methods=['POST'])
@role_required('admin', 'hr', 'manager')
def bulk_update_tasks():
    """
    Batch update multiple tasks in one request.

    JSON body:
        task_ids: list[int]        – IDs to update
        action: str                – 'complete', 'skip', 'reassign', 'delete'
        assigned_email: str (opt)  – required for 'reassign'
    """
    user = get_current_user()
    data = request.get_json(force=True)
    task_ids = data.get("task_ids", [])
    action = data.get("action", "")

    if not task_ids or not isinstance(task_ids, list):
        return jsonify({"error": "task_ids must be a non-empty list"}), 400
    if action not in ("complete", "skip", "reassign", "delete"):
        return jsonify({"error": "action must be one of: complete, skip, reassign, delete"}), 400

    # Sanitize: ensure all IDs are integers
    try:
        task_ids = [int(tid) for tid in task_ids]
    except (ValueError, TypeError):
        return jsonify({"error": "task_ids must contain only integers"}), 400

    db = get_db()
    try:
        now = datetime.utcnow().isoformat()
        placeholders = ",".join("?" for _ in task_ids)

        if action == "complete":
            db.execute(
                f"UPDATE tasks SET status = 'completed', completed_at = ?, "
                f"completed_by = ? WHERE id IN ({placeholders}) AND status != 'completed'",
                [now, user["email"]] + task_ids,
            )
            # Unblock dependent tasks
            db.execute(
                f"UPDATE tasks SET status = 'pending' WHERE depends_on_task_id IN ({placeholders}) "
                f"AND status = 'blocked'",
                task_ids,
            )

        elif action == "skip":
            db.execute(
                f"UPDATE tasks SET status = 'skipped' "
                f"WHERE id IN ({placeholders}) AND status NOT IN ('completed', 'skipped')",
                task_ids,
            )
            # Unblock dependent tasks
            db.execute(
                f"UPDATE tasks SET status = 'pending' WHERE depends_on_task_id IN ({placeholders}) "
                f"AND status = 'blocked'",
                task_ids,
            )

        elif action == "reassign":
            assigned_email = data.get("assigned_email", "").strip().lower()
            if not assigned_email:
                return jsonify({"error": "assigned_email is required for reassign"}), 400
            db.execute(
                f"UPDATE tasks SET assigned_email = ?, assigned_to = ? "
                f"WHERE id IN ({placeholders})",
                [assigned_email, assigned_email] + task_ids,
            )
            # Notify new assignee via Slack
            try:
                from .slack_client import SlackClient
                slack = SlackClient()
                task_count = len(task_ids)
                slack.send_dm(
                    assigned_email,
                    f"📋 *{task_count} task{'s' if task_count != 1 else ''} reassigned to you*\n"
                    f"_Reassigned by {user.get('name', user['email'])}. "
                    f"Check the onboarding portal for details._",
                )
            except Exception:
                pass  # Slack is best-effort

        elif action == "delete":
            db.execute(
                f"DELETE FROM tasks WHERE id IN ({placeholders})",
                task_ids,
            )

        db.commit()
        updated = db.total_changes
        _audit(f"bulk_{action}", "tasks", None, {
            "task_ids": task_ids,
            "count": len(task_ids),
        })
    finally:
        db.close()

    return jsonify({"ok": True, "action": action, "updated": updated, "requested": len(task_ids)})


@mgr_bp.route('/tasks/<int:task_id>/comments', methods=['POST'])
@login_required
def add_task_comment(task_id):
    """Add a comment to a task."""
    data = request.get_json(silent=True)
    comment_text = (data or {}).get('comment', '').strip()
    if not comment_text:
        return jsonify({'error': 'Comment text is required'}), 400

    user = get_current_user()
    db = get_db()
    try:
        task = db.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if not task:
            return jsonify({'error': 'Task not found'}), 404

        db.execute(
            'INSERT INTO task_comments (task_id, user_email, comment, created_at) VALUES (?, ?, ?, ?)',
            (task_id, user['email'], comment_text, datetime.utcnow().isoformat())
        )
        db.commit()
        return jsonify({'message': 'Comment added'}), 201
    finally:
        db.close()


@mgr_bp.route('/tasks/<int:task_id>/comments', methods=['GET'])
@login_required
def get_task_comments(task_id):
    """Get comments for a task."""
    db = get_db()
    try:
        task = db.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if not task:
            return jsonify({'error': 'Task not found'}), 404

        comments = db.execute(
            'SELECT * FROM task_comments WHERE task_id = ? ORDER BY created_at', (task_id,)
        ).fetchall()
        return jsonify([dict(c) for c in comments])
    finally:
        db.close()


# ── Equipment ────────────────────────────────────────────────────────────────

@mgr_bp.route('/employees/<int:emp_id>/equipment', methods=['GET'])
@login_required
def list_equipment(emp_id):
    """List equipment for an employee."""
    user = get_current_user()
    db = get_db()
    try:
        emp = db.execute('SELECT * FROM employees WHERE id = ?', (emp_id,)).fetchone()
        if not emp:
            return jsonify({'error': 'Employee not found'}), 404
        if not _can_access_employee(user, emp):
            return jsonify({'error': 'Access denied'}), 403

        rows = db.execute(
            'SELECT * FROM equipment WHERE employee_id = ? ORDER BY created_at', (emp_id,)
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        db.close()


@mgr_bp.route('/employees/<int:emp_id>/equipment', methods=['POST'])
@login_required
@role_required(['admin', 'hr', 'manager'])
def add_equipment(emp_id):
    """Add an equipment item for an employee."""
    data = request.get_json(silent=True)
    if not data or not data.get('item_type'):
        return jsonify({'error': 'item_type is required'}), 400

    db = get_db()
    try:
        emp = db.execute('SELECT * FROM employees WHERE id = ?', (emp_id,)).fetchone()
        if not emp:
            return jsonify({'error': 'Employee not found'}), 404

        db.execute(
            'INSERT INTO equipment (employee_id, item_type, model, serial_number, notes, status, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (emp_id, data['item_type'], data.get('model'), data.get('serial_number'),
             data.get('notes'), data.get('status', 'pending'), datetime.utcnow().isoformat())
        )
        db.commit()
        eq_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]

        _audit('equipment_added', 'employee', emp_id,
               {'equipment_id': eq_id, 'item_type': data['item_type']})

        eq = db.execute('SELECT * FROM equipment WHERE id = ?', (eq_id,)).fetchone()
        return jsonify(dict(eq)), 201
    finally:
        db.close()


EQUIPMENT_KEYWORDS = ('laptop', 'badge', 'phone', 'monitor', 'keyboard', 'mouse',
                      'headset', 'tablet', 'charger', 'docking station', 'key card')


def _sync_equipment_task_status(db, employee_id, equipment_type, new_status, source='system'):
    """Bidirectional sync helper for equipment <-> offboarding task status.

    When *new_status* is ``'returned'`` (equipment side), any offboarding tasks
    whose title mentions *equipment_type* are auto-completed.

    When *new_status* is ``'completed'`` (task side), equipment records whose
    item_type or model matches *equipment_type* are marked returned.

    Circular updates are prevented by checking the current status before writing.
    Every automatic change is audit-logged.
    """
    now_iso = datetime.utcnow().isoformat()
    today_str = datetime.utcnow().strftime('%Y-%m-%d')

    if new_status == 'returned':
        # Equipment was returned -> auto-complete matching offboarding tasks
        search_term = equipment_type.lower()
        if not search_term:
            return
        matching_tasks = db.execute(
            "SELECT t.id, t.title, t.status FROM tasks t "
            "JOIN checklists c ON t.checklist_id = c.id "
            "WHERE t.employee_id = ? AND c.checklist_type = 'offboarding' "
            "  AND t.status != 'completed' "
            "  AND LOWER(t.title) LIKE ?",
            (employee_id, f'%{search_term}%')
        ).fetchall()
        for task_row in matching_tasks:
            if task_row['status'] == 'completed':
                continue  # prevent circular update
            db.execute(
                "UPDATE tasks SET status = 'completed', completed_at = ?, completed_by = ? "
                "WHERE id = ? AND status != 'completed'",
                (now_iso, source, task_row['id'])
            )
            _audit('task_auto_completed', 'task', task_row['id'], {
                'reason': 'equipment_returned',
                'equipment_type': equipment_type,
                'employee_id': employee_id,
                'task_title': task_row['title'],
            })
        db.commit()

    elif new_status == 'completed':
        # Task was completed -> auto-return matching equipment
        equip_rows = db.execute(
            'SELECT id, item_type, model, status FROM equipment '
            'WHERE employee_id = ? AND status != ?',
            (employee_id, 'returned')
        ).fetchall()
        type_lower = equipment_type.lower()
        for eq_row in equip_rows:
            if eq_row['status'] == 'returned':
                continue  # prevent circular update
            eq_type = (eq_row['item_type'] or '').lower()
            eq_model = (eq_row['model'] or '').lower()
            if (eq_type and eq_type in type_lower) or (eq_model and eq_model in type_lower):
                db.execute(
                    "UPDATE equipment SET status = 'returned', returned_date = ? "
                    "WHERE id = ? AND status != 'returned'",
                    (today_str, eq_row['id'])
                )
                _audit('equipment_auto_returned', 'equipment', eq_row['id'], {
                    'reason': 'task_completed',
                    'task_title': equipment_type,
                    'employee_id': employee_id,
                })
        db.commit()


@mgr_bp.route('/equipment/<int:eq_id>', methods=['PUT'])
@login_required
@role_required(['admin', 'hr', 'manager'])
def update_equipment(eq_id):
    """Update equipment status, tracking, return date, etc."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Request body required'}), 400

    db = get_db()
    try:
        eq = db.execute('SELECT * FROM equipment WHERE id = ?', (eq_id,)).fetchone()
        if not eq:
            return jsonify({'error': 'Equipment not found'}), 404

        allowed = ['model', 'serial_number', 'notes', 'status', 'tracking_number',
                    'issued_date', 'returned_date']
        updates = []
        params = []
        for field in allowed:
            if field in data:
                updates.append(f'{field} = ?')
                params.append(data[field])

        if not updates:
            return jsonify({'error': 'No valid fields'}), 400

        params.append(eq_id)
        db.execute(f'UPDATE equipment SET {", ".join(updates)} WHERE id = ?', params)
        db.commit()

        _audit('equipment_updated', 'equipment', eq_id, data)

        # Auto-sync: if equipment marked as 'returned', complete matching offboarding tasks
        if data.get('status') == 'returned':
            employee_id = eq['employee_id']
            item_type = eq['item_type'] or ''
            model_name = eq['model'] or ''
            # Sync by item_type, then by model if different
            synced_terms = set()
            for term in (item_type, model_name):
                if term and term.lower() not in synced_terms:
                    synced_terms.add(term.lower())
                    _sync_equipment_task_status(db, employee_id, term, 'returned', source='system')
            logger.info("Equipment %s returned - synced offboarding tasks for employee %s", eq_id, employee_id)

        updated = db.execute('SELECT * FROM equipment WHERE id = ?', (eq_id,)).fetchone()
        return jsonify(dict(updated))
    finally:
        db.close()


# ── Milestones (30/60/90) ────────────────────────────────────────────────────

@mgr_bp.route('/employees/<int:emp_id>/milestones', methods=['GET'])
@login_required
def list_milestones(emp_id):
    """Get milestones for an employee."""
    user = get_current_user()
    db = get_db()
    try:
        emp = db.execute('SELECT * FROM employees WHERE id = ?', (emp_id,)).fetchone()
        if not emp:
            return jsonify({'error': 'Employee not found'}), 404
        if not _can_access_employee(user, emp):
            return jsonify({'error': 'Access denied'}), 403

        rows = db.execute(
            'SELECT * FROM milestones WHERE employee_id = ? ORDER BY day_marker', (emp_id,)
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        db.close()


@mgr_bp.route('/employees/<int:emp_id>/milestones', methods=['POST'])
@login_required
@role_required(['admin', 'hr', 'manager'])
def create_milestones(emp_id):
    """Create or AI-generate milestones for an employee."""
    db = get_db()
    try:
        emp = db.execute('SELECT * FROM employees WHERE id = ?', (emp_id,)).fetchone()
        if not emp:
            return jsonify({'error': 'Employee not found'}), 404

        data = request.get_json(silent=True) or {}

        if data.get('use_ai'):
            # Generate with Claude API
            generated = _ai_generate_milestones(db, emp_id, emp, data.get('context', ''))
            if not generated:
                # Fall back to department defaults
                _generate_milestones(db, emp_id, emp['role_title'], emp['department'], emp.get('hire_type', 'full-time'))
                db.commit()
        elif data.get('milestones'):
            # Manual milestones from request body
            for m in data['milestones']:
                db.execute(
                    'INSERT INTO milestones (employee_id, day_marker, title, expectations, status, created_at) '
                    'VALUES (?, ?, ?, ?, ?, ?)',
                    (emp_id, m.get('day_marker', 30), m.get('title', ''),
                     m.get('expectations', ''), 'pending', datetime.utcnow().isoformat())
                )
            db.commit()
        else:
            # Default milestones
            _generate_milestones(db, emp_id, emp['role_title'], emp['department'], emp.get('hire_type', 'full-time'))
            db.commit()

        # Create an acknowledgment task for the employee to review the plan
        checklist = db.execute(
            "SELECT id FROM checklists WHERE employee_id = ? AND checklist_type = 'onboarding' AND status = 'active'",
            (emp_id,)
        ).fetchone()
        if checklist:
            due_date = (datetime.utcnow() + timedelta(days=3)).strftime('%Y-%m-%d')
            db.execute(
                'INSERT INTO tasks (checklist_id, employee_id, title, description, category, '
                'assigned_to, assigned_email, due_date, status, phase, is_acknowledgment, sort_order) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (checklist['id'], emp_id,
                 'Review your 30/60/90 day plan with your manager',
                 'Your manager has prepared a 30/60/90 day plan outlining expectations for your first three months. '
                 'Review the milestones in the My 30/60/90 section and discuss them in your next 1:1.',
                 'employee',
                 f"{emp['first_name']} {emp['last_name']}", emp['email'],
                 due_date, 'pending', 'role_training', 1, 999)
            )
            db.commit()

        rows = db.execute(
            'SELECT * FROM milestones WHERE employee_id = ? ORDER BY day_marker', (emp_id,)
        ).fetchall()
        return jsonify([dict(r) for r in rows]), 201
    finally:
        db.close()


@mgr_bp.route('/employees/<int:emp_id>/milestones/generate-ai', methods=['POST'])
@login_required
@role_required(['admin', 'hr', 'manager'])
def generate_ai_milestones(emp_id):
    """On-demand AI milestone generation with optional manager context.
    Clears existing milestones and generates new ones using Claude API.
    Accepts optional 'context' field with manager notes about the role."""
    db = get_db()
    try:
        emp = db.execute('SELECT * FROM employees WHERE id = ?', (emp_id,)).fetchone()
        if not emp:
            return jsonify({'error': 'Employee not found'}), 404

        data = request.get_json(silent=True) or {}
        context = data.get('context', '')

        # Clear existing milestones for this employee
        db.execute('DELETE FROM milestones WHERE employee_id = ?', (emp_id,))
        db.commit()

        # Try AI generation
        success = _ai_generate_milestones(db, emp_id, emp, context)

        if not success:
            # Fall back to department-specific defaults
            _generate_milestones(db, emp_id, emp['role_title'], emp['department'], emp.get('hire_type', 'full-time'))
            db.commit()

        rows = db.execute(
            'SELECT * FROM milestones WHERE employee_id = ? ORDER BY day_marker', (emp_id,)
        ).fetchall()
        return jsonify({
            'milestones': [dict(r) for r in rows],
            'source': 'ai' if success else 'department_defaults'
        }), 201
    finally:
        db.close()


@mgr_bp.route('/milestones/<int:ms_id>', methods=['PUT'])
@login_required
@role_required(['admin', 'hr', 'manager'])
def update_milestone(ms_id):
    """Update a milestone."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Request body required'}), 400

    user = get_current_user()
    db = get_db()
    try:
        ms = db.execute('SELECT * FROM milestones WHERE id = ?', (ms_id,)).fetchone()
        if not ms:
            return jsonify({'error': 'Milestone not found'}), 404

        allowed = ['title', 'expectations', 'status', 'notes']
        updates = []
        params = []
        for field in allowed:
            if field in data:
                updates.append(f'{field} = ?')
                params.append(data[field])

        if data.get('status') in ('achieved', 'missed'):
            updates.append('reviewed_by = ?')
            params.append(user['email'])
            updates.append('reviewed_at = ?')
            params.append(datetime.utcnow().isoformat())

            # If it's the 90-day milestone achieved, mark employee as ramped + celebrate
            if ms['day_marker'] == 90 and data.get('status') == 'achieved':
                db.execute(
                    'UPDATE employees SET ramped_at = ?, ramped_by = ? WHERE id = ?',
                    (datetime.utcnow().isoformat(), user['email'], ms['employee_id'])
                )

                # Calculate journey stats and send celebration
                try:
                    emp = db.execute('SELECT * FROM employees WHERE id = ?', (ms['employee_id'],)).fetchone()
                    stats = db.execute(
                        'SELECT COUNT(*) as total_tasks, '
                        'SUM(CASE WHEN status=\'completed\' THEN 1 ELSE 0 END) as completed_tasks '
                        'FROM tasks WHERE employee_id = ?',
                        (ms['employee_id'],)
                    ).fetchone()
                    mg_stats = db.execute(
                        'SELECT COUNT(*) as total_meets, '
                        'SUM(CASE WHEN completed = 1 THEN 1 ELSE 0 END) as people_met '
                        'FROM meet_greets WHERE employee_id = ?',
                        (ms['employee_id'],)
                    ).fetchone()

                    if emp:
                        emp_name = f"{emp['first_name']} {emp['last_name']}"
                        completed = stats['completed_tasks'] or 0
                        total = stats['total_tasks'] or 0
                        people_met = mg_stats['people_met'] or 0
                        total_meets = mg_stats['total_meets'] or 0

                        from .slack_client import SlackClient
                        slack = SlackClient()

                        # Post to general channel
                        slack.post_message('#general',
                            f"🎉🎉🎉 *{emp_name} is officially ramped!*\n\n"
                            f"After 90 days, {emp['first_name']} has completed their onboarding journey "
                            f"as {emp['role_title']} in {emp['department']}.\n\n"
                            f"📊 Journey stats: {completed}/{total} tasks completed, "
                            f"{people_met}/{total_meets} people met\n\n"
                            f"Welcome to the team, {emp['first_name']}! 🚀")

                        # DM the employee
                        slack.send_dm(emp['email'],
                            f"🎉 Congratulations, {emp['first_name']}! You've been officially marked as fully ramped!\n\n"
                            f"Your 90-day onboarding journey is complete. Here's a look back:\n"
                            f"• {completed} tasks completed\n"
                            f"• {people_met} people met across the company\n\n"
                            f"You're now a full member of the Celito team. Keep crushing it! 💪")
                except Exception as e:
                    logger.warning(f"Failed to send ramp celebration: {e}")

        if not updates:
            return jsonify({'error': 'No valid fields'}), 400

        params.append(ms_id)
        db.execute(f'UPDATE milestones SET {", ".join(updates)} WHERE id = ?', params)
        db.commit()

        _audit('milestone_updated', 'milestone', ms_id, data)

        updated = db.execute('SELECT * FROM milestones WHERE id = ?', (ms_id,)).fetchone()
        return jsonify(dict(updated))
    finally:
        db.close()


# ── Cohorts ──────────────────────────────────────────────────────────────────

@mgr_bp.route('/cohorts', methods=['GET'])
@login_required
@role_required(['admin', 'hr'])
def list_cohorts():
    """List onboarding cohorts with member count."""
    db = get_db()
    try:
        rows = db.execute(
            'SELECT c.*, '
            '(SELECT COUNT(*) FROM employees e WHERE e.cohort_id = c.id) as member_count '
            'FROM onboarding_cohorts c ORDER BY c.start_week DESC'
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        db.close()


@mgr_bp.route('/cohorts', methods=['POST'])
@login_required
@role_required(['admin', 'hr'])
def create_cohort():
    """Create a new cohort."""
    data = request.get_json(silent=True)
    if not data or not data.get('name'):
        return jsonify({'error': 'name is required'}), 400

    db = get_db()
    try:
        db.execute(
            'INSERT INTO onboarding_cohorts (name, start_week, created_at) VALUES (?, ?, ?)',
            (data['name'], data.get('start_week'), datetime.utcnow().isoformat())
        )
        db.commit()
        cid = db.execute('SELECT last_insert_rowid()').fetchone()[0]
        cohort = db.execute('SELECT * FROM onboarding_cohorts WHERE id = ?', (cid,)).fetchone()
        return jsonify(dict(cohort)), 201
    finally:
        db.close()


@mgr_bp.route('/cohorts/<int:cohort_id>/members', methods=['GET'])
@login_required
@role_required(['admin', 'hr'])
def list_cohort_members(cohort_id):
    """List members of a cohort."""
    db = get_db()
    try:
        rows = db.execute(
            'SELECT * FROM employees WHERE cohort_id = ? ORDER BY start_date', (cohort_id,)
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        db.close()


# ── Surveys ──────────────────────────────────────────────────────────────────

@mgr_bp.route('/employees/<int:emp_id>/surveys/send', methods=['POST'])
@login_required
@role_required(['admin', 'hr'])
def send_survey(emp_id):
    """Trigger a pulse survey."""
    data = request.get_json(silent=True) or {}
    survey_type = data.get('survey_type', 'day_7')

    if survey_type not in ('day_7', 'day_30', 'day_90', 'exit_program'):
        return jsonify({'error': 'Invalid survey_type'}), 400

    db = get_db()
    try:
        emp = db.execute('SELECT * FROM employees WHERE id = ?', (emp_id,)).fetchone()
        if not emp:
            return jsonify({'error': 'Employee not found'}), 404

        db.execute(
            'INSERT INTO surveys (employee_id, survey_type, sent_at, status) VALUES (?, ?, ?, ?)',
            (emp_id, survey_type, datetime.utcnow().isoformat(), 'sent')
        )
        db.commit()
        survey_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]

        # Send Slack DM with survey link
        try:
            from .slack_client import SlackClient
            slack = SlackClient()
            survey_url = f"/survey/{survey_id}"
            slack.send_dm(
                emp['email'],
                f"📋 *Onboarding Pulse Survey ({survey_type.replace('_', ' ').title()})*\n"
                f"We'd love to hear how your onboarding is going! Please take a moment to share your feedback.\n"
                f"👉 Complete your survey in the Onboarding Portal"
            )
        except Exception as e:
            logger.warning(f"Failed to send survey DM: {e}")

        _audit('survey_sent', 'employee', emp_id, {'survey_type': survey_type, 'survey_id': survey_id})

        return jsonify({'survey_id': survey_id, 'status': 'sent'}), 201
    finally:
        db.close()


@mgr_bp.route('/surveys/auto-send', methods=['POST'])
@login_required
@role_required(['admin', 'hr'])
def auto_send_surveys():
    """Auto-send any scheduled surveys whose send date has passed.
    Called manually by HR or could be triggered by a daily cron/dashboard load.
    """
    db = get_db()
    try:
        today = date.today().isoformat()
        pending = db.execute(
            "SELECT s.id, s.employee_id, s.survey_type, e.email, e.first_name "
            "FROM surveys s JOIN employees e ON s.employee_id = e.id "
            "WHERE s.status = 'scheduled' AND s.sent_at <= ?",
            (today,)
        ).fetchall()

        sent_count = 0
        for survey in pending:
            try:
                from .slack_client import SlackClient
                slack = SlackClient()
                friendly = survey['survey_type'].replace('_', ' ').title()
                slack.send_dm(
                    survey['email'],
                    f"Hi {survey['first_name']}! 📝 It's time for your *{friendly}* pulse survey.\n\n"
                    f"Please take 2 minutes to share how your onboarding is going. "
                    f"Log into the onboarding portal and check the Surveys section."
                )
            except Exception as e:
                logger.warning(f"Failed to send survey DM to {survey['email']}: {e}")

            db.execute(
                "UPDATE surveys SET status = 'sent', sent_at = ? WHERE id = ?",
                (datetime.utcnow().isoformat(), survey['id'])
            )
            sent_count += 1

        db.commit()
        _audit('surveys_auto_sent', 'survey', None, {'sent_count': sent_count})

        return jsonify({'sent': sent_count, 'message': f'{sent_count} survey(s) sent'})
    finally:
        db.close()


@mgr_bp.route('/employees/<int:emp_id>/surveys', methods=['GET'])
@login_required
def list_surveys(emp_id):
    """List surveys for an employee."""
    user = get_current_user()
    db = get_db()
    try:
        emp = db.execute('SELECT * FROM employees WHERE id = ?', (emp_id,)).fetchone()
        if not emp:
            return jsonify({'error': 'Employee not found'}), 404
        if not _can_access_employee(user, emp):
            return jsonify({'error': 'Access denied'}), 403

        surveys = db.execute(
            'SELECT s.*, '
            '(SELECT COUNT(*) FROM survey_responses WHERE survey_id = s.id) as response_count, '
            '(SELECT AVG(rating) FROM survey_responses WHERE survey_id = s.id AND rating IS NOT NULL) as avg_rating '
            'FROM surveys s WHERE s.employee_id = ? ORDER BY s.sent_at DESC',
            (emp_id,)
        ).fetchall()
        return jsonify([dict(s) for s in surveys])
    finally:
        db.close()


@mgr_bp.route('/surveys/<int:survey_id>/respond', methods=['POST'])
@login_required
def submit_survey_response(survey_id):
    """Submit a survey response."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Request body required'}), 400

    db = get_db()
    try:
        survey = db.execute('SELECT * FROM surveys WHERE id = ?', (survey_id,)).fetchone()
        if not survey:
            return jsonify({'error': 'Survey not found'}), 404

        responses = data.get('responses', [data])  # Accept single or array
        for resp in responses:
            db.execute(
                'INSERT INTO survey_responses (survey_id, question, answer, rating, created_at) '
                'VALUES (?, ?, ?, ?, ?)',
                (survey_id, resp.get('question', ''), resp.get('answer'),
                 resp.get('rating'), datetime.utcnow().isoformat())
            )

        db.execute(
            "UPDATE surveys SET status = 'completed', completed_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), survey_id)
        )
        db.commit()

        return jsonify({'message': 'Survey response submitted'}), 201
    finally:
        db.close()


@mgr_bp.route('/surveys/metrics', methods=['GET'])
@login_required
@role_required(['admin', 'hr'])
def survey_metrics():
    """Aggregate survey metrics (NPS, averages)."""
    db = get_db()
    try:
        # Overall NPS from ratings (1-5 scale: 4-5 = promoter, 3 = passive, 1-2 = detractor)
        ratings = db.execute(
            'SELECT sr.rating FROM survey_responses sr '
            'JOIN surveys s ON sr.survey_id = s.id '
            'WHERE sr.rating IS NOT NULL'
        ).fetchall()

        total_ratings = len(ratings)
        if total_ratings > 0:
            promoters = sum(1 for r in ratings if r['rating'] >= 4)
            detractors = sum(1 for r in ratings if r['rating'] <= 2)
            nps = round(((promoters - detractors) / total_ratings) * 100, 1)
            avg_rating = round(sum(r['rating'] for r in ratings) / total_ratings, 2)
        else:
            nps = 0
            avg_rating = 0

        # By survey type
        by_type = db.execute(
            'SELECT s.survey_type, COUNT(DISTINCT s.id) as count, '
            'AVG(sr.rating) as avg_rating '
            'FROM surveys s LEFT JOIN survey_responses sr ON sr.survey_id = s.id '
            "WHERE s.status = 'completed' GROUP BY s.survey_type"
        ).fetchall()

        return jsonify({
            'nps': nps,
            'avg_rating': avg_rating,
            'total_surveys': total_ratings,
            'by_type': [dict(b) for b in by_type],
        })
    finally:
        db.close()


# ── Meet & Greets ────────────────────────────────────────────────────────────

@mgr_bp.route('/employees/<int:emp_id>/meet-greets', methods=['GET'])
@login_required
def list_meet_greets(emp_id):
    """List meet-and-greet assignments."""
    user = get_current_user()
    db = get_db()
    try:
        emp = db.execute('SELECT * FROM employees WHERE id = ?', (emp_id,)).fetchone()
        if not emp:
            return jsonify({'error': 'Employee not found'}), 404
        if not _can_access_employee(user, emp):
            return jsonify({'error': 'Access denied'}), 403

        rows = db.execute(
            'SELECT * FROM meet_greets WHERE employee_id = ? ORDER BY week_number, id', (emp_id,)
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        db.close()


@mgr_bp.route('/employees/<int:emp_id>/meet-greets', methods=['POST'])
@login_required
@role_required(['admin', 'hr', 'manager'])
def create_meet_greets(emp_id):
    """Create meet-and-greet assignments (bulk)."""
    data = request.get_json(silent=True)
    if not data or not data.get('contacts'):
        return jsonify({'error': 'contacts array is required'}), 400

    db = get_db()
    try:
        emp = db.execute('SELECT * FROM employees WHERE id = ?', (emp_id,)).fetchone()
        if not emp:
            return jsonify({'error': 'Employee not found'}), 404

        created = []
        for contact in data['contacts']:
            db.execute(
                'INSERT INTO meet_greets (employee_id, contact_name, contact_email, contact_role, '
                'reason, week_number) VALUES (?, ?, ?, ?, ?, ?)',
                (emp_id, contact.get('name') or '', contact.get('email') or '',
                 contact.get('role') or '', contact.get('reason') or '',
                 contact.get('week_number', 1))
            )
            mg_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
            mg = db.execute('SELECT * FROM meet_greets WHERE id = ?', (mg_id,)).fetchone()
            created.append(dict(mg))

        db.commit()
        _audit('meet_greets_created', 'employee', emp_id, {'count': len(created)})

        return jsonify(created), 201
    finally:
        db.close()


@mgr_bp.route('/meet-greets/batch-schedule', methods=['POST'])
@login_required
@role_required(['admin', 'hr', 'manager'])
def batch_schedule_meet_greets():
    """
    Schedule multiple meet-and-greets at once.

    Mode 1 – direct batch (set the same date for selected IDs):
      { "meet_greet_ids": [1, 2, 3], "scheduled_date": "2026-09-15" }

    Mode 2 – auto-spread across a date range for one or more employees:
      { "employee_ids": [1, 2, 3],
        "date_range_start": "2026-09-15",
        "date_range_end": "2026-09-19",
        "slots_per_day": 4 }
    """
    user = get_current_user()
    body = request.get_json(silent=True) or {}
    db = get_db()
    try:
        updated = 0

        if 'meet_greet_ids' in body:
            ids = body['meet_greet_ids']
            sched_date = body.get('scheduled_date', '')
            if not ids or not sched_date:
                return jsonify({'error': 'meet_greet_ids and scheduled_date required'}), 400
            placeholders = ','.join('?' * len(ids))
            db.execute(
                f"UPDATE meet_greets SET scheduled_date = ? WHERE id IN ({placeholders}) AND completed = 0",
                [sched_date] + list(ids)
            )
            updated = len(ids)

        elif 'employee_ids' in body:
            emp_ids = body['employee_ids']
            start_str = body.get('date_range_start', '')
            end_str = body.get('date_range_end', '')
            slots_per_day = body.get('slots_per_day', 4)
            if not emp_ids or not start_str or not end_str:
                return jsonify({'error': 'employee_ids, date_range_start, date_range_end required'}), 400

            placeholders = ','.join('?' * len(emp_ids))
            unscheduled = db.execute(
                f"SELECT id FROM meet_greets WHERE employee_id IN ({placeholders}) "
                f"AND completed = 0 AND (scheduled_date IS NULL OR scheduled_date = '') "
                f"ORDER BY week_number, id",
                emp_ids
            ).fetchall()

            current_date = date.fromisoformat(start_str)
            end_date = date.fromisoformat(end_str)
            slot_idx = 0

            for mg in unscheduled:
                # Advance past weekends
                while current_date.weekday() >= 5 and current_date <= end_date:
                    current_date += timedelta(days=1)
                if current_date > end_date:
                    break

                db.execute(
                    "UPDATE meet_greets SET scheduled_date = ? WHERE id = ?",
                    (current_date.isoformat(), mg['id'])
                )
                updated += 1
                slot_idx += 1
                if slot_idx >= slots_per_day:
                    slot_idx = 0
                    current_date += timedelta(days=1)
        else:
            return jsonify({'error': 'Provide meet_greet_ids or employee_ids'}), 400

        db.commit()
        _audit('batch_schedule_meet_greets', 'meet_greet', None, {'count': updated})
    finally:
        db.close()

    return jsonify({'ok': True, 'scheduled': updated})


@mgr_bp.route('/meet-greets/<int:mg_id>', methods=['PUT'])
@login_required
def update_meet_greet(mg_id):
    """Mark a meet-and-greet as completed."""
    data = request.get_json(silent=True) or {}
    db = get_db()
    try:
        mg = db.execute('SELECT * FROM meet_greets WHERE id = ?', (mg_id,)).fetchone()
        if not mg:
            return jsonify({'error': 'Meet & greet not found'}), 404

        updates = []
        params = []

        if 'completed' in data:
            updates.append('completed = ?')
            params.append(1 if data['completed'] else 0)
            if data['completed']:
                updates.append('completed_at = ?')
                params.append(datetime.utcnow().isoformat())

        if 'scheduled_date' in data:
            updates.append('scheduled_date = ?')
            params.append(data['scheduled_date'])

        if 'contact_email' in data:
            updates.append('contact_email = ?')
            params.append((data['contact_email'] or '').strip().lower())

        if 'contact_name' in data:
            updates.append('contact_name = ?')
            params.append(data['contact_name'])

        if not updates:
            return jsonify({'error': 'No valid fields'}), 400

        params.append(mg_id)
        db.execute(f'UPDATE meet_greets SET {", ".join(updates)} WHERE id = ?', params)
        db.commit()

        updated = db.execute('SELECT * FROM meet_greets WHERE id = ?', (mg_id,)).fetchone()
        return jsonify(dict(updated))
    finally:
        db.close()


# ── Buddy Tasks ──────────────────────────────────────────────────────────────

@mgr_bp.route('/employees/<int:emp_id>/buddy-tasks', methods=['GET'])
@login_required
def list_buddy_tasks(emp_id):
    """List buddy's tasks for an employee."""
    user = get_current_user()
    db = get_db()
    try:
        emp = db.execute('SELECT * FROM employees WHERE id = ?', (emp_id,)).fetchone()
        if not emp:
            return jsonify({'error': 'Employee not found'}), 404
        if not _can_access_employee(user, emp) and user['email'] != emp.get('buddy_email'):
            return jsonify({'error': 'Access denied'}), 403

        rows = db.execute(
            'SELECT * FROM buddy_tasks WHERE employee_id = ? ORDER BY due_offset_days', (emp_id,)
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        db.close()


@mgr_bp.route('/buddy-tasks/<int:bt_id>', methods=['PUT'])
@login_required
def update_buddy_task(bt_id):
    """Update a buddy task status."""
    data = request.get_json(silent=True) or {}
    db = get_db()
    try:
        bt = db.execute('SELECT * FROM buddy_tasks WHERE id = ?', (bt_id,)).fetchone()
        if not bt:
            return jsonify({'error': 'Buddy task not found'}), 404

        if data.get('status') == 'completed':
            db.execute(
                "UPDATE buddy_tasks SET status = 'completed', completed_at = ? WHERE id = ?",
                (datetime.utcnow().isoformat(), bt_id)
            )
        elif 'status' in data:
            db.execute('UPDATE buddy_tasks SET status = ? WHERE id = ?', (data['status'], bt_id))

        db.commit()
        updated = db.execute('SELECT * FROM buddy_tasks WHERE id = ?', (bt_id,)).fetchone()
        return jsonify(dict(updated))
    finally:
        db.close()


# ── Checklist Templates ──────────────────────────────────────────────────────

@mgr_bp.route('/templates', methods=['GET'])
@login_required
@role_required(['admin', 'hr'])
def list_templates():
    """List checklist templates."""
    db = get_db()
    try:
        rows = db.execute(
            'SELECT * FROM checklist_templates WHERE is_active = 1 ORDER BY department, phase'
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d['tasks'] = json.loads(d.pop('tasks_json', '[]'))
            except (json.JSONDecodeError, TypeError):
                d['tasks'] = []
            result.append(d)
        return jsonify(result)
    finally:
        db.close()


@mgr_bp.route('/templates/defaults', methods=['GET'])
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


@mgr_bp.route('/templates/<int:tmpl_id>/duplicate', methods=['POST'])
@login_required
@role_required(['admin'])
def duplicate_template(tmpl_id):
    """Duplicate an existing template."""
    db = get_db()
    try:
        tmpl = db.execute(
            'SELECT * FROM checklist_templates WHERE id = ?', (tmpl_id,)
        ).fetchone()
        if not tmpl:
            return jsonify({'error': 'Template not found'}), 404

        data = request.get_json(silent=True) or {}
        new_name = data.get('name', f"{tmpl['name']} (Copy)")

        db.execute(
            'INSERT INTO checklist_templates (name, department, checklist_type, phase, '
            'tasks_json, location_mode, is_active, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, 1, ?)',
            (new_name, tmpl['department'], tmpl['checklist_type'], tmpl['phase'],
             tmpl['tasks_json'], tmpl['location_mode'],
             datetime.utcnow().isoformat())
        )
        db.commit()
        new_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]

        _audit('template_duplicated', 'template', new_id,
               {'source_id': tmpl_id, 'name': new_name})

        new_tmpl = db.execute(
            'SELECT * FROM checklist_templates WHERE id = ?', (new_id,)
        ).fetchone()
        return jsonify(dict(new_tmpl)), 201
    finally:
        db.close()


@mgr_bp.route('/templates', methods=['POST'])
@login_required
@role_required(['admin'])
def create_template():
    """Create a checklist template."""
    data = request.get_json(silent=True)
    if not data or not data.get('name') or not data.get('tasks_json'):
        return jsonify({'error': 'name and tasks_json are required'}), 400

    db = get_db()
    try:
        tasks_json = data['tasks_json']
        if isinstance(tasks_json, list):
            tasks_json = json.dumps(tasks_json)

        db.execute(
            'INSERT INTO checklist_templates (name, department, checklist_type, phase, tasks_json, '
            'location_mode, is_active, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (data['name'], data.get('department'), data.get('checklist_type', 'onboarding'),
             data.get('phase', 'company_onboarding'), tasks_json,
             data.get('location_mode', 'all'), 1, datetime.utcnow().isoformat())
        )
        db.commit()
        tid = db.execute('SELECT last_insert_rowid()').fetchone()[0]

        _audit('template_created', 'template', tid, {'name': data['name']})

        tmpl = db.execute('SELECT * FROM checklist_templates WHERE id = ?', (tid,)).fetchone()
        return jsonify(dict(tmpl)), 201
    finally:
        db.close()


@mgr_bp.route('/templates/<int:tmpl_id>', methods=['PUT'])
@login_required
@role_required(['admin'])
def update_template(tmpl_id):
    """Update a checklist template."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Request body required'}), 400

    db = get_db()
    try:
        tmpl = db.execute('SELECT * FROM checklist_templates WHERE id = ?', (tmpl_id,)).fetchone()
        if not tmpl:
            return jsonify({'error': 'Template not found'}), 404

        allowed = ['name', 'department', 'checklist_type', 'phase', 'location_mode']
        updates = []
        params = []
        for field in allowed:
            if field in data:
                updates.append(f'{field} = ?')
                params.append(data[field])

        if 'tasks_json' in data:
            tasks_json = data['tasks_json']
            if isinstance(tasks_json, list):
                tasks_json = json.dumps(tasks_json)
            updates.append('tasks_json = ?')
            params.append(tasks_json)

        updates.append('updated_at = ?')
        params.append(datetime.utcnow().isoformat())

        params.append(tmpl_id)
        db.execute(f'UPDATE checklist_templates SET {", ".join(updates)} WHERE id = ?', params)
        db.commit()

        _audit('template_updated', 'template', tmpl_id, data)

        updated = db.execute('SELECT * FROM checklist_templates WHERE id = ?', (tmpl_id,)).fetchone()
        return jsonify(dict(updated))
    finally:
        db.close()


@mgr_bp.route('/templates/<int:tmpl_id>', methods=['DELETE'])
@login_required
@role_required(['admin'])
def delete_template(tmpl_id):
    """Deactivate a template."""
    db = get_db()
    try:
        tmpl = db.execute('SELECT * FROM checklist_templates WHERE id = ?', (tmpl_id,)).fetchone()
        if not tmpl:
            return jsonify({'error': 'Template not found'}), 404

        db.execute('UPDATE checklist_templates SET is_active = 0 WHERE id = ?', (tmpl_id,))
        db.commit()

        _audit('template_deactivated', 'template', tmpl_id, {})

        return jsonify({'message': 'Template deactivated'})
    finally:
        db.close()


# ── Dashboard ────────────────────────────────────────────────────────────────

@mgr_bp.route('/dashboard', methods=['GET'])
@login_required
def get_dashboard():
    """Get dashboard data with enhanced metrics."""
    user = get_current_user()
    db = get_db()
    today = date.today().isoformat()

    try:
        if user['role'] in ('admin', 'hr'):
            # Active onboardings with progress
            onboardings = db.execute(
                "SELECT c.*, e.first_name, e.last_name, e.email as emp_email, e.department, "
                "e.start_date, e.role_title, e.location_mode, e.buddy_email, "
                "(SELECT COUNT(*) FROM tasks WHERE checklist_id = c.id) as total_tasks, "
                "(SELECT COUNT(*) FROM tasks WHERE checklist_id = c.id AND status = 'completed') as done_tasks "
                "FROM checklists c JOIN employees e ON c.employee_id = e.id "
                "WHERE c.checklist_type = 'onboarding' AND c.status = 'active' "
                "ORDER BY e.start_date"
            ).fetchall()

            onboarding_data = []
            for o in onboardings:
                od = dict(o)
                total = od.pop('total_tasks', 0)
                done = od.pop('done_tasks', 0)
                od['progress'] = round((done / total * 100) if total > 0 else 0, 1)
                od['total_tasks'] = total
                od['completed_tasks'] = done
                onboarding_data.append(od)

            # Active offboardings
            offboardings = db.execute(
                "SELECT c.*, e.first_name, e.last_name, e.email as emp_email, e.department, "
                "e.end_date, e.role_title, "
                "(SELECT COUNT(*) FROM tasks WHERE checklist_id = c.id) as total_tasks, "
                "(SELECT COUNT(*) FROM tasks WHERE checklist_id = c.id AND status = 'completed') as done_tasks "
                "FROM checklists c JOIN employees e ON c.employee_id = e.id "
                "WHERE c.checklist_type = 'offboarding' AND c.status = 'active' "
                "ORDER BY e.end_date"
            ).fetchall()

            offboarding_data = []
            for o in offboardings:
                od = dict(o)
                total = od.pop('total_tasks', 0)
                done = od.pop('done_tasks', 0)
                od['progress'] = round((done / total * 100) if total > 0 else 0, 1)
                od['total_tasks'] = total
                od['completed_tasks'] = done
                offboarding_data.append(od)

            # Upcoming starts (next 30 days)
            upcoming = db.execute(
                "SELECT * FROM employees WHERE status = 'pending' AND start_date >= ? "
                "AND start_date <= date(?, '+30 days') ORDER BY start_date",
                (today, today)
            ).fetchall()

            # Pre-boarding readiness for upcoming starts
            pre_boarding = []
            for u in upcoming:
                ud = dict(u)
                # Check IT tasks completion
                it_ready = db.execute(
                    "SELECT COUNT(*) as total, "
                    "SUM(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END) as done "
                    "FROM tasks t JOIN checklists c ON t.checklist_id = c.id "
                    "WHERE c.employee_id = ? AND t.phase = 'pre_boarding' AND t.category = 'it'",
                    (u['id'],)
                ).fetchone()
                equip_ready = db.execute(
                    "SELECT COUNT(*) as total, "
                    "SUM(CASE WHEN status IN ('delivered', 'issued') THEN 1 ELSE 0 END) as done "
                    "FROM equipment WHERE employee_id = ?",
                    (u['id'],)
                ).fetchone()
                admin_ready = db.execute(
                    "SELECT COUNT(*) as total, "
                    "SUM(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END) as done "
                    "FROM tasks t JOIN checklists c ON t.checklist_id = c.id "
                    "WHERE c.employee_id = ? AND t.phase = 'pre_boarding' AND t.category = 'hr'",
                    (u['id'],)
                ).fetchone()

                def _pct(row):
                    t = row['total'] or 0
                    d = row['done'] or 0
                    return round((d / t * 100) if t > 0 else 0, 1)

                ud['it_readiness'] = _pct(it_ready)
                ud['equipment_readiness'] = _pct(equip_ready)
                ud['admin_readiness'] = _pct(admin_ready)
                ud['overall_readiness'] = round((ud['it_readiness'] + ud['equipment_readiness'] + ud['admin_readiness']) / 3, 1)
                pre_boarding.append(ud)

            # Overdue tasks
            overdue = db.execute(
                "SELECT t.*, e.first_name || ' ' || e.last_name as employee_name, "
                "c.checklist_type, t.phase "
                "FROM tasks t JOIN employees e ON t.employee_id = e.id "
                "JOIN checklists c ON t.checklist_id = c.id "
                "WHERE t.status NOT IN ('completed', 'skipped') AND t.due_date < ? "
                "AND t.parent_task_id IS NULL "
                "ORDER BY t.due_date",
                (today,)
            ).fetchall()

            # Overdue by phase
            overdue_by_phase = {}
            for t in overdue:
                p = t['phase'] or 'other'
                if p not in overdue_by_phase:
                    overdue_by_phase[p] = 0
                overdue_by_phase[p] += 1

            # Monthly metrics
            month_start = date.today().replace(day=1).isoformat()
            hires = db.execute(
                "SELECT COUNT(*) as cnt FROM employees WHERE start_date >= ?", (month_start,)
            ).fetchone()['cnt']
            departures = db.execute(
                "SELECT COUNT(*) as cnt FROM employees WHERE end_date >= ? AND end_date <= ?",
                (month_start, today)
            ).fetchone()['cnt']

            # Advanced metrics
            # Time to ramp (avg days between start_date and ramped_at)
            ramp_data = db.execute(
                "SELECT AVG(julianday(ramped_at) - julianday(start_date)) as avg_days "
                "FROM employees WHERE ramped_at IS NOT NULL"
            ).fetchone()
            avg_ramp = round(ramp_data['avg_days'], 1) if ramp_data['avg_days'] else None

            # 90-day retention
            ninety_ago = (date.today() - timedelta(days=90)).isoformat()
            started_90 = db.execute(
                "SELECT COUNT(*) as cnt FROM employees WHERE start_date <= ? AND start_date >= date(?, '-180 days')",
                (ninety_ago, ninety_ago)
            ).fetchone()['cnt']
            departed_90 = db.execute(
                "SELECT COUNT(*) as cnt FROM employees WHERE start_date <= ? AND start_date >= date(?, '-180 days') "
                "AND status = 'departed' AND julianday(end_date) - julianday(start_date) <= 90",
                (ninety_ago, ninety_ago)
            ).fetchone()['cnt']
            retention_90 = round(((started_90 - departed_90) / started_90 * 100) if started_90 > 0 else 100, 1)

            # Task on-time %
            completed_tasks = db.execute(
                "SELECT COUNT(*) as total, "
                "SUM(CASE WHEN completed_at <= due_date OR completed_at IS NULL THEN 1 ELSE 0 END) as on_time "
                "FROM tasks WHERE status = 'completed' AND due_date IS NOT NULL"
            ).fetchone()
            tasks_on_time = round(
                (completed_tasks['on_time'] / completed_tasks['total'] * 100) if completed_tasks['total'] else 100, 1
            )

            # NPS from surveys
            ratings = db.execute(
                'SELECT rating FROM survey_responses WHERE rating IS NOT NULL'
            ).fetchall()
            if ratings:
                promoters = sum(1 for r in ratings if r['rating'] >= 4)
                detractors = sum(1 for r in ratings if r['rating'] <= 2)
                nps = round(((promoters - detractors) / len(ratings)) * 100, 1)
            else:
                nps = None

            # Active cohorts
            cohorts = db.execute(
                "SELECT c.*, "
                "(SELECT COUNT(*) FROM employees e WHERE e.cohort_id = c.id) as member_count, "
                "(SELECT COUNT(*) FROM employees e WHERE e.cohort_id = c.id AND e.status = 'active') as active_count "
                "FROM onboarding_cohorts c "
                "WHERE EXISTS (SELECT 1 FROM employees e WHERE e.cohort_id = c.id AND e.status IN ('pending','active')) "
                "ORDER BY c.start_week DESC LIMIT 5"
            ).fetchall()

            return jsonify({
                'role': user['role'],
                'onboardings': onboarding_data,
                'offboardings': offboarding_data,
                'upcoming_starts': [dict(u) for u in upcoming],
                'pre_boarding_readiness': pre_boarding,
                'overdue_tasks': [dict(t) for t in overdue],
                'overdue_by_phase': overdue_by_phase,
                'metrics': {
                    'hires_this_month': hires,
                    'departures_this_month': departures,
                    'active_onboardings': len(onboarding_data),
                    'active_offboardings': len(offboarding_data),
                    'overdue_count': len(overdue),
                    'time_to_ramp_avg': avg_ramp,
                    'retention_90_day': retention_90,
                    'new_hire_nps': nps,
                    'tasks_on_time_pct': tasks_on_time,
                },
                'cohort_summary': [dict(c) for c in cohorts],
            })

        elif user['role'] == 'manager':
            # Manager's team with progress
            my_reports = db.execute(
                "SELECT e.*, "
                "(SELECT COUNT(*) FROM tasks t JOIN checklists c ON t.checklist_id = c.id "
                "WHERE c.employee_id = e.id AND c.status = 'active') as total_tasks, "
                "(SELECT COUNT(*) FROM tasks t JOIN checklists c ON t.checklist_id = c.id "
                "WHERE c.employee_id = e.id AND c.status = 'active' AND t.status = 'completed') as done_tasks "
                "FROM employees e WHERE e.manager_email = ? AND e.status != 'departed' "
                "ORDER BY e.start_date DESC",
                (user['email'],)
            ).fetchall()

            team_data = []
            team_ids = []
            active_onboardings = 0
            for r in my_reports:
                rd = dict(r)
                total = rd.pop('total_tasks', 0)
                done = rd.pop('done_tasks', 0)
                rd['progress'] = round((done / total * 100) if total > 0 else 0, 1)
                rd['total_tasks'] = total
                rd['completed_tasks'] = done
                team_data.append(rd)
                team_ids.append(r['id'])
                if r['status'] in ('active', 'onboarding'):
                    active_onboardings += 1

            # Tasks assigned to me
            my_tasks = db.execute(
                "SELECT t.*, e.first_name || ' ' || e.last_name as employee_name, t.phase "
                "FROM tasks t JOIN employees e ON t.employee_id = e.id "
                "WHERE t.assigned_email = ? AND t.status NOT IN ('completed', 'skipped') "
                "ORDER BY t.due_date",
                (user['email'],)
            ).fetchall()

            # Overdue tasks for my team
            overdue_tasks = []
            if team_ids:
                placeholders = ','.join('?' * len(team_ids))
                overdue_tasks = db.execute(
                    "SELECT t.*, e.first_name || ' ' || e.last_name as employee_name "
                    "FROM tasks t JOIN employees e ON t.employee_id = e.id "
                    "WHERE t.employee_id IN (" + placeholders + ") "
                    "AND t.status NOT IN ('completed', 'skipped') "
                    "AND t.due_date < ? AND t.parent_task_id IS NULL "
                    "ORDER BY t.due_date",
                    (*team_ids, today)
                ).fetchall()

            # Upcoming milestones for my team (next 14 days)
            upcoming_milestones = []
            if team_ids:
                placeholders = ','.join('?' * len(team_ids))
                fourteen_later = (date.today() + timedelta(days=14)).isoformat()
                upcoming_milestones = db.execute(
                    "SELECT m.*, e.first_name || ' ' || e.last_name as employee_name, "
                    "date(e.start_date, '+' || m.day_marker || ' days') as target_date "
                    "FROM milestones m JOIN employees e ON m.employee_id = e.id "
                    "WHERE m.employee_id IN (" + placeholders + ") "
                    "AND m.status != 'achieved' "
                    "AND e.start_date IS NOT NULL "
                    "AND date(e.start_date, '+' || m.day_marker || ' days') <= ? "
                    "ORDER BY date(e.start_date, '+' || m.day_marker || ' days')",
                    (*team_ids, fourteen_later)
                ).fetchall()

            # Buddy tasks assigned to me
            my_buddy_tasks = db.execute(
                "SELECT bt.*, e.first_name || ' ' || e.last_name as employee_name "
                "FROM buddy_tasks bt JOIN employees e ON bt.employee_id = e.id "
                "WHERE bt.buddy_email = ? AND bt.status != 'completed' "
                "ORDER BY bt.due_offset_days",
                (user['email'],)
            ).fetchall()

            return jsonify({
                'role': 'manager',
                'team': team_data,
                'my_tasks': [dict(t) for t in my_tasks],
                'my_buddy_tasks': [dict(bt) for bt in my_buddy_tasks],
                'overdue_tasks': [dict(t) for t in overdue_tasks],
                'upcoming_milestones': [dict(m) for m in upcoming_milestones],
                'stats': {
                    'team_size': len(team_data),
                    'active_onboardings': active_onboardings,
                    'tasks_pending': len(my_tasks),
                    'tasks_overdue': len(overdue_tasks),
                    'upcoming_milestones': len(upcoming_milestones),
                },
            })

        else:
            return jsonify({'role': 'employee', 'redirect': '/api/me/onboarding'})
    finally:
        db.close()


# ── This Week's Focus ────────────────────────────────────────────────────────

@mgr_bp.route('/employees/<int:emp_id>/this-week', methods=['GET'])
@login_required
def this_weeks_focus(emp_id):
    """Get tasks due this week for an employee — the 'don't cram' view."""
    user = get_current_user()
    db = get_db()
    try:
        emp = db.execute('SELECT * FROM employees WHERE id = ?', (emp_id,)).fetchone()
        if not emp:
            return jsonify({'error': 'Employee not found'}), 404
        if not _can_access_employee(user, emp):
            return jsonify({'error': 'Access denied'}), 403

        today = date.today()
        week_end = (today + timedelta(days=7)).isoformat()
        today_str = today.isoformat()

        tasks = db.execute(
            "SELECT t.*, c.checklist_type FROM tasks t "
            "JOIN checklists c ON t.checklist_id = c.id "
            "WHERE t.employee_id = ? AND t.status NOT IN ('completed', 'skipped') "
            "AND t.due_date >= ? AND t.due_date <= ? "
            "AND t.parent_task_id IS NULL "
            "ORDER BY t.due_date, t.sort_order",
            (emp_id, today_str, week_end)
        ).fetchall()

        # Overdue tasks (always show)
        overdue = db.execute(
            "SELECT t.*, c.checklist_type FROM tasks t "
            "JOIN checklists c ON t.checklist_id = c.id "
            "WHERE t.employee_id = ? AND t.status NOT IN ('completed', 'skipped') "
            "AND t.due_date < ? AND t.parent_task_id IS NULL "
            "ORDER BY t.due_date",
            (emp_id, today_str)
        ).fetchall()

        return jsonify({
            'this_week': [dict(t) for t in tasks],
            'overdue': [dict(t) for t in overdue],
            'employee_name': f"{emp['first_name']} {emp['last_name']}",
        })
    finally:
        db.close()


# ── Documents ────────────────────────────────────────────────────────────────

@mgr_bp.route('/employees/<int:emp_id>/welcome-letter', methods=['POST'])
@login_required
@role_required(['admin', 'hr'])
def generate_welcome_letter(emp_id):
    """Generate a welcome letter as a .docx file."""
    db = get_db()
    try:
        emp = db.execute('SELECT * FROM employees WHERE id = ?', (emp_id,)).fetchone()
        if not emp:
            return jsonify({'error': 'Employee not found'}), 404

        from docx import Document
        from docx.shared import Pt, RGBColor
        from docx.enum.text import WD_ALIGN_PARAGRAPH

        doc = Document()

        title = doc.add_heading('Celito Communications, Inc.', level=1)
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        for run in title.runs:
            run.font.color.rgb = RGBColor(0x00, 0x44, 0x7c)

        doc.add_paragraph('')
        welcome = doc.add_heading('Welcome to Celito!', level=2)
        welcome.alignment = WD_ALIGN_PARAGRAPH.CENTER

        doc.add_paragraph('')
        doc.add_paragraph(f"Date: {date.today().strftime('%B %d, %Y')}")
        doc.add_paragraph('')
        doc.add_paragraph(f"Dear {emp['first_name']} {emp['last_name']},")
        doc.add_paragraph('')
        doc.add_paragraph(
            f"We are thrilled to welcome you to Celito Communications, Inc.! "
            f"We are excited to have you join our team as a {emp['role_title']} in the "
            f"{emp['department']} department."
        )
        doc.add_paragraph('')
        doc.add_paragraph(
            f"Your start date is {emp['start_date']}. "
            f"{'Please report to our ' + emp['location'] + ' office.' if emp.get('location') else ''} "
            f"Your manager will reach out to you before your first day with more details about "
            f"what to expect."
        )

        doc.add_paragraph('')
        doc.add_heading('Before Your First Day', level=3)
        for item in [
            'Review and sign your employment agreement and NDA',
            'Complete your I-9 documentation (bring valid ID)',
            'Set up direct deposit for payroll',
            'Review the employee handbook',
            'Look for your welcome SWAG!',
        ]:
            doc.add_paragraph(item, style='List Bullet')

        doc.add_paragraph('')
        doc.add_heading('Your First Week', level=3)
        doc.add_paragraph(
            'During your first week, you will attend an orientation session with senior leadership, '
            'meet your team, connect with your onboarding buddy, and begin your personalized '
            'onboarding journey. Your 30/60/90 day plan will be shared by your manager.'
        )

        if emp.get('buddy_email'):
            doc.add_paragraph('')
            doc.add_heading('Your Onboarding Buddy', level=3)
            doc.add_paragraph(
                f'Your onboarding buddy ({emp["buddy_email"]}) will reach out before your start date. '
                f'They are a peer outside your reporting line who can help with day-to-day questions.'
            )

        doc.add_paragraph('')
        doc.add_paragraph('We look forward to working with you!')
        doc.add_paragraph('')
        doc.add_paragraph('Best regards,')
        doc.add_paragraph('Human Resources')
        doc.add_paragraph('Celito Communications, Inc.')

        buffer = io.BytesIO()
        doc.save(buffer)
        buffer.seek(0)

        filename = f"Welcome_Letter_{emp['first_name']}_{emp['last_name']}.docx"
        _audit('document_generated', 'employee', emp_id, {'type': 'welcome_letter'})

        return send_file(
            buffer, as_attachment=True, download_name=filename,
            mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        )

    except ImportError:
        return jsonify({'error': 'python-docx is not installed. Run: pip install python-docx'}), 500
    except Exception as e:
        logger.error(f"Welcome letter generation failed: {e}")
        return jsonify({'error': f'Document generation failed: {str(e)}'}), 500
    finally:
        db.close()


@mgr_bp.route('/employees/<int:emp_id>/equipment-request', methods=['POST'])
@login_required
@role_required(['admin', 'hr', 'manager'])
def generate_equipment_request(emp_id):
    """Generate an equipment request form as a .docx file."""
    db = get_db()
    try:
        emp = db.execute('SELECT * FROM employees WHERE id = ?', (emp_id,)).fetchone()
        if not emp:
            return jsonify({'error': 'Employee not found'}), 404

        from docx import Document
        from docx.shared import RGBColor
        from docx.enum.text import WD_ALIGN_PARAGRAPH

        doc = Document()
        title = doc.add_heading('Equipment Request Form', level=1)
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        for run in title.runs:
            run.font.color.rgb = RGBColor(0x00, 0x44, 0x7c)

        doc.add_paragraph(f"Date: {date.today().strftime('%B %d, %Y')}")
        doc.add_paragraph('')

        doc.add_heading('Employee Information', level=2)
        table = doc.add_table(rows=6, cols=2)
        table.style = 'Table Grid'
        for i, (label, value) in enumerate([
            ('Employee Name', f"{emp['first_name']} {emp['last_name']}"),
            ('Email', emp['email']),
            ('Department', emp['department']),
            ('Role', emp['role_title']),
            ('Start Date', emp['start_date']),
            ('Location', emp.get('location') or 'TBD'),
        ]):
            table.cell(i, 0).text = label
            table.cell(i, 1).text = value or ''

        doc.add_paragraph('')
        doc.add_heading('Equipment Requested', level=2)

        # Pull from equipment table if records exist
        eq_rows = db.execute(
            'SELECT * FROM equipment WHERE employee_id = ?', (emp_id,)
        ).fetchall()

        if eq_rows:
            eq_table = doc.add_table(rows=len(eq_rows) + 1, cols=4)
            eq_table.style = 'Table Grid'
            eq_table.cell(0, 0).text = 'Item'
            eq_table.cell(0, 1).text = 'Model/Spec'
            eq_table.cell(0, 2).text = 'Notes'
            eq_table.cell(0, 3).text = 'Status'
            for i, eq in enumerate(eq_rows):
                eq_table.cell(i + 1, 0).text = eq['item_type']
                eq_table.cell(i + 1, 1).text = eq['model'] or ''
                eq_table.cell(i + 1, 2).text = eq['notes'] or ''
                eq_table.cell(i + 1, 3).text = eq['status'] or 'pending'
        else:
            equip_table = doc.add_table(rows=8, cols=3)
            equip_table.style = 'Table Grid'
            equip_table.cell(0, 0).text = 'Item'
            equip_table.cell(0, 1).text = 'Specification'
            equip_table.cell(0, 2).text = 'Approved'
            for i, (item, spec) in enumerate([
                ('Laptop/Mac', 'Standard config for role'),
                ('Monitor(s)', 'Dual 24" or single 27"'),
                ('Keyboard & Mouse', 'Standard wireless set'),
                ('Headset', 'USB headset with microphone'),
                ('Docking Station', 'USB-C dock'),
                ('Phone', 'Desk phone or softphone'),
                ('Other', ''),
            ]):
                equip_table.cell(i + 1, 0).text = item
                equip_table.cell(i + 1, 1).text = spec
                equip_table.cell(i + 1, 2).text = '☐'

        doc.add_paragraph('')
        doc.add_heading('Approvals', level=2)
        doc.add_paragraph('Manager: ________________________  Date: ____________')
        doc.add_paragraph('IT Manager: _____________________  Date: ____________')

        buffer = io.BytesIO()
        doc.save(buffer)
        buffer.seek(0)

        filename = f"Equipment_Request_{emp['first_name']}_{emp['last_name']}.docx"
        _audit('document_generated', 'employee', emp_id, {'type': 'equipment_request'})

        return send_file(
            buffer, as_attachment=True, download_name=filename,
            mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        )

    except ImportError:
        return jsonify({'error': 'python-docx is not installed'}), 500
    except Exception as e:
        logger.error(f"Equipment request generation failed: {e}")
        return jsonify({'error': f'Document generation failed: {str(e)}'}), 500
    finally:
        db.close()


@mgr_bp.route('/employees/<int:emp_id>/access-request', methods=['POST'])
@login_required
@role_required(['admin', 'hr', 'manager'])
def generate_access_request(emp_id):
    """Generate an IT access request form as a .docx file."""
    db = get_db()
    try:
        emp = db.execute('SELECT * FROM employees WHERE id = ?', (emp_id,)).fetchone()
        if not emp:
            return jsonify({'error': 'Employee not found'}), 404

        from docx import Document
        from docx.shared import RGBColor
        from docx.enum.text import WD_ALIGN_PARAGRAPH

        doc = Document()
        title = doc.add_heading('IT Access Request Form', level=1)
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        for run in title.runs:
            run.font.color.rgb = RGBColor(0x00, 0x44, 0x7c)

        doc.add_paragraph(f"Date: {date.today().strftime('%B %d, %Y')}")
        doc.add_paragraph('')

        doc.add_heading('Employee Information', level=2)
        table = doc.add_table(rows=5, cols=2)
        table.style = 'Table Grid'
        for i, (label, value) in enumerate([
            ('Employee Name', f"{emp['first_name']} {emp['last_name']}"),
            ('Email', emp['email']),
            ('Department', emp['department']),
            ('Role', emp['role_title']),
            ('Start Date', emp['start_date']),
        ]):
            table.cell(i, 0).text = label
            table.cell(i, 1).text = value or ''

        doc.add_paragraph('')
        doc.add_heading('Access Requested', level=2)

        access_table = doc.add_table(rows=11, cols=3)
        access_table.style = 'Table Grid'
        access_table.cell(0, 0).text = 'System/Application'
        access_table.cell(0, 1).text = 'Access Level'
        access_table.cell(0, 2).text = 'Approved'
        for i, (system, level) in enumerate([
            ('Microsoft 365 (Email, Teams)', 'Standard'),
            ('Active Directory / Entra ID', 'Standard User'),
            ('VPN', 'Standard'),
            ('Salesforce', 'Based on role'),
            ('Slack', 'Standard'),
            ('Zoom', 'Standard'),
            ('GitHub / Source Control', 'Based on role'),
            ('Shared Drives / SharePoint', 'Department access'),
            ('CRM / ERP Systems', 'Based on role'),
            ('Other: _______________', ''),
        ]):
            access_table.cell(i + 1, 0).text = system
            access_table.cell(i + 1, 1).text = level
            access_table.cell(i + 1, 2).text = '☐'

        doc.add_paragraph('')
        doc.add_heading('Approvals', level=2)
        doc.add_paragraph('Manager: ________________________  Date: ____________')
        doc.add_paragraph('IT Security: ____________________  Date: ____________')
        doc.add_paragraph('IT Manager: _____________________  Date: ____________')

        buffer = io.BytesIO()
        doc.save(buffer)
        buffer.seek(0)

        filename = f"Access_Request_{emp['first_name']}_{emp['last_name']}.docx"
        _audit('document_generated', 'employee', emp_id, {'type': 'access_request'})

        return send_file(
            buffer, as_attachment=True, download_name=filename,
            mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        )

    except ImportError:
        return jsonify({'error': 'python-docx is not installed'}), 500
    except Exception as e:
        logger.error(f"Access request generation failed: {e}")
        return jsonify({'error': f'Document generation failed: {str(e)}'}), 500
    finally:
        db.close()


# ── Slack Integration ────────────────────────────────────────────────────────

@mgr_bp.route('/employees/<int:emp_id>/notify-slack', methods=['POST'])
@login_required
@role_required(['admin', 'hr'])
def notify_slack(emp_id):
    """Send a Slack notification about an employee's onboarding/offboarding."""
    db = get_db()
    try:
        emp = db.execute('SELECT * FROM employees WHERE id = ?', (emp_id,)).fetchone()
        if not emp:
            return jsonify({'error': 'Employee not found'}), 404

        data = request.get_json(silent=True) or {}
        channel = data.get('channel', '#hr-onboarding')
        message_type = data.get('type', 'onboarding')

        from .slack_client import SlackClient
        slack = SlackClient()

        if message_type == 'onboarding':
            text = (
                f"🎉 *New Hire Alert*\n"
                f"*{emp['first_name']} {emp['last_name']}* is joining Celito as a "
                f"*{emp['role_title']}* in the *{emp['department']}* department.\n"
                f"📅 Start Date: {emp['start_date']}\n"
                f"📍 Location: {emp.get('location') or 'TBD'}\n"
                f"👤 Manager: {emp['manager_email'] or 'TBD'}"
            )
        elif message_type == 'offboarding':
            term_type = data.get('term_type', 'voluntary')
            if term_type == 'involuntary':
                text = (
                    f"🚨 *URGENT — INVOLUNTARY TERMINATION*\n"
                    f"*Employee:* {emp['first_name']} {emp['last_name']} ({emp['role_title']}, {emp['department']})\n"
                    f"*Last Day:* TODAY\n"
                    f"*Action Required:* All access must be revoked immediately."
                )
                # Also DM sysadmin directly
                sa_email = config.get('team_assignments.sysadmin_email', '')
                if sa_email:
                    try:
                        slack.send_dm(sa_email,
                            f"🚨 URGENT: Immediate access revocation required for "
                            f"{emp['first_name']} {emp['last_name']}. All IT tasks are due NOW.")
                    except Exception as dm_err:
                        logger.warning(f"Failed to DM sysadmin: {dm_err}")
            else:
                text = (
                    f"📋 *Offboarding Notice*\n"
                    f"*{emp['first_name']} {emp['last_name']}* ({emp['role_title']}, "
                    f"{emp['department']}) — Last day: {emp.get('end_date') or 'TBD'}\n"
                    f"*Type:* Voluntary"
                )
        else:
            text = data.get('message', f"Update for {emp['first_name']} {emp['last_name']}")

        result = slack.post_message(channel, text)

        _audit('slack_notification', 'employee', emp_id, {
            'channel': channel, 'type': message_type
        })

        return jsonify({'message': 'Notification sent', 'channel': channel})

    except Exception as e:
        logger.error(f"Slack notification failed: {e}")
        return jsonify({'error': f'Slack notification failed: {str(e)}'}), 500
    finally:
        db.close()


# ── Salesforce Integration ───────────────────────────────────────────────────

@mgr_bp.route('/employees/<int:emp_id>/sync-salesforce', methods=['POST'])
@login_required
@role_required(['admin', 'hr'])
def sync_salesforce(emp_id):
    """Create or update Salesforce Case and Tasks for an employee."""
    db = get_db()
    try:
        emp = db.execute('SELECT * FROM employees WHERE id = ?', (emp_id,)).fetchone()
        if not emp:
            return jsonify({'error': 'Employee not found'}), 404

        from .salesforce_client import SalesforceClient
        sf = SalesforceClient()
        sf.authenticate()

        active_checklist = db.execute(
            "SELECT * FROM checklists WHERE employee_id = ? AND status = 'active' ORDER BY created_at DESC LIMIT 1",
            (emp_id,)
        ).fetchone()

        if not active_checklist:
            return jsonify({'error': 'No active checklist found for this employee'}), 404

        checklist_type = active_checklist['checklist_type']

        if emp['salesforce_case_id']:
            case_id = emp['salesforce_case_id']
            sf.update_record('Case', case_id, {
                'Status': 'In Progress',
                'Description': f"{checklist_type.title()} in progress for {emp['first_name']} {emp['last_name']}"
            })
        else:
            emp_data = {
                'first_name': emp['first_name'],
                'last_name': emp['last_name'],
                'email': emp['email'],
                'role_title': emp['role_title'],
                'department': emp['department'],
                'manager_email': emp.get('manager_email', ''),
                'salesforce_contact_id': emp.get('salesforce_contact_id'),
            }
            if checklist_type == 'onboarding':
                emp_data['start_date'] = emp['start_date']
                case_id = sf.create_onboarding_case(emp_data)
            else:
                emp_data['end_date'] = emp.get('end_date')
                emp_data['offboard_type'] = emp.get('hire_type', 'voluntary')
                emp_data['reason'] = ''
                case_id = sf.create_offboarding_case(emp_data)

            # create_onboarding_case / create_offboarding_case return the Case Id string directly
            db.execute('UPDATE employees SET salesforce_case_id = ? WHERE id = ?', (case_id, emp_id))

        tasks = db.execute(
            'SELECT * FROM tasks WHERE checklist_id = ? AND salesforce_task_id IS NULL AND parent_task_id IS NULL',
            (active_checklist['id'],)
        ).fetchall()

        created_tasks = 0
        for task in tasks:
            try:
                sf_task_id = sf.create_task(case_id, {
                    'subject': task['title'],
                    'description': task.get('description', ''),
                    'due_date': task['due_date'] or '',
                    'priority': 'High' if task.get('is_security_critical') else 'Normal',
                })
                if sf_task_id:
                    db.execute('UPDATE tasks SET salesforce_task_id = ? WHERE id = ?',
                               (sf_task_id, task['id']))
                    created_tasks += 1
            except Exception as e:
                logger.warning(f"Failed to create SF task '{task['title']}': {e}")

        db.commit()

        _audit('salesforce_sync', 'employee', emp_id, {
            'case_id': case_id, 'tasks_created': created_tasks
        })

        return jsonify({
            'message': 'Salesforce sync complete',
            'case_id': case_id,
            'tasks_created': created_tasks
        })

    except Exception as e:
        logger.error(f"Salesforce sync failed: {e}")
        return jsonify({'error': f'Salesforce sync failed: {str(e)}'}), 500
    finally:
        db.close()


# ── Task CRUD (Add / Remove / Duplicate on live checklists) ───────────────

@mgr_bp.route('/employees/<int:emp_id>/tasks', methods=['POST'])
@login_required
@role_required(['admin', 'hr', 'manager'])
def add_task(emp_id):
    """Add a custom task to an employee's active checklist."""
    db = get_db()
    try:
        user = get_current_user()
        emp = db.execute('SELECT * FROM employees WHERE id = ?', (emp_id,)).fetchone()
        if not emp:
            return jsonify({'error': 'Employee not found'}), 404
        if not _can_access_employee(user, emp):
            return jsonify({'error': 'Access denied'}), 403

        # Find the active checklist (prefer onboarding, fall back to offboarding)
        checklist = db.execute(
            'SELECT * FROM checklists WHERE employee_id = ? AND status = ? '
            'ORDER BY CASE checklist_type WHEN \'onboarding\' THEN 0 ELSE 1 END LIMIT 1',
            (emp_id, 'active')
        ).fetchone()
        if not checklist:
            return jsonify({'error': 'No active checklist found for this employee'}), 404

        data = request.get_json(force=True)
        title = (data.get('title') or '').strip()
        if not title:
            return jsonify({'error': 'Title is required'}), 400

        parent_task_id = data.get('parent_task_id')
        if parent_task_id:
            parent = db.execute(
                'SELECT id FROM tasks WHERE id = ? AND checklist_id = ?',
                (parent_task_id, checklist['id'])
            ).fetchone()
            if not parent:
                return jsonify({'error': 'Parent task not found in this checklist'}), 400

        depends_on_task_id = data.get('depends_on_task_id')
        if depends_on_task_id:
            dep = db.execute(
                'SELECT id FROM tasks WHERE id = ? AND checklist_id = ?',
                (depends_on_task_id, checklist['id'])
            ).fetchone()
            if not dep:
                return jsonify({'error': 'Dependency task not found in this checklist'}), 400

        # Determine sort_order (append at end, or after parent's last subtask)
        if parent_task_id:
            max_sort = db.execute(
                'SELECT MAX(sort_order) as mx FROM tasks WHERE checklist_id = ? AND parent_task_id = ?',
                (checklist['id'], parent_task_id)
            ).fetchone()
        else:
            max_sort = db.execute(
                'SELECT MAX(sort_order) as mx FROM tasks WHERE checklist_id = ?',
                (checklist['id'],)
            ).fetchone()
        sort_order = (max_sort['mx'] or 0) + 1 if max_sort else 1

        status = 'pending'
        if depends_on_task_id:
            dep_task = db.execute('SELECT status FROM tasks WHERE id = ?', (depends_on_task_id,)).fetchone()
            if dep_task and dep_task['status'] != 'completed':
                status = 'blocked'

        due_date = data.get('due_date')
        if not due_date and data.get('due_offset_days') is not None:
            due_date = _offset_date(emp['start_date'] or date.today().isoformat(), int(data['due_offset_days']))

        db.execute(
            'INSERT INTO tasks (checklist_id, employee_id, title, description, category, '
            'assigned_to, assigned_email, due_date, status, sort_order, parent_task_id, '
            'depends_on_task_id, phase, due_offset_days, conditions, location_mode, '
            'is_acknowledgment, is_security_critical, compliance_required, urgency) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                checklist['id'], emp_id,
                title,
                data.get('description', ''),
                data.get('category', 'hr'),
                data.get('assigned_to', ''),
                data.get('assigned_email', ''),
                due_date,
                status, sort_order,
                parent_task_id,
                depends_on_task_id,
                data.get('phase', ''),
                data.get('due_offset_days'),
                json.dumps(data['conditions']) if data.get('conditions') else None,
                data.get('location_mode', 'all'),
                1 if data.get('is_acknowledgment') else 0,
                1 if data.get('is_security_critical') else 0,
                1 if data.get('compliance_required') else 0,
                data.get('urgency', 'normal'),
            )
        )
        task_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
        db.commit()

        _audit('task_added', 'task', task_id, {'title': title, 'employee_id': emp_id, 'checklist_id': checklist['id']})

        task = db.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
        return jsonify(dict(task)), 201

    except Exception as e:
        logger.error(f"Failed to add task: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


@mgr_bp.route('/tasks/<int:task_id>', methods=['DELETE'])
@login_required
@role_required(['admin', 'hr', 'manager'])
def delete_task(task_id):
    """Remove a task from a checklist. Only works on incomplete tasks."""
    db = get_db()
    try:
        user = get_current_user()
        task = db.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if not task:
            return jsonify({'error': 'Task not found'}), 404

        emp = db.execute('SELECT * FROM employees WHERE id = ?', (task['employee_id'],)).fetchone()
        if emp and not _can_access_employee(user, emp):
            return jsonify({'error': 'Access denied'}), 403

        if task['status'] == 'completed':
            return jsonify({'error': 'Cannot delete a completed task'}), 400

        # Check subtasks — refuse if any subtask is completed
        subtasks = db.execute(
            'SELECT id, status, title FROM tasks WHERE parent_task_id = ?', (task_id,)
        ).fetchall()
        completed_subs = [s for s in subtasks if s['status'] == 'completed']
        if completed_subs:
            return jsonify({
                'error': 'Cannot delete: some subtasks are already completed',
                'completed_subtasks': [s['title'] for s in completed_subs]
            }), 400

        # Clear dependency references on tasks that depend on this one
        db.execute(
            'UPDATE tasks SET depends_on_task_id = NULL, status = CASE WHEN status = \'blocked\' THEN \'pending\' ELSE status END '
            'WHERE depends_on_task_id = ?', (task_id,)
        )

        # Delete subtasks first, then the task
        deleted_subtask_count = 0
        for sub in subtasks:
            db.execute('DELETE FROM tasks WHERE id = ?', (sub['id'],))
            deleted_subtask_count += 1

        db.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
        db.commit()

        _audit('task_deleted', 'task', task_id, {
            'title': task['title'],
            'employee_id': task['employee_id'],
            'checklist_id': task['checklist_id'],
            'subtasks_deleted': deleted_subtask_count,
        })

        return jsonify({
            'message': 'Task deleted',
            'id': task_id,
            'subtasks_deleted': deleted_subtask_count
        })

    except Exception as e:
        logger.error(f"Failed to delete task: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


@mgr_bp.route('/tasks/<int:task_id>/duplicate', methods=['POST'])
@login_required
@role_required(['admin', 'hr', 'manager'])
def duplicate_task(task_id):
    """Duplicate a task with optional overrides."""
    db = get_db()
    try:
        user = get_current_user()
        task = db.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if not task:
            return jsonify({'error': 'Task not found'}), 404

        emp = db.execute('SELECT * FROM employees WHERE id = ?', (task['employee_id'],)).fetchone()
        if emp and not _can_access_employee(user, emp):
            return jsonify({'error': 'Access denied'}), 403

        data = request.get_json(force=True) if request.is_json else {}

        # Determine sort_order (insert right after the original)
        max_sort = db.execute(
            'SELECT MAX(sort_order) as mx FROM tasks WHERE checklist_id = ?',
            (task['checklist_id'],)
        ).fetchone()
        sort_order = (max_sort['mx'] or 0) + 1

        db.execute(
            'INSERT INTO tasks (checklist_id, employee_id, title, description, category, '
            'assigned_to, assigned_email, due_date, status, sort_order, parent_task_id, '
            'depends_on_task_id, phase, due_offset_days, conditions, location_mode, '
            'is_acknowledgment, is_security_critical, compliance_required, urgency) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                task['checklist_id'],
                task['employee_id'],
                data.get('title', task['title']),
                data.get('description', task['description']),
                data.get('category', task['category']),
                data.get('assigned_to', task['assigned_to']),
                data.get('assigned_email', task['assigned_email']),
                data.get('due_date', task['due_date']),
                'pending',  # always start as pending
                sort_order,
                task['parent_task_id'],  # keep same parent
                None,  # no dependency on the duplicate
                task['phase'],
                task['due_offset_days'],
                task['conditions'],
                task['location_mode'],
                task['is_acknowledgment'],
                task['is_security_critical'],
                task['compliance_required'],
                data.get('urgency', task['urgency']),
            )
        )
        new_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
        db.commit()

        _audit('task_duplicated', 'task', new_id, {
            'original_task_id': task_id,
            'title': data.get('title', task['title']),
            'employee_id': task['employee_id'],
        })

        new_task = db.execute('SELECT * FROM tasks WHERE id = ?', (new_id,)).fetchone()
        return jsonify(dict(new_task)), 201

    except Exception as e:
        logger.error(f"Failed to duplicate task: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


# ── Team Onboarding Grid ────────────────────────────────────────────────────

@mgr_bp.route('/team/onboarding-grid', methods=['GET'])
@login_required
def team_onboarding_grid():
    """
    Card-grid data for a manager's team onboardings.

    Returns per-employee: progress %, phase breakdown, overdue count,
    blockers, current phase, days since start, tasks due this week.
    HR/admin see everyone; managers see their reports.
    """
    user = get_current_user()
    db = get_db()
    today = date.today()
    today_str = today.isoformat()
    week_end = (today + timedelta(days=7)).isoformat()

    try:
        # Determine which employees to show
        if user['role'] in ('admin', 'hr'):
            employees = db.execute(
                "SELECT e.* FROM employees e "
                "WHERE e.status IN ('active', 'onboarding', 'pending') "
                "ORDER BY e.start_date DESC"
            ).fetchall()
        elif user['role'] == 'manager':
            employees = db.execute(
                "SELECT e.* FROM employees e "
                "WHERE e.manager_email = ? AND e.status IN ('active', 'onboarding', 'pending') "
                "ORDER BY e.start_date DESC",
                (user['email'],)
            ).fetchall()
        else:
            return jsonify({'error': 'Access denied'}), 403

        cards = []
        for emp in employees:
            eid = emp['id']

            # Overall progress
            totals = db.execute(
                "SELECT COUNT(*) as total, "
                "SUM(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END) as completed, "
                "SUM(CASE WHEN t.status NOT IN ('completed','skipped') AND t.due_date < ? THEN 1 ELSE 0 END) as overdue "
                "FROM tasks t JOIN checklists c ON t.checklist_id = c.id "
                "WHERE c.employee_id = ? AND c.status = 'active' AND t.parent_task_id IS NULL",
                (today_str, eid)
            ).fetchone()

            total = totals['total'] or 0
            completed = totals['completed'] or 0
            overdue = totals['overdue'] or 0
            pct = round((completed / total * 100) if total > 0 else 0, 1)

            # Phase breakdown
            phases_raw = db.execute(
                "SELECT t.phase, COUNT(*) as total, "
                "SUM(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END) as done "
                "FROM tasks t JOIN checklists c ON t.checklist_id = c.id "
                "WHERE c.employee_id = ? AND c.status = 'active' AND t.parent_task_id IS NULL "
                "GROUP BY t.phase",
                (eid,)
            ).fetchall()
            phases = {}
            for p in phases_raw:
                phases[p['phase'] or 'other'] = {
                    'total': p['total'],
                    'completed': p['done'] or 0
                }

            # Tasks due this week
            week_tasks = db.execute(
                "SELECT COUNT(*) as cnt FROM tasks t "
                "JOIN checklists c ON t.checklist_id = c.id "
                "WHERE c.employee_id = ? AND c.status = 'active' "
                "AND t.status NOT IN ('completed','skipped') "
                "AND t.due_date >= ? AND t.due_date <= ? AND t.parent_task_id IS NULL",
                (eid, today_str, week_end)
            ).fetchone()

            # Blocked tasks
            blocked = db.execute(
                "SELECT t.title FROM tasks t "
                "JOIN checklists c ON t.checklist_id = c.id "
                "WHERE c.employee_id = ? AND c.status = 'active' "
                "AND t.status = 'blocked' AND t.parent_task_id IS NULL",
                (eid,)
            ).fetchall()

            # Buddy info
            buddy_name = ''
            if emp['buddy_email']:
                buddy_row = db.execute(
                    "SELECT display_name FROM users WHERE email = ?",
                    (emp['buddy_email'],)
                ).fetchone()
                buddy_name = buddy_row['display_name'] if buddy_row else emp['buddy_email']

            cards.append({
                'id': eid,
                'first_name': emp['first_name'],
                'last_name': emp['last_name'],
                'email': emp['email'],
                'department': emp['department'] or '',
                'role_title': emp['role_title'] or '',
                'location_mode': emp['location_mode'] or '',
                'start_date': emp['start_date'] or '',
                'status': emp['status'],
                'buddy_name': buddy_name,
                'buddy_email': emp['buddy_email'] or '',
                'progress': pct,
                'total_tasks': total,
                'completed_tasks': completed,
                'overdue_count': overdue,
                'due_this_week': week_tasks['cnt'] or 0,
                'blocked_tasks': [dict(b) for b in blocked],
                'phases': phases,
            })

        # Summary stats
        total_active = len(cards)
        avg_progress = round(sum(c['progress'] for c in cards) / total_active, 1) if total_active else 0
        total_overdue = sum(c['overdue_count'] for c in cards)
        on_track = sum(1 for c in cards if c['overdue_count'] == 0 and c['progress'] > 0)

        return jsonify({
            'cards': cards,
            'summary': {
                'total_active': total_active,
                'avg_progress': avg_progress,
                'total_overdue': total_overdue,
                'on_track': on_track,
            }
        })
    finally:
        db.close()
