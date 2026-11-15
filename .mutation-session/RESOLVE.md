# Conflict resolver session (django-bolt)

About 275 pull requests of a mutation-testing effort are being merged into `master` of dj-bolt/django-bolt, one squash
commit for each. The repository owner said: merge the reviewed pull requests; if something breaks on `master`, fix it
after the merge. Each pull request in "YOUR LIST" (at the end of your task text) was reviewed and is right alone, but
GitHub cannot merge it now: it conflicts with a pull request that merged before it.

Your task: bring each branch of your list up to date with `master`, resolve the conflicts, and MERGE the pull request.
You do not post comments or reviews on GitHub.

Read CLAUDE.md of the repository first; its rules apply. There is no `gh` and no `just`: use git, and the GitHub MCP
tools (`mcp__github__*`, load with ToolSearch). Setup: `uv sync`, `uv run maturin develop` (again after each Rust
change, and after each merge of `master` that changes Rust files: `git diff --name-only HEAD@{1} HEAD | grep -E '\.rs$|Cargo'`),
full suite `uv run pytest python/tests -n 4 -q -p no:cacheprovider`. This container has no IPv6, no PostgreSQL, no Redis;
skip the Rust test `bind_listener_binds_an_ipv6_address_in_brackets`.

## Rules
- Work on the pull requests in the ORDER of your list. A pull request that names parents (`child <- parents`) holds
  the commits of its parents: merge the parents first. A parent outside your list that is still open is the work of
  another session: do the other pull requests first and come back; if it is still open at the end, leave the child and
  report it.
- CHANGELOG.md: the owner decided that the pull requests carry NO CHANGELOG entry (one later pull request adds all
  entries). After each merge of `master` into a branch, CHANGELOG.md of the branch must be exactly the file of
  `origin/master`: `git checkout origin/master -- CHANGELOG.md`.
- Never rebase, never force-push. One merge commit and, if needed, one fix commit for each pull request.
- Other sessions merge into `master` at the same time. `git fetch origin master` immediately before each merge of
  `master` into a branch. If the GitHub merge is refused again because `master` moved, merge `master` again and repeat.

## For each pull request N of your list
1. `mcp__github__pull_request_read` method `get`: `head.ref` (branch), `base.ref`, `state`. If it is already merged or
   closed, go on. If `base.ref` is not `master`: `mcp__github__update_pull_request` with base `master`.
2. `git fetch origin master <head.ref>`, `git switch <head.ref>` (track `origin/<head.ref>`), `git merge origin/master`.
3. Resolve each conflict by reading both sides. Find the merged pull request that made the other side:
   `git log origin/master --oneline -8 -- <file>`, `git show <commit>`. The usual case: two pull requests added tests,
   docs paragraphs, imports or code at one place: keep BOTH. When two pull requests change the same function, read
   what each does and make one function that has both behaviors; your task text may give the right resolution for a
   known pair. For a stacked pull request whose parent merged as one squash commit: the parent's lines are on
   `master` already; keep the version of `master` for them and the child's own change on top.
   Then `git checkout origin/master -- CHANGELOG.md`.
4. Check before you commit: `git diff origin/master --stat` shows only the own change of the pull request. Run the
   test files that the pull request changes and the tests of each file where you resolved a conflict, then
   `uv run ruff check` and `uv run ruff format --check` on the changed Python files (and `cargo fmt --all --check`,
   `cargo clippy --workspace --all-targets -- -D warnings`, `cargo test -p <changed crate>` when Rust files changed).
   Run the full suite when you resolved a conflict in library code (not only tests or docs), or after each third pull request.
   If a test fails because two pull requests need each other (a test of one pins what the other changes on purpose),
   correct the test to the new behavior and say so in the commit message. Never skip or delete a test to get green.
5. Commit the merge: `Merge branch 'master' into <head.ref>` plus one or two sentences on how you resolved each
   conflict, and the attribution line that your own session instructions give. No Co-Authored-By line. `git push origin <head.ref>`.
6. Merge it on GitHub: `mcp__github__merge_pull_request` with owner `dj-bolt`, repo `django-bolt`, pullNumber N,
   merge_method `squash`, expectedHeadSha = the sha that you pushed, commit_title = `<the title of the pull request> (#N)`,
   commit_message = the first paragraph of the pull request text (what was wrong and what is true now, at most 6
   lines, no checklist) followed by an empty line and the attribution line. Do not wait for CI: the owner said so
   (the CI queue is many hours long; your local run is the check). If GitHub answers that the branch is not
   mergeable yet, wait 20 seconds and try again, up to 3 times; if it answers "merge conflicts", go back to step 2.

## Report
When your list is done, send ONE message to the coordinating session with the `send_message` tool of the
claude-code-remote MCP server (session_id `session_01LwJUCTKW7YUV9kjpjWNenA`). One line for each pull request:
`<number> | merged <sha7 of the squash commit on master> or NOT MERGED | <files where you resolved conflicts> | <how, at most 20 words>`
After the lines: tests that you changed and why; anything that is now wrong on `master` and that you did not fix.
Then stop. If the message tool is not available, put the same text in your final message.
