#!/usr/bin/env bash
# Writes, backs up, lists and restores /etc/adapter/cameras/<path>.env -- the file that
# configures a USB camera (guide §22.2). Invoked from the ONVIF admin GUI via sudo, the
# same arrangement as provision-camera.sh: the Flask app runs unprivileged and shells out
# here rather than writing under /etc itself, so "what root writes, and how" stays in one
# reviewable, input-validated script. As provision-camera.sh notes, `vladimir` already has
# unrestricted passwordless sudo on this Pi, so this is input hygiene and a single source
# of truth for the file format -- not a privilege boundary that actually exists here.
#
# It deliberately does NOT restart anything. The units are systemd *user* units, which
# root cannot manage, and the caller has to verify the result and decide about rolling
# back anyway. Keeping root's job to "write this file, keep a copy of the old one" is the
# smallest thing that has to be trusted.
#
#   configure-camera.sh apply  <path> <file>    replace the config, backing up the old one
#   configure-camera.sh list   <path>           backups, newest first: name<TAB>saved<TAB>model
#   configure-camera.sh revert <path> <backup>  restore one (backing up the current first)
#
# Content is validated line by line against a whitelist of keys and value patterns. The
# file is sourced by shell scripts, so anything not matching is refused rather than
# escaped: a value containing a backtick or $( is a command, not a setting.
set -euo pipefail

CAMERAS_DIR="${ADAPTER_CAMERAS_DIR:-/etc/adapter/cameras}"
BACKUP_DIR="$CAMERAS_DIR/backups"
KEEP_BACKUPS="${KEEP_BACKUPS:-20}"
MAX_BYTES=4096

die() { echo "$*" >&2; exit 1; }

valid_path() { [[ "$1" =~ ^cam[0-9]{2}$ ]] || die "invalid camera path: $1"; }

# One pattern per accepted key. Anything else in the file is a refusal, not a warning.
validate() {                      # $1 = file to check
  local file="$1" line key value n=0
  [[ -s "$file" ]] || die "refusing to write an empty configuration"
  [[ "$(stat -c%s "$file")" -le "$MAX_BYTES" ]] || die "configuration is larger than ${MAX_BYTES} bytes"
  while IFS= read -r line || [[ -n "$line" ]]; do
    n=$((n + 1))
    [[ -z "${line//[[:space:]]/}" || "${line#"${line%%[![:space:]]*}"}" == \#* ]] && continue
    [[ "$line" == *=* ]] || die "line $n is not KEY=VALUE: $line"
    key="${line%%=*}"; value="${line#*=}"
    case "$key" in
      CAM_MATCH)        [[ "$value" =~ ^[A-Za-z0-9_.:+-]*$ ]] || die "line $n: bad CAM_MATCH" ;;
      CAM_DEVICE)       [[ "$value" =~ ^(/dev/[A-Za-z0-9_./-]+)?$ ]] || die "line $n: bad CAM_DEVICE" ;;
      CAPS)             [[ "$value" =~ ^(image/jpeg|video/x-raw)(,[a-z_]+=[A-Za-z0-9/]+)*$ ]] || die "line $n: bad CAPS" ;;
      CAM_FPS_OUT|VIDEO_BITRATE|GOP|AUDIO_RATE|H264_LEVEL)
                        [[ "$value" =~ ^[0-9]+$ ]] || die "line $n: $key must be a number" ;;
      H264_PROFILE)     [[ "$value" =~ ^(baseline|main|high)$ ]] || die "line $n: bad H264_PROFILE" ;;
      V4L2_MODE_CTRLS|V4L2_VALUE_CTRLS)
                        [[ "$value" =~ ^([a-z0-9_]+=-?[0-9]+(,[a-z0-9_]+=-?[0-9]+)*)?$ ]] || die "line $n: bad $key" ;;
      AUDIO_CARD)       [[ "$value" =~ ^(hw:CARD=[A-Za-z0-9_]+,DEV=[0-9]+)?$ ]] || die "line $n: bad AUDIO_CARD" ;;
      # Provenance, so a backup can be offered as "AVerMedia PW310 -- 27 Sep 11:02" rather
      # than as a timestamp. Nothing reads these as settings. Spaces are allowed because
      # load_env_file parses KEY=VALUE itself and exports the value literally -- the file
      # is never sourced, so a space cannot start a second word and a metacharacter cannot
      # start a command. The patterns still exclude both, because that guarantee belongs
      # to today's parser and this file outlives it.
      CAM_MODEL)        [[ "$value" =~ ^[A-Za-z0-9\ ._:+/()-]*$ ]] || die "line $n: bad CAM_MODEL" ;;
      CAM_SERIAL)       [[ "$value" =~ ^[A-Za-z0-9_.-]*$ ]] || die "line $n: bad CAM_SERIAL" ;;
      SAVED_AT)         [[ "$value" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] || die "line $n: bad SAVED_AT" ;;
      SAVED_BY)         [[ "$value" =~ ^[a-z_][a-z0-9_-]*$ ]] || die "line $n: bad SAVED_BY" ;;
      *)                die "line $n: unknown setting '$key'" ;;
    esac
  done < "$file"
}

