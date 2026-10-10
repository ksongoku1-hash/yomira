"""PyInstaller runtime hook: packaged app defaults to the production profile."""

import os

if not os.environ.get("YOMIRA_APP_ENV"):
    os.environ["YOMIRA_APP_ENV"] = "prod"
