import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "done_gate.py"
spec = importlib.util.spec_from_file_location("done_gate", HOOK)
dg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dg)


class T:
    """Build a transcript the way Claude Code writes it."""
    def __init__(self):
        self.lines, self.n = [], 0

    def prompt(self, text="fix the bug"):
        self.lines.append({"type": "user", "message": {"role": "user", "content": text}})
        return self

    def say(self, text):
        self.lines.append({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})
        return self

    def tool(self, name, inp, out="", error=False):
        self.n += 1
        tid = f"t{self.n}"
        self.lines.append({"type": "assistant", "message": {"role": "assistant",
                           "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}})
        self.lines.append({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "is_error": error, "content": out}]}})
        return self

    def bash(self, cmd, out="", error=False):
        return self.tool("Bash", {"command": cmd}, out, error)

    def edit(self, path="src/app.py", old="a", new="b"):
        return self.tool("Edit", {"file_path": path, "old_string": old, "new_string": new}, "ok")

    def judge(self, final=None):
        return dg.judge(dg.events(self.lines), final)


class Judge(unittest.TestCase):
    def v(self, t, final=None):
        return t.judge(final)["kind"] or t.judge(final)["verdict"]

    def test_no_claim(self):
        self.assertEqual(self.v(T().prompt().edit().say("Changed the parser.")), "NO_CLAIM")

    def test_verified(self):
        t = T().prompt().edit().bash("pytest -q", "12 passed in 0.4s").say("Fixed. All tests pass.")
        self.assertEqual(self.v(t), "PASS")

    def test_claim_without_run(self):
        t = T().prompt().edit().say("Fixed it, all tests pass.")
        self.assertEqual(self.v(t), "tests_not_run")

    def test_edit_after_run(self):
        t = T().prompt().bash("pytest", "5 passed").edit().say("Done, tests pass.")
        self.assertEqual(self.v(t), "tests_stale")

    def test_doc_edit_after_run_is_fine(self):
        t = T().prompt().edit().bash("pytest", "5 passed").edit("README.md").say("Done, tests pass.")
        self.assertEqual(self.v(t), "PASS")

    def test_failed_exit(self):
        t = T().prompt().edit().bash("npm test", "Exit code 1\nTests: 2 failed, 40 passed", error=True).say("All tests pass.")
        self.assertEqual(self.v(t), "tests_failed")

    def test_failed_hidden_by_pipe(self):
        t = T().prompt().edit().bash("pytest | tail -3", "==== 1 failed, 11 passed in 2s ====").say("Tests pass now.")
        self.assertEqual(self.v(t), "tests_failed")

    def test_subset_with_all(self):
        t = T().prompt().edit().bash("pytest tests/test_parser.py", "8 passed").say("All tests pass.")
        self.assertEqual(self.v(t), "tests_subset")

    def test_subset_without_all_is_fine(self):
        t = T().prompt().edit().bash("pytest tests/test_parser.py", "8 passed").say("The parser tests pass.")
        self.assertEqual(self.v(t), "PASS")

    def test_go_and_cargo(self):
        t = T().prompt().edit().bash("go test ./...", "--- FAIL: TestX\nFAIL\tpkg\t0.1s", error=True).say("All tests pass.")
        self.assertEqual(self.v(t), "tests_failed")
        t = T().prompt().edit().bash("cargo test", "test result: ok. 9 passed; 0 failed").say("All 9 tests pass.")
        self.assertEqual(self.v(t), "PASS")

    def test_disclosed_failure_not_a_claim(self):
        t = T().prompt().edit().bash("pytest", "1 failed, 9 passed").say("Tests pass except test_timeout, which is flaky.")
        self.assertEqual(self.v(t), "NO_CLAIM")

    def test_negation_not_a_claim(self):
        t = T().prompt().edit().say("Tests should pass once you install the deps.")
        self.assertEqual(self.v(t), "NO_CLAIM")

    def test_build_claim(self):
        t = T().prompt().edit().say("Build succeeds.")
        self.assertEqual(self.v(t), "build_not_run")
        t = T().prompt().edit().bash("npm run build", "npm ERR! code ELIFECYCLE", error=True).say("The build passes.")
        self.assertEqual(self.v(t), "build_failed")
        t = T().prompt().edit().bash("npm run build", "compiled successfully").say("The build passes.")
        self.assertEqual(self.v(t), "PASS")

    def test_weakened_test(self):
        t = T().prompt().edit("tests/test_api.py", "def test_a():\n    assert f() == 2\n", "def test_a():\n    pass\n") \
            .bash("pytest", "3 passed").say("Fixed, all tests pass.")
        self.assertEqual(self.v(t), "tests_weakened")

    def test_weakened_but_disclosed(self):
        t = T().prompt().edit("src/x.test.ts", "it('a', () => {", "it.skip('a', () => {") \
            .bash("npx vitest run", "Tests  4 passed | 1 skipped").say("All tests pass. I skipped the 'a' test because it needs network.")
        self.assertEqual(self.v(t), "PASS")

    def test_previous_turn_run_counts_if_no_edit_since(self):
        t = T().prompt().edit().bash("pytest", "5 passed").say("ok").prompt("are tests passing?").say("Yes, all tests pass.")
        self.assertEqual(self.v(t), "PASS")

    def test_last_assistant_message_wins(self):
        t = T().prompt().edit().bash("pytest", "5 passed").say("Tests pass.")
        self.assertEqual(self.v(t, final="I refactored the module; tests pass."), "PASS")
        t = T().prompt().edit().say("Tests pass.")
        self.assertEqual(self.v(t, final="Refactored the module."), "NO_CLAIM")

    def test_table_and_checkbox_ignored(self):
        t = T().prompt().edit().say("| Check | Status |\n| tests pass | yes |\n- [ ] All tests pass")
        self.assertEqual(self.v(t), "NO_CLAIM")


