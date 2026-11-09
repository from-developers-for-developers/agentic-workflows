# SPDX-License-Identifier: GPL-3.0-or-later
"""``agent: {name, run: headless}`` in ww.yaml and ``agents.<name>.headless``
in ww.json: parsing, inheritance, the plan's record, and the static checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ww.agents import HeadlessSettings
from ww.cli import main
from ww.config import load_configuration
from ww.errors import ConfigurationError
from ww.execution_models.plan_codec import _plan_from_dict
from ww.plan import ParallelBranch, PlanItem, WorkflowPlan, WorkflowPlanCompiler
from ww.project_config import ProjectConfig, load_project_config


def _compiled(
    tmp_path: Path, workflows: str, settings: dict[str, object] | None = None
) -> WorkflowPlan:
    path = tmp_path / "ww.yaml"
    path.write_text(workflows, encoding="utf-8")
    if settings is not None:
        (tmp_path / "ww.json").write_text(json.dumps(settings), encoding="utf-8")
    return WorkflowPlanCompiler(
        load_configuration(path),
        tmp_path,
        "claudecode",
        "TASK-1",
        project_config=load_project_config(tmp_path / "ww.json"),
    ).compile("task")


def _steps(plan: WorkflowPlan) -> dict[str, PlanItem]:
    return {item.step: item for item in plan.items if item.phase == "step"}


def test_a_step_names_its_agent_and_how_it_runs(tmp_path: Path) -> None:
    steps = _steps(
        _compiled(
            tmp_path,
            """workflows:
  - name: task
    steps:
      - review: Review the change.
        agent: {name: codex, run: headless}
        model: gpt-6.1-sol
        reasoning: xhigh
      - fix: Fix the findings.
        agent: codex
      - note: Write a note.
        agent: {name: cursor}
""",
        )
    )

    assert steps["review"].requested_agent == "codex"
    assert steps["review"].requested_run == "headless"
    assert steps["fix"].requested_run == "session"
    assert (steps["note"].requested_agent, steps["note"].requested_run) == (
        "cursor",
        "session",
    )


def test_run_is_inherited_together_with_the_agent(tmp_path: Path) -> None:
    steps = _steps(
        _compiled(
            tmp_path,
            """workflows:
  - name: task
    agent: {name: claudecode, run: headless}
    steps:
      - develop: Develop.
      - name: group
        steps:
          - review: Review.
            agent: codex
          - fix: Fix.
            model: opus
      - name: plan
        description: Plan the change with the operator.
        role: manager
""",
        )
    )

    assert steps["develop"].requested_run == "headless"
    assert (
        steps["group/review"].requested_agent,
        steps["group/review"].requested_run,
    ) == (
        "codex",
        "session",
    )
    assert steps["group/fix"].requested_run == "headless"
    # An inherited headless agent passes over a manager's step.
    assert steps["plan"].requested_run == "session"


@pytest.mark.parametrize(
    ("agent", "message"),
    [
        ("{name: codex, run: api}", "reserved"),
        ("{name: codex, run: remote}", "run must be one of"),
        ("{run: headless}", "agent.name is required"),
        ("{name: codex, mode: headless}", "unknown key(s): mode"),
        ("{name: auto, run: headless}", "must not be 'auto'"),
    ],
)
def test_malformed_agent_requests_are_refused(
    tmp_path: Path, agent: str, message: str
) -> None:
    with pytest.raises(
        ConfigurationError, match=message.replace("(", r"\(").replace(")", r"\)")
    ):
        _compiled(
            tmp_path,
            f"""workflows:
  - name: task
    steps:
      - review: Review.
        agent: {agent}
""",
        )


@pytest.mark.parametrize(
    ("step", "message"),
    [
        (
            "role: manager",
            "run: headless needs a step a worker agent performs",
        ),
        (
            "interactive: true",
            "run: headless needs a step a worker agent performs",
        ),
    ],
)
def test_a_step_that_names_a_headless_agent_must_be_a_workers(
    tmp_path: Path, step: str, message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        _compiled(
            tmp_path,
            f"""workflows:
  - name: task
    steps:
      - review: Review.
        agent: {{name: codex, run: headless}}
        {step}
