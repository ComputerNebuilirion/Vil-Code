"""L3 沙箱执行层（Windows Job Object / POSIX rlimit）

Windows + pywin32: Job Object 限制 内存/CPU，环境清理剥离敏感变量，
                   超时用 TerminateJobObject 杀整个 Job（含后代）。
POSIX (Linux/macOS): resource.setrlimit 限内存(RLIMIT_AS)/CPU(RLIMIT_CPU)/句柄数，
                   start_new_session + killpg 杀整个进程组（含孙进程）。
兜底:              subprocess + timeout（best-effort，非真正沙箱；会显式告警）。
"""
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from .ast_check import check_safety
from ..i18n import t

# ---- 平台能力检测（三态分派：Job Object → rlimit → 兜底）----
# Windows pywin32 检测
if sys.platform == "win32":
    try:
        import win32job
        import win32con
        import win32api
        _HAS_WIN32 = True
    except ImportError:
        _HAS_WIN32 = False
else:
    _HAS_WIN32 = False

# POSIX rlimit 检测（Linux/macOS 标准库 resource）
if os.name == "posix":
    try:
        import resource
        _HAS_RLIMIT = True
    except ImportError:  # 极少数裁剪构建没有 resource
        resource = None  # type: ignore[assignment]
        _HAS_RLIMIT = False
else:
    resource = None  # type: ignore[assignment]
    _HAS_RLIMIT = False


# 兜底路径无法落实 mem_mb 时的显式告警（避免用户误以为有内存限制）
def _no_limit_warning() -> str:
    """延迟取语言：警告在实际返回输出时才渲染（模块导入后再切语言也生效）"""
    return t(
        "[sandbox] 警告: 当前平台没有内存/CPU 限制实现，mem_mb 被忽略"
        "（best-effort 执行，隔离强度弱）",
        "[sandbox] warning: no memory/CPU limit implementation on this platform; "
        "mem_mb is ignored (best-effort execution, weak isolation)",
    )


def sandbox_run(code: str,
                timeout: float = 30,
                mem_mb: int = 256,
                cwd: str | None = None) -> tuple[int, str]:
    """
    在沙盒里执行一段 Python 代码

    Windows + pywin32: Job Object 限制内存/CPU，环境清理，TerminateJobObject 杀后代
    POSIX (Linux/macOS): rlimit 限内存/CPU/句柄，start_new_session + killpg 杀进程组
    其他: subprocess + timeout (best-effort，会在输出里显式告警)

    :return: (exit_code, 合并后的 stdout/stderr)
    """
    owned_cwd = cwd is None
    if owned_cwd:
        cwd = tempfile.mkdtemp(prefix="sandbox_")

    # L1 预检：高危代码不进沙箱（ctypes/exec/eval 能逃逸 Job Object）
    is_safe, msg = check_safety(code)
    if not is_safe:
        if owned_cwd:
            _rmtree(cwd)
        return -1, t(f"安全检查失败: {msg}", f"safety check failed: {msg}")

    env = _clean_env()

    try:
        if _HAS_WIN32:
            return _run_with_job_object(code, timeout, mem_mb, cwd, env)
        if _HAS_RLIMIT:
            return _run_with_rlimit(code, timeout, mem_mb, cwd, env)
        return _run_fallback(code, timeout, mem_mb, cwd, env)
    finally:
        # 用后清理：只删自己 mkdtemp 出来的目录，不动调用方传入的 cwd
        if owned_cwd:
            _rmtree(cwd)


def _rmtree(path: str, attempts: int = 3) -> None:
    """删除目录树（Windows 上文件可能被短暂占用，重试几次）；失败不抛异常"""
    for i in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if i < attempts - 1:
                time.sleep(0.1 * (i + 1))
    shutil.rmtree(path, ignore_errors=True)


def clean_stale_sandboxes() -> int:
    """
    清扫临时目录里历史遗留的 sandbox_* 目录（进程异常退出时会残留）

    仅删除本模块命名约定（sandbox_ 前缀）的目录，返回删除数量。
    """
    root = tempfile.gettempdir()
    removed = 0
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    for name in names:
        if not name.startswith("sandbox_"):
            continue
        full = os.path.join(root, name)
        if os.path.isdir(full):
            _rmtree(full)
            removed += 1
    return removed


def _clean_env(env: dict | None = None) -> dict:
    """剥离敏感环境变量（API key/token/secret/password）"""
    base = dict(env if env is not None else os.environ)
    sensitive = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH")
    for k in list(base.keys()):
        if any(s in k.upper() for s in sensitive):
            base.pop(k, None)
    # 显式清几个常见凭证变量名
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY",
              "VIL_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        base.pop(k, None)
    # 保留 PATH / SYSTEMROOT / TEMP 等基础变量，足以让 python 跑
    return base


