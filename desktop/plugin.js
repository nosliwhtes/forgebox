import { atom, Badge, Button, host, ScrollArea, useQuery, useQueryClient, useValue } from '@hermes/plugin-sdk';
import { jsx } from 'react/jsx-runtime';

const muted = { color: 'var(--ui-text-secondary)' };
const border = '1px solid var(--ui-stroke-secondary)';
const queryPrefix = ['forgebox'];

function queryKey(repo, selection) {
  return [...queryPrefix, repo, selection?.repo === repo ? selection.id : null];
}

async function request(ctx, action, body) {
  const result = await ctx.rest(`/${action}`, { method: 'POST', body });
  if (!result || typeof result !== 'object') throw new Error(`Invalid ${action} response`);
  return result;
}

async function readPane(ctx, repo, wantedId) {
  const listing = await request(ctx, 'list_sessions', { repo });
  if (listing.ok === false) throw new Error(listing.error || 'Cannot list Forgebox sessions');
  const sessions = listing.sessions || [];
  const session = wantedId ? sessions.find((item) => item.id === wantedId) : sessions[0];
  if (!session) return { sessions, session: null, status: null, diff: null };
  const body = { repo, session_id: session.id };
  const [status, diff] = await Promise.all([
    request(ctx, 'status', body), request(ctx, 'diff', body),
  ]);
  return { sessions, session, status, diff };
}

