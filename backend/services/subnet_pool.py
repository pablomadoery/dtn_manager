"""Subnet pool allocator for Docker bridge networks."""

from __future__ import annotations

from backend.errors import CapacityError


class SubnetPool:
    """Manages /24 subnets from a /16 block to avoid collisions.

    Allocates subnets like 172.50.0.0/24, 172.50.1.0/24, etc.
    Base is 172.50.x.x to avoid conflicts with existing scenarios
    (simple_ion=172.33, static_route_ion=172.34, continuous=172.35,
     periodic=172.36, disruption=172.37).

    The pool is a cache of what is actually live in Docker. On startup the
    manager calls :meth:`from_live` (via reconcile) to seed the in-use set
    from the subnets of adopted networks, so allocation can never re-issue a
    subnet that a crash survivor already occupies.
    """

    # Third octet is a single byte; 0..255 gives 256 distinct /24s.
    MAX_INDEX = 255

    def __init__(self, base_second_octet: int = 50):
        self.base = base_second_octet
        self._next_id = 0
        self._released: list[int] = []
        self._in_use: set[int] = set()

    def _subnet_tuple(self, link_id: int) -> tuple[int, str, str, str]:
        subnet = f"172.{self.base}.{link_id}.0/24"
        ip_a = f"172.{self.base}.{link_id}.2"
        ip_b = f"172.{self.base}.{link_id}.3"
        return link_id, subnet, ip_a, ip_b

    def allocate(self) -> tuple[int, str, str, str]:
        """Allocate a subnet. Returns (link_id, subnet, ip_a, ip_b)."""
        # Prefer reuse of explicitly released indices.
        while self._released:
            link_id = self._released.pop(0)
            if link_id not in self._in_use:
                self._in_use.add(link_id)
                return self._subnet_tuple(link_id)

        # Otherwise advance the counter, skipping anything already in use.
        while self._next_id <= self.MAX_INDEX:
            link_id = self._next_id
            self._next_id += 1
            if link_id in self._in_use:
                continue
            self._in_use.add(link_id)
            return self._subnet_tuple(link_id)

        raise CapacityError(
            f"Subnet pool exhausted: no free /24 in 172.{self.base}.0.0/16 "
            f"(max {self.MAX_INDEX + 1} links)."
        )

    def reserve(self, link_id: int) -> None:
        """Mark a specific index as in use (idempotent).

        Used by reconcile() to adopt the subnet of a live network and by
        create_link when it discovers an existing network whose real subnet
        differs from the one just allocated.
        """
        if link_id < 0 or link_id > self.MAX_INDEX:
            return
        self._in_use.add(link_id)
        if link_id in self._released:
            self._released.remove(link_id)
        if link_id >= self._next_id:
            self._next_id = link_id + 1

    def from_live(self, indices) -> None:
        """Seed the in-use set from live network subnet indices (reconcile)."""
        for idx in indices:
            self.reserve(idx)

    def release(self, link_id: int):
        """Return a subnet to the pool."""
        self._in_use.discard(link_id)
        if link_id not in self._released:
            self._released.append(link_id)
            self._released.sort()
