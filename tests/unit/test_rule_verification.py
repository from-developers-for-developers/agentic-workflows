# SPDX-License-Identifier: GPL-3.0-or-later
"""Resolving rules against the store, verifier results, and operator decisions."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from ww.actions import CommandDefinition, Commands
from ww.config import load_configuration
from ww.config.rules import rule_text_hash
from ww.errors import StateError
from ww.execution_models import PlanItemExecution
from ww.execution_models.records import (
    HeldCompletion,
    RuleVerdict,
    VerificationRule,
)
from ww.plan import PlanItem, WorkflowPlanCompiler
from ww.rule_store import CheckEntry, CheckSpec, RuleAutomation, RuleEntry
from ww.rule_verification import (
    Decisions,
    apply_decisions,
    automatic_decisions,
    blocking_proposals,
    effective_hints,
    parse_check_results,
    parse_rule_results,
    record_results,
    resolve_rules,
    revoke_check,
    verification_item,
    verification_needs,
)
from ww.workflow_config import RuleHints

WORKFLOWS = """workflows:
  - name: task
    steps:
      - name: develop
        description: Develop it.
        rules:
          - Keep the public CLI unchanged.
          - Name things clearly.
          - text: Log every failure.
            model: opus
          - text: Include foo.
            shell: grep -L foo $WW_STEP_CHANGED_FILES || true
            assert: { operator: empty }