function ForgeboxPane({ ctx, state }) {
  const repo = useValue(host.state.cwd) || '';
  const selection = useValue(state.selection);
  const pending = useValue(state.pending);
  const confirmation = useValue(state.confirmation);
  const notice = useValue(state.notice);
  const client = useQueryClient();
  const key = queryKey(repo, selection);
  const query = useQuery({
    queryKey: key,
    queryFn: () => readPane(ctx, repo, key[2]),
    // OAuth remotes do not deliver ctx.socket events. Always refresh over REST.
    refetchInterval: 5000,
    refetchIntervalInBackground: true,
    retry: false,
  });
  const data = query.data;
  const session = data?.session;
  const summary = data?.status?.summary;
  const files = data?.diff?.files || [];
  const issue = query.error?.message || data?.status?.error || data?.diff?.error;
  const armed = confirmation && confirmation.repo === repo && confirmation.id === session?.id;

  function choose(id) {
    if (state.pending.get()) return;
    // Read the host at the moment of interaction, never a captured cwd.
    state.selection.set({ repo: host.state.cwd.get() || '', id });
    state.confirmation.set(null);
    state.notice.set('');
  }

  async function act(action) {
    if (state.pending.get()) return;
    const currentRepo = host.state.cwd.get() || '';
    const current = client.getQueryData(queryKey(currentRepo, state.selection.get()));
    const target = current?.session;
    if (!target) {
      state.notice.set('Select an existing Forgebox session first.');
      return;
    }
    if (action !== 'preflight') {
      const approved = state.confirmation.get();
      if (!approved || approved.action !== action || approved.id !== target.id || approved.repo !== currentRepo) {
        state.confirmation.set({ action, id: target.id, repo: currentRepo });
        return;
      }
    }
    state.confirmation.set(null);
    state.pending.set(action);
    state.notice.set('');
    try {
      const body = { repo: currentRepo, session_id: target.id };
      if (action === 'apply') {
        const check = await request(ctx, 'preflight', body);
        if (check.ok === false) throw new Error(check.error || 'Preflight failed');
        if (check.status === 'conflict' || check.conflicts?.length) {
          throw new Error(`Conflicts: ${(check.conflicts || []).join(', ')}. Base repository was not changed.`);
        }
      }
      const result = await request(ctx, action, body);
      if (result.conflicts?.length) throw new Error(`Conflicts: ${result.conflicts.join(', ')}. Nothing applied.`);
      if (result.ok === false) throw new Error(result.error || `${action} failed`);
      let message;
      if (action === 'preflight') {
        message = `Preflight: ${result.status || 'complete'}${result.conflicts?.length ? ` — ${result.conflicts.join(', ')}` : ''}`;
      } else if (action === 'apply') {
        // Read persisted state back; do not treat a successful mutation response as proof.
        const remaining = await request(ctx, 'diff', body);
        if (remaining.ok === false) throw new Error(`Apply returned; verification failed: ${remaining.error}`);
        message = `Applied ${(result.applied || []).length} file(s). Snapshot diff: ${(remaining.files || []).length} file(s).`;
      } else {
        const listing = await request(ctx, 'list_sessions', { repo: currentRepo });
        if (listing.ok === false || listing.sessions?.some((item) => item.id === target.id)) {
          throw new Error('Discard returned; could not verify session removal. Refresh before retrying.');
        }
        state.selection.set(null);
        message = `Discarded session ${target.id}.`;
      }
      state.notice.set(message);
    } catch (error) {
      state.notice.set(error instanceof Error ? error.message : String(error));
    } finally {
      state.pending.set('');
      await client.invalidateQueries({ queryKey: queryPrefix });
    }
  }

  const actions = ['preflight', 'apply', 'discard'].map((action) => jsx(Button, {
    type: 'button',
    disabled: Boolean(pending) || !session || (action !== 'discard' && (!session.worktree_path || Boolean(issue))),
    onClick: () => act(action),
    children: pending === action ? `${action[0].toUpperCase() + action.slice(1)}…` :
      `${armed && confirmation.action === action ? 'Confirm ' : ''}${action[0].toUpperCase() + action.slice(1)}`,
  }, action));

  return jsx('section', {
    'aria-label': 'Forgebox snapshot review',
    'aria-busy': Boolean(pending) || query.isFetching,
    style: { height: '100%', display: 'flex', flexDirection: 'column', gap: 8, padding: 10, fontSize: 12, minHeight: 0 },
    children: [
      jsx('header', {
        style: { display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' },
        children: [
          jsx('strong', { children: 'Forgebox' }),
          jsx('span', { style: { ...muted, overflowWrap: 'anywhere', flex: 1 }, children: repo || 'Gateway working directory' }),
          ...actions,
          jsx(Button, {
            type: 'button', disabled: Boolean(pending) || query.isFetching,
            onClick: () => client.invalidateQueries({ queryKey: queryPrefix }), children: 'Refresh',
          }),
        ],
      }),
      armed ? jsx('div', {
        style: { border, padding: 8 },
        children: [
          jsx('span', { children: confirmation.action === 'discard' ?
            `Permanently remove worktree changes in ${session.id}? This cannot be undone. ` :
            `Apply changes from ${session.id} to ${session.repo}? Conflicts will block apply. ` }),
          jsx(Button, { type: 'button', onClick: () => state.confirmation.set(null), children: 'Cancel' }),
        ],
      }) : null,
      jsx('div', {
        role: 'status', 'aria-live': 'polite', style: muted,
        children: notice || (issue ? `Refresh failed; displayed data may be stale. ${issue}` :
          query.isPending ? 'Loading snapshots…' : 'REST refresh every 5 seconds. Apply always checks conflicts.'),
      }),
      jsx(ScrollArea, {
        style: { flex: 1, minHeight: 0 },
        children: jsx('div', {
          style: { display: 'flex', flexWrap: 'wrap', gap: 12 },
          children: [
            jsx('nav', {
              'aria-label': 'Forgebox sessions', style: { flex: '0 1 190px', display: 'flex', flexDirection: 'column', gap: 4 },
              children: [
                jsx('strong', { children: 'Sessions' }),
                ...(data?.sessions || []).map((item) => jsx(Button, {
                  type: 'button', 'aria-pressed': item.id === session?.id, disabled: Boolean(pending),
                  onClick: () => choose(item.id),
                  title: `${item.repo}\n${item.created_at}`,
                  children: `${item.id} · ${item.status}${item.id === session?.id ? ' · selected' : ''}`,
                }, item.id)),
                !data?.sessions?.length && !query.isPending ? jsx('p', {
                  style: muted, children: 'No snapshots here. Run forgebox begin --repo PATH in the repository to start.',
                }) : null,
              ],
            }),
            jsx('div', {
              style: { flex: '1 1 280px', minWidth: 0 },
              children: session ? [
                jsx('p', {
                  style: { ...muted, margin: '0 0 8px' },
                  children: `${session.id} · ${summary?.entries ?? '—'} journal entries · ${summary?.commands ?? '—'} commands · last exit ${summary?.last_exit ?? '—'}`,
                }),
                jsx('strong', { children: data?.diff?.summary || 'Diff unavailable' }),
                ...files.map((file) => jsx('article', {
                  style: { border, padding: 8, marginTop: 6, overflowWrap: 'anywhere' },
                  children: [
                    jsx(Badge, { children: file.status }),
                    jsx('span', { style: { marginLeft: 8 }, children: file.path }),
                    jsx('span', { style: { ...muted, marginLeft: 8 }, children: `+${file.lines_added ?? 0} / −${file.lines_deleted ?? 0}` }),
                  ],
                }, file.path)),
                !files.length && data?.diff?.ok ? jsx('p', { style: muted, children: 'No changes in this experiment.' }) : null,
                data?.diff?.patch ? jsx('details', {
                  style: { marginTop: 8 }, children: [
                    jsx('summary', { children: 'Unified patch' }),
                    jsx('pre', { style: { overflow: 'auto', maxHeight: 300, whiteSpace: 'pre', fontSize: 11 }, children: data.diff.patch }),
                  ],
                }) : null,
              ] : jsx('p', { style: muted, children: 'Select a session to review its journal and file changes.' }),
            }),
          ],
        }),
      }),
    ],
  });
}

export default {
  id: 'forgebox',
  name: 'Forgebox',
  register(ctx) {
    const state = { selection: atom(null), pending: atom(''), confirmation: atom(null), notice: atom('') };
    ctx.register({
      id: 'pane', area: 'panes', title: 'Forgebox',
      data: { placement: 'bottom', dock: { pane: 'workspace', pos: 'bottom' }, height: '200px' },
      render: () => jsx(ForgeboxPane, { ctx, state }),
    });
  },
};
