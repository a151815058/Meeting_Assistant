from flask import Blueprint

meetings_bp = Blueprint("meetings", __name__, url_prefix="/meetings")

from app.meetings import routes  # noqa: E402,F401
