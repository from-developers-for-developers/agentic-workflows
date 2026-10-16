# SPDX-License-Identifier: GPL-3.0-or-later
"""The project as the page sees it, read afresh through the core each time.

A configuration that fails to load is not an error of the page: the page
shows why, and the tasks, which do not depend on it, stay readable.
"""

from __future__ import annotations

from dataclasses import dataclass

from ww.config import load_configuration
from ww.errors import WwError
from ww.extensions import ExtensionRegistry
from ww.service import WorkflowService
from ww.storage import Storage
from ww.workflow_config import WorkflowConfiguration


@dataclass(frozen=True)
class Project:
    storage: Storage
    extensions: ExtensionRegistry | None
    configuration: WorkflowConfiguration | None
    # Why the settings or the workflows could not be loaded, as ww says it.
    error: str | None = None

    @classmethod
    def load(cls, storage: Storage) -> Project:
        try:
            extensions = ExtensionRegistry.discover(
                storage.root, storage.project_config_path
            )
        except (WwError, OSError, ValueError) as error:
            return cls(storage, None, None, str(error))
        try:
            configuration = load_configuration(storage.config_path, extensions)
        except (WwError, OSError, ValueError) as error:
            return cls(storage, extensions, None, str(error))
        return cls(storage, extensions, configuration)

    def service(self) -> WorkflowService:
        return WorkflowService(self.storage, extensions=self.extensions)
