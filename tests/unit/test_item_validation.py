# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared validation of item collections: settings authority and item saves."""

from __future__ import annotations

from pathlib import Path

import pytest

from ww.cli import main
from ww.config import load_configuration
from ww.errors import ConfigurationError
from ww.plan import compile_workflow_plan
from ww.workflow_config import (
    ItemFieldUpdate,
    ItemFlow,
    StepDefinition,
    WorkflowConfiguration,
    WorkflowDefinition,
)
from ww.workflow_validation import validate_configuration


def _load(tmp_path: Path, text: str) -> WorkflowConfiguration:
    path = tmp_path / "ww.yaml"
    path.write_text(text, encoding="utf-8")
    return load_configuration(path)


def _passes(first: str, later: str) -> str:
    """Two passes over one collection, each with its own settings block."""
    return f"""workflows:
  - name: review
    steps:
      - collect: Collect.
        items:{first}
          steps: []
      - fix-together: Fix every item.
      - finish: Reuse the collected items.
        items:{later}
          steps:
            - report: Report it.
              item_phase: report
"""


# --- collection settings: the first declaration decides --------------------


@pytest.mark.parametrize(
    "later",
    [
        "",
        "\n          persistent: true\n          identity: source",
        "\n          unique: [reply, source]\n          identity: source",
        "\n          unique: [source, reply]",
    ],
    ids=["omitted", "repeated", "repeated-unique", "unique-in-another-order"],
)
def test_a_later_pass_may_omit_or_repeat_the_collection_settings(
    tmp_path: Path, later: str
) -> None:
    first = (
        "\n          persistent: true\n          identity: source"
        "\n          unique: [reply]"
    )
    _load(tmp_path, _passes(first, later))


@pytest.mark.parametrize(
    ("first", "later", "message"),
    [
        (
            "\n          persistent: true",
            "\n          persistent: false",
            "step 'finish' sets items.persistent to false, but the collection's "
            "first items step 'collect' sets it to true",
        ),
        (
            "",
            "\n          persistent: true",
            "sets items.persistent to true, but the collection's first items "
            "step 'collect' leaves it unset",
        ),
        (
            "",
            "\n          identity: source",
            "sets items.identity to 'source', but the collection's first items "
            "step 'collect' leaves it unset",
        ),
        (
            "\n          persistent: false",
            "\n          persistent: true",
            "sets items.persistent to true",
        ),
        (
            "\n          identity: source",
            "\n          identity: reply",
            "sets items.identity to 'reply', but the collection's first items "
            "step 'collect' sets it to 'source'",
        ),
        (
            "\n          unique: [source]",
            "\n          unique: [source, reply]",
            "sets items.unique to [reply, source]",
        ),
        (
            "\n          identity: source",
            "\n          unique: [reply]",
            "sets items.unique to [reply, source], but the collection's first "
            "items step 'collect' sets it to [source]",
        ),
    ],
    ids=[
        "persistent",
        "first-omits",
        "first-omits-identity",
        "explicit-false",
        "identity",
        "unique",
        "identity-implies-unique",
    ],
)
def test_a_later_pass_cannot_contradict_the_collection_settings(
    tmp_path: Path, first: str, later: str, message: str
) -> None:
    with pytest.raises(ConfigurationError) as error:
        _load(tmp_path, _passes(first, later))
    assert message in str(error.value)
    assert "workflow 'review'" in str(error.value)


def test_an_explicit_unique_equal_to_identity_is_not_read_as_omitted(
    tmp_path: Path,
) -> None:
    first = "\n          identity: comment_id\n          unique: [comment_id, thread]"
    later = "\n          identity: comment_id\n          unique: [comment_id]"
    with pytest.raises(ConfigurationError) as error:
        _load(tmp_path, _passes(first, later))
    assert (
        "step 'finish' sets items.unique to [comment_id], but the collection's "
        "first items step 'collect' sets it to [comment_id, thread]"
    ) in str(error.value)
    # Repeating identity alone, or naming only the other field, still fits.
    _load(tmp_path, _passes(first, "\n          identity: comment_id"))
    _load(tmp_path, _passes(first, "\n          unique: [thread]"))


