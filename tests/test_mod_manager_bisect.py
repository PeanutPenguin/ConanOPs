from __future__ import annotations

import mod_manager


def _mods(ids):
    return [{"id": i, "name": i, "enabled": True} for i in ids]


# ------------------------------------------------------------------ bisect --

def test_bisect_finds_the_culprit_in_the_middle():
    ids = ["a", "b", "c", "d", "e"]
    state = mod_manager.start_bisect(ids)
    culprit = "c"

    # Simulate: the server is "broken" whenever `culprit` is enabled.
    while not state.done:
        disabled = set(state.current_test_disabled)
        still_broken = culprit not in disabled  # culprit still enabled -> still broken
        state = mod_manager.report_result(state, still_broken)

    assert state.culprit == culprit


def test_bisect_rules_out_all_candidates_when_nothing_is_actually_the_cause():
    ids = ["a", "b", "c", "d"]
    state = mod_manager.start_bisect(ids)

    while not state.done:
        state = mod_manager.report_result(state, problem_still_happens=True)  # always still broken, no matter what's off

    assert state.culprit is None
    assert state.remaining == []


def test_bisect_single_candidate_resolves_in_one_round():
    state = mod_manager.start_bisect(["only-one"])
    state = mod_manager.report_result(state, problem_still_happens=False)  # fixed once disabled
    assert state.done is True
    assert state.culprit == "only-one"


def test_bisect_ignores_calls_after_done():
    state = mod_manager.start_bisect(["a", "b"])
    state = mod_manager.report_result(state, problem_still_happens=False)
    state = mod_manager.report_result(state, problem_still_happens=False)
    while not state.done:
        state = mod_manager.report_result(state, problem_still_happens=False)
    culprit_before = state.culprit
    state2 = mod_manager.report_result(state, problem_still_happens=True)  # must be a no-op
    assert state2.culprit == culprit_before


# ------------------------------------------------------------- apply_bisect_test --

def test_apply_bisect_test_disables_only_current_round():
    mods = _mods(["a", "b", "c", "d"])
    state = mod_manager.start_bisect(["a", "b", "c", "d"])

    result = mod_manager.apply_bisect_test(mods, state)

    disabled = {m["id"] for m in result if not m["enabled"]}
    assert disabled == set(state.current_test_disabled)


def test_apply_bisect_test_re_enables_a_cleared_mod():
    """The bug this was written to fix: a mod disabled for one round's
    test, then cleared (known_good) because the problem persisted
    WITHOUT it, must come back ON for the next round -- not stay
    disabled just because nothing explicitly re-enabled it."""
    mods = _mods(["a", "b", "c", "d"])
    state = mod_manager.start_bisect(["a", "b", "c", "d"])
    first_disabled = set(state.current_test_disabled)  # e.g. {"a", "b"}

    state = mod_manager.report_result(state, problem_still_happens=True)  # clears the disabled half as known_good
    assert set(state.known_good) == first_disabled  # sanity: that half really did get cleared

    result = mod_manager.apply_bisect_test(mods, state)
    result_by_id = {m["id"]: m["enabled"] for m in result}

    for mod_id in first_disabled:
        assert result_by_id[mod_id] is True, f"{mod_id} was cleared (known_good) but is still disabled"


def test_apply_bisect_test_leaves_non_candidate_mods_untouched():
    mods = _mods(["a", "b"]) + [{"id": "not-a-candidate", "name": "x", "enabled": False}]
    state = mod_manager.start_bisect(["a", "b"])

    result = mod_manager.apply_bisect_test(mods, state)

    other = next(m for m in result if m["id"] == "not-a-candidate")
    assert other["enabled"] is False  # left exactly as it was, never touched by the bisect


def test_apply_bisect_test_does_not_mutate_the_input_list(monkeypatch):
    mods = _mods(["a", "b"])
    original_snapshot = [dict(m) for m in mods]
    state = mod_manager.start_bisect(["a", "b"])

    mod_manager.apply_bisect_test(mods, state)

    assert mods == original_snapshot


