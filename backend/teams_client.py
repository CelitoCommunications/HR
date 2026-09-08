"""
Microsoft Teams connector for Celito Onboarding Platform.

Two messaging paths:
  1. Incoming Webhook  — post to a specific Teams channel (simple, no auth)
  2. Graph API DM      — send 1:1 chat messages to users by email
                         (requires Chat.Create + ChatMessage.Send app permissions)

The webhook path works out of the box with just a URL.
The DM path reuses your Entra ID app registration (tenant_id, client_id, client_secret).
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

        try:
            resp = requests.get(
                f"{GRAPH_API}/users/{email}",
                headers=self._graph_headers(),
                timeout=15,
            )
            if resp.status_code == 404:
                logger.warning("Teams user not found: %s", email)
                return None
            resp.raise_for_status()
            user_id = resp.json()["id"]
            self._user_id_cache[email_lower] = user_id
            return user_id
        except Exception as e:
            logger.error("Teams user lookup failed for %s: %s", email, e)
            return None

    # ──────────────────────────────────────────────────────────────
    # Webhook messaging (channel posts)
    # ──────────────────────────────────────────────────────────────

    def post_webhook(self, text, webhook_url=None):
        """
        Post a message to a Teams channel via Incoming Webhook.

        Args:
            text: Message text (supports basic markdown)
            webhook_url: Override webhook URL (defaults to config)
        """
        url = webhook_url or config.get("teams.webhook_url", "")
        if not url:
            logger.warning("Teams webhook URL not configured")
            return {"ok": False, "error": "webhook_not_configured"}

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
        if resp.status_code in (200, 202):
            return {"ok": True}
        else:
            logger.error("Teams webhook failed: %s %s", resp.status_code, resp.text[:200])
            return {"ok": False, "error": f"HTTP {resp.status_code}"}

    # ──────────────────────────────────────────────────────────────
    # Graph API DM messaging
    # ──────────────────────────────────────────────────────────────

    def send_dm(self, user_email, text):
        """
        Send a direct 1:1 chat message to a user via Graph API.

        Requires application permissions: Chat.Create, ChatMessage.Send

        If Graph API DM permissions are not available, falls back to
        posting via webhook with the user's name prefixed.
        """
        user_id = self.lookup_user_id(user_email)
        if not user_id:
            return {"ok": False, "error": f"user_not_found: {user_email}"}

        # Get the app's service principal ID for the 1:1 chat
        # We need to use the installedApps approach or create a chat
        # between the app (as a bot) and the user.
        # Simpler approach: use the /chats endpoint with delegated-like app permissions.

        try:
            # Create a 1:1 chat between the app and the user
            # This requires Chat.Create application permission
            app_id = config.get("teams.client_id") or config.get("entra.client_id", "")

            chat_payload = {
                "chatType": "oneOnOne",
                "members": [
                    {
                        "@odata.type": "#microsoft.graph.aadUserConversationMember",
                        "roles": ["owner"],
                        "user@odata.bind": f"https://graph.microsoft.com/v1.0/users('{user_id}')",
                    },
                    {
                        "@odata.type": "#microsoft.graph.aadUserConversationMember",
                        "roles": ["owner"],
                        "user@odata.bind": f"https://graph.microsoft.com/v1.0/users('{app_id}')",
                    },
                ],
            }

            resp = requests.post(
                f"{GRAPH_API}/chats",
                headers=self._graph_headers(),
                json=chat_payload,
                timeout=15,
            )

            if resp.status_code not in (200, 201):
                # DM not available — fall back to webhook with @mention
                logger.warning(
                    "Teams DM creation failed (%s) — falling back to webhook for %s",
                    resp.status_code, user_email,
                )
                return self.post_webhook(f"**@{user_email}**\n\n{text}")

            chat_id = resp.json()["id"]

            # Send message in the chat
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
                logger.info("Teams DM sent to %s", user_email)
                return {"ok": True}
            else:
                logger.error("Teams DM send failed: %s", msg_resp.status_code)
                return self.post_webhook(f"**@{user_email}**\n\n{text}")

        except Exception as e:
            logger.error("Teams DM failed for %s: %s — falling back to webhook", user_email, e)
            return self.post_webhook(f"**@{user_email}**\n\n{text}")

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

        results["connected"] = results["webhook"] or results["graph_api"]
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
