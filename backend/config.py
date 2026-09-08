"""
Configuration loader for Celito Onboarding Platform.

Reads settings from config/settings.json (relative to project root).
Environment variables override settings.json values.
Env var names: dots replaced with underscores, uppercased.
  e.g. entra.client_id -> ENTRA_CLIENT_ID
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG_PATH = os.path.join(PROJECT_DIR, "config", "settings.json")

# Default values for keys that need them
DEFAULTS = {
    "server.port": 8780,
    "server.host": "127.0.0.1",
    "anthropic.model": "claude-sonnet-4-6",
    "salesforce.domain": "login",
}


class Config:
    """Application configuration backed by settings.json and environment variables."""

    def __init__(self, config_path=None):
        self._settings = {}
        self._path = config_path or CONFIG_PATH
        self.reload()

    def reload(self):
        """Reload settings from disk. Safe to call at any time."""
        if not os.path.isfile(self._path):
            logger.warning("Settings file not found: %s — using defaults only", self._path)
            self._settings = {}
            return

        try:
            with open(self._path, "r", encoding="utf-8") as f:
                raw = f.read()
            self._settings = json.loads(raw)
            logger.info("Loaded settings from %s", self._path)
        except json.JSONDecodeError as exc:
            logger.error(
                "JSON parse error in %s (line %s, col %s): %s — "
                "discarding all settings, falling back to defaults",
                self._path, exc.lineno, exc.colno, exc.msg,
            )
            self._settings = {}
        except OSError as exc:
            logger.error("Could not read %s: %s", self._path, exc)
            self._settings = {}

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def get(self, dotted_key, default=None):
        """
        Retrieve a config value.

        Lookup order:
          1. Environment variable (dots → underscores, uppercased)
          2. Nested key in settings.json  (e.g. "entra.client_id" → settings["entra"]["client_id"])
          3. DEFAULTS dict
          4. Caller-supplied default

        Returns the first non-None hit.
        """
        # 1. Env var
        env_key = dotted_key.replace(".", "_").upper()
        env_val = os.environ.get(env_key)
        if env_val is not None:
            return self._coerce(env_val, dotted_key)

        # 2. settings.json (walk nested dicts)
        parts = dotted_key.split(".")
        node = self._settings
        for part in parts:
            if isinstance(node, dict):
                node = node.get(part)
            else:
                node = None
                break
        if node is not None:
            return node

        # 3. Built-in defaults
        if dotted_key in DEFAULTS:
            return DEFAULTS[dotted_key]

        # 4. Caller default
        return default

    def _coerce(self, value, key):
        """Coerce env-var string to int when the default is int."""
        if key in DEFAULTS and isinstance(DEFAULTS[key], int):
            try:
                return int(value)
            except (ValueError, TypeError):
                pass
        return value

    def __getitem__(self, key):
        val = self.get(key)
        if val is None:
            raise KeyError(f"Config key not found: {key}")
        return val

    def __contains__(self, key):
        return self.get(key) is not None


# Module-level singleton — import and use directly
config = Config()
