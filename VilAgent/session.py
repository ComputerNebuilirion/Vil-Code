"""会话管理：按 session_uuid 物理隔离对话历史

目录结构:
    sessions_dir/
        index.json              # 全部 session 元数据（list[dict]）
        {session_id}.jsonl      # 每个 session 一个对话历史文件

设计要点:
- 物理隔离（按文件分）而非"单文件 + session_id 字段过滤"
- 元数据单独存 index.json，list/show 不必扫所有 jsonl
- State 类不感知 session，仍只管"对一个 jsonl 文件读写"
"""
import json
import re
import time
import unicodedata
import uuid
from pathlib import Path

# 建会话时的占位 task（REPL 场景任务名无意义，标题要从真实对话里推）
_PLACEHOLDER_TASKS = {
    "(交互式会话)", "(interactive session)", "(新会话)", "(new session)",
}

_TITLE_MAX = 48

# 长度预算：中文按「汉字 + 英文词」计 15 个单位，英文按词计 20 个（fit_title 用）
_TITLE_MAX_ZH = 15
_TITLE_MAX_EN = 20

# LLM 常把标题写成列表项/加粗，如 `- "标题"`、`1. 标题`、`**标题**`
_TITLE_MARKER_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+")

# 汉字（含扩展 A 区/兼容区）
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
# 拉丁字母/数字连续段（Linux、GPT-4、v1.2 都算「一个词」）
_LATIN_RUN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+#@/-]*")

# 可以在这些字符处断开：落在标点/空白边界上，标题才是完整词组
# 注：括号类故意不算断点——切在「）」前会留下不平衡的括号
_TITLE_BREAKS = set(" \t，,。.;；:：、!！?？|/·\"'“”‘’—-")
_BREAK_RSTRIP = "".join(sorted(_TITLE_BREAKS))
# 强断点：分句标点，切在这里得到的必然是完整分句
_STRONG_BREAKS = set("，,。.;；:：!！?？、")
# 弱断点：空白/括号/破折号，切在这里是完整词组但不一定完整分句
_WEAK_BREAKS = _TITLE_BREAKS - _STRONG_BREAKS

# 断句后可能悬在尾部的虚词/连接词（多字词必须排在单字之前）
_TRAILING_PARTICLES = (
    "然后", "以及", "并且", "而且", "即将", "将要", "进行", "继续", "开始",
    "的", "了", "并", "且", "与", "及", "或",
)

# 英文标题尾部不该出现的虚词
_EN_TAIL_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "for", "in", "on", "at",
    "with", "about", "by", "from", "as", "that", "is", "are", "was", "be",
    "then", "so", "but", "we", "i", "you", "it", "this",
}
# 英文从句连接词：标题优先在这里断开（"…compatibility and then write a…" -> "…compatibility"）
_EN_CLAUSE_WORDS = {
    "and", "but", "then", "so", "or", "while", "because", "which", "that",
    "when", "where", "after", "before", "plus", "also", "however", "therefore",
}


def clean_title(raw: str, limit: int = _TITLE_MAX) -> str:
    """归一化标题：压成单行、去列表符/引号/装饰符、超长截断

    :param limit: 硬截断长度（字符数，非显示宽度）；传 0 表示不截断
    """
    s = " ".join(str(raw or "").split())
    s = _TITLE_MARKER_RE.sub("", s)
    # 反复剥：`**"标题"**` 这类嵌套装饰，单次 strip 只能去掉最外层
    for _ in range(3):
        before = s
        s = s.strip("#*• \t").strip("\"'`“”‘’「」").strip()
        if s == before:
            break
    if limit and len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def _strip_tail(s: str, keep: int = 3) -> str:
    """剥掉断句后残留的尾部标点/虚词：`对Linux的兼容，` -> `对Linux的兼容`"""
    out = s
    for _ in range(3):
        before = out
        out = out.rstrip(_BREAK_RSTRIP).rstrip()
        for p in _TRAILING_PARTICLES:
            if out.endswith(p) and len(out) - len(p) >= keep:
                out = out[: -len(p)].rstrip()
                break
        if out == before:
            break
    return out


