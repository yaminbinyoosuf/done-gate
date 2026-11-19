#!/usr/bin/env python3
"""Done Gate: a Claude Code Stop hook that won't let the agent end its turn on an unproven
"tests pass" / "build succeeds", or after quietly weakening tests.

It reads the session transcript and checks the agent's final message against what actually ran:
  - claims tests pass, but no test command ran after the last code edit       -> block
  - the last test run after the last edit failed (exit code or failure output) -> block
  - says ALL tests pass, but only a subset ran (a file, -k, --grep ...)        -> block
  - same checks for "build succeeds"
  - edited a test file to add skips or remove assertions, without saying so   -> block once
When it blocks, the reason goes back to the agent, which then runs the tests or corrects itself.
At most 3 blocks in a row per turn; after that the user gets a warning instead.
Zero dependencies. Nothing leaves your machine: verdicts are logged locally (plugin data dir, or ~/.done-gate/).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

MAX_BLOCKS = 3
STATE = Path(os.environ.get("DONE_GATE_HOME") or os.environ.get("CLAUDE_PLUGIN_DATA") or Path.home() / ".done-gate")

# --- what counts as a claim (in the agent's final message) ---
TEST_CLAIM = re.compile(
    r"(?:all\s+)?(?:\d[\d,]*\s+)?(?:unit\s+|existing\s+|the\s+|new\s+)?tests?\s+(?:now\s+|still\s+)?"
    r"(?:pass(?:es|ed|ing)?|are\s+passing|(?:are\s+)?green|succeed(?:ed)?)\b|test suite (?:passes|passed|is green)", re.I)
BUILD_CLAIM = re.compile(r"\bbuild\s+(?:now\s+)?(?:passes|passed|succeeds|succeeded|is\s+green|works|is\s+clean)\b|"
                         r"\bbuilds\s+(?:cleanly|successfully)\b|\bcompiles\s+(?:cleanly|successfully|without errors)\b", re.I)
NOT_A_CLAIM = re.compile(r"\b(not|n't|no|never|fail\w*|until|once|if|when|should|will|would|could|might|make sure|ensure|"
                         r"verify|to pass|unable|couldn't|can't|didn't|before)\b", re.I)
DISCLOSED = re.compile(r"\bexcept\b|\bfail|\bbut\b|\bskipp?ed\b|\bunrelated\b|\bpre-?existing\b|\bflaky\b", re.I)
ALL = re.compile(r"\ball\b|\bfull\s+(?:test\s+)?suite\b|\bentire\b|\bevery\s+test\b", re.I)

# --- what counts as evidence (Bash commands in the transcript) ---
TEST_CMD = re.compile(
    r"\b(pytest|py\.test|python[\d.]*\s+(-\w+\s+)*-m\s+(pytest|unittest)|(npm|pnpm|yarn|bun)\s+(run\s+)?test\b|"
    r"npx\s+(jest|vitest|playwright\s+test|mocha)|\bjest\b|\bvitest\b|\bmocha\b|go\s+test|cargo\s+(test|nextest)|"
    r"\brspec\b|rake\s+test|rails\s+test|mvnw?\b.*\b(test|verify)\b|gradlew?\b.*\b(test|check)\b|make\s+(test|check)\b|"
    r"\btox\b|\bnox\b|phpunit|dotnet\s+test|swift\s+test|mix\s+test|\bctest\b|deno\s+test|bazel\s+test|"
    r"playwright\s+test|cypress\s+run)", re.I)
BUILD_CMD = re.compile(
    r"\b((npm|pnpm|yarn|bun)\s+(run\s+)?build|cargo\s+build|go\s+build|\btsc\b|\bmake\b|mvnw?\b.*\b(package|install|compile)\b|"
    r"gradlew?\b.*\b(build|assemble)\b|xcodebuild|swift\s+build|dotnet\s+build|next\s+build|vite\s+build|webpack)", re.I)
SUBSET = re.compile(r"\s-k\s|\s-t\s|--testNamePattern|--grep|--include|--filter|--testPathPattern|\s-run\s|::|"
                    r"\s[\w./-]+\.(?:py|tsx?|jsx?|mjs|rb|go|rs|java|kt|cs|php|ex|exs)\b|\s--\s+\S|--lf\b|--last-failed", re.I)
# Failure in the output even when the exit code was hidden (e.g. `pytest | tail`).
FAILED_COUNT = re.compile(r"\b[1-9]\d*\s+(?:failed|failing|failures?|errors?)\b|Tests?:\s+[1-9]\d*\s+failed|"
                          r"Test Files\s+[1-9]\d*\s+failed|FAILED \((?:failures|errors)=", re.I)
FAILED_MARK = re.compile(r"^FAIL\b|^FAILED\b|\bFAILED\s+\S+::|test result: FAILED|^not ok\b|BUILD FAILURE|BUILD FAILED|"
                         r"^error(?:\[E\d+\])?:\s|npm ERR!|ELIFECYCLE|Command failed with exit code", re.M)
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
DOC_FILE = re.compile(r"\.(md|mdx|txt|rst|adoc)$|(^|/)(CHANGELOG|LICENSE|README)[^/]*$", re.I)

# --- test weakening ---
TEST_FILE = re.compile(r"(^|/)(tests?|spec|__tests__)/|(^|/)test_[^/]+\.py$|_test\.(py|go|rb|exs)$|"
                       r"\.(test|spec)\.[jt]sx?$|Tests?\.(java|kt|cs|swift)$|_spec\.rb$", re.I)
SKIP = re.compile(r"@pytest\.mark\.(skip|xfail)|pytest\.skip\(|@unittest\.skip|\b(it|test|describe)\.(skip|todo)\(|"
                  r"\bx(it|describe|test)\(|\.only\(|t\.Skip\(|#\[ignore\]|@Disabled|@Ignore|\bskip\(", re.I)
ASSERT = re.compile(r"\bassert\w*|\bexpect\(|\.should\b|assert_eq!|assert!|t\.(Error|Fatal)f?\(|\brequire\.\w+\(", re.I)


def load(path: str) -> list[dict]:
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue  # the last line may still be being written
    except OSError:
        pass
    return out


def events(entries: list[dict]) -> list[dict]:
    """Flatten the transcript into ordered events: prompt, text, tool_use, tool_result."""
    ev, results = [], {}
    for e in entries:
        m = e.get("message") or {}
        c = m.get("content")
        if e.get("type") == "user":
            if isinstance(c, str):
                if not e.get("isMeta"):
                    ev.append({"k": "prompt"})
                continue
            for b in c or []:
                if b.get("type") == "tool_result":
                    content = b.get("content")
                    if isinstance(content, list):
                        content = "\n".join(x.get("text", "") for x in content if isinstance(x, dict))
                    results[b.get("tool_use_id")] = {"error": bool(b.get("is_error")), "out": str(content or "")}
                elif b.get("type") == "text" and not e.get("isMeta"):
                    ev.append({"k": "prompt"})
        elif e.get("type") == "assistant":
            for b in c or []:
                if b.get("type") == "text":
                    ev.append({"k": "text", "text": b.get("text", "")})
                elif b.get("type") == "tool_use":
                    ev.append({"k": "tool", "id": b.get("id"), "name": b.get("name"), "input": b.get("input") or {}})
    for x in ev:
        if x["k"] == "tool":
            x["result"] = results.get(x["id"])
    return ev


def claim_in(text: str, rx: re.Pattern) -> str | None:
    for s in re.split(r"[\n\r]+|(?<=[.!?])\s+", text):
        if s.lstrip().startswith("|") or re.match(r"\s*[-*]?\s*\[ \]", s):
            continue
        m = rx.search(s)
        if m and not NOT_A_CLAIM.search(s[:m.end()]) and not DISCLOSED.search(s):
            return re.sub(r"[*_`#>]", "", s).strip()[:200]
    return None


def is_code_edit(x: dict) -> bool:
    if x["name"] not in EDIT_TOOLS:
        return False
    p = x["input"].get("file_path") or x["input"].get("notebook_path") or ""
    return not DOC_FILE.search(p)


def run_failed(x: dict) -> str | None:
    r = x.get("result")
    if r is None:
        return "no result recorded"
    if r["error"]:
        first = r["out"].strip().splitlines()[:1]
        return first[0][:120] if first else "non-zero exit"
    m = FAILED_COUNT.search(r["out"]) or FAILED_MARK.search(r["out"])
    return m.group(0).strip()[:120] if m else None


def weakened(ev: list[dict]) -> list[str]:
    notes = []
    for x in ev:
        if x["k"] != "tool" or x["name"] not in ("Edit", "MultiEdit"):
            continue
        p = x["input"].get("file_path", "")
        if not TEST_FILE.search(p):
            continue
        edits = x["input"].get("edits") or [x["input"]]
        old = "".join(e.get("old_string", "") for e in edits)
        new = "".join(e.get("new_string", "") for e in edits)
        name = p.rsplit("/", 1)[-1]
        if len(SKIP.findall(new)) > len(SKIP.findall(old)):
            notes.append(f"added a skip/only marker in {name}")
        lost = len(ASSERT.findall(old)) - len(ASSERT.findall(new))
        if lost > 0:
            notes.append(f"removed {lost} assertion{'s' if lost > 1 else ''} in {name}")
    return notes


def judge(ev: list[dict], final: str | None = None) -> dict:
    """Return {"verdict": PASS|BLOCK|NO_CLAIM, "reason": str, "kind": str}."""
    starts = [i for i, x in enumerate(ev) if x["k"] == "prompt"]
    turn = ev[starts[-1] + 1:] if starts else ev
    last_tool = max((i for i, x in enumerate(ev) if x["k"] == "tool"), default=-1)
    if final is None:  # older Claude Code: no last_assistant_message, read it from the transcript
        final = "\n".join(x["text"] for x in ev[last_tool + 1:] if x["k"] == "text")
    last_edit = max((i for i, x in enumerate(ev) if x["k"] == "tool" and is_code_edit(x)), default=-1)
    notes = weakened(turn)

    for kind, claim_rx, cmd_rx, noun in (("tests", TEST_CLAIM, TEST_CMD, "test"), ("build", BUILD_CLAIM, BUILD_CMD, "build")):
        claim = claim_in(final, claim_rx)
        if not claim:
            continue
        runs = [(i, x) for i, x in enumerate(ev) if x["k"] == "tool" and x["name"] == "Bash"
                and cmd_rx.search(x["input"].get("command", ""))]
        after = [(i, x) for i, x in runs if i > last_edit]
        if not runs:
            return {"verdict": "BLOCK", "kind": f"{kind}_not_run",
                    "reason": f'You wrote "{claim}", but no {noun} command ran in this session. Run it now and '
                              f"report the real result, or remove the claim."}
        if not after:
            return {"verdict": "BLOCK", "kind": f"{kind}_stale",
                    "reason": f'You wrote "{claim}", but you edited code after the last {noun} run. '
                              f"Run it again on the current code and report the real result."}
        i, last = after[-1]
        cmd = last["input"].get("command", "").strip().splitlines()[0][:120]
        why = run_failed(last)
        if why:
            return {"verdict": "BLOCK", "kind": f"{kind}_failed",
                    "reason": f'You wrote "{claim}", but the last {noun} run (`{cmd}`) failed: {why}. '
                              f"Fix it, or tell the user plainly what fails."}
        if kind == "tests" and ALL.search(claim) and SUBSET.search(" " + cmd + " "):
            return {"verdict": "BLOCK", "kind": "tests_subset",
                    "reason": f'You wrote "{claim}", but the last run (`{cmd}`) ran only part of the suite. '
                              f"Run the full suite, or say exactly which tests ran."}
    if notes and not re.search(r"skip|assert|remov|disabl|weaken", final, re.I):
        return {"verdict": "BLOCK", "kind": "tests_weakened",
                "reason": "You " + "; ".join(notes) + ". If that was intended, say so plainly to the user in your "
                          "final message and why; otherwise restore the tests."}
    has_claim = claim_in(final, TEST_CLAIM) or claim_in(final, BUILD_CLAIM)
    return {"verdict": "PASS" if has_claim else "NO_CLAIM", "kind": "", "reason": ""}


def counter(session: str, reset: bool = False) -> int:
    f = STATE / "blocks" / (re.sub(r"[^\w-]", "", session)[:80] or "unknown")
    if reset:
        f.unlink(missing_ok=True)
        return 0
    f.parent.mkdir(parents=True, exist_ok=True)
    n = int(f.read_text() or 0) + 1 if f.exists() else 1
    f.write_text(str(n))
    return n


def log(rec: dict) -> None:
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        with open(STATE / "log.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def stats() -> str:
    rows = []
    try:
        rows = [json.loads(l) for l in open(STATE / "log.jsonl")]
    except (OSError, ValueError):
        pass
    caught = [r for r in rows if r.get("verdict") == "BLOCK"]
    kinds: dict[str, int] = {}
    for r in caught:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    lines = [f"Done Gate: {len(caught)} unproven or false 'done' claims stopped, "
             f"{sum(1 for r in rows if r.get('verdict') == 'PASS')} claims verified."]
    lines += [f"  {k}: {v}" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])]
    return "\n".join(lines)


def main() -> int:
    if "--stats" in sys.argv:
        print(stats())
        return 0
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    if os.environ.get("DONE_GATE_OFF"):
        return 0
    session = str(data.get("session_id", ""))
    result = judge(events(load(data.get("transcript_path", ""))), data.get("last_assistant_message"))
    rec = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "session": session[:12],
           "verdict": result["verdict"], "kind": result["kind"]}
    if result["verdict"] != "BLOCK":
        counter(session, reset=True)
        if result["verdict"] == "PASS":
            log(rec)
        return 0
    n = counter(session)
    if n > MAX_BLOCKS:
        counter(session, reset=True)
        rec["verdict"] = "GAVE_UP"
        log(rec)
        print(json.dumps({"systemMessage": "Done Gate: the agent still claims something it hasn't shown. "
                                           + result["reason"].replace("You wrote", "It wrote")}))
        return 0
    log(rec)
    print(json.dumps({"decision": "block", "reason": "Done Gate: " + result["reason"],
                      "systemMessage": "Done Gate stopped an unproven claim: " + result["kind"].replace("_", " ")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
