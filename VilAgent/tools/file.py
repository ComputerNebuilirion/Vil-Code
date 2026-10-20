"""文件操作工具"""
import fnmatch
import os
import shutil
import subprocess
from pathlib import Path

from . import tool
from ..i18n import L
from ..safety import is_path_safe


@tool(
    name="read_file",
    description=L(
        "读取文件内容，支持指定起始行和行数限制",
        "Read file contents, with optional start line and line-count limit",
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": L("文件路径（相对于工作区）", "File path (relative to workspace)")},
            "offset": {"type": "integer", "description": L("起始行号（0-based）", "Start line (0-based)"), "default": 0},
            "limit": {"type": "integer", "description": L("读取行数（0=全部）", "Number of lines to read (0=all)"), "default": 50},
        },
        "required": ["path"],
    },
    readonly=True,
)
def read_file(path: str, offset: int = 0, limit: int = 50, _ctx=None) -> str:
    ws = Path(_ctx["workspace"])
    if not is_path_safe(path, ws):
        return f"Error: path '{path}' escapes workspace"

    full = (ws / path).resolve()
    if not full.is_file():
        return f"Error: not a file: {path}"

    content = full.read_text(encoding="utf-8", errors="replace")
    lines = content.splitlines()

    start = offset
    end = offset + limit if limit > 0 else len(lines)
    selected = lines[start:end]

    # 加行号方便 LLM 定位
    numbered = [f"{i+start+1:4d} | {line}" for i, line in enumerate(selected)]
    return "\n".join(numbered) if numbered else "(empty file)"


@tool(
    name="write_file",
    description=L("写入或覆盖文件内容（需要 --trust）", "Write or overwrite file contents (requires --trust)"),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": L("文件路径（相对于工作区）", "File path (relative to workspace)")},
            "content": {"type": "string", "description": L("要写入的内容", "Content to write")},
            "append": {"type": "boolean", "description": L("是否追加而非覆盖", "Append instead of overwrite"), "default": False},
        },
        "required": ["path", "content"],
    },
    requires_trust=True,
)
def write_file(path: str, content: str, append: bool = False, _ctx=None) -> str:
    ws = Path(_ctx["workspace"])
    if not is_path_safe(path, ws):
        return f"Error: path '{path}' escapes workspace"

    full = ws / path
    full.parent.mkdir(parents=True, exist_ok=True)

    # 覆盖模式：读旧内容塞 _ctx["_file_diff"]，供 loop emit 给 view 渲染。
    # diff 只给人看，不进 result/tool_msg，LLM 不见，省 token。
    # append 模式跳过（追加做 diff 没意义）。
    if not append and _ctx is not None:
        old = (full.read_text(encoding="utf-8", errors="replace")
               if full.is_file() else "")
        _ctx["_file_diff"] = {"path": path, "old": old, "new": content}

    mode = "a" if append else "w"
    with open(full, mode, encoding="utf-8") as f:
        f.write(content)

    action = "appended to" if append else "wrote"
    return f"{action} {len(content)} chars to {path}"


@tool(
    name="delete_file",
    description=L("删除文件（破坏性操作，需 trust，会交互确认）", "Delete a file (destructive; needs trust, asks for confirmation)"),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": L("文件路径（相对于工作区）", "File path (relative to workspace)")},
        },
        "required": ["path"],
    },
    requires_trust=True,
)
def delete_file(path: str, _ctx=None) -> str:
    ws = Path(_ctx["workspace"])

    # —— 防呆层（在权限确认之前，直接拒绝明显误用）——
    # 1. 通配符：delete_file 不支持 glob，防 agent 当 rm *.py 用
    if any(c in path for c in "*?["):
        rm_hint = "del" if os.name == "nt" else "rm"
        return (f"Error: wildcards not supported in delete_file "
                f"(got '{path}'); use run_command('{rm_hint}') if you really need glob")

    # 2. 空路径 / 根路径
    if not path or path in (".", "/", "\\"):
        return f"Error: invalid path: '{path}'"

    # 3. 路径越界（防 .. 绕过）
    if not is_path_safe(path, ws):
        return f"Error: path '{path}' escapes workspace"

    # 4. 关键路径保护：删了会破坏项目基础设施或丢历史
    if _is_protected_delete(path):
        return (f"Error: '{path}' is protected (project infrastructure "
                f"or git/vil metadata); remove manually if needed")

    full = (ws / path).resolve()
    if not full.is_file():
        return f"Error: not a file: {path}"

    # 删除前读旧内容塞 _ctx["_file_diff"]：old=全文，new=""，
    # view 渲染的 diff 全是 - 行，让人看到删了什么（和 write_file 对称）。
    # 不进 result/tool_msg，LLM 不见，省 token。
    if _ctx is not None:
        old = full.read_text(encoding="utf-8", errors="replace")
        _ctx["_file_diff"] = {"path": path, "old": old, "new": ""}

    try:
        full.unlink()
    except PermissionError as e:
        return f"Error: permission denied (read-only or locked?): {e}"

    return f"deleted {path}"


