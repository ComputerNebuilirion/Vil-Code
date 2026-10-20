"""命令执行工具"""
import subprocess
import sys

from . import tool
from ..i18n import L
from ..safety import match_dangerous_command

# shell=True 在各平台的实际解释器：Windows = cmd.exe，POSIX = /bin/sh（非 bash）
# 把结果直接写进工具描述，提示 LLM 生成「当前平台语法」的命令，
# 避免 Linux 上产出 dir/copy/del 这类 cmd 风格命令。
_SHELL_NAME = "cmd.exe" if sys.platform == "win32" else "POSIX sh (/bin/sh, not bash)"


@tool(
    name="run_command",
    description=L(
        f"执行 shell 命令并返回输出（需要 --trust）。"
        f"当前平台由 {_SHELL_NAME} 执行，请使用该 shell 的语法"
        f"（Windows 用 dir/del，Linux/macOS 用 ls/rm）。",
        f"Run a shell command and return its output (requires --trust). "
        f"Commands run under {_SHELL_NAME} on this platform, so use its syntax "
        f"(dir/del on Windows, ls/rm on Linux/macOS).",
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": L("要执行的 shell 命令", "Shell command to run")},
            "timeout": {"type": "number", "description": L("超时秒数", "Timeout in seconds"), "default": 30},
        },
        "required": ["command"],
    },
    requires_trust=True,
)
def run_command(command: str, timeout: float = 30, _ctx=None) -> str:
    ws = str(_ctx["workspace"])

    # 危险命令黑名单（与 L2 权限层硬底线共用同一匹配器，避免两套口径不一致）
    hit = match_dangerous_command(command)
    if hit:
        return f"Error: blocked dangerous command pattern: {hit}"

    # Windows 下 shell=True 走 cmd.exe，默认 codepage 936 (GBK)
    # 先 chcp 65001 切到 UTF-8 codepage，让命令输出 UTF-8 字节
    # 这样我们用 encoding="utf-8" 解码才正确（否则中文乱码）
    if sys.platform == "win32":
        command = f"chcp 65001 >nul 2>&1 && {command}"
    # POSIX 下 shell=True 走 /bin/sh，输出本就是 UTF-8，无需前缀

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=ws,
            capture_output=True,
            # 不把 tty 的 stdin 继承给子进程：Linux 下 cat/read/ssh 之类会
            # 抢走终端按键，表现为输入被吃掉、提示符被抹掉
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout,
        )
        output = (result.stdout + result.stderr).strip()
        if not output:
            return "(no output)"
        # 截断过长输出
        if len(output) > 5000:
            return output[:5000] + f"\n... (truncated, {len(output)} total chars)"
        return output
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s"
