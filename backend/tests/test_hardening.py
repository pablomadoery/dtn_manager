"""Daemon-free unit tests for the hardening fixes.

These cover the pure-logic invariants from the audit's prevention checklist:
ION admin parsing, subnet allocation seeding/exhaustion, convergence-layer
config generation, scenario filesystem confinement, and import schema
validation. They need neither a Docker daemon nor pytest:

    python3 -m unittest backend.tests.test_hardening
"""

import unittest

from backend.errors import CapacityError, ValidationError
from backend.services.subnet_pool import SubnetPool
from backend.services.docker_manager import parse_exit_line, parse_plan_line
from backend.services.ion_config import (
    resolve_cl,
    generate_add_plan_cmd,
    generate_add_outduct_cmd,
)


class TestAdminParsers(unittest.TestCase):
    def test_parse_exit_line(self):
        self.assertEqual(
            parse_exit_line("From 3 through 5, forward via ipn:2.0."),
            {"dest_first": 3, "dest_last": 5, "gateway_id": 2})

    def test_parse_exit_line_rejects_noise(self):
        self.assertIsNone(parse_exit_line(": Stopping ipnadmin."))
        self.assertIsNone(parse_exit_line(""))
        self.assertIsNone(parse_exit_line("garbage"))

    def test_parse_plan_line_variants(self):
        self.assertEqual(
            parse_plan_line("Egress plan for node number 2: tcp/172.50.0.3:4556"), 2)
        self.assertEqual(parse_plan_line("To node 5 via tcp/..."), 5)
        self.assertEqual(parse_plan_line("2 tcp/172.50.0.3:4556"), 2)
        # Must read the node id, never an IP octet.
        self.assertEqual(parse_plan_line("7 udp/10.40.0.3:4556"), 7)
        self.assertIsNone(parse_plan_line("no match here"))


class TestSubnetPool(unittest.TestCase):
    def test_from_live_seeds_past_max(self):
        p = SubnetPool(50)
        p.from_live([0, 2])
        idx, subnet, ip_a, ip_b = p.allocate()
        self.assertEqual(idx, 3)  # past the max adopted index
        self.assertEqual(subnet, "172.50.3.0/24")
        self.assertEqual((ip_a, ip_b), ("172.50.3.2", "172.50.3.3"))

    def test_no_collision_with_live(self):
        p = SubnetPool(50)
        p.from_live([0, 1, 2])
        allocated = {p.allocate()[0] for _ in range(3)}
        self.assertTrue(allocated.isdisjoint({0, 1, 2}))

    def test_release_is_reused(self):
        p = SubnetPool(50)
        a = p.allocate()[0]
        p.release(a)
        self.assertEqual(p.allocate()[0], a)

    def test_exhaustion_raises(self):
        p = SubnetPool(50)
        p.from_live(range(0, 256))
        with self.assertRaises(CapacityError):
            p.allocate()


class TestConvergenceLayer(unittest.TestCase):
    def test_plan_and_outduct_honor_cl(self):
        self.assertEqual(
            generate_add_plan_cmd(2, "1.2.3.4", "udpcl"),
            "a plan 2 udp/1.2.3.4:4556")
        self.assertEqual(
            generate_add_outduct_cmd("1.2.3.4", "stcp"),
            "a outduct stcp 1.2.3.4:4556 stcpclo")

    def test_unsupported_and_unknown_raise(self):
        with self.assertRaises(ValidationError):
            resolve_cl("ltpcl")  # recognized but not yet supported
        with self.assertRaises(ValidationError):
            resolve_cl("bogus")  # unknown


class TestScenarioPathConfinement(unittest.TestCase):
    def setUp(self):
        from fastapi import HTTPException
        from backend.api.scenarios import _safe_scenario_path, SCENARIOS_DIR
        self.HTTPException = HTTPException
        self.safe = _safe_scenario_path
        self.base = SCENARIOS_DIR.resolve()

    def test_legit_name_inside_dir(self):
        p = self.safe("my_scenario.json")
        self.assertEqual(p.parent, self.base)

    def test_traversal_reduced_to_basename(self):
        # '..' components are stripped to the basename inside SCENARIOS_DIR.
        self.assertEqual(self.safe("../../x.json").parent, self.base)

    def test_rejects_bad_names(self):
        for bad in ["/etc/passwd", "evil.json.txt", "no_ext", "weird name.json"]:
            with self.assertRaises(self.HTTPException):
                self.safe(bad)


class TestImportSchema(unittest.TestCase):
    def setUp(self):
        from backend.schemas import ScenarioSpec
        self.Spec = ScenarioSpec

    def test_valid(self):
        s = self.Spec.model_validate({
            "version": "1.2",
            "topology": {"nodes": [{"id": 1}, {"id": 2}],
                         "links": [{"node_a": 1, "node_b": 2}]},
        })
        self.assertEqual(len(s.topology.nodes), 2)

    def test_referential_integrity(self):
        with self.assertRaises(Exception):
            self.Spec.model_validate({
                "topology": {"nodes": [{"id": 1}],
                             "links": [{"node_a": 1, "node_b": 9}]}})

    def test_rejects_unknown_major_version(self):
        with self.assertRaises(Exception):
            self.Spec.model_validate({
                "version": "9.0",
                "topology": {"nodes": [{"id": 1}], "links": []}})

    def test_rejects_empty_and_dup(self):
        with self.assertRaises(Exception):
            self.Spec.model_validate({"topology": {"nodes": [], "links": []}})
        with self.assertRaises(Exception):
            self.Spec.model_validate({
                "topology": {"nodes": [{"id": 1}, {"id": 1}], "links": []}})


if __name__ == "__main__":
    unittest.main()
