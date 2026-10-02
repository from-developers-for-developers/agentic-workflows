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
    effective_hints,
    parse_rule_results,
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
            assert: [empty]
"""
CLI, NAMES, LOGS = (
    "Keep the public CLI unchanged.",
    "Name things clearly.",
    "Log every failure.",
)
NOW = "2026-09-29T12:00:00Z"


@pytest.fixture
def develop(tmp_path: Path) -> PlanItem:
    (tmp_path / "ww.yaml").write_text(WORKFLOWS, encoding="utf-8")
    configuration = load_configuration(tmp_path / "ww.yaml")
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


def _rule(text: str = CLI, rule_id: str = "develop/1") -> VerificationRule:
    return VerificationRule(rule_id, text, rule_text_hash(text), "judged")


def test_resolution_covers_every_store_status(develop: PlanItem) -> None:
    automation = (
        RuleAutomation()
        .with_rule(rule_text_hash(CLI), RuleEntry(CLI, "converted", check="lint"))
        .with_rule(rule_text_hash(NAMES), RuleEntry(NAMES, "approach_proposed"))
        .with_rule(
            rule_text_hash(LOGS), RuleEntry(LOGS, "rejected", interpretation="Log.")
        )
        .with_check("lint", _check((rule_text_hash(CLI),)))
    )

    resolutions, checks = resolve_rules(develop, automation)

    assert [(entry.id, entry.status, entry.check) for entry in resolutions] == [
        ("develop/1", "converted", "lint"),
        ("develop/2", "judged", None),
        ("develop/3", "judged", None),
    ]
    assert resolutions[2].interpretation == "Log."
    assert [(check.id, check.source, check.covers) for check in checks] == [
        ("lint", "derived", ("develop/1",))
    ]


@pytest.mark.parametrize(
    ("status", "check_status", "resolved"),
    [
        (None, None, "judged"),
        ("interpreted", None, "judged"),
        ("approach_proposed", None, "judged"),
        ("approach_approved", None, "judged"),
        ("proposed", "proposed", "judged"),
        ("ambiguous", None, "judged"),
        ("not_convertible", None, "judged"),
        ("rejected", None, "judged"),
        ("converted", "converted", "converted"),
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
    assert [check.id for check in checks] == (
        ["lint"] if resolved == "converted" else []
    )


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


def test_needs_skip_checked_verified_and_waived_rules(develop: PlanItem) -> None:
    resolutions, lint = resolve_rules(
        develop,
        RuleAutomation()
        .with_rule(rule_text_hash(CLI), RuleEntry(CLI, "converted", check="lint"))
        .with_rule(
            rule_text_hash(NAMES),
            RuleEntry(
                NAMES, "approach_approved", check="names", interpretation="Clear."
            ),
        )
        .with_check("lint", _check((rule_text_hash(CLI),))),
    )
    record = _record(resolved_checks=lint, rule_resolutions=resolutions)

    needs = verification_needs(develop, record)

    assert [(need.id, need.state, need.interpretation) for need in needs] == [
        ("develop/2", "judged", "Clear."),
        ("develop/3", "judged", None),
    ]
    assert [need.check for need in needs] == [None, None]
    done = _record(
        resolved_checks=lint,
        rule_resolutions=resolutions,
        held_completion=HeldCompletion(
            verdicts=(RuleVerdict("develop/3", "pass", "v"),)
        ),
        checks_waived=(("develop/2", "not here"),),
    )
    assert verification_needs(develop, done) == ()


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
        (({"id": "develop/1", "status": "approach"},), "status must be judged"),
        (
            ({"id": "develop/1", "status": "not_convertible", "verdict": "pass"},),
            "status must be judged",
        ),
        (
            (
                {
                    "id": "develop/1",
                    "status": "judged",
                    "verdict": "pass",
                    "interpretation": "x",
                },
            ),
            "unknown keys: interpretation",
        ),
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
        _results((_rule(),), *payloads)


def test_rule_results_that_are_not_json_are_refused() -> None:
    with pytest.raises(StateError, match="not valid JSON"):
        parse_rule_results(("{nope",), (_rule(),))


def test_a_failing_verdict_carries_its_evidence() -> None:
    (result,) = _results(
        (_rule(),),
        {
            "id": "develop/1",
            "status": "judged",
            "verdict": "fail",
            "failures": [{"file": "cli.py", "line": 3, "what": "flag renamed"}],
        },
    )

    assert (result.status, result.verdict) == ("judged", "fail")
    assert result.failures[0].text() == "cli.py:3 — flag renamed"


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


def test_hints_default_to_the_step(develop: PlanItem) -> None:
    step = replace(develop, requested_agent="claudecode", requested_model="sonnet")
    rule = replace(develop.rules[0], hints=RuleHints(model="opus"))

    assert effective_hints(rule, step) == RuleHints("claudecode", "opus", "auto")


def _converted(config: tuple[str, ...]) -> RuleAutomation:
    cli = rule_text_hash(CLI)
    check = _check((cli,))
    return (
        RuleAutomation()
        .with_check("lint", replace(check, spec=replace(check.spec, config=config)))
        .with_rule(cli, RuleEntry(CLI, "converted", check="lint"))
    )


@pytest.mark.parametrize("path", ["../lint.toml", "/etc/hosts"])
def test_a_config_path_outside_the_directory_counts_as_missing(
    develop: PlanItem, tmp_path: Path, path: str
) -> None:
    (tmp_path / "lint.toml").write_text("", encoding="utf-8")
    directory = tmp_path / "tree"
    directory.mkdir()

    resolutions, checks = resolve_rules(
        develop, _converted((path,)), directory=directory
    )

    cli = next(entry for entry in resolutions if entry.id == "develop/1")
    assert (cli.status, cli.check, cli.missing) == ("judged", "lint", path)
    assert checks == ()


def test_a_judged_rule_names_its_check_whose_config_is_missing(
    develop: PlanItem,
) -> None:
    resolutions, checks = resolve_rules(
        develop, _converted(("lint.toml",)), directory=Path("/nonexistent")
    )

    needs = verification_needs(develop, _record(rule_resolutions=resolutions))

    cli = next(need for need in needs if need.id == "develop/1")
    assert (cli.state, cli.check, cli.missing) == ("judged", "lint", "lint.toml")
    assert checks == ()
