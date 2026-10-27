"""Agent 应用的 Model + View（共享给 CLI 与 Frontend 入口）

MVC 划分：
    Model       AgentModel      配置 + LLM/State/Context 组件装配
    View        TerminalView    事件渲染 + 权限交互输入（无业务逻辑）
    Controller  cli/frontend    各自实现参数解析与驱动方式

两个入口共享同一套 Model/View，差异只在交互方式：
    vil-agent-cli        单次 CLI：一次 task 跑完退出
    vil-agent-frontend   交互 REPL：一个 session 内多轮 user 输入
"""
import asyncio
import os
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.status import Status
from rich.syntax import Syntax
from rich.text import Text

from .config import load_config, set_value, get_value, CONFIG_FILE
from .context import Context
from .i18n import set_lang, t
from .input_line import read_line
from .llm import LLMClient
from .loop import AgentLoop
from .safety import clean_stale_sandboxes
from .session import SessionManager
from .state import State

# session 历史根目录（每个 session 一个 jsonl 文件 + index.json）
# 支持 VIL_SESSIONS_DIR 环境变量覆盖，便于测试 / 多实例隔离
SESSIONS_DIR = Path(
    os.environ.get("VIL_SESSIONS_DIR") or (Path.home() / ".vil" / "sessions")
)


# ===================== Model =====================

class AgentModel:
    """配置 + agent 组件（数据与业务对象，不碰 I/O）"""

    def __init__(self):
        cfg = load_config()
        # 按配置切换输出语言（cn/en），影响 t()/L() 取值；未知值回退 cn
        set_lang(cfg.get("lang"))
        llm_cfg = cfg.get("llm") or {}
        if not llm_cfg.get("endpoint") or not llm_cfg.get("model"):
            raise SystemExit(t(
                "未配置 LLM，先设置：\n"
                "  from VilAgent import set_value\n"
                "  set_value('llm.endpoint', '<URL>')\n"
                "  set_value('llm.model', '<模型名>')\n"
                "或编辑 ~/.vil/config.json",
                "LLM not configured. Set it first:\n"
                "  from VilAgent import set_value\n"
                "  set_value('llm.endpoint', '<URL>')\n"
                "  set_value('llm.model', '<model>')"
                "\nor edit ~/.vil/config.json"))
        self.cfg = cfg
        self.llm = LLMClient(
            endpoint=llm_cfg["endpoint"],
            model=llm_cfg["model"],
            api_key=llm_cfg.get("api_key"),
            temperature=llm_cfg.get("temperature", 0.2),
            max_retries=llm_cfg.get("max_retries", 3),
            retry_base_delay=llm_cfg.get("retry_base_delay", 0.5),
        )
        self.ctx = Context(workspace=Path.cwd())
        # 启动时清扫历史遗留的沙箱临时目录（异常退出会残留，best-effort）
        try:
            clean_stale_sandboxes()
        except Exception:
            pass
        # 跟踪 httpx client 所属的事件循环，跨 asyncio.run() 时重建 client
        self._client_loop: asyncio.AbstractEventLoop | None = None

    def ensure_loop(self) -> None:
        """确保 httpx AsyncClient 在当前事件循环里可用。

        正常路径下前端用一个常驻事件循环（见 vil-agent-frontend._run），
        此方法恒为 no-op。仅当有代码另起 asyncio.run() 时才需要重建
        client —— 否则 transport 绑在旧循环上会抛 Event loop is closed。
        """
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            return  # 不在事件循环里，安全跳过
        if self._client_loop is None:
            self._client_loop = current
        elif self._client_loop is not current:
            # 事件循环变了 → 重建 httpx client
            self.llm.recreate_client()
            self._client_loop = current

    def build_agent(self, mode: str, trust: bool, stream: bool,
                    max_steps: int, permission_handler, state: State,
                    session_id: str = "",
                    session_manager: SessionManager | None = None,
                    max_steps_total: int | None = None,
                    permission_level: str | None = None):
        # state 由 controller 创建：每个 session 一个独立 jsonl 文件
        # context_budget：超此 token 数时摘要旧消息（None=不启用）
        budget = self.cfg.get("context_budget")
        # 单轮步数硬上限：到达 max_steps 后自动续跑至此值（<=max_steps 时不启用）
        if max_steps_total is None:
            max_steps_total = self.cfg.get("max_steps_total")
        # 权限层级：调用方显式传入优先，否则用配置（strict / balanced / auto）
        if permission_level is None:
            permission_level = self.cfg.get("permission_level")
        return AgentLoop(
            llm=self.llm, state=state, context=self.ctx,
            mode=mode, trust=trust, stream=stream,
            max_steps=max_steps,
            max_steps_total=max_steps_total,
            permission_handler=permission_handler,
            permission_level=permission_level,
            permission_summary=self.cfg.get("permission_summary", True),
            context_budget=budget,
            session_id=session_id,
            session_manager=session_manager,
        )

    async def close(self):
        await self.llm.aclose()


