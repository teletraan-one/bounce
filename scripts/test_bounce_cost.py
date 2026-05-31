"""Unit tests for bounce.py compute_bounce_cost (caching + long-context tiering).

Run: pytest .claude/scripts/test_bounce_cost.py
"""
import importlib.util
import pathlib

import pytest

_spec = importlib.util.spec_from_file_location(
    "bounce", pathlib.Path(__file__).parent / "bounce.py"
)
bounce = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bounce)
compute = bounce.compute_bounce_cost

# gpt-5.5 pricing per the verified config.
PRICING = {
    "input": 5.0,
    "output": 30.0,
    "cached_input": 0.5,
    "tier_threshold_tokens": 272_000,
    "tier_input_mult": 2.0,
    "tier_output_mult": 1.5,
}


def test_no_cache_no_tier():
    # 100K prompt, 0 cached, 10K completion — flat rates.
    in_c, out_c, meta = compute(100_000, 0, 10_000, PRICING)
    assert in_c == pytest.approx(100_000 * 5.0 / 1e6)   # 0.50
    assert out_c == pytest.approx(10_000 * 30.0 / 1e6)  # 0.30
    assert meta["tiered_over_threshold"] is False
    assert meta["cached_tokens"] == 0


def test_caching_discounts_input():
    # 100K prompt of which 80K cached → 80K at $0.50, 20K at $5.00.
    in_c, _, meta = compute(100_000, 80_000, 0, PRICING)
    expected = (80_000 * 0.5 + 20_000 * 5.0) / 1e6
    assert in_c == pytest.approx(expected)
    assert meta["cached_tokens"] == 80_000
    # Cheaper than billing all 100K at full input rate.
    assert in_c < 100_000 * 5.0 / 1e6


def test_cached_clamped_to_prompt():
    # cached can't exceed prompt; no negative uncached.
    in_c, _, meta = compute(1_000, 5_000, 0, PRICING)
    assert meta["cached_tokens"] == 1_000
    assert in_c == pytest.approx(1_000 * 0.5 / 1e6)


def test_tier_marginal_input_and_whole_output():
    # 300K prompt (28K over 272K), 0 cached, 10K completion.
    in_c, out_c, meta = compute(300_000, 0, 10_000, PRICING)
    over = 300_000 - 272_000
    under = 272_000
    expected_in = (under * 5.0 + over * 5.0 * 2.0) / 1e6
    assert in_c == pytest.approx(expected_in)
    # Output billed at 1.5x because prompt crossed the threshold.
    assert out_c == pytest.approx(10_000 * 30.0 * 1.5 / 1e6)
    assert meta["tiered_over_threshold"] is True


def test_tier_and_cache_together():
    # 300K prompt, 250K cached, 10K completion.
    # cached fill the low end; tier surcharge applies only to uncached-over-threshold.
    in_c, out_c, meta = compute(300_000, 250_000, 10_000, PRICING)
    cached = 250_000
    uncached = 50_000
    over = min(300_000 - 272_000, uncached)   # 28_000
    normal = uncached - over                  # 22_000
    expected_in = (cached * 0.5 + normal * 5.0 + over * 5.0 * 2.0) / 1e6
    assert in_c == pytest.approx(expected_in)
    assert out_c == pytest.approx(10_000 * 30.0 * 1.5 / 1e6)


def test_no_cached_input_rate_falls_back_to_full():
    # If config lacks cached_input, cached tokens bill at full input (no invented discount).
    pricing = {"input": 5.0, "output": 30.0}
    in_c, _, _ = compute(100_000, 80_000, 0, pricing)
    assert in_c == pytest.approx(100_000 * 5.0 / 1e6)


def test_exact_threshold_not_tiered():
    # prompt == threshold → NOT over (code uses >), no overage, no output multiplier.
    in_c, out_c, meta = compute(272_000, 0, 10_000, PRICING)
    assert meta["tiered_over_threshold"] is False
    assert in_c == pytest.approx(272_000 * 5.0 / 1e6)
    assert out_c == pytest.approx(10_000 * 30.0 / 1e6)  # no 1.5x


def test_zero_completion_over_threshold():
    in_c, out_c, meta = compute(300_000, 0, 0, PRICING)
    assert meta["tiered_over_threshold"] is True
    assert out_c == 0.0


def test_cached_exceeds_threshold_no_negative_bucket():
    # 300K prompt, 290K cached (cached > threshold). Only 10K uncached, none over
    # threshold beyond what uncached allows; no negative normal bucket.
    in_c, _, meta = compute(300_000, 290_000, 0, PRICING)
    cached = 290_000
    uncached = 10_000
    over = min(300_000 - 272_000, uncached)   # min(28_000, 10_000) = 10_000
    normal = uncached - over                  # 0
    expected = (cached * 0.5 + normal * 5.0 + over * 5.0 * 2.0) / 1e6
    assert in_c == pytest.approx(expected)
    assert normal == 0


def test_no_tier_config_means_no_tiering():
    # Model whose pricing lacks tier keys must NOT be tiered even over 272K.
    pricing = {"input": 5.0, "output": 30.0, "cached_input": 0.5}
    in_c, out_c, meta = compute(400_000, 0, 10_000, pricing)
    assert meta["tiered_over_threshold"] is False
    assert in_c == pytest.approx(400_000 * 5.0 / 1e6)   # flat, no 2x
    assert out_c == pytest.approx(10_000 * 30.0 / 1e6)  # flat, no 1.5x


def test_sum_per_call_not_aggregate():
    # THE regression test for the aggregate-vs-per-call bug: two 150K calls
    # (each UNDER 272K) must NOT trigger the tier, even though aggregate=300K.
    records = [
        {"prompt_tokens": 150_000, "cached_prompt_tokens": 0, "completion_tokens": 5_000},
        {"prompt_tokens": 150_000, "cached_prompt_tokens": 0, "completion_tokens": 5_000},
    ]
    in_c, out_c, meta = bounce.sum_bounce_costs(records, PRICING)
    assert meta["any_call_tiered"] is False
    assert meta["api_calls"] == 2
    assert in_c == pytest.approx(300_000 * 5.0 / 1e6)    # flat — no tier
    assert out_c == pytest.approx(10_000 * 30.0 / 1e6)
    # Contrast: the OLD aggregate approach would have tiered (300K > 272K). Guard against regressing.
    agg_in, _, agg_meta = compute(300_000, 0, 10_000, PRICING)
    assert agg_meta["tiered_over_threshold"] is True   # proves aggregate would mis-tier
    assert in_c < agg_in                                # per-call is correctly cheaper


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
