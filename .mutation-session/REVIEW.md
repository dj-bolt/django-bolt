# Review worker session (django-bolt)

About 275 pull requests are open in dj-bolt/django-bolt from a mutation-testing effort. The repository owner said:
"Start merging these PRs if their CI is green and fix if it is not. Do simple review before merging."

You do the simple review for the pull requests of "YOUR GROUP" at the end of your task text. A coordinating session
(id `session_01LwJUCTKW7YUV9kjpjWNenA`) merges. You do NOT merge, and you do not post comments or reviews on GitHub.

Read CLAUDE.md of the repository first. Its rules are the review standard.

## Setup (only if you must fix something)
```
uv sync
uv run maturin develop
```
There is no `gh` and no `just`. Use the GitHub MCP tools (`mcp__github__*`, load with ToolSearch). To read diffs, git is
faster than the API: `git fetch origin 'refs/pull/*/head:refs/remotes/pr/*'`, then
`git diff origin/master...pr/<N>` (three dots: the change of the pull request with its stack parents) and, for a pull
request that stacks on others, `git diff pr/<parent>...pr/<N>` to see only its own change.

## The review of one pull request (aim: 3 to 6 minutes; it is a simple review, not an audit)
1. Read the title and the text (`pull_request_read` method `get`), the diff, and the comments (`get_comments`).
   Two bots comment: CodSpeed (performance) and CodeRabbit (often only "review limit reached").
2. Check:
   - The diff does what the title says and nothing else. No scratch or probe file (`test_zz_*`, `probe*`, `tmp*`),
     no debug print, no commented-out code, no file from another change.
   - A fix has a test that fails without it and that checks behavior (an HTTP response through `TestClient`, or the
     public API). No new `skip`/`xfail`. No assert removed from an existing test unless the text says why.
   - The changed code is right: read it for an obvious fault (wrong condition, an error that is hidden, a case that
     the text names but the code does not handle, a broad `except`).
   - Hot path (CLAUDE.md "Hot-path rules": `_dispatch`, `_dispatch_sync`, `serialize_response`, `_kwargs/` injectors,
     `dependencies.py`, the Rust request path): no new work for each request unless the text gives numbers.
     If the CodSpeed comment reports a regression (not "will not alter performance"), write the numbers in your note.
   - A fix or a new option has a CHANGELOG entry under `## [Unreleased]`. Docs agree with the new behavior.
   - No secret, no weaker security check, no `Co-Authored-By` line.
3. Verdict:
   - `OK`: nothing found.
   - `FIXED`: you found a small defect and corrected it. Rules: check out the head branch of the pull request
     (`pull_request_read` gives `head.ref`), ONE follow-up commit, never force-push, never rebase, run the changed test
     files and `uv run ruff check` + `uv run ruff format --check` on the changed files, then push. A push starts CI again,
     and the CI queue is many hours long: push only for a real defect, not for style or wording.
     Commit message: conventional subject, a short body, and the attribution line that your own session instructions give.
   - `HOLD`: a real problem that is not a small fix (the change is wrong, breaks a documented rule, or needs a design
     decision that its text does not state). Say what in one or two sentences.
   A behavior change that the pull request states under "For the merge decision" is accepted by the owner: it is not a
   reason for `HOLD`.
4. Do not check CI and do not check merge conflicts: the coordinating session does both.

## Report
When you have reviewed each pull request of your group, send ONE message to the coordinating session with the
`send_message` tool of the claude-code-remote MCP server (session_id `session_01LwJUCTKW7YUV9kjpjWNenA`). One line for
each pull request, in number order, in this exact form:

`<number> | <first 7 characters of the head sha that you reviewed, or of your new commit for FIXED> | OK or FIXED or HOLD | <note, at most 25 words; "-" if none>`

After the table: at most 10 lines on things that the owner must know (a pair of pull requests in your group that
cannot both merge as they are, a CodSpeed regression, a test that looks flaky). Then stop.
If the message tool is not available, put the same text in your final message.
