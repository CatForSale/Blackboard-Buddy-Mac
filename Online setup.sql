-- Blackboard Buddy: Online board. Paste all of this into Supabase > SQL Editor and press Run.
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
