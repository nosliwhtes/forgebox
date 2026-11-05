# Forgebox launch thread

## 1/7

Your coding agent broke the refactor. Now you have to separate its edits from your unfinished work.

Forgebox gives Hermes Agent a disposable Git worktree: try the change, inspect the diff, then apply or discard. Your dirty repo is the starting point.

## 2/7

No cleanup commit first.

Forgebox snapshots staged edits, unstaged edits, and untracked files. The experiment starts from that state in a detached worktree, not a stale copy of HEAD.

Your half-finished work comes with it.

## 3/7

The demo starts with a broken attempt: edit auth code, run tests, watch them fail.

Discard the worktree. Check the real repo: the original edits are still there.

No hunting through the agent's patch to recover your work.

## 4/7

Second attempt: tests pass.

Review the diff and tool journal, then apply. Forgebox checks the patch before landing it, all or nothing.

Want a Git commit too? That's optional. Passing tests don't decide what lands. You do.

## 5/7

The important part: someone edits auth.py in the real repo mid-experiment.

Default apply detects that the affected file changed since the snapshot and stops. Nothing from the patch lands.

Conflict-safe means refusing an overwrite, not promising a magic merge.

## 6/7

The boundary matters: Forgebox uses Git worktrees and Hermes path-checking hooks, not an OS sandbox.

Discard drops the experiment. It cannot unsend a network request or undo an external service call.

Use it for reviewable repo changes, not hostile-code containment.

## 7/7

Follow the 87-second demo storyboard, try a disposable repo, and tell us which agent task you'd put in a worktree first.

Code, CLI setup, and Hermes integration notes:
https://github.com/nosliwhtes/forgebox

#Forgebox #HermesAgent #AIAgents
