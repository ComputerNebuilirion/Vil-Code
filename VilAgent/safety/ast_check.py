"""L1 AST 预检层：代码分类 + 风险评级（非安全边界，仅预警/供 L2 决策）

设计哲学（参考 Claude Code）：AST 不是安全边界，混淆代码永远能绕过字符串匹配。
真正隔离靠 L3 OS 沙箱；本层只做"快速分类 + 风险提示"，给 L2 权限层和用户看。
"""
import ast
import re

from ..i18n import t

# ---- 分类用模块/函数表 ----
NETWORK_MODULES = {
    'socket', 'urllib', 'urllib2', 'requests', 'http.client',
    'ftplib', 'smtplib', 'poplib', 'imaplib', 'websocket',
}
EXEC_MODULES = {'subprocess', 'multiprocessing'}
# 高危模块（可在用户态绕过 OS 沙箱，如 ctypes 直接调 Win32）
DANGEROUS_MODULES = {
    'ctypes', 'ctypes.windll', 'ctypes.cdll',
    'win32api', 'win32con', 'win32job', 'win32process',
}

# 高危函数（exec/eval/compile/__import__ 能逃逸任何静态分析）
HIGH_RISK_CALLS = {'exec', 'eval', 'compile', '__import__', 'globals', 'locals'}
# 执行外部命令
EXEC_CALLS = {
    'os.system', 'os.popen', 'os.popen2', 'os.popen3', 'os.popen4',
    'subprocess.run', 'subprocess.Popen', 'subprocess.call',
    'subprocess.check_output', 'subprocess.check_call',
    'os.spawnl', 'os.spawnle', 'os.spawnlp', 'os.spawnlpe',
    'os.spawnv', 'os.spawnve', 'os.spawnvp', 'os.spawnvpe',
}
# 删除类操作（不可逆，与普通写操作分开成档）
DELETE_CALLS = {
    'os.remove', 'os.unlink', 'shutil.rmtree',
}
# 文件/系统写操作（创建、改名、改权限；不销毁已有数据）
WRITE_CALLS = {
    'os.rename', 'os.replace', 'os.chmod', 'os.chown',
    'os.makedirs', 'os.mkdir', 'os.symlink', 'os.link', 'os.chdir',
    'shutil.move', 'shutil.copy', 'shutil.copyfile', 'shutil.copytree',
}
# 只读文件/系统访问：不进任何白名单，只用于把 type 从 unknown 提准成 read
READ_CALLS = {
    'os.listdir', 'os.scandir', 'os.walk', 'os.stat', 'os.getcwd',
    'glob.glob', 'glob.iglob',
}
# 按「方法名」归纳 pathlib / 文件对象方法：调用点拿不到接收者类型，只能看
# 方法名。识别不出不影响任何决策（cmd_type 只用于展示/供人看），只是更准。
_METHOD_TYPES = {
    'read_text': 'read', 'read_bytes': 'read', 'iterdir': 'read',
    'glob': 'read', 'rglob': 'read', 'exists': 'read', 'is_file': 'read',
    'is_dir': 'read', 'stat': 'read',
    'write_text': 'write', 'write_bytes': 'write', 'mkdir': 'write',
    'touch': 'write', 'rename': 'write', 'replace': 'write',
    'symlink_to': 'write', 'hardlink_to': 'write',
    'unlink': 'delete', 'rmdir': 'delete',
}
# 内置纯函数：只有「所有调用都在这里（或属于下面的纯计算模块）」才算 type=pure。
# 刻意用白名单而非「不认识就算纯」——后者会把 df.to_csv() 之类标成纯计算。
_PURE_CALLS = frozenset({
    'print', 'len', 'range', 'sum', 'min', 'max', 'abs', 'round', 'sorted',
    'reversed', 'enumerate', 'zip', 'map', 'filter', 'any', 'all',
    'int', 'float', 'str', 'bool', 'list', 'dict', 'set', 'frozenset',
    'tuple', 'bytes', 'complex', 'divmod', 'pow', 'format', 'repr',
    'isinstance', 'issubclass', 'ord', 'chr', 'hex', 'oct', 'bin',
})


