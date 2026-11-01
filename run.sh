#!/usr/bin/env bash
# One-command start for Linux/macOS.
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
echo "Starting PDFKit at http://127.0.0.1:8000"
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
