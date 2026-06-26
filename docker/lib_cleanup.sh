#!/usr/bin/env bash
# lib_cleanup.sh — shared label-based teardown for DTN-Manager.
#
# Sourced by both start.sh (preflight orphan prune) and stop.sh (teardown) so
# the reaping logic lives in exactly one place. Reaps by the ionmgr.managed
# label. Containers are removed BEFORE networks, because a network with an
# attached container cannot be removed.

IONMGR_LABEL="${IONMGR_LABEL:-ionmgr.managed}"

# Returns 0 if the Docker daemon is reachable, 1 otherwise.
ionmgr_docker_ok() {
    docker info >/dev/null 2>&1
}

# Remove all containers and networks carrying the managed label.
# Prints what it does. Safe to run repeatedly (idempotent).
ionmgr_reap_managed() {
    if ! ionmgr_docker_ok; then
        echo "[!] Docker daemon not reachable — cannot reap managed resources" >&2
        return 1
    fi

    # Containers first (a network with an attached container won't remove).
    local containers
    containers=$(docker ps -aq --filter "label=${IONMGR_LABEL}" 2>/dev/null || true)
    if [[ -n "$containers" ]]; then
        echo "    Removing managed containers..."
        echo "$containers" | xargs -r docker rm -f >/dev/null 2>&1 || true
    fi

    # Then networks.
    local networks
    networks=$(docker network ls -q --filter "label=${IONMGR_LABEL}" 2>/dev/null || true)
    if [[ -n "$networks" ]]; then
        echo "    Removing managed networks..."
        echo "$networks" | xargs -r docker network rm >/dev/null 2>&1 || true
    fi

    # Secondary net: same-named networks that lost their label after a crash.
    local stale
    stale=$(docker network ls --filter "name=ionmgr_link_" -q 2>/dev/null || true)
    if [[ -n "$stale" ]]; then
        echo "$stale" | xargs -r docker network rm >/dev/null 2>&1 || true
    fi
    return 0
}
