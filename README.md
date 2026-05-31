# bounce

[![tests](https://github.com/teletraan-one/bounce/actions/workflows/test.yml/badge.svg)](https://github.com/teletraan-one/bounce/actions/workflows/test.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Cross-model peer review for Claude Code, with file access, source citations, secret scanning, and a real audit log.**

Claude does a piece of work. Bounce sends it to a second AI (default: OpenAI's `gpt-5.5`). The peer reviewer reads project files itself, pushes back hard on weak reasoning, and is forced to cite sources for every factual claim — and so is Claude. Both sides catch each other's mistakes. Every file read, every cost, every iteration is logged.

Built because *one* AI can be confidently wrong in ways another AI from a different vendor catches in seconds. Tested in production on stakeholder docs where overclaims would have real consequences.

## What makes this different from existing peer-review tools

| Feature | bounce | [agent-peer-review](https://github.com/jcputney/agent-peer-review) | Single-AI prompting |
|---|---|---|---|
| Two-AI cross-check | ✓ | ✓ | ✗ |
| **Peer reviewer reads files directly** (no Claude pre-packaging) | ✓ | ✗ | n/a |
| **Mandatory source citations on both sides** | ✓ | ✗ | ✗ |
| **Audit log of every tool call + content hashes** | ✓ | ✗ | ✗ |
| **Secret scanner + filename denylist** | ✓ | ✗ | ✗ |
| **Per-bounce + daily cost tracking** | ✓ | ✗ | varies |
| **Iteration metadata for diminishing-returns analysis** | ✓ | ✗ | ✗ |
| Plugin marketplace integration | ✗ (manual install) | ✓ | n/a |
| Focused on PR/diff review | partial | ✓ | n/a |
| General-purpose (code, docs, design decisions) | ✓ | partial | n/a |

If you want PR/code-review specifically and don't need PII protections or citation enforcement, `agent-peer-review` is more mature. Use bounce when you need source-cited peer review on sensitive material or non-code work.

*Comparison reflects a review of `agent-peer-review`'s public README and feature surface as of 2026-05-31, not an exhaustive code audit; if the project has added features since, verify against the latest release.*

## Why it works

The point isn't "two AIs agreed" — it's closer to the opposite. A reviewer model from a *different* lab has *different failure tendencies*, so **cross-model review drastically reduces the blind spots that survive** — especially the ones one model has but the other is positioned to examine. A model checking its own output tends to rubber-stamp; a different-lineage model, told to push back, routinely catches what the first missed. The residual is the narrower set both models *share* (plus anything neither examines closely). **The value is the disagreement that surfaces, not the agreement.**

- **Hallucination defense.** Both sides must cite a file/line or label a claim as inference, and either can challenge the other with "show me." A fabricated claim is more likely to be challenged and easier to catch — the audit log makes every claim checkable rather than merely asserted. **The error-resolution ledger goes further on the *admitted*-error path:** a self-admitted error is hard-gated until it's resolved (corrected/removed/relabeled) and logged — the tool won't clear with an unresolved admission. *Un*-admitted errors still rely on the human audit.
- **Privacy tripwire.** The bidirectional honesty check instructs *both* models to stop if they encounter apparent real PII — even in a file whose name looks innocent — and refuse to continue until a human confirms. This is an instruction-level *detector*, not a hard programmatic PII block, and a second provider does see what it reads — so it doesn't replace anonymization or provider-term review; it's an extra failure-catch layered on top (with the secret scanner and filename denylist).
- **Convergence.** Apply findings, re-bounce, repeat until only nitpicks remain — a refinement loop that *can* produce a better result than either model alone. The per-bounce metadata lets you watch the findings-per-round curve flatten.
- **Verifiable, not "trust me."** Every file read is logged and content-hashed; you can confirm what each model actually saw versus what it claimed.
- **No single-vendor dependence.** If one lab's model has a systematic weakness, the other may not. This *reduces* — it does not *eliminate* — shared AI failure modes.

The honest bound: model diversity reduces — drastically, but doesn't fully eliminate — shared AI failure modes, and the human stays the decision-maker. "Two AIs agreed" is never the safeguard — forced citation, surfaced disagreement, and a human adjudicator are.

## Track record

Not theoretical. In production use on real stakeholder and code work, cross-model bounce caught — each a blind spot one model missed and the other found:

- a **live billing bug in this tool's own cost calculation** (a discount silently not applied, and a usage tier computed at the wrong granularity) — missed *twice* by the authoring model, caught by the reviewer, fixed with unit tests;
- **overclaims** — including the authoring model overstating *bounce's own value* (e.g. calling a review step "not a leak" when a second provider does see the content);
- **dropped requirements**, **doctrine drift**, **cross-file inconsistencies**, and **factual/realism errors**, across 14 documents in two intensive build days.

**Hard numbers from those two days (from the author's local tally — `--cost-summary all`; the tally itself is gitignored and not part of this repo):** 27 bounces total (23 tagged + 4 calibration), 14 documents, $24.20. Most documents converged in 1–3 rounds with findings tapering to nitpicks (the documented stop signal).

This is an operational record, not a controlled benchmark — there's no measured false-negative rate — but every catch is verifiable in the resulting commits. In this operational sample, cross-model review repeatedly found issues the authoring model had missed; we don't claim that pattern generalizes without bound.

## Quick install

```bash
git clone https://github.com/teletraan-one/bounce.git
cd bounce
./install.sh /path/to/your/project
```

The install script copies `bounce.py` to `<project>/.claude/scripts/bounce.py`, the slash command template to `<project>/.claude/commands/bounce.md`, and a starter config to `<project>/.claude/bounce-config.json` (empty presets — opt-in).

Restart Claude Code in the target project. `/bounce` should appear as a slash command.

## Configure your OpenAI key

Add to `~/.claude/settings.json`:

```json
{
  "env": {
    "OPENAI_API_KEY": "sk-..."
  }
}
```

(Do NOT put the key in `~/.zshrc` — Claude Code's Bash tool runs a non-interactive shell that doesn't source it. `~/.zshenv` works too but settings.json is cleaner.)

Restart Claude Code so the env propagates.

## Configure pricing (optional, for cost tracking)

Add `~/.claude/bounce-pricing.json`:

```json
{
  "models": {
    "gpt-5.5": {
      "input": 5.00,
      "output": 30.00,
      "currency": "USD",
      "unit": "per_1m_tokens",
      "source": "https://developers.openai.com/api/docs/models/gpt-5.5",
      "_note": "Placeholder rates — verify against your most recent OpenAI invoice before relying on the dollar figures. The script's built-in KNOWN_PRICING table leaves gpt-5.5 unset on purpose; this external config is where you supply rates you've actually confirmed."
    }
  },
  "budgets": {
    "daily_warn_usd": 5.00
  }
}
```

Without this file, the tally records token counts only (no dollar cost).

## Project-specific denylist (presets)

Bounce ships with a **base denylist** (always active) covering:
- Credentials and OS state (`.env`, `.ssh/`, `.aws/`, `.gnupg/`, `*.key`, `*.pem`, etc.)
- Claude Code settings (`~/.claude/settings.json`)
- Universal "do not send" markers in filenames: `PII`, `confidential`, `deny AI`, `do not use AI`

For project-specific patterns, create `.claude/bounce-config.json`:

```json
{
  "presets": ["my-project"],
  "extra_denylist_patterns": [
    ".*customer-data.*\\.csv$"
  ]
}
```

Then define presets at `.claude/scripts/bounce-presets/my-project.json`:

```json
{
  "name": "my-project",
  "description": "Project-specific denylist patterns.",
  "denylist_patterns": [
    ".*confidential.*",
    "/Internal Reports/.*\\.docx$"
  ]
}
```

See `presets/example-project.json` in this repo for a fuller example showing anonymization-workflow patterns and the per-file precision principle (block specific confirmed-sensitive files rather than entire directories).

## Usage

### As a slash command

```
/bounce
```

(after typing `/bounce`, paste your prompt or describe what you want reviewed)

### Direct CLI

```bash
cat prompt.txt | .claude/scripts/bounce.py
```

### Cost summary

```bash
.claude/scripts/bounce.py --cost-summary today
.claude/scripts/bounce.py --cost-summary 7d
.claude/scripts/bounce.py --cost-summary all
```

If you've been tracking `BOUNCE_DOC_ID` and `BOUNCE_ROUND` (recommended for multi-round iterations on a single doc), the summary will show a per-document, per-round breakdown — useful for measuring diminishing returns on iteration:

```
Cost summary — today (2026-05-29)
  bounces: 4
  total tokens:      817,708
  cost (known):      $4.9252
  by document:
    stew-briefing: 4 bounce(s), 817,708 tokens $4.9252
      round 1: 219,640 tokens, 27 tool calls $1.5990
      round 2: 190,700 tokens, 32 tool calls $1.2202
      round 3: 266,583 tokens, 22 tool calls $1.5175
      round 4: 98,638 tokens, 15 tool calls $0.5901
```

### Error-resolution ledger

When the peer reviewer challenges a claim, bounce doesn't just log it — it forces resolution. The protocol runs in two stages:

**Stage 1 (automatic, every bounce):** The reviewer ends its response with a `## Challenges` JSON block (one entry per real challenge to a claim, each with a `challenge_id`). The script logs these into the tally entry and prints a `run_id`. If the block is missing or malformed, the script exits `12`.

**Stage 2 (operator-invoked):** After working through the challenges, pipe a `## Resolutions` block back to record how each one resolved (outer fence uses four backticks so the inner `` ```json `` block renders correctly):

````bash
cat <<'RES_EOF' | .claude/scripts/bounce.py --record-resolutions <run_id>
## Resolutions

```json
[
  {"challenge_id": "c1", "resolution": "corrected",
   "reason": "Reviewer was right — the README example omitted a required field.",
   "resolution_evidence": "path/to/file.md:42",
   "support_quote": "...the corrected text..."},
  {"challenge_id": "c2", "resolution": "defended",
   "reason": "Held the original claim — the reviewer missed the spec section that defines this behavior.",
   "accepted_by": "chatgpt",
   "resolution_evidence": "design-doc.md:117",
   "support_quote": "...the supporting passage..."},
  {"challenge_id": "c3", "resolution": "contested",
   "reason": "Defended the original claim but the reviewer did not accept the defense; leaving open for human adjudication."}
]
```
RES_EOF
````

**Required fields on every resolution:** `challenge_id`, `resolution` (one of the enum below), and `reason` (free-text justification — why this resolution applies).

**Valid `resolution` values:**
- `corrected` — restate with evidence (also requires `resolution_evidence` + `support_quote`)
- `removed` — claim withdrawn entirely
- `relabeled` — restated under a more honest evidence tag (e.g. fact → inference)
- `defended` — held the original claim (requires `resolution_evidence` + `support_quote` **and** `accepted_by: "chatgpt"` or `"human"` — if the reviewer didn't accept the defense, use `contested` instead)
- `contested` — defended but the reviewer didn't accept; stays legitimately open (ledger status becomes `contested`, command exits 0)
- `deferred` — escalated to human judgment

The script reconciles by `challenge_id` and **hard-gates** (exit `11`) on any unresolved challenge, bad enum, missing `reason`, or `corrected`/`defended` missing evidence — so no admitted error is silently left as just "flagged." `contested` resolutions stay open but do **not** fail the command. Query the ledger anytime:

```bash
.claude/scripts/bounce.py --corrections-summary today
.claude/scripts/bounce.py --corrections-summary 7d
.claude/scripts/bounce.py --corrections-summary all
```

Honest scope: Stage 2 is operator-invoked, not automatic — the initial bounce can finish `awaiting_resolutions`, and the gate covers only *admitted* challenges. Un-admitted errors (a misread neither side caught, or an error both models share) still rely on the human audit layer.

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | required | Your OpenAI key |
| `BOUNCE_MODEL` | `gpt-5.5` | Override model; use `auto` to probe `/v1/models` |
| `BOUNCE_TEMPERATURE` | unset | Set if your model accepts custom temperature |
| `BOUNCE_PRICE_INPUT` | unset | One-shot pricing override ($/1M input tokens) |
| `BOUNCE_PRICE_OUTPUT` | unset | One-shot pricing override ($/1M output tokens) |
| `BOUNCE_ROUND` | unset | Iteration round number for tally tracking |
| `BOUNCE_DOC_ID` | unset | Document identifier for per-doc cost analysis |

## What bounce does NOT do

- It does NOT mechanically reject uncited claims. Citation discipline is enforced by the system prompt to both AIs and verified via the audit log + human review.
- It does NOT detect sensitive content in files with innocuous names. The denylist is filename-pattern based; both AIs are instructed to stop and flag if they encounter sensitive content despite an innocent filename.
- It does NOT auto-resolve admitted errors. The error-resolution ledger Stage 2 (`--record-resolutions`) is operator-invoked — the bounce can finish `awaiting_resolutions`, and the gate only covers *admitted* challenges. Errors neither side admits still rely on the human audit.
- It does NOT replace the human reviewer. Both AIs feed a human; the human decides.
- It does NOT eliminate shared AI failure modes. Two AIs from different vendors reduces single-vendor blind spots but does not eliminate them.

## Iteration / diminishing-returns guidance

Empirical observation from production use: findings-per-dollar drops ~3× per round. Convergence on stakeholder material typically takes 3-4 rounds. Diminishing returns kick in hard at round 4-5. The slash-command protocol recommends a hard stop at 5 rounds (not currently enforced in code — the script just records `BOUNCE_ROUND` to the tally).

| Round | Typical findings | Pattern |
|---|---|---|
| 1 | High (architectural / framing issues) | Always worth it for non-trivial work |
| 2 | Medium (new overclaims introduced while fixing R1) | Worth it for stakeholder docs |
| 3 | Lower (wording subtleties, cross-doc consistency) | Worth it for high-stakes material |
| 4 | Convergence verification | Cheap, often $0 substantive findings |
| 5 | Hard stop | Usually nitpick territory |

For internal/scaffolding work: 1 round (or skip the bounce) is often the right call.

## Limitations

- Python 3.9+ required (uses standard library only — no extra deps).
- macOS / Linux (uses `fcntl.flock` for tally locking; Windows would need a different lock).
- OpenAI API only as the peer reviewer (the model can be configured, but the API client is OpenAI-specific).
- Audit log lives at `/tmp/bounce-<timestamp>.log` (volatile across reboots — for cost tracking use the persistent tally at `~/.claude/bounce-tally/`).
- Memory tools assume Claude Code's project-slug directory structure under `~/.claude/projects/`.

## License

MIT. See LICENSE.

## Backlog / future work

- **Contribute features upstream to `agent-peer-review`** — file access for the peer reviewer, citation discipline, audit log, and PII denylist are real gaps in that tool. Once bounce is more mature, consider PRs.
- **Plugin marketplace integration** — current install is manual via `install.sh`. A Claude Code plugin would be a smoother UX.
- **Multi-provider peer reviewers** — currently OpenAI only. Adding Anthropic-as-reviewer (Claude reviewing Claude with different model versions) would give a same-vendor cross-check option, similar to `claude-peer-review`.
- **Windows support** — `fcntl.flock` for tally locking is Unix-only.
- **Tier-pricing invoice verification** — the >272K long-context tier cost calc (marginal input + whole-call output multipliers) is modeled but not verified against a real OpenAI invoice. The flat per-token calc remains the conservative historical figure.

## Acknowledgments

Inspired by [`agent-peer-review`](https://github.com/jcputney/agent-peer-review) (Claude Code plugin for two-AI code review). Built independently to address file-access, citation, audit, and PII concerns that the existing tools don't cover. Differences detailed in the comparison table above.
