# Copy one image with dual guards and private byte identity

Cross-Reminder image consolidation is a recognized product operation, but raw
attachment export and private backing paths remain outside the public
Interface. The `copy_image` action therefore performs the transfer entirely
inside the guarded Native Extension Module.

## Decision

- The caller supplies a fresh destination `rev1`, a distinct fresh source
  `rev1`, one exact active source image attachment ID, and an idempotency key.
- The Module revalidates both public guards, obtains both current private
  Reminder revisions, and rejects a missing, truncated, ambiguous, deleted, or
  non-image source before dispatch.
- The adapter resolves backing files only inside the Reminders Files container.
  Multiple content-addressed candidates are equivalent only when every regular
  non-symlink file matches the stored source SHA-512.
- The source is copied into a mode-0600 temporary snapshot. Its decoded format,
  byte bounds, dimensions, SHA-256, and stored SHA-512 are checked before both
  Reminder revisions and the exact attachment are re-read immediately before
  the native destination save.
- ReminderKit attaches the snapshot through its image-data path. `verified`
  requires exact destination attachment identity, matching SHA-512, byte size,
  width and height, native decoded-type verification, mobile-visibility
  evidence, an unchanged source, and final public destination read-back.
- Both public References are consumed after dispatch even though the source is
  unchanged. A possible write never receives a fresh Reference or an automatic
  retry instruction.

## UTI normalization

One live source attachment reported `public.png` while both of its
content-addressed files decoded as JPEG. ReminderKit preserved the exact bytes
and SHA-512 but correctly normalized the destination to `public.jpeg`. Requiring
source and destination UTI strings to match would have rejected a byte-exact
copy because of stale source metadata.

Byte identity is therefore SHA-512 plus file size and dimensions. The native
helper independently requires the destination UTI to match the type decoded
from the snapshot bytes. A UTI change is acceptable only under both proofs.

## Local evidence

On macOS 26.5.2 (25F84), Reminders 7.0 (3976), the public MCP copied one exact
image from an active synthetic source into a disposable exact destination. The
public Receipt was `verified`; native inspection and the Reminders UI both
showed one image. The disposable Reminder was then deleted through a fresh
public Reference and exact local absence was verified. User-specific IDs and
attachment content are intentionally omitted from repository evidence.

The initial live run also exposed two hardening defects before final success:
equivalent `.png`/`.jpeg` backing candidates were treated as ambiguous, and a
generic pre-dispatch adapter error used the CLI command name instead of the
guarded operation name. Equivalent candidates now collapse by digest, and the
single proven-no-write CLI envelope is normalized without weakening
post-dispatch uncertainty.

## Consequences

The action closes copy/consolidation without exporting image bytes or paths.
HEIC and other decoded formats remain outside the current PNG/JPEG native input
boundary. A future preview/download tool or format conversion path requires a
separate privacy, fidelity, and payload decision.
