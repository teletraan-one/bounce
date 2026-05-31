"""Unit tests for the bounce error-admission → resolution ledger
(BOUNCE_Error_Resolution_Spec.md v0.2).

Run: pytest .claude/scripts/test_bounce_ledger.py
"""
import importlib.util
import pathlib

import pytest

_spec = importlib.util.spec_from_file_location(
    "bounce", pathlib.Path(__file__).parent / "bounce.py"
)
bounce = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bounce)


# ---- extract_labeled_json_block ----

def test_extract_present_block():
    text = 'intro\n## Challenges\n```json\n[{"challenge_id":"c-1"}]\n```\noutro'
    out = bounce.extract_labeled_json_block(text, "Challenges")
    assert out == [{"challenge_id": "c-1"}]


def test_extract_empty_array():
    text = "## Challenges\n```json\n[]\n```"
    assert bounce.extract_labeled_json_block(text, "Challenges") == []


def test_extract_absent_block_returns_none():
    assert bounce.extract_labeled_json_block("no header here", "Challenges") is None


def test_extract_invalid_json_sentinel():
    text = "## Challenges\n```json\n[not valid]\n```"
    assert bounce.extract_labeled_json_block(text, "Challenges") == "INVALID_JSON"


def test_extract_non_list_sentinel():
    text = '## Resolutions\n```json\n{"a":1}\n```'
    assert bounce.extract_labeled_json_block(text, "Resolutions") == "INVALID_SHAPE"


def test_extract_unfenced_after_header_returns_none():
    assert bounce.extract_labeled_json_block("## Challenges\njust prose", "Challenges") is None


def test_extract_tolerates_bold_heading():
    # Models bold the heading (`**## Challenges**`) — this happened on the first live run.
    text = '**## Challenges**\n```json\n[{"challenge_id":"c-1"}]\n```'
    assert bounce.extract_labeled_json_block(text, "Challenges") == [{"challenge_id": "c-1"}]


def test_extract_prose_mention_does_not_match():
    # A bare prose mention (no heading/emphasis markers) must NOT match.
    assert bounce.extract_labeled_json_block("see the Challenges below\n```json\n[]\n```", "Challenges") is None


# ---- validate_challenge / validate_resolution ----

def test_valid_challenge_passes():
    c = {"challenge_id": "c-1", "claim": "x", "error_class": "overclaim",
         "challenge_reason": "too strong"}
    assert bounce.validate_challenge(c) == []


def test_challenge_bad_error_class():
    c = {"challenge_id": "c-1", "claim": "x", "error_class": "nonsense",
         "challenge_reason": "y"}
    errs = bounce.validate_challenge(c)
    assert any("error_class" in e for e in errs)


def test_challenge_missing_fields():
    errs = bounce.validate_challenge({})
    assert any("challenge_id" in e for e in errs)
    assert any("claim" in e for e in errs)


def test_resolution_corrected_requires_evidence():
    r = {"challenge_id": "c-1", "resolution": "corrected", "reason": "fixed"}
    errs = bounce.validate_resolution(r)
    assert any("resolution_evidence" in e for e in errs)
    assert any("support_quote" in e for e in errs)


def test_resolution_removed_needs_no_evidence():
    r = {"challenge_id": "c-1", "resolution": "removed", "reason": "deleted it"}
    assert bounce.validate_resolution(r) == []


def test_resolution_defended_requires_evidence_and_acceptance():
    ok = {"challenge_id": "c-1", "resolution": "defended", "reason": "held",
          "resolution_evidence": ["file.py:10"], "support_quote": "the line says X",
          "accepted_by": "chatgpt"}
    assert bounce.validate_resolution(ok) == []
    no_evidence = {"challenge_id": "c-1", "resolution": "defended", "reason": "held",
                   "accepted_by": "chatgpt"}
    assert bounce.validate_resolution(no_evidence)  # non-empty


def test_defended_without_acceptance_is_invalid():
    # c-003 fix: a defended hold with no accepted_by (or not_checked) must fail —
    # an unaccepted defense should be logged 'contested', not 'defended'.
    r = {"challenge_id": "c-1", "resolution": "defended", "reason": "held",
         "resolution_evidence": ["f:1"], "support_quote": "x", "accepted_by": "not_checked"}
    errs = bounce.validate_resolution(r)
    assert any("accepted_by" in e for e in errs)


def test_bad_accepted_by_enum():
    r = {"challenge_id": "c-1", "resolution": "removed", "reason": "z", "accepted_by": "bogus"}
    assert any("accepted_by" in e for e in bounce.validate_resolution(r))


