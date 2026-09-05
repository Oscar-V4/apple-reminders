#!/bin/sh

# Codex launched from Finder can have a smaller PATH than the user's shell.
# Prefer a supported PATH interpreter, then inspect the fixed locations used by
# Homebrew and python.org without sourcing shell startup files.

set -u

script_directory=$(CDPATH= cd -P "$(/usr/bin/dirname "$0")" && pwd)
plugin_root=$(/usr/bin/dirname "$script_directory")
server_path="$plugin_root/mcp/server.py"

# /usr/bin/python3 belongs to Apple's developer tools. Merely probing it can
# open the Command Line Tools installer on a Mac that has no toolchain. Resolve
# symlinks before checking, so PATH aliases cannot accidentally invoke it.
is_non_system_python() {
  python_path=$1
  python_links=0
  while :
  do
    python_directory=$(CDPATH= cd -P "$(/usr/bin/dirname "$python_path")" 2>/dev/null && pwd) || return 1
    python_path="$python_directory/${python_path##*/}"
    [ -L "$python_path" ] || break
    python_links=$((python_links + 1))
    [ "$python_links" -le 32 ] || return 1
    python_target=$(/usr/bin/readlink "$python_path") || return 1
    case "$python_target" in
      /*) python_path=$python_target ;;
      *) python_path="$python_directory/$python_target" ;;
    esac
  done
  case "$python_path" in
    "/usr/bin/python3"|"/bin/python3") return 1 ;;
  esac
  # Also recognize hard links to the system shim without executing it.
  [ ! "$python_path" -ef "/usr/bin/python3" ]
}

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
  is_non_system_python "$candidate" || continue
  if is_supported_python "$candidate"; then
    PYTHONDONTWRITEBYTECODE=1
    export PYTHONDONTWRITEBYTECODE
    exec "$candidate" "$server_path" "$@"
  fi
done
set +f
IFS=$original_ifs

printf '%s\n' \
  'Apple Reminders requires Python 3.11 or newer.' \
  'Install the macOS installer from https://www.python.org/downloads/macos/, restart Codex, and retry. Xcode is not required.' \
  >&2
exit 78
