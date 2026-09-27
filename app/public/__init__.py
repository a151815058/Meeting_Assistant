"""Public pages that need no login: the homepage, privacy policy (REQ-55) and terms of service (REQ-57).

Google's OAuth brand verification requires them to be reachable without signing in, on the
verified domain, with the homepage describing the app and linking to the privacy policy.
"""
from flask import Blueprint, redirect, render_template, url_for
from flask_login import current_user

public_bp = Blueprint("public", __name__)


@public_bp.route("/")
def home():
    if current_user.is_authenticated:
        return redirect(url_for("meetings.dashboard"))
    return render_template("public/home.html")


@public_bp.route("/privacy")
def privacy():
    return render_template("public/privacy.html")


@public_bp.route("/terms")
def terms():
    return render_template("public/terms.html")
