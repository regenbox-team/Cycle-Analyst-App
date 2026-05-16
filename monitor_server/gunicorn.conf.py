import multiprocessing
import os


bind = os.getenv("MONITOR_GUNICORN_BIND", f"0.0.0.0:{os.getenv('MONITOR_PORT', '8080')}")
preload_app = True
workers = int(os.getenv("MONITOR_GUNICORN_WORKERS", str(max(2, min(4, multiprocessing.cpu_count())))))
worker_class = os.getenv("MONITOR_GUNICORN_WORKER_CLASS", "gthread")
threads = int(os.getenv("MONITOR_GUNICORN_THREADS", "4"))
timeout = int(os.getenv("MONITOR_GUNICORN_TIMEOUT", "120"))
graceful_timeout = int(os.getenv("MONITOR_GUNICORN_GRACEFUL_TIMEOUT", "30"))
keepalive = int(os.getenv("MONITOR_GUNICORN_KEEPALIVE", "5"))
max_requests = int(os.getenv("MONITOR_GUNICORN_MAX_REQUESTS", "1000"))
max_requests_jitter = int(os.getenv("MONITOR_GUNICORN_MAX_REQUESTS_JITTER", "100"))
accesslog = os.getenv("MONITOR_GUNICORN_ACCESSLOG", "-")
errorlog = os.getenv("MONITOR_GUNICORN_ERRORLOG", "-")
loglevel = os.getenv("MONITOR_GUNICORN_LOGLEVEL", "info")
