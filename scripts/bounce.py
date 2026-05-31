#!/usr/bin/env python3
"""bounce.py — peer-review bounce with function-calling file access.

Sends a short prompt + the OpenAI function-calling toolset to ChatGPT so it
can read workspace files, memory, and CLAUDE.md directly. ChatGPT picks the
evidence; Claude doesn't pre-select. Every file return is scanned for secrets
and redacted before going over the wire. Every tool call is logged to an
audit file under /tmp so the user can verify what was actually read.

Stdin: the bounce prompt (what's being reviewed, what kind of pushback is wanted).
Stdout: ChatGPT's final response.
Stderr: progress (one line per tool call) + audit log path.

Env:
  OPENAI_API_KEY        required
  BOUNCE_MODEL          default 'gpt-5.5'. Use 'auto' to probe /v1/models.
  BOUNCE_TEMPERATURE    optional; many newer models reject explicit values.
  BOUNCE_PRICE_INPUT    optional USD-per-1M-input-tokens override (one-shot).
  BOUNCE_PRICE_OUTPUT   optional USD-per-1M-output-tokens override (one-shot).

Pricing config (persistent):
  ~/.claude/bounce-pricing.json — JSON with {models: {<id>: {input, output, currency,
  unit, source, updated_at}}, budgets: {daily_warn_usd}}. Env vars override.

Cost tally (persistent, daily JSONL):
  ~/.claude/bounce-tally/YYYY-MM-DD.jsonl — one line per successful bounce.
  Use `bounce.py --cost-summary today|7d|all` to print a summary.

CLI modes:
  bounce.py                       — run a bounce (reads prompt from stdin)
  bounce.py --cost-summary today  — print today's cost summary
  bounce.py --cost-summary 7d     — print last 7 days
  bounce.py --cost-summary all    — print all recorded days

Exit codes:
  0 success
  2 missing API key
  3 empty stdin
  4 API error (non-2xx response)
  6 could not resolve 'auto' model
  7 network/transport failure
  8 hit max tool-call iterations
  9 unknown --cost-summary window
"""

import datetime
import fcntl
import hashlib
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path
from urllib import request, error

# ---------- Configuration ----------

API_KEY = os.environ.get("OPENAI_API_KEY")
MODEL = os.environ.get("BOUNCE_MODEL", "gpt-5.5")
TEMPERATURE = os.environ.get("BOUNCE_TEMPERATURE")
# Optional iteration metadata for diminishing-returns analysis. Set per bounce when
# you're iterating on a document: BOUNCE_ROUND=2 BOUNCE_DOC_ID=stew-briefing
BOUNCE_ROUND = os.environ.get("BOUNCE_ROUND")
BOUNCE_DOC_ID = os.environ.get("BOUNCE_DOC_ID")
WORKSPACE_ROOT = Path.cwd().resolve()

# Project memory root: Claude Code maps workspace path -> slug under ~/.claude/projects/
def _project_slug(p: Path) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", str(p))

MEMORY_ROOT = (Path.home() / ".claude" / "projects" / _project_slug(WORKSPACE_ROOT) / "memory").resolve()
AUDIT_LOG = Path(f"/tmp/bounce-{int(time.time())}.log")

# Cost tracking paths (per-design — daily JSONL, persistent in ~/.claude/)
BOUNCE_PRICING_FILE = Path.home() / ".claude" / "bounce-pricing.json"
TALLY_DIR = Path.home() / ".claude" / "bounce-tally"
TALLY_LOCK = TALLY_DIR / ".lock"
TALLY_SCHEMA_VERSION = 1

# Paths ChatGPT may read from (resolved roots)
ALLOWED_ROOTS = [WORKSPACE_ROOT, MEMORY_ROOT]

# Base denylist — always active. Generic credentials/OS/AI-marker patterns that
# any project should block. Compiled case-insensitively.
BASE_DENYLIST_PATTERNS = [
    # Credentials / shell / OS state
    r"/\.env(\.|$|/)",
    r"/\.envrc$",
    r"/\.ssh/",
    r"/\.claude/settings.*\.json$",
    r"\.key$",
    r"\.pem$",
    r"/id_rsa($|\.)",
    r"/\.git/",
    r"/\.aws/",
    r"/\.gnupg/",
    # Universal "do not send to AI" markers — projects can rely on these by naming convention
    r"(?<![A-Za-z])PII(?![A-Za-z])",
    r"deny[_\- ]?AI",
    r"do[_\- ]?not[_\- ]?use[_\- ]?ai",
    r"confidential",
]

# Optional presets — opt-in via .claude/bounce-config.json `presets` list. Each
# preset file at .claude/scripts/bounce-presets/<name>.json contains a list of
# additional regex patterns. Use these for project-specific PII/case-private
# artifact patterns. The presets dir lives next to the running script when
# installed into a project's .claude/scripts/. As a fallback we also look at
# .claude/scripts/bounce-presets/ relative to cwd, so the script works from
# anywhere as long as the user is in a project that has presets.
PRESETS_DIRS = [
    Path(__file__).parent / "bounce-presets",
    Path.cwd() / ".claude" / "scripts" / "bounce-presets",
]
BOUNCE_CONFIG_FILE = Path.cwd() / ".claude" / "bounce-config.json"


def _load_bounce_config():
    """Load .claude/bounce-config.json from the workspace. Returns {} on any failure."""
    if not BOUNCE_CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(BOUNCE_CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except Exception as e:
        print(f"bounce.py: could not parse {BOUNCE_CONFIG_FILE}: {e}; ignoring", file=sys.stderr)
        return {}


def _load_preset_patterns(preset_name):
    """Load a preset by name from the first PRESETS_DIRS location that has it.
    Returns list of regex pattern strings, or []."""
    for d in PRESETS_DIRS:
        p = d / f"{preset_name}.json"
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("denylist_patterns"), list):
                    return [str(x) for x in data["denylist_patterns"]]
                if isinstance(data, list):
                    return [str(x) for x in data]
            except Exception as e:
                print(f"bounce.py: could not parse preset {preset_name} at {p}: {e}; ignoring", file=sys.stderr)
                return []
    print(f"bounce.py: preset '{preset_name}' not found in any of: "
          f"{', '.join(str(d) for d in PRESETS_DIRS)}", file=sys.stderr)
    return []


def _build_denylist():
    """Combine base + active presets + extra. Returns list of compiled regexes."""
    patterns = list(BASE_DENYLIST_PATTERNS)
    config = _load_bounce_config()
    for preset in (config.get("presets") or []):
        patterns.extend(_load_preset_patterns(preset))
    patterns.extend(config.get("extra_denylist_patterns") or [])
    return [re.compile(p, re.IGNORECASE) for p in patterns]


_DENYLIST_COMPILED = _build_denylist()
# Kept as DENYLIST_PATTERNS for backward compatibility / inspection
DENYLIST_PATTERNS = BASE_DENYLIST_PATTERNS

