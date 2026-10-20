"""vil-agent-cli: VilAgent 的单次 CLI 入口

单次 CLI 模式：一次 task 跑完即退出。
多轮对话请用 vil-agent-frontend（REPL）。

会话策略（与 frontend 一致）:
    默认        续接上次会话（无则新建），实现跨次 CLI 历史复用
    --new       强制新建 session
    --continue <id|last>  续接指定会话 ID（'last' 等价于默认行为）

用法（模式即子命令，必须显式指定）:
    python vil-agent-cli.py ask 列出当前目录文件并说明项目结构
    python vil-agent-cli.py do 在项目根加 hello.py
    python vil-agent-cli.py do --new 隔离执行不相关任务
    python vil-agent-cli.py review 审查 VilAgent/loop.py
    python vil-agent-cli.py history list
    python vil-agent-cli.py config set llm.temperature 0.3

结构（经典 MVC，Model/View 共享自 VilAgent/agent_app.py）:
    Model       AgentModel      配置 + LLM/State/Context 组件装配
    View        TerminalView    事件渲染 + 权限交互输入
    Controller  本文件           解析参数、装配 Model+View、驱动 agent
"""
import argparse
import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

from VilAgent import SessionManager, State, load_config, set_value, get_value, CONFIG_FILE
from VilAgent.agent_app import (
    AgentModel, TerminalView,
    SESSIONS_DIR as _SESSIONS_DIR,
    console as _console,
)
from VilAgent.session import truncate_display as _trunc
from VilAgent.session import clean_title, title_from_messages
from VilAgent.loop import retitle_sessions
from VilAgent.i18n import t, set_lang


# ===================== Controller =====================

# 写类模式：默认 trust=True（允许写操作）
_WRITE_MODES = {"do"}