class Hook(unittest.TestCase):
    def run_hook(self, t, final, home):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write("\n".join(json.dumps(x) for x in t.lines) + "\n{\"partial")
        data = {"session_id": "s1", "transcript_path": f.name, "hook_event_name": "Stop",
                "stop_hook_active": False, "last_assistant_message": final}
        env = dict(os.environ, DONE_GATE_HOME=home)
        r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(data), capture_output=True, text=True, env=env)
        os.unlink(f.name)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout) if r.stdout.strip() else None

    def test_blocks_then_gives_up(self):
        with tempfile.TemporaryDirectory() as home:
            t = T().prompt().edit()
            for _ in range(3):
                out = self.run_hook(t, "All tests pass.", home)
                self.assertEqual(out["decision"], "block")
                self.assertIn("no test command ran", out["reason"])
            out = self.run_hook(t, "All tests pass.", home)
            self.assertNotIn("decision", out)
            self.assertIn("systemMessage", out)
            t.bash("pytest", "4 passed")
            self.assertIsNone(self.run_hook(t, "All tests pass.", home))
            st = subprocess.run([sys.executable, str(HOOK), "--stats"], capture_output=True, text=True,
                                env=dict(os.environ, DONE_GATE_HOME=home)).stdout
            self.assertIn("3 unproven or false 'done' claims stopped, 1 claims verified", st)

    def test_notify_commands(self):
        with tempfile.TemporaryDirectory() as home:
            marker = Path(home) / "calls.txt"
            script = Path(home) / "note.sh"
            script.write_text(f'#!/bin/sh\necho "$1" >> "{marker}"\n')
            script.chmod(0o755)
            os.environ["DONE_GATE_ON_BLOCK"] = f"{script} block"
            os.environ["DONE_GATE_ON_PASS"] = f"{script} pass"
            try:
                t = T().prompt().edit()
                self.run_hook(t, "All tests pass.", home)
                t.bash("pytest", "4 passed")
                self.run_hook(t, "All tests pass.", home)
            finally:
                del os.environ["DONE_GATE_ON_BLOCK"], os.environ["DONE_GATE_ON_PASS"]
            import time
            for _ in range(20):
                if marker.exists() and len(marker.read_text().split()) >= 2:
                    break
                time.sleep(0.1)
            self.assertEqual(sorted(marker.read_text().split()), ["block", "pass"])

    def test_bad_input_never_blocks(self):
        r = subprocess.run([sys.executable, str(HOOK)], input="not json", capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
