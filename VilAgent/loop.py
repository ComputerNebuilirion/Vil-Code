"""Agent 执行循环：ReAct（Reason + Act）"""
import functools
import json
import time
from pathlib import Path
from typing import Awaitable, Callable

from .context import Context
from .i18n import t, get_lang
from .llm import LLMClient, LLMError
from .safety import classify, classify_command
from .safety.permissions import (
    PermissionSystem, default_rules_file, ALLOW, ASK, DENY,
)
from .session import (
    SessionManager, _TITLE_MAX_EN, _TITLE_MAX_ZH,
    clean_title, fit_title, title_from_messages,
)
from .state import State
from .tools import execute_tool, get_tool_schemas, tool_is_readonly

PROMPTS_DIR = Path(__file__).parent / "prompts"

# 工具结果喂回 LLM 前的字符上限（head+tail，避免单步超大输出撑爆上下文）
_TOOL_RESULT_MAX_CHARS = 4000


def _truncate_tool_result(text: str, max_chars: int = _TOOL_RESULT_MAX_CHARS) -> str:
    """超长工具结果截成 head+tail，保留首尾（错误信息常在末尾）"""
    if not isinstance(text, str) or len(text) <= max_chars:
        return text
    half = max_chars // 2
    return (text[:half] + f"\n...[truncated {len(text) - max_chars} chars]...\n"
            + text[-half:])

# 模式 → 提示词文件名（追加在 system.txt 之后）
_MODE_PROMPT = {
    "ask": "ask",
    "do": "do",
    "review": "review",
}


async def generate_title_for(llm, session_manager: SessionManager, session_id: str,
                             force: bool = False,
                             max_zh: int = _TITLE_MAX_ZH,
                             max_en: int = _TITLE_MAX_EN,
                             dry_run: bool = False) -> str | None:
    """给指定会话用 LLM 生成短标题（/sessions 靠它识别用途）。

    抽成模块级函数是为了「批量迁移历史标题」能复用：批量场景没有 AgentLoop 实例
    （不需要 State/Context/工具），只需 llm + session_manager + session_id。

    :param force: 已有标题时是否重新生成（/title 命令用）
    :param max_zh: 中文标题预算（汉字/英文词计 1 个单位）
    :param max_en: 英文标题预算（按词计）
    :param dry_run: 只算出标题不落盘（预览用）
    LLM 失败不报错，退回本地推导（首条 user 消息），保证标题始终有值。
    """
    meta = session_manager.get(session_id) or {}
    if not force and clean_title(meta.get("title", "")):
        return None

    msgs = session_manager.load_messages(session_id, limit=16)
    lines = []
    for m in msgs:
        if m.get("role") not in ("user", "assistant"):
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            content = json.dumps(content, ensure_ascii=False)
        text = " ".join(str(content or "").replace("<new_task/>", " ").split())
        if text:
            lines.append(f"[{m.get('role')}] {text[:300]}")
    if not lines:
        return None
    # 标题看开头最有效：取前 10 段，长了再补末尾 3 段
    sample = lines[:10] if len(lines) <= 13 else lines[:10] + ["..."] + lines[-3:]

    if get_lang() == "en":
        prompt = (
            f"Give the conversation below a short title in English "
            f"(at most {max_en} words, single line, no quotes, no period; "
            f"avoid vague words like 'session'). "
            f"It must be a noun phrase, not a half sentence and not a "
            f"truncated clause — e.g. 'Linux platform compatibility', "
            f"not 'Audit the code, now we are going to'. "
            f"Output the title only.\n\n"
            f"Conversation:\n" + "\n".join(sample)
        )
    else:
        prompt = (
            f"给下面这段对话起一个简短标题（不超过 {max_zh} 个字，"
            f"英文单词按 1 个字算，单行，不要引号、不要句号，"
            f"避免「会话/对话」这类空泛词）。"
            f"标题必须是名词性短语、完整词组，不要写成半句话，"
            f"例如「Linux平台兼容」而不是「审计代码，现在即将需要…」。"
            f"只输出标题本身。\n\n对话：\n" + "\n".join(sample)
        )

    title = ""
    try:
        result = await llm.chat(
            messages=[{"role": "user", "content": prompt}],
            stream=False,
        )
        # 长度预算在此统一收口：模型不听话时裁到完整词组
        title = fit_title((result or {}).get("content") or "",
                          zh_budget=max_zh, en_budget=max_en)
    except Exception:
        title = ""  # 标题是锦上添花，失败绝不打断主流程
    if not title:
        # LLM 失败/空回复：已有标题就保留——不拿本地粗推导去覆盖原本更好的标题
        title = clean_title(meta.get("title", "")) or title_from_messages(
            msgs, meta.get("task", ""))
    if not title:
        return None
    if dry_run:
        return title
    return session_manager.set_title(session_id, title) or None


