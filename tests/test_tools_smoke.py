"""工具层 smoke 测试：通过 execute_tool 走真实分发链路，逐个验证注册的工具。

覆盖两层：
1. registry 层：schema 完整性 / requires_trust / readonly 标记 / 未知工具
2. 行为层：每个工具的正常路径 + 关键防御路径（越界、危险命令、保护路径…）

所有写操作都发生在 tempfile 临时工作区，不碰仓库本身。
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from VilAgent.tools import (
    _REGISTRY,
    execute_tool,
    get_tool_schemas,
    tool_is_readonly,
)

# 期望被注册的工具全集：少一个或多一个都应该让人看见
EXPECTED_TOOLS = {
    "read_file", "write_file", "delete_file", "search_code", "list_dir",
    "run_command",
    "git_status", "git_diff",
    "run_python",
    "grep_file",
    "edit_file",
    "todo_write",
}

READONLY_TOOLS = {
    "read_file", "search_code", "list_dir",
    "grep_file", "git_status", "git_diff", "todo_write",
}


class ToolTestCase(unittest.TestCase):
    """基类：准备一个临时工作区 + 完整 _ctx"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.sessions = self.ws / ".vil" / "sessions"
        self.sessions.mkdir(parents=True, exist_ok=True)
        self.ctx = {
            "workspace": str(self.ws),
            "trust": True,
            "mode": "do",
            "session_id": "test",
            "sessions_dir": str(self.sessions),
        }

    def tearDown(self):
        self._tmp.cleanup()

    def call(self, name, **args):
        return execute_tool(name, args, self.ctx)

    def write(self, rel, text):
        p = self.ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p


# ---------------------------------------------------------------- registry

class TestRegistry(unittest.TestCase):
    def test_all_expected_tools_registered(self):
        self.assertEqual(set(_REGISTRY), EXPECTED_TOOLS)

    def test_schema_shape(self):
        for name in EXPECTED_TOOLS:
            entry = _REGISTRY[name]
            schema = entry["schema"]
            self.assertEqual(schema["type"], "function")
            fn = schema["function"]
            self.assertEqual(fn["name"], name, f"{name}: schema 名不匹配")
            self.assertTrue(fn["description"], f"{name}: description 为空")
            self.assertEqual(fn["parameters"]["type"], "object")
            # required 里的每个键都必须在 properties 里声明
            props = fn["parameters"].get("properties", {})
            for key in fn["parameters"].get("required", []):
                self.assertIn(key, props, f"{name}: required '{key}' 未在 properties 声明")

    def test_i18n_schemas_resolve(self):
        """get_tool_schemas 在两种语言下都要能产出可 json 序列化的结果"""
        import json
        for schemas in (get_tool_schemas(), get_tool_schemas(readonly_only=True)):
            json.dumps(schemas)  # 抛异常即说明 description 仍是 L 对象未解析

    def test_readonly_tools(self):
        ro = {n for n in EXPECTED_TOOLS if tool_is_readonly(n)}
        self.assertEqual(ro, READONLY_TOOLS)

    def test_readonly_filter(self):
        names = {s["function"]["name"] for s in get_tool_schemas(readonly_only=True)}
        self.assertEqual(names, READONLY_TOOLS)

    def test_unknown_tool(self):
        self.assertIn("unknown tool", execute_tool("nope", {}, {"workspace": ".", "trust": True}))


# ---------------------------------------------------------------- dispatch

class TestDispatch(ToolTestCase):
    def test_trust_gate_blocks_write_tools(self):
        ctx = dict(self.ctx, trust=False)
        for name, args in [
            ("write_file", {"path": "a.txt", "content": "x"}),
            ("delete_file", {"path": "a.txt"}),
            ("edit_file", {"path": "a.txt", "old_text": "x", "new_text": "y"}),
            ("run_command", {"command": "echo hi"}),
            ("run_python", {"code": "print(1)"}),
        ]:
            out = execute_tool(name, args, ctx)
            self.assertIn("requires --trust", out, f"{name} 未受 trust 闸门约束")

    def test_readonly_tool_allowed_without_trust(self):
        self.write("a.txt", "hello\n")
        ctx = dict(self.ctx, trust=False)
        self.assertIn("hello", execute_tool("read_file", {"path": "a.txt"}, ctx))

    def test_bad_arguments_reported(self):
        out = self.call("read_file", bogus=1)
        self.assertIn("bad arguments", out)