""",
        )


def test_only_an_agent_with_a_headless_launcher_runs_headless(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="cannot run agent 'custom:aider'"):
        _compiled(
            tmp_path,
            """workflows:
  - name: task
    steps:
      - review: Review.
        agent: {name: "custom:aider", run: headless}
""",
        )


@pytest.mark.parametrize(
    ("agent", "model", "reasoning", "message"),
    [
        ("cursor", "gpt-5", "high", "takes no reasoning setting"),
        ("codex", "gpt-6.1-sol", "extreme", "reasoning 'extreme'"),
        ("codex", "cheapest", "auto", "defines no cheapest model"),
        ("claudecode", "opus", "ultra", "reasoning 'ultra'"),
    ],
)
def test_a_headless_steps_model_and_reasoning_are_checked_when_it_compiles(
    tmp_path: Path, agent: str, model: str, reasoning: str, message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        _compiled(
            tmp_path,
            f"""workflows:
  - name: task
    steps:
      - review: Review.
        agent: {{name: {agent}, run: headless}}
        model: {model}
        reasoning: {reasoning}
""",
        )


def test_the_same_settings_in_a_session_are_not_checked(tmp_path: Path) -> None:
    steps = _steps(
        _compiled(
            tmp_path,
            """workflows:
  - name: task
    steps:
      - review: Review.
        agent: cursor
        reasoning: high
""",
        )
    )

    assert steps["review"].requested_reasoning == "high"


def test_headless_branches_are_recorded_and_survive_a_round_trip(
    tmp_path: Path,
) -> None:
    plan = _compiled(
        tmp_path,
        """workflows:
  - name: task
    steps:
      - name: review
        description: Merge the reviews.
        parallel: true
        steps:
          - security: Review security.
            agent: {name: codex, run: headless}
            model: gpt-6.1-sol
          - quality: Review quality.
""",
    )
    review = _steps(plan)["review"]

    security, quality = review.parallel_branches
    assert (security.requested_agent, security.requested_run) == ("codex", "headless")
    assert quality.requested_run == "session"
    assert security.to_dict()["requested_run"] == "headless"
    assert "requested_run" not in quality.to_dict()
    assert ParallelBranch.from_dict(security.to_dict(), "branch") == security
    with pytest.raises(ValueError, match="requested_run must be one of"):
        ParallelBranch.from_dict({**security.to_dict(), "requested_run": "x"}, "b")
    assert _plan_from_dict(plan.to_dict()) == plan


def test_a_plan_records_run_only_when_it_is_not_a_session(tmp_path: Path) -> None:
    plan = _compiled(
        tmp_path,
        """workflows:
  - name: task
    steps:
      - review: Review.
        agent: {name: codex, run: headless}
      - fix: Fix.
