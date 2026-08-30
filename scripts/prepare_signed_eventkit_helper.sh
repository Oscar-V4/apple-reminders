#!/bin/bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: prepare_signed_eventkit_helper.sh \
  --output-directory PATH \
  --identity IDENTITY \
  --team-id TEAM_ID \
  --notary-key PATH \
  --notary-key-id KEY_ID \
  --notary-issuer-id ISSUER_ID \
  --source-commit COMMIT \
  --workflow-commit COMMIT

Builds, signs, notarizes, staples, structurally verifies, and packages the
universal Apple Reminders EventKit helper. It never launches the helper. Run
verify_eventkit_helper.py --run-protocol-probes later, only after every signing
and notarization credential has been removed from the job environment.

The caller owns credential-file and temporary-keychain cleanup.
EOF
}

die() {
  printf 'prepare signed helper failed: %s\n' "$1" >&2
  exit 1
}

require_command() {
  [[ -x "$1" ]] || die "required tool is unavailable: $1"
}

output_directory=""
identity=""
team_id=""
notary_key=""
notary_key_id=""
notary_issuer_id=""
source_commit=""
workflow_commit=""

while (($#)); do
  case "$1" in
    --output-directory)
      (($# >= 2)) || die "--output-directory requires a value"
      output_directory=$2
      shift 2
      ;;
    --identity)
      (($# >= 2)) || die "--identity requires a value"
      identity=$2
      shift 2
      ;;
    --team-id)
      (($# >= 2)) || die "--team-id requires a value"
      team_id=$2
      shift 2
      ;;
    --notary-key)
      (($# >= 2)) || die "--notary-key requires a value"
      notary_key=$2
      shift 2
      ;;
    --notary-key-id)
      (($# >= 2)) || die "--notary-key-id requires a value"
      notary_key_id=$2
      shift 2
      ;;
    --notary-issuer-id)
      (($# >= 2)) || die "--notary-issuer-id requires a value"
      notary_issuer_id=$2
      shift 2
      ;;
    --source-commit)
      (($# >= 2)) || die "--source-commit requires a value"
      source_commit=$2
      shift 2
      ;;
    --workflow-commit)
      (($# >= 2)) || die "--workflow-commit requires a value"
      workflow_commit=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

[[ -n "$output_directory" ]] || die "--output-directory is required"
[[ -n "$identity" && "$identity" != "-" ]] || die "a Developer ID --identity is required"
[[ "$team_id" =~ ^[A-Z0-9]{10}$ ]] || die "--team-id must be 10 uppercase letters or digits"
[[ "$notary_key_id" =~ ^[A-Z0-9]{10}$ ]] || die "--notary-key-id must be 10 uppercase letters or digits"
[[ "$notary_issuer_id" =~ ^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$ ]] ||
  die "--notary-issuer-id must be a UUID"
[[ "$source_commit" =~ ^[0-9a-f]{40}([0-9a-f]{24})?$ ]] ||
  die "--source-commit must be a full lowercase Git object ID"
[[ "$workflow_commit" =~ ^[0-9a-f]{40}([0-9a-f]{24})?$ ]] ||
  die "--workflow-commit must be a full lowercase Git object ID"
[[ -f "$notary_key" && ! -L "$notary_key" ]] ||
  die "notary key must be a regular, non-symlink file"
[[ ! -L "$output_directory" ]] || die "output directory must not be a symlink"

require_command /usr/bin/codesign
require_command /usr/bin/ditto
require_command /usr/bin/git
require_command /usr/bin/python3
require_command /usr/bin/shasum
require_command /usr/bin/xcrun
require_command /usr/sbin/spctl

script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
repository_root=$(CDPATH= cd -- "$script_directory/.." && pwd -P)
plugin_root="$repository_root/plugins/apple-reminders"

actual_commit=$(/usr/bin/git -C "$repository_root" rev-parse HEAD 2>/dev/null) ||
  die "could not resolve the repository HEAD"
[[ "$actual_commit" == "$source_commit" ]] ||
  die "source commit does not match the checked-out HEAD"
[[ -z "$(/usr/bin/git -C "$repository_root" status --porcelain=v1 --untracked-files=all)" ]] ||
  die "source checkout must be clean before preparing a provenance-bound helper"

mkdir -p "$output_directory"
[[ -d "$output_directory" && ! -L "$output_directory" ]] ||
  die "output directory must be a regular directory"
output_directory=$(CDPATH= cd -- "$output_directory" && pwd -P)

app="$output_directory/AppleRemindersEventKitHelper.app"
prestaple_zip="$output_directory/AppleRemindersEventKitHelper-prestaple.zip"
final_zip="$output_directory/AppleRemindersEventKitHelper-notarized.zip"
submission_json="$output_directory/notary-submission.json"
notary_log="$output_directory/notary-log.json"
manifest="$output_directory/eventkit-helper-build.json"
checksums="$output_directory/SHA256SUMS"

for target in "$app" "$prestaple_zip" "$final_zip" "$submission_json" "$notary_log" "$manifest" "$checksums"; do
  [[ ! -e "$target" && ! -L "$target" ]] ||
    die "refusing to overwrite existing output: $target"
done

/usr/bin/python3 "$script_directory/build_eventkit_helper_app.py" \
  --plugin-root "$plugin_root" \
  --output-app "$app" \
  --identity "$identity" \
  --expected-team-id "$team_id"

# Structural verification intentionally does not execute the native helper.
/usr/bin/python3 "$script_directory/verify_eventkit_helper.py" \
  --plugin-root "$plugin_root" \
  --app "$app" \
  --expected-team-id "$team_id" \
  --require-developer-id

/usr/bin/ditto -c -k --keepParent --sequesterRsrc "$app" "$prestaple_zip"

set +e
/usr/bin/xcrun notarytool submit "$prestaple_zip" \
  --key "$notary_key" \
  --key-id "$notary_key_id" \
  --issuer "$notary_issuer_id" \
  --wait \
  --timeout 30m \
  --no-progress \
  --output-format json >"$submission_json"
submit_status=$?
set -e

submission_id=$(/usr/bin/python3 - "$submission_json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    print("")
else:
    value = payload.get("id")
    print(value if isinstance(value, str) else "")
PY
)
submission_status=$(/usr/bin/python3 - "$submission_json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    print("")
else:
    value = payload.get("status")
    print(value if isinstance(value, str) else "")
PY
)

if [[ $submit_status -ne 0 || "$submission_status" != "Accepted" ]]; then
  if [[ -n "$submission_id" ]]; then
    /usr/bin/xcrun notarytool log "$submission_id" \
      --key "$notary_key" \
      --key-id "$notary_key_id" \
      --issuer "$notary_issuer_id" \
      --output-format json >"$notary_log" || true
  fi
  die "Apple notarization was not accepted; inspect the retained notary JSON outputs"
fi

previous_umask=$(umask)
umask 022
/usr/bin/xcrun stapler staple "$app"
umask "$previous_umask"
/usr/bin/xcrun stapler validate "$app"

# The final verification remains non-executing while signing credentials may
# still be available to this process.
/usr/bin/python3 "$script_directory/verify_eventkit_helper.py" \
  --plugin-root "$plugin_root" \
  --app "$app" \
  --expected-team-id "$team_id" \
  --require-developer-id \
  --require-notarized \
  --source-commit "$source_commit" \
  --workflow-commit "$workflow_commit" \
  --write-manifest "$manifest"

/usr/bin/ditto -c -k --keepParent --sequesterRsrc "$app" "$final_zip"
(
  cd "$output_directory"
  /usr/bin/shasum -a 256 \
    "$(basename -- "$final_zip")" \
    "$(basename -- "$manifest")" >"$(basename -- "$checksums")"
)

printf 'Prepared notarized helper without launching it: %s\n' "$final_zip"
printf 'After credential cleanup, run protocol probes with verify_eventkit_helper.py --run-protocol-probes.\n'