def test_full_bisect_run_via_apply_bisect_test_leaves_only_culprit_disabled():
    """End-to-end: run every round through apply_bisect_test (as a real
    caller would) and confirm the FINAL state has exactly the culprit
    disabled -- not any of the exonerated mods."""
    ids = ["a", "b", "c", "d", "e", "f", "g"]
    mods = _mods(ids)
    state = mod_manager.start_bisect(ids)
    culprit = "e"

    while not state.done:
        mods = mod_manager.apply_bisect_test(mods, state)
        disabled_now = {m["id"] for m in mods if not m["enabled"]}
        still_broken = culprit not in disabled_now
        state = mod_manager.report_result(state, still_broken)

    assert state.culprit == culprit
    # One more apply reflecting the final confirmed round:
    mods = mod_manager.apply_bisect_test(mods, state)
    disabled_final = {m["id"] for m in mods if not m["enabled"]}
    assert disabled_final == {culprit}


# --------------------------------------------------------------- ddmin --

def _run_ddmin(candidates, is_failing):
    """Drives the ddmin generator with a scripted oracle -- mirrors
    exactly how auto_bisect_runner.py drives it against real restarts,
    but with a pure, instant function instead."""
    gen = mod_manager.ddmin(candidates)
    try:
        to_test = next(gen)
        while True:
            to_test = gen.send(is_failing(frozenset(to_test)))
    except StopIteration as stop:
        return stop.value


def test_ddmin_isolates_a_single_culprit():
    result = _run_ddmin(["a", "b", "c", "d", "e", "f"], lambda s: "c" in s)
    assert result == ["c"]


def test_ddmin_finds_one_minimal_set_with_two_independent_culprits():
    """The exact scenario that defeats plain repeated bisection (see
    the test_find_all_* tests in test_auto_bisect_runner.py and this
    module's own docstring): ddmin still correctly isolates ONE
    genuine, minimal failing set -- here, necessarily just one of the
    two independent culprits, since either alone already reproduces
    the failure and ddmin stops once ANY minimal set is found. The
    caller is responsible for looping (remove it, test what's left,
    ddmin again) to find the other one -- see auto_bisect_runner.py."""
    result = _run_ddmin(["a", "b", "c", "d", "e", "f"], lambda s: ("b" in s) or ("e" in s))
    assert len(result) == 1
    assert result[0] in {"b", "e"}


def test_ddmin_finds_a_two_mod_combo_that_only_fails_together():
    """Neither mod alone reproduces the failure -- only both together.
    This is the case plain bisection cannot detect AT ALL."""
    result = _run_ddmin(["a", "b", "c", "d", "e", "f"], lambda s: ("b" in s) and ("e" in s))
    assert set(result) == {"b", "e"}


def test_ddmin_finds_a_three_mod_combo():
    result = _run_ddmin(
        ["a", "b", "c", "d", "e", "f", "g", "h"],
        lambda s: {"c", "f", "h"}.issubset(s),
    )
    assert set(result) == {"c", "f", "h"}


def test_ddmin_single_candidate_is_trivially_minimal():
    result = _run_ddmin(["only"], lambda s: True)
    assert result == ["only"]


def test_ddmin_all_candidates_required_together():
    """The pathological case: nothing smaller than the WHOLE set
    reproduces the failure -- ddmin must correctly conclude "the
    entire set is the minimal failing set" rather than looping
    forever or crashing."""
    full = ["a", "b", "c", "d"]
    result = _run_ddmin(full, lambda s: set(s) == set(full))
    assert set(result) == set(full)


def test_ddmin_never_tests_more_than_the_original_candidates():
    seen_supersets = []

    def oracle(s):
        seen_supersets.append(s)
        return "d" in s and "g" in s
    _run_ddmin(["a", "b", "c", "d", "e", "f", "g"], oracle)
    all_candidates = {"a", "b", "c", "d", "e", "f", "g"}
    assert all(s <= all_candidates for s in seen_supersets)
