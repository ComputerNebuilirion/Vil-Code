"""L2 权限层：工具调用前的 allow/ask/deny 决策（Windows 上的实际安全边界）

设计哲学（参考 Claude Code 在 Windows 上的做法）：没有 OS 级沙箱时，
per-action 权限评估就是安全边界。trust 控制默认姿态，规则文件细化。

默认姿态由**用户可选的权限层级**（`permission_level`）决定，
公共前置（trust=True，即 do 模式）：
  - 只读工具            → allow
  - 内置高危 deny 规则  → deny（硬底线，用户规则无法放行）
  - 高危（risk=high）   → ask
层级（`level`，见 normalize_level）：
  - strict   → 所有非只读操作一律 ask（写/编辑也不例外，最保守）
  - balanced → 写/编辑类 allow；命令/代码执行类 ask；破坏性工具 ask（默认值）
  - auto     → 在 balanced 基础上，静态检查 risk=low 的 run_command / run_python
               直接 allow，risk=medium/high 仍需 ask
非 trust 模式：写/执行一律 deny（只读放行）。

规则文件（用户可覆盖默认，用于进一步放松或收紧）：
  优先 <workspace>/.vil/permissions.json，其次 ~/.vil/permissions.json
  格式：{"rules":[{"tool":"run_command","pattern":"git *","action":"allow"}]}
  注意：内置 deny 是硬底线，规则文件里的 allow 无法放行危险命令
  （rm -rf、del、rd /s、format、mkfs、Remove-Item -Recurse 等，规范化正则匹配，
   可覆盖大小写/前缀(sudo)/拼接(a && rm ...)等绕过写法）。
"""
import fnmatch
import json
from dataclasses import dataclass
from pathlib import Path

from ..i18n import t
from .ast_check import classify
from .cmd_check import classify_command, match_dangerous_command

ALLOW = "allow"
ASK = "ask"
DENY = "deny"

# —— 用户可选的权限层级（config: permission_level / CLI --permission / REPL /perm）——
# strict   所有非只读操作都要人工确认（写/编辑也不例外）
# balanced 只有命令/代码执行 + 破坏性操作要确认，写/编辑直接放行（默认）
# auto     balanced + 静态检查 risk=low 的命令/代码直接放行（medium/high 仍确认）
LEVEL_STRICT = "strict"
LEVEL_BALANCED = "balanced"
LEVEL_AUTO = "auto"
DEFAULT_LEVEL = LEVEL_BALANCED

# 别名：允许用户写 1/2/3 或语义化词，未知值一律回退默认层级
_LEVEL_ALIASES = {
    "1": LEVEL_STRICT, "strict": LEVEL_STRICT, "all": LEVEL_STRICT,
    "ask": LEVEL_STRICT,
    "2": LEVEL_BALANCED, "balanced": LEVEL_BALANCED,
    "default": LEVEL_BALANCED, "normal": LEVEL_BALANCED,
    "3": LEVEL_AUTO, "auto": LEVEL_AUTO, "yolo": LEVEL_AUTO,
}


def normalize_level(level) -> str:
    """把用户输入（config/CLI/别名）归一化为三种层级之一，未知值回退默认"""
    return _LEVEL_ALIASES.get(str(level or "").strip().lower(), DEFAULT_LEVEL)


def level_label(level) -> str:
    """层级的单行说明（给 /perm、banner 等人看）"""
    level = normalize_level(level)
    if level == LEVEL_STRICT:
        return t("strict：所有非只读操作都要确认",
                 "strict: confirm every non-readonly action")
    if level == LEVEL_AUTO:
        return t("auto：低风险命令/代码直接放行，中/高风险确认",
                 "auto: low-risk commands and code run directly, medium/high ask")
    return t("balanced：只有命令/删除类操作需要确认",
             "balanced: only command / delete actions need confirmation")


# 即使 trust=True 也仍需人工确认的「执行类」工具（其余非只读工具默认放行）
_EXEC_TOOLS = {"run_command", "run_python"}

# auto 层级可以直接放行的执行类工具（静态检查 risk=low 时）
_AUTO_RISK_TOOLS = {"run_command", "run_python"}


def _classify_risk(tool_name: str, args: dict) -> str | None:
    """给 auto 层级补一次静态风险分类（命令层 / AST 层各用各的分类器）"""
    if tool_name == "run_command":
        return classify_command(str(args.get("command") or "")).get("risk")
    if tool_name == "run_python":
        return classify(str(args.get("code") or "")).get("risk")
    return None

# 即使 trust=True 也仍需人工确认的「破坏性」工具：删除不可逆，单独收紧为 ask
_DESTRUCTIVE_TOOLS = {"delete_file"}


