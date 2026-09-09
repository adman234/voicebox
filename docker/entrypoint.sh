#!/bin/sh
# Resolve where data lives, take ownership of it, then drop from root to the
# uid/gid Unraid expects so files written to the array shares are owned by the
# user rather than by root.
set -e

PUID=${PUID:-99}
PGID=${PGID:-100}
UMASK=${UMASK:-022}

umask "$UMASK"

VOICEBOX_CONFIG_DIR=${VOICEBOX_CONFIG_DIR:-/config}
VOICEBOX_INGEST_DIR=${VOICEBOX_INGEST_DIR:-/ingest}
VOICEBOX_OUTPUT_DIR=${VOICEBOX_OUTPUT_DIR:-/output}

# True when a path is a real mount point rather than a plain directory in the
# image. Field 5 of mountinfo is the mount point.
is_mounted() {
    awk -v path="$1" '$5 == path { found = 1 } END { exit !found }' /proc/self/mountinfo 2>/dev/null
}

# Decide a directory: an explicit environment variable wins, then a dedicated
# mount point if one is actually mapped, otherwise a folder inside the config
# directory so a single /config mapping keeps everything together.
resolve_dir() {
    explicit=$1
    mount_point=$2
    fallback=$3

    if [ -n "$explicit" ]; then
        printf '%s' "$explicit"
    elif is_mounted "$mount_point"; then
        printf '%s' "$mount_point"
    else
        printf '%s' "$fallback"
    fi
}

VOICEBOX_LOG_DIR=$(resolve_dir "$VOICEBOX_LOG_DIR" /logs "$VOICEBOX_CONFIG_DIR/logs")
HF_HOME=$(resolve_dir "$HF_HOME" /models "$VOICEBOX_CONFIG_DIR/huggingface")
XDG_CACHE_HOME=$(resolve_dir "$XDG_CACHE_HOME" /cache "$VOICEBOX_CONFIG_DIR/cache")
# Libraries that fall back to ~/.cache must land somewhere writable by PUID.
HOME=$VOICEBOX_CONFIG_DIR

export VOICEBOX_CONFIG_DIR VOICEBOX_INGEST_DIR VOICEBOX_OUTPUT_DIR
export VOICEBOX_LOG_DIR HF_HOME XDG_CACHE_HOME HOME

# Reuse an existing group/user with these ids when there is one (PGID 100 is
# Debian's own "users" group, which is exactly what Unraid defaults to).
if ! getent group "$PGID" >/dev/null 2>&1; then
    groupadd -g "$PGID" voicebox
fi
APP_GROUP=$(getent group "$PGID" | cut -d: -f1)

if ! getent passwd "$PUID" >/dev/null 2>&1; then
    useradd -u "$PUID" -g "$PGID" -M -d "$VOICEBOX_CONFIG_DIR" -s /usr/sbin/nologin voicebox
fi
APP_USER=$(getent passwd "$PUID" | cut -d: -f1)

# Claim the directories the app has to write to. Deliberately not recursive:
# a recursive chown of a large output share costs minutes on every start, and
# the files in it are already owned correctly.
for dir in "$VOICEBOX_INGEST_DIR" "$VOICEBOX_INGEST_DIR/processed" "$VOICEBOX_INGEST_DIR/failed" \
           "$VOICEBOX_OUTPUT_DIR" "$VOICEBOX_CONFIG_DIR" "$VOICEBOX_LOG_DIR" \
           "$HF_HOME" "$XDG_CACHE_HOME"; do
    [ -n "$dir" ] || continue
    mkdir -p "$dir" 2>/dev/null || true
    chown "$PUID:$PGID" "$dir" 2>/dev/null \
        || echo "[voicebox] warning: could not chown $dir (read-only or remote mount?)"
done

echo "[voicebox] running as ${APP_USER}(${PUID}):${APP_GROUP}(${PGID}) with umask ${UMASK}"
echo "[voicebox] ingest=${VOICEBOX_INGEST_DIR} output=${VOICEBOX_OUTPUT_DIR} config=${VOICEBOX_CONFIG_DIR}"
echo "[voicebox] logs=${VOICEBOX_LOG_DIR} models=${HF_HOME} cache=${XDG_CACHE_HOME}"

exec gosu "${PUID}:${PGID}" "$@"