def _is_pure_call(full: str | None, method: str) -> bool:
    """调用点是否算「纯计算」：内置纯函数，或白名单纯计算模块里的函数"""
    if method in _PURE_CALLS:
        return True
    # math.sqrt / json.dumps 这类：按模块前缀判断（SAFE_IMPORT_MODULES 见下）
    return bool(full) and full.split(".", 1)[0] in SAFE_IMPORT_MODULES


def classify(code: str) -> dict:
    """
    分类 Python 代码的风险等级和操作类型（L1 预检）

    :param code: Python 源码
    :return: {"risk": "low"|"medium"|"high",
              "cmd_type": "pure"|"read"|"write"|"delete"|"network"|"exec"|"unknown",
              "reasons": [str, ...]}

    cmd_type 只用于展示/供人判断，**不参与放行决策**（放行判据是 safe_python 的
    正向白名单）：
      pure    所有调用都是已知纯函数（print/len… 或白名单纯计算模块），read 只读访问，
      write 创建/改名/写文件，delete 删除（不可逆），network 联网，
      exec 执行外部命令/进程/动态执行原语，
      unknown 有认不出的调用 —— 它既不等于安全也不等于危险，只是没信息。
    """
    reasons: list[str] = []
    found_types: set[str] = set()
    risk = "low"
    # 出现过无法归类的调用 → 不能算 pure（pure 的语义是「只碰已知纯函数」）
    saw_other_call = False

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return {"risk": "high", "cmd_type": "unknown",
                "reasons": [t(f"语法错误: {e}", f"syntax error: {e}")]}

    for node in ast.walk(tree):
        # import 语句
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if _match_module(name, NETWORK_MODULES):
                    found_types.add("network")
                    reasons.append(t(f"导入网络模块: {name}", f"imports network module: {name}"))
                if _match_module(name, EXEC_MODULES):
                    found_types.add("exec")
                    reasons.append(t(f"导入执行模块: {name}", f"imports exec module: {name}"))
                if _match_module(name, DANGEROUS_MODULES):
                    risk = "high"
                    reasons.append(t(f"导入高危模块: {name}", f"imports high-risk module: {name}"))

        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if _match_module(mod, NETWORK_MODULES):
                found_types.add("network")
                reasons.append(t(f"从网络模块 {mod} 导入", f"imports from network module {mod}"))
            if _match_module(mod, EXEC_MODULES):
                found_types.add("exec")
                reasons.append(t(f"从执行模块 {mod} 导入", f"imports from exec module {mod}"))
            if _match_module(mod, DANGEROUS_MODULES):
                risk = "high"
                reasons.append(t(f"从高危模块 {mod} 导入", f"imports from high-risk module {mod}"))

        elif isinstance(node, ast.Call):
            full = _get_full_name(node.func)
            # 方法名兜底：`Path(p).read_text()` / `p.unlink()` 这类拿不到接收者
            # 类型的链式调用，按方法名归类（只影响 cmd_type 展示，不影响决策）
            method = full.rsplit(".", 1)[-1] if full else _attr_name(node.func)
            recognized = False
            if full:
                if full in HIGH_RISK_CALLS:
                    # 动态执行原语：归到 exec 档（risk 已是 high）
                    risk = "high"
                    found_types.add("exec")
                    reasons.append(t(f"调用高危函数: {full}", f"calls high-risk function: {full}"))
                    recognized = True
                if full in EXEC_CALLS:
                    found_types.add("exec")
                    reasons.append(t(f"调用执行函数: {full}", f"calls exec function: {full}"))
                    recognized = True
                if full in WRITE_CALLS:
                    found_types.add("write")
                    reasons.append(t(f"调用写操作: {full}", f"calls write operation: {full}"))
                    recognized = True
                if full in DELETE_CALLS:
                    found_types.add("delete")
                    reasons.append(t(f"调用删除操作: {full}",
                                     f"calls delete operation: {full}"))
                    recognized = True
                if full in READ_CALLS:
                    found_types.add("read")
                    recognized = True
                # open() 模式分析
                if full == "open" or full.endswith(".open"):
                    mode = _get_open_mode(node)
                    if mode and any(c in mode for c in ("w", "a", "x", "+")):
                        found_types.add("write")
                        reasons.append(t(f"open(mode='{mode}') 写文件",
                                         f"open(mode='{mode}') writes a file"))
                    else:
                        found_types.add("read")
                    recognized = True
            kind = _METHOD_TYPES.get(method)
            if kind:
                found_types.add(kind)
                recognized = True
                if kind in ("write", "delete"):
                    reasons.append(t(f"调用 {method}（{kind} 操作）",
                                     f"calls {method} ({kind} operation)"))
            # 认不出的调用 → 不能算 pure（pure 的语义是「只碰纯函数」）
            if not recognized and not _is_pure_call(full, method):
                saw_other_call = True

    # cmd_type 优先级：exec > network > delete > write > read
    for kind in ("exec", "network", "delete", "write", "read"):
        if kind in found_types:
            cmd_type = kind
            break
    else:
        # 没有可识别的 I/O/执行动作：全部调用都是已知纯函数 → pure，
        # 有认不出的调用 → unknown（既不是「安全」也不是「危险」，只是没信息）
        cmd_type = "unknown" if saw_other_call else "pure"

    # 风险升级（未被高危函数定 high 的）
    if risk != "high":
        if cmd_type in ("exec", "network", "delete", "write"):
            risk = "medium"
        else:
            risk = "low"

    return {"risk": risk, "cmd_type": cmd_type, "reasons": reasons}


