"""
Waitress production server entry point for Celito Onboarding Platform.

Usage:  python server.py
"""

import logging
import os
import sys
import time
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler

# Ensure the backend package is importable
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(BACKEND_DIR, ".."))
sys.path.insert(0, PROJECT_DIR)

from backend.app import create_app
from backend.config import config


def setup_logging():
    """Configure logging with daily-rotating file handler and console output."""
    logs_dir = os.path.join(PROJECT_DIR, "logs")
    os.makedirs(logs_dir, exist_ok=True)

    # Daily rotating log file
    log_file = os.path.join(logs_dir, f"server_{datetime.now():%Y-%m-%d}.log")
    file_handler = TimedRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    file_handler.suffix = "%Y-%m-%d"

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)

    # Format
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    # Root logger
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # Quiet noisy loggers
    logging.getLogger("waitress").setLevel(logging.WARNING)
    logging.getLogger("msal").setLevel(logging.WARNING)


def main():
    setup_logging()
    logger = logging.getLogger("server")

    host = config.get("server.host", "127.0.0.1")
    port = config.get("server.port", 8780)

    logger.info("=" * 60)
    logger.info("  Celito Employee Onboarding & Offboarding Platform")
    logger.info("  Starting on http://%s:%s", host, port)
    logger.info("  Project dir: %s", PROJECT_DIR)
    logger.info("=" * 60)

    # 5-second startup delay (let dashboard load from cached DB before
    # any background sync tasks start consuming resources)
    logger.info("Waiting 5 seconds before starting background tasks...")
    time.sleep(5)

    app = create_app()

    try:
        from waitress import serve
        logger.info("Server ready — listening on http://%s:%s", host, port)
        serve(app, host=host, port=port, threads=4)
    except ImportError:
        logger.warning(
            "Waitress not installed — falling back to Flask dev server "
            "(NOT suitable for production)"
        )
        app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
