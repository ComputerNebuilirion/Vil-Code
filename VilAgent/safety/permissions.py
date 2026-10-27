"""L2 权限层：工具调用前的 allow/ask/deny 决策（Windows 上的实际安全边界）

设计哲学（参考 Claude Code 在 Windows 上的做法）：没有 OS 级沙箱时，
per-action 权限评估就是安全边界。trust 控制默认姿态，规则文件细化。

默认姿态由**用户可选的权限层级**（`permission_level`）决定。
任何层级都先过的公共前置：
  - 只读工具            → allow
  - 内置高危 deny 规则  → deny（硬底线，用户规则无法放行）
                          命令类=危险命令模式，代码类=AST 判 high（与 sandbox L1 同判据）
  - 用户规则命中        → 该规则的 action（可放松为 allow，也可收紧为 ask/deny）
  - 只读命令            → allow（balanced/strict 下，`safe_command` 证明整条命令只读：
                          ls / cat / dir / git status / ls | grep x 都算，
                          见 cmd_check.SAFE_COMMANDS。yolo 本就全放行，不参与）
层级（`level`，见 normalize_level）：
  - strict   → 所有非只读操作一律 ask（写/编辑也不例外，最保守）
  - balanced → 写/编辑类 allow；命令/代码执行类 ask；破坏性工具 ask（默认值）
  - yolo     → 不做执行前检查：过掉兜底后所有调用直接 allow（含命令/代码/删除）
非 trust 模式：写/执行一律 deny（只读放行）。yolo 是唯一例外——它在构造时
强制 trust=True，否则会退化成「所有写操作被拒」，与该层级意图正相反。

规则文件（用户可覆盖默认，用于进一步放松或收紧）：
  优先 <workspace>/.vil/permissions.json，其次 ~/.vil/permissions.json
  格式：{"rules":[{"tool":"run_command","pattern":"git *","action":"allow"}]}
  注意：内置 deny 是硬底线，规则文件里的 allow 无法放行危险命令
  （rm -rf、del、rd /s、format、mkfs、Remove-Item -Recurse 等，规范化正则匹配，
   可覆盖大小写/前缀(sudo)/拼接(a && rm ...)等绕过写法），也无法放行
  AST 判为 high 的 run_python 代码（ctypes/win32api 导入、exec/eval/compile/
  __import__ 调用等，与沙箱 L1 预检同一判据）。
"""
import fnmatch
import json
from dataclasses import dataclass
from pathlib import Path

from ..i18n import t
from .ast_check import classify
from .cmd_check import match_dangerous_command, safe_command

ALLOW = "allow"
ASK = "ask"
DENY = "deny"

# —— 用户可选的权限层级（config: permission_level / CLI --permission / REPL /perm）——
# strict   所有非只读操作都要人工确认（写/编辑也不例外）
# balanced 只有命令/代码执行 + 破坏性操作要确认，写/编辑直接放行（默认）
# 前两者都对「已证明只读」的 run_command 直接放行（见 safe_command）
# yolo     不做任何执行前检查，所有工具调用直接执行（仅保留下面的 deny 硬底线）
LEVEL_STRICT = "strict"
LEVEL_BALANCED = "balanced"
LEVEL_YOLO = "yolo"
DEFAULT_LEVEL = LEVEL_BALANCED

# 别名：允许用户写 1/2/3 或语义化词，未知值一律回退默认层级
# "auto" 是 yolo 层级的历史名字（旧配置里可能还是它），保留映射避免静默回退
_LEVEL_ALIASES = {
    "1": LEVEL_STRICT, "strict": LEVEL_STRICT, "all": LEVEL_STRICT,
    "ask": LEVEL_STRICT,
    "2": LEVEL_BALANCED, "balanced": LEVEL_BALANCED,
    "default": LEVEL_BALANCED, "normal": LEVEL_BALANCED,
    "3": LEVEL_YOLO, "yolo": LEVEL_YOLO, "auto": LEVEL_YOLO,
}


def normalize_level(level) -> str:
    """把用户输入（config/CLI/别名）归一化为三种层级之一，未知值回退默认"""
    return _LEVEL_ALIASES.get(str(level or "").strip().lower(), DEFAULT_LEVEL)


