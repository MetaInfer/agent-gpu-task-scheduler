"""Uvicorn entrypoint; runtime identity must already exist."""

from agent_scheduler.api.app import create_app
from agent_scheduler.config import Settings

app = create_app(Settings.from_env())