# ---- reconcile_challenges (the gate) ----

def test_reconcile_all_resolved_clean():
    chs = [{"challenge_id": "c-1", "claim": "x", "error_class": "overclaim", "challenge_reason": "y"}]
    res = [{"challenge_id": "c-1", "resolution": "removed", "reason": "deleted"}]
    out = bounce.reconcile_challenges(chs, res)
    assert out["resolved"] == ["c-1"]
    assert out["open"] == []
    assert out["errors"] == []


def test_reconcile_open_challenge_is_gate_error():
    chs = [{"challenge_id": "c-1", "claim": "x", "error_class": "overclaim", "challenge_reason": "y"}]
    out = bounce.reconcile_challenges(chs, [])  # no resolution
    assert "c-1" in out["open"]
    assert any("unresolved" in e for e in out["errors"])


def test_reconcile_contested_is_open_warning_not_error():
    chs = [{"challenge_id": "c-1", "claim": "x", "error_class": "overclaim", "challenge_reason": "y"}]
    res = [{"challenge_id": "c-1", "resolution": "contested", "reason": "disputed"}]
    out = bounce.reconcile_challenges(chs, res)
    assert "c-1" in out["open"]
    assert out["errors"] == []           # contested is not a hard-gate error
    assert any("contested" in w for w in out["warnings"])


def test_reconcile_unknown_resolution_id_errors():
    chs = [{"challenge_id": "c-1", "claim": "x", "error_class": "overclaim", "challenge_reason": "y"}]
    res = [{"challenge_id": "c-99", "resolution": "removed", "reason": "z"}]
    out = bounce.reconcile_challenges(chs, res)
    assert any("unknown challenge_id" in e for e in out["errors"])
    assert "c-1" in out["open"]  # c-1 still unresolved


def test_reconcile_empty_is_clean():
    out = bounce.reconcile_challenges([], [])
    assert out["errors"] == [] and out["open"] == [] and out["resolved"] == []


# ---- scan_admission_markers ----

def test_scan_strong_marker():
    out = bounce.scan_admission_markers("After review, I cannot source that figure.")
    assert out["strong"] and not out["soft"]


def test_scan_soft_marker():
    out = bounce.scan_admission_markers("You're right, that's worth reconsidering.")
    assert out["soft"]


def test_scan_clean():
    out = bounce.scan_admission_markers("Everything checks out.")
    assert out["strong"] == [] and out["soft"] == []


# ---- summarize_corrections (the statistics engine) ----

def test_summarize_joins_and_counts():
    entries = [
        {"challenges": [
            {"challenge_id": "c-1", "error_class": "fabrication"},
            {"challenge_id": "c-2", "error_class": "overclaim"},
            {"challenge_id": "c-3", "error_class": "unsupported"},
         ],
         "resolutions": [
            {"challenge_id": "c-1", "resolution": "removed"},
            {"challenge_id": "c-2", "resolution": "defended"},
            # c-3 left unresolved
         ]},
        {"challenges": []},          # clean bounce, ignored
        {"prompt_tokens": 100},      # pre-ledger entry, no challenges
    ]
    s = bounce.summarize_corrections(entries)
    assert s["bounces_with_challenges"] == 1
    assert s["challenges"] == 3
    assert s["admitted_errors"] == 1          # c-1 removed
    assert s["defended"] == 1                 # c-2
    assert s["open_or_unrecorded"] == 1       # c-3
    assert s["by_error_class"]["fabrication"] == 1
    assert s["hold_rate"] == pytest.approx(1 / 3)


def test_summarize_no_challenges_zero():
    s = bounce.summarize_corrections([{"prompt_tokens": 5}])
    assert s["challenges"] == 0 and s["hold_rate"] == 0.0


# ---- duplicate-id detection ----

def test_reconcile_duplicate_challenge_id():
    chs = [
        {"challenge_id": "c-1", "claim": "x", "error_class": "overclaim", "challenge_reason": "a"},
        {"challenge_id": "c-1", "claim": "y", "error_class": "overclaim", "challenge_reason": "b"},
    ]
    assert any("duplicate challenge_id" in e for e in bounce.reconcile_challenges(chs, [])["errors"])


def test_reconcile_duplicate_resolution():
    chs = [{"challenge_id": "c-1", "claim": "x", "error_class": "overclaim", "challenge_reason": "a"}]
    res = [{"challenge_id": "c-1", "resolution": "removed", "reason": "1"},
           {"challenge_id": "c-1", "resolution": "removed", "reason": "2"}]
    assert any("duplicate resolution" in e for e in bounce.reconcile_challenges(chs, res)["errors"])


