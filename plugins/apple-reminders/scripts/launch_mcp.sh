#!/bin/sh

# Codex launched from Finder can have a smaller PATH than the user's shell.
# Prefer a supported PATH interpreter, then inspect the fixed locations used by
# Homebrew and python.org without sourcing shell startup files.

set -u

script_directory=$(CDPATH= cd -P "$(/usr/bin/dirname "$0")" && pwd)
plugin_root=$(/usr/bin/dirname "$script_directory")
server_path="$plugin_root/mcp/server.py"
fallback_python=

is_supported_python() {
  [ -n "$1" ] && [ -x "$1" ] &&
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
      >/dev/null 2>&1
}

candidate_path=${PATH-}
candidate_path="${candidate_path:+$candidate_path:}/opt/homebrew/bin:/usr/local/bin"
candidate_path="$candidate_path:/Library/Frameworks/Python.framework/Versions/Current/bin"
candidate_path="$candidate_path:/Library/Frameworks/Python.framework/Versions/3.14/bin"
candidate_path="$candidate_path:/Library/Frameworks/Python.framework/Versions/3.13/bin"
candidate_path="$candidate_path:/Library/Frameworks/Python.framework/Versions/3.12/bin"
candidate_path="$candidate_path:/Library/Frameworks/Python.framework/Versions/3.11/bin"

original_ifs=$IFS
IFS=:
set -f
for directory in $candidate_path
do
  [ -n "$directory" ] || directory=.
  candidate="$directory/python3"
  [ -x "$candidate" ] || continue
  [ -n "$fallback_python" ] || fallback_python=$candidate
  if is_supported_python "$candidate"; then
    PYTHONDONTWRITEBYTECODE=1
    export PYTHONDONTWRITEBYTECODE
    exec "$candidate" "$server_path"
  fi
done
set +f
IFS=$original_ifs

# Preserve the server's structured unsupported-runtime response when an older
# Python is the only interpreter visible.
if [ -n "$fallback_python" ]; then
  PYTHONDONTWRITEBYTECODE=1
  export PYTHONDONTWRITEBYTECODE
  exec "$fallback_python" "$server_path"
fi

printf '%s\n' \
  'Apple Reminders requires Python 3.11 or newer. Install Python, restart Codex, and retry.' \
  >&2
exit 78
