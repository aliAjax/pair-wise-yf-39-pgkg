from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .rules import RuleEngine, assert_field_region
from .repository import utcnow


GEO_TIME_FIELDS = ("lat", "lon", "observed_at")
SYNCABLE_KINDS = ("observation", "sample", "cluster")
RESERVED_FIELDS = {"id", "kind", "region", "status"}


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
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if expected_version is not None and int(expected_version) != entity["version"]:
            raise ConflictError(
                "version conflict: expected %s, found %s"
                % (expected_version, entity["version"])
            )
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )

        if entity["kind"] == "handover" and action == "accept":
            return self._accept_handover(actor, entity)

        merged = dict(entity["data"])
        merged.update(patch)

        if entity["kind"] == "cluster" and action == "reconfirm_cluster":
            merged.pop("invalidated", None)

        updated = self.repository.update_entity(entity_id, entity["version"], next_status, merged)

        if entity["kind"] == "observation" and action == "revise":
            changed = [
                field
                for field in GEO_TIME_FIELDS
                if field in patch and entity["data"].get(field) != updated["data"].get(field)
            ]
            invalidated = (
                self._invalidate_confirmed_clusters(actor, updated, "observation_revised")
                if changed else []
            )
            self.audit.record(
                entity_id, actor, action, entity["status"], updated["status"],
                {"patch": patch, "invalidated_clusters": invalidated},
            )
        else:
            self.audit.record(
                entity_id,
                actor,
                action,
                entity["status"],
                updated["status"],
                {"patch": patch},
            )
        return updated

    def _accept_handover(self, actor, handover):
        """Transfer every still-pending item, then close the handover.

        Items move one transaction at a time with per-item checkpoints, so a
        dropped connection can resume by simply calling accept again; done
        items are never moved or audited twice.
        """
        to_region = handover["data"]["to_region"]
        transferred = []
        for index, item in enumerate(handover["data"].get("items", [])):
            if item.get("status") == "done":
                continue
            result = self.repository.transfer_handover_item(
                handover["id"], item, to_region, actor, index
            )
            if result:
                transferred.append(result)

        current = self.repository.get_entity(handover["id"])
        all_done = all(
            item.get("status") == "done" for item in current["data"].get("items", [])
        )
        if not all_done:
            raise ConflictError("handover still has untransferred items")
        if current["status"] != "accepted":
            updated = self.repository.update_entity(
                handover["id"], None, "accepted", current["data"]
            )
            self.audit.record(
                handover["id"], actor, "accept", "in_transit", "accepted",
                {"transferred": transferred, "to_region": to_region},
            )
            return updated
        return current

    def _invalidate_confirmed_clusters(self, actor, observation, reason):
        """Any confirmed cluster referencing the observation loses confirmation.

        It must be recomputed and re-confirmed by an epidemiologist; field
        investigators cannot perform that action.
        """
        invalidated = []
        for cluster in self.repository.list_entities(kind="cluster", status="confirmed"):
            member_ids = cluster["data"].get("observation_ids") or []
            if observation["id"] not in member_ids:
                continue
            data = dict(cluster["data"])
            data["invalidated"] = {
                "observation_id": observation["id"],
                "reason": reason,
                "by": actor.user_id,
                "at": utcnow(),
            }
            self.repository.update_entity(cluster["id"], cluster["version"], "invalidated", data)
            self.audit.record(
                cluster["id"], actor, "auto_invalidate", "confirmed", "invalidated",
                {"reason": reason, "observation_id": observation["id"]},
            )
            invalidated.append(cluster["id"])
        return invalidated

    # ------------------------------------------------------------------
    # Offline reconnect: batch sync
    # ------------------------------------------------------------------

    def sync_batch(self, actor, batch_id, items):
        if not batch_id:
            raise ValidationError("batch_id is required")
        cached = self.repository.get_sync_batch(batch_id)
        # A redelivered batch is answered from its stored result: nothing is
        # created, merged or audited a second time.
        if cached is not None:
            cached["replayed"] = True
            return cached
        if actor.role == "field" and not actor.region:
            raise PermissionDenied("field investigator requires an assigned region")

        results = []
        for raw in items or []:
            try:
                results.append(self._sync_item(actor, batch_id, dict(raw)))
            except Exception as exc:
                results.append({
                    "id": raw.get("id") if isinstance(raw, dict) else None,
                    "kind": self.rules.normalize_kind(raw.get("kind"))
                    if isinstance(raw, dict) and raw.get("kind") else None,
                    "result": "error",
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                })

        summary = {
            "batch_id": batch_id,
            "results": results,
            "counts": self._count_results(results),
            "replayed": False,
        }
        self.repository.save_sync_batch(batch_id, actor.user_id, summary)
        return summary

    @staticmethod
    def _count_results(results):
        counts = {}
        for result in results:
            counts[result["result"]] = counts.get(result["result"], 0) + 1
        return counts

    def _sync_item(self, actor, batch_id, raw):
        payload = raw.pop("data", None)
        if payload is None:
            payload = raw
        kind = self.rules.normalize_kind(raw.get("kind") or payload.get("kind"))
        if kind not in SYNCABLE_KINDS:
            raise ValidationError("cannot sync kind: " + str(kind))
        data = {key: value for key, value in payload.items() if key not in RESERVED_FIELDS}
        entity_id = raw.get("id") or payload.get("id")

        existing = None
        if entity_id:
            existing = self.repository.get_entity(entity_id)
        if existing is None:
            existing = self._find_natural_twin(actor, kind, data)

        if existing is not None:
            return self._sync_merge(actor, batch_id, kind, existing, data)

        entity_id = str(entity_id or uuid4())
        self._validate_import(actor, kind, data)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(
            entity_id, kind, status, data, actor.user_id
        )
        self.audit.record(
            entity_id, actor, "create", None, status,
            {"kind": kind, "source": "sync", "batch_id": batch_id},
        )
        return {"id": entity_id, "kind": kind, "result": "created"}

    def _find_natural_twin(self, actor, kind, data):
        if kind == "observation":
            event_id = data.get("event_id")
            observed_at = data.get("observed_at")
            if not event_id or not observed_at:
                return None
            for row in self._lookup("observation", "event_id", event_id):
                if row["data"].get("observed_at") == observed_at:
                    return row
        elif kind == "sample":
            sample_code = data.get("sample_code")
            if not sample_code:
                return None
            rows = self._lookup("sample", "sample_code", sample_code)
            return rows[0] if rows else None
        return None

    def _sync_merge(self, actor, batch_id, kind, existing, incoming):
        assert_field_region(actor, existing["data"].get("region"))

        # Reconnect only merges the unconfirmed part: any cluster that has
        # already been confirmed (or dismissed / pending re-confirmation) is a
        # human decision and is never overwritten by a redelivered batch.
        if kind == "cluster" and existing["status"] != "draft":
            return {"id": existing["id"], "kind": kind,
                    "result": "skipped_confirmed", "status": existing["status"]}

        merged = dict(existing["data"])
        merged.update(incoming)
        merged["region"] = existing["data"].get("region")

        if merged == existing["data"]:
            # Duplicate delivery carrying no new information writes nothing.
            return {"id": existing["id"], "kind": kind, "result": "duplicate"}

        updated = self.repository.update_entity(
            existing["id"], existing["version"], existing["status"], merged
        )
        geo_changed = kind == "observation" and any(
            existing["data"].get(field) != updated["data"].get(field)
            for field in GEO_TIME_FIELDS
        )
        invalidated = (
            self._invalidate_confirmed_clusters(actor, updated, "sync_merge")
            if geo_changed else []
        )
        self.repository.append_audit(
            existing["id"], actor.user_id, actor.role, "sync_merge",
            existing["status"], updated["status"],
            {"batch_id": batch_id, "invalidated_clusters": invalidated},
            dedupe_key="sync:%s:%s" % (batch_id, existing["id"]),
        )
        return {"id": existing["id"], "kind": kind, "result": "merged",
                "invalidated_clusters": invalidated}

    def _validate_import(self, actor, kind, data):
        if kind == "cluster":
            if actor.role not in ("admin", "epidemiologist"):
                raise PermissionDenied("role %s cannot create clusters" % actor.role)
            if not data.get("region"):
                raise ValidationError("missing required field: region")
            return
        if actor.role not in ("admin", "field"):
            raise PermissionDenied("role %s cannot sync %s" % (actor.role, kind))
        if kind == "observation":
            for field in ("event_id", "species", "location", "observed_at", "lat", "lon"):
                if data.get(field) in (None, "", [], {}):
                    raise ValidationError("missing required field: " + field)
            if actor.role == "field":
                data["region"] = actor.region
        elif kind == "sample":
            for field in ("observation_id", "sample_code"):
                if data.get(field) in (None, "", [], {}):
                    raise ValidationError("missing required field: " + field)
            observation = self.repository.get_entity(data["observation_id"])
            if not observation:
                raise ValidationError(
                    "sample requires an existing observation: " + data["observation_id"]
                )
            data["region"] = observation["data"].get("region")

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None, region=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status, region=region)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
