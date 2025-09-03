from __future__ import annotations
import os
from flask import jsonify, request, send_file
from app.config import GPX_ROUTE_FILE


def upload_gpx():
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No file part"}), 400
        f = request.files['file']
        if f.filename == '':
            return jsonify({"error": "No selected file"}), 400
        # Basic validation: require .gpx extension
        filename = f.filename.lower()
        if not filename.endswith('.gpx'):
            return jsonify({"error": "Unsupported file type; must be .gpx"}), 400

        # Save/overwrite
        f.save(GPX_ROUTE_FILE)
        try:
            size = os.path.getsize(GPX_ROUTE_FILE)
        except Exception:
            size = None
        return jsonify({"status": "ok", "message": "file successfully uploaded and read", "size": size})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def gpx_status():
    try:
        if os.path.exists(GPX_ROUTE_FILE):
            st = os.stat(GPX_ROUTE_FILE)
            return jsonify({
                "exists": True,
                "mtime": int(st.st_mtime),
                "size": st.st_size
            })
        return jsonify({"exists": False})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def get_gpx():
    if not os.path.exists(GPX_ROUTE_FILE):
        return ("Not Found", 404)
    # Serve as GPX XML
    return send_file(GPX_ROUTE_FILE, mimetype='application/gpx+xml', as_attachment=False, download_name='route.gpx')


def erase_gpx():
    try:
        if os.path.exists(GPX_ROUTE_FILE):
            os.remove(GPX_ROUTE_FILE)
        return jsonify({"status": "ok", "message": "track erased"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def create_blueprint():
    from flask import Blueprint
    bp = Blueprint('tracks', __name__)
    bp.add_url_rule('/gpx/upload', methods=['POST'], view_func=upload_gpx)
    bp.add_url_rule('/gpx/status', view_func=gpx_status)
    bp.add_url_rule('/gpx/track', methods=['GET'], view_func=get_gpx)
    bp.add_url_rule('/gpx/erase', methods=['POST', 'DELETE'], view_func=erase_gpx)
    return bp


def register(app):
    app.add_url_rule('/gpx/upload', methods=['POST'], view_func=upload_gpx)
    app.add_url_rule('/gpx/status', view_func=gpx_status)
    app.add_url_rule('/gpx/track', methods=['GET'], view_func=get_gpx)
    app.add_url_rule('/gpx/erase', methods=['POST', 'DELETE'], view_func=erase_gpx)

