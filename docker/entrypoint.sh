#!/bin/sh
# Drop from root to the uid/gid Unraid expects, so files written to the array
# shares are owned by the user rather than by root.
set -e

PUID=${PUID:-99}
PGID=${PGID:-100}
UMASK=${UMASK:-022}

umask "$UMASK"

# Reuse an existing group/user with these ids when there is one (PGID 100 is
# Debian's own "users" group, which is exactly what Unraid defaults to).
if ! getent group "$PGID" >/dev/null 2>&1; then
    groupadd -g "$PGID" voicebox
fi
APP_GROUP=$(getent group "$PGID" | cut -d: -f1)

if ! getent passwd "$PUID" >/dev/null 2>&1; then
    useradd -u "$PUID" -g "$PGID" -M -d /app -s /usr/sbin/nologin voicebox
fi
APP_USER=$(getent passwd "$PUID" | cut -d: -f1)

# Claim the directories the app has to write to. Deliberately not recursive:
# a recursive chown of a large output share costs minutes on every start, and
# the files in it are already owned correctly.
for dir in "$VOICEBOX_INGEST_DIR" "$VOICEBOX_INGEST_DIR/processed" "$VOICEBOX_INGEST_DIR/failed" \
           "$VOICEBOX_OUTPUT_DIR" "$VOICEBOX_CONFIG_DIR" "$HF_HOME" /app/logs; do
    [ -n "$dir" ] || continue
    mkdir -p "$dir" 2>/dev/null || true
    chown "$PUID:$PGID" "$dir" 2>/dev/null \
        || echo "[voicebox] warning: could not chown $dir (read-only or remote mount?)"
done

echo "[voicebox] running as ${APP_USER}(${PUID}):${APP_GROUP}(${PGID}) with umask ${UMASK}"
echo "[voicebox] ingest=${VOICEBOX_INGEST_DIR} output=${VOICEBOX_OUTPUT_DIR} config=${VOICEBOX_CONFIG_DIR}"

exec gosu "${PUID}:${PGID}" "$@"
