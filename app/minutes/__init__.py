"""Meeting minutes: templates, LLM provider abstraction, generation (Phase 4)."""
from flask import Blueprint

minutes_bp = Blueprint("minutes", __name__)

from app.minutes import routes  # noqa: E402,F401
