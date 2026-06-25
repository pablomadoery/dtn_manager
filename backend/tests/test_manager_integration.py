"""Daemon-free integration tests for DockerManager control flow.

Exercises the real create/link/delete/reconcile/rollback paths against an
in-memory fake docker client (backend.tests.fake_docker), so the transactional
and adoption fixes (#1, #2, #8) are covered without a Docker daemon:

    python3 -m unittest backend.tests.test_manager_integration
"""

import unittest
from unittest import mock

import docker

from backend.errors import ConflictError
from backend.tests.fake_docker import (
    FakeClient, FakeContainer, FakeNetwork, stub_runtime,
)


def _new_manager(containers=(), networks=()):
    client = FakeClient(containers=containers, networks=networks)
    with mock.patch("docker.from_env", return_value=client):
        from backend.services.docker_manager import DockerManager
        return stub_runtime(DockerManager())


class TestProvisioning(unittest.TestCase):
    def test_create_link_happy_path(self):
        m = _new_manager()
        m.create_node(1)
        m.create_node(2)
        link = m.create_link(1, 2)
        self.assertEqual(link.status.value, "up")
        self.assertEqual(link.subnet, "172.50.0.0/24")
        self.assertEqual((link.ip_a, link.ip_b), ("172.50.0.2", "172.50.0.3"))
        topo = m.get_topology()
        self.assertEqual(len(topo["nodes"]), 2)
        self.assertEqual(len(topo["links"]), 1)
        self.assertEqual(m.get_neighbors(1), {2})

    def test_duplicate_link_conflicts(self):
        m = _new_manager()
        m.create_node(1)
        m.create_node(2)
        m.create_link(1, 2)
        with self.assertRaises(ConflictError):
            m.create_link(1, 2)

    def test_delete_releases_subnet(self):
        m = _new_manager()
        m.create_node(1)
        m.create_node(2)
        m.create_link(1, 2)
        m.delete_link("1-2")
        self.assertEqual(m.get_topology()["links"], [])
        idx = m.subnet_pool.allocate()[0]
        self.assertEqual(idx, 0)  # released subnet is reused

    def test_create_link_rolls_back_on_failure(self):
        m = _new_manager()
        m.create_node(1)
        m.create_node(2)
        before = set(m.subnet_pool._in_use)
        m._connect = mock.Mock(side_effect=RuntimeError("connect failed"))
        with self.assertRaises(RuntimeError):
            m.create_link(1, 2)
        # No link recorded, subnet released, no orphan left behind.
        self.assertNotIn("1-2", m.links)
        self.assertEqual(set(m.subnet_pool._in_use), before)


class TestReconcile(unittest.TestCase):
    def _live(self):
        c1 = FakeContainer("ionmgr_n1",
                           {"ionmgr.managed": "true", "ionmgr.node_id": "1"})
        c2 = FakeContainer("ionmgr_n2",
                           {"ionmgr.managed": "true", "ionmgr.node_id": "2"})
        net = FakeNetwork(
            "ionmgr_link_1_2", "172.50.0.0/24",
            containers={
                "cid_ionmgr_n1": {"Name": "ionmgr_n1",
                                  "IPv4Address": "172.50.0.2/24"},
                "cid_ionmgr_n2": {"Name": "ionmgr_n2",
                                  "IPv4Address": "172.50.0.3/24"},
            },
            labels={"ionmgr.managed": "true", "ionmgr.link_id": "1-2"})
        orphan = FakeNetwork("ionmgr_link_3_4", "172.50.5.0/24",
                             containers={},
                             labels={"ionmgr.managed": "true"})
        return c1, c2, net, orphan

    def test_adopts_live_topology(self):
        c1, c2, net, orphan = self._live()
        m = _new_manager(containers=(c1, c2), networks=(net, orphan))
        self.assertEqual(set(m.nodes), {1, 2})
        self.assertIn("1-2", m.links)
        self.assertEqual(m.links["1-2"].status.value, "up")
        self.assertEqual((m.links["1-2"].ip_a, m.links["1-2"].ip_b),
                         ("172.50.0.2", "172.50.0.3"))

    def test_reaps_orphan_network(self):
        c1, c2, net, orphan = self._live()
        _new_manager(containers=(c1, c2), networks=(net, orphan))
        self.assertTrue(orphan.removed)  # 0-container network reaped
        self.assertFalse(net.removed)    # healthy network kept

    def test_seeds_counters_from_live(self):
        c1, c2, net, orphan = self._live()
        m = _new_manager(containers=(c1, c2), networks=(net, orphan))
        # next node id past the max adopted
        self.assertEqual(m._next_node_id(), 3)
        # adopted subnet index 0 is reserved, so allocation skips it
        self.assertNotEqual(m.subnet_pool.allocate()[0], 0)

    def test_link_create_after_reconcile_no_409(self):
        # Reproduces the original incident: adopt live N1<->N2, then create a
        # fresh N3 + link N2<->N3 must succeed (no subnet/name collision).
        c1, c2, net, orphan = self._live()
        m = _new_manager(containers=(c1, c2), networks=(net, orphan))
        m.create_node(3)
        link = m.create_link(2, 3)
        self.assertEqual(link.status.value, "up")
        self.assertNotEqual(link.subnet, "172.50.0.0/24")  # no reuse of N1<->N2


if __name__ == "__main__":
    unittest.main()
