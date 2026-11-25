# Setting up notes (about 15 minutes, free)

Notes are stored in a free Supabase database. Anyone can read and post; nobody can edit or delete except you.

## 1. Make the database
1. Go to supabase.com and sign up (free plan).
2. Click **New project**. Give it a name (e.g. `ryans-feed`), set a database password (save it somewhere), pick a region near you, and create it. Wait a minute while it starts.

## 2. Create the notes table
1. In the left sidebar, open **SQL Editor** and click **New query**.
2. Paste everything below and click **Run**.

```sql
create table public.notes (
  id bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  name text check (char_length(name) <= 40),
  body text not null check (char_length(body) between 1 and 500),
  page text check (char_length(page) <= 30),
  approved boolean not null default true
);

alter table public.notes enable row level security;

-- anyone can read notes that are approved (all of them, unless you turn on approval)
create policy "read approved notes" on public.notes
  for select to anon using (approved);

-- anyone can post, but only the name, note and page; never "approved"
create policy "post notes" on public.notes
  for insert to anon with check (true);
revoke insert on public.notes from anon;
grant insert (name, body, page) on public.notes to anon;
grant select on public.notes to anon;

-- flood guard: no more than 20 notes in any 10 minutes, site-wide
create function public.notes_flood_guard() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  if (select count(*) from public.notes where created_at > now() - interval '10 minutes') >= 20 then
    raise exception 'Lots of notes right now. Please try again in a few minutes.';
  end if;
  return new;
end $$;
create trigger notes_flood before insert on public.notes
  for each row execute function public.notes_flood_guard();
```

## 3. Connect your site
1. In Supabase, open **Project Settings → API** (or **API Keys**).
2. Copy the **Project URL** (looks like `https://abcd1234.supabase.co`).
3. Copy the **publishable** key (starts with `sb_publishable_`) or, on older projects, the **anon public** key. Never use the secret or service_role key.
4. On GitHub, open `site/data/notes.json`, click the pencil, paste them in:
   ```json
   { "url": "https://abcd1234.supabase.co", "key": "sb_publishable_..." }
   ```
   and commit. The notes tab works as soon as the site rebuilds.

## Managing notes
- **Delete a note:** Supabase → **Table Editor** → `notes` → select the row → delete.
- **Hide a note without deleting:** untick its `approved` box.
- **Turn on approval** (notes wait for you before appearing): run this once in the SQL Editor:
  ```sql
  alter table public.notes alter column approved set default false;
  ```
  Then tick `approved` on each note you want shown. To turn it off again, run the same line with `true`.
