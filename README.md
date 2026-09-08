# Celito Employee Onboarding & Offboarding Platform

A web-based platform for managing employee onboarding and offboarding workflows at Celito Communications, Inc.

## Features

### Onboarding
- **4-Phase Onboarding** — Pre-boarding → Company-wide → Department → Role-specific
- **80+ Auto-Generated Tasks** — Phase-aware with due dates, dependencies, subtasks, and conditional logic
- **Pre-boarding Employee Tasks** — Welcome video, bio form, Day 1 guide, business overview, profile photo
- **Remote/Hybrid Support** — Location-specific tasks (VPN setup, home office checklist, A/V test, async norms)
- **Equipment Tracking** — Full lifecycle: pending → ordered → shipped → delivered → issued → returned
- **Buddy Program** — Auto-generated buddy tasks, Slack notification on assignment
- **Onboarding Cohorts** — Auto-assigned by start week, Slack channel creation
- **Meet & Greet Circuit** — Auto-populated contacts with Outlook calendar scheduling links
- **Department-Specific Milestones** — Auto-generated 30/60/90 day plans per department
- **AI-Powered Milestones** — Claude AI generates custom milestones based on role context
- **Day 90 Celebration** — Journey stats, Slack #general announcement, employee DM
- **Pulse Surveys** — Auto-scheduled at Day 7/30/90 with star ratings and text responses

### Offboarding
- **Comprehensive Checklists** — HR, SysAdmin, TSM, CSM, Final Disposition categories
- **Involuntary Urgency** — All IT tasks set to immediate, due today, urgent Slack DMs
- **Access Revocation** — VPN, Mimecast, Slack, Teams, and all system access tracking
- **Salesforce Integration** — Automatic case and task creation

### Dashboard & Reports
- **Role-Specific Dashboards** — HR sees company-wide metrics; managers see their team only
- **Manager Dashboard** — Team onboardings, pending tasks, upcoming milestones, overdue alerts
- **Pre-boarding Readiness** — IT/Equipment/Admin readiness per upcoming hire
- **Trend Metrics** — Monthly snapshots with 6-month Canvas line charts (hiring, time-to-ramp, retention, satisfaction)
- **Phase Bottleneck Analysis** — Identifies where onboardings stall
- **CSV Export** — Employees and tasks exportable

### Employee Self-Service
- **My Onboarding** — Phase stepper, progress tracking, task completion
- **Progress Split** — "Your Progress" (employee tasks) vs "Overall Onboarding" (all teams)
- **This Week's Focus** — CEO's "don't cram" philosophy in action
- **Navigation Guide** — Location-aware systems map, who-to-ask directory, glossary, quick tips
- **Milestones, Surveys, Meet & Greets** — Self-service views with status tracking
- **Task Comments** — Add comments and flag blockers (notifies manager via Slack)
- **Buddy Self-Service** — View assignments and complete buddy tasks

### Administration
- **Role-Based Access Control** — Admin, HR, Manager, Employee, Disabled roles
- **Template Management** — Create, edit, duplicate checklist templates per department
- **User Management** — Add/remove users, change roles, deactivate accounts
- **Audit Log** — Searchable/filterable by user, action, date range with pagination
- **Task Reassignment** — Reassign tasks with Slack notification to new assignee
- **Admin Impersonation** — View the platform as any user role
- **Integration Testing** — Test Salesforce, Slack, and Claude API connections

### Integrations
- **Microsoft Entra ID SSO** — Single sign-on via "Sign in with Microsoft"
- **Salesforce** — Cases and Tasks created automatically (Client Credentials OAuth 2.0)
- **Slack** — Channel notifications, DMs to managers/new hires/buddies, celebration posts
- **Claude AI** — AI-generated milestones and checklists (claude-sonnet-4-6)

## Tech Stack

- **Backend:** Python 3.14 + Flask + Waitress
- **Frontend:** Single HTML file with embedded CSS/JS (no build step)
- **Database:** SQLite with WAL mode (15 tables)
- **Auth:** Microsoft Entra ID via MSAL
- **Server:** IIS reverse proxy → Waitress on localhost:8780
- **Charts:** Pure HTML5 Canvas (no external libraries)

## Quick Start

### 1. Install Dependencies
```batch
install_dependencies.bat
```

### 2. Configure Settings
```batch
copy config\settings.json.example config\settings.json
```
Edit `config\settings.json` with your credentials:
- Entra ID (client_id, client_secret, tenant_id)
- Salesforce (client_id, client_secret)
- Slack (bot_token, user_token)
- Anthropic (api_key)
- Team Assignments (sysadmin_email, servicedesk_email, hr_email, etc.)

### 3. Start the Server
```batch
start_onboard.bat
```
Or directly:
```bash
python backend/server.py
```

The server runs on `http://127.0.0.1:8780` by default.

### 4. First Login
1. Navigate to `https://onboard.celito.net` (or localhost:8780 for dev)
2. Sign in with your Microsoft account
3. The first user is automatically assigned the `admin` role
4. Use the Admin panel to add other users and assign roles

## Directory Structure

