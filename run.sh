#!/usr/bin/env bash
# Starts the Dressmaker Try-On app at http://127.0.0.1:5000
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
(sleep 1.5 && xdg-open http://127.0.0.1:5000 >/dev/null 2>&1) &
exec .venv/bin/python app.py
