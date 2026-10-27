"""vil-agent-frontend: VilAgent 的多轮 REPL 前端

交互模式：一个 session 内多轮 user 输入，agent 跨轮利用上下文。
每轮用 ask/do/review 前缀切换模式（不带前缀用默认 mode）。

用法:
    python vil-agent-frontend.py
    python vil-agent-frontend.py --continue last
    python vil-agent-frontend.py --mode do

REPL 命令:
    :ask <task>           只读分析/规划
    :do <task>            执行（允许写操作）
    :review <task>        代码审查
    /help                 显示此帮助
    /exit  /quit          退出
    /sessions             列出所有会话
    /switch <id>          切换到指定会话
    /history              显示当前会话最近消息
    /export [sid] [-o f]  导出会话为 Markdown（默认当前会话）
    /clear                清空当前会话历史
    /mode `ask/do/review` 查看/切换默认 mode
    /tokens               显示当前会话累计 token
    /compress [all]       调 LLM 压缩当前会话旧历史（all=强制重生成，默认增量）
    /config [get|set|list|path] 配置管理（在 REPL 内修改 config.json）
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

from VilAgent import SessionManager, State
from VilAgent.session import truncate_display as _trunc
from VilAgent.loop import retitle_sessions
from VilAgent.agent_app import (
    AgentModel, TerminalView,
    SESSIONS_DIR, console as _console,
)
from rich.box import ROUNDED
from rich.console import Group
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text
from VilAgent.config import (
    CONFIG_FILE, load_config, set_value, get_value,
)
from VilAgent.i18n import t, set_lang, get_lang
from VilAgent.input_line import read_line
from VilAgent.safety import (normalize_level, level_label,
                             LEVEL_STRICT, LEVEL_BALANCED, LEVEL_YOLO)


# —— 常驻事件循环 ——
# 前端要跨很多轮调用协程（每轮 agent.run / title / compress 等）。
# 若每处都用 asyncio.run()，每次都会新建并关闭一个 loop，而 httpx.AsyncClient
# 的 transport 绑定在首次使用的 loop 上 —— 后续 loop 里复用会抛
# "Event loop is closed"，且退出时 model.close() 也在新 loop 里关旧 loop 的连接，
# 必然失败。这里整个进程共用一个 loop，彻底绕开该问题。
_LOOP: asyncio.AbstractEventLoop | None = None


def _get_loop() -> asyncio.AbstractEventLoop:
    """惰性创建进程级常驻事件循环。"""
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP


def _run(coro):
    """在常驻 loop 上执行协程，语义对齐 asyncio.run()（但不关闭 loop）。

    Ctrl+C 时取消任务并让取消清理流程跑完，再向上抛 KeyboardInterrupt，
    避免中断后任务残留在 loop 上。
    """
    loop = _get_loop()
    task = loop.create_task(coro)
    try:
        return loop.run_until_complete(task)
    except KeyboardInterrupt:
        task.cancel()
        try:
            loop.run_until_complete(task)
        except BaseException:
            pass
        raise


def _close_loop() -> None:
    """退出时收尾：跑完 asyncgen 清理再关闭常驻 loop。"""
    global _LOOP
    if _LOOP is not None and not _LOOP.is_closed():
        try:
            _LOOP.run_until_complete(_LOOP.shutdown_asyncgens())
        except Exception:
            pass
        _LOOP.close()
    _LOOP = None


# 写类模式：trust=True
_WRITE_MODES = {"do"}

# 输入行：跨平台可编辑（prompt_toolkit > readline > console.input）。
# 不用 console.input() 的四个原因见 VilAgent/input_line.py 的模块 docstring。
def _read_input(prompt_markup: str) -> str:
    """读一行用户输入；EOFError / KeyboardInterrupt 原样抛给调用方。"""
    return read_line(prompt_markup, _console)


def _help_text() -> str:
    """按当前语言返回 REPL 帮助文本。"""
    return _EN_HELP if get_lang() == "en" else _CN_HELP


_CN_HELP = """\
REPL 命令:
  :ask <task>            只读分析/规划
  :do <task>             执行（允许写操作）
  :review <task>         代码审查
  <不带前缀>              用默认 mode 跑
会话命令:
  /new                   开新 session（保留旧的，可 /switch 回看）
  /sessions              列出所有会话
  /switch <id>           切换到指定会话
  /history               显示当前会话最近消息
  /export \\[sid] [-o f]   导出会话为 Markdown（默认当前会话）
  /clear                 清空当前会话历史（需确认）
  /delete <id>           永久删除会话（需确认，默认当前）
  /deleteall             永久删除所有会话（需确认）
  /mode \\[ask|do|review]  查看/切换默认 mode
  /perm \\[strict|balanced|yolo]
                         查看/切换权限层级（strict=所有非只读都要确认，
                         balanced=只有命令/删除类需确认；
                         两者都直接放行只读命令 ls/cat/git status 等，
                         yolo=不做执行前检查，所有调用直接执行）
  /tokens                显示当前会话累计 token
  /title \\[force]         用 LLM 生成/重生成当前会话标题（/sessions 里显示）
  /title all [--dry-run]  批量重生成所有会话标题（老标题一次性迁移；dry-run 只预览）
  /compress \\[all]        调 LLM 压缩当前会话旧历史（all=强制重生成，
                         默认只压缩新增部分，保留最近 10 条原文）
配置命令:
  /config                列出所有配置（隐藏 api_key）
  /config get <key>      读取配置（dotted 路径，如 llm.endpoint）
  /config set <key> <v>  设置配置（自动 JSON parse，true/false/数字/null）
  /config path           显示 config.json 路径
  /help                  本帮助
  /exit                  退出
当 agent 检测到当前输入跟前面任务明显不相关时，会自动提示 /new 切换。
"""

_EN_HELP = """\
REPL commands:
  :ask <task>            read-only analysis / planning
  :do <task>             execute (write ops allowed)
  :review <task>         code review
  <no prefix>            run with the default mode
Session commands:
  /new                   start a new session (keeps the old one; /switch to revisit)
  /sessions              list all sessions
  /switch <id>           switch to the given session
  /history               show recent messages of the current session
  /export \\[sid] [-o f]   export session to Markdown (current by default)
  /clear                 clear current session history (confirmation required)
  /delete <id>           permanently delete a session (confirm; current by default)
  /deleteall             permanently delete all sessions (confirmation required)
  /mode \\[ask|do|review]  view / switch the default mode
  /perm \\[strict|balanced|yolo]
                         view / switch the permission level (strict = confirm every
                         non-readonly action, balanced = only command/delete actions,
                         yolo = no pre-execution checks, every call runs directly)
  /tokens                show cumulative tokens of the current session
  /title \\[force]         generate/regenerate a title for the current session
                         (shown by /sessions)
  /title all [--dry-run] regenerate titles for all sessions (one-off migration;
                         --dry-run only previews)
  /compress \\[all]         LLM-compress old history of the current session (all=force
                         regenerate; default compresses only new parts, keeps last 10)
