from datetime import datetime

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


SAME_STATUS = "__same__"
REGION_SCOPED_KINDS = ("observation", "sample", "cluster")


def _parse_date(value):
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (ValueError, TypeError):
        raise ValidationError("invalid date: " + str(value))


def _date_ordinal(value):
    return _parse_date(value).toordinal()


def _as_float(value, field):
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValidationError(field + " must be a number")


def assert_field_region(actor, region):
    """Field investigators may only touch records of their own district."""
    if actor.role != "field":
        return
    if not getattr(actor, "region", None):
        raise PermissionDenied("field investigator requires an assigned region")
    if not region:
        raise PermissionDenied("record is not assigned to any district")
    if actor.region != region:
        raise PermissionDenied(
            "district %s is outside your assigned district %s" % (region, actor.region)
        )


def _validate_observation(actor, data, lookup):
    rows = lookup("observation", "event_id", data.get("event_id")) or [] if lookup else []
    for row in rows:
        if row["data"].get("observed_at") == data.get("observed_at"):
            raise ConflictError("duplicate observation event")
    if not data.get("species"):
        raise ValidationError("species is required")
    for coord in ("lat", "lon"):
        if coord in data:
            _as_float(data[coord], coord)
    if "observed_at" in data:
        _parse_date(data["observed_at"])
    if actor.role == "field":
        if not getattr(actor, "region", None):
            raise PermissionDenied("field investigator requires an assigned region")
        target = data.get("region") or actor.region
        if target != actor.region:
            raise PermissionDenied(
                "cannot create records in district %s" % target
            )
        data["region"] = actor.region


def _validate_sample(actor, data, lookup):
    observation = _find_one(lookup, "observation", "id", data.get("observation_id"))
    if not observation or observation["status"] not in ("submitted", "sampled"):
        raise ValidationError("sample requires a submitted observation")
    region = observation["data"].get("region")
    assert_field_region(actor, region)
    # Samples are maintained in the same district as their observation.
    data["region"] = region


def _validate_lab_result(actor, entity, data, lookup):
    if data.get("result", "").lower() not in ("positive", "negative"):
        raise ValidationError("lab result must be positive or negative")


def _validate_revise_observation(actor, entity, data, lookup):
    allowed = {"lat", "lon", "observed_at", "location", "species"}
    extra = set(data) - allowed
    if extra:
        raise ValidationError("field %s cannot be revised" % sorted(extra)[0])
    if not data:
        raise ValidationError("no fields supplied")
    patch = {}
    for field in ("lat", "lon"):
        if field in data:
            patch[field] = _as_float(data[field], field)
    if "observed_at" in data:
        _parse_date(data["observed_at"])
        patch["observed_at"] = data["observed_at"]
    for field in ("location", "species"):
        if field in data:
            patch[field] = data[field]
    return patch


def _cluster_points(observation_ids, lookup):
    points = []
    for observation_id in observation_ids or []:
        observation = _find_one(lookup, "observation", "id", observation_id)
        if not observation:
            raise ValidationError("observation not found: " + str(observation_id))
        data = observation["data"]
        for field in ("lat", "lon", "observed_at"):
            if field not in data:
                raise ValidationError("observation %s lacks %s" % (observation_id, field))
        points.append(
            {
                "id": observation["id"],
                "lat": _as_float(data["lat"], "lat"),
                "lon": _as_float(data["lon"], "lon"),
                "observed_at": data["observed_at"],
            }
        )
    return points


def _validate_confirm_cluster(actor, entity, data, lookup):
    observation_ids = data.get("observation_ids")
    if not observation_ids:
        raise ValidationError("observation_ids is required")
    points = _cluster_points(observation_ids, lookup)
    if not is_cluster(points):
        raise ValidationError("selected observations do not form a cluster")


def _validate_reconfirm_cluster(actor, entity, data, lookup):
    observation_ids = data.get("observation_ids") or entity["data"].get("observation_ids")
    if not observation_ids:
        raise ValidationError("cluster has no observation_ids to recompute")
    points = _cluster_points(observation_ids, lookup)
    if not is_cluster(points):
        raise ValidationError(
            "observations no longer form a cluster; dismiss instead of confirming"
        )
    return {"observation_ids": list(observation_ids)}


def _normalize_handover_items(raw_items, lookup):
    normalized = []
    for raw in raw_items or []:
        if isinstance(raw, dict):
            item_id = raw.get("id")
            kind = raw.get("kind")
        else:
            item_id, kind = raw, None
        if not item_id:
            raise ValidationError("handover item requires an id")
        if kind is not None:
            kind = RuleEngine.ALIASES.get(kind, kind)
        if kind is None:
            for candidate in ("observation", "sample", "cluster"):
                if _find_one(lookup, candidate, "id", item_id):
                    kind = candidate
                    break
        if kind is None:
            raise ValidationError("handover item not found: " + str(item_id))
        if kind not in ("observation", "sample", "cluster"):
            raise ValidationError("cannot hand over kind: " + kind)
        normalized.append({"id": item_id, "kind": kind, "status": "pending"})
    if not normalized:
        raise ValidationError("handover requires at least one item")
    return normalized


def _validate_handover_create(actor, data, lookup):
    if data["from_region"] == data["to_region"]:
        raise ValidationError("from_region and to_region must differ")
    if actor.role == "field" and actor.region != data["from_region"]:
        raise PermissionDenied(
            "only the outgoing district can open a handover"
        )
    data["items"] = _normalize_handover_items(data.get("items"), lookup)


