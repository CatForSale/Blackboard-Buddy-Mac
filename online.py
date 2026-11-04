"""Online: a shared board for group work. A big open space for notes, code, terminal output, checklists,
pictures, links, drawings and arrows, which everyone in the group sees change live.

It runs on a free Supabase project (an online database with live updates). People sign in anonymously and
join a group with its invite code; the database only lets members of a group see that group's board.
Without a project set up, it works as a board on this computer only.
"""

import json
import re

# The group's Supabase project. Baked into the build that gets shared, so friends don't have to type it.
# The anon key is meant to be public: the rules in SETUP_SQL decide what it can do.
DEFAULT_URL = "https://pbsfruwusfxwaafeypgj.supabase.co"
DEFAULT_KEY = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InBic2ZydXd1c2Z4d2FhZmV5cGdqIiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTM2MDE0OTEsImV4cCI6MjEwOTE3NzQ5MX0"
               ".kTL76AGFsBntFjJD78F02vUb7FI8WLw05s1qQ7dn5p4")

URL_OK = re.compile(r"^https://[a-z0-9-]+\.supabase\.(co|in)/?$")


def config(settings):
    url = (settings.get("online_url") or DEFAULT_URL).strip().rstrip("/")
    key = (settings.get("online_key") or DEFAULT_KEY).strip()
    return {"url": url, "key": key} if url and key else {}


SETUP_SQL = r"""-- Blackboard Buddy: Online board. Paste all of this into Supabase > SQL Editor and press Run.
-- Running it again is safe.

create table if not exists public.groups (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  code text unique not null,
  created_by uuid default auth.uid(),
  created_at timestamptz default now()
);

create table if not exists public.members (
  group_id uuid references public.groups on delete cascade,
  user_id uuid default auth.uid(),
  name text,
  color text,
  joined_at timestamptz default now(),
  primary key (group_id, user_id)
);

create table if not exists public.blocks (
  id uuid primary key,
  group_id uuid not null references public.groups on delete cascade,
  kind text not null,
  x real default 0, y real default 0, w real default 260, h real default 160,
  z bigint default 0,
  data jsonb default '{}'::jsonb check (pg_column_size(data) < 700000),
  deleted boolean default false,
  updated_by uuid default auth.uid(),
  updated_at timestamptz default now(),
  created_at timestamptz default now()
);
create index if not exists blocks_group on public.blocks (group_id);

create or replace function public.touch() returns trigger language plpgsql as $$
begin new.updated_at := now(); new.updated_by := auth.uid(); return new; end $$;
drop trigger if exists blocks_touch on public.blocks;
create trigger blocks_touch before insert or update on public.blocks for each row execute function public.touch();

-- security definer so the check itself isn't blocked by the rules it's used in
create or replace function public.is_member(g uuid) returns boolean
language sql security definer stable set search_path = public as $$
  select exists (select 1 from members where group_id = g and user_id = auth.uid())
$$;

-- what signed-in people may try at all; the policies below then narrow it to their own groups
grant usage on schema public to authenticated;
grant select on public.groups to authenticated;
grant select, update, delete on public.members to authenticated;
grant select, insert, update on public.blocks to authenticated;
revoke all on public.groups, public.members, public.blocks from anon;

alter table public.groups enable row level security;
alter table public.members enable row level security;
alter table public.blocks enable row level security;

drop policy if exists "see own groups" on public.groups;
create policy "see own groups" on public.groups for select to authenticated using (public.is_member(id));
drop policy if exists "see group members" on public.members;
create policy "see group members" on public.members for select to authenticated using (public.is_member(group_id));
drop policy if exists "change own name" on public.members;
create policy "change own name" on public.members for update to authenticated
  using (user_id = auth.uid()) with check (user_id = auth.uid());
drop policy if exists "leave group" on public.members;
create policy "leave group" on public.members for delete to authenticated using (user_id = auth.uid());
drop policy if exists "read board" on public.blocks;
create policy "read board" on public.blocks for select to authenticated using (public.is_member(group_id));
drop policy if exists "add to board" on public.blocks;
create policy "add to board" on public.blocks for insert to authenticated with check (public.is_member(group_id));
drop policy if exists "edit board" on public.blocks;
create policy "edit board" on public.blocks for update to authenticated
  using (public.is_member(group_id)) with check (public.is_member(group_id));

-- make a group with an invite code like K7QM-2XPA
create or replace function public.create_group(p_name text, p_member text, p_color text) returns json
language plpgsql security definer set search_path = public as $$
declare g groups; c text; abc text := 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
begin
  if auth.uid() is null then raise exception 'Not signed in'; end if;
  loop
    c := '';
    for i in 1..8 loop
      c := c || substr(abc, 1 + floor(random() * 32)::int, 1);
      if i = 4 then c := c || '-'; end if;
    end loop;
    exit when not exists (select 1 from groups where code = c);
  end loop;
  insert into groups (name, code, created_by)
    values (left(coalesce(nullif(trim(p_name), ''), 'Group'), 60), c, auth.uid()) returning * into g;
  insert into members (group_id, user_id, name, color) values (g.id, auth.uid(), left(p_member, 40), p_color);
  return json_build_object('id', g.id, 'name', g.name, 'code', g.code);
end $$;

-- join with a code; dashes, spaces and case don't matter
create or replace function public.join_group(p_code text, p_member text, p_color text) returns json
language plpgsql security definer set search_path = public as $$
declare g groups;
begin
  if auth.uid() is null then raise exception 'Not signed in'; end if;
  select * into g from groups
    where replace(code, '-', '') = regexp_replace(upper(coalesce(p_code, '')), '[^A-Z0-9]', '', 'g');
  if not found then raise exception 'No group has that code'; end if;
  insert into members (group_id, user_id, name, color) values (g.id, auth.uid(), left(p_member, 40), p_color)
    on conflict (group_id, user_id) do update set name = excluded.name, color = excluded.color;
  return json_build_object('id', g.id, 'name', g.name, 'code', g.code);
end $$;

grant execute on function public.create_group(text, text, text) to authenticated;
grant execute on function public.join_group(text, text, text) to authenticated;
revoke execute on function public.create_group(text, text, text) from anon;
revoke execute on function public.join_group(text, text, text) from anon;

-- rename a group: anyone in it may
create or replace function public.rename_group(p_group uuid, p_name text) returns json
language plpgsql security definer set search_path = public as $$
declare g groups;
begin
  if not public.is_member(p_group) then raise exception 'You are not in that group'; end if;
  update groups set name = left(coalesce(nullif(trim(p_name), ''), name), 60) where id = p_group returning * into g;
  return json_build_object('id', g.id, 'name', g.name, 'code', g.code);
end $$;

-- delete a group for everyone: only whoever made it may. Its board and member list go with it.
create or replace function public.delete_group(p_group uuid) returns void
language plpgsql security definer set search_path = public as $$
begin
  delete from groups where id = p_group and created_by = auth.uid();
  if not found then raise exception 'Only whoever made the group can delete it'; end if;
end $$;

grant execute on function public.rename_group(uuid, text) to authenticated;
grant execute on function public.delete_group(uuid) to authenticated;
revoke execute on function public.rename_group(uuid, text) from anon;
revoke execute on function public.delete_group(uuid) from anon;

-- messages on a group: to everyone in it, or private to one person
create table if not exists public.messages (
  id uuid primary key default gen_random_uuid(),
  group_id uuid not null references public.groups on delete cascade,
  from_id uuid not null default auth.uid(),
  from_name text,
  to_id uuid,
  to_name text,
  text text not null check (length(text) <= 2000),
  created_at timestamptz default now()
);
grant select, insert on public.messages to authenticated;
revoke all on public.messages from anon;
alter table public.messages enable row level security;
drop policy if exists "read messages" on public.messages;
create policy "read messages" on public.messages for select to authenticated
  using (public.is_member(group_id) and (to_id is null or to_id = auth.uid() or from_id = auth.uid()));
drop policy if exists "send messages" on public.messages;
create policy "send messages" on public.messages for insert to authenticated
  with check (public.is_member(group_id) and from_id = auth.uid());

-- shared notes: one person's This week, deadlines, notes and plans, readable by anyone with the code.
-- Only the person who made the code (who holds its key) can change or remove it. Nobody can list them.
create table if not exists public.shares (
  code text primary key,
  secret text not null,
  name text,
  data text not null default '',
  size int not null default 0,
  updated_at timestamptz default now(),
  created_at timestamptz default now()
);
revoke all on public.shares from anon, authenticated;
alter table public.shares enable row level security;

create or replace function public.share_create(p_secret text, p_name text)
returns text language plpgsql security definer set search_path = public as $$
declare c text;
begin
  if length(coalesce(p_secret, '')) < 24 then raise exception 'bad key'; end if;
  loop
    c := '';
    for i in 1..8 loop c := c || substr('ABCDEFGHJKMNPQRSTUVWXYZ23456789', 1 + floor(random() * 31)::int, 1); end loop;
    exit when not exists (select 1 from public.shares where code = c);
  end loop;
  insert into public.shares (code, secret, name) values (c, encode(sha256(convert_to(p_secret, 'UTF8')), 'hex'), left(p_name, 40));
  return c;
end $$;

create or replace function public.share_put(p_code text, p_secret text, p_name text, p_data text)
returns timestamptz language plpgsql security definer set search_path = public as $$
declare t timestamptz;
begin
  if length(p_data) > 8000000 then raise exception 'too big'; end if;
  update public.shares set data = p_data, name = left(p_name, 40), size = length(p_data), updated_at = now()
    where code = p_code and secret = encode(sha256(convert_to(p_secret, 'UTF8')), 'hex')
    returning updated_at into t;
  if t is null then raise exception 'not yours'; end if;
  return t;
end $$;

create or replace function public.share_stop(p_code text, p_secret text)
returns boolean language plpgsql security definer set search_path = public as $$
begin
  delete from public.shares where code = p_code and secret = encode(sha256(convert_to(p_secret, 'UTF8')), 'hex');
  return found;
end $$;

create or replace function public.share_get(p_code text)
returns table (name text, data text, updated_at timestamptz) language sql stable security definer set search_path = public as $$
  select s.name, s.data, s.updated_at from public.shares s where s.code = upper(regexp_replace(p_code, '[^A-Za-z0-9]', '', 'g'));
$$;

create or replace function public.share_when(p_code text)
returns timestamptz language sql stable security definer set search_path = public as $$
  select s.updated_at from public.shares s where s.code = upper(regexp_replace(p_code, '[^A-Za-z0-9]', '', 'g'));
$$;

grant execute on function public.share_create(text, text), public.share_put(text, text, text, text),
  public.share_stop(text, text), public.share_get(text), public.share_when(text) to anon, authenticated;

-- live updates
do $$ begin
  if not exists (select 1 from pg_publication_tables where pubname = 'supabase_realtime' and tablename = 'blocks') then
    alter publication supabase_realtime add table public.blocks;
  end if;
  if not exists (select 1 from pg_publication_tables where pubname = 'supabase_realtime' and tablename = 'members') then
    alter publication supabase_realtime add table public.members;
  end if;
  if not exists (select 1 from pg_publication_tables where pubname = 'supabase_realtime' and tablename = 'messages') then
    alter publication supabase_realtime add table public.messages;
  end if;
end $$;
"""


def _icon(d):
    return f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">{d}</svg>'


TOOLS = [
    ("select", "Pick things out \u00b7 drag to look around \u00b7 Shift-drag to pick out several (V)", '<path d="M5 3l14 8-6 1.6L10 19z"/>'),
    ("hand", "Move around the board (H, or hold space)", '<path stroke-linecap="square" stroke-linejoin="miter" d="M8 12V6M11 11V3.5M14 11V4.5M17 12V7M8 14 5.5 11.5 4 13l4.5 7h8l1.5-4V12"/>'),
    ("|", "", ""),
    ("note", "Note (N)", '<path d="M5 4h14v16H5z"/><path d="M8.5 9h7M8.5 12.5h7M8.5 16h4"/>'),
    ("text", "Big text (T)", '<path d="M5 6V4.5h14V6M12 4.5V20M9 20h6"/>'),
    ("code", "Code (C)", '<path d="M8.5 7L3.5 12l5 5M15.5 7l5 5-5 5"/>'),
    ("proj", "Code project: files, run, like a mini VS Code (K)", '<path d="M3 6.5A1.5 1.5 0 014.5 5H9l2 2h8.5A1.5 1.5 0 0121 8.5v9a1.5 1.5 0 01-1.5 1.5h-15A1.5 1.5 0 013 17.5z"/><path d="M10 11l-2 2 2 2M14 11l2 2-2 2"/>'),
    ("term", "Terminal output (R)", '<rect x="3" y="4.5" width="18" height="15" rx="2"/><path d="M7 10l3 2.5L7 15M12.5 15.5H17"/>'),
    ("todo", "Checklist (L)", '<path d="M4 6.5l1.8 1.8L9 5M4 13.5l1.8 1.8L9 12M12.5 7h7.5M12.5 14h7.5M4 19.5h16"/>'),
    ("image", "Picture (I)", '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="M20.5 16l-5-5L5 19.5"/>'),
    ("link", "Link (U)", '<path d="M10 14a4.5 4.5 0 006.4 0l3-3a4.5 4.5 0 00-6.4-6.4l-1 1M14 10a4.5 4.5 0 00-6.4 0l-3 3a4.5 4.5 0 006.4 6.4l1-1"/>'),
    ("|", "", ""),
    ("pen", "Pen (P)", '<path d="M15.5 4.5l4 4L8 20H4v-4z"/><path d="M13 7l4 4"/>'),
    ("arrow", "Join two blocks with a line (A, or right-click a block)", '<path d="M4 20L20 4M11 4h9v9"/>'),
    ("shape", "Box or circle to group things (O)", '<rect x="3.5" y="6" width="17" height="12" rx="2"/>'),
]

ON_HTML = f"""
<div class="ol" tabindex="-1">
 <div class="ol-board" aria-label="Group board">
  <div class="ol-world"><svg class="ol-links" width="1" height="1"><defs>
   <marker id="ol-head" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
    <path d="M0 0L10 5L0 10z" fill="context-stroke"/></marker></defs></svg></div>
  <svg class="ol-draft"><path/></svg>
  <div class="ol-marquee"></div>
  <div class="ol-cursors"></div>
  <div class="ol-empty"><b>A blank board</b>Double-click anywhere to write.<br>Paste code, terminal output, a link or a picture,
   or drag files in.<br>Drag to pick out several, right-drag or hold space to move about, scroll to zoom.</div>
 </div>
 <div class="ol-tools glass">{"".join('<i class="ol-sep"></i>' if k == "|" else f'<button data-tool="{k}" title="{t}" aria-label="{t}">{_icon(d)}</button>' for k, t, d in TOOLS)}
  <div class="ol-inks"></div>
 </div>
 <div class="ol-top glass">
  <button class="ol-switch" aria-haspopup="menu" aria-expanded="false"
   title="Switch board or group (Ctrl+K)" aria-label="Switch board or group">
   <span class="ol-state"><i></i><b>Local</b></span><span class="ol-gname"></span><span class="ol-chev">▾</span>
  </button>
  <button class="ol-code" title="Copy the invite code"></button>
  <span class="ol-people"></span>
  <button class="ol-chatb ghost" hidden title="Messages" aria-label="Messages"><b class="ol-here"></b><span>Chat</span><i class="ol-unread"></i></button>
  <button class="ol-more ghost" aria-label="Board menu">•••</button>
  <div class="ol-menu glass"></div>
 </div>
 <div class="ol-chat glass" hidden>
  <div class="ol-chh"><b>Messages</b><button class="ol-chx ghost" aria-label="Close messages">✕</button></div>
  <div class="ol-chn"></div>
  <div class="ol-chlist" aria-live="polite"></div>
  <div class="ol-chtyping" aria-live="polite"></div>
  <div class="ol-chin"><select class="ol-chto" aria-label="Send to"></select>
   <textarea class="ol-cht" rows="1" maxlength="1000" placeholder="Message · Enter sends, Shift+Enter for a new line" aria-label="Message" spellcheck="true"></textarea><button class="ol-chsend">Send</button></div>
 </div>
 <div class="ol-zoom glass">
  <button data-z="-" aria-label="Zoom out">−</button><button data-z="0" class="ol-zv" title="Back to 100%">100%</button>
  <button data-z="+" aria-label="Zoom in">+</button><button data-z="fit" title="Show everything (F)">Fit</button>
 </div>
 <div class="ol-corner">
  <div class="ol-map glass" hidden>
   <canvas width="260" height="170"></canvas><i class="ol-mapv"></i>
  </div>
  <div class="ol-hunt glass">
   <button class="ol-mapb ghost" title="Map of the board (M)" aria-label="Map of the board"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 4 3 6.5v13.5l6-2.5 6 2.5 6-2.5V4l-6 2.5zM9 4v13.5M15 6.5V20"/></svg></button>
   <input class="ol-huntq" placeholder="Find on the board" aria-label="Find on the board">
   <span class="ol-huntn"></span>
   <button class="ol-huntp ghost" title="Previous match (Shift+Enter)" aria-label="Previous"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 15 6-6 6 6"/></svg></button>
   <button class="ol-huntx ghost" title="Next match (Enter)" aria-label="Next"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg></button>
  </div>
 </div>
 <div class="ol-fix glass" hidden>
  <div class="ol-fixh"><b>Writing</b><span class="ol-fixn"></span>
   <button class="ol-fixx ghost" aria-label="Close">✕</button></div>
  <ul class="ol-fixl"></ul>
  <div class="ol-fixf"><label><input type="checkbox" class="ol-fixauto"> Fix typos as I type</label>
   <button class="ol-fixall">Fix everything</button></div>
 </div>
 <div class="ol-ctx glass"></div>
 <div class="ol-gate"><div class="ol-card glass"></div></div>
 <div class="ol-toast"></div>
 <input class="ol-file" type="file" accept="image/*" hidden>
</div>
"""

ON_CSS = r"""
html:has(#online.on) header{display:none}
main:has(#online.on){max-width:none;padding:66px 16px 0}
main:has(#online.on)::after{height:0}
#online.on > *{animation:none}
.ol{position:relative;height:max(480px,calc(100vh - 160px));border-radius:24px;overflow:hidden;outline:none;
 border:1px solid var(--rim);background:var(--bg);--sel:var(--red);--blk:var(--glass-strong)}
.ol-board{position:absolute;inset:0;overflow:hidden;cursor:grab;touch-action:none;
 background-image:radial-gradient(var(--dot) 1.1px,transparent 1.5px);background-size:var(--g,22px) var(--g,22px);background-position:var(--gx,0) var(--gy,0)}
.ol-board.panning{cursor:grabbing} .ol[data-tool=pen] .ol-board,.ol[data-tool=arrow] .ol-board{cursor:crosshair}
.ol[data-tool=note] .ol-board,.ol[data-tool=sticky] .ol-board,.ol[data-tool=text] .ol-board,.ol[data-tool=code] .ol-board,
.ol[data-tool=term] .ol-board,.ol[data-tool=todo] .ol-board,.ol[data-tool=link] .ol-board,.ol[data-tool=image] .ol-board{cursor:copy}
.ol-world{position:absolute;left:0;top:0;transform-origin:0 0}
.ol-world.moving{will-change:transform}
.ol-links{position:absolute;left:0;top:0;overflow:visible;pointer-events:none}
.ol-links path{fill:none;stroke:var(--text);stroke-width:2;pointer-events:stroke;cursor:pointer}
.ol-links path.hit{stroke:transparent;stroke-width:18}
.ol-links marker path{fill:var(--text);fill:context-stroke;stroke:none;pointer-events:none}
.ol-links path.sel{stroke:var(--sel)}
/* a word written on the line, on a patch of background so the line doesn't run through it */
.ol-links text{fill:var(--text);font:11px var(--mono);letter-spacing:.06em;text-transform:uppercase;
 text-anchor:middle;dominant-baseline:middle;pointer-events:none}
.ol-links rect{fill:var(--bg);pointer-events:none}
.ol-links text.sel,.ol-links path.sel+rect+text{fill:var(--sel)}

/* a box or circle drawn behind everything, to group things or point them out. Only its rim and
   its name take clicks, so whatever sits inside it stays usable. */
.ol-b[data-kind=shape]{background:transparent;border-color:transparent;box-shadow:none;overflow:visible;pointer-events:none}
.ol-b[data-kind=shape]::after{content:"";position:absolute;inset:0;border:2px solid var(--sh,var(--text));border-radius:18px}
.ol-b[data-kind=shape][data-form=ellipse]::after{border-radius:50%}
.ol-b[data-kind=shape].fill::after{background:color-mix(in srgb,var(--sh,var(--text)) 12%,transparent)}
.ol-b[data-kind=shape].dash::after{border-style:dashed}
.ol-srim{position:absolute;pointer-events:auto;cursor:move;z-index:2}
.ol-srim.n{left:-8px;right:-8px;top:-8px;height:18px} .ol-srim.s{left:-8px;right:-8px;bottom:-8px;height:18px}
.ol-srim.w{top:-8px;bottom:-8px;left:-8px;width:18px}  .ol-srim.e{top:-8px;bottom:-8px;right:-8px;width:18px}
.ol-b[data-kind=shape] .ol-c{position:absolute;left:16px;top:-12px;flex:none;min-height:0;max-width:calc(100% - 46px);
 padding:2px 9px;background:var(--bg);color:var(--sh,var(--text));border-radius:9px;font:11px/1.5 var(--mono);
 letter-spacing:.08em;text-transform:uppercase;pointer-events:auto;overflow:hidden;white-space:nowrap;z-index:3}
.ol-b[data-kind=shape][data-form=ellipse] .ol-c{left:50%;transform:translateX(-50%)}
.ol-b[data-kind=shape] .ol-rs{pointer-events:auto}
.ol-b.locked .ol-rs,.ol-b.sel.locked .ol-rs{display:none}
.ol-b.locked::before{content:"\1F512";position:absolute;right:6px;top:4px;z-index:4;font-size:11px;opacity:.5}
.ol-draft{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;overflow:visible}
.ol-draft path{fill:none;stroke:var(--text);stroke-width:2.5;stroke-linecap:round;stroke-linejoin:round}
.ol-marquee{position:absolute;display:none;border:1px dashed var(--sel);background:color-mix(in srgb,var(--sel) 8%,transparent);pointer-events:none}
.ol-empty{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);text-align:center;color:var(--muted);
 font:12px/1.9 var(--mono);letter-spacing:.06em;pointer-events:none;text-transform:uppercase}
.ol-empty b{display:block;font:800 34px/1.2 var(--dotf);color:var(--text);letter-spacing:.02em;margin-bottom:10px}
.ol.has-blocks .ol-empty{display:none}

/* blocks */
.ol-b{position:absolute;display:flex;flex-direction:column;border-radius:16px;background:var(--blk);border:1px solid var(--rim);
 box-shadow:0 1px 2px rgba(0,0,0,.05),0 6px 22px rgba(0,0,0,.07);cursor:default;overflow:hidden;color:var(--text);
 transition:box-shadow .25s,outline-color .2s;outline:2px solid transparent;outline-offset:2px}
.ol-b.sel{outline-color:var(--sel)} .ol-b.lift{box-shadow:0 18px 50px rgba(0,0,0,.18)}
.ol-b.peer{outline-color:var(--pc)} .ol-b.peer::before{content:attr(data-who);position:absolute;right:8px;top:-1px;z-index:3;
 background:var(--pc);color:#fff;font:10px/1 var(--mono);letter-spacing:.06em;text-transform:uppercase;padding:4px 7px;border-radius:0 0 8px 8px}
.ol-b.editing{cursor:text;outline-color:var(--text)}
.ol-c{flex:1;min-height:0;overflow:auto;padding:14px 16px;outline:none;font-size:15px;line-height:1.5;word-wrap:break-word;scrollbar-width:thin}
.ol-c:empty::before{content:attr(data-ph);color:var(--muted)}
.ol-c h1,.ol-c h2,.ol-c h3{margin:.2em 0 .4em;line-height:1.2} .ol-c p{margin:0 0 .5em} .ol-c ul,.ol-c ol{margin:.2em 0;padding-left:1.3em}
.ol-c img{max-width:100%} .ol-c a{color:inherit}
.ol-h{display:flex;align-items:center;gap:8px;padding:8px 10px 8px 14px;border-bottom:1px solid var(--line);
 font:10.5px/1 var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--muted);flex:none;min-height:34px}
.ol-h .ol-t{flex:1;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.ol-h button,.ol-h select{border:1px solid var(--line);background:transparent;color:inherit;border-radius:999px;padding:4px 9px;
 font:10px var(--mono);letter-spacing:.08em;text-transform:uppercase;cursor:pointer}
.ol-h select{max-width:110px} .ol-h button:hover{color:var(--text);background:var(--inset)}
.ol-b[data-kind=text]{background:transparent;border-color:transparent;box-shadow:none}
.ol-b[data-kind=text] .ol-c{font:800 var(--fs,40px)/1.05 var(--dotf);letter-spacing:.01em;padding:4px 6px;overflow:hidden;text-transform:uppercase}
.ol-b[data-kind=text].sel,.ol-b[data-kind=text]:hover{border-color:var(--line)}
.ol-b[data-kind=sticky]{background:var(--sc,#fff4b8);color:#111;border-color:transparent}
.ol-b[data-kind=sticky] .ol-c{font-size:16px} .ol-b[data-kind=sticky] .ol-c:empty::before{color:#0006}
/* whatever has been chosen for this one block, laid over the usual look of its kind */
.ol-b .ol-c,.ol-b .ol-todo{font-family:var(--tf,inherit);color:var(--tc,inherit)}
.ol-b:not([data-kind=text]):not([data-kind=code]):not([data-kind=term]) .ol-c,
.ol-b .ol-todo span{font-size:var(--fs2,inherit)}
.ol-b[data-kind=code] .ol-c,.ol-b[data-kind=term] .ol-c{font-size:var(--fs2,12.5px)}
/* a project's text follows its size too: the code, the file list and what it printed */
.ol-b[data-kind=proj] .ol-peek{font-size:var(--fs2,11.5px)}
.ol-b[data-kind=proj] .ol-pfiles{font-size:calc(var(--fs2,11.5px) + 1.5px)}
.ol-b[data-kind=proj] .ol-out,.ol-b[data-kind=code] .ol-out{font-size:var(--fs2,12px)}
.ol-b[data-kind=text] .ol-c{font-family:var(--tf,var(--dotf));text-transform:var(--caps,uppercase)}
.ol-b[data-kind=text].editing .ol-c{text-transform:none}
.ol-b.bold .ol-c,.ol-b.bold .ol-todo{font-weight:700}
.ol-b.italic .ol-c,.ol-b.italic .ol-todo{font-style:italic}
.ol-b[data-align=centre] .ol-c{text-align:center}
.ol-b[data-align=right] .ol-c{text-align:right}
.ol-b[data-kind=note],.ol-b[data-kind=todo],.ol-b[data-kind=link]{background:var(--bgc,var(--blk))}
.ol-b[data-kind=text]{background:var(--bgc,transparent)}
.ol-b[data-kind=code] .ol-c,.ol-b[data-kind=term] .ol-c{font:12.5px/1.6 var(--mono);white-space:pre;padding:12px 14px;tab-size:4}
.ol-code-wrap{position:relative;flex:1;min-height:0;display:flex}
.ol-code-wrap textarea{position:absolute;inset:0;width:100%;height:100%;resize:none;border:0;background:var(--blk);color:var(--text);
 font:12.5px/1.6 var(--mono);padding:12px 14px;outline:none;white-space:pre;tab-size:4;overflow:auto}
.ol-b[data-kind=term]{background:#0c0c0c;color:#e8e8e8;border-color:#262626}
.ol-b[data-kind=term] .ol-h{color:#8a8a8a;border-color:#222}
.ol-b[data-kind=term] .ol-h button{border-color:#333}
.ol-b[data-kind=term] .ol-h button:hover{color:#fff;background:#1d1d1d}
.ol-b[data-kind=term] textarea{background:#0c0c0c;color:#e8e8e8}
.ol-lights{display:flex;gap:5px} .ol-lights i{width:8px;height:8px;border-radius:50%;background:#333} .ol-lights i:first-child{background:var(--red)}
.ol-term .p{color:#fff;font-weight:700} .ol-term .e{color:#ff5a5f} .ol-term .w{color:#f5c26b} .ol-term .ok{color:#7fd88f} .ol-term .d{color:#777}
/* syntax colours, kept to a Nothing palette: black/white, grey and one red */
.ol .hljs-keyword,.ol .hljs-built_in,.ol .hljs-type,.ol .hljs-meta .hljs-keyword{font-weight:700}
.ol .hljs-string,.ol .hljs-regexp,.ol .hljs-symbol{color:color-mix(in srgb,var(--text) 62%,var(--red))}
.ol .hljs-number,.ol .hljs-literal{color:var(--red)} .ol .hljs-comment,.ol .hljs-quote{color:var(--muted);font-style:italic}
.ol .hljs-title,.ol .hljs-title.function_,.ol .hljs-section{text-decoration:underline;text-decoration-color:var(--line);text-underline-offset:3px}
.ol .hljs-attr,.ol .hljs-attribute,.ol .hljs-variable,.ol .hljs-params{color:color-mix(in srgb,var(--text) 78%,transparent)}
.ol .hljs-meta,.ol .hljs-tag,.ol .hljs-name{color:var(--muted)}
.ol-todo{list-style:none;margin:0;padding:8px 10px 10px;flex:1;overflow:auto;scrollbar-width:thin}
.ol-todo li{display:flex;align-items:flex-start;gap:9px;padding:5px 4px;border-radius:8px}
.ol-todo li:hover{background:var(--inset)}
.ol-todo input{appearance:none;flex:none;width:17px;height:17px;margin:2px 0 0;border:1.5px solid var(--text);border-radius:50%;cursor:pointer;display:grid;place-items:center}
.ol-todo input:checked{background:var(--text)} .ol-todo input:checked::after{content:"";width:5px;height:5px;border-radius:50%;background:var(--bg)}
.ol-todo span{flex:1;outline:none;min-width:20px} .ol-todo li.done span{color:var(--muted);text-decoration:line-through}
.ol-todo .who{flex:none;border:1px solid var(--line);background:transparent;color:var(--muted);border-radius:999px;padding:1px 8px;font:10px/1.6 var(--mono);text-transform:uppercase;cursor:pointer}
.ol-todo .who.set{color:#fff;border-color:transparent;background:var(--wc)}
.ol-todo .x{flex:none;border:0;background:transparent;color:var(--muted);cursor:pointer;opacity:0;padding:0 2px}
.ol-todo li:hover .x{opacity:1}
.ol-add{margin:0 10px 10px;border:1px dashed var(--line);background:transparent;color:var(--muted);border-radius:10px;padding:6px;font:11px var(--mono);cursor:pointer}
.ol-b[data-kind=image]{background:transparent;padding:0} .ol-b[data-kind=image] img{width:100%;height:100%;object-fit:contain;display:block;pointer-events:none;user-select:none}
.ol-b[data-kind=ink]{background:transparent;border-color:transparent;box-shadow:none;overflow:visible}
.ol-b[data-kind=ink] svg{width:100%;height:100%;overflow:visible} .ol-b[data-kind=ink] path{fill:none;stroke-linecap:round;stroke-linejoin:round}
.ol-b[data-kind=ink].sel{border-color:var(--line)}
.ol-lnk{flex:1;display:flex;flex-direction:column;justify-content:center;gap:4px;padding:12px 16px;min-width:0}
.ol-lnk b{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}
.ol-lnk small{font:11px var(--mono);color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ol-lnk input{border:1px solid var(--line);background:var(--inset);color:inherit;border-radius:8px;padding:6px 9px;font:inherit;font-size:13px}
.ol-rs{position:absolute;width:24px;height:24px;display:none;z-index:4}
.ol-rs[data-c=se]{right:0;bottom:0;cursor:nwse-resize} .ol-rs[data-c=nw]{left:0;top:0;cursor:nwse-resize}
.ol-rs[data-c=ne]{right:0;top:0;cursor:nesw-resize}   .ol-rs[data-c=sw]{left:0;bottom:0;cursor:nesw-resize}
.ol-rs::after{content:"";position:absolute;width:11px;height:11px;border:0 solid var(--sel);transition:width .15s,height .15s}
.ol-rs[data-c=se]::after{right:5px;bottom:5px;border-right-width:3px;border-bottom-width:3px;border-bottom-right-radius:4px}
.ol-rs[data-c=nw]::after{left:5px;top:5px;border-left-width:3px;border-top-width:3px;border-top-left-radius:4px}
.ol-rs[data-c=ne]::after{right:5px;top:5px;border-right-width:3px;border-top-width:3px;border-top-right-radius:4px}
.ol-rs[data-c=sw]::after{left:5px;bottom:5px;border-left-width:3px;border-bottom-width:3px;border-bottom-left-radius:4px}
.ol-rs:hover::after{width:15px;height:15px}
.ol-b.sel .ol-rs{display:block}
.ol-flash{animation:olflash .9s ease-out}
@keyframes olflash{0%{outline-color:var(--sel)}100%{outline-color:transparent}}

/* chrome */
.ol-tools{position:absolute;left:14px;top:50%;transform:translateY(-50%);display:flex;flex-direction:column;gap:2px;padding:6px;border-radius:22px;z-index:5}
.ol-tools button{width:38px;height:38px;border:0;border-radius:14px;background:transparent;color:var(--text);cursor:pointer;display:grid;place-items:center;transition:background .2s,transform .4s var(--bounce)}
.ol-tools button svg{width:19px;height:19px} .ol-tools button:hover{background:var(--inset)} .ol-tools button:active{transform:scale(.9)}
.ol-tools button.on{background:var(--text);color:var(--bg)}
.ol-sep{height:1px;margin:4px 8px;background:var(--line)}
.ol-inks{display:none;flex-direction:column;align-items:center;gap:6px;padding:6px 0 2px}
.ol[data-tool=pen] .ol-inks{display:flex}
.ol-inks button{width:18px;height:18px;border-radius:50%;border:2px solid var(--bg);box-shadow:0 0 0 1px var(--line);padding:0}
.ol-inks button.on{box-shadow:0 0 0 2px var(--text)}
.ol-top{position:absolute;top:12px;right:12px;display:flex;align-items:center;gap:10px;padding:6px 6px 6px 14px;border-radius:999px;z-index:5;
 font:11px var(--mono);letter-spacing:.08em;text-transform:uppercase;max-width:calc(100% - 90px)}
.ol-state{display:flex;align-items:center;gap:7px;white-space:nowrap} .ol-state i{width:7px;height:7px;border-radius:50%;background:var(--muted)}
.ol-state.live i{background:var(--red);animation:blink 1.6s steps(2) infinite} .ol-state.wait i{animation:blink .6s steps(2) infinite}
.ol-state.off i{background:transparent;border:1.5px solid var(--muted)}
.ol-code{border:1px dashed var(--line);background:transparent;color:var(--text);border-radius:999px;padding:5px 10px;font:inherit;cursor:pointer}
.ol-code:empty{display:none}
.ol-people{display:flex} .ol-people:empty{display:none}
.ol-people i{width:24px;height:24px;border-radius:50%;margin-left:-6px;border:2px solid var(--bg);display:grid;place-items:center;
 font:700 10px/1 var(--mono);color:#fff;font-style:normal;background:var(--pc)} .ol-people i.away{opacity:.35}
.ol-more{padding:6px 12px;font-size:12px;letter-spacing:.05em}
.ol-chatb{display:flex;align-items:center;gap:6px;padding:5px 11px 5px 6px;font-size:12px;letter-spacing:.05em;position:relative}
.ol-chatb[hidden]{display:none}
.ol-here{min-width:20px;height:20px;border-radius:10px;background:#2f9e5b;color:#fff;font:700 10px/20px var(--mono);text-align:center;padding:0 5px;box-sizing:border-box}
.ol-unread{position:absolute;top:-5px;right:-5px;min-width:17px;height:17px;border-radius:9px;background:var(--red);color:#fff;font:700 9px/17px var(--mono);font-style:normal;text-align:center;padding:0 4px;box-sizing:border-box}
.ol-unread:empty{display:none}
.ol-chat{position:absolute;top:64px;right:12px;width:340px;max-width:calc(100% - 24px);height:min(470px,calc(100% - 150px));display:flex;flex-direction:column;border-radius:18px;z-index:4;overflow:hidden}
.ol-chat[hidden]{display:none}
.ol-chh{display:flex;align-items:center;gap:8px;padding:10px 10px 8px 16px;border-bottom:1px solid var(--line)}
.ol-chh b{font-size:13px;flex:1}
.ol-chn{display:flex;flex-wrap:wrap;align-items:center;gap:5px;padding:7px 12px;border-bottom:1px solid var(--line);font:11px var(--mono);color:var(--muted)}
.ol-chip{border:0;border-radius:999px;padding:3px 9px 3px 7px;font:11px var(--mono);background:var(--inset);color:var(--text);cursor:pointer;display:inline-flex;align-items:center;gap:5px}
.ol-chip::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--pc)} .ol-chip:hover{background:color-mix(in srgb,var(--pc) 22%,var(--inset))}
.ol-chday{align-self:center;font:10.5px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--muted);padding:4px 10px}
.ol-chnew{display:flex;align-items:center;gap:8px;font:10.5px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--red)}
.ol-chnew::before,.ol-chnew::after{content:"";flex:1;height:1px;background:var(--red);opacity:.5}
.ol-msg{position:relative} .ol-msg[title]{cursor:pointer} .ol-msg.cont{margin-top:-5px} .ol-msg.cont small{display:none}
.ol-msg.tome{box-shadow:inset 3px 0 0 #f2c200}
.ol-mbody a{color:inherit;text-decoration:underline;overflow-wrap:anywhere}
.ol-ment{font-weight:700;color:#0a84ff} .ol-ment.me{color:#000;background:#f2c200;border-radius:4px;padding:0 3px}
.ol-macts{position:absolute;top:-12px;right:6px;display:none;gap:2px;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:2px;z-index:1}
.ol-msg:hover .ol-macts{display:flex} .ol-msg.mine .ol-macts{right:auto;left:6px}
.ol-macts button{border:0;background:transparent;color:var(--text);font:10.5px var(--mono);padding:3px 6px;border-radius:6px;cursor:pointer;white-space:nowrap}
.ol-macts button:hover{background:var(--inset)}
.ol-retry{border:0;background:none;color:var(--red);font:inherit;text-decoration:underline;cursor:pointer;padding:0}
.ol-chtyping{min-height:18px;padding:0 14px;font:11px var(--mono);color:var(--muted)}
.ol-chin.priv{box-shadow:inset 0 2px 0 var(--red)}
.ol-cht{resize:none;max-height:120px;line-height:1.4;font-family:inherit}
.ol-chatb.ment .ol-unread{background:#f2c200;color:#000}
.ol-chx{padding:3px 9px;font-size:12px}
.ol-chlist{flex:1;overflow:auto;padding:12px;display:flex;flex-direction:column;gap:8px}
.ol-msg{max-width:85%;align-self:flex-start;background:var(--inset);border-radius:14px 14px 14px 4px;padding:7px 11px;font-size:13px;line-height:1.4;overflow-wrap:anywhere;white-space:pre-wrap}
.ol-msg.mine{align-self:flex-end;border-radius:14px 14px 4px 14px;background:color-mix(in srgb,var(--text) 14%,transparent)}
.ol-msg.priv{box-shadow:inset 0 0 0 1px var(--red)}
.ol-msg small{display:block;font:10.5px var(--mono);color:var(--muted);margin-bottom:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ol-msg.failed{opacity:.5}
.ol-chnote{color:var(--muted);font-size:12.5px;text-align:center;margin:auto 10px;line-height:1.5}
.ol-chin{display:flex;gap:6px;padding:8px;border-top:1px solid var(--line);flex-wrap:wrap}
.ol-chto,.ol-cht{border:1px solid var(--line);background:var(--inset);color:var(--text);border-radius:10px;padding:7px 10px;font:inherit;font-size:13px;outline:none}
.ol-chto{flex:1 1 100%} .ol-cht{flex:1;min-width:0}
.ol-chsend{padding:6px 14px;font-size:12px}
/* Which board you are on, and the way to change it - one control, so it reads as something
   you can press rather than a label that happens to be clickable. */
.ol-switch{display:flex;align-items:center;gap:8px;max-width:330px;padding:5px 9px 5px 11px;border-radius:999px;
 border:1px solid var(--line);background:var(--inset);color:inherit;font:inherit;letter-spacing:inherit;
 text-transform:inherit;cursor:pointer;transition:border-color .2s,background .2s}
.ol-switch:hover{border-color:var(--text)}
.ol-switch:focus-visible{outline:2px solid var(--sel);outline-offset:2px}
.ol-switch[aria-expanded=true]{background:var(--text);color:var(--bg);border-color:var(--text)}
.ol-switch[aria-expanded=true] .ol-state i{background:var(--bg)}
.ol-chev{opacity:.55;font-size:9px;margin-left:-2px}
.ol-gname{font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:200px}
.ol-gname:not(:empty)::before{content:"\00b7";opacity:.45;margin-right:7px;font-weight:400}
.ol-menu{position:absolute;right:0;top:calc(100% + 8px);display:none;flex-direction:column;min-width:220px;padding:6px;border-radius:16px;
 background:var(--glass-strong);text-transform:none;letter-spacing:0;font:14px/1.3 -apple-system,"Segoe UI Variable","Segoe UI",system-ui,sans-serif}
.ol-menu.on{display:flex;animation:rise .35s var(--spring) both}
.ol-menu button,.ol-ctx button{border:0;background:transparent;color:var(--text);text-align:left;padding:9px 12px;border-radius:10px;cursor:pointer;font:inherit}
.ol-menu button:hover,.ol-ctx button:hover{background:var(--inset)}
.ol-menu small,.ol-ctx small{color:var(--muted);padding:6px 12px 2px;font:10.5px var(--mono);text-transform:uppercase;letter-spacing:.1em}

/* the right-click menu. Placed at the pointer, so it needs its own corner and no animation
   that moves it - it opens where you clicked and nowhere else. */
/* find on the board */
.ol-fix{position:absolute;right:14px;top:58px;width:286px;max-height:min(62vh,540px);z-index:8;
 display:flex;flex-direction:column;border-radius:16px;overflow:hidden}
.ol-fix[hidden]{display:none}
.ol-fix:not([hidden]){animation:rise .3s var(--spring) both}
.ol-fixh{display:flex;align-items:center;gap:8px;padding:10px 8px 10px 14px;border-bottom:1px solid var(--line)}
.ol-fixh b{font:10.5px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase}
.ol-fixn{flex:1;font:10.5px var(--mono);color:var(--muted)}
.ol-fixx{width:26px;height:26px;border-radius:50%;display:grid;place-items:center;font-size:11px;flex:none}
.ol-fixl{list-style:none;margin:0;padding:6px;overflow:auto;flex:1;min-height:40px}
.ol-fixl li{padding:7px 8px;border-radius:10px;display:flex;align-items:flex-start;gap:8px;font-size:12.5px}
.ol-fixl li:hover{background:var(--inset)}
.ol-fixl li.where{display:block;padding:10px 8px 3px;font:10px var(--mono);letter-spacing:.12em;
 text-transform:uppercase;color:var(--muted)}
.ol-fixl li.where:hover{background:transparent}
.ol-fixl li.none{display:block;color:var(--muted);font-size:12.5px;padding:14px 10px}
.ol-fixl .what{flex:1;line-height:1.35;word-break:break-word}
.ol-fixl .why{display:block;color:var(--muted);font-size:11px;margin-top:1px}
.ol-fixl button{border:1px solid var(--line);border-radius:999px;padding:3px 10px;font-size:11px;
 background:transparent;color:var(--text);flex:none}
.ol-fixl button:hover{background:var(--text);color:var(--bg);border-color:var(--text)}
.ol-fixf{display:flex;align-items:center;gap:8px;padding:9px 10px;border-top:1px solid var(--line);font-size:11.5px}
.ol-fixf label{display:flex;align-items:center;gap:6px;flex:1;color:var(--muted)}
.ol-fixf .ol-fixall{border:1px solid var(--line);border-radius:999px;padding:4px 11px;font-size:11px;
 background:transparent;color:var(--text)}
.ol-fixf .ol-fixall:hover{background:var(--text);color:var(--bg);border-color:var(--text)}
.ol-fixf .ol-fixall[disabled]{opacity:.4}
::highlight(bb-typo){text-decoration:underline wavy var(--red);text-decoration-skip-ink:none}
/* bottom left: the map, and a search box that is always there */
.ol-corner{position:absolute;left:12px;bottom:12px;z-index:6;display:flex;flex-direction:column;gap:8px;
 align-items:flex-start;max-width:min(300px,42vw)}
.ol-map{padding:6px;border-radius:14px;position:relative;line-height:0}
.ol-map[hidden]{display:none}
.ol-map canvas{width:260px;height:170px;border-radius:9px;cursor:pointer;display:block}
.ol-mapv{position:absolute;border:1.5px solid var(--sel);border-radius:4px;pointer-events:none;
 background:color-mix(in srgb,var(--sel) 12%,transparent)}
.ol-hunt{display:flex;align-items:center;gap:2px;height:40px;box-sizing:border-box;padding:0 5px;border-radius:999px}
.ol-hunt input{border:0;background:transparent;color:var(--text);outline:none;width:150px;height:30px;
 font:12.5px/30px var(--mono);padding:0 6px;margin:0}
.ol-hunt input::placeholder{color:var(--muted)}
.ol-huntn{color:var(--muted);font:10.5px/30px var(--mono);min-width:38px;text-align:center}
.ol-huntn:empty{display:none}
.ol-hunt button{width:30px;height:30px;padding:0;margin:0;border-radius:50%;display:grid;place-items:center;flex:none;line-height:0}
.ol-hunt button svg{width:15px;height:15px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;display:block}
.ol-hunt .ol-huntp,.ol-hunt .ol-huntx{border-color:transparent}
.ol-hunt .ol-huntp:hover,.ol-hunt .ol-huntx:hover{background:var(--inset)}
.ol-mapb.on{background:var(--text);color:var(--bg)}
/* zoomed far out, the writing is too small to read, so each one shows its name instead */
.ol.far .ol-b > *:not(.ol-tag):not(.ol-rs):not(.ol-srim){display:none}
.ol.far .ol-b{overflow:hidden}
.ol-tag{display:none;padding:0 calc(7px / var(--k,1));font-weight:700;line-height:1.2;
 font-size:calc(13px / var(--k,1));text-align:center;margin:auto;max-width:100%;
 overflow:hidden;word-break:break-word;-webkit-box-orient:vertical;-webkit-line-clamp:3}
.ol.far .ol-tag{display:-webkit-box}
/* a box is a container, not a card: it keeps its outline and wears its name at the top */
.ol.far .ol-b[data-kind=shape]{background:transparent}
.ol.far .ol-b[data-kind=shape] .ol-tag{margin:calc(9px / var(--k,1)) auto auto;-webkit-line-clamp:1}
.ol-gone{position:absolute;left:50%;top:12px;transform:translateX(-50%);display:none;align-items:center;gap:6px;
 padding:6px 6px 6px 12px;border-radius:999px;z-index:66;background:var(--glass-strong)}
.ol-find.on{display:flex;animation:rise .3s var(--spring) both}
.ol-find input{border:0;background:transparent;color:var(--text);outline:none;width:210px;
 font:14px -apple-system,"Segoe UI Variable","Segoe UI",system-ui,sans-serif;text-transform:none;letter-spacing:0}
.ol-find .ol-fn{color:var(--muted);font:11px var(--mono);min-width:46px;text-align:center}
.ol-find button{width:28px;height:28px;border-radius:50%;display:grid;place-items:center;font-size:12px}
.ol-b.found{outline-color:var(--sel)!important;box-shadow:0 0 0 6px color-mix(in srgb,var(--sel) 22%,transparent)}
html.cur-live .ol-board.linking,html.cur-live .ol-board.linking *{cursor:none!important}
.ol-board.linking{cursor:crosshair}
.ol-board{cursor:default}
.ol-board[data-rz=se],.ol-board[data-rz=nw]{cursor:nwse-resize} .ol-board[data-rz=ne],.ol-board[data-rz=sw]{cursor:nesw-resize}
.ol-board.grab,.ol-board.grab *{cursor:grab!important}
.ol-board.panning,.ol-board.panning *{cursor:grabbing!important}
html.cur-live .ol-board.grab,html.cur-live .ol-board.grab *,
html.cur-live .ol-board.panning,html.cur-live .ol-board.panning *{cursor:none!important}

.ol-ctx{position:absolute;left:0;top:0;display:none;flex-direction:column;min-width:214px;max-width:280px;padding:6px;
 max-height:calc(100% - 12px);overflow:auto;
 border-radius:14px;z-index:70;background:var(--glass-strong);text-transform:none;letter-spacing:0;
 font:14px/1.3 -apple-system,"Segoe UI Variable","Segoe UI",system-ui,sans-serif}
.ol-ctx.on{display:flex;animation:ctxin .16s var(--spring) both}
@keyframes ctxin{from{opacity:0;transform:scale(.94)}to{opacity:1;transform:none}}
html.no-anim .ol-ctx.on{animation:none}
.ol-ctx button{display:flex;align-items:center;gap:10px;white-space:nowrap}
.ol-ctx button span.k{margin-left:auto;color:var(--muted);font:11px var(--mono)}
.ol-ctx button.warn:hover{background:var(--red);color:#fff}
.ol-ctx hr{border:0;border-top:1px solid var(--line);margin:5px 8px}
.ol-ctx .cols{display:flex;flex-wrap:wrap;gap:6px;padding:6px 12px 8px}
.ol-ctx .cols i{width:22px;height:22px;border-radius:50%;cursor:pointer;border:1.5px solid rgba(0,0,0,.18)}
.ol-ctx .cols i:hover{transform:scale(1.14)}
/* the extra dressing a block can have, laid over everything above */
.ol-b{opacity:var(--op,1)}
.ol-b .ol-c,.ol-b .ol-todo span{text-decoration-line:var(--deco,none)}
.ol-b .ol-todo li.done span{text-decoration-line:line-through}
.ol-b.lh .ol-c,.ol-b.lh[data-kind] .ol-c{line-height:var(--lh)}
.ol-b.ls .ol-c,.ol-b.ls[data-kind] .ol-c{letter-spacing:var(--ls)}
.ol-b.rad:not([data-kind=shape]){border-radius:var(--rad)}
.ol-b[data-kind=shape].rad:not([data-form=ellipse])::after{border-radius:var(--rad)}
.ol-b.bd:not([data-kind=shape]){border-color:var(--bd);border-width:var(--bw,1px)}
.ol-b.flat{box-shadow:none} .ol-b.lifted{box-shadow:0 2px 4px rgba(0,0,0,.06),0 20px 48px rgba(0,0,0,.2)}
.ol-b.glow{box-shadow:0 0 0 1px color-mix(in srgb,var(--glow,var(--red)) 40%,transparent),0 0 26px color-mix(in srgb,var(--glow,var(--red)) 45%,transparent)}
.ol-b[data-kind=shape].thick::after{border-width:4px}
.ol-b[data-kind=shape].dots::after{border-style:dotted}
.ol-b[data-kind=shape].solid::after{background:color-mix(in srgb,var(--sh,var(--text)) 28%,transparent)}

/* the bar that floats over whatever is picked out */
.ol-fmt{position:absolute;left:0;top:0;z-index:6;display:none;align-items:center;gap:3px;padding:5px;border-radius:16px;
 max-width:calc(100% - 16px);flex-wrap:wrap;font:13px var(--mono);color:var(--text);user-select:none}
.ol-fmt.on{display:flex;animation:ctxin .14s var(--spring) both}
html.no-anim .ol-fmt.on{animation:none}
.ol-fmt .sep{width:1px;align-self:stretch;margin:4px 3px;background:var(--line)}
.ol-fmt button{height:34px;min-width:34px;padding:0 8px;margin:0;border:0;border-radius:9px;background:transparent;color:inherit;
 display:inline-flex;align-items:center;justify-content:center;gap:5px;cursor:pointer;font:inherit;line-height:0;flex:none}
.ol-fmt button:hover{background:var(--inset)}
.ol-fmt button.on{background:var(--text);color:var(--bg)}
.ol-fmt button svg{width:17px;height:17px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;display:block}
.ol-fmt button b,.ol-fmt button i,.ol-fmt button u,.ol-fmt button s{font:inherit;font-size:14px;line-height:1}
.ol-fmt button b{font-weight:800} .ol-fmt button i{font-style:italic;font-family:Georgia,serif}
.ol-fmt .chip{width:16px;height:16px;min-width:16px;max-width:16px;box-sizing:border-box;padding:0;border-radius:4px;border:1.5px solid rgba(128,128,128,.45);flex:0 0 16px;display:block}
.ol-fmt .chip.none{background:linear-gradient(135deg,transparent 45%,var(--red) 45% 55%,transparent 55%)}
.ol-fmt .lbl{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.ol-fmt select{height:30px;border:0;border-radius:9px;background:transparent;color:inherit;font:inherit;padding:0 4px;cursor:pointer;max-width:118px}
.ol-fmt select:hover{background:var(--inset)}
.ol-fmt select option{background:var(--bg);color:var(--text)}
.ol-fmt .num{width:34px;height:26px;border:1px solid var(--line);border-radius:7px;background:transparent;color:inherit;
 font:inherit;text-align:center;padding:0;-moz-appearance:textfield}
.ol-fmt .num::-webkit-inner-spin-button{display:none}
.ol-fmtpop{position:absolute;z-index:7;display:none;flex-direction:column;gap:10px;padding:14px;border-radius:16px;width:292px;
 font:12.5px var(--mono);color:var(--text)}
.ol-fmtpop.on{display:flex;animation:ctxin .14s var(--spring) both}
.ol-fmtpop>*{flex:none} .ol-fmtpop{max-height:calc(100% - 120px);overflow:auto;background:var(--glass-strong)}
html.no-anim .ol-fmtpop.on{animation:none}
.ol-fmtpop small{color:var(--muted);font-size:11px;letter-spacing:.1em;text-transform:uppercase}
.ol-fmtpop .sw{display:grid;grid-template-columns:repeat(8,26px);gap:7px;justify-content:space-between;
 width:auto;height:auto;min-height:0;border:0;border-radius:0;background:none;box-shadow:none;padding:0;margin:0}
.ol-fmtpop .sw::before,.ol-fmtpop .sw::after{content:none;display:none}
.ol-fmtpop .sw i{display:block;width:26px;height:26px;box-sizing:border-box;border-radius:50%;cursor:pointer;border:1.5px solid rgba(128,128,128,.35);transition:transform .15s}
.ol-fmtpop .sw i:hover{transform:scale(1.15)} .ol-fmtpop .sw i.on{outline:2px solid var(--text);outline-offset:1px}
.ol-fmtpop .row{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.ol-fmtpop .row button{border:1px solid var(--line);background:transparent;color:inherit;border-radius:999px;padding:7px 12px;font:inherit;cursor:pointer}
.ol-fmtpop .row button:hover{background:var(--inset)} .ol-fmtpop .row button.on{background:var(--text);color:var(--bg);border-color:var(--text)}
.ol-fmtpop input[type=color]{width:38px;height:30px;border:1px solid var(--line);border-radius:7px;padding:0;background:transparent;cursor:pointer}
.ol-fmtpop input[type=range]{flex:1;accent-color:var(--red)}

/* hover pop-outs: what a button does */
.ol-tip{position:absolute;z-index:80;pointer-events:none;max-width:260px;padding:6px 9px;border-radius:8px;
 background:var(--text);color:var(--bg);font:11px/1.4 var(--mono);letter-spacing:.02em;opacity:0;transform:translateY(3px);
 transition:opacity .12s,transform .12s;white-space:normal}
.ol-tip.on{opacity:1;transform:none}
.ol-tip b{font-weight:400;opacity:.6;margin-left:6px}
/* how the board itself looks, a choice for this computer only */
.ol[data-grid=lines] .ol-board{background-image:linear-gradient(var(--dot) 1px,transparent 1px),linear-gradient(90deg,var(--dot) 1px,transparent 1px)}
.ol[data-grid=none] .ol-board{background-image:none}
.ol[data-grid=big] .ol-board{background-image:radial-gradient(var(--dot) 1.8px,transparent 2.3px);background-size:calc(var(--g,22px) * 2) calc(var(--g,22px) * 2)}
.ol[data-paper=warm]{background:color-mix(in srgb,var(--bg) 90%,#f2c200)}
.ol[data-paper=cool]{background:color-mix(in srgb,var(--bg) 90%,#0a84ff)}
.ol[data-paper=green]{background:color-mix(in srgb,var(--bg) 90%,#2f9e5b)}
.ol[data-paper=dark]{background:color-mix(in srgb,var(--bg) 70%,#000)}
.ol-zoom{position:absolute;right:12px;bottom:12px;display:flex;gap:2px;padding:4px;border-radius:999px;z-index:5}
.ol-zoom button{border:0;background:transparent;color:var(--text);min-width:32px;height:30px;border-radius:999px;cursor:pointer;font:11px var(--mono);letter-spacing:.06em}
.ol-zoom button:hover{background:var(--inset)} .ol-zv{min-width:54px!important}
.ol-cursors{position:absolute;inset:0;pointer-events:none;overflow:hidden;z-index:4}
.ol-cur{position:absolute;left:0;top:0;transition:transform .09s linear;color:var(--pc)}
.ol-b.glide{transition:left .09s linear,top .09s linear,width .09s linear,height .09s linear}
.ol-cur svg{width:18px;height:18px;display:block;filter:drop-shadow(0 1px 2px rgba(0,0,0,.3))}
.ol-cur span{position:absolute;left:14px;top:16px;background:var(--pc);color:#fff;font:10px/1 var(--mono);letter-spacing:.06em;
 padding:4px 7px;border-radius:999px;white-space:nowrap;text-transform:uppercase}
.ol-toast{position:absolute;left:50%;bottom:18px;transform:translate(-50%,30px);opacity:0;z-index:7;pointer-events:none;
 background:var(--text);color:var(--bg);padding:9px 16px;border-radius:999px;font:11px var(--mono);letter-spacing:.08em;text-transform:uppercase;
 transition:transform .5s var(--bounce),opacity .25s;max-width:80%;text-align:center}
.ol-toast.on{transform:translate(-50%,0);opacity:1}
.ol-gate{position:absolute;inset:0;z-index:8;display:none;place-items:center;background:color-mix(in srgb,var(--bg) 55%,transparent);
 backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);padding:20px}
.ol-gate.on{display:grid}
.ol-card{width:min(460px,100%);border-radius:28px;padding:26px 28px;background:var(--glass-strong);animation:rise .6s var(--spring) both;max-height:100%;overflow:auto}
.ol-card h2{margin:0 0 6px;font:800 34px/1 var(--dotf);text-transform:uppercase}
.ol-card p{margin:6px 0 14px;color:var(--muted);font-size:14px}
.ol-card label{display:block;font:10.5px var(--mono);text-transform:uppercase;letter-spacing:.12em;color:var(--muted);margin:14px 0 6px}
.ol-card input{width:100%;padding:11px 16px;border-radius:999px;border:1px solid var(--line);background:var(--inset);color:var(--text);font:inherit;font-size:15px}
.ol-card .row2{display:flex;gap:8px} .ol-card .row2 input{flex:1;min-width:0}
.ol-card .btn{border:0;cursor:pointer;white-space:nowrap}
.ol-card .or{display:flex;align-items:center;gap:10px;margin:18px 0 4px;color:var(--muted);font:10.5px var(--mono);letter-spacing:.12em}
.ol-card .or::before,.ol-card .or::after{content:"";flex:1;height:1px;background:var(--line)}
.ol-card .msg{min-height:1.2em;margin:10px 0 0;font:11.5px var(--mono);color:var(--red)}
.ol-card .alt{display:block;margin:16px auto 0;border:0;background:transparent;color:var(--muted);font:11px var(--mono);letter-spacing:.08em;text-transform:uppercase;cursor:pointer;text-decoration:underline;text-underline-offset:3px}
.ol-groups{display:flex;flex-direction:column;gap:6px;margin-top:8px}
.ol-groups button{display:flex;justify-content:space-between;align-items:center;border:1px solid var(--line);background:transparent;color:var(--text);
 border-radius:14px;padding:11px 14px;cursor:pointer;font:inherit;text-align:left} .ol-groups button:hover{background:var(--inset)}
.ol-groups small{font:11px var(--mono);color:var(--muted)}
/* coding */
.ol-h button.run{background:var(--text);color:var(--bg);border-color:var(--text)}
.ol-out{flex:none;margin:0;max-height:42%;overflow:auto;padding:22px 14px 10px;border-top:1px solid var(--line);font:12px/1.5 var(--mono);
 white-space:pre-wrap;background:var(--inset);position:relative;scrollbar-width:thin}
.ol-out::before{content:attr(data-by);position:absolute;top:5px;left:14px;font-size:9.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.ol-out.bad{color:var(--red)} .ol-out.busy{color:var(--muted);animation:blink 1s steps(2) infinite}
.ol-proj{flex:1;min-height:0;display:grid;grid-template-columns:minmax(120px,32%) 1fr}
.ol-pfiles{list-style:none;margin:0;padding:8px 6px;border-right:1px solid var(--line);overflow:auto;font-size:13px;scrollbar-width:thin}
.ol-pfiles li{display:flex;align-items:center;gap:7px;padding:3px 6px;border-radius:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ol-pfiles li.main{font-weight:700} .ol-pfiles li.more{color:var(--muted);font-size:12px}
.ol-peek{margin:0;padding:10px 12px;overflow:hidden;font:11.5px/1.55 var(--mono);white-space:pre;color:var(--text);opacity:.9}
.fi{flex:none;min-width:26px;text-align:center;font:700 8.5px/16px var(--mono);text-transform:uppercase;border-radius:4px;font-style:normal;
 background:var(--inset);color:var(--muted);padding:0 3px}
.ol-ide{position:absolute;inset:0;z-index:9;display:none;flex-direction:column;background:var(--bg);border-radius:24px;overflow:hidden}
.ol-ide.on{display:flex;animation:rise .45s var(--spring) both}
.ide-top{display:flex;align-items:center;gap:8px;padding:10px 58px 10px 18px;border-bottom:1px solid var(--line);flex:none}
.ide-dot{width:8px;height:8px;border-radius:50%;background:var(--red);flex:none}
.ide-title{flex:1;min-width:60px;border:0;background:transparent;color:var(--text);font:700 15px/1.2 var(--mono);outline:none;padding:4px 0}
.ide-who{font:11px var(--mono);color:var(--muted);letter-spacing:.04em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ide-who button{margin-left:6px}
.ol-ide button,.ide-lang,.ide-where{border:1px solid var(--line);background:transparent;color:var(--text);border-radius:999px;padding:6px 12px;cursor:pointer;
 font:11px var(--mono);letter-spacing:.06em;text-transform:uppercase;white-space:nowrap}
.ol-ide button:hover{background:var(--inset)}
.ol-ide .ide-run{background:var(--text);color:var(--bg);border-color:var(--text);padding:6px 16px}
.ol-ide .ide-run:hover{background:var(--text);opacity:.85}
.ol-ide .ide-run.stop{background:var(--red);border-color:var(--red);color:#fff}
.ide-body{flex:1;min-height:0;display:flex}
.ide-files{width:220px;flex:none;border-right:1px solid var(--line);display:none;flex-direction:column;min-height:0}
.ol-ide.proj .ide-files{display:flex}
.ide-fh{display:flex;align-items:center;justify-content:space-between;padding:10px 10px 6px 16px;font:10.5px var(--mono);letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
.ol-ide .ide-fh button{padding:2px 9px;font-size:14px;line-height:1}
.ide-files ul{list-style:none;margin:0;padding:0 6px;overflow:auto;flex:1}
.ide-files li{display:flex;align-items:center;gap:8px;padding:5px 8px;border-radius:8px;cursor:pointer;font-size:13.5px}
.ide-files li span{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap} .ide-files li:hover{background:var(--inset)}
.ide-files li.on{background:var(--inset);font-weight:600} .ide-files li.main span::after{content:" ▶";color:var(--red);font-size:9px}
.ol-ide .ide-files li button{border:0;padding:0 4px;opacity:0;font-size:12px;border-radius:4px} .ide-files li:hover button{opacity:.7}
.ide-tip{margin:0;padding:10px 16px 14px;font-size:11.5px;color:var(--muted);line-height:1.45}
.ide-main{flex:1;min-width:0;display:flex;flex-direction:column}
.ide-live{display:none;flex:none;width:var(--lw,44%);min-width:240px;max-width:75%;border-left:1px solid var(--line);flex-direction:column;position:relative;min-height:0}
.ol-ide.live .ide-live{display:flex}
.ide-lgrip{position:absolute;top:0;bottom:0;left:-4px;width:8px;cursor:ew-resize;z-index:2}
.ide-lgrip:hover{background:linear-gradient(90deg,transparent 3px,var(--red) 3px,var(--red) 5px,transparent 5px)}
.ide-lh{display:flex;align-items:center;gap:8px;padding:8px 10px 6px 14px;font:10.5px var(--mono);letter-spacing:.12em;text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--line);min-height:36px;box-sizing:border-box}
.ide-lh .ide-lst{flex:1;text-transform:none;letter-spacing:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ol-ide .ide-lh button{padding:2px 9px;font-size:13px;line-height:1}
.ide-lbody{flex:1;min-height:0;display:flex}
.ide-lv,.ide-lout,.ide-lpc{display:none;flex:1;min-width:0}
.ide-lv{border:0;background:#fff}
.ide-lout{margin:0;padding:10px 14px;overflow:auto;font:12.5px/1.55 "Cascadia Code",Consolas,var(--mono);white-space:pre-wrap}
.ide-lout .e{color:var(--red)} .ide-lout .n{color:var(--muted);font-style:italic}
.ide-lpc{flex-direction:column;align-items:center;justify-content:center;gap:12px;padding:20px;text-align:center;color:var(--muted);font-size:13px;line-height:1.5}
.ol-ide[data-lmode=web] .ide-lv,.ol-ide[data-lmode=out] .ide-lout{display:block}
.ol-ide[data-lmode=pc] .ide-lpc,.ol-ide[data-lmode=none] .ide-lpc{display:flex}
.ol-ide[data-lmode=none] .ide-lgo{display:none}
.ol-ide .ide-disp.on{background:var(--text);color:var(--bg);border-color:var(--text)}
.ide-tabs{display:flex;gap:2px;padding:6px 8px 0;border-bottom:1px solid var(--line);overflow-x:auto;flex:none;scrollbar-width:none;min-height:36px}
.ol-ide .ide-tabs .tab{display:flex;align-items:center;gap:7px;border:0;border-radius:10px 10px 0 0;padding:7px 12px;text-transform:none;letter-spacing:0;
 font:13px -apple-system,"Segoe UI Variable","Segoe UI",system-ui,sans-serif;color:var(--muted)}
.ol-ide .ide-tabs .tab.on{background:var(--inset);color:var(--text);box-shadow:inset 0 -2px 0 var(--red)}
.ide-tabs .tab b{font-weight:400;opacity:.6;padding:0 2px} .ide-tabs .tab b:hover{opacity:1}
.ide-ed{flex:1;min-height:120px;position:relative}
.ide-ta{position:absolute;inset:0;width:100%;height:100%;border:0;resize:none;outline:none;padding:12px 16px;background:var(--bg);color:var(--text);
 font:13.5px/1.6 "Cascadia Code",Consolas,var(--mono);white-space:pre;tab-size:4}
.ide-panel{height:32%;min-height:110px;flex:none;border-top:1px solid var(--line);display:flex;flex-direction:column}
.ide-ph{display:flex;align-items:center;gap:6px;padding:6px 10px;flex:none}
.ol-ide .ide-ph [data-p]{border:0;color:var(--muted)} .ol-ide .ide-ph [data-p].on{color:var(--text);box-shadow:inset 0 -2px 0 var(--text);border-radius:0}
.ide-in{flex:1;min-width:80px;border:1px solid var(--line);background:var(--inset);color:var(--text);border-radius:999px;padding:6px 12px;font:12px var(--mono)}
.ide-out{flex:1;margin:0;padding:8px 16px 12px;overflow:auto;font:12.5px/1.55 "Cascadia Code",Consolas,var(--mono);white-space:pre-wrap;background:transparent}
.ide-out .e,.ide-out.bad{color:var(--red)} .ide-view{flex:1;border:0;background:#fff}
.ide-status{display:flex;gap:18px;padding:5px 16px;border-top:1px solid var(--line);font:10.5px var(--mono);letter-spacing:.06em;color:var(--muted);text-transform:uppercase;flex:none}
.ide-status .ide-saved{margin-left:auto}
.ide-start{position:absolute;inset:58px 0 0;display:none;place-items:center;background:color-mix(in srgb,var(--bg) 86%,transparent);z-index:3}
.ide-start.on{display:grid} .ide-start>div{text-align:center;padding:20px}
.ide-start h3{font:800 30px/1 var(--dotf);text-transform:uppercase;margin:0 0 8px} .ide-start p{color:var(--muted);margin:0 0 16px}
.ide-tpl{display:flex;flex-wrap:wrap;gap:8px;justify-content:center} .ol-ide .ide-tpl button{padding:12px 20px;font-size:12px}
.ide-fb{display:flex;gap:2px} .ol-ide .ide-fb button{border:0;padding:4px 5px;border-radius:6px;line-height:0;color:var(--muted)}
.ol-ide .ide-fb button:hover{color:var(--text)}
.ide-fb svg,.ide-note svg{width:15px;height:15px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.ide-files li.dir{color:var(--muted);font-weight:600;font-size:12.5px} .ide-files li.dir .fi{background:transparent;min-width:14px;font-size:11px}
.ide-files li.link span{font-style:italic} .ide-files li.link .fi{box-shadow:inset 0 0 0 1px var(--red)}
.ol-pfiles li.link span{font-style:italic}
.ide-main{position:relative}
.ide-note{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:7px 14px;border-bottom:1px solid var(--line);background:var(--inset);font-size:12.5px;color:var(--muted)}
.ide-note[hidden]{display:none} .ol-ide .ide-note button{padding:3px 10px;font-size:10px}
.ide-pick{position:absolute;top:44px;left:50%;transform:translateX(-50%);width:min(560px,92%);z-index:6;background:var(--bg);border:1px solid var(--line);
 border-radius:14px;box-shadow:0 18px 50px rgba(0,0,0,.18);overflow:hidden}
.ide-pick[hidden]{display:none}
.ide-pick input{width:100%;box-sizing:border-box;border:0;border-bottom:1px solid var(--line);background:transparent;color:var(--text);padding:12px 16px;font:14px var(--mono);outline:none}
.ide-pick ul{list-style:none;margin:0;padding:6px;max-height:340px;overflow:auto}
.ide-pick li{display:flex;align-items:baseline;gap:10px;padding:7px 10px;border-radius:8px;cursor:pointer;font-size:13.5px}
.ide-pick li small{margin-left:auto;color:var(--muted);font:11px var(--mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:55%}
.ide-pick li.on,.ide-pick li:hover{background:var(--inset)} .ide-pick li.on{box-shadow:inset 2px 0 0 var(--red)} .ide-pick li.none{color:var(--muted);cursor:default}
.ide-panel{position:relative}
.ide-grip{position:absolute;left:0;right:0;top:-4px;height:8px;cursor:ns-resize;z-index:2}
.ide-grip:hover{background:linear-gradient(transparent 3px,var(--red) 3px,var(--red) 5px,transparent 5px)}
.ide-pane{display:none;flex:1;min-height:0}
.ol-ide[data-panel=out] .ide-out,.ol-ide[data-panel=view] .ide-view{display:block}
.ol-ide[data-panel=con] .ide-con,.ol-ide[data-panel=find] .ide-find,.ol-ide[data-panel=ai] .ide-ai,.ol-ide[data-panel=term] .ide-term{display:flex;flex-direction:column}
.ide-tbar{display:flex;align-items:center;gap:8px;padding:0 10px 6px;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.ide-tbar input{flex:1;min-width:80px;text-transform:none;letter-spacing:0}
.ide-tbar .ide-tst{text-transform:none;letter-spacing:0;white-space:nowrap}
.ide-tin{margin:0 10px 10px}
.ol-ide.pcbusy .ide-tin{border-color:var(--red)}
.ol-ide:not([data-panel=out]) .ide-in{visibility:hidden}
.ol-ide[data-panel=view] .ide-clear,.ol-ide[data-panel=find] .ide-clear{visibility:hidden}
.ide-out a.ide-loc{color:inherit;text-decoration:underline;cursor:pointer}
.ide-img{display:block;max-width:min(100%,560px);margin:8px 0;border-radius:10px;background:#fff}
.ide-cout,.ide-tout{flex:1;margin:0;padding:8px 16px;overflow:auto;font:12.5px/1.55 "Cascadia Code",Consolas,var(--mono);white-space:pre-wrap}
.ide-cout .cmd,.ide-tout .cmd{color:var(--muted)} .ide-cout .e,.ide-tout .e{color:var(--red)} .ide-cout .n,.ide-tout .n{color:var(--muted);font-style:italic}
.ide-cin,.ide-tin,.ide-tbar input,.ide-fr input,.ide-ait{border:1px solid var(--line);background:var(--inset);color:var(--text);border-radius:12px;padding:8px 12px;font:12.5px "Cascadia Code",Consolas,var(--mono);outline:none}
.ide-cin{margin:0 10px 10px}
.ide-fr{display:flex;gap:6px;padding:0 10px 6px} .ide-fr input{flex:1;min-width:60px}
.ide-fres{list-style:none;margin:0;padding:0 6px 8px;overflow:auto;flex:1;font:12px/1.5 "Cascadia Code",Consolas,var(--mono)}
.ide-fres li{padding:3px 8px;border-radius:6px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ide-fres li:hover{background:var(--inset)} .ide-fres li b{font-weight:600;margin-right:10px;color:var(--muted)}
.ide-fres mark{background:var(--red);color:#fff;border-radius:3px;padding:0 1px} .ide-fres li.none{color:var(--muted);cursor:default}
.ide-ans{flex:1;overflow:auto;padding:4px 16px 8px;font-size:13.5px;line-height:1.55}
.ide-aiq{display:flex;flex-wrap:wrap;gap:6px;margin:4px 0 8px} .ide-aitip{color:var(--muted);font-size:12px;margin:0 0 8px}
.ide-ans .q{margin:12px 0 4px;font-weight:700} .ide-ans .q::before{content:"You  ";color:var(--red);font:10px var(--mono);letter-spacing:.1em}
.ide-ans .a p{margin:6px 0;white-space:pre-wrap} .ide-ans .a code{font:12.5px var(--mono);background:var(--inset);padding:1px 5px;border-radius:5px}
.ide-ans .busy{color:var(--muted);animation:blink 1s steps(2) infinite} .ide-ans .e{color:var(--red);margin:6px 0}
.ide-code{margin:8px 0;border:1px solid var(--line);border-radius:12px;overflow:hidden}
.ide-code pre{margin:0;padding:10px 14px;overflow:auto;font:12.5px/1.5 "Cascadia Code",Consolas,var(--mono);background:var(--inset);max-height:320px}
.ide-code>div{display:flex;gap:6px;padding:6px 8px;justify-content:flex-end} .ol-ide .ide-code button{padding:3px 10px;font-size:10px}
.ide-aiin{display:flex;gap:6px;padding:6px 10px 10px;align-items:flex-end} .ide-ait{flex:1;resize:none;font-family:inherit;font-size:13.5px;max-height:120px}
.ol-ide .ide-aib{border-color:var(--red);color:var(--red)}
.ol-ide .ide-x{border-color:var(--red);color:var(--red)} .ol-ide .ide-x:hover{background:var(--red);color:#fff}
.ide-status .ide-hint{margin-left:auto;opacity:.7} .ide-status .ide-saved{margin-left:0}
@media (max-width:760px){.ide-files{width:150px} .ol-ide .ide-vs,.ide-status .ide-hint{display:none}}
@media (max-width:700px){.ol-tools{top:auto;bottom:56px;transform:none;flex-direction:row;left:10px;right:10px;overflow-x:auto} .ol-sep{width:1px;height:auto;margin:8px 4px}
 .ol-top{left:12px;right:12px;max-width:none} .ol-gname{max-width:84px} .ol-switch{max-width:190px}}
"""

ON_JS = r"""
(() => {
const R = document.querySelector('#online .ol'); if (!R) return;
const $ = s => R.querySelector(s);
const board = $('.ol-board'), world = $('.ol-world'), links = $('.ol-links'), draft = $('.ol-draft path'),
  marquee = $('.ol-marquee'), cursLayer = $('.ol-cursors'), gate = $('.ol-gate'), card = $('.ol-card');
const api = () => window.pywebview && window.pywebview.api;
const CFG = (window.BB && BB.online) || {};
const sfx = (n, k) => { try { window.SFX && SFX.play(n, k); } catch (_) {} };
const store = {get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (_) { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (_) {} }};
// Plenty to choose from: a bright row, then the same hues again deeper, and some greys.
const PAL = ['#d71921', '#ff6b35', '#e08a00', '#f2c200', '#2f9e5b', '#0f9ea0', '#0a84ff', '#4c5fd7',
             '#8e4ec6', '#e0457b', '#8c5a2b', '#5b6b7f',
             '#8f0f16', '#a85500', '#1d6b3c', '#0a6e70', '#064a94', '#2e3a8c', '#5c2f85', '#94203f',
             '#111111', '#555555', '#9a9a9a', '#ffffff'];
const PEOPLE = PAL.slice(0, 20);
const personColor = c => { const k = okColor(c); if (!k) return PAL[0]; const n = parseInt(k.slice(1), 16);
  const l = ((n >> 16) * 299 + ((n >> 8) & 255) * 587 + (n & 255) * 114) / 1000; return l > 215 || l < 40 ? '#5b6b7f' : k; };
const STICKY = ['#fff4b8', '#ffe9a8', '#ffd6d6', '#ffcfe6', '#d9ecff', '#cfe9f7', '#dcf5dc', '#d8f0cf',
                '#ece0ff', '#ffe7cc', '#ececec', '#cfcfcf'];
const PAPER = ['#fff4b8', '#ffd9d9', '#ffd0e8', '#d9ecff', '#cde8f5', '#dcf5dc', '#ece0ff', '#ffe7cc',
               '#f3f3f3', '#e2e2e2', '#3a3a3a', '#1c1c1c'];
const INKS = ['auto', '#d71921', '#0a84ff', '#2f9e5b'];
const LANGS = ['auto', 'python', 'javascript', 'typescript', 'java', 'c', 'cpp', 'csharp', 'kotlin', 'html', 'css', 'sql', 'bash', 'powershell', 'json', 'plaintext'];
const SIZES = {note: [280, 180], sticky: [200, 180], text: [360, 70], code: [460, 260], proj: [560, 340], term: [500, 240], todo: [300, 220], image: [320, 220], link: [300, 86], shape: [400, 280]};
const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : 'xxxxxxxx-xxxx-4xxx-8xxx-xxxxxxxxxxxx'.replace(/x/g, () => (Math.random() * 16 | 0).toString(16)));
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const now = () => new Date().toISOString();
const okColor = c => /^#[0-9a-f]{6}$/i.test(c || '') ? c : null;
// White picked in dark mode vanishes in light mode (and black the other way), so those two ends follow the theme
const themed = c => { const k = okColor(c); if (!k) return null; const n = parseInt(k.slice(1), 16);
  const l = ((n >> 16) * 299 + ((n >> 8) & 255) * 587 + (n & 255) * 114) / 1000; return l > 225 || l < 30 ? 'var(--text)' : k; };
const okImg = s => typeof s === 'string' && /^data:image\/(png|jpe?g|gif|webp);base64,[a-z0-9+/=]+$/i.test(s) ? s : '';
const okUrl = s => { try { const u = new URL(s); return /^https?:$/.test(u.protocol) ? u.href : ''; } catch (_) { return ''; } };
const throttle = (fn, ms) => { let t = 0, timer = null, last; return (...a) => { last = a; const d = Date.now() - t;
  if (d >= ms) { t = Date.now(); fn(...a); } else if (!timer) timer = setTimeout(() => { timer = null; t = Date.now(); fn(...last); }, ms - d); }; };
const toast = (() => { const el = $('.ol-toast'); let t; return msg => { el.textContent = msg; el.classList.add('on'); clearTimeout(t); t = setTimeout(() => el.classList.remove('on'), 2600); }; })();
const h = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const initials = n => (n || '?').trim().split(/\s+/).map(w => w[0]).join('').slice(0, 2).toUpperCase() || '?';

// ---------------------------------------------------------------- state
const S = {blocks: new Map(), nodes: new Map(), view: store.get('olView', {x: 80, y: 60, k: 1}), tool: 'select', ink: 'auto',
  sel: new Set(), editing: null, peers: new Map(), me: null, group: null, mode: 'local', members: new Map()};
const meName = () => store.get('olName', null) || (window.BB && BB.name) || 'Me';

// ---------------------------------------------------------------- safe HTML for notes (other people write these)
const ALLOWED = new Set(['B', 'STRONG', 'I', 'EM', 'U', 'S', 'STRIKE', 'BR', 'P', 'DIV', 'UL', 'OL', 'LI', 'H1', 'H2', 'H3', 'CODE', 'PRE', 'BLOCKQUOTE', 'A', 'SPAN', 'MARK', 'HR']);
function clean(html) {
  const doc = new DOMParser().parseFromString('<div>' + (html || '') + '</div>', 'text/html'), root = doc.body.firstChild;
  const walk = n => { [...n.childNodes].forEach(c => {
    if (c.nodeType === 3) return;
    if (c.nodeType !== 1 || !ALLOWED.has(c.tagName)) { if (c.nodeType === 1 && !/^(SCRIPT|STYLE|IFRAME|OBJECT|EMBED)$/.test(c.tagName)) { walk(c); c.replaceWith(...c.childNodes); } else c.remove(); return; }
    const href = c.tagName === 'A' ? okUrl(c.getAttribute('href')) : '';
    [...c.attributes].forEach(a => c.removeAttribute(a.name));
    if (href) c.setAttribute('href', href);
    walk(c); }); };
  walk(root); return root.innerHTML;
}

// ---------------------------------------------------------------- what pasted text is
const TERM_RE = /^(\s*(\$|>|#|PS [A-Za-z]:\\[^>]*>|[A-Za-z]:\\[^>]*>|>>>|\w[\w.-]*@[\w.-]+:|Traceback \(most recent|npm (ERR|WARN)|error(\[\w+\])?:|warning:|fatal:|Exception in thread|\s+at [\w.$<>]+\(|\s+File ".*", line \d+))/m;
function sniff(t) {
  const s = t.trim();
  if (/^https?:\/\/\S+$/.test(s)) return 'link';
  if (/\x1b\[[0-9;]*m/.test(t)) return 'term';
  const lines = s.split('\n');
  const termish = lines.slice(0, 8).filter(l => TERM_RE.test(l)).length;
  if (termish >= 1 && (lines.length > 1 || /^(PS |[A-Z]:\\|\$ )/.test(s))) return 'term';
  const codeish = lines.filter(l => /[;{}]\s*$|^\s*(def |class |import |from \S+ import|function |const |let |var |public |private |#include|return\b|if\s*\(|for\s*\(|while\s*\(|print\(|console\.|<\/?[a-z][\w-]*[ >])|=>|^\s{4}\S/.test(l)).length;
  if (codeish >= Math.max(1, lines.length * .3)) return 'code';
  return 'note';
}
function guessLang(t) {
  if (/^\s*(def |import |from \S+ import |print\(|elif |class \w+(\(.*\))?:)/m.test(t)) return 'python';
  if (/^\s*#include|\bprintf\(|\bint main\s*\(/m.test(t)) return /std::|cout|<iostream>/.test(t) ? 'cpp' : 'c';
  if (/\bpublic\s+(static\s+)?(class|void)|System\.out\./.test(t)) return 'java';
  if (/\busing System|Console\.Write/.test(t)) return 'csharp';
  if (/\bfun \w+\(|\bval \w+\s*=/.test(t)) return 'kotlin';
  if (/^\s*<(!doctype|html|div|head|body|\w+[^>]*>)/im.test(t)) return 'html';
  if (/^\s*(select|insert|update|create table|delete from)\b/im.test(t)) return 'sql';
  if (/^\s*[{\[]\s*"/.test(t)) return 'json';
  if (/^\s*[.#]?[\w-]+\s*\{[^}]*:[^}]*\}/m.test(t)) return 'css';
  if (/\binterface \w+|: (string|number|boolean)\b/.test(t)) return 'typescript';
  if (/\b(const|let|function|=>|console\.log|document\.)/.test(t)) return 'javascript';
  if (/^\s*(Get-|Set-|\$env:)/m.test(t)) return 'powershell';
  if (/^\s*(sudo |cd |ls |echo |git |npm |pip )/m.test(t)) return 'bash';
  return 'plaintext';
}
const noAnsi = t => (t || '').replace(/\x1b\[[0-9;?]*[A-Za-z]/g, '');

// ---------------------------------------------------------------- libraries, loaded when first needed
const libs = {};
function lib(url, global) {
  if (window[global]) return Promise.resolve(window[global]);
  return libs[url] || (libs[url] = new Promise((ok, no) => { const s = document.createElement('script'); s.src = url;
    s.onload = () => window[global] ? ok(window[global]) : no(new Error('not loaded')); s.onerror = () => { delete libs[url]; no(new Error('offline')); };
    document.head.appendChild(s); }));
}
const HLJS = 'https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js';
const SUPA = 'https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.45.4/dist/umd/supabase.js';
let hl = null; lib(HLJS, 'hljs').then(x => { hl = x; S.blocks.forEach(b => b.kind === 'code' && paint(b)); }).catch(() => {});
function highlight(code, lang) {
  const e = document.createElement('div'); e.textContent = code;
  if (!hl) return e.innerHTML;
  try { return (lang && lang !== 'auto' && hl.getLanguage(lang)) ? hl.highlight(code, {language: lang, ignoreIllegals: true}).value : hl.highlightAuto(code).value; }
  catch (_) { return e.innerHTML; }
}
function termHTML(text) {
  return noAnsi(text).split('\n').map(l => { const e = document.createElement('span'); e.textContent = l;
    const c = /^\s*(\$|PS [A-Za-z]:\\|[A-Za-z]:\\[^>]*>|>>>|\w[\w.-]*@[\w.-]+:)/.test(l) ? 'p'
      : /\b(error|exception|traceback|failed|fatal|not found|denied)\b/i.test(l) ? 'e'
      : /\b(warn(ing)?|deprecated)\b/i.test(l) ? 'w' : /\b(success(fully)?|passed|done|ok|compiled)\b/i.test(l) ? 'ok' : '';
    if (c) e.className = c; return e.outerHTML; }).join('\n');
}

// ---------------------------------------------------------------- view
let settle;
function applyView() {
  const v = S.view, dpr = devicePixelRatio || 1, px = n => Math.round(n * dpr) / dpr;
  // Landing the board on a fraction of a pixel makes the browser resample every letter, which
  // is what made text go soft after a zoom. Draw on whole device pixels; S.view keeps the exact
  // value, so panning and zooming still add up properly.
  world.style.transform = `translate(${px(v.x)}px,${px(v.y)}px) scale(${v.k})`;
  // Text inside a layer the browser is holding ready to move loses some of its sharpening, so
  // only ask for that while the board is actually moving.
  world.classList.add('moving');
  clearTimeout(settle);
  settle = setTimeout(() => world.classList.remove('moving'), 180);
  world.style.setProperty('--k', v.k);
  // Zoomed out, blocks stay exactly as they are (Min asked: no swapping them for their names),
  // and the dots behind stay still and the same size, whatever the view does
  drawMap();
  $('.ol-zv').textContent = Math.round(v.k * 100) + '%'; placeCursors(); saveView(); fmtSoon();
}
const saveView = (() => { let t; return () => { clearTimeout(t); t = setTimeout(() => store.set('olView', S.view), 400); }; })();
function toWorld(ev) { const r = board.getBoundingClientRect(), f = r.width / board.clientWidth || 1;
  return {x: ((ev.clientX - r.left) / f - S.view.x) / S.view.k, y: ((ev.clientY - r.top) / f - S.view.y) / S.view.k}; }
function zoomAt(k, sx, sy) {
  const v = S.view, nk = clamp(k, .12, 3); if (sx == null) { sx = board.clientWidth / 2; sy = board.clientHeight / 2; }
  v.x = sx - (sx - v.x) * nk / v.k; v.y = sy - (sy - v.y) * nk / v.k; v.k = nk; applyView();
}
function bounds(list) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  list.forEach(b => { if (b.kind === 'arrow') return; x0 = Math.min(x0, b.x); y0 = Math.min(y0, b.y); x1 = Math.max(x1, b.x + b.w); y1 = Math.max(y1, b.y + b.h); });
  return x0 === Infinity ? null : {x0, y0, x1, y1};
}
function fit(list, animate = true) {
  const bb = bounds(list || [...S.blocks.values()]); if (!bb) { S.view = {x: 80, y: 60, k: 1}; return applyView(); }
  const W = board.clientWidth, H = board.clientHeight, pad = 90;
  const k = clamp(Math.min((W - pad * 2) / (bb.x1 - bb.x0 || 1), (H - pad * 2) / (bb.y1 - bb.y0 || 1)), .12, 1.2);
  const target = {k, x: W / 2 - (bb.x0 + bb.x1) / 2 * k, y: H / 2 - (bb.y0 + bb.y1) / 2 * k};
  if (!animate || matchMedia('(prefers-reduced-motion: reduce)').matches) { S.view = target; return applyView(); }
  const from = {...S.view}, t0 = performance.now();
  const step = t => { const p = Math.min(1, (t - t0) / 420), e = 1 - Math.pow(1 - p, 3);
    S.view = {x: from.x + (target.x - from.x) * e, y: from.y + (target.y - from.y) * e, k: from.k + (target.k - from.k) * e}; applyView();
    if (p < 1) requestAnimationFrame(step); };
  requestAnimationFrame(step);
}
// the middle of what's on screen, in board units: where pasted things land
function centre() { return {x: (board.clientWidth / 2 - S.view.x) / S.view.k, y: (board.clientHeight / 2 - S.view.y) / S.view.k}; }

// ---------------------------------------------------------------- drawing blocks
function node(b) {
  let d = S.nodes.get(b.id);
  if (!d) { d = h('div', 'ol-b'); d.dataset.id = b.id; ['se', 'nw', 'ne', 'sw'].forEach(c => { const g = d.appendChild(h('i', 'ol-rs')); g.dataset.c = c; }); world.appendChild(d); S.nodes.set(b.id, d); }
  return d;
}
function place(b) {
  if (b.kind === 'arrow') return drawLinks();
  const d = node(b); d.dataset.kind = b.kind;
  Object.assign(d.style, {left: b.x + 'px', top: b.y + 'px', width: b.w + 'px', height: b.h + 'px', zIndex: b.z || 0});
  d.classList.toggle('sel', S.sel.has(b.id));
  d.classList.toggle('locked', locked(b));
  linksSoon();
}
// The fonts on offer. Two come with the app; the rest are whatever the computer already has.
const FONTS = {plain: '', mono: 'var(--mono)', dots: 'var(--dotf)',
               serif: 'Georgia,"Times New Roman",serif',
               round: '"Trebuchet MS","Segoe UI",sans-serif'};
const FONT_NAME = {plain: 'The usual', mono: 'Typewriter', dots: 'Dotted',
                   serif: 'Bookish', round: 'Rounded'};
const LH = {tight: '1.15', airy: '1.9'}, LS = {tight: '-.02em', wide: '.1em'};
const RAD = {square: '3px', soft: '9px', round: '26px', pill: '44px'};
// Everything a block can be dressed in, kept on the block itself so it travels with it.
function dress(b, d) {
  const D = b.data || {};
  d.classList.toggle('bold', !!D.bold);
  d.classList.toggle('italic', !!D.italic);
  if (D.align) d.dataset.align = D.align; else delete d.dataset.align;
  const set = (k, v) => v ? d.style.setProperty(k, v) : d.style.removeProperty(k);
  set('--tf', FONTS[D.font] || '');
  set('--tc', themed(D.tcolor));
  set('--bgc', okColor(D.bg));
  set('--fs2', D.fs ? clamp(D.fs, 9, 160) + 'px' : '');
  set('--caps', b.kind === 'text' && D.caps === false ? 'none' : '');
  set('--deco', [D.ul && 'underline', D.strike && 'line-through'].filter(Boolean).join(' '));
  set('--op', D.op != null && D.op < 1 ? String(clamp(+D.op || 1, .15, 1)) : '');
  d.classList.toggle('lh', !!LH[D.lh]); set('--lh', LH[D.lh]);
  d.classList.toggle('ls', !!LS[D.ls]); set('--ls', LS[D.ls]);
  d.classList.toggle('rad', !!RAD[D.rad]); set('--rad', RAD[D.rad]);
  const bd = themed(D.bd);
  d.classList.toggle('bd', !!bd); set('--bd', bd); set('--bw', bd && D.bw ? clamp(+D.bw, 1, 6) + 'px' : '');
  ['flat', 'lifted', 'glow'].forEach(k => d.classList.toggle(k, D.shadow === k));
  set('--glow', D.shadow === 'glow' ? okColor(D.glow) : '');
  d.classList.toggle('thick', b.kind === 'shape' && !!D.thick);
  d.classList.toggle('dots', b.kind === 'shape' && !!D.dots);
  d.classList.toggle('solid', b.kind === 'shape' && D.fill === 'solid');
}

function paint(b) {
  if (b.kind === 'arrow') return drawLinks();
  const d = node(b), rs = d.querySelector('.ol-rs'); place(b);
  if (S.editing === b.id) return;   // never replace what you're typing in
  [...d.childNodes].forEach(c => !(c.classList && c.classList.contains('ol-rs')) && c.remove());
  const add = e => d.insertBefore(e, rs), D = b.data || {};
  dress(b, d);
  const tag = add(h('div', 'ol-tag'));
  try { tag.textContent = shortName(b); } catch (_) { tag.textContent = b.kind; }
  if (b.kind === 'note') { const c = add(h('div', 'ol-c')); c.dataset.ph = 'Write something…'; c.innerHTML = clean(D.html); }
  else if (b.kind === 'text') { const c = add(h('div', 'ol-c', D.text || '')); c.dataset.ph = 'Title'; d.style.setProperty('--fs', clamp(D.size || 40, 14, 160) + 'px'); }
  else if (b.kind === 'sticky') { const c = add(h('div', 'ol-c', D.text || '')); c.dataset.ph = 'Idea…'; d.style.setProperty('--sc', okColor(D.color) || STICKY[0]); }
  else if (b.kind === 'shape') {
    d.dataset.form = D.form === 'ellipse' ? 'ellipse' : 'box';
    d.style.setProperty('--sh', themed(D.color) || 'var(--text)');
    d.classList.toggle('fill', !!D.fill); d.classList.toggle('dash', !!D.dash);
    ['n', 'e', 's', 'w'].forEach(side => add(h('i', 'ol-srim ' + side)));
    const c = add(h('div', 'ol-c', D.text || '')); c.dataset.ph = 'Name it…';
  }
  else if (b.kind === 'code' || b.kind === 'term') {
    const head = add(h('div', 'ol-h'));
    if (b.kind === 'term') { const l = head.appendChild(h('span', 'ol-lights')); l.append(h('i'), h('i'), h('i')); }
    head.appendChild(h('span', 'ol-t', D.title || (b.kind === 'term' ? 'Terminal' : (D.lang && D.lang !== 'auto' ? D.lang : 'Code'))));
    if (b.kind === 'code') { const s = head.appendChild(h('select')); s.title = 'Language';
      LANGS.forEach(l => { const o = h('option', '', l === 'auto' ? 'Auto' : l); o.value = l; s.appendChild(o); }); s.value = D.lang || 'auto';
      s.onchange = () => { change(b.id, x => { x.data = {...x.data, lang: s.value}; }); }; }
    if (b.kind === 'code') runButtons(head, b);
    const cp = head.appendChild(h('button', '', 'Copy')); cp.dataset.act = 'copy';
    const wrap = add(h('div', 'ol-code-wrap')), c = wrap.appendChild(h('pre', 'ol-c' + (b.kind === 'term' ? ' ol-term' : '')));
    c.innerHTML = b.kind === 'code' ? highlight(D.text || '', D.lang) : termHTML(D.text || '');
    if (!D.text) c.textContent = b.kind === 'code' ? '# double-click to write code' : '$ double-click and paste terminal output';
    if (b.kind === 'code') outBox(add, b);
  }
  else if (b.kind === 'proj') {
    const head = add(h('div', 'ol-h')); head.appendChild(h('span', 'ol-t', D.title || 'Project'));
    runButtons(head, b);
    const body = add(h('div', 'ol-proj')), list = body.appendChild(h('ul', 'ol-pfiles')), files = filesOf(b), m = mainOf(D);
    files.slice(0, 16).forEach(f => { const li = list.appendChild(h('li', (f.n === m ? 'main ' : '') + (f.link ? 'link' : ''))); li.append(h('i', 'fi', ext(f.n) || '·'), h('span', '', f.n)); });
    if (files.length > 16) list.appendChild(h('li', 'more', `+${files.length - 16} more`));
    if (!files.length) list.appendChild(h('li', 'more', 'Double-click to start'));
    const mf = files.find(f => f.n === m), peek = body.appendChild(h('pre', 'ol-peek'));
    if (mf) peek.innerHTML = highlight((mf.t || '').split('\n').slice(0, 40).join('\n'), {py: 'python', js: 'javascript', html: 'xml', htm: 'xml', css: 'css', java: 'java', ts: 'typescript'}[ext(m)] || 'auto');
    outBox(add, b);
  }
  else if (b.kind === 'todo') {
    const head = add(h('div', 'ol-h')); head.appendChild(h('span', 'ol-t', D.title || 'Checklist'));
    const items = D.items || [], left = items.filter(i => !i.done).length;
    head.appendChild(h('span', '', items.length ? `${items.length - left}/${items.length}` : ''));
    const ul = add(h('ul', 'ol-todo'));
    items.forEach((it, i) => { const li = ul.appendChild(h('li', it.done ? 'done' : ''));
      const cb = li.appendChild(h('input')); cb.type = 'checkbox'; cb.checked = !!it.done; cb.dataset.i = i;
      li.appendChild(h('span', '', it.t || '')).dataset.i = i;
      const who = S.members.get(it.who), w = li.appendChild(h('button', 'who' + (who ? ' set' : ''), who ? initials(who.name) : '+'));
      w.dataset.who = i; w.title = who ? `${who.name} is doing this. Click to change` : 'Give this to someone';
      if (who) w.style.setProperty('--wc', personColor(who.color));
      const x = li.appendChild(h('button', 'x', '×')); x.dataset.del = i; x.title = 'Remove'; });
    add(h('button', 'ol-add', '+ Add item')).dataset.act = 'additem';
  }
  else if (b.kind === 'image') { const img = add(h('img')); img.alt = D.alt || 'Picture'; img.src = okImg(D.src); img.draggable = false; }
  else if (b.kind === 'link') {
    const box = add(h('div', 'ol-lnk')), u = okUrl(D.url);
    let host = ''; try { host = new URL(u).hostname.replace(/^www\./, ''); } catch (_) {}
    box.appendChild(h('b', '', D.title || host || 'Link'));
    box.appendChild(h('small', '', u || 'double-click to add the address'));
    if (u) { const head = h('div', 'ol-h'); head.appendChild(h('span', 'ol-t', host)); const o = head.appendChild(h('button', '', 'Open')); o.dataset.act = 'open'; d.insertBefore(head, box); }
  }
  else if (b.kind === 'ink') {
    const ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns, 'svg'), p = document.createElementNS(ns, 'path');
    svg.setAttribute('viewBox', `0 0 ${D.vw || b.w} ${D.vh || b.h}`); svg.setAttribute('preserveAspectRatio', 'none');
    p.setAttribute('d', /^[MLQCZ0-9.,\s-]*$/i.test(D.d || '') ? D.d : ''); p.setAttribute('stroke', 'currentColor'); p.style.stroke = themed(D.color) || '';
    p.setAttribute('stroke-width', clamp(D.width || 3, 1, 20)); p.setAttribute('vector-effect', 'non-scaling-stroke');
    svg.appendChild(p); add(svg);
  }
  markPeers();
}
function runButtons(head, b) {
  if (canRun(b)) { const r = head.appendChild(h('button', 'run', running.has(b.id) ? '■ Stop' : '▶ Run')); r.dataset.act = 'run'; r.title = 'Run it here, safely inside the app'; }
  const o = head.appendChild(h('button', '', 'Open')); o.dataset.act = 'ide'; o.title = 'Open in the code editor';
}
function outBox(add, b) {
  const out = b.data.out; if (!out && !running.has(b.id)) return;
  const o = add(h('pre', 'ol-out' + (out && !out.ok ? ' bad' : '')));
  if (running.has(b.id)) { o.textContent = 'Running…'; o.classList.add('busy'); return; }
  const lines = (out.text || '').replace(/\s+$/, '').split('\n');
  o.textContent = (lines.length > 8 ? '…\n' : '') + lines.slice(-8).join('\n') || '(no output)';
  o.dataset.by = `${out.ok ? 'Ran' : 'Error'} · ${out.by || ''}`;
}
function unpaint(id) { S.nodes.get(id)?.remove(); S.nodes.delete(id); S.sel.delete(id); drawLinks(); fmtSoon(); }
function paintAll() { S.nodes.forEach((_, id) => { if (!S.blocks.has(id)) unpaint(id); }); S.blocks.forEach(paint); drawLinks(); emptyState(); }
const emptyState = () => R.classList.toggle('has-blocks', S.blocks.size > 0);

// arrows: a line from the edge of one block to the edge of the other
function edge(b, tx, ty) {
  const cx = b.x + b.w / 2, cy = b.y + b.h / 2, dx = tx - cx, dy = ty - cy;
  if (!dx && !dy) return {x: cx, y: cy};
  const s = Math.min(Math.abs((b.w / 2 + 6) / (dx || 1e-9)), Math.abs((b.h / 2 + 6) / (dy || 1e-9)));
  return {x: cx + dx * s, y: cy + dy * s};
}
// Where a joining line runs, and the point halfway along it for the label.
function linkPath(a, c, form) {
  const p1 = edge(a, c.x + c.w / 2, c.y + c.h / 2), p2 = edge(c, a.x + a.w / 2, a.y + a.h / 2);
  if (form === 'straight') return {d: `M${p1.x},${p1.y} L${p2.x},${p2.y}`, mid: {x: (p1.x + p2.x) / 2, y: (p1.y + p2.y) / 2}};
  if (form === 'elbow') {
    // one right-angled turn, along whichever axis the two blocks are further apart on
    const across = Math.abs(p2.x - p1.x) >= Math.abs(p2.y - p1.y);
    const k1 = across ? {x: (p1.x + p2.x) / 2, y: p1.y} : {x: p1.x, y: (p1.y + p2.y) / 2};
    const k2 = across ? {x: (p1.x + p2.x) / 2, y: p2.y} : {x: p2.x, y: (p1.y + p2.y) / 2};
    return {d: `M${p1.x},${p1.y} L${k1.x},${k1.y} L${k2.x},${k2.y} L${p2.x},${p2.y}`,
            mid: {x: (k1.x + k2.x) / 2, y: (k1.y + k2.y) / 2}};
  }
  const mx = (p1.x + p2.x) / 2, my = (p1.y + p2.y) / 2;
  const bend = Math.min(60, Math.hypot(p2.x - p1.x, p2.y - p1.y) * .12);
  const nx = -(p2.y - p1.y), ny = p2.x - p1.x, nl = Math.hypot(nx, ny) || 1;
  const qx = mx + nx / nl * bend, qy = my + ny / nl * bend;
  return {d: `M${p1.x},${p1.y} Q${qx},${qy} ${p2.x},${p2.y}`,
          mid: {x: (p1.x + 2 * qx + p2.x) / 4, y: (p1.y + 2 * qy + p2.y) / 4}};   // halfway along the curve
}
let linksT = 0;
function linksSoon() { if (!linksT) linksT = requestAnimationFrame(() => { linksT = 0; drawLinks(); }); }
function drawLinks() {
  const ns = 'http://www.w3.org/2000/svg';
  [...links.querySelectorAll(':scope > path, :scope > text, :scope > rect')].forEach(p => p.remove());   // not the arrowhead inside <defs>
  S.blocks.forEach(b => {
    if (b.kind !== 'arrow') return;
    const a = S.blocks.get(b.data?.from), c = S.blocks.get(b.data?.to); if (!a || !c) return;
    const D = b.data || {}, {d, mid} = linkPath(a, c, D.form), on = S.sel.has(b.id), col = themed(D.color);
    const line = document.createElementNS(ns, 'path');
    line.setAttribute('d', d);
    const ends = D.ends || 'to';
    if (ends === 'to' || ends === 'both') line.setAttribute('marker-end', 'url(#ol-head)');
    if (ends === 'from' || ends === 'both') line.setAttribute('marker-start', 'url(#ol-head)');
    if (D.thick) line.style.strokeWidth = '3.6';
    if (D.dash) line.setAttribute('stroke-dasharray', D.thick ? '11 8' : '7 6');
    if (on) line.setAttribute('class', 'sel'); else if (col) line.style.stroke = col;
    const hit = document.createElementNS(ns, 'path');
    hit.setAttribute('d', d); hit.setAttribute('class', 'hit'); hit.dataset.id = b.id;
    links.append(line, hit);
    if (D.label) {
      const t = document.createElementNS(ns, 'text');
      t.setAttribute('x', mid.x); t.setAttribute('y', mid.y);
      t.textContent = String(D.label).slice(0, 60);
      if (on) t.setAttribute('class', 'sel'); else if (col) t.style.fill = col;
      links.appendChild(t);
      const r = t.getBBox(), bg = document.createElementNS(ns, 'rect');   // a patch of background behind it
      bg.setAttribute('x', r.x - 5); bg.setAttribute('y', r.y - 2);
      bg.setAttribute('width', r.width + 10); bg.setAttribute('height', r.height + 4); bg.setAttribute('rx', 6);
      links.insertBefore(bg, t);
    }
  });
}

// ---------------------------------------------------------------- changing things (all edits come through here)
const history = [], future = [];
const snap = b => b ? JSON.parse(JSON.stringify(b)) : null;
function record(steps) { if (!steps.length) return; history.push(steps); if (history.length > 200) history.shift(); future.length = 0; }
function put(b, {send = true, soon = false} = {}) {
  b.updated_at = now(); S.blocks.set(b.id, b); paint(b); emptyState();
  if (send) Sync.save(b, soon);
  if (S.sel.has(b.id)) fmtSoon();
}
function change(id, fn, opts) { const b = S.blocks.get(id); if (!b) return; const before = snap(b); fn(b); record([{before, after: snap(b)}]); put(b, opts); }
// New lines come out the way the last one you liked was set, if you said so.
const LINE_DEFAULT = {ends: 'none', form: 'elbow', dash: true, thick: false, color: null};
const lineLook = () => store.get('olLine', null) || LINE_DEFAULT;
function rememberLine(D) {
  store.set('olLine', {ends: D.ends || 'to', form: D.form || 'curve', dash: !!D.dash, thick: !!D.thick, color: okColor(D.color)});
}
function add(kind, x, y, data = {}, opts = {}) {
  if (kind === 'arrow') data = {...lineLook(), ...data};
  else data = {...(store.get('olLooks', {})[kind] || {}), ...data};     // the look picked for new ones of this kind
  const [w, hgt] = opts.size || SIZES[kind] || [260, 160];
  const top = Math.max(0, ...[...S.blocks.values()].map(b => b.z || 0)) + 1;
  const b = {id: uuid(), kind, x: Math.round(x - (opts.centre === false ? 0 : w / 2)), y: Math.round(y - (opts.centre === false ? 0 : hgt / 2)), w, h: hgt, z: top, data};
  // pasting several things in a row shouldn't pile them exactly on top of each other
  if (opts.centre !== false) for (let i = 0; i < 30 && [...S.blocks.values()].some(o => o.kind !== 'arrow' && Math.abs(o.x - b.x) < 24 && Math.abs(o.y - b.y) < 24); i++) { b.x += 36; b.y += 36; }
  record([{before: null, after: snap(b)}]); put(b); sfx('tap');
  return b;
}
function remove(ids) {
  const all = new Set(ids);
  S.blocks.forEach(b => { if (b.kind === 'arrow' && (all.has(b.data?.from) || all.has(b.data?.to))) all.add(b.id); });
  const steps = [];
  all.forEach(id => { const b = S.blocks.get(id); if (!b) return; steps.push({before: snap(b), after: null});
    S.blocks.delete(id); unpaint(id); Sync.remove(b); });
  record(steps); emptyState(); if (steps.length) sfx('off');
}
function undo(redo) {
  const steps = (redo ? future : history).pop(); if (!steps) return;
  (redo ? history : future).push(steps);
  steps.forEach(s => { const want = redo ? s.after : s.before, id = (s.before || s.after).id;
    if (want) put(snap(want)); else { const b = S.blocks.get(id); if (b) { S.blocks.delete(id); unpaint(id); Sync.remove(b); } } });
  emptyState(); sfx('tab');
}
function select(ids, keep) {
  if (!keep) S.sel.clear(); ids.forEach(id => S.sel.add(id));
  S.nodes.forEach((d, id) => d.classList.toggle('sel', S.sel.has(id))); drawLinks(); fmtSoon(true);
}
// Shapes are meant to sit behind what they group, so they go to the back when made.
function toBack(ids) {
  let low = Math.min(0, ...[...S.blocks.values()].map(b => b.z || 0));
  ids.forEach(id => { const b = S.blocks.get(id); if (b) { b.z = --low; place(b); put(b); } });
}
const locked = b => !!(b && b.data && b.data.lock);
// What sits inside a box. Used so that dragging the box takes its contents along, which is
// what makes it a group rather than a drawing.
// The smallest unlocked box this point is in, the one nearest the front when they overlap.
function boxAt(p) {
  const hits = [...S.blocks.values()].filter(b => b.kind === 'shape' && !locked(b)
    && p.x > b.x && p.x < b.x + b.w && p.y > b.y && p.y < b.y + b.h);
  if (!hits.length) return null;
  return hits.sort((a, b) => (a.w * a.h) - (b.w * b.h) || (b.z || 0) - (a.z || 0))[0];
}
// How big this box has to be to hold what is in it: its contents, plus a margin, plus room
// at the top for its own name. Returns null when it is empty.
function needs(shape) {
  const list = inside(shape);
  if (!list.length) return null;
  const pad = 18, head = 40;
  const x0 = Math.min(...list.map(b => b.x)) - pad;
  const y0 = Math.min(...list.map(b => b.y)) - head;
  const x1 = Math.max(...list.map(b => b.x + b.w)) + pad;
  const y1 = Math.max(...list.map(b => b.y + b.h)) + pad;
  return {x0, y0, x1, y1};
}

// Let the box out until everything inside fits. It never pulls in - things that have been put
// side by side stay where they were put.
function growToFit(shape) {
  const n = needs(shape);
  if (!n) return false;
  const x = Math.min(shape.x, Math.round(n.x0)), y = Math.min(shape.y, Math.round(n.y0));
  const r = Math.max(shape.x + shape.w, Math.round(n.x1)), bm = Math.max(shape.y + shape.h, Math.round(n.y1));
  if (x === shape.x && y === shape.y && r - x === shape.w && bm - y === shape.h) return false;
  const before = snap(shape);
  shape.x = x; shape.y = y; shape.w = r - x; shape.h = bm - y;
  put(shape);
  return {before, after: snap(shape)};
}

// Every box that now holds something it did not hold before, or has had its contents moved.
function regrowBoxes(ids) {
  const touched = new Set();
  [...S.blocks.values()].filter(b => b.kind === 'shape' && !locked(b)).forEach(shape => {
    if (ids && !inside(shape).some(b => ids.includes(b.id)) && !ids.includes(shape.id)) return;
    const step = growToFit(shape);
    if (step) touched.add(step);
  });
  return [...touched];
}

function inside(shape) {
  return [...S.blocks.values()].filter(b => b.id !== shape.id && b.kind !== 'arrow' && !locked(b)
    && b.x + b.w / 2 > shape.x && b.x + b.w / 2 < shape.x + shape.w
    && b.y + b.h / 2 > shape.y && b.y + b.h / 2 < shape.y + shape.h);
}
function withInsides(ids) {
  const out = new Set(ids);
  ids.forEach(i => { const b = S.blocks.get(i);
    if (b && b.kind === 'shape') inside(b).forEach(c => out.add(c.id)); });
  return [...out];
}
function toFront(ids) { let top = Math.max(0, ...[...S.blocks.values()].map(b => b.z || 0)); ids.forEach(id => { const b = S.blocks.get(id); if (b && b.z !== top) { b.z = ++top; place(b); } }); }

// ---------------------------------------------------------------- typing inside a block
let editBefore = null;
function startEdit(id, at) {
  const b = S.blocks.get(id); if (!b || ['image', 'ink', 'arrow'].includes(b.kind)) return;
  if (locked(b)) return toast('That one is locked. Right-click it to unlock.');
  const coding = b.kind === 'code' || b.kind === 'proj';
  const who = peerOn(id);
  if (who) { if (coding) { if (S.editing) stopEdit(); IDE.open(b, true, who); return; } toast(`${who.name} is typing in this one`); return; }
  if (S.editing && S.editing !== id) stopEdit();
  S.editing = id; editBefore = snap(b); select([id]); Sync.presence();
  const d = node(b); d.classList.add('editing');
  if (coding) { IDE.open(b, false); return; }
  if (b.kind === 'term') {
    const wrap = d.querySelector('.ol-code-wrap'), ta = h('textarea'); ta.spellcheck = false; ta.value = b.data.text || '';
    ta.placeholder = b.kind === 'code' ? 'Write or paste code' : 'Paste terminal output'; wrap.appendChild(ta); ta.focus();
    ta.oninput = () => typed(b, {text: ta.value});
    ta.onkeydown = ev => { if (ev.key === 'Tab') { ev.preventDefault(); const s = ta.selectionStart; ta.setRangeText('    ', s, ta.selectionEnd, 'end'); typed(b, {text: ta.value}); } };
  } else if (b.kind === 'link') {
    const box = d.querySelector('.ol-lnk'); box.replaceChildren();
    const u = h('input'); u.placeholder = 'https://…'; u.value = b.data.url || '';
    const t = h('input'); t.placeholder = 'Name (optional)'; t.value = b.data.title || '';
    box.append(u, t); u.focus();
    const go = () => typed(b, {url: u.value.trim(), title: t.value});
    u.oninput = t.oninput = go; u.onkeydown = t.onkeydown = ev => { if (ev.key === 'Enter') stopEdit(); };
  } else if (b.kind === 'todo') {
    d.querySelectorAll('.ol-todo span').forEach(s => s.contentEditable = 'plaintext-only');
    const first = at && at.closest && at.closest('.ol-todo span') || d.querySelector('.ol-todo span');
    d.querySelectorAll('.ol-todo span').forEach(sp => Writing.watch(sp));
    if (first) caretEnd(first); else addItem(b);
  } else {
    const c = d.querySelector('.ol-c'); c.contentEditable = b.kind === 'note' ? 'true' : 'plaintext-only'; caretEnd(c);
    Writing.watch(c);
    c.oninput = () => typed(b, b.kind === 'note' ? {html: clean(c.innerHTML)}
      : {text: (b.kind === 'text' ? c.textContent : c.innerText).replace(/\n$/, '')});
    c.onpaste = ev => { if (b.kind !== 'note') return; const t = ev.clipboardData.getData('text/plain'); ev.preventDefault();
      document.execCommand('insertText', false, t); };
  }
}
function caretEnd(el) { el.focus(); const r = document.createRange(); r.selectNodeContents(el); r.collapse(false); const s = getSelection(); s.removeAllRanges(); s.addRange(r); }
function typed(b, patch) {
  b.data = {...b.data, ...patch}; b.updated_at = now();
  if (b.kind === 'note' || b.kind === 'sticky' || b.kind === 'todo') {   // grow to fit while typing
    const d = node(b), c = d.querySelector('.ol-c,.ol-todo'); if (c && c.scrollHeight > c.clientHeight + 2) { b.h = Math.min(1600, b.h + c.scrollHeight - c.clientHeight + 6); place(b); }
  }
  Sync.live(b); Sync.save(b);
}
function stopEdit() {
  const id = S.editing; if (!id) return; S.editing = null;
  if (IDE.isOn(id)) IDE.hide();
  const b = S.blocks.get(id); const d = S.nodes.get(id); d && d.classList.remove('editing');
  if (b) { if (b.kind === 'todo') readItems(b); const changed = JSON.stringify(editBefore?.data) !== JSON.stringify(b.data) || editBefore?.h !== b.h;
    if (changed) { record([{before: editBefore, after: snap(b)}]); put(b, {soon: true}); } else paint(b); }
  editBefore = null; Sync.presence(); R.focus({preventScroll: true});
}
function readItems(b) {
  const d = node(b), items = [...(b.data.items || [])];
  d.querySelectorAll('.ol-todo span').forEach(s => { const i = +s.dataset.i; if (items[i]) items[i] = {...items[i], t: s.innerText.trim()}; });
  b.data = {...b.data, items: items.filter((it, i) => it.t || it.done || it.who)};
}
function addItem(b) {
  if (S.editing === b.id) readItems(b);
  const items = [...(b.data.items || []), {t: '', done: false}]; b.data = {...b.data, items};
  const was = S.editing === b.id; S.editing = null; paint(b); if (!was) editBefore = snap(b);
  S.editing = b.id; node(b).classList.add('editing');
  const spans = node(b).querySelectorAll('.ol-todo span'); spans.forEach(s => s.contentEditable = 'plaintext-only');
  caretEnd(spans[spans.length - 1]); Sync.presence();
}

// ---------------------------------------------------------------- coding: files, running code, and a VS Code style editor
const RUN_EXT = {py: 'python', js: 'javascript', mjs: 'javascript', html: 'html', htm: 'html'};
const ext = n => ((n || '').includes('.') ? n.split('.').pop() : '').toLowerCase();
const EXT_FOR = {python: 'py', javascript: 'js', typescript: 'ts', java: 'java', c: 'c', cpp: 'cpp', csharp: 'cs', kotlin: 'kt', html: 'html', css: 'css', sql: 'sql', bash: 'sh', powershell: 'ps1', json: 'json'};
const codeLang = b => (b.data.lang && b.data.lang !== 'auto') ? b.data.lang : guessLang(b.data.text || '');
const cleanName = n => (n || '').trim().replace(/\\/g, '/').replace(/^\/+/, '').replace(/(^|\/)\.\.(?=\/|$)/g, '').replace(/[<>:"|?*\x00-\x1f]/g, '').slice(0, 80);
const isKeep = n => /(^|\/)\.keep$/.test(n);   // an empty folder is kept as folder/.keep
// a project file can be a link to another block on the board: its text is read from that block every time
const LINKABLE = {code: 1, note: 1, text: 1, sticky: 1, todo: 1, term: 1, link: 1};
function blockText(x) {
  const D = x.data || {};
  if (x.kind === 'term') return noAnsi(D.text || '');
  if (x.kind === 'note') {
    const html = clean(D.html || '').replace(/<br\s*\/?>/gi, '\n').replace(/<\/(p|div|li|h\d|blockquote|pre)>/gi, '$&\n').replace(/<li[^>]*>/gi, '$&- ');
    return new DOMParser().parseFromString(html, 'text/html').body.textContent.replace(/\n{3,}/g, '\n\n').trim() + '\n';
  }
  if (x.kind === 'todo') return (D.items || []).map(i => `[${i.done ? 'x' : ' '}] ${i.t || ''}`).join('\n') + '\n';
  if (x.kind === 'link') return (D.url || '') + '\n';
  return D.text || '';
}
// A handful of words for the far-out view: whatever this block would be called.
function shortName(b) {
  const D = b.data || {};
  const first = D.title || (b.kind === 'shape' ? D.text : '') || '';
  if (first.trim()) return first.trim().slice(0, 40);
  if (b.kind === 'image') return 'Picture';
  if (b.kind === 'link') return (D.url || 'Link').replace(/^https?:\/\//, '').slice(0, 30);
  if (b.kind === 'ink') return 'Drawing';
  if (b.kind === 'todo') {                       // what is still to do, not the ticks around it
    const items = (D.items || []).filter(i => i && i.t);
    const next = items.find(i => !i.done) || items[0];
    return next ? next.t.slice(0, 40) : 'Checklist';
  }
  const line = x => (x || '').split('\n').map(t => t.trim()).find(t => t) || '';
  if (b.kind === 'code' || b.kind === 'term') return line(blockText(b)).slice(0, 34);
  const words = (blockText(b) || '').replace(/\s+/g, ' ').trim();
  return words ? words.slice(0, 40) : (KIND_NAME[b.kind] || b.kind);
}
function linkName(x) {
  const D = x.data || {}, first = D.title || (x.kind === 'code' ? 'snippet' : (blockText(x).split('\n').find(l => l.trim()) || x.kind));
  let s = first.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 24) || x.kind;
  if (/^\d/.test(s)) s = 'b_' + s;
  return s + '.' + (x.kind === 'code' ? (EXT_FOR[codeLang(x)] || 'txt') : x.kind === 'note' ? 'md' : 'txt');
}
function filesOf(b) {
  if (b.kind === 'proj') return (b.data.files || []).filter(f => !isKeep(f.n)).map(f => {
    if (!f.link) return f; const x = S.blocks.get(f.link); return {n: f.n, t: x ? blockText(x) : '', link: f.link, gone: !x}; });
  return [{n: 'main.' + (EXT_FOR[codeLang(b)] || 'txt'), t: b.data.text || ''}];
}
function mainOf(D) {
  const fs = (D.files || []).filter(f => !isKeep(f.n));
  if (D.main && fs.some(f => f.n === D.main)) return D.main;
  return (fs.find(f => /^(main\.py|index\.html?|main\.m?js)$/i.test(f.n)) || fs.find(f => RUN_EXT[ext(f.n)]) || fs[0] || {}).n;
}
const mainFile = b => b.kind === 'proj' ? mainOf(b.data) : filesOf(b)[0].n;
const canRun = b => !!RUN_EXT[ext(mainFile(b))];
// "On this PC": the app writes the files to the project's folder and runs a command there with whatever is installed
const whereOf = b => b.data.where || (RUN_EXT[ext(mainFile(b))] ? 'app' : 'pc');
const projTitle = b => b.data.title || (b.kind === 'proj' ? 'Project' : 'Code');
const qn = s => /[\s&()]/.test(s) ? `"${s}"` : s;
const MAC = /Mac/.test(navigator.platform), OUT = MAC ? 'app' : 'app.exe', BIN = MAC ? './app' : 'app.exe';
function autoCmd(b) {
  const fs = filesOf(b).map(f => f.n), main = mainFile(b) || '', e = ext(main), all = x => fs.filter(n => ext(n) === x).map(qn).join(' ');
  const stem = main.split('/').pop().replace(/\.[^.]+$/, '');
  switch (e) {
    case 'py': return (MAC ? 'python3 ' : 'python ') + qn(main);
    case 'js': case 'mjs': case 'cjs': return 'node ' + qn(main);
    case 'ts': return 'npx --yes tsx ' + qn(main);
    case 'java': return fs.filter(n => ext(n) === 'java').length > 1 ? `javac -d out ${all('java')} && java -cp out ${stem}` : 'java ' + qn(main);
    case 'c': return `gcc ${all('c')} -o ${OUT} && ${BIN}`;
    case 'cpp': case 'cc': return `g++ ${all(e)} -o ${OUT} && ${BIN}`;
    case 'cs': return 'dotnet run';
    case 'go': return 'go run .';
    case 'rs': return fs.includes('Cargo.toml') ? 'cargo run' : `rustc ${qn(main)} -o ${OUT} && ${BIN}`;
    case 'html': case 'htm': return (MAC ? 'open ' : 'start "" ') + qn(main);
    case 'bat': case 'cmd': return qn(main);
    case 'rb': return 'ruby ' + qn(main);
    case 'php': return 'php ' + qn(main);
    default: return '';
  }
}
const runCmd = b => (b.data.cmd || '').trim() || autoCmd(b);

// Python runs in Pyodide (Python compiled for the browser) inside a worker: no access to the computer or the page
const PYODIDE = 'https://cdn.jsdelivr.net/pyodide/v0.26.4/full/';
const PY_WORKER = `importScripts('${PYODIDE}pyodide.js');
let py = null, g = null;
const PY = (...l) => l.join('\\n');
// a clean folder each run, so a deleted file can't still be imported; charts draw off-screen
const CLEAN = PY("import os, shutil", "for _n in os.listdir('/home/pyodide'):",
  "    _p = os.path.join('/home/pyodide', _n)", "    shutil.rmtree(_p) if os.path.isdir(_p) else os.remove(_p)");
const PREP = PY("import os, sys, warnings", "os.environ['MPLBACKEND'] = 'AGG'", "warnings.filterwarnings('ignore', message='.*non-GUI backend.*')", "os.chdir('/home/pyodide')",
  "if '/home/pyodide' not in sys.path: sys.path.insert(0, '/home/pyodide')",
  "for _m in [k for k, v in list(sys.modules.items()) if str(getattr(v, '__file__', '') or '').startswith('/home/pyodide')]: del sys.modules[_m]",
  "def _bb_repl(src, g):", "    try: c = compile(src, '<console>', 'single')", "    except SyntaxError: c = compile(src, '<console>', 'exec')", "    exec(c, g)");
// imports that aren't project files, aren't built in and aren't installed yet
const MISSING = PY("import ast, os, sys, importlib.util", "_need = set()",
  "for _r, _d, _fs in os.walk('.'):", "    for _f in _fs:", "        if not _f.endswith('.py'): continue",
  "        try: _t = ast.parse(open(os.path.join(_r, _f), encoding='utf-8').read())", "        except Exception: continue",
  "        for _n in ast.walk(_t):", "            if isinstance(_n, ast.Import): _need.update(a.name.split('.')[0] for a in _n.names)",
  "            elif isinstance(_n, ast.ImportFrom) and _n.module and not _n.level: _need.add(_n.module.split('.')[0])",
  "_local = {os.path.splitext(f)[0] for f in os.listdir('.')}",
  "[m for m in sorted(_need) if m not in _local and m not in sys.modules and importlib.util.find_spec(m) is None]");
// matplotlib charts become pictures in the Output panel
const FIGS = PY("import sys", "_imgs = []", "if 'matplotlib.pyplot' in sys.modules:", "    import io, base64", "    _plt = sys.modules['matplotlib.pyplot']",
  "    for _i in _plt.get_fignums():", "        _b = io.BytesIO(); _plt.figure(_i).savefig(_b, format='png', dpi=100, bbox_inches='tight')",
  "        _imgs.append(base64.b64encode(_b.getvalue()).decode())", "    _plt.close('all')", "_imgs");
const figs = () => { try { const r = py.runPython(FIGS), l = r.toJs(); r.destroy(); l.forEach(s => postMessage({t: 'img', s: 'data:image/png;base64,' + s})); } catch (_) {} };
const ready = loadPyodide({indexURL: '${PYODIDE}'}).then(p => { py = p; });
const tidy = m => { const L = String(m).split('\\n'), o = [];
  for (let i = 0; i < L.length; i++) { if (/^\\s+File "\\/lib\\//.test(L[i])) { while (L[i + 1] && /^\\s{4}/.test(L[i + 1]) && !/^\\s+File /.test(L[i + 1])) i++; continue; } o.push(L[i]); }
  return o.join('\\n').split('/home/pyodide/').join(''); };
onmessage = async ({data}) => {
  try { if (!py) postMessage({t: 'out', s: 'Starting Python (the first run takes a few seconds)…', note: true}); await ready; }
  catch (e) { postMessage({t: 'out', s: "Couldn't load Python. Check your internet.", err: true}); postMessage({t: 'end', ok: false}); return; }
  if (data.note) postMessage({t: 'clear'});
  py.setStdout({batched: s => postMessage({t: 'out', s})});
  py.setStderr({batched: s => postMessage({t: 'out', s, err: true})});
  const lines = (data.stdin || '').split('\\n'); let i = 0;
  py.setStdin({stdin: () => i < lines.length ? lines[i++] + '\\n' : undefined});
  const home = '/home/pyodide/', note = s => postMessage({t: 'out', s, note: true});
  try {
    if (data.repl != null) {   // the console: runs in what the last run left behind
      if (!g) { py.runPython(PREP); g = py.globals.get('dict')(); g.set('__name__', '__main__'); }
      py.globals.get('_bb_repl')(data.repl, g); figs(); postMessage({t: 'end', ok: true}); return;
    }
    py.runPython(CLEAN);
    for (const f of data.files) { const parts = f.n.split('/'); if (parts.length > 1) py.FS.mkdirTree(home + parts.slice(0, -1).join('/')); py.FS.writeFile(home + f.n, f.t); }
    py.runPython(PREP);
    const code = data.files.filter(f => /\\.py$/.test(f.n)).map(f => f.t).join('\\n');
    await py.loadPackagesFromImports(code, {messageCallback: m => { if (/^Loading /.test(m)) note(m.replace(/^Loading/, 'Getting') + '…'); }, errorCallback: () => {}});
    const want = py.runPython(MISSING), names = want.toJs(); want.destroy();
    if (names.length) {
      await py.loadPackage('micropip', {messageCallback: () => {}}); const mp = py.pyimport('micropip');
      for (const n of names) { note('Installing ' + n + '…'); try { await mp.install(n); } catch (_) { note("Couldn't install " + n + " (it may not work in the browser)"); } }
    }
    g = py.globals.get('dict')(); g.set('__name__', '__main__');
    await py.runPythonAsync(data.files.find(f => f.n === data.main).t, {globals: g, filename: data.main});
    figs(); postMessage({t: 'end', ok: true});
  } catch (e) { figs(); postMessage({t: 'out', s: tidy(e.message || e), err: true}); postMessage({t: 'end', ok: false}); }
};`;
const JS_WORKER = `const show = a => a.map(x => typeof x === 'string' ? x : (() => { try { return JSON.stringify(x); } catch (_) { return String(x); } })()).join(' ');
for (const k of ['log', 'info', 'warn', 'error', 'debug']) console[k] = (...a) => postMessage({t: 'out', s: show(a), err: k === 'warn' || k === 'error'});
onmessage = async ({data}) => {
  // files can use each other: require('./stats') and readFile('marks.csv')
  const files = data.files || [], cache = {};
  const find = n => { n = String(n).replace(/^\\.\\//, ''); return files.find(f => f.n === n) || files.find(f => f.n === n + '.js') || files.find(f => f.n === n + '.mjs'); };
  self.readFile = n => { const f = find(n); if (!f) throw new Error('No file called ' + n + ' in this project'); return f.t; };
  self.require = n => { const f = find(n); if (!f) throw new Error("Can't find " + n + ' in this project');
    if (/\\.json$/.test(f.n)) return JSON.parse(f.t);
    if (cache[f.n]) return cache[f.n].exports;
    const module = cache[f.n] = {exports: {}};
    new Function('module', 'exports', 'require', f.t + '\\n//# sourceURL=' + f.n)(module, module.exports, self.require); return module.exports; };
  try { await (0, eval)('(async () => {\\n' + data.code + '\\n})()'); postMessage({t: 'end', ok: true}); }
  catch (e) { postMessage({t: 'out', s: String(e && e.stack || e).split('\\n').filter(l => !/eval at|<anonymous>:|blob:/.test(l) || /^\\w*Error/.test(l)).join('\\n'), err: true}); postMessage({t: 'end', ok: false}); }
};`;
const workerFrom = src => new Worker(URL.createObjectURL(new Blob([src], {type: 'text/javascript'})));
let pyWorker = null;
const running = new Map();   // block id -> stop()
function runIn(kind, files, main, stdin, out, opt = {}) {
  return new Promise(done => {
    let w, timer, finished = false;
    const end = ok => { if (finished) return; finished = true; clearTimeout(timer); done(ok);
      if (kind === 'javascript') setTimeout(() => w.terminate(), 1500); };
    const stop = msg => { if (kind === 'python') { pyWorker && pyWorker.terminate(); pyWorker = null; } else w.terminate(); out('\n' + msg + '\n', true); end(false); };
    if (kind === 'python') { w = pyWorker || (pyWorker = workerFrom(PY_WORKER)); }
    else w = workerFrom(JS_WORKER);
    w.onmessage = ({data: m}) => { if (m.t === 'out') out(m.s + '\n', m.err, m.note); else if (m.t === 'clear') out(null); else if (m.t === 'img') opt.img && opt.img(m.s); else if (m.t === 'end') end(m.ok); };
    w.onerror = e => { out(String(e.message || e) + '\n', true); end(false); };
    const first = kind === 'python' && !pyWorker.started; if (kind === 'python') pyWorker.started = true;
    timer = setTimeout(() => stop('[Stopped: it ran for more than 30 seconds]'), (first ? 90 : 30) * 1000);
    running.stopNow = () => stop('[Stopped]');
    if (kind === 'python') w.postMessage(opt.repl != null ? {repl: opt.repl, note: first} : {files, main, stdin, note: first});
    else w.postMessage({code: (files.find(f => f.n === main) || {}).t || '', files});
  });
}
// a web page made of the project's files, with its console sent back to the Output panel
function webPage(files, main) {
  const get = n => { const f = files.find(x => x.n === n.replace(/^\.\//, '')); return f ? f.t : null; };
  const hook = '<script>(()=>{const s=(e,a)=>parent.postMessage({bbrun:1,err:e,s:a.map(x=>typeof x==="string"?x:JSON.stringify(x)).join(" ")},"*");'
    + 'for(const k of ["log","info","warn","error"]){const o=console[k];console[k]=(...a)=>{s(k==="warn"||k==="error",a);o.apply(console,a)}}'
    + 'addEventListener("error",e=>s(true,[e.message+" (line "+e.lineno+")"]))})()<\/script>';
  let html = get(main) || '';
  html = html.replace(/<link\b[^>]*?href=["']([^"']+)["'][^>]*>/gi, (m, n) => /stylesheet/i.test(m) && get(n) != null ? '<style>' + get(n) + '</style>' : m)
    .replace(/<script\b([^>]*?)\bsrc=["']([^"']+)["']([^>]*)>\s*<\/script>/gi, (m, a, n, c) => get(n) != null ? '<script' + a + c + '>' + get(n).replace(/<\/script/gi, '<\\/script') + '<\/script>' : m);
  return /<head[^>]*>/i.test(html) ? html.replace(/<head[^>]*>/i, x => x + hook) : hook + html;
}
async function run(b, stdin, sink, img) {
  if (running.has(b.id)) { running.get(b.id)(); return; }
  const files = filesOf(b), main = mainFile(b), kind = RUN_EXT[ext(main)];
  if (!kind) {
    const msg = `${main ? ext(main).toUpperCase() || 'This' : 'This'} code can't run inside the app. Pick "On this PC" next to Run to run it on your computer.`;
    sink && sink(msg + '\n', true); return finish(b.id, {text: msg, ok: false});
  }
  let text = '';
  const out = (s, err, note) => { if (s === null) { text = ''; sink && sink(null); return; } if (!note) text += s; sink && sink(s, err); };
  running.set(b.id, () => running.stopNow && running.stopNow());
  paintRun(b.id); IDE.busy(b.id, true); sfx('send');
  const ok = await runIn(kind, files, main, stdin, out, {img: src => { text += '[chart]\n'; img && img(src); }});
  running.delete(b.id); IDE.busy(b.id, false);
  finish(b.id, {text: text.length > 20000 ? '…\n' + text.slice(-20000) : text, ok});
  sfx(ok ? 'done' : 'error');
}
// the last run's output is shared with the group, unless someone else is typing in that block right now
function finish(id, out) {
  const b = S.blocks.get(id); if (!b) return;
  out = {...out, by: S.me ? S.me.name : meName(), at: now()};
  b.data = {...b.data, out};
  if (peerOn(id)) { paint(b); return; }
  if (S.editing === id) { Sync.live(b); Sync.save(b, true); paint(b); } else put(b, {soon: true});
}
const paintRun = id => { const b = S.blocks.get(id); if (b && S.editing !== id) paint(b); };
function boardRun(b) {
  if (RUN_EXT[ext(mainFile(b))] === 'html' || whereOf(b) === 'pc') { startEdit(b.id); (IDE.ready || Promise.resolve()).then(() => IDE.go()); return; }
  run(b, '', null);
}

// Monaco is the editor inside VS Code. It loads from the internet the first time a code block is opened.
const MONACO = 'https://cdnjs.cloudflare.com/ajax/libs/monaco-editor/0.45.0/min/';
let monacoP = null;
function loadMonaco() {
  if (window.monaco && window.monaco.editor) return Promise.resolve(window.monaco);
  return monacoP || (monacoP = new Promise((ok, no) => {
    window.MonacoEnvironment = {getWorkerUrl: () => URL.createObjectURL(new Blob([
      `self.MonacoEnvironment = {baseUrl: '${MONACO}'}; importScripts('${MONACO}vs/base/worker/workerMain.js');`], {type: 'text/javascript'}))};
    const s = document.createElement('script'); s.src = MONACO + 'vs/loader.min.js';
    s.onload = () => { const req = window.require; req.config({paths: {vs: MONACO + 'vs'}});
      req(['vs/editor/editor.main'], () => { if (window.define && window.define.amd) delete window.define.amd; ok(window.monaco); }, e => { monacoP = null; no(e); }); };
    s.onerror = () => { monacoP = null; no(new Error('offline')); };
    document.head.appendChild(s);
  }));
}
const MON = {py: 'python', js: 'javascript', mjs: 'javascript', ts: 'typescript', html: 'html', htm: 'html', css: 'css', json: 'json', java: 'java', c: 'c', h: 'c',
  cpp: 'cpp', hpp: 'cpp', cs: 'csharp', kt: 'kotlin', sql: 'sql', sh: 'shell', ps1: 'powershell', md: 'markdown', xml: 'xml', yml: 'yaml', yaml: 'yaml'};
const monLang = n => MON[ext(n)] || 'plaintext';
const TEMPLATES = {
  'Python': [{n: 'main.py', t: 'from helpers import greet\n\nname = input("Your name: ")\nprint(greet(name))\n'},
    {n: 'helpers.py', t: 'def greet(name):\n    return f"Hello, {name or \'group\'}!"\n'}],
  'Web page': [{n: 'index.html', t: '<!doctype html>\n<html>\n<head>\n  <meta charset="utf-8">\n  <title>Our page</title>\n  <link rel="stylesheet" href="style.css">\n</head>\n<body>\n  <h1>Hello, group!</h1>\n  <button id="go">Click me</button>\n  <script src="app.js"><\/script>\n</body>\n</html>\n'},
    {n: 'style.css', t: 'body {\n  font-family: system-ui, sans-serif;\n  margin: 40px;\n}\n\nbutton {\n  padding: 10px 18px;\n  border-radius: 999px;\n}\n'},
    {n: 'app.js', t: "document.getElementById('go').onclick = () => {\n  console.log('Clicked at', new Date().toLocaleTimeString());\n};\n"}],
  'Python + data': [{n: 'main.py', t: 'import csv\nimport matplotlib.pyplot as plt\n\nwith open("marks.csv") as f:\n    rows = list(csv.DictReader(f))\n\nnames = [r["module"] for r in rows]\nmarks = [int(r["mark"]) for r in rows]\nprint("Average:", sum(marks) / len(marks))\n\nplt.bar(names, marks, color="#d71921")\nplt.title("Marks")\nplt.show()\n'},
    {n: 'marks.csv', t: 'module,mark\nSDGP,68\nWeb,74\nDatabases,61\nMaths,79\n'}],
  'JavaScript': [{n: 'main.js', t: "const {average} = require('./stats');\n\nconst marks = [62, 71, 58, 80];\nconsole.log('Average:', average(marks));\n"},
    {n: 'stats.js', t: 'function average(xs) {\n  return xs.reduce((a, b) => a + b, 0) / xs.length;\n}\n\nmodule.exports = {average};\n'}],
  'Java': [{n: 'Main.java', t: 'public class Main {\n    public static void main(String[] args) {\n        Student s = new Student("Min", 72);\n        System.out.println(s.name + " got " + s.mark + " (" + s.grade() + ")");\n    }\n}\n'},
    {n: 'Student.java', t: 'public class Student {\n    final String name;\n    final int mark;\n\n    Student(String name, int mark) {\n        this.name = name;\n        this.mark = mark;\n    }\n\n    String grade() {\n        return mark >= 70 ? "First" : mark >= 60 ? "2:1" : mark >= 50 ? "2:2" : "Third";\n    }\n}\n'}],
  'C': [{n: 'main.c', t: '#include <stdio.h>\n#include "maths.h"\n\nint main(void) {\n    int marks[] = {62, 71, 58, 80};\n    printf("Average: %.1f\\n", average(marks, 4));\n    return 0;\n}\n'},
    {n: 'maths.h', t: '#ifndef MATHS_H\n#define MATHS_H\n\ndouble average(const int *xs, int n);\n\n#endif\n'},
    {n: 'maths.c', t: '#include "maths.h"\n\ndouble average(const int *xs, int n) {\n    double total = 0;\n    for (int i = 0; i < n; i++) total += xs[i];\n    return n ? total / n : 0;\n}\n'}],
  'C++': [{n: 'main.cpp', t: '#include <iostream>\n#include <string>\n#include <vector>\n\nint main() {\n    std::vector<std::string> team = {"Min", "Alex", "Sam"};\n    std::cout << "Team of " << team.size() << ":\\n";\n    for (const auto &name : team) std::cout << " - " << name << "\\n";\n    std::string you;\n    std::cout << "Your name: ";\n    std::getline(std::cin, you);\n    std::cout << "Hello, " << you << "!\\n";\n}\n'}],
  'Empty': [{n: 'README.md', t: '# Our project\n'}],
};
// typing one of these words in Python offers the whole shape
const PY_SNIPS = [
  ['def', 'def ${1:name}(${2}):\n    ${3:pass}', 'Function'],
  ['class', 'class ${1:Name}:\n    def __init__(self${2}):\n        ${3:pass}', 'Class'],
  ['for', 'for ${1:item} in ${2:items}:\n    ${3:print(item)}', 'Loop over a list'],
  ['forr', 'for ${1:i} in range(${2:10}):\n    ${3:print(i)}', 'Loop a number of times'],
  ['while', 'while ${1:True}:\n    ${2:break}', 'While loop'],
  ['ifmain', 'if __name__ == "__main__":\n    ${1:main()}', 'Only when this file is the one being run'],
  ['try', 'try:\n    ${1:pass}\nexcept ${2:Exception} as e:\n    print("Error:", e)', 'Catch an error'],
  ['open', 'with open("${1:data.txt}") as f:\n    ${2:text = f.read()}', 'Read a project file'],
  ['csv', 'import csv\n\nwith open("${1:data.csv}") as f:\n    rows = list(csv.DictReader(f))', 'Read a CSV file into rows'],
  ['inputint', '${1:n} = int(input("${2:Number: }"))', 'Ask for a whole number'],
  ['plot', 'import matplotlib.pyplot as plt\n\nplt.plot(${1:[1, 2, 3]}, ${2:[2, 4, 3]})\nplt.title("${3:Chart}")\nplt.show()', 'Chart (shows in Output)'],
];
const ICON = {
  file: '<svg viewBox="0 0 24 24"><path d="M6 3h8l4 4v14H6zM14 3v4h4M12 11v6M9 14h6"/></svg>',
  folder: '<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2zM12 10v6M9 13h6"/></svg>',
  up: '<svg viewBox="0 0 24 24"><path d="M12 16V4M7 9l5-5 5 5M4 20h16"/></svg>',
  link: '<svg viewBox="0 0 24 24"><path d="M10 14a4 4 0 005.7 0l3-3a4 4 0 00-5.7-5.7l-1 1M14 10a4 4 0 00-5.7 0l-3 3a4 4 0 005.7 5.7l1-1"/></svg>',
};

const IDE = (() => {
  const el = h('div', 'ol-ide'); R.appendChild(el);
  el.innerHTML = `<div class="ide-top"><i class="ide-dot"></i><input class="ide-title" maxlength="80" placeholder="Untitled" aria-label="Name">
    <span class="ide-who"></span><select class="ide-lang" aria-label="Language"></select>
    <button class="ide-run" title="Run (Ctrl+Enter or F5)">▶ Run</button><select class="ide-where" title="Where Run runs it" aria-label="Where to run"><option value="app">In the app</option><option value="pc">On this PC</option></select><button class="ide-disp" title="Show what you're making beside the code">Display</button><button class="ide-aib" title="Ask AI about your code (Ctrl+I)">Ask AI</button>
    <button class="ide-vs" title="Save these files to a folder and open it in VS Code">Open in VS Code</button>
    <button class="ide-more" title="All commands and shortcuts (Ctrl+Shift+P)" aria-label="All commands">⋯</button>
    <button class="ide-x" title="Back to the board (Esc)" aria-label="Back to the board">✕</button></div>
   <div class="ide-body"><aside class="ide-files"><div class="ide-fh"><span>Explorer</span><span class="ide-fb">
       <button class="ide-new" title="New file">${ICON.file}</button><button class="ide-dir" title="New folder">${ICON.folder}</button>
       <button class="ide-up" title="Upload files or a folder from your computer">${ICON.up}</button><button class="ide-ln" title="Link a block from the board (note, code, checklist…) as a file">${ICON.link}</button></span></div><ul></ul>
     <p class="ide-tip">Drop files or folders here. Double-click a name to rename it (type <b>src/app.py</b> to move it). ▶ picks the file Run starts from.</p></aside>
    <div class="ide-main"><div class="ide-tabs"></div><div class="ide-note" hidden></div><div class="ide-ed"></div>
     <div class="ide-pick" hidden><input aria-label="Search" spellcheck="false"><ul></ul></div>
     <div class="ide-panel"><i class="ide-grip" title="Drag to resize"></i><div class="ide-ph"><button data-p="out" class="on">Output</button><button data-p="view">Preview</button>
       <button data-p="term">Terminal</button><button data-p="con">Console</button><button data-p="find">Search</button><button data-p="ai">AI</button>
       <input class="ide-in" placeholder="Input for input() — one line each, separate with  |  " aria-label="Program input"><button class="ide-clear">Clear</button></div>
      <pre class="ide-out ide-pane"></pre><iframe class="ide-view ide-pane" sandbox="allow-scripts allow-modals" title="Preview"></iframe>
      <div class="ide-term ide-pane"><div class="ide-tbar"><span>Run command</span><input class="ide-cmd" spellcheck="false" aria-label="Run command"><span class="ide-tst"></span><button class="ide-fold" title="Open this project's folder on your computer">Folder</button></div>
       <pre class="ide-tout"></pre><input class="ide-tin" spellcheck="false" placeholder="A command, Enter runs it in the project folder: pip install requests · git status · javac Main.java" aria-label="Terminal"></div>
      <div class="ide-con ide-pane"><pre class="ide-cout"></pre><input class="ide-cin" spellcheck="false" placeholder=">>> Python here, Enter runs it. Your last run's variables are still here" aria-label="Python console"></div>
      <div class="ide-find ide-pane"><div class="ide-fr"><input class="ide-fq" spellcheck="false" placeholder="Search all files" aria-label="Search all files"><input class="ide-fw" spellcheck="false" placeholder="Replace with" aria-label="Replace with"><button class="ide-frep">Replace all</button></div><ul class="ide-fres"></ul></div>
      <div class="ide-ai ide-pane"><div class="ide-ans"></div>
        <div class="ide-aiin"><textarea class="ide-ait" rows="1" placeholder="Ask about your code… (select lines first to ask about just those)" aria-label="Ask AI"></textarea><button class="ide-aisend">Ask</button></div></div></div></div>
    <div class="ide-live"><i class="ide-lgrip" title="Drag to resize"></i><div class="ide-lh"><span>Display</span><span class="ide-lst"></span><button class="ide-lre" title="Show it again" aria-label="Show it again">↻</button></div>
     <div class="ide-lbody"><iframe class="ide-lv" sandbox="allow-scripts allow-modals" title="Display"></iframe><pre class="ide-lout"></pre><div class="ide-lpc"><p></p><button class="btn ide-lgo">▶ Run on this PC</button></div></div></div></div>
   <div class="ide-status"><span class="ide-pos">Ln 1, Col 1</span><span class="ide-lg"></span><span class="ide-hint">Ctrl+P files · Ctrl+Shift+P all commands</span><span class="ide-saved"></span></div>
   <div class="ide-start"><div><h3>Start a project</h3><p>Pick a starting point. You can add, upload, link and delete files after.</p><div class="ide-tpl"></div></div></div>`;
  const q = s => el.querySelector(s);
  let mon = null, ed = null, ta = null, b = null, ro = false, file = null, tabs = [], quiet = false, savedT = null, marked = [], lastId = null, hi = 0, provided = false;
  const models = new Map(), closed = new Set(), chat = [], hist = [];
  const P = (() => { const d = {wrap: false, mini: true, size: 14, ph: 0}; try { return {...d, ...JSON.parse(localStorage.bbIde || '{}')}; } catch (_) { return d; } })();
  const keepP = () => { try { localStorage.bbIde = JSON.stringify(P); } catch (_) {} };
  const theme = () => ((document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')) === 'dark' ? 'bb-dark' : 'bb-light');
  const saved = t => { q('.ide-saved').textContent = t; };
  const isProj = () => b && b.kind === 'proj';
  const cur = () => isProj() ? file : mainFile(b);
  const curFile = () => filesOf(b).find(f => f.n === cur()) || {};
  LANGS.forEach(l => { const o = h('option', '', l === 'auto' ? 'Auto' : l); o.value = l; q('.ide-lang').appendChild(o); });
  Object.keys(TEMPLATES).forEach(k => { const t = q('.ide-tpl').appendChild(h('button', '', k)); t.onclick = () => {
    if (!b || ro) return; const files = TEMPLATES[k].map(f => ({...f}));
    typed(b, {files, main: mainOf({files}), title: b.data.title || (k === 'Empty' ? '' : k + ' project')}); q('.ide-title').value = b.data.title || '';
    q('.ide-start').classList.remove('on'); tabs = []; show(mainOf(b.data)); ed && ed.focus(); }; });

  // ------------------------------------------------ the editor
  async function ensure() {
    if (ed || ta) return;
    try {
      mon = await loadMonaco();
      mon.editor.defineTheme('bb-light', {base: 'vs', inherit: true, rules: [{token: 'number', foreground: 'd71921'}, {token: 'comment', foreground: '8a8a8a', fontStyle: 'italic'},
        {token: 'keyword', foreground: '0b0b0b', fontStyle: 'bold'}, {token: 'string', foreground: '8a3d3d'}],
        colors: {'editor.background': '#ffffff', 'editorLineNumber.foreground': '#b5b5b5', 'editorCursor.foreground': '#d71921', 'editor.selectionBackground': '#ffd6d6', 'editor.lineHighlightBackground': '#f6f6f6'}});
      mon.editor.defineTheme('bb-dark', {base: 'vs-dark', inherit: true, rules: [{token: 'number', foreground: 'ff5a5f'}, {token: 'comment', foreground: '777777', fontStyle: 'italic'},
        {token: 'keyword', foreground: 'ffffff', fontStyle: 'bold'}, {token: 'string', foreground: 'd9a7a7'}],
        colors: {'editor.background': '#0c0c0c', 'editorLineNumber.foreground': '#4a4a4a', 'editorCursor.foreground': '#ff5a5f', 'editor.selectionBackground': '#5a1a1d', 'editor.lineHighlightBackground': '#161616'}});
      ed = mon.editor.create(q('.ide-ed'), {theme: theme(), automaticLayout: true, fontFamily: '"Cascadia Code", Consolas, "Space Mono", monospace', fontSize: P.size,
        minimap: {enabled: P.mini}, wordWrap: P.wrap ? 'on' : 'off', scrollBeyondLastLine: false, tabSize: 4, bracketPairColorization: {enabled: true}, smoothScrolling: true, mouseWheelZoom: true,
        cursorSmoothCaretAnimation: 'on', stickyScroll: {enabled: true}, fixedOverflowWidgets: true, padding: {top: 10}, guides: {bracketPairs: true}, linkedEditing: true, renderWhitespace: 'selection'});
      ed.onDidChangeCursorPosition(e => { q('.ide-pos').textContent = `Ln ${e.position.lineNumber}, Col ${e.position.column}`; });
      ed.addCommand(mon.KeyMod.CtrlCmd | mon.KeyCode.Enter, () => go());
      ed.addCommand(mon.KeyCode.F5, () => go());
      ed.addCommand(mon.KeyMod.CtrlCmd | mon.KeyCode.KeyS, () => { if (b && !ro) { Sync.save(b, true); saved('Saved'); } });
      ed.addCommand(mon.KeyCode.Escape, () => close(), '!suggestWidgetVisible && !findWidgetVisible && !renameInputVisible && !parameterHintsVisible && !editorHasMultipleSelections');
      new MutationObserver(() => mon.editor.setTheme(theme())).observe(document.documentElement, {attributes: true, attributeFilter: ['data-theme']});
      suggestions();
    } catch (_) {   // no internet: a plain box still works
      ta = h('textarea', 'ide-ta'); ta.spellcheck = false; q('.ide-ed').appendChild(ta);
      ta.oninput = () => edited(ta.value);
      ta.onkeydown = ev => { if (ev.key === 'Tab') { ev.preventDefault(); ta.setRangeText('    ', ta.selectionStart, ta.selectionEnd, 'end'); edited(ta.value); }
        if (ev.ctrlKey && ev.key === 'Enter') go(); };
      saved('Simple editor (no internet)');
    }
  }
  // Python snippets, and names from the project's other files so one file can use another's
  function suggestions() {
    if (provided) return; provided = true;
    const kinds = mon.languages.CompletionItemKind, snip = mon.languages.CompletionItemInsertTextRule.InsertAsSnippet;
    const range = (m, pos) => { const w = m.getWordUntilPosition(pos); return {startLineNumber: pos.lineNumber, endLineNumber: pos.lineNumber, startColumn: w.startColumn, endColumn: w.endColumn}; };
    mon.languages.registerCompletionItemProvider('python', {provideCompletionItems: (m, pos) => ({suggestions: PY_SNIPS.map(([label, insertText, documentation]) =>
      ({label, kind: kinds.Snippet, insertText, insertTextRules: snip, documentation, detail: 'snippet', range: range(m, pos)}))})});
    ['python', 'javascript'].forEach(lang => mon.languages.registerCompletionItemProvider(lang, {provideCompletionItems: (m, pos) => {
      if (!isProj()) return {suggestions: []};
      const out = [], seen = new Set(), r = range(m, pos);
      filesOf(b).forEach(f => {
        if (f.n === file || monLang(f.n) !== lang) return;
        const re = lang === 'python' ? /^(?:def|class)\s+(\w+)|^([A-Za-z_]\w*)\s*=/gm : /(?:function\s+|class\s+|(?:const|let|var)\s+)([A-Za-z_$][\w$]*)/g;
        const mod = f.n.replace(/\.\w+$/, '').replace(/\//g, '.');
        let x; while ((x = re.exec(f.t || '')) && out.length < 300) {
          const name = x[1] || x[2]; if (!name || seen.has(name)) continue; seen.add(name);
          out.push({label: name, kind: kinds.Reference, insertText: name, detail: 'from ' + f.n, range: r,
            documentation: lang === 'python' ? `from ${mod} import ${name}` : `const {${name}} = require('./${f.n}')`});
        }
      });
      return {suggestions: out};
    }}));
  }
  function model(key, text, lang) {
    let m = models.get(key);
    if (!m || m.isDisposed()) { m = mon.editor.createModel(text, lang); const k = key; m.onDidChangeContent(() => { if (!quiet) edited(m.getValue(), k.split('\n')[1]); }); models.set(key, m); }
    else {
      if (m.getLanguageId() !== lang) mon.editor.setModelLanguage(m, lang);
      if (m.getValue() !== text) { quiet = true; m.pushEditOperations([], [{range: m.getFullModelRange(), text}], () => null); quiet = false; }
    }
    return m;
  }
  function edited(text, n = file) {
    if (!b || ro) return;
    if (isProj()) { if ((b.data.files || []).some(f => f.n === n && f.link)) return; typed(b, {files: (b.data.files || []).map(f => f.n === n ? {...f, t: text} : f)}); }
    else typed(b, {text});
    saved('Saving…'); clearTimeout(savedT); savedT = setTimeout(() => saved('Saved'), 900);
    live();
  }
  function show(n) {
    if (!b) return;
    file = n; if (isProj() && n && !tabs.includes(n)) tabs.push(n);
    const f = isProj() ? (filesOf(b).find(x => x.n === n) || {}) : {};
    const text = isProj() ? f.t || '' : b.data.text || '', lock = ro || !!f.link;
    const lang = isProj() ? monLang(n) : (MON[EXT_FOR[codeLang(b)]] || 'plaintext');
    if (ed) { const m = model(b.id + '\n' + (isProj() ? n : ''), text, lang); if (ed.getModel() !== m) ed.setModel(m); ed.updateOptions({readOnly: lock}); }
    else if (ta) { if (ta.value !== text) ta.value = text; ta.readOnly = lock; }
    q('.ide-lg').textContent = lang === 'plaintext' ? 'Plain text' : lang;
    linkNote(f); drawFiles(); drawTabs();
  }
  function linkNote(f) {
    const box = q('.ide-note'); box.replaceChildren(); box.hidden = !f.link; if (!f.link) return;
    const x = S.blocks.get(f.link);
    if (!x) { box.append(h('span', '', 'The board block this file was linked to has been deleted. ')); }
    else { box.append(h('span', '', `Linked to a ${x.kind === 'todo' ? 'checklist' : x.kind} on the board, so it updates when that changes. Edit it there. `));
      const go_ = box.appendChild(h('button', '', 'Go to block')); go_.onclick = () => goBlock(x.id); }
    if (!ro) { const c = box.appendChild(h('button', '', x ? 'Make it a normal file' : 'Remove'));
      c.onclick = () => { if (!x) { typed(b, {files: b.data.files.filter(g => g.n !== f.n)}); return show(mainOf(b.data)); }
        typed(b, {files: b.data.files.map(g => g.n === f.n ? {n: g.n, t: blockText(x)} : g)}); show(f.n); toast(f.n + ' is now a normal file you can edit'); }; }
  }
  function goBlock(id) { const x = S.blocks.get(id); if (!x) return; close(); fit([x]); select([id]); }

  // ------------------------------------------------ explorer: files and folders
  const sortKey = (path, dir) => path.split('/').map((p, i, a) => (dir || i < a.length - 1 ? '0' : '1') + p.toLowerCase()).join('/');
  function drawFiles() {
    const ul = q('.ide-files ul'); ul.replaceChildren(); if (!isProj()) return;
    const m = mainOf(b.data), all = b.data.files || [], dirs = new Set();
    all.forEach(f => { const p = f.n.split('/'); for (let i = 1; i < p.length; i++) dirs.add(p.slice(0, i).join('/')); });
    const rows = [...[...dirs].map(d => ({d, k: sortKey(d, true)})), ...filesOf(b).map(f => ({f, k: sortKey(f.n)}))].sort((x, y) => x.k.localeCompare(y.k));
    const hidden = path => path.split('/').slice(0, -1).some((_, i, a) => closed.has(path.split('/').slice(0, i + 1).join('/')));
    rows.forEach(({d, f}) => {
      const path = d || f.n; if (hidden(path)) return;
      const depth = path.split('/').length - 1, name = path.split('/').pop();
      const li = ul.appendChild(h('li', d ? 'dir' + (closed.has(d) ? ' shut' : '') : (f.n === file ? 'on ' : '') + (f.n === m ? 'main ' : '') + (f.link ? 'link' : '')));
      li.style.paddingLeft = 8 + depth * 14 + 'px';
      li.append(h('i', 'fi', d ? (closed.has(d) ? '▸' : '▾') : ext(f.n) || '·'), h('span', '', name));
      if (d) {
        li.title = d; li.onclick = () => { closed.has(d) ? closed.delete(d) : closed.add(d); drawFiles(); };
        if (!ro) {
          const a = li.appendChild(h('button', 'fr', '+')); a.title = 'New file in ' + d; a.onclick = ev => { ev.stopPropagation(); newFile(d + '/'); };
          const x = li.appendChild(h('button', 'fx', '×')); x.title = 'Delete folder ' + d; x.onclick = ev => { ev.stopPropagation(); delDir(d); };
          li.ondblclick = ev => { ev.stopPropagation(); renameDir(d); };
        }
        return;
      }
      li.title = f.link ? f.n + ' (linked from the board)' : f.n === m ? f.n + ' (Run starts here)' : f.n;
      if (!ro) {
        const r = li.appendChild(h('button', 'fr', '▶')); r.title = 'Run starts from this file';
        r.onclick = ev => { ev.stopPropagation(); typed(b, {main: f.n}); drawFiles(); };
        const x = li.appendChild(h('button', 'fx', '×')); x.title = (f.link ? 'Unlink ' : 'Delete ') + f.n;
        x.onclick = ev => { ev.stopPropagation(); if (!confirm(f.link ? `Unlink ${f.n}? The block stays on the board.` : `Delete ${f.n}?`)) return;
          typed(b, {files: b.data.files.filter(g => g.n !== f.n)}); tabs = tabs.filter(t => t !== f.n);
          if (!filesOf(b).length) { q('.ide-start').classList.add('on'); drawFiles(); drawTabs(); } else if (file === f.n) show(mainOf(b.data)); else { drawFiles(); drawTabs(); } };
        li.ondblclick = () => rename(f.n);
      }
      li.onclick = () => show(f.n);
    });
  }
  const taken = n => (b.data.files || []).some(g => g.n === n || g.n.startsWith(n + '/'));
  function rename(old) {
    const nn = cleanName(prompt('Rename (use a/b.py to put it in a folder)', old)); if (!nn || nn === old) return;
    if (taken(nn)) return toast('There is already something called ' + nn);
    const m = models.get(b.id + '\n' + old); m && m.dispose(); models.delete(b.id + '\n' + old);
    typed(b, {files: b.data.files.map(g => g.n === old ? {...g, n: nn} : g), main: b.data.main === old ? nn : b.data.main});
    tabs = tabs.map(t => t === old ? nn : t); show(file === old ? nn : file);
  }
  function renameDir(d) {
    const nd = cleanName(prompt('Rename folder', d)).replace(/\/+$/, ''); if (!nd || nd === d) return;
    if (taken(nd)) return toast('There is already something called ' + nd);
    const move = n => n.startsWith(d + '/') ? nd + n.slice(d.length) : n;
    models.forEach((m, k) => { if (k.startsWith(b.id + '\n' + d + '/')) { m.dispose(); models.delete(k); } });
    typed(b, {files: b.data.files.map(g => ({...g, n: move(g.n)})), main: b.data.main && move(b.data.main)});
    tabs = tabs.map(move); show(move(file || mainOf(b.data)));
  }
  function delDir(d) {
    const n = filesOf(b).filter(f => f.n.startsWith(d + '/')).length;
    if (!confirm(n ? `Delete the folder ${d} and the ${n} file${n > 1 ? 's' : ''} in it?` : `Delete the folder ${d}?`)) return;
    typed(b, {files: b.data.files.filter(g => !g.n.startsWith(d + '/'))}); tabs = tabs.filter(t => !t.startsWith(d + '/'));
    if (!filesOf(b).length) { q('.ide-start').classList.add('on'); drawFiles(); drawTabs(); } else show(file && !file.startsWith(d + '/') ? file : mainOf(b.data));
  }
  function addFiles(list) {
    const files = [...(b.data.files || [])];
    list.forEach(f => { const i = files.findIndex(g => g.n === f.n); if (i >= 0) files[i] = f; else files.push(f); });
    typed(b, {files, main: b.data.main || mainOf({files})}); q('.ide-start').classList.remove('on');
    models.forEach((m, k) => { if (list.some(f => k === b.id + '\n' + f.n)) { m.dispose(); models.delete(k); } });
    const first = list.find(f => !isKeep(f.n)); first ? show(first.n) : drawFiles();
  }
  function newFile(dir = '') {
    if (!isProj() || ro) return; const n = cleanName(prompt('New file name (use a/b.py to put it in a folder)', dir + 'helpers.py')); if (!n) return;
    if (filesOf(b).some(f => f.n === n)) return show(n); addFiles([{n, t: ''}]);
  }
  function newDir() {
    if (!isProj() || ro) return; const d = cleanName(prompt('New folder name', 'src')).replace(/\/+$/, ''); if (!d) return;
    if (taken(d)) return toast('There is already something called ' + d);
    typed(b, {files: [...(b.data.files || []), {n: d + '/.keep', t: ''}]}); closed.delete(d); q('.ide-start').classList.remove('on'); drawFiles();
  }
  // files from the computer: text only, skipping pictures, build folders and anything huge
  const SKIP_DIR = /^(node_modules|\.git|__pycache__|\.venv|venv|env|dist|build|\.idea|\.vscode|\.next|target|bin|obj)$/;
  async function readUploads(pairs) {
    if (!isProj() || ro) return;
    let size = (b.data.files || []).reduce((s, f) => s + (f.t || '').length, 0), skipped = 0; const out = [];
    for (const [name, f] of pairs.slice(0, 600)) {
      const n = cleanName(name);
      if (!n || n.split('/').some(p => SKIP_DIR.test(p)) || f.size > 400000 || size + f.size > 1500000 || /^(image|video|audio)\//.test(f.type) || out.length >= 200) { skipped++; continue; }
      const t = await f.text(); if (/\x00/.test(t)) { skipped++; continue; }
      size += f.size; out.push({n, t});
    }
    if (out.length) addFiles(out);
    toast(out.length ? `Added ${out.length} file${out.length > 1 ? 's' : ''}` + (skipped ? `, skipped ${skipped} (pictures, build folders or too big)` : '') : 'Nothing to add: only text and code files up to 400 KB');
  }
  async function dropped(dt) {
    const roots = [...(dt.items || [])].map(i => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
    if (!roots.length) return [...dt.files].map(f => [f.name, f]);
    const out = [];
    const walk = (en, path) => new Promise(res => {
      if (en.isFile) en.file(f => { out.push([path + f.name, f]); res(); }, () => res());
      else if (en.isDirectory && !SKIP_DIR.test(en.name) && out.length < 600) {
        const r = en.createReader(), kids = [];
        const more = () => r.readEntries(async es => { if (es.length) { kids.push(...es); return more(); } for (const k of kids) await walk(k, path + en.name + '/'); res(); }, () => res());
        more();
      } else res();
    });
    for (const e of roots) await walk(e, '');
    return out;
  }
  const up = h('input'); up.type = 'file'; up.multiple = true; up.hidden = true; el.appendChild(up);
  const upd = h('input'); upd.type = 'file'; upd.webkitdirectory = true; upd.hidden = true; el.appendChild(upd);
  up.onchange = () => { readUploads([...up.files].map(f => [f.name, f])); up.value = ''; };
  upd.onchange = () => { readUploads([...upd.files].map(f => [f.webkitRelativePath || f.name, f])); upd.value = ''; };
  function upload() {
    if (!isProj() || ro) return;
    pick('Add from your computer', [{label: 'Files…', sub: 'one or more files', run: () => up.click()},
      {label: 'A whole folder…', sub: 'keeps its folders, skips node_modules, .git and venv', run: () => upd.click()}]);
  }
  // another block on the board becomes a file here, kept up to date
  function linkPick() {
    if (!isProj() || ro) return;
    const items = [...S.blocks.values()].filter(x => LINKABLE[x.kind]).map(x => {
      const has = (b.data.files || []).find(f => f.link === x.id), first = (blockText(x).split('\n').find(l => l.trim()) || '').slice(0, 60);
      return {label: has ? has.n : linkName(x), sub: (has ? 'already linked · ' : '') + ({todo: 'checklist', term: 'terminal'}[x.kind] || x.kind) + (first ? ' · ' + first : ''),
        run: () => { if (has) return show(has.n);
          let n = linkName(x); const [base, e] = [n.replace(/\.\w+$/, ''), ext(n)]; for (let i = 2; filesOf(b).some(f => f.n === n); i++) n = `${base}_${i}.${e}`;
          addFiles([{n, link: x.id}]); toast(`Linked ${n}. It updates when the block changes`); }};
    });
    if (!items.length) return toast('Nothing on the board to link yet. Add a note, code or checklist first');
    pick('Link a board block into this project as a file', items);
  }

  // ------------------------------------------------ tabs and the bottom panel
  function drawTabs() {
    const box = q('.ide-tabs'); box.replaceChildren(); if (!b) return;
    const have = filesOf(b).map(f => f.n), names = isProj() ? tabs.filter(t => have.includes(t)) : [have[0]];
    names.forEach(n => {
      const t = box.appendChild(h('button', 'tab' + (!isProj() || n === file ? ' on' : '')));
      t.append(h('i', 'fi', ext(n)), h('span', '', n.split('/').pop())); t.title = n;
      if (isProj()) { t.onclick = () => show(n);
        if (names.length > 1) { const x = t.appendChild(h('b', '', '×')); x.title = 'Close';
          x.onclick = ev => { ev.stopPropagation(); tabs = tabs.filter(z => z !== n); if (file === n) show(tabs[tabs.length - 1]); else drawTabs(); }; } }
    });
  }
  // a sandboxed frame made while the editor was hidden never paints in Chromium, so each preview gets a fresh one
  function view(html) { const old = q('.ide-view'), v = old.cloneNode(false); old.replaceWith(v); if (html) v.srcdoc = html; }
  function panel(p) { el.querySelectorAll('.ide-ph [data-p]').forEach(x => x.classList.toggle('on', x.dataset.p === p)); el.dataset.panel = p; }
  const outEl = q('.ide-out');
  // Python error lines like File "helpers.py", line 4 become links to that line
  function linked(sp, s) {
    const names = b ? filesOf(b).map(f => f.n) : [], re = /File "([^"]+)", line (\d+)/g; let m, last = 0;
    while ((m = re.exec(s))) {
      if (!names.includes(m[1])) continue;
      sp.append(s.slice(last, m.index)); const a = sp.appendChild(h('a', 'ide-loc', m[0])); const [, f, l] = m; a.onclick = () => jump(f, +l); last = re.lastIndex;
    }
    sp.append(s.slice(last));
  }
  function write(s, err) { if (s === null) { outEl.textContent = ''; return; } const sp = outEl.appendChild(h('span', err ? 'e' : '')); linked(sp, s); outEl.scrollTop = outEl.scrollHeight; return sp; }
  // ------------------------------------------------ the display: what you're making, beside the code
  // A web page redraws as you type. Python and JavaScript run again once you stop typing and show what they print
  // (charts too). A project that runs on this computer opens its own window there, so the display offers to run it.
  let liveOn = !!store.get('bbDisplay', false), liveT = null, liveBusy = false, liveAgain = false;
  if (P.lw) q('.ide-live').style.setProperty('--lw', P.lw + 'px');
  function livePage(blk) {
    const m = mainFile(blk); if (RUN_EXT[ext(m)] === 'html') return m;
    const fs = filesOf(blk), f = fs.find(x => /(^|\/)index\.html?$/i.test(x.n)) || fs.find(x => /\.html?$/i.test(x.n));
    return f ? f.n : null;
  }
  function liveMode(blk) {
    const k = RUN_EXT[ext(mainFile(blk))];
    if (k === 'html') return 'web';
    if (whereOf(blk) === 'pc') return 'pc';
    if (k) return 'out';
    return livePage(blk) ? 'web' : 'none';
  }
  function live(now) {
    clearTimeout(liveT); if (!liveOn || !b) return;
    const blk = b, mode = liveMode(blk); el.dataset.lmode = mode;
    if (mode === 'pc' || mode === 'none') {
      q('.ide-lst').textContent = mode === 'pc' ? 'on this PC' : '';
      q('.ide-lpc p').textContent = mode === 'pc'
        ? 'This runs on your computer, so an app opens in its own window and anything it prints shows in the Terminal. Run it to see it.'
        : `${(ext(mainFile(blk)) || 'This').toUpperCase()} code needs your computer to run. Pick "On this PC" next to Run, or add an index.html to see a page here.`;
      return;
    }
    liveT = setTimeout(() => mode === 'web' ? liveWeb(blk) : liveRun(blk), now ? 0 : mode === 'web' ? 300 : 900);
  }
  function liveWeb(blk) {
    const pg = livePage(blk); q('.ide-lst').textContent = pg;
    // a fresh frame each time (see view()); it must have its page before it goes in, or Chromium leaves it white
    const old = q('.ide-lv'), v = old.cloneNode(false); v.srcdoc = webPage(filesOf(blk), pg); old.replaceWith(v);
  }
  async function liveRun(blk) {
    if (liveBusy || running.size) { liveAgain = true; return; }
    liveBusy = true; liveAgain = false;
    const box = q('.ide-lout'), main = mainFile(blk); q('.ide-lst').textContent = main + ' \u00b7 running\u2026';
    const guard = setTimeout(() => { liveBusy = false; }, 100000);   // never stay stuck if the worker was taken over
    let fresh = true;
    const sink = (t, err) => { if (fresh) { box.replaceChildren(); fresh = false; } if (t === null) return box.replaceChildren(); box.appendChild(h('span', err ? 'e' : '', t)); };
    const ok = await runIn(RUN_EXT[ext(main)], filesOf(blk), main, q('.ide-in').value.split(/\s*\|\s*/).join('\n'), sink, {img: src => picture(box, src)});
    clearTimeout(guard);
    if (fresh) box.replaceChildren(h('span', 'n', '(it ran, and printed nothing)'));
    q('.ide-lst').textContent = main + (ok ? '' : ' \u00b7 stopped with an error');
    liveBusy = false;
    if (liveAgain && b && b.id === blk.id) live(true);
  }
  function setLive(on) {
    liveOn = on; store.set('bbDisplay', on); el.classList.toggle('live', on); q('.ide-disp').classList.toggle('on', on);
    if (on) live(true); else { clearTimeout(liveT); const old = q('.ide-lv'); old.replaceWith(old.cloneNode(false)); }
  }
  function picture(box, src) { const i = box.appendChild(h('img', 'ide-img')); i.src = src; i.alt = 'Chart'; box.scrollTop = box.scrollHeight; }
  function jump(n, line, col = 1, len = 0) {
    if (!b) return;
    if (isProj()) { if (!filesOf(b).some(f => f.n === n)) return; if (file !== n) show(n); }
    if (!ed) return; const m = ed.getModel(), l = Math.min(line, m.getLineCount());
    ed.revealLineInCenter(l); ed.setSelection(new mon.Selection(l, col, l, col + len)); ed.focus();
  }
  // a red squiggle on the line the error came from
  function markError(text) {
    if (!ed || !b) return;
    const names = filesOf(b).map(f => f.n), re = /File "([^"]+)", line (\d+)/g; let m, hit = null;
    while ((m = re.exec(text))) if (names.includes(m[1])) hit = [m[1], +m[2]];
    if (!hit) return;
    const f = filesOf(b).find(x => x.n === hit[0]), key = b.id + '\n' + (isProj() ? hit[0] : '');
    const mdl = models.get(key) || model(key, f.t || '', isProj() ? monLang(hit[0]) : ed.getModel().getLanguageId());
    const l = Math.min(hit[1], mdl.getLineCount());
    mon.editor.setModelMarkers(mdl, 'bb', [{severity: mon.MarkerSeverity.Error, message: text.trim().split('\n').pop(), startLineNumber: l, endLineNumber: l, startColumn: 1, endColumn: mdl.getLineMaxColumn(l)}]);
    marked.push(mdl);
  }
  function unmark() { if (mon) marked.forEach(m => !m.isDisposed() && mon.editor.setModelMarkers(m, 'bb', [])); marked = []; }
  async function go() {
    if (!b) return; const blk = b;
    if (running.has(blk.id)) { running.get(blk.id)(); return; }
    if (running.has('console')) return toast('The console is still busy');
    unmark();
    if (whereOf(blk) === 'pc') return pcRun(runCmd(blk), true);
    const main = mainFile(blk);
    if (RUN_EXT[ext(main)] === 'html') { panel('view'); outEl.textContent = ''; view(webPage(filesOf(blk), main)); sfx('send'); return; }
    while (liveBusy) await new Promise(r => setTimeout(r, 100));
    panel('out'); outEl.textContent = '';
    await run(blk, q('.ide-in').value.split(/\s*\|\s*/).join('\n'), (s, err) => { if (b && b.id === blk.id) write(s, err); }, src => { if (b && b.id === blk.id) picture(outEl, src); });
    const out = (S.blocks.get(blk.id) || blk).data.out;
    if (b && b.id === blk.id && out && !out.ok) markError(out.text);
  }
  addEventListener('message', ev => { if (!ev.data || !ev.data.bbrun || ev.source !== q('.ide-view').contentWindow) return; write(ev.data.s + '\n', ev.data.err); });
  function busy(id, on) { if (b && b.id === id) { q('.ide-run').textContent = on ? '■ Stop' : '▶ Run'; q('.ide-run').classList.toggle('stop', on); } }

  // ------------------------------------------------ console: Python, one line at a time
  const cout = q('.ide-cout');
  const cwrite = (s, cls) => { cout.appendChild(h('span', cls || '', s)); cout.scrollTop = cout.scrollHeight; };
  async function consoleRun() {
    const inp = q('.ide-cin'), src = inp.value; if (!src.trim()) return;
    hist.push(src); hi = hist.length; inp.value = ''; cwrite('>>> ' + src + '\n', 'cmd');
    if (running.size) return cwrite('Wait for the program to finish (or press Stop) first.\n', 'e');
    running.set('console', () => running.stopNow && running.stopNow());
    await runIn('python', [], '', '', (s, err, note) => { if (s != null) cwrite(s, err ? 'e' : note ? 'n' : ''); }, {repl: src, img: src => picture(cout, src)});
    running.delete('console');
  }
  function openConsole() {
    panel('con'); q('.ide-cin').focus();
    if (!cout.childNodes.length) cwrite('Python console. Run your program first and its variables are here to look at, e.g. type a name and press Enter.\n', 'n');
  }

  // ------------------------------------------------ terminal: a real command line in the project's folder
  const tout = q('.ide-tout'), procs = new Map(), synced = new Map(), thist = [];
  let thi = 0;
  const twrite = (s, cls) => { tout.appendChild(h('span', cls || '', s)); while (tout.childNodes.length > 3000) tout.firstChild.remove(); tout.scrollTop = tout.scrollHeight; };
  // the app sends what the program prints here, a bit at a time, and the exit code at the end
  window.bbTerm = (key, text, code) => {
    const p = procs.get(key); if (!p) return;
    if (text != null) { const t = noAnsi(text).replace(/\r\n?/g, '\n'); p.text = (p.text + t).slice(-40000); if (b && b.id === key) twrite(t); return; }
    procs.delete(key); running.delete(key); busy(key, false); el.classList.toggle('pcbusy', !!(b && procs.has(b.id)));
    if (b && b.id === key) twrite(`\n[finished${code ? ', exit code ' + code : ''}]\n`, code ? 'e' : 'n');
    p.done({code, text: p.text});
  };
  // On a group board the code comes from other people, so the first time a block runs a command it asks
  async function trusted(blk, cmd) {
    if (S.mode !== 'live') return true;
    const k = blk.id + '|' + cmd, ok = store.get('bbPcOk', []);
    if (ok.includes(k)) return true;
    if (!confirm(`This runs on your computer, with the same access as any program you start:\n\n${cmd}\n\nin the folder for "${projTitle(blk)}". The code comes from your group board, so only carry on if you trust what is in it.`)) return false;
    store.set('bbPcOk', [...ok, k].slice(-200)); return true;
  }
  async function pcRun(cmd, asRun) {
    if (!b) return; const blk = b;
    cmd = (cmd || '').trim();
    if (procs.has(blk.id)) { if (asRun) return; twrite(cmd + '\n', 'cmd'); api().term_input(blk.id, cmd); return; }   // the program is waiting for input
    openTerm();
    if (!cmd) { twrite(asRun ? 'Type the command that runs this project in "Run command" above (e.g. python main.py).\n' : '', 'n'); return; }
    if (/^(cls|clear)$/i.test(cmd)) { tout.replaceChildren(); return; }
    if (/^cd(\s|$)/i.test(cmd)) { twrite('Each command starts in the project folder. Put cd in the same line, e.g. cd src && python app.py\n', 'n'); return; }
    if (!api() || !api().term_start) { twrite('This works in the Blackboard Buddy app.\n', 'e'); return; }
    if (!(await trusted(blk, cmd))) return;
    const files = filesOf(blk).filter(f => !f.link || f.t != null).map(f => ({n: f.n, t: f.t || ''}));
    twrite((asRun ? '▶ ' : '> ') + cmd + '\n', 'cmd');
    const done = new Promise(ok => procs.set(blk.id, {text: '', done: ok}));
    let r; try { r = await api().term_start(blk.id, projTitle(blk), files, cmd); } catch (e) { r = {error: String(e)}; }
    if (!r || r.error) { procs.delete(blk.id); twrite(((r && r.error) || "Couldn't start it") + '\n', 'e'); if (asRun) finish(blk.id, {text: (r && r.error) || '', ok: false}); return; }
    synced.set(blk.id, new Map(files.map(f => [f.n, f.t])));
    running.set(blk.id, () => api().term_stop(blk.id)); busy(blk.id, true); el.classList.add('pcbusy');
    if (asRun) sfx('send');
    q('.ide-tin').focus();
    const end = await done;
    if (asRun) { finish(blk.id, {text: end.text.length > 20000 ? '…\n' + end.text.slice(-20000) : end.text, ok: !end.code}); sfx(end.code ? 'error' : 'done'); }
    await pullBack(blk.id, r.started);
  }
  // what the program wrote, or what a command made, comes back into the project (text files only)
  async function pullBack(id, since) {
    let got; try { got = await api().proj_pull(projTitle(S.blocks.get(id) || {data: {}}), since); } catch (_) { return; }
    const cur = S.blocks.get(id); if (!Array.isArray(got) || !got.length || !cur || cur.kind !== 'proj') return;
    const was = synced.get(id) || new Map(), list = filesOf(cur).map(f => ({...f})), added = [], changed = [];
    for (const f of got) {
      const mine = list.find(x => x.n === f.n);
      if (!mine) { list.push({n: f.n, t: f.t}); added.push(f.n); }
      else if (!mine.link && mine.t !== f.t && was.get(f.n) === mine.t) { mine.t = f.t; changed.push(f.n); }   // changed there, not here
    }
    if (!added.length && !changed.length) return;
    typed(cur, {files: list});
    if (b && b.id === id) { refresh(cur); drawFiles(); twrite(`Brought back from the folder: ${[...added.map(n => n + ' (new)'), ...changed].join(', ')}\n`, 'n'); }
  }
  function openTerm() {
    panel('term');
    if (b) { q('.ide-cmd').placeholder = autoCmd(b) || 'e.g. python main.py'; q('.ide-cmd').value = b.data.cmd || ''; }
    if (!tout.childNodes.length) twrite('Commands run in this project\'s own folder on your computer (Documents\\Blackboard Buddy projects), with whatever you have installed. Ctrl+C stops a program.\n', 'n');
    if (api() && api().pc_tools && !openTerm.told) { openTerm.told = true;
      api().pc_tools().then(t => { if (!t) return; const have = Object.entries(t).filter(([, v]) => v).map(([k]) => k);
        q('.ide-tst').textContent = have.length ? 'Has: ' + have.filter(k => !['py', 'javac', 'g++', 'rustc', 'npm'].includes(k)).join(', ') : 'Nothing to run code with is installed yet'; }).catch(() => {}); }
    q('.ide-tin').focus();
  }

  // ------------------------------------------------ search and replace in every file
  function find() {
    const qv = q('.ide-fq').value, res = q('.ide-fres'); res.replaceChildren(); if (!b || !qv) return;
    const needle = qv.toLowerCase(); let count = 0;
    for (const f of filesOf(b)) {
      (f.t || '').split('\n').forEach((ln, i) => {
        const at = ln.toLowerCase().indexOf(needle); if (at < 0 || count >= 300) return; count++;
        const li = res.appendChild(h('li'));
        li.append(h('b', '', `${f.n}:${i + 1}`), h('span', '', (at > 40 ? '…' : '') + ln.slice(Math.max(0, at - 40), at)), h('mark', '', ln.substr(at, qv.length)), h('span', '', ln.slice(at + qv.length, at + qv.length + 80)));
        li.onclick = () => jump(f.n, i + 1, at + 1, qv.length);
      });
    }
    res.prepend(h('li', 'none', count ? `${count >= 300 ? '300+' : count} match${count === 1 ? '' : 'es'}` : 'No matches'));
  }
  function replaceAll() {
    const qv = q('.ide-fq').value, to = q('.ide-fw').value; if (!b || ro || !qv) return;
    const re = new RegExp(qv.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi'), count = s => ((s || '').match(re) || []).length;
    const n = filesOf(b).filter(f => !f.link).reduce((s, f) => s + count(f.t), 0);
    if (!n) return toast('No matches to replace');
    if (!confirm(`Replace ${n} match${n === 1 ? '' : 'es'} of "${qv}" with "${to}"?`)) return;
    if (isProj()) typed(b, {files: b.data.files.map(f => f.link || isKeep(f.n) ? f : {...f, t: f.t.replace(re, () => to)})});
    else typed(b, {text: (b.data.text || '').replace(re, () => to)});
    show(file); find(); toast(`Replaced ${n}`);
  }
  function openFind() {
    panel('find'); const f = q('.ide-fq');
    const sel = ed && ed.getModel() ? ed.getModel().getValueInRange(ed.getSelection()) : '';
    if (sel && !sel.includes('\n')) f.value = sel;
    f.focus(); f.select(); find();
  }

  // ------------------------------------------------ Ask AI (uses the app's own Claude account)
  const QUICK = {
    error: () => { const o = (S.blocks.get(b.id) || b).data.out; if (!o || o.ok) { toast('The last run had no error. Run it first'); return null; }
      return ['Explain the error from the last run in simple words, and show how to fix it.', 'Explain the error']; },
    file: () => [`Explain what ${cur()} does, step by step, in simple words.`, 'Explain this file'],
    bugs: () => [`Look for bugs and problems in ${cur()}, and show the fixes.`, 'Find bugs'],
    next: () => ['Suggest one useful next step for this project, with the code for it.', 'What next?'],
  };
  const ans = q('.ide-ans');
  function resetChat() {
    chat.length = 0; ans.replaceChildren();
    const row = ans.appendChild(h('div', 'ide-aiq'));
    Object.entries({error: 'Explain the error', file: 'Explain this file', bugs: 'Find bugs', next: 'What next?'})
      .forEach(([k, label]) => { const x = row.appendChild(h('button', '', label)); x.dataset.ask = k; x.onclick = () => quickAsk(k); });
    ans.appendChild(h('p', 'ide-aitip', 'Answers use your Claude account from Settings and see this project\'s files and last output.'));
  }
  function quickAsk(k) { if (!b) return; const r = QUICK[k](); if (r) ask(r[0], r[1]); }
  function rich(md) {   // a small, safe markdown: paragraphs, `code`, **bold**, and code blocks with Copy / Insert
    const box = h('div', 'a');
    md.split('```').forEach((part, i) => {
      if (i % 2) {
        const nl = part.indexOf('\n'), code = (nl >= 0 ? part.slice(nl + 1) : part).replace(/\n$/, '');
        const wrap = box.appendChild(h('div', 'ide-code')); wrap.appendChild(h('pre', '', code));
        const bar = wrap.appendChild(h('div')), c = bar.appendChild(h('button', '', 'Copy'));
        c.onclick = () => { navigator.clipboard.writeText(code); toast('Copied'); };
        if (!ro) { const ins = bar.appendChild(h('button', '', 'Insert at cursor')); ins.onclick = () => insert(code); }
        return;
      }
      part.split(/\n{2,}/).forEach(par => {
        par = par.replace(/^#{1,4}\s*/gm, '').trim(); if (!par) return; const p = box.appendChild(h('p'));
        par.split(/(`[^`\n]+`|\*\*[^*\n]+\*\*)/).forEach(bit => {
          if (/^`[^`]+`$/.test(bit)) p.appendChild(h('code', '', bit.slice(1, -1)));
          else if (/^\*\*.+\*\*$/.test(bit)) p.appendChild(h('b', '', bit.slice(2, -2)));
          else p.append(bit);
        });
      });
    });
    return box;
  }
  function insert(code) {
    if (!ed || !b || ro) return; if (curFile().link) return toast('This file is linked from the board. Edit it there');
    ed.executeEdits('ai', [{range: ed.getSelection(), text: code, forceMoveMarkers: true}]); ed.focus(); toast('Inserted. Ctrl+Z undoes it');
  }
  async function ask(question, label) {
    if (!b) return; panel('ai');
    const say = (cls, t) => { const d = ans.appendChild(h('div', cls, t)); ans.scrollTop = ans.scrollHeight; return d; };
    say('q', label || question);
    if (!api() || !api().code_ask) return say('e', 'Ask AI works in the Blackboard Buddy app, using your Claude account from Settings.');
    const sel = ed && ed.getModel() ? ed.getModel().getValueInRange(ed.getSelection()) : '';
    if (sel.trim()) question += `\n\nThe lines I selected in ${cur()}:\n\`\`\`\n${sel}\n\`\`\``;
    chat.push({role: 'user', content: question});
    const wait = say('a busy', 'Thinking…'), btn = q('.ide-aisend'); btn.disabled = true;
    let r;
    try { r = await api().code_ask(chat.slice(-9), filesOf(b).map(f => ({n: f.n, t: (f.t || '').slice(0, 20000)})), cur() || '', ((S.blocks.get(b.id) || b).data.out || {}).text || ''); }
    catch (e) { r = {error: String(e)}; }
    btn.disabled = false;
    if (!r || r.error || !r.md) { chat.pop(); wait.className = 'e'; wait.textContent = (r && r.error) || 'No answer came back. Try again.'; return; }
    chat.push({role: 'assistant', content: r.md}); wait.replaceWith(rich(r.md)); ans.scrollTop = ans.scrollHeight;
  }
  function sendAsk() { const t = q('.ide-ait'), v = t.value.trim(); if (!v) return; t.value = ''; t.style.height = ''; ask(v); }
  function openAI() { panel('ai'); q('.ide-ait').focus(); }

  // ------------------------------------------------ the picker: files (Ctrl+P), commands (Ctrl+Shift+P), uploads, links
  let pickItems = [], pickAt = 0, pickList = [];
  const fuzzy = (v, s) => { let i = 0; for (const c of s) if (c === v[i]) i++; return i === v.length; };
  function pick(ph, items) {
    const box = q('.ide-pick'), inp = box.querySelector('input');
    box.hidden = false; inp.placeholder = ph; inp.value = ''; pickItems = items; pickAt = 0; drawPick(); inp.focus();
  }
  function drawPick() {
    const box = q('.ide-pick'), ul = box.querySelector('ul'), v = box.querySelector('input').value.toLowerCase().replace(/\s+/g, '');
    const rank = i => { const l = i.label.toLowerCase().replace(/\s+/g, ''), all = l + (i.sub || '').toLowerCase().replace(/\s+/g, '');
      return !v ? 0 : l.includes(v) ? 1 : all.includes(v) ? 2 : fuzzy(v, l) ? 3 : fuzzy(v, all) ? 4 : 9; };
    pickList = pickItems.map((i, n) => [rank(i), n, i]).filter(x => x[0] < 9).sort((x, y) => x[0] - y[0] || x[1] - y[1]).map(x => x[2]).slice(0, 80);
    pickAt = Math.max(0, Math.min(pickAt, pickList.length - 1)); ul.replaceChildren();
    pickList.forEach((it, i) => { const li = ul.appendChild(h('li', i === pickAt ? 'on' : '')); li.append(h('span', '', it.label)); if (it.sub) li.append(h('small', '', it.sub));
      li.onmousedown = ev => { ev.preventDefault(); unpick(); it.run(); }; });
    if (!pickList.length) ul.appendChild(h('li', 'none', 'Nothing matches'));
    const on = ul.querySelector('.on'); on && on.scrollIntoView({block: 'nearest'});
  }
  function unpick() { const box = q('.ide-pick'); if (box.hidden) return false; box.hidden = true; if (ed) ed.focus(); return true; }
  const pinp = q('.ide-pick input');
  pinp.oninput = () => { pickAt = 0; drawPick(); };
  pinp.onblur = () => setTimeout(() => { q('.ide-pick').hidden = true; }, 120);
  pinp.onkeydown = ev => {
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') { ev.preventDefault(); pickAt += ev.key === 'ArrowDown' ? 1 : -1; drawPick(); }
    else if (ev.key === 'Enter') { ev.preventDefault(); const it = pickList[pickAt]; unpick(); it && it.run(); }
  };
  function quick() { if (!b) return; pick('Go to a file', filesOf(b).map(f => ({label: f.n, sub: f.link ? 'linked from the board' : f.n === mainFile(b) ? 'Run starts here' : '', run: () => show(f.n)}))); }
  const act = id => { if (!ed) return toast('This needs the full editor (it loads from the internet)'); ed.focus(); const a = ed.getAction(id); a ? a.run() : ed.trigger('bb', id, null); };
  function setP(k, v) { P[k] = v; keepP(); if (ed) ed.updateOptions({wordWrap: P.wrap ? 'on' : 'off', minimap: {enabled: P.mini}, fontSize: P.size}); }
  const size = d => { setP('size', clamp(P.size + d, 10, 26)); toast('Text size ' + P.size); };
  async function exportTo(how) {
    if (!b) return; if (!api() || !api().ol_vscode) { toast('This works in the Blackboard Buddy app'); return; }
    toast(await api().ol_vscode(b.data.title || (isProj() ? 'Project' : 'Code'), filesOf(b).map(f => ({n: f.n, t: f.t})), how));
  }
  function commands() {
    const C = [
      ['Run', 'Ctrl+Enter · F5', go], ['Go to a file', 'Ctrl+P', quick], ['Search in all files', 'Ctrl+Shift+F', openFind],
      ['Python console', 'Ctrl+`', openConsole], ['Ask AI about the code', 'Ctrl+I', openAI],
      ['Run on this computer', '', () => b && pcRun(runCmd(b), true)], ['Display: show what you\u2019re making beside the code', '', () => setLive(!liveOn)], ['Terminal (this computer)', '', openTerm],
      ['Open the project folder', '', () => exportTo('folder')],
      ['Explain the last error (AI)', '', () => quickAsk('error')], ['Find bugs in this file (AI)', '', () => quickAsk('bugs')],
      ['Go to line…', 'Ctrl+G', () => act('editor.action.gotoLine')], ['Find in this file', 'Ctrl+F', () => act('actions.find')],
      ['Replace in this file', 'Ctrl+H', () => act('editor.action.startFindReplaceAction')], ['Comment / uncomment lines', 'Ctrl+/', () => act('editor.action.commentLine')],
      ['Change every copy of this word', 'Ctrl+F2', () => act('editor.action.changeAll')], ['Select the next copy of this word', 'Ctrl+D', () => act('editor.action.addSelectionToNextFindMatch')],
      ['Copy line down', 'Shift+Alt+↓', () => act('editor.action.copyLinesDownAction')], ['Move line up / down', 'Alt+↑ / Alt+↓', () => act('editor.action.moveLinesDownAction')],
      ['Format the file (JS, HTML, CSS, JSON)', 'Shift+Alt+F', () => act('editor.action.formatDocument')],
      ['Fold all', 'Ctrl+K Ctrl+0', () => act('editor.foldAll')], ['Unfold all', 'Ctrl+K Ctrl+J', () => act('editor.unfoldAll')],
      ['Word wrap on / off', 'Alt+Z', () => setP('wrap', !P.wrap)], ['Minimap on / off', '', () => setP('mini', !P.mini)],
      ['Bigger text', 'Ctrl+=', () => size(1)], ['Smaller text', 'Ctrl+-', () => size(-1)],
      ...(isProj() && !ro ? [['New file', '', () => newFile()], ['New folder', '', newDir], ['Upload files', '', () => up.click()], ['Upload a folder', '', () => upd.click()],
        ['Link a block from the board', '', linkPick], ['Rename this file', '', () => file && rename(file)]] : []),
      ['Save to computer (a folder in Documents)', '', () => exportTo('folder')], ['Open in VS Code', '', () => exportTo('code')], ['Back to the board', 'Esc', close],
    ];
    pick('Type a command', C.map(([label, sub, run]) => ({label, sub, run})));
  }
  // shortcuts that work anywhere in the editor; called before the board or Monaco see the key
  function keys(ev) {
    if (!b) return false;
    const c = ev.ctrlKey || ev.metaKey, k = (ev.key || '').toLowerCase();
    if (k === 'escape' && unpick()) return true;
    if (c && ev.shiftKey && k === 'p') { commands(); return true; }
    if (c && !ev.shiftKey && !ev.altKey && k === 'p') { quick(); return true; }
    if (c && ev.shiftKey && k === 'f') { openFind(); return true; }
    if (c && ev.code === 'Backquote') { openConsole(); return true; }
    if (c && !ev.shiftKey && k === 'i') { openAI(); return true; }
    if (ev.altKey && !c && k === 'z') { setP('wrap', !P.wrap); return true; }
    if (c && (k === '=' || k === '+')) { size(1); return true; }
    if (c && k === '-') { size(-1); return true; }
    if (c && k === 'enter' && !ev.target.closest('.monaco-editor, .ide-ait, .ide-cin, .ide-pick')) { go(); return true; }
    return false;
  }

  // ------------------------------------------------ opening, closing, other people
  async function open(blk, readOnly, who) {
    b = blk; ro = !!readOnly; tabs = []; file = null;
    if (lastId !== blk.id) { lastId = blk.id; resetChat(); closed.clear(); cout.replaceChildren(); tout.replaceChildren(); }
    el.classList.add('on'); el.classList.toggle('proj', isProj()); el.classList.toggle('ro', ro);
    q('.ide-who').textContent = ro ? `${(who && who.name) || 'Someone'} is editing. You're watching live` : '';
    q('.ide-title').value = b.data.title || ''; q('.ide-title').readOnly = ro;
    q('.ide-lang').hidden = b.kind !== 'code'; q('.ide-lang').value = b.data.lang || 'auto'; q('.ide-lang').disabled = ro;
    q('.ide-where').value = whereOf(b); q('.ide-where').disabled = ro; q('.ide-cmd').readOnly = ro;
    el.classList.toggle('pcbusy', procs.has(b.id));
    outEl.textContent = ''; outEl.classList.remove('bad'); if (b.data.out) write(b.data.out.text, !b.data.out.ok);
    panel('out'); busy(b.id, running.has(b.id)); view(''); q('.ide-pick').hidden = true; q('.ide-note').hidden = true;
    if (P.ph) q('.ide-panel').style.height = P.ph + 'px';
    q('.ide-start').classList.toggle('on', isProj() && !filesOf(b).length && !ro);
    saved(ro ? 'Read only' : 'Saved');
    el.classList.toggle('live', liveOn); q('.ide-disp').classList.toggle('on', liveOn); live(true);
    sfx('open');
    await ensure(); if (b !== blk) return;
    if (mon) mon.editor.setTheme(theme());
    if (isProj() && !filesOf(b).length) { drawFiles(); drawTabs(); return; }
    show(isProj() ? mainOf(b.data) : '');
    if (!ro) (ed || ta).focus();
  }
  function close() {
    if (!el.classList.contains('on')) return;
    const id = b && b.id, wasRo = ro; el.classList.remove('on'); b = null; q('.ide-pick').hidden = true;
    if (!wasRo && S.editing === id) stopEdit(); else R.focus({preventScroll: true});
    sfx('close');
  }
  // someone else's changes, while you watch; or a linked block changed
  function refresh(blk) {
    if (!b || !el.classList.contains('on')) return;
    if (blk.id !== b.id) { const lf = isProj() && (b.data.files || []).find(f => f.link === blk.id); if (lf && file === lf.n) show(file); return; }
    if (!ro && S.editing === b.id) return;
    b = blk; q('.ide-title').value = b.data.title || '';
    if (isProj() && !filesOf(b).length) return drawFiles();
    if (isProj() && !filesOf(b).some(f => f.n === file)) file = mainOf(b.data);
    show(isProj() ? file : '');
    if (b.data.out && !running.has(b.id)) { outEl.textContent = ''; write(b.data.out.text, !b.data.out.ok); }
    live();
  }
  function peers() {
    if (!b || !ro || !el.classList.contains('on')) return;
    const who = peerOn(b.id);
    if (!who) { q('.ide-who').replaceChildren(h('span', '', 'Nobody else is editing now. ')); const e = q('.ide-who').appendChild(h('button', '', 'Edit'));
      e.onclick = () => { const id = b.id; el.classList.remove('on'); b = null; startEdit(id); }; }
  }

  // ------------------------------------------------ buttons
  q('.ide-x').onclick = close;
  q('.ide-run').onclick = () => go();
  q('.ide-aib').onclick = openAI;
  q('.ide-more').onclick = commands;
  q('.ide-clear').onclick = () => { const p = el.dataset.panel; if (p === 'term') tout.replaceChildren(); else if (p === 'con') cout.replaceChildren(); else if (p === 'ai') resetChat(); else outEl.textContent = ''; };
  el.querySelectorAll('.ide-ph [data-p]').forEach(x => x.onclick = () => ({con: openConsole, find: openFind, ai: openAI, term: openTerm}[x.dataset.p] || (() => panel(x.dataset.p)))());
  q('.ide-title').oninput = () => { if (b && !ro) typed(b, {title: q('.ide-title').value}); };
  q('.ide-lang').onchange = () => { if (b && !ro) { typed(b, {lang: q('.ide-lang').value}); show(''); } };
  q('.ide-new').onclick = () => newFile();
  q('.ide-dir').onclick = newDir;
  q('.ide-up').onclick = upload;
  q('.ide-ln').onclick = linkPick;
  q('.ide-vs').onclick = () => exportTo('code');
  q('.ide-fold').onclick = () => exportTo('folder');
  q('.ide-where').onchange = () => { if (b && !ro) { typed(b, {where: q('.ide-where').value}); if (q('.ide-where').value === 'pc') openTerm(); live(true); } };
  q('.ide-disp').onclick = () => { setLive(!liveOn); sfx('tap', .7); };
  q('.ide-lre').onclick = () => live(true);
  q('.ide-lgo').onclick = () => go();
  q('.ide-lgrip').onpointerdown = ev => {
    ev.preventDefault(); const z = (window.uiZoom && uiZoom()) || 1, right = q('.ide-body').getBoundingClientRect().right;
    const box = q('.ide-live'), max = q('.ide-body').offsetWidth * .75;
    const mv = e => { P.lw = Math.round(clamp((right - e.clientX) / z, 240, max)); box.style.setProperty('--lw', P.lw + 'px'); };
    const done = () => { removeEventListener('pointermove', mv); removeEventListener('pointerup', done); keepP(); };
    addEventListener('pointermove', mv); addEventListener('pointerup', done);
  };
  q('.ide-cmd').oninput = () => { if (b && !ro) typed(b, {cmd: q('.ide-cmd').value}); };
  q('.ide-cmd').onkeydown = ev => { if (ev.key === 'Enter') { ev.preventDefault(); b && pcRun(runCmd(b), true); } };
  q('.ide-tin').onkeydown = ev => {
    const t = ev.target;
    if (ev.key === 'Enter') { ev.preventDefault(); const v = t.value; t.value = ''; if (v.trim()) { thist.push(v); thi = thist.length; } pcRun(v, false); }
    else if (ev.key === 'ArrowUp') { ev.preventDefault(); if (thi > 0) t.value = thist[--thi]; }
    else if (ev.key === 'ArrowDown') { ev.preventDefault(); thi = Math.min(thist.length, thi + 1); t.value = thist[thi] || ''; }
    else if (ev.ctrlKey && ev.key.toLowerCase() === 'c' && t.selectionStart === t.selectionEnd && b && procs.has(b.id)) {
      ev.preventDefault(); twrite('^C\n', 'e'); api().term_stop(b.id); }
  };
  q('.ide-in').onkeydown = ev => { if (ev.key === 'Enter') { ev.preventDefault(); go(); } };
  q('.ide-cin').onkeydown = ev => {
    if (ev.key === 'Enter') { ev.preventDefault(); consoleRun(); }
    else if (ev.key === 'ArrowUp') { ev.preventDefault(); if (hi > 0) ev.target.value = hist[--hi]; }
    else if (ev.key === 'ArrowDown') { ev.preventDefault(); hi = Math.min(hist.length, hi + 1); ev.target.value = hist[hi] || ''; }
  };
  let findT; q('.ide-fq').oninput = () => { clearTimeout(findT); findT = setTimeout(find, 150); };
  q('.ide-fq').onkeydown = ev => { if (ev.key === 'Enter') { ev.preventDefault(); const first = q('.ide-fres li:not(.none)'); first && first.click(); } };
  q('.ide-frep').onclick = replaceAll;
  q('.ide-aisend').onclick = sendAsk;
  q('.ide-ait').onkeydown = ev => { if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); sendAsk(); } };
  q('.ide-ait').oninput = ev => { const t = ev.target; t.style.height = ''; t.style.height = Math.min(t.scrollHeight, 120) + 'px'; };
  q('.ide-grip').onpointerdown = ev => {
    ev.preventDefault(); const p = q('.ide-panel'), z = (window.uiZoom && uiZoom()) || 1, y0 = ev.clientY, h0 = p.offsetHeight, max = q('.ide-main').offsetHeight - 140;
    const mv = e => { P.ph = Math.round(clamp(h0 - (e.clientY - y0) / z, 90, max)); p.style.height = P.ph + 'px'; };
    const done = () => { removeEventListener('pointermove', mv); removeEventListener('pointerup', done); keepP(); };
    addEventListener('pointermove', mv); addEventListener('pointerup', done);
  };
  el.addEventListener('dragover', ev => ev.preventDefault());
  el.addEventListener('drop', async ev => { ev.preventDefault(); if (!isProj() || ro) return; readUploads(await dropped(ev.dataTransfer)); });
  resetChat();
  return {open: (...a) => (IDE.ready = open(...a)), close, hide: () => { el.classList.remove('on'); b = null; }, isOn: id => !!b && b.id === id && el.classList.contains('on'),
    refresh, peers, busy, go, keys, ready: null, get id() { return b && b.id; }};
})();

// ---------------------------------------------------------------- mouse and touch on the board
let drag = null;
const toolBtns = [...R.querySelectorAll('.ol-tools [data-tool]')];
function setTool(t, form) { if (S.editing) stopEdit(); S.tool = t; S.shapeForm = form || 'box';
  R.dataset.tool = t; toolBtns.forEach(b => b.classList.toggle('on', b.dataset.tool === t)); }
INKS.forEach(c => { const b = h('button'); b.style.background = c === 'auto' ? 'var(--text)' : c; b.title = c === 'auto' ? 'Black/white' : 'Ink colour';
  b.onclick = () => { S.ink = c; R.querySelectorAll('.ol-inks button').forEach(x => x.classList.toggle('on', x === b)); }; $('.ol-inks').appendChild(b); });
$('.ol-inks button').classList.add('on');
toolBtns.forEach(b => b.onclick = () => { setTool(b.dataset.tool); sfx('tab');
  if (b.dataset.tool === 'image') { $('.ol-file').click(); setTool('select'); } });
$('.ol-file').onchange = async ev => { const f = ev.target.files[0]; ev.target.value = ''; if (f) addImage(f, centre()); };

board.addEventListener('pointerdown', ev => {
  // The right button drags the board about. Nothing starts here, though: the drag only begins
  // once the mouse has really moved, so a plain right-click still opens exactly the menu it
  // always did, on exactly the thing it was over.
  if (ev.button === 2) {
    if (!ev.target.closest('.ol-ide,input,textarea,[contenteditable="true"],[contenteditable="plaintext-only"]'))
      rightFrom = {x: ev.clientX, y: ev.clientY, moved: false, vx: S.view.x, vy: S.view.y};
    return;
  }
  const t = ev.target, blockEl = t.closest('.ol-b'), id = blockEl?.dataset.id || t.dataset?.id, p = toWorld(ev);
  if (linkFrom && ev.button === 0) { if (linkClick(blockEl?.dataset.id)) { ev.preventDefault(); return; } }
  closeMenu(); closeCtx();
  // a click inside the block being typed in is just typing, except on its corners, which resize it and carry on typing
  const inEdit = S.editing && blockEl && blockEl.dataset.id === S.editing;
  if (inEdit && !t.classList.contains('ol-rs')) return;
  if (S.editing && !inEdit) stopEdit();
  if (!inEdit) R.focus({preventScroll: true});
  if (ev.button === 1 || S.tool === 'hand' || spaceDown || (ev.button === 0 && ev.altKey)) return startPan(ev);
  if (S.tool === 'pen') { drag = {kind: 'pen', pts: [p]}; board.setPointerCapture(ev.pointerId); drawDraft(); return; }
  if (S.tool === 'arrow') {
    if (!blockEl) return toast('Start the arrow on a block');
    drag = {kind: 'arrow', from: blockEl.dataset.id, p}; board.setPointerCapture(ev.pointerId); return;
  }
  if (SIZES[S.tool] && S.tool !== 'image') {
    const k = S.tool;
    // A box is there to hold other things, so its size is the point. Drag one out as big as you
    // want it; a plain click still gives the usual size.
    if (k === 'shape') { drag = {kind: 'make', k, p, form: S.shapeForm || 'box'};
                         board.setPointerCapture(ev.pointerId); ev.preventDefault(); return; }
    setTool('select');
    const b = add(k, p.x, p.y, k === 'code' ? {lang: 'auto', text: ''} : k === 'proj' ? {title: '', files: []} : k === 'todo' ? {items: []} : k === 'sticky' ? {color: STICKY[Math.floor(Math.random() * STICKY.length)]} : {});
    setTimeout(() => startEdit(b.id), 10); ev.preventDefault(); return;
  }
  // controls inside a block
  if (blockEl && t.closest('button,select,input')) {
    const b = S.blocks.get(id);
    if (t.matches('input[type=checkbox]')) { ev.preventDefault(); const i = +t.dataset.i;
      change(id, x => { const items = [...x.data.items]; items[i] = {...items[i], done: !items[i].done}; x.data = {...x.data, items}; }, {soon: true});
      sfx(b.data.items[i].done ? 'done' : 'off'); return; }
    if (t.dataset.who != null) { ev.preventDefault(); const i = +t.dataset.who, ids = [null, ...S.members.keys()], cur = b.data.items[i].who || null;
      const nxt = ids[(ids.indexOf(cur) + 1) % ids.length];
      change(id, x => { const items = [...x.data.items]; items[i] = {...items[i], who: nxt}; x.data = {...x.data, items}; }, {soon: true}); return; }
    if (t.dataset.del != null) { ev.preventDefault(); change(id, x => { const items = [...x.data.items]; items.splice(+t.dataset.del, 1); x.data = {...x.data, items}; }, {soon: true}); return; }
    if (t.dataset.act === 'additem') { ev.preventDefault(); select([id]); addItem(b); return; }
    if (t.dataset.act === 'copy') { ev.preventDefault(); navigator.clipboard.writeText(noAnsi(b.data.text || '')).then(() => toast('Copied')); sfx('tap'); return; }
    if (t.dataset.act === 'run') { ev.preventDefault(); boardRun(b); return; }
    if (t.dataset.act === 'ide') { ev.preventDefault(); startEdit(id); return; }
    if (t.dataset.act === 'open') { ev.preventDefault(); const u = okUrl(b.data.url); if (u) (api()?.open_inside ? api().open_inside(u, b.data.title || u) : window.open(u)); return; }
    return;   // the language box
  }
  if (t.classList.contains('ol-rs')) {
    const b = S.blocks.get(id);
    if (locked(b)) { ev.preventDefault(); return; }
    const n = b.kind === 'shape' ? needs(b) : null;
    const c = t.dataset.c || 'se';
    drag = {kind: 'resize', id, p, c, before: snap(b), x: b.x, y: b.y, w: b.w, h: b.h, ratio: b.w / b.h,
            // never smaller than what is in it, whichever corner pulls
            floor: n ? {w: Math.max(80, Math.round(c.includes('w') ? b.x + b.w - n.x0 : n.x1 - b.x)),
                        h: Math.max(60, Math.round(c.includes('n') ? b.y + b.h - n.y0 : n.y1 - b.y))} : null};
    board.dataset.rz = c; board.setPointerCapture(ev.pointerId); ev.preventDefault(); return;
  }
  if (id && S.blocks.has(id)) {
    if (ev.shiftKey || ev.ctrlKey) { S.sel.has(id) ? S.sel.delete(id) : S.sel.add(id); select([...S.sel]); }
    else if (!S.sel.has(id)) select([id]);
    if (locked(S.blocks.get(id))) { ev.preventDefault(); return; }   // pick it up with the lock off
    toFront([...S.sel]);
    drag = {kind: 'move', p, moved: false, items: withInsides([...S.sel]).map(i => S.blocks.get(i)).filter(Boolean).map(b => ({b, x: b.x, y: b.y, before: snap(b)})), at: t};
    board.setPointerCapture(ev.pointerId); ev.preventDefault(); return;
  }
  // The middle of a box lets clicks through, so that what sits in it stays usable - but a
  // drag starting on bare board inside one is meant for the box itself.
  if (!ev.shiftKey && S.tool === 'select') {
    const holder = boxAt(p);
    if (holder) {
      if (!S.sel.has(holder.id)) select([holder.id]);
      toFront([...S.sel]);
      drag = {kind: 'move', p, moved: false, at: t,
              items: withInsides([...S.sel]).map(i => S.blocks.get(i)).filter(Boolean)
                .map(x => ({b: x, x: x.x, y: x.y, before: snap(x)}))};
      board.setPointerCapture(ev.pointerId); ev.preventDefault(); return;
    }
  }
  // Dragging bare board draws a box round the things you want; Shift adds to what is already
  // picked out. Moving about is the right button, the hand tool, space, the middle button or
  // Alt-drag.
  if (ev.shiftKey || S.tool === 'select') {
    if (!ev.shiftKey) select([]);
    drag = {kind: 'box', p, keep: ev.shiftKey ? [...S.sel] : []};
    board.setPointerCapture(ev.pointerId); ev.preventDefault(); return;
  }
  select([]); startPan(ev);
});
let rightFrom = null;        // where the right button went down, while it is still down
addEventListener('pointermove', ev => {
  if (!rightFrom || !(ev.buttons & 2)) return;
  if (!rightFrom.moved && Math.hypot(ev.clientX - rightFrom.x, ev.clientY - rightFrom.y) < 5) return;
  rightFrom.moved = true;
  board.classList.add('panning');
  const f = board.getBoundingClientRect().width / board.clientWidth || 1;
  S.view.x = rightFrom.vx + (ev.clientX - rightFrom.x) / f;
  S.view.y = rightFrom.vy + (ev.clientY - rightFrom.y) / f;
  applyView();
});
addEventListener('pointerup', ev => { if (ev.button === 2) board.classList.remove('panning'); });
function startPan(ev) { drag = {kind: 'pan', sx: ev.clientX, sy: ev.clientY, vx: S.view.x, vy: S.view.y}; board.classList.add('panning'); board.setPointerCapture(ev.pointerId); ev.preventDefault(); }
board.addEventListener('pointermove', ev => {
  const p = toWorld(ev); Sync.cursor(p);
  if (!drag) return;
  if (drag.kind === 'pan') { const f = board.getBoundingClientRect().width / board.clientWidth || 1;
    S.view.x = drag.vx + (ev.clientX - drag.sx) / f; S.view.y = drag.vy + (ev.clientY - drag.sy) / f; applyView(); return; }
  if (drag.kind === 'move') {
    const dx = p.x - drag.p.x, dy = p.y - drag.p.y;
    if (!drag.moved && Math.hypot(dx, dy) * S.view.k < 3) return;
    if (!drag.moved) { drag.moved = true; drag.items.forEach(i => node(i.b).classList.add('lift')); }
    drag.items.forEach(i => { i.b.x = Math.round(i.x + dx); i.b.y = Math.round(i.y + dy); place(i.b); Sync.live(i.b, true); });
    return;
  }
  if (drag.kind === 'resize') { const b = S.blocks.get(drag.id), c = drag.c, dx = p.x - drag.p.x, dy = p.y - drag.p.y;
    b.w = Math.max(80, Math.round(drag.w + (c.includes('w') ? -dx : dx)));
    b.h = Math.max(b.kind === 'text' ? 36 : 60, Math.round(drag.h + (c.includes('n') ? -dy : dy)));
    if (b.kind === 'shape' && drag.floor) {            // never smaller than what is in it
      b.w = Math.max(b.w, drag.floor.w); b.h = Math.max(b.h, drag.floor.h); }
    if (b.kind === 'image') b.h = Math.round(b.w / drag.ratio);
    // the opposite corner stays put
    b.x = c.includes('w') ? drag.x + drag.w - b.w : drag.x; b.y = c.includes('n') ? drag.y + drag.h - b.h : drag.y;
    if (b.kind === 'text') b.data = {...b.data, size: Math.round(clamp(b.h * .62, 14, 160))}, node(b).style.setProperty('--fs', b.data.size + 'px');
    place(b); Sync.live(b, true); return; }
  if (drag.kind === 'pen') { const last = drag.pts[drag.pts.length - 1]; if (Math.hypot(p.x - last.x, p.y - last.y) * S.view.k > 1.5) { drag.pts.push(p); drawDraft(); } return; }
  if (drag.kind === 'arrow') { drag.to = p; drawDraft(); return; }
  if (drag.kind === 'make') {
    const v = S.view, x0 = Math.min(drag.p.x, p.x), y0 = Math.min(drag.p.y, p.y);
    Object.assign(marquee.style, {display: 'block', left: x0 * v.k + v.x + 'px', top: y0 * v.k + v.y + 'px',
      width: Math.abs(p.x - drag.p.x) * v.k + 'px', height: Math.abs(p.y - drag.p.y) * v.k + 'px'});
    drag.at = p; return;
  }
  if (drag.kind === 'box') {
    const x0 = Math.min(drag.p.x, p.x), y0 = Math.min(drag.p.y, p.y), x1 = Math.max(drag.p.x, p.x), y1 = Math.max(drag.p.y, p.y), v = S.view;
    Object.assign(marquee.style, {display: 'block', left: x0 * v.k + v.x + 'px', top: y0 * v.k + v.y + 'px', width: (x1 - x0) * v.k + 'px', height: (y1 - y0) * v.k + 'px'});
    select([...drag.keep, ...[...S.blocks.values()].filter(b => b.kind !== 'arrow' && b.x < x1 && b.x + b.w > x0 && b.y < y1 && b.y + b.h > y0).map(b => b.id)]);
  }
});
board.addEventListener('pointerup', ev => {
  const d = drag; drag = null; board.classList.remove('panning'); marquee.style.display = 'none'; draft.setAttribute('d', '');
  if (!d) return;
  if (d.kind === 'move') {
    d.items.forEach(i => node(i.b).classList.remove('lift'));
    if (d.moved) {
      const steps = d.items.map(i => ({before: i.before, after: snap(i.b)}));
      d.items.forEach(i => put(i.b, {soon: true}));
      steps.push(...regrowBoxes(d.items.map(i => i.b.id)));     // a box takes in what was put in it
      record(steps);
      sfx('tap', .6);
    }
  }
  if (d.kind === 'resize') { delete board.dataset.rz; const b = S.blocks.get(d.id); record([{before: d.before, after: snap(b)}]); put(b, {soon: true}); }
  if (d.kind === 'make') {
    setTool('select');
    const q = d.at || d.p;
    const w = Math.abs(q.x - d.p.x), hh = Math.abs(q.y - d.p.y);
    const drawn = w > 40 && hh > 40;                    // anything smaller was meant as a click
    const b = drawn
      ? add(d.k, Math.min(d.p.x, q.x), Math.min(d.p.y, q.y), {form: d.form},
            {size: [Math.round(w), Math.round(hh)], centre: false})
      : add(d.k, d.p.x, d.p.y, {form: d.form});
    toBack([b.id]);
    setTimeout(() => startEdit(b.id), 10);
  }
  const penXs = d.kind === 'pen' ? d.pts.map(p => p.x) : [], penYs = d.kind === 'pen' ? d.pts.map(p => p.y) : [];
  const scribble = d.kind === 'pen' && d.pts.length > 2
    && Math.max(Math.max(...penXs) - Math.min(...penXs), Math.max(...penYs) - Math.min(...penYs)) * S.view.k > 6;
  if (scribble) {
    const xs = penXs, ys = penYs, pad = 6;
    const x0 = Math.min(...xs) - pad, y0 = Math.min(...ys) - pad, w = Math.max(...xs) - x0 + pad, hh = Math.max(...ys) - y0 + pad;
    const path = d.pts.map((p, i) => (i ? 'L' : 'M') + (p.x - x0).toFixed(1) + ',' + (p.y - y0).toFixed(1)).join(' ');
    add('ink', x0, y0, {d: path, vw: Math.round(w), vh: Math.round(hh), color: S.ink === 'auto' ? null : S.ink, width: 3}, {size: [Math.round(w), Math.round(hh)], centre: false});
  }
  if (d.kind === 'arrow') {
    const t = document.elementsFromPoint(ev.clientX, ev.clientY).find(e => e.classList?.contains('ol-b'));
    const to = t?.dataset.id;
    if (to && to !== d.from) { add('arrow', 0, 0, {from: d.from, to}, {size: [0, 0], centre: false}); setTool('select'); }
    else if (d.to) toast('Let go on another block to join them');
  }
});
// the board holds the pointer while something is dragged, so a double-click reports the board itself: look underneath instead
board.addEventListener('dblclick', ev => {
  if (S.tool !== 'select') return;
  const under = document.elementsFromPoint(ev.clientX, ev.clientY), top = under[0];
  if (!top || !board.contains(top) || under.some(e => e.matches?.('.ol-links path.hit'))) return;
  const blockEl = top.closest('.ol-b');
  if (blockEl) { if (S.editing !== blockEl.dataset.id && !top.closest('button,select,input')) startEdit(blockEl.dataset.id, top); return; }
  const p = toWorld(ev), b = add('note', p.x, p.y, {html: ''}); startEdit(b.id);
});
links.addEventListener('pointerdown', ev => { const id = ev.target.dataset.id; if (!id) return; ev.stopPropagation(); if (S.editing) stopEdit(); select([id]); });
board.addEventListener('wheel', ev => {
  if (ev.target.closest('.ol-c,.ol-todo,textarea') && S.editing && !ev.ctrlKey) return;   // scroll inside the block being typed in
  ev.preventDefault();
  const r = board.getBoundingClientRect(), f = r.width / board.clientWidth || 1;
  if (ev.shiftKey && !ev.ctrlKey) { S.view.x -= (ev.deltaY || ev.deltaX); applyView(); return; }
  zoomAt(S.view.k * Math.exp(-ev.deltaY * (ev.deltaMode ? .05 : .0016)), (ev.clientX - r.left) / f, (ev.clientY - r.top) / f);
}, {passive: false});
function drawDraft() {
  if (!drag) return; const v = S.view, sc = p => `${p.x * v.k + v.x},${p.y * v.k + v.y}`;
  if (drag.kind === 'pen') { draft.setAttribute('d', drag.pts.map((p, i) => (i ? 'L' : 'M') + sc(p)).join(' ')); draft.style.stroke = S.ink === 'auto' ? '' : S.ink; }
  if (drag.kind === 'arrow' && drag.to) { const a = S.blocks.get(drag.from); draft.setAttribute('d', `M${sc({x: a.x + a.w / 2, y: a.y + a.h / 2})} L${sc(drag.to)}`); draft.style.stroke = ''; }
}

// ---------------------------------------------------------------- keys
// listened for on the window, before the app's tab keys, so the board's own keys work wherever the focus is
const onBoard = ev => document.getElementById('online')?.classList.contains('on') && !gate.classList.contains('on')
  && !document.querySelector('.sheet.on, .viewer:not([hidden]), .map-pop.on, dialog[open]')
  && (R.contains(ev.target) || !ev.target.closest?.('input,textarea,select,[contenteditable="true"],[contenteditable="plaintext-only"]'));
addEventListener('keydown', ev => {
  if (!onBoard(ev)) return;
  if (ev.target.closest && ev.target.closest('.ol-chat')) return;   // the message box has its own keys
  if (ev.target.closest && ev.target.closest('.ol-ide')) {
    if (IDE.keys(ev)) { ev.preventDefault(); ev.stopPropagation(); return; }
    const inEd = ev.target.closest('.monaco-editor');   // Monaco handles its own keys (Ctrl+Enter, F5, Ctrl+S, Esc)
    if (ev.key === 'F5') { ev.preventDefault(); if (!inEd) IDE.go(); }   // F5 would reload the whole app
    else if (ev.key === 'Escape' && !inEd) IDE.close();
    return;
  }
  // while a menu is open it owns the keyboard, or the board would swallow the arrows
  if (ctx.classList.contains('on') && ev.target.closest('.ol-ctx')) {
    if (ctxKeys(ev)) ev.stopPropagation();
    return;
  }
  if (ev.key !== 'Escape') ev.stopPropagation();
  const typing = ev.target.closest('input,textarea,[contenteditable="true"],[contenteditable="plaintext-only"],select');
  if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 'f' && !ev.target.closest('.ol-ide')) {
    ev.preventDefault(); openFind(); return; }
  if (ev.key === 'F7') { ev.preventDefault(); Writing.open(S.sel.size ? [...S.sel] : null); return; }
  if (!typing && (ev.key === 'm' || ev.key === 'M') && !ev.ctrlKey && !ev.metaKey) {
    ev.preventDefault(); showMap(!mapOn); return; }
  if ((ev.ctrlKey || ev.metaKey) && !typing && S.sel.size) {
    const k = ev.key.toLowerCase();
    const dress_ = fn => { ev.preventDefault();
      [...S.sel].forEach(i => { const b = S.blocks.get(i); if (b) change(i, x => { x.data = {...x.data, ...fn(x)}; }); }); };
    if (k === 'l') { ev.preventDefault(); lookMenu([...S.sel], ctxWas); return; }
    if (k === 'b') return dress_(b => CAN_STYLE.includes(b.kind) ? {bold: !b.data.bold} : {});
    if (k === 'i') return dress_(b => CAN_STYLE.includes(b.kind) ? {italic: !b.data.italic} : {});
    if (k === 'u') return dress_(b => CAN_STYLE.includes(b.kind) ? {ul: !b.data.ul} : {});
    if (ev.key === '+' || ev.key === '=')
      return dress_(b => CAN_SIZE.includes(b.kind) ? sizePatch(b, Math.round(clamp(baseSize(b) * 1.2, 9, 160))) : {});
    if (ev.key === '-' || ev.key === '_')
      return dress_(b => CAN_SIZE.includes(b.kind) ? sizePatch(b, Math.round(clamp(baseSize(b) / 1.2, 9, 160))) : {});
  }
  if (ev.key === 'Escape') { if (linkFrom) { stopLink(); toast('Stopped'); return; }
    if (ctx.classList.contains('on')) { closeCtx(); return; }
    if (S.editing) stopEdit(); else { select([]); setTool('select'); } closeMenu(); return; }
  if (S.editing && S.blocks.get(S.editing)?.kind === 'todo' && ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); addItem(S.blocks.get(S.editing)); return; }
  if (S.editing && ev.ctrlKey && ev.key === 'Enter') { stopEdit(); return; }
  if (typing) return;
  const k = ev.key.toLowerCase(), ctrl = ev.ctrlKey || ev.metaKey;
  if (ctrl && k === 'z') { ev.preventDefault(); undo(ev.shiftKey); return; }
  if (ctrl && k === 'y') { ev.preventDefault(); undo(true); return; }
  if (ctrl && k === 'a') { ev.preventDefault(); select([...S.blocks.keys()]); return; }
  if (ctrl && k === 'k') { ev.preventDefault(); showBoards($('.ol-switch')); return; }
  if (ctrl && k === 'd') { ev.preventDefault(); duplicate([...S.sel], 24); return; }
  if (ctrl && k === 'c' && S.sel.size) { const list = [...S.sel].map(id => S.blocks.get(id)).filter(Boolean);
    navigator.clipboard.writeText('bb-online:' + JSON.stringify(list)); toast(`Copied ${list.length} block${list.length > 1 ? 's' : ''}`); return; }
  if (ctrl && (k === '=' || k === '+')) { ev.preventDefault(); zoomAt(S.view.k * 1.2); return; }
  if (ctrl && k === '-') { ev.preventDefault(); zoomAt(S.view.k / 1.2); return; }
  if (ctrl) return;
  if ((ev.key === 'Delete' || ev.key === 'Backspace') && S.sel.size) { ev.preventDefault(); remove([...S.sel]); return; }
  if (ev.key === 'Enter' && S.sel.size === 1) { ev.preventDefault(); startEdit([...S.sel][0]); return; }
  if (ev.key === 'F2' && S.sel.size === 1) { ev.preventDefault(); renameBlock(S.blocks.get([...S.sel][0])); return; }
  if (ev.key.startsWith('Arrow') && S.sel.size) { ev.preventDefault(); const n = ev.shiftKey ? 20 : 2, dx = {ArrowLeft: -n, ArrowRight: n}[ev.key] || 0, dy = {ArrowUp: -n, ArrowDown: n}[ev.key] || 0;
    const steps = []; S.sel.forEach(id => { const b = S.blocks.get(id); if (!b || b.kind === 'arrow') return; const before = snap(b); b.x += dx; b.y += dy; steps.push({before, after: snap(b)}); put(b); }); record(steps); return; }
  const tools = {v: 'select', h: 'hand', n: 'note', t: 'text', c: 'code', k: 'proj', r: 'term', l: 'todo', u: 'link', p: 'pen', a: 'arrow', o: 'shape'};
  if (tools[k]) { setTool(tools[k]); return; }
  if (k === 'i') { $('.ol-file').click(); return; }
  if (k === 'f') { fit(S.sel.size ? [...S.sel].map(id => S.blocks.get(id)).filter(Boolean) : null); return; }
  if (k === '0') { zoomAt(1); return; }
}, true);
function duplicate(ids, off) {
  const map = new Map(), out = [];
  ids.forEach(id => { const b = S.blocks.get(id); if (b && b.kind !== 'arrow') map.set(id, uuid()); });
  ids.forEach(id => { const b = S.blocks.get(id); if (!b) return;
    if (b.kind === 'arrow') { if (!map.has(b.data.from) || !map.has(b.data.to)) return; out.push({...snap(b), id: uuid(), data: {from: map.get(b.data.from), to: map.get(b.data.to)}}); }
    else out.push({...snap(b), id: map.get(id), x: b.x + off, y: b.y + off}); });
  out.forEach(b => put(b)); record(out.map(b => ({before: null, after: snap(b)}))); select(out.map(b => b.id)); sfx('tap');
}

// ---------------------------------------------------------------- paste and drop
document.addEventListener('paste', ev => {
  if (!onBoard(ev) || S.editing || ev.target.closest('input,textarea,[contenteditable="true"],[contenteditable="plaintext-only"]')) return;
  const cd = ev.clipboardData; if (!cd) return;
  const img = [...cd.items].find(i => i.type.startsWith('image/'));
  if (img) { ev.preventDefault(); addImage(img.getAsFile(), centre()); return; }
  const t = cd.getData('text/plain'); if (!t) return; ev.preventDefault();
  if (t.startsWith('bb-online:')) { try { const list = JSON.parse(t.slice(10)); list.forEach(b => S.blocks.has(b.id) || S.blocks.set(b.id, {...b, _ghost: true}));
    duplicate(list.map(b => b.id), 30); list.forEach(b => S.blocks.get(b.id)?._ghost && S.blocks.delete(b.id)); } catch (_) {} return; }
  addText(t, centre());
});
function addText(t, p, name) {
  const kind = sniff(t);
  if (kind === 'link') return add('link', p.x, p.y, {url: okUrl(t.trim()), title: ''});
  if (kind === 'term') { const lines = noAnsi(t).split('\n').length; return add('term', p.x, p.y, {text: noAnsi(t).replace(/\s+$/, ''), title: name || ''}, {size: [560, clamp(lines * 20 + 60, 120, 600)]}); }
  if (kind === 'code') { const lines = t.split('\n').length; return add('code', p.x, p.y, {text: t.replace(/\s+$/, ''), lang: guessLang(t), title: name || ''}, {size: [520, clamp(lines * 20 + 60, 120, 640)]}); }
  const e = document.createElement('div'); e.textContent = t;
  return add('note', p.x, p.y, {html: e.innerHTML.replace(/\n/g, '<br>')}, {size: [300, clamp(t.split('\n').length * 23 + 50, 120, 600)]});
}
function addImage(file, p) {
  if (!file || file.size > 25e6) return toast('That picture is too big');
  const url = URL.createObjectURL(file), img = new Image();
  img.onload = () => {
    const max = 1400, s = Math.min(1, max / Math.max(img.width, img.height));
    const c = document.createElement('canvas'); c.width = Math.round(img.width * s); c.height = Math.round(img.height * s);
    c.getContext('2d').drawImage(img, 0, 0, c.width, c.height); URL.revokeObjectURL(url);
    let q = .85, src = c.toDataURL('image/jpeg', q);
    while (src.length > 450000 && q > .4) { q -= .12; src = c.toDataURL('image/jpeg', q); }   // stays small enough to send live
    const w = Math.min(420, c.width), hh = Math.round(w * c.height / c.width);
    add('image', p.x, p.y, {src, alt: file.name || 'Picture'}, {size: [w, hh]});
  };
  img.onerror = () => toast("That file isn't a picture"); img.src = url;
}
board.addEventListener('dragover', ev => { ev.preventDefault(); });
board.addEventListener('drop', async ev => {
  ev.preventDefault(); const p = toWorld(ev); let off = 0;
  let dropped = [...ev.dataTransfer.files];
  const uri = ev.dataTransfer.getData('text/uri-list');
  const codeFiles = dropped.filter(f => langFromName(f.name) && f.size < 400000);
  if (codeFiles.length >= 2) {
    const files = (await Promise.all(codeFiles.slice(0, 60).map(async f => ({n: cleanName(f.name), t: await f.text()})))).filter(f => !/\x00/.test(f.t));
    add('proj', p.x, p.y, {title: 'Dropped files', files, main: mainOf({files})}); toast(`Made a project from ${files.length} files`);
    dropped = dropped.filter(f => !codeFiles.includes(f));
  }
  dropped.slice(0, 12).forEach(f => { const at = {x: p.x + off, y: p.y + off}; off += 30;
    if (f.type.startsWith('image/')) addImage(f, at);
    else if (f.size < 400000) f.text().then(t => { if (/\x00/.test(t)) return toast(`${f.name} isn't text`);
      const kind = /\.(log|out|txt)$/i.test(f.name) ? (sniff(t) === 'term' ? 'term' : 'note') : 'code';
      if (kind === 'note') addText(t, at, f.name); else add(kind, at.x, at.y, {text: t.replace(/\s+$/, ''), lang: kind === 'code' ? (langFromName(f.name) || guessLang(t)) : undefined, title: f.name},
        {size: [540, clamp(t.split('\n').length * 20 + 60, 140, 640)]}); });
    else toast(`${f.name} is too big to put on the board`); });
  if (uri && !ev.dataTransfer.files.length) add('link', p.x, p.y, {url: okUrl(uri.split('\n')[0]), title: ''});
});
const langFromName = n => ({mjs: 'javascript', md: 'plaintext', py: 'python', js: 'javascript', ts: 'typescript', java: 'java', c: 'c', h: 'c', cpp: 'cpp', cs: 'csharp', kt: 'kotlin', html: 'html', css: 'css', sql: 'sql', sh: 'bash', ps1: 'powershell', json: 'json'})[(n.split('.').pop() || '').toLowerCase()];

// ---------------------------------------------------------------- other people: cursors and who's typing where
const CURSOR = '<svg viewBox="0 0 24 24"><path d="M4 2l16 9-7 2-3 7z" fill="currentColor" stroke="#fff" stroke-width="1.5" stroke-linejoin="round"/></svg>';
function placeCursors() {
  const v = S.view, t = Date.now();
  S.peers.forEach((p, id) => {
    let el = cursLayer.querySelector(`[data-u="${CSS.escape(id)}"]`);
    if (!p.cur || t - p.seen > 15000) { el && el.remove(); return; }
    if (!el) { el = h('div', 'ol-cur'); el.dataset.u = id; el.innerHTML = CURSOR; el.appendChild(h('span')); cursLayer.appendChild(el); }
    el.style.setProperty('--pc', personColor(p.color)); el.querySelector('span').textContent = p.name || 'Someone';
    el.style.transform = `translate(${p.cur.x * v.k + v.x}px,${p.cur.y * v.k + v.y}px)`;
  });
  [...cursLayer.children].forEach(el => S.peers.has(el.dataset.u) || el.remove());
}
setInterval(placeCursors, 3000);
function peerOn(id) { for (const p of S.peers.values()) if (p.editing === id) return p; return null; }
function markPeers() {
  S.nodes.forEach((d, id) => { const p = peerOn(id); d.classList.toggle('peer', !!p);
    if (p) { d.dataset.who = (p.name || 'Someone') + ' is typing'; d.style.setProperty('--pc', personColor(p.color)); } });
  IDE.peers();
}
function drawPeople() {
  drawHere();
  const box = $('.ol-people'); box.replaceChildren(); if (S.mode === 'local') return;
  const online = new Set([...S.peers.values()].map(p => p.uid));
  [...S.members.values()].sort((a, b) => (online.has(b.id) - online.has(a.id))).slice(0, 8).forEach(m => {
    const i = h('i', online.has(m.id) || m.id === S.me?.id ? '' : 'away', initials(m.name)); i.title = m.name + (m.id === S.me?.id ? ' (you)' : online.has(m.id) ? ', here now' : '');
    i.style.setProperty('--pc', personColor(m.color)); box.appendChild(i); });
}

// ---------------------------------------------------------------- messages: to everyone on the group, or to one person
// They live on the server in `messages`, whose rules only hand a private one to the two people in it, so it never
// reaches anyone else's computer. A server set up before messages existed says so in the box. Who is typing goes
// over the board's live channel and is never stored.
let chatChan = null, lastSend = 0;
const htmlOf = t => String(t).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'})[c]).replace(/\n/g, '<br>');
function chatState() { return S.chat || (S.chat = {list: [], unread: 0, missing: false, readAt: 0, typing: new Map()}); }
const seeable = m => !!m && (!m.to_id || m.to_id === S.me?.id || m.from_id === S.me?.id);
const byTime = (a, b) => String(a.created_at).localeCompare(String(b.created_at)) || String(a.id).localeCompare(String(b.id));
const mentionsMe = m => !!S.me?.name && m.from_id !== S.me.id && new RegExp('(^|\\W)@' + S.me.name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '(\\W|$)', 'i').test(m.text || '');
function drawHere() {
  const live = S.mode === 'live' && !!S.group, btn = $('.ol-chatb');
  btn.hidden = !live;
  if (!live) { $('.ol-chat').hidden = true; return; }
  const C = chatState(), here = [...S.peers.values()].filter(p => p.uid && p.uid !== S.me?.id);
  const n = here.length + 1;
  $('.ol-here').textContent = n; btn.title = `${n} here now · messages`;
  $('.ol-unread').textContent = C.unread > 9 ? '9+' : C.unread || '';
  btn.classList.toggle('ment', !!C.mention);
  // who's here: click a name to write to them privately
  const chips = $('.ol-chn'); chips.replaceChildren(h('span', '', n === 1 ? 'only you here' : `${n} here:`));
  here.forEach(p => { const c = h('button', 'ol-chip', p.name || 'someone'); c.style.setProperty('--pc', personColor(p.color));
    c.title = `Write to ${p.name || 'them'} privately`; c.onclick = () => { $('.ol-chto').value = p.uid; drawTo(); $('.ol-cht').focus(); }; chips.appendChild(c); });
  const to = $('.ol-chto'), was = to.value, on = new Set(here.map(p => p.uid));
  to.replaceChildren(Object.assign(h('option', '', 'To everyone'), {value: ''}));
  [...S.members.values()].filter(m => m.id !== S.me?.id)
    .sort((a, b) => on.has(b.id) - on.has(a.id) || String(a.name).localeCompare(String(b.name)))
    .forEach(m => to.appendChild(Object.assign(h('option', '', `Only ${m.name || 'them'}${on.has(m.id) ? ' ● here' : ''} (private)`), {value: m.id})));
  to.value = [...to.options].some(o => o.value === was) ? was : '';
  drawTo();
}
function drawTo() { $('.ol-chin').classList.toggle('priv', !!$('.ol-chto').value); }
// the words of a message: links you can open and @names picked out, never HTML
function msgBody(text) {
  const out = document.createDocumentFragment(), names = [...S.members.values()].map(m => m.name).filter(Boolean).sort((a, b) => b.length - a.length);
  String(text).split(/(https?:\/\/[^\s<>"]+)/g).forEach((part, i) => {
    if (i % 2) {
      const u = okUrl(part.replace(/[.,;:!?)]+$/, '')), a = h('a', '', part);
      if (u) { a.href = u; a.onclick = ev => { ev.preventDefault(); ev.stopPropagation(); api()?.open_inside ? api().open_inside(u, u) : window.open(u, '_blank', 'noopener'); }; }
      out.appendChild(a); return;
    }
    let rest = part;
    while (rest) {
      const at = rest.indexOf('@'); if (at < 0) { out.appendChild(document.createTextNode(rest)); break; }
      const who = names.find(n => rest.slice(at + 1, at + 1 + n.length).toLowerCase() === n.toLowerCase());
      if (!who) { out.appendChild(document.createTextNode(rest.slice(0, at + 1))); rest = rest.slice(at + 1); continue; }
      out.appendChild(document.createTextNode(rest.slice(0, at)));
      out.appendChild(h('b', 'ol-ment' + (who.toLowerCase() === (S.me?.name || '').toLowerCase() ? ' me' : ''), '@' + who));
      rest = rest.slice(at + 1 + who.length);
    }
  });
  return out;
}
function dayName(d) {
  const t = new Date(); t.setHours(0, 0, 0, 0); const x = new Date(d); x.setHours(0, 0, 0, 0);
  const days = Math.round((t - x) / 864e5);
  return days === 0 ? 'Today' : days === 1 ? 'Yesterday' : x.toLocaleDateString([], {weekday: 'long', day: 'numeric', month: 'short'});
}
function drawChat() {
  const C = chatState(), box = $('.ol-chlist'), atEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.replaceChildren();
  if (C.missing) box.appendChild(h('p', 'ol-chnote', 'Messages need a one-off update to the group server: open "Online setup.sql" from the app folder, paste all of it into Supabase > SQL Editor and press Run.'));
  else if (!C.list.length) box.appendChild(h('p', 'ol-chnote', 'No messages yet. Write to everyone, or pick one person below for a private message. Type @ and a name to get someone’s attention.'));
  let prev = null, day = '', newShown = false;
  C.list.forEach(m => {
    const at = new Date(m.created_at || Date.now()), mine = m.from_id === S.me?.id, dn = dayName(at);
    if (dn !== day) { box.appendChild(h('div', 'ol-chday', dn)); day = dn; prev = null; }
    if (!newShown && C.readAt && !mine && Date.parse(m.created_at) > C.readAt) { box.appendChild(h('div', 'ol-chnew', 'New')); newShown = true; prev = null; }
    const cont = prev && prev.from_id === m.from_id && (prev.to_id || '') === (m.to_id || '') && at - new Date(prev.created_at) < 180000;
    const d = h('div', 'ol-msg' + (mine ? ' mine' : '') + (m.to_id ? ' priv' : '') + (m.failed ? ' failed' : '') + (cont ? ' cont' : '') + (mentionsMe(m) ? ' tome' : ''));
    d.dataset.id = m.id;
    const when = at.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
    const who = mine ? (m.to_id ? `You → ${m.to_name || 'them'} · private` : 'You') : (m.to_id ? `${m.from_name || 'Someone'} → you · private` : m.from_name || 'Someone');
    const meta = h('small', '', `${who} · ${when}`);
    if (m.failed) { const r = h('button', 'ol-retry', 'Not sent · try again'); r.onclick = ev => { ev.stopPropagation(); resend(m); }; meta.append(' ', r); }
    const acts = h('span', 'ol-macts');
    const act = (t, fn) => { const b = h('button', '', t); b.onclick = ev => { ev.stopPropagation(); fn(); }; acts.appendChild(b); };
    if (!mine && S.members.has(m.from_id)) act('Reply privately', () => { $('.ol-chto').value = m.from_id; drawTo(); $('.ol-cht').focus(); });
    if (!mine && S.members.has(m.from_id)) { d.title = `Click to answer ${m.from_name || 'them'} privately`;
      d.onclick = ev => { if (ev.target.closest('a,button') || getSelection().toString()) return; $('.ol-chto').value = m.from_id; drawTo(); $('.ol-cht').focus(); }; }
    act('Copy', () => navigator.clipboard.writeText(m.text).then(() => toast('Copied')).catch(() => {}));
    act('Put on board', () => { const c = centre(); const b = add('sticky', c.x - 100, c.y - 90, {color: STICKY[0], text: m.text}); select([b.id]); toast('Put on the board'); });
    const body = h('div', 'ol-mbody'); body.appendChild(msgBody(m.text));
    d.append(meta, body, acts);
    box.appendChild(d); prev = m;
  });
  if (atEnd || C.jump) box.scrollTop = box.scrollHeight; C.jump = false;
  drawTyping(); drawHere();
}
function drawTyping() {
  const C = chatState(), t = Date.now();
  const names = [...C.typing.values()].filter(x => t - x.at < 3500).map(x => x.name || 'Someone');
  $('.ol-chtyping').textContent = !names.length ? '' : names.length === 1 ? `${names[0]} is typing…`
    : names.length === 2 ? `${names[0]} and ${names[1]} are typing…` : `${names.length} people are typing…`;
}
setInterval(() => { if (S.chat && S.chat.typing.size) { const t = Date.now(); S.chat.typing.forEach((v, k) => t - v.at > 3500 && S.chat.typing.delete(k)); drawTyping(); } }, 1000);
function chatTyping(p) {
  if (!p || !p.uid || p.uid === S.me?.id || (p.to && p.to !== S.me?.id)) return;
  const C = chatState(); if (p.stop) C.typing.delete(p.uid); else C.typing.set(p.uid, {name: p.name, at: Date.now()});
  drawTyping();
}
const sayTyping = throttle(() => { if (S.mode === 'live' && chan) chan.send({type: 'broadcast', event: 'typing', payload: {uid: S.me.id, name: S.me.name, to: $('.ol-chto').value || null}}); }, 1500);
function gotMessage(m) {
  const C = chatState();
  if (!m || m.group_id !== groupId || !seeable(m)) return;
  const had = C.list.findIndex(x => x.id === m.id);
  if (had >= 0) { if (C.list[had].failed && !m.failed) { C.list[had] = m; drawChat(); } return; }
  C.list.push(m); C.list.sort(byTime); if (C.list.length > 300) C.list.shift();
  C.typing.delete(m.from_id);
  if (m.from_id !== S.me?.id) {
    const shut = $('.ol-chat').hidden, me = mentionsMe(m);
    if (shut) { C.unread++; if (me) C.mention = true; }
    if (shut || me) { toast(`${m.from_name || 'Someone'}${me ? ' mentioned you' : m.to_id ? ' (to you)' : ''}: ${m.text.slice(0, 80)}`); sfx('tap', me ? 1 : .7); }
  } else C.jump = true;
  drawChat();
}
async function stopChat() {
  const C = chatState(); C.list = []; C.unread = 0; C.mention = false; C.missing = false; C.typing.clear();
  if (chatChan) { const c = chatChan; chatChan = null; try { await sb.removeChannel(c); } catch (_) {} }
}
async function loadChat(gid) {
  await stopChat(); drawChat();
  const C = chatState(); C.readAt = store.get('olChatRead:' + gid, 0);
  const {data, error} = await sb.from('messages').select('*').eq('group_id', gid).order('created_at', {ascending: false}).limit(200);
  if (gid !== groupId) return;
  if (error) { C.missing = true; drawChat(); return; }
  C.list = (data || []).filter(seeable).sort(byTime);
  C.unread = C.list.filter(m => m.from_id !== S.me?.id && Date.parse(m.created_at) > C.readAt).length;
  C.mention = C.list.some(m => Date.parse(m.created_at) > C.readAt && mentionsMe(m));
  C.jump = true; drawChat();
  // its own channel, so a server without the table can't upset the board's live connection
  chatChan = sb.channel('chat:' + gid)
    .on('postgres_changes', {event: 'INSERT', schema: 'public', table: 'messages', filter: 'group_id=eq.' + gid}, ev => gotMessage(ev.new))
    .subscribe();
}
async function post(m) {
  const {failed, ...row} = m;
  const {error} = await sb.from('messages').insert(row);
  if (error) { m.failed = true; drawChat(); toast(/relation|schema cache|PGRST205|42P01/i.test(error.message || '') ? 'Messages need the one-off server update' : "Couldn't send that. Click it to try again"); }
  else if (m.failed) { m.failed = false; drawChat(); }
}
function resend(m) { if (S.mode === 'live' && m.group_id === groupId) { m.failed = false; drawChat(); post(m); } }
async function sendChat() {
  const inp = $('.ol-cht'), text = inp.value.trim(), C = chatState();
  if (!text || S.mode !== 'live' || !groupId) return;
  if (C.missing) return drawChat();
  if (Date.now() - lastSend < 400) return;                 // a held-down Enter doesn't flood the group
  lastSend = Date.now();
  const to = $('.ol-chto').value || null;
  const m = {id: uuid(), group_id: groupId, from_id: S.me.id, from_name: S.me.name, to_id: to,
             to_name: to ? (S.members.get(to) || {}).name || null : null, text: text.slice(0, 1000), created_at: new Date().toISOString()};
  inp.value = ''; grow(inp); gotMessage(m); sfx('send', .6);
  if (chan) chan.send({type: 'broadcast', event: 'typing', payload: {uid: S.me.id, stop: true, to}});
  post(m);
}
function grow(t) { t.style.height = ''; t.style.height = Math.min(t.scrollHeight, 120) + 'px'; }
function openChat(show = true) {
  const box = $('.ol-chat'); box.hidden = !show;
  const C = chatState();
  if (show) { C.unread = 0; C.mention = false; C.jump = true; drawChat(); $('.ol-cht').focus(); }
  else if (groupId) { C.readAt = Date.now(); store.set('olChatRead:' + groupId, C.readAt); }
  drawHere(); fmtSoon();
}
$('.ol-chatb').onclick = ev => { ev.stopPropagation(); openChat($('.ol-chat').hidden); sfx('tap', .7); };
$('.ol-chx').onclick = () => openChat(false);
$('.ol-chsend').onclick = sendChat;
$('.ol-chto').onchange = drawTo;
$('.ol-cht').addEventListener('keydown', ev => {
  ev.stopPropagation();
  if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); sendChat(); }
  if (ev.key === 'Escape') openChat(false);
});
$('.ol-cht').addEventListener('input', ev => { grow(ev.target); if (ev.target.value.trim()) sayTyping(); });
$('.ol-chat').addEventListener('pointerdown', ev => ev.stopPropagation());

// ---------------------------------------------------------------- saving: this computer only, or live with the group
// The file on the computer is the real copy. Writing it goes through the app, which takes a
// moment, so every change is also mirrored into the browser's own storage straight away -
// that one cannot be beaten by closing the window, and the two are compared on the way back in.
// Several offline boards: the first is 'main' (the file it always had), the rest have ids of their own.
// Nothing is on screen until the first board has loaded, so nothing may be saved over a board before then.
S.swapping = true;
const Local = {
  board: store.get('olLocalBoard', 'main'),
  boards: [{id: 'main', name: 'Offline board'}],
  key(k) { return this.board === 'main' ? k : k + ':' + this.board; },
  name() { return (this.boards.find(b => b.id === this.board) || {}).name || 'Offline board'; },
  async list() {
    try { const got = api()?.ol_boards ? await api().ol_boards() : null; if (Array.isArray(got) && got.length) this.boards = got; } catch (_) {}
    if (!this.boards.some(b => b.id === this.board)) this.board = 'main';
    return this.boards;
  },
  async load() {
    await window.bbApiReady;
    await this.list();
    const board = this.board;
    let rows = [];
    try { rows = api()?.ol_local_load ? await api().ol_local_load(this.board) : store.get(this.key('olBoard'), []); } catch (_) {}
    if (!Array.isArray(rows)) rows = [];
    const kept = store.get(this.key('olBoardMirror'), null);
    if (kept && Array.isArray(kept.rows)) {
      let fileAt = 0;
      try { fileAt = api()?.ol_local_saved_at ? await api().ol_local_saved_at(this.board) : 0; } catch (_) {}
      // a second's grace: the mirror is written first, the file a moment later
      if (kept.at > fileAt + 1500 && board === this.board && !kept.rows.some(r => r && r.group_id)) {
        setTimeout(() => toast('Picked up the last few changes from before'), 900);
        return kept.rows;
      }
    }
    return rows;
  },
  // Offline and group boards never share a save. Each of these checks the offline board is the one on screen and
  // settled (not halfway through a switch), so a save can't carry a group's blocks into it, or one board's into another.
  here() { return S.mode === 'local' && !S.swapping; },
  mirror() { if (!window.bbFrozen && this.here()) store.set(this.key('olBoardMirror'), {at: Date.now(), rows: [...S.blocks.values()]}); },
  t: null,
  write() {
    if (!Local.here()) return;
    Local.mirror();                                    // instant, and survives a quick close
    clearTimeout(Local.t);
    const board = Local.board, gen = S.gen;
    Local.t = setTimeout(() => { Local.t = null; if (Local.board === board && S.gen === gen) Local.now(); }, 250);
  },
  // whatever is waiting, saved now: for stepping off this board
  flush() { if (Local.t) { clearTimeout(Local.t); Local.t = null; Local.now(); } },
  now() {
    if (window.bbFrozen || !Local.here()) return;
    const rows = [...S.blocks.values()];
    if (api()?.ol_local_save) api().ol_local_save(rows, Local.board); else store.set(Local.key('olBoard'), rows);
  },
};
let sb = null, chan = null, groupId = null;
const pending = new Map(), timers = new Map();
// The newest stamp seen for each block. A copy saved a moment ago and coming back from the server is often older than
// what the live messages have already shown, and used to jump the block back for a moment (the stutter).
const revs = new Map();
function stamp(b) {
  const r = Math.max(Date.now(), ((b.data && b.data._rev) || 0) + 1);
  b.data = {...(b.data || {}), _rev: r}; revs.set(b.id, r);
}
const stale = (id, r) => !!r && r <= (revs.get(id) || 0);
const Sync = {
  save(b, soon) {
    if (S.mode === 'local') return Local.write();
    if (S.swapping || !groupId) return;
    if (!b.deleted) stamp(b);
    pending.set(b.id, {b, g: groupId}); clearTimeout(timers.get(b.id));
    timers.set(b.id, setTimeout(() => Sync.flush(b.id), soon ? 30 : 600));
  },
  async flush(id) {
    const it = pending.get(id); if (!it || !sb) return; pending.delete(id); timers.delete(id);
    const {b, g} = it;
    const row = {id: b.id, group_id: g, kind: b.kind, x: b.x, y: b.y, w: b.w, h: b.h, z: b.z || 0, data: b.data || {}, deleted: !!b.deleted};
    const {error} = await sb.from('blocks').upsert(row);
    if (error) { pending.set(id, it); if (g === groupId) setState('off', "Can't save. Trying again"); setTimeout(() => Sync.flush(id), 5000); }
    else if (S.mode === 'live' && R.querySelector('.ol-state.off')) setState('live');
  },
  remove(b) { if (S.mode === 'local') return Local.write(); Sync.save({...b, deleted: true}, true); },
  // Everything waiting, sent at once: for closing the window, or stepping away from it.
  flushAll() {
    if (S.mode === 'local') { Local.flush(); Local.now(); return; }
    [...pending.keys()].forEach(id => { clearTimeout(timers.get(id)); timers.delete(id); Sync.flush(id); });
  },
  // Live changes and the cursor go out together, at most one message every 80 ms per person. The server only takes
  // so many messages a second for the whole group, and five people each sending blocks and a cursor separately
  // went over it, so some were dropped and things jumped. Moving several blocks at once now sends all of them.
  live(b, posOnly) {
    if (S.mode !== 'live' || !chan) return;
    stamp(b);
    const was = Sync.out.get(b.id);
    Sync.out.set(b.id, posOnly && (!was || was.p) ? {p: 1, id: b.id, x: b.x, y: b.y, w: b.w, h: b.h, rev: b.data._rev} : {p: 0, id: b.id});
    Sync.tick();
  },
  cursor(p) { if (S.mode !== 'live' || !chan) return; Sync.cur = {x: Math.round(p.x), y: Math.round(p.y)}; Sync.tick(); },
  out: new Map(), cur: null,
  tick: throttle(() => {
    if (S.mode !== 'live' || !chan || (!Sync.out.size && !Sync.cur)) return;
    const items = [...Sync.out.values()].map(o => {
      if (o.p) return o;
      const b = S.blocks.get(o.id); return b ? {id: b.id, block: b} : null;
    }).filter(Boolean);
    const payload = {uid: S.me.id, items};
    if (Sync.cur) payload.cur = Sync.cur;
    Sync.out.clear(); Sync.cur = null;
    chan.send({type: 'broadcast', event: 'tick', payload});
  }, 80),
  presence() { if (S.mode === 'live' && chan) chan.track({uid: S.me.id, name: S.me.name, color: S.me.color, editing: S.editing}); },
};
// whatever gets moved live is held separately per block so a slow save never jumps it back
function incoming(row) {
  if (!row || !row.id) return;
  if (row.deleted) { if (S.blocks.has(row.id)) { S.blocks.delete(row.id); unpaint(row.id); emptyState(); } return; }
  // what you're typing in, dragging or still sending wins over the copy coming back
  if (S.editing === row.id || pending.has(row.id) || (drag && drag.items && drag.items.some(i => i.b.id === row.id))) return;
  // and so does anything newer that has already arrived live (or that you made yourself)
  const r = row.data && row.data._rev; if (stale(row.id, r)) return; if (r) revs.set(row.id, r);
  const b = {id: row.id, kind: row.kind, x: row.x, y: row.y, w: row.w, h: row.h, z: row.z, data: row.data || {}, updated_at: row.updated_at, _remote: true};
  S.blocks.set(b.id, b); paint(b); emptyState(); IDE.refresh(b);
}
// One block from someone's live message. Moves glide over the 80 ms between messages instead of hopping.
function liveIn(p) {
  if (!p || !p.id || S.editing === p.id) return;
  if (drag && drag.items && drag.items.some(i => i.b.id === p.id)) return;
  if (p.block && p.block.id === p.id) {
    if (stale(p.id, p.block.data && p.block.data._rev)) return;
    if (p.block.data && p.block.data._rev) revs.set(p.id, p.block.data._rev);
    const nb = {...p.block, data: p.block.data || {}}; S.blocks.set(nb.id, nb); glide(nb.id); paint(nb); emptyState(); IDE.refresh(nb);
    return;
  }
  const b = S.blocks.get(p.id); if (!b || stale(p.id, p.rev)) return;
  if (p.rev) { revs.set(p.id, p.rev); b.data = {...b.data, _rev: p.rev}; }
  Object.assign(b, {x: +p.x, y: +p.y, w: +p.w, h: +p.h}); glide(b.id); place(b);
}
function glide(id) {
  const d = S.nodes.get(id); if (!d) return;
  d.classList.add('glide'); clearTimeout(d._glide); d._glide = setTimeout(() => d.classList.remove('glide'), 160);
}
let cursT = 0;
function cursorsSoon() { if (!cursT) cursT = requestAnimationFrame(() => { cursT = 0; placeCursors(); }); }
function setState(cls, text) {
  const s = $('.ol-state'); s.className = 'ol-state ' + cls;
  s.querySelector('b').textContent = text || {live: 'Live', wait: 'Connecting', off: 'Offline', local: 'This computer'}[cls];
}

async function connect() {
  if (!CFG.url || !CFG.key) return false;
  try { await lib(SUPA, 'supabase'); } catch (_) { setState('off', 'No internet'); return false; }
  if (!sb) sb = window.supabase.createClient(CFG.url, CFG.key, {auth: {persistSession: true, autoRefreshToken: true, storageKey: 'bb-online-auth'}});
  let {data: {session}} = await sb.auth.getSession();
  if (!session) { const r = await sb.auth.signInAnonymously(); if (r.error) { setState('off', 'Sign-in failed'); gateMsg(r.error.message); return false; } session = r.data.session; }
  const color = store.get('olColor', null) || PEOPLE[parseInt(session.user.id.slice(0, 6), 16) % PEOPLE.length];
  S.me = {id: session.user.id, name: meName(), color};
  return true;
}
async function myGroups() {
  const {data, error} = await sb.from('members').select('group_id, groups(name, code)').eq('user_id', S.me.id);
  return error ? [] : (data || []).filter(r => r.groups).map(r => ({id: r.group_id, name: r.groups.name, code: r.groups.code}));
}
// Before a board is swapped for another, whatever is open on the old one is finished and saved there.
function leaveBoard() {
  if (S.editing) stopEdit();
  if (IDE.id) IDE.close();
  drag = null;
  Sync.flushAll();
  revs.clear(); Sync.out.clear(); Sync.cur = null;
  stopChat(); $('.ol-chat').hidden = true;
  S.gen = (S.gen || 0) + 1; S.swapping = true;
  return S.gen;
}
async function openGroup(g) {
  const gen = leaveBoard();
  if (chan) { await sb.removeChannel(chan); chan = null; }
  if (gen !== S.gen) return;
  S.group = g; groupId = g.id; store.set('olGroup', g); S.mode = 'live'; setState('wait');
  $('.ol-gname').textContent = g.name; $('.ol-code').textContent = g.code; gate.classList.remove('on');
  store.set('olLast', g.id);
  S.blocks.clear(); S.peers.clear(); S.sel.clear(); history.length = future.length = 0; paintAll();
  const load = async () => {
    const [{data: rows, error}, {data: mem}] = await Promise.all([
      sb.from('blocks').select('*').eq('group_id', g.id).eq('deleted', false),
      sb.from('members').select('user_id,name,color').eq('group_id', g.id)]);
    if (groupId !== g.id || S.mode !== 'live') return;   // moved on to another board meanwhile
    if (error) { setState('off', 'Not a member'); return; }
    S.members = new Map((mem || []).map(m => [m.user_id, {id: m.user_id, name: m.name, color: m.color}]));
    const keep = new Set(rows.map(r => r.id));
    [...S.blocks.keys()].forEach(id => keep.has(id) || pending.has(id) || (S.blocks.delete(id), unpaint(id)));
    rows.forEach(incoming); drawPeople(); emptyState(); return rows;
  };
  const rows = await load();
  if (gen !== S.gen) return;
  S.swapping = false;
  loadChat(g.id);
  if (rows && rows.length && !store.get('olView:' + g.id, null)) fit(null, false);
  chan = sb.channel('board:' + g.id, {config: {presence: {key: S.me.id}, broadcast: {self: false}}})
    .on('postgres_changes', {event: '*', schema: 'public', table: 'blocks', filter: 'group_id=eq.' + g.id}, ev => {
      const row = ev.new; if (!row) return;
      if (row.kind === 'image' && !row.deleted && !(row.data && row.data.src)) {   // big rows can arrive without their data
        sb.from('blocks').select('*').eq('id', row.id).single().then(r => r.data && incoming(r.data)); return; }
      incoming(row); })
    .on('postgres_changes', {event: '*', schema: 'public', table: 'members', filter: 'group_id=eq.' + g.id}, ev => {
      const m = ev.new; if (m && m.user_id) { S.members.set(m.user_id, {id: m.user_id, name: m.name, color: m.color}); drawPeople(); } })
    .on('broadcast', {event: 'typing'}, ({payload: p}) => chatTyping(p))
    .on('broadcast', {event: 'tick'}, ({payload: p}) => {
      if (!p || !p.uid) return;
      (p.items || []).forEach(liveIn);
      if (p.cur) { const peer = S.peers.get(p.uid) || {uid: p.uid, ...(S.members.get(p.uid) || {})};
        peer.cur = {x: +p.cur.x, y: +p.cur.y}; peer.seen = Date.now(); S.peers.set(p.uid, peer); cursorsSoon(); }
    })
    .on('broadcast', {event: 'live'}, ({payload: p}) => {
      if (!p || S.editing === p.id) return;
      const b = S.blocks.get(p.id);
      if (p.block && p.block.id === p.id) { const nb = {...p.block, data: p.block.data || {}}; S.blocks.set(nb.id, nb); paint(nb); emptyState(); IDE.refresh(nb); }
      else if (b) { Object.assign(b, {x: +p.x, y: +p.y, w: +p.w, h: +p.h}); place(b); }
    })
    .on('broadcast', {event: 'group'}, ({payload: p}) => {
      if (!p || !S.group || p.id !== S.group.id) return;
      if (p.gone) { toast(`${S.group.name} was deleted by whoever made it`); store.set('olGroup', null); useLocal(true); return; }
      if (p.name) { S.group.name = p.name; store.set('olGroup', S.group); $('.ol-gname').textContent = p.name; }
    })
    .on('broadcast', {event: 'cur'}, ({payload: p}) => {
      if (!p || !p.uid) return; const peer = S.peers.get(p.uid) || {uid: p.uid, ...(S.members.get(p.uid) || {})};
      peer.cur = {x: +p.x, y: +p.y}; peer.seen = Date.now(); S.peers.set(p.uid, peer); cursorsSoon(); })
    .on('presence', {event: 'sync'}, () => {
      const st = chan.presenceState(), seen = new Set();
      Object.values(st).forEach(list => list.forEach(p => { if (!p.uid || p.uid === S.me.id) return; seen.add(p.uid);
        const old = S.peers.get(p.uid) || {}; S.peers.set(p.uid, {...old, uid: p.uid, name: p.name, color: p.color, editing: p.editing, seen: old.seen || Date.now()}); }));
      [...S.peers.keys()].forEach(id => seen.has(id) || S.peers.delete(id));
      markPeers(); drawPeople(); placeCursors(); })
    .subscribe(status => {
      if (status === 'SUBSCRIBED') { setState('live'); Sync.presence(); if (chan._wasDown) load(); chan._wasDown = false; }
      else if (status === 'CHANNEL_ERROR' || status === 'TIMED_OUT' || status === 'CLOSED') { setState('wait', 'Reconnecting'); if (chan) chan._wasDown = true; }
    });
  sfx('on');
}
async function useLocal(remember = true, board) {
  if (S.mode === 'local' && !S.swapping && board && board !== Local.board) {
    Local.flush(); Local.now(); Local.mirror();
    if (api()?.ol_local_save) await api().ol_local_save([...S.blocks.values()], Local.board);
  }
  const gen = leaveBoard();
  if (board) { Local.board = board; store.set('olLocalBoard', board); }
  S.sel.clear();
  if (chan) { await sb.removeChannel(chan); chan = null; }
  S.mode = 'local'; S.group = null; groupId = null; if (remember) store.set('olGroup', 'local'); setState('local');
  $('.ol-gname').textContent = ''; $('.ol-code').textContent = ''; gate.classList.remove('on');
  S.blocks.clear(); S.members.clear(); S.peers.clear(); history.length = future.length = 0;
  const rows = await Local.load();
  if (gen !== S.gen) return;                           // moved on to another board meanwhile
  rows.forEach(b => b && b.id && S.blocks.set(b.id, b)); S.swapping = false; paintAll(); drawPeople(); placeCursors();
  if (Local.boards.length > 1) $('.ol-gname').textContent = Local.name();
}
async function newLocalBoard() {
  const name = prompt('Name the new offline board', 'Board ' + (Local.boards.length + 1));
  if (name === null) return;
  if (!api()?.ol_board_new) return toast('This needs the app');
  const b = await api().ol_board_new(name);
  await Local.list(); await useLocal(true, b.id); fit(); toast('Made ' + b.name);
}
async function renameLocalBoard() {
  const name = prompt('Rename this board', Local.name());
  if (!name || !api()?.ol_board_rename) return;
  Local.boards = await api().ol_board_rename(Local.board, name);
  $('.ol-gname').textContent = Local.name(); toast('Renamed');
}
async function removeLocalBoard() {
  if (Local.board === 'main') return toast('The first board stays');
  if (!confirm(`Take "${Local.name()}" off the list? A copy is kept in the backups folder.`)) return;
  Local.now(); const gone = Local.board;
  if (api()?.ol_local_save) await api().ol_local_save([...S.blocks.values()], gone);
  const r = api()?.ol_board_remove ? await api().ol_board_remove(gone) : null;
  await useLocal(true, 'main'); await Local.list(); toast((r && r.msg) || 'Removed');
}

// Renaming and deleting a group are done by the server, which checks who is asking. A server set up before
// these were added says it can't find them, and only whoever looks after the server can fix that.
function oldServer(error) {
  if (!/PGRST202|Could not find the function|does not exist/i.test(error.message || '') && error.code !== 'PGRST202') return false;
  alert('The group server needs a one-off update first.\n\nOpen "Online setup.sql" from the app folder, paste all of it into Supabase > SQL Editor and press Run. Running it again is safe.');
  return true;
}
async function renameGroup() {
  const name = prompt('Rename this group (everyone in it sees the new name)', S.group.name);
  if (!name || !name.trim() || name.trim() === S.group.name) return;
  const {data, error} = await sb.rpc('rename_group', {p_group: groupId, p_name: name.trim()});
  if (error) return oldServer(error) || toast(error.message);
  S.group.name = data.name; store.set('olGroup', S.group); $('.ol-gname').textContent = data.name;
  chan?.send({type: 'broadcast', event: 'group', payload: {id: groupId, name: data.name}}); toast('Renamed');
}
async function leaveGroup() {
  if (!confirm(`Leave ${S.group.name}? The board stays for everyone else, and you can join again with the code.`)) return;
  await sb.from('members').delete().eq('group_id', groupId).eq('user_id', S.me.id); store.set('olGroup', null); showGate();
}
async function deleteGroup() {
  const g = S.group;
  const typed = prompt(`This deletes ${g.name} and its whole board for everyone in it. It can't be undone.\n\nA copy of the board is kept on this computer as an offline board.\n\nType the group's name to delete it:`);
  if (typed === null) return;
  if (typed.trim().toLowerCase() !== g.name.trim().toLowerCase()) return toast("That isn't the group's name, so nothing was deleted");
  // keep a copy first, so a slip of the hand isn't the end of everyone's work
  const rows = [...S.blocks.values()].filter(b => !b.deleted).map(({_remote, ...b}) => b);
  let copy = null;
  if (api()?.ol_board_new && api()?.ol_local_save) {
    copy = await api().ol_board_new(g.name + ' (copy)'); await api().ol_local_save(rows, copy.id);
  }
  const {error} = await sb.rpc('delete_group', {p_group: g.id});
  if (error) {
    if (copy) await api().ol_board_remove?.(copy.id);
    return oldServer(error) || toast(/Only whoever/.test(error.message) ? 'Only whoever made the group can delete it. You can leave it instead' : error.message);
  }
  chan?.send({type: 'broadcast', event: 'group', payload: {id: g.id, gone: true}});
  store.set('olGroup', null); store.set('olLast', null);
  if (copy) { await Local.list(); await useLocal(true, copy.id); toast(`Deleted ${g.name}. Its board is kept here as "${copy.name}"`); }
  else { await useLocal(true); toast(`Deleted ${g.name}`); }
}

// ---------------------------------------------------------------- the start card: make or join a group
function gateMsg(m) { const e = card.querySelector('.msg'); if (e) e.textContent = m || ''; }
async function showGate(view) {
  gate.classList.add('on'); card.replaceChildren();
  const local = h('button', 'alt', 'Use a board on this computer only'); local.onclick = useLocal;
  if (!CFG.url || !CFG.key) {
    card.innerHTML = `<h2>Online</h2><p>Work on one board with your group, live. This needs your group's server address and key.
      Ask whoever set it up for them, or see "Online setup" in the app folder to set one up (free).</p>
      <label>Server address</label><input class="u" placeholder="https://xxxx.supabase.co">
      <label>Public (anon) key</label><input class="k" placeholder="eyJ…">
      <p class="msg"></p><button class="btn go">Connect</button>`;
    card.querySelector('.go').onclick = async () => {
      const url = card.querySelector('.u').value.trim().replace(/\/$/, ''), key = card.querySelector('.k').value.trim();
      if (!/^https:\/\/[a-z0-9-]+\.supabase\.(co|in)$/.test(url)) return gateMsg('The address looks like https://something.supabase.co');
      if (key.length < 30) return gateMsg("That key looks too short. It's the long \"anon public\" one");
      const r = api()?.online_config ? await api().online_config(url, key) : 'ok';
      if (r !== 'ok') return gateMsg(r);
      CFG.url = url; CFG.key = key; showGate();
    };
    card.appendChild(local); return;
  }
  card.innerHTML = '<h2>Online</h2><p>Connecting…</p>';
  if (!await connect()) { card.innerHTML = '<h2>Online</h2><p>Couldn\'t reach the group server. Check your internet and try again.</p><p class="msg"></p><button class="btn go">Try again</button>';
    card.querySelector('.go').onclick = () => showGate(); card.appendChild(local); return; }
  const groups = await myGroups();
  card.innerHTML = `<h2>Online</h2><p>One board for your whole group. Everyone sees changes as they happen.</p>
    <label>Your name</label><input class="nm" maxlength="40">
    <div class="ol-groups"></div>
    <label>Join a group</label><div class="row2"><input class="cd" placeholder="Invite code, like K7QM-2XPA" maxlength="12"><button class="btn jn">Join</button></div>
    <div class="or">OR</div>
    <label>Start a new group</label><div class="row2"><input class="gn" placeholder="Group name, like SDGP Team 7" maxlength="60"><button class="btn mkgroup">Create</button></div>
    <p class="msg"></p>`;
  const nm = card.querySelector('.nm'); nm.value = S.me.name;
  const name = () => { const v = nm.value.trim() || 'Me'; store.set('olName', v); S.me.name = v; return v; };
  if (groups.length) { const box = card.querySelector('.ol-groups'); box.before(Object.assign(h('label', '', 'Your groups')));
    groups.forEach(g => { const b = h('button'); b.append(h('span', '', g.name), h('small', '', g.code)); b.onclick = () => { name(); openGroup(g); }; box.appendChild(b); }); }
  card.querySelector('.jn').onclick = async () => {
    const code = card.querySelector('.cd').value; if (code.replace(/[^a-z0-9]/gi, '').length < 8) return gateMsg('Codes have 8 letters and numbers');
    gateMsg(''); const {data, error} = await sb.rpc('join_group', {p_code: code, p_member: name(), p_color: S.me.color});
    if (error) return gateMsg(/No group/.test(error.message) ? "No group has that code. Check it with whoever sent it" : error.message);
    openGroup(data); toast(`You've joined ${data.name}`);
  };
  card.querySelector('.mkgroup').onclick = async () => {
    const gn = card.querySelector('.gn').value.trim(); if (!gn) return gateMsg('Give the group a name');
    gateMsg(''); const {data, error} = await sb.rpc('create_group', {p_name: gn, p_member: name(), p_color: S.me.color});
    if (error) return gateMsg(error.message);
    openGroup(data); copyCode(); toast(`Made ${data.name}. The invite code ${data.code} is copied, send it to your group`);
  };
  card.querySelector('.cd').onkeydown = ev => { if (ev.key === 'Enter') card.querySelector('.jn').click(); };   // returning false here would eat every key
  card.querySelector('.gn').onkeydown = ev => { if (ev.key === 'Enter') card.querySelector('.mkgroup').click(); };   // returning false here would eat every key
  card.appendChild(local);
  if (view === 'join') card.querySelector('.cd').focus();
  else if (view === 'new') card.querySelector('.gn').focus();
}
// ---------------------------------------------------------------- switching board
// You can be in as many groups as you like. This lists them, with the board kept on this
// computer first, so moving between them is one click rather than a trip through the start card.
// An array of groups, 'noserver' when none has ever been set up, or null when one has but it
// cannot be reached. Each of those wants different words in the list.
async function knownGroups() {
  if (!CFG.url || !CFG.key) return 'noserver';
  if (!sb && !(await connect())) return null;
  try { return await myGroups(); } catch (_) { return null; }
}
async function showBoards(anchor) {
  const here = S.mode === 'live' && S.group ? S.group.id : 'local';
  const tick = on => on ? '\u2713  ' : '\u00a0\u00a0\u00a0 ';
  anchor.setAttribute('aria-expanded', 'true');
  // show the list straight away, then fill the groups in when the server answers
  const build = (gs, loading) => {
    const list = Array.isArray(gs) ? gs : [];
    const items = [{head: 'on your own'},
      ...Local.boards.map(b => ({label: tick(here === 'local' && Local.board === b.id) + b.name, key: b.id === 'main' ? 'only here' : '',
        go: () => { if (here !== 'local' || Local.board !== b.id) useLocal(true, b.id); }})),
      {label: '\u00a0\u00a0\u00a0 New offline board\u2026', go: newLocalBoard},
      here === 'local' && {label: '\u00a0\u00a0\u00a0 Rename this board\u2026', go: renameLocalBoard},
      here === 'local' && Local.board !== 'main' && {label: '\u00a0\u00a0\u00a0 Remove this board\u2026', go: removeLocalBoard},
      {head: loading ? 'groups \u2014 looking\u2026' : 'with a group'}].filter(Boolean);
    list.forEach(g => items.push({label: tick(here === g.id) + g.name, key: g.code,
                                  go: () => { if (here !== g.id) openGroup(g); }}));
    if (here !== 'local') items.push({label: '\u00a0\u00a0\u00a0 Rename this group\u2026', go: renameGroup},
                                     {label: '\u00a0\u00a0\u00a0 Leave this group\u2026', go: leaveGroup},
                                     {label: '\u00a0\u00a0\u00a0 Delete this group\u2026', go: deleteGroup});
    if (!loading && Array.isArray(gs) && !gs.length)
      items.push({label: '\u00a0\u00a0\u00a0 You are not in a group yet', go: () => showGate('join')});
    if (!loading && gs === null)
      items.push({label: '\u00a0\u00a0\u00a0 Can\u2019t reach the server', go: () => showGate()});
    if (!loading && gs === 'noserver')
      items.push({label: '\u00a0\u00a0\u00a0 No group server set up yet', go: () => showGate()});
    items.push({rule: 1});
    if (gs === 'noserver') items.push({label: 'Set up a group server\u2026', go: () => showGate()});
    else {
      items.push({label: 'Join a group with a code\u2026', go: () => showGate('join')});
      items.push({label: 'Start a new group\u2026', go: () => showGate('new')});
    }
    ctxBuild(items);
    const r = anchor.getBoundingClientRect();
    ctxOpen(r.left, r.bottom + 6);
  };
  await Local.list();
  build(null, true);
  build(await knownGroups(), false);
}
const switcher = $('.ol-switch');
switcher.onclick = ev => {
  ev.stopPropagation();
  if (switcher.getAttribute('aria-expanded') === 'true') return closeCtx();
  closeMenu(); showBoards(switcher); sfx('tap', .7);
};

function copyCode() { if (S.group) navigator.clipboard.writeText(S.group.code).catch(() => {}); }
$('.ol-code').onclick = () => { copyCode(); toast('Invite code copied. Send it to your group'); sfx('tap'); };

// ---------------------------------------------------------------- the ••• menu
const menu = $('.ol-menu');
function closeMenu() { menu.classList.remove('on'); }
$('.ol-more').onclick = ev => { ev.stopPropagation(); if (menu.classList.toggle('on')) fillMenu(); };
function fillMenu() {
  menu.replaceChildren();
  const item = (t, fn) => { const b = h('button', '', t); b.onclick = () => { closeMenu(); fn(); }; menu.appendChild(b); };
  if (S.mode === 'live') {
    menu.appendChild(h('small', '', S.group.name));
    item('Copy invite code', () => { copyCode(); toast('Invite code copied'); });
    item('Messages', () => openChat(true));
    item('Change my name', async () => { const v = prompt('Your name on the board', S.me.name); if (!v || !v.trim()) return;
      S.me.name = v.trim().slice(0, 40); store.set('olName', S.me.name); Sync.presence();
      await sb.from('members').update({name: S.me.name}).eq('group_id', groupId).eq('user_id', S.me.id); });
    item('Switch board or group', () => showBoards($('.ol-switch')));
    item('Join another group with a code', () => showGate('join'));
    item('Start a new group', () => showGate('new'));
    item('Rename this group\u2026', renameGroup);
    item('Leave this group\u2026', leaveGroup);
    item('Delete this group for everyone\u2026', deleteGroup);
  } else {
    menu.appendChild(h('small', '', 'This computer only'));
    item('Switch board or group', () => showBoards($('.ol-switch')));
    item('Rename this board\u2026', renameLocalBoard);
    if (Local.board !== 'main') item('Remove this board\u2026', removeLocalBoard);
    item('Go online with a group', () => showGate());
  }
  menu.appendChild(h('small', '', 'Board'));
  item('Find on the board', openFind);
  item('Add: To do \u00b7 Doing \u00b7 Done', () => dropLayout('board', centre()));
  item('Add: Revision plan', () => dropLayout('revision', centre()));
  item('Show everything', () => fit());
  item('Save the board as a file', exportBoard);
  menu.appendChild(h('small', '', 'Code'));
  item('Bring in code files or a zip\u2026', () => importCode('files'));
  item('Bring in a code folder\u2026', () => importCode('folder'));
  item('Save all the code on the board\u2026', () => exportCode([...S.blocks.values()]));
  item('Clear my selection', () => select([]));
}
function exportBoard() {
  const rows = [...S.blocks.values()].map(({_remote, ...b}) => b);
  const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([JSON.stringify({board: S.group?.name || 'Board', saved: now(), blocks: rows}, null, 1)], {type: 'application/json'}));
  a.download = (S.group?.name || 'board').replace(/[^\w -]/g, '') + '.json'; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

// ---------------------------------------------------------------- code in and out of the board
// In the app, Windows' own Open and Save boxes do it (zips included); in a plain browser it falls
// back to the browser's file picker and a download.
const LANG_OF = {...Object.fromEntries(Object.entries(EXT_FOR).map(([l, e]) => [e, l])),
  mjs: 'javascript', jsx: 'javascript', tsx: 'typescript', htm: 'html', h: 'c', hpp: 'cpp', cc: 'cpp', bat: 'powershell', txt: 'plaintext', md: 'plaintext'};
const fileSlug = t => (t || '').trim().replace(/[<>:"/\\|?*\x00-\x1f]/g, '').slice(0, 60);
// What a block holds, as files: a project's own, or one file named after a code or terminal block.
function codeFiles(b) {
  if (b.kind === 'proj') return filesOf(b).map(({n, t}) => ({n, t: t || ''}));
  if (b.kind === 'code') {
    const e = EXT_FOR[codeLang(b)] || 'txt', t = fileSlug(b.data.title);
    return [{n: t ? (ext(t) ? t : t + '.' + e) : linkName(b), t: b.data.text || ''}];
  }
  if (b.kind === 'term') return [{n: (fileSlug(b.data.title) || 'terminal') + '.txt', t: noAnsi(b.data.text || '')}];
  return [];
}
async function exportCode(list, name) {
  const coded = list.filter(b => ['code', 'proj', 'term'].includes(b.kind));
  if (!coded.length) return toast('There is no code on the board to save yet');
  let files;
  if (coded.length === 1) files = codeFiles(coded[0]);
  else {                                  // several blocks: each project gets a folder, loose files sit beside them
    files = []; const used = new Set();
    coded.forEach(b => {
      const dir = b.kind === 'proj' ? (fileSlug(b.data.title) || 'project') : '';
      codeFiles(b).forEach(f => {
        let n = (dir ? dir + '/' : '') + f.n;
        const [stem, e] = [n.replace(/\.[^./]+$/, ''), ext(n)];
        for (let i = 2; used.has(n); i++) n = stem + '_' + i + (e ? '.' + e : '');
        used.add(n); files.push({n, t: f.t});
      });
    });
  }
  if (!files.length) return toast('There is no code in there to save');
  name = fileSlug(name || (coded.length === 1 && coded[0].data.title) || S.group?.name || 'code') || 'code';
  if (api()?.ol_export) {
    const r = await api().ol_export(name, files);
    if (r && r.error) toast(r.error); else if (r && r.msg) { toast(r.msg); sfx('done'); }
    return;
  }
  // a plain browser: one file downloads as itself, several as one bundle of files
  const one = files.length === 1, a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([one ? files[0].t : JSON.stringify({files}, null, 1)], {type: 'text/plain'}));
  a.download = one ? files[0].n.split('/').pop() : name + '.json'; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}
// One file comes in as a code block; several, a folder or a zip come in as a project.
function placeCode(name, files, at) {
  files = (files || []).filter(f => f && f.n && typeof f.t === 'string');
  if (!files.length) return toast('Nothing to bring in: only text and code files up to 400 KB');
  const p = at || centre();
  let b;
  if (files.length === 1) {
    const f = files[0], lang = LANG_OF[ext(f.n)] || 'auto';
    b = add('code', p.x, p.y, {lang: lang === 'plaintext' ? 'auto' : lang, text: f.t, title: f.n.split('/').pop()});
  } else {
    b = add('proj', p.x, p.y, {title: fileSlug(name) || 'Imported code', files: files.map(f => ({n: cleanName(f.n), t: f.t}))});
  }
  select([b.id]); sfx('done');
  toast(files.length === 1 ? 'Brought in ' + files[0].n.split('/').pop() : `Brought in ${files.length} files as a project`);
  return b;
}
const codePick = (() => { const i = document.createElement('input'); i.type = 'file'; i.multiple = true; i.hidden = true; return i; })();
async function importCode(how, at) {
  if (api()?.ol_import) {
    const r = await api().ol_import(how || 'files');
    if (!r || r.cancelled) return;
    if (r.error) return toast(r.error);
    placeCode(r.name, r.files, at);
    if (r.skipped) setTimeout(() => toast(`Left out ${r.skipped} (pictures, build folders, binaries or too big)`), 2700);
    return;
  }
  codePick.webkitdirectory = how === 'folder';
  codePick.onchange = async () => {
    const out = [];
    for (const f of [...codePick.files].slice(0, 300)) {
      if (f.size > 400000 || /^(image|video|audio)\//.test(f.type)) continue;
      const t = await f.text(); if (!/\x00/.test(t)) out.push({n: f.webkitRelativePath || f.name, t});
    }
    codePick.value = '';
    placeCode(how === 'folder' && out[0] ? out[0].n.split('/')[0] : (out.length > 1 ? 'Imported code' : ''), out, at);
  };
  codePick.click();
}
document.addEventListener('pointerdown', ev => { if (!ev.target.closest('.ol-menu,.ol-more')) closeMenu(); });

// Holding space turns the pointer into the hand for as long as it is held.
let spaceDown = false;
addEventListener('keydown', ev => {
  if (ev.code !== 'Space' || spaceDown || !onBoard(ev)) return;
  if (ev.target.closest('input,textarea,[contenteditable],select,.ol-ide')) return;
  spaceDown = true; board.classList.add('grab'); ev.preventDefault();
}, true);
addEventListener('keyup', ev => {
  if (ev.code !== 'Space') return;
  spaceDown = false; board.classList.remove('grab');
}, true);
addEventListener('blur', () => { spaceDown = false; board.classList.remove('grab'); });

// ---------------------------------------------------------------- find on the board
// Looks through whatever text a block holds, then walks the matches, centring each one.
const findIn = $('.ol-huntq'), findN = $('.ol-huntn');
let hits = [], hitAt = -1;

function searchable(b) {
  const D = b.data || {};
  const bits = [D.title, D.text, D.label, D.url, D.lang,
                D.html && stripTags(D.html),
                Array.isArray(D.items) && D.items.map(i => i && (i.t || i.text)).join(' '),
                Array.isArray(D.files) && D.files.map(f => f && (f.n + ' ' + (f.t || ''))).join(' ')];
  return bits.filter(x => typeof x === 'string' && x).join(' ').toLowerCase();
}
const stripTags = html => { const e = document.createElement('div'); e.innerHTML = html; return e.textContent || ''; };

function runFind(q) {
  S.nodes.forEach(d => d.classList.remove('found'));
  hits = []; hitAt = -1;
  q = (q || '').trim().toLowerCase();
  if (q) {
    hits = [...S.blocks.values()]
      .filter(b => b.kind !== 'arrow' && searchable(b).includes(q))
      .sort((a, b) => a.y - b.y || a.x - b.x)
      .map(b => b.id);
  }
  findN.textContent = !q ? '' : hits.length ? '0/' + hits.length : 'none';
  if (hits.length) stepFind(1);
}
function stepFind(by) {
  if (!hits.length) return;
  S.nodes.forEach(d => d.classList.remove('found'));
  hitAt = (hitAt + by + hits.length) % hits.length;
  const b = S.blocks.get(hits[hitAt]);
  if (!b) { hits.splice(hitAt, 1); hitAt--; return stepFind(by); }
  findN.textContent = (hitAt + 1) + '/' + hits.length;
  // centre it without changing how far in you are zoomed
  S.view.x = board.clientWidth / 2 - (b.x + b.w / 2) * S.view.k;
  S.view.y = board.clientHeight / 2 - (b.y + b.h / 2) * S.view.k;
  applyView();
  select([b.id]);
  node(b).classList.add('found');
  sfx('tap', .7);
}
function openFind() { findIn.focus(); findIn.select(); if (findIn.value) runFind(findIn.value); }
function closeFind() {
  findIn.value = '';
  S.nodes.forEach(d => d.classList.remove('found'));
  hits = []; hitAt = -1; findN.textContent = '';
  R.focus({preventScroll: true});
}

// ================================================================ writing: typos and tidying
// Nothing here guesses. It only ever changes a word it has been told is wrong, so it cannot
// mangle a name, a module or anything else it has simply never met.
const Writing = (() => {
  const PAIRS =
    'seperate>separate|seperately>separately|definately>definitely|recieve>receive|recieved>received|' +
    'occured>occurred|occuring>occurring|untill>until|alot>a lot|teh>the|adn>and|becuase>because|' +
    'becasue>because|beacuse>because|acheive>achieve|acheived>achieved|beleive>believe|belive>believe|' +
    'calender>calendar|concious>conscious|embarass>embarrass|embarassing>embarrassing|' +
    'enviroment>environment|existance>existence|foriegn>foreign|goverment>government|grammer>grammar|' +
    'independant>independent|neccessary>necessary|necesary>necessary|occassion>occasion|' +
    'occassionally>occasionally|occurence>occurrence|occurance>occurrence|persistant>persistent|' +
    'posession>possession|publically>publicly|recomend>recommend|reccomend>recommend|refered>referred|' +
    'relevent>relevant|religous>religious|remeber>remember|resturant>restaurant|succesful>successful|' +
    'successfull>successful|succesfully>successfully|suprise>surprise|tommorow>tomorrow|' +
    'tommorrow>tomorrow|truely>truly|unfortunatly>unfortunately|wierd>weird|writting>writing|' +
    'yeild>yield|accomodate>accommodate|acommodate>accommodate|adress>address|agressive>aggressive|' +
    'apparant>apparent|aparently>apparently|arguement>argument|basicly>basically|begining>beginning|' +
    'bizzare>bizarre|buisness>business|cemetary>cemetery|cheif>chief|collegue>colleague|comming>coming|' +
    'commitee>committee|completly>completely|concieve>conceive|decieve>deceive|diffrent>different|' +
    'dissapoint>disappoint|dissapear>disappear|equiped>equipped|excercise>exercise|exagerate>exaggerate|' +
    'familar>familiar|finaly>finally|freind>friend|gaurd>guard|harrass>harass|hieght>height|' +
    'heirarchy>hierarchy|immediatly>immediately|incidently>incidentally|interupt>interrupt|' +
    'irresistable>irresistible|knowlege>knowledge|liason>liaison|libary>library|maintainance>maintenance|' +
    'maintenence>maintenance|millenium>millennium|mischievious>mischievous|noticable>noticeable|' +
    'paralel>parallel|parliment>parliament|peice>piece|perseverence>perseverance|personel>personnel|' +
    'posses>possess|preceed>precede|prefered>preferred|priviledge>privilege|' +
    'pronounciation>pronunciation|propoganda>propaganda|questionaire>questionnaire|reciept>receipt|' +
    'rythm>rhythm|rythem>rhythm|sacrafice>sacrifice|seige>siege|sieze>seize|similer>similar|' +
    'sincerly>sincerely|speach>speech|strenght>strength|supercede>supersede|tendancy>tendency|' +
    'threshhold>threshold|tounge>tongue|twelth>twelfth|vaccum>vacuum|vehical>vehicle|visable>visible|' +
    'whereever>wherever|withold>withhold|aquire>acquire|arround>around|athiest>atheist|' +
    'auxilary>auxiliary|aweful>awful|challange>challenge|comparsion>comparison|critisise>criticise|' +
    'curiousity>curiosity|decison>decision|desicion>decision|developement>development|dilemna>dilemma|' +
    'aswell>as well|infront>in front|atleast>at least|incase>in case|ofcourse>of course|' +
    'offcourse>of course|thankyou>thank you|' +
    'dont>don\u2019t|doesnt>doesn\u2019t|dosent>doesn\u2019t|didnt>didn\u2019t|cant>can\u2019t|' +
    'wont>won\u2019t|wouldnt>wouldn\u2019t|couldnt>couldn\u2019t|shouldnt>shouldn\u2019t|isnt>isn\u2019t|' +
    'arent>aren\u2019t|wasnt>wasn\u2019t|werent>weren\u2019t|hasnt>hasn\u2019t|havent>haven\u2019t|' +
    'hadnt>hadn\u2019t|im>I\u2019m|ive>I\u2019ve|youre>you\u2019re|theyre>they\u2019re|thats>that\u2019s|' +
    'whats>what\u2019s|' +
    'funtion>function|fucntion>function|retrun>return|reutrn>return|lenght>length|widht>width|' +
    'heigth>height|pyton>python|pyhton>python|varaible>variable|varible>variable|paramter>parameter|' +
    'arguemnt>argument|databse>database|defualt>default|dependancy>dependency|excpetion>exception|' +
    'exeption>exception|inital>initial|jsut>just|oject>object|perfomance>performance|' +
    'performace>performance|recursivly>recursively|refrence>reference|requried>required|' +
    'resposne>response|respone>response|sucess>success|sucessful>successful|swtich>switch|' +
    'swich>switch|wether>whether|wiht>with|wtih>with';
  const FIX = {};
  PAIRS.split('|').forEach(pair => { const [bad, good] = pair.split('>'); FIX[bad] = good; });
  const WORD = '[A-Za-z\u2019\']+';
  const esc = t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

  // The fix borrows the shape of what was typed: TEH becomes THE, Teh becomes The.
  function lookup(w) {
    const good = FIX[w.toLowerCase().replace(/\u2019/g, "'").replace(/'/g, "'")]
      || FIX[w.toLowerCase().replace(/'/g, '\u2019')];
    if (!good) return null;
    if (w.length > 1 && w === w.toUpperCase() && /[A-Z]/.test(w)) return good.toUpperCase();
    if (w[0] === w[0].toUpperCase()) return good[0].toUpperCase() + good.slice(1);
    return good;
  }

  // ---------------------------------------------------------- what counts as worth fixing
  // Each one knows how many times it happens and how to put it right, so a single press can
  // fix every one of them at once.
  function finds(text) {
    const out = [];
    const typos = new Map();
    (text.match(new RegExp(WORD, 'g')) || []).forEach(w => {
      if (lookup(w)) typos.set(w, (typos.get(w) || 0) + 1);
    });
    typos.forEach((n, w) => out.push({
      what: w + ' \u2192 ' + lookup(w), why: 'a misspelling', n,
      apply: t => t.replace(new RegExp('\\b' + esc(w) + '\\b', 'g'), lookup(w)),
    }));

    const twice = new Map(), dblRe = new RegExp('\\b(' + WORD + ')(\\s+)\\1\\b', 'gi');
    text.replace(dblRe, (m, w) => { twice.set(w.toLowerCase(), (twice.get(w.toLowerCase()) || 0) + 1); return m; });
    twice.forEach((n, w) => out.push({
      what: '\u201c' + w + ' ' + w + '\u201d', why: 'the same word twice over', n,
      apply: t => t.replace(new RegExp('\\b(' + esc(w) + ')\\s+\\1\\b', 'gi'), '$1'),
    }));

    const lone = (text.match(/\bi\b(?!\.)/g) || []).length;
    if (lone) out.push({what: 'i \u2192 I', why: 'the word I takes a capital', n: lone,
                        apply: t => t.replace(/\bi\b(?!\.)/g, 'I')});

    const sp = (text.match(/[ \t]+[,.;:!?]/g) || []).length;
    if (sp) out.push({what: 'a space before a comma or full stop', why: 'the space goes after, not before',
                      n: sp, apply: t => t.replace(/[ \t]+([,.;:!?])/g, '$1')});

    const two = (text.match(/\S {2,}\S/g) || []).length;
    if (two) out.push({what: 'two spaces in a row', why: 'one is enough', n: two,
                       apply: t => t.replace(/(\S) {2,}(\S)/g, '$1 $2')});

    //  is the seam a note's html is stitched with, so the first word still counts as
    // the start of the text even when it is wrapped in bold.
    const small = (text.match(/(^*|[.!?]\s+)[a-z]/g) || []).length;
    if (small) out.push({what: 'a sentence starting small', why: 'a capital after a full stop', n: small,
                         apply: t => t.replace(/(^*|[.!?]\s+)([a-z])/g, (m, a, c) => a + c.toUpperCase())});
    return out;
  }

  // ---------------------------------------------------------- reading and writing a block
  const FIELD = {note: 'html', sticky: 'text', text: 'text', shape: 'text', link: 'title', todo: 'items'};
  const minds = b => b && !!FIELD[b.kind] && !locked(b);
  // A note keeps its words inside html. The text between the tags is stitched together and
  // fixed in one go, or a rule like 'a capital after a full stop' would treat every tag as the
  // start of a new sentence. The joins are a character nothing else here ever touches.
  const SEAM = '';
  function overText(html, fn) {
    const parts = html.split(/(<[^>]*>)/);
    const mine = parts.filter((_, i) => i % 2 === 0);
    const out = fn(mine.join(SEAM)).split(SEAM);
    if (out.length !== mine.length) return html;          // a seam went missing: leave it be
    let k = 0;
    return parts.map((p, i) => i % 2 ? p : out[k++]).join('');
  }

  function words(b) {
    const D = b.data || {};
    if (b.kind === 'note') { try { return blockText(b); } catch (_) { return ''; } }
    if (b.kind === 'todo') return (D.items || []).map(i => i.t || '').join('\n');
    return D[FIELD[b.kind]] || '';
  }
  function patch(b, fn) {
    const D = b.data || {};
    if (b.kind === 'note') return {html: overText(D.html || '', fn)};
    if (b.kind === 'todo') return {items: (D.items || []).map(i => ({...i, t: fn(i.t || '')}))};
    return {[FIELD[b.kind]]: fn(D[FIELD[b.kind]] || '')};
  }
  function put(b, fn) { change(b.id, x => { x.data = {...x.data, ...patch(x, fn)}; }); }

  // ---------------------------------------------------------- fixing a word as it is typed
  let on = store.get('bbFixTyping', true);
  const BOUNDARY = /[\s.,;:!?)\]}"\u2019']/;

  // Replaces the word just finished, through the browser's own editing commands, so Ctrl+Z
  // still takes it back the way any other typing would.
  function fixCaret() {
    if (!on) return;
    const s = getSelection();
    if (!s || !s.rangeCount) return;
    const r = s.getRangeAt(0);
    if (!r.collapsed || r.startContainer.nodeType !== 3) return;
    const node = r.startContainer, at = r.startOffset;
    const before = node.textContent.slice(0, at);
    const m = new RegExp('(' + WORD + ')(.)$').exec(before);
    if (!m || !BOUNDARY.test(m[2])) return;            // still in the middle of the word
    const good = lookup(m[1]);
    if (!good) return;
    const start = at - m[0].length;
    const pick = document.createRange();
    pick.setStart(node, start); pick.setEnd(node, start + m[1].length);
    s.removeAllRanges(); s.addRange(pick);
    document.execCommand('insertText', false, good);
    try { s.modify('move', 'forward', 'character'); } catch (_) {}   // step back past the space
    toast(m[1] + ' \u2192 ' + good);
  }

  // ---------------------------------------------------------- the wavy line under a typo
  const canMark = typeof Highlight === 'function' && window.CSS && CSS.highlights;
  function unmark() { if (canMark) CSS.highlights.delete('bb-typo'); }
  function mark(el) {
    if (!canMark || !el) return;
    const ranges = [], walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let n = walk.nextNode(); n; n = walk.nextNode()) {
      const re = new RegExp(WORD, 'g');
      for (let m = re.exec(n.textContent); m; m = re.exec(n.textContent)) {
        if (!lookup(m[0])) continue;
        const r = document.createRange();
        r.setStart(n, m.index); r.setEnd(n, m.index + m[0].length);
        ranges.push(r);
      }
    }
    if (ranges.length) CSS.highlights.set('bb-typo', new Highlight(...ranges));
    else unmark();
  }

  // Called from startEdit for anything that holds plain words.
  let soon;
  function watch(el) {
    if (!el) return;
    mark(el);
    el.addEventListener('input', ev => {
      if (ev.inputType === 'insertText' && ev.data && BOUNDARY.test(ev.data)) fixCaret();
      clearTimeout(soon); soon = setTimeout(() => mark(el), 300);
    });
  }

  // ---------------------------------------------------------- the panel
  const box = $('.ol-fix'), listEl = box.querySelector('.ol-fixl'), countEl = box.querySelector('.ol-fixn');
  const autoEl = box.querySelector('.ol-fixauto'), allEl = box.querySelector('.ol-fixall');
  let looking = [];                                    // the blocks this run is about

  function close() { box.hidden = true; R.focus({preventScroll: true}); }
  function open(ids) {
    looking = (ids && ids.length ? ids : [...S.blocks.keys()])
      .map(i => S.blocks.get(i)).filter(minds);
    autoEl.checked = on;
    box.hidden = false;
    draw();
  }
  function draw() {
    listEl.replaceChildren();
    let total = 0;
    const many = looking.length > 1;
    looking.forEach(b => {
      const live = S.blocks.get(b.id);
      if (!live) return;
      const list = finds(words(live));
      if (!list.length) return;
      total += list.reduce((a, f) => a + f.n, 0);
      if (many) listEl.appendChild(h('li', 'where', linkName(live)));
      list.forEach(f => {
        const li = listEl.appendChild(h('li'));
        const what = li.appendChild(h('div', 'what', f.what + (f.n > 1 ? '  \u00d7' + f.n : '')));
        what.appendChild(h('span', 'why', f.why));
        const go = li.appendChild(h('button', '', 'Fix'));
        go.onclick = () => { put(live, f.apply); sfx('tap'); draw(); };
      });
    });
    allEl.disabled = !total;
    countEl.textContent = total ? total + (total === 1 ? ' thing to look at' : ' things to look at') : '';
    if (!total) listEl.appendChild(h('li', 'none',
      looking.length ? 'Nothing to fix here. Good writing.' : 'Nothing with words in it yet.'));
  }
  function fixAll() {
    let n = 0;
    looking.forEach(b => {
      const live = S.blocks.get(b.id);
      if (!live) return;
      const list = finds(words(live));
      if (!list.length) return;
      n += list.reduce((a, f) => a + f.n, 0);
      put(live, t => list.reduce((text, f) => f.apply(text), t));
    });
    draw();
    toast(n ? 'Fixed ' + n : 'Nothing to fix');
    if (n) sfx('done', 1.2);
  }

  box.querySelector('.ol-fixx').onclick = close;
  allEl.onclick = fixAll;
  autoEl.onchange = () => { on = autoEl.checked; store.set('bbFixTyping', on); unmark(); };
  box.addEventListener('keydown', ev => { if (ev.key === 'Escape') { ev.stopPropagation(); close(); } });

  return {open, close, watch, unmark, minds, finds, words, lookup,
          get auto() { return on; },
          set auto(v) { on = !!v; autoEl.checked = on; store.set('bbFixTyping', on); if (!on) unmark(); },
          get shown() { return !box.hidden; }};
})();

// ================================================================ a copy kept on this computer
// The group board lives on the server, so without this there would be nothing on the laptop to
// fall back on. Both boards are copied the same way, under their own name.
const Backup = {
  name() { return S.mode === 'local' ? (Local.board === 'main' ? 'offline' : 'offline ' + Local.name()) : ((S.group && S.group.name) || groupId || 'group'); },
  last: 0,
  async take(force) {
    if (!api()?.board_backup) return false;
    const rows = [...S.blocks.values()];
    if (!rows.length && !force) return false;              // nothing worth keeping yet
    this.last = Date.now();
    try { return await api().board_backup(this.name(), rows, force ? 0 : 300); }
    catch (_) { return false; }
  },
  async list() {
    if (!api()?.board_backups) return [];
    try { return await api().board_backups(this.name()) || []; } catch (_) { return []; }
  },
  async read(file) {
    if (!api()?.board_backup_read) return null;
    try { return await api().board_backup_read(file); } catch (_) { return null; }
  },
};

// Everything that is waiting, written out now. Called when the window goes away, when it loses
// the screen, and every few minutes while you work.
function saveEverything(backupToo) {
  try { Sync.flushAll(); } catch (_) {}
  try { Local.mirror(); } catch (_) {}
  if (backupToo) Backup.take(false);
}
addEventListener('pagehide', () => saveEverything(true));
(window.bbBeforeLeave = window.bbBeforeLeave || []).push(async () => {
  saveEverything(false);
  if (S.mode === 'local' && api()?.ol_local_save) await api().ol_local_save([...S.blocks.values()], Local.board);
});
addEventListener('beforeunload', () => saveEverything(true));
addEventListener('blur', () => saveEverything(false));
document.addEventListener('visibilitychange', () => { if (document.hidden) saveEverything(true); });
setInterval(() => saveEverything(true), 120000);

// The app asks for this as the window closes and writes the file itself, which is the one way
// a save cannot be outrun by the window going away.
window.bbRows = () => {
  try { Sync.flushAll(); } catch (_) {}
  return {name: Backup.name(), mode: S.mode, board: Local.board, rows: [...S.blocks.values()]};
};

// Put a copy back. A group board is never written over from here - its copy is opened on the
// offline board instead, where it can be looked at without touching what the group has.
async function restoreBackup() {
  const list = await Backup.list();
  if (!list.length) return toast('No copies kept yet');
  const when = f => {
    const d = new Date(f.when);
    return f.latest ? 'The newest copy'
      : d.toLocaleDateString(undefined, {day: 'numeric', month: 'short'}) + ' \u00b7 ' +
        d.toLocaleTimeString(undefined, {hour: '2-digit', minute: '2-digit'});
  };
  ctxBuild([
    {head: 'copies of ' + Backup.name()},
    ...list.slice(0, 12).map(f => ({label: when(f), key: Math.round(f.size / 1024) + 'k',
                                    go: () => useBackup(f)})),
    {rule: 1},
    {label: 'Open the folder they are in', go: () => api()?.board_backup_folder && api().board_backup_folder()},
  ]);
  const r = $('.ol-switch').getBoundingClientRect();
  ctxOpen(r.left, r.bottom + 6, true);
}
async function useBackup(f) {
  const rows = await Backup.read(f.file);
  if (!Array.isArray(rows) || !rows.length) return toast('That copy is empty');
  const live = S.mode !== 'local';
  if (live && !confirm('This opens that copy on your offline board, so the group keeps what it has. Carry on?')) return;
  if (!live && !confirm('This puts the board back to that copy. What is on it now will be replaced. Carry on?')) return;
  if (live) await useLocal();
  await Backup.take(true);                       // keep what is there now before replacing it
  [...S.blocks.keys()].forEach(unpaint);
  S.blocks.clear(); S.sel.clear();
  rows.forEach(incoming);
  Local.now();
  emptyState(); drawLinks(); fit();
  toast('Put back from ' + (f.latest ? 'the newest copy' : f.when.replace('T', ' ')));
}

// ================================================================ the little map
// Everything on the board, shrunk into a corner, with a frame showing the part you are looking
// at. Click or drag it to go somewhere. It is drawn on a canvas so hundreds of blocks cost
// nothing to show.
const mapBox = $('.ol-map'), mapCv = mapBox.querySelector('canvas'), mapFrame = $('.ol-mapv');
const mapBtn = $('.ol-mapb');
let mapOn = store.get('olMap', false);
let mapFit = null;                       // how board coordinates map onto the canvas

function showMap(on) {
  mapOn = on;
  store.set('olMap', on);
  mapBox.hidden = !on;
  mapBtn.classList.toggle('on', on);
  mapBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
  if (on) drawMap();
}

function drawMap() {
  if (!mapOn || mapBox.hidden) return;
  const w = mapCv.width, hgt = mapCv.height, g = mapCv.getContext('2d');
  const list = [...S.blocks.values()].filter(b => b.kind !== 'arrow');
  g.clearRect(0, 0, w, hgt);
  const v = S.view, seen = {x0: -v.x / v.k, y0: -v.y / v.k,
                            x1: (board.clientWidth - v.x) / v.k, y1: (board.clientHeight - v.y) / v.k};
  const bb = bounds(list) || {x0: seen.x0, y0: seen.y0, x1: seen.x1, y1: seen.y1};
  // the part you are looking at always belongs on the map, even with nothing in it
  const x0 = Math.min(bb.x0, seen.x0) - 60, y0 = Math.min(bb.y0, seen.y0) - 60;
  const x1 = Math.max(bb.x1, seen.x1) + 60, y1 = Math.max(bb.y1, seen.y1) + 60;
  const k = Math.min(w / (x1 - x0), hgt / (y1 - y0));
  const ox = (w - (x1 - x0) * k) / 2, oy = (hgt - (y1 - y0) * k) / 2;
  mapFit = {x0, y0, k, ox, oy};
  const at = (x, y) => [ox + (x - x0) * k, oy + (y - y0) * k];
  const ink = getComputedStyle(R).getPropertyValue('--text').trim() || '#000';
  list.forEach(b => {
    const [px, py] = at(b.x, b.y);
    const pw = Math.max(2, b.w * k), ph = Math.max(2, b.h * k);
    const D = b.data || {};
    g.fillStyle = okColor(D.color) || okColor(D.bg) || (b.kind === 'shape' ? 'transparent' : ink);
    g.globalAlpha = b.kind === 'shape' ? 1 : .55;
    if (b.kind === 'shape') { g.strokeStyle = okColor(D.color) || ink; g.lineWidth = 1; g.strokeRect(px, py, pw, ph); }
    else g.fillRect(px, py, pw, ph);
    if (S.sel.has(b.id)) { g.globalAlpha = 1; g.strokeStyle = PAL[0]; g.lineWidth = 1.5; g.strokeRect(px, py, pw, ph); }
  });
  g.globalAlpha = 1;
  const [fx, fy] = at(seen.x0, seen.y0), [fx1, fy1] = at(seen.x1, seen.y1);
  Object.assign(mapFrame.style, {left: (6 + fx) + 'px', top: (6 + fy) + 'px',
    width: Math.max(6, fx1 - fx) + 'px', height: Math.max(6, fy1 - fy) + 'px'});
}

// clicking or dragging the map takes you there
function mapGo(ev) {
  if (!mapFit) return;
  const r = mapCv.getBoundingClientRect();
  const wx = mapFit.x0 + (ev.clientX - r.left - mapFit.ox) / mapFit.k;
  const wy = mapFit.y0 + (ev.clientY - r.top - mapFit.oy) / mapFit.k;
  const v = S.view;
  v.x = board.clientWidth / 2 - wx * v.k;
  v.y = board.clientHeight / 2 - wy * v.k;
  applyView();
}
mapCv.addEventListener('pointerdown', ev => { mapCv.setPointerCapture(ev.pointerId); mapGo(ev); });
mapCv.addEventListener('pointermove', ev => { if (ev.buttons & 1) mapGo(ev); });
mapBtn.onclick = () => showMap(!mapOn);
showMap(mapOn);

// ================================================================ the search box in the corner
// Always there, so finding something is typing rather than remembering a shortcut.
const hunt = $('.ol-huntq');
hunt.oninput = () => runFind(hunt.value);
hunt.onkeydown = ev => {
  if (ev.key === 'Enter') { ev.preventDefault(); stepFind(ev.shiftKey ? -1 : 1); }
  else if (ev.key === 'Escape') { ev.preventDefault(); closeFind(); R.focus({preventScroll: true}); }
  ev.stopPropagation();
};
$('.ol-huntx').onclick = () => stepFind(1);
$('.ol-huntp').onclick = () => stepFind(-1);

// ---------------------------------------------------------------- joining two blocks
// The arrow tool wants a drag. This is the other way in: say where the line starts, then
// click the block it goes to, with a line following the mouse in between.
let linkFrom = null;
function startLink(id) {
  linkFrom = id; select([id]);
  board.classList.add('linking');
  toast('Now click the block to join it to \u2014 Esc to stop');
}
function stopLink() {
  if (!linkFrom) return;
  linkFrom = null; board.classList.remove('linking'); draft.setAttribute('d', '');
}
// true when the click was used up making the line
function linkClick(id) {
  if (!linkFrom) return false;
  const from = linkFrom;
  stopLink();
  if (!id || id === from) { toast('Nothing joined'); return !!id; }
  add('arrow', 0, 0, {from, to: id}, {size: [0, 0], centre: false});
  return true;
}
board.addEventListener('pointermove', ev => {
  if (!linkFrom) return;
  const a = S.blocks.get(linkFrom); if (!a) return stopLink();
  const v = S.view, sc = q => `${q.x * v.k + v.x},${q.y * v.k + v.y}`, m = toWorld(ev);
  draft.setAttribute('d', `M${sc({x: a.x + a.w / 2, y: a.y + a.h / 2})} L${sc(m)}`);
  draft.style.stroke = '';
});

// ---------------------------------------------------------------- the right-click menu
// Right-click a block for what you can do with it, or empty space to put something there.
// Inside text being typed the browser's own menu is left alone, so copy, paste and spelling work.
const ctx = $('.ol-ctx');
const KIND_NAME = {note: 'note', sticky: 'sticky note', text: 'text', code: 'code', proj: 'project',
                   term: 'terminal output', todo: 'checklist', image: 'picture', link: 'link',
                   ink: 'drawing', arrow: 'line', shape: 'box'};
function closeCtx() {
  ctx.classList.remove('on'); ctx.replaceChildren();
  const sw = $('.ol-switch');
  if (sw && sw.getAttribute('aria-expanded') === 'true') { sw.setAttribute('aria-expanded', 'false'); sw.focus(); }
}

function ctxBuild(items) {
  ctx.replaceChildren();
  for (const it of items) {
    if (!it) continue;
    if (it.head) { ctx.appendChild(h('small', '', it.head)); continue; }
    if (it.rule) { ctx.appendChild(h('hr')); continue; }
    if (it.colours) {
      const row = ctx.appendChild(h('div', 'cols'));
      for (const c of (it.palette || STICKY)) {
        const sw = row.appendChild(h('i'));
        sw.style.background = c; sw.title = 'Colour';
        sw.onclick = () => { closeCtx(); it.colours(c); };
      }
      continue;
    }
    const b = ctx.appendChild(h('button', it.warn ? 'warn' : '', it.label));
    b.setAttribute('role', 'menuitem');
    if (it.key) b.appendChild(h('span', 'k', it.key));
    b.onclick = () => { closeCtx(); sfx('tap', .6); it.go(); };
  }
}

// Open at the pointer, then pull it back inside the board if it would hang off an edge.
// Arrow keys walk the menu, Enter picks, Escape closes and hands focus back.
ctx.setAttribute('role', 'menu');
function ctxKeys(ev) {
  const items = [...ctx.querySelectorAll('button')];
  if (!items.length) return false;
  const at = items.indexOf(document.activeElement);
  if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
    ev.preventDefault();
    const step = ev.key === 'ArrowDown' ? 1 : -1;
    items[(at + step + items.length) % items.length].focus();
  } else if (ev.key === 'Home') { ev.preventDefault(); items[0].focus(); }
  else if (ev.key === 'End') { ev.preventDefault(); items[items.length - 1].focus(); }
  else if (ev.key === 'Escape') { ev.preventDefault(); closeCtx(); }
  else if (ev.key === 'Tab') { ev.preventDefault(); closeCtx(); }
  else return false;                    // Enter and Space are the button's own business
  return true;
}
let ctxWas = null;          // where the last menu was opened, so the next one follows it
function ctxOpen(cx, cy, focusFirst) {
  ctxWas = {x: cx, y: cy};
  fpopClose();
  const r = board.getBoundingClientRect();
  ctx.classList.add('on');
  ctx.style.left = '0px'; ctx.style.top = '0px';
  const w = ctx.offsetWidth, hh = ctx.offsetHeight;
  ctx.style.left = clamp(cx - r.left, 6, Math.max(6, r.width - w - 6)) + 'px';
  ctx.style.top = clamp(cy - r.top, 6, Math.max(6, r.height - hh - 6)) + 'px';
  if (focusFirst !== false && $('.ol-switch').getAttribute('aria-expanded') === 'true')
    ctx.querySelector('button')?.focus();
}

board.addEventListener('contextmenu', ev => {
  const dragged = rightFrom && rightFrom.moved;
  rightFrom = null;
  if (dragged) { ev.preventDefault(); return; }         // that was a drag round the board
  // typing: leave the browser's own menu alone, it has copy, paste and spelling
  if (ev.target.closest('input,textarea,[contenteditable="true"],[contenteditable="plaintext-only"],.ol-ide')) return;
  ev.preventDefault();
  closeMenu();
  const lineEl = ev.target.closest('.ol-links path.hit');
  if (lineEl) {                                    // a joining line is not a block
    const b = S.blocks.get(lineEl.dataset.id);
    if (b) { select([b.id]); ctxBuild(linkItems(b)); ctxOpen(ev.clientX, ev.clientY); }
    return;
  }
  const blockEl = ev.target.closest('.ol-b');
  if (blockEl) {
    const id = blockEl.dataset.id, b = S.blocks.get(id);
    if (!b) return;
    if (!S.sel.has(id)) select([id]);          // right-clicking outside the selection picks that one
    ctxBuild(blockItems(b, [...S.sel]));
  } else {
    select([]);
    ctxBuild(spaceItems(toWorld(ev)));
  }
  ctxOpen(ev.clientX, ev.clientY);
}, true);

// Everything you can do to a line joining two blocks.
function linkItems(b) {
  const D = b.data || {}, set = patch => {
    change(b.id, x => { x.data = {...x.data, ...patch}; });
    if (!('from' in patch) && !('label' in patch)) rememberLine(S.blocks.get(b.id).data);
  };
  const ends = D.ends || 'to', form = D.form || 'curve';
  const pick = (label, on, patch) => ({label: (on ? '\u2713  ' : '\u00a0\u00a0\u00a0 ') + label, go: () => set(patch)});
  return [
    {head: 'line'},
    pick('Arrow one way', ends === 'to', {ends: 'to'}),
    pick('Arrows both ways', ends === 'both', {ends: 'both'}),
    pick('Plain line, no arrow', ends === 'none', {ends: 'none'}),
    {rule: 1},
    pick('Curved', form === 'curve', {form: 'curve'}),
    pick('Straight', form === 'straight', {form: 'straight'}),
    pick('Right angle', form === 'elbow', {form: 'elbow'}),
    {rule: 1},
    pick('Dashed', !!D.dash, {dash: !D.dash}),
    pick('Thicker', !!D.thick, {thick: !D.thick}),
    {label: D.label ? 'Change the label\u2026' : 'Write on the line\u2026', go: () => {
      const v = prompt('A word or two for the middle of the line', D.label || '');
      if (v !== null) set({label: v.trim().slice(0, 60)}); }},
    D.label && {label: 'Take the label off', go: () => set({label: ''})},
    {colours: c => set({color: c}), palette: PAL},
    {label: 'Back to the plain colour', go: () => set({color: null})},
    {rule: 1},
    {rule: 1},
    {label: 'New lines go back to the usual look', go: () => {
      store.set('olLine', null); toast('New lines: right angle, no arrow, dashed'); }},
    {rule: 1},
    {label: 'Swap which end points', go: () => set({from: D.to, to: D.from})},
    {label: 'Delete the line', key: 'Del', warn: 1, go: () => remove([b.id])},
  ];
}

// The blocks that carry a name of their own, and where it lives.
const TITLED = {code: 'title', proj: 'title', term: 'title', todo: 'title', link: 'title', shape: 'text'};
function renameBlock(b) {
  const field = TITLED[b.kind];
  if (!field) return toast('That one has no name \u2014 press Enter to edit what is in it');
  const was = (b.data || {})[field] || '';
  const v = prompt('A name for this ' + (KIND_NAME[b.kind] || 'block'), was);
  if (v === null) return;
  change(b.id, x => { x.data = {...x.data, [field]: v.trim().slice(0, 80)}; });
  sfx('tap');
}

// Which blocks can be dressed up, and in what.
const CAN_SIZE = ['note', 'sticky', 'text', 'todo', 'shape', 'link', 'code', 'term', 'proj'];
const CAN_STYLE = ['note', 'sticky', 'text', 'todo', 'shape', 'link'];
const baseSize = b => b.kind === 'text' ? (b.data.size || 40)
  : (b.data.fs || (b.kind === 'sticky' ? 16 : b.kind === 'code' || b.kind === 'term' ? 12.5 : b.kind === 'proj' ? 11.5 : 15));
const sizePatch = (b, px) => b.kind === 'text' ? {size: px} : {fs: px};

// Everything about how a block looks, in its own menu so the main one stays short. It works on
// however many are picked out, so a whole group can be changed at once.
function lookMenu(ids, at) {
  const list = ids.map(i => S.blocks.get(i)).filter(Boolean);
  const first = list[0];
  if (!first) return;
  const D = first.data || {};
  const many = list.length > 1;
  const styleable = list.filter(b => CAN_STYLE.includes(b.kind));
  const sizeable = list.filter(b => CAN_SIZE.includes(b.kind));
  const each = (blocks, fn) => blocks.forEach(b => change(b.id, x => { x.data = {...x.data, ...fn(x)}; }));
  const tick = on => on ? '\u2713  ' : '\u00a0\u00a0\u00a0 ';
  const step = by => each(sizeable, b => sizePatch(b, Math.round(clamp(baseSize(b) * by, 9, 160))));

  const items = [
    {head: many ? list.length + ' blocks' : 'how it looks'},
    sizeable.length && {label: 'Bigger text', key: 'Ctrl +', go: () => step(1.2)},
    sizeable.length && {label: 'Smaller text', key: 'Ctrl \u2212', go: () => step(1 / 1.2)},
    sizeable.length && {label: 'The usual size', go: () => each(sizeable, b =>
      b.kind === 'text' ? {size: 40} : {fs: null})},
  ];
  if (styleable.length) {
    items.push({rule: 1}, {head: 'lettering'});
    Object.keys(FONTS).forEach(f => items.push({
      label: tick((D.font || 'plain') === f) + FONT_NAME[f],
      go: () => each(styleable, () => ({font: f === 'plain' ? null : f})),
    }));
    items.push({rule: 1});
    items.push({label: tick(!!D.bold) + 'Bold', key: 'Ctrl B', go: () => each(styleable, b => ({bold: !b.data.bold}))});
    items.push({label: tick(!!D.italic) + 'Slanted', key: 'Ctrl I', go: () => each(styleable, b => ({italic: !b.data.italic}))});
    if (list.some(b => b.kind === 'text'))
      items.push({label: tick(D.caps !== false) + 'ALL CAPITALS',
                  go: () => each(list.filter(b => b.kind === 'text'), b => ({caps: b.data.caps === false}))});
    items.push({rule: 1}, {head: 'lined up'});
    ['left', 'centre', 'right'].forEach(a => items.push({
      label: tick((D.align || 'left') === a) + 'To the ' + a,
      go: () => each(styleable, () => ({align: a === 'left' ? null : a})),
    }));
    items.push({rule: 1}, {head: 'colour of the writing'});
    items.push({colours: c => each(styleable, () => ({tcolor: c})), palette: PAL});
    items.push({label: 'Back to the usual colour', go: () => each(styleable, () => ({tcolor: null}))});
    const backable = styleable.filter(b => ['note', 'todo', 'link', 'text'].includes(b.kind));
    if (backable.length) {
      items.push({rule: 1}, {head: 'colour behind it'});
      items.push({colours: c => each(backable, () => ({bg: c})), palette: PAPER});
      items.push({label: 'No colour behind it', go: () => each(backable, () => ({bg: null}))});
    }
  }
  if (!many && first.kind === 'sticky')
    items.push({rule: 1}, {head: 'the note itself'}, {colours: c => each(list, () => ({color: c})), palette: STICKY});
  items.push({rule: 1}, {label: 'Back to how it started', warn: 1, go: () => each(list, () => ({
    font: null, bold: null, italic: null, align: null, tcolor: null, bg: null, fs: null, caps: null,
    ul: null, strike: null, op: null, rad: null, bd: null, bw: null, shadow: null, glow: null, lh: null, ls: null,
    ...(first.kind === 'text' ? {size: 40} : {}),
  }))});
  ctxBuild(items);
  ctxOpen(at ? at.x : innerWidth / 2, at ? at.y : 120, true);
}

function blockItems(b, sel) {
  const many = sel.length > 1, coding = b.kind === 'code' || b.kind === 'proj';
  const plain = ['image', 'ink', 'arrow'].includes(b.kind);
  const text = () => {
    try { return b.kind === 'term' || b.kind === 'code' ? noAnsi(b.data.text || '') : blockText(b); }
    catch (_) { return ''; }
  };
  const D = b.data || {}, lock = locked(b);
  const shape = b.kind === 'shape';
  const setD = patch => change(b.id, x => { x.data = {...x.data, ...patch}; });
  return [
    {head: many ? sel.length + ' blocks' : (KIND_NAME[b.kind] || b.kind)},
    !many && !lock && TITLED[b.kind] && {label: 'Rename\u2026', key: 'F2', go: () => renameBlock(b)},
    !many && !lock && {label: 'Draw a line from here', key: 'A', go: () => startLink(b.id)},
    !many && coding && {label: 'Open in the editor', key: 'Enter', go: () => startEdit(b.id)},
    !many && coding && {label: 'Run it', key: 'F5', go: () => boardRun(b)},
    !many && !coding && !plain &&
      {label: b.kind === 'todo' ? 'Edit the list' : 'Edit the text', key: 'Enter', go: () => startEdit(b.id)},
    !many && b.kind === 'todo' && {label: 'Add an item', go: () => addItem(b)},
    !many && b.kind === 'link' && {label: 'Open the link', go: () => {
      const u = okUrl(b.data.url);
      if (u) (api()?.open_inside ? api().open_inside(u, b.data.title || u) : window.open(u));
      else toast('No link in there yet'); }},
    (CAN_STYLE.includes(b.kind) || CAN_SIZE.includes(b.kind)) &&
      {label: 'How it looks…', key: 'Ctrl L', go: () => lookMenu(sel, ctxWas)},
    !many && Writing.minds(b) && {label: 'Check the writing\u2026', key: 'F7', go: () => Writing.open([b.id])},
    sel.some(i => ['code', 'proj', 'term'].includes(S.blocks.get(i)?.kind)) &&
      {label: many ? 'Save their code\u2026' : b.kind === 'proj' ? 'Save as a zip\u2026' : 'Save as a file\u2026',
       go: () => exportCode(sel.map(i => S.blocks.get(i)).filter(Boolean))},
    !many && !!text() && {label: 'Copy the text',
      go: () => navigator.clipboard.writeText(text()).then(() => toast('Copied'))},
    {rule: 1},
    {label: many ? 'Copy them' : 'Copy', key: 'Ctrl C', go: () => {
      const list = sel.map(i => S.blocks.get(i)).filter(Boolean);
      navigator.clipboard.writeText('bb-online:' + JSON.stringify(list));
      toast(`Copied ${list.length} block${list.length > 1 ? 's' : ''}`); }},
    {label: many ? 'Duplicate them' : 'Duplicate', key: 'Ctrl D', go: () => duplicate(sel, 24)},
    {label: 'Bring to the front', go: () => toFront(sel)},
    {label: 'Send to the back', go: () => toBack(sel)},
    !many && b.kind === 'sticky' && {colours: c => setD({color: c})},
    !many && shape && {rule: 1},
    !many && shape && {label: (D.form === 'ellipse' ? '\u2713  ' : '\u00a0\u00a0\u00a0 ') + 'Make it a circle',
                       go: () => setD({form: D.form === 'ellipse' ? 'box' : 'ellipse'})},
    !many && shape && {label: (D.fill ? '\u2713  ' : '\u00a0\u00a0\u00a0 ') + 'Shade it in',
                       go: () => setD({fill: !D.fill})},
    !many && shape && {label: (D.dash ? '\u2713  ' : '\u00a0\u00a0\u00a0 ') + 'Dashed edge',
                       go: () => setD({dash: !D.dash})},
    !many && shape && {label: 'Tidy what is in here', go: () => tidyInside(b)},
    !many && shape && {label: 'Pick out what is in here', go: () => {
      const list = inside(b); if (!list.length) return toast('Nothing in this one yet');
      select([b.id, ...list.map(x => x.id)]); }},
    !many && shape && {colours: c => setD({color: c}), palette: PAL},
    many && {rule: 1},
    many && {head: 'line them up'},
    many && {label: 'Down the left', go: () => tidy(sel, 'left')},
    many && {label: 'Along the top', go: () => tidy(sel, 'top')},
    many && {label: 'Centred across', go: () => tidy(sel, 'middle')},
    many && {label: 'Spread out in a row', go: () => tidy(sel, 'row')},
    many && {label: 'Spread out in a column', go: () => tidy(sel, 'column')},
    {rule: 1},
    {label: lock ? 'Unlock it' : (many ? 'Lock them in place' : 'Lock it in place'),
     go: () => { sel.forEach(i => change(i, x => { x.data = {...x.data, lock: !lock}; }));
                 toast(lock ? 'Unlocked' : 'Locked \u2014 right-click to unlock'); }},
    {label: many ? 'Show just these' : 'Show just this one', key: 'F',
     go: () => fit(sel.map(i => S.blocks.get(i)).filter(Boolean))},
    {label: many ? `Delete ${sel.length} blocks` : 'Delete', key: 'Del', warn: 1, go: () => remove(sel)},
  ];
}

// ---------------------------------------------------------------- the style bar
// Pick something out and a bar of how it looks floats just above it, so nothing needs hunting
// for in the right-click menu. It works on the whole selection at once, and one undo takes
// back one click whatever it changed.
const fmt = h('div', 'ol-fmt glass'), fpop = h('div', 'ol-fmtpop glass');
fmt.setAttribute('role', 'toolbar'); fmt.setAttribute('aria-label', 'How it looks');
R.append(fmt, fpop);
const LOOK_KEYS = ['font', 'bold', 'italic', 'ul', 'strike', 'align', 'tcolor', 'bg', 'fs', 'op', 'rad', 'bd', 'bw',
                   'shadow', 'glow', 'lh', 'ls', 'caps'];
const BACKED = ['note', 'todo', 'link', 'text'];
const FMT_ICON = {
  left: 'M4 6h16M4 12h10M4 18h14', centre: 'M4 6h16M7 12h10M5 18h14', right: 'M4 6h16M10 12h10M6 18h14',
  box: 'M7 4h10a3 3 0 0 1 3 3v10a3 3 0 0 1-3 3H7a3 3 0 0 1-3-3V7a3 3 0 0 1 3-3z',
  circle: 'M12 4a8 8 0 1 0 0 16a8 8 0 1 0 0-16z',
  to: 'M4 12h15M14 7l5 5-5 5', both: 'M5 12h14M9 7l-5 5 5 5M15 7l5 5-5 5', none: 'M4 12h16',
  curve: 'M4 18Q12 0 20 18', straight: 'M4 20 20 4', elbow: 'M4 18h8V6h8',
  dash: 'M3 12h4M10 12h4M17 12h4', dots: 'M4 12h.01M9 12h.01M14 12h.01M19 12h.01', plain: 'M4 12h16',
  brush: 'M15 4l5 5-7 7-5-5zM8 11l-4 9 9-4', paste: 'M9 3h6v4H9zM7 5H5v16h14V5h-2',
  lock: 'M7 11V8a5 5 0 0 1 10 0v3M5 11h14v10H5z', unlock: 'M7 11V8a5 5 0 0 1 9.6-2M5 11h14v10H5z',
  reset: 'M4 12a8 8 0 1 0 3-6.2M4 4v4h4', more: 'M5 12h.01M12 12h.01M19 12h.01',
  star: 'M12 3l2.6 5.6 6 .7-4.5 4.1 1.2 6L12 16.4 6.7 19.4l1.2-6L3.4 9.3l6-.7z',
};
const svgIcon = (k, w) => `<svg viewBox="0 0 24 24" aria-hidden="true"${w ? ` style="stroke-width:${w}"` : ''}><path d="${FMT_ICON[k]}"/></svg>`;
var fmtRaf = 0, fmtKey = '', fmtLook = null;
const fmtOn = () => store.get('olFmt', true) !== false;

// Change these blocks in one go, as one undo step.
function restyle(blocks, fn) {
  const steps = [];
  const lines = blocks.filter(b => b.kind === 'arrow');
  blocks.forEach(b => {
    if (locked(b) && b.kind !== 'arrow') return;
    const before = snap(b);
    b.data = {...b.data, ...fn(b)};
    steps.push({before, after: snap(b)});
    put(b);
    if (S.editing === b.id) dress(b, node(b));      // the text being typed in is left alone, but its look still changes
  });
  record(steps);
  if (lines.length) rememberLine(lines[0].data);       // new lines follow the last one you changed
  fmtSoon(true);
}

function fmtSoon(rebuild) {
  if (rebuild) fmtKey = '';
  if (fmtRaf) return;
  fmtRaf = requestAnimationFrame(() => { fmtRaf = 0; fmtSync(); });
}
function fmtHide() { fmt.classList.remove('on'); fpopClose(); fmtKey = ''; }
function fmtSync() {
  const list = [...S.sel].map(i => S.blocks.get(i)).filter(Boolean);
  const ed = S.editing && S.blocks.get(S.editing);
  if (!fmtOn() || !list.length || !R.offsetParent || (drag && (drag.moved || drag.kind !== 'move'))
      || board.classList.contains('panning') || (ed && (ed.kind === 'code' || ed.kind === 'proj'))
      || document.querySelector('.ol-ide.on')) return fmtHide();
  const key = list.map(b => b.id + ':' + b.updated_at).join(',');
  if (key !== fmtKey && !fmt.contains(document.activeElement)) { fmtKey = key; fmtBuild(list); }
  fmtPlace(list);
}

// Sit above what is picked out, or below it when that is off the top, and never off the board.
function fmtPlace(list) {
  const rr = R.getBoundingClientRect(), f = rr.width / R.offsetWidth || 1;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  list.forEach(b => {
    const el = b.kind === 'arrow' ? links.querySelector(`path.hit[data-id="${b.id}"]`) : S.nodes.get(b.id);
    if (!el) return;
    const r = el.getBoundingClientRect();
    x0 = Math.min(x0, r.left); y0 = Math.min(y0, r.top); x1 = Math.max(x1, r.right); y1 = Math.max(y1, r.bottom);
  });
  if (x0 === Infinity) return fmtHide();
  const W = R.clientWidth, H = R.clientHeight;
  const L = (x0 - rr.left) / f - R.clientLeft, T = (y0 - rr.top) / f - R.clientTop;
  const Rt = (x1 - rr.left) / f - R.clientLeft, B = (y1 - rr.top) / f - R.clientTop;
  if (B < 0 || T > H || Rt < 0 || L > W) { fmt.classList.remove('on'); fpopClose(); return; }
  fmt.classList.add('on');
  const w = fmt.offsetWidth;
  // along the top of the board, in the middle; with the message box open, in the middle of what's left of it,
  // or along the bottom when that's too narrow, so neither covers the other
  const chat = $('.ol-chat'), room = chat.hidden ? W : W - chat.offsetWidth - 24;
  if (w + 16 <= room) { fmt.style.left = Math.max(8, (room - w) / 2) + 'px'; fmt.style.top = '62px'; }
  else { fmt.style.left = Math.max(8, (W - w) / 2) + 'px'; fmt.style.top = Math.max(62, H - fmt.offsetHeight - 76) + 'px'; }
  if (fpop.classList.contains('on')) fpopPlace();
}

// A colour picker: the palette, any colour at all, the ones picked lately, and the usual.
function swatches(el, label, colours, cur, pick, noneLabel) {
  el.appendChild(h('small', '', label));
  const grid = el.appendChild(h('div', 'sw'));
  const recent = store.get('olRecent', []).filter(okColor);
  const all = [...colours, ...recent.filter(c => !colours.includes(c))];
  all.forEach(c => {
    const i = grid.appendChild(h('i'));
    i.style.background = c; i.title = c;
    if ((cur || '').toLowerCase() === c.toLowerCase()) i.classList.add('on');
    i.onclick = () => pick(c);
  });
  const row = el.appendChild(h('div', 'row'));
  const any = row.appendChild(h('input'));
  any.type = 'color'; any.value = okColor(cur) || '#d71921'; any.title = 'Any colour';
  any.onchange = () => {
    store.set('olRecent', [any.value, ...recent.filter(c => c !== any.value)].slice(0, 8));
    pick(any.value);
  };
  row.appendChild(h('span', '', 'Any colour'));
  if (noneLabel) {
    const n = row.appendChild(h('button', cur ? '' : 'on', noneLabel));
    n.style.marginLeft = 'auto'; n.onclick = () => pick(null);
  }
}
// A row of choices, one of them lit.
function choices(el, label, opts, cur, pick) {
  el.appendChild(h('small', '', label));
  const row = el.appendChild(h('div', 'row'));
  opts.forEach(([v, name]) => {
    const b = row.appendChild(h('button', (cur ?? null) === v ? 'on' : '', name));
    b.onclick = () => pick(v);
  });
}

function fpopClose() { fpop.classList.remove('on'); fpop.replaceChildren(); fpop._k = null; }
function fpopPlace() {
  const a = fmt.querySelector(`[data-k="${fpop._k}"]`);
  if (!a) return fpopClose();
  const W = R.clientWidth, H = R.clientHeight, w = fpop.offsetWidth, hh = fpop.offsetHeight;
  const ax = a.offsetLeft + fmt.offsetLeft, ay = fmt.offsetTop;
  let top = ay + fmt.offsetHeight + 8;
  if (top + hh > H - 8) top = Math.max(8, ay - hh - 8);
  fpop.style.left = clamp(ax + a.offsetWidth / 2 - w / 2, 8, Math.max(8, W - w - 8)) + 'px';
  fpop.style.top = top + 'px';
}
// Open a panel under one of the bar's buttons; the same button again shuts it.
function fpopOpen(k, build) {
  if (fpop._k === k) return fpopClose();
  fpop._k = k; fpop._build = build;
  fpop.replaceChildren(); build(fpop);
  fpop.classList.add('on'); fpopPlace();
}

function fmtBuild(list) {
  fmt.replaceChildren();
  const first = list[0], D = first.data || {};
  const blocks = list.filter(b => b.kind !== 'arrow');
  const lines = list.filter(b => b.kind === 'arrow');
  const styleable = list.filter(b => CAN_STYLE.includes(b.kind));
  const sizeable = list.filter(b => CAN_SIZE.includes(b.kind));
  const shapes = list.filter(b => b.kind === 'shape');
  const inks = list.filter(b => b.kind === 'ink');
  const boxy = blocks.filter(b => b.kind !== 'ink');
  const SD = (styleable[0] || first).data || {};
  let any = false;
  const sep = () => { if (any) fmt.appendChild(h('i', 'sep')); any = true; };
  const btn = (html, title, on, go, k) => {
    const b = fmt.appendChild(h('button', on ? 'on' : ''));
    b.innerHTML = html; b.title = title; b.setAttribute('aria-label', title.replace(/\s*\(.*\)$/, ''));
    if (on != null) b.setAttribute('aria-pressed', on ? 'true' : 'false');
    if (k) { b.dataset.k = k; b.setAttribute('aria-haspopup', 'true'); }
    b.onclick = () => { sfx('tap', .5); go(b); };
    return b;
  };
  const chip = c => `<span class="chip${c ? '' : ' none'}" style="background:${c || 'transparent'}"></span>`;
  // after a change in a panel, draw the panel again so it shows what is now picked
  const keep = fn => v => { fn(v); requestAnimationFrame(() => { if (fpop._k && fpop._build) {
    fpop.replaceChildren(); fpop._build(fpop); fpopPlace(); } }); };
  const shut = fn => v => { fn(v); fpopClose(); };

  if (styleable.length) {
    sep();
    const sel = fmt.appendChild(h('select'));
    sel.title = 'Lettering';
    Object.keys(FONTS).forEach(f => { const o = sel.appendChild(h('option', '', FONT_NAME[f])); o.value = f;
      if (FONTS[f]) o.style.fontFamily = FONTS[f]; });
    sel.value = SD.font || 'plain';
    sel.onchange = () => { restyle(styleable, () => ({font: sel.value === 'plain' ? null : sel.value})); sel.blur(); };
  }
  if (sizeable.length) {
    const sb = sizeable[0], step = by => restyle(sizeable, b => sizePatch(b, Math.round(clamp(baseSize(b) * by, 9, 160))));
    btn('<span style="font-size:15px;line-height:1">−</span>', 'Smaller text (Ctrl −)', null, () => step(1 / 1.15));
    const num = fmt.appendChild(h('input', 'num'));
    num.type = 'number'; num.min = 9; num.max = 160; num.value = Math.round(baseSize(sb)); num.title = 'Text size';
    num.onchange = () => { const v = clamp(Math.round(+num.value) || baseSize(sb), 9, 160);
      num.blur(); restyle(sizeable, b => sizePatch(b, v)); };
    num.onkeydown = ev => { if (ev.key === 'Enter') num.onchange(); if (ev.key === 'Escape') num.blur(); ev.stopPropagation(); };
    btn('<span style="font-size:15px;line-height:1">+</span>', 'Bigger text (Ctrl +)', null, () => step(1.15));
  }
  if (styleable.length) {
    sep();
    btn('<b>B</b>', 'Bold (Ctrl B)', !!SD.bold, () => restyle(styleable, () => ({bold: !SD.bold || null})));
    btn('<i>I</i>', 'Slanted (Ctrl I)', !!SD.italic, () => restyle(styleable, () => ({italic: !SD.italic || null})));
    btn('<u>U</u>', 'Underlined (Ctrl U)', !!SD.ul, () => restyle(styleable, () => ({ul: !SD.ul || null})));
    btn('<s>S</s>', 'Crossed out', !!SD.strike, () => restyle(styleable, () => ({strike: !SD.strike || null})));
    const al = SD.align || 'left', nextAl = {left: 'centre', centre: 'right', right: 'left'}[al];
    btn(svgIcon(al), 'Lined up to the ' + al + ' (click for ' + nextAl + ')', null,
        () => restyle(styleable, () => ({align: nextAl === 'left' ? null : nextAl})));
    btn('<span style="font-size:13px;font-weight:800;line-height:1">A</span>' + chip(okColor(SD.tcolor) || 'var(--text)'),
        'Colour of the writing', null, () => fpopOpen('tc', el => swatches(el, 'colour of the writing', PAL, SD.tcolor,
          shut(c => restyle(styleable, () => ({tcolor: c}))), 'The usual')), 'tc');
    btn('<span style="font-size:12px;line-height:1">Aa</span>', 'Spacing and capitals', null, () => fpopOpen('sp', el => {
      const XD = ((styleable[0] || first).data) || {};
      choices(el, 'gap between lines', [['tight', 'Close'], [null, 'Usual'], ['airy', 'Airy']], XD.lh || null,
              keep(v => restyle(styleable, () => ({lh: v}))));
      choices(el, 'gap between letters', [['tight', 'Close'], [null, 'Usual'], ['wide', 'Wide']], XD.ls || null,
              keep(v => restyle(styleable, () => ({ls: v}))));
      const texts = styleable.filter(b => b.kind === 'text');
      if (texts.length) choices(el, 'big text', [[null, 'ALL CAPITALS'], [false, 'As typed']],
        texts[0].data.caps === false ? false : null, keep(v => restyle(texts, () => ({caps: v}))));
    }), 'sp');
  }

  // what fills the block: paper for notes, the note itself for stickies, a tint for boxes
  const fillers = blocks.filter(b => BACKED.includes(b.kind) || b.kind === 'sticky');
  if (fillers.length || shapes.length) {
    sep();
    if (fillers.length) {
      const fb = fillers[0], sticky = fb.kind === 'sticky';
      const cur = sticky ? okColor(fb.data.color) || STICKY[0] : okColor(fb.data.bg);
      btn(chip(cur) + '<span class="lbl">Fill</span>', 'Colour behind it', null, () => fpopOpen('bg', el =>
        swatches(el, sticky ? 'colour of the note' : 'colour behind it', sticky ? STICKY : [...PAPER, ...PAL.slice(0, 8)], cur,
          shut(c => restyle(fillers, b => b.kind === 'sticky' ? {color: c || STICKY[0]} : {bg: c})),
          sticky ? null : 'None')), 'bg');
    }
    if (shapes.length) {
      const sd = shapes[0].data || {};
      btn(chip(okColor(sd.color) || 'var(--text)') + '<span class="lbl">Box</span>', 'Colour of the box', null,
          () => fpopOpen('sh', el => swatches(el, 'colour of the box', PAL, sd.color,
            shut(c => restyle(shapes, () => ({color: c}))), 'The usual')), 'sh');
      btn(svgIcon(sd.form === 'ellipse' ? 'circle' : 'box'), sd.form === 'ellipse' ? 'Circle (click for a box)' : 'Box (click for a circle)',
          null, () => restyle(shapes, () => ({form: sd.form === 'ellipse' ? 'box' : 'ellipse'})));
      const edge = sd.dots ? 'dots' : sd.dash ? 'dash' : 'plain';
      const nextEdge = {plain: 'dash', dash: 'dots', dots: 'plain'}[edge];
      btn(svgIcon(edge, sd.thick ? 3.4 : 2), 'Edge: ' + {plain: 'solid', dash: 'dashed', dots: 'dotted'}[edge] + ' (right-click for thick)',
          null, () => restyle(shapes, () => ({dash: nextEdge === 'dash' || null, dots: nextEdge === 'dots' || null})))
        .oncontextmenu = ev => { ev.preventDefault(); restyle(shapes, () => ({thick: !sd.thick || null})); };
      const fill = sd.fill === 'solid' ? 'solid' : sd.fill ? 'light' : 'none';
      const nextFill = {none: 'light', light: 'solid', solid: 'none'}[fill];
      btn('<span class="lbl">' + {none: 'Empty', light: 'Tinted', solid: 'Shaded'}[fill] + '</span>', 'Inside the box (click to change)', null,
          () => restyle(shapes, () => ({fill: nextFill === 'none' ? false : nextFill === 'solid' ? 'solid' : true})));
    }
  }

  if (lines.length) {
    sep();
    const ld = lines[0].data || {}, ends = ld.ends || 'to', form = ld.form || 'curve';
    const nextEnds = {to: 'both', both: 'none', none: 'to', from: 'both'}[ends];
    const nextForm = {curve: 'straight', straight: 'elbow', elbow: 'curve'}[form];
    btn(svgIcon(ends === 'from' ? 'to' : ends), 'Arrow heads (click to change)', null, () => restyle(lines, () => ({ends: nextEnds})));
    btn(svgIcon(form), 'Line shape: ' + form + ' (click to change)', null, () => restyle(lines, () => ({form: nextForm})));
    btn(svgIcon('dash'), 'Dashed', !!ld.dash, () => restyle(lines, () => ({dash: !ld.dash})));
    btn(svgIcon('plain', 4), 'Thicker', !!ld.thick, () => restyle(lines, () => ({thick: !ld.thick})));
    btn(chip(okColor(ld.color) || 'var(--text)'), 'Colour of the line', null, () => fpopOpen('ln', el =>
      swatches(el, 'colour of the line', PAL, ld.color, shut(c => restyle(lines, () => ({color: c}))), 'The usual')), 'ln');
    if (lines.length === 1)
      btn('<span class="lbl">' + (ld.label ? 'Label' : '+ Label') + '</span>', 'Write on the line', null, () => {
        const v = prompt('A word or two for the middle of the line', ld.label || '');
        if (v !== null) restyle(lines, () => ({label: v.trim().slice(0, 60)})); });
  }

  if (inks.length) {
    sep();
    const idd = inks[0].data || {}, w = clamp(idd.width || 3, 1, 20);
    btn(chip(okColor(idd.color) || 'var(--text)'), 'Colour of the drawing', null, () => fpopOpen('ink', el =>
      swatches(el, 'colour of the drawing', PAL, idd.color, shut(c => restyle(inks, () => ({color: c}))), 'Black/white')), 'ink');
    const nextW = w < 3 ? 3 : w < 6 ? 6 : w < 10 ? 10 : 2;
    btn(svgIcon('plain', clamp(w, 1, 6)), 'Pen width ' + w + ' (click to change)', null, () => restyle(inks, () => ({width: nextW})));
  }

  // the block as a whole: corners, edge, shadow and how see-through it is
  if (blocks.length) {
    sep();
    btn(svgIcon('box') + '<span class="lbl">Style</span>', 'Corners, edge, shadow and see-through', null, () => fpopOpen('box', el => {
      const BD = (boxy[0] || blocks[0]).data || {};
      if (boxy.length) {
        choices(el, 'corners', [['square', 'Sharp'], ['soft', 'Soft'], [null, 'Usual'], ['round', 'Round'], ['pill', 'Very round']],
                BD.rad || null, keep(v => restyle(boxy, () => ({rad: v}))));
        const edged = boxy.filter(b => b.kind !== 'shape');
        if (edged.length) {
          swatches(el, 'edge colour', PAL, BD.bd, keep(c => restyle(edged, () => ({bd: c}))), 'Usual');
          if (okColor(BD.bd)) choices(el, 'edge thickness', [[null, 'Thin'], [2, 'Medium'], [4, 'Thick']], BD.bw || null,
                                      keep(v => restyle(edged, () => ({bw: v}))));
          choices(el, 'shadow', [[null, 'Usual'], ['flat', 'None'], ['lifted', 'Lifted'], ['glow', 'Glow']], BD.shadow || null,
                  keep(v => restyle(edged, () => ({shadow: v}))));
          if (BD.shadow === 'glow') swatches(el, 'glow colour', PAL.slice(0, 12), BD.glow,
                                             keep(c => restyle(edged, () => ({glow: c}))), 'Red');
        }
      }
      el.appendChild(h('small', '', 'see-through'));
      const row = el.appendChild(h('div', 'row')), r = row.appendChild(h('input')), out = row.appendChild(h('span'));
      r.type = 'range'; r.min = 15; r.max = 100; r.step = 5;
      r.value = Math.round((BD.op == null ? 1 : BD.op) * 100); out.textContent = r.value + '%';
      // show it while sliding, but only save it once you let go
      r.oninput = () => { out.textContent = r.value + '%';
        blocks.forEach(b => S.nodes.get(b.id)?.style.setProperty('--op', r.value / 100)); };
      r.onchange = () => restyle(blocks, () => ({op: +r.value >= 100 ? null : +r.value / 100}));
    }), 'box');
  }

  // copy one block's look onto others, and make it the look new ones of that kind start with
  sep();
  const lookOf = b => Object.fromEntries(LOOK_KEYS.map(k => [k, (b.data || {})[k] ?? null]));
  btn(svgIcon('brush'), 'Copy this look', null, () => { fmtLook = lookOf(styleable[0] || blocks[0] || first);
    toast('Look copied — pick something else and press paste'); fmtSoon(true); });
  if (fmtLook) btn(svgIcon('paste'), 'Paste the copied look', null, () => {
    restyle(blocks, b => { const L = {...fmtLook}; if (b.kind !== 'text') delete L.caps; return L; });
    toast('Look pasted'); });
  if (list.length === 1 && blocks.length === 1 && first.kind !== 'image') {
    const kinds = store.get('olLooks', {}), mine = !!kinds[first.kind];
    const many_ = n => /(x|s|sh|ch)$/.test(n) ? n + 'es' : n + 's', kn = many_(KIND_NAME[first.kind] || first.kind);
    btn(svgIcon('star'), mine ? 'New ' + kn + ' start like this one (click to stop)'
                              : 'Start new ' + kn + ' looking like this one', mine, () => {
      const all = store.get('olLooks', {});
      if (mine) delete all[first.kind]; else all[first.kind] = Object.fromEntries(Object.entries(lookOf(first)).filter(([, v]) => v != null));
      store.set('olLooks', all);
      toast(mine ? 'New ones go back to the usual look' : 'New ' + kn + ' will look like this');
      fmtSoon(true); });
  }
  btn(svgIcon('reset'), 'Back to how it started', null, () => restyle(blocks, b => ({
    ...Object.fromEntries(LOOK_KEYS.map(k => [k, null])), ...(b.kind === 'text' ? {size: 40} : {}) })));
  const lock = locked(first);
  btn(svgIcon(lock ? 'lock' : 'unlock'), lock ? 'Locked (click to unlock)' : 'Lock in place', lock, () => {
    const steps = [];
    list.forEach(b => { const before = snap(b); b.data = {...b.data, lock: !lock}; steps.push({before, after: snap(b)}); put(b); });
    record(steps); fmtSoon(true); });
  btn(svgIcon('more', 3.2), 'Everything else', null, b => {
    const r = b.getBoundingClientRect();
    ctxBuild(lines.length && !blocks.length ? linkItems(first) : blockItems(blocks[0] || first, [...S.sel]));
    ctxOpen(r.left, r.bottom + 6, true);
  });
}

// Clicks on the bar must not take the typing cursor out of the note being written in.
fmt.addEventListener('pointerdown', ev => { if (!ev.target.closest('input,select')) ev.preventDefault(); ev.stopPropagation(); });
fpop.addEventListener('pointerdown', ev => { if (!ev.target.closest('input')) ev.preventDefault(); ev.stopPropagation(); });
[fmt, fpop].forEach(el => el.addEventListener('keydown', ev => {
  if (ev.key === 'Escape') { ev.preventDefault(); fpop._k ? fpopClose() : (document.activeElement.blur(), fmtHide()); }
  if (ev.target.closest('input,select')) ev.stopPropagation();       // Delete in a box must not delete the block
}));
document.addEventListener('pointerdown', ev => { if (fpop._k && !ev.target.closest('.ol-fmt,.ol-fmtpop')) fpopClose(); }, true);
board.addEventListener('pointerup', () => fmtSoon());
addEventListener('resize', () => fmtSoon());

// Hover a button for a moment and a small label says what it does. The browser's own tooltip is
// slow and plain, so the title is moved aside while ours shows, and put back after.
const tip = h('div', 'ol-tip'); R.appendChild(tip);
let tipT = 0, tipEl = null;
function tipHide() {
  clearTimeout(tipT); tip.classList.remove('on');
  if (tipEl && tipEl.dataset.tip != null) { tipEl.title = tipEl.dataset.tip; delete tipEl.dataset.tip; }
  tipEl = null;
}
R.addEventListener('pointerover', ev => {
  const el = ev.target.closest('button[title],select[title],input[title],[data-tool][title],.ol-tools button,.ol-zoom button');
  if (el === tipEl) return;
  tipHide();
  if (!el || !R.contains(el) || el.closest('.ol-b') || !(el.title || el.getAttribute('aria-label'))) return;
  tipEl = el;
  const text = el.title || el.getAttribute('aria-label');
  if (el.title) { el.dataset.tip = el.title; el.title = ''; }
  tipT = setTimeout(() => {
    if (tipEl !== el || !el.isConnected) return;
    const m = /^(.*?)\s*\(([^()]{1,24})\)\s*$/.exec(text);       // "Bold (Ctrl B)": the keys go in grey
    tip.replaceChildren(document.createTextNode(m ? m[1] : text));
    if (m) tip.appendChild(h('b', '', m[2]));
    const rr = R.getBoundingClientRect(), f = rr.width / R.offsetWidth || 1, r = el.getBoundingClientRect();
    tip.classList.add('on');
    const w = tip.offsetWidth, hh = tip.offsetHeight;
    const toolbar = el.closest('.ol-tools');
    let x = toolbar ? (r.right - rr.left) / f + 10 : (r.left + r.width / 2 - rr.left) / f - w / 2;
    let y = toolbar ? (r.top + r.height / 2 - rr.top) / f - hh / 2 : (r.bottom - rr.top) / f + 8;
    if (!toolbar && y + hh > R.clientHeight - 6) y = (r.top - rr.top) / f - hh - 8;
    tip.style.left = clamp(x, 6, R.clientWidth - w - 6) + 'px';
    tip.style.top = clamp(y, 6, R.clientHeight - hh - 6) + 'px';
  }, 380);
});
R.addEventListener('pointerout', ev => { if (tipEl && !tipEl.contains(ev.relatedTarget)) tipHide(); });
R.addEventListener('pointerdown', tipHide, true);
addEventListener('wheel', tipHide, {passive: true});

// How the board itself looks, a choice kept on this computer.
function boardLook() {
  R.dataset.grid = store.get('olGrid', 'dots');
  const p = store.get('olPaper', '');
  if (p) R.dataset.paper = p; else delete R.dataset.paper;
}
boardLook();
function boardLookMenu(at) {
  const tick = on => on ? '✓  ' : '    ';
  const g = store.get('olGrid', 'dots'), p = store.get('olPaper', '');
  const pick = (k, v) => () => { store.set(k, v); boardLook(); };
  ctxBuild([
    {head: 'the board'},
    ...[['dots', 'Dots'], ['big', 'Big dots'], ['lines', 'Squared paper'], ['none', 'Plain']]
      .map(([v, n]) => ({label: tick(g === v) + n, go: pick('olGrid', v)})),
    {rule: 1}, {head: 'paper colour'},
    ...[['', 'The usual'], ['warm', 'Warm'], ['cool', 'Cool'], ['green', 'Green'], ['dark', 'Darker']]
      .map(([v, n]) => ({label: tick(p === v) + n, go: pick('olPaper', v)})),
    {rule: 1},
    {label: tick(fmtOn()) + 'Style bar over picked things', go: () => { store.set('olFmt', !fmtOn()); fmtSoon(true);
      toast(fmtOn() ? 'The style bar shows over what you pick' : 'Style bar off — right-click for the look instead'); }},
  ]);
  ctxOpen(at ? at.x : innerWidth / 2, at ? at.y : 120, true);
}

// Lay out whatever is sitting in a box, in rows inside it, in the order it is already in:
// left to right, top to bottom. The box grows taller if its contents will not fit.
function tidyInside(shape) {
  const list = inside(shape).sort((a, b) => (a.y - b.y) || (a.x - b.x));
  if (!list.length) return toast('Nothing in this one yet');
  const pad = 20, gap = 16, top = shape.y + 44;            // room for the box's own name
  const room = Math.max(120, shape.w - pad * 2);
  const steps = [];
  let x = shape.x + pad, y = top, tallest = 0;
  list.forEach(b => {
    if (x > shape.x + pad && x + b.w > shape.x + shape.w - pad) {    // no room left on this row
      x = shape.x + pad; y += tallest + gap; tallest = 0;
    }
    const before = snap(b);
    b.x = Math.round(x); b.y = Math.round(y);
    steps.push({before, after: snap(b)});
    put(b);
    x += Math.min(b.w, room) + gap;
    tallest = Math.max(tallest, b.h);
  });
  const grew = growToFit(shape);
  if (grew) steps.push(grew);
  record(steps);
  toast('Tidied ' + list.length + (list.length === 1 ? ' thing' : ' things'));
  sfx('done', 1.2);
}

// Line a selection up, or spread it out evenly. Arrows follow whatever they join, so they
// are left out of it.
function tidy(ids, how) {
  const list = ids.map(i => S.blocks.get(i)).filter(b => b && b.kind !== 'arrow' && !locked(b));
  if (list.length < 2) return toast('Pick at least two things first');
  const steps = [], mv = (b, x, y) => {
    const before = snap(b); b.x = Math.round(x); b.y = Math.round(y);
    steps.push({before, after: snap(b)}); put(b);
  };
  const L = Math.min(...list.map(b => b.x)), R = Math.max(...list.map(b => b.x + b.w));
  const T = Math.min(...list.map(b => b.y)), B = Math.max(...list.map(b => b.y + b.h));
  if (how === 'left') list.forEach(b => mv(b, L, b.y));
  else if (how === 'right') list.forEach(b => mv(b, R - b.w, b.y));
  else if (how === 'top') list.forEach(b => mv(b, b.x, T));
  else if (how === 'bottom') list.forEach(b => mv(b, b.x, B - b.h));
  else if (how === 'middle') list.forEach(b => mv(b, (L + R) / 2 - b.w / 2, b.y));
  else if (how === 'centre') list.forEach(b => mv(b, b.x, (T + B) / 2 - b.h / 2));
  else if (how === 'row' || how === 'column') {
    const across = how === 'row';
    const sorted = [...list].sort((a, b) => across ? a.x - b.x : a.y - b.y);
    const gap = 28;
    let at = across ? L : T;
    sorted.forEach(b => { across ? mv(b, at, b.y) : mv(b, b.x, at); at += (across ? b.w : b.h) + gap; });
  }
  record(steps);
  sfx('done', 1.2);
}

// ================================================================ starter layouts
const LAYOUTS = {
  board: {
    name: 'To do \u00b7 Doing \u00b7 Done',
    make(x, y) {
      const made = [];
      ['To do', 'Doing', 'Done'].forEach((title, i) => {
        const sx = x + i * 340;
        const f = add('shape', sx, y, {form: 'box', text: title, color: PAL[i === 2 ? 2 : i === 1 ? 3 : 7]},
                      {size: [310, 460], centre: false});
        made.push(f.id);
        if (i === 0) for (let k = 0; k < 2; k++)
          made.push(add('note', sx + 22, y + 40 + k * 120,
                        {html: '', bg: PAPER[k % PAPER.length]}, {size: [266, 100], centre: false}).id);
      });
      return made;
    },
  },
  revision: {
    name: 'Revision plan',
    make(x, y) {
      const made = [];
      const f = add('shape', x, y, {form: 'box', text: 'This week', color: PAL[1], fill: true},
                    {size: [700, 420], centre: false});
      made.push(f.id);
      const topics = add('todo', x + 24, y + 48, {items: [
        {t: 'Topic to go over', done: false}, {t: 'Past paper', done: false},
        {t: 'Make notes', done: false}]}, {size: [300, 180], centre: false});
      const notes = add('note', x + 352, y + 48, {html: 'What I did not understand'},
                        {size: [320, 180], centre: false});
      const next = add('note', x + 24, y + 254, {html: 'Next: ', bg: PAPER[0]},
                       {size: [300, 130], centre: false});
      made.push(topics.id, notes.id, next.id);
      add('arrow', 0, 0, {from: topics.id, to: notes.id, form: 'straight', ends: 'to'},
          {size: [0, 0], centre: false});
      add('arrow', 0, 0, {from: notes.id, to: next.id, form: 'elbow', ends: 'to', label: 'then', dash: true},
          {size: [0, 0], centre: false});
      return made;
    },
  },
};
function dropLayout(key, p) {
  const L = LAYOUTS[key]; if (!L) return;
  const made = L.make(Math.round(p.x), Math.round(p.y));
  // the frames belong behind what sits in them
  toBack(made.filter(i => S.blocks.get(i)?.kind === 'shape'));
  select(made);
  toast(L.name + ' added');
}

function spaceItems(p) {
  const put = (kind, data = {}) => () => {
    const b = add(kind, p.x, p.y, data);
    setTimeout(() => startEdit(b.id), 10);
  };
  return [
    {head: 'Put here'},
    {label: 'Note', key: 'N', go: put('note')},
    {label: 'Big text', key: 'T', go: put('text')},
    {label: 'Code', key: 'C', go: put('code', {lang: 'auto', text: ''})},
    {label: 'Code project', key: 'K', go: put('proj', {title: '', files: []})},
    {label: 'Checklist', key: 'L', go: put('todo', {items: []})},
    {label: 'Terminal output', key: 'R', go: put('term')},
    {label: 'Link', key: 'U', go: put('link', {url: '', title: ''})},
    {label: 'Picture\u2026', key: 'I', go: () => $('.ol-file').click()},
    {label: 'Code files or a zip from this computer\u2026', go: () => importCode('files', p)},
    {label: 'A code folder from this computer\u2026', go: () => importCode('folder', p)},
    {rule: 1},
    {label: 'Box to group things\u2026', key: 'O', go: () => {
      setTool('shape', 'box'); toast('Drag out how big it should be'); }},
    {label: 'Circle\u2026', go: () => {
      setTool('shape', 'ellipse'); toast('Drag out how big it should be'); }},
    {rule: 1},
    {head: 'ready-made'},
    {label: LAYOUTS.board.name, go: () => dropLayout('board', p)},
    {label: LAYOUTS.revision.name, go: () => dropLayout('revision', p)},
    {rule: 1},
    {label: 'Paste here', key: 'Ctrl V', go: () => pasteAt(p)},
    {label: 'Find on the board\u2026', key: 'Ctrl F', go: openFind},
    {label: (mapOn ? '\u2713  ' : '\u00a0\u00a0\u00a0 ') + 'Map of the board', key: 'M',
     go: () => showMap(!mapOn)},
    {label: 'Check the writing on the board\u2026', key: 'F7', go: () => Writing.open(null)},
    {label: (Writing.auto ? '\u2713  ' : '\u00a0\u00a0\u00a0 ') + 'Fix typos as I type',
     go: () => { Writing.auto = !Writing.auto;
                 toast(Writing.auto ? 'Typos get fixed as you type' : 'Typing left alone'); }},
    {label: 'Select everything', key: 'Ctrl A', go: () => select([...S.blocks.keys()])},
    {label: 'How the board looks…', go: () => boardLookMenu(ctxWas)},
    {rule: 1},
    {label: 'Show everything', key: 'F', go: () => fit()},
    {label: 'Back to 100%', key: '0', go: () => zoomAt(1)},
    {rule: 1},
    {head: 'copies on this computer'},
    {label: 'Back one up now', go: async () => {
      const ok = await Backup.take(true);
      toast(ok ? 'Copied to this computer' : 'Could not make a copy'); }},
    {label: 'Put back an older copy\u2026', go: restoreBackup},
    {label: 'Save the board as a file', go: exportBoard},
  ];
}

// A menu click is not a paste event, so this has to ask for the clipboard itself. That can be
// refused, in which case say what to press instead of appearing to do nothing.
async function pasteAt(p) {
  let t = '';
  try { t = await navigator.clipboard.readText(); } catch (_) { return toast('Press Ctrl+V to paste here'); }
  if (!t.trim()) return toast('Nothing to paste');
  if (t.startsWith('bb-online:')) {
    try {
      const list = JSON.parse(t.slice(10));
      list.forEach(b => S.blocks.has(b.id) || S.blocks.set(b.id, {...b, _ghost: true}));
      duplicate(list.map(b => b.id), 30);
      list.forEach(b => S.blocks.get(b.id)?._ghost && S.blocks.delete(b.id));
    } catch (_) { toast('That copy could not be read'); }
    return;
  }
  addText(t, p);
}

document.addEventListener('pointerdown', ev => { if (!ev.target.closest('.ol-ctx,.ol-switch')) closeCtx(); }, true);
addEventListener('wheel', ev => { if (!ev.target.closest('.ol-ctx')) closeCtx(); }, {passive: true, capture: true});

// ---------------------------------------------------------------- zoom buttons, start
R.querySelectorAll('.ol-zoom button').forEach(b => b.onclick = () => { const z = b.dataset.z;
  if (z === '+') zoomAt(S.view.k * 1.25); else if (z === '-') zoomAt(S.view.k / 1.25); else if (z === '0') zoomAt(1); else fit(); sfx('tap', .6); });
let started = false;
async function start() {
  if (started) return; started = true; setTool('select'); applyView();
  const g = store.get('olGroup', null);
  if (g === 'local' || (!CFG.url && g !== null)) return useLocal();
  if (CFG.url && g && g.id) { setState('wait'); if (await connect()) return openGroup(g); return useLocal().then(() => setState('off', 'Offline: showing this computer\'s board')); }
  await useLocal(false); showGate();
}
const nav = document.querySelector('nav button[data-tab="online"]');
nav && nav.addEventListener('click', () => setTimeout(() => { start(); R.focus({preventScroll: true}); }, 30));
if (document.querySelector('#online.on')) start();
new MutationObserver(() => { if (document.querySelector('#online.on')) start(); }).observe(document.getElementById('online'), {attributes: true, attributeFilter: ['class']});
window.bbWriting = Writing;
window.bbOnline = {S, IDE, run, add, fit, placeCode, codeFiles, select, autoCmd, start, useLocal, incoming, sniff, guessLang, clean, paint, place, put, drawLinks, startEdit, tidy, dropLayout, Writing, view: v => { S.view = v; applyView(); }};
})();
"""
