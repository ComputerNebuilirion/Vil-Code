# VilCode

嵌入终端的**代码 Agent 运行时 / Harness**。本仓库不含模型权重：它是一套把任意 LLM 装配成代码 Agent 的外壳——ReAct 循环、工具集、记忆/状态、安全沙箱与终端 UI 都在这里，模型接口由 `llm.py` 统一抽象。运行中的 `AgentLoop` 实例（harness + 真实模型）才构成一个完整的 Agent，即 **Agent = Model + Harness**。Phase 0：纯 CLI，Agent 内嵌于进程内。

## 架构

八层（原五层 + Session/App/Config 拆出来显式列）：

| 层 | 文件 | 职责 |
|---|---|---|
| Context | `context.py` | 采集工作区路径、文件列表、git 状态 |
| LLM | `llm.py` | OpenAI-compatible chat completions，httpx async，流式 tool_calls 累积，重试/退避 + 流式降级 |
| Tools | `tools/` | `@tool` 装饰器注册中心 + file/search/edit/exec/git/**todo** 工具 |
| Safety | `safety/` | AST 静态检查 + OS 沙箱（Windows Job Object / POSIX rlimit）+ 路径穿越防护 |
| Loop | `loop.py` | ReAct 循环 + tool result 截断 + 循环检测 + KeyboardInterrupt 处理 + `last_stats` 同步写入 + 长会话摘要触发 |
| State | `state.py` | JSONL 对话历史：滑窗 `max_length=30` + L2 工具结果压缩 + 孤立 tool 剔除 + token 预算 + 摘要缓存 |
| Session | `session.py` | `SessionManager`：按 session_uuid 物理隔离历史（`index.json` + `{sid}.jsonl`） |
| App | `agent_app.py` | MVC 共享层：`AgentModel`（装配 LLM/State/Context + `ensure_loop()` 跨事件循环 httpx 复用）+ `TerminalView`（事件渲染 + 权限交互 + spinner + Live/Markdown 流式） |
| Config | `config.py` | 全局配置 `~/.vil/config.json`（`load/save/get/set_value`，与 `DEFAULTS` 深合并） |
| i18n | `i18n.py` | 输出字符串 cn/en 双语：`t()` 立即取值、`L()` 延迟取值、`resolve()` 递归展开 |

## 目录

```
VilAgent/
├── __init__.py        包出口，导入即触发工具注册
├── agent_app.py       MVC 共享层：AgentModel + TerminalView（cli/frontend 入口共用）
├── llm.py             OpenAI-compatible 客户端，含 usage/latency 统计、重试/退避、流式降级
├── state.py           JSONL 对话历史（滑窗 max_length=30 + L2 工具结果压缩 + 孤立 tool 剔除 + token 预算 + 摘要缓存）
├── session.py         SessionManager：按 session_uuid 物理隔离历史
├── context.py         工作区上下文采集
├── loop.py            ReAct 循环 + 统计累加 + 循环检测 + KeyboardInterrupt 处理 + 长会话摘要触发
├── config.py          全局配置 ~/.vil/config.json（load/save/get/set_value，与 DEFAULTS 深合并）
├── i18n.py            轻量 i18n：输出字符串 cn/en 双语（t 立即 / L 延迟 / resolve 展开）
├── safety/
│   ├── ast_check.py   L1 AST 分类
│   ├── permissions.py L2 权限系统
│   ├── sandbox.py     L3 OS 沙箱
│   └── __init__.py
├── tools/
│   ├── __init__.py    @tool / get_tool_schemas / execute_tool
│   ├── file.py        read_file / write_file / delete_file / search_code / list_dir
│   ├── search.py      grep_file（代码库搜索，只读）
│   ├── edit.py        edit_file（patch 模式，局部修改，需 trust）
│   ├── exec.py        run_command（Windows 前置 chcp 65001 修 GBK 乱码；POSIX 走 /bin/sh）
│   ├── python_exec.py run_python（串联 L1→L2→L3）
│   ├── git.py         git_status / git_diff
│   ├── todo.py        todo_write（Agent 自主规划、整列表替换、readonly）
└── prompts/
    ├── system.txt     基础 prompt，含 {{context}}
    ├── ask.txt        ask 模式追加（只读分析/规划）
    ├── do.txt         do 模式追加（执行/改文件）
    └── review.txt     review 模式追加（结构化审查）
```

## 模式

| mode | trust | 工具 | 提示词 |
|---|---|---|---|
| `ask` | False | 只读 | ask |
| `do` | True（默认） | 全部 | do |
| `review` | False | 只读 | review |

`ask` / `review` 只暴露 readonly 工具；`write_file`、`run_command` 被屏蔽。

只读由工具过滤强制；ask 的 prompt 也说明只读（双保险，且 ask/do 姿态分开）。

## 用法

```python
import asyncio
from pathlib import Path
from VilAgent import AgentLoop, Context, LLMClient, State

async def cb(e): print(e)

async def main():
    llm = LLMClient(endpoint="http://localhost:11434/v1", model="qwen2.5:7b")
    state = State(Path.home() / ".vil" / "history.jsonl")
    ctx = Context(workspace=".")
    agent = AgentLoop(llm=llm, state=state, context=ctx, mode="ask", stream=True)
    await agent.run("列出当前目录文件", callback=cb)
    await llm.aclose()

asyncio.run(main())
```

## 配置（全局）

`~/.vil/config.json`，仅全局层（项目层 / init 以后再做）。`config.py` 提供：

```python
from VilAgent import load_config, set_value, get_value, save_config, DEFAULTS

set_value("llm.endpoint", "https://api.deepseek.com/v1")
set_value("llm.model", "deepseek-v4-flash")
set_value("llm.api_key", "sk-...")   # 只存全局，绝不进项目文件
cfg = load_config()                  # 与 DEFAULTS 深合并
```

`DEFAULTS`：`llm.{endpoint,model,api_key,temperature(0.2),max_retries(3),retry_base_delay(0.5)}`、`stream(true)`、`default_mode(ask)`、`max_steps(50)`、`max_steps_total(200)`、`context_budget(8000=历史超此 token 数自动摘要；置 null 关闭)`、`lang(cn)`。

文件只存用户实际设的字段（不把 DEFAULTS 写进文件污染），`load_config` 运行时合并。

依赖：`pip install -r requirements.txt`（httpx + rich + tiktoken；Windows 自动附带 `pywin32`，Linux/macOS 由 `sys_platform` 标记跳过）。L3 沙箱在 Windows 需要 pywin32，在 Linux/macOS 用标准库 `resource`，无需额外安装。

## 输出语言（i18n）

用户可见 / LLM 可见的输出字符串支持 **cn（中文，默认）/ en（英文）** 双语；注释与 docstring 保持中文。`i18n.py` 不引入 key 目录，直接 inline 双语，两套取值方式：

```python
from VilAgent.i18n import set_lang, get_lang, t, L, resolve

t("已取消", "Cancelled")     # 立即求值：运行时用户可见字符串（CLI / frontend / 报错）
L("读取文件", "Read file")    # 延迟求值：import 期构建、运行期才定语言的场景（@tool 的 description/parameters）
resolve(obj)                 # 递归把 dict/list 里的 L 实例替换成当前语言字符串
```

切换语言优先读配置项 `lang`，两个入口启动时 `set_lang(load_config().get("lang"))`：

```bash
vil-agent config set lang en     # 写入 ~/.vil/config.json
```

## 事件回调

`callback` 接收 dict，`event` 字段：

- `start` / `step` / `done` / `max_steps`
- `budget_extended`（到达软上限 `max_steps` 但仍在推进：step / prev_limit / limit，自动续跑一段）
- `content_delta`（流式 token）
- `tool_call`（含 name、args）
- `tool_result`（含 name、result）
- `risk_report`（run_python 的 L1 分类 / run_command 的命令分类：risk / cmd_type / reasons；命令仅在命中理由时上报）
- `permission_decision`（L2 决策：name / action）
- `permission_warning`（规则文件加载失败等权限告警：message；不阻断循环）
- `llm_response`（每步：step / latency_s / usage）
- `stats`（收尾：steps / elapsed_s / usage 累加 + `status` 字段，reasoning 为 completion 子集单列；中断时 status="interrupted"，含已完成步数与部分 token 统计）
- `loop_detected`（连续 3 步相同 tool_calls 签名：step / pattern）
- `interrupted`（用户 Ctrl+C：reason="user" / step / message="用户中止"。REPL 同步化后 Ctrl+C 可靠捕获：提示符时直接回 prompt、执行中显示部分 stats 后回 prompt）
- `file_diff`（write_file 覆盖模式：path / old / new。**只给 view 渲染，不进 tool_msg，LLM 不见，省 token**）
- `summary`（长会话摘要生成完成：covered_count / summary_tokens。仅在 `context_budget` 启用时触发）
- `error`

`stats.status` 取值：`done` / `max_steps` / `loop_detected` / `interrupted`，分别对应终态符号 `✓` / `⚠` / `⚠` / `⛔`。

`file_diff` 由 `write_file`（覆盖模式）/ `delete_file` 写入 `_ctx["_file_diff"]`，`AgentLoop` 在 `execute_tool` 后 `pop` 出来 emit。`TerminalView._on_file_diff` 用 `difflib.unified_diff` + `rich.syntax.Syntax("diff")` 渲染成 `Panel`（cyan 边框）。内容不变时打 `(no changes in <path>)`，超 200 行截断。`write_file` append 模式不发 `file_diff`（追加做 diff 没意义）；`delete_file` 的 diff 全是 `-` 行（new 为空），让人看到删了什么。

## 工具开发

新工具写到 `tools/` 下任意模块，用 `@tool` 装饰：

```python
from . import tool

@tool(
    name="my_tool",
    description="...",
    parameters={"type": "object", "properties": {...}, "required": [...]},
    requires_trust=False,   # True 则需 trust 才能调用
    readonly=True,          # True 则 ask/review 模式可见
)
def my_tool(foo: str, _ctx=None) -> str:
    ws = _ctx["workspace"]
    trust = _ctx["trust"]
    return "..."
```

在 `tools/__init__.py` 末尾加一行 `from . import my_module` 即可注册。

`_ctx` 永远是 kwargs，包含 `workspace` / `trust` / `mode`。

## 安全（三层防御，Windows / Linux / macOS）

参考 Claude Code：AST 不是安全边界（混淆代码永远能绕过字符串匹配），真正隔离靠运行时。

即使有 OS 沙箱，**L2 权限层仍是主要的实际安全边界**（沙箱只限制资源，不限制能访问哪些文件）。

```
┌──────────────────────────────────────────┐
│ L1 预检层（AST 分类，零信任，仅预警）       │  ast_check.py
├──────────────────────────────────────────┤
│ L2 权限层（ask/deny，per-action，跨平台）   │  permissions.py
├──────────────────────────────────────────┤
│ L3 OS 沙箱（Job Object / rlimit，跨平台）    │  sandbox.py
└──────────────────────────────────────────┘
```

- **L1 `classify(code)`** → `{risk: low|medium|high, cmd_type: read|write|network|exec|unknown, reasons: [...]}`。
  高危（`exec/eval/ctypes/win32*`）= 可逃逸沙箱，L3 直接拒；中等（网络/执行/写）= 放行进沙箱并交给 L2 决策。
  `check_safety(code)` 是兼容旧接口，等价于 `classify()["risk"] != "high"`。
  `classify_command(cmd)`（`cmd_check.py`）是 shell 命令版：对 `run_command` 的命令做风险分级，危险模式（`rm -rf`/`del`/`rd /s`/`format`/`mkfs`/`Remove-Item -Recurse`，以及 Linux 专属的 fork 炸弹 / `chmod -R 777 /` / `> /dev/sda` …）判 `high`，网络工具判 `medium`。
- **L2 `PermissionSystem`** → 工具调用前决策 `allow/ask/deny`。
  顺序：readonly 工具放行 → 内置 deny 硬底线（危险命令，**用户规则无法放行**）→ 用户规则命中 → 默认姿态。
  硬底线用 `match_dangerous_command()` 对命令做**规范化（小写+折叠空白）+ 正则 search**，可覆盖大小写（`RM -RF /`）、前缀（`sudo rm -rf /`）、拼接（`echo x && rm -rf /`）以及 Windows/PowerShell（`del`/`rd /s`/`format`/`Remove-Item -Recurse`）、Linux 专属（fork 炸弹 `:(){ :|:& };:`、根目录放开权限 `chmod -R 777 /`、裸块设备写入 `> /dev/sda`/`tee /dev/nvme0n1`）等绕过写法（`exec.py` 复用同一匹配器，避免两套口径不一致）。黑名单混合三类平台条目（Unix / Windows cmd / PowerShell），在各自平台上互为死代码但无害，不按平台分层以保持单一口径。

  **默认姿态（trust 模式 / do 模式）**：写/编辑类工具（`write_file`/`edit_file`）自动 **allow**（低摩擦，自动化友好）；命令/代码执行类（`run_command`/`run_python`）**ask** 人工确认；破坏性工具（`delete_file`）因删除不可逆，亦单独 **ask**；`risk=high` 亦 **ask**。非 trust 模式写/执行一律 **deny**。

  **规则文件**（自动加载，无需传参）：优先 `<workspace>/.vil/permissions.json`，其次 `~/.vil/permissions.json`（`permissions_file` 传空串/None 均走此默认解析）；文件损坏时记录告警（`permission_warning` 事件）并忽略，不阻断循环。
  格式：`{"rules":[{"tool":"run_command","pattern":"git *","action":"allow"}]}`，fnmatch 对 `args["path"]` / `args["command"]` / `args["code"]` **逐个匹配，命中任一即算**。
  例：`{"tool":"run_command","action":"allow"}`（省略 `pattern`）可把该工具所有调用整体放行（充分自动化）；`{"tool":"write_file","pattern":"src/**","action":"deny"}` 可反向收紧。
  `action="ask"` 时调用 `permission_handler`（由调用方提供，如 CLI 交互输入）。
- **L3 `sandbox_run(code, timeout, mem_mb, cwd)`** → 环境清理剥离 API key/secret；默认在系统临时目录建 `sandbox_*` 目录隔离执行，**用后自动清理**（超时/异常路径也会删）；`clean_stale_sandboxes()` 清扫异常退出残留的旧目录。
  - **Windows（有 pywin32）**：Job Object 限制内存/CPU/UI，超时用 `TerminateJobObject` 杀整个 Job（含后代）。
  - **Linux / macOS**：`resource.setrlimit` 限制 `RLIMIT_AS`（内存，默认 256MB，超限时被测代码拿到 `MemoryError` 可自行交代原因）、`RLIMIT_CPU`（CPU 兜底）、`RLIMIT_NOFILE`（句柄数）；`start_new_session=True` + 超时 `killpg` 回收整个进程组（**含孙进程**，只用 `kill()` 会漏）。
  - 两者都不可用（如 Windows 无 pywin32）→ `subprocess + timeout` best-effort，**且在输出里显式告警「mem_mb 被忽略」**，不静默降级。
  - 不设 `RLIMIT_NPROC`：它按 uid 全局计数，会误伤正常 `Popen`（实测 EAGAIN）；防 fork 炸弹交给 cgroup `pids.max`。
  - 已知边界：`RLIMIT_AS` 限的是虚拟地址空间而非 RSS，`mmap` 大文件型负载会比 RSS 更早被拒。bubblewrap / sandbox-exec 的额外 mount 隔离仍 **TODO**。
- **路径**：`is_path_safe(path, workspace)` 防止 `..` 越界。

`run_python` 工具串联三层：L1 分类 → L2 权限 → L3 沙箱执行。

## 状态

> **当前重点 feature：历史记录管理与复用**——本节（多层 token 治理）+ 下文「会话管理」（物理隔离 + 复用机制）合起来构成 Vil 的历史记录系统。设计目标：多轮 ReAct 既要跨轮保留上下文，又要防 token 爆炸。

`State` 用 JSONL 存对话历史，每条一行：

- `append(msg)` 追加
- `load()` 全量读取
- `all_messages(compress=False)` 保留首条 + 最后 `max_length-1` 条。滑窗后若开头是孤立 tool（前面 `assistant with tool_calls` 被切掉），递归丢弃，避免触发 OpenAI API `"tool must be a response to a preceding message with 'tool_calls'"` 错误
- `all_messages(compress=True)` 额外启用 **L2 工具结果压缩**：保留最近 `keep_recent=3` 个 `role="tool"` 消息全文，更早的 tool `content` 替换为 `[tool result cleared, was X chars, tool_call_id=...]` 占位符，防止旧 tool result 长期占用 token。**只在运行时压缩，不影响持久化文件**，`user`/`assistant` 消息不动
- 摘要缓存（`{sid}.summary.json`，手动 `/compress` 或自动摘要写入）：`all_messages()` 有缓存时把最旧 `covered_count` 条历史替换为一条 `[Earlier conversation summary]` system 消息（仅运行时替换，不落盘；`truncate()` 用 `_apply_summary=False` 保证不把合成 system 写回 JSONL）。`covered_count` 被 `truncate` 裁短历史后自动收敛（`_effective_covered`），不会吞掉尾部保留的原文
- `all_messages(token_budget=N)` 启用 **token 预算模式**：用 `tiktoken`（`cl100k_base` encoding）估算每条消息 token，从最新往回累加至预算 80%，超出部分不进 messages。配合摘要缓存：若有 `{sid}.summary.json` 则插入一条 `[Earlier conversation summary]` system 消息替代旧历史。**不删原始消息**，持久化文件保留全部历史
- `truncate()` 落盘截断（仅条数模式用；token 预算模式不删原始消息）
- `clear()` 清空（同时清理 `.summary.json` 缓存）
- `get_messages_to_summarize(keep_recent=10)` 返回需要摘要的旧消息段（供 `AgentLoop._maybe_summarize` 调 LLM 生成摘要）
- `_save_summary(summary, covered_count)` / `_load_summary()` 读写 `{sid}.summary.json` 摘要缓存

默认 `max_length=30`（≈12 个 ReAct 步），平衡上下文保留和 token 控制。system 消息不写入 state，每次按当前 context 重建，避免上下文陈旧。

`AgentLoop._build_messages` 在 `context_budget` 启用时走 `all_messages(compress=True, token_budget=N)`；否则走原条数滑窗。两条路径都会读取摘要缓存，把旧历史替换为 `[Earlier conversation summary]` 摘要 system 消息（紧跟主 system 之后）。`run()` 开始时调 `_maybe_summarize()`：**仅在 `context_budget` 启用时自动触发**，检查是否有未被摘要覆盖的旧消息（至少 5 条新增），调一次 LLM 生成 ≤300 token 的摘要并缓存。`AgentLoop.summarize_history(force=)` 是自动/手动共用的摘要入口——前端 `/compress` 直接调它，即使 `context_budget` 未开也能让摘要生效。摘要失败静默降级为占位符压缩。

### TodoWrite 工具

`todo_write` 是 Agent **自主决策**的规划工具（`readonly=True`，三模式均可用）。使用**整列表替换模式**：LLM 每次传入完整 todos 数组，工具直接覆盖存储。

- 持久化：`{sessions_dir}/{sid}.todo.json`
- `AgentLoop._load_system_prompt()` 运行时自动读取当前 todo 注入 system prompt，Agent 跨轮可见进度
- `SessionManager.get_todos/set_todos/clear_todos` 管理待办文件
- 删除 session 时自动清理 todo 文件
- 前端 banner 渲染 todo 进度（完成数/总数 + 每项图标，最多 5 项）
- `_on_tool_call` 特殊渲染：`todo_write` 调用时展开待办列表（图标 + 状态色），不截断；其他工具仍走 100 字符截断

Schema:

```json
{
  "todos": [
    {"content": "描述", "status": "pending|in_progress|completed"}
  ]
}
```

## 会话管理

`SessionManager`（`session.py`）按 `session_uuid` **物理隔离**对话历史，而非"单文件 + session_id 字段过滤"。

目录结构（默认 `~/.vil/sessions/`，可设 `VIL_SESSIONS_DIR` 环境变量覆盖）：

```
sessions/
├── index.json              全部 session 元数据（list[dict]）
├── {session_id}.jsonl      每个 session 一个对话历史文件
```

`session_id` 是 12 位 hex（`uuid4().hex[:12]`，比完整 uuid 短）。元数据单独存 `index.json`，`list/show` 不必扫所有 jsonl。`State` 类不感知 session，仍只管"对一个 jsonl 文件读写"，`SessionManager.path_for(sid)` 把 sid 映射到 jsonl 路径喂给 `State`。

主要方法：

- `create(mode, task) -> sid`：新建（元数据 + 空白 jsonl）
- `get(sid) / list_all() / update(sid, **fields)`：读取/列表/增量更新（自动刷 `last_active_at`）
- `delete(sid) -> bool`：删元数据 + jsonl 文件
- `delete_all() -> int`：清空所有，`index.json` 置 `[]`，返回删除数
- `last_session_id() -> sid | None`：最近活跃的（按 `last_active_at` 倒序取首）
- `cleanup_empty() -> int`：清理 jsonl 不存在或 size=0 的 session（启动时调用，删"启动后立刻 /exit"残留的空元数据）
- `export_markdown(sid, messages) -> str`：导出 markdown
- `set_title(sid, title) / title_of(sid) / ensure_titles(force=False)`：会话标题（见下）；`force=True` 用本地规则重算已有标题
- `label_of(sid, short=12, title_width=40) -> str`：面向用户的会话标签 `ID · 标题`，无标题退化成纯 ID。**凡是把 session_id 打给用户看的地方（启动续接提示、REPL 提示符、`/switch`、`/delete` 确认、`history show`）都走它**，否则用户记不住 12 位 hex。

**会话标题**：`/sessions` 靠 `title` 字段辨识「这个会话是干嘛的」。REPL 建的会话 task 只是 `(交互式会话)` 这种占位符，无信息量，因此标题单独生成：

1. 首轮 `agent.run()` 结束后，前端调 `AgentLoop.generate_title()` 用 LLM 把开场对话压成单行标题（`/title` 可手动触发，`/title force` 强制重生成）；已有标题则跳过，不重复烧 token。预算：中文 ≤15 字（汉字/英文词各计 1），英文 ≤20 词。
2. LLM 不可用/报错时 `title_from_messages()` 本地兜底：取首条有效 user 消息（跳过占位 task）。**已有标题则保留旧标题**——批量重生成时 LLM 临时挂掉，不会拿本地粗推导覆盖原本更好的标题。
3. 历史遗留的会话没有 `title`，`/sessions` 渲染前调 `ensure_titles()` 本地推导并落盘一次。
4. **老标题批量迁移**：规则升级后旧标题（硬截断的半句话）需要重算——`/title all`（REPL）或 `vil-agent-cli history retitle`（CLI）逐会话调 LLM 重生成，`--dry-run` 预览、`--local` 只走本地规则不烧 token。实现是模块级 `retitle_sessions(llm, sm, only, dry_run, on_result)`，单个会话失败只跳过它。
5. CLI 一次性运行用真实 task 直接当标题（省一次 LLM 调用）。
6. `clean_title()` 统一归一化：压成单行、去首尾引号/`#*`、48 字硬截断兜底（`limit` 可关）。
7. `fit_title()` 负责长度预算，且**只在词组边界收尾**：中文优先切在分句标点（`，。；！？`）处，英文优先切在从句连接词（`and`/`then`/`so`…）处，并剥掉切断后残留的尾部虚词（`的`/`并在`）与悬空英文词（`这个项目在 Linux` → `这个项目`）。另有显示宽度兜底（中文 ≤30 列、英文 ≤40 字符），防止「英文单词按 1 字计」让混排标题放得过长。目标是 `Linux平台兼容` 而不是 `审计代码，现在即将需要进行对Linux的兼容，写…`。

元数据字段：`id / mode / task / created_at / last_active_at / tokens_total / steps_total / status(active|done) / summary / title / last_status(done|interrupted|loop_detected|max_steps)`。

前端 REPL 命令（`vil-agent-frontend.py`）：

| 命令 | 行为 |
|------|------|
| `/new` | `create()` 开新 session（保留旧的） |
| `/switch <sid>` | 切到指定 sid（`State` 重新绑定到新 jsonl） |
| `/delete [sid]` | `delete()`；删当前则 `last_session_id()` 切回上次活跃，没有就 `create()` 新建 |
| `/deleteall` | `delete_all()` + 必须新建一个 session（当前也已被删） |
| `/clear` | `state.clear()` + 元数据 `tokens_total/steps_total/summary` 归零 |
| `/sessions` `/tokens` `/history` | 列表（含 ID/MODE/STEPS/TOKENS/UPDATED/**TITLE**，`*` 标当前会话）/ 显示累计 / 显示最近消息（彩色角色区分：USER/ASSISTANT/TOOL/SYSTEM） |
| `/title [force]` | 用 LLM 生成/重生成当前会话标题（缺标题时自动生成，`force` 强制重生成） |
| `/title all [--dry-run]` | 批量重生成所有会话标题（老标题一次性迁移），`--dry-run` 只预览不写盘 |
| `/compress [all]` | 手动摘要压缩：调 LLM 把当前会话旧历史压成摘要缓存（保留最近 10 条原文），`all` 忽略缓存强制重生成；无需 `context_budget` 即可生效 |
| `/export [sid] [-o f]` | `export_markdown()` 导出为 .md（默认当前会话，支持 `-o` 指定路径） |
| `/config` | 修改 `~/.vil/config.json`（详见下文「配置」） |

启动时 `cleanup_empty()` 自动清理空 session；**frontend 和 CLI 均默认续接上次会话**（`last_session_id()`），`--new` 强制新建，`--continue <sid|last>` 指定续接。CLI 通过 `history list/show/clear/export/retitle` 子命令管理会话（`retitle` 批量重生成标题：`--dry-run` 预览、`--local` 只用本地规则、可选 session ID 前缀只处理一个）。

`<new_task/>` 检测：当 agent 判定当前输入与前面任务明显不相关时，在输出末尾打 `<new_task/>` 标记。`TerminalView` 渲染时静默过滤该标记；前端在 `stats` 事件后检测原始 `content` 是否含 `<new_task/>`，是则提示 `/new` 切换 session（显示当前累计步数/token）。

## MVC 共享层（agent_app.py）

`vil-agent-cli.py` 和 `vil-agent-frontend.py` 共享同一套 Model/View，差异只在交互方式（单次 vs 多轮）：

| 角色 | 类 | 职责 |
|------|---|------|
| Model | `AgentModel` | 装配 LLM/State/Context；`build_agent(mode, trust, stream, max_steps, permission_handler, state, max_steps_total) -> AgentLoop`；`ensure_loop()` 检测事件循环变化 → `LLMClient.recreate_client()` 重建 httpx transport（跨多次 `asyncio.run()` 调用时防 `Event loop is closed`） |
| View | `TerminalView` | 事件渲染 + 权限交互输入（无业务逻辑，不改 agent 状态） |
| Controller | `vil-agent-cli.py` / `vil-agent-frontend.py` | 各自实现参数解析与驱动方式 |

`TerminalView` 关键能力：

- **流式正文**：`Live + Markdown` 增量渲染，**粗体**/列表/代码块实时渲染；非流式回退时在 `done` 事件补打最终答案
- **加载动画**：`step` 事件启动 `rich.status.Status(spinner="dots")` 显示 "agent thinking..."；收到第一个 chunk / tool_call / 错误等立刻停止（`transient=True` 不留残影）
- **圆角 Banner**（仅 frontend）：`Panel(box=ROUNDED, border_style="cyan")` 显示 logo/mode/cwd/model/max_steps；会话信息（sid/steps/tokens/上次任务）放在 Panel 外；快捷提示精简为两行（模式前缀 + `/help`/`/exit`）
- **彩色提示符**（仅 frontend）：`[sid] (mode) >`，sid 黄色、mode 绿色 ask / 红色 do / 蓝色 review。用 Rich markup 而非裸 ANSI，兼容 Windows PowerShell
- **`/history` 角色彩色区分**：user `>` 青色、assistant `<` 品红、tool `#` 暗黄、system `*` dim；工具调用单独一行显示
- **Rich markup `\[tag]` 转义**：所有字面方括号（`[session]`/`[history]` 等）用 `\[xxx]` 转义，避免被 Rich 误解析为 markup 标签而消失
- **`file_diff` 渲染**：`write_file`/`delete_file` 覆盖模式自动生成 unified diff，用 `Syntax("diff")` + `Panel` 渲染给用户（LLM 不见，省 token）
- **`<new_task/>` 静默过滤**：渲染时 `content.replace("<new_task/>", "")`，不显示给用户（前端 repl_loop 检测原始 content 触发提示）
- **权限交互**：`prompt_permission(req)` 弹 `Panel` + `允许执行? [y/N] >`，返回 `allow`/`deny`；EOFError/KeyboardInterrupt 一律 `deny`

`SESSIONS_DIR = Path(os.environ.get("VIL_SESSIONS_DIR") or (Path.home() / ".vil" / "sessions"))`，可设环境变量覆盖（测试/多实例隔离用）。

