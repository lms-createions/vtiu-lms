web: python -m flask --app app db upgrade && gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 60 --max-requests 50 --max-requests-jitter 5 --preload --log-level info