def test_identity_alone_still_makes_its_field_unique_at_runtime(
    tmp_path: Path,
) -> None:
    configuration = _load(tmp_path, _passes("\n          identity: source", ""))
    plan = compile_workflow_plan(configuration, tmp_path, "review", "codex")
    collector = next(item for item in plan.items if item.item_operation == "collect")
    assert (collector.item_identity, collector.item_unique) == ("source", ("source",))


def test_settings_conflicts_are_found_in_groups_and_outcomes(tmp_path: Path) -> None:
    text = """workflows:
  - name: review
    steps:
      - collect: Collect.
        items:
          persistent: true
          steps: []
      - rounds: Review in rounds.
        steps:
          - assess:
              question: Anything left?
              outcomes:
                positive:
                  steps:
                    - again: Reuse the collected items.
                      items:
                        persistent: false
                        steps: []
          - decide: Decide.

"""
    with pytest.raises(ConfigurationError) as error:
        _load(tmp_path, text)
    assert "step 'rounds/assess/positive/again' sets items.persistent" in str(
        error.value
    )


def test_the_shared_validator_rejects_conflicts_from_any_frontend() -> None:
    """Built from definitions directly, without the YAML frontend."""

    def pass_step(name: str, flow: ItemFlow) -> StepDefinition:
        return StepDefinition(name=name, description="Collect.", items=flow)

    configuration = WorkflowConfiguration(
        modes=(),
        profiles=(),
        handlers=(),
        global_hooks=(),
        workflows=(
            WorkflowDefinition(
                name="review",
                steps=(
                    pass_step("collect", ItemFlow(identity="a", unique=("a",))),
                    pass_step("again", ItemFlow(identity="b", unique=("b",))),
                ),
            ),
        ),
    )
    with pytest.raises(ConfigurationError, match="items.identity"):
        validate_configuration(configuration)


def test_a_workflow_with_one_collection_per_workflow_is_checked_alone(
    tmp_path: Path,
) -> None:
    """Different workflows have different collections."""
    _load(
        tmp_path,
        """workflows:
  - name: one
    steps:
      - collect: Collect.
        items:
          persistent: true
          steps: []
  - name: two
    steps:
      - collect: Collect.
        items:
          persistent: false
          steps: []
""",
    )


# --- where item field saves are meaningful ---------------------------------


ITEM_SAVE = "saves:\n{indent}  - item.field.reply_id: The reply's ID."


def _with_save(indent: int) -> str:
    return ITEM_SAVE.format(indent=" " * indent)


def test_collector_and_per_item_stage_saves_are_valid(tmp_path: Path) -> None:
    configuration = _load(
        tmp_path,
        f"""workflows:
  - name: review
    steps:
      - collect: Collect.
        {_with_save(8)}
        items:
          steps:
            - reply: Reply.
              {_with_save(14)}
            - check: Check it.
              steps:
                - attempt: Try.
                  {_with_save(18)}

            - assess:
                question: Needs more?
                outcomes:
                  positive:
                    steps:
                      - more: More.
                        {_with_save(24)}
""",
    )
    plan = compile_workflow_plan(configuration, tmp_path, "review", "codex")
    assert {
        item.name for item in plan.items if item.update_item and item.phase == "step"
    } == {"collect", "reply", "attempt", "more"}


@pytest.mark.parametrize(
    ("steps", "where"),
    [
        (
            f"""      - collect: Collect.
        items:
          steps: []
      - fix-together: Fix every item.
        {_with_save(8)}
""",
            "step 'fix-together'",
        ),
        (
            f"""      - rounds: Rounds.
        steps:
          - batch: Batch.
            {_with_save(12)}

""",
            "step 'rounds/batch'",
        ),
        (
            f"""      - assess:
          question: Fix?
          outcomes:
            positive:
              steps:
                - fix: Fix.
                  {_with_save(18)}
""",
            "step 'assess/positive/fix'",
        ),
    ],
    ids=["between-passes", "in-a-group", "in-an-outcome"],
)
def test_unbound_item_saves_are_rejected(
    tmp_path: Path, steps: str, where: str
) -> None:
    with pytest.raises(ConfigurationError) as error:
        _load(tmp_path, f"workflows:\n  - name: review\n    steps:\n{steps}")
    message = str(error.value)
    assert f"workflow 'review' {where} saves item.field.reply_id outside any item" in (
        message
    )