def _add_mode_flags(sp: argparse.ArgumentParser) -> None:
    """给模式子命令加公共 flag"""
    sp.add_argument("--no-stream", action="store_true",
                    help=t("关闭流式输出", "disable streaming output"))
    sp.add_argument("--max-steps", type=int, default=None,
                    help=t("最大步数（默认 50）", "max steps (default 50)"))
    sp.add_argument("--continue", dest="continue_session", metavar="SESSION",
                    help=t("续接指定会话 ID（'last' 表示最近一次，默认行为）",
                           "resume given session ID ('last' = most recent, the default)"))
    sp.add_argument("--new", dest="new_session", action="store_true",
                    help=t("强制新建 session（不续接上次）",
                           "force a new session (do not resume the last one)"))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vil-agent",
        description="Vil Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            t(
                "示例:\n"
                "  vil-agent ask 列出当前目录文件并说明项目结构\n"
                "  vil-agent do 在项目根加 hello.py\n"
                "  vil-agent do --new 隔离执行不相关任务\n"
                "  vil-agent do --continue <id> 续接指定会话\n"
                "  vil-agent review 审查 VilAgent/loop.py\n"
                "  vil-agent history list\n"
                "  vil-agent history show <session_id>\n"
                "  vil-agent history export <session_id> -o out.md\n"
                "  vil-agent config set llm.temperature 0.3",
                "examples:\n"
                "  vil-agent ask list files in the current dir and explain the project layout\n"
                "  vil-agent do add hello.py at the project root\n"
                "  vil-agent do --new run an unrelated task in isolation\n"
                "  vil-agent do --continue <id> resume the given session\n"
                "  vil-agent review review VilAgent/loop.py\n"
                "  vil-agent history list\n"
                "  vil-agent history show <session_id>\n"
                "  vil-agent history export <session_id> -o out.md\n"
                "  vil-agent config set llm.temperature 0.3",
            )
        ),
    )
    sub = p.add_subparsers(dest="command", metavar="<command>")

    # agent 模式即子命令：必须显式指定
    for name, help_text in [
        ("ask", t("只读分析/规划", "read-only analysis/planning")),
        ("do", t("执行（允许写操作）", "execute (writes allowed)")),
        ("review", t("代码审查", "code review")),
    ]:
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("task", nargs="+", help=t("任务文本", "task text"))
        _add_mode_flags(sp)

    # history：按 session 物理隔离，每个会话一个 jsonl 文件
    ph = sub.add_parser("history", help=t("对话历史管理（按 session 隔离）",
                                          "conversation history (isolated per session)"))
    hsub = ph.add_subparsers(dest="action", required=False, metavar="<action>")
    hsub.add_parser("list", help=t("列出所有会话（最近优先）", "list all sessions (most recent first)"))
    sh = hsub.add_parser("show", help=t("显示最近或指定会话内容", "show the most recent or given session"))
    sh.add_argument("session", nargs="?", help=t("会话 ID（缺省=最近一次）", "session ID (default: most recent)"))
    sh.add_argument("-n", type=int, default=10, help=t("显示消息条数（默认 10）", "number of messages to show (default 10)"))
    cl = hsub.add_parser("clear", help=t("删除最近或指定会话", "delete the most recent or given session"))
    cl.add_argument("session", nargs="?", help=t("会话 ID（缺省=最近一次）", "session ID (default: most recent)"))
    ex = hsub.add_parser("export", help=t("导出会话为 Markdown", "export a session as Markdown"))
    ex.add_argument("session", help=t("会话 ID", "session ID"))
    ex.add_argument("-o", "--out", help=t("输出文件路径（默认 session_<id>.md）",
                                         "output file path (default session_<id>.md)"))
    rt = hsub.add_parser("retitle", help=t("批量重生成会话标题（老标题一次性迁移）",
                                           "regenerate session titles (one-off migration)"))
    rt.add_argument("session", nargs="?", help=t("只处理该会话 ID（前缀匹配，缺省=全部）",
                                                 "only this session ID (prefix match; default: all)"))
    rt.add_argument("-n", "--dry-run", action="store_true",
                    help=t("只打印结果不写盘", "print results without writing"))
    rt.add_argument("--local", action="store_true",
                    help=t("只用本地规则，不调 LLM（省 token，质量略低）",
                           "local rules only, no LLM call (cheaper, lower quality)"))

    # config
    pc = sub.add_parser("config", help=t("全局配置管理", "global config management"))
    csub = pc.add_subparsers(dest="action", required=False, metavar="<action>")
    ps = csub.add_parser("set", help="set KEY VAL"); ps.add_argument("key"); ps.add_argument("value")
    pg = csub.add_parser("get", help="get KEY"); pg.add_argument("key")
    csub.add_parser("list", help=t("列出合并后配置（敏感字段掩码）",
                                   "list the merged config (sensitive fields masked)"))
    csub.add_parser("path", help=t("显示配置文件路径", "show the config file path"))

    p._history_sp = ph
    p._config_sp = pc
    return p


