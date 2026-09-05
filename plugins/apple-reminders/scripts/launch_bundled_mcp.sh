#!/bin/sh

# Distribution-stage launcher. The MCP configuration switches to this only
# after both signed runtime capsules and their provenance have been released.
# All utilities are supplied by macOS; startup never discovers or downloads
# Python, invokes developer tools, or changes Gatekeeper/quarantine policy.
set -eu
umask 077
LC_ALL=C
export LC_ALL
unset UNZIP UNZIPOPT ZIPINFO ZIPINFOOPT DITTOABORT DITTONORSRC \
  DITTOKEEPBINARIESPATTERN DITTOKEEPBINARIESDIR DITTO_TEST_OPTIONS

fail() {
  printf 'Apple Reminders: %s\n' "$1" >&2
  exit 78
}

script_directory=$(CDPATH= cd -P "$(/usr/bin/dirname "$0")" && pwd)
plugin_root=$(/usr/bin/dirname "$script_directory")
entrypoint="$plugin_root/mcp/server.py"
if [ "${1-}" = --render-daily-brief ]; then
  entrypoint="$plugin_root/skills/apple-reminders-daily-brief/scripts/render_daily_brief.py"
  shift
fi
runtime_directory="$plugin_root/runtime"
app_name=AppleRemindersPythonRuntime.app

[ "$(/usr/bin/uname -s)" = Darwin ] || fail 'The bundled runtime requires macOS 14 or newer.'
system_version=$(/usr/bin/sw_vers -productVersion)
system_major=${system_version%%.*}
case "$system_major" in ''|*[!0-9]*) fail 'Could not determine the macOS version.' ;; esac
[ "$system_major" -ge 14 ] || fail 'The bundled runtime requires macOS 14 or newer.'
architecture=$(/usr/bin/uname -m)
case "$architecture" in arm64|x86_64) ;; *) fail 'This Mac architecture is not supported.' ;; esac

archive_name="python-runtime-macos-$architecture.zip"
manifest_name="python-runtime-build-$architecture.json"
archive="$runtime_directory/$archive_name"
manifest="$runtime_directory/$manifest_name"
checksums="$runtime_directory/SHA256SUMS"

regular_file() {
  [ ! -L "$1" ] && [ -f "$1" ]
}

[ ! -L "$runtime_directory" ] && [ -d "$runtime_directory" ] ||
  fail 'The bundled runtime is missing or invalid. Reinstall the Apple Reminders plugin and retry.'
for file in "$archive" "$manifest" "$checksums" "$entrypoint"; do
  regular_file "$file" || fail 'A bundled runtime file is missing or invalid. Reinstall the Apple Reminders plugin and retry.'
done