# A backup is named for when it was taken and carries the model it describes, so the GUI
# can offer "AVerMedia PW310 -- 27 Sep 11:02" instead of a bare timestamp.
backup_current() {                # $1 = path; echoes the backup name, or nothing
  local path="$1" live="$CAMERAS_DIR/$path.env" stamp name
  [[ -f "$live" ]] || return 0
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  name="$path.$stamp.env"
  mkdir -p "$BACKUP_DIR"
  # Two backups inside the same second are not hypothetical: revert backs up the current
  # file immediately before restoring one, and without this the new backup lands on the
  # name of the backup being restored -- which silently restored the file it had just
  # replaced. Found by testing revert, not by reading it.
  local seq=2
  while [[ -e "$BACKUP_DIR/$name" ]]; do
    name="$path.$stamp-$seq.env"
    seq=$((seq + 1))
  done
  cp -p "$live" "$BACKUP_DIR/$name"
  # Keep the newest KEEP_BACKUPS; a swap should not be able to fill /etc over time.
  local old
  while read -r old; do [[ -n "$old" ]] && rm -f "$BACKUP_DIR/$old"; done < <(
    ls -1t "$BACKUP_DIR" 2>/dev/null | grep "^$path\..*\.env$" | tail -n "+$((KEEP_BACKUPS + 1))")
  echo "$name"
}

install_file() {                  # $1 = path, $2 = validated source
  local path="$1" src="$2" dest="$CAMERAS_DIR/$path.env" tmp
  mkdir -p "$CAMERAS_DIR"
  tmp="$(mktemp "$CAMERAS_DIR/.$path.XXXXXX")"
  cat "$src" > "$tmp"
  chmod 0644 "$tmp"
  mv -f "$tmp" "$dest"            # rename: readers see the old file or the new one
}

case "${1:-}" in
  apply)
    path="${2:?usage: apply <path> <file>}"; src="${3:?usage: apply <path> <file>}"
    valid_path "$path"
    [[ -f "$src" ]] || die "no such file: $src"
    validate "$src"
    backup="$(backup_current "$path")"
    install_file "$path" "$src"
    echo "applied $CAMERAS_DIR/$path.env${backup:+ (previous saved as $backup)}"
    ;;
  list)
    path="${2:?usage: list <path>}"; valid_path "$path"
    [[ -d "$BACKUP_DIR" ]] || exit 0
    for f in $(ls -1t "$BACKUP_DIR" 2>/dev/null | grep "^$path\..*\.env$" || true); do
      model="$(sed -n 's/^CAM_MODEL=//p' "$BACKUP_DIR/$f" | head -1)"
      saved="$(sed -n 's/^SAVED_AT=//p' "$BACKUP_DIR/$f" | head -1)"
      [[ -n "$saved" ]] || saved="$(date -u -r "$BACKUP_DIR/$f" +%Y-%m-%dT%H:%M:%SZ)"
      printf '%s\t%s\t%s\n' "$f" "$saved" "${model:-unknown camera}"
    done
    ;;
  revert)
    path="${2:?usage: revert <path> <backup>}"; name="${3:?usage: revert <path> <backup>}"
    valid_path "$path"
    # The name comes from a browser, so it is checked rather than trusted: no slashes, no
    # traversal, and it has to be a backup of THIS camera.
    [[ "$name" =~ ^${path}\.[0-9TZ]+(-[0-9]+)?\.env$ ]] || die "invalid backup name: $name"
    [[ -f "$BACKUP_DIR/$name" ]] || die "no such backup: $name"
    validate "$BACKUP_DIR/$name"
    # Copy first, then back up the current file, then install from the copy. Restoring
    # straight from $BACKUP_DIR would depend on backup_current not having touched it.
    staged="$(mktemp)"; trap 'rm -f "$staged"' EXIT
    cat "$BACKUP_DIR/$name" > "$staged"
    backup="$(backup_current "$path")"
    install_file "$path" "$staged"
    echo "reverted to $name${backup:+ (previous saved as $backup)}"
    ;;
  *)
    die "usage: $0 apply <path> <file> | list <path> | revert <path> <backup>"
    ;;
esac
