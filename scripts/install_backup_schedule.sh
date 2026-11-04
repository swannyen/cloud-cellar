#!/usr/bin/env bash
# Set up the twice-monthly backup on this Mac, run it once now, or take it away.
#
#   PYTHON=/path/to/python-with-boto3 ./scripts/install_backup_schedule.sh   # install, or update after a code change
#   ./scripts/install_backup_schedule.sh --run-now                          # one backup now, the way the schedule runs it
#   ./scripts/install_backup_schedule.sh --uninstall
#
# It installs a launchd job that checks every day at 10:00 and when you log in.
# The export only runs when there is no backup yet since the last 1st or 15th of
# the month, so the backups are made on the 1st and the 15th, or the next time the
# Mac is on (a Mac asleep at 10:00 runs the job when it wakes). Checks that aren't
# due don't touch AWS. Backups go to ~/cloud-cellar-backups.
#
# macOS doesn't let a launchd job read ~/Documents, where the repo is, so this
# copies scripts/export_csv.py, scripts/live_table.py, scripts/scheduled_backup.sh
# and src/ to ~/Library/Application Support/cloud-cellar-backup and the job runs
# that copy. Run this again after changing any of them.
#
# AWS_PROFILE (default household), AWS_REGION (default ap-southeast-2) and
# STACK_NAME (default nuffnuffbot) are written into the job. No secrets are: boto3
# reads the profile's keys from ~/.aws when the job runs.
set -euo pipefail

LABEL=com.cloud-cellar.backup
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/cloud-cellar-backup.log"
APP="$HOME/Library/Application Support/cloud-cellar-backup"
BACKUP_DIR="$HOME/cloud-cellar-backups"
DOMAIN="gui/$(id -u)"
REPO=$(cd "$(dirname "$0")/.." && pwd)

case "${1:-}" in
  --uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    rm -rf "$APP"
    echo "Removed the backup schedule. The backups already made stay in $BACKUP_DIR."
    exit 0 ;;
  --run-now)
    [[ -f "$PLIST" ]] || { echo "The schedule isn't installed yet. Run this script without --run-now first." >&2; exit 1; }
    touch "$APP/run-now"
    launchctl kickstart "$DOMAIN/$LABEL"
    echo "Started a backup. It takes a few seconds; the log is $LOG and the files go to $BACKUP_DIR."
    exit 0 ;;
  "") ;;
  *) echo "Unknown option $1. Use no option, --run-now or --uninstall." >&2; exit 2 ;;
esac

PYTHON=${PYTHON:-$(command -v python3)}
"$PYTHON" -c "import boto3" 2>/dev/null || { echo "$PYTHON can't import boto3. Set PYTHON to a Python that can." >&2; exit 1; }
PYTHON=$("$PYTHON" -c "import sys; print(sys.executable)")
AWS_PROFILE=${AWS_PROFILE:-household}
AWS_REGION=${AWS_REGION:-ap-southeast-2}
STACK_NAME=${STACK_NAME:-nuffnuffbot}
for value in "$APP" "$PYTHON" "$HOME" "$AWS_PROFILE" "$AWS_REGION" "$STACK_NAME"; do
  [[ "$value" != *[\<\>\&]* ]] || { echo "Can't write $value into the job: it has <, > or &." >&2; exit 1; }
done
case "$PYTHON" in
  "$HOME/Documents/"*|"$HOME/Desktop/"*|"$HOME/Downloads/"*)
    echo "$PYTHON is in a folder macOS doesn't let launchd jobs read. Use a Python installed elsewhere." >&2; exit 1 ;;
esac

# A fresh copy of the code the export needs.
rm -rf "$APP"
mkdir -p "$APP/scripts" "$APP/src" "$BACKUP_DIR" "$(dirname "$PLIST")" "$(dirname "$LOG")"
cp "$REPO/scripts/export_csv.py" "$REPO/scripts/live_table.py" "$REPO/scripts/scheduled_backup.sh" "$APP/scripts/"
cp "$REPO"/src/*.py "$APP/src/"
git -C "$REPO" log -1 --format='Copied from %h (%cs) of %s' > "$APP/COPIED_FROM" 2>/dev/null || true
date '+on %Y-%m-%d %H:%M, with any uncommitted changes' >> "$APP/COPIED_FROM"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>$APP/scripts/scheduled_backup.sh</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHON</key><string>$PYTHON</string>
    <key>BACKUP_DIR</key><string>$BACKUP_DIR</string>
    <key>AWS_PROFILE</key><string>$AWS_PROFILE</string>
    <key>AWS_REGION</key><string>$AWS_REGION</string>
    <key>STACK_NAME</key><string>$STACK_NAME</string>
    <key>BACKUP_LOG</key><string>$LOG</string>
  </dict>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>10</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF
plutil -lint -s "$PLIST"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true   # replace an earlier install
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "Installed $PLIST, running a copy of the code in $APP."
echo "It checks every day at 10:00 and at login, and backs up on the 1st and 15th into $BACKUP_DIR."
echo "Log: $LOG"