def _handover_items(entity, lookup):
    items = []
    for item in entity["data"].get("items", []):
        row = _find_one(lookup, item["kind"], "id", item["id"])
        if not row:
            raise ValidationError("handover item not found: " + item["id"])
        items.append((item, row))
    return items


def _validate_handover_dispatch(actor, entity, data, lookup):
    if actor.role == "field" and actor.region != entity["data"]["from_region"]:
        raise PermissionDenied("only the outgoing district can dispatch a handover")
    for item, row in _handover_items(entity, lookup):
        region = row["data"].get("region")
        if region not in (entity["data"]["from_region"], entity["data"]["to_region"]):
            raise ValidationError(
                "item %s belongs to %s, not %s"
                % (item["id"], region, entity["data"]["from_region"])
            )


def _validate_handover_accept(actor, entity, data, lookup):
    if actor.role == "field" and actor.region != entity["data"]["to_region"]:
        raise PermissionDenied("only the receiving district can accept a handover")
    for item, row in _handover_items(entity, lookup):
        region = row["data"].get("region")
        if region not in (entity["data"]["from_region"], entity["data"]["to_region"]):
            raise ValidationError("item %s is outside the handover" % item["id"])


def _haversine_km(lat1, lon1, lat2, lon2):
    from math import asin, cos, radians, sin, sqrt
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 6371.0 * 2 * asin(sqrt(a))


def is_cluster(observations, max_days=14, radius_km=10):
    if len(observations) < 3:
        return False
    points = observations[:3]
    same_window = all(
        abs(_date_ordinal(points[0].get("observed_at")) - _date_ordinal(item.get("observed_at"))) <= max_days
        for item in points[1:]
    )
    close = all(
        _haversine_km(points[0]["lat"], points[0]["lon"], item["lat"], item["lon"]) <= radius_km
        for item in points[1:]
    )
    return same_window and close


CUSTOM_CREATE = {
    'observation': _validate_observation,
    'sample': _validate_sample,
    'handover': _validate_handover_create,
}
CUSTOM_TRANSITIONS = {
    ('sample', 'lab_result'): _validate_lab_result,
    ('observation', 'revise'): _validate_revise_observation,
    ('cluster', 'confirm_cluster'): _validate_confirm_cluster,
    ('cluster', 'reconfirm_cluster'): _validate_reconfirm_cluster,
    ('handover', 'dispatch'): _validate_handover_dispatch,
    ('handover', 'accept'): _validate_handover_accept,
}


class RuleEngine:
    ALIASES = {
        'observations': 'observation',
        'samples': 'sample',
        'clusters': 'cluster',
        'handovers': 'handover',
    }
    INITIAL_STATUS = {
        'observation': 'captured',
        'sample': 'collected',
        'cluster': 'draft',
        'handover': 'draft',
    }
    TRANSITIONS = {
        'observation': {
            'submit': (('captured',), 'submitted'),
            'reject': (('submitted',), 'rejected'),
            'link_sample': (('submitted',), 'sampled'),
            'revise': (('captured', 'submitted', 'sampled'), SAME_STATUS),
        },
        'sample': {
            'send_lab': (('collected',), 'in_lab'),
            'lab_result': (('in_lab',), 'resulted'),
            'retest': (('resulted',), 'in_lab'),
            'close': (('resulted',), 'closed'),
        },
        'cluster': {
            'confirm_cluster': (('draft',), 'confirmed'),
            'reconfirm_cluster': (('invalidated',), 'confirmed'),
            'dismiss': (('draft', 'invalidated'), 'dismissed'),
        },
        'handover': {
            'dispatch': (('draft',), 'in_transit'),
            # accept from "accepted" is the idempotent replay of a resumed
            # handover: validation still runs, but the service writes nothing.
            'accept': (('in_transit', 'accepted'), 'accepted'),
        },
    }
    CREATE_REQUIRED = {
        'observation': ('event_id', 'species', 'location', 'observed_at', 'lat', 'lon'),
        'sample': ('observation_id', 'sample_code'),
        'cluster': ('region',),
        'handover': ('from_region', 'to_region', 'items'),
    }
    ACTION_REQUIRED = {
        ('observation', 'submit'): ('location', 'observed_at'),
        ('observation', 'reject'): ('reason',),
        ('observation', 'link_sample'): ('sample_id',),
        ('sample', 'send_lab'): ('lab_id',),
        ('sample', 'lab_result'): ('result', 'result_at'),
        ('sample', 'retest'): ('reason',),
        ('sample', 'close'): ('outcome',),
        ('cluster', 'confirm_cluster'): ('observation_ids', 'centroid'),
        ('cluster', 'dismiss'): ('reason',),
    }
    CREATE_ROLES = {
        'observation': ('admin', 'field'),
        'sample': ('admin', 'field'),
        'cluster': ('admin', 'epidemiologist'),
        'handover': ('admin', 'field'),
    }
    ROLE_ACTIONS = {
        'submit': ('admin', 'field'),
        'reject': ('admin', 'epidemiologist'),
        'link_sample': ('admin', 'field'),
        'revise': ('admin', 'field'),
        'send_lab': ('admin', 'field'),
        'lab_result': ('admin', 'lab'),
        'retest': ('admin', 'lab'),
        'close': ('admin', 'epidemiologist'),
        'confirm_cluster': ('admin', 'epidemiologist'),
        'reconfirm_cluster': ('admin', 'epidemiologist'),
        'dismiss': ('admin', 'epidemiologist'),
        'dispatch': ('admin', 'field'),
        'accept': ('admin', 'field'),
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        if kind in REGION_SCOPED_KINDS:
            assert_field_region(actor, entity["data"].get("region"))
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        if next_status == SAME_STATUS:
            next_status = entity["status"]
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None