async def run_agent(args: argparse.Namespace) -> None:
    """agent 模式：mode 由子命令决定，trust 由模式决定

    会话：默认新建；--continue <id|last> 续接，避免跨任务历史污染。
    """
    model = AgentModel()
    view = TerminalView()

    mode = args.command
    trust = mode in _WRITE_MODES
    cfg = model.cfg
    stream = not args.no_stream and cfg.get("stream", True)
    max_steps = args.max_steps or cfg.get("max_steps", 50)

    task = " ".join(args.task)

    # —— 会话解析：默认续接上次 / --new 强制新建 / --continue <id> 指定 ——
    sm = SessionManager(_SESSIONS_DIR)
    # 启动时清理空 session（启动后立刻中断残留的）
    sm.cleanup_empty()
    cont = getattr(args, "continue_session", None)
    force_new = getattr(args, "new_session", False)

    if cont:
        # 显式 --continue <id|last>
        sid = cont
        if sid == "last":
            # 优先持久化的当前会话指针，失效再按最近活跃时间兜底
            sid = sm.get_current() or sm.last_session_id() or ""
            if not sid:
                raise SystemExit(t("[session] 没有可续接的会话",
                                   "[session] no resumable session"))
        meta = sm.get(sid)
        if not meta:
            raise SystemExit(t(f"[session] 会话 {sid} 不存在",
                               f"[session] session {sid} not found"))
        # 续接时以当前子命令的 mode 为准（允许 do 续接为 ask 等切换）
        sm.update(sid, status="active", mode=mode)
        _console.print(t(f"[dim]\\[session] 续接 {sm.label_of(sid)}[/dim]",
                         f"[dim]\\[session] resumed {sm.label_of(sid)}[/dim]"))
    elif force_new:
        # 显式 --new
        sid = sm.create(mode=mode, task=task)
        _console.print(t(f"[dim]\\[session] 新建 {sid}[/dim]",
                         f"[dim]\\[session] new {sid}[/dim]"))
    else:
        # 默认：续接上次会话（无则新建）
        last_sid = sm.get_current() or sm.last_session_id()
        if last_sid and sm.get(last_sid):
            sid = last_sid
            sm.update(sid, status="active", mode=mode)
            _console.print(t(f"[dim]\\[session] 续接上次 {sm.label_of(sid)}[/dim]",
                             f"[dim]\\[session] resumed last {sm.label_of(sid)}[/dim]"))
        else:
            sid = sm.create(mode=mode, task=task)
            _console.print(t(f"[dim]\\[session] 新建 {sid}[/dim]",
                             f"[dim]\\[session] new {sid}[/dim]"))

    sm.set_current(sid)  # 记录当前会话，供下次默认续接

    # State 路径指向该 session 的独立 jsonl 文件
    state = State(sm.path_for(sid))

    agent = model.build_agent(
        mode=mode, trust=trust, stream=stream,
        max_steps=max_steps, permission_handler=view.prompt_permission,
        state=state,
        session_id=sid,
        session_manager=sm,
    )

    # 联合 callback：view 渲染 + 把最终 stats / summary 写回 session
    last = {"content": ""}

    async def on_event(event: dict) -> None:
        await view.on_event(event)
        e = event.get("event")
        if e == "done":
            last["content"] = event.get("content", "")
        elif e == "stats":
            # status 从 loop.py 透传：done / interrupted / loop_detected / max_steps
            status = event.get("status", "done")
            sm.update(
                sid,
                tokens_total=event["usage"].get("total_tokens", 0),
                steps_total=event["steps"],
                status=status,
                summary=last["content"][:200],
            )

    try:
        await agent.run(task, callback=on_event)
    finally:
        await model.close()


def _fmt_time(ts: float) -> str:
    if not ts:
        return "?"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def cmd_history(args: argparse.Namespace) -> int:
    sm = SessionManager(_SESSIONS_DIR)
    action = args.action

    if action == "retitle":
        return cmd_history_retitle(args)

    if action == "list":
        return _history_list(sm, limit=50)

    if action == "show":
        sid = args.session
        if sid is None:
            sid = sm.last_session_id()
            if not sid:
                print(t("[history] (空)", "[history] (empty)"))
                return 0
        meta = sm.get(sid)
        if not meta:
            print(t(f"[history] 会话 {sid} 不存在", f"[history] session {sid} not found"))
            return 1
        state = State(sm.path_for(sid))
        msgs = state.load()
        print(f"=== session {sm.label_of(sid, short=0)} (mode={meta.get('mode')}, "
              f"steps={meta.get('steps_total', 0)}, "
              f"tokens={meta.get('tokens_total', 0)}) ===")
        print(f"task: {meta.get('task', '')}")
        n = args.n
        for m in msgs[-n:]:
            role = m.get("role", "?")
            content = m.get("content", "")
            if isinstance(content, str):
                preview = content[:120] + ("..." if len(content) > 120 else "")
            else:
                preview = str(content)[:120]
            print(f"[{role}] {preview}")
        print(t(f"[history] 共 {len(msgs)} 条，显示最近 {min(n, len(msgs))} 条",
                 f"[history] {len(msgs)} messages, showing the latest {min(n, len(msgs))}"))
        return 0

    if action == "clear":
        sid = args.session
        if sid is None:
            sid = sm.last_session_id()
            if not sid:
                print(t("[history] (空)", "[history] (empty)"))
                return 0
        label = sm.label_of(sid, short=6)
        if sm.delete(sid):
            print(t(f"[history] 已删除会话 {label}", f"[history] deleted session {label}"))
            return 0
        print(t(f"[history] 会话 {sid} 不存在", f"[history] session {sid} not found"))
        return 1

    if action == "export":
        sid = args.session
        meta = sm.get(sid)
        if not meta:
            print(t(f"[history] 会话 {sid} 不存在", f"[history] session {sid} not found"))
            return 1
        state = State(sm.path_for(sid))
        msgs = state.load()
        md = sm.export_markdown(sid, msgs)
        out = args.out or f"session_{sid}.md"
        Path(out).write_text(md, encoding="utf-8")
        print(t(f"[history] 已导出 {out}（{sm.label_of(sid, short=6)}）",
                 f"[history] exported to {out} ({sm.label_of(sid, short=6)})"))
        return 0

    return 1