async def retitle_sessions(llm, session_manager: SessionManager, *,
                           only: list[str] | None = None,
                           dry_run: bool = False,
                           max_zh: int = _TITLE_MAX_ZH,
                           max_en: int = _TITLE_MAX_EN,
                           on_result: Callable[[dict], None] | None = None,
                           ) -> list[dict]:
    """批量重生成会话标题（老标题一次性迁移到新的「完整词组」规则）。

    每个会话：先记下旧标题（本身没标题则本地推导），再无条件调 LLM 重生成，
    这样「旧的硬截断标题」也能一起修掉。

    :param only: 只处理这些 session id（缺省=全部，按最近活跃倒序）
    :param dry_run: 只计算不写盘，用于预览
    :return: [{"id", "old", "new", "changed", "error"}]
    """
    rows: list[dict] = []
    for meta in session_manager.list_all():
        sid = meta.get("id")
        if not sid or (only and sid not in only):
            continue
        old = clean_title(meta.get("title", "")) or title_from_messages(
            session_manager.load_messages(sid, limit=12), meta.get("task", ""))
        row = {"id": sid, "old": old, "new": "", "changed": False, "error": ""}
        try:
            new = await generate_title_for(
                llm, session_manager, sid, force=True, dry_run=dry_run,
                max_zh=max_zh, max_en=max_en)
        except Exception as exc:  # 单个会话失败不影响其余会话
            new, row["error"] = None, f"{type(exc).__name__}: {exc}"
        row["new"] = new or ""
        row["changed"] = bool(new and new != old)
        rows.append(row)
        if on_result:
            on_result(row)
    return rows


