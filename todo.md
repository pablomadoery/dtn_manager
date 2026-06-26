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

### The 17 fixes — ALL DONE
- [x] **#11 Convergence-layer registry** — `ion_config.py` `CL_REGISTRY` + `resolve_cl()`;
  threaded through `docker_manager.py` runtime callsites (create_link, delete_link, repair,
  get_node_config plans, neighbors builders) AND the export generator (`scenario_export.py`).
  Unsupported CL (ltp) raises loudly instead of silently becoming TCP.

- [x] **#3 [CRITICAL] Hardened argv exec primitive** — `docker_manager.py` `_exec(container, argv,
  stdin=, timeout=)` via `subprocess.run([...], input=...)`. `_ion_exec` pipes the admin command on
  stdin; `send_bundle` is pure argv (RCE fixed); `_get_interface`/tc/tail/grep all argv.
  `TimeoutExpired`→`BackendTimeout`, non-zero→`IonExecError`. Only remaining `bash -c` is the
  bpsink launcher with `int(node_id)` (documented, no untrusted input).

- [x] **#1 [CRITICAL] Startup reconcile()/adopt** — `reconcile()` rebuilds nodes/links from labeled
  live resources (subnet from IPAM, IPs from `attrs['Containers']`), marks half-built links ERROR,
  reaps 0-container orphan networks, seeds `_next_node_id` + `SubnetPool.from_live()`. Explicit
  `ionmgr.node_id`/`ionmgr.link_id` labels at create. Called at end of `__init__`.

- [x] **#2 [HIGH] Idempotent get-or-create + transactional rollback** — `_get_or_create_network`
  (adopt existing / catch 409 race), create_link wrapped with rollback flags, create_node readiness-
  gated + bpsink-verified + `/tmp/ion_n{id}_*` sweep + status=ERROR on failure. Benign
  duplicate/already-exists classified as success (`_ion_apply`).

- [x] **#8 [HIGH] Single-writer RLock** — reentrant lock guards all mutations; docker/subprocess work
  stays outside the lock; reserve-then-build with CREATING/DOWN placeholders; `get_topology` snapshots
  under lock.

- [x] **#9 [HIGH] Offload + background stats poller** — `asyncio.to_thread` for every blocking call
  from a coroutine; one `_stats_poller` task in lifespan owns per-node stats; ws + `/api/bundle-stats`
  serve the snapshot. `get_node` async, no shared-state mutation in the GET.

- [x] **#4 [CRITICAL] Bind 127.0.0.1 + caps (no token)** — `start.sh --host "${BIND_HOST:-127.0.0.1}"`
  with an exposure warning; `MAX_NODES`/`MAX_LINKS` enforced in create_node/create_link and in the
  import schema. No CORS, no token (per decision).

- [x] **#5 [HIGH] Path-traversal confinement** — `_safe_scenario_path()` (`Path(name).name` + regex +
  `is_relative_to`) routes export-write, delete, import-from-disk. Export `meta.name` sanitized.

- [x] **#7 [MEDIUM] Decouple resource lifecycle from process** — lifespan shutdown preserves by default
  (gated behind `IONMGR_CLEANUP_ON_EXIT`); `stop.sh` default stops server + LEAVES resources,
  `--teardown` wipes.

- [x] **#6 [HIGH] Shared cleanup lib + script flags** — `docker/lib_cleanup.sh` sourced by both scripts;
  containers reaped before networks; `start.sh --clean` (gated on no-live-PID); `stop.sh` guards on
  `docker info` failure.

- [x] **#10 [MEDIUM] Validate imports before destructive cleanup + transactional** — one `_do_import`
  helper; Pydantic `ScenarioSpec` (bounded ids, capped counts, referential integrity, version,
  bounded upload bytes) validated BEFORE `cleanup_all()`; create failures roll back + return non-2xx.

- [x] **#12 [MEDIUM] Shared parsers + declarative route reconcile** — `parse_exit_line()`/
  `parse_plan_line()`; `_reconcile_node_exits` diffs BFS-desired vs live, applies deltas, verifies,
  returns errors. Old positional/regex bugs gone.

- [x] **#13 [MEDIUM] Fail-loud ION startup + readiness** — `entrypoint.sh` exits 1 on ionstart/bpadmin
  failure; `_wait_for_ion_ready` returns bool; create_link gates → status=ERROR; Dockerfile HEALTHCHECK
  via bpadmin; create_node reflects exited container as ERROR.

- [x] **#14 [MEDIUM] Fix bundle-stats counting/parsing** — `get_bundle_count` = whole-file `grep -c`;
  `get_bundle_stats` polls for a fresh bpstats marker and parses only lines after it; "parsed nothing"
  logged as a warning.

- [x] **#15 [MEDIUM] Central error mapping + manager DI** — `app.add_exception_handler` maps
  domain/docker/timeout errors to status codes (generic message, real logged); per-route catch-alls
  removed; `app.state.manager` + `get_manager` Depends (503 if None).

- [x] **#16 [MEDIUM] Pin provenance + harden export** — `ARG ION_REF` pinned clone (Dockerfile.ion +
  export generator), `--no-install-recommends`, HEALTHCHECK, `==`-pinned requirements. Generated
  start.sh/stop.sh run `docker compose down --remove-orphans` FIRST, subnet sweep secondary.

- [x] **#17 [MEDIUM] Frontend hardening** — `apiFetch()` (text-first defensive parse, ok-before-json);
  `makeResilientSocket()` (capped backoff+jitter, single timer, self-owned reconnect) for topology +
  log WS; `connectionState` indicator + button gating; `runMutation()` in-flight lock; no optimistic
  terminal destroy (reconcile from server); epoch/boot_id flush of stale bundleStats.

- [x] **VERIFY** — `python3 -m py_compile` (all backend) ✓; `node --check frontend/js/app.js` ✓;
  `python3 -m unittest backend.tests.test_hardening` (16 daemon-free tests) ✓; app imports + topology
  snapshot smoke ✓. **Runtime VERIFY (requires a Docker daemon, not available in this CI sandbox):**
  `./start.sh`; `curl /api/topology`; create N1,N2,N3 → link N1↔N2; `kill -9` server, leave networks,
  restart → confirm reconcile adopts/cleans and link-create still works (original 409 gone).

## Status
All 17 fixes implemented on branch `claude/todo-md-tasks-b84eb5`. Static verification complete;
runtime reproduction needs a host with Docker. Daemon-free unit tests live in
`backend/tests/test_hardening.py`.
