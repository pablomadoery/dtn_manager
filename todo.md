# DTN-Manager — Hardening / Corrections TODO

Resume guide for the deep-audit corrections. A 78-agent audit found **66 verified
issues** (3 critical, 21 high, 32 medium, 10 low) collapsing into 3 systemic causes.
This file tracks the 17-fix plan and what's done so far.

Full audit detail (durable, in-repo): [`AUDIT.md`](AUDIT.md) — executive summary, root causes,
all 17 fixes with problem/fix/prevention/risk, architectural recommendations, prevention
checklist, and every verified finding by subsystem. This `todo.md` is the status tracker.

## The incident that started this
Creating link N1↔N2 returned `409 Conflict ("network ionmgr_link_1_2 already exists")`.
Root: a crashed prior session left orphaned Docker networks; the manager starts with
empty in-memory state and never reconciles against live Docker, so `create_link` collides
on the deterministic network name. (Two stale June-9 networks were manually removed to unblock.)

## Three systemic root causes
- **A. Volatile state treated as source of truth, no reconciliation.** `DockerManager.__init__`
  starts empty + `SubnetPool._next_id=0`; never queries Docker for `ionmgr.managed` resources.
- **B. Non-transactional, fail-silent provisioning.** `create_link`/`create_node`/import create
  resources in steps, record state only on success, and `_docker_exec` swallows non-zero exits.
- **C. Unauthenticated privileged control plane + unsafe I/O.** `--host 0.0.0.0`, no auth;
  `send_bundle` interpolates user input into `bash -c` (RCE); 4 path-traversal endpoints;
  blocking `docker exec` on the asyncio event loop; no locking.

## Decisions locked in (2026-06-24)
- **Scope:** implement **ALL 17** fixes.
- **Network access:** **localhost only** — bind `127.0.0.1` by default, opt-in expose via
  `BIND_HOST` env var. **No auth token.**
- **Restart behavior:** **preserve & adopt** — restart re-adopts running topology via
  `reconcile()`; clean shutdown does NOT wipe; explicit wipe via `start.sh --clean` /
  `stop.sh --teardown`. So `start.sh` does NOT blanket-prune on a normal start (would defeat
  preservation); `reconcile()` adopts healthy resources and removes only true orphans.

## Status legend: [x] done · [~] in progress · [ ] todo

### Foundations (new files) — DONE
- [x] `backend/errors.py` — domain exception hierarchy (NotFoundError, ConflictError,
  ValidationError, BackendTimeout, IonExecError, CapacityError). Used by #15/#3/#2.
- [x] `backend/settings.py` — env-driven knobs: `IONMGR_MAX_NODES`(64), `IONMGR_MAX_LINKS`(128),
  `IONMGR_MAX_IMPORT_BYTES`(5MB), `IONMGR_CLEANUP_ON_EXIT`(0=preserve), SUPPORTED versions.

### The 17 fixes
- [~] **#11 Convergence-layer registry** — `backend/services/ion_config.py` REWRITTEN with
  `CL_REGISTRY` + `resolve_cl()`, all generators threaded with `convergence_layer`.
  REMAINING: thread CL through the runtime callsites in `docker_manager.py`
  (create_link ~195-202, delete_link plan/outduct ~231-241 loop, get_node_config ~543-544,
  repair_ion_config ~836-849, neighbors builders ~508/510). Pass each link's `convergence_layer`.

