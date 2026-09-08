"""
Salesforce API connector for Celito Onboarding Platform.

Uses Client Credentials OAuth 2.0 flow (matching other Celito apps).
API version: v59.0
"""

import logging
import time
from datetime import datetime

import requests

from .config import config

logger = logging.getLogger(__name__)

API_VERSION = "v59.0"


class SalesforceClient:
    """Salesforce REST API client using Client Credentials flow."""

    def __init__(self):
        self._access_token = None
        self._instance_url = None
        self._token_expiry = 0  # epoch seconds

    # ──────────────────────────────────────────────────────────────
    # Authentication
    # ──────────────────────────────────────────────────────────────

    def authenticate(self):
        """
        Obtain an access token via Username-Password OAuth 2.0 flow.
        Caches the token until it expires.
        """
        if self._access_token and time.time() < self._token_expiry:
            return

        domain = config.get("salesforce.domain", "login")
        token_url = f"https://{domain}.salesforce.com/services/oauth2/token"

        payload = {
            "grant_type": "client_credentials",
            "client_id": config.get("salesforce.client_id", ""),
            "client_secret": config.get("salesforce.client_secret", ""),
        }

        resp = requests.post(token_url, data=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        self._access_token = data["access_token"]
        self._instance_url = data["instance_url"]
        # Salesforce tokens typically last 2 hours; refresh at 90 min
        self._token_expiry = time.time() + 5400
        logger.info("Salesforce authenticated — instance: %s", self._instance_url)

    def _headers(self):
        """Return Authorization headers, authenticating if needed."""
        self.authenticate()
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

    def _url(self, path):
        """Build a full Salesforce REST API URL."""
        return f"{self._instance_url}/services/data/{API_VERSION}/{path}"

    # ──────────────────────────────────────────────────────────────
    # Generic CRUD
    # ──────────────────────────────────────────────────────────────

    def query(self, soql):
        """Execute a SOQL query and return the records list."""
        resp = requests.get(
            self._url("query"),
            headers=self._headers(),
            params={"q": soql},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("records", [])

    def create_record(self, sobject, data):
        """
        Create a Salesforce record.
        Returns the new record's Id.
        """
        resp = requests.post(
            self._url(f"sobjects/{sobject}"),
            headers=self._headers(),
            json=data,
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()
        record_id = result.get("id", "")
        logger.info("Created %s: %s", sobject, record_id)
        return record_id

    def update_record(self, sobject, record_id, data):
        """Update an existing Salesforce record by Id."""
        resp = requests.patch(
            self._url(f"sobjects/{sobject}/{record_id}"),
            headers=self._headers(),
            json=data,
            timeout=30,
        )
        resp.raise_for_status()
        logger.info("Updated %s/%s", sobject, record_id)

    # ──────────────────────────────────────────────────────────────
    # Onboarding / Offboarding helpers
    # ──────────────────────────────────────────────────────────────

    def create_onboarding_case(self, employee_data):
        """
        Create a Case in Salesforce for a new hire onboarding.

        Args:
            employee_data: dict with keys first_name, last_name, email,
                           role_title, department, start_date, manager_email

        Returns:
            The new Case Id.
        """
        name = f"{employee_data['first_name']} {employee_data['last_name']}"
        case_data = {
            "Subject": f"Onboarding — {name}",
            "Description": (
                f"New hire onboarding for {name}\n"
                f"Role: {employee_data.get('role_title', '')}\n"
                f"Department: {employee_data.get('department', '')}\n"
                f"Start Date: {employee_data.get('start_date', '')}\n"
                f"Manager: {employee_data.get('manager_email', '')}"
            ),
            "Type": "Onboarding",
            "Status": "New",
            "Priority": "Medium",
            "Origin": "Web",
        }

        # Link to Contact if we have a Salesforce contact Id
        contact_id = employee_data.get("salesforce_contact_id")
        if contact_id:
            case_data["ContactId"] = contact_id

        return self.create_record("Case", case_data)

    def create_offboarding_case(self, employee_data):
        """
        Create a Case for an employee offboarding.

        Args:
            employee_data: dict with first_name, last_name, email,
                           end_date, offboard_type (voluntary/involuntary)

        Returns:
            The new Case Id.
        """
        name = f"{employee_data['first_name']} {employee_data['last_name']}"
        offboard_type = employee_data.get("offboard_type", "voluntary")
        priority = "High" if offboard_type == "involuntary" else "Medium"

        case_data = {
            "Subject": f"Offboarding — {name} ({offboard_type})",
            "Description": (
                f"Employee offboarding for {name}\n"
                f"Type: {offboard_type}\n"
                f"Last Day: {employee_data.get('end_date', '')}\n"
                f"Reason: {employee_data.get('reason', '')}"
            ),
            "Type": "Offboarding",
            "Status": "New",
            "Priority": priority,
            "Origin": "Web",
        }

        contact_id = employee_data.get("salesforce_contact_id")
        if contact_id:
            case_data["ContactId"] = contact_id

        return self.create_record("Case", case_data)

    def create_task(self, case_id, task_data):
        """
        Create a Task in Salesforce linked to a Case.

        Args:
            case_id: The parent Case Id
            task_data: dict with subject, description, due_date, assigned_to

        Returns:
            The new Task Id.
        """
        sf_task = {
            "WhatId": case_id,
            "Subject": task_data.get("subject", ""),
            "Description": task_data.get("description", ""),
            "ActivityDate": task_data.get("due_date", ""),
            "Status": "Not Started",
            "Priority": task_data.get("priority", "Normal"),
        }

        # Owner assignment (if we have a Salesforce User Id)
        owner_id = task_data.get("owner_id")
        if owner_id:
            sf_task["OwnerId"] = owner_id

        return self.create_record("Task", sf_task)