@dataclass
class Rule:
    tool: str | None = None      # None = 匹配任意工具
    pattern: str | None = None   # fnmatch 模式，匹配 args["path"] 或 args["command"] / args["code"]
    action: str = ASK            # allow / ask / deny


# 内置硬底线：命令类工具命中危险命令模式 → 始终 deny，用户规则无法放行
# （与 exec.py 黑名单统一为同一匹配器，规范化正则可覆盖大小写/前缀/拼接绕过）
def _builtin_deny(tool_name: str, args: dict) -> str | None:
    cmd = args.get("command")
    if isinstance(cmd, str):
        return match_dangerous_command(cmd)
    return None


def default_rules_file(workspace=None) -> Path | None:
    """解析默认规则文件：项目层 .vil/permissions.json 优先，其次 ~/.vil/permissions.json。

    都不存在时返回 None（全走内置默认姿态）。
    """
    if workspace:
        project = Path(workspace) / ".vil" / "permissions.json"
        if project.exists():
            return project
    global_ = Path.home() / ".vil" / "permissions.json"
    if global_.exists():
        return global_
    return None


class PermissionSystem:
    """
    权限决策器

    决策顺序：
      1. readonly 工具 → allow
      2. 内置 deny 硬底线命中 → deny（用户规则无法放行）
      3. 用户规则命中（默认规则文件或显式传入）→ 该规则的 action
      4. 默认姿态（由 trust + level 共同决定）：
         - risk=high              → trust ? ask : deny
         - 非 trust（写/执行）     → deny
         - level=strict           → ask（所有非只读）
         - level=auto + run_command + risk=low → allow
         - 执行/破坏类（run_command / run_python / delete_file）→ ask
         - 写/编辑类              → allow
    """

    def __init__(self, rules_file: Path | str | None = None, trust: bool = False,
                 level: str = DEFAULT_LEVEL):
        self.trust = trust
        self.level = normalize_level(level)
        self.rules_file = str(rules_file) if rules_file else ""
        self.user_rules: list[Rule] = []
        # 规则文件加载失败时的说明（供上层告警，不影响循环继续）
        self.load_error: str | None = None
        if rules_file:
            p = Path(rules_file)
            if p.exists():
                self._load_file(p)

    def _load_file(self, path: Path) -> None:
        # 规则文件坏了不能拖垮整个 agent 循环 → 记录告警并忽略
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            self.load_error = t(
                f"权限规则文件加载失败，已忽略: {path} ({e})",
                f"failed to load permission rules file, ignored: {path} ({e})",
            )
            return
        for r in data.get("rules") or []:
            if not isinstance(r, dict):
                continue
            self.user_rules.append(Rule(
                tool=r.get("tool"),
                pattern=r.get("pattern"),
                action=r.get("action", ASK),
            ))

    @staticmethod
    def _match(rule: Rule, tool_name: str, args: dict) -> bool:
        if rule.tool and rule.tool != tool_name:
            return False
        if rule.pattern:
            # 对所有相关字段逐个匹配，命中任一即可（避免 path/command 同时存在时歧义）
            targets = [str(args[k]) for k in ("path", "command", "code")
                       if args.get(k)]
            if not any(fnmatch.fnmatch(tgt, rule.pattern) for tgt in targets):
                return False
        return True

    def evaluate(self, tool_name: str, args: dict,
                 readonly: bool, risk: str | None = None) -> str:
        # 1. 只读工具放行
        if readonly:
            return ALLOW

        # 2. 内置硬底线（不可被用户规则放行）
        if _builtin_deny(tool_name, args):
            return DENY

        # 3. 用户规则（可放松为 allow，或收紧为 ask/deny）
        for rule in self.user_rules:
            if self._match(rule, tool_name, args):
                return rule.action

        # 4. 默认姿态
        if risk == "high":
            return ASK if self.trust else DENY
        if not self.trust:
            return DENY
        # 层级 1（strict）：所有非只读操作都要人工确认，写/编辑也不例外
        if self.level == LEVEL_STRICT:
            return ASK
        # 层级 3（auto）：静态检查 low 的执行类调用直接放行（medium/high 仍 ask）
        # run_python 与 run_command 同等待遇：沙箱本身已有限额隔离，
        # 让「安全的临时计算」零摩擦，模型才愿意走沙箱，
        # 而不是 write_file 造脚本再 run_command 绕过去。
        if self.level == LEVEL_AUTO and tool_name in _AUTO_RISK_TOOLS:
            if risk is None:
                # 调用方没先分类时自行补一次（只读分类，无副作用）
                risk = _classify_risk(tool_name, args)
            if risk == "low":
                return ALLOW
        if tool_name in _EXEC_TOOLS or tool_name in _DESTRUCTIVE_TOOLS:
            return ASK
        return ALLOW