Config commands:
  /config                list all config (api_key hidden)
  /config get <key>      read a config value (dotted path, e.g. llm.endpoint)
  /config set <key> <v>  set a config value (auto JSON parse: true/false/number/null)
  /config path           show the config.json path
  /help                  this help
  /exit                  quit
When the agent detects the current input is clearly unrelated to the previous task,
it will prompt you to /new switch.
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vil-agent-frontend",
        description=t("Vil Agent HCI Frontend - Vil Agent 交互式前端（多轮 REPL）",
                      "Vil Agent HCI Frontend - Vil Agent interactive frontend (multi-turn REPL)"),
    )
    p.add_argument("--continue", dest="continue_session", metavar="SESSION",
                   help=t("续接指定会话 ID（'last' 表示最近一次）",
                          "Resume the given session ID ('last' = the most recent one)"))
    p.add_argument("--new", action="store_true",
                   help=t("强制新建 session（默认行为是续接上次会话）",
                          "Force a new session (default is to resume the last one)"))
    p.add_argument("--mode", default="ask", choices=["ask", "do", "review"],
                   help=t("默认模式（用户输入不带 mode 前缀时用）",
                          "Default mode (used when input has no mode prefix)"))
    p.add_argument("--max-steps", type=int, default=None,
                   help=t("最大步数（默认 50）", "Max steps (default 50)"))
    p.add_argument("--no-stream", action="store_true",
                   help=t("关闭流式输出", "Disable streaming output"))
    p.add_argument("--permission", dest="permission", default=None,
                   choices=["strict", "balanced", "yolo", "auto"],
                   help=t(
                       "权限层级：strict=所有非只读操作都要确认；"
                       "balanced=只有命令/删除类操作需确认（默认）；"
                       "两者都直接放行只读命令（ls/cat/git status 等，含只读管道）；"
                       "yolo=不做执行前检查，所有调用直接执行（auto 为旧别名）",
                       "permission level: strict=confirm every non-readonly action; "
                       "balanced=only command/delete actions need confirmation (default); "
                       "both auto-pass read-only commands (ls/cat/git status, "
                       "including read-only pipes); "
                       "yolo=no pre-execution checks, everything runs "
                       "(auto is a legacy alias)"))
    return p


# —— REPL 命令处理 ——

# 标签样式映射：key 为标签名，value 为 Rich style
_TAG_STYLES = {
    "session": "bold green",
    "error": "bold red",
    "history": "bold yellow",
    "config": "bold cyan",
    "interrupt": "bold magenta",
}

# 单个反斜杠：Rich markup 里用来转义字面 [，避免被当成标签
_BS = "\\"


def _tag(tag: str) -> str:
    """生成带 Rich markup 的标签字符串，如 _tag('session') → '[bold green]\\[session][/bold green]'"""
    style = _TAG_STYLES.get(tag, "bold")
    return f"[{style}]{_BS}[{tag}][/{style}]"

def _cmd_sessions(sm: SessionManager) -> None:
    items = sm.list_all()
    if not items:
        _console.print(t(f"{_tag('history')} (空)", f"{_tag('history')} (empty)"))
        return
    # 历史遗留会话没有 title → 本地兜底补一次（不调 LLM，只读 jsonl），
    # 让每行都有可辨识的标题，而不是「(交互式会话)」
    sm.ensure_titles()
    items = sm.list_all()
    # 标题列按控制台剩余宽度动态截断（前面固定 59 列），避免整行折行
    budget = max(16, getattr(_console, "width", 80) - 60)
    _console.print(f"{'ID':<14} {'MODE':<8} {'STEPS':<6} {'TOKENS':<8} "
                   f"{'UPDATED':<17} {'TITLE'}")
    for it in items[:20]:
        title = (it.get("title") or "").strip()
        if not title:
            title = (it.get("task") or "").strip()
        title = _trunc(title, budget)
        ts = it.get("last_active_at", 0)
        updated = (time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
                   if ts else "?")
        mark = "*" if it["id"] == sm.get_current() else " "
        _console.print(f"{it['id']:<14} {it.get('mode','?'):<8} "
                       f"{it.get('steps_total',0):<6} "
                       f"{it.get('tokens_total',0):<8} "
                       f"{updated:<17} {mark}{title}")
    _console.print(t(f"{_tag('history')} 共 {len(items)} 个会话，显示最近 "
                     f"{min(20, len(items))} 个（* = 当前会话）",
                     f"{_tag('history')} {len(items)} sessions, showing latest "
                     f"{min(20, len(items))} (* = current)"))


def _cmd_title_all(model: AgentModel, sm: SessionManager,
                   dry_run: bool = False) -> None:
    """批量重生成所有会话的标题（老标题一次性迁移到新的「完整词组」规则）。

    分批逐个调 LLM（每个会话 1 次），边算边打印「旧 → 新」；
    单个会话失败只跳过它，不影响其余会话。
    """
    items = sm.list_all()
    if not items:
        _console.print(t(f"{_tag('history')} (空)", f"{_tag('history')} (empty)"))
        return
    total = len(items)
    done = [0]

    def _on_result(row: dict) -> None:
        done[0] += 1
        head = f"  [{done[0]}/{total}] {row['id'][:8]}"
        if row["error"]:
            _console.print(t(f"{head} 失败: {row['error']}",
                             f"{head} failed: {row['error']}"))
        elif not row["new"]:
            _console.print(t(f"{head} 跳过（无可读对话）",
                             f"{head} skipped (no readable messages)"))
        else:
            style = "green" if row["changed"] else "dim"
            _console.print(
                f"{head} [dim]{escape(row['old'] or '-')}[/dim] → "
                f"[{style}]{escape(row['new'])}[/{style}]")

    model.ensure_loop()
    _console.print(t(f"{_tag('session')} 正在用 LLM 重生成 {total} 个会话的标题…",
                     f"{_tag('session')} regenerating titles for {total} sessions "
                     f"with the LLM..."))
    try:
        rows = _run(retitle_sessions(model.llm, sm, dry_run=dry_run,
                                     on_result=_on_result))
    except KeyboardInterrupt:
        _console.print(t(f"{_tag('interrupt')} 已取消（已处理的会话已落盘）",
                         f"{_tag('interrupt')} cancelled (finished sessions are saved)"))
        return
    changed = sum(1 for r in rows if r["changed"])
    skipped = sum(1 for r in rows if not r["new"])
    failed = sum(1 for r in rows if r["error"])
    tail = t("（dry-run，未写盘）", " (dry-run, nothing written)") if dry_run else ""
    _console.print(t(
        f"{_tag('session')} 共 {len(rows)} 个会话：{changed} 个更新，"
        f"{skipped} 个跳过，{failed} 个失败{tail}",
        f"{_tag('session')} {len(rows)} sessions: {changed} updated, "
        f"{skipped} skipped, {failed} failed{tail}"))


