"""
Microsoft Teams connector for Celito Onboarding Platform.

Two messaging paths:
  1. Incoming Webhook  — post to a specific Teams channel (simple, no auth)
  2. Graph API DM      — send 1:1 chat messages between two real users
                         (requires Chat.ReadWrite.All application permission)

The webhook path works out of the box with just a URL.
The DM path reuses your Entra ID app registration (tenant_id, client_id, client_secret)
and requires a "sender" user (e.g. the HR service account or the bot account email)
configured as teams.sender_email in settings.json.
"""

import logging
import time

import requests

from .config import config

logger = logging.getLogger(__name__)

GRAPH_API = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"


class TeamsClient:
    """Microsoft Teams messaging client."""

    def __init__(self):
        self._access_token = None
        self._token_expiry = 0
        self._user_id_cache = {}  # email -> Graph user id

    # ──────────────────────────────────────────────────────────────
    # Graph API authentication (Client Credentials flow)
    # ──────────────────────────────────────────────────────────────

    def _get_graph_token(self):
        """Obtain a Graph API access token using client credentials."""
        if self._access_token and time.time() < self._token_expiry:
            return self._access_token

        tenant_id = config.get("teams.tenant_id") or config.get("entra.tenant_id", "")
        client_id = config.get("teams.client_id") or config.get("entra.client_id", "")
        client_secret = config.get("teams.client_secret") or config.get("entra.client_secret", "")

        if not all([tenant_id, client_id, client_secret]):
            raise ValueError("Teams Graph API credentials not configured (tenant_id, client_id, client_secret)")

        resp = requests.post(
            TOKEN_URL.format(tenant=tenant_id),
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        self._access_token = data["access_token"]
        self._token_expiry = time.time() + data.get("expires_in", 3600) - 120
        logger.info("Teams Graph API authenticated")
        return self._access_token

    def _graph_headers(self):
        """Return auth headers for Graph API calls."""
        token = self._get_graph_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    # ──────────────────────────────────────────────────────────────
    # User lookup
    # ──────────────────────────────────────────────────────────────

    def lookup_user_id(self, email):
        """Look up a Microsoft Graph user ID by email address."""
        email_lower = email.lower()
        if email_lower in self._user_id_cache:
            return self._user_id_cache[email_lower]

        resp = requests.get(
            f"{GRAPH_API}/users/{email}",
            headers=self._graph_headers(),
            timeout=15,
        )
        if resp.status_code == 404:
            raise ValueError(f"Teams user not found: {email}")
        resp.raise_for_status()
        user_id = resp.json()["id"]
        self._user_id_cache[email_lower] = user_id
        return user_id

    # ──────────────────────────────────────────────────────────────
    # Webhook messaging (channel posts)
    # ──────────────────────────────────────────────────────────────

    def post_webhook(self, text, webhook_url=None):
        """
        Post a message to a Teams channel via Incoming Webhook.

        Args:
            text: Message text (supports basic markdown)
            webhook_url: Override webhook URL (defaults to config)

        Raises:
            RuntimeError: if webhook is not configured or the post fails.
        """
        url = webhook_url or config.get("teams.webhook_url", "")
        if not url:
            raise RuntimeError("Teams webhook URL not configured")

        # Adaptive Card for rich formatting
        payload = {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "contentUrl": None,
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": [
                            {
                                "type": "TextBlock",
                                "text": text,
                                "wrap": True,
                                "size": "Default",
                            }
                        ],
                    },
                }
            ],
        }

        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code not in (200, 202):
            raise RuntimeError(f"Teams webhook failed: HTTP {resp.status_code}")

        logger.info("Teams webhook message posted")

    # ──────────────────────────────────────────────────────────────
    # Graph API DM messaging
    # ──────────────────────────────────────────────────────────────

    def send_dm(self, user_email, text):
        """
        Send a 1:1 chat message to a user via Graph API.

        Uses a real "sender" user (configured as teams.sender_email) to
        create a chat with the recipient.  Requires Chat.ReadWrite.All
        and User.Read.All application permissions.

        Falls back to webhook channel post (tagged with the user's email)
        if Graph DM fails.

        Raises:
            RuntimeError: if neither Graph DM nor webhook succeeds.
        """
        sender_email = config.get("teams.sender_email", "")
        graph_ok = False

        # ── Try Graph API DM first ────────────────────────────────
        if sender_email:
            try:
                sender_id = self.lookup_user_id(sender_email)
                recipient_id = self.lookup_user_id(user_email)

                # Create (or get existing) 1:1 chat between two real users
                chat_payload = {
                    "chatType": "oneOnOne",
                    "members": [
                        {
                            "@odata.type": "#microsoft.graph.aadUserConversationMember",
                            "roles": ["owner"],
                            "user@odata.bind": f"https://graph.microsoft.com/v1.0/users('{sender_id}')",
                        },
                        {
                            "@odata.type": "#microsoft.graph.aadUserConversationMember",
                            "roles": ["owner"],
                            "user@odata.bind": f"https://graph.microsoft.com/v1.0/users('{recipient_id}')",
                        },
                    ],
                }

                resp = requests.post(
                    f"{GRAPH_API}/chats",
                    headers=self._graph_headers(),
                    json=chat_payload,
                    timeout=15,
                )

                if resp.status_code in (200, 201):
                    chat_id = resp.json()["id"]

                    # Send the message in that chat
                    msg_resp = requests.post(
                        f"{GRAPH_API}/chats/{chat_id}/messages",
                        headers=self._graph_headers(),
                        json={
                            "body": {
                                "contentType": "html",
                                "content": _markdown_to_html(text),
                            }
                        },
                        timeout=15,
                    )

                    if msg_resp.status_code in (200, 201):
                        logger.info("Teams DM sent to %s (from %s)", user_email, sender_email)
                        graph_ok = True
                    else:
                        logger.warning(
                            "Teams DM message send failed (%s %s) — will try webhook",
                            msg_resp.status_code, msg_resp.text[:200],
                        )
                else:
                    logger.warning(
                        "Teams chat creation failed (%s %s) — will try webhook",
                        resp.status_code, resp.text[:200],
                    )

            except Exception as e:
                logger.warning("Teams Graph DM failed for %s: %s — will try webhook", user_email, e)

        if graph_ok:
            return

        # ── Fall back to webhook ──────────────────────────────────
        webhook_url = config.get("teams.webhook_url", "")
        if not webhook_url:
            hint = ""
            if not sender_email:
                hint = " (Hint: set teams.sender_email in settings.json for Graph API DMs)"
            raise RuntimeError(
                f"Teams: could not DM {user_email} — Graph API DM "
                f"{'failed' if sender_email else 'not configured (no sender_email)'} "
                f"and no webhook_url is set.{hint}"
            )

        # Post to channel with the recipient tagged
        logger.info("Falling back to Teams webhook for %s", user_email)
        self.post_webhook(f"**@{user_email}**\n\n{text}")

    # ──────────────────────────────────────────────────────────────
    # Connection test
    # ──────────────────────────────────────────────────────────────

    def test_connection(self):
        """Test Teams connectivity. Returns a dict with status info."""
        results = {"webhook": False, "graph_api": False}

        # Test webhook
        webhook_url = config.get("teams.webhook_url", "")
        if webhook_url:
            results["webhook"] = True
            results["webhook_url_set"] = True
        else:
            results["webhook_url_set"] = False

        # Test Graph API
        try:
            self._get_graph_token()
            results["graph_api"] = True
            results["graph_token"] = True
        except Exception as e:
            results["graph_api"] = False
            results["graph_error"] = str(e)

        # Test sender lookup
        sender_email = config.get("teams.sender_email", "")
        if sender_email and results["graph_api"]:
            try:
                self.lookup_user_id(sender_email)
                results["sender_ok"] = True
            except Exception as e:
                results["sender_ok"] = False
                results["sender_error"] = str(e)
        else:
            results["sender_ok"] = False
            if not sender_email:
                results["sender_error"] = "teams.sender_email not set — Graph DMs disabled"

        results["connected"] = results["webhook"] or results["graph_api"]
        results["dm_capable"] = results["graph_api"] and results.get("sender_ok", False)
        return results


