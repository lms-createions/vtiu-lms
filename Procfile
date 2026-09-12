web: gunicorn app:app --bind 0.0.0.0:$PORT --worker-class eventlet --workers 1 --timeout 60 --max-requests 50 --max-requests-jitter 5 --log-level info
