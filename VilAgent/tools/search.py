"""搜索工具：grep_file（代码库搜索，只读）"""
import os
from pathlib import Path

from . import tool
from ..i18n import L
from ..safety import is_path_safe


@tool(
    name="grep_file",
    description=L("在指定文件或目录中搜索关键词，返回匹配行（只读，不修改任何文件）",
                 "Search for a keyword in a file or directory, returning matching lines (read-only)"),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": L("搜索关键词（支持简单字符串，不需要正则）", "Keyword to search (plain string, no regex needed)")},
            "path": {"type": "string", "description": L("文件或目录路径（相对于工作区），目录会递归搜索", "File or directory path (relative to workspace); directories are searched recursively")},
            "max_results": {"type": "integer", "description": L("最多返回多少条匹配，默认 30", "Max matches to return (default 30)"), "default": 30},
        },
        "required": ["pattern", "path"],
    },
    readonly=True,
)
def grep_file(pattern: str, path: str, max_results: int = 30, _ctx=None) -> str:
    ws = Path(_ctx["workspace"])
    target = (ws / path).resolve()
    max_results = min(max_results, 200)  # 硬性上限 200，防 token 爆炸

    if not target.exists():
        return f"Error: path not found: {path}"

    # 显式越界校验（目录分支 os.walk 会走出工作区，靠 relative_to 抛异常兜底
    # 虽然不泄漏内容，但会变成 ValueError 栈信息；这里提前拦成统一错误）
    if not is_path_safe(path, ws):
        return f"Error: path '{path}' escapes workspace"

    matches: list[str] = []

    def _search_file(fp: Path) -> None:
        try:
            lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            return
        for i, line in enumerate(lines, 1):
            if pattern.lower() in line.lower():
                rel = fp.relative_to(ws)
                matches.append(f"{rel}:{i}: {line.strip()[:200]}")
                if len(matches) >= max_results:
                    return

    if target.is_file():
        if is_path_safe(path, ws):
            _search_file(target)
    elif target.is_dir():
        for root, dirs, files in os.walk(target):
            # 跳过常见噪音目录
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("__pycache__", "node_modules", ".git")]
            for fn in files:
                fp = Path(root) / fn
                if fp.suffix.lower() in (".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".md", ".txt", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".sh", ".bat", ".ps1", ".css", ".html", ".sql", ".rs", ".go", ".java", ".cpp", ".h", ".c", ".hpp"):
                    _search_file(fp)
                    if len(matches) >= max_results:
                        break
            if len(matches) >= max_results:
                break
    else:
        return f"Error: not a file or directory: {path}"

    if not matches:
        return f"No matches found for '{pattern}' in {path}"

    truncated = ""
    if len(matches) >= max_results:
        truncated = f"\n... truncated at {max_results} results"

    return "\n".join(matches) + truncated
