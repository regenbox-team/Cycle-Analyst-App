from __future__ import annotations
from flask import render_template, request, jsonify, redirect
from app import state
from app.game_db import insert_score, top_scores


def game_start():
    if not state.session_active:
        return redirect("/start")
    return render_template("game_start.html", current_user=state.current_user)


def game_play():
    if not state.session_active:
        return redirect("/start")
    username = request.args.get("user") or state.current_user or ""
    return render_template("game_play.html", username=username)


def game_submit_score():
    try:
        data = request.get_json(force=True) or {}
        user = (data.get("user") or state.current_user or "").strip() or "Anonymous"
        distance = int(float(data.get("distance_m") or 0))
        insert_score(user, distance)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


def game_leaderboard():
    if not state.session_active:
        return redirect("/start")
    scores = top_scores(10)
    return render_template("game_leaderboard.html", scores=scores)


def create_blueprint():
    from flask import Blueprint
    bp = Blueprint("game", __name__)
    bp.add_url_rule("/game", view_func=game_start)
    bp.add_url_rule("/game/play", view_func=game_play)
    bp.add_url_rule("/game/score", methods=["POST"], view_func=game_submit_score)
    bp.add_url_rule("/game/leaderboard", view_func=game_leaderboard)
    return bp


def register(app):
    app.add_url_rule("/game", view_func=game_start)
    app.add_url_rule("/game/play", view_func=game_play)
    app.add_url_rule("/game/score", methods=["POST"], view_func=game_submit_score)
    app.add_url_rule("/game/leaderboard", view_func=game_leaderboard)

