import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, PermissionDenied
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class HandoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.field_a = Actor("field-a", "field", area="A")
        self.field_b = Actor("field-b", "field", area="B")
        self.epi = Actor("epi", "epidemiologist")

    def tearDown(self):
        self.tmp.cleanup()

    def _obs(self, actor, event_id, lat=40.0, lon=116.0, observed_at="2026-04-01"):
        return self.service.create(
            actor,
            "observation",
            {
                "event_id": event_id,
                "species": "deer",
                "location": "North",
                "observed_at": observed_at,
                "lat": lat,
                "lon": lon,
            },
        )

    def test_field_area_restriction(self):
        obs = self._obs(self.field_a, "E-1")
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.field_b,
                obs["id"],
                "submit",
                {"location": "North", "observed_at": "2026-04-01"},
            )

    def test_handover_transfers_records_with_stable_ids(self):
        o1 = self._obs(self.field_a, "E-1")
        o2 = self._obs(self.field_a, "E-2", lat=40.01, lon=116.01, observed_at="2026-04-03")
        handover = self.service.create(
            self.admin,
            "handover",
            {"from_area": "A", "to_area": "B", "record_ids": [o1["id"], o2["id"]]},
        )
        self.assertEqual(handover["status"], "completed")
        self.assertEqual(handover["data"]["to_area"], "B")
        for oid in (o1["id"], o2["id"]):
            transferred = self.service.get(oid)
            self.assertEqual(transferred["data"]["area"], "B")
        self.assertEqual(o1["id"], self.service.get(o1["id"])["id"])
        self.assertEqual(o2["id"], self.service.get(o2["id"])["id"])

    def test_handover_allows_receiving_field_to_continue(self):
        o1 = self._obs(self.field_a, "E-1")
        self.service.create(
            self.admin,
            "handover",
            {"from_area": "A", "to_area": "B", "record_ids": [o1["id"]]},
        )
        updated = self.service.transition(
            self.field_b,
            o1["id"],
            "submit",
            {"location": "North", "observed_at": "2026-04-01"},
        )
        self.assertEqual(updated["status"], "submitted")

    def test_sync_is_idempotent(self):
        record = {
            "kind": "observation",
            "id": "stable-obs-1",
            "data": {
                "event_id": "E-99",
                "species": "deer",
                "location": "North",
                "observed_at": "2026-04-01",
                "lat": 40.0,
                "lon": 116.0,
            },
        }
        first = self.service.sync(self.field_a, [record])
        self.assertEqual(len(first["created"]), 1)
        self.assertEqual(first["duplicates"], [])
        second = self.service.sync(self.field_a, [record])
        self.assertEqual(second["created"], [])
        self.assertEqual(second["duplicates"], ["stable-obs-1"])
        audits = self.service.audit_log(entity_id="stable-obs-1")
        sync_audits = [a for a in audits if a["action"] == "sync"]
        self.assertEqual(len(sync_audits), 1)

    def test_sync_skips_confirmed_records(self):
        obs = self._obs(self.field_a, "E-1")
        self.service.transition(
            self.field_a,
            obs["id"],
            "submit",
            {"location": "North", "observed_at": "2026-04-01"},
        )
        record = {
            "kind": "observation",
            "id": obs["id"],
            "status": "submitted",
            "data": obs["data"],
        }
        result = self.service.sync(self.field_a, [record])
        self.assertEqual(result["skipped"], [obs["id"]])
        self.assertEqual(result["created"], [])

    def test_cluster_invalidated_on_coordinate_change(self):
        o1 = self._obs(self.admin, "E-1")
        o2 = self._obs(self.admin, "E-2", lat=40.01, lon=116.01, observed_at="2026-04-03")
        o3 = self._obs(self.admin, "E-3", lat=40.02, lon=116.02, observed_at="2026-04-05")
        for obs in (o1, o2, o3):
            self.service.transition(
                self.admin,
                obs["id"],
                "submit",
                {"location": "North", "observed_at": obs["data"]["observed_at"]},
            )
        cluster = self.service.create(
            self.admin,
            "cluster",
            {"region": "North", "observation_ids": [o1["id"], o2["id"], o3["id"]]},
        )
        cluster = self.service.transition(
            self.epi,
            cluster["id"],
            "confirm_cluster",
            {"observation_ids": [o1["id"], o2["id"], o3["id"]], "centroid": [40.01, 116.01]},
        )
        self.assertEqual(cluster["status"], "confirmed")
        self.service.transition(
            self.admin,
            o1["id"],
            "correct",
            {"lat": 40.005, "lon": 116.0},
        )
        invalid = self.service.get(cluster["id"])
        self.assertEqual(invalid["status"], "invalid")
        self.assertAlmostEqual(invalid["data"]["centroid"][0], 40.011666, places=4)
        reconfirmed = self.service.transition(
            self.epi,
            cluster["id"],
            "confirm_cluster",
            {"observation_ids": [o1["id"], o2["id"], o3["id"]], "centroid": [40.011666, 116.01]},
        )
        self.assertEqual(reconfirmed["status"], "confirmed")

    def test_cluster_invalidated_on_time_change(self):
        o1 = self._obs(self.admin, "E-1")
        o2 = self._obs(self.admin, "E-2", lat=40.01, lon=116.01, observed_at="2026-04-03")
        o3 = self._obs(self.admin, "E-3", lat=40.02, lon=116.02, observed_at="2026-04-05")
        for obs in (o1, o2, o3):
            self.service.transition(
                self.admin,
                obs["id"],
                "submit",
                {"location": "North", "observed_at": obs["data"]["observed_at"]},
            )
        cluster = self.service.create(
            self.admin,
            "cluster",
            {"region": "North", "observation_ids": [o1["id"], o2["id"], o3["id"]]},
        )
        cluster = self.service.transition(
            self.epi,
            cluster["id"],
            "confirm_cluster",
            {"observation_ids": [o1["id"], o2["id"], o3["id"]], "centroid": [40.01, 116.01]},
        )
        self.assertEqual(cluster["status"], "confirmed")
        self.service.transition(
            self.admin,
            o1["id"],
            "correct",
            {"observed_at": "2026-05-01"},
        )
        invalid = self.service.get(cluster["id"])
        self.assertEqual(invalid["status"], "invalid")


if __name__ == "__main__":
    unittest.main()