HANDLERS = """handlers:
  - name: post-reply
    description: Post the reply.
    saves:
      - item.field.reply_id: The reply's ID.
  - name: reply-step
    description: Reply in the thread.
    saves:
      - item.field.reply_id: The reply's ID.
"""


def test_a_reusable_handler_is_checked_where_it_is_used(tmp_path: Path) -> None:
    # Defined and reused in per-item stages, as a step and as a hook.
    configuration = _load(
        tmp_path,
        HANDLERS
        + """workflows:
  - name: review
    steps:
      - collect: Collect.
        items:
          steps:
            - reply:
              handler: reply-step
              hooks:
                after_complete:
                  - post-reply: ~
""",
    )
    plan = compile_workflow_plan(configuration, tmp_path, "review", "codex")
    assert [
        (item.name, item.phase)
        for item in plan.items
        if item.update_item and item.item_template
    ] == [("reply", "step"), ("post-reply", "after_complete")]


@pytest.mark.parametrize(
    ("workflow", "where"),
    [
        (
            """      - collect: Collect.
        items:
          steps: []
      - reply-all:
        handler: reply-step
""",
            "step 'reply-all' saves",
        ),
        (
            """      - collect: Collect.
        items:
          steps: []
      - fix-together: Fix every item.
        hooks:
          after_complete:
            - post-reply: ~
""",
            "step 'fix-together' after_complete hook 'post-reply' saves",
        ),
        (
            """      - collect: Collect.
        items:
          steps: []
        hooks:
          before_start:
            - post-reply: ~
""",
            "step 'collect' before_start hook 'post-reply' saves",
        ),
    ],
    ids=["step-reference", "step-hook", "collector-hook"],
)
def test_a_reused_handler_outside_an_item_is_rejected(
    tmp_path: Path, workflow: str, where: str
) -> None:
    with pytest.raises(ConfigurationError) as error:
        _load(
            tmp_path,
            HANDLERS + f"workflows:\n  - name: review\n    steps:\n{workflow}",
        )
    assert f"workflow 'review' {where} item.field.reply_id outside any item" in str(
        error.value
    )


def test_global_hooks_with_item_saves_apply_only_to_per_item_stages(
    tmp_path: Path,
) -> None:
    hooks = """hooks:
  after_complete:
    - post-reply: ~
      steps: [{selector}]
"""
    workflows = """workflows:
  - name: review
    steps:
      - collect: Collect.
        items:
          steps:
            - reply: Reply.
      - wrap: Wrap up.
"""
    _load(tmp_path, HANDLERS + hooks.format(selector="reply") + workflows)
    with pytest.raises(ConfigurationError) as error:
        _load(tmp_path, HANDLERS + hooks.format(selector="wrap") + workflows)
    assert "step 'wrap' after_complete hook 'post-reply' saves" in str(error.value)
    with pytest.raises(ConfigurationError) as error:
        _load(
            tmp_path,
            HANDLERS
            + "hooks:\n  before_complete_workflow:\n    - post-reply: ~\n"
            + workflows,
        )
    assert "before_complete_workflow hook 'post-reply' saves" in str(error.value)


def test_an_unused_handler_with_item_saves_is_valid(tmp_path: Path) -> None:
    _load(
        tmp_path,
        HANDLERS + "workflows:\n  - name: review\n    steps:\n      - plain: Work.\n",
    )


def test_metadata_and_document_saves_outside_items_still_work(tmp_path: Path) -> None:
    configuration = _load(
        tmp_path,
        """documents:
  - name: notes
    description: Notes.
workflows:
  - name: review
    steps:
      - collect: Collect.
        items:
          steps: []
      - fix-together: Fix every item.
        saves:
          - metadata.fix.summary: What was fixed.
          - documents.notes: Record what was fixed.
""",
    )
    step = configuration.workflows[0].steps[1]
    assert step.save_metadata and step.update_document
    assert step.update_item == ()


def test_lint_rejects_an_unbound_item_save(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "ww.yaml").write_text(
        f"""workflows:
  - name: review
    steps:
      - collect: Collect.
        items:
          steps: []
      - fix-together: Fix every item.
        {_with_save(8)}
""",
        encoding="utf-8",
    )

    assert main(["--root", str(tmp_path), "lint"]) != 0
    assert "'fix-together' saves item.field.reply_id outside any item" in (
        capsys.readouterr().err
    )