# 关键路径保护：删除这些会破坏项目基础设施或丢失历史，直接拒绝。
# agent 想清理这些应该让用户手动处理，不通过 delete_file。
_PROTECTED_DELETE_FILES = {
    # 文档
    "README.md", "VilAgent/README.md",
    # CLI 入口（删了工具跑不起来）
    "vil-agent-cli.py", "vil-agent-frontend.py",
    # git / vil 元数据
    ".gitignore",
    # 配置（工作区内的项目配置）
    ".vil/config.json", ".vil/config.local.json", ".vil/permissions.json",
}
_PROTECTED_DELETE_PREFIXES = (
    ".git/",      # git 仓库元数据
    ".vil/",      # vil 配置 + sessions 历史
)


def _is_protected_delete(path: str) -> bool:
    """检查是否为受保护的关键路径（删除会破坏项目基础设施）"""
    # 规范化：反斜杠→正斜杠，剥掉前导 "./"（不能用 lstrip("./")，那是字符集
    # lstrip，会把 ".gitignore" 剥成 "gitignore" 导致前缀匹配失败）
    norm = path.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    norm = norm.lower()
    for p in _PROTECTED_DELETE_PREFIXES:
        if norm.startswith(p):
            return True
    if norm in {p.lower() for p in _PROTECTED_DELETE_FILES}:
        return True
    return False


def _glob_match(rel: str, pattern: str) -> bool:
    """rel（相对工作区路径）是否匹配 glob。

    兼容 LLM 常写的 '**/*.py'：fnmatch 的 * 会跨 '/'，但 '**/' 前缀要求
    至少一层目录，故同时尝试剥掉前缀和只用 basename 匹配。
    """
    if pattern in ("", "*", "**", "**/*", "**/**"):
        return True
    rel = rel.replace(os.sep, "/")
    if fnmatch.fnmatch(rel, pattern):
        return True
    stripped = pattern[3:] if pattern.startswith("**/") else pattern
    return fnmatch.fnmatch(rel, stripped) or fnmatch.fnmatch(os.path.basename(rel), stripped)


@tool(
    name="search_code",
    description=L("在代码中搜索文本，返回匹配的行和行号", "Search text in code, returning matching lines and line numbers"),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": L("搜索关键词", "Search keyword")},
            "glob": {"type": "string", "description": L("文件 glob 模式", "File glob pattern"), "default": "**/*"},
        },
        "required": ["query"],
    },
    readonly=True,
)
def search_code(query: str, glob: str = "**/*", _ctx=None) -> str:
    ws = str(_ctx["workspace"])

    # 优先用 ripgrep
    if shutil.which("rg"):
        try:
            result = subprocess.run(
                ["rg", "--line-number", "--no-heading", "-g", glob, query, ws],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=15,
            )
            output = result.stdout.strip()
            if not output:
                return f"no matches for '{query}'"
            # 截断防止过长
            lines = output.splitlines()
            if len(lines) > 100:
                return "\n".join(lines[:100]) + f"\n... ({len(lines)} total matches)"
            return output
        except subprocess.TimeoutExpired:
            return "Error: search timed out"

    # fallback: 遍历文件
    # 无 ripgrep 时 glob 必须自己过滤：否则 -g '**/*.py' 会静默失效，
    # 调用方以为收窄了范围，实际全库都在搜（还拖慢速度）。
    matches = []
    for root, dirs, files in os.walk(ws):
        # 跳过隐藏目录和常见忽略目录
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in
                   ("__pycache__", "node_modules", ".venv", "venv")]
        for fname in files:
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, ws)
            if not _glob_match(rel, glob):
                continue
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if query in line:
                            matches.append(f"{rel}:{i}: {line.rstrip()}")
                            if len(matches) >= 100:
                                matches.append(f"... (truncated, more than 100 matches)")
                                return "\n".join(matches)
            except (OSError, UnicodeError):
                continue

    return "\n".join(matches) if matches else f"no matches for '{query}'"


@tool(
    name="list_dir",
    description=L("列出目录内容", "List directory contents"),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": L("目录路径（相对于工作区）", "Directory path (relative to workspace)"), "default": "."},
        },
        "required": [],
    },
    readonly=True,
)
def list_dir(path: str = ".", _ctx=None) -> str:
    ws = Path(_ctx["workspace"])
    if not is_path_safe(path, ws):
        return f"Error: path '{path}' escapes workspace"

    target = (ws / path).resolve()
    if not target.is_dir():
        return f"Error: not a directory: {path}"

    entries = []
    for entry in sorted(target.iterdir()):
        prefix = "[DIR] " if entry.is_dir() else "      "
        entries.append(f"{prefix}{entry.name}")

    return "\n".join(entries) if entries else "(empty directory)"