class AgentLoop:
    """
    ReAct Agent 循环

    模式:
      ask    - 只读分析/规划（仅 readonly 工具，由工具过滤强制）
      do     - 执行（全部工具，默认 trust=True）
      review - 代码审查（只读，结构化反馈）
    """

    def __init__(
        self,
        llm: LLMClient,
        state: State,
        context: Context,
        max_steps: int = 50,
        max_steps_total: int | None = None,
        mode: str = "ask",
        trust: bool = False,
        stream: bool = False,
        permission_handler: Callable[[dict], Awaitable[str]] | None = None,
        permissions_file: Path | str | None = None,
        context_budget: int | None = None,
        session_id: str = "",
        session_manager: SessionManager | None = None,
    ):
        self.llm = llm
        self.state = state
        self.context = context
        self.max_steps = max_steps
        # 硬上限：仅在 > max_steps 时启用自动续跑；否则等价于单段上限（不续）
        self.max_steps_total = (
            max_steps_total
            if (max_steps_total and max_steps_total > max_steps)
            else None
        )
        self.mode = mode
        self.trust = trust
        self.stream = stream
        self.permission_handler = permission_handler
        # token 预算：超此值时摘要旧消息（None=不启用，用原条数滑窗）
        self.context_budget = context_budget
        self.session_id = session_id
        self.session_manager = session_manager
        # 最近一次统计（供外部查询，如中断时显示部分完成的统计）
        self.last_stats: dict | None = None

        # do 模式默认开启 trust（允许写操作）
        if mode == "do":
            self.trust = True

        # L2 权限系统：显式传入优先，否则自动解析 <workspace>/.vil/permissions.json
        # 或 ~/.vil/permissions.json（让规则文件真正生效）；空串也走默认解析
        if not permissions_file:
            permissions_file = default_rules_file(context.workspace)
        self.permissions = PermissionSystem(
            rules_file=permissions_file, trust=self.trust
        )

        # 运行时上下文，透传给工具函数（_ctx 参数）
        self._ctx = {
            "workspace": str(context.workspace),
            "trust": self.trust,
            "mode": self.mode,
            "session_id": session_id,
            "sessions_dir": str(session_manager.dir) if session_manager else "",
        }

    async def run(self, task: str, callback: Callable[[dict], Awaitable[None]] | None = None) -> str:
        """
        ReAct 循环：构建 messages → llm.chat → 有 tool_calls 则执行并喂回 → 否则完成

        :param task: 用户任务文本
        :param callback: 事件回调，接收 dict：
            {"event": "start"|"step"|"content_delta"|"tool_call"|"tool_result"|"done"|"error"|"max_steps"|"budget_extended"|"loop_detected"|"interrupted", ...}
        :return: 最终 assistant 文本
        """
        await self._emit(callback, {"event": "start", "task": task, "mode": self.mode})

        # 记录用户任务到历史
        user_msg = {"role": "user", "content": task}
        self.state.append(user_msg)

        # 规则文件加载失败：告警但不阻断（用户可能以为规则已生效）
        if self.permissions.load_error:
            await self._emit(callback, {
                "event": "permission_warning",
                "message": self.permissions.load_error,
            })

        # 长会话摘要：run 开始时检查是否有足够的旧消息需要摘要
        # 花 1 次小 LLM 调用，换后续每步不再重发旧历史——净省 token
        await self._maybe_summarize(callback)

        messages = self._build_messages(user_msg)

        # ask / review 模式只暴露只读工具
        readonly_only = self.mode in ("ask", "review")
        tools = get_tool_schemas(readonly_only=readonly_only)

        # 统计：总耗时 + 累加 token 用量
        t_start = time.perf_counter()
        usage_acc = {
            "prompt_tokens": 0, "completion_tokens": 0,
            "total_tokens": 0, "reasoning_tokens": 0,
        }
        step = 0  # 防 except 块 NameError

        # 循环检测：记录最近 3 步的 tool_calls 签名
        step_signatures: list[str] = []

        try:
            # 软上限 limit 会随续跑增大；到软上限若仍有硬上限额度则自动续跑
            limit = self.max_steps
            while True:
                step += 1
                if step > limit:
                    if self.max_steps_total and limit < self.max_steps_total:
                        new_limit = min(limit + self.max_steps,
                                        self.max_steps_total)
                        await self._emit(callback, {
                            "event": "budget_extended",
                            "step": step - 1,
                            "prev_limit": limit,
                            "limit": new_limit,
                        })
                        limit = new_limit
                    else:
                        break
                await self._emit(callback, {"event": "step", "step": step})

                try:
                    assistant = await self.llm.chat(
                        messages=messages,
                        tools=tools,
                        stream=self.stream,
                        on_chunk=functools.partial(self._on_chunk, callback=callback),
                    )
                except LLMError as e:
                    await self._emit(callback, {"event": "error", "message": str(e)})
                    raise

                # 累加该步 token 用量 + 回传每步延迟
                self._accumulate_usage(usage_acc, self.llm.last_usage)
                await self._emit(callback, {
                    "event": "llm_response",
                    "step": step,
                    "latency_s": round(self.llm.last_latency or 0, 2),
                    "usage": self.llm.last_usage,
                })

                messages.append(assistant)
                self.state.append(assistant)

                tool_calls = assistant.get("tool_calls")
                if not tool_calls:
                    # 无 tool_calls → 任务完成
                    content = assistant.get("content") or ""
                    self.state.truncate()
                    await self._emit(callback, {"event": "done", "content": content})
                    await self._emit_stats(callback, step, t_start, usage_acc)
                    return content

                # 循环检测：记录本步 tool_calls 签名
                sig = self._compute_step_signature(tool_calls)
                step_signatures.append(sig)
                if len(step_signatures) > 3:
                    step_signatures.pop(0)
                # 连续 3 步相同签名 → 死循环
                if len(step_signatures) == 3 and len(set(step_signatures)) == 1:
                    await self._emit(callback, {
                        "event": "loop_detected",
                        "step": step,
                        "pattern": sig[:80],
                    })
                    self.state.truncate()
                    await self._emit_stats(callback, step, t_start, usage_acc,
                                           status="loop_detected")
                    return "(agent loop detected)"

                # 执行所有 tool_calls
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    try:
                        args = json.loads(fn["arguments"]) if fn.get("arguments") else {}
                    except json.JSONDecodeError as e:
                        args = {}
                        result = f"Error: invalid arguments JSON: {e}"
                        await self._emit(callback, {
                            "event": "tool_call", "name": name, "args": args
                        })
                        await self._emit(callback, {
                            "event": "tool_result", "name": name, "result": result
                        })
                        tool_msg = {
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": _truncate_tool_result(result),
                        }
                        messages.append(tool_msg)
                        self.state.append(tool_msg)
                        continue

                    await self._emit(callback, {
                        "event": "tool_call", "name": name, "args": args
                    })

                    # L1 风险分类（仅对执行代码的工具）
                    readonly = tool_is_readonly(name)
                    risk = None
                    if name == "run_python" and "code" in args:
                        cls = classify(args["code"])
                        risk = cls["risk"]
                        await self._emit(callback, {
                            "event": "risk_report", "name": name,
                            "risk": cls["risk"], "cmd_type": cls["cmd_type"],
                            "reasons": cls["reasons"],
                        })
                    elif name == "run_command" and "command" in args:
                        # 命令层风险分类（与 run_python 的 AST 分类互补）
                        cls = classify_command(args["command"])
                        risk = cls["risk"]
                        # 仅在命中理由时上报，避免每条普通命令都刷 "risk: low"
                        if cls["reasons"]:
                            await self._emit(callback, {
                                "event": "risk_report", "name": name,
                                "risk": cls["risk"], "cmd_type": cls["cmd_type"],
                                "reasons": cls["reasons"],
                            })

                    # L2 权限决策
                    action = self.permissions.evaluate(
                        name, args, readonly=readonly, risk=risk
                    )
                    if action == ASK and self.permission_handler:
                        action = await self.permission_handler({
                            "tool": name, "args": args,
                            "risk": risk, "proposed": ASK,
                        })
                    await self._emit(callback, {
                        "event": "permission_decision",
                        "name": name, "action": action,
                    })

                    if action != ALLOW:
                        result = (f"Error: permission denied for '{name}' "
                                  f"(action={action})")
                    else:
                        result = execute_tool(name, args, self._ctx)

                    # write_file 覆盖模式产生的 diff：只给人看，不进 tool_msg，
                    # LLM 不见，省 token。在 tool_result 之前 emit，view 先渲染 diff。
                    file_diff = self._ctx.pop("_file_diff", None)
                    if file_diff:
                        await self._emit(callback, {
                            "event": "file_diff", "name": name,
                            "path": file_diff["path"],
                            "old": file_diff["old"], "new": file_diff["new"],
                        })

                    await self._emit(callback, {
                        "event": "tool_result", "name": name, "result": result
                    })

                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": _truncate_tool_result(result),
                    }
                    messages.append(tool_msg)
                    self.state.append(tool_msg)

            # 达到步数上限仍未完成（上下文保留，可在同一会话续跑）
            await self._emit(callback, {"event": "max_steps", "max_steps": limit})
            await self._emit_stats(callback, limit, t_start, usage_acc,
                                   status="max_steps")
            return t(f"(已达本轮步数上限 {limit}，上下文已保留，输入「继续」即可在同一会话接着做)",
                     f"(reached step limit {limit}; context preserved, "
                     f"type 'continue' to resume in the same session)")

        except KeyboardInterrupt:
            # 用户 Ctrl+C 中止：已写入的历史保留（jsonl append 模式），
            # 发 interrupted 事件 + 最终 stats（带 status="interrupted" 标记）
            try:
                await self._emit(callback, {
                    "event": "interrupted",
                    "reason": "user",
                    "step": step,
                    "message": t("用户中止", "user interrupted"),
                })
                self.state.truncate()
                await self._emit_stats(callback, step, t_start, usage_acc,
                                       status="interrupted")
            except BaseException:
                # 防用户连按 Ctrl+C：KeyboardInterrupt 继承自 BaseException，
                # 若用 except Exception 会捕不住，导致第二次中断打断 stats
                # 输出、异常穿透到前端 UI 状态不一致（spinner 泄漏）。
                pass
            return "(user interrupted)"

    @staticmethod
    def _compute_step_signature(tool_calls: list[dict]) -> str:
        """单步 tool_calls 签名：sorted(name + args dict)。
        用于循环检测：连续 3 步完全相同签名 = 死循环。
        """
        if not tool_calls:
            return ""
        parts = []
        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            args_key = json.dumps(args, sort_keys=True, ensure_ascii=False)
            parts.append(f"{name}:{args_key}")
        return "|".join(sorted(parts))

    def _build_messages(self, user_msg: dict) -> list[dict]:
        """构建初始 messages：system（含 context）+ 历史 + 当前 user 任务"""
        system_prompt = self._load_system_prompt()
        messages: list[dict] = [{"role": "system", "content": system_prompt}]

        # 加载历史。若启用 token 预算，走摘要压缩路径；否则用原条数滑窗。
        if self.context_budget is not None:
            hist = self.state.all_messages(
                compress=True, token_budget=self.context_budget
            )
        else:
            hist = self.state.all_messages(compress=True)

        # 摘要 system 消息（[Earlier conversation summary]，由 state 注入）
        # 要紧跟主 system 之后进入 prompt；其他意外 system 一律忽略。
        summary_msg = None
        for msg in hist:
            if msg.get("role") == "system":
                content = msg.get("content") or ""
                if content.startswith("[Earlier conversation summary]") \
                        and summary_msg is None:
                    summary_msg = msg
                continue
            messages.append(msg)
        if summary_msg is not None:
            messages.insert(1, summary_msg)

        return messages

    async def summarize_history(self, callback=None, force: bool = False):
        """摘要压缩当前历史：把最旧一段（默认保留最近 10 条原文）调 LLM
        压成摘要并缓存到 {sid}.summary.json。

        自动触发（_maybe_summarize）与手动命令（前端 /compress）共用入口。
        :param callback: 事件回调（"summary" 事件），可为 None
        :param force: 忽略已有覆盖强制重新生成（/compress all）
        :return: (summary, covered_count) | None（无可压缩内容或摘要失败）
        """
        KEEP_RECENT = 10
        MIN_TO_SUMMARIZE = 5  # 至少 5 条新增历史才值得调一次 LLM

        total = len(self.state.load())
        info = self.state.summary_meta()
        existing = info["covered_count"] if info else 0
        target = max(0, total - KEEP_RECENT)  # 本次应覆盖到 target 条

        # 已有覆盖已到最新边界 → 无新增可压缩内容，跳过（force 则重生成）
        if not force and (target - existing) < MIN_TO_SUMMARIZE:
            return None

        # 待摘要的旧消息：整个待覆盖区间（含已覆盖部分，重生成保证连贯）
        old_msgs = self.state.get_messages_to_summarize(keep_recent=KEEP_RECENT)
        if len(old_msgs) < MIN_TO_SUMMARIZE:
            return None

        # 打包旧消息为文本
        lines = []
        for m in old_msgs:
            role = m.get("role", "?")
            content = m.get("content", "")
            if isinstance(content, list):
                content = json.dumps(content, ensure_ascii=False)
            tc = m.get("tool_calls")
            if tc:
                names = [t.get("function", {}).get("name", "?") for t in tc]
                lines.append(f"[{role}] (tool_calls: {', '.join(names)}) {content}")
            else:
                lines.append(f"[{role}] {content}")

        text = "\n".join(lines)
        # 摘要请求本身也要控制 token：超长则截断
        MAX_SUMMARY_INPUT = 8000
        if len(text) > MAX_SUMMARY_INPUT:
            text = text[:MAX_SUMMARY_INPUT] + "\n...[truncated]..."

        prompt = (
            "Summarize the following conversation history concisely.\n"
            "Focus on: decisions made, files/paths mentioned, errors encountered, "
            "task progress. Keep under 300 tokens.\n\n"
            f"History:\n{text}"
        )

        try:
            result = await self.llm.chat(
                messages=[{"role": "user", "content": prompt}],
                stream=False,
            )
            summary = (result.get("content") or "").strip()
            if summary:
                self.state._save_summary(summary, target, keep_recent=KEEP_RECENT)
                await self._emit(callback, {
                    "event": "summary",
                    "covered_count": target,
                    "summary_tokens": len(summary) // 3,  # 粗估
                })
                return (summary, target)
        except Exception:
            # 摘要失败静默降级：_build_messages 会回退到占位符压缩
            pass
        return None

    async def generate_title(self, force: bool = False,
                             max_zh: int = _TITLE_MAX_ZH,
                             max_en: int = _TITLE_MAX_EN) -> str | None:
        """用 LLM 给当前会话起个短标题并写入元数据（/sessions 靠它识别用途）。

        :param force: 已有标题时是否重新生成（/title 命令用）
        实现见模块级 generate_title_for()——批量迁移历史标题也复用它。
        """
        if not (self.session_manager and self.session_id):
            return None
        return await generate_title_for(
            self.llm, self.session_manager, self.session_id,
            force=force, max_zh=max_zh, max_en=max_en)

    async def _maybe_summarize(self, callback) -> None:
        """自动摘要：仅在 token 预算模式启用；无新增可压缩消息时跳过。

        失败时静默降级（_build_messages 会回退到占位符压缩）。
        """
        if self.context_budget is None:
            return  # 未启用 token 预算模式

        await self.summarize_history(callback=callback)

    def _load_system_prompt(self) -> str:
        """加载 system.txt 并注入 {{context}}，再按模式追加 mode 提示词"""
        # 语言子目录：en 走 prompts/en/；缺失文件回退到根 prompts/
        prompt_dir = PROMPTS_DIR / "en" if get_lang() == "en" else PROMPTS_DIR
        base_path = prompt_dir / "system.txt"
        if not base_path.exists():
            base_path = PROMPTS_DIR / "system.txt"
        if not base_path.exists():
            base = "You are a code agent with tools. Context:\n{{context}}"
        else:
            base = base_path.read_text(encoding="utf-8")

        ctx_text = self.context.render_for_prompt()
        prompt = base.replace("{{context}}", ctx_text)

        mode_name = _MODE_PROMPT.get(self.mode)
        if mode_name:
            mode_path = prompt_dir / f"{mode_name}.txt"
            if not mode_path.exists():
                mode_path = PROMPTS_DIR / f"{mode_name}.txt"
            if mode_path.exists():
                prompt += "\n\n" + mode_path.read_text(encoding="utf-8")

        # 注入当前 todo 状态（如有），让 agent 知道之前规划的进度
        if self.session_manager and self.session_id:
            todos = self.session_manager.get_todos(self.session_id)
            if todos:
                todo_lines = [t("\n## 当前任务进度（由 todo_write 工具规划）",
                                "\n## Current task progress (planned via todo_write tool)")]
                for _item in todos:
                    icon = {"pending": "○", "in_progress": "◐",
                            "completed": "●"}.get(_item.get("status", ""), "○")
                    todo_lines.append(
                        f"{icon} [{_item.get('status', '?')}] {_item.get('content', '')}"
                    )
                todo_lines.append(
                    t("使用 todo_write 工具更新进度（传完整列表替换）。"
                      "完成某项时标记为 completed。",
                      "Use the todo_write tool to update progress (pass the full list to replace). "
                      "Mark an item as completed when done.")
                )
                prompt += "\n" + "\n".join(todo_lines)

        return prompt

    async def _on_chunk(self, chunk: dict, callback) -> None:
        """流式 chunk 转发：只转发 content delta，tool_call_delta 太碎不转发"""
        if not callback:
            return
        if chunk.get("type") == "content":
            await callback({"event": "content_delta", "chunk": chunk["chunk"]})

    async def _emit(self, callback, event: dict) -> None:
        if callback:
            await callback(event)

    @staticmethod
    def _accumulate_usage(acc: dict, usage: dict | None) -> None:
        """把单步 usage 累加进总量（reasoning 是 completion 的子集，不重复计）"""
        if not usage:
            return
        acc["prompt_tokens"] += usage.get("prompt_tokens", 0) or 0
        acc["completion_tokens"] += usage.get("completion_tokens", 0) or 0
        acc["total_tokens"] += usage.get("total_tokens", 0) or 0
        details = usage.get("completion_tokens_details") or {}
        acc["reasoning_tokens"] += details.get("reasoning_tokens", 0) or 0

    async def _emit_stats(self, callback, steps: int, t_start: float,
                          usage_acc: dict, status: str = "done") -> None:
        stats = {
            "event": "stats",
            "steps": steps,
            "elapsed_s": round(time.perf_counter() - t_start, 2),
            "usage": dict(usage_acc),
            "status": status,
        }
        self.last_stats = stats
        await self._emit(callback, stats)
