#!/bin/sh
# Local development: loads .env and serves on :8080 with reload.
cd "$(dirname "$0")"
set -a; . ./.env; set +a
export DATA_DIR="${DATA_DIR:-./data}"
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload
