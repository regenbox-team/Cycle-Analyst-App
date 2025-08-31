from cycle_server import create_app

# Gunicorn/Uwsgi entry point
application = create_app(start_reader=False)

