"""Supplementary benchmark-scope verdict; never rewrites original test outcomes."""
import re


def outcomes(text):
    # Parse verbose result lines, never short-summary lines or across newlines.
    return dict(re.findall(r"^([^\s]+\.py::[^\n]*?)[ \t]+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:[ \t]|$)", text, re.M))


def node_matches(name, observed):
    if name in observed:
        return {name}
    if "[" in name and not name.endswith("]"):
        return {node for node in observed if node.split(" ", 1)[0] == name}
    return set()


def defect_status(task, text, required_status):
    observed = outcomes(text)
    explicit_failures = set(re.findall(r"^([^\s]+\.py::[^\n]*?)[ \t]+FAILED(?:[ \t]|$)", text, re.M))
    # A failing test body may additionally fail teardown. An ERROR alone does
    # not establish the requested defect; require the explicit FAILED record.
    return {name: bool(matches := node_matches(name, observed)) and all(
        (n in explicit_failures and observed[n] in ("FAILED", "ERROR")) if required_status == "FAILED"
        else observed[n] == required_status for n in matches)
            for name in task["FAIL_TO_PASS"]}


def assess(task, before_text, after_text, before_rc, after_rc):
    before, after = outcomes(before_text), outcomes(after_text)
    defects = set(task["FAIL_TO_PASS"])
    regressions = set(task["PASS_TO_PASS"])
    known = set(task["FAIL_TO_FAIL"])
    aliases = {}
    def expand(names):
        resolved, missing = set(), set()
        for n in names:
            matches = node_matches(n, before)
            # Some dataset parser node IDs stop at the first space in a parameter.
            # Expand to EVERY exact observed node in that group, never one lucky match.
            if matches and n not in before:
                aliases[n] = sorted(matches)
            resolved.update(matches)
            if not matches:
                missing.add(n)
        return resolved, missing
    defects, missing_defects = expand(defects)
    regressions, missing_regressions = expand(regressions)
    known, _ = expand(known)
    failed_before = {n for n, s in before.items() if s in ("FAILED", "ERROR")}
    failed_after = {n for n, s in after.items() if s in ("FAILED", "ERROR")}
    previous_pass = {n for n, s in before.items() if s == "PASSED"}
    unexpected_before = failed_before - defects - known
    missing_before = missing_defects | missing_regressions
    baseline_scope_valid = bool(defects) and before_rc == 1 and not missing_defects and not unexpected_before and all(
        defect_status(task, before_text, "FAILED").values()) and all(before.get(n) == "PASSED" for n in regressions)
    baseline_valid = baseline_scope_valid and not missing_before
    lost = {n for n in previous_pass | regressions if after.get(n) != "PASSED"}
    unresolved = {n for n in defects if after.get(n) != "PASSED"}
    unexpected_after = failed_after - known
    correct = baseline_valid and after_rc in (0, 1) and not lost and not unresolved and not unexpected_after
    scope_correct = baseline_scope_valid and after_rc in (0, 1) and not lost and not unresolved and not unexpected_after
    return {"policy": "all declared defects pass; all declared and observed previous passes preserved; only dataset-declared pre-existing failures allowed",
        "baseline_valid": baseline_valid, "benchmark_correct": correct,
        "baseline_in_observed_scope_valid": baseline_scope_valid, "observed_scope_correct": scope_correct,
        "declared_regression_coverage_complete": not missing_regressions,
        "missing_baseline_nodes": sorted(missing_before), "unexpected_baseline_failures": sorted(unexpected_before),
        "dataset_truncated_node_expansions": aliases,
        "declared_defects_not_passed": sorted(unresolved), "previous_passes_not_preserved": sorted(lost),
        "unexpected_after_failures": sorted(unexpected_after), "known_failures_remaining": sorted(failed_after & known),
        "module_all_green": after_rc == 0, "does_not_replace_original_validation": True}
