# Clean-state checklist — clock-out gate

A session is not complete until all five boxes tick. Run through this
list before every commit, then record the outcome in `PROGRESS.md`.

- [ ] **Build passes.** `ruff check .` is clean; no new warnings.
- [ ] **Tests pass.** The feature's verification command from
  `feature_list.json` exits 0 (full `pytest` when the change is broad).
- [ ] **Feature list updated.** The worked feature moved to `passing`
  (or back to `planned` with a Blocker note); at most one `active`.
- [ ] **No debug artifacts.** No `debug_*.json`, `pytest_out*.log`,
  stray dumps, or session scratch in the tree (`git status` shows only
  intended files).
- [ ] **PROGRESS.md updated.** Current State has the new commit hash +
  test status; Next Steps names the next concrete action.

If any box cannot tick, do not force the commit — write the reason
under `## Blockers` in `PROGRESS.md` and leave a clean checkpoint.
