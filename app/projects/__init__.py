"""Project management (REQ-64): projects a user files their meetings under."""
from flask import Blueprint

projects_bp = Blueprint("projects", __name__, url_prefix="/projects")

from app.projects import routes  # noqa: E402,F401