# Require the complete, unambiguous four-file inventory. The release verifier
# validates provenance JSON; startup checks its exact packaged bytes as well.
[ "$(/usr/bin/stat -f %z "$checksums")" -le 1024 ] || fail 'The runtime checksum inventory is invalid. Reinstall the plugin.'
digests=$(/usr/bin/awk -v archive="$archive_name" -v manifest="$manifest_name" '
  BEGIN {
    allowed["python-runtime-macos-arm64.zip"] = 1
    allowed["python-runtime-macos-x86_64.zip"] = 1
    allowed["python-runtime-build-arm64.json"] = 1
    allowed["python-runtime-build-x86_64.json"] = 1
  }
  {
    if (NF != 2 || length($1) != 64 || $1 !~ /^[0-9a-f]+$/ ||
        $0 != $1 "  " $2 || !($2 in allowed) || seen[$2]++) bad = 1
    digest[$2] = $1
  }
  END {
    if (bad || NR != 4 || !(archive in digest) || !(manifest in digest)) exit 1
    print digest[archive], digest[manifest]
  }
' "$checksums") || fail 'The runtime checksum inventory is invalid. Reinstall the plugin.'
archive_hash=${digests%% *}
manifest_hash=${digests#* }
[ "$(/usr/bin/stat -f %z "$archive")" -le 104857600 ] || fail 'The bundled runtime archive exceeds its size limit. Reinstall the plugin.'
[ "$(/usr/bin/stat -f %z "$manifest")" -le 8388608 ] || fail 'The bundled runtime manifest exceeds its size limit. Reinstall the plugin.'
actual_hash=$(/usr/bin/shasum -a 256 "$archive") || fail 'Could not verify the bundled runtime archive.'
[ "${actual_hash%% *}" = "$archive_hash" ] || fail 'The bundled runtime archive checksum does not match. Reinstall the plugin.'
actual_hash=$(/usr/bin/shasum -a 256 "$manifest") || fail 'Could not verify the bundled runtime manifest.'
[ "${actual_hash%% *}" = "$manifest_hash" ] || fail 'The bundled runtime manifest checksum does not match. Reinstall the plugin.'
code_directory_hash=$(/usr/bin/plutil -extract code_directory_hash raw -expect string -o - "$manifest" 2>/dev/null) ||
  fail 'The bundled runtime manifest has no valid signed code identity. Reinstall the plugin.'
[ "${#code_directory_hash}" -eq 40 ] || fail 'The bundled runtime signed code identity is invalid. Reinstall the plugin.'
case "$code_directory_hash" in *[!0-9a-f]*) fail 'The bundled runtime signed code identity is invalid. Reinstall the plugin.' ;; esac

current_uid=$(/usr/bin/id -u)
case "${HOME-}" in /*) ;; *) fail 'An absolute home directory is required for the private runtime cache.' ;; esac

# Do not chmod shared user folders. Only plugin-owned cache directories are
# private; each component is checked before a child is created or inspected.
owned_directory() {
  directory=$1
  [ ! -L "$directory" ] || fail 'The runtime cache contains a symbolic link. Restore the cache directory and retry.'
  if [ ! -e "$directory" ]; then
    /bin/mkdir "$directory" 2>/dev/null || [ -d "$directory" ] || fail 'Could not create the runtime cache directory.'
  fi
  [ ! -L "$directory" ] && [ -d "$directory" ] || fail 'The runtime cache path is not a directory.'
  [ "$(/usr/bin/stat -f %u "$directory")" = "$current_uid" ] || fail 'The runtime cache directory belongs to another user.'
  if [ "$2" = private ]; then
    /bin/chmod 700 "$directory" || fail 'Could not make the runtime cache private.'
  fi
}

owned_directory "$HOME" shared
owned_directory "$HOME/Library" shared
owned_directory "$HOME/Library/Caches" shared
owned_directory "$HOME/Library/Caches/apple-reminders-codex" private
cache_parent="$HOME/Library/Caches/apple-reminders-codex/python-runtime"
owned_directory "$cache_parent" private
cache="$cache_parent/$archive_hash"
owned_directory "$cache" private
ready="$cache/ready"
staging=
cleanup() {
  if [ -n "$staging" ] && [ ! -L "$staging" ] && [ -d "$staging" ]; then
    # A signal can arrive immediately after publication. Never remove the
    # winning instance, even before the publishing command has returned.
    [ ! "$staging/ready" -ef "$ready" ] || return 0
    /bin/rm -rf "$staging"
  fi
}
trap cleanup EXIT
trap 'exit 78' HUP INT TERM

signing_requirement='anchor apple generic and identifier "io.github.oscar-v4.apple-reminders.python-runtime" and certificate leaf[subject.OU] = "V8347N9346" and certificate leaf[field.1.2.840.113635.100.6.1.13] exists'
verify_app() {
  app=$1
  [ ! -L "$app" ] && [ -d "$app" ] || fail 'The cached runtime app is missing or invalid. Reinstall the plugin or remove its Python runtime cache and retry.'
  unsafe_entry=$(/usr/bin/find "$app" \( ! -user "$current_uid" -o \( ! -type f -a ! -type d \) \) -print -quit) || fail 'Could not inspect the cached runtime app.'
  [ -z "$unsafe_entry" ] || fail 'The cached runtime contains an unexpected owner, link, or special file. Remove its Python runtime cache and retry.'
  regular_file "$app/Contents/MacOS/apple-reminders-python" &&
    [ -x "$app/Contents/MacOS/apple-reminders-python" ] || fail 'The bundled Python launcher is missing or not executable. Reinstall the plugin.'
  /usr/bin/codesign --verify --deep --strict --all-architectures --test-requirement "=$signing_requirement" "$app" >&2 ||
    fail 'The bundled runtime signature is invalid. Reinstall the plugin or remove its Python runtime cache and retry.'
  # Team/bundle identity alone would also accept an older signed capsule. The
  # root CodeDirectory binds this exact executable and its sealed resources.
  signature_details=$(/usr/bin/codesign --display --verbose=4 "$app" 2>&1) || fail 'Could not inspect the bundled runtime signed code identity.'
  app_code_hash=$(printf '%s\n' "$signature_details" | /usr/bin/awk '
    /^CDHash=/ {
      count++; hash = substr($0, 8)
      if (length(hash) != 40 || hash !~ /^[0-9a-f]+$/) bad = 1
    }
    END { if (bad || count != 1) exit 1; print hash }
  ') || fail 'The bundled runtime signed code identity is invalid. Reinstall the plugin.'
  [ "$app_code_hash" = "$code_directory_hash" ] ||
    fail 'The runtime code identity does not match its packaged manifest. Remove its Python runtime cache, reinstall the plugin, and retry.'
}

select_cached_instance() {
  regular_file "$ready" && [ "$(/usr/bin/stat -f %u "$ready")" = "$current_uid" ] &&
    [ "$(/usr/bin/stat -f %z "$ready")" -eq 18 ] ||
    fail 'The runtime cache ready receipt is invalid. Remove its Python runtime cache and retry.'
  instance_name=$(/usr/bin/awk '
    NR == 1 && length($0) == 17 && /^instance[.][A-Za-z0-9]+$/ { name = $0; next }
    { bad = 1 }
    END { if (bad || NR != 1) exit 1; print name }
  ' "$ready") || fail 'The runtime cache ready receipt is invalid. Remove its Python runtime cache and retry.'
  instance="$cache/$instance_name"
  [ ! -L "$instance" ] && [ -d "$instance" ] ||
    fail 'The runtime cache instance is missing or invalid. Remove its Python runtime cache and retry.'
  owned_directory "$instance" private
  regular_file "$instance/ready" && [ "$instance/ready" -ef "$ready" ] ||
    fail 'The runtime cache ready receipt does not identify its published instance. Remove its Python runtime cache and retry.'
}

if [ ! -e "$ready" ] && [ ! -L "$ready" ]; then
  staging=$(/usr/bin/mktemp -d "$cache/instance.XXXXXXXX") || fail 'Could not prepare the private runtime cache.'
  owned_directory "$staging" private

  # Validate the complete central-directory listing before extraction. Bounds
  # match the release verifier. Restricted names also make zipinfo line parsing
  # unambiguous and reject traversal, alternate roots, control bytes and links.
  (ulimit -f 8192; /usr/bin/unzip -Z -l -T "$archive" > "$staging/inventory") 2>/dev/null ||
    fail 'The bundled runtime archive could not be inspected. Reinstall the plugin.'
  /usr/bin/awk -v root="$app_name" '
    NR == 1 { if ($0 !~ /^Archive:  /) bad = 1; next }
    NR == 2 {
      if (NF != 9 || $1 != "Zip" || $9 !~ /^[0-9]+$/ || $9 < 1 || $9 > 10000) bad = 1
      expected = $9; next
    }
    /^[0-9]+ files?, / { trailers++; if ($1 != expected) bad = 1; next }
    {
      count++
      if (NF != 9 || ($1 != "drwxr-xr-x" && $1 != "-rw-r--r--" && $1 != "-rwxr-xr-x") ||
          $3 != "unx" || $4 !~ /^[0-9]+$/ || $4 > 52428800 ||
          $5 !~ /^[bt][-x]$/ || $6 !~ /^[0-9]+$/ || ($7 != "stor" && $7 != "defN" && $7 != "defX" && $7 != "defS" && $7 != "defF")) bad = 1
      size += $4
      name = $9
      if (length(name) > 1024 || name !~ /^[A-Za-z0-9_.+@%\/-]+$/ ||
          index(name, root "/") != 1 || name ~ /\/\// || name ~ /(^|\/)\.\.?($|\/)/) bad = 1
      directory = substr($1, 1, 1) == "d"
      if (directory != (substr(name, length(name), 1) == "/") || (directory && $4 != 0)) bad = 1
      sub(/\/$/, "", name)
      key = tolower(name)
      if (seen[key]++) bad = 1
      types[key] = directory ? "directory" : "file"
      names[count] = key
      if (count > 10000 || size > 314572800) bad = 1
    }
    END {
      for (entry in names) {
        parent = names[entry]
        while (sub(/\/[^\/]+$/, "", parent)) {
          if (!(parent in types) || types[parent] != "directory") bad = 1
        }
      }
      if (bad || count != expected || trailers != 1 || types[tolower(root)] != "directory" ||
          types[tolower(root "/Contents/MacOS/apple-reminders-python")] != "file" ||
          types[tolower(root "/Contents/Info.plist")] != "file") exit 1
    }
  ' "$staging/inventory" || fail 'The bundled runtime archive contains invalid paths, modes, types, or sizes. Reinstall the plugin.'
  # Also check local headers and payload CRCs; conflicting central/local names
  # must not reach a different ZIP implementation during extraction.
  /usr/bin/unzip -t -qq "$archive" >&2 || fail 'The bundled runtime ZIP headers or payload are invalid. Reinstall the plugin.'
  /bin/rm "$staging/inventory"
  # ditto preserves stapled-ticket metadata and quarantine. Never pass --noqtn
  # or remove quarantine attributes, even when Gatekeeper rejects a candidate.
  /usr/bin/ditto -x -k --qtn "$archive" "$staging" >&2 || fail 'The bundled runtime could not be extracted. Reinstall the plugin.'
  verify_app "$staging/$app_name"
  /usr/sbin/spctl --assess --type execute "$staging/$app_name" >&2 ||
    fail 'macOS did not approve the bundled runtime. Reinstall the signed plugin and retry.'

  printf '%s\n' "${staging##*/}" > "$staging/ready" || fail 'Could not prepare the runtime cache ready receipt.'
  # POSIX link(1) atomically creates a new regular-file name and never replaces
  # an existing receipt or follows a destination directory. Only fully verified
  # instances can publish. Concurrent losers validate the winner below; an
  # interrupted unpublished instance needs no stale-lock recovery or waiting.
  if ! /bin/link "$staging/ready" "$ready" 2>/dev/null; then
    [ -e "$ready" ] || [ -L "$ready" ] || fail 'Could not publish the runtime cache. Retry in a moment.'
  fi
fi

select_cached_instance
# Check all nested signatures on every launch, including a concurrent winner.
verify_app "$instance/$app_name"
cleanup
staging=
PYTHONDONTWRITEBYTECODE=1
export PYTHONDONTWRITEBYTECODE
# The signed native shim clears all inherited PYTHON* settings, disables user
# site packages/bytecode and invokes only its own embedded interpreter.
exec "$instance/$app_name/Contents/MacOS/apple-reminders-python" "$entrypoint" "$@"
