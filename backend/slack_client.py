"""
Slack API connector for Celito Onboarding Platform.

Uses raw HTTP requests (no slack_sdk dependency) with bot and user tokens.
Handles rate limiting (429) with retry-after headers.
"""

import logging
import time

import requests

from .config import config

logger = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"


class SlackClient:
    """Slack Web API client using bot token (and optional user token)."""

    def __init__(self):
        self._user_cache = {}  # email -> slack user id

    # ──────────────────────────────────────────────────────────────
    # Low-level request helper
    # ──────────────────────────────────────────────────────────────

    def _call(self, method, endpoint, token_type="bot", **kwargs):
        """
        Make a Slack API call with automatic rate-limit retry.

        Args:
            method: "GET" or "POST"
            endpoint: Slack API method name (e.g. "chat.postMessage")
            token_type: "bot" or "user"
            **kwargs: passed to requests (json=, params=, files=, data=)

        Returns:
            Parsed JSON response dict.

        Raises:
            requests.HTTPError on non-2xx status after retries.
        """
        token_key = "slack.bot_token" if token_type == "bot" else "slack.user_token"
        token = config.get(token_key, "")
        if not token:
            logger.warning("Slack %s token not configured", token_type)
            return {"ok": False, "error": "token_not_configured"}

        url = f"{SLACK_API}/{endpoint}"
        headers = {"Authorization": f"Bearer {token}"}

        max_retries = 3
        for attempt in range(max_retries):
            if method.upper() == "GET":
                resp = requests.get(url, headers=headers, params=kwargs.get("params"), timeout=15)
            else:
                if "files" in kwargs:
                    resp = requests.post(url, headers=headers, data=kwargs.get("data", {}),
                                         files=kwargs["files"], timeout=30)
                else:
                    resp = requests.post(url, headers=headers, json=kwargs.get("json", {}), timeout=15)

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 5))
                logger.warning("Slack rate limited — retrying in %ds (attempt %d/%d)",
                               retry_after, attempt + 1, max_retries)
                time.sleep(retry_after)
                continue

            resp.raise_for_status()
            data = resp.json()

            if not data.get("ok"):
                logger.error("Slack API error on %s: %s", endpoint, data.get("error"))

            return data

        # Exhausted retries
        logger.error("Slack API %s: max retries exceeded", endpoint)
        return {"ok": False, "error": "max_retries_exceeded"}

    # ──────────────────────────────────────────────────────────────
    # User lookup
    # ──────────────────────────────────────────────────────────────

    def lookup_user_by_email(self, email):
        """
        Look up a Slack user ID by email address.
        Results are cached for the lifetime of this client instance.
        """
        if email in self._user_cache:
            return self._user_cache[email]

        data = self._call("GET", "users.lookupByEmail", params={"email": email})
        if data.get("ok"):
            user_id = data["user"]["id"]
            self._user_cache[email] = user_id
            return user_id

        logger.warning("Could not find Slack user for email: %s", email)
        return None

    # ──────────────────────────────────────────────────────────────
    # Messaging
    # ──────────────────────────────────────────────────────────────

    def post_message(self, channel, text, blocks=None):
        """
        Post a message to a Slack channel.

        Args:
            channel: Channel ID or name (e.g. "#hr-onboarding" or "C0123ABC")
            text: Fallback text (shown in notifications)
            blocks: Optional Block Kit blocks list
        """
        payload = {"channel": channel, "text": text}
        if blocks:
            payload["blocks"] = blocks
        return self._call("POST", "chat.postMessage", json=payload)

    def send_dm(self, user_email, text, blocks=None):
        """
        Send a direct message to a user by their email address.
        Opens a DM channel first, then posts the message.
        """
        user_id = self.lookup_user_by_email(user_email)
        if not user_id:
            return {"ok": False, "error": f"user_not_found: {user_email}"}

        # Open DM conversation
        conv = self._call("POST", "conversations.open", json={"users": user_id})
        if not conv.get("ok"):
            return conv

        dm_channel = conv["channel"]["id"]
        payload = {"channel": dm_channel, "text": text}
        if blocks:
            payload["blocks"] = blocks
        return self._call("POST", "chat.postMessage", json=payload)

    # ──────────────────────────────────────────────────────────────
    # File uploads
    # ──────────────────────────────────────────────────────────────

    def upload_file(self, channel, file_path, title=None):
        """Upload a file to a Slack channel."""
        with open(file_path, "rb") as f:
            return self._call(
                "POST",
                "files.upload",
                data={"channels": channel, "title": title or ""},
                files={"file": f},
            )

    # ──────────────────────────────────────────────────────────────
    # Channel management
    # ──────────────────────────────────────────────────────────────

    def join_channel(self, channel_id):
        """Have the bot join a channel."""
        return self._call("POST", "conversations.join", json={"channel": channel_id})

    def list_channels(self, limit=200):
        """List public channels in the workspace."""
        data = self._call(
            "GET", "conversations.list",
            params={"types": "public_channel", "limit": limit, "exclude_archived": "true"},
        )
        return data.get("channels", []) if data.get("ok") else []

    def create_channel(self, name):
        """
        Create a new public channel.

        Args:
            name: Channel name (lowercase, no spaces, max 80 chars)

        Returns:
            Channel info dict or error.
        """
        return self._call("POST", "conversations.create", json={"name": name})