# ---------------------------------------------------------------- file tools

class TestFileTools(ToolTestCase):
    def test_read_file_full_and_slice(self):
        self.write("a.txt", "l1\nl2\nl3\nl4\n")
        out = self.call("read_file", path="a.txt", limit=0)
        self.assertIn("l1", out)
        self.assertIn("l4", out)
        out = self.call("read_file", path="a.txt", offset=1, limit=2)
        self.assertIn("l2", out)
        self.assertIn("l3", out)
        self.assertNotIn("l1", out)
        self.assertNotIn("l4", out)

    def test_read_file_missing(self):
        self.assertIn("not a file", self.call("read_file", path="nope.txt"))

    def test_read_file_escape(self):
        self.assertIn("escapes workspace", self.call("read_file", path="../../etc/passwd"))

    def test_read_file_empty(self):
        self.write("empty.txt", "")
        self.assertEqual(self.call("read_file", path="empty.txt"), "(empty file)")

    def test_write_file_creates_dirs_and_reports(self):
        out = self.call("write_file", path="deep/dir/x.txt", content="abc")
        self.assertIn("wrote 3 chars", out)
        self.assertEqual((self.ws / "deep/dir/x.txt").read_text(encoding="utf-8"), "abc")

    def test_write_file_sets_file_diff(self):
        self.write("x.txt", "old")
        self.call("write_file", path="x.txt", content="new")
        diff = self.ctx.get("_file_diff")
        self.assertIsNotNone(diff, "write_file 未向 _ctx 写入 _file_diff")
        self.assertEqual((diff["path"], diff["old"], diff["new"]), ("x.txt", "old", "new"))

    def test_write_file_append(self):
        self.write("x.txt", "a")
        self.call("write_file", path="x.txt", content="b", append=True)
        self.assertEqual((self.ws / "x.txt").read_text(encoding="utf-8"), "ab")

    def test_write_file_escape(self):
        self.assertIn("escapes workspace", self.call("write_file", path="../evil.txt", content="x"))

    def test_write_file_chinese_roundtrip(self):
        self.call("write_file", path="cn.txt", content="中文内容 ✅")
        self.assertEqual(self.call("read_file", path="cn.txt", limit=0).strip().split("| ")[-1],
                         "中文内容 ✅")


class TestDeleteFile(ToolTestCase):
    def test_delete_ok(self):
        self.write("gone.txt", "x")
        self.assertEqual(self.call("delete_file", path="gone.txt"), "deleted gone.txt")
        self.assertFalse((self.ws / "gone.txt").exists())

    def test_delete_sets_file_diff(self):
        self.write("gone.txt", "x")
        self.call("delete_file", path="gone.txt")
        self.assertEqual(self.ctx["_file_diff"], {"path": "gone.txt", "old": "x", "new": ""})

    def test_delete_rejects_wildcard(self):
        out = self.call("delete_file", path="*.py")
        self.assertIn("wildcards not supported", out)

    def test_delete_rejects_protected(self):
        self.write("README.md", "doc")
        out = self.call("delete_file", path="README.md")
        self.assertIn("protected", out)
        self.assertTrue((self.ws / "README.md").exists())

    def test_delete_rejects_vil_metadata(self):
        self.write(".vil/config.json", "{}")
        out = self.call("delete_file", path=".vil/config.json")
        self.assertIn("protected", out)

    def test_delete_rejects_escape(self):
        self.assertIn("escapes workspace", self.call("delete_file", path="../x.txt"))

    def test_delete_missing(self):
        self.assertIn("not a file", self.call("delete_file", path="nope.txt"))


# ---------------------------------------------------------------- edit

