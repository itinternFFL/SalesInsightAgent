# Role-Based Access Control

Who can see which sales data, based on a 3-tier reporting hierarchy.

## The hierarchy

```
Manager
  └─ Senior Executive (reports to a Manager)
       └─ Executive / Employee (reports to a Senior Executive)
```

Every user has a `role` (`manager` | `senior_executive` | `executive`) and a
`reports_to_id` pointing at the user one level up (`NULL` for a manager -
they're the top of their own branch). This lives in the `users` table in
`db/users.db` (see `backend/db.py`).

**Roles are self-declared, not admin-assigned.** A user picks their own
role and manager at registration (email/password) or on first Microsoft SSO
login (see `POST /auth/complete-profile`, shown to a first-time SSO user by
`frontend/src/CompleteProfile.jsx`). This is a deliberate simplification for
a small internal tool with no admin panel - the tradeoff is that domain
membership is verified (Microsoft tenant, or an `@yourdomain` email per
`SETUP.md`) but a person's *claimed role* is not independently verified.
Nothing stops someone from registering as "Manager." If that gap matters,
the fix is an admin-approval step before a role becomes active - not
currently built.

## Access rules

Visibility is scoped strictly to a user's own reporting branch, walked
**down** from themselves - never sideways, never up:

- **Manager**: own data + every Senior Executive and Executive/Employee
  below them in their branch.
- **Senior Executive**: own data + every Executive/Employee who reports up
  through them. Cannot see their Manager, or any other Senior Executive's
  branch.
- **Executive / Employee**: own data only. Cannot see peers, their Senior
  Executive, or their Manager.

## What "data" means here, and where it lives

This app's sales data (`data/*.xlsx`) is company-wide monthly report
files, not records individual users create - there's no salesperson/rep
column in the schema. So "a user's data" is defined as **the report files
they uploaded**, and ownership of a file is determined by **which folder
it's physically sitting in** - not a database table. `data/` has two parts:

- **Legacy company-wide files** stay directly in `data/` - the original
  monthly exports that predate per-employee folders entirely (see
  "Unattributed data" below). Nobody "owns" these; they're not per-employee.
- **Per-employee uploads** live under `data/employees/<employee name>/` -
  one subfolder per person, created automatically the first time they
  upload. `POST /api/upload` writes new files here
  (`employee_folder_name()` in `backend/access_control.py`, sanitizing the
  display name for Windows); the 7 files uploaded while building/testing
  this feature were moved into their matching folders by hand as a
  one-time migration.

`get_accessible_filenames()` (below) reads these folders directly, fresh,
on every request - there's no `file_uploads`-style database table
recording who owns what; the folder location *is* that record. The one
thing that still comes from the database is the **reporting hierarchy**
(who reports to whom), since that relationship has no filesystem
representation - `backend/db.py`'s `users` table, `reports_to_id` column.
A small `file_uploads` table still exists purely as an upload-time
timestamp log (who uploaded something and when), but nothing here reads
it to make an access decision.

`src/ingest.py`'s `load_all()` searches the whole `data/` tree recursively
(`rglob`, not `glob`), so both legacy and per-employee files are found
regardless of nesting. **Canonical filenames must stay unique across the
WHOLE tree, not just within one folder** - `source_file` (used for RBAC
scoping and the entity-scope fast-path) is keyed by filename alone, with
no path component, so two different employees' files for the same
calendar month can't both be named `Sale_Report_FMO-<Mon>-<Year>.xlsx`
even in different folders, or their rows would become indistinguishable
by owner despite the files sitting in different places. `_find_existing_path()`
and `_find_free_path_globally()` in `backend/main.py` check the whole
tree, not one folder, for exactly this reason - a bug caught and fixed
while building the folder reorganization: an earlier version of "Keep
Both" checked uniqueness only within the uploader's own folder, which let
two identically-named files coexist on disk while their (at the time,
database-driven) attribution silently collided onto whichever was written
last.

**Real Windows/NTFS folder permissions are a separate, complementary
layer, not part of the app.** `deploy/setup-folder-permissions.ps1` sets
up per-employee local Windows accounts and folder permissions for people
who browse `data/` directly (file share, RDP, etc.) rather than through
the app - the chat app itself never checks NTFS permissions, and this
script is deliberately not run automatically: creating login-capable
local Windows accounts and changing a server's folder permissions is a
real, hard-to-reverse change to its security surface, done once by
whoever administers the machine.

## `get_accessible_user_ids` / `get_accessible_filenames` and how they're used

`backend/access_control.py`:

- **`get_accessible_user_ids(user_row) -> set[int]`** - self, plus every
  user reachable by walking down `reports_to_id` in the database (not just
  direct reports - a Manager's set includes their Senior Executives'
  Executives too). Executives (and anyone with no role yet) get back just
  `{self}`. This is the one place the reporting hierarchy is consulted.
- **`employee_folder_name(user_row) -> str`** - the single definition of
  "this user's folder name," used both when writing a new upload and when
  reading who owns what - keeping the write path and read path in
  agreement is what makes the folder trustworthy as a source of truth
  instead of two things that could drift apart.
- **`get_accessible_filenames(user_row, data_dir) -> set[str]`** - turns
  the accessible user-id set into actual filenames by listing each
  accessible employee's folder directly off disk (plus the legacy
  top-level files, manager-only - see "Unattributed data" below). No
  caching, no database lookup for ownership - just a fresh directory
  listing per call.
