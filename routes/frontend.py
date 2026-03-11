"""
Frontend blueprint — HTML dashboard and static assets.
"""

from flask import Blueprint, render_template

frontend_bp = Blueprint("frontend", __name__)


@frontend_bp.route("/")
def dashboard():
    return render_template("dashboard.html")


@frontend_bp.route("/favicon.ico")
def favicon():
    return "", 204
