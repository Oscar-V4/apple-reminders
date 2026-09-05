# 0021 — Make Core the default experience

**Status:** Accepted for implementation; release and clean-Mac acceptance remain pending.

The default plugin should let a first-time user finish routine Reminder work without entering the version-sensitive Native Extension or Recovery paths. Keep the existing signed EventKit helper and the Python safety boundary; make experimental tool exposure an explicit launch choice, and remove private URL composition from default Core. This narrows the ordinary installation and support contract while preserving an opt-in path for existing advanced workflows.

## Decision

- Default startup exposes nine tools: `request_reminders_access`, `list_reminder_lists`, `fetch_reminders`, `read_reminder`, `create_reminder`, `change_reminder`, `delete_reminder`, `ensure_reminder_list`, and `diagnose_reminders`.
- Explicit `--experimental` startup adds six tools: `inspect_reminder_native`, `create_reminder_section`, `organize_reminder`, `change_reminder_attachment`, `inspect_recently_deleted`, and `recover_deleted_reminder`. Exposure is fixed for the runtime's lifetime. Default dispatch rejects calls to these disabled tools even when the caller already knows their names.
- Opt-in changes exposure, not compatibility or mutation authority. The exact build/schema admission, compiler requirements where applicable, fresh References, and Receipt read-back gates from [ADR 0020](0020-fail-closed-experimental-runtime-gate.md) still apply. There is no fallback or launch option that admits an unsupported private build.
- Default Core create/change stores and verifies `url` through public EventKit only. A verified Core URL Receipt does not establish a visible native URL attachment, a UI card, or remote-device convergence. Experimental startup retains the existing hybrid EventKit/native URL composition and its partial-result semantics. This supersedes ADR 0020's description of all Core URL writes as hybrid.
- The launcher must reject Apple's `/usr/bin/python3` developer-tool shim before probing it, including resolved aliases, and must not fall back to an interpreter that failed the supported-version check. Python 3.11+ remains a requirement; this change does not provide a self-contained runtime.
- Preserve the signed, notarized EventKit helper and provenance described in [ADR 0019](0019-prebuilt-signed-eventkit-core-helper.md). Python/MCP changes in this work do not require a native rebuild or helper version bump.

## Why this boundary

The existing implementation has valuable bounded reads, identity and Revision checks, durable mutation outcomes, and signed-helper provenance. Expanding private capabilities increases compatibility and recovery work without necessarily improving first-use success. Keeping fifteen tools available by default also lets an agent select private operations for routine requests. A static opt-in boundary reduces that risk without discarding existing contracts or forcing a rewrite.

The reviewed Google Calendar 1.2.7 package is a thin manifest/connector integration; its tool descriptions define bounded discovery and exact-target changes. The reviewed official Messages 1.0.1000926 bundle has a small MCP launcher that delegates to the host's already-installed, signed native client. Neither package has a bundled skill. These are examples of keeping the installation surface small and enforcing behavior in tools/runtime. They do not establish that this plugin can reuse Google's hosted connector or OpenAI's proprietary native client. See the [official Google Calendar package](https://github.com/openai/plugins/tree/main/plugins/google-calendar), [Messages usage and platform limits](https://learn.chatgpt.com/docs/plugins), and [plugin packaging guidance](https://developers.openai.com/plugins/build/plugins).

## Consequences and remaining evidence

Default callers lose implicit visible-URL attachment composition and direct access to experimental tools. Documentation must state that boundary, and callers requiring the legacy composition must select experimental startup deliberately. Starting experimentally does not mean an individual native capability is supported on the current Mac.

Validation must cover both tool inventories and disabled dispatch, Core URL create/change without native composition, retained opt-in hybrid behavior, and launcher rejection of the Apple shim and unsupported Python. These are acceptance requirements, not a claim that this work's checks have finished.

The next distribution priority is a self-contained signed runtime that removes the user's Python installation step. Fresh nondeveloper Mac validation remains necessary for Finder-launched startup, initial TCC grant/denial, revocation, and permission continuity after updates. Existing signed bytes and development-machine tests cannot substitute for that evidence or for observing ordinary users complete real workflows.
