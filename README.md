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
- Best-effort redirect known Hermes file-path arguments and command working directories into that worktree; reject recognized path arguments outside it.
- Inspect a unified diff, including newly created files.
- Review a JSONL journal of tool calls and reported execution results.
- Detect changes to affected real-repo files before applying anything.
- Stage the patch before applying, with rollback protection for ordinary write failures; discard experiments you do not want.
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

With registered Hermes hooks active (Hermes 0.21.6+), supported file-path arguments and command working directories are redirected and tool calls are journaled. Shell source is preserved verbatim to avoid corrupting scripts, literals, and here-documents. Absolute paths in shell commands, `cd`, dynamically generated paths, and subprocess writes are not rewritten or contained. Commands you run directly in another shell are not intercepted or journaled. Supported file tools are `read_file`, `write_file`, `search_files`, and `patch` replace mode. Multi-file `patch` mode is blocked during an active session because its embedded headers are not redirected. Unknown plugin-tool fields are left untouched. Approval hooks only record decisions; apply explicitly with `forgebox apply` or the dashboard/Desktop panel.

Hooks discover the active session from Hermes's task working directory or a supported file tool's target. Relative paths keep the task's current subdirectory, including after a terminal `cd`; the gateway or Desktop process can run elsewhere. A task workspace without a session does not inherit one from the process's launch directory.

## Architecture

```text
Dirty repo → temporary-index snapshot commit → detached worktree
Hermes hooks → path checks and rewrites → JSONL journal
Worktree diff + current repo → preflight → staged apply + rollback | discard
Desktop pane → JSON handlers → session, diff, and action controls
```

See [ARCH.md](ARCH.md) for the module boundaries and public interfaces.

## Safety guarantees and limits

- **Advisory redirection:** known hook-mediated file-path arguments and command working directories are checked and redirected on a best-effort basis. Shell command text is not rewritten. This is a worktree workflow, not an OS security sandbox. Do not rely on it to contain hostile shell commands, subprocesses, network calls, or external services.
- **Preserve the starting point:** snapshots use a temporary Git index. Begin does not stage your work in the real index or require a normal commit. Session files and worktrees live under `.forgebox/`.
- **Preflight conflicts:** every path the experiment changes is compared with its snapshot state in the real repo. If an affected file changed, default apply refuses the transaction rather than silently overwriting it. `--force` opts out.
- **Rollback-protected apply:** the patch is checked and applied in staging before live files change. Renamed originals are retained until installation succeeds, and ordinary write failures trigger rollback. If rollback also fails, the error names the recovery directory under `.forgebox/apply-*`; preserve it and recover the originals before retrying. This is not crash-atomic or safe against simultaneous external editors, and optional Git commits are separate.
- **Checkout ownership:** sessions belong to the checkout where they began. Main and linked worktrees have separate session lists and apply destinations. Run the CLI or panel from that checkout or its subdirectories.
- **Sparse checkout:** omitted paths retain their actual indexed blobs, including staged changes. Applying an edit may materialize an omitted file without changing the real index. To apply a deletion of an omitted file, first include that path in the sparse checkout; otherwise preflight refuses it rather than silently resurrecting the indexed file later.
- **Discard:** removes the disposable worktree and snapshot reference without copying experimental edits back. It does not undo external side effects, erase the execution journal, or reverse an earlier apply.

## Demo

The [demo storyboard](docs/DEMO.md) starts with a dirty repo, discards a broken attempt, applies a passing one, then catches a concurrent edit. It specifies shots to record, not fabricated terminal output. The [X thread](docs/X-THREAD.md) is ready for launch copy.

## License

[MIT](LICENSE). Source: [nosliwhtes/forgebox](https://github.com/nosliwhtes/forgebox).


## Development checks

```bash
python -m pip install -e . pytest
python -m pytest tests/
```

The Hermes integration job also checks the real PluginManager against Hermes
0.21.6 (pinned in CI). To run it locally, check out that Hermes version, install
the integration dependencies listed in `.github/workflows/ci.yml`, and set
`HERMES_SOURCE=/path/to/hermes-agent` when running pytest.
Without Hermes available, only that integration module is skipped.