# ===================== View =====================

console = Console()
err_console = Console(stderr=True)

# 风险等级 → 颜色
_RISK_COLOR = {"high": "red", "medium": "yellow", "low": "green"}


class TerminalView:
    """事件渲染 + 交互输入。无业务逻辑，不改 agent 状态。

    风格参考 Claude Code：紧凑、少量颜色；流式正文用 Live + Markdown
    增量渲染，**粗体** / 列表 / 代码块 等实时渲染。"""

    def __init__(self):
        # 当前是否处于流式正文输出中（需要收尾换行）
        self._content_open = False
        # 本次 run 是否发生过流式输出（非流式回退时打印最终答案）
        self._streamed_any = False
        # 流式正文缓冲 + Live 渲染器（用 Markdown 增量渲染）
        self._content_buf = ""
        self._live = None
        # 加载动画（step 之间 LLM 思考阶段显示）
        self._thinking_status: Status | None = None
        # 最近一次 stats（供中断时显示部分完成的统计）
        self._last_stats: dict | None = None

    async def on_event(self, event: dict) -> None:
        e = event.get("event")
        # step 事件：停上一轮 spinner + 启动新一轮流式思考动画
        if e == "step":
            self._stop_thinking()
            self._start_thinking()
        else:
            # 任何其他事件先停 spinner（如果还在跑）
            self._stop_thinking()
        handler = getattr(self, f"_on_{e}", None)
        if handler:
            await handler(event)

    def _start_thinking(self) -> None:
        """step 开始：启动加载动画（LLM 思考中）"""
        if self._thinking_status is None:
            self._thinking_status = Status(
                "[dim]agent thinking...[/dim]", console=console,
                spinner="dots"
            )
            self._thinking_status.start()

    def _stop_thinking(self) -> None:
        """收到第一个 chunk / tool_call / 错误等：停加载动画"""
        if self._thinking_status is not None:
            try:
                self._thinking_status.stop()
            except Exception:
                pass
            self._thinking_status = None

    def stop_all(self) -> None:
        """run 结束兜底清理：停 spinner + 关 Live。

        无论正常 done / 异常 / 中断都必须把 UI 状态复位，
        否则泄漏的 rich Status 渲染线程会让终端"卡在 thinking 状态"。
        幂等：内部各方法都有 None 检查，可安全重复调用。
        """
        self._stop_thinking()
        self._close_content(preserve=False)

    def _close_content(self, preserve: bool = True) -> None:
        """结束当前流式正文：停止 Live（保留渲染结果）并复位标志。

        preserve=True（默认）：正常完成时保留最后一帧渲染
        preserve=False（中断时）：用 transient 清掉 Live 显示，避免残留文本
        """
        if self._content_open:
            if self._live is not None:
                if preserve:
                    self._live.stop()
                else:
                    # 清屏：把当前行覆盖为空再 stop
                    self._live.update(Markdown(""))
                    self._live.stop()
                self._live = None
            self._content_open = False
            self._content_buf = ""

    def reset_for_new_run(self) -> None:
        """每轮 run 开始前复位 stream 标志（前端多轮复用同一 View）"""
        self._streamed_any = False

    # —— 起始（用户任务）——
    async def _on_start(self, e):
        line = Text()
        line.append("● ", style="bold")
        line.append(e["task"], style="bold")
        line.append(f"  (mode: {e['mode']})", style="dim")
        console.print(line)

    # —— 助手正文流式（Live + Markdown 增量渲染：**粗体** 等会实时渲染）——
    async def _on_content_delta(self, e):
        if not self._content_open:
            console.print(Text("⏺", style="bold green"))
            self._content_open = True
            self._streamed_any = True
            self._content_buf = ""
            self._live = Live(Markdown(""), console=console,
                             refresh_per_second=10, transient=False)
            self._live.start()
        self._content_buf += e["chunk"]
        # 静默过滤 <new_task/> 标记：不渲染给用户看（前端 repl_loop 检测原始 content 触发提示）
        display = self._content_buf.replace("<new_task/>", "")
        self._live.update(Markdown(display))

    # —— 单步 LLM 响应完成 ——
    async def _on_llm_response(self, e):
        self._close_content()
        u = e.get("usage") or {}
        console.print(
            f"[dim]  step {e['step']} · {e['latency_s']}s · "
            f"in {u.get('prompt_tokens', 0)} / out {u.get('completion_tokens', 0)}[/dim]"
        )

    # —— 工具调用 ——
    async def _on_tool_call(self, e):
        self._close_content()
        name = e["name"]
        args = e["args"]

        # todo_write 特殊渲染：展开待办列表，不截断
        if name == "todo_write" and "todos" in args:
            line = Text()
            line.append("⏺ ", style="bold yellow")
            line.append("todo_write", style="bold yellow")
            todos = args["todos"]
            line.append(t(f"({len(todos)} 项)", f"({len(todos)} items)"))
            console.print(line)
            for _item in todos:
                status = _item.get("status", "pending")
                icon = {"pending": "○", "in_progress": "◐",
                        "completed": "●"}.get(status, "○")
                color = {"pending": "dim", "in_progress": "yellow",
                         "completed": "green"}.get(status, "dim")
                tline = Text()
                tline.append(f"  {icon} ", style=color)
                tline.append(_item.get("content", "?"), style=color)
                console.print(tline)
            return

        args_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
        if len(args_str) > 100:
            args_str = args_str[:100] + "…"
        line = Text()
        line.append("⏺ ", style="bold yellow")
        line.append(name, style="bold yellow")
        line.append(f"({args_str})")
        console.print(line)

    async def _on_file_diff(self, e):
        """渲染 write_file 的 unified diff（只给人看，LLM 不见，省 token）"""
        import difflib
        path = e["path"]
        old = e.get("old", "") or ""
        new = e.get("new", "") or ""
        # splitlines(keepends=True) 保留换行符，difflib 需要它生成正确的 diff
        old_lines = old.splitlines(keepends=True)
        new_lines = new.splitlines(keepends=True)
        diff = difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"{path} (old)", tofile=f"{path} (new)",
        )
        diff_text = "".join(diff)
        if not diff_text:
            console.print(Text(f"  ↳ (no changes in {path})", style="dim"))
            return
        # 内容不变 = diff 为空（unified_diff 不输出任何行），
        # 但 LLM 可能输出完全相同内容，这种情况也提示一下
        # 截断超长 diff（避免大文件全量 diff 刷屏）
        max_lines = 200
        diff_lines = diff_text.splitlines()
        if len(diff_lines) > max_lines:
            shown = "\n".join(diff_lines[:max_lines])
            diff_text = (shown + f"\n... ({len(diff_lines)} total diff lines, "
                         f"truncated)")
        syntax = Syntax(diff_text, "diff", theme="ansi_dark",
                        line_numbers=False, background_color="default",
                        word_wrap=True)
        console.print(Panel(syntax, title=f"diff: {path}",
                            title_align="left", border_style="cyan"))

    async def _on_risk_report(self, e):
        color = _RISK_COLOR.get(e["risk"], "white")
        line = Text()
        line.append(f"  risk: {e['risk']} ({e['cmd_type']})", style=color)
        line.append(f"  {'; '.join(e['reasons'])}", style="dim")
        console.print(line)

    async def _on_permission_decision(self, e):
        action = e["action"]
        color = "green" if action == "allow" else "red"
        line = Text()
        line.append("  perm: ", style="dim")
        line.append(f"{e['name']} → {action}", style=color)
        console.print(line)

    async def _on_permission_warning(self, e):
        console.print(Text(f"  ⚠ {e.get('message', '')}", style="bold yellow"))

    async def _on_tool_result(self, e):
        r = e["result"]
        preview = r[:200] + ("…" if len(r) > 200 else "")
        console.print(Text(f"  ↳ {preview}", style="dim"))

    async def _on_done(self, e):
        self._close_content()
        # 非流式模式：最终答案没被流式打印，这里补打（渲染 markdown）
        if not self._streamed_any:
            content = e.get("content", "")
            # 静默过滤 <new_task/> 标记
            content = content.replace("<new_task/>", "") if content else content
            if content:
                console.print(Text("⏺", style="bold green"))
                console.print(Markdown(content))

    async def _on_stats(self, e):
        self._last_stats = e
        u = e["usage"]
        status = e.get("status", "done")
        # 状态对应符号 + 颜色
        status_styles = {
            "done": ("✓", "bold green"),
            "max_steps": ("⚠", "bold yellow"),
            "loop_detected": ("⚠", "bold yellow"),
            "interrupted": ("", "bold yellow"),
        }
        symbol, style = status_styles.get(status, ("✓", "bold green"))
        status_tag = "" if status == "done" else f" [{status}]"
        prefix = f"{symbol}{status_tag}" if symbol or status_tag else ""
        console.print(
            f"[{style}]{prefix}[/{style}] "
            + t(f"[dim]{e['steps']} 步 · ", f"[dim]{e['steps']} steps · ")
            + f"{e['elapsed_s']}s [/dim] "
            f"[cyan]↑{u['prompt_tokens']} tokens[/cyan] "
            f"[magenta]↓{u['completion_tokens']} tokens[/magenta] "
            + t(f"[dim](推理 {u['reasoning_tokens']} tokens)[/dim] ",
                f"[dim](reasoning {u['reasoning_tokens']} tokens)[/dim] ")
            + f"[bold]Σ {u['total_tokens']} tokens[/bold]"
        )

    async def _on_error(self, e):
        self._close_content()
        line = Text()
        line.append("✗ ", style="red")
        line.append(e["message"], style="red")
        err_console.print(line)

    async def _on_budget_extended(self, e):
        # 到达软上限但仍在推进：自动续跑一段（提示用户，可随时 Ctrl+C）
        console.print(
            f"[dim]↻ {t('自动续跑', 'auto-extend')}: "
            f"{e['prev_limit']} → {e['limit']} {t('步', 'steps')}[/dim]"
        )

    async def _on_max_steps(self, e):
        self._close_content()
        err_console.print(
            f"[yellow]⚠ {t('已达本轮步数上限', 'reached step limit')} ({e['max_steps']})"
            + t("，上下文已保留，输入「继续」可接着做",
                "; context preserved, type 'continue' to resume")
            + "[/yellow]"
        )

    async def _on_loop_detected(self, e):
        self._close_content()
        err_console.print(
            f"[yellow]⚠ {t('检测到循环', 'loop detected')} (step {e['step']} "
            + t("重复签名", "repeated signature") + ")[/yellow]\n"
            f"[dim]  pattern: {e['pattern']}[/dim]"
        )

    async def _on_interrupted(self, e):
        self._close_content(preserve=False)
        err_console.print(
            f"[yellow]⚠ {t('用户中止', 'user interrupted')} (step {e['step']})[/yellow]"
        )

    # —— 权限交互（agent 调用此 handler 拿 allow/deny）——
    async def prompt_permission(self, req: dict) -> str:
        tool = req["tool"]
        args = req["args"]
        risk = req.get("risk")
        summary = req.get("summary")
        body = Text()
        body.append(t("请求执行 ", "Request to run "), style="dim")
        body.append(tool, style="bold")
        if risk:
            body.append(t(f"\n风险评级: {risk}", f"\nrisk level: {risk}"), style="yellow")
        # 模型生成的「这条命令/这段代码想干什么」：只在需要确认的执行类工具上有
        if summary:
            body.append(t("\n意图: ", "\nintent: "), style="dim")
            body.append(summary, style="cyan")
        for k, v in args.items():
            s = str(v)
            preview = s[:120] + ("…" if len(s) > 120 else "")
            body.append(f"\n  {k}: ", style="dim")
            body.append(preview)
        console.print(Panel(body, border_style="yellow",
                           title=t("权限请求", "permission request"), title_align="left"))
        # 读输入前先停掉 spinner：rich 的 Status 刷新线程会和行编辑器的重绘抢终端
        self._stop_thinking()
        # 提示符里有 [y/N]，必须转义，否则会被 rich 当成 style tag
        prompt_markup = f"[bold]{escape(t('  允许执行? [y/N] > ', '  Allow? [y/N] > '))}[/bold]"
        try:
            # 用共享的跨平台输入行（裸 input() 在 Linux/WSL 下方向键会变成 ^[[D）
            ans = await asyncio.to_thread(read_line, prompt_markup, console)
        except (EOFError, KeyboardInterrupt):
            console.print()
            return "deny"
        return "allow" if ans.strip().lower() in ("y", "yes") else "deny"