def _cmd_history(state: State, n: int = 20) -> None:
    msgs = state.load()
    if not msgs:
        _console.print(t(f"{_tag('history')} (空)", f"{_tag('history')} (empty)"))
        return
    # 角色样式表：前缀符号 + 颜色 + 标签（无 emoji）
    role_styles = {
        "user":      ("> ",  "bold cyan",    "USER"),
        "assistant": ("< ",  "bold magenta", "ASSISTANT"),
        "tool":      ("# ",  "dim yellow",   "TOOL"),
        "system":    ("* ",  "dim",          "SYSTEM"),
    }
    for m in msgs[-n:]:
        role = m.get("role", "?")
        content = m.get("content", "")
        # 处理 content：可能是 str / list（多模态）/ 含 tool_calls
        tool_calls = m.get("tool_calls")
        if tool_calls:
            # assistant 带 tool_calls：列出调用的工具
            calls = ", ".join(
                tc.get("function", {}).get("name", "?") for tc in tool_calls
            ) if isinstance(tool_calls, list) else str(tool_calls)
            icon, style, label = role_styles.get("assistant", ("", "", "ASSISTANT"))
            _console.print(f"[{style}]{icon}{label} → call: {calls}[/]")
            continue
        if isinstance(content, str):
            preview = content
        elif isinstance(content, list):
            # 拼接 list（多模态/分段）
            parts = []
            for seg in content:
                if isinstance(seg, dict):
                    if seg.get("type") == "text":
                        parts.append(seg.get("text", ""))
                    else:
                        parts.append(f"<{seg.get('type', '?')}>")
                else:
                    parts.append(str(seg))
            preview = "\n".join(p for p in parts if p)
        else:
            preview = str(content)
        # 截断过长内容
        if len(preview) > 200:
            preview = preview[:200] + "..."
        preview = escape(preview)
        icon, style, label = role_styles.get(role, ("", "", role.upper()))
        # 多行内容用缩进对齐
        if "\n" in preview:
            indented = preview.replace("\n", "\n    ")
            _console.print(f"[{style}]{icon}{label}[/]\n    [dim]{indented}[/]")
        else:
            _console.print(f"[{style}]{icon}{label}[/] [dim]{preview}[/]")
    _console.print(t(f"{_tag('history')} 共 {len(msgs)} 条，显示最近 "
                     f"{min(n, len(msgs))} 条",
                     f"{_tag('history')} {len(msgs)} messages, showing latest "
                     f"{min(n, len(msgs))}"))


def _cmd_tokens(sm: SessionManager, sid: str) -> None:
    meta = sm.get(sid) or {}
    _console.print(f"{_tag('session')} {sm.label_of(sid)} "
                   f"tokens={meta.get('tokens_total', 0)} "
                   f"steps={meta.get('steps_total', 0)} "
                   f"status={meta.get('status', '?')}")


def _cmd_export(line: str, sm: SessionManager, current_sid: str) -> None:
    """导出会话为 Markdown：/export [sid] [-o out.md]"""
    from pathlib import Path

    parts = line.split()
    target_sid = current_sid
    out_path = None

    # 解析：/export [sid] [-o path]
    i = 1
    while i < len(parts):
        if parts[i] == "-o" and i + 1 < len(parts):
            out_path = parts[i + 1]
            i += 2
        else:
            target_sid = parts[i]
            i += 1

    # 支持前缀匹配
    if not sm.get(target_sid):
        matches = [s["id"] for s in sm.list_all()
                   if s["id"].startswith(target_sid)]
        if len(matches) == 1:
            target_sid = matches[0]
        elif not matches:
            _console.print(t(f"{_tag('error')} 找不到会话 {target_sid}", f"{_tag('error')} session not found: {target_sid}"))
            return
        else:
            _console.print(
                t(f"{_tag('error')} 匹配多个会话: "
                  f"{' '.join(m[:6] for m in matches)}，请输更长的前缀",
                  f"{_tag('error')} matches multiple sessions: "
                  f"{' '.join(m[:6] for m in matches)}, please type a longer prefix")
            )
            return

    state = State(sm.path_for(target_sid))
    msgs = state.load()
    if not msgs:
        _console.print(t(f"{_tag('error')} 会话 {target_sid[:6]} 无消息可导出",
                         f"{_tag('error')} session {target_sid[:6]} has no messages to export"))
        return

    md = sm.export_markdown(target_sid, msgs)
    if out_path is None:
        out_path = f"session_{target_sid}.md"
    Path(out_path).write_text(md, encoding="utf-8")
    _console.print(t(f"{_tag('session')} 已导出 {target_sid[:6]} → {out_path}（{len(msgs)} 条消息）",
                     f"{_tag('session')} exported {target_sid[:6]} → {out_path} ({len(msgs)} messages)"))


def _cmd_config(line: str) -> None:
    """REPL 内 config 命令：/config [get <key> | set <key> <v> | path | list]"""
    import json
    parts = line.split(maxsplit=3)
    sub = parts[1] if len(parts) > 1 else "list"

    if sub == "path":
        _console.print(f"{_tag('config')} {CONFIG_FILE}")
        return

    if sub == "get":
        if len(parts) < 3:
            _console.print(t(f"{_tag('config')} 用法: /config get <key>（如 llm.endpoint）",
                             f"{_tag('config')} usage: /config get <key> (e.g. llm.endpoint)"))
            return
        key = parts[2]
        val = get_value(key)
        if val is None and key != "llm.api_key":
            _console.print(t(f"{_tag('config')} {key} 未设置",
                             f"{_tag('config')} {key} is not set"))
        elif key == "llm.api_key":
            _console.print(t(f"{_tag('config')} {key} = (hidden, 长度 {len(str(val or ''))})",
                             f"{_tag('config')} {key} = (hidden, length {len(str(val or ''))})"))
        else:
            _console.print(f"{_tag('config')} {key} = {val!r}")
        return

    if sub == "set":
        if len(parts) < 4:
            _console.print(t(f"{_tag('config')} 用法: /config set <key> <value>",
                             f"{_tag('config')} usage: /config set <key> <value>"))
            return
        key, raw = parts[2], parts[3]
        # 自动 JSON parse：true/false/数字/null/对象/数组
        try:
            val = json.loads(raw)
        except json.JSONDecodeError:
            val = raw  # 当字符串
        set_value(key, val)
        _console.print(t(f"{_tag('config')} 已设置 {key} = {val!r}",
                         f"{_tag('config')} set {key} = {val!r}"))
        # 提示哪些配置需要重启生效
        if key.startswith("llm."):
            _console.print(t("[dim]⚠ llm.* 配置修改后需重启 frontend 才生效[/dim]",
                             "[dim]⚠ llm.* changes take effect only after restarting the frontend[/dim]"))
        else:
            _console.print(t(f"[dim]✓ {key} 下次轮询自动生效[/dim]",
                             f"[dim]✓ {key} takes effect on the next poll[/dim]"))
        return

    if sub == "list":
        cfg = load_config()
        # 隐藏 api_key
        if isinstance(cfg.get("llm"), dict) and "api_key" in cfg["llm"]:
            cfg = json.loads(json.dumps(cfg))  # deep copy
            ak = cfg["llm"].get("api_key") or ""
            cfg["llm"]["api_key"] = t(f"(hidden, 长度 {len(ak)})", f"(hidden, length {len(ak)})")
        _console.print(t(f"{_tag('config')} 当前配置:", f"{_tag('config')} current config:"))
        for k, v in cfg.items():
            if isinstance(v, dict):
                _console.print(f"  [bold]{k}[/bold]:")
                for sk, sv in v.items():
                    _console.print(f"    [dim]{sk}[/dim]: {sv!r}")
            else:
                _console.print(f"  [bold]{k}[/bold]: {v!r}")
        _console.print(t(f"[dim]路径: {CONFIG_FILE}[/dim]", f"[dim]Path: {CONFIG_FILE}[/dim]"))
        return

    _console.print(t(f"{_tag('config')} 子命令: get / set / list / path",
                     f"{_tag('config')} subcommand: get / set / list / path"))


