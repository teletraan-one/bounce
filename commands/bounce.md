---
description: Bounce work to a second AI as a co-equal peer reviewer with direct file access. Both sides must cite sources for factual claims.
argument-hint: "[short description of what you want reviewed]"
---

The user wants to "bounce" a decision/output to a second AI (default: OpenAI's API) for co-equal peer review. The peer reviewer has direct file-access tools (list_files, read_file, grep_workspace, list_memory, read_memory, read_claude_md) — Claude does NOT pre-package the context. The peer picks its own evidence. Source-citation is required from both sides for every factual claim.

`$1` is a short user-supplied description of what's being reviewed. If empty, infer from the recent conversation.

## Step 1 — Write a short, specific prompt

Do NOT dump CLAUDE.md, memory, or files into the prompt. The peer reviewer will fetch them itself via tools. Write a SHORT prompt that explains:

```
# What I'm asking you to review
<one paragraph: the decision, output, claim, or design under review>

# Note on framing (read first)
Treat Claude's "claims to test" below as starting hypotheses, not the frame of the answer. You may reject the premise. If a different question is the right one to ask, name it.

# Claims to test — may be wrong
<2–5 bullets stating what Claude currently thinks/recommends, with citations when available. Use the form: "claim — supporting source (file:line or memory:name or "(inference)")">

# Reasons these claims may be wrong
<1–3 bullets: Claude's own honest worries about the claims above. Where could the reasoning be off?>

# Open questions I want you to weigh in on
<bulleted list of specific points where I want pushback>

# Note on access
You have tools to read this workspace, memory, and CLAUDE.md. Use them — don't trust Claude's summary. Spot-check anything claimed. The user wants source-citations on both sides.
```

**Redaction discipline (project-specific):**

The script's denylist enforces filename-pattern blocks (credentials, secrets, OS state, and any optional presets the project has enabled in `.claude/bounce-config.json`). But the denylist is filename-pattern based — it does NOT detect sensitive content in arbitrarily-named files. Both sides are responsible for catching what the denylist misses:

- **Claude (constructing the prompt):** do not include real PII, credentials, or content your team has marked sensitive. If a file's basename looks innocuous but its contents are sensitive, flag it in the prompt.
- **Peer reviewer (reading via tools):** the system prompt instructs the peer to stop and flag if it encounters what looks like sensitive content even in a file with an innocent name.

## Step 2 — Show the user the prompt, then send

Print the prompt in a fenced ```bounce ``` block so the user can interrupt if anything's off. Then send:

```bash
cat <<'BOUNCE_EOF' | .claude/scripts/bounce.py
<paste the prompt verbatim>
BOUNCE_EOF
```

For iteration tracking on a multi-round bounce, set `BOUNCE_ROUND` and `BOUNCE_DOC_ID` **on the `bounce.py` invocation, not on `cat`** — env-var prefixes in a pipeline apply only to the immediately-following command, so `VAR=x cat <<EOF | bounce.py` sets `VAR` for `cat`, never for `bounce.py`. Pass the heredoc directly to `bounce.py`:

```bash
BOUNCE_ROUND=2 BOUNCE_DOC_ID=stew-briefing .claude/scripts/bounce.py <<'BOUNCE_EOF'
...
BOUNCE_EOF
```

(Or, if you must keep `cat | bounce.py`, put the env-var prefix on the right side: `cat <<EOF | BOUNCE_ROUND=2 BOUNCE_DOC_ID=stew-briefing .claude/scripts/bounce.py`.)

Exit codes:
- 2: `OPENAI_API_KEY` not set → tell the user to add it to `~/.zshenv` or `~/.claude/settings.json` env block, restart Claude Code.
- 4: API error — surface the stderr message verbatim. If "not a chat model" or "temperature unsupported," suggest `BOUNCE_MODEL=<id>` or `BOUNCE_TEMPERATURE=` (unset).
- 6: `auto` model resolution failed — set `BOUNCE_MODEL=<id>` explicitly.
- 7: network/transport failure.
- 8: hit max tool-call iterations.
- 11: ledger Stage 2 hard-gate failed (unresolved challenge, bad enum, missing `reason`, or `corrected`/`defended` missing evidence) when running `--record-resolutions`. Surface the script's gate messages verbatim; the user must complete or fix the resolutions block.
- 12: ledger Stage 1 failed — the reviewer's response was missing or had a malformed `## Challenges` block. The response was still printed; re-bounce with a prompt that more clearly instructs the reviewer to emit the block.

## Step 3 — Present the peer reviewer's response cleanly first

Show the response verbatim in a fenced ``` block. Do NOT critique inline. Do NOT prepend your own framing. The user reads the peer directly, unfiltered.

## Step 4 — Source-citation audit (BOTH sides)

After the response, run a citation audit:

**For the peer's claims:** check the Sources section and inline citations. For each substantive claim, is there a tool call that backs it? Flag any uncited factual claim.

**For your own (Claude's) claims when you disagree:** you must cite your sources too. File path + line, memory name, prior conversation turn, or explicit "this is inference." If you can't produce a source for your view, say so plainly.

## Step 5 — Equal-trust disagreement audit

After the citation pass, audit the substance:

- **Where we agree** — brief.
- **Where the peer pushed back and is right** — name it, accept it.
- **Where the peer pushed back and you (Claude) still hold your position** — explain why, with YOUR sources cited.
- **Where it's a judgment call** — present the tradeoff cleanly, no verdict.
- **What the peer spotted that you missed** — be honest.
- **Uncited claims on either side** — list explicitly.

## Step 5b — Record resolutions to the correction ledger

The peer reviewer ends its response with a `## Challenges` JSON block (each real challenge to one of your claims, with a `challenge_id`). The script logs these into the bounce tally entry (Stage 1) and prints the `run_id`.

After working through the challenges in Steps 4–5, **record how each one resolved** (Stage 2). Every resolution must include `challenge_id`, `resolution`, and `reason`. Pick `resolution` from: `corrected` (restate, evidence-backed — needs `resolution_evidence` + `support_quote`), `removed`, `relabeled`, `defended` (you held with a citation — needs evidence + quote **and** `accepted_by: "chatgpt"` or `"human"`; if the reviewer didn't accept, use `contested` instead), `contested`, or `deferred`. Pipe a `## Resolutions` block back:

````bash
cat <<'RES_EOF' | .claude/scripts/bounce.py --record-resolutions <run_id>
## Resolutions
```json
[{"challenge_id":"c-001","resolution":"corrected","resolution_evidence":"file:line","support_quote":"...","reason":"..."}]
```
RES_EOF
````

The script reconciles by `challenge_id` and **hard-gates** (exit 11) on any unresolved/open challenge, bad enum, missing `reason`, or a `corrected`/`defended` missing evidence — so no admitted error is left merely flagged. A `contested` item legitimately stays open and does not fail the command; don't fabricate resolutions to pass the gate. Query the ledger with `.claude/scripts/bounce.py --corrections-summary [today|7d|all]`.

## Step 6 — Bottom line

One sentence: what (if anything) changes in the current work based on this bounce.

## Step 7 — Offer next moves

- If the peer asked clarifying questions or named context it would have wanted, offer to re-bounce with that prompt added.
- If you want to challenge an uncited claim, offer to send a follow-up bounce.
- Never auto-rebounce. The user decides.

## Iteration protocol

If the peer returns substantive findings, apply fixes and re-bounce (round 2). If only nitpicks, apply silently and stop. Hard stop at 5 rounds. Track `BOUNCE_ROUND` env var per round for diminishing-returns analysis.

## Notes

- Default model: `gpt-5.5`. Override with `BOUNCE_MODEL=<id>`.
- The peer can only read files; it cannot write to the workspace, modify memory, or execute anything else.
- Path safety: `bounce.py` enforces a denylist of credential/OS/sensitive filename patterns. Project-specific patterns load via `.claude/bounce-config.json` presets.
- Secret scanning: every file content runs through a regex scanner that redacts known token patterns (OpenAI `sk-*`/`sk-proj-*`, GitHub `ghp_`/`gho_`/`github_pat_`, GitLab `glpat-`, AWS access key IDs, Google API keys, Stripe `sk_live_`/`rk_live_`, Slack `xox*`, and PEM private-key blocks) before transmission. See `SECRET_PATTERNS` in `bounce.py` for the current list; the scanner does NOT catch every credential format (e.g., AWS secret access keys, JWTs, and bespoke service tokens have no distinctive prefix).
- Audit log at `/tmp/bounce-<timestamp>.log` records every tool call with arguments and a result summary (content hashed, not stored verbatim).
- Cost tally at `~/.claude/bounce-tally/YYYY-MM-DD.jsonl` (persistent, daily). Query with `bounce.py --cost-summary today|7d|all`.
