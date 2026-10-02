# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ww import package_updates as updates


def release(**overrides: object) -> list[dict[str, object]]:
    return [{"packagetype": "sdist", "requires_python": ">=3.10", **overrides}]


@pytest.mark.parametrize(
    ("installed", "pre", "expected"),
    [
        ("1.0.0", False, "1.0.1"),
        ("1.0.0", True, "1.1.0.dev2"),
        ("1.0.0.dev1", False, "1.1.0.dev2"),
        ("1.0.0b1", False, "1.1.0.dev2"),
        ("1.0.0rc1", False, "1.1.0.dev2"),
    ],
)
def test_installed_channel_controls_selection(
    installed: str, pre: bool, expected: str
) -> None:
    data = {"releases": {"1.0.1": release(), "1.1.0.dev2": release()}}
    assert updates.latest_compatible(data, installed, pre=pre) == expected


def test_final_release_supersedes_its_development_build() -> None:
    data = {"releases": {"1.0.0.dev2": release(), "1.0.0": release()}}
    assert updates.latest_compatible(data, "1.0.0.dev1") == "1.0.0"


def test_incompatible_yanked_empty_and_invalid_releases_are_skipped() -> None:
    data = {
        "releases": {
            "1.0.1": release(),
            "1.0.2": release(yanked=True),
            "1.0.3": release(requires_python=">=99"),
            "1.0.4": [],
            "garbage": release(),
            "1.0.5": release(packagetype="bdist_wheel", filename="invalid.whl"),
            "1.0.6": release(requires_python="invalid"),
        }
    }
    assert updates.latest_compatible(data, "1.0.0") == "1.0.1"


def test_notice_cache_checks_once_and_reprints_on_demand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WW_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(updates, "__version__", "1.0.0.dev1")
    calls = []

    def fetch() -> dict[str, object]:
        calls.append(True)
        return {"releases": {"1.0.0.dev2": release()}}

    monkeypatch.setattr(updates, "fetch_releases", fetch)
    notice = updates.pending_package_notice()
    assert notice is not None
    assert "1.0.0.dev1 → 1.0.0.dev2" in notice.render()
    assert "ww upgrade" in notice.render()
    updates.mark_package_announced(notice)
    assert updates.pending_package_notice() is None
    assert updates.last_package_notice() == notice
    assert len(calls) == 1
    assert updates.pending_package_notice(force=True) == notice
    assert len(calls) == 2


def test_offline_attempt_is_cached_and_installations_are_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WW_STATE_HOME", str(tmp_path))
    calls = []
    monkeypatch.setattr(updates, "fetch_releases", lambda: calls.append(True))
    assert updates.pending_package_notice() is None
    assert updates.pending_package_notice() is None
    assert len(calls) == 1
    monkeypatch.setattr(updates, "__version__", "1.0.0.dev100")
    assert updates.pending_package_notice() is None
    assert len(calls) == 2


def test_http_request_has_timeout_and_a_size_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            assert size == updates.MAX_RESPONSE_BYTES + 1
            return json.dumps({"releases": {}}).encode()

    def open_url(request, *, timeout):
        assert request.full_url == updates.PYPI_URL
        assert timeout == updates.HTTP_TIMEOUT_SECONDS
        return Response()

    monkeypatch.setattr(updates, "urlopen", open_url)
    assert updates.fetch_releases() == {"releases": {}}
