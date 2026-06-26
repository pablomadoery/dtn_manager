"""A minimal in-memory fake of the docker SDK surface DockerManager uses.

Lets the manager's control flow (create/link/reconcile/rollback) be exercised
without a Docker daemon. Only the methods the manager actually calls are
implemented.
"""

from __future__ import annotations

import docker


class FakeContainer:
    def __init__(self, name, labels=None, status="running"):
        self.id = "cid_" + name
        self.name = name
        self.status = status
        self.labels = labels or {}

    def reload(self):
        pass

    def remove(self, force=False):
        pass


class FakeNetwork:
    def __init__(self, name, subnet, containers=None, labels=None):
        self.id = "nid_" + name
        self.name = name
        self.removed = False
        self.attrs = {
            "IPAM": {"Config": [{"Subnet": subnet}]},
            "Containers": containers or {},
            "Labels": labels or {},
        }

    def reload(self):
        pass

    def connect(self, container, ipv4_address=None):
        cid = getattr(container, "id", container)
        self.attrs["Containers"][cid] = {
            "Name": getattr(container, "name", str(container)),
            "IPv4Address": (ipv4_address or "") + "/24",
        }

    def disconnect(self, container, force=False):
        pass

    def remove(self):
        self.removed = True


class _Containers:
    def __init__(self):
        self._by_name = {}

    def add(self, container):
        self._by_name[container.name] = container

    def get(self, name):
        if name in self._by_name:
            return self._by_name[name]
        raise docker.errors.NotFound(name)

    def run(self, **kw):
        c = FakeContainer(kw["name"], labels=kw.get("labels", {}))
        self._by_name[kw["name"]] = c
        return c

    def list(self, all=False, filters=None):
        return list(self._by_name.values())


class _Networks:
    def __init__(self):
        self._by_name = {}

    def add(self, network):
        self._by_name[network.name] = network

    def get(self, name):
        net = self._by_name.get(name)
        if net is not None and not net.removed:
            return net
        raise docker.errors.NotFound(name)

    def create(self, name, driver, ipam, labels):
        subnet = ipam["Config"][0]["Subnet"]
        net = FakeNetwork(name, subnet, labels=labels)
        self._by_name[name] = net
        return net

    def list(self, filters=None):
        return [n for n in self._by_name.values() if not n.removed]


class FakeClient:
    def __init__(self, containers=(), networks=()):
        self.containers = _Containers()
        self.networks = _Networks()
        for c in containers:
            self.containers.add(c)
        for n in networks:
            self.networks.add(n)


def stub_runtime(manager):
    """Replace the real container-exec/readiness calls with no-op stubs."""
    import subprocess
    manager._exec = lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")
    manager._wait_for_ion_ready = lambda *a, **k: True
    manager._start_bpsink = lambda *a, **k: None
    manager._bpsink_running = lambda *a, **k: True
    return manager
