#!/bin/bash
# launch_ofc.sh
# Starts LinuxCNC and status.py together.
# When LinuxCNC exits (for ANY reason), status.py is cleanly terminated.

LINUXCNC_CONFIG="/home/user_name/linuxcnc/configs/OFC_PC/OFC_PC.ini"
STATUS_SCRIPT="/home/user_name/linuxcnc/scripts/status.py"
LOG_FILE="/tmp/cnc_status_launcher.log"

echo "$(date): Launcher started" >> "$LOG_FILE"

# Start LinuxCNC (foreground — this script waits here until LinuxCNC closes)
linuxcnc "$LINUXCNC_CONFIG" &
LINUXCNC_PID=$!
echo "$(date): LinuxCNC started (PID $LINUXCNC_PID)" >> "$LOG_FILE"

# Wait for LinuxCNC NML channels to be ready before starting status.py
sleep 3

# Start status.py in background
python3 "$STATUS_SCRIPT" &
STATUS_PID=$!
echo "$(date): status.py started (PID $STATUS_PID)" >> "$LOG_FILE"

# Wait for LinuxCNC to exit (blocks here)
wait $LINUXCNC_PID
LINUXCNC_EXIT_CODE=$?
echo "$(date): LinuxCNC exited (code $LINUXCNC_EXIT_CODE) — stopping status.py" >> "$LOG_FILE"

# Cleanly stop status.py
if kill -0 "$STATUS_PID" 2>/dev/null; then
    kill -SIGTERM "$STATUS_PID"
    # Give it 5 seconds to shut down gracefully
    sleep 5
    # Force kill if still running
    if kill -0 "$STATUS_PID" 2>/dev/null; then
        echo "$(date): status.py did not stop gracefully — force killing" >> "$LOG_FILE"
        kill -SIGKILL "$STATUS_PID"
    fi
fi

echo "$(date): Launcher exited cleanly" >> "$LOG_FILE"