def check_safety(code: str) -> tuple[bool, str]:
    """
    兼容旧接口：高风险 → 不安全。

    :return: (是否通过预检, 说明)
    """
    c = classify(code)
    if c["risk"] == "high":
        return False, "; ".join(c["reasons"]) or t("高风险操作", "high-risk operation")
    return True, t(f"通过预检 (risk={c['risk']}, type={c['cmd_type']})",
                  f"passed pre-check (risk={c['risk']}, type={c['cmd_type']})")


# ---- 「结构上可证明纯粹」判定（正向白名单，fail-closed）----
#
# 注意：它**不再参与放行决策**。yolo 层级不做执行前检查，balanced/strict 靠的是
# 「工具类别」而非代码结构，所以下面这套判据目前没有调用方。保留的理由：
#   1. 它是一个独立的、可复用的「这段代码是否碰不到文件/网络/进程」证明器，
#      将来若要恢复「只自动放行纯计算」的中间层级，直接接上即可；
#   2. 它的白名单是黑名单的对照物，改 classify() 时能拿来交叉验证口径。
#
# 与 classify() 刻意分成两套语义，不要合并：
#   classify().risk == "low"  → 「没发现危险」（黑名单发现式，完备性无法保证）
#   safe_python()             → 「结构上够不到逃逸原语」（白名单证明式，判不准即不放行）
#
# 白名单只收「不碰文件/网络/进程」的纯计算模块。任何 import 未列入白名单的模块
# （os/sys/pathlib/subprocess…）都会判 False。将来新增危险模块**不需要改这里**
# —— 不在白名单里就天然不通过。
SAFE_IMPORT_MODULES = frozenset({
    "math", "cmath", "random", "statistics", "decimal", "fractions",
    "json", "re", "itertools", "functools", "collections", "operator",
    "datetime", "time", "string", "textwrap", "unicodedata",
    "hashlib", "base64", "binascii", "zlib", "struct", "copy", "enum",
    "typing", "dataclasses", "pprint", "uuid", "html",
})

# 动态派发/执行原语：一经出现即拒绝（哪怕叫法合法，能借此触达的属性不可穷举）
_FORBIDDEN_CALLS = frozenset({
    "getattr", "setattr", "delattr", "vars", "globals", "locals", "dir",
    "eval", "exec", "compile", "__import__", "open", "breakpoint", "input",
})

# 直接引用这些名字等于把逃逸出口放进作用域
_FORBIDDEN_NAMES = frozenset({
    "__builtins__", "__import__", "__loader__", "__spec__",
    "__package__", "__file__", "__cached__", "__debug__",
})


