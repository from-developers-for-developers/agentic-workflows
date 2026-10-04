# SPDX-License-Identifier: GPL-3.0-or-later
"""The design documents read the same from a checkout and an installation."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import ww.design_docs as design_docs
from ww.cli import main
from ww.design_docs import DESIGN_DOCUMENTS, read_design_document
from ww.errors import StateError

ROOT = Path(__file__).parents[2]
ASSETS = ROOT / "src/ww/assets"


@pytest.mark.parametrize("name", DESIGN_DOCUMENTS)
def test_a_checkout_reads_the_canonical_file(name: str) -> None:
    canonical = (ROOT / "documentation" / f"{name}.md").read_text(encoding="utf-8")

    assert read_design_document(name) == canonical


def test_a_packaged_copy_wins_over_the_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packaged = tmp_path / design_docs.PACKAGED_DIRECTORY
    packaged.mkdir()
    (packaged / "features.md").write_text("# Packaged\n", encoding="utf-8")
    monkeypatch.setattr(design_docs, "files", lambda _package: tmp_path)

    assert read_design_document("features") == "# Packaged\n"
    # A document the build did not package falls back to the checkout.
    assert read_design_document("examples").startswith("# Examples")


def test_a_missing_document_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(design_docs, "files", lambda _package: tmp_path)
    monkeypatch.setattr(design_docs, "_CHECKOUT", tmp_path / "absent")

    with pytest.raises(StateError, match="not available in this installation"):
        read_design_document("features")
    with pytest.raises(StateError, match="unknown document"):
        read_design_document("architecture")


@pytest.mark.parametrize("name", DESIGN_DOCUMENTS)
def test_the_docs_command_prints_the_document(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    (tmp_path / "ww.yaml").write_text(
        "workflows:\n  - name: task\n    steps:\n      - work: Work.\n",
        encoding="utf-8",
    )

    assert main(["--root", str(tmp_path), "docs", name]) == 0

    assert capsys.readouterr().out == read_design_document(name)


def test_the_docs_command_refuses_other_documents(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["docs", "architecture"])

    assert raised.value.code == 2


def _shipped_texts() -> list[tuple[str, str]]:
    paths = [*ASSETS.glob("*.md"), *(ASSETS / "workflows").glob("*.yaml")]
    return [(path.name, path.read_text(encoding="utf-8")) for path in paths]


@pytest.mark.parametrize(("name", "text"), _shipped_texts())
def test_shipped_skills_and_workflows_point_at_docs_not_repository_paths(
    name: str, text: str
) -> None:
    # A target project has no ww checkout; the documents are read with `docs`.
    assert not re.search(r"documentation/(specification|features|examples)", text), name


@pytest.mark.parametrize(("name", "text"), _shipped_texts())
def test_shipped_text_has_no_blanket_rule_for_verification_in_hooks(
    name: str, text: str
) -> None:
    flat = " ".join(text.split()).lower()

    assert "before_complete` hooks with" not in flat, name
    assert "never also ask the agent to run these commands" not in flat, name