```
HR/
├── backend/
│   ├── __init__.py            # Package marker
│   ├── app.py                 # Flask app factory, blueprint registration
│   ├── auth.py                # Entra ID SSO, login/role decorators
│   ├── config.py              # Configuration loader (JSON + env var overrides)
│   ├── db.py                  # Database schema (15 tables), migrations, helpers
│   ├── server.py              # Waitress entry point, daily log rotation
│   ├── routes_admin.py        # Admin API: users, settings, audit, stats, templates, metrics
│   ├── routes_mgr.py          # Manager/HR API: employees, tasks, onboard/offboard, milestones
│   ├── routes_emp.py          # Employee self-service API: profile, tasks, surveys, nav guide
│   ├── salesforce_client.py   # Salesforce Client Credentials OAuth 2.0
│   ├── slack_client.py        # Slack HTTP-only client with rate limiting
│   └── templates/
│       ├── login.html         # Microsoft SSO login page
│       └── 403.html           # Access denied page
├── frontend/
│   └── celito_onboarding_dashboard.html  # Single-file dashboard (all views)
├── config/
│   ├── settings.json          # (gitignored) API keys and config
│   └── settings.json.example  # Template with all config keys
├── logs/                      # Auto-created, daily rotation
├── start_onboard.bat          # Task Scheduler start script
├── stop_onboard.bat           # Task Scheduler stop script
├── install_dependencies.bat   # One-click pip install
├── requirements.txt           # flask, waitress, msal, requests, python-docx
└── README.md
```

## Database Tables

| Table | Purpose |
|---|---|
| users | Platform users with roles |
| employees | Employee records with onboarding/offboarding state |
| checklists | Onboarding/offboarding checklist containers |
| tasks | Individual tasks with phases, dependencies, subtasks |
| task_comments | Comments on tasks with blocker flagging |
| audit_log | All platform actions with timestamps |
| app_settings | Application configuration |
| onboarding_cohorts | Weekly cohort groupings |
| equipment | Equipment tracking with lifecycle status |
| milestones | 30/60/90 day milestones per employee |
| checklist_templates | Reusable checklist templates |
| surveys | Pulse surveys (Day 7/30/90) |
| survey_responses | Survey answers with ratings |
| meet_greets | Meet-and-greet circuit with contact info |
| buddy_tasks | Buddy program task tracking |
| metric_snapshots | Monthly metric snapshots for trend charts |

## Production Deployment (Azure VM)

### IIS Reverse Proxy
1. Create a new IIS site for `onboard.celito.net`
2. Install URL Rewrite and ARR modules
3. Configure reverse proxy: `https://onboard.celito.net` → `http://localhost:8780`
4. Bind SSL certificate in IIS

### Task Scheduler
Create two scheduled tasks:

**Start Server:**
- Trigger 1: At system startup
- Trigger 2: Daily at 6:15 AM
- Action: Run `start_onboard.bat`
- Run whether user is logged on or not

**Stop Server:**
- Trigger: Daily at 7:45 PM
- Action: Run `stop_onboard.bat`
- Run whether user is logged on or not

## Environment Variables (Optional Overrides)

Environment variables override `settings.json` values. Use uppercase with underscores:

| Variable | Overrides |
|---|---|
| `ENTRA_CLIENT_ID` | `entra.client_id` |
| `ENTRA_CLIENT_SECRET` | `entra.client_secret` |
| `ENTRA_TENANT_ID` | `entra.tenant_id` |
| `SALESFORCE_CLIENT_ID` | `salesforce.client_id` |
| `SLACK_BOT_TOKEN` | `slack.bot_token` |
| `ANTHROPIC_API_KEY` | `anthropic.api_key` |

## Security

- **CSRF Protection** — All POST/PUT/DELETE require `X-Requested-With: XMLHttpRequest` header
- **XSS Prevention** — All user data escaped with `esc()` helper before rendering
- **Session Security** — 12-hour expiry, secret key persisted in SQLite
- **Security Headers** — HSTS, CSP, X-Frame-Options: DENY, X-Content-Type-Options: nosniff
- **SQL Injection** — Parameterized queries throughout (? placeholders)
- **Open Redirect** — `_is_safe_redirect()` validates all redirect URLs
- **Admin Impersonation** — Available via `?as=Employee+Name` URL parameter (admin only)

## API Overview

### Admin Routes (`/api/admin/*`)
- `GET/POST/PUT /users` — User management
- `GET/PUT /settings` — Application settings
- `GET /audit-log` — Filterable audit log with pagination
- `GET /audit-log/actions` — Distinct action types for filter dropdown
- `GET /stats` — Dashboard statistics (14 metrics)
- `GET/POST/PUT/DELETE /templates` — Checklist template CRUD
- `GET /metrics/trends` — 6-month trend data for charts
- `POST /metrics/snapshot` — Manual metric snapshot
- `GET /cohorts` — Cohort management
- `GET/POST /surveys` — Survey management
- `GET /equipment` — Equipment summary
- `POST /test-*` — Integration connection tests

### Manager/HR Routes (`/api/*`)
- `GET/POST /employees` — Employee list and creation
- `GET /employees/<id>` — Employee detail with checklists
- `POST /employees/<id>/onboard` — Initiate onboarding (generates 80+ tasks)
- `POST /employees/<id>/offboard` — Initiate offboarding
- `PUT/DELETE /tasks/<id>` — Task update (status, assignee) and deletion
- `POST /tasks/<id>/duplicate` — Duplicate a task
- `POST /employees/<id>/milestones/generate-ai` — AI milestone generation
- `GET /dashboard` — Role-aware dashboard (company-wide or manager-specific)
- `POST /employees/bulk` — Bulk import (max 50)

### Employee Routes (`/api/me/*`)
- `GET /profile` — Employee profile with buddy, HR owner, cohort
- `GET /onboarding` — Onboarding tasks grouped by phase with progress split
- `POST /tasks/<id>/toggle` — Complete/uncomplete own tasks
- `POST /tasks/<id>/comment` — Add comment with blocker flag
- `GET /milestones` — Personal milestones
- `GET /meet-greets` — Meet-and-greet circuit with scheduling links
- `GET /surveys` — Pulse surveys
- `GET /navigation-guide` — Location-aware navigation guide
- `GET /buddy` — Buddy information and tasks

## License

Proprietary — Celito Communications, Inc. Internal use only.
