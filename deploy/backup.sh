#!/usr/bin/env bash
# Back up the hosted stack's state to one dated .tar.gz and keep the newest N.
#
#   ~/lego/brickpipe/deploy/backup.sh
#
# The archive holds:
#   brickpipe-data/  brickpipe's runs and Rebrickable cache (volume brickpipe_brickpipe-data;
#                    the LDraw library it downloaded is left out, it downloads again)
#   nova-config/     Nova's model entries, API keys and Claude/ChatGPT sign-ins
#                    (volume brickpipe_nova-config)
#   nova-data/       Nova's chats and generated models (~/lego/ldraw-nova-docker/data)
#
# It CONTAINS SECRETS (keys and sign-in tokens): it is written mode 600, and
# copies must be kept private. Settings, from the environment or deploy/.env:
#   BACKUP_DIR   where archives go          (default ~/lego/backups)
#   BACKUP_KEEP  how many archives to keep  (default 7)
# The containers keep running; a run or chat busy at that moment may be
# caught half written. Uses the brickpipe image, so build the stack first.
#
# Restore (replaces what is there now):
#   cd ~/lego/brickpipe
#   docker compose -f deploy/docker-compose.yml --env-file deploy/.env down
#   docker volume rm brickpipe_brickpipe-data brickpipe_nova-config   # for an exact copy
#   docker compose -f deploy/docker-compose.yml --env-file deploy/.env up --no-start   # fresh, empty volumes
#   docker run --rm \
#     -v brickpipe_brickpipe-data:/restore/brickpipe-data \
#     -v brickpipe_nova-config:/restore/nova-config \
#     -v ~/lego/ldraw-nova-docker/data:/restore/nova-data \
#     -v ~/lego/backups:/backup:ro \
#     brickpipe tar -xzpf /backup/brickpipe-backup-YYYYMMDD-HHMMSS.tar.gz -C /restore
#   docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d
# brickpipe downloads the LDraw library again on its first start.
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRICKPIPE_DIR="$(dirname "$DEPLOY_DIR")"
LEGO_DIR="$(dirname "$BRICKPIPE_DIR")"
ENV_FILE="$DEPLOY_DIR/.env"

BRICKPIPE_VOLUME=brickpipe_brickpipe-data
NOVA_CONFIG_VOLUME=brickpipe_nova-config
NOVA_DATA_DIR="$LEGO_DIR/ldraw-nova-docker/data"
IMAGE=brickpipe

die() { printf 'backup.sh: %s\n' "$*" >&2; exit 1; }

# A setting from the environment, else from deploy/.env (read, never executed).
setting() {
    local name="$1" default="$2" value=""
    if [ -n "${!name:-}" ]; then
        printf '%s' "${!name}"
        return
    fi
    if [ -f "$ENV_FILE" ]; then
        value="$(sed -n "s/^${name}=//p" "$ENV_FILE" | tail -n 1)"
        value="${value%\"}"; value="${value#\"}"
    fi
    printf '%s' "${value:-$default}"
}

BACKUP_DIR="$(setting BACKUP_DIR "$LEGO_DIR/backups")"
BACKUP_KEEP="$(setting BACKUP_KEEP 7)"
case "$BACKUP_KEEP" in
    ''|*[!0-9]*|0) die "BACKUP_KEEP must be a whole number of at least 1 (got '$BACKUP_KEEP')." ;;
esac

command -v docker >/dev/null || die "docker is not installed."
docker image inspect "$IMAGE" >/dev/null 2>&1 || die "no '$IMAGE' image yet; run deploy/update.sh first."
for volume in "$BRICKPIPE_VOLUME" "$NOVA_CONFIG_VOLUME"; do
    docker volume inspect "$volume" >/dev/null 2>&1 || die "volume $volume does not exist; has the stack been started?"
done
[ -d "$NOVA_DATA_DIR" ] || die "$NOVA_DATA_DIR not found."

umask 077
mkdir -p "$BACKUP_DIR"
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd)"
name="brickpipe-backup-$(date +%Y%m%d-%H%M%S).tar.gz"

printf 'Backing up %s, %s and %s\n' "$BRICKPIPE_VOLUME" "$NOVA_CONFIG_VOLUME" "$NOVA_DATA_DIR"
# Run as root in a throwaway container: /config is root-only. The archive is
# written under a temporary name and handed to you (owner, mode 600) at the end.
docker run --rm --network none \
    -v "$BRICKPIPE_VOLUME":/src/brickpipe-data:ro \
    -v "$NOVA_CONFIG_VOLUME":/src/nova-config:ro \
    -v "$NOVA_DATA_DIR":/src/nova-data:ro \
    -v "$BACKUP_DIR":/backup \
    "$IMAGE" sh -c '
        set -e
        tar -czpf "/backup/$1.partial" -C /src --exclude=brickpipe-data/brickpipe/ldraw \
            brickpipe-data nova-config nova-data
        chown "$2" "/backup/$1.partial"
        chmod 600 "/backup/$1.partial"
        mv "/backup/$1.partial" "/backup/$1"
    ' sh "$name" "$(id -u):$(id -g)"

# Keep the newest BACKUP_KEEP archives (names sort by date).
shopt -s nullglob
archives=("$BACKUP_DIR"/brickpipe-backup-*.tar.gz)
if [ "${#archives[@]}" -gt "$BACKUP_KEEP" ]; then
    for old in "${archives[@]:0:${#archives[@]}-BACKUP_KEEP}"; do
        printf 'Removing old backup %s\n' "$old"
        rm -f -- "$old"
    done
fi

printf 'Made %s (%s)\n' "$BACKUP_DIR/$name" "$(du -h "$BACKUP_DIR/$name" | cut -f1)"
