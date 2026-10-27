#!/usr/bin/env bash
# Update the hosted stack: pull brickpipe, the Nova fork (master) and the
# Nova toolkit (pinned tag), rebuild, restart, remove dangling images
# and build cache older than a week.
#
#   ~/lego/brickpipe/deploy/update.sh
#
# Expects ~/lego/{brickpipe,ldraw-nova,ldraw-nova-docker} side by side (it
# clones the two Nova repos if they are missing) and deploy/.env filled in.
# Safe to re-run: it only fast-forwards, refuses to touch a repo with local
# edits to tracked files, and leaves the containers running if a step fails.
#
# Override what it checks out in deploy/.env or the environment:
#   NOVA_DOCKER_REF   (default master)  branch, tag or commit of the fork
#   NOVA_TOOLKIT_REF  (default v0.6.0)  tag or commit of the toolkit
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRICKPIPE_DIR="$(dirname "$DEPLOY_DIR")"
LEGO_DIR="$(dirname "$BRICKPIPE_DIR")"
ENV_FILE="$DEPLOY_DIR/.env"
COMPOSE=(docker compose -f "$DEPLOY_DIR/docker-compose.yml" --env-file "$ENV_FILE")

NOVA_DOCKER_URL="https://github.com/cgans/ldraw-nova-docker.git"
NOVA_TOOLKIT_URL="https://github.com/anteloc/ldraw-nova.git"

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'update.sh: %s\n' "$*" >&2; exit 1; }

# A setting from the environment, else from deploy/.env (read, never executed).
setting() {
    local name="$1" default="$2" value
    if [ -n "${!name:-}" ]; then
        printf '%s' "${!name}"
        return
    fi
    value="$(sed -n "s/^${name}=//p" "$ENV_FILE" | tail -n 1)"
    value="${value%\"}"; value="${value#\"}"
    printf '%s' "${value:-$default}"
}

# data/ is skipped: Nova writes there at run time (the fork tracks a few demo
# chats in it). A pull that would overwrite such a file still stops safely.
no_local_edits() {
    git -C "$1" diff --quiet HEAD -- . ':(exclude)data' \
        || die "$1 has local changes to tracked files; commit or stash them first."
}

# sync_repo <folder> <url> <ref>: clone if missing, point origin at <url>,
# then fast-forward to branch <ref> or check out tag/commit <ref>.
sync_repo() {
    local dir="$1" url="$2" ref="$3" current
    if [ ! -d "$dir/.git" ]; then
        say "Cloning $url into $dir"
        git clone "$url" "$dir"
    fi
    current="$(git -C "$dir" remote get-url origin 2>/dev/null || true)"
    if [ "$current" != "$url" ]; then
        say "Pointing $dir at $url (was ${current:-none})"
        git -C "$dir" remote remove origin 2>/dev/null || true
        git -C "$dir" remote add origin "$url"
    fi
    no_local_edits "$dir"
    say "Fetching $dir"
    git -C "$dir" fetch --tags --prune origin
    if git -C "$dir" show-ref --verify --quiet "refs/remotes/origin/$ref"; then
        if git -C "$dir" show-ref --verify --quiet "refs/heads/$ref"; then
            git -C "$dir" checkout --quiet "$ref"
        else
            git -C "$dir" checkout --quiet -b "$ref" --track "origin/$ref"
        fi
        git -C "$dir" merge --ff-only --quiet "origin/$ref"
    else
        git -C "$dir" -c advice.detachedHead=false checkout --quiet --detach "$ref"
    fi
    printf '    %s is at %s (%s)\n' "$(basename "$dir")" "$ref" "$(git -C "$dir" rev-parse --short HEAD)"
}

# Everything runs from main(), which bash reads whole before starting: the
# git pull below may replace this very file.
main() {
    command -v git >/dev/null || die "git is not installed."
    command -v docker >/dev/null || die "docker is not installed."
    docker compose version >/dev/null 2>&1 || die "the docker compose plugin is not installed."
    [ -f "$ENV_FILE" ] || die "no $ENV_FILE yet: cp deploy/.env.example deploy/.env and fill it in."
    case "$(setting TUNNEL_TOKEN '')" in
        ""|paste-the-tunnel-token-here) die "set TUNNEL_TOKEN in $ENV_FILE first." ;;
    esac
    case "$(setting ANTHROPIC_API_KEY '')" in
        ""|sk-ant-replace-with-your-key)
            printf 'update.sh: warning: ANTHROPIC_API_KEY is not set in %s; Nova has no default key.\n' "$ENV_FILE" >&2 ;;
    esac

    say "Updating brickpipe in $BRICKPIPE_DIR"
    no_local_edits "$BRICKPIPE_DIR"
    git -C "$BRICKPIPE_DIR" pull --ff-only
    printf '    brickpipe is at %s\n' "$(git -C "$BRICKPIPE_DIR" rev-parse --short HEAD)"

    sync_repo "$LEGO_DIR/ldraw-nova-docker" "$NOVA_DOCKER_URL" "$(setting NOVA_DOCKER_REF master)"
    sync_repo "$LEGO_DIR/ldraw-nova" "$NOVA_TOOLKIT_URL" "$(setting NOVA_TOOLKIT_REF v0.6.0)"

    say "Pulling the tunnel image"
    "${COMPOSE[@]}" pull tunnel

    say "Building images (refreshing base images; Nova's first build takes a while)"
    "${COMPOSE[@]}" build --pull

    say "Restarting changed containers and waiting until they are healthy"
    "${COMPOSE[@]}" up -d --remove-orphans --wait --wait-timeout 1200

    say "Removing dangling images left by the rebuild"
    docker image prune -f

    say "Removing build cache older than a week (newer cache keeps rebuilds fast)"
    docker builder prune -f --filter until=168h

    say "Done"
    "${COMPOSE[@]}" ps
    printf '\n    https://%s  and  https://%s\n' \
        "$(setting BRICKPIPE_HOSTNAME bricks.example.com)" "$(setting NOVA_HOSTNAME nova.example.com)"
}

main "$@"
exit
