# SPDX-License-Identifier: GPL-3.0-or-later
"""Parts of the extension API that still work but are going away.

A deprecated feature keeps working for the rest of the current
``EXTENSION_API_VERSION``; removing it is a breaking change that moves the API
version. Each feature is declared once in ``DEPRECATED_API``, with when it was
deprecated, when it goes, what replaces it, and how to tell an extension uses
it. When the registry loads an extension that does, it records one warning
naming the extension and the replacement, and the CLI prints it the way it
prints a deprecated configuration (``ww warning: Deprecated: ...``), so the
extension's author hears of it while it still works.

Nothing in the API is deprecated yet, so ``DEPRECATED_API`` is empty.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ww.extensions.api import Extension


@dataclass(frozen=True)
class DeprecatedApiFeature:
    """One deprecated part of the extension API and how to find it in use.

    ``deprecated_in`` and ``removed_in`` are ww versions, the second the one
    expected to drop the feature together with an ``EXTENSION_API_VERSION``
    bump. ``used_by`` answers whether a loaded extension relies on the
    feature; it only reads the extension.
    """

    feature: str
    deprecated_in: str
    removed_in: str
    replacement: str
    used_by: Callable[[Extension], bool]

    def notice(self, extension: Extension, source: str) -> str:
        return (
            f"Deprecated: extension {extension.identifier!r} ({source}) uses "
            f"{self.feature}, deprecated in ww {self.deprecated_in} and removed "
            f"in ww {self.removed_in}; use {self.replacement} instead."
        )


# Every deprecated feature of the extension API, oldest first.
DEPRECATED_API: tuple[DeprecatedApiFeature, ...] = ()


def deprecation_notices(extension: Extension, source: str) -> tuple[str, ...]:
    """One warning per deprecated feature ``extension`` uses, from ``source``.

    A detector that fails is a bug in ww, not in the extension, so it is not
    contained.
    """
    return tuple(
        feature.notice(extension, source)
        for feature in DEPRECATED_API
        if feature.used_by(extension)
    )