""",
    )
    steps = _steps(plan)

    assert steps["review"].to_dict()["requested_run"] == "headless"
    assert "requested_run" not in steps["fix"].to_dict()
    assert _plan_from_dict(plan.to_dict()) == plan


def test_headless_settings_are_read_per_agent_and_merged_with_local_ones(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww.json").write_text(
        json.dumps(
            {
                "agents": {
                    "claudecode": {
                        "headless": {
                            "permissions": "full",
                            "timeout_minutes": 60,
                            "max_parallel": 2,
                            "fallback_model": "sonnet",
                            "max_budget_usd": 5,
                            "env": {"CLAUDE_CONFIG_DIR": "/srv/claude"},
                            "isolated": True,
                            "args": ["--name", "ww"],
                            "models": {"review-model": "opus"},
                        }
                    },
                    "aider": {"title": "Aider"},
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "ww.local.json").write_text(
        json.dumps({"agents": {"claudecode": {"headless": {"binary": "/opt/claude"}}}}),
        encoding="utf-8",
    )

    config = load_project_config(tmp_path / "ww.json")

    assert config.headless_settings("claudecode") == HeadlessSettings(
        binary="/opt/claude",
        permissions="full",
        timeout_minutes=60.0,
        max_parallel=2,
        fallback_model="sonnet",
        max_budget_usd=5.0,
        env={"CLAUDE_CONFIG_DIR": "/srv/claude"},
        isolated=True,
        args=("--name", "ww"),
        models={"review-model": "opus"},
    )
    assert config.headless_settings("codex") == HeadlessSettings()
    assert [agent.name for agent in config.agents] == ["aider"]


@pytest.mark.parametrize(
    ("agents", "message"),
    [
        ({"codex": {"title": "Mine"}}, "takes only headless settings"),
        ({"codex": {"headless": {"timeout": 5}}}, "unknown key\\(s\\): timeout"),
        ({"codex": {"headless": {"permissions": "all"}}}, "permissions must be one of"),
        ({"codex": {"headless": {"max_parallel": 0}}}, "max_parallel must be"),
        ({"codex": {"headless": {"timeout_minutes": -1}}}, "must be a positive number"),
        ({"codex": {"headless": {"args": "--json"}}}, "args must be a list"),
        ({"codex": {"headless": {"env": {"A": 1}}}}, "env must be an object"),
        ({"codex": {"headless": {"isolated": "yes"}}}, "isolated must be true"),
        ({"aider": {"headless": {}}}, "unknown key\\(s\\): headless"),
    ],
)
def test_malformed_headless_settings_are_refused(
    tmp_path: Path, agents: dict[str, object], message: str
) -> None:
    (tmp_path / "ww.json").write_text(json.dumps({"agents": agents}), "utf-8")

    with pytest.raises(ConfigurationError, match=message):
        load_project_config(tmp_path / "ww.json")


def test_settings_an_agent_cannot_honour_fail_its_headless_steps(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigurationError, match="permissions 'read-only'"):
        _compiled(
            tmp_path,
            """workflows:
  - name: task
    steps:
      - review: Review.
        agent: {name: codex, run: headless}
""",
            {"agents": {"codex": {"headless": {"permissions": "read-only"}}}},
        )


def test_model_aliases_resolve_before_the_checks(tmp_path: Path) -> None:
    workflows = """workflows:
  - name: task
    steps:
      - review: Review.
        agent: {name: codex, run: headless}
        model: cheap-review
"""
    with pytest.raises(ConfigurationError, match="defines no cheapest model"):
        _compiled(
            tmp_path,
            workflows,
            {
                "agents": {
                    "codex": {"headless": {"models": {"cheap-review": "cheapest"}}}
                }
            },
        )
    plan = _compiled(
        tmp_path,
        workflows,
        {"agents": {"codex": {"headless": {"models": {"cheap-review": "gpt-6-luna"}}}}},
    )
    assert _steps(plan)["review"].requested_model == "cheap-review"


def test_the_plan_page_says_which_steps_ww_runs_headless(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - review: Review.
        agent: {name: codex, run: headless}
""",
        encoding="utf-8",
    )

    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "plan",
                "--workflow",
                "task",
                "--agent",
                "claudecode",
            ]
        )
        == 0
    )
    assert "- Agent: `codex`, run headless by ww" in capsys.readouterr().out


def test_agents_check_uses_the_configured_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    program = tmp_path / "my-codex"
    program.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  '--version') echo 'codex-cli 0.164.0' ;;\n"
        "  'login status') echo 'Logged in using ChatGPT' ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    program.chmod(0o755)
    (tmp_path / "ww.local.json").write_text(
        json.dumps({"agents": {"codex": {"headless": {"binary": str(program)}}}}),
        encoding="utf-8",
    )

    assert main(["--root", str(tmp_path), "agents", "--check", "codex"]) == 0
    assert f"- Binary: {program}" in capsys.readouterr().out
    assert ProjectConfig().headless_settings("codex") == HeadlessSettings()
