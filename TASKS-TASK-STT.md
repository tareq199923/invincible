# Remaining tasks — handoff for session `task stt`

Written 2026-09-24 by the session that released 0.3.1 and documented the
Railway ownership transfer. **This is a handoff, not the canonical list** —
`docs/WORKQUEUE.md` remains the source of truth. Scope here is deliberately
narrow: two items were selected. The Railway ownership transfer is **excluded**
(see bottom).

Repo root: `C:\Users\SARK\Desktop\ai-gateway`. At handoff creation, branch
`main` had a clean tree at `8edead81`. The `facts` decision below is now
recorded in uncommitted edits to `docs/ROADMAP.md` and `docs/WORKQUEUE.md`;
this handoff and `codereview.md` are untracked working files. Recheck
`git status` before acting because this snapshot describes handoff time, not
a guaranteed current repository state.

---

## Task A — Decision 3: retarget the `facts` triple store

### What the decision is

The `facts` table still exists as a **Postgres** table (migrated long ago —
no SQLite involved). Since Phase 4 it is unused by request-serving code. The
legacy SQLite importer remains its only production writer. Its replacement,
`memories`, already exists and is what the current service uses.

So the open question was not "does it work" — it was **what to do with a
table normal requests no longer use**:

1. **Drop it** — cleanest, matches "retire superseded local-era pieces"
   (Phase 8). Requires a new Alembic revision, and it is irreversible for any
   rows in production.
2. **Keep it as history** — zero code, zero migration. Retains schema and
   storage plus the legacy-only importer branch.

The previous WORKQUEUE phrasing was "no code depends on it either way" — the
point of this task was to make that call.

### Verified current state (re-check before acting)

Schema — `invincible/core/db.py:415`:

```python
facts = Table(
    "facts", metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("user_id", Text, nullable=False, server_default="default"),
    Column("session_id", Text, nullable=False),
    Column("entity", Text, nullable=False),
    Column("relation", Text, nullable=False),
    Column("target", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    UniqueConstraint("user_id", "session_id", "entity", "relation", "target",
                     name="uq_facts_triple"),
)
```

Every reference in the tree, and what it is:

| Location | Role | Verdict |
|---|---|---|
| `invincible/core/db.py:415` | Table definition | Live schema |
| `invincible/migrations/versions/20260825_0001_baseline.py:73,86,221` | CREATE + drop in baseline | Historical |
| `invincible/core/memory.py:10-11` | Docstring: "inert history as of Phase 4" | **Accurate** |
| `invincible/core/db_import.py:33,156-165` | Only writer: legacy SQLite → PG import | **Keep while importing** |
| `invincible/core/db_import.py:211` | Resynchronizes the `facts` identity sequence after import | Keep with importer |
| `invincible/cli.py:1988` | Importer docstring lists `facts` | Keep with importer |

**Important nuance the WORKQUEUE wording can hide:** `memory.py` reuses the
*name* `facts` in live code: `extract_facts()` (line 61) feeds the `memories`
writer. The similarly named `memory_max_facts` setting
(`settings.py:239`) is a legacy accessor with no current caller. **Dropping
or retiring the `facts` table must not remove `extract_facts()`.** Read
`memory.py:1-12` first; the docstring is the clearest statement of the split.

### Dependency to settle first

The legacy importer (`db import`) is itself listed for retirement in
WORKQUEUE §2. **Dropping the `facts` table while the importer still inserts
into it is inconsistent** — either drop the table *after* the importer goes,
or drop both together. Decide the ordering before writing a migration.

### Before dropping — check production data

The table has a `user_id` column, so it is per-user. Production is Neon
(`ep-bold-field-azoysw7g`, ap-southeast-1). **Do not run a destructive
migration without first checking whether the table actually holds rows**, and
take a Neon branch/backup first. Note the project rule that has held so far:
Neon is the only real data; local DBs are test data. Confirm before any drop.

### Definition of done

- The keep decision is recorded in `docs/WORKQUEUE.md` §2 and
  `docs/ROADMAP.md` §Deprecated with the importer dependency stated.
- No schema, importer, or request-serving code is changed while the importer
  remains supported.
- A word-boundary sweep such as `git grep -n -w facts -- invincible tests docs`
  confirms no dangling table reference; review hits to distinguish them from
  live `extract_facts()` code and prose uses.

---

## Task B — docs / stale claims after the plan change

**Correction to the handoff brief:** at handoff creation, these edits were
*not* uncommitted. The serving-model documentation landed in `8edead81`, and
the plan-change documentation landed in `abbb2cb6`. The separate `facts`
decision now present in `docs/ROADMAP.md` and `docs/WORKQUEUE.md` is
uncommitted. Re-verify the docs against the new plan and fix what went stale.
The two existing commits did a lot of this; below is what was found still
worth a look, not a known-broken list.

### Specific things to verify

1. **`docs/WORKQUEUE.md:15` and §1** — these were *just rewritten* for the
   ownership transfer. Re-read them and confirm they still read true; they are
   the most recently edited lines and therefore the most likely to be
   half-updated.
2. **`docs/DEPLOYMENT.md`** — the transfer doc cites it as the source for
   "required production variables, database, single-instance posture." Check
   it doesn't still frame anything as an imminent *host migration* (the old
   Azure plan). `abbb2cb6` touched it (4 lines) but a full re-read is cheap.
3. **Anything still naming Azure as the next host or current migration
   target.** `MIGRATION-AZURE.md` was deleted in `abbb2cb6`, and
   `ROADMAP.md`/`WORKQUEUE.md` were reworded. Search for `Azure`, but allow
   valid historical mentions and supported generic deployment options such
   as the Azure examples in `DEPLOYMENT.md` and `CONFIGURATION.md`.
4. **The 0.3.1 doc-sync commit `2708517`** is titled "sync the work queue with
   the 0.3.1 release and drop two stale claims." Worth reading before adding
   more edits, so you don't re-fix something it just fixed.
5. **`docs/TESTING.md`** — `8edead81` fixed the coverage-map line. Confirm the
   suite count it cites (1136) still matches reality.

### Definition of done

- A short, honest pass over the five points above.
- Any fix is a *doc* fix in the relevant file; no scope creep into code.
- If nothing is stale, say so and close the task — do not invent edits to
  justify the pass.

---

## Explicitly out of scope

**Railway ownership transfer.** Selected-out. It is the WORKQUEUE §1 item and
has its own runbook at `docs/RAILWAY-ACCOUNT-TRANSFER.md` (un-started
checklist). It belongs to whichever session is doing the operations work, not
here.

## References

- `docs/WORKQUEUE.md` — canonical task list; §1 Railway, §2 Phase 8
  (contains the `facts` bullet), §Decisions pending.
- `docs/ROADMAP.md` — Phase 7 deployment status; §Deprecated table.
- `docs/ARCHITECTURE.md` — module map.
- `invincible/core/memory.py` — the `facts` docstring and the live
  `memories`-writing code that must not be disturbed.
