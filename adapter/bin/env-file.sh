# Sourced, not executed. load_env_file FILE exports the KEY=VALUE lines of FILE, but never
# over a variable that is already set: the environment beats the file everywhere in the
# adapter (adapter/config.py follows the same rule). A missing or unreadable FILE is not an
# error here -- callers decide what is required. Same format as systemd's EnvironmentFile=:
# no `export`, no spaces around `=`; values may contain further `=` (v4l2 control lists do).
load_env_file() {
  local file="$1" key value
  [ -r "$file" ] || return 0
  while IFS='=' read -r key value; do
    [[ "$key" =~ ^[A-Z_][A-Z0-9_]*$ ]] || continue   # skips comments and blank lines
    value="${value%\"}"; value="${value#\"}"
    [ -n "${!key:-}" ] || export "$key=$value"
  done < "$file"
}