# Secret detection patterns (applied to every file return)
SECRET_PATTERNS = [
    (re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}"), "openai-project-key"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{30,}"), "openai-api-key"),
    (re.compile(r"ghp_[A-Za-z0-9]{30,}"), "github-personal-token"),
    (re.compile(r"gho_[A-Za-z0-9]{30,}"), "github-oauth-token"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{60,}"), "github-fine-grained-pat"),
    (re.compile(r"glpat-[A-Za-z0-9_\-]{20,}"), "gitlab-personal-token"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "aws-access-key"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{35}"), "google-api-key"),
    (re.compile(r"sk_live_[A-Za-z0-9]{24,}"), "stripe-live-secret-key"),
    (re.compile(r"rk_live_[A-Za-z0-9]{24,}"), "stripe-live-restricted-key"),
    # Match the full PRIVATE KEY block (header + body + footer) — handles cases where
    # both markers are in the same returned window.
    (re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----.*?-----END [A-Z ]+PRIVATE KEY-----", re.DOTALL), "private-key-block"),
    # Fallback: catch the BEGIN or END marker alone in case a partial window is returned
    # (read_file with a line slice that splits the block). Redacting either marker is
    # enough to make the surrounding base64 useless context.
    (re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----"), "private-key-begin"),
    (re.compile(r"-----END [A-Z ]+PRIVATE KEY-----"), "private-key-end"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), "slack-token"),
]

# Tool schemas exposed to OpenAI function calling
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in the workspace matching an optional glob pattern. Use this to discover what exists before reading. Denied paths are filtered out.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob like '*.py', 'docs/**/*.md'. Default '**/*'."}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the workspace or memory directory. Returns content with secret-scan redactions applied. Lines are 1-indexed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative to workspace root, or absolute path under an allowed root."},
                    "start_line": {"type": "integer", "description": "Default 1."},
                    "max_lines": {"type": "integer", "description": "Default 500. Cap 2000."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_workspace",
            "description": "Search files for a regex. Returns matching lines with file path and line number. Capped at 100 matches.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python regex."},
                    "path_glob": {"type": "string", "description": "Default '**/*'."},
                    "case_insensitive": {"type": "boolean", "description": "Default false."},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_memory",
            "description": "List Claude's memory files for this project. Memories are markdown files that persist across sessions.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_memory",
            "description": "Read a memory file by name (with or without .md). Memories live at ~/.claude/projects/<slug>/memory/.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_claude_md",
            "description": "Read the project's CLAUDE.md (operating rules and project truth).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


# ---------- Safety helpers ----------

def is_path_safe(path: Path):
    try:
        resolved = path.expanduser().resolve()
    except Exception as e:
        return False, f"path resolve failed: {e}"

    inside_allowed = any(
        str(resolved).startswith(str(root) + os.sep) or resolved == root
        for root in ALLOWED_ROOTS
    )
    if not inside_allowed:
        return False, f"outside allowed roots: {resolved}"

    path_str = str(resolved)
    for compiled in _DENYLIST_COMPILED:
        if compiled.search(path_str):
            return False, f"matches denylist pattern: {compiled.pattern}"

    return True, ""


def redact_secrets(content: str):
    findings = []
    out = content
    for pattern, name in SECRET_PATTERNS:
        def replacer(m, _n=name):
            findings.append({"type": _n, "preview": m.group(0)[:10] + "..."})
            return f"[REDACTED:{_n}]"
        out = pattern.sub(replacer, out)
    return out, findings


# ---------- Tool implementations ----------

def tool_list_files(args):
    pattern = args.get("pattern") or "**/*"
    try:
        results = []
        for p in WORKSPACE_ROOT.glob(pattern):
            if not p.is_file():
                continue
            ok, _ = is_path_safe(p)
            if not ok:
                continue
            results.append(str(p.relative_to(WORKSPACE_ROOT)))
        results.sort()
        return {"workspace_root": str(WORKSPACE_ROOT), "files": results[:300], "truncated": len(results) > 300}
    except Exception as e:
        return {"error": str(e)}


def tool_read_file(args):
    path_str = args.get("path")
    if not path_str:
        return {"error": "path is required"}
    p = Path(path_str).expanduser()
    if not p.is_absolute():
        p = WORKSPACE_ROOT / p
    ok, reason = is_path_safe(p)
    if not ok:
        return {"error": f"denied: {reason}"}
    if not p.exists() or not p.is_file():
        return {"error": "file not found"}

    start = max(1, int(args.get("start_line") or 1))
    max_lines = min(2000, max(1, int(args.get("max_lines") or 500)))

    if p.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            return {"error": "PDF read requires pypdf — install with: pip install pypdf"}
        try:
            reader = PdfReader(str(p))
            parts: list[str] = []
            for i, page in enumerate(reader.pages, start=1):
                parts.append(f"--- PAGE {i} ---")
                parts.append(page.extract_text() or "")
            text = "\n".join(parts)
        except Exception as e:
            return {"error": f"PDF decode failed: {e}"}
    else:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return {"error": f"read failed: {e}"}

    lines = text.splitlines()
    selected = lines[start - 1 : start - 1 + max_lines]
    body = "\n".join(selected)
    redacted, findings = redact_secrets(body)

    try:
        display_path = str(p.relative_to(WORKSPACE_ROOT))
    except ValueError:
        display_path = str(p)

    out = {
        "path": display_path,
        "start_line": start,
        "lines_returned": len(selected),
        "total_lines": len(lines),
        "content": redacted,
    }
    if findings:
        out["redactions"] = findings
    return out


def tool_grep(args):
    pattern_str = args.get("pattern")
    if not pattern_str:
        return {"error": "pattern is required"}
    path_glob = args.get("path_glob") or "**/*"
    flags = re.IGNORECASE if args.get("case_insensitive") else 0
    try:
        compiled = re.compile(pattern_str, flags)
    except re.error as e:
        return {"error": f"invalid regex: {e}"}

    matches = []
    for p in WORKSPACE_ROOT.glob(path_glob):
        if not p.is_file():
            continue
        ok, _ = is_path_safe(p)
        if not ok:
            continue
        try:
            with p.open(encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    if compiled.search(line):
                        rel = str(p.relative_to(WORKSPACE_ROOT))
                        # Redact FIRST, then truncate — otherwise truncation can split
                        # a secret below its minimum match length and leak the prefix.
                        red_line, _ = redact_secrets(line.rstrip())
                        red_line = red_line[:300]
                        matches.append({"path": rel, "line": i, "text": red_line})
                        if len(matches) >= 100:
                            return {"matches": matches, "truncated": True}
        except Exception:
            continue
    return {"matches": matches, "truncated": False}


def tool_list_memory(args):
    if not MEMORY_ROOT.exists():
        return {"memory_root": str(MEMORY_ROOT), "exists": False, "files": []}
    files = [p.stem for p in MEMORY_ROOT.glob("*.md")]
    files.sort()
    return {"memory_root": str(MEMORY_ROOT), "files": files}


def tool_read_memory(args):
    name = args.get("name")
    if not name:
        return {"error": "name is required"}
    if not name.endswith(".md"):
        name = name + ".md"
    p = (MEMORY_ROOT / name)
    ok, reason = is_path_safe(p)
    if not ok or not p.exists():
        return {"error": f"memory not found or denied: {name}"}
    text = p.read_text(encoding="utf-8", errors="replace")
    redacted, findings = redact_secrets(text)
    lines = redacted.splitlines()
    out = {
        "name": name,
        "path": str(p),
        "content": redacted,
        "start_line": 1,
        "lines_returned": len(lines),
        "total_lines": len(lines),
    }
    if findings:
        out["redactions"] = findings
    return out


def tool_read_claude_md(args):
    p = WORKSPACE_ROOT / "CLAUDE.md"
    ok, reason = is_path_safe(p)
    if not ok:
        return {"error": f"denied: {reason}"}
    if not p.exists():
        return {"error": "CLAUDE.md not found at workspace root"}
    text = p.read_text(encoding="utf-8", errors="replace")
    redacted, findings = redact_secrets(text)
    lines = redacted.splitlines()
    out = {
        "path": "CLAUDE.md",
        "content": redacted,
        "start_line": 1,
        "lines_returned": len(lines),
        "total_lines": len(lines),
    }
    if findings:
        out["redactions"] = findings
    return out


TOOL_DISPATCH = {
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "grep_workspace": tool_grep,
    "list_memory": tool_list_memory,
    "read_memory": tool_read_memory,
    "read_claude_md": tool_read_claude_md,
}


# ---------- Audit log ----------

def audit(event, **kwargs):
    entry = {"ts": time.time(), "event": event, **kwargs}
    try:
        with AUDIT_LOG.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# ---------- Model resolution ----------

def resolve_auto_model():
    """Probe /v1/models for a usable chat model. Returns model id or None."""
    try:
        req = request.Request(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        with request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception:
        data = {"data": []}

    candidates = []
    for m in data.get("data", []):
        mid = m.get("id", "")
        if not re.match(r"^(gpt|chatgpt)-", mid):
            continue
        if re.search(r"embedding|audio|realtime|image|search|tts|whisper|moderation|transcribe", mid):
            continue
        if re.search(r"-(pro|instruct|base)$", mid):
            continue
        if re.search(r"-\d{4}-\d{2}-\d{2}$", mid):
            continue
        ver_match = re.search(r"(\d+(?:\.\d+)?)", mid)
        ver = float(ver_match.group(1)) if ver_match else 0.0
        candidates.append((ver, -len(mid), mid))  # higher ver, shorter name
    candidates.sort(reverse=True)
    if candidates:
        return candidates[0][2]

    # Fallback chain
    for fallback in ("gpt-5.5", "gpt-5", "chatgpt-4o-latest", "gpt-4o"):
        try:
            req = request.Request(
                f"https://api.openai.com/v1/models/{fallback}",
                headers={"Authorization": f"Bearer {API_KEY}"},
            )
            with request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    return fallback
        except Exception:
            continue
    return None


# ---------- API call ----------

# Known per-million-token pricing (USD). Set to None for models where I don't have a
# verified price. Updated 2026-05-29 — replace with current OpenAI pricing when known.
# Unknown models log token counts only; no cost line is computed.
KNOWN_PRICING_USD_PER_1M = {
    "gpt-4o":          {"input": 5.00,  "output": 15.00},
    "gpt-4o-mini":     {"input": 0.15,  "output": 0.60},
    "chatgpt-4o-latest": {"input": 5.00, "output": 15.00},
    # GPT-5/5.5 family: pricing not verified in my knowledge — explicitly None so
    # we don't fabricate a number. Update via BOUNCE_PRICE_INPUT and BOUNCE_PRICE_OUTPUT
    # env vars (USD per 1M tokens) or by editing this dict.
    "gpt-5":           None,
    "gpt-5.5":         None,
}


def _load_pricing_config():
    """Load ~/.claude/bounce-pricing.json. Returns parsed dict or {} on any failure.

    Schema:
      {
        "models": {
          "<model_id>": {
            "input": <float USD per 1M tokens>,
            "output": <float USD per 1M tokens>,
            "currency": "USD",
            "unit": "per_1m_tokens",
            "source": "...",
            "updated_at": "YYYY-MM-DD"
          }
        },
        "budgets": {"daily_warn_usd": <float>}
      }
    """
    if not BOUNCE_PRICING_FILE.exists():
        return {}
    try:
        data = json.loads(BOUNCE_PRICING_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            print(f"bounce.py: pricing config is not a JSON object; ignoring", file=sys.stderr)
            return {}
        return data
    except Exception as e:
        print(f"bounce.py: could not parse {BOUNCE_PRICING_FILE}: {e}; ignoring", file=sys.stderr)
        return {}


def get_pricing(model):
    """Return {"input": float, "output": float, ...} pricing for model, or None.

    Precedence: env vars > pricing JSON > hardcoded known table > None.
    """
    # 1. Env override always wins
    env_in = os.environ.get("BOUNCE_PRICE_INPUT")
    env_out = os.environ.get("BOUNCE_PRICE_OUTPUT")
    if env_in and env_out:
        try:
            return {
                "input": float(env_in),
                "output": float(env_out),
                "source": "env_var",
            }
        except ValueError:
            pass

    # 2. Pricing JSON config
    config = _load_pricing_config()
    models = config.get("models", {}) if isinstance(config, dict) else {}
    entry = models.get(model)
    if isinstance(entry, dict):
        try:
            in_rate = float(entry.get("input"))
            out_rate = float(entry.get("output"))
            if in_rate < 0 or out_rate < 0:
                raise ValueError("negative rate")
            result = {
                "input": in_rate,
                "output": out_rate,
                "currency": entry.get("currency", "USD"),
                "unit": entry.get("unit", "per_1m_tokens"),
                "source": entry.get("source", "pricing_json"),
                "updated_at": entry.get("updated_at"),
            }
            # Pass through optional cost-model fields (caching + long-context tier)
            # so compute_bounce_cost() actually uses the configured rates, not defaults.
            for k in ("cached_input", "tier_threshold_tokens", "tier_input_mult", "tier_output_mult"):
                if k in entry and entry[k] is not None:
                    result[k] = entry[k]
            return result
        except (TypeError, ValueError) as e:
            print(f"bounce.py: pricing config for {model} is malformed ({e}); falling back", file=sys.stderr)

    # 3. Hardcoded known table
    fallback = KNOWN_PRICING_USD_PER_1M.get(model)
    if fallback:
        return {**fallback, "source": "hardcoded"}
    return None


def compute_bounce_cost(prompt_tokens, cached_tokens, completion_tokens, pricing):
    """Compute (input_cost_usd, output_cost_usd, meta) for ONE API request.

    IMPORTANT: this is a per-request calculator. The long-context tier applies
    per API call, so callers MUST sum per-call results (see sum_bounce_costs),
    never pass per-bounce aggregate totals.

    Models two things the original flat calc missed (all rates/thresholds come
    from the pricing config — nothing fabricated in code):

    1. Prompt caching: cached input tokens bill at pricing['cached_input'].
       Falls back to the full input rate if 'cached_input' is absent — no
       invented discount.
    2. Long-context tiering — ONLY when the config explicitly provides all of
       tier_threshold_tokens / tier_input_mult / tier_output_mult. Input tokens
       above the threshold bill at input * tier_input_mult (marginal); if the
       request crosses the threshold, output bills at output * tier_output_mult
       (whole output). A MODEL — verify against an actual invoice; the
       marginal-input / whole-output interaction is an approximation. Models
       without these config keys are NOT tiered (no global default).

    All token counts are non-negative ints; rates are USD per 1M tokens.
    """
    base_in = float(pricing["input"])
    out_rate = float(pricing["output"])
    cached_rate = float(pricing.get("cached_input", base_in))

    # Tiering is opt-in via explicit config — never a global default.
    tier_on = all(k in pricing for k in
                  ("tier_threshold_tokens", "tier_input_mult", "tier_output_mult"))

    cached = max(0, min(int(cached_tokens), int(prompt_tokens)))
    uncached = int(prompt_tokens) - cached

    if tier_on:
        threshold = int(pricing["tier_threshold_tokens"])
        in_mult = float(pricing["tier_input_mult"])
        out_mult = float(pricing["tier_output_mult"])
        over = min(max(0, int(prompt_tokens) - threshold), uncached)  # cached not surcharged
        normal_uncached = uncached - over
        tiered = int(prompt_tokens) > threshold
    else:
        threshold = None
        over = 0
        normal_uncached = uncached
        tiered = False

    input_cost = (cached * cached_rate
                  + normal_uncached * base_in
                  + over * base_in * (in_mult if tier_on else 1.0)) / 1_000_000
    eff_out_rate = out_rate * out_mult if tiered else out_rate
    output_cost = int(completion_tokens) * eff_out_rate / 1_000_000

    return input_cost, output_cost, {
        "cached_tokens": cached,
        "tiered_over_threshold": tiered,
        "tier_threshold_tokens": threshold,
    }


def sum_bounce_costs(usage_records, pricing):
    """Sum per-API-call costs across one bounce. Tiering is per request, so cost
    MUST be computed per call and summed — never on aggregate token totals.

    usage_records: list of {prompt_tokens, cached_prompt_tokens, completion_tokens}.
    Returns (input_cost, output_cost, meta).
    """
    in_total = out_total = 0.0
    any_tiered = False
    for rec in usage_records:
        ic, oc, m = compute_bounce_cost(
            rec.get("prompt_tokens", 0),
            rec.get("cached_prompt_tokens", 0),
            rec.get("completion_tokens", 0),
            pricing,
        )
        in_total += ic
        out_total += oc
        any_tiered = any_tiered or m["tiered_over_threshold"]
    return in_total, out_total, {"any_call_tiered": any_tiered, "api_calls": len(usage_records)}


def get_daily_budget_warn():
    """Return daily_warn_usd from pricing config, or None."""
    config = _load_pricing_config()
    budgets = config.get("budgets", {}) if isinstance(config, dict) else {}
    val = budgets.get("daily_warn_usd")
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


# ---------- Cost tally (daily JSONL, persistent, fcntl-locked) ----------

def _tally_path_for(date_str):
    """Return Path to the JSONL tally file for the given YYYY-MM-DD string."""
    return TALLY_DIR / f"{date_str}.jsonl"


def _ensure_tally_dir():
    """Create the tally dir with chmod 700 if missing."""
    TALLY_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(TALLY_DIR, 0o700)
    except OSError:
        pass


def append_tally(entry):
    """Append a JSON object as one line to today's tally file. Locked + chmod 600.

    The caller is responsible for the entry's schema. This function only handles
    serialization, locking, atomic append, and permissions.
    """
    _ensure_tally_dir()
    date_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    path = _tally_path_for(date_str)
    line = json.dumps(entry, separators=(",", ":")) + "\n"

    # Acquire an exclusive lock around the append. Use a separate lock file so
    # concurrent invocations don't fight over the tally file itself.
    lock_path = TALLY_LOCK
    try:
        with open(lock_path, "w") as lock_fp:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
            try:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line)
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
            finally:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        print(f"bounce.py: tally append failed: {e}", file=sys.stderr)


def _read_tally_for_date(date_str):
    """Return list of parsed entries from a single day's tally file."""
    path = _tally_path_for(date_str)
    if not path.exists():
        return []
    entries = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        print(f"bounce.py: could not read {path}: {e}", file=sys.stderr)
    return entries


def _summarize_entries(entries):
    """Aggregate a list of tally entries. Returns {bounces, prompt, completion,
    total, cost_known_usd, cost_unknown_count, by_model, by_doc}."""
    summary = {
        "bounces": len(entries),
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cost_known_usd": 0.0,
        "cost_unknown_count": 0,
        "by_model": {},
        "by_doc": {},   # group by document_id for diminishing-returns analysis
    }
    for e in entries:
        m = e.get("model", "unknown")
        per = summary["by_model"].setdefault(
            m, {"bounces": 0, "prompt": 0, "completion": 0, "total": 0, "cost_usd": 0.0, "cost_unknown": 0}
        )
        per["bounces"] += 1
        per["prompt"] += int(e.get("prompt_tokens", 0) or 0)
        per["completion"] += int(e.get("completion_tokens", 0) or 0)
        per["total"] += int(e.get("total_tokens", 0) or 0)
        summary["prompt_tokens"] += int(e.get("prompt_tokens", 0) or 0)
        summary["completion_tokens"] += int(e.get("completion_tokens", 0) or 0)
        summary["total_tokens"] += int(e.get("total_tokens", 0) or 0)
        if e.get("cost_known"):
            cost = float(e.get("total_cost_usd", 0) or 0)
            per["cost_usd"] += cost
            summary["cost_known_usd"] += cost
        else:
            per["cost_unknown"] += 1
            summary["cost_unknown_count"] += 1

        # Per-document breakdown with per-round detail (only if doc_id was tracked)
        doc_id = e.get("document_id")
        if doc_id:
            doc = summary["by_doc"].setdefault(
                doc_id, {"bounces": 0, "total_tokens": 0, "cost_usd": 0.0, "rounds": {}}
            )
            doc["bounces"] += 1
            doc["total_tokens"] += int(e.get("total_tokens", 0) or 0)
            if e.get("cost_known"):
                doc["cost_usd"] += float(e.get("total_cost_usd", 0) or 0)
            r = e.get("round")
            if r is not None:
                doc["rounds"][str(r)] = {
                    "tokens": int(e.get("total_tokens", 0) or 0),
                    "cost_usd": float(e.get("total_cost_usd", 0) or 0) if e.get("cost_known") else None,
                    "tool_calls": int(e.get("tool_call_count", 0) or 0),
                }
    return summary


def cost_summary(window):
    """Print a human-readable cost summary for 'today', '7d', or 'all'.

    'today' and '7d' use UTC dates. 'all' walks every jsonl file in the tally dir.
    """
    # Validate window first so an unknown value fails fast even with empty tally
    if window not in ("today", "7d", "all"):
        print(f"bounce.py: unknown window '{window}'. Use today | 7d | all", file=sys.stderr)
        sys.exit(9)

    if not TALLY_DIR.exists():
        print(f"No tally data yet. Tally dir: {TALLY_DIR}")
        return

    today = datetime.datetime.utcnow().date()
    if window == "today":
        dates = [today.strftime("%Y-%m-%d")]
        label = f"today ({dates[0]})"
    elif window == "7d":
        dates = [(today - datetime.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
        label = f"last 7 days (UTC, ending {dates[0]})"
    else:  # "all"
        dates = sorted({p.stem for p in TALLY_DIR.glob("*.jsonl")})
        label = f"all time ({len(dates)} day(s) on record)"

    all_entries = []
    for d in dates:
        all_entries.extend(_read_tally_for_date(d))

    if not all_entries:
        print(f"No bounces in {label}.")
        return

    s = _summarize_entries(all_entries)
    print(f"Cost summary — {label}")
    print(f"  bounces: {s['bounces']}")
    print(f"  prompt tokens:     {s['prompt_tokens']:>10,}")
    print(f"  completion tokens: {s['completion_tokens']:>10,}")
    print(f"  total tokens:      {s['total_tokens']:>10,}")
    if s["cost_known_usd"] > 0:
        print(f"  cost (known):      ${s['cost_known_usd']:.4f}")
    if s["cost_unknown_count"]:
        print(f"  cost (unknown):    {s['cost_unknown_count']} bounce(s) with no pricing configured")
    if s["by_model"]:
        print(f"  by model:")
        for m, per in sorted(s["by_model"].items()):
            cost_part = f" ${per['cost_usd']:.4f}" if per["cost_usd"] > 0 else ""
            unk_part = f" ({per['cost_unknown']} unpriced)" if per["cost_unknown"] else ""
            print(f"    {m}: {per['bounces']} bounce(s), {per['total']:,} tokens{cost_part}{unk_part}")
    if s["by_doc"]:
        print(f"  by document:")
        for doc, info in sorted(s["by_doc"].items()):
            cost_part = f" ${info['cost_usd']:.4f}" if info["cost_usd"] > 0 else ""
            print(f"    {doc}: {info['bounces']} bounce(s), {info['total_tokens']:,} tokens{cost_part}")
            if info["rounds"]:
                # Show round-by-round trajectory (the diminishing-returns view)
                for r in sorted(info["rounds"].keys(), key=lambda x: int(x) if x.isdigit() else 99):
                    rd = info["rounds"][r]
                    rcost = f" ${rd['cost_usd']:.4f}" if rd["cost_usd"] is not None else " (unpriced)"
                    print(f"      round {r}: {rd['tokens']:,} tokens, {rd['tool_calls']} tool calls{rcost}")


def call_api(messages):
    payload = {"model": MODEL, "messages": messages, "tools": TOOLS}
    if TEMPERATURE:
        try:
            payload["temperature"] = float(TEMPERATURE)
        except ValueError:
            pass

    req = request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read())
    except error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            errmsg = json.loads(body).get("error", {}).get("message", body)
        except Exception:
            errmsg = body
        print(f"bounce.py: API error ({e.code}): {errmsg}", file=sys.stderr)
        audit("api_error", code=e.code, message=errmsg)
        sys.exit(4)
    except Exception as e:
        print(f"bounce.py: network/transport failure: {e}", file=sys.stderr)
        audit("network_error", message=str(e))
        sys.exit(7)


# ---------- System prompt ----------

# ---------- Error-admission → resolution ledger ----------
# Implements BOUNCE_Error_Resolution_Spec.md v0.2: a two-stage challenge/resolution
# ledger keyed by challenge_id. Stage 1 = ChatGPT's "## Challenges" (logged into the
# bounce tally entry); Stage 2 = Claude's "## Resolutions" (recorded via
# --record-resolutions, reconciled by challenge_id, with deterministic gates).

CHALLENGE_ERROR_CLASSES = {"fabrication", "miscitation", "overclaim", "unsupported", "contradiction", "other"}
RESOLUTION_TYPES = {"corrected", "removed", "relabeled", "defended", "contested", "deferred"}
RESOLUTIONS_REQUIRING_EVIDENCE = {"corrected", "defended"}
ADMITTED_ERROR_RESOLUTIONS = {"corrected", "removed", "relabeled"}
ACCEPTED_BY_VALUES = {"chatgpt", "human", "not_checked"}
ADMISSION_MARKERS_STRONG = (
    "i cannot source", "i can't source", "i fabricated", "no source exists",
    "does not support the claim", "doesn't support the claim", "that's a fabrication",
)
ADMISSION_MARKERS_SOFT = ("you're right", "you are right", "good catch", "fair point")


def extract_labeled_json_block(text, header):
    """Parse the JSON array under a '## <header>' markdown section.

    Returns: a list (entries), [] (explicit empty), None (no such block), or a sentinel
    string 'INVALID_JSON' / 'INVALID_SHAPE'.
    """
    if not text:
        return None
    # Match a heading/emphasis line for `header`: requires ≥1 leading marker char
    # (#, *, _) so prose like "the Challenges section" won't match, but tolerates
    # `## Challenges`, `**## Challenges**` (models bold headings), and `**Challenges**`.
    m = re.search(r"(?im)^[ \t]*(?:[#*_]+[ \t]*)+" + re.escape(header) + r"\b.*$", text)
    if not m:
        return None
    # Bound the search to THIS section: stop at the next markdown heading so a
    # fence-less section can't accidentally capture a later section's code block.
    rest = text[m.end():]
    nxt = re.search(r"(?m)^[ \t]*#{1,6}[ \t]+\S", rest)
    section = rest[: nxt.start()] if nxt else rest
    fence = re.search(r"```(?:json)?\s*(.*?)```", section, re.DOTALL)
    if not fence:
        return None
    try:
        data = json.loads(fence.group(1).strip())
    except json.JSONDecodeError:
        return "INVALID_JSON"
    return data if isinstance(data, list) else "INVALID_SHAPE"


def validate_challenge(c):
    """Return a list of validation error strings for one challenge dict (empty = valid)."""
    if not isinstance(c, dict):
        return ["not an object"]
    errs = []
    if not c.get("challenge_id"):
        errs.append("missing challenge_id")
    if not c.get("claim"):
        errs.append("missing claim")
    if c.get("error_class") not in CHALLENGE_ERROR_CLASSES:
        errs.append(f"bad error_class: {c.get('error_class')!r}")
    if not c.get("challenge_reason"):
        errs.append("missing challenge_reason")
    return errs


def validate_resolution(r):
    """Return a list of validation error strings for one resolution dict (empty = valid)."""
    if not isinstance(r, dict):
        return ["not an object"]
    errs = []
    if not r.get("challenge_id"):
        errs.append("missing challenge_id")
    res = r.get("resolution")
    if res not in RESOLUTION_TYPES:
        errs.append(f"bad resolution: {res!r}")
    if not r.get("reason"):
        errs.append("missing reason")
    if res in RESOLUTIONS_REQUIRING_EVIDENCE:
        if not r.get("resolution_evidence"):
            errs.append(f"{res} requires resolution_evidence")
        if not r.get("support_quote"):
            errs.append(f"{res} requires support_quote")
    acc = r.get("accepted_by")
    if acc is not None and acc not in ACCEPTED_BY_VALUES:
        errs.append(f"bad accepted_by: {acc!r}")
    # A 'defended' hold is only an accepted hold if the challenger/human accepted it;
    # otherwise it's still open and must be logged as 'contested' (spec §5).
    if res == "defended" and acc not in ("chatgpt", "human"):
        errs.append("defended requires accepted_by chatgpt|human (else use contested)")
    return errs


def reconcile_challenges(challenges, resolutions):
    """Match resolutions to challenges by challenge_id.

    Returns {resolved, open, errors, warnings}. Hard-gate conditions go to 'errors'
    (invalid challenge/resolution, unknown challenge_id, unresolved/open challenge);
    non-blocking notes go to 'warnings' (e.g. contested).
    """
    challenges = challenges or []
    resolutions = resolutions or []
    errors, warnings = [], []
    by_id = {}
    for c in challenges:
        ce = validate_challenge(c)
        if ce:
            errors.append(f"challenge {c.get('challenge_id', '?')}: {'; '.join(ce)}")
            continue
        cid = c["challenge_id"]
        if cid in by_id:
            errors.append(f"duplicate challenge_id: {cid}")
            continue
        by_id[cid] = None
    seen_res = set()
    for r in resolutions:
        rerr = validate_resolution(r)
        if rerr:
            errors.append(f"resolution {r.get('challenge_id', '?')}: {'; '.join(rerr)}")
            continue
        cid = r["challenge_id"]
        if cid in seen_res:
            errors.append(f"duplicate resolution for challenge_id: {cid}")
            continue
        seen_res.add(cid)
        if cid not in by_id:
            errors.append(f"resolution references unknown challenge_id: {cid}")
            continue
        by_id[cid] = r
    resolved, open_ids = [], []
    for cid, r in by_id.items():
        if r is None:
            open_ids.append(cid)
            errors.append(f"challenge {cid} unresolved (open)")
        elif r.get("resolution") == "contested":
            open_ids.append(cid)
            warnings.append(f"challenge {cid} contested — defense not accepted, still open")
        else:
            resolved.append(cid)
    return {"resolved": resolved, "open": open_ids, "errors": errors, "warnings": warnings}


def scan_admission_markers(text):
    """Heuristic scan for self-admission phrases. Returns {'strong': [...], 'soft': [...]}."""
    t = (text or "").lower()
    return {
        "strong": [m for m in ADMISSION_MARKERS_STRONG if m in t],
        "soft": [m for m in ADMISSION_MARKERS_SOFT if m in t],
    }


def summarize_corrections(entries):
    """Aggregate challenge/resolution stats across bounce tally entries.

    These are CHALLENGE-RESOLUTION statistics, not an error rate (self-report bounded —
    see spec §4/§6). Joins a bounce entry's `challenges` to its `resolutions` by id.
    """
    s = {"bounces_with_challenges": 0, "challenges": 0, "by_error_class": {},
         "by_resolution": {}, "admitted_errors": 0, "defended": 0, "contested": 0,
         "deferred": 0, "open_or_unrecorded": 0}
    for e in entries:
        chs = e.get("challenges") or []
        if not chs:
            continue
        s["bounces_with_challenges"] += 1
        res_by_id = {r.get("challenge_id"): r for r in (e.get("resolutions") or []) if isinstance(r, dict)}
        for c in chs:
            if not isinstance(c, dict):
                continue
            s["challenges"] += 1
            ec = c.get("error_class", "other")
            s["by_error_class"][ec] = s["by_error_class"].get(ec, 0) + 1
            r = res_by_id.get(c.get("challenge_id"))
            res = r.get("resolution") if isinstance(r, dict) else None
            if res is None:
                s["open_or_unrecorded"] += 1
            else:
                s["by_resolution"][res] = s["by_resolution"].get(res, 0) + 1
                if res in ADMITTED_ERROR_RESOLUTIONS:
                    s["admitted_errors"] += 1
                elif res == "defended":
                    s["defended"] += 1
                elif res == "contested":
                    s["contested"] += 1
                elif res == "deferred":
                    s["deferred"] += 1
    s["hold_rate"] = (s["defended"] / s["challenges"]) if s["challenges"] else 0.0
    return s


def _write_entries_unlocked(date_str, entries):
    """Write entries to a day's tally file via temp+replace. CALLER MUST HOLD THE LOCK."""
    path = _tally_path_for(date_str)
    tmp = path.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def update_tally_entry(date_str, run_id, updater):
    """Locked read-modify-write of one run_id's tally entry (closes the race).

    `updater(entry)` mutates the matched entry in place and returns a value.
    Returns ("ok", updater_result) | ("not_found", None) | ("duplicate", count).
    Read and write happen inside one exclusive lock so a concurrent append can't be lost.
    """
    _ensure_tally_dir()
    with open(TALLY_LOCK, "w") as lock_fp:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
        try:
            entries = _read_tally_for_date(date_str)
            matches = [e for e in entries if e.get("run_id") == run_id]
            if not matches:
                return ("not_found", None)
            if len(matches) > 1:
                return ("duplicate", len(matches))
            result = updater(matches[0])
            _write_entries_unlocked(date_str, entries)
            return ("ok", result)
        finally:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)


def record_resolutions(run_id):
    """Stage-2: ingest a `## Resolutions` block (stdin) for a prior bounce, reconcile by
    challenge_id, apply gates, write back into the tally entry. Exit 11 on hard-gate."""
    raw = sys.stdin.read().strip()
    res = extract_labeled_json_block(raw, "Resolutions")
    if res is None:
        try:
            res = json.loads(raw)
        except json.JSONDecodeError:
            res = "INVALID_JSON"
    if res in ("INVALID_JSON", "INVALID_SHAPE") or not isinstance(res, list):
        print("bounce.py: --record-resolutions expects a `## Resolutions` JSON array on stdin.", file=sys.stderr)
        sys.exit(9)
    if not re.match(r"^\d{4}-\d{2}-\d{2}T", run_id):
        print(f"bounce.py: run_id {run_id!r} is not in the expected YYYY-MM-DDT... format.", file=sys.stderr)
        sys.exit(9)
    date_str = run_id[:10]

    recbox = {}

    def _upd(entry):
        rec = reconcile_challenges(entry.get("challenges", []), res)
        entry["resolutions"] = res
        if rec["errors"]:
            entry["ledger_status"] = "gated"
        elif rec["open"] or rec["warnings"]:
            entry["ledger_status"] = "contested"   # contested/open is NOT clean
        else:
            entry["ledger_status"] = "clean"
        recbox.update(rec)
        return rec

    status, _ = update_tally_entry(date_str, run_id, _upd)
    if status == "not_found":
        print(f"bounce.py: no bounce with run_id {run_id} on {date_str}.", file=sys.stderr)
        sys.exit(10)
    if status == "duplicate":
        print(f"bounce.py: multiple tally entries share run_id {run_id} — refusing to guess.", file=sys.stderr)
        sys.exit(10)

    rec = recbox
    print(f"bounce.py: recorded {len(res)} resolution(s) for {run_id}: "
          f"{len(rec['resolved'])} resolved, {len(rec['open'])} open.", file=sys.stderr)
    for w in rec["warnings"]:
        print(f"bounce.py: WARN — {w}", file=sys.stderr)
    if rec["errors"]:
        for er in rec["errors"]:
            print(f"bounce.py: GATE — {er}", file=sys.stderr)
        print("bounce.py: ledger gate failed (unresolved/invalid). Resolve all challenges and re-record.", file=sys.stderr)
        sys.exit(11)


def corrections_summary(window):
    """Print challenge-resolution statistics for today | 7d | all."""
    if window not in ("today", "7d", "all"):
        print(f"bounce.py: unknown window '{window}'. Use today | 7d | all", file=sys.stderr)
        sys.exit(9)
    if not TALLY_DIR.exists():
        print(f"No tally data yet. Tally dir: {TALLY_DIR}")
        return
    today = datetime.datetime.utcnow().date()
    if window == "today":
        dates = [today.strftime("%Y-%m-%d")]
    elif window == "7d":
        dates = [(today - datetime.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    else:
        dates = sorted({p.stem for p in TALLY_DIR.glob("*.jsonl")})
    entries = []
    for d in dates:
        entries.extend(_read_tally_for_date(d))
    s = summarize_corrections(entries)
    print(f"Correction ledger — {window} (challenge-resolution statistics, NOT an error rate)")
    print(f"  bounces with challenges: {s['bounces_with_challenges']}")
    print(f"  total challenges:        {s['challenges']}")
    if s["challenges"]:
        print(f"  admitted errors (corrected/removed/relabeled): {s['admitted_errors']}")
        print(f"  defended (accepted): {s['defended']}   contested: {s['contested']}   deferred: {s['deferred']}")
        print(f"  open / unrecorded:   {s['open_or_unrecorded']}")
        print(f"  hold rate (defended/total): {s['hold_rate'] * 100:.0f}%  (process signal, confounded — see spec §4)")
        if s["by_error_class"]:
            print("  by error class: " + ", ".join(f"{k}={v}" for k, v in sorted(s["by_error_class"].items())))
        if s["by_resolution"]:
            print("  by resolution:  " + ", ".join(f"{k}={v}" for k, v in sorted(s["by_resolution"].items())))
    else:
        print("  (no challenges logged yet — bounces predating the ledger, or clean runs)")


SYSTEM_PROMPT = """Work synergistically with me (Claude) to make this 10/10. We are treating this as a collaboration to benefit the user. Not a competition to pick one over the other.

You are a peer reviewer with equal authority to Claude. The user trusts you and Claude equally. This is co-equal peer review, not second opinion.

YOU HAVE DIRECT FILE-ACCESS TOOLS. Use them. Do not assume Claude included the right context — verify. List files first to see what exists. Read CLAUDE.md yourself. Read the relevant memory files yourself. Grep when looking for specifics. Read the actual source files you're being asked to review.

CITATION PROTOCOL (load-bearing):
- Every factual claim must be backed by a tool call you actually made.
- Cite inline: "(read_file: bounce.py:142)" or "(grep: 3 hits in bounce.md)".
- If you state something without a tool call to back it, label it INFERENCE or SPECULATION explicitly.
- If Claude (in the user prompt) makes a claim you cannot verify with your tools, say: "Where did you get that? Show me." — and propose what tool call would verify it.

HONESTY CHECK ON SENSITIVE CONTENT (non-optional):
The file-access path safety is filename-pattern based and does NOT catch all sensitive data. If, while reading files, you encounter what looks like real PII or case-private content — real personal names + contact info, raw grant application text, Cadre evaluation reports, anonymization mapping content (original names, emails, phone numbers, Drive URLs, grant numbers, Airtable record IDs), or any file whose innocuous basename hides case-private content — you MUST:
1. Stop opining on the review topic.
2. State clearly: "I encountered what looks like [type] in [file:lines]. This may be case-private content that should not be in this bounce."
3. Ask the user to confirm whether this was intentional before continuing.
The user explicitly chose this honesty protocol over a blanket extension block. They are relying on both sides to catch what the denylist misses. This is non-optional.

YOUR JOB: push back hard on weak reasoning, unstated assumptions, missing evidence, and overconfident claims — whether from Claude or the user. Where Claude is right, say so briefly. Where Claude is wrong or made a debatable call, name it specifically and explain what you would do instead, with citations.

Collaboration framing: push back hard AND build on what's good, not tear down to look stronger. If you and Claude land on different but compatible answers, name the synthesis. The user wins when we both make it stronger.

Be direct, not diplomatic. No flattery. No "great question." Treat the user as an experienced developer/project lead.

END YOUR RESPONSE WITH:
1. **Bottom line** — one sentence.
2. **Most important thing to reconsider** — one item, or "nothing — current direction is sound."
3. **Sources** — a list of the tool calls you actually made, with a one-line note on what each contributed (or "no tool calls — relied entirely on user prompt" if you didn't read anything).
4. A markdown heading line that reads exactly `## Challenges` (a real heading — do NOT bold it, do not write `**## Challenges**`), then immediately a fenced ```json array listing every factual claim of Claude's (or the user's) that you challenged as wrong, unsupported, or overstated. One object per challenge:
   {"challenge_id": "c-001", "claim": "<short text of the claim>", "challenged_by": "chatgpt", "author": "claude", "error_class": "fabrication|miscitation|overclaim|unsupported|contradiction|other", "challenge_reason": "<why it's wrong/unsupported>", "status": "open"}
   Use an empty array [] if you challenged nothing (a clean bounce — an honest signal in itself). This block is logged into the correction ledger; the authoring model records the matching resolutions separately by challenge_id. Do NOT invent challenges to pad it — only real ones.
"""


# ---------- Main ----------

def main():
    global MODEL

    # CLI mode: --cost-summary today|7d|all
    if len(sys.argv) > 1 and sys.argv[1] == "--cost-summary":
        window = sys.argv[2] if len(sys.argv) > 2 else "today"
        cost_summary(window)
        return

    # CLI mode: --corrections-summary today|7d|all (challenge-resolution stats)
    if len(sys.argv) > 1 and sys.argv[1] == "--corrections-summary":
        window = sys.argv[2] if len(sys.argv) > 2 else "today"
        corrections_summary(window)
        return

    # CLI mode: --record-resolutions <run_id> (Stage 2; reads ## Resolutions from stdin)
    if len(sys.argv) > 1 and sys.argv[1] == "--record-resolutions":
        if len(sys.argv) < 3:
            print("bounce.py: --record-resolutions requires a <run_id>.", file=sys.stderr)
            sys.exit(9)
        record_resolutions(sys.argv[2])
        return

    if not API_KEY:
        print("bounce.py: OPENAI_API_KEY not set.", file=sys.stderr)
        print("  Add to ~/.zshenv OR ~/.claude/settings.json env block.", file=sys.stderr)
        sys.exit(2)

    user_prompt = sys.stdin.read().strip()
    if not user_prompt:
        print("bounce.py: empty input on stdin.", file=sys.stderr)
        sys.exit(3)

    if MODEL == "auto" or MODEL == "latest":
        resolved = resolve_auto_model()
        if not resolved:
            print("bounce.py: could not resolve auto model. Set BOUNCE_MODEL=<id>.", file=sys.stderr)
            sys.exit(6)
        print(f"bounce.py: resolved auto -> {resolved}", file=sys.stderr)
        MODEL = resolved

    print(f"bounce.py: model={MODEL}, audit_log={AUDIT_LOG}", file=sys.stderr)
    audit("bounce_start", model=MODEL, workspace=str(WORKSPACE_ROOT), memory_root=str(MEMORY_ROOT))

    # Bounce run identity + timing
    start_time = time.time()
    run_id = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%SZ-") + secrets.token_hex(3)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    # Token + tool-call accumulators across all API calls in this bounce
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "cached_prompt_tokens": 0, "total_tokens": 0, "api_calls": 0}
    usage_records = []  # per-API-call usage; cost is summed per call (tier is per request)
    tool_call_count = 0

    max_iter = 30
    for i in range(max_iter):
        response = call_api(messages)
        msg = response["choices"][0]["message"]

        # Accumulate usage from this API call
        usage = response.get("usage") or {}
        total_usage["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
        total_usage["completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)
        total_usage["total_tokens"] += int(usage.get("total_tokens", 0) or 0)
        # Cached prompt tokens (OpenAI: usage.prompt_tokens_details.cached_tokens),
        # billed at the cheaper cached_input rate when the model supports caching.
        _details = usage.get("prompt_tokens_details") or {}
        _cached = int(_details.get("cached_tokens", 0) or 0)
        total_usage["cached_prompt_tokens"] += _cached
        total_usage["api_calls"] += 1
        # Per-call record so cost is summed per request (tier is per request, not per bounce).
        usage_records.append({
            "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "cached_prompt_tokens": _cached,
            "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
        })
        audit("api_call_usage", iteration=i, usage=usage)

        # Append exactly what the API returned so tool_call_ids match
        messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})

        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            final = msg.get("content", "")
            elapsed_seconds = round(time.time() - start_time, 2)

            # Compute cost if pricing is known
            pricing = get_pricing(MODEL)
            cost_known = pricing is not None
            tally_entry = {
                "schema_version": TALLY_SCHEMA_VERSION,
                "run_id": run_id,
                "timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "model": MODEL,
                "prompt_tokens": total_usage["prompt_tokens"],
                "completion_tokens": total_usage["completion_tokens"],
                "cached_prompt_tokens": total_usage["cached_prompt_tokens"],
                "total_tokens": total_usage["total_tokens"],
                "api_calls": total_usage["api_calls"],
                "tool_call_count": tool_call_count,
                "elapsed_seconds": elapsed_seconds,
                "status": "success",
                "cost_known": cost_known,
            }
            # Iteration metadata for diminishing-returns analysis (optional)
            if BOUNCE_ROUND:
                try:
                    tally_entry["round"] = int(BOUNCE_ROUND)
                except ValueError:
                    tally_entry["round"] = BOUNCE_ROUND
            if BOUNCE_DOC_ID:
                tally_entry["document_id"] = BOUNCE_DOC_ID

            # Stage-1 of the error ledger: capture + validate ChatGPT's `## Challenges`.
            # Deterministic problems hard-gate (exit 12, after the response is printed);
            # heuristic admission markers only warn (false-positive-prone).
            stage1_gate = None
            ch_block = extract_labeled_json_block(final, "Challenges")
            if ch_block is None:
                stage1_gate = "no `## Challenges` block emitted"
            elif ch_block in ("INVALID_JSON", "INVALID_SHAPE"):
                stage1_gate = f"`## Challenges` block unparseable ({ch_block})"
            else:  # a list
                tally_entry["challenges"] = ch_block
                bad = []
                for c in ch_block:
                    if not isinstance(c, dict):
                        bad.append("?: not an object")
                    elif validate_challenge(c):
                        bad.append(f"{c.get('challenge_id', '?')}: {'; '.join(validate_challenge(c))}")
                ids = [c.get("challenge_id") for c in ch_block if isinstance(c, dict)]
                if bad:
                    stage1_gate = "malformed challenge(s): " + " | ".join(bad)
                elif len(ids) != len(set(ids)):
                    stage1_gate = "duplicate challenge_id(s) in `## Challenges`"
            adm = scan_admission_markers(final)
            if (adm["strong"] or adm["soft"]) and not (isinstance(ch_block, list) and ch_block):
                print(f"bounce.py: WARN — admission markers {adm['strong'] + adm['soft']} found "
                      f"but no challenges logged — verify nothing was conceded unlogged.", file=sys.stderr)
            if stage1_gate:
                tally_entry["ledger_status"] = "stage1_gated"
            elif isinstance(ch_block, list) and ch_block:
                tally_entry["ledger_status"] = "awaiting_resolutions"
                print(f"bounce.py: ledger — {len(ch_block)} challenge(s) logged; "
                      f"record resolutions with: bounce.py --record-resolutions {run_id}", file=sys.stderr)
            else:
                tally_entry["ledger_status"] = "clean"  # explicit empty [] = clean bounce

            audit_summary = dict(total_usage)
            if pricing:
                # Cost is summed PER API CALL (tier applies per request, not per bounce).
                in_cost, out_cost, cost_meta = sum_bounce_costs(usage_records, pricing)
                tally_entry["pricing_per_1m"] = {
                    "input": pricing["input"],
                    "output": pricing["output"],
                    "cached_input": pricing.get("cached_input", pricing["input"]),
                    "tier_input_mult": pricing.get("tier_input_mult"),
                    "tier_output_mult": pricing.get("tier_output_mult"),
                    "tier_threshold_tokens": pricing.get("tier_threshold_tokens"),
                    "currency": pricing.get("currency", "USD"),
                    "source": pricing.get("source", "unknown"),
                }
                tally_entry["input_cost_usd"] = round(in_cost, 6)
                tally_entry["output_cost_usd"] = round(out_cost, 6)
                tally_entry["total_cost_usd"] = round(in_cost + out_cost, 6)
                tally_entry["cost_model"] = cost_meta  # any_call_tiered, api_calls
                if cost_meta["any_call_tiered"]:
                    tally_entry["cost_note"] = (
                        "at least one API call crossed the long-context threshold — "
                        "tier applied per request (modeled, verify against invoice)"
                    )
                audit_summary["pricing_per_1m"] = tally_entry["pricing_per_1m"]
                audit_summary["input_cost_usd"] = tally_entry["input_cost_usd"]
                audit_summary["output_cost_usd"] = tally_entry["output_cost_usd"]
                audit_summary["total_cost_usd"] = tally_entry["total_cost_usd"]
                audit_summary["cost_model"] = cost_meta
            else:
                tally_entry["cost_note"] = (
                    f"no pricing for {MODEL}; set BOUNCE_PRICE_INPUT/BOUNCE_PRICE_OUTPUT, "
                    f"add entry to {BOUNCE_PRICING_FILE}, or update KNOWN_PRICING_USD_PER_1M"
                )
                audit_summary["cost_note"] = tally_entry["cost_note"]

            audit("bounce_usage", **audit_summary)
            audit("bounce_complete", iterations=i + 1, response_length=len(final),
                  run_id=run_id, elapsed_seconds=elapsed_seconds)

            # Append to daily tally (durable, locked)
            append_tally(tally_entry)

            # Read today's running total for the stderr summary
            today_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
            today_entries = _read_tally_for_date(today_str)
            today_sum = _summarize_entries(today_entries)
            today_cost_str = (
                f"${today_sum['cost_known_usd']:.4f}" if today_sum["cost_known_usd"] > 0 else "$0.0000"
            )
            if today_sum["cost_unknown_count"]:
                today_cost_str += f" + {today_sum['cost_unknown_count']} unpriced"

            # Stderr summary
            if pricing:
                print(
                    f"bounce.py: this bounce prompt={total_usage['prompt_tokens']} "
                    f"completion={total_usage['completion_tokens']} "
                    f"total={total_usage['total_tokens']} "
                    f"cost=${tally_entry['total_cost_usd']:.4f} "
                    f"| today ({today_str}): {today_sum['bounces']} bounce(s), "
                    f"{today_sum['total_tokens']:,} tokens, {today_cost_str} "
                    f"| model={MODEL}",
                    file=sys.stderr,
                )
            else:
                print(
                    f"bounce.py: this bounce prompt={total_usage['prompt_tokens']} "
                    f"completion={total_usage['completion_tokens']} "
                    f"total={total_usage['total_tokens']} "
                    f"cost=unknown (no pricing for {MODEL}) "
                    f"| today ({today_str}): {today_sum['bounces']} bounce(s), "
                    f"{today_sum['total_tokens']:,} tokens, {today_cost_str} "
                    f"| model={MODEL}",
                    file=sys.stderr,
                )

            # Budget warning (warning-only, never refuses)
            daily_warn = get_daily_budget_warn()
            if daily_warn is not None and today_sum["cost_known_usd"] > daily_warn:
                print(
                    f"bounce.py: WARNING — today's known cost (${today_sum['cost_known_usd']:.4f}) "
                    f"exceeds daily_warn_usd (${daily_warn:.2f}) from pricing config.",
                    file=sys.stderr,
                )

            print(final)
            if stage1_gate:
                print(f"bounce.py: STAGE-1 GATE — {stage1_gate}. Response shown above; "
                      f"ledger contract not satisfied (exit 12).", file=sys.stderr)
                sys.exit(12)
            return

        for tc in tool_calls:
            tool_call_count += 1
            fn_name = tc["function"]["name"]
            raw_args = tc["function"].get("arguments", "{}")
            try:
                args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                args = {}

            handler = TOOL_DISPATCH.get(fn_name)
            if handler is None:
                result = {"error": f"unknown tool: {fn_name}"}
            else:
                try:
                    result = handler(args)
                except Exception as e:
                    result = {"error": f"tool execution failed: {e}"}

            audit("tool_call", iteration=i, name=fn_name, args=args)

            # Build non-sensitive result metadata: content hash + line counts so
            # citation audits can verify what was returned without logging content itself.
            # Hash the structured payload too so grep/list_files results are reproducible.
            result_meta = {}
            if isinstance(result, dict):
                result_meta["error"] = "error" in result
                result_meta["keys"] = list(result.keys())
                for k in ("path", "name", "start_line", "lines_returned", "total_lines",
                          "truncated", "redactions"):
                    if k in result:
                        result_meta[k] = result[k]
                if isinstance(result.get("content"), str):
                    result_meta["content_sha256"] = hashlib.sha256(
                        result["content"].encode("utf-8")
                    ).hexdigest()
                    result_meta["content_bytes"] = len(result["content"])
                if isinstance(result.get("matches"), list):
                    matches = result["matches"]
                    result_meta["match_count"] = len(matches)
                    # Hash the canonical serialization so audits can verify the exact match set
                    result_meta["matches_sha256"] = hashlib.sha256(
                        json.dumps(matches, sort_keys=True).encode("utf-8")
                    ).hexdigest()
                if isinstance(result.get("files"), list):
                    files = result["files"]
                    result_meta["file_count"] = len(files)
                    result_meta["files_sha256"] = hashlib.sha256(
                        json.dumps(files, sort_keys=True).encode("utf-8")
                    ).hexdigest()
            audit("tool_result", iteration=i, name=fn_name, meta=result_meta)

            # Compact progress to stderr
            arg_brief = ", ".join(f"{k}={v!r}" for k, v in args.items())[:80]
            outcome = "error" if (isinstance(result, dict) and "error" in result) else "ok"
            print(f"bounce.py: [{i+1:02d}] {fn_name}({arg_brief}) -> {outcome}", file=sys.stderr)

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps(result),
            })

    print(f"bounce.py: hit max iterations ({max_iter}) without final response.", file=sys.stderr)
    audit("bounce_max_iterations")
    sys.exit(8)


if __name__ == "__main__":
    main()
