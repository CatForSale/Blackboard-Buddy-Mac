// A stand-in for supabase-js good enough to test the Online board: tables live in each page and every write is
// copied to the other pages over a BroadcastChannel, which also carries live messages and presence.
(() => {
  const bc = new BroadcastChannel('fakesb');
  const DB = {groups: [], members: [], blocks: [], messages: []};
  const hex = () => [...crypto.getRandomValues(new Uint8Array(16))].map(b => b.toString(16).padStart(2, '0')).join('');
  const me = sessionStorage.fakeUser || (sessionStorage.fakeUser = hex().replace(/^(.{8})(.{4})(.{4})(.{4})/, '$1-$2-$3-$4-'));
  const keyOf = (t, r) => t === 'members' ? r.group_id + '/' + r.user_id : r.id;
  const pg = [], chans = [], pres = {};
  window.__fake = {DB, me, sent: [], fail: false};
  function apply(table, row, del) {
    const t = DB[table], i = t.findIndex(r => keyOf(table, r) === keyOf(table, row));
    if (del) { if (i >= 0) t.splice(i, 1); return; }
    const out = i >= 0 ? (t[i] = {...t[i], ...row}) : (t.push(row), row);
    pg.filter(h => h.table === table && (!h.f || out[h.f[0]] === h.f[1])).forEach(h => h.cb({new: out}));
  }
  function write(table, row, del) { row = {...row}; if (table === 'blocks') row.updated_at = new Date().toISOString();
    apply(table, row, del); bc.postMessage({t: 'row', table, row, del}); }
  bc.onmessage = ({data: m}) => {
    if (m.t === 'row') apply(m.table, m.row, m.del);
    if (m.t === 'hello') bc.postMessage({t: 'state', DB});
    if (m.t === 'state') for (const k in m.DB) m.DB[k].forEach(r => DB[k].some(x => keyOf(k, x) === keyOf(k, r)) || DB[k].push(r));
    if (m.t === 'bc') chans.forEach(c => c.h.filter(h => h.type === 'broadcast' && h.f.event === m.event).forEach(h => h.cb({payload: m.payload})));
    if (m.t === 'pres') { pres[m.uid] = m.state; chans.forEach(c => c.sync()); }
  };
  bc.postMessage({t: 'hello'});
  function query(table) {
    const f = []; let single = false, op = 'select', val = null, sel = '';
    const q = {
      select(s) { sel = s || ''; return q; }, eq(k, v) { f.push([k, v]); return q; }, single() { single = true; return q; },
      upsert(row) { if (window.__fake.fail) return Promise.resolve({error: {message: 'offline'}}); window.__fake.sent.push(row); write(table, row); return Promise.resolve({error: null}); },
      update(p) { op = 'update'; val = p; return q; }, delete() { op = 'delete'; return q; },
      order() { return q; }, limit() { return q; },
      insert(row) { if (window.__fake.fail) return Promise.resolve({error: {message: 'offline'}}); if (window.__fake.noMessages && table === 'messages') return Promise.resolve({error: {message: 'relation does not exist'}}); write(table, row); return Promise.resolve({error: null}); },
      then(ok) {
        if (window.__fake.noMessages && table === 'messages') return ok({data: null, error: {code: 'PGRST205', message: 'Could not find the table public.messages in the schema cache'}});
        const rows = DB[table].filter(r => f.every(([k, v]) => r[k] === v));
        if (op === 'update') { rows.forEach(r => write(table, {...r, ...val})); return ok({error: null}); }
        if (op === 'delete') { rows.forEach(r => write(table, r, true)); return ok({error: null}); }
        let out = rows.map(r => ({...r}));
        if (/groups\(/.test(sel)) out = out.map(r => ({...r, groups: DB.groups.find(g => g.id === r.group_id) || null}));
        return ok({data: single ? out[0] : out, error: null});
      }};
    return q;
  }
  const client = {
    auth: {getSession: async () => ({data: {session: sessionStorage.fakeSigned ? {user: {id: me}} : null}}),
      signInAnonymously: async () => { sessionStorage.fakeSigned = '1'; return {data: {session: {user: {id: me}}}, error: null}; }},
    from: query,
    async rpc(name, a) {
      if (name === 'create_group') { const g = {id: hex(), name: a.p_name, code: 'K7QM-2XPA', created_by: me}; write('groups', g);
        write('members', {group_id: g.id, user_id: me, name: a.p_member, color: a.p_color}); return {data: g, error: null}; }
      if (name === 'join_group') { const g = DB.groups.find(g => g.code.replace('-', '') === a.p_code.toUpperCase().replace(/[^A-Z0-9]/g, ''));
        if (!g) return {data: null, error: {message: 'No group has that code'}};
        write('members', {group_id: g.id, user_id: me, name: a.p_member, color: a.p_color}); return {data: g, error: null}; }
      if (window.__fake.old) return {data: null, error: {code: 'PGRST202', message: 'Could not find the function public.' + name}};
      if (name === 'rename_group') { const g = DB.groups.find(g => g.id === a.p_group); if (!g) return {data: null, error: {message: 'You are not in that group'}};
        write('groups', {...g, name: a.p_name}); return {data: {...g, name: a.p_name}, error: null}; }
      if (name === 'delete_group') { const g = DB.groups.find(g => g.id === a.p_group);
        if (!g || g.created_by !== me) return {data: null, error: {message: 'Only whoever made the group can delete it'}};
        DB.members.filter(m => m.group_id === g.id).forEach(m => write('members', m, true));
        DB.blocks.filter(b => b.group_id === g.id).forEach(b => write('blocks', b, true));
        write('groups', g, true); return {data: null, error: null}; }
    },
    channel(name) {
      const c = {h: [],
        on(type, f, cb) { if (type === 'postgres_changes') { const m = /^(\w+)=eq\.(.+)$/.exec(f.filter || ''); pg.push({table: f.table, f: m && [m[1], m[2]], cb}); }
          else c.h.push({type, f, cb}); return c; },
        subscribe(cb) { chans.push(c); setTimeout(() => cb && cb('SUBSCRIBED'), 20); return c; },
        send({event, payload}) { bc.postMessage({t: 'bc', event, payload}); },
        track(state) { pres[me] = state; bc.postMessage({t: 'pres', uid: me, state}); c.sync(); },
        presenceState() { const o = {}; for (const k in pres) o[k] = [pres[k]]; return o; },
        sync() { c.h.filter(h => h.type === 'presence').forEach(h => h.cb()); }};
      return c;
    },
    async removeChannel() {},
  };
  window.supabase = {createClient: () => client};
})();
