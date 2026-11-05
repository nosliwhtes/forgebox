# Forgebox demo: try it without untangling it

**Runtime: 87 seconds.** Terminal screencast with Hermes chat on the left, worktree commands and diff on the right. Keep the real repo path visible. Use a disposable demo repo, not production work.

## Before recording

- Prepare a small Python project with a passing test suite. Leave one staged edit, one unstaged edit, and one Git-visible untracked file in the real repo.
- Rehearse two attempts: a broken refactor and a corrected version. Use the project's actual test results, not prewritten success or failure output.
- Install and enable the Hermes hook integration before claiming automatic redirection. Otherwise demonstrate the CLI and explicitly run commands in the worktree.
- Set `REPO` to the demo repo's absolute path. `forgebox begin --repo "$REPO"` returns the session ID; the worktree is `$REPO/.forgebox/wt/<id>`.
- Capture `git -C "$REPO" status --short` and the contents of the starting files. Exclude `.forgebox/` session metadata from before/after comparisons.
- Pre-record the long-running agent and test steps. Cut waiting time, not the review, discard, or conflict outcome. All terminal output must come from the recording.

## Scene 1: work already in flight | 0:00–0:10

**Show:** Real-repo `git status --short`. Point out the staged edit, unstaged edit, and untracked file. Keep this framing for the later comparison.

**On-screen:** “Dirty repo. Unfinished work. Yours.”

**Voice:** “Staged changes, unfinished edits, an untracked file. This is the repo you're about to give an agent. You shouldn't have to clean it up first.”

## Scene 2: begin from the mess | 0:10–0:22

**Show:** Run `forgebox begin --repo "$REPO"`, then `forgebox status --repo "$REPO"`. Highlight the actual worktree path under `.forgebox/wt/`. Show that the starting files are present there.

**On-screen:** “Snapshot the starting point. Experiment in a worktree.”

**Voice:** “Forgebox snapshots that dirty state and opens a disposable worktree. The agent starts with your work included, without staging it in your real index.”

## Scene 3: let the attempt fail | 0:22–0:35

**Show:** Agent edits `auth.py` and adds or changes its tests inside the worktree. Run the real test command there. Hold on the actual failure, then show `forgebox diff --repo "$REPO"` and `forgebox log --repo "$REPO"`.

**On-screen:** “The experiment broke. Your source files didn't change.”

**Voice:** “The agent changes authentication and runs the tests. This attempt fails. Its diff and tool journal are available for review, while your original source files stay put.”

## Scene 4: discard, then prove it | 0:35–0:49

**Show:** Run `forgebox discard --repo "$REPO"`. Return to the original `git status --short` framing. Compare the starting files and index, not just the list of changed paths. Hold the unchanged source view for two full seconds.

**On-screen:** “Discard the attempt. Keep your unfinished work.”

**Voice:** “Discard removes the worktree and snapshot reference. No experimental edits come back. Those same staged changes, unfinished edits, and untracked files are still yours.”

## Scene 5: a passing attempt lands | 0:49–1:05

**Show:** Begin again. Jump-cut to the corrected agent attempt and actual passing tests. Show `forgebox diff --repo "$REPO"`, `forgebox preflight --repo "$REPO"`, then `forgebox apply --repo "$REPO"`. Show the resulting real-repo diff.

**On-screen:** “Review. Preflight. Apply the whole patch.”

**Voice:** “Try again. Tests pass this time. Review the diff, check preflight, then apply. The patch lands as a whole. A Git commit is optional, not automatic.”

## Scene 6: someone edits the real file | 1:05–1:21

**Show:** Begin a third session and change `auth.py` in its worktree. In a second terminal, make a different edit to the real repo's `auth.py`. Run `forgebox apply --repo "$REPO"`. Highlight the reported conflict and nonzero exit status. Show that the real edit remains and no experiment files were applied.

**On-screen:** “Changed since snapshot? Apply stops.”

**Voice:** “Now someone edits the real authentication file during the experiment. Apply catches the conflict and refuses the patch. It doesn't merge blindly or overwrite their work.”

## End card | 1:21–1:27

**Show:** Forgebox name, repository URL, and the workflow below. No new feature claims.

**On-screen:**

```text
Forgebox: try, then commit.
begin → review → apply | discard
github.com/nosliwhtes/forgebox
```

**Voice:** “Forgebox. Let the agent try. You decide what lands. Code and setup are on GitHub.”

## Recording guardrails

- Show actual CLI output; scene descriptions above are shot directions, not output fixtures.
- Do not use `/forgebox start`: the contracted CLI command is `forgebox begin`.
- Do not show automatic rebase, reconciliation, or a conflict-resolution menu. Conflict detection stops default apply; the user decides what to do next.
- Do not use `--force` in the conflict shot. It explicitly bypasses that protection.
- Keep the security claim narrow: hook-mediated path checks are not an OS sandbox, and discard does not roll back external effects.

[Setup and safety limits](../README.md) · [Architecture contract](../ARCH.md)
