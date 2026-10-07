#!/usr/bin/env bash
# Starts Dressmaker Atelier and opens it in your browser (Mac and Linux). Stop it with Ctrl+C.
set -e
cd "$(dirname "$0")"
URL=http://127.0.0.1:5000/atelier
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# (Re)install if the packages are missing, e.g. when the first setup was interrupted.
if ! .venv/bin/python -c "import flask, requests, PIL" 2>/dev/null; then
  echo "Setting things up (only the first time)..."
  .venv/bin/pip install -q -r requirements.txt
fi
(sleep 1.5 && { xdg-open "$URL" || open "$URL"; } >/dev/null 2>&1) &
echo "Dressmaker Atelier is at $URL  (press Ctrl+C to stop)"
exec .venv/bin/python app.py
