"""命令风险识别：shell 命令危险模式匹配 + 粗粒度风险分级

与 ast_check（针对 Python 源码）互补：本模块针对 run_command 的 shell 命令。

用「规范化 + 正则 search」而非整串 fnmatch，可覆盖原先漏掉的绕过写法：
  - 前缀：`sudo rm -rf /`
  - 拼接：`echo hi && rm -rf /`
  - 大小写：`RM -RF /`
  - Windows/PowerShell 破坏性命令：`rd /s /q`、`rmdir /s`、`Remove-Item -Recurse`

本层是「硬底线」判定（配合 L2 permissions 的不可放行 deny），不追求完备——
真正的隔离仍靠 L3 沙箱（Windows Job Object / POSIX rlimit）。

黑名单混合了三类平台条目（Unix / Windows cmd / PowerShell），互相为死代码但
无害：Linux 上 Unix 段生效，Windows 上 cmd/PowerShell 段生效。
"""
import re

from ..i18n import t

__all__ = ["normalize_command", "match_dangerous_command", "classify_command",
           "safe_command", "split_command_segments"]


def normalize_command(cmd: str) -> str:
    """小写 + 折叠空白（含换行），便于跨越前缀/拼接做子串式匹配"""
    return re.sub(r"\s+", " ", str(cmd).strip().lower())