def level_label(level) -> str:
    """层级的单行说明（给 /perm、banner 等人看）"""
    level = normalize_level(level)
    if level == LEVEL_STRICT:
        return t("strict：非只读操作都要确认，只读命令直接放行",
                 "strict: confirm every non-readonly action, "
                 "read-only commands run directly")
    if level == LEVEL_YOLO:
        return t("yolo：不做执行前检查，所有调用直接执行（仅保留灾难命令硬拦截）",
                 "yolo: no pre-execution checks, every call runs directly "
                 "(only catastrophic-command guard remains)")
    return t("balanced：只有命令/删除类操作需要确认（只读命令直接放行）",
             "balanced: only command / delete actions need confirmation "
             "(read-only commands run directly)")


# 即使 trust=True 也仍需人工确认的「执行类」工具（其余非只读工具默认放行）
_EXEC_TOOLS = {"run_command", "run_python"}

# 即使 trust=True 也仍需人工确认的「破坏性」工具：删除不可逆，单独收紧为 ask
_DESTRUCTIVE_TOOLS = {"delete_file"}


@dataclass
class Rule:
    tool: str | None = None      # None = 匹配任意工具
    pattern: str | None = None   # fnmatch 模式，匹配 args["path"] 或 args["command"] / args["code"]
    action: str = ASK            # allow / ask / deny


# 内置硬底线 → 始终 deny，用户规则无法放行
#   命令类：命中危险命令模式（与 exec.py 黑名单同一匹配器，规范化正则可覆盖
#           大小写/前缀(sudo)/拼接(a && rm ...)等绕过写法）
#   代码类：AST 判 risk=high（与 sandbox.py 的 L1 预检 check_safety 完全同一判据）
# 代码侧刻意复用同一判据，而非另设一套：这样不新增任何拒绝面，只是把
# 「先问用户、批准后再被沙箱拒绝」提前成「直接拒绝」，省掉一次无意义的确认弹窗。
def _builtin_deny(tool_name: str, args: dict) -> str | None:
    if tool_name == "run_python":
        code = args.get("code")
        if isinstance(code, str):
            cls = classify(code)
            if cls.get("risk") == "high":
                return "; ".join(cls.get("reasons") or []) or t(
                    "高风险代码", "high-risk code")
        return None
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
      3. 用户规则命中（默认规则文件或显式传入的规则）→ 该规则的 action
      4. 只读命令（balanced/strict 下的 run_command）→ allow
      5. 默认姿态（由 trust + level 共同决定）：
         - 非 trust（写/执行）     → deny
         - level=yolo             → allow（不做执行前检查，含执行/破坏类）
         - risk=high              → trust ? ask : deny
         - level=strict           → ask（所有非只读）
         - 执行/破坏类（run_command / run_python / delete_file）→ ask
         - 写/编辑类              → allow
    """

    def __init__(self, rules_file: Path | str | None = None, trust: bool = False,
                 level: str = DEFAULT_LEVEL):
        self.trust = trust
        self.level = normalize_level(level)
        # yolo 语义是「不做执行前检查」，而 trust=False 的默认姿态是「写/执行全 deny」。
        # 两者叠加会得到「所有写操作被拒」——与该层级的意图正相反，因此这里强制 trust。
        if self.level == LEVEL_YOLO:
            self.trust = True
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
        """返回 allow / ask / deny。

        ``risk`` 只用于 balanced/strict 下「加厚拒绝」（high → 至少 ask）与上层展示，
        不参与 yolo 的决策：yolo 的语义就是「不问」，任何静态评级都不改变它。
        """
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

        # 4. 只读命令白名单：balanced/strict 下把「已证明只读」的 run_command
        #    视同只读工具直接放行（ls / cat / git status / ls | grep x …）。
        #    放在用户规则之后 → 用户显式写的 ask/deny 规则仍然能收紧它；
        #    yolo 无需判断（它本来就全放行）；门禁仍在第 2 步 deny 硬底线。
        if tool_name == "run_command" and self.level != LEVEL_YOLO:
            cmd = args.get("command")
            if isinstance(cmd, str) and safe_command(cmd)[0]:
                return ALLOW

        # 5. 默认姿态
        if not self.trust:
            return DENY
        # 层级 3（yolo）：不做任何执行前检查 → 直接放行（含执行/破坏类）。
        # 唯一还拦得住它的是上面的 deny 硬底线（灾难命令 / AST 判 high 的代码，
        # 后者本来也会被沙箱 L1 拒掉），以及用户自己写进规则的 ask/deny。
        if self.level == LEVEL_YOLO:
            return ALLOW
        if risk == "high":
            return ASK
        # 层级 1（strict）：所有非只读操作都要人工确认，写/编辑也不例外
        if self.level == LEVEL_STRICT:
            return ASK
        if tool_name in _EXEC_TOOLS or tool_name in _DESTRUCTIVE_TOOLS:
            return ASK
        return ALLOW