def _history_list(sm: SessionManager, limit: int = 20) -> int:
    items = sm.list_all()
    if not items:
        print(t("[history] (空)", "[history] (empty)"))
        return 0
    sm.ensure_titles()  # 历史遗留会话本地兜底补标题（不调 LLM）
    items = sm.list_all()
    budget = max(16, shutil.get_terminal_size((80, 24)).columns - 60)
    print(f"{'ID':<14} {'MODE':<8} {'STEPS':<6} {'TOKENS':<8} "
          f"{'UPDATED':<17} {'TITLE'}")
    for it in items[:limit]:
        title = _trunc(it.get("title") or it.get("task") or "", budget)
        print(f"{it['id']:<14} {it.get('mode', '?'):<8} "
              f"{it.get('steps_total', 0):<6} "
              f"{it.get('tokens_total', 0):<8} "
              f"{_fmt_time(it.get('last_active_at', 0)):<17} "
              f"{title}")
    print(t(f"[history] 共 {len(items)} 个会话，显示最近 {min(limit, len(items))} 个",
             f"[history] {len(items)} sessions, showing the latest {min(limit, len(items))}"))
    return 0


def _title_row(idx: int, total: int, sm: SessionManager, row: dict) -> None:
    """打印单条标题迁移结果：`旧 -> 新`（`~` 表示有变化）"""
    head = f"  [{idx}/{total}] {row['id'][:8]}"
    if row.get("error"):
        print(t(f"{head} 失败: {row['error']}", f"{head} failed: {row['error']}"))
    elif not row.get("new"):
        print(t(f"{head} 跳过（无可读对话）", f"{head} skipped (no readable messages)"))
    else:
        flag = "~" if row["changed"] else "="
        print(f"{head} {flag} {row['old'] or '-'} -> {row['new']}")


def _title_summary(rows: list[dict], dry_run: bool) -> None:
    changed = sum(1 for r in rows if r["changed"])
    skipped = sum(1 for r in rows if not r.get("new"))
    failed = sum(1 for r in rows if r.get("error"))
    tail = t("（dry-run，未写盘）", " (dry-run, nothing written)") if dry_run else ""
    print(t(f"[title] 共 {len(rows)} 个会话：{changed} 个更新，{skipped} 个跳过，"
            f"{failed} 个失败{tail}",
            f"[title] {len(rows)} sessions: {changed} updated, {skipped} skipped, "
            f"{failed} failed{tail}"))


