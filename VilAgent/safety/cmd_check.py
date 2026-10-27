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
           "safe_command"]


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

    risk: low | medium | high；cmd_type: exec | network | unknown
    """
    label = match_dangerous_command(cmd)
    if label:
        return {
            "risk": "high",
            "cmd_type": "exec",
            "reasons": [t(f"匹配危险命令模式: {label}",
                          f"matches dangerous command pattern: {label}")],
        }
    low = normalize_command(cmd)
    if any(k in low for k in _NETWORK_KEYWORDS):
        return {
            "risk": "medium",
            "cmd_type": "network",
            "reasons": [t("命令含网络工具", "command uses a network tool")],
        }
    return {"risk": "low", "cmd_type": "unknown", "reasons": []}


# ---- auto 层级的「可自动放行」命令判定（正向白名单，fail-closed）----
#
# 与 classify_command 的黑名单语义分开，理由同 ast_check.safe_python：
#   classify_command().risk == "low"  → 「没发现危险」（展示/提示用）
#   safe_command()                    → 「已证明只读且无法拼接」（放行凭据）
#
# 比 Python 侧更严，因为 **run_command 完全不过沙箱**（sandbox 仅用于 run_python）：
# L2 权限是它唯一的边界，一次误放行等于以当前用户身份直连执行。
# 因此这里只放「只读探查」：任何可能写仓库、装依赖、联网或执行任意代码的命令
# （python/pytest/npm/pip/curl/sed -i/tee…）都不在名单内 → 退回 ask。
# 将来新增危险命令不必改这里：不在白名单里就天然不放行。
SAFE_COMMANDS = frozenset({
    "ls", "dir", "pwd", "echo", "cat", "type", "head", "tail", "wc",
    "sort", "uniq", "grep", "findstr", "which", "where", "whoami",
    "date", "uname", "more", "tree",
})

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

# shell 元字符：出现即可能拼接命令或重定向写文件 → 不放行
_SHELL_META = (";", "&", "|", ">", "<", "$", "`", "(", ")", "\n", "\r")


def safe_command(cmd: str) -> tuple[bool, str]:
    """
    正向判定：这条命令是否「已证明只读、且无法拼接」，可在 auto 层级直接放行。

    :param cmd: shell 命令字符串
    :return: (是否可自动放行, 说明)
    """
    raw = str(cmd or "")
    if not raw.strip():
        return False, t("空命令", "empty command")
    if any(m in raw for m in _SHELL_META):
        return False, t("命令含 shell 元字符（可拼接命令或重定向写文件）",
                        "command contains shell metacharacters "
                        "(can chain commands or redirect to files)")

    parts = raw.split()
    exe = parts[0].lower()
    if exe == "git":
        return _safe_git(parts[1:])
    if exe not in SAFE_COMMANDS:
        return False, t(f"命令未列入白名单: {exe}",
                        f"command not allowlisted: {exe}")
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
