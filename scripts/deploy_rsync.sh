#!/usr/bin/env bash
set -Eeuo pipefail

: "${GENSHIN_SSH_HOST:?Missing GENSHIN_SSH_HOST secret}"
: "${GENSHIN_SSH_PORT:?Missing GENSHIN_SSH_PORT secret}"
: "${GENSHIN_SSH_USER:?Missing GENSHIN_SSH_USER secret}"

if [[ ! "$GENSHIN_SSH_HOST" =~ ^[A-Za-z0-9.-]+$ ]]; then
  echo "GENSHIN_SSH_HOST contains unsupported characters." >&2
  exit 2
fi
if [[ ! "$GENSHIN_SSH_PORT" =~ ^[0-9]{1,5}$ ]] || (( GENSHIN_SSH_PORT < 1 || GENSHIN_SSH_PORT > 65535 )); then
  echo "GENSHIN_SSH_PORT must be a valid TCP port." >&2
  exit 2
fi
if [[ ! "$GENSHIN_SSH_USER" =~ ^[a-z_][a-z0-9_-]*$ ]]; then
  echo "GENSHIN_SSH_USER contains unsupported characters." >&2
  exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
IMAGE_ROOT="$REPO_ROOT/fastapi_genshin_map/GetMapImage"
SSH_KEY_PATH="$HOME/.ssh/genshin_map_actions"
KNOWN_HOSTS_PATH="$HOME/.ssh/known_hosts"

for directory in map_data resource_data icon_data; do
  if [[ ! -d "$IMAGE_ROOT/$directory" ]]; then
    echo "Missing generated directory: $IMAGE_ROOT/$directory" >&2
    exit 1
  fi
done

[[ -s "$SSH_KEY_PATH" ]] || { echo "SSH private key file is missing." >&2; exit 1; }
[[ -s "$KNOWN_HOSTS_PATH" ]] || { echo "known_hosts file is missing." >&2; exit 1; }

TARGET="$GENSHIN_SSH_USER@$GENSHIN_SSH_HOST"
REMOTE_ROOT="/home/$GENSHIN_SSH_USER/data/genshin-map"
RELEASE_ID="$(date -u +%Y%m%dT%H%M%SZ)-${GITHUB_RUN_ID:-manual}-${GITHUB_RUN_ATTEMPT:-1}"
STAGE_NAME=".staging-$RELEASE_ID"
REMOTE_STAGE="$REMOTE_ROOT/$STAGE_NAME"

SSH_OPTS=(
  -i "$SSH_KEY_PATH"
  -p "$GENSHIN_SSH_PORT"
  -o IdentitiesOnly=yes
  -o BatchMode=yes
  -o StrictHostKeyChecking=yes
  -o UserKnownHostsFile="$KNOWN_HOSTS_PATH"
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=6
)
RSYNC_RSH="ssh -i $SSH_KEY_PATH -p $GENSHIN_SSH_PORT -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$KNOWN_HOSTS_PATH -o ServerAliveInterval=30 -o ServerAliveCountMax=6"

printf 'Deploying release: %s\n' "$RELEASE_ID"
printf 'Remote destination: %s:%s\n' "$TARGET" "$REMOTE_ROOT"

# Create a staging copy of the currently published release using hard links.
# Unchanged files occupy no additional disk space. rsync replaces changed files
# by writing temporary files and renaming them, leaving current untouched.
ssh "${SSH_OPTS[@]}" "$TARGET" bash -s -- "$REMOTE_ROOT" "$STAGE_NAME" <<'REMOTE_PREPARE'
set -Eeuo pipefail
root="$1"
stage_name="$2"
stage="$root/$stage_name"
mkdir -p "$root/releases" "$stage"

# Remove abandoned staging trees older than 48 hours.
find "$root" -mindepth 1 -maxdepth 1 -type d -name '.staging-*' -mmin +2880 -exec rm -rf -- {} +

if [[ -L "$root/current" ]]; then
  current_real="$(readlink -f "$root/current")"
  if [[ ! -d "$current_real" ]]; then
    echo "The current symlink points to a missing release." >&2
    exit 1
  fi
  cp -al -- "$current_real/." "$stage/"
elif [[ -d "$root/current" ]]; then
  # Accommodate a pre-existing real directory on the very first deployment.
  cp -al -- "$root/current/." "$stage/"
fi

mkdir -p "$stage/map_data" "$stage/resource_data" "$stage/icon_data"
printf 'Remote staging directory prepared.\n'
REMOTE_PREPARE

for directory in map_data resource_data icon_data; do
  echo "===== rsync $directory ====="
  rsync -a --checksum --delete-delay --partial --human-readable --stats \
    -e "$RSYNC_RSH" \
    "$IMAGE_ROOT/$directory/" \
    "$TARGET:$REMOTE_STAGE/$directory/"
done

# Verify the staged tree before publishing it. The build script already checked
# every configured map PNG locally; this is an additional remote-side guard.
ssh "${SSH_OPTS[@]}" "$TARGET" bash -s -- "$REMOTE_ROOT" "$STAGE_NAME" <<'REMOTE_PUBLISH'
set -Eeuo pipefail
root="$1"
stage="$root/$2"

for directory in map_data resource_data icon_data; do
  [[ -d "$stage/$directory" ]] || { echo "Missing $directory in staging tree" >&2; exit 1; }
  if ! find "$stage/$directory" -type f -size +0c -print -quit | grep -q .; then
    echo "$directory is empty in staging tree" >&2
    exit 1
  fi
done

map_count="$(find "$stage/map_data" -maxdepth 1 -type f -name '*.png' -size +0c | wc -l)"
if (( map_count < 6 )); then
  echo "Expected at least 6 non-empty map PNGs; found $map_count" >&2
  exit 1
fi

release_id="${2#.staging-}"
release="$root/releases/$release_id"
[[ ! -e "$release" ]] || { echo "Release already exists: $release" >&2; exit 1; }
mv -- "$stage" "$release"
# If an older manual setup created a real current directory, all its content
# has already been hard-linked into the staging release, so it is safe to unlink.
if [[ -d "$root/current" && ! -L "$root/current" ]]; then
  rm -rf -- "$root/current"
fi
rm -f -- "$root/current.next"
ln -s "releases/$release_id" "$root/current.next"
mv -Tf -- "$root/current.next" "$root/current"

# Keep current plus one previous successful release for rollback.
mapfile -t releases < <(find "$root/releases" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort -r)
kept=0
for name in "${releases[@]}"; do
  kept=$((kept + 1))
  if (( kept > 2 )); then
    rm -rf -- "$root/releases/$name"
  fi
done

printf 'Published release: %s\n' "$release_id"
printf 'Current path: %s\n' "$(readlink -f "$root/current")"
printf 'Retained releases:\n'
find "$root/releases" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort -r
REMOTE_PUBLISH

echo 'Incremental deployment completed successfully.'