def safe_python(code: str) -> tuple[bool, str]:
    """
    正向判定：这段代码是否「结构上够不到逃逸原语」。

    与 check_safety 的区别在于提问方向：check_safety 答「没发现危险」（黑名单），
    本函数答「已证明无逃逸路径」（白名单）。判不准一律返回 False。

    目前没有调用方（yolo 层级不做执行前检查，其余层级按工具类别决策），
    保留为可复用的证明器 + 黑名单的交叉验证对照，见上方注释。

    :param code: Python 源码
    :return: (是否通过证明, 说明)
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, t(f"语法错误: {e}", f"syntax error: {e}")

    for node in ast.walk(tree):
        # 1. import：白名单制（不是"危险模块黑名单"，方向相反）
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in SAFE_IMPORT_MODULES:
                    return False, t(
                        f"导入未列入白名单的模块: {alias.name}",
                        f"imports non-allowlisted module: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if not mod or mod.split(".")[0] not in SAFE_IMPORT_MODULES:
                return False, t(
                    f"从非白名单模块导入: {mod or '(相对导入)'}",
                    f"imports from non-allowlisted module: {mod or '(relative import)'}")

        # 2. 逃逸出口的名字
        elif isinstance(node, ast.Name):
            if node.id in _FORBIDDEN_NAMES:
                return False, t(f"访问内置原语: {node.id}",
                               f"accesses builtin primitive: {node.id}")

        # 3. dunder 属性访问：().__class__ / f.__globals__ / t.__subclasses__ 一类
        #    的沙箱逃逸全部依赖它，一律拒绝（含 __init__，代价只是多问一次）
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__"):
                return False, t(f"访问 dunder 属性: .{node.attr}",
                               f"accesses dunder attribute: .{node.attr}")

        # 4. 调用：必须是静态可解析的名字链，且不是执行/动态派发原语
        elif isinstance(node, ast.Call):
            name = _get_full_name(node.func)
            if name is None:
                # obj[0]()、f()() 这类无法静态解析的调用 → 证明不了，不放行
                return False, t("存在无法静态解析的调用目标",
                               "call target cannot be resolved statically")
            if name in _FORBIDDEN_CALLS or name.split(".")[-1] in _FORBIDDEN_CALLS:
                return False, t(f"调用执行/动态派发原语: {name}",
                               f"calls exec/dynamic-dispatch primitive: {name}")

    return True, t("纯计算，无逃逸原语", "pure computation, no escape primitive")


def _match_module(name: str, modules) -> bool:
    """匹配 import 名：urllib.request 匹配 urllib；os 匹配 os"""
    return any(name == m or name.startswith(m + ".") for m in modules)


def _startswith_any(name: str, patterns) -> bool:
    return _match_module(name, patterns)


def _get_full_name(node) -> str | None:
    """获取 AST 节点完整名称，如 os.system"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _get_full_name(node.value)
        if parent:
            return f"{parent}.{node.attr}"
    return None


def _attr_name(node) -> str:
    """调用点的「方法名/函数名」（拿不到完整限定名时的兜底）"""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _get_open_mode(call_node: ast.Call) -> str | None:
    """提取 open() 的 mode 参数"""
    # 位置参数：open(path, mode)
    if len(call_node.args) >= 2:
        v = call_node.args[1]
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            return v.value
    # 关键字：open(path, mode=...)
    for kw in call_node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            return kw.value.value
    return None


def is_valid_python(code: str) -> tuple[bool, str]:
    """验证代码是否为有效 Python"""
    try:
        ast.parse(code)
        return True, t("有效", "valid")
    except SyntaxError as e:
        return False, t(f"语法错误: {e}", f"syntax error: {e}")


def contains_unsafe_patterns(code: str) -> tuple[bool, str]:
    """正则快速扫不安全模式（补充 AST 漏检的混淆写法提示）"""
    unsafe_patterns = [
        (r'\b__import__\s*\(', t("__import__ 动态导入", "__import__ dynamic import")),
        (r'\bglobals\s*\(\s*\)', "globals()"),
        (r'\blocals\s*\(\s*\)', "locals()"),
        (r'\bgetattr\s*\(\s*\w+\s*,\s*["\']', t("getattr 动态属性访问（可能绕过静态检查）", "getattr dynamic attribute access (may bypass static checks)")),
        (r'__\w+__\s*\(', t("dunder 方法调用", "dunder method call")),
    ]
    for pattern, desc in unsafe_patterns:
        if re.search(pattern, code):
            return True, t(f"检测到可疑模式: {desc}", f"suspicious pattern detected: {desc}")
    return False, t("安全", "safe")
