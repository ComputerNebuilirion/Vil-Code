"""Python 代码执行工具：串联 L1 分类 → L2 权限 → L3 沙箱"""
from . import tool
from ..i18n import L, t
from ..safety import classify, sandbox_run


@tool(
    name="run_python",
    description=L(
        "在沙箱里执行 Python 代码（需 --trust；高危代码会被 L1 预检拦截）。"
        "注意：脚本运行在系统临时目录（Linux 为 /tmp，Windows 通常为 C 盘）里的独立沙箱中，"
        "cwd 不是工作区，且环境变量已清理；因此不能用相对路径访问工作区文件。"
        "需要读工作区数据时，请先用 read_file/search_code 取出内容，"
        "再在代码里内联传入或使用绝对路径。",
        "Run Python code in a sandbox (requires --trust; high-risk code is blocked by the L1 pre-check). "
        "Note: scripts run in an isolated sandbox under the system temp dir "
        "(/tmp on Linux, usually C: on Windows), the cwd is NOT the workspace, and environment "
        "variables are cleared; so you cannot use relative paths to reach workspace files. "
        "To read workspace data, first fetch it with read_file/search_code, "
        "then inline it or pass an absolute path in the code.",
    ),
    parameters={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": L("要执行的 Python 源码", "Python source code to run")},
            "timeout": {"type": "number", "description": L("超时秒数", "Timeout in seconds"), "default": 15},
            "mem_mb": {"type": "integer", "description": L("内存上限 MB", "Memory limit in MB"), "default": 256},
        },
        "required": ["code"],
    },
    requires_trust=True,
)
def run_python(code: str, timeout: float = 15, mem_mb: int = 256, _ctx=None) -> str:
    # L1 预检：高危直接拒（ctypes/exec/eval 这类能逃逸 Job Object）
    cls = classify(code)
    if cls["risk"] == "high":
        return (t(f"Error: L1 预检拦截（risk=high, type={cls['cmd_type']}）: ",
                  f"Error: blocked by L1 pre-check (risk=high, type={cls['cmd_type']}): ")
                + "; ".join(cls["reasons"]))

    # L3 沙箱执行（Windows Job Object + 环境清理 + TerminateJobObject）
    rc, out = sandbox_run(code, timeout=timeout, mem_mb=mem_mb)
    return f"[exit={rc} risk={cls['risk']} type={cls['cmd_type']}]\n{out}"
