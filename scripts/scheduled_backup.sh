#!/bin/sh
# What the backup schedule runs. scripts/install_backup_schedule.sh copies this,
# export_csv.py, live_table.py and src/ to ~/Library/Application Support/cloud-cellar-backup
# and runs the copy from there, because macOS doesn't let a launchd job read ~/Documents.
#
# Normally it runs export_csv.py --if-due. If a file named run-now is next to
# scripts/ (install_backup_schedule.sh --run-now puts it there), it removes it and
# exports whether due or not. If the export fails, a notification says so, since
# nobody is watching the log.
#
# launchd sets PYTHON (a Python with boto3), BACKUP_DIR, AWS_PROFILE, AWS_REGION,
# STACK_NAME and BACKUP_LOG, and appends this script's output to BACKUP_LOG.
cd "$(dirname "$0")/.." || exit 1
echo "--- $(date '+%Y-%m-%d %H:%M:%S %Z')"
if [ -e run-now ]; then
  rm -f run-now
  "${PYTHON:?PYTHON is not set}" scripts/export_csv.py --out "${BACKUP_DIR:?BACKUP_DIR is not set}"
else
  "${PYTHON:?PYTHON is not set}" scripts/export_csv.py --out "${BACKUP_DIR:?BACKUP_DIR is not set}" --if-due
fi
status=$?
if [ "$status" -ne 0 ]; then
  osascript -e "display notification \"The stock table backup failed (exit $status). See ${BACKUP_LOG:-the log}.\" with title \"cloud-cellar backup\"" || true
fi
exit "$status"
