import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def make_observation(service, actor, event_id, day, lat=40.0, lon=116.0):
    entity = service.create(
        actor,
        "observation",
        {
            "event_id": event_id,
            "species": "deer",
            "location": "North",
            "observed_at": "2026-04-%02d" % day,
            "lat": lat,
            "lon": lon,
        },
    )
    service.transition(
        actor, entity["id"], "submit",
        {"location": "North", "observed_at": entity["data"]["observed_at"]},
    )
    return entity


class HandoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.field_a = Actor("inv-A", "field", region="A")
        self.field_b = Actor("inv-B", "field", region="B")
        self.field_c = Actor("inv-C", "field", region="C")
        self.epi = Actor("epi", "epidemiologist")

    def tearDown(self):
        self.tmp.cleanup()

    def _three_observations_and_cluster(self):
        o1 = make_observation(self.service, self.field_a, "E-1", 1)
        o2 = make_observation(self.service, self.field_a, "E-2", 3, 40.01, 116.01)
        o3 = make_observation(self.service, self.field_a, "E-3", 5, 40.02, 116.02)
        sample = self.service.create(
            self.field_a, "sample",
            {"observation_id": o1["id"], "sample_code": "W-1"},
        )
        cluster = self.service.create(self.epi, "cluster", {"region": "A"})
        self.service.transition(
            self.epi, cluster["id"], "confirm_cluster",
            {"observation_ids": [o1["id"], o2["id"], o3["id"]],
             "centroid": [40.01, 116.01]},
        )
        return o1, o2, o3, sample, cluster

    def test_records_follow_handover_with_stable_ids_and_single_audit(self):
        o1, o2, o3, sample, cluster = self._three_observations_and_cluster()
        original_ids = [o1["id"], o2["id"], o3["id"], sample["id"], cluster["id"]]

        handover = self.service.create(
            self.field_a, "handover",
            {"from_region": "A", "to_region": "B",
             "items": [
                 {"id": o1["id"], "kind": "observation"},
                 {"id": o2["id"], "kind": "observation"},
                 {"id": o3["id"], "kind": "observation"},
                 sample["id"],
                 cluster["id"],
             ]},
        )
        self.service.transition(self.field_a, handover["id"], "dispatch", {})
        accepted = self.service.transition(self.field_b, handover["id"], "accept", {})

        self.assertEqual(accepted["status"], "accepted")
        # Every record keeps its stable id and now belongs to district B.
        for entity_id in original_ids:
            entity = self.service.get(entity_id)
            self.assertEqual(entity["data"]["region"], "B", entity_id)
        # A confirmed cluster is handed over as-is; the receiving district
        # inherits the confirmed event instead of nobody owning it.
        self.assertEqual(self.service.get(cluster["id"])["status"], "confirmed")

        # Exactly one transfer audit row per item; a second accept is idempotent
        # and writes neither new transfers nor new audit rows.
        audit = self.service.audit_log()
        transfers = [row for row in audit if row["action"] == "transfer"]
        self.assertEqual(len(transfers), 5)
        self.assertEqual({row["entity_id"] for row in transfers}, set(original_ids))
        for row in transfers:
            self.assertEqual(row["from_status"], "A")
            self.assertEqual(row["to_status"], "B")

        audit_count_before = len(self.service.audit_log())
        again = self.service.transition(self.field_b, handover["id"], "accept", {})
        self.assertEqual(again["status"], "accepted")
        self.assertEqual(len(self.service.audit_log()), audit_count_before)

    def test_interrupted_handover_resumes_without_double_work(self):
        o1, o2, o3, sample, cluster = self._three_observations_and_cluster()
        items = [o1["id"], o2["id"], o3["id"], sample["id"], cluster["id"]]
        handover = self.service.create(
            self.admin, "handover",
            {"from_region": "A", "to_region": "B",
             "items": [{"id": item_id} for item_id in items]},
        )
        self.service.transition(self.admin, handover["id"], "dispatch", {})

        # Simulate a disconnect after the first two items moved: mark them done
        # by driving the repository transfer directly, then accept resumes.
        self.repo.transfer_handover_item(
            handover["id"], {"id": o1["id"], "kind": "observation"},
            "B", self.field_b, 0,
        )
        self.repo.transfer_handover_item(
            handover["id"], {"id": o2["id"], "kind": "observation"},
            "B", self.field_b, 1,
        )
        self.assertEqual(self.service.get(o1["id"])["data"]["region"], "B")
        self.assertEqual(self.service.get(o3["id"])["data"]["region"], "A")

        accepted = self.service.transition(self.field_b, handover["id"], "accept", {})
        self.assertEqual(accepted["status"], "accepted")
        for entity_id in items:
            self.assertEqual(self.service.get(entity_id)["data"]["region"], "B")
        # Retransferring an already-done item performs no write.
        self.assertIsNone(
            self.repo.transfer_handover_item(
                handover["id"], {"id": o1["id"], "kind": "observation"},
                "B", self.field_b, 0,
            )
        )
        self.assertEqual(
            len([row for row in self.service.audit_log() if row["action"] == "transfer"]),
            5,
        )

    def test_field_cannot_touch_other_districts(self):
        o1 = make_observation(self.service, self.field_a, "E-1", 1)
        # A third district cannot revise or move A's records.
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.field_c, o1["id"], "revise", {"lat": 40.5}
            )
        # Only the outgoing district can open the handover.
        with self.assertRaises(PermissionDenied):
            self.service.create(
                self.field_c, "handover",
                {"from_region": "A", "to_region": "C",
                 "items": [{"id": o1["id"], "kind": "observation"}]},
            )
        handover = self.service.create(
            self.field_a, "handover",
            {"from_region": "A", "to_region": "B",
             "items": [{"id": o1["id"], "kind": "observation"}]},
        )
        self.service.transition(self.field_a, handover["id"], "dispatch", {})
        # District C cannot accept B's inbound handover.
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.field_c, handover["id"], "accept", {})
        # Field investigators cannot create or confirm clusters.
        with self.assertRaises(PermissionDenied):
            self.service.create(self.field_a, "cluster", {"region": "A"})

    def test_geo_or_time_change_after_handover_invalidates_cluster_for_epi(self):
        o1, o2, o3, sample, cluster = self._three_observations_and_cluster()
        handover = self.service.create(
            self.field_a, "handover",
            {"from_region": "A", "to_region": "B",
             "items": [
                 {"id": o1["id"], "kind": "observation"},
                 {"id": o2["id"], "kind": "observation"},
                 {"id": o3["id"], "kind": "observation"},
                 cluster["id"],
             ]},
        )
        self.service.transition(self.field_a, handover["id"], "dispatch", {})
        self.service.transition(self.field_b, handover["id"], "accept", {})

        # District A no longer controls the records after handover.
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.field_a, o1["id"], "revise", {"lat": 41.0}
            )

        # Receiving investigator moves a sampling coordinate far away.
        self.service.transition(
            self.field_b, o1["id"], "revise", {"lat": 41.0, "lon": 117.0}
        )
        invalidated = self.service.get(cluster["id"])
        self.assertEqual(invalidated["status"], "invalidated")
        self.assertEqual(invalidated["data"]["invalidated"]["observation_id"], o1["id"])
        invalidation_rows = [
            row for row in self.service.audit_log(cluster["id"])
            if row["action"] == "auto_invalidate"
        ]
        self.assertEqual(len(invalidation_rows), 1)

        # A field investigator cannot re-confirm; only the epidemiologist can,
        # and recomputation must still detect a cluster first.
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.field_b, cluster["id"], "reconfirm_cluster", {}
            )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.epi, cluster["id"], "reconfirm_cluster", {}
            )

        # Epi restores a consistent geometry, recomputes and re-confirms.
        self.service.transition(
            self.field_b, o1["id"], "revise", {"lat": 40.0, "lon": 116.0}
        )
        reconfirmed = self.service.transition(
            self.epi, cluster["id"], "reconfirm_cluster", {}
        )
        self.assertEqual(reconfirmed["status"], "confirmed")
        self.assertNotIn("invalidated", reconfirmed["data"])

        # A further sampling-time change invalidates immediately again.
        self.service.transition(
            self.field_b, o1["id"], "revise", {"observed_at": "2026-05-20"}
        )
        self.assertEqual(self.service.get(cluster["id"])["status"], "invalidated")
        self.assertEqual(
            len([row for row in self.service.audit_log(cluster["id"])
                 if row["action"] == "auto_invalidate"]),
            2,
        )
        dismissed = self.service.transition(
            self.epi, cluster["id"], "dismiss", {"reason": "no longer clustered"}
        )
        self.assertEqual(dismissed["status"], "dismissed")


if __name__ == "__main__":
    unittest.main()