def _zh_units(s: str) -> int:
    """计长度单位：汉字 1 个、英文/数字连续段 1 个，标点与空白不计"""
    units = 0
    idx, n = 0, len(s)
    while idx < n:
        m = _LATIN_RUN_RE.match(s, idx)
        if m:
            units += 1
            idx = m.end()
            continue
        if s[idx] not in _TITLE_BREAKS:
            units += 1
        idx += 1
    return units


def _display_width(s: str) -> int:
    """按终端列宽计（CJK 记 2 列），用于给中英混排标题兜住显示长度"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)


# 悬空英文词前面的中文虚词/连接词：`缺失并在 title` -> `缺失`
_DANGLING_LEAD_CHARS = set("在并和与对给把是的了用改加到将需")

_DANGLING_LATIN_RE = re.compile(r"[ \t]*([A-Za-z][A-Za-z0-9_.+#/-]*)$")


def _drop_dangling_latin(h: str, budget: int) -> str:
    """去掉尾部悬空的英文词：`这个项目在 Linux` -> `这个项目`

    只在「英文词前紧挨着 1~2 个中文虚词」且「剥掉后仍有实质内容」时生效，
    否则会误伤 `修掉 loop.py`、`把 sandbox 改成 Linux` 这类正常标题。
    """
    m = _DANGLING_LATIN_RE.search(h)
    if not m:
        return h
    before = h[: m.start()].rstrip()
    lead = ""
    while before and len(lead) < 2 and before[-1] in _DANGLING_LEAD_CHARS:
        lead = before[-1] + lead
        before = before[:-1].rstrip()
    if not lead or not before:
        return h
    if _zh_units(before) < max(3, budget // 2):
        return h
    return _strip_tail(before, keep=2) or h


def _fit_zh(s: str, budget: int) -> str:
    """中文标题：优先切在分句标点处（得到完整分句），其次才按预算硬切

    双重约束：单位数（汉字/英文词各 1）≤ budget，且显示宽度 ≤ budget*2 列。
    后者防止 `修掉 loop.py 的 i18n 缺失…` 这类混排标题按「词计 1」放行后过长。
    """
    width_budget = budget * 2
    if _zh_units(s) <= budget and _display_width(s) <= width_budget:
        return s

    soft_units, soft_width = budget + max(3, budget // 3), width_budget + 8
    strong: list[tuple[int, int, int]] = []   # (cut_index, units_before, width_before)
    weak: list[tuple[int, int, int]] = []
    units = width = idx = 0
    n = len(s)
    while idx < n:
        m = _LATIN_RUN_RE.match(s, idx)
        if m:
            add_u, step = 1, m.end() - idx
            add_w = _display_width(s[idx: m.end()])
        else:
            ch = s[idx]
            add_u = 0 if ch in _TITLE_BREAKS else 1
            add_w = _display_width(ch)
            if ch in _STRONG_BREAKS:
                strong.append((idx, units, width))
            elif ch in _WEAK_BREAKS:
                weak.append((idx, units, width))
            step = 1
        if units + add_u > soft_units or width + add_w > soft_width:
            break
        units, width, idx = units + add_u, width + add_w, idx + step
    hard_cut = idx

    def _head(cut: int) -> str:
        h = _strip_tail(s[:cut], keep=2) or s[:cut].strip()
        return _drop_dangling_latin(h, budget) or h

    # 1) 分句优先：第一个「有实质内容（≥3 单位）」的强断点，切出来就是完整分句
    for cut, u, _ in strong:
        if u >= 3:
            return _head(cut)
    # 2) 退一步：预算内最后一个强断点
    fits = [(cut, u) for cut, u, w in strong if u <= budget and w <= width_budget]
    if fits and fits[-1][1] >= 2:
        return _head(fits[-1][0])
    # 3) 弱断点（空白/破折号）：只接受用掉一半以上预算的，避免标题被砍得过短
    usable = [cut for cut, u, _ in weak if u >= max(2, budget // 2)]
    if usable:
        return _head(usable[-1])
    return _head(hard_cut) or s


def _fit_en(s: str, budget: int, max_width: int = 40) -> str:
    """英文标题：优先切在从句连接词处（得到完整名词短语），其次按词数硬切

    :param budget: 词数上限（默认 20 词）
    :param max_width: 字符数软上限——20 个词往往是 100+ 字符，早就不像标题了，
        这里对齐 `label_of(title_width=40)` 的显示宽度，避免存下来又被截一次
    """
    words = s.split()
    if len(words) <= budget and len(s) <= max_width:
        return s
    limit = min(budget, len(words))

    def _finish(seq: list[str]) -> str:
        keep = list(seq)
        while len(keep) > 2 and keep[-1].strip(",.;:!?").lower() in _EN_TAIL_STOP:
            keep.pop()
        return " ".join(keep).rstrip(",.;:!?—-").strip()

    # 1) 从句连接词：`... compatibility and then write a ...` -> `... compatibility`
    for i in range(2, limit):
        if words[i].strip(",.;:!?").lower() in _EN_CLAUSE_WORDS:
            head = _finish(words[:i])
            if head:
                return head
    # 2) 预算内的从句标点
    for i in range(limit - 1, max(1, limit // 2) - 1, -1):
        if words[i][-1] in ",;:—-":
            head = _finish(words[: i + 1])
            if head:
                return head
    # 3) 兜底：按词数取满，再按字符宽度整词退让（绝不切半个词/丢掉关键修饰词）
    keep = words[:limit]
    while len(keep) > 2 and len(" ".join(keep)) > max_width:
        keep.pop()
    return _finish(keep) or " ".join(keep)


def fit_title(raw: str, zh_budget: int = _TITLE_MAX_ZH,
              en_budget: int = _TITLE_MAX_EN) -> str:
    """把标题裁到「完整词组」长度：中文 ≤15 字、英文 ≤20 词。

    与 clean_title 的分工：clean_title 只做清洗（单行/去装饰/48 字硬截断兜底），
    fit_title 负责长度预算，并在标点、词边界处收尾，避免出现
    `审计代码，现在即将需要进行对Linux的兼容，写…` 这种半句话标题。
    """
    s = clean_title(raw, limit=0)  # 不在这里硬截断，交给词/标点边界逻辑
    if not s:
        return ""
    if not _CJK_RE.search(s):
        return _fit_en(s, en_budget) or s
    return _fit_zh(s, zh_budget) or s


def truncate_display(text: str, width: int = 44) -> str:
    """按终端显示宽度截断（CJK 按 2 列算），避免表格列换行"""
    out: list[str] = []
    used = 0
    for ch in str(text or ""):
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + w > width:
            return "".join(out) + "…"
        out.append(ch)
        used += w
    return "".join(out)


def title_from_messages(messages: list[dict], fallback: str = "") -> str:
    """从对话历史本地推导标题（不调 LLM）：取第一条有效 user 消息首行。

    用于 LLM 不可用/未生成的兜底，保证 /sessions 永远有东西可显示。
    """
    for m in messages:
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                str(seg.get("text", "")) if isinstance(seg, dict) else str(seg)
                for seg in content
            )
        text = " ".join(str(content or "").replace("<new_task/>", " ").split())
        if not text or text in _PLACEHOLDER_TASKS:
            continue
        return fit_title(text)
    # fallback（通常是建会话时的 task）也可能是占位符，此时视为无标题
    fb = clean_title(fallback)
    return "" if fb in _PLACEHOLDER_TASKS else fit_title(fb)


class SessionManager:
    """会话生命周期管理（不持有 State，仅管元数据 + 文件路径）"""

    def __init__(self, sessions_dir: Path | str):
        self.dir = Path(sessions_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "index.json"
        if not self.index_path.exists():
            self.index_path.write_text("[]", encoding="utf-8")
        # "当前会话"指针文件：记录最后一次使用的 session_id，
        # 用于重启后精确恢复（/switch 切换的会话 last_active_at 不变，
        # 靠时间排序会恢复错）
        self.current_path = self.dir / ".current"

    # —— index.json 读写 ——

    def _load_index(self) -> list[dict]:
        try:
            data = self.index_path.read_text(encoding="utf-8") or "[]"
            return json.loads(data)
        except (json.JSONDecodeError, OSError):
            return []

    def _save_index(self, items: list[dict]) -> None:
        self.index_path.write_text(
            json.dumps(items, ensure_ascii=False, indent=2),
            encoding="utf-8")

    # —— 生命周期 ——

    def create(self, mode: str, task: str) -> str:
        """新建 session，返回 session_id（12 位 hex，避免 uuid4 太长）"""
        session_id = uuid.uuid4().hex[:12]
        now = time.time()
        item = {
            "id": session_id,
            "mode": mode,
            "task": (task or "")[:200],
            "created_at": now,
            "last_active_at": now,
            "tokens_total": 0,
            "steps_total": 0,
            "status": "active",
            "summary": "",
            # 占位 task（REPL）不给标题，等首轮跑完由 LLM 生成；
            # 一次性运行（CLI）用真实 task 直接兜底，省一次 LLM 调用
            "title": ("" if (task or "").strip() in _PLACEHOLDER_TASKS
                      else clean_title(task)),
        }
        items = self._load_index()
        items.append(item)
        self._save_index(items)
        # 创建空白对话文件
        self.path_for(session_id).touch()
        return session_id

    def path_for(self, session_id: str) -> Path:
        """返回该 session 的 jsonl 路径（供 State 使用）"""
        return self.dir / f"{session_id}.jsonl"

    def todo_path(self, session_id: str) -> Path:
        """返回该 session 的 todo 文件路径"""
        return self.dir / f"{session_id}.todo.json"

    def get_todos(self, session_id: str) -> list:
        """读取待办清单；返回空列表（若无文件或解析失败）"""
        p = self.todo_path(session_id)
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def set_todos(self, session_id: str, todos: list) -> None:
        """写入待办清单"""
        p = self.todo_path(session_id)
        p.write_text(
            json.dumps(todos, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def clear_todos(self, session_id: str) -> None:
        """删除待办文件"""
        p = self.todo_path(session_id)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass

    def get(self, session_id: str) -> dict | None:
        for it in self._load_index():
            if it["id"] == session_id:
                return it
        return None

    def list_all(self) -> list[dict]:
        """按 last_active_at 倒序返回"""
        items = self._load_index()
        return sorted(items,
                      key=lambda x: x.get("last_active_at", 0),
                      reverse=True)

    def update(self, session_id: str, **fields) -> None:
        """增量更新元数据；自动刷新 last_active_at"""
        items = self._load_index()
        for it in items:
            if it["id"] == session_id:
                it.update(fields)
                if "last_active_at" not in fields:
                    it["last_active_at"] = time.time()
                break
        self._save_index(items)

    # —— 标题 ——

    def load_messages(self, session_id: str, limit: int = 0) -> list[dict]:
        """直接读 jsonl 消息（limit=0 不限条数）；坏行/IO 失败跳过"""
        p = self.path_for(session_id)
        out: list[dict] = []
        try:
            with p.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict):
                        out.append(item)
                    if limit and len(out) >= limit:
                        break
        except OSError:
            pass
        return out

    def set_title(self, session_id: str, title: str) -> str:
        """写入 session 标题（/sessions 用于识别会话用途），返回落库的标题"""
        t = clean_title(title)
        if t:
            self.update(session_id, title=t)
        return t

    def title_of(self, session_id: str) -> str:
        """返回标题；缺标题时用对话历史兜底推导（不写盘）"""
        meta = self.get(session_id) or {}
        title = clean_title(meta.get("title", ""))
        if title:
            return title
        return title_from_messages(self.load_messages(session_id, limit=12),
                                   meta.get("task", ""))

    def label_of(self, session_id: str, short: int = 12,
                 title_width: int = 40) -> str:
        """会话的可读标签：`ID · 标题`，无标题时退化为纯 ID。

        用户记不住 12 位 hex，凡是把 session_id 打给用户看的地方（启动续接提示、
        /switch、/delete 确认等）都应该走这里，用标题帮忙辨认是哪个会话。
        :param short: ID 保留前几位（0 = 完整 ID）
        :param title_width: 标题按显示宽度截断（CJK 算 2 列），避免长标题撑爆一行
        """
        sid = str(session_id or "")
        if not sid:
            return ""
        short_id = sid[:short] if short and short > 0 else sid
        title = truncate_display(self.title_of(sid), title_width)
        return f"{short_id} · {title}" if title else short_id

    def ensure_titles(self, limit: int = 12, force: bool = False) -> int:
        """给历史遗留的、缺标题的会话补标题（本地推导后写盘），返回补了几个

        :param force: 已有标题也重算（用本地规则把老的硬截断标题修成完整词组）
        """
        fixed = 0
        for it in self.list_all():
            if not force and clean_title(it.get("title", "")):
                continue
            title = title_from_messages(
                self.load_messages(it["id"], limit=limit), it.get("task", ""))
            if title:
                self.update(it["id"], title=title)
                fixed += 1
        return fixed

    def delete(self, session_id: str) -> bool:
        """删除 session 元数据 + 对话文件 + todo 文件"""
        items = self._load_index()
        new_items = [it for it in items if it["id"] != session_id]
        if len(new_items) == len(items):
            return False
        self._save_index(new_items)
        p = self.path_for(session_id)
        if p.exists():
            p.unlink()
        self.clear_todos(session_id)
        # 删的是当前会话 → 清掉指针
        try:
            if self.current_path.read_text(encoding="utf-8").strip() == session_id:
                self.current_path.unlink()
        except OSError:
            pass
        return True

    # —— 当前会话指针 ——

    def set_current(self, session_id: str) -> None:
        """持久化"当前会话"指针。

        在启动续接、/new、/switch 等任何"当前会话变化"的时刻调用，
        而非仅在退出时记录——进程崩溃/强杀时指针依然正确。
        """
        self.current_path.write_text(session_id, encoding="utf-8")

    def get_current(self) -> str | None:
        """读取当前会话指针；指向的会话已不存在则视为失效，返回 None"""
        try:
            sid = self.current_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return sid if sid and self.get(sid) else None

    def last_session_id(self) -> str | None:
        items = self.list_all()
        return items[0]["id"] if items else None

    def delete_all(self) -> int:
        """删除所有 session 元数据 + 对话文件 + todo 文件，返回删除数量。
        index.json 被清空为 []，但保留文件本身。
        """
        items = self._load_index()
        removed = len(items)
        for it in items:
            sid = it["id"]
            p = self.path_for(sid)
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
            self.clear_todos(sid)
        self._save_index([])
        self.current_path.unlink(missing_ok=True)
        return removed

    def cleanup_empty(self) -> int:
        """清理空 session（jsonl 不存在或 size=0）。

        启动时调用，删掉那些"启动后立刻 /exit"残留的空 session 元数据。
        返回清理的 session 数。
        """
        items = self._load_index()
        survivors = []
        removed = 0
        for it in items:
            sid = it["id"]
            p = self.path_for(sid)
            # jsonl 不存在或 size=0 → 删元数据 + 删空文件
            if not p.exists() or p.stat().st_size == 0:
                if p.exists():
                    try:
                        p.unlink()
                    except OSError:
                        pass
                removed += 1
                continue
            survivors.append(it)
        if removed:
            self._save_index(survivors)
            # 指针指向的会话刚被清掉 → 失效指针一并清理
            try:
                cur = self.current_path.read_text(encoding="utf-8").strip()
                if cur and cur not in {it["id"] for it in survivors}:
                    self.current_path.unlink()
            except OSError:
                pass
        return removed

    # —— 导出 ——

    def export_markdown(self, session_id: str,
                        messages: list[dict]) -> str:
        meta = self.get(session_id) or {}
        ts = meta.get("created_at", 0)
        created = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
                   if ts else "?")
        lines = [
            f"# Session {session_id}",
            f"- mode: `{meta.get('mode', '?')}`",
            f"- task: {meta.get('task', '')}",
            f"- created: {created}",
            f"- steps: {meta.get('steps_total', 0)}",
            f"- tokens: {meta.get('tokens_total', 0)}",
            "",
        ]
        for m in messages:
            role = m.get("role", "?")
            content = m.get("content", "")
            if isinstance(content, list):
                content = json.dumps(content, ensure_ascii=False)
            content = str(content)
            lines.append(f"## [{role}]")
            if role == "tool":
                # 工具输出是原始文本（含 | # 等 markdown 特殊字符），
                # 用围栏代码块包裹，避免被 markdown 渲染器误解析。
                # 自适应围栏长度：比内容中最长的连续反引号多一个
                runs = re.findall(r"`+", content)
                fence = "`" * (max([len(r) for r in runs], default=0) + 3)
                lines.append(fence)
                lines.append(content)
                lines.append(fence)
            else:
                lines.append(content)
            lines.append("")
        return "\n".join(lines)
