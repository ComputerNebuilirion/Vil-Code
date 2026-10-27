"""VilAgent - 终端代码 Agent 包

公共出口：AgentLoop / LLMClient / State / Context / 安全与工具函数
导入本包即自动注册所有内置工具（file / edit / exec / git / search / todo）。
"""
from .context import Context
from .llm import LLMClient, LLMError
from .loop import AgentLoop
from .session import SessionManager
from .state import State
from .config import (
    CONFIG_DIR, CONFIG_FILE, DEFAULTS,
    load_config, save_config, get_value, set_value,
)

# 导入 tools 子包触发 @tool 注册
from . import tools  # noqa: F401
from .tools import execute_tool, get_tool_schemas
from .safety import (
    check_safety, classify, is_path_safe, sandbox_run,
    PermissionSystem, Rule, ALLOW, ASK, DENY,
    LEVEL_STRICT, LEVEL_BALANCED, LEVEL_YOLO, DEFAULT_LEVEL, normalize_level,
)

__all__ = [
    "AgentLoop",
    "LLMClient",
    "LLMError",
    "State",
    "SessionManager",
    "Context",
    "get_tool_schemas",
    "execute_tool",
    "check_safety",
    "classify",
    "sandbox_run",
    "is_path_safe",
    "PermissionSystem",
    "Rule",
    "ALLOW",
    "ASK",
    "DENY",
    "LEVEL_STRICT",
    "LEVEL_BALANCED",
    "LEVEL_YOLO",
    "DEFAULT_LEVEL",
    "normalize_level",
    "CONFIG_DIR",
    "CONFIG_FILE",
    "DEFAULTS",
    "load_config",
    "save_config",
    "get_value",
    "set_value",
]

__version__ = "0.1.0"