class TestEditFile(ToolTestCase):
    def test_edit_unique(self):
        self.write("e.py", "x = 1\ny = 2\n")
        out = self.call("edit_file", path="e.py", old_text="x = 1", new_text="x = 99")
        self.assertIn("1 occurrence", out)
        self.assertIn("x = 99", (self.ws / "e.py").read_text(encoding="utf-8"))

    def test_edit_ambiguous_refused(self):
        self.write("e.py", "a\na\n")
        out = self.call("edit_file", path="e.py", old_text="a", new_text="b")
        self.assertIn("appears 2 times", out)
        self.assertEqual((self.ws / "e.py").read_text(encoding="utf-8"), "a\na\n")

    def test_edit_replace_all(self):
        self.write("e.py", "a\na\n")
        out = self.call("edit_file", path="e.py", old_text="a", new_text="b", replace_all=True)
        self.assertIn("2 occurrence", out)
        self.assertEqual((self.ws / "e.py").read_text(encoding="utf-8"), "b\nb\n")

    def test_edit_not_found(self):
        self.write("e.py", "x\n")
        self.assertIn("old_text not found", self.call("edit_file", path="e.py", old_text="zzz", new_text="y"))

    def test_edit_missing_file(self):
        self.assertIn("not a file", self.call("edit_file", path="nope.py", old_text="a", new_text="b"))

    def test_edit_multiline(self):
        self.write("e.py", "def f():\n    return 1\n")
        self.call("edit_file", path="e.py",
                  old_text="def f():\n    return 1",
                  new_text="def f():\n    return 2")
        self.assertIn("return 2", (self.ws / "e.py").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- grep

class TestGrepFile(ToolTestCase):
    def test_grep_dir_recursive(self):
        self.write("a.py", "needle = 1\n")
        self.write("sub/b.py", "x = needle\n")
        self.write("sub/c.py", "nothing\n")
        out = self.call("grep_file", pattern="needle", path=".")
        self.assertIn("a.py:1", out)
        self.assertIn(("sub/b.py" if os.sep == "/" else "sub\\b.py"), out)
        self.assertNotIn("c.py", out)

    def test_grep_single_file(self):
        self.write("a.py", "needle\n")
        out = self.call("grep_file", pattern="needle", path="a.py")
        self.assertIn("needle", out)

    def test_grep_case_insensitive(self):
        self.write("a.py", "NeEdLe\n")
        self.assertIn("NeEdLe", self.call("grep_file", pattern="needle", path="."))

    def test_grep_max_results_capped(self):
        self.write("many.txt", "\n".join("hit" for _ in range(300)))
        out = self.call("grep_file", pattern="hit", path="many.txt", max_results=1000)
        hits = [ln for ln in out.splitlines() if not ln.startswith("...")]
        self.assertLessEqual(len(hits), 200, "max_results 硬上限 200 未生效")
        self.assertIn("truncated", out)

    def test_grep_no_match_and_missing_path(self):
        self.write("a.py", "x\n")
        self.assertIn("No matches found", self.call("grep_file", pattern="zzz", path="."))
        self.assertIn("path not found", self.call("grep_file", pattern="x", path="nodir"))

    def test_grep_dir_escape_blocked(self):
        """目录分支必须显式拦越界（不能靠 relative_to 抛 ValueError 兜底）"""
        out = self.call("grep_file", pattern="x", path="..")
        self.assertIn("escapes workspace", out)
        self.assertNotIn("ValueError", out)


# ---------------------------------------------------------------- list_dir

class TestListDir(ToolTestCase):
    def test_lists_dirs_and_files_with_prefix(self):
        self.write("a.txt", "x")
        (self.ws / "sub").mkdir()
        out = self.call("list_dir", path=".")
        self.assertIn("[DIR] sub", out)
        self.assertIn("a.txt", out)

    def test_empty_dir(self):
        (self.ws / "empty").mkdir()
        self.assertEqual(self.call("list_dir", path="empty"), "(empty directory)")

    def test_not_a_directory(self):
        self.write("a.txt", "x")
        self.assertIn("not a directory", self.call("list_dir", path="a.txt"))

    def test_escape_blocked(self):
        self.assertIn("escapes workspace", self.call("list_dir", path=".."))

    def test_includes_hidden_dirs(self):
        """list_dir 是给人看目录结构的，隐藏目录也要列出来（不同于 grep 的忽略策略）"""
        (self.ws / ".hidden_dir").mkdir(exist_ok=True)
        self.assertIn(".hidden_dir", self.call("list_dir", path="."))


# ---------------------------------------------------------------- search_code

class TestSearchCode(ToolTestCase):
    def test_finds_match_with_line_number(self):
        self.write("m.py", "x = 1\nneedle_here = 2\n")
        out = self.call("search_code", query="needle_here")
        self.assertIn("m.py", out)
        self.assertIn("2", out)
        self.assertIn("needle_here = 2", out)

    def test_no_match(self):
        self.write("m.py", "x = 1\n")
        self.assertIn("no matches", self.call("search_code", query="zzz_absent"))

    def test_glob_filters_files(self):
        self.write("m.py", "needle_here\n")
        self.write("m.txt", "needle_here\n")
        out = self.call("search_code", query="needle_here", glob="**/*.py")
        self.assertIn("m.py", out)
        self.assertNotIn("m.txt", out)

    def test_only_searches_workspace(self):
        """工作区外的内容不能出现在结果里"""
        self.write("m.py", "needle_here\n")
        out = self.call("search_code", query="needle_here")
        self.assertNotIn(str(self.ws.parent), out)


# ---------------------------------------------------------------- run_command

class TestRunCommand(ToolTestCase):
    def test_echo(self):
        out = self.call("run_command", command="echo hello")
        self.assertEqual(out, "hello")

    @unittest.expectedFailure
    def test_nonzero_exit_reported(self):
        """已知差距：run_command 不回传退出码，静默失败的命令只得到 "(no output)"。

        LLM 因此无法判断命令是否成功（对比 run_python 会打印 [exit=rc]）。
        修法：非 0 时在输出前加 f"[exit={result.returncode}]"。
        """
        out = self.call("run_command", command="exit 3")
        self.assertIn("exit=3", out)
        self.assertNotIn("(no output)", out)

    def test_cwd_is_workspace(self):
        self.write("marker.txt", "x")
        out = self.call("run_command", command="ls" if os.name != "nt" else "dir")
        self.assertIn("marker.txt", out)

    def test_dangerous_command_blocked(self):
        out = self.call("run_command", command="rm -rf /")
        self.assertIn("blocked dangerous command", out)

    def test_timeout(self):
        cmd = "sleep 5" if os.name != "nt" else "ping -n 6 127.0.0.1 >nul"
        out = self.call("run_command", command=cmd, timeout=0.5)
        self.assertIn("timed out", out.lower())

    def test_unicode_output(self):
        out = self.call("run_command", command="echo 中文")
        self.assertIn("中文", out)

    def test_stderr_captured(self):
        # 用可控的 stderr 输出，避开各 shell / 系统语言的报错文案差异
        cmd = '"%s" -c "import sys; sys.stderr.write(\'boom-stderr\')"' % sys.executable
        out = self.call("run_command", command=cmd)
        self.assertIn("boom-stderr", out)


# ---------------------------------------------------------------- run_python

class TestRunPython(ToolTestCase):
    def test_hello(self):
        out = self.call("run_python", code="print('hello sandbox')")
        self.assertIn("exit=0", out)
        self.assertIn("hello sandbox", out)

    def test_exit_code_propagates(self):
        out = self.call("run_python", code="raise SystemExit(7)")
        self.assertIn("exit=7", out)

    def test_l1_blocks_high_risk(self):
        out = self.call("run_python", code="import ctypes")
        self.assertIn("L1", out)
        self.assertIn("risk=high", out)

    def test_cwd_is_not_workspace(self):
        """沙箱 cwd 应在临时目录，而不是工作区（否则相对路径会误伤仓库）"""
        out = self.call("run_python", code="import os; print(os.getcwd())")
        self.assertNotIn(str(self.ws), out)

    def test_timeout_kills(self):
        out = self.call("run_python", code="import time; time.sleep(10)", timeout=0.5)
        self.assertIn("timeout", out.lower())

    def test_memory_limit(self):
        """内存上限要么生效，要么显式告警——绝不允许静默降级

        Windows 未装 pywin32 / 其它无强约束实现的平台会走 fallback 路径，
        此时 mem_mb 无法落实，但输出里必须带 [sandbox] 警告文案。
        """
        out = self.call("run_python",
                        code="x = bytearray(512 * 1024 * 1024)",
                        mem_mb=64)
        if "exit=0" not in out:
            return  # 限制生效：被测代码没跑完（MemoryError 或被回收）
        self.assertIn("mem_mb", out,
                      f"内存限制静默失效（既没拦下也没告警）: {out}")


# ---------------------------------------------------------------- git

class TestGitTools(ToolTestCase):
    def setUp(self):
        super().setUp()
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(["git", "init", "-q"], cwd=self.ws, check=True, env=env)
        self.write("a.txt", "v1\n")
        subprocess.run(["git", "add", "a.txt"], cwd=self.ws, check=True, env=env)

    def run_git_clean_check(self):
        return subprocess.run(["git", "status", "--porcelain"], cwd=self.ws,
                              capture_output=True, text=True, encoding="utf-8")

    def test_git_status_dirty(self):
        out = self.call("git_status")
        self.assertIn("a.txt", out)

    def test_git_status_clean(self):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        subprocess.run(["git", "commit", "-qm", "init"], cwd=self.ws, check=True, env=env)
        self.assertEqual(self.call("git_status"), "working tree clean")

    def test_git_diff_unstaged(self):
        self.call("write_file", path="a.txt", content="v2\n")
        out = self.call("git_diff")
        self.assertIn("-v1", out)
        self.assertIn("+v2", out)

    def test_git_diff_specific_file_and_no_changes(self):
        self.assertEqual(self.call("git_diff"), "no changes")
        self.call("write_file", path="a.txt", content="v2\n")
        self.write("b.txt", "staged\n")
        self.assertIn("v1", self.call("git_diff", file="a.txt"))

    def test_git_diff_staged(self):
        self.call("write_file", path="a.txt", content="v2\n")
        subprocess.run(["git", "add", "a.txt"], cwd=self.ws, check=True)
        out = self.call("git_diff", staged=True)
        self.assertIn("+v2", out)


# ---------------------------------------------------------------- todo

class TestTodoWrite(ToolTestCase):
    def test_save_and_persist(self):
        todos = [
            {"content": "step one", "status": "completed"},
            {"content": "step two", "status": "in_progress"},
        ]
        out = self.call("todo_write", todos=todos)
        self.assertTrue(out)
        path = self.sessions / "test.todo.json"
        self.assertTrue(path.exists(), "todo 未落盘")
        import json
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")),
                         [{"content": "step one", "status": "completed"},
                          {"content": "step two", "status": "in_progress"}])

    def test_whole_list_replace(self):
        self.call("todo_write", todos=[{"content": "a", "status": "pending"}])
        self.call("todo_write", todos=[{"content": "b", "status": "completed"}])
        import json
        data = json.loads((self.sessions / "test.todo.json").read_text(encoding="utf-8"))
        self.assertEqual(data, [{"content": "b", "status": "completed"}])

    def test_extra_fields_stripped(self):
        self.call("todo_write", todos=[{"content": "a", "status": "pending", "junk": 1}])
        import json
        data = json.loads((self.sessions / "test.todo.json").read_text(encoding="utf-8"))
        self.assertEqual(set(data[0]), {"content", "status"})

    def test_invalid_status(self):
        out = self.call("todo_write", todos=[{"content": "a", "status": "doing"}])
        self.assertIn("pending/in_progress/completed", out)

    def test_empty_content(self):
        out = self.call("todo_write", todos=[{"content": "", "status": "pending"}])
        self.assertIn("content", out)

    def test_missing_session_info(self):
        ctx = dict(self.ctx, session_id="", sessions_dir="")
        out = execute_tool("todo_write", {"todos": []}, ctx)
        self.assertIn("session", out)


if __name__ == "__main__":
    unittest.main()
