#!/usr/bin/env bash
# Nightly backup of the Finance Team Toolkit's live state.
#
# Archives the two things that exist ONLY on the server (everything else is
# in git): the data/ directory (allocations, holds, priority list, contacts,
# compliance batches, remittance statements…) and config.json (SMTP + auth
# users + AI key). Keeps 14 days of archives in /var/backups/finance-toolkit.
#
# Installed by deploy: /etc/cron.d/finance-toolkit-backup runs this at 02:30.
# Restore:  tar -xzf <archive> -C /opt/finance-toolkit
set -euo pipefail

APP_DIR="/opt/finance-toolkit"
BACKUP_DIR="/var/backups/finance-toolkit"
KEEP_DAYS=14
STAMP="$(date +%Y-%m-%d_%H%M)"
ARCHIVE="${BACKUP_DIR}/finance-toolkit_${STAMP}.tar.gz"

mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"          # contains secrets (config.json)

tar -czf "${ARCHIVE}" -C "${APP_DIR}" data config.json

# Sanity: a backup that can't be listed is no backup.
tar -tzf "${ARCHIVE}" > /dev/null

# Retention: drop archives older than KEEP_DAYS.
find "${BACKUP_DIR}" -name 'finance-toolkit_*.tar.gz' -mtime "+${KEEP_DAYS}" -delete

echo "backup ok: ${ARCHIVE} ($(du -h "${ARCHIVE}" | cut -f1))"
