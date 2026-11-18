# SPDX-License-Identifier: GPL-3.0-or-later
"""TASK-74: the files ww's configuration names and its commands reach."""

from __future__ import annotations

from ww.actions.contracts import CommandDefinition, Commands
from ww.reached_files import (
    Reach,
    command_references,
    frontmatter,
    parse,
    reaches,
    root_references,
)


def test_root_references_leave_out_paths_built_at_run_time() -> None:
    text = (
        'python3 "$WW_ROOT/tools/a.py" ${WW_ROOT}/tools/b.sh '
        "{{ ww.root }}/tools/../tools/c.php $WW_ROOT/tools/*.sh "
        "$WW_ROOT/tools/$NAME.py {{ww.root}}/../outside.py $WW_ROOT/tools/a.py"
    )
    assert root_references(text) == ("tools/a.py", "tools/b.sh", "tools/c.php")


def test_command_references_read_shell_argv_args_and_env() -> None:
    command = Commands(
        (
            CommandDefinition(
                shell='sh "$WW_ROOT/tools/run.sh"',
                args=("{{ww.root}}/tools/arg.txt",),
                env=(("CONFIG", "{{ww.root}}/tools/env.toml"),),
            ),
            CommandDefinition(argv=("python3", "{{ww.root}}/tools/run.sh")),
        )
    )
    assert command_references(command) == (
        "tools/arg.txt",
        "tools/env.toml",
        "tools/run.sh",
    )


def test_frontmatter_is_split_like_a_rule_file_and_tolerates_errors() -> None:
    assert frontmatter("---\ncheck:\n  shell: x\n---\nBody.\n") == {
        "check": {"shell": "x"}
    }
    assert frontmatter("No frontmatter.\n") is None
    assert frontmatter("---\n: [\n---\nBody.\n") is None
    assert frontmatter("---\n- a list\n---\nBody.\n") is None
    assert frontmatter("---\ncheck: {}\nnever closed\n") is None


def test_reaches_name_the_command_and_every_file_it_needs() -> None:
    rule = parse(
        "rules/docs/lint.md",
        b"---\npaths: ['*.md']\ncheck:\n"
        b'  shell: python3 "$WW_ROOT/tools/lint.py"\n'
        b"  files: [tools/lint.py, tools/helper.php]\n---\nLint it.\n",
    )
    assert sorted(reaches("rules/docs/lint.md", rule)) == [
        Reach("rule file rules/docs/lint.md", "tools/helper.php"),
        Reach("rule file rules/docs/lint.md", "tools/lint.py"),
    ]
    store = parse(
        "ww-rule-automation.json",
        b'{"checks": {"header": {"shell": "sh \\"$WW_ROOT/tools/h.sh\\"",'
        b' "config": ["tools/h.toml"]}}}',
    )
    assert sorted(reaches("ww-rule-automation.json", store)) == [
        Reach("ww-rule-automation.json at checks.header", "tools/h.sh"),
        Reach("ww-rule-automation.json at checks.header", "tools/h.toml"),
    ]
    workflows = parse(
        "ww.yaml",
        b"workflows:\n  - name: task\n    steps:\n      - name: work\n"
        b"        rules:\n          - text: Lint it.\n"
        b"            argv: [python3, '{{ww.root}}/tools/lint.py']\n",
    )
    assert list(reaches("ww.yaml", workflows)) == [
        Reach("ww.yaml at workflows[0].steps[0].rules[0]", "tools/lint.py")
    ]
