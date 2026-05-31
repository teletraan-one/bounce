# Security Policy

## Reporting a vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Instead, report privately via [GitHub's private vulnerability reporting](https://github.com/teletraan-one/bounce/security/advisories/new). This routes directly to the maintainer and stays confidential until a fix ships.

## What to include

- Version / commit SHA you tested against
- Description of the issue and its impact
- Steps to reproduce
- Suggested fix, if you have one

## What to expect

This is a small open-source project maintained on a best-effort basis.

- Acknowledgment within ~7 days
- Initial assessment (accept / decline / need-more-info) within ~14 days
- For accepted issues, a fix shipped before public disclosure

## In scope

- Path-safety bypasses in `scripts/bounce.py` (anything that lets the peer reviewer read files outside the configured allowed roots, including via symlinks, relative paths, or denylist evasion)
- Secret-scanner gaps that allow known token formats to reach the peer reviewer unredacted
- Audit-log integrity issues (missing reads, falsifiable entries)
- Anything that causes sensitive content to be sent to the peer reviewer (a third-party API) unintentionally — including issues in `commands/bounce.md` or `install.sh` that mislead operators into unsafe defaults
- Injection / shell-escape issues in the CLI surface

## Out of scope

- Vulnerabilities in OpenAI's API itself
- Vulnerabilities in Claude Code itself
- Issues requiring an already-compromised local environment (e.g., a hostile `~/.claude/settings.json` or a malicious bounce-config preset)
- Cost-blowup scenarios from misconfigured pricing — these are operational, not security
- The honest limits documented in the README and `commands/bounce.md` (see below)

## Known security boundaries (documented, not bugs)

These are inherent to the design and explicitly noted in the README:

- The **filename-pattern denylist does not detect sensitive content in innocuously-named files**. A file called `notes.txt` containing real PII will be sent to the peer reviewer. The bidirectional honesty check (instructing both AIs to stop and flag if they see apparent PII) is a detector layered on top, not a hard programmatic block — and it fires only *after* the model has seen the content.
- The **secret scanner covers a specific list of token formats** (see `SECRET_PATTERNS` in `bounce.py`). Formats without a distinctive prefix (AWS secret access keys, JWTs, bespoke service tokens) are not caught.
- The **peer reviewer is a third-party API** (OpenAI by default). Anything its tools fetch is sent over the wire. Redact at the prompt level and via presets; do not assume a hidden classifier will catch what you missed.
- The **error-resolution ledger Stage 2 is operator-invoked**, not automatic. A bounce can finish `awaiting_resolutions`. The gate only covers *admitted* challenges — errors neither model admits still rely on the human audit.

If you find a way to break one of these *beyond* what the README acknowledges (e.g., the denylist itself can be bypassed, or the secret scanner misses a format on its declared list), that's in scope.