# ---- section-bounded extraction (no cross-section fence capture) ----

def test_extract_is_section_bounded():
    text = ("## Challenges\n(none this round)\n\n"
            "## Appendix\n```json\n[{\"challenge_id\":\"x\"}]\n```")
    assert bounce.extract_labeled_json_block(text, "Challenges") is None


# ---- record_resolutions: file I/O + gates (integration) ----

import io
import json as _json

RUN_ID = "2026-05-30T12-00-00Z-abc123"


def _setup_tally(monkeypatch, tmp_path, entry):
    monkeypatch.setattr(bounce, "TALLY_DIR", tmp_path)
    monkeypatch.setattr(bounce, "TALLY_LOCK", str(tmp_path / ".lock"))
    monkeypatch.setattr(bounce, "_tally_path_for", lambda d: tmp_path / f"{d}.jsonl")
    monkeypatch.setattr(bounce, "_ensure_tally_dir", lambda: None)
    (tmp_path / f"{entry['run_id'][:10]}.jsonl").write_text(_json.dumps(entry) + "\n")


def _challenge(cid):
    return {"challenge_id": cid, "claim": "x", "challenged_by": "chatgpt", "author": "claude",
            "error_class": "overclaim", "challenge_reason": "y", "status": "open"}


def _record(monkeypatch, resolutions, run_id=RUN_ID):
    monkeypatch.setattr("sys.stdin", io.StringIO(resolutions))
    try:
        bounce.record_resolutions(run_id)
        return 0
    except SystemExit as e:
        return e.code if e.code is not None else 0


def _read_back(tmp_path, run_id=RUN_ID):
    lines = (tmp_path / f"{run_id[:10]}.jsonl").read_text().splitlines()
    return [_json.loads(l) for l in lines if l.strip()][0]


def test_record_clean_writeback(monkeypatch, tmp_path):
    _setup_tally(monkeypatch, tmp_path, {"run_id": RUN_ID, "challenges": [_challenge("c-1")]})
    code = _record(monkeypatch, '## Resolutions\n```json\n[{"challenge_id":"c-1","resolution":"removed","reason":"deleted"}]\n```')
    assert code == 0
    e = _read_back(tmp_path)
    assert e["ledger_status"] == "clean" and len(e["resolutions"]) == 1


def test_record_open_hard_gate(monkeypatch, tmp_path):
    _setup_tally(monkeypatch, tmp_path, {"run_id": RUN_ID, "challenges": [_challenge("c-1"), _challenge("c-2")]})
    code = _record(monkeypatch, '## Resolutions\n```json\n[{"challenge_id":"c-1","resolution":"removed","reason":"x"}]\n```')
    assert code == 11                                   # c-2 unresolved
    assert _read_back(tmp_path)["ledger_status"] == "gated"


def test_record_missing_evidence_gate(monkeypatch, tmp_path):
    _setup_tally(monkeypatch, tmp_path, {"run_id": RUN_ID, "challenges": [_challenge("c-1")]})
    code = _record(monkeypatch, '## Resolutions\n```json\n[{"challenge_id":"c-1","resolution":"corrected","reason":"x"}]\n```')
    assert code == 11                                   # corrected w/o evidence


def test_record_contested_is_not_clean(monkeypatch, tmp_path):
    _setup_tally(monkeypatch, tmp_path, {"run_id": RUN_ID, "challenges": [_challenge("c-1")]})
    code = _record(monkeypatch, '## Resolutions\n```json\n[{"challenge_id":"c-1","resolution":"contested","reason":"disputed"}]\n```')
    assert code == 0
    assert _read_back(tmp_path)["ledger_status"] == "contested"


def test_record_bad_runid_format(monkeypatch, tmp_path):
    _setup_tally(monkeypatch, tmp_path, {"run_id": RUN_ID, "challenges": [_challenge("c-1")]})
    assert _record(monkeypatch, "[]", run_id="not-a-date") == 9


def test_record_unknown_runid(monkeypatch, tmp_path):
    _setup_tally(monkeypatch, tmp_path, {"run_id": RUN_ID, "challenges": [_challenge("c-1")]})
    assert _record(monkeypatch, '## Resolutions\n```json\n[]\n```', run_id="2026-05-30T09-00-00Z-zzz999") == 10


def test_record_malformed_resolutions_exit9(monkeypatch, tmp_path):
    _setup_tally(monkeypatch, tmp_path, {"run_id": RUN_ID, "challenges": [_challenge("c-1")]})
    assert _record(monkeypatch, "not json at all") == 9


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