"""
CLI, NAMES, LOGS = (
    "Keep the public CLI unchanged.",
    "Name things clearly.",
    "Log every failure.",
)
NOW = "2026-09-29T12:00:00Z"


@pytest.fixture
def develop(tmp_path: Path) -> PlanItem:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(WORKFLOWS, encoding="utf-8")
    configuration = load_configuration(tmp_path / "ww-agentic-workflows.yaml")
    plan = WorkflowPlanCompiler(configuration, tmp_path, "codex", "TASK-1").compile(
        "task"
    )
    return next(item for item in plan.items if item.name == "develop")


def _check(covers: tuple[str, ...], status: str = "converted") -> CheckEntry:
    return CheckEntry(
        CheckSpec(Commands((CommandDefinition(argv=("lint-tool",)),)), covers=covers),
        status,  # type: ignore[arg-type]
    )


def _record(**fields: object) -> PlanItemExecution:
    return replace(PlanItemExecution("develop", 2), **fields)  # type: ignore[arg-type]


def _rule(state: str, text: str = CLI, rule_id: str = "develop/1") -> VerificationRule:
    return VerificationRule(
        rule_id,
        text,
        rule_text_hash(text),
        state,  # type: ignore[arg-type]
        check="cli-surface" if state == "approach-approved" else None,
        approach="diff the help" if state == "approach-approved" else None,
    )


def test_resolution_covers_every_store_status(develop: PlanItem) -> None:
    automation = (
        RuleAutomation()
        .with_rule(rule_text_hash(CLI), RuleEntry(CLI, "converted", check="lint"))
        .with_rule(rule_text_hash(NAMES), RuleEntry(NAMES, "approach-proposed"))
        .with_rule(
            rule_text_hash(LOGS), RuleEntry(LOGS, "rejected", interpretation="Log.")
        )
        .with_check("lint", _check((rule_text_hash(CLI),)))
    )

    resolutions, checks = resolve_rules(develop, automation)

    assert [(entry.id, entry.status, entry.check) for entry in resolutions] == [
        ("develop/1", "converted", "lint"),
        ("develop/2", "pending_operator", None),
        ("develop/3", "judged", None),
    ]
    assert resolutions[2].interpretation == "Log."
    assert [(check.id, check.source, check.covers) for check in checks] == [
        ("lint", "derived", ("develop/1",))
    ]


@pytest.mark.parametrize(
    ("status", "check_status", "resolved"),
    [
        (None, None, "unresolved"),
        ("interpreted", None, "unresolved"),
        ("approach-approved", None, "unresolved"),
        ("proposed", None, "pending_operator"),
        ("ambiguous", None, "pending_operator"),
        ("not-convertible", None, "judged"),
        # Converted, but its check is not: nothing mechanical runs.
        ("converted", "proposed", "judged"),
    ],
)
def test_a_rule_resolves_by_its_store_status(
    develop: PlanItem, status: str | None, check_status: str | None, resolved: str
) -> None:
    automation = RuleAutomation()
    if status is not None:
        automation = automation.with_rule(
            rule_text_hash(CLI),
            RuleEntry(CLI, status, check="lint"),  # type: ignore[arg-type]
        )
    if check_status is not None:
        automation = automation.with_check(
            "lint", _check((rule_text_hash(CLI),), check_status)
        )

    resolutions, checks = resolve_rules(develop, automation)

    assert resolutions[0].status == resolved
    assert checks == ()


def test_one_planned_check_per_shared_check_name(develop: PlanItem) -> None:
    hashes = (rule_text_hash(CLI), rule_text_hash(NAMES))
    automation = (
        RuleAutomation()
        .with_rule(hashes[0], RuleEntry(CLI, "converted", check="lint"))
        .with_rule(hashes[1], RuleEntry(NAMES, "converted", check="lint"))
        .with_check("lint", _check(hashes))
    )

    _, checks = resolve_rules(develop, automation)

    assert len(checks) == 1
    assert checks[0].covers == ("develop/1", "develop/2")


def test_needs_skip_checked_verified_and_waiting_rules(develop: PlanItem) -> None:
    automation = RuleAutomation().with_rule(
        rule_text_hash(NAMES), RuleEntry(NAMES, "approach-approved", check="names")
    )
    _, lint = resolve_rules(
        develop,
        RuleAutomation()
        .with_rule(rule_text_hash(CLI), RuleEntry(CLI, "converted", check="lint"))
        .with_check("lint", _check((rule_text_hash(CLI),))),
    )
    record = _record(resolved_checks=lint)

    needs = verification_needs(develop, record, automation)

    assert [(need.id, need.state) for need in needs] == [
        ("develop/2", "approach-approved"),
        ("develop/3", "unresolved"),
    ]
    verified = _record(
        resolved_checks=lint,
        held_completion=HeldCompletion(
            verdicts=(RuleVerdict("develop/3", "pass", "v"),)
        ),
        open_proposals=("names",),
    )
    assert verification_needs(develop, verified, automation) == ()


def test_a_verification_item_per_hint_set(develop: PlanItem) -> None:
    rules = {rule.id: rule for rule in develop.rules}

    hints = {rule_id: effective_hints(rules[rule_id], develop) for rule_id in rules}
    item = verification_item(develop, 2, hints["develop/3"])

    assert hints["develop/1"] == hints["develop/2"] != hints["develop/3"]
    assert hints["develop/3"].model == "opus"
    assert item.id == "task:develop:verify:2"
    assert item.phase == "before_complete"
    assert item.requested_model == "opus"
    assert item.verifies is not None and item.verifies.item_id == develop.id
    assert not item.hands_over and not item.rules and not item.checks


def _results(rules: tuple[VerificationRule, ...], *payloads: dict) -> tuple:
    return parse_rule_results(tuple(json.dumps(payload) for payload in payloads), rules)


@pytest.mark.parametrize(
    ("payloads", "message"),
    [
        ((), "missing for develop/1"),
        (({"id": "develop/9", "status": "judged"},), "does not cover"),
        (
            (
                {"id": "develop/1", "status": "judged", "verdict": "pass"},
                {"id": "develop/1", "status": "judged", "verdict": "pass"},
            ),
            "given twice",
        ),
        (({"id": "develop/1", "status": "approach"},), "status must be one of judged"),
        (({"id": "develop/1", "status": "judged"},), "requires verdict"),
        (
            ({"id": "develop/1", "status": "judged", "verdict": "fail"},),
            "needs failures",
        ),
        (
            (
                {
                    "id": "develop/1",
                    "status": "judged",
                    "verdict": "pass",
                    "failures": [{"file": "a", "what": "b"}],
                },
            ),
            "lists no failures",
        ),
        (
            ({"id": "develop/1", "status": "judged", "verdict": "pass", "x": 1},),
            "unknown keys",
        ),
    ],
)
def test_rule_results_are_validated(payloads: tuple, message: str) -> None:
    with pytest.raises(StateError, match=message):
        _results((_rule("judged"),), *payloads)


def test_rule_results_that_are_not_json_are_refused() -> None:
    with pytest.raises(StateError, match="not valid JSON"):
        parse_rule_results(("{nope",), (_rule("judged"),))


def test_a_stage_a_result_needs_an_approach_and_a_check_name() -> None:
    rules = (_rule("unresolved"),)

    with pytest.raises(StateError, match="requires approach"):
        _results(rules, {"id": "develop/1", "status": "approach", "check": "x"})
    with pytest.raises(StateError, match="kebab-case"):
        _results(
            rules,
            {"id": "develop/1", "status": "approach", "check": "X Y", "approach": "a"},
        )
    with pytest.raises(StateError, match="two or more candidates"):
        _results(rules, {"id": "develop/1", "status": "ambiguous", "candidates": ["a"]})
    result = _results(
        rules,
        {
            "id": "develop/1",
            "status": "not-convertible",
            "reason": "taste",
            "verdict": "fail",
            "failures": [{"file": "cli.py", "line": 3, "what": "flag renamed"}],
        },
    )[0]
    assert result.verdict == "fail"
    assert result.failures[0].text() == "cli.py:3 — flag renamed"


def test_check_results_must_match_the_prepared_rules(develop: PlanItem) -> None:
    rules = (_rule("approach-approved"),)
    results = _results(
        rules, {"id": "develop/1", "status": "approach", "check": "cli-surface"}
    )
    check = {
        "name": "cli-surface",
        "argv": ["help-diff"],
        "config": ["tools/help.txt"],
        "covers": ["develop/1"],
        "proven": True,
    }

    with pytest.raises(StateError, match="missing for cli-surface"):
        parse_check_results((), rules, results, develop)
    with pytest.raises(StateError, match="named by no rule"):
        parse_check_results(
            (json.dumps({**check, "name": "other"}),), rules, results, develop
        )
    with pytest.raises(StateError, match="must cover the rules that name it"):
        parse_check_results(
            (json.dumps({**check, "covers": ["develop/2"]}),), rules, results, develop
        )
    with pytest.raises(StateError, match="not rules without a command"):
        parse_check_results(
            (json.dumps({**check, "covers": ["develop/1", "develop/4"]}),),
            rules,
            results,
            develop,
        )
    with pytest.raises(StateError, match="proven"):
        parse_check_results(
            (json.dumps({**check, "proven": "yes"}),), rules, results, develop
        )
    (parsed,) = parse_check_results((json.dumps(check),), rules, results, develop)
    assert parsed.command.commands[0].argv == ("help-diff",)
    assert parsed.config == ("tools/help.txt",)


def test_stage_a_results_become_proposals(develop: PlanItem) -> None:
    rules = (_rule("unresolved"), _rule("unresolved", NAMES, "develop/2"))
    results = _results(
        rules,
        {
            "id": "develop/1",
            "interpretation": "No flags change.",
            "status": "approach",
            "check": "lint",
            "approach": "extend lint",
        },
        {"id": "develop/2", "status": "ambiguous", "candidates": ["short", "long"]},
    )
    automation = RuleAutomation().with_check("lint", _check(()))

    updated, opened, notices = record_results(
        automation, rules, results, (), develop, "v1", NOW
    )

    cli = updated.rules[rule_text_hash(CLI)]
    assert (cli.status, cli.check, cli.extends) == ("approach-proposed", "lint", True)
    assert cli.interpretation == "No flags change."
    assert updated.rules[rule_text_hash(NAMES)].candidates == ("short", "long")
    assert opened == (rule_text_hash(CLI), rule_text_hash(NAMES))
    assert notices == ()


def test_an_entry_that_moved_on_is_never_overwritten(develop: PlanItem) -> None:
    rules = (_rule("unresolved"),)
    results = _results(
        rules,
        {"id": "develop/1", "status": "approach", "check": "x", "approach": "a"},
    )
    decided = RuleEntry(CLI, "rejected", reason="operator: no", proposed_in="other")
    automation = RuleAutomation().with_rule(rule_text_hash(CLI), decided)

    updated, opened, notices = record_results(
        automation, rules, results, (), develop, "v1", NOW
    )

    assert updated.rules[rule_text_hash(CLI)] == decided
    assert opened == ()
    assert "not overwritten" in notices[0]


def test_a_prepared_extension_of_an_approved_check_is_a_pending_revision(
    develop: PlanItem,
) -> None:
    rules = (_rule("approach-approved"),)
    results = _results(
        rules, {"id": "develop/1", "status": "approach", "check": "cli-surface"}
    )
    checks = parse_check_results(
        (
            json.dumps(
                {
                    "name": "cli-surface",
                    "argv": ["lint-tool", "--strict"],
                    "config": [],
                    "covers": ["develop/1"],
                    "proven": True,
                }
            ),
        ),
        rules,
        results,
        develop,
    )
    approved = _check(("other-hash",))
    automation = (
        RuleAutomation()
        .with_rule(
            rule_text_hash(CLI),
            RuleEntry(CLI, "approach-approved", check="cli-surface"),
        )
        .with_check("cli-surface", approved)
    )

    updated, opened, _ = record_results(
        automation, rules, results, checks, develop, "v1", NOW
    )

    check = updated.checks["cli-surface"]
    assert check.spec == approved.spec
    assert check.pending is not None
    assert check.pending.covers == (rule_text_hash(CLI),)
    assert updated.rules[rule_text_hash(CLI)].status == "proposed"
    assert opened == ("cli-surface",)


def test_new_checks_are_proposed_never_converted(develop: PlanItem) -> None:
    rules = (_rule("approach-approved"),)
    results = _results(
        rules, {"id": "develop/1", "status": "approach", "check": "cli-surface"}
    )
    checks = parse_check_results(
        (
            json.dumps(
                {
                    "name": "cli-surface",
                    "shell": "help-diff",
                    "assert": {"operator": "empty"},
                    "covers": ["develop/1"],
                    "proven": False,
                }
            ),
        ),
        rules,
        results,
        develop,
    )
    automation = RuleAutomation().with_rule(
        rule_text_hash(CLI), RuleEntry(CLI, "approach-approved", check="cli-surface")
    )

    updated, _, _ = record_results(
        automation, rules, results, checks, develop, "v1", NOW
    )

    assert updated.checks["cli-surface"].status == "proposed"
    assert updated.checks["cli-surface"].proposed_at == NOW


def _proposed() -> RuleAutomation:
    cli, names = rule_text_hash(CLI), rule_text_hash(NAMES)
    return (
        RuleAutomation()
        .with_rule(cli, RuleEntry(CLI, "approach-proposed", approach="a", check="c"))
        .with_rule(names, RuleEntry(NAMES, "ambiguous", candidates=("x", "y")))
        .with_rule(
            rule_text_hash(LOGS), RuleEntry(LOGS, "proposed", check="log-check")
        )
        .with_check("log-check", _check((rule_text_hash(LOGS),), "proposed"))
    )


KEYS = (rule_text_hash(CLI), rule_text_hash(NAMES), "log-check")


def test_approving_decides_approaches_and_converts_checks() -> None:
    automation, remaining, approved = apply_decisions(
        _proposed(),
        KEYS,
        Decisions(approve=(rule_text_hash(CLI)[:12], "log-check")),
        NOW,
    )

    assert automation.rules[rule_text_hash(CLI)].status == "approach-approved"
    assert automation.checks["log-check"].status == "converted"
    assert automation.checks["log-check"].approved_at == NOW
    assert automation.rules[rule_text_hash(LOGS)].status == "converted"
    assert remaining == (rule_text_hash(NAMES),)
    assert approved == ("log-check",)


def test_an_approach_and_a_pick_are_the_operators_own() -> None:
    automation, remaining, _ = apply_decisions(
        _proposed(),
        KEYS,
        Decisions(
            approaches=((rule_text_hash(CLI), "Diff `--help` output."),),
            picks=((rule_text_hash(NAMES)[:8], 2),),
        ),
        NOW,
    )

    cli = automation.rules[rule_text_hash(CLI)]
    assert (cli.status, cli.approach) == ("approach-approved", "Diff `--help` output.")
    names = automation.rules[rule_text_hash(NAMES)]
    assert (names.status, names.interpretation) == ("interpreted", "y")
    assert remaining == ("log-check",)


def test_rejecting_rejects_every_undecided_proposal() -> None:
    automation, remaining, _ = apply_decisions(
        _proposed(), KEYS, Decisions(reject="not now"), NOW
    )

    assert remaining == ()
    assert automation.rules[rule_text_hash(CLI)].reason == "operator: not now"
    assert automation.rules[rule_text_hash(NAMES)].status == "rejected"
    assert automation.checks["log-check"].status == "rejected"
    assert automation.rules[rule_text_hash(LOGS)].status == "rejected"


def test_an_approved_revision_replaces_the_running_check() -> None:
    revision = CheckSpec(Commands((CommandDefinition(argv=("new",)),)), covers=("h",))
    automation = RuleAutomation().with_check(
        "lint", replace(_check(("old",)), pending=revision)
    )

    approved, remaining, _ = apply_decisions(
        automation, ("lint",), Decisions(approve=("lint",)), NOW
    )

    assert approved.checks["lint"].spec == revision
    assert approved.checks["lint"].pending is None
    assert remaining == ()
    rejected, _, _ = apply_decisions(automation, ("lint",), Decisions(reject="no"), NOW)
    assert rejected.checks["lint"].status == "converted"
    assert rejected.checks["lint"].pending is None


def test_approvals_record_who_approved_and_in_which_run() -> None:
    automation, _, _ = apply_decisions(
        _proposed(),
        KEYS,
        Decisions(approve=(rule_text_hash(CLI), "log-check")),
        NOW,
        approved_by="auto",
        run="TASK-1/01-task",
    )

    cli = automation.rules[rule_text_hash(CLI)]
    assert (cli.approved_by, cli.approved_in) == ("auto", "TASK-1/01-task")
    check = automation.checks["log-check"]
    assert (check.approved_by, check.approved_in) == ("auto", "TASK-1/01-task")
    assert automation.rules[rule_text_hash(LOGS)].approved_by == "auto"
    # Nothing undecided is marked approved.
    assert automation.rules[rule_text_hash(NAMES)].approved_by is None


def _proven(proven: bool) -> RuleAutomation:
    check = _check((rule_text_hash(LOGS),), "proposed")
    return _proposed().with_check(
        "log-check", replace(check, spec=replace(check.spec, proven=proven))
    )


def test_operator_approval_decides_nothing_automatically() -> None:
    assert not automatic_decisions(_proven(True), KEYS, "operator")
    assert blocking_proposals(_proven(True), KEYS, "operator") == KEYS


def test_check_approval_approves_only_approaches() -> None:
    decisions = automatic_decisions(_proven(True), KEYS, "check")

    assert decisions == Decisions(approve=(rule_text_hash(CLI),))
    assert blocking_proposals(_proven(True), KEYS, "check") == KEYS


@pytest.mark.parametrize("proven", [True, False])
def test_auto_approval_approves_approaches_and_proven_checks(proven: bool) -> None:
    decisions = automatic_decisions(_proven(proven), KEYS, "auto")

    expected = (rule_text_hash(CLI), "log-check") if proven else (rule_text_hash(CLI),)
    assert decisions == Decisions(approve=expected)
    # An ambiguous rule and an unproven check never stop the task under auto.
    assert blocking_proposals(_proven(proven), KEYS, "auto") == ()


def test_revoking_rejects_the_check_and_the_rules_it_covers() -> None:
    logs = rule_text_hash(LOGS)
    automation = (
        RuleAutomation()
        .with_check("log-check", _check((logs, "gone")))
        .with_rule(logs, RuleEntry(LOGS, "converted", check="log-check"))
        .with_rule(
            rule_text_hash(CLI), RuleEntry(CLI, "converted", check="other-check")
        )
    )

    revoked, rules = revoke_check(automation, "log-check", "revoked: slow")

    assert rules == (logs,)
    check = revoked.checks["log-check"]
    assert (check.status, check.reason) == ("rejected", "revoked: slow")
    assert revoked.rules[logs].status == "rejected"
    assert revoked.rules[rule_text_hash(CLI)].status == "converted"
    with pytest.raises(StateError, match="already rejected"):
        revoke_check(revoked, "log-check", "again")
    with pytest.raises(StateError, match="has no check 'nope'"):
        revoke_check(revoked, "nope", "x")


@pytest.mark.parametrize(
    ("decisions", "message"),
    [
        (Decisions(approve=("nope",)), "not an undecided proposal"),
        (Decisions(approve=(rule_text_hash(NAMES),)), "--pick"),
        (Decisions(picks=((rule_text_hash(NAMES), 3),)), "from 1 to 2"),
        (Decisions(picks=((rule_text_hash(CLI), 1),)), "not an ambiguous rule"),
        (Decisions(approaches=((rule_text_hash(NAMES), "x"),)), "proposed approach"),
    ],
)
def test_decisions_are_validated(decisions: Decisions, message: str) -> None:
    with pytest.raises(StateError, match=message):
        apply_decisions(_proposed(), KEYS, decisions, NOW)


def test_hints_default_to_the_step(develop: PlanItem) -> None:
    step = replace(develop, requested_agent="claudecode", requested_model="sonnet")
    rule = replace(develop.rules[0], hints=RuleHints(model="opus"))

    assert effective_hints(rule, step) == RuleHints("claudecode", "opus", "auto")
