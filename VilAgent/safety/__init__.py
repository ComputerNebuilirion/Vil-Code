from .ast_check import (check_safety, classify, is_valid_python,
                        contains_unsafe_patterns, safe_python)
from .cmd_check import (match_dangerous_command, classify_command,
                        normalize_command, safe_command)
from .sandbox import sandbox_run, clean_stale_sandboxes, child_utf8_env, decode_output
from .permissions import (
    PermissionSystem, Rule, ALLOW, ASK, DENY,
    LEVEL_STRICT, LEVEL_BALANCED, LEVEL_AUTO, DEFAULT_LEVEL,
    normalize_level, level_label,
)
from pathlib import Path


def is_path_safe(path: str | Path, workspace: str | Path) -> bool:
    """
    检查路径是否在 workspace 范围内，防止目录穿越

    :param path: 相对或绝对路径
    :param workspace: 工作区根目录
    :return: 路径是否安全
    """
    try:
        ws = Path(workspace).resolve()
        target = (ws / path).resolve()
    except (ValueError, OSError):
        return False
    return target == ws or ws in target.parents


__all__ = [
    "check_safety",
    "classify",
    "classify_command",
    "safe_python",
    "safe_command",
    "match_dangerous_command",
    "normalize_command",
    "is_valid_python",
    "contains_unsafe_patterns",
    "sandbox_run",
    "clean_stale_sandboxes",
    "child_utf8_env",
    "decode_output",
    "is_path_safe",
    "PermissionSystem",
    "Rule",
    "ALLOW",
    "ASK",
    "DENY",
    "LEVEL_STRICT",
    "LEVEL_BALANCED",
    "LEVEL_AUTO",
    "DEFAULT_LEVEL",
    "normalize_level",
    "level_label",
]
