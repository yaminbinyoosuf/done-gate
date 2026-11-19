# Done Gate

A Claude Code plugin that won't let Claude end its turn on an unproven "tests pass".

Before Claude stops, Done Gate reads the session and checks the final message against what actually ran. If Claude says the tests pass (or the build succeeds) and the session doesn't show it, Done Gate sends Claude back to work with the reason. Claude then runs the tests, fixes the code, or tells you the truth.

## Install

In Claude Code:

```
/plugin marketplace add yaminbinyoosuf/done-gate
/plugin install done-gate@cogext
```

That's it. No config, no account, nothing leaves your machine.

## What it catches

| Claude's final message says | but the session shows | Done Gate tells Claude |
|---|---|---|
| "All tests pass" | no test command ran | run them and report the real result |
| "Tests pass" | code was edited after the last test run | run them again on the current code |
| "Tests pass" | the last run failed (exit code, or failures in the output even behind `\| tail`) | fix it, or say plainly what fails |
| "**All** tests pass" | only one file or a `-k` / `--grep` subset ran | run the full suite, or say which tests ran |
| "Build succeeds" | no build ran, or it failed | same checks for the build |
| anything | a test file got a new `skip` / `.only`, or lost assertions, and the message doesn't mention it | say so plainly, or restore the tests |

Sentences that already disclose a problem ("tests pass except the flaky e2e test", "440 of 448 pass") and hedges ("tests should pass once you install X") are not treated as claims.

## What it looks like

A real run. Claude was told to add a function, run nothing, and finish with "All tests pass." Done Gate blocked the stop twice. Claude's final answer:

> I haven't run the test suite, so I have no result to report. `subtract` is added to `calc.py`. `divide` still returns `a * b` and is marked `# bug`, so any test covering it will likely fail.

## Why

In public pull requests from AI coding agents that we check daily, about 1 in 30 that say "tests pass" have a failing test or build check in their own CI, and more than half of those get merged anyway. Usually the agent ran part of the suite and reported it as all of it. Data, updated daily: [cogextai.com/honesty](https://cogextai.com/honesty).

Done Gate catches it at the source, in the session, before it becomes a PR.

## Details

- Works with pytest, unittest, jest, vitest, mocha, Playwright, Cypress, go test, cargo test/nextest, rspec, Maven, Gradle, make test, tox, nox, PHPUnit, dotnet, swift, mix, deno, bazel, ctest; builds with npm/pnpm/yarn/bun build, cargo, go, tsc, make, Maven, Gradle, xcodebuild and others.
- Edits to docs (`.md`, `.txt`, `README`, `CHANGELOG`) don't make a test run stale.
- At most 3 blocks in a row; after that it stops blocking and shows you a warning instead, so it can never trap a session.
- Python 3 standard library only. One file: `hooks/done_gate.py`.
- Local stats: `python3 <plugin dir>/hooks/done_gate.py --stats` shows how many claims were stopped and verified.
- Turn it off for one session: start Claude Code with `DONE_GATE_OFF=1`. Remove it: `/plugin uninstall done-gate`.

## Limits

It judges what the session shows. It can't know about tests you ran in another terminal, and it reads the transcript format Claude Code uses today. If it blocks something it shouldn't, please [open an issue](https://github.com/yaminbinyoosuf/done-gate/issues) with the final message and the command that ran.

## Related

- [cogextai.com/check](https://cogextai.com/check): paste a public repo, see which of its PRs said tests pass while CI failed.
- [false-done-check](https://github.com/yaminbinyoosuf/false-done-check): the same check as a GitHub Action, on every PR.

MIT licensed. Made by [COGEXT](https://cogextai.com).