- **`find_owning_employee_folder(filename, data_dir) -> str | None`** -
  the same folder-location lookup, used by the replace-permission check
  instead of a database query.
- **`require_role_assigned`** - a FastAPI dependency that 403s any
  data-bearing request from a user who hasn't picked a role yet. Used
  instead of the plain `get_current_user` on every endpoint that touches
  sales data.

Every relevant endpoint in `backend/main.py` filters through these before
touching data - **enforcement is server-side only**, the frontend never
decides what's visible:

| Endpoint | Enforcement |
|---|---|
| `GET /api/stats` | Row/month/category counts computed from `_scoped_data(user)`, not the full dataset. |
| `POST /api/chat` | The RAG chunk index is built from `_scoped_data(user)`'s filtered DataFrame - the LLM only ever sees data the user can access. A question naming a specific out-of-scope entity is refused before that, with no index build or LLM call - see "Fast-path refusal" below. |
| `POST /api/upload` | New file written to `data/employees/<uploader's folder>/` - the folder placement itself is the access grant, no separate database record needed. |
| `POST /api/upload/resolve` (`action=replace`) | `_ensure_can_replace_file` 403s if the file being overwritten sits in a folder outside the caller's accessible set - prevents one branch destroying another's data by uploading a file for the same month. |

There's no per-user "fetch record by ID" endpoint in this app today (no
individual sales rows are addressable), so the escalation surface is
narrower than a typical CRUD API - but if one is added later, it must
check the target id against `get_accessible_user_ids(current_user)` and
403 otherwise, the same way `_ensure_can_replace_file` does for files.

## Edge cases

- **No reports (Executive)**: `get_accessible_user_ids` returns `{self}` -
  no special-casing needed, it falls out of the walk naturally.
- **Reporting-structure or folder changes**: nothing here is cached.
  Every request re-reads `reports_to_id` from the database and re-lists
  the relevant folders fresh, so reassigning someone to a different
  Senior Executive, or moving/adding a file, takes effect on their very
  next request - no invalidation step required. (See "Performance &
  caching" below for the one thing that *is* cached, and why it's still
  always correct.)
- **Deleted or unassigned manager**: there's no delete-user endpoint in the
  app yet, but if a row were ever removed directly from the database, any
  user whose `reports_to_id` pointed at it becomes an orphan. This is
  **safe by construction, not by special-case code**: `get_accessible_user_ids`
  only ever walks *downward* from the current user, so a dangling
  `reports_to_id` on an ancestor is simply never followed - it can only
  shrink what's visible (the orphan's own branch stays intact, but nobody
  above them can see it anymore), never leak data to the wrong person.
  The same logic covers a user who hasn't picked a manager yet
  (`reports_to_id IS NULL` on a non-manager) - they're invisible to
  everyone until they complete that step. Fixing an orphaned branch means
  reassigning it via `POST /auth/complete-profile`, same as any other
  reassignment.