def _run_with_job_object(code: str, timeout: float, mem_mb: int,
                         cwd: str, env: dict) -> tuple[int, str]:
    """Windows Job Object 沙箱执行"""
    hJob = win32job.CreateJobObject(None, "")

    basic_limit = win32job.QueryInformationJobObject(
        hJob, win32job.JobObjectExtendedLimitInformation)
    basic_limit['ProcessMemoryLimit'] = mem_mb * 1024 * 1024
    basic_limit['BasicLimitInformation']['LimitFlags'] |= (
        win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY |
        win32job.JOB_OBJECT_LIMIT_JOB_TIME
    )
    basic_limit['BasicLimitInformation']['PerJobUserTimeLimit'] = int(timeout * 10000000)
    win32job.SetInformationJobObject(
        hJob, win32job.JobObjectExtendedLimitInformation, basic_limit)

    # UI 限制：禁止关机/重启/注销
    ui_limit = win32job.QueryInformationJobObject(
        hJob, win32job.JobObjectBasicUIRestrictions)
    ui_limit['UIRestrictionsClass'] |= win32job.JOB_OBJECT_UILIMIT_EXITWINDOWS
    win32job.SetInformationJobObject(
        hJob, win32job.JobObjectBasicUIRestrictions, ui_limit)

    # 写临时脚本（uuid 后缀避免与本模块 sandbox.py 撞名，防子进程误 import 自身）
    script = os.path.join(cwd, f"sandbox_{uuid.uuid4().hex}.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(code)

    # 启动子进程（带清理后的 env）
    proc = subprocess.Popen(
        [sys.executable, script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8", errors="replace",
        cwd=cwd,
        env=env,
    )

    # 关进 Job（失败则退化为普通 timeout，不阻断执行）
    job_assigned = True
    try:
        win32job.AssignProcessToJobObject(
            hJob,
            win32api.OpenProcess(win32con.PROCESS_ALL_ACCESS, False, proc.pid)
        )
    except Exception:
        job_assigned = False

    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # 用 TerminateJobObject 杀整个 Job（含所有后代进程），比 proc.kill() 更彻底
        if job_assigned:
            try:
                win32job.TerminateJobObject(hJob, 1)
            except Exception:
                proc.kill()
        else:
            proc.kill()
        out = "SANDBOX TIMEOUT"
    finally:
        try:
            win32api.CloseHandle(hJob)
        except Exception:
            pass
        try:
            os.unlink(script)
        except OSError:
            pass

    return proc.returncode, out


def _resolve_mem_limit(mem_mb: int | None) -> tuple[int, str]:
    """把 mem_mb 换算成 RLIMIT_AS 字节数；被系统硬上限压住时返回告警文案

    实测（WSL2/Ubuntu + Python 3.14）：mem_mb 可与 RLIMIT_AS 一对一映射，
    无需下限补偿 —— 裸解释器 16MB 即可启动，import rich/asyncio 从 24MB 起通过。
    """
    if not mem_mb or mem_mb <= 0:
        return 0, ""  # 0 表示不设内存上限
    mem_bytes = int(mem_mb) * 1024 * 1024
    try:
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
    except (ValueError, OSError):
        return mem_bytes, ""
    if hard != resource.RLIM_INFINITY and mem_bytes > hard:
        hard_mb = hard // (1024 * 1024)
        note = t(
            f"[sandbox] 警告: 内存上限被系统硬上限压到 {hard_mb}MB（请求 {mem_mb}MB）",
            f"[sandbox] warning: memory limit clamped to {hard_mb}MB "
            f"by the system hard limit (requested {mem_mb}MB)",
        )
        return int(hard), note
    return mem_bytes, ""


def _kill_process_group(pid: int) -> None:
    """SIGKILL 整个进程组（`start_new_session=True` 保证 pgid == 子进程 pid）

    只 kill() 父进程会漏掉孙进程：实测 `sh -c 'sleep 300 & sleep 300 & wait'`
    会残留 1 个孙进程，改用 killpg 后残留清零。
    """
    if not hasattr(os, "killpg"):
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:  # ProcessLookupError / PermissionError 的子类，进程组已消失
        pass


def _run_with_rlimit(code: str, timeout: float, mem_mb: int,
                     cwd: str, env: dict) -> tuple[int, str]:
    """POSIX（Linux/macOS）rlimit 沙箱执行

    限制都在子进程 exec 前用 preexec_fn 落地：
      - RLIMIT_AS    : 虚拟地址空间上限（= mem_mb）；超限时被测代码拿到
                       Python 层 MemoryError（可在代码里 try/except 交代原因），
                       而不是被内核 OOM killer 静默 SIGKILL
      - RLIMIT_CPU   : CPU 时间兜底（略大于墙钟 timeout）
      - RLIMIT_NOFILE: 文件句柄上限，防句柄耗尽
    start_new_session=True（= setsid）让子进程成为新会话/进程组组长，
    超时用 killpg 回收整个进程组（含孙进程）。

    ⚠️ 三条隐式约束，重构时勿破坏：
      1. 一旦传入 preexec_fn，CPython 会放弃 posix_spawn 快路径改走 fork+exec；
         删掉 preexec_fn 会让内存限制**静默失效**。
      2. preexec_fn 在多线程进程中不安全（子进程可能继承父进程持有的锁而死锁）。
         当前仓库无线程池（Rich 交互由 asyncio 驱动），该前提成立；未来引入
         线程池需重新评估（可改用 `subprocess.Popen(..., user=...)` 或 cgroup）。
      3. **不要设 RLIMIT_NPROC**：它按 uid 全局计数，实测在 WSL2/root 下会让
         第一个 Popen 直接 EAGAIN，表现为「沙箱看起来坏了」。防 fork 炸弹
         应交给 cgroup pids.max。
    """
    script = os.path.join(cwd, f"sandbox_{uuid.uuid4().hex}.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(code)

    mem_bytes, limit_note = _resolve_mem_limit(mem_mb)
    cpu_seconds = max(1, int(math.ceil(timeout)) + 1)
    resource_mod = resource

    def _apply_limits() -> None:
        # 只在 fork 出的子进程里执行（preexec_fn）
        if mem_bytes > 0:
            try:
                resource_mod.setrlimit(resource_mod.RLIMIT_AS,
                                       (mem_bytes, mem_bytes))
            except (ValueError, OSError):
                pass
        try:
            resource_mod.setrlimit(resource_mod.RLIMIT_CPU,
                                   (cpu_seconds, cpu_seconds))
        except (ValueError, OSError):
            pass
        try:
            _, hard = resource_mod.getrlimit(resource_mod.RLIMIT_NOFILE)
            soft = 256 if hard == resource_mod.RLIM_INFINITY else min(256, hard)
            resource_mod.setrlimit(resource_mod.RLIMIT_NOFILE, (soft, hard))
        except (ValueError, OSError):
            pass

    proc = subprocess.Popen(
        [sys.executable, script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8", errors="replace",
        cwd=cwd,
        env=env,
        start_new_session=True,
        preexec_fn=_apply_limits,  # 勿删：删了内存限制会静默失效（见 docstring）
    )

    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_group(proc.pid)
        try:
            out, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out = ""
    finally:
        # 正常结束也可能留下后台孙进程，统一收尾（进程组已消失时是 no-op）
        _kill_process_group(proc.pid)
        try:
            os.unlink(script)
        except OSError:
            pass

    out = out or ""
    if limit_note:
        out = f"{out}\n{limit_note}" if out else limit_note

    if timed_out:
        return -1, "SANDBOX TIMEOUT" + (f"\n{limit_note}" if limit_note else "")
    return proc.returncode, out


def _run_fallback(code: str, timeout: float, mem_mb: int,
                  cwd: str, env: dict) -> tuple[int, str]:
    """无 Job Object 也无 rlimit：subprocess + timeout（best-effort）

    此路径**无法落实 mem_mb**，因此在输出里显式告警，避免用户误以为有内存限制。
    POSIX 上仍用 start_new_session + killpg 回收进程组，避免孙进程泄漏。
    """
    script = os.path.join(cwd, f"sandbox_{uuid.uuid4().hex}.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(code)

    posix = os.name == "posix"
    proc = subprocess.Popen(
        [sys.executable, script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8", errors="replace",
        cwd=cwd,
        env=env,
        start_new_session=True if posix else False,
    )

    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        if posix:
            _kill_process_group(proc.pid)
        else:
            proc.kill()
        try:
            out, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out = ""
    finally:
        if posix:
            _kill_process_group(proc.pid)
        try:
            os.unlink(script)
        except OSError:
            pass

    out = out or ""
    if timed_out:
        return -1, "SANDBOX TIMEOUT\n" + _no_limit_warning()
    return proc.returncode, f"{out}\n{_no_limit_warning()}" if out else _no_limit_warning()
