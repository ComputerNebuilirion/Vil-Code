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

__all__ = ["normalize_command", "match_dangerous_command", "classify_command"]


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
