from __future__ import annotations

from flask import redirect, render_template, request

from app.user_profiles import active_profiles, create_profile, hide_profile, load_profiles, upsert_profile


def users_page():
    setup = request.args.get("setup") == "1"
    try:
        from app.monitor_client import fetch_monitor_users
        server_users = fetch_monitor_users() if setup else []
    except Exception:
        server_users = []
    return render_template(
        "users.html",
        users=load_profiles(),
        active_users=active_profiles(),
        server_users=server_users,
        setup=setup,
    )


def create_user():
    initials = request.form.get("initials", "").strip().upper()
    first_name = request.form.get("first_name", "").strip()
    last_name = request.form.get("last_name", "").strip()
    date_of_birth = request.form.get("date_of_birth", "").strip()
    gender = request.form.get("gender", "").strip()
    if not initials:
        initials = "".join([part[:1] for part in (first_name, last_name) if part]).upper()
    if initials and (first_name or last_name):
        create_profile(
            initials=initials,
            first_name=first_name,
            last_name=last_name,
            date_of_birth=date_of_birth,
            gender=gender,
            active=True,
        )
    return redirect("/users")


def hide_user(user_id: str):
    hide_profile(user_id)
    return redirect("/users")


def import_user():
    user_id = request.form.get("user_id", "").strip()
    try:
        from app.monitor_client import fetch_monitor_users
        server_users = fetch_monitor_users()
    except Exception:
        server_users = []
    for user in server_users:
        if user.get("user_id") == user_id:
            user["active"] = True
            upsert_profile(user, active=True)
            break
    return redirect("/users")


def create_blueprint():
    from flask import Blueprint
    bp = Blueprint("users", __name__)
    bp.add_url_rule("/users", view_func=users_page)
    bp.add_url_rule("/users/create", methods=["POST"], view_func=create_user)
    bp.add_url_rule("/users/import", methods=["POST"], view_func=import_user)
    bp.add_url_rule("/users/<user_id>/hide", methods=["POST"], view_func=hide_user)
    return bp


def register(app):
    app.add_url_rule("/users", view_func=users_page)
    app.add_url_rule("/users/create", methods=["POST"], view_func=create_user)
    app.add_url_rule("/users/import", methods=["POST"], view_func=import_user)
    app.add_url_rule("/users/<user_id>/hide", methods=["POST"], view_func=hide_user)