# (标签, 正则) —— 对规范化后的命令做 re.search（命中任意位置即算）
# 注意：`format`/`del`/`rd` 等只在「命令起始位置」（行首或 ; & | 之后）匹配，
# 避免误伤 `git log --format=...` 这类把关键词当参数/子串的常见命令。
_DANGEROUS_COMMAND_PATTERNS: list[tuple[str, str]] = [
    # ---- Unix 破坏性删除 ----
    ("rm -r/-f", r"\brm\s+(?:-[a-z]*[rf][a-z]*|--(?:recursive|force|no-preserve-root))\b"),
    ("rm /", r"\brm\s+(?:-\S+\s+)*(?:/|~|\*|/\*)(?:\s|$|&|;|\|)"),
    ("sudo rm", r"\bsudo\s+(?:-\S+\s+)*rm\b"),
    ("shred", r"\bshred\b"),
    ("mkfs", r"\bmkfs(?:\.\w+)?\b"),
    ("dd of=", r"\bdd\b.*?\bof="),
    # ---- Linux 专属强化（硬底线，不求完备）----
    # fork 炸弹 :(){ :|:& };:
    ("fork bomb", r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
    # 把根目录权限开成人人可写：chmod -R 777 /
    ("chmod 777 /", r"\bchmod\s+(?:-\S+\s+)*(?:777|a\+rwx)\s+(?:-\S+\s+)*/(?:\s|$|;|&|\|)"),
    # 直接写裸块设备（绕过文件系统毁数据）：> /dev/sda
    ("raw disk write", r">\s*/dev/(?:sd|hd|vd|xvd|nvme|mmcblk|disk)"),
    ("tee to disk", r"\btee\s+(?:-\S+\s+)*/dev/(?:sd|hd|vd|xvd|nvme|mmcblk|disk)"),
    # ---- Windows cmd ----
    ("del", r"(?:^|[;&|]\s*)(?:del|erase)\b"),
    ("rd /s", r"(?:^|[;&|]\s*)r(?:d|mdir)\b.*?\s/s\b"),
    ("format", r"(?:^|[;&|]\s*)format\b"),
    ("cipher /w", r"(?:^|[;&|]\s*)cipher\b.*?\s/w\b"),
    # ---- PowerShell ----
    ("Remove-Item", r"\bremove-item\b.*?(?:-recurse|-force)"),
    ("Format-Volume", r"\bformat-volume\b"),
    ("Clear-Disk", r"\bclear-disk\b"),
]

# 含网络工具的命令：粗判为 medium（仅提示，不改变默认决策）
_NETWORK_KEYWORDS = (
    "curl ", "wget ", "ssh ", "scp ", "rsync ", "sftp ", "nc ", "telnet ",
    "invoke-webrequest", "invoke-restmethod",
)


def match_dangerous_command(cmd: str) -> str | None:
    """命中危险命令模式则返回模式标签，否则返回 None"""
    if not cmd:
        return None
    norm = normalize_command(cmd)
    for label, pattern in _DANGEROUS_COMMAND_PATTERNS:
        if re.search(pattern, norm):
            return label
    return None


def classify_command(cmd: str) -> dict:
    """粗粒度风险分级，返回 {"risk", "cmd_type", "reasons"}（与 ast_check.classify 对齐）

    risk: low | medium | high
    cmd_type: read | write | delete | network | exec | unknown（判定见下方
    `_command_type`，依赖白名单表；只用于展示，不参与放行判定）
    """
    label = match_dangerous_command(cmd)
    ctype = _command_type(cmd, label)
    if label:
        return {
            "risk": "high",
            "cmd_type": "exec",
            "reasons": [t(f"匹配危险命令模式: {label}",
                          f"matches dangerous command pattern: {label}")],
        }
    if ctype == "network":
        return {
            "risk": "medium",
            "cmd_type": "network",
            "reasons": [t("命令含网络工具", "command uses a network tool")],
        }
    if ctype == "delete":
        return {
            "risk": "medium",
            "cmd_type": "delete",
            "reasons": [t("命令会删除文件", "command deletes files")],
        }
    return {"risk": "low", "cmd_type": ctype, "reasons": []}


# ---- 「已证明只读」的命令判定（正向白名单，fail-closed）----
#
# 参与放行决策：permissions.evaluate() 在 balanced/strict 下用它把
# 「只读探查命令」视同只读工具直接放行（yolo 本就全放行，不需要它）。
#
# 与 classify_command 的黑名单语义分开，理由同 ast_check.safe_python：
#   classify_command().risk == "low"  → 「没发现危险」（展示/提示用）
#   safe_command()                    → 「已证明只读」（正向证明）
#
# 比 Python 侧更严，因为 **run_command 完全不过沙箱**（sandbox 仅用于 run_python）：
# L2 权限是它唯一的边界，一次误放行等于以当前用户身份直连执行。
# 因此这里只收「只读探查」：任何可能写仓库、装依赖、联网或执行任意代码的命令
# （python/pytest/npm/pip/curl/sed -i/tee/find -exec/xargs…）都不在名单内。
# 将来新增危险命令不必改这里：不在白名单里就天然不通过。
#
# `date` 刻意不在名单内：POSIX 无参数时只打印时间，但 Windows cmd 的 `date`
# 是**交互式改系统日期**命令（PowerShell 下才等价于 Get-Date）。同一命令名
# 跨平台语义相反时，只能按危险的一侧处理。
SAFE_COMMANDS = frozenset({
    "ls", "dir", "pwd", "echo", "cat", "type", "head", "tail", "wc",
    "sort", "uniq", "grep", "findstr", "which", "where", "whoami",
    "uname", "more", "tree",
})

# 白名单命令里能写文件的参数：命令本身只读，选项却给了写出口 → 逐参数拦
# （sort -o out.txt / tree -o out.txt 都是真的写盘，且不带 shell 元字符）
_READONLY_CMD_WRITE_ARGS = {
    "sort": ("-o", "--output"),
    "tree": ("-o",),
}

# git 只放只读子命令
SAFE_GIT_SUBCOMMANDS = frozenset({
    "status", "log", "diff", "show", "rev-parse", "describe",
    "ls-files", "blame", "shortlog",
})

# branch/remote 携带写语义（-D 删分支、remote add 改配置）→ 只认这几个参数
_GIT_SUB_ALLOWED_ARGS = {
    "branch": frozenset({"-l", "--list", "-a", "-r", "-v", "-vv",
                         "--show-current", "--all", "--remotes"}),
    "remote": frozenset({"-v", "--verbose", "show"}),
}

# git 里能执行任意程序的参数（-c alias.x=!cmd、--exec-path、--output 写文件）
_GIT_UNSAFE_ARGS = frozenset({"-c", "--exec-path", "--upload-pack",
                              "--output", "--ext-diff"})

# —— classify_command 用的首词分类表（仅供展示，不参与放行判定）——
# 删除类：不可逆，单独成档
_DELETE_COMMANDS = frozenset({
    "rm", "rmdir", "rd", "unlink", "del", "erase", "shred",
})
# 写类：创建/改名/改权限/复制
_WRITE_COMMANDS = frozenset({
    "touch", "mkdir", "cp", "mv", "tee", "chmod", "chown", "ln", "dd",
    "copy", "move", "ren", "rename", "xcopy", "truncate",
})
# 包装词：跳过它们看真正的命令首词（sudo rm x → rm）
_PREFIX_WORDS = frozenset({
    "sudo", "time", "nohup", "env", "nice", "command", "exec",
})

# —— git 子命令归类（classify_command 的 cmd_type 用）——
# 放行判定与这些表无关，仍走 safe_command → _safe_git（fail-closed）；
# 本表只保证「报出来的类型」不撒谎：`git push` 不是 read、`git clean -fdx`
# 不是 read。此前把所有 git 一律记 read，等于让「不可逆」命令在展示层伪装成
# 只读操作——一旦有人拿 cmd_type 当可逆性/放行依据就会直接误放行。
_GIT_DELETE_SUBCOMMANDS = frozenset({      # 不可恢复：删工作区文件或对象
    "clean", "prune", "gc", "rm", "filter-branch",
})
_GIT_NETWORK_SUBCOMMANDS = frozenset({     # 与远端交互
    "clone", "fetch", "pull", "push", "ls-remote", "submodule",
})
_GIT_WRITE_SUBCOMMANDS = frozenset({       # 改本地仓库/工作区，git 本身可回退
    "add", "commit", "checkout", "switch", "restore", "stash", "merge",
    "rebase", "reset", "cherry-pick", "revert", "tag", "mv", "apply", "am",
    "config", "init", "branch", "notes", "worktree", "lfs", "remote",
})
# 把「可回退的写」变成「不可恢复的删」的参数（reset --hard / checkout -f / branch -D）
_GIT_FORCE_FLAGS = frozenset({
    "-f", "--force", "-d", "-D", "--hard", "--delete", "--discard-changes",
})

# 命令拼接/管道：只看首段会把 `git status; git push` 误判成只读
_CHAIN_RE = re.compile(r";|&&|\|\|")


def _git_cmd_type(args: list[str]) -> str:
    """git 子命令的操作类型；认不出就 unknown（不猜成 read）"""
    if _safe_git(args)[0]:
        return "read"
    lowered = [a.lower() for a in args]
    sub = next((a for a in lowered if not a.startswith("-")), "")
    if not sub:
        return "unknown"
    if sub in _GIT_DELETE_SUBCOMMANDS:
        return "delete"
    # stash drop/clear 丢掉的是不可恢复的暂存，其余 stash 操作可回退
    if sub == "stash" and any(a in ("drop", "clear") for a in lowered):
        return "delete"
    if sub in _GIT_NETWORK_SUBCOMMANDS:
        return "network"
    # remote add/set-url 只改本地 .git/config；remote update 才会访问远端
    if sub == "remote" and any(a == "update" for a in lowered):
        return "network"
    if sub in _GIT_WRITE_SUBCOMMANDS:
        if any(a.split("=", 1)[0] in _GIT_FORCE_FLAGS for a in lowered):
            return "delete"
        return "write"
    return "unknown"


def _command_type(cmd: str, dangerous_label: str | None) -> str:
    """命令的操作类型：read | write | delete | network | exec | unknown

    纯展示用途。`unknown` 表示「首词不在任何表里」，既不等于安全也不等于危险。
    """
    if dangerous_label:
        return "exec"
    norm = normalize_command(cmd)
    if any(k in norm for k in _NETWORK_KEYWORDS):
        return "network"
    tokens = norm.replace(";", " ").replace("&&", " ").replace("||", " ").split()
    idx = next((i for i, w in enumerate(tokens) if w not in _PREFIX_WORDS), -1)
    exe = tokens[idx].strip("'\"") if idx >= 0 else ""  # 兼容 "rm" / 'rm' 带引号写法
    if not exe:
        return "unknown"
    if exe == "git":
        # 拼接命令（git status; git push）只看首段会误判成只读 → 保守归 unknown
        if _CHAIN_RE.search(str(cmd)):
            return "unknown"
        return _git_cmd_type(tokens[idx + 1:])
    if exe in _DELETE_COMMANDS:
        return "delete"
    if exe in _WRITE_COMMANDS or ">" in norm:
        return "write"
    if exe in SAFE_COMMANDS:
        return "read"
    return "unknown"


# 命令分隔符：长串在前，避免 `|` 抢先吃掉 `||`
_CMD_SEPARATORS = ("&&", "||", "|", ";", "&")


def split_command_segments(cmd: str) -> list[str] | None:
    """按 shell 分隔符把命令切成若干段（引号内的分隔符不切）。

    返回 None 表示**无法安全解析**，调用方必须按「未证明只读」处理：
      - 重定向 `<` `>`、命令替换 `$` 和反引号、子 shell / 分块 `( ) { }`、换行
      - 引号未闭合、双引号内的 `$` / 反引号展开
    这些写法要么能写文件、要么要再套一层解释器才能算出真实命令，
    静态证明的成本远高于收益，一律退回人工确认。

    :param cmd: 原始命令字符串
    :return: 各段命令的列表；无法安全解析时返回 None
    """
    s = str(cmd or "")
    if not s.strip():
        return None
    segs: list[str] = []
    buf: list[str] = []
    quote = ""
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if quote:
            if ch == quote:
                quote = ""
            elif quote == '"' and ch in "$`":
                return None          # 双引号内仍会做命令替换
            buf.append(ch)
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch in "$`(){}<>" or ch in "\n\r":
            return None
        sep = next((x for x in _CMD_SEPARATORS if s.startswith(x, i)), "")
        if sep:
            segs.append("".join(buf))
            buf = []
            i += len(sep)
            continue
        buf.append(ch)
        i += 1
    if quote:
        return None
    segs.append("".join(buf))
    return [seg for seg in segs if seg.strip()] or None


def _safe_single(raw: str) -> tuple[bool, str]:
    """单段命令（已切掉分隔符）的只读证明"""
    parts = raw.split()
    if not parts:
        return False, t("空命令段", "empty command segment")
    exe = parts[0].lower().strip("'\"")
    if not exe:
        return False, t("空命令段", "empty command segment")
    args = [p.strip("'\"") for p in parts[1:]]
    if exe == "git":
        return _safe_git(args)
    if exe not in SAFE_COMMANDS:
        return False, t(f"命令未列入只读白名单: {exe}",
                        f"command not allowlisted as read-only: {exe}")
    flags = _READONLY_CMD_WRITE_ARGS.get(exe)
    if flags:
        for arg in args:
            low = arg.lower()
            for f in flags:
                if low.startswith("--"):
                    # 长选项：--output / --output=x
                    hit = low == f or low.startswith(f + "=")
                elif low.startswith("-"):
                    # 短选项可能合并书写（sort -ro out.txt == sort -r -o out.txt）
                    hit = f[1:] in low[1:].split("=", 1)[0]
                else:
                    hit = False
                if hit:
                    return False, t(f"{exe} 的 {f} 参数会写文件",
                                    f"{exe} {f} writes a file")
    return True, t("只读探查命令", "read-only inspection command")


def safe_command(cmd: str) -> tuple[bool, str]:
    """
    正向判定：这条命令是否「已证明只读」。

    允许管道与顺序拼接，但要求**每一段**都单独通过只读证明：`ls | grep x`、
    `git log | head` 通过，`ls && rm x` 因 rm 段不通过而拒绝。任一段无法解析
    （重定向、命令替换、引号未闭合…）即整体拒绝——只要有一处说不清就不放行。

    :param cmd: shell 命令字符串
    :return: (是否通过证明, 说明)
    """
    raw = str(cmd or "")
    if not raw.strip():
        return False, t("空命令", "empty command")
    segs = split_command_segments(raw)
    if segs is None:
        return False, t("命令无法静态证明只读（重定向/命令替换/引号不闭合）",
                        "cannot statically prove read-only "
                        "(redirection, command substitution or unbalanced quotes)")
    for seg in segs:
        ok, why = _safe_single(seg)
        if not ok:
            return False, why
    return True, t("只读探查命令", "read-only inspection command")


def _safe_git(args: list[str]) -> tuple[bool, str]:
    """git 参数校验：只读子命令 + 无执行任意程序的参数"""
    lowered = [a.lower() for a in args]
    for a in lowered:
        # 兼容 --output=x / --exec-path=/x 这类带等号写法
        if a.split("=", 1)[0] in _GIT_UNSAFE_ARGS:
            return False, t(f"git 参数可执行任意程序或写文件: {a}",
                            f"git arg can exec arbitrary program or write a file: {a}")
    if not args:
        return False, t("git 缺少只读子命令", "git without a read-only subcommand")

    sub = next((a for a in lowered if not a.startswith("-")), "")
    if not sub:
        return False, t("git 缺少只读子命令", "git without a read-only subcommand")

    # branch / remote 的写语义藏在位置参数里（git branch -D x / git remote add）
    if sub in _GIT_SUB_ALLOWED_ARGS:
        allowed = _GIT_SUB_ALLOWED_ARGS[sub]
        rest = [a for a in lowered if a != sub]
        for a in rest:
            if a not in allowed:
                return False, t(f"git {sub} 参数含写语义: {a}",
                                f"git {sub} arg has write semantics: {a}")
        return True, t("git 只读查询", "read-only git query")

    if sub not in SAFE_GIT_SUBCOMMANDS:
        return False, t(f"git 子命令非只读: {sub}",
                        f"git subcommand not read-only: {sub}")
    return True, t("git 只读查询", "read-only git query")
