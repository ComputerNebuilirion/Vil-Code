"""跨平台可编辑输入行。

为什么不用 rich 的 ``console.input()`` —— 每一条都已在 WSL(Ubuntu 26.04) 的
真 PTY 里复现过：

1. rich 只是把提示符 print 到 stdout，再调用内置 ``input()``，提示符和行编辑器
   互不知情，提示符对行编辑器来说"宽度为 0"；
2. Linux/WSL 下内置 ``input()`` **只有在 import readline 之后**才有行编辑能力。
   没导入时终端处于 cooked mode，方向键以 ESC 序列字面量进入缓冲区：
   屏幕上显示 ``^[[D``，回车后字符串里真的带 ``\\x1b[D``；
3. 即使 readline 生效，因为提示符为空，任何重绘都是 ``\\r`` 回到第 0 列再重画，
   会把 rich 刚打印的提示符覆盖掉 —— 表现为"提示符被删掉/冒出上次的输入内容"；
4. cooked mode 下退格按**字节**删。删掉半个汉字后 ``input()`` 用 surrogateescape
   解码，得到孤立代理字符（``'中\\udce6\\udc96'``），后续
   ``json.dumps(ensure_ascii=False)`` 落盘/发包时报
   ``UnicodeEncodeError: surrogates not allowed``。
   若 locale 是 ``C``，readline 还会把 UTF-8 尾字节当 meta 键，吐出 ``(arg: 8)``
   之类的垃圾。

所以这里优先用 prompt_toolkit 接管整行编辑（自带按键解析、CJK 宽字符宽度、
光标重绘、历史文件），退化时用 readline + ``\\001..\\002`` 包裹提示符，
最后退化回 ``console.input``（Windows 老行为）。
"""
import re
import sys
from pathlib import Path

from .i18n import t

_HISTORY_FILE = Path.home() / ".vil" / "history"
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")

# 模块级单例：PromptSession 内部维护历史/补全状态，跨轮复用才有 ↑ 历史
_session = None
_session_ready = False


def _get_session():
    """懒加载 PromptSession；prompt_toolkit 未安装时返回 None。"""
    global _session, _session_ready
    if _session_ready:
        return _session
    _session_ready = True
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory
    except Exception:
        return None
    try:
        _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _session = PromptSession(history=FileHistory(str(_HISTORY_FILE)))
    except Exception:
        _session = None
    return _session


def _ansi_prompt(prompt: str, console, markup: bool) -> str:
    """把提示符渲染成带颜色的 ANSI 字符串，交给行编辑器。"""
    with console.capture() as cap:
        console.print(prompt, end="", highlight=False, markup=markup)
    return cap.get()


def _readline_prompt(ansi: str) -> str:
    """readline 要求非打印序列用 \\001..\\002 包裹，否则列宽算错、重绘错位。"""
    return _ANSI_RE.sub(lambda m: "\001" + m.group(0) + "\002", ansi)


def sanitize(line: str, console=None) -> str:
    """兜底：丢掉孤立代理字符（半个 UTF-8 汉字），避免后续编码崩溃。"""
    if not line:
        return line
    cleaned = _SURROGATE_RE.sub("", line)
    if cleaned != line and console is not None:
        console.print(t(
            "[yellow]⚠ 输入里有被截断的多字节字符，已丢弃（退格删到了半个汉字）[/yellow]",
            "[yellow]⚠ input had a truncated multibyte char; it was dropped[/yellow]"))
    return cleaned


def read_line(prompt: str, console, *, markup: bool = True) -> str:
    """读一行用户输入，返回 sanitize 后的 str。

    ``EOFError`` / ``KeyboardInterrupt`` 原样抛给调用方，语义与内置 ``input()`` 一致。
    优先级：prompt_toolkit > readline > ``console.input``。
    """
    # 非交互（管道/重定向）时不接管终端
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return sanitize(console.input(prompt, markup=markup), console)

    ansi = _ansi_prompt(prompt, console, markup)

    session = _get_session()
    if session is not None:
        from prompt_toolkit.formatted_text import ANSI
        return sanitize(session.prompt(ANSI(ansi)), console)

    # 退路 1：readline（Windows 上 import 失败，自动落到 console.input）
    try:
        import readline
    except ImportError:
        return sanitize(console.input(prompt, markup=markup), console)
    for cmd in ("set input-meta on", "set convert-meta off",
                "set output-meta on", "set enable-bracketed-paste on"):
        try:
            readline.parse_and_bind(cmd)
        except Exception:
            pass
    return sanitize(input(_readline_prompt(ansi)), console)
