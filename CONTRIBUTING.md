# Contributing

This repository is a local Codex plugin prototype for Apple Reminders. Contributions should keep the local adapter conservative, auditable, and explicit about private API boundaries.

## Local Development

Run the focused checks before proposing changes:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests/test_reminders_adapter.py
PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile scripts/reminders_adapter.py scripts/reminders_doctor.py scripts/validate_minis_export.py
PYTHONDONTWRITEBYTECODE=1 python3 scripts/validate_minis_export.py
clang -x objective-c -fobjc-arc -framework Foundation -framework AppKit -fsyntax-only scripts/remkit_attach_image.m
```

If PyYAML is available in the current Python environment, also run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py .
```

## Safety Rules

- Read the relevant Reminders state before writes.
- Keep scans and bulk operations bounded.
- Preserve fields the user did not ask to change.
- Back up the Reminders container before broad repair flows.
- Verify writes with read-back evidence.
- Keep private SQLite writes narrow, schema-checked, and transactional.
- Do not hard-delete Reminders database rows.

## Private API Boundary

The local plugin uses private macOS Reminders storage details and private ReminderKit for iPhone-visible image attachments. Keep this code out of OpenMinis app PRs and public MinisSkills exports unless the target project explicitly accepts that dependency.

## MinisSkills Export

OpenMinis accepts skill contributions through `OpenMinis/MinisSkills`, not through the mirrored `OpenMinis/OpenMinis` app repository. Use only `minis/apple-reminders/` as the contribution source. It targets Minis' built-in `apple-reminders` command surface and deliberately omits this macOS-only adapter. Do not substitute the local Codex skill, whose instructions and evals include private attachment, section, tag, cache, and repair workflows.
