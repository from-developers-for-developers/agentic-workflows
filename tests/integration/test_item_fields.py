# SPDX-License-Identifier: GPL-3.0-or-later
"""Custom item fields: declared per step, enforced on completion, unique."""

import json
from pathlib import Path

import pytest

from tests.workflow_helpers import start_after_init
from ww.cli import main
from ww.errors import ConfigurationError, StateError
from ww.items import WorkItem
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage

WORKFLOWS = """workflows:
  - name: review
    steps:
      - name: collect
        description: Split the pull request by comment.
        items:
          shared: true
          identity: bitbucket_comment_id
          unique: [bitbucket_reply_id]
          steps:
            - name: fix
              description: "Fix comment {{field.bitbucket_comment_id}}: {{item.text}}"
              resolve_item: ~
            - name: reply
              description: Reply in the thread.
              report_item: ~
              update_item:
                - bitbucket_reply_id: The ID of the reply you posted.
                - bitbucket_thread_resolved: Set to true once the thread is resolved.
"""


def _service(tmp_path: Path) -> WorkflowService:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(WORKFLOWS, encoding="utf-8")
    return WorkflowService(Storage(tmp_path))


def test_fields_are_declared_enforced_unique_and_interpolated(tmp_path: Path) -> None:
    service = _service(tmp_path)
    md = MarkdownOutputAdapter()
    start_after_init(service, "review", "TASK-1", agent="codex")
    collect = service.next("TASK-1")
    assert (collect.collects_items, collect.item_identity, collect.item_unique) == (
        True,
        "bitbucket_comment_id",
        ("bitbucket_comment_id", "bitbucket_reply_id"),
    )
    rendered = md.render_instruction(collect)
    assert "### Item fields" in rendered
    assert "A new item must carry `bitbucket_comment_id`" in rendered
    assert "--field bitbucket_comment_id=<value>" in rendered
    assert "a value may appear once over all items" in rendered
    assert "./ww item TASK-1 --by <name>=<value>" in rendered

    # Identity is required; the unique pool spans both fields.
    with pytest.raises(StateError, match="needs the field 'bitbucket_comment_id'"):
        service.add_item("TASK-1", WorkItem("c1", "Rename it."))
    service.add_item(
        "TASK-1",
        WorkItem("c1", "Rename it.", fields=(("bitbucket_comment_id", "100"),)),
    )
    with pytest.raises(StateError, match="'100' is already the bitbucket_comment_id"):
        service.add_item(
            "TASK-1",
            WorkItem("dup", "Again.", fields=(("bitbucket_comment_id", "100"),)),
        )
    service.add_item(
        "TASK-1",
        WorkItem("c2", "Add a test.", fields=(("bitbucket_comment_id", "101"),)),
    )
    assert service.find_item("TASK-1", "bitbucket_comment_id", "101").id == "c2"
    with pytest.raises(StateError, match="no item has bitbucket_comment_id = '9'"):
        service.find_item("TASK-1", "bitbucket_comment_id", "9")
    service.complete("TASK-1", artifact="split", summary_for_next="Two comments.")

    # The fix stage sees the item's fields and text in its prompt.
    fix = service.next("TASK-1")
    assert fix.action_text is not None
    assert "Fix comment 100: Rename it." in fix.action_text
    service.update_item("TASK-1", "c1", actual_solution="Renamed.", resolved=True)
    service.complete("TASK-1", artifact="fixed", summary_for_next="Renamed.")

    # The reply stage must set its declared fields, several in one call.
    reply = service.next("TASK-1")
    assert [f.name for f in reply.required_item_fields] == [
        "bitbucket_reply_id",
        "bitbucket_thread_resolved",
    ]
    rendered = md.render_instruction(reply)
    assert "Set these custom fields on this step's item before completing" in rendered
    assert "- `bitbucket_reply_id` — The ID of the reply you posted." in rendered
    assert "./ww update-item TASK-1 --id <id> --field <name>=<value>" in rendered
    with pytest.raises(StateError, match="still empty: bitbucket_reply_id on c1"):
        service.complete("TASK-1", artifact="replied", summary_for_next="x")
    service.update_item(
        "TASK-1",
        "c1",
        reported=True,
        fields={"bitbucket_reply_id": "200", "bitbucket_thread_resolved": "true"},
    )
    assert dict(service.item("TASK-1", "c1").fields) == {
        "bitbucket_comment_id": "100",
        "bitbucket_reply_id": "200",
        "bitbucket_thread_resolved": "true",
    }
    service.complete("TASK-1", artifact="replied", summary_for_next="Replied.")

    # A reply's ID is taken across the pool: no item may be added for it,
    # and no other item may claim it.
    service.next("TASK-1")
    with pytest.raises(StateError, match="already the bitbucket_reply_id of item 'c1'"):
        service.update_item("TASK-1", "c2", fields={"bitbucket_reply_id": "200"})
    with pytest.raises(StateError, match="invalid item field name"):
        service.update_item("TASK-1", "c2", fields={"bad name": "x"})


def test_the_pool_reaches_the_shared_store_and_the_cli_takes_fields(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _service(tmp_path)
    start_after_init(service, "review", "TASK-2", agent="codex")
    service.next("TASK-2")
    root = ["--root", str(tmp_path)]
    assert (
        main(
            [
                *root,
                "add-item",
                "TASK-2",
                "--id",
                "c1",
                "--item",
                "Fix.",
                "--field",
                "bitbucket_comment_id=100",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                *root,
                "update-item",
                "TASK-2",
                "--id",
                "c1",
                "--field",
                "bitbucket_reply_id=200",
                "--field",
                "foo=bar",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main([*root, "item", "TASK-2", "--by", "bitbucket_reply_id=200"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["fields"] == {
        "bitbucket_comment_id": "100",
        "bitbucket_reply_id": "200",
        "foo": "bar",
    }
    assert main([*root, "item", "TASK-2"]) == 1
    assert "item takes --id or --by" in capsys.readouterr().err
    assert (
        main(
            [
                *root,
                "add-item",
                "TASK-2",
                "--id",
                "c2",
                "--item",
                "Own reply.",
                "--field",
                "bitbucket_comment_id=200",
            ]
        )
        == 1
    )
    assert "'200' is already the bitbucket_reply_id of item 'c1'" in (
        capsys.readouterr().err
    )

    # The store carries the fields into the next run, and the pool sees them.
    stored = service.tasks.read_shared_items("TASK-2")
    assert dict(stored[0].fields)["bitbucket_reply_id"] == "200"
    seeded = service._seed_shared_items("TASK-2", service.load("TASK-2")[1].plan)
    assert seeded is not None and dict(seeded[0].fields)["bitbucket_reply_id"] == "200"


def test_field_declarations_are_validated(tmp_path: Path) -> None:
    def load(items: str) -> None:
        (tmp_path / "ww-agentic-workflows.yaml").write_text(
            "workflows:\n  - task: ~\n    steps:\n      - collect: Collect.\n"
            f"        items:\n{items}",
            encoding="utf-8",
        )
        WorkflowService(Storage(tmp_path)).start("task", "TASK-3", agent="codex")

    with pytest.raises(ConfigurationError, match="identity must be a field name"):
        load("          identity: 'bad name'\n")
    with pytest.raises(ConfigurationError, match="unique must be a list of field"):
        load("          unique: reply_id\n")
    with pytest.raises(ConfigurationError, match="must be a non-empty normalized name"):
        load("          update_item:\n            - 'bad name': x\n")
