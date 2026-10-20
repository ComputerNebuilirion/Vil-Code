"""安全边界测试：命令危险模式匹配（cmd_check）"""
import unittest

from VilAgent.safety.cmd_check import (
    classify_command,
    match_dangerous_command,
    normalize_command,
)


class TestNormalize(unittest.TestCase):
    def test_lowercase_and_collapse_whitespace(self):
        self.assertEqual(normalize_command("  RM\n  -RF  /  "), "rm -rf /")


class TestMatchDangerousCommand(unittest.TestCase):
    def test_plain_safe_commands(self):
        for cmd in ("ls -la", "git status", "echo hello",
                    "git log --format=%H", "python -c 'print(1)'"):
            self.assertIsNone(match_dangerous_command(cmd), cmd)

    def test_rm_recursive(self):
        self.assertIsNotNone(match_dangerous_command("rm -rf /"))

    def test_rm_case_insensitive(self):
        self.assertIsNotNone(match_dangerous_command("RM -RF /tmp"))

    def test_prefixed_sudo(self):
        self.assertIsNotNone(match_dangerous_command("sudo rm -rf /"))

    def test_chained_commands(self):
        self.assertIsNotNone(match_dangerous_command("echo hi && rm -rf /"))

    def test_windows_del(self):
        self.assertIsNotNone(match_dangerous_command("del important.txt"))

    def test_windows_del_after_chain(self):
        self.assertIsNotNone(match_dangerous_command("mkdir x && del x"))

    def test_format_not_confused_with_argument(self):
        # `--format` 不是命令起始，不应误伤
        self.assertIsNone(match_dangerous_command("git log --format=oneline"))

    def test_empty(self):
        self.assertIsNone(match_dangerous_command(""))

    # ---- Linux 专属强化项 ----
    def test_linux_fork_bomb(self):
        self.assertIsNotNone(match_dangerous_command(":(){ :|:& };:"))
        self.assertIsNotNone(match_dangerous_command(":() { :|: & };:"))

    def test_linux_chmod_root_recursive(self):
        self.assertIsNotNone(match_dangerous_command("chmod -R 777 /"))
        self.assertIsNotNone(match_dangerous_command("sudo chmod -R a+rwx /"))

    def test_linux_raw_disk_write(self):
        self.assertIsNotNone(match_dangerous_command("echo x > /dev/sda"))
        self.assertIsNotNone(match_dangerous_command("cat img > /dev/nvme0n1"))

    def test_common_safe_commands_not_flagged(self):
        # /dev/null、普通 chmod 目标、相对路径重定向都不应误伤
        for cmd in ("ls > /dev/null", "cat a > /dev/null 2>&1",
                    "chmod 755 ./run.sh", "echo ok > out.txt"):
            self.assertIsNone(match_dangerous_command(cmd), cmd)


class TestClassifyCommand(unittest.TestCase):
    def test_low(self):
        r = classify_command("ls -la")
        self.assertEqual(r["risk"], "low")

    def test_network_medium(self):
        r = classify_command("curl https://example.com")
        self.assertEqual(r["risk"], "medium")
        self.assertEqual(r["cmd_type"], "network")

    def test_dangerous_high(self):
        r = classify_command("rm -rf /")
        self.assertEqual(r["risk"], "high")
        self.assertEqual(r["cmd_type"], "exec")


if __name__ == "__main__":
    unittest.main()
