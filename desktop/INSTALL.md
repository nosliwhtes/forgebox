# Forgebox desktop pane

The pane shows snapshots for the desktop's current repository, a journal summary,
changed-file cards, and a unified patch. Preflight checks base-repository conflicts;
Apply and Discard require a second confirmation click. The pane never force-applies
or creates a Git commit. Apply leaves the session available for review; Discard
removes it and clears matching CLI active-session metadata.

## 1. Make the core importable on the gateway

Install Forgebox in **the Python environment running the Hermes gateway**, not an
unrelated system Python. From this checkout, using that environment's Python:

```sh
cd /home/nosliwhtes/Projects/forgebox
python -m pip install -e .
python -c 'import forgebox; print(forgebox.__file__)'
```

The core uses Python 3.10+, the standard library, and Git. `HANDLERS` can be imported
without FastAPI or the core modules being present; actual actions need the core.
For REST mounting, this module also exposes a lazy `router` using FastAPI already
provided by Hermes. No separate HTTP service or WebSocket endpoint is required.

## 2. Copy the backend and desktop files

These commands use `~/.hermes` by default. For a named profile, set `HERMES_HOME`
to its directory for the **backend**. On a remote gateway, install the backend and
Python package on that remote machine, and the desktop file locally.

```sh
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
mkdir -p "$HERMES_HOME/plugins/forgebox/dashboard"
cp src/forgebox/plugin_api.py "$HERMES_HOME/plugins/forgebox/dashboard/plugin_api.py"

# Desktop plugins are app-level. Use the local desktop app's plugin root.
mkdir -p "$HOME/.hermes/desktop-plugins/forgebox"
cp desktop/plugin.js "$HOME/.hermes/desktop-plugins/forgebox/plugin.js"
```

The backend directory also needs `dashboard/manifest.json` with:

```json
{"name":"forgebox","api":"plugin_api.py"}
```

Keep the Forgebox agent package's existing `plugin.yaml` if one is installed.
For a backend-only manual installation, provide this minimal package descriptor at
`$HERMES_HOME/plugins/forgebox/plugin.yaml` (do not overwrite existing hooks):

```yaml
name: forgebox
version: 0.1.0
description: Transactional Git worktree review and apply
```

Alternatively, put the desktop file at
`$HERMES_HOME/plugins/forgebox/desktop/plugin.js` in the **local unified package**,
next to its `dashboard/` directory and `plugin.yaml`. The app syncs that desktop
half into its app-level plugin directory. Use **Rescan** after changes to a copied
unified package. Do not maintain two conflicting copies. An agent package installed
only on a remote machine cannot supply a local desktop file automatically.

## 3. Enable both halves

1. Enable the Forgebox **agent/backend** plugin in Hermes' plugin management UI
   or supported plugin CLI. It must be in the selected gateway profile's
   `plugins.enabled` allow-list and not in `plugins.disabled`; merely enabling
   the desktop half does not authorize Python imports. Preserve other enabled
   plugins rather than replacing the list.
2. Restart the gateway if needed to mount the new backend routes.
3. In **Settings → Plugins** (called **Capabilities → Plugins** in newer builds),
   enable **Forgebox**. For a unified package, use **Rescan** if it is not listed.
4. Open **Cmd+K** (or the command-palette shortcut on your platform), then run
   **Reload desktop plugins**.

The pane starts below the workspace at 200 px high and can be resized or dragged.
Select the base repository in Hermes, then run `forgebox begin --repo PATH` to
create a snapshot. Click a session, inspect the patch, run **Preflight**, then
**Apply** / **Confirm Apply** or **Discard** / **Confirm Discard**. Conflicts are
shown as paths, and default Apply never overwrites a conflict.

## API and refresh behavior

All routes accept POST JSON objects under `/api/plugins/forgebox/`:

| Path | Response |
| --- | --- |
| `/list_sessions` | `{ok, repo, sessions: [{id, repo, created_at, status, ...}]}` |
| `/status` | Journal summary fields plus `{ok, session, summary}` |
| `/diff` | `{ok, session_id, patch, files, summary}` |
| `/preflight` | `{ok, session_id, status, conflicts, patch, files}` |
| `/apply` | `{ok, session_id, applied, conflicts, commit_sha}` |
| `/discard` | `{ok, session_id}` |

Send `repo` and `session_id` explicitly. With no repository parameter, handlers
use the gateway process's working directory. Read-only handlers default to the
newest snapshot; Apply/Discard require an explicit session ID. Errors are dicts
with `{ok: false, error, code}`. `list_sessions` wraps its list in an object so
every exported handler returns a dict. Snapshot refs and registered worktrees are
the session inventory; the CLI's `.forgebox/active.json` is not a second registry.

The SDK's shared query client performs POST-based REST refreshes every **5 seconds**,
including a background polling fallback. No WebSocket availability is assumed:
`ctx.socket` is a no-op on OAuth remotes. Refresh errors retain existing content
and identify it as potentially stale. Mutations invalidate queries and read back
the affected state before showing completion.

## Troubleshooting and checks

- **404:** check the manifest, correct gateway profile, backend allow-list and
  gateway restart. The desktop toggle alone cannot mount Python routes.
- **Import error:** ensure `forgebox` is installed in the gateway's own Python
  environment. A copied API file alone is not the core package.
- **No snapshots:** check the desktop cwd; snapshots are repository-scoped, not
  Hermes conversation IDs. Existing dirty files are captured by `forgebox begin`.
- **Session without worktree:** only Discard is available; do not Apply it.
- **After Apply:** the snapshot diff can still show the same changed files because
  it compares the experiment against the original snapshot, not the updated base.
- **Conflicts:** resolve/review the base changes before trying again; the desktop
  pane intentionally offers no force control.

From the repository root:

```sh
node --check desktop/plugin.js
python -m py_compile src/forgebox/plugin_api.py
PYTHONPATH=src python -c 'from forgebox.plugin_api import HANDLERS; results = {name: fn({}) for name, fn in HANDLERS.items()}; assert all(isinstance(value, dict) for value in results.values()); print(results)'
```

The last check is a no-session smoke test, not proof that Apply worked. When no
snapshot exists, error dicts for session-dependent operations are expected. Actual
desktop rendering, theme switching and authenticated remote routing must also be
checked in the Hermes app; syntax checks cannot certify those behaviors.
