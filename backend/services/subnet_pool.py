"""Subnet pool allocator for Docker bridge networks."""


class SubnetPool:
    """Manages /24 subnets from a /16 block to avoid collisions.
    
    Allocates subnets like 172.50.0.0/24, 172.50.1.0/24, etc.
    Base is 172.50.x.x to avoid conflicts with existing scenarios
    (simple_ion=172.33, static_route_ion=172.34, continuous=172.35,
     periodic=172.36, disruption=172.37).
    """

    def __init__(self, base_second_octet: int = 50):
        self.base = base_second_octet
        self._next_id = 0
        self._released: list[int] = []

    def allocate(self) -> tuple[int, str, str, str]:
        """Allocate a subnet. Returns (link_id, subnet, ip_a, ip_b)."""
        if self._released:
            link_id = self._released.pop(0)
        else:
            link_id = self._next_id
            self._next_id += 1

        subnet = f"172.{self.base}.{link_id}.0/24"
        ip_a = f"172.{self.base}.{link_id}.2"
        ip_b = f"172.{self.base}.{link_id}.3"
        return link_id, subnet, ip_a, ip_b

    def release(self, link_id: int):
        """Return a subnet to the pool."""
        if link_id not in self._released:
            self._released.append(link_id)
            self._released.sort()
