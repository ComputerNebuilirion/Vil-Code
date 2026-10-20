"""L3 沙箱 POSIX 分支测试（Linux / macOS）

断言的是 rlimit 与 killpg 的真实行为，Windows 上整组 skip
（Windows 走 Job Object 分支，不适用这些断言）。
"""
import os
import shutil
import tempfile
import time
import unittest

from VilAgent.safety.sandbox import sandbox_run

_SKIP_REASON = "POSIX-only (rlimit / killpg)"


def _pid_alive(pid: int) -> bool:
    """进程是否还活着（僵尸视为已死，避免误判为泄漏）"""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace") as f:
            state = f.read().rsplit(")", 1)[-1].split()[0]
        return state != "Z"
    except OSError:
        return True


@unittest.skipUnless(os.name == "posix", _SKIP_REASON)
class TestSandboxPosix(unittest.TestCase):
    def test_normal_code_exits_zero(self):
        rc, out = sandbox_run("print('hello sandbox')", timeout=15)
        self.assertEqual(rc, 0)
        self.assertIn("hello sandbox", out)

    def test_memory_limit_is_applied(self):
        """RLIMIT_AS 必须真的落到子进程（删掉 preexec_fn 会静默失效）"""
        code = (
            "import resource\n"
            "print('AS=%d' % resource.getrlimit(resource.RLIMIT_AS)[0])\n"
            "print('CPU=%d' % resource.getrlimit(resource.RLIMIT_CPU)[0])\n"
        )
        rc, out = sandbox_run(code, timeout=15, mem_mb=64)
        self.assertEqual(rc, 0, out)
        self.assertIn(f"AS={64 * 1024 * 1024}", out)
        # CPU 兜底：略大于墙钟 timeout
        self.assertNotIn("CPU=0", out)

    def test_memory_limit_raises_memory_error(self):
        """超限时被测代码拿到 MemoryError（而不是被 OOM killer 静默 -9）"""
        code = (
            "chunks = []\n"
            "try:\n"
            "    for _ in range(4000):\n"
            "        chunks.append(bytearray(1024 * 1024))\n"
            "except MemoryError:\n"
            "    print('MEMORY_LIMIT_HIT')\n"
        )
        rc, out = sandbox_run(code, timeout=30, mem_mb=64)
        self.assertIn("MEMORY_LIMIT_HIT", out)
        self.assertEqual(rc, 0, out)

    def test_timeout_kills_whole_process_group(self):
        """超时必须回收孙进程（只 kill 父进程会残留）"""
        tmpdir = tempfile.mkdtemp(prefix="vil_sandbox_test_")
        pidfile = os.path.join(tmpdir, "grandchild.pid")
        code = (
            "import subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c',\n"
            "                      'import time; time.sleep(300)'])\n"
            f"open({pidfile!r}, 'w').write(str(p.pid))\n"
            "time.sleep(300)\n"
        )
        try:
            rc, out = sandbox_run(code, timeout=3, mem_mb=256)
            self.assertEqual(rc, -1, out)
            self.assertIn("SANDBOX TIMEOUT", out)

            with open(pidfile, "r", encoding="utf-8") as f:
                grandchild = int(f.read().strip())

            deadline = time.time() + 5
            while time.time() < deadline and _pid_alive(grandchild):
                time.sleep(0.1)
            self.assertFalse(_pid_alive(grandchild),
                             f"grandchild pid {grandchild} leaked")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_safety_check_still_blocks_high_risk(self):
        """L1 预检在 POSIX 分支前拦截高危代码（契约仍是 (-1, msg)）"""
        rc, out = sandbox_run("import ctypes\nprint('nope')\n", timeout=10)
        self.assertEqual(rc, -1)
        self.assertNotIn("nope", out)


if __name__ == "__main__":
    unittest.main()
