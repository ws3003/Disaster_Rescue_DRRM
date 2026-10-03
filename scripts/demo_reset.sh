#!/bin/bash

echo "================================"
echo "Reset Disaster Rescue Demo Data"
echo "================================"

PROJECT_ROOT=$(cd "$(dirname "$0")/.." && pwd)

cd "$PROJECT_ROOT" || exit 1


echo "[1/4] Reset SQLite database..."

sqlite3 data/drrm.db <<EOF
DELETE FROM tasks;
DELETE FROM events;
DELETE FROM persons;
DELETE FROM agent_logs;
EOF


echo "[2/4] Clear communication logs..."

if [ -f data/communication_log.jsonl ]; then
    truncate -s 0 data/communication_log.jsonl
fi


echo "[3/4] Clear snapshots..."

rm -rf data/snapshots/*


echo "[4/4] Clear voice cache..."

rm -rf data/voice/*


echo "================================"
echo "Demo reset completed"
echo "================================"
