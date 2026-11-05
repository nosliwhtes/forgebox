# Forgebox

**Let the agent try. You decide what lands.**

Forgebox is a transactional execution backend for Hermes Agent. It snapshots your Git repo, including staged edits, unstaged edits, and untracked files, then gives the agent a disposable worktree. Review the diff and execution journal, check for conflicts, and apply the result or discard the experiment. Your unfinished work is the starting point, not collateral damage.

[Repository](https://github.com/nosliwhtes/forgebox) · [87-second demo](docs/DEMO.md) · [Launch thread](docs/X-THREAD.md)

## Why try-then-commit?

An agent can pass a test after making changes you never wanted. It can also break a refactor while you are halfway through something else. Cleaning up afterward should not mean separating its edits from yours.

Forgebox makes the experiment a transaction: **begin → edit and test → review → apply or discard**. A failed attempt stays in the worktree. A successful attempt gets checked against the current repo before it lands. Here, “commit” means accepting the transaction; creating a Git commit is optional.

## Features

- Snapshot dirty work without requiring a stash or a preparatory commit.
- Run experiments in a detached Git worktree.
- Redirect supported Hermes tool-path arguments into that worktree and reject paths outside the repo through hooks.
- Inspect a unified diff, including newly created files.
- Review a JSONL journal of tool calls and reported execution results.
- Detect changes to affected real-repo files before applying anything.
- Apply the whole patch or nothing; discard the worktree when the attempt is not worth keeping.
- Use the CLI or the Hermes desktop pane for review and session controls.

## Install

Requires **Python 3.10+ and Git** on Linux or macOS. Runtime code uses Python's standard library and the Git CLI; pytest is development-only.

```bash
git clone https://github.com/nosliwhtes/forgebox.git
cd forgebox
python3 -m pip install -e .
forgebox --help
```

### Hermes plugin

For a native-plugin-packaged revision, install by repository URL rather than assuming a catalog listing:

```bash
hermes plugins install https://github.com/nosliwhtes/forgebox --no-enable
hermes plugins enable forgebox
hermes plugins list
```

Start a fresh Hermes session after enabling. The native installer requires a `plugin.yaml` manifest and a registered plugin entry point. If your checkout does not include that packaging, use the standalone CLI; installing the Python package or copying hook functions does **not** activate Hermes interception.

### Desktop pane

The desktop UI and Python hooks are separate activation steps. From the cloned Forgebox directory, install the pane into the active Hermes profile:

```bash
HERMES_PROFILE_HOME="${HERMES_HOME:-$HOME/.hermes}"
mkdir -p "$HERMES_PROFILE_HOME/desktop-plugins/forgebox"
cp desktop/plugin.js "$HERMES_PROFILE_HOME/desktop-plugins/forgebox/plugin.js"
hermes desktop
```

Open **Settings → Plugins** and enable Forgebox if it is disabled. If it has not appeared, use **Reload desktop plugins** in the command palette. The pane needs the enabled Forgebox Python backend to load session data and perform actions; copying JavaScript alone does not install that backend. A packaged plugin may instead ship the pane under `plugins/forgebox/desktop/plugin.js`; enable that desktop half in Settings too.

## Usage

Run these commands from the Git repo you want to experiment on, not necessarily the Forgebox source checkout:

```bash
REPO="$(git rev-parse --show-toplevel)"
SESSION_ID="$(forgebox begin --repo "$REPO")"
forgebox status --repo "$REPO"
```

Without activated Hermes hooks, direct commands must explicitly run inside the worktree. For a Python project using pytest:

```bash
(
  cd "$REPO/.forgebox/wt/$SESSION_ID"
  # Make experimental edits here, then use your project's test runner.
  python -m pytest
)
```

Inspect before accepting:

```bash
forgebox log --repo "$REPO" --json
forgebox diff --repo "$REPO"
forgebox preflight --repo "$REPO"
forgebox apply --repo "$REPO"
```

Or reject the attempt instead of applying it:

```bash
forgebox discard --repo "$REPO"
```

`apply` runs preflight itself. A conflict returns a nonzero exit status and leaves the patch unapplied. Inspect the changed paths; discard and begin again from the updated repo when appropriate. There is no automatic conflict reconciliation.

To request a Git commit, use `forgebox apply --repo "$REPO" --commit-message "Accept agent experiment"`. Review your existing staged and dirty work first: optional commit handling stages changes in the real repo. `--force` explicitly bypasses conflict protection; it is not the normal review path.

With registered Hermes hooks active, supported file-path arguments are redirected for the agent and tool calls are journaled. Commands you run directly in another shell are not automatically intercepted or journaled.

## Architecture

```text
Dirty repo → temporary-index snapshot commit → detached worktree
Hermes hooks → path checks and rewrites → JSONL journal
Worktree diff + current repo → preflight → atomic apply | discard
Desktop pane → JSON handlers → session, diff, and action controls
```

See [ARCH.md](ARCH.md) for the module boundaries and public interfaces.

## Safety guarantees and limits

- **Sandbox:** supported hook-mediated path arguments are checked and redirected. This is a worktree and hook boundary, not an OS security sandbox. Do not rely on it to contain hostile shell commands, subprocesses, network calls, or external services.
- **Preserve the starting point:** snapshots use a temporary Git index. Begin does not stage your work in the real index or require a normal commit. Session files and worktrees live under `.forgebox/`.
- **Preflight conflicts:** every path the experiment changes is compared with its snapshot state in the real repo. If an affected file changed, default apply refuses the transaction rather than silently overwriting it. `--force` opts out.
- **Atomic apply:** the patch is checked before application; a rejected patch must not leave a partially applied change set. This does not promise power-loss durability or make an optional Git commit part of the same filesystem transaction.
- **Discard:** removes the disposable worktree and snapshot reference without copying experimental edits back. It does not undo external side effects, erase the execution journal, or reverse an earlier apply.

## Demo

The [demo storyboard](docs/DEMO.md) starts with a dirty repo, discards a broken attempt, applies a passing one, then catches a concurrent edit. It specifies shots to record, not fabricated terminal output. The [X thread](docs/X-THREAD.md) is ready for launch copy.

## License

[MIT](LICENSE). Source: [nosliwhtes/forgebox](https://github.com/nosliwhtes/forgebox).