def _confirm_dangerous(action_desc: str) -> bool:
    """危险操作二次确认（Minecraft 删除世界风格）"""
    _console.print(
        t(f"[bold red]⚠ 你确定要永久{action_desc}吗？此操作无法撤销！（真的很久！）[/bold red]",
          f"[bold red]⚠ Are you sure you want to permanently {action_desc}? "
          f"This cannot be undone! (a really long time!)[/bold red]")
    )
    try:
        ans = _read_input(t("  确认？[y/N] > ", "  Confirm? [y/N] > ")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("y", "yes", "是")


# —— 核心 REPL 循环 ——

def repl_loop(model: AgentModel, view: TerminalView,
               sm: SessionManager, sid: str, state: State,
               args: argparse.Namespace) -> None:
    default_mode = args.mode
    cfg = model.cfg
    # 权限层级：--permission 优先，否则读配置（/perm 可在运行时切换，仅本次运行）
    perm_level = normalize_level(
        getattr(args, "permission", None) or cfg.get("permission_level"))

    while True:
        # 提示符：[sid] (perm) (mode) >
        # 用 Rich markup（Rich 内部处理 Windows ANSI）
        # 只放短 session ID：稳定、窄、不随标题长度抖动
        # perm 放在 mode 左边：mode 决定「能不能写」，perm 决定「要不要问」，
        # 两者都会被 /mode、/perm 在运行时改，重画提示符即可自动跟上
        mode_style = {"ask": "bold green", "do": "bold red",
                      "review": "bold blue"}.get(default_mode, "bold green")
        # 红黄绿对应严格程度：strict 最紧（绿）、yolo 最松（红）
        perm_style = {LEVEL_STRICT: "bold green", LEVEL_BALANCED: "bold yellow",
                      LEVEL_YOLO: "bold red"}.get(perm_level, "bold yellow")
        prompt = (
            f"[bold cyan] Vil [/bold cyan]"
            f"[bold yellow]\\[{sid[:6]}][/bold yellow] "
            f"[{perm_style}]({perm_level})[/{perm_style}] "
            f"[{mode_style}]({default_mode})[/{mode_style}] "
            f"[bold bright_black]>[/bold bright_black] "
        )
        try:
            line = _read_input(prompt)
        except (EOFError, KeyboardInterrupt):
            _console.print()
            _console.print(t(f"{_tag('interrupt')} 已返回提示符",
                             f"{_tag('interrupt')} back to prompt"))
            continue

        line = line.strip()
        if not line:
            continue

        # —— 命令分支 ——
        if line in ("/exit", "/quit"):
            break
        if line in ("/help", "/h", "?"):
            _console.print(_help_text())
            continue
        if line == "/config" or line.startswith("/config "):
            _cmd_config(line)
            continue
        if line == "/sessions":
            _cmd_sessions(sm)
            continue
        if line == "/history":
            _cmd_history(state)
            continue
        if line.startswith("/export"):
            _cmd_export(line, sm, sid)
            continue
        if line == "/tokens":
            _cmd_tokens(sm, sid)
            continue
        if line == "/title" or line.startswith("/title "):
            # 手动生成标题：/title 缺标题时生成，/title force 强制重生成
            # /title all 批量重生成所有会话（老标题一次性迁移），--dry-run 只预览
            parts = line.split()[1:]
            flags = {p for p in parts if p.startswith("-") or p == "all"}
            batch = bool(flags & {"all", "--all", "-a"})
            if batch:
                try:
                    _cmd_title_all(model, sm,
                                   dry_run=bool(flags & {"--dry-run", "-n"}))
                except Exception as e:
                    _console.print(t(f"{_tag('error')} 批量生成标题失败: {e}",
                                     f"{_tag('error')} batch title generation failed: {e}"))
                continue
            force_title = bool(parts)
            agent = model.build_agent(
                mode="ask", trust=False, stream=False, max_steps=1,
                permission_handler=None, state=state,
                session_id=sid, session_manager=sm,
            )
            _console.print(t(f"{_tag('session')} 正在用 LLM 生成会话标题…",
                             f"{_tag('session')} generating session title with the LLM..."))
            try:
                model.ensure_loop()
                new_title = _run(agent.generate_title(force=force_title))
            except KeyboardInterrupt:
                _console.print(t(f"{_tag('interrupt')} 已取消",
                                 f"{_tag('interrupt')} cancelled"))
                continue
            except Exception as e:
                _console.print(t(f"{_tag('error')} 生成标题失败: {e}",
                                 f"{_tag('error')} title generation failed: {e}"))
                continue
            if new_title:
                _console.print(t(f"{_tag('session')} 标题已更新: {new_title}",
                                 f"{_tag('session')} title updated: {new_title}"))
            else:
                _console.print(
                    t(f"{_tag('session')} 已有标题（用 /title force 重新生成）",
                      f"{_tag('session')} title already set (use /title force to regenerate)"))
            continue
        if line == "/compress" or line.startswith("/compress "):
            # 手动摘要压缩：调 LLM 把当前会话旧历史压成摘要缓存。
            # 与自动 _maybe_summarize 共用 AgentLoop.summarize_history；
            # 即使 context_budget 未开也能生效（state 滑窗路径读摘要缓存）。
            parts = line.split()
            force_all = len(parts) > 1 and parts[1] in ("all", "--all", "-a")
            agent = model.build_agent(
                mode="ask", trust=False, stream=False, max_steps=1,
                permission_handler=None, state=state,
                session_id=sid, session_manager=sm,
            )
            _console.print(t(f"{_tag('session')} 正在用 LLM 压缩当前会话历史…",
                             f"{_tag('session')} compressing current session history with the LLM..."))
            try:
                model.ensure_loop()
                res = _run(agent.summarize_history(force=force_all))
            except KeyboardInterrupt:
                _console.print(t(f"{_tag('interrupt')} 已取消压缩",
                                 f"{_tag('interrupt')} compression cancelled"))
                continue
            except Exception as e:
                _console.print(t(f"{_tag('error')} 压缩失败: {e}",
                                 f"{_tag('error')} compression failed: {e}"))
                continue
            if res is None:
                _console.print(
                    t(f"{_tag('session')} 没有足够的新增历史可压缩"
                      f"（若想重生成摘要，请用 /compress all）",
                      f"{_tag('session')} not enough new history to compress"
                      f" (to regenerate the summary, use /compress all)")
                )
            else:
                summary, covered = res
                preview = " ".join(summary.split())
                if len(preview) > 160:
                    preview = preview[:160] + "…"
                _console.print(
                    t(f"{_tag('session')} 已压缩 {covered} 条旧消息为摘要"
                      f"（保留最近 10 条原文），后续轮次将携带该摘要上下文",
                      f"{_tag('session')} compressed {covered} old messages into a summary"
                      f" (kept the latest 10 raw), later turns will carry this summary")
                )
                _console.print(t(f"[dim]  摘要预览: {preview}[/dim]",
                                 f"[dim]  summary preview: {preview}[/dim]"))
            continue
        if line == "/clear":
            meta = sm.get(sid) or {}
            label = sm.label_of(sid, short=6)
            if not _confirm_dangerous(
                    t(f"清空会话 {label} 的历史（{meta.get('steps_total', 0)} 步、"
                      f"{meta.get('tokens_total', 0)} tokens 将丢失）",
                      f"clear the history of session {label} "
                      f"({meta.get('steps_total', 0)} steps, "
                      f"{meta.get('tokens_total', 0)} tokens will be lost)")):
                _console.print(t("[dim]已取消[/dim]", "[dim]cancelled[/dim]"))
                continue
            state.clear()
            sm.update(sid, tokens_total=0, steps_total=0,
                      summary="", status="active")
            _console.print(t(f"{_tag('session')} 已清空 {label}",
                             f"{_tag('session')} cleared {label}"))
            continue
        if line == "/deleteall" or line.startswith("/deleteall "):
            # 删除所有会话（mc 风格二次确认）
            all_sessions = sm.list_all()
            total_steps = sum(s.get("steps_total", 0) for s in all_sessions)
            total_tokens = sum(s.get("tokens_total", 0) for s in all_sessions)
            desc = t(f"删除所有 {len(all_sessions)} 个会话（共 {total_steps} 步、"
                     f"{total_tokens} tokens，将连同文件一并删除）",
                     f"delete all {len(all_sessions)} sessions ({total_steps} steps, "
                     f"{total_tokens} tokens; their files will be removed too)")
            if not _confirm_dangerous(desc):
                _console.print(t("[dim]已取消[/dim]", "[dim]cancelled[/dim]"))
                continue
            removed = sm.delete_all()
            _console.print(t(f"{_tag('session')} 已删除 {removed} 个会话",
                             f"{_tag('session')} deleted {removed} sessions"))
            # 必须开新 session（当前也已被删）
            sid = sm.create(mode=default_mode, task=t("(新会话)", "(new session)"))
            state = State(sm.path_for(sid))
            sm.set_current(sid)
            _console.print(t(f"{_tag('session')} 已开新 session {sid[:6]}",
                             f"{_tag('session')} opened new session {sid[:6]}"))
            continue
        if line.startswith("/delete") or line.startswith("/del"):
            parts = line.split()
            target_sid = parts[1] if len(parts) > 1 else sid
            # 支持前缀匹配（输 6 位短 id 也能找到）
            if not sm.get(target_sid):
                matches = [s["id"] for s in sm.list_all()
                           if s["id"].startswith(target_sid)]
                if len(matches) == 1:
                    target_sid = matches[0]
                elif not matches:
                    _console.print(t(f"{_tag('error')} 找不到会话 {target_sid}", f"{_tag('error')} session not found: {target_sid}"))
                    continue
                else:
                    _console.print(
                        t(f"{_tag('error')} 匹配多个会话: "
                          f"{' / '.join(sm.label_of(m, short=6) for m in matches)}"
                          f"，请输更长的前缀",
                          f"{_tag('error')} matches multiple sessions: "
                          f"{' / '.join(sm.label_of(m, short=6) for m in matches)}"
                          f", please type a longer prefix")
                    )
                    continue
            meta = sm.get(target_sid) or {}
            target_label = sm.label_of(target_sid, short=6)
            desc = t(f"删除会话 {target_label}（{meta.get('mode', '?')} 模式、"
                     f"{meta.get('steps_total', 0)} 步、"
                     f"{meta.get('tokens_total', 0)} tokens，将连同文件一并删除）",
                     f"delete session {target_label} ({meta.get('mode', '?')} mode, "
                     f"{meta.get('steps_total', 0)} steps, "
                     f"{meta.get('tokens_total', 0)} tokens; its files will be removed too)")
            if not _confirm_dangerous(desc):
                _console.print(t("[dim]已取消[/dim]", "[dim]cancelled[/dim]"))
                continue
            if sm.delete(target_sid):
                _console.print(t(f"{_tag('session')} 已删除会话 {target_label}",
                                 f"{_tag('session')} deleted session {target_label}"))
                if target_sid == sid:
                    # 删的就是当前 session → 切回上次活跃的，没有就开新
                    prev_sid = sm.last_session_id()
                    if prev_sid:
                        sid = prev_sid
                        sm.update(sid, status="active", mode=default_mode)
                        state = State(sm.path_for(sid))
                        sm.set_current(sid)
                        meta = sm.get(sid) or {}
                        _console.print(
                            t(f"{_tag('session')} 已切回上次会话 {sm.label_of(sid, short=6)} · "
                              f"{meta.get('mode', '?')} 模式 · "
                              f"{meta.get('steps_total', 0)} 步累计 · "
                              f"{meta.get('tokens_total', 0)} tokens",
                              f"{_tag('session')} switched back to last session "
                              f"{sm.label_of(sid, short=6)} · "
                              f"{meta.get('mode', '?')} mode · "
                              f"{meta.get('steps_total', 0)} steps total · "
                              f"{meta.get('tokens_total', 0)} tokens")
                        )
                    else:
                        sid = sm.create(mode=default_mode, task=t("(新会话)", "(new session)"))
                        state = State(sm.path_for(sid))
                        sm.set_current(sid)
                        _console.print(
                            t(f"{_tag('session')} 没有可切回的会话，已开新 session {sid[:6]}",
                              f"{_tag('session')} no session to switch back to, "
                              f"opened new session {sid[:6]}")
                        )
            else:
                _console.print(t(f"{_tag('session')} 删除失败（会话不存在？）",
                                 f"{_tag('session')} delete failed (session not found?)"))
            continue
        if line == "/new":
            # 开新 session（保留旧的，可 /switch 回看）
            # 同时把当前 default_mode 带过去
            sid = sm.create(mode=default_mode, task=t("(新会话)", "(new session)"))
            state = State(sm.path_for(sid))
            sm.set_current(sid)
            _console.print(t(f"{_tag('session')} 已切换到新 session {sid}",
                             f"{_tag('session')} switched to new session {sid}"))
            continue
        if line.startswith("/mode"):
            parts = line.split()
            if len(parts) == 1:
                _console.print(t(f"当前默认 mode: {default_mode}",
                                 f"current default mode: {default_mode}"))
            elif parts[1] in ("ask", "do", "review"):
                default_mode = parts[1]
                _console.print(t(f"默认 mode → {default_mode}",
                                 f"default mode → {default_mode}"))
            else:
                _console.print(t(f"{_tag('error')} 未知 mode: {parts[1]}",
                                 f"{_tag('error')} unknown mode: {parts[1]}"))
            continue
        if line == "/perm" or line.startswith("/perm "):
            parts = line.split()
            if len(parts) == 1:
                _console.print(t(f"当前权限层级: {perm_level} — {level_label(perm_level)}",
                                 f"current permission level: {perm_level} — {level_label(perm_level)}"))
                _console.print(t(
                    "[dim]用法: /perm strict|balanced|yolo（仅本次运行生效；"
                    "持久化请用 /config set permission_level yolo）[/dim]",
                    "[dim]usage: /perm strict|balanced|yolo (this run only; "
                    "persist with /config set permission_level yolo)[/dim]"))
            elif parts[1].lower() in ("strict", "balanced", "yolo", "auto"):
                perm_level = normalize_level(parts[1])
                _console.print(t(f"权限层级 → {perm_level} — {level_label(perm_level)}",
                                 f"permission level → {perm_level} — {level_label(perm_level)}"))
                if perm_level == LEVEL_YOLO:
                    # 这一层没有任何执行前检查，切换时把边界说清楚（不是阻塞确认）
                    _console.print(t(
                        "[yellow]⚠ yolo：不再询问任何操作，agent 以你的用户权限执行，"
                        "可写工作区外的任意路径；仅灾难命令（rm -rf / 等）仍被硬拦截[/yellow]",
                        "[yellow]⚠ yolo: no more prompts; the agent runs with your user "
                        "privileges and can write anywhere, not just the workspace. "
                        "Only catastrophic commands (rm -rf / etc.) are hard-blocked[/yellow]"))
            else:
                _console.print(t(
                    f"{_tag('error')} 未知权限层级: {parts[1]}（strict/balanced/yolo）",
                    f"{_tag('error')} unknown permission level: {parts[1]} "
                    f"(strict/balanced/yolo)"))
            continue
        if line.startswith("/switch "):
            new_sid = line[len("/switch "):].strip()
            if not sm.get(new_sid):
                matches = [s["id"] for s in sm.list_all()
                           if s["id"].startswith(new_sid)]
                if len(matches) == 1:
                    new_sid = matches[0]
                elif not matches:
                    _console.print(t(f"{_tag('error')} 找不到会话 {new_sid}",
                                     f"{_tag('error')} session not found: {new_sid}"))
                    continue
                else:
                    _console.print(
                        t(f"{_tag('error')} 匹配多个会话: "
                          f"{' / '.join(sm.label_of(m, short=6) for m in matches)}"
                          f"，请输更长的前缀",
                          f"{_tag('error')} matches multiple sessions: "
                          f"{' / '.join(sm.label_of(m, short=6) for m in matches)}"
                          f", please type a longer prefix")
                    )
                    continue
            sid = new_sid
            state = State(sm.path_for(sid))
            sm.set_current(sid)
            _console.print(t(f"{_tag('session')} 切换到 {sm.label_of(sid)}",
                             f"{_tag('session')} switched to {sm.label_of(sid)}"))
            continue
        if line.startswith("/"):
            _console.print(t(f"{_tag('error')} 未知命令: {line}（/help 查看可用命令）",
                             f"{_tag('error')} unknown command: {line} (/help for available commands)"))
            continue

        # —— 任务执行 ——
        parts = line.split(maxsplit=1)
        if parts[0] in (":ask", ":do", ":review"):
            mode = parts[0][1:]  # 去掉冒号 → "do"
            task = parts[1].strip() if len(parts) > 1 else ""
        else:
            mode = default_mode
            task = line

        if not task:
            _console.print(t(f"{_tag('error')} 任务不能为空",
                             f"{_tag('error')} task cannot be empty"))
            continue

        trust = mode in _WRITE_MODES
        stream = not args.no_stream and cfg.get("stream", True)
        max_steps = args.max_steps or cfg.get("max_steps", 50)

        # 每轮新建 AgentLoop：mode/trust 可能切换；State 共享 → 跨轮上下文
        agent = model.build_agent(
            mode=mode, trust=trust, stream=stream,
            max_steps=max_steps,
            permission_handler=view.prompt_permission,
            state=state,
            session_id=sid,
            session_manager=sm,
            permission_level=perm_level,
        )
        sm.update(sid, mode=mode, status="active")
        view.reset_for_new_run()

        # 联合 callback：view 渲染 + 累加 stats 写回 session + 检测 <new_task/> 标记
        last = {"content": "", "new_task": False, "max_steps_hit": False}
        meta = sm.get(sid) or {}
        tok_acc = meta.get("tokens_total", 0)
        step_acc = meta.get("steps_total", 0)

        async def on_event(event: dict) -> None:
            nonlocal tok_acc, step_acc
            await view.on_event(event)
            e = event.get("event")
            if e == "done":
                content = event.get("content", "") or ""
                last["content"] = content
                # 检测 agent 输出的 <new_task/> 标记（view 已静默过滤渲染，
                # 但这里看原始 content 触发用户提示）
                if "<new_task/>" in content:
                    last["new_task"] = True
            elif e == "max_steps":
                # 达到最大步数：标记，让 stats 后提示 /new
                last["max_steps_hit"] = True
            elif e == "stats":
                tok_acc += event["usage"].get("total_tokens", 0)
                step_acc += event["steps"]
                # status 从 loop.py 透传：done / interrupted / loop_detected / max_steps
                status = event.get("status", "done")
                # active 表示 session 仍可用；done/interrupted/loop_detected/max_steps
                # 都是单次 run 结束的终态标记，写到 summary 字段；
                # session.status 仍按是否还在 REPL 内区分 active/done
                sm.update(sid, tokens_total=tok_acc, steps_total=step_acc,
                          status="active", summary=last["content"][:200],
                          last_status=status)
                # run 结束后提示用户：步数上限已到，可在同一会话续跑（勿丢上下文）
                if last.get("max_steps_hit"):
                    _console.print(
                        t(f"[yellow]⚠ 已达本轮步数上限，上下文已保留，"
                          f"输入「继续」可在同一会话接着做 "
                          f"(累计 {step_acc} 步、{tok_acc} tokens)[/yellow]",
                          f"[yellow]⚠ step limit reached; context preserved, "
                          f"type 'continue' to resume in the same session "
                          f"({step_acc} steps, {tok_acc} tokens total)[/yellow]")
                    )
                    last["max_steps_hit"] = False
                elif last.get("new_task"):
                    _console.print(
                        t(f"[yellow]⚠ 检测到新任务，建议 /new 切换会话 "
                          f"(累计 {step_acc} 步、{tok_acc} tokens)[/yellow]",
                          f"[yellow]⚠ new task detected, consider /new to switch session "
                          f"({step_acc} steps, {tok_acc} tokens total)[/yellow]")
                    )
                    last["new_task"] = False

        try:
            model.ensure_loop()  # 常驻 loop 下为 no-op，保留作防御
            _run(agent.run(task, callback=on_event))
        except KeyboardInterrupt:
            view._close_content(preserve=False)
            view._stop_thinking()
            _console.print()
            # 显示已完成的部分统计（优先 agent 内部存的，兜底 view 存的）
            stats = agent.last_stats or view._last_stats
            if stats:
                u = stats["usage"]
                # 补更新 session 元数据（防止 stats 事件没发出去导致 session 记录落后）
                sm.update(sid, tokens_total=u["total_tokens"],
                          steps_total=stats["steps"],
                          last_status="interrupted")
                _console.print(
                    t(f"[dim]截至中断: {stats['steps']} 步 · "
                      f"[cyan]↑{u['prompt_tokens']} tokens[/cyan] "
                      f"[magenta]↓{u['completion_tokens']} tokens[/magenta] "
                      f"[bold]Σ {u['total_tokens']} tokens[/bold][/dim]",
                      f"[dim]at interruption: {stats['steps']} steps · "
                      f"[cyan]↑{u['prompt_tokens']} tokens[/cyan] "
                      f"[magenta]↓{u['completion_tokens']} tokens[/magenta] "
                      f"[bold]Σ {u['total_tokens']} tokens[/bold][/dim]")
                )
            _console.print(t(f"{_tag('interrupt')} 用户中止，已返回提示符",
                             f"{_tag('interrupt')} user interrupted, back to prompt"))
        except Exception as e:
            view._close_content(preserve=False)
            view._stop_thinking()
            _console.print(f"{_tag('error')} {e}")
        finally:
            # 兜底：任何出口（done / 中断 / 异常 / 连按两次 Ctrl+C）都复位
            # spinner + Live，防止 rich Status 渲染线程泄漏让终端卡住
            view.stop_all()

        # 会话标题：本轮已有真实对话，缺标题时用 LLM 生成一次（失败静默降级）
        try:
            model.ensure_loop()
            title = _run(agent.generate_title())
            if title:
                _console.print(t(f"[dim]{_tag('session')} 会话标题: {title}[/dim]",
                                 f"[dim]{_tag('session')} session title: {title}[/dim]"))
        except KeyboardInterrupt:
            pass  # 标题只是锦上添花，绝不打断 REPL
        except Exception:
            pass


def run_frontend(args: argparse.Namespace) -> None:
    model = AgentModel()
    view = TerminalView()

    sm = SessionManager(SESSIONS_DIR)

    # 启动时清理空 session（启动后立刻 /exit 残留的）
    removed = sm.cleanup_empty()
    if removed:
        _console.print(t(f"[dim]{_tag('session')} 清理了 {removed} 个空 session[/dim]",
                         f"[dim]{_tag('session')} cleaned up {removed} empty sessions[/dim]"))

    cont = args.continue_session
    force_new = args.new
    # 优先级：--continue <sid> > --new > 默认（续接上次会话或新建）
    if cont:
        sid = cont
        if sid == "last":
            # 优先用持久化的"当前会话"指针（/switch 后重启也能精确恢复），
            # 指针失效再按最近活跃时间兜底
            sid = sm.get_current() or sm.last_session_id() or ""
            if not sid:
                raise SystemExit(t(f"{_tag('session')} 没有可续接的会话",
                                   f"{_tag('session')} no session to resume"))
        if not sm.get(sid):
            raise SystemExit(t(f"{_tag('session')} 会话 {sid} 不存在",
                               f"{_tag('session')} session {sid} does not exist"))
        sm.update(sid, status="active", mode=args.mode)
        resumed = True
    elif force_new:
        sid = sm.create(mode=args.mode, task=t("(交互式会话)", "(interactive session)"))
        resumed = False
    else:
        # 默认：优先精确恢复上次使用的会话（.current 指针），否则新建
        last_sid = sm.get_current() or sm.last_session_id()
        if last_sid and sm.get(last_sid):
            sid = last_sid
            meta = sm.get(sid)
            sm.update(sid, status="active", mode=args.mode)
            resumed = True
        else:
            sid = sm.create(mode=args.mode, task=t("(交互式会话)", "(interactive session)"))
            resumed = False

    sm.set_current(sid)  # 记录当前会话，/switch、崩溃后重启均可靠恢复
    state = State(sm.path_for(sid))

    _render_banner(_console, sm, sid, resumed, args)

    try:
        repl_loop(model, view, sm, sid, state, args)
    finally:
        # 关闭 httpx client：此时仍在常驻 loop 上，连接与 loop 归属一致，可安全关闭
        try:
            _run(model.close())
        except Exception:
            pass  # 退出阶段不因清理失败而中断（fd 由 OS 回收）
        _close_loop()
        # 收尾会话必须取 repl_loop 最终所在的会话（内部可能 /switch 过），
        # 否则 done 状态和 last_active_at 会写回旧会话，导致下次恢复错乱
        final_sid = sm.get_current() or sid
        sm.update(final_sid, status="done")


def _render_banner(console, sm, sid: str, resumed: bool, args) -> None:
    """Claude Code 风格启动 banner：圆角矩形 + 上下左右完整边框"""
    from VilAgent.config import __version__ as _ver

    cfg = load_config()
    ws = Path.cwd()
    mode = (sm.get(sid) or {}).get("mode") or args.mode or "ask"
    mode_color = "bold green" if mode == "ask" else ("bold red" if mode == "do" else "bold yellow")
    perm = normalize_level(getattr(args, "permission", None) or cfg.get("permission_level"))
    perm_color = {"strict": "bold red", "balanced": "bold yellow",
                  "yolo": "bold green"}.get(perm, "bold yellow")
    max_steps = args.max_steps or cfg.get("max_steps") or 50
    llm = cfg.get("llm", {}) or {}
    model_name = llm.get("model") or "?"

    # Panel 内宽 = 控制台宽度 - 左右边框(2) - padding 左右(2)
    try:
        console_width = console.size.width
    except (AttributeError, OSError):
        console_width = 80
    panel_width = max(60, console_width - 4)

    lines = []

    # 1. Logo 行（两端对齐：左 logo，右版本号）
    logo = Text()
    logo.append("●  ", style="cyan")
    logo.append("Vil Agent HCI Frontend", style="bold cyan")
    right = Text(f"v{_ver}", style="dim")
    lines.append(_justify_row(logo, right, width=panel_width))

    # 2. 模式 chip（ask=绿 do=红 review=黄）+ 权限层级 chip（strict=红 balanced=黄 yolo=绿）
    mode_line = Text()
    mode_line.append("  mode:  ", style="dim")
    mode_line.append(mode.upper().ljust(6), style=mode_color)
    mode_line.append("  perm: ", style="dim")
    mode_line.append(perm.upper(), style=perm_color)
    lines.append(mode_line)

    # 3. cwd
    info_line = Text()
    info_line.append("  cwd:   ", style="dim")
    info_line.append(str(ws), style="")
    lines.append(info_line)

    # 4. model + max_steps
    model_line = Text()
    model_line.append("  model: ", style="dim")
    model_line.append(model_name, style="")
    model_line.append(t("  ·  单任务max_steps: ", "  ·  max_steps/task: "), style="dim")
    model_line.append(str(max_steps), style="")
    _total = cfg.get("max_steps_total")
    if _total and isinstance(_total, int) and _total > max_steps:
        model_line.append(t(f"(自动续至{_total})", f"(auto-extend to {_total})"),
                          style="dim")
    lines.append(model_line)

    # 5. 分隔线（按 Panel 内宽精确拼 cell_len，前导 2 空格 + ─ 填满）
    sep_dashes = max(40, panel_width - 2)
    sep_line = Text()
    sep_line.append("  ", style="default")
    sep_line.append("─" * sep_dashes, style="cyan")
    lines.append(sep_line)

    # 6. 快捷提示（两行：模式前缀 + 自然语言导航）
    # 模式行
    sh_mode = Text()
    sh_mode.append(t("  模式: ", "  mode: "), style="dim")
    sh_mode.append(":ask", style="bold")
    sh_mode.append(t(" 只读 · ", " read-only · "), style="dim")
    sh_mode.append(":do", style="bold")
    sh_mode.append(t(" 执行 · ", " execute · "), style="dim")
    sh_mode.append(":review", style="bold")
    sh_mode.append(t(" 审查", " code review"), style="dim")
    lines.append(sh_mode)
    # 命令行（自然语言，强调 /help 查看所有命令）
    sh_cmd = Text()
    # sh_cmd.append("  命令: ", style="dim")
    sh_cmd.append(t("  键入 ", "  type "), style="dim")
    sh_cmd.append("/help", style="bold")
    sh_cmd.append(t(" 查看所有命令，", " to list all commands, "), style="dim")
    sh_cmd.append("/exit", style="bold")
    sh_cmd.append(t(" 退出", " to quit"), style="dim")
    lines.append(sh_cmd)

    body = Group(*lines)
    panel = Panel(
        body,
        border_style="cyan",
        box=ROUNDED,
        padding=(0, 1),
        expand=False,
    )
    console.print(panel)

    # 7. 会话信息（Panel 外：resumed / new 不同，放在 banner 下方）
    if resumed:
        meta = sm.get(sid) or {}
        steps = meta.get("steps_total", 0)
        tokens = meta.get("tokens_total", 0)
        task_preview = (meta.get("task") or meta.get("summary") or "")[:80]

        head = Text()
        head.append("  ↻ ", style="bold green")
        head.append(t("已恢复会话 ", "resumed session "), style="")
        title = sm.title_of(sid)
        if title:
            # 标题放主位（用户靠它辨认会话），短 ID 退成灰色括注
            head.append(_trunc(title, 44), style="bold yellow")
            head.append(f" ({sid[:12]})", style="dim")
        else:
            head.append(sid[:12], style="bold yellow")
        head.append("  ·  ", style="dim")
        head.append(t(f"{steps} 步", f"{steps} steps"), style="")
        head.append("  ·  ", style="dim")
        head.append(f"{tokens:,} tokens", style="")
        console.print(head)

        if task_preview and task_preview != title and \
                task_preview not in ("(交互式会话)", "(interactive session)",
                                     "(新会话)", "(new session)"):
            task_line = Text()
            task_line.append(t("    ↳ 上次任务: ", "    ↳ last task: "), style="dim")
            task_line.append(task_preview, style="italic")
            console.print(task_line)

        # 渲染 todo 进度（如有）
        todos = sm.get_todos(sid)
        if todos:
            todo_text = Text()
            done_count = sum(1 for t in todos if t.get("status") == "completed")
            total = len(todos)
            todo_text.append(t(f"    📋 进度: {done_count}/{total} 项完成",
                               f"    📋 progress: {done_count}/{total} done"), style="cyan")
            console.print(todo_text)
            for item in todos[:5]:  # 最多显示 5 项
                status = item.get("status", "pending")
                icon = {"pending": "○", "in_progress": "◐",
                        "completed": "●"}.get(status, "○")
                color = {"pending": "dim", "in_progress": "yellow",
                         "completed": "green"}.get(status, "dim")
                line = Text()
                line.append(f"      {icon} ", style=color)
                line.append(item.get("content", "?"), style=color)
                console.print(line)
            if len(todos) > 5:
                more = Text()
                more.append(t(f"      ... 还有 {len(todos) - 5} 项",
                              f"      ... {len(todos) - 5} more"), style="dim")
                console.print(more)

        tip_line = Text()
        tip_line.append(t("    提示: ", "    tip: "), style="cyan")
        tip_line.append(t("直接键入任务继续，或 ", "type a task to continue, or "), style="dim")
        tip_line.append("/new", style="bold")
        tip_line.append(t(" 开新会话", " to start a new session"), style="dim")
        console.print(tip_line)
    else:
        head = Text()
        head.append("  ✨ ", style="bold magenta")
        head.append(t("新会话 ", "new session "), style="")
        head.append(sid[:12], style="bold yellow")
        console.print(head)

        tip_line = Text()
        tip_line.append(t("    提示: ", "    tip: "), style="cyan")
        tip_line.append(t("键入任务开始，或 ", "type a task to begin, or "), style="dim")
        tip_line.append(":do", style="bold")
        tip_line.append(t(" 切执行模式 / ", " to switch to do mode / "), style="dim")
        tip_line.append(":ask", style="bold")
        tip_line.append(t(" 切只读模式", " to switch to read-only mode"), style="dim")
        console.print(tip_line)
    console.print()


def _justify_row(left: Text, right: Text, width: int | None = None) -> Text:
    """把 left 放左端、right 放右端，中间空格填满。
    width=None 时用控制台宽度；传值按给定宽度（Panel 内比 console 窄 4 列）。"""
    if width is None:
        try:
            width = _console.size.width
        except (AttributeError, OSError):
            width = 80
    total = Text()
    total.append(left)
    gap = max(2, width - left.cell_len - right.cell_len)
    total.append(" " * gap)
    total.append(right)
    return total


def main() -> int:
    # 尽早按配置确定输出语言，保证 banner / help 文案也走对应语言
    set_lang(load_config().get("lang"))
    parser = build_parser()
    args = parser.parse_args()
    try:
        run_frontend(args)
    except KeyboardInterrupt:
        pass  # REPL 内部已处理，这里忽略剩余的中断信号
    return 0


if __name__ == "__main__":
    sys.exit(main())