- **Unattributed (legacy) data**: files sitting directly in `data/` (not
  under any `data/employees/<name>/` folder) predate per-employee folders
  entirely. Policy: visible to **managers only** - the broadest legitimate
  role - rather than everyone (would leak across branches) or no one
  (would silently vanish real data). To attach a legacy file to a specific
  employee, either move it into their `data/employees/<name>/` folder by
  hand, or re-upload it through the app (which writes it into the
  uploader's folder, same as any new upload).

## Performance & caching

`get_accessible_filenames()` itself is never cached - it re-lists the
relevant folders on every single call, which is what makes "reflects
folder changes live" (above) true with no invalidation logic anywhere.
Directory listings are cheap (filenames only, no file content read), so
this doesn't reintroduce the kind of cost that *is* worth caching below.

Building the embedded chunk index (`src/index.py`'s `build_index_from_df`)
*is* the expensive step, so `backend/main.py` caches one built index per
*distinct accessible-filenames set* in `_state["scoped_index_cache"]`
(keyed by a `frozenset` of filenames). This is safe to cache because the
key itself is derived fresh from the filesystem on every request - if a
reassignment or a folder change alters what a user can see, it produces a
different key, which simply misses the cache and builds a fresh index; it
never serves stale data under an unchanged key. `_refresh_state()` (called
after every successful upload) clears the whole cache outright, since a
new file changes the picture for everyone.

## Fast-path refusal for named out-of-scope entities

Retrieval and generation together take tens of seconds - wasteful for a
question that was always going to end in "no data," e.g. an Executive
asking about a brand only their Manager uploaded. `backend/access_control.py`'s
`find_out_of_scope_entity(question, full_entity_values, scoped_entity_values)`
runs first, before either step: it checks whether the question names a
specific brand, customer, material, channel, or sale type that exists in
the *full* dataset but nowhere in what this user can see, and if so,
`POST /api/chat` refuses immediately with no index build and no LLM call -
measured at ~400ms versus the usual 30-60+ seconds.

"The asker's own values" here means exactly what `get_accessible_filenames`
already determined for them - the content of their own and their reports'
`data/employees/<name>/` folders (plus legacy data if they're a manager).
It deliberately checks against both sides - the full dataset's values
*and* the asker's own - not the asker's own values alone. Checking only
the asker's own data, with nothing to compare against, can't distinguish
"names a real entity that belongs to someone else" (should refuse) from
"names nothing in particular" (an ordinary question like "what's the
grand total?", which must NOT be refused) - both would equally fail to
match the asker's own small value set, so that approach would refuse most
normal questions along with the ones that should be refused.

The function itself only does string matching, which stays cheap however
large the dataset gets - the part that scales with dataset size is
extracting each side's distinct values (`collect_entity_values`, an
O(rows) scan), so that step is never done per-request. The whole-dataset
side is computed once in `_refresh_state()` (`_state["entity_values_cache"]`)
and reused for every request until the next upload; the per-scope side is
cached the same way as the embedded chunk index
(`_state["scoped_entity_values_cache"]`, keyed by the same
accessible-filenames set as `scoped_index_cache`). Neither cache re-scans
the master DataFrame on every question, so the check's per-request cost
doesn't grow as more data is uploaded over time - only the (still cheap)
one-time extraction after each upload does.

This is a **skip-only** optimization, never a grant: a miss (no entity
named, or the named entity happens to be in scope) just falls through to
the normal pipeline, which still correctly declines on its own, only
slower. It deliberately doesn't consider `month` - a user can have partial
access to a month (some of its files, not all), so naming a month isn't
proof of "no access" the way naming a brand that's entirely outside their
scope is. It also skips short/generic values (under `MIN_ENTITY_LENGTH`)
to avoid false-matching on incidental substrings.

## Upgrading an existing local database

`db/users.db` from before this feature existed has the old two-column
schema (no `role`/`reports_to_id`) and `CREATE TABLE IF NOT EXISTS` won't
alter it. If the backend errors with `no such column: role`, delete
`db/users.db` and restart - any existing local/password accounts will need
to register again. Not an issue in production if RBAC ships before the
first real deployment (see `DEPLOYMENT.md`).

## Testing

`tests/test_access_control.py` (`python -m pytest tests/test_access_control.py -v`)
covers: a Manager seeing their whole branch, a Senior Executive seeing only
their own Executives (Manager and sibling-branch data excluded), an
Executive seeing only themselves (peers excluded), a no-role user, live
reassignment, an orphaned report, folder-based file scoping end-to-end
(including that moving a file between employee folders changes access on
the very next call, with no cache to invalidate), `find_owning_employee_folder`,
and `find_out_of_scope_entity`'s fast-path (an out-of-scope brand/customer
name is caught, an in-scope one and a no-entity question are not, and
short generic values don't false-match).

The whole folder-based rearchitecture was also verified live against the
running API and cross-checked against the exact figures established
before the change: all 7 real accounts' grand totals matched their
pre-refactor values exactly (e.g. a Senior Executive's Rs 6,500,000
unchanged), plus live tests of upload → correct folder placement,
cross-folder naming-conflict detection, cross-branch replace correctly
blocked, "Keep Both" disambiguating globally rather than colliding
between two employees' folders, and the fast-path still answering an
out-of-scope question in ~400ms versus the usual 30-60+ seconds.
