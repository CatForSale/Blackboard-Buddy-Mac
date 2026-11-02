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

-- live updates
do $$ begin
  if not exists (select 1 from pg_publication_tables where pubname = 'supabase_realtime' and tablename = 'blocks') then
    alter publication supabase_realtime add table public.blocks;
  end if;
  if not exists (select 1 from pg_publication_tables where pubname = 'supabase_realtime' and tablename = 'members') then
    alter publication supabase_realtime add table public.members;
  end if;
end $$;