- [ ] **#3 [CRITICAL] Hardened argv exec primitive** — `docker_manager.py`. Add one `_exec(container,
  argv, stdin=None, timeout=)` using `subprocess.run([...], input=...)` (NEVER `bash -c` with
  interpolation). Rewrite `_ion_exec` to pipe the admin command via stdin to bpadmin/ipnadmin.
  Rewrite `send_bundle` → `['docker','exec',c,'bpsource',f'ipn:{int(to)}.1', message]`. Migrate
  `_get_interface`, tc/tail commands. Catch `TimeoutExpired`→`BackendTimeout`. (RCE fix.)

- [ ] **#1 [CRITICAL] Startup reconcile()/adopt** — `docker_manager.py`, `subnet_pool.py`, `main.py`.
  `reconcile()` lists `ionmgr.managed` containers/networks, rebuilds `self.nodes`/`self.links`
  from live data (`network.reload()`, parse subnet from `attrs['IPAM']['Config'][0]['Subnet']`,
  IPs from `attrs['Containers']`). Mark half-built links status=ERROR; remove true orphans
  (networks with 0 containers). Seed `_next_node_id` from max adopted id; add
  `SubnetPool.reserve(idx)`/`from_live(indices)`. Add explicit `ionmgr.node_id`/`ionmgr.link_id`
  labels at create time. Call `reconcile()` at end of `__init__`.

- [ ] **#2 [HIGH] Idempotent get-or-create networks + transactional rollback** — `docker_manager.py`.
  `_get_or_create_network(name, subnet)`: try `networks.get`, reuse real subnet if found, catch
  409 race → fall back to get. Wrap `create_link` body in try/except with flags
  (network_created/connected_a/connected_b) → on failure disconnect + remove + release subnet,
  re-raise. `_docker_exec` surfaces returncode/timeout; treat 'duplicate/already exists' as success.
  `create_node`: replace `sleep(2)` with `_wait_for_ion_ready`, verify bpsink started, sweep
  leftover `/tmp/ion_n{id}_*`, status=ERROR on failure. depends_on #1.

- [ ] **#8 [HIGH] Single-writer RLock** — `docker_manager.py`, `api/websocket.py`. `threading.RLock`
  (reentrant: delete_node→delete_link). Guard all mutating methods; keep docker/subprocess work
  OUTSIDE the lock, hold only around dict mutations + SubnetPool allocate/release. Reserve-then-build
  with PENDING placeholder for ids. Snapshot under lock in `get_topology`. depends_on #2.

- [ ] **#9 [HIGH] Offload blocking calls + background stats poller** — `api/websocket.py`,
  `main.py`, `api/nodes.py`. Wrap blocking manager calls from coroutines in `asyncio.to_thread`.
  One background poll task gathers per-node stats on a cadence under the lock → ws + `/api/bundle-stats`
  serve the snapshot (docker load O(1)/interval). Make `get_node` async, stop mutating shared Node
  in a GET. depends_on #8.

- [ ] **#4 [CRITICAL] Bind 127.0.0.1 + caps (no token)** — `start.sh`, `main.py`, `api/scenarios.py`.
  `start.sh` `--host "${BIND_HOST:-127.0.0.1}"`. Enforce `MAX_NODES`/`MAX_LINKS` in create_node/
  create_link and at import. (Per decision: NO auth token.) No permissive CORS.

- [ ] **#5 [HIGH] Path-traversal confinement** — `api/scenarios.py`. `_safe_scenario_path(filename)`:
  `Path(name).name`, regex `^[A-Za-z0-9_.\-]+\.json$`, resolve + assert `is_relative_to(SCENARIOS_DIR)`.
  Route export-write, delete, import-from-disk through it. Sanitize `meta.name` on export.

- [ ] **#7 [MEDIUM] Decouple resource lifecycle from process** — `main.py`, `stop.sh`. Remove
  `cleanup_all()` from lifespan shutdown (gate behind `IONMGR_CLEANUP_ON_EXIT`). Rely on `reconcile()`
  at startup. `stop.sh` default = stop server, LEAVE resources; `--teardown` wipes. depends_on #1.

- [ ] **#6 [HIGH] Shared cleanup lib + script flags** — `start.sh`, `stop.sh`, `docker/lib_cleanup.sh`.
  Factor label-based teardown into a sourced function. `start.sh --clean` (opt-in full wipe, gated on
  no-live-PID), prune containers BEFORE networks. Guard stop.sh when `docker info` fails. depends_on #1.

- [ ] **#10 [MEDIUM] Validate imports before destructive cleanup + transactional** — `api/scenarios.py`,
  `models.py`. Extract ONE shared import helper (both `_do_import` + `_do_import_file` are dupes).
  Pydantic NodeSpec/LinkSpec/Topology, cap upload bytes + node/link counts BEFORE `cleanup_all()`,
  referential-integrity + version check before destroy, rollback + non-2xx on failure. depends_on #2.

- [ ] **#12 [MEDIUM] Shared ION admin parsers + declarative route reconcile** — `docker_manager.py`.
  One `parse_exit_line()`/`parse_plan_line()` (reuse working `list_exits` regex). `_propagate_routes`
  → diff desired (BFS) vs live, apply deltas, verify, surface errors. Fix `_clear_node_exits`
  (557-570 positional split bug) and `_list_plan_peers` (883-901 regex mismatch). depends_on #2.

- [ ] **#13 [MEDIUM] Fail-loud ION startup + readiness** — `docker/entrypoint.sh`, `docker/Dockerfile.ion`,
  `docker_manager.py`. entrypoint checks `ionstart` exit → `exit 1` on fail. `_wait_for_ion_ready`
  bool/raise contract; create_link gates on it (status=ERROR not UP). HEALTHCHECK via bpadmin probe.
  create_node reflects Exited container as failed status. depends_on #2.

- [ ] **#14 [MEDIUM] Fix bundle-stats counting/parsing** — `docker_manager.py`. `get_bundle_count`:
  `grep -c 'Payload delivered'` whole file (not tail -500). `get_bundle_stats`: poll for fresh bpstats
  marker (not sleep), bound read after marker, distinguish no-match from 0. Add log fixtures + tests.

- [ ] **#15 [MEDIUM] Central FastAPI error mapping + manager DI** — `api/*.py`, `main.py`. Register
  `add_exception_handler` for docker APIError→.status_code, NotFound→404, TimeoutExpired/BackendTimeout
  →504, ConflictError→409, ValueError→400, catch-all→500 (log real, return generic). Remove per-route
  catch-alls. Replace `manager=None` globals with `app.state.manager` + `get_manager` Depends (503 if None).

- [ ] **#16 [MEDIUM] Pin build provenance + harden export** — `docker/Dockerfile.ion`,
  `backend/requirements.txt`, `start.sh`, `backend/services/scenario_export.py`. `ARG ION_REF` pinned
  clone (+ same pin in scenario_export.py:64), pin base image + apt, `==` + hashes in requirements,
  HEALTHCHECK. Generated start.sh/stop.sh: `docker compose down --remove-orphans` FIRST (compose
  `name:` carries project), subnet sweep only secondary.

- [ ] **#17 [MEDIUM] Frontend hardening** — `frontend/js/app.js`. One `apiFetch()` (read text first,
  parse defensively, check `res.ok` BEFORE json — fixes the "Unexpected token <" that hides the 409).
  `makeResilientSocket()` with capped backoff+jitter for topology + log WS. `connectionState`
  indicator + gate mutating buttons. `runMutation(fn)` in-flight lock (anti double-submit). Stop
  optimistic terminal destroy; reconcile from server. Server topology/stats epoch id → flush stale
  bundleStats. depends_on #9.

- [ ] **VERIFY** — `python3 -m py_compile` all; restart via `./start.sh`; `curl /api/topology`;
  reproduce link-create (create N1,N2,N3 → link N1↔N2 succeeds); kill -9 server, leave networks,
  restart → confirm reconcile adopts/cleans and link-create still works (the original 409 is gone).

## How to resume
1. Read this file + [`AUDIT.md`](AUDIT.md) for full per-fix detail (problem/fix/prevention/risk).
2. Continue in dependency order: #3 → #1 → #2 → (#8,#12,#11,#14,#13) → #7 → #4 → #9 → #15 → #5 → #10
   → #6 → #16 → #17 → VERIFY.
3. `git -C dtn_manager status` / `git diff` shows all changes (clean baseline was commit `213961d`).
4. Done so far: `backend/errors.py`, `backend/settings.py`, `backend/services/ion_config.py`.
