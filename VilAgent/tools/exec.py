"""命令执行工具"""
import os
import re
import signal
import subprocess
import sys

from . import tool
from ..i18n import L, t
from ..safety import match_dangerous_command, child_utf8_env, decode_output

# shell=True 在各平台的实际解释器：Windows = cmd.exe，POSIX = /bin/sh（非 bash）
# 把结果直接写进工具描述，提示 LLM 生成「当前平台语法」的命令，
# 避免 Linux 上产出 dir/copy/del 这类 cmd 风格命令。
_SHELL_NAME = "cmd.exe" if sys.platform == "win32" else "POSIX sh (/bin/sh, not bash)"

# —— 「绕沙箱」检测 ——
# `python -c "..."` / `python - <<EOF` / `echo ... | python -` 这类内联执行
# 完全绕过 L3 沙箱（无内存/超时/进程树/环境剥离），而 run_python 能做得更好，
# 所以直接拦截并把模型引导回 run_python。
_PY = r"(?:py|python\d?(?:\.\d+)?)(?:\.exe)?"
_INLINE_PY_RE = re.compile(rf"\b{_PY}\s+(?:-c\b|-\s*(?:<|\||$))", re.I)
# `python script.py` / `py -u tmp_x.py`：可能是临时脚本，附提示但不拦截
# （项目自身 CLI、脚本、pytest 入口等合法用法仍然放行）
_SCRIPT_PY_RE = re.compile(rf"\b{_PY}\s+(?:-[\w-]+\s+)*[\w./\\-]+\.pyw?\b", re.I)


def _inline_python_blocked(command: str) -> bool:
    return bool(_INLINE_PY_RE.search(command))


def _looks_like_script_python(command: str) -> bool:
    return bool(_SCRIPT_PY_RE.search(command))


def _kill_tree(proc: subprocess.Popen) -> None:
    """杀掉子进程及其全部后代

    subprocess.run(timeout=) 只杀直接子进程（shell），孙进程会继续跑：
    `python long_task.py` 超时后仍在写仓库，用户看到「已超时」但文件还在变。
    POSIX 用进程组 killpg，Windows 用 taskkill /T。
    """
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError, PermissionError):
            pass
    else:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


@tool(
    name="run_command",
    description=L(
        f"执行 shell 命令并返回输出（需要 --trust）。"
        f"当前平台由 {_SHELL_NAME} 执行，请使用该 shell 的语法"
        f"（Windows 用 dir/del，Linux/macOS 用 ls/rm）。"
        f"用途是项目自身的命令（构建/测试/git/CLI）和 shell 操作；"
        f"执行 Python 代码请用 run_python，不要为了跑临时代码而先写 .py 文件再在这里执行，"
        f"也不要使用 python -c / 管道喂 stdin（会被拦截——那条路径绕过沙箱限额）。",
        f"Run a shell command and return its output (requires --trust). "
        f"Commands run under {_SHELL_NAME} on this platform, so use its syntax "
        f"(dir/del on Windows, ls/rm on Linux/macOS). "
        f"Use it for the project's own commands (build/test/git/CLI) and shell operations; "
        f"to execute Python code use run_python instead — do not write a .py file just to run it here, "
        f"and avoid python -c / piping code into stdin (blocked: that path bypasses the sandbox limits).",
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

    # 内联 Python：把模型引导回沙箱，而不是在这里放行
    if _inline_python_blocked(command):
        return t(
            "Error: 内联 Python（python -c / 管道喂 stdin）已被拦截——"
            "这条路径绕过沙箱的内存/超时/进程树/环境隔离。\n"
            "请改用 run_python(code=\"...\")；需要用相对路径读写工作区时加 "
            "workdir=\"workspace\"（限额隔离照旧生效）。",
            "Error: inline Python (python -c / piping code to stdin) is blocked — "
            "that path bypasses the sandbox's memory/timeout/process-tree/env isolation.\n"
            "Use run_python(code=\"...\") instead, plus workdir=\"workspace\" when relative "
            "paths into the workspace are needed (limits still apply).",
        )

    # Windows 下 shell=True 走 cmd.exe，默认 codepage 936 (GBK)
    # 先 chcp 65001 切到 UTF-8 codepage，让命令输出 UTF-8 字节
    # 这样我们用 encoding="utf-8" 解码才正确（否则中文乱码）
    if sys.platform == "win32":
        command = f"chcp 65001 >nul 2>&1 && {command}"
    # POSIX 下 shell=True 走 /bin/sh，输出本就是 UTF-8，无需前缀

    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=ws,
            # 不把 tty 的 stdin 继承给子进程：Linux 下 cat/read/ssh 之类会
            # 抢走终端按键，表现为输入被吃掉、提示符被抹掉
            stdin=subprocess.DEVNULL,
            # stderr 并入 stdout：保留两者真实交错顺序（分开收集会打乱报错位置）
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            # 收字节后交给 decode_output()：固定 encoding="utf-8" 会把
            # 非 UTF-8 输出（Git for Windows 的部分本地化文案、老工具）全打成 �
            # 独立会话/进程组，超时时能整棵树回收
            start_new_session=(os.name == "posix"),
            # chcp 只改控制台代码页，Python 子进程在管道下仍按 ANSI 代码页
            # （cp936）吐字节，这条 env 把 python 栈的输出钉到 UTF-8
            env=child_utf8_env(),
        )
    except Exception as e:
        return f"Error: failed to start command: {type(e).__name__}: {e}"

    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            out, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out = b""

    output = decode_output(out).strip()
    rc = proc.returncode

    if timed_out:
        note = t(f"Error: command timed out after {timeout}s (process tree killed)",
                 f"Error: command timed out after {timeout}s (process tree killed)")
        return f"{note}\n{output}" if output else note

    # 退出码必须回传：否则 `false` / 报错脚本只得到 "(no output)"，
    # LLM 会把失败当成功，继续往下推理
    if rc:
        head = f"[exit={rc}]"
    else:
        head = ""

    if not output:
        # 非 0 退出码本身已经是信息，不必再补 "(no output)"
        body = "" if rc else "(no output)"
    elif len(output) > 5000:
        body = output[:5000] + f"\n... (truncated, {len(output)} total chars)"
    else:
        body = output

    result = f"{head}\n{body}" if head else body

    # 跑了 .py 脚本：附一行提示，把「写临时文件再 run_command」的习惯
    # 掰回沙箱（不拦截，项目自身 CLI/pytest 等合法用法照旧）
    if _looks_like_script_python(command):
        result += "\n" + t(
            "[hint] 如果这是为了验证临时代码而写的 .py，下次直接用 "
            "run_python(code=..., workdir=\"workspace\")：沙箱会给它内存/超时/进程树限额，"
            "而且不用在仓库里留文件。",
            "[hint] If you wrote that .py just to verify temporary code, use "
            "run_python(code=..., workdir=\"workspace\") next time: the sandbox enforces "
            "memory/timeout/process-tree limits and leaves no files in the repo.",
        )
    return result