def cmd_history_retitle(args: argparse.Namespace) -> int:
    """批量重生成会话标题：把老的「硬截断」标题迁移成完整词组。

    默认逐会话调 LLM；`--local` 只走本地规则（不烧 token）；
    `--dry-run` 只打印不写盘。单个会话失败只跳过它。
    """
    sm = SessionManager(_SESSIONS_DIR)
    items = sm.list_all()
    want = (args.session or "").strip()
    if want:
        items = [it for it in items if it["id"].startswith(want)]
        if not items:
            print(t(f"[title] 会话 {want} 不存在", f"[title] session {want} not found"))
            return 1
    if not items:
        print(t("[title] (空)", "[title] (empty)"))
        return 0

    total = len(items)
    if args.local:
        rows = []
        for idx, it in enumerate(items, 1):
            sid = it["id"]
            msgs = sm.load_messages(sid, limit=12)
            old = clean_title(it.get("title", "")) or title_from_messages(
                msgs, it.get("task", ""))
            new = title_from_messages(msgs, it.get("task", ""))
            row = {"id": sid, "old": old, "new": new or "", "error": "",
                   "changed": bool(new and new != old)}
            if new and row["changed"] and not args.dry_run:
                sm.set_title(sid, new)
            rows.append(row)
            _title_row(idx, total, sm, row)
        _title_summary(rows, args.dry_run)
        return 0

    # LLM 路径：没配 LLM 就没法生成，明确提示而不是静默给个糟糕标题
    try:
        model = AgentModel()
    except SystemExit:
        print(t("[title] 未配置 LLM；可改用 --local 走本地规则",
                "[title] LLM not configured; use --local for local rules"))
        return 1
    model.ensure_loop()
    done = [0]

    def _on_result(row: dict) -> None:
        done[0] += 1
        _title_row(done[0], total, sm, row)

    print(t(f"[title] 正在用 LLM 重生成 {total} 个会话的标题…",
            f"[title] regenerating titles for {total} sessions with the LLM..."))
    try:
        rows = asyncio.run(retitle_sessions(model.llm, sm, dry_run=args.dry_run,
                                            on_result=_on_result))
    except KeyboardInterrupt:
        print(t("\n[title] 已取消（已处理的会话已落盘）",
                "\n[title] cancelled (finished sessions are saved)"), file=sys.stderr)
        return 130
    _title_summary(rows, args.dry_run)
    return 0


def _parse_value(s: str):
    """尝试 JSON 解析（true/false/数字/null），否则当字符串"""
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return s


def _mask_value(v):
    """敏感字符串值用星号掩码：前 4 + **** + 后 4"""
    if not isinstance(v, str) or len(v) <= 8:
        return "****" if isinstance(v, str) and v else v
    return v[:4] + "****" + v[-4:]


def _mask_config(obj):
    """递归掩码敏感字段（key/token/secret/password/credential）的值"""
    sensitive = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    if isinstance(obj, dict):
        return {k: (_mask_value(v) if any(s in k.upper() for s in sensitive)
                    and isinstance(v, (str, int, float))
                    else _mask_config(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask_config(i) for i in obj]
    return obj


def cmd_config(args: argparse.Namespace) -> int:
    if args.action == "set":
        set_value(args.key, _parse_value(args.value))
        print(f"[config] {args.key} = {args.value}")
        return 0
    if args.action == "get":
        # 显式取值显示原文（方便核对）
        print(get_value(args.key))
        return 0
    if args.action == "list":
        print(json.dumps(_mask_config(load_config()),
                         ensure_ascii=False, indent=2))
        return 0
    if args.action == "path":
        print(CONFIG_FILE)
        return 0
    return 1


def main() -> int:
    # 尽早按配置确定输出语言，保证 argparse 的 help 文案也走对应语言
    set_lang(load_config().get("lang"))
    parser = build_parser()
    args = parser.parse_args()
    cmd = args.command
    if cmd is None:
        parser.print_help()
        return 0
    if cmd in ("ask", "do", "review"):
        try:
            asyncio.run(run_agent(args))
        except KeyboardInterrupt:
            print(t("\n[interrupt]", "\n[interrupt]"), file=sys.stderr)
            return 130
        return 0
    if cmd == "history":
        if args.action is None:
            parser._history_sp.print_help()
            return 0
        return cmd_history(args)
    if cmd == "config":
        if args.action is None:
            parser._config_sp.print_help()
            return 0
        return cmd_config(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
