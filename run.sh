#!/bin/bash
set -e
# Simple runner - ensures env and DB
if [ ! -f .env ]; then
  echo "No .env found, copying from .env.example"
  cp .env.example .env
  echo "Please edit .env with strong secrets before production!"
fi
if [ ! -f instance/worklog.db ]; then
  echo "Initializing DB via seed.py..."
  python seed.py
else
  echo "DB already exists at instance/worklog.db"
fi
echo "Starting Flask on http://localhost:5000"
python app.py
