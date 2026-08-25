#!/bin/sh

# Codex launched from Finder can have a smaller PATH than the user's shell.
# Prefer a supported PATH interpreter, then inspect the fixed locations used by
# Homebrew and python.org without sourcing shell startup files.

set -u

script_directory=$(CDPATH= cd -P "$(dirname "$0")" && pwd)
plugin_root=$(dirname "$script_directory")
server_path="$plugin_root/mcp/server.py"
path_python=$(command -v python3 2>/dev/null || true)

is_supported_python() {
  [ -n "$1" ] && [ -x "$1" ] &&
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
      >/dev/null 2>&1
}

for candidate in \
  "$path_python" \
  /opt/homebrew/bin/python3 \
  /usr/local/bin/python3 \
  /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
  /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 \
  /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
  /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
  /Library/Frameworks/Python.framework/Versions/3.11/bin/python3
do
  if is_supported_python "$candidate"; then
    PYTHONDONTWRITEBYTECODE=1
    export PYTHONDONTWRITEBYTECODE
    exec "$candidate" "$server_path"
  fi
done

# Preserve the server's structured unsupported-runtime response when an older
# Python is the only interpreter visible.
if [ -n "$path_python" ]; then
  PYTHONDONTWRITEBYTECODE=1
  export PYTHONDONTWRITEBYTECODE
  exec "$path_python" "$server_path"
fi

printf '%s\n' \
  'Apple Reminders requires Python 3.11 or newer. Install Python, restart Codex, and retry.' \
  >&2
exit 78
