"""Python 代码执行工具：串联 L1 分类 → L2 权限 → L3 沙箱"""
import os

from . import tool
from ..i18n import L, t
from ..safety import classify, sandbox_run

# workdir 取值：sandbox（默认，隔离临时目录）/ workspace（工作区，可用相对路径）
_WORKDIR_SANDBOX = "sandbox"
_WORKDIR_WORKSPACE = "workspace"
_WORKDIR_ALIASES = {
    "sandbox": _WORKDIR_SANDBOX, "temp": _WORKDIR_SANDBOX, "tmp": _WORKDIR_SANDBOX,
    "default": _WORKDIR_SANDBOX, "isolated": _WORKDIR_SANDBOX,
    "workspace": _WORKDIR_WORKSPACE, "ws": _WORKDIR_WORKSPACE,
    "repo": _WORKDIR_WORKSPACE, "project": _WORKDIR_WORKSPACE,
}


def _resolve_workdir(workdir: str, ws: str | None) -> tuple[str | None, str]:
    """把 workdir 参数解析成真实路径；返回 (path, error_msg)"""
    key = str(workdir or _WORKDIR_SANDBOX).strip().lower()
    mode = _WORKDIR_ALIASES.get(key)
    if mode is None:
        return None, t(
            f"Error: workdir='{workdir}' 无效，只支持 'sandbox'（默认）或 'workspace'",
            f"Error: invalid workdir='{workdir}', only 'sandbox' (default) or 'workspace'",
        )
    if mode == _WORKDIR_WORKSPACE:
        if not ws:
            return None, t(
                "Error: workdir='workspace' 需要工作区路径，但当前会话没有工作区",
                "Error: workdir='workspace' needs a workspace path, but this session has none",
            )
        return ws, ""
    # sandbox 模式：交给 sandbox_run 自建临时目录（None = 由它 mkdtemp 并负责清理）
    return None, ""


@tool(
    name="run_python",
    description=L(
        "在沙箱里执行 Python 代码（需 --trust；高危代码会被 L1 预检拦截）。"
        "这是执行 Python 的首选方式——需要写代码验证/计算/试跑时用它，"
        "不要先 write_file 造一个 .py 再 run_command 执行（那条路径没有沙箱限额）。"
        "沙箱自带内存上限、超时杀进程树、敏感环境变量剥离，cwd 由 workdir 决定："
        "workdir='sandbox'（默认）= 系统临时目录，碰不到工作区；"
        "workdir='workspace' = 以工作区为 cwd，可用相对路径读写仓库文件"
        "（限额隔离照旧生效，但脚本写的文件会真实落进仓库）。"
        "add_path=true（默认）会把工作区加入 PYTHONPATH，便于 import 项目模块。",
        "Run Python code in a sandbox (requires --trust; high-risk code is blocked by the L1 pre-check). "
        "This is the preferred way to execute Python: use it for quick experiments, calculations and "
        "verification instead of writing a .py file with write_file and running it via run_command "
        "(that path has no sandbox limits). The sandbox enforces a memory cap, kills the process tree "
        "on timeout and strips sensitive env vars; cwd is chosen by workdir: "
        "workdir='sandbox' (default) = system temp dir with no access to the workspace; "
        "workdir='workspace' = cwd is the workspace, so relative paths reach repo files "
        "(limits still apply, but files the script writes really land in the repo). "
        "add_path=true (default) puts the workspace on PYTHONPATH so project modules can be imported.",
    ),
    parameters={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": L("要执行的 Python 源码", "Python source code to run")},
            "timeout": {"type": "number", "description": L("超时秒数", "Timeout in seconds"), "default": 15},
            "mem_mb": {"type": "integer", "description": L("内存上限 MB", "Memory limit in MB"), "default": 256},
            "workdir": {
                "type": "string",
                "enum": [_WORKDIR_SANDBOX, _WORKDIR_WORKSPACE],
                "description": L(
                    "'sandbox'（默认，临时目录隔离）或 'workspace'（以工作区为 cwd，可用相对路径）",
                    "'sandbox' (default, isolated temp dir) or 'workspace' (cwd = workspace, relative paths work)",
                ),
                "default": _WORKDIR_SANDBOX,
            },
            "add_path": {
                "type": "boolean",
                "description": L(
                    "是否把工作区加入 PYTHONPATH（默认 true，便于 import 项目模块）",
                    "Put the workspace on PYTHONPATH (default true, so project modules are importable)",
                ),
                "default": True,
            },
        },
        "required": ["code"],
    },
    requires_trust=True,
)
def run_python(code: str, timeout: float = 15, mem_mb: int = 256,
               workdir: str = _WORKDIR_SANDBOX, add_path: bool = True,
               _ctx=None) -> str:
    # L1 预检：高危直接拒（ctypes/exec/eval 这类能逃逸 Job Object）
    cls = classify(code)
    if cls["risk"] == "high":
        return (t(f"Error: L1 预检拦截（risk=high, type={cls['cmd_type']}）: ",
                  f"Error: blocked by L1 pre-check (risk=high, type={cls['cmd_type']}): ")
                + "; ".join(cls["reasons"]))

    ws = str(_ctx["workspace"]) if _ctx and _ctx.get("workspace") else None
    cwd, err = _resolve_workdir(workdir, ws)
    if err:
        return err

    # 注入 PYTHONPATH 让脚本能 import 项目模块（_clean_env 之后覆盖，不会被剥离）
    extra_env = {}
    if add_path and ws:
        parts = [ws]
        if os.environ.get("PYTHONPATH"):
            parts.append(os.environ["PYTHONPATH"])
        extra_env["PYTHONPATH"] = os.pathsep.join(parts)
        # 不让 import 在工作区里生成 __pycache__：跑一段模型代码不该把仓库弄脏
        extra_env["PYTHONDONTWRITEBYTECODE"] = "1"

    # L3 沙箱执行（Windows Job Object + 环境清理 + TerminateJobObject）
    rc, out = sandbox_run(code, timeout=timeout, mem_mb=mem_mb,
                          cwd=cwd, extra_env=extra_env)
    where = _WORKDIR_WORKSPACE if cwd else _WORKDIR_SANDBOX
    return f"[exit={rc} risk={cls['risk']} type={cls['cmd_type']} cwd={where}]\n{out}"
