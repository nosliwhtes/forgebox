# Forgebox desktop pane

The pane shows snapshots for the desktop's current repository, a journal summary,
changed-file cards, and a unified patch. Preflight checks base-repository conflicts;
Apply and Discard require a second confirmation click. The pane never force-applies
or creates a Git commit. Apply leaves the session available for review; Discard
removes it and clears matching CLI active-session metadata.

## 1. Install the unified plugin

On the machine running the Hermes gateway, install into the selected profile
(set `HERMES_HOME` first for a named profile):

```sh
hermes plugins install forgebox
hermes plugins enable forgebox
```

If Forgebox is not yet available in your catalog, use
`hermes plugins install https://github.com/nosliwhtes/forgebox` instead.
The package includes `plugin.yaml`, agent hooks, `src/forgebox/`,
`dashboard/manifest.json`, `dashboard/plugin_api.py`, and `desktop/plugin.js`.
Keep the whole package together. The dashboard shim imports the bundled core
without a separate pip install in the gateway environment. Hermes supplies FastAPI;
Python 3.10+ and Git are required. No separate HTTP server is needed.

The bundled dashboard manifest declares the backend and hides the unused web tab:

```json
{"name":"forgebox","api":"plugin_api.py","tab":{"hidden":true}}
```

## 2. Make the desktop half available locally

For a local unified install, the desktop app syncs the package's
`desktop/plugin.js` into its app-level plugin directory. Use **Rescan** after
installation. Do not maintain conflicting copies.

An agent package installed only on a remote gateway cannot supply a local desktop
file automatically. Install the unified package locally too, or copy just the
pane from a matching Forgebox checkout on the desktop machine:

```sh
mkdir -p "$HOME/.hermes/desktop-plugins/forgebox"
cp desktop/plugin.js "$HOME/.hermes/desktop-plugins/forgebox/plugin.js"
```

Copying the pane alone does not install the remote backend. Do not copy only
`src/forgebox/plugin_api.py` to the gateway; its deferred imports need the core.
Installing the standalone CLI with `python -m pip install -e .` is optional and
separate from catalog installation, but is required for the `forgebox` shell
commands shown below unless you already have that command installed.

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
- **Import error:** reinstall the complete unified package in the gateway profile.
  Check that `src/forgebox/` is beside `dashboard/`, and that the dashboard contains
  the shim from this package, not a standalone copy of the canonical API file.
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

