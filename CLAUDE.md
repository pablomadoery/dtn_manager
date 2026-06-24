# DTN-Manager — project & active task

Interactive web app to manage ION-DTN networks backed by Docker containers.
FastAPI backend (`backend/`) + vanilla-JS/Cytoscape frontend (`frontend/`).
Run: `./start.sh` (port 8080). Stop: `./stop.sh`. See `README.md` for features/API.

Resource naming (deterministic, all labeled `ionmgr.managed`):
containers `ionmgr_n{id}`, networks `ionmgr_link_{a}_{b}`.

## 🔴 ACTIVE TASK — resume here

A deep audit (78 agents, **66 verified issues**) produced a 17-fix hardening plan.
Implementation is **in progress**. To continue:

1. Read **[`todo.md`](todo.md)** — status tracker for all 17 fixes, in dependency order,
   with target `file:line`s and what's done vs. remaining.
2. Read **[`AUDIT.md`](AUDIT.md)** — full per-fix detail (problem / fix / prevention / risk),
   root-cause analysis, and every verified finding by subsystem.

### Decisions already made (do not re-litigate)
- **Scope:** implement **all 17** fixes.
- **Network access:** **localhost only** (bind `127.0.0.1`, opt-in expose via `BIND_HOST`). **No auth token.**
- **Restart behavior:** **preserve & adopt** — restart re-adopts running topology via `reconcile()`;
  clean shutdown does NOT wipe; explicit wipe via `start.sh --clean` / `stop.sh --teardown`.

### Done so far
- `backend/errors.py` — domain exception hierarchy.
- `backend/settings.py` — env-driven caps/toggles (`IONMGR_MAX_NODES`, `IONMGR_CLEANUP_ON_EXIT`, …).
- `backend/services/ion_config.py` — rewritten with convergence-layer registry (#11 partial:
  runtime callsites in `docker_manager.py` still need CL threaded through).

### Next up (dependency spine)
**#3** hardened argv exec primitive (RCE fix) → **#1** startup `reconcile()` → **#2** idempotent
get-or-create + transactional rollback → then #8, #12, #11(finish), #14, #13, #7, #4, #9, #15, #5,
#10, #6, #16, #17 → **VERIFY** (restart, reproduce the link-create 409, confirm reconcile adopts/cleans).

### Verify after changes
`python3 -m py_compile` the backend; `./start.sh`; `curl localhost:8080/api/topology`; create
N1/N2/N3 and link N1↔N2 (must succeed); then `kill -9` the server leaving networks behind and
`./start.sh` again — confirm `reconcile()` adopts/cleans and link-create still works (no 409).
