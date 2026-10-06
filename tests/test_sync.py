import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, PermissionDenied
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


OBS = {
    "event_id": "E-1",
    "species": "deer",
    "location": "North",
    "observed_at": "2026-04-01",
    "lat": 40.0,
    "lon": 116.0,
}


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.field_a = Actor("inv-A", "field", region="A")
        self.field_b = Actor("inv-B", "field", region="B")
        self.epi = Actor("epi", "epidemiologist")

    def tearDown(self):
        self.tmp.cleanup()

    def _audit_rows(self):
        return self.service.audit_log()

    def test_batch_replay_is_idempotent_and_creates_stable_ids(self):
        items = [
            {"kind": "observation", "id": "offline-o1", "data": OBS},
        ]
        first = self.service.sync_batch(self.field_a, "batch-1", items)
        self.assertEqual(first["counts"], {"created": 1})
        self.assertFalse(first["replayed"])

        # Same batch redelivered after reconnect: same stable id, no new audit.
        audit_before = len(self._audit_rows())
        replay = self.service.sync_batch(self.field_a, "batch-1", items)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["results"][0]["id"], "offline-o1")
        self.assertEqual(len(self._audit_rows()), audit_before)
        self.assertEqual(
            len(self.service.list("observation")), 1
        )

    def test_natural_key_twin_is_merged_not_duplicated(self):
        # Same event_id + observed_at captured independently by two devices.
        self.service.sync_batch(
            self.field_a, "batch-1",
            [{"kind": "observation", "id": "device-x-1", "data": OBS}],
        )
        second = self.service.sync_batch(
            self.field_a, "batch-2",
            [{"kind": "observation", "id": "device-y-1",
              "data": dict(OBS, location="North Ridge")}],
        )
        self.assertEqual(second["results"][0]["result"], "merged")
        observations = self.service.list("observation")
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["id"], "device-x-1")
        self.assertEqual(observations[0]["data"]["location"], "North Ridge")

    def test_unchanged_redelivery_writes_no_audit(self):
        self.service.sync_batch(
            self.field_a, "batch-1",
            [{"kind": "observation", "id": "o1", "data": OBS}],
        )
        audit_before = len(self._audit_rows())
        result = self.service.sync_batch(
            self.field_a, "batch-2",
            [{"kind": "observation", "id": "o1", "data": OBS}],
        )
        self.assertEqual(result["results"][0]["result"], "duplicate")
        self.assertEqual(len(self._audit_rows()), audit_before)

    def test_confirmed_cluster_is_not_overwritten_on_reconnect(self):
        obs1 = dict(OBS, id="o1")
        obs2 = dict(OBS, id="o2", event_id="E-2", observed_at="2026-04-03",
                    lat=40.01, lon=116.01)
        obs3 = dict(OBS, id="o3", event_id="E-3", observed_at="2026-04-05",
                    lat=40.02, lon=116.02)
        self.service.sync_batch(
            self.field_a, "base",
            [{"kind": "observation", "data": obs1},
             {"kind": "observation", "data": obs2},
             {"kind": "observation", "data": obs3}],
        )
        ids = [row["id"] for row in self.service.list("observation")]
        cluster = self.service.create(self.epi, "cluster", {"region": "A"})
        self.service.transition(
            self.epi, cluster["id"], "confirm_cluster",
            {"observation_ids": ids, "centroid": [40.01, 116.01]},
        )

        # A stale device tries to replace the confirmed cluster with a draft.
        result = self.service.sync_batch(
            self.field_a, "stale",
            [{"kind": "cluster", "id": cluster["id"],
              "data": {"region": "A", "observation_ids": [ids[0]]}}],
        )
        self.assertEqual(result["results"][0]["result"], "skipped_confirmed")
        stored = self.service.get(cluster["id"])
        self.assertEqual(stored["status"], "confirmed")
        self.assertEqual(len(stored["data"]["observation_ids"]), 3)
        # Draft clusters still merge normally.
        draft = self.service.create(self.epi, "cluster", {"region": "A"})
        merged = self.service.sync_batch(
            self.field_a, "draft-touch",
            [{"kind": "cluster", "id": draft["id"],
              "data": {"region": "A", "observation_ids": ids[:2]}}],
        )
        self.assertEqual(merged["results"][0]["result"], "merged")
        self.assertEqual(
            self.service.get(draft["id"])["data"]["observation_ids"], ids[:2]
        )

    def test_synced_geo_change_invalidates_confirmed_cluster_once(self):
        obs1 = dict(OBS)
        obs2 = dict(OBS, event_id="E-2", observed_at="2026-04-03",
                    lat=40.01, lon=116.01)
        obs3 = dict(OBS, event_id="E-3", observed_at="2026-04-05",
                    lat=40.02, lon=116.02)
        self.service.sync_batch(
            self.field_a, "base",
            [{"kind": "observation", "id": "o1", "data": obs1},
             {"kind": "observation", "id": "o2", "data": obs2},
             {"kind": "observation", "id": "o3", "data": obs3}],
        )
        cluster = self.service.create(self.epi, "cluster", {"region": "A"})
        self.service.transition(
            self.epi, cluster["id"], "confirm_cluster",
            {"observation_ids": ["o1", "o2", "o3"],
             "centroid": [40.01, 116.01]},
        )

        # Offline edit of a sampling coordinate arrives on reconnect.
        result = self.service.sync_batch(
            self.field_a, "geo",
            [{"kind": "observation", "id": "o1",
              "data": dict(obs1, lat=41.0, lon=117.0)}],
        )
        self.assertEqual(result["results"][0]["invalidated_clusters"], [cluster["id"]])
        self.assertEqual(self.service.get(cluster["id"])["status"], "invalidated")

        # Replaying or redelivering the same batch does not invalidate or audit twice.
        audit_before = len(self._audit_rows())
        self.service.sync_batch(
            self.field_a, "geo",
            [{"kind": "observation", "id": "o1",
              "data": dict(obs1, lat=41.0, lon=117.0)}],
        )
        self.assertEqual(len(self._audit_rows()), audit_before)

    def test_field_sync_is_confined_to_own_district(self):
        self.service.sync_batch(
            self.field_a, "seed",
            [{"kind": "observation", "id": "a1", "data": OBS}],
        )
        # District B cannot merge into district A's record.
        result = self.service.sync_batch(
            self.field_b, "cross",
            [{"kind": "observation", "id": "a1",
              "data": dict(OBS, location="Hijack")}],
        )
        self.assertEqual(result["results"][0]["result"], "error")
        self.assertEqual(result["results"][0]["error_type"], "PermissionDenied")
        self.assertEqual(
            self.service.get("a1")["data"]["location"], "North"
        )

    def test_sample_merges_by_sample_code_and_inherits_region(self):
        self.service.sync_batch(
            self.field_a, "obs",
            [{"kind": "observation", "id": "o1", "data": OBS}],
        )
        first = self.service.sync_batch(
            self.field_a, "s1",
            [{"kind": "sample", "id": "s1",
              "data": {"observation_id": "o1", "sample_code": "W-1"}}],
        )
        self.assertEqual(first["results"][0]["result"], "created")
        twin = self.service.sync_batch(
            self.field_a, "s2",
            [{"kind": "sample", "id": "other-device-id",
              "data": {"observation_id": "o1", "sample_code": "W-1",
                       "note": "field tag 7"}}],
        )
        self.assertEqual(twin["results"][0]["result"], "merged")
        samples = self.service.list("sample")
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["id"], "s1")
        self.assertEqual(samples[0]["data"]["region"], "A")
        self.assertEqual(samples[0]["data"]["note"], "field tag 7")


if __name__ == "__main__":
    unittest.main()
