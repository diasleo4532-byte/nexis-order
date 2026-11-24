from __future__ import annotations

from nexis.config import Settings
from nexis.memory import MemoryStore
from nexis.tools import apps, files, media, memory_tools, web
from nexis.tools import browser as browser_tools
from nexis.tools.browser import WorkspaceBrowser
from nexis.tools.registry import ToolRegistry
from nexis.workspace import Workspace


def build_registry(
    settings: Settings,
    workspace: Workspace,
    memory: MemoryStore,
    browser: WorkspaceBrowser,
) -> ToolRegistry:
    registry = ToolRegistry()
    files.register(registry, workspace)
    apps.register(registry, workspace)
    web.register(registry)
    browser_tools.register(registry, browser)
    media.register(registry, browser, workspace)
    memory_tools.register(registry, memory)
    return registry