def test_item_field_update_model_is_unchanged() -> None:
    """The check is about placement only; a valid save keeps its contract."""
    assert ItemFieldUpdate("reply_id").to_dict() == {
        "name": "reply_id",
        "description": "",
    }


# --- item_phase: only on an acting step of a per-item stage -----------------

_COLLECT = """      - collect: Record items.
        items:
          identity: comment_id
          steps: []
"""


def _assessment_with_phase(where: str) -> str:
    return f"""workflows:
  - name: probe
    steps:
{_COLLECT}      - answer: Reuse the items.
        items:
          steps:
            - assess:
                question: Does this comment get a reply?
                {where}
                positive:
                  steps:
                    - reply: Reply.
                      item_phase: report
                negative:
                  steps:
                    - skip: Nothing to reply.
      - wrap: Wrap up.
"""


def test_item_phase_on_an_assessment_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(
        ConfigurationError,
        match=r"step 'answer/assess' sets item_phase on an assessment.*"
        r"outcome steps that do the work",
    ):
        _load(tmp_path, _assessment_with_phase("item_phase: report"))


def test_item_phase_on_the_outcome_steps_of_an_assessment_is_valid(
    tmp_path: Path,
) -> None:
    configuration = _load(tmp_path, _assessment_with_phase(""))
    assert configuration is not None


def test_lint_rejects_item_phase_on_an_assessment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "ww.yaml").write_text(
        _assessment_with_phase("item_phase: report"), encoding="utf-8"
    )

    assert main(["--root", str(tmp_path), "lint"]) != 0
    assert "sets item_phase on an assessment" in capsys.readouterr().err


_PHASE_OUTSIDE = {
    "top-level": "      - fix: Fix it.\n        item_phase: resolve\n",
    "between-passes": (
        _COLLECT
        + "      - fix-together: Fix.\n        item_phase: resolve\n"
        + "      - again: Reuse.\n        items:\n          steps: []\n"
    ),
    "group-outside-items": (
        "      - repeat: Repeat.\n        steps:\n"
        "          - fix: Fix it.\n            item_phase: report\n"
    ),
    "through-a-handler": ("      - fix:\n        handler: phased\n"),
}


@pytest.mark.parametrize("case", sorted(_PHASE_OUTSIDE))
def test_item_phase_outside_a_per_item_stage_is_rejected(
    tmp_path: Path, case: str
) -> None:
    handlers = (
        "handlers:\n  - name: phased\n    description: Phased.\n"
        "    item_phase: resolve\n"
        if case == "through-a-handler"
        else ""
    )
    text = handlers + "workflows:\n  - name: probe\n    steps:\n" + _PHASE_OUTSIDE[case]
    with pytest.raises(
        ConfigurationError, match=r"sets item_phase outside any per-item stage"
    ):
        _load(tmp_path, text)


def test_lint_rejects_item_phase_outside_a_per_item_stage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "ww.yaml").write_text(
        "workflows:\n  - name: probe\n    steps:\n" + _PHASE_OUTSIDE["top-level"],
        encoding="utf-8",
    )

    assert main(["--root", str(tmp_path), "lint"]) != 0
    assert "step 'fix' sets item_phase outside any per-item stage" in (
        capsys.readouterr().err
    )


def test_item_phase_is_valid_in_per_item_stages_however_nested(
    tmp_path: Path,
) -> None:
    text = f"""handlers:
  - name: phased
    description: Phased.
    item_phase: resolve
workflows:
  - name: probe
    steps:
{_COLLECT}      - answer: Reuse the items.
        items:
          steps:
            - plain: Plain stage.
              item_phase: analyze
            - handled:
              handler: phased
            - repeat: Repeat.
              steps:
                - inner: Inner.
                  item_phase: resolve
            - assess:
                question: Reply?
                positive:
                  steps:
                    - reply: Reply.
                      item_phase: report
      - unused: Unused.
        items: ~
"""
    assert _load(tmp_path, text) is not None


def test_an_unused_handler_with_item_phase_is_valid(tmp_path: Path) -> None:
    _load(
        tmp_path,
        "handlers:\n  - name: phased\n    description: Phased.\n"
        "    item_phase: resolve\n"
        "workflows:\n  - name: probe\n    steps:\n      - plain: Work.\n",
    )
