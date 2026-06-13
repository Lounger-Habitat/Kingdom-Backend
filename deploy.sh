#!/bin/bash
set -e
cd /root/kingdom-server
git pull origin main
uv pip install -r requirements.txt --python .venv/bin/python
uv run python manage.py migrate
uv run python manage.py collectstatic --noinput
pkill gunicorn || true
uv run python -m gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3 --daemon
echo "Deploy done!"