def _markdown_to_html(text):
    """
    Convert Slack-style markdown to basic HTML for Teams messages.
    Handles *bold*, _italic_, ~strike~, `code`, and newlines.
    """
    import re

    html = text
    # Bold: *text* → <b>text</b>
    html = re.sub(r'\*([^*]+)\*', r'<b>\1</b>', html)
    # Italic: _text_ → <i>text</i>
    html = re.sub(r'(?<!\w)_([^_]+)_(?!\w)', r'<i>\1</i>', html)
    # Strikethrough: ~text~ → <s>text</s>
    html = re.sub(r'~([^~]+)~', r'<s>\1</s>', html)
    # Code: `text` → <code>text</code>
    html = re.sub(r'`([^`]+)`', r'<code>\1</code>', html)
    # Slack links: <url|text> → <a href="url">text</a>
    html = re.sub(r'<(https?://[^|>]+)\|([^>]+)>', r'<a href="\1">\2</a>', html)
    # Plain URLs: <url> → <a href="url">url</a>
    html = re.sub(r'<(https?://[^>]+)>', r'<a href="\1">\1</a>', html)
    # Newlines
    html = html.replace('\n', '<br>')
    # Bullet points
    html = re.sub(r'<br>\s*•\s*', '<br>• ', html)

    return html
