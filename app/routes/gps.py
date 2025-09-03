from __future__ import annotations
from flask import jsonify

from app.gps import get_status


def status():
    return jsonify(get_status())


def create_blueprint():
    from flask import Blueprint
    bp = Blueprint("gps", __name__)
    bp.add_url_rule("/gps_status", view_func=status)
    return bp


def register(app):
    app.add_url_rule("/gps_status", view_func=status)

