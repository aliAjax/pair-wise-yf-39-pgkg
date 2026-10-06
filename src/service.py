from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import RuleEngine, is_confirmed


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        payload = self.rules.validate_create(actor, kind, payload, self._lookup)
        if kind == "handover":
            return self._create_handover(actor, payload)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def _create_handover(self, actor, payload):
        from_area = payload["from_area"]
        to_area = payload["to_area"]
        record_ids = list(payload["record_ids"])
        self.repository.transfer_area(record_ids, to_area)
        entity_id = str(uuid4())
        handover_data = {
            "from_area": from_area,
            "to_area": to_area,
            "record_ids": record_ids,
        }
        entity = self.repository.create_entity(
            entity_id, "handover", "completed", handover_data, actor.user_id
        )
        self.audit.record(
            entity_id,
            actor,
            "create",
            None,
            "completed",
            {"kind": "handover", "from_area": from_area, "to_area": to_area},
        )
        for rid in record_ids:
            record = self.repository.get_entity(rid)
            current_status = record["status"] if record else None
            self.audit.record(
                rid,
                actor,
                "handover",
                current_status,
                current_status,
                {"from_area": from_area, "to_area": to_area},
            )
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        if entity["kind"] == "observation":
            self._invalidate_affected_clusters(entity, merged, actor)
        return updated

    def _invalidate_affected_clusters(self, old_entity, new_data, actor):
        old_data = old_entity["data"]
        changed = (
            old_data.get("lat") != new_data.get("lat")
            or old_data.get("lon") != new_data.get("lon")
            or old_data.get("observed_at") != new_data.get("observed_at")
        )
        if not changed:
            return
        for cluster in self.repository.list_entities(kind="cluster"):
            if cluster["status"] != "confirmed":
                continue
            if old_entity["id"] in cluster["data"].get("observation_ids", []):
                self._invalidate_cluster(cluster, actor)

    def _invalidate_cluster(self, cluster, actor):
        obs_ids = cluster["data"].get("observation_ids", [])
        centroid = self._compute_centroid(obs_ids)
        data = dict(cluster["data"])
        data["centroid"] = centroid
        updated = self.repository.update_entity(cluster["id"], None, "invalid", data)
        self.audit.record(
            cluster["id"],
            actor,
            "invalidate",
            "confirmed",
            "invalid",
            {"reason": "observation coordinates or sampling time changed", "centroid": centroid},
        )
        return updated

    def _compute_centroid(self, obs_ids):
        lats, lons = [], []
        for oid in obs_ids:
            obs = self.repository.get_entity(oid)
            if obs and obs["kind"] == "observation":
                lat = obs["data"].get("lat")
                lon = obs["data"].get("lon")
                if lat is not None and lon is not None:
                    lats.append(lat)
                    lons.append(lon)
        if not lats:
            return None
        return [sum(lats) / len(lats), sum(lons) / len(lons)]

    def sync(self, actor, records):
        created, duplicates, skipped = [], [], []
        for record in records:
            kind = self.rules.normalize_kind(record.get("kind"))
            stable_id = record.get("id")
            if not stable_id:
                raise ValidationError("sync record requires a stable id")
            status = record.get("status") or self.rules.initial_status(kind)
            if is_confirmed(kind, status):
                skipped.append(stable_id)
                continue
            payload = dict(record.get("data") or {})
            if actor.area is not None:
                payload["area"] = actor.area
            if self.repository.get_entity(stable_id):
                duplicates.append(stable_id)
                continue
            entity = self.repository.create_entity(
                stable_id, kind, status, payload, actor.user_id
            )
            self.audit.record(
                stable_id, actor, "sync", None, status, {"kind": kind}
            )
            created.append(entity)
        return {"created": created, "duplicates": duplicates, "skipped": skipped}

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
