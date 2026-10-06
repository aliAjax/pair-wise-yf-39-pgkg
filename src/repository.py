import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    dedupe_key TEXT UNIQUE
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS sync_batches (
                    batch_id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)
            self._migrate(connection)

    def _migrate(self, connection):
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(audit_log)").fetchall()
        }
        if "dedupe_key" not in columns:
            connection.execute("ALTER TABLE audit_log ADD COLUMN dedupe_key TEXT")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_dedupe "
            "ON audit_log(dedupe_key) WHERE dedupe_key IS NOT NULL"
        )

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None, region=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        items = [self._entity_from_row(row) for row in rows]
        if region:
            items = [item for item in items if item["data"].get("region") == region]
        return items

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def transfer_handover_item(self, handover_id, item, to_region, actor, index):
        """Move one handover item to the receiving district.

        Each item runs in its own transaction and is marked on the handover
        inside the same commit, so an interrupted handover can resume without
        moving the same record twice or double-writing audit rows.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (item["id"],)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + item["id"])
            handover_row = connection.execute(
                "SELECT data, version FROM entities WHERE id = ?", (handover_id,)
            ).fetchone()
            handover_data = json.loads(handover_row["data"])
            entry = handover_data["items"][index]
            if entry.get("status") == "done":
                connection.commit()
                return None
            entity_data = json.loads(row["data"])
            from_region = entity_data.get("region")
            now = utcnow()
            entity_data["region"] = to_region
            connection.execute(
                "UPDATE entities SET data = ?, version = version + 1, updated_at = ? WHERE id = ?",
                (json.dumps(entity_data, ensure_ascii=False, sort_keys=True), now, item["id"]),
            )
            entry["status"] = "done"
            connection.execute(
                "UPDATE entities SET data = ?, version = version + 1, updated_at = ? WHERE id = ?",
                (json.dumps(handover_data, ensure_ascii=False, sort_keys=True), now, handover_id),
            )
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, "
                "to_status, detail, created_at, dedupe_key) "
                "VALUES (?, ?, ?, 'transfer', ?, ?, ?, ?, ?)",
                (
                    item["id"],
                    actor.user_id,
                    actor.role,
                    from_region,
                    to_region,
                    json.dumps(
                        {"handover_id": handover_id, "from_region": from_region,
                         "to_region": to_region},
                        ensure_ascii=False, sort_keys=True,
                    ),
                    now,
                    "handover:%s:%s" % (handover_id, item["id"]),
                ),
            )
            connection.commit()
            return {"id": item["id"], "kind": item["kind"], "from_region": from_region,
                    "to_region": to_region}
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status,
                     to_status, detail, dedupe_key=None):
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, "
                    "from_status, to_status, detail, created_at, dedupe_key) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        entity_id,
                        actor_id,
                        actor_role,
                        action,
                        from_status,
                        to_status,
                        json.dumps(detail, ensure_ascii=False, sort_keys=True),
                        utcnow(),
                        dedupe_key,
                    ),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def get_sync_batch(self, batch_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT summary FROM sync_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
        return json.loads(row["summary"]) if row else None

    def save_sync_batch(self, batch_id, actor_id, summary):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO sync_batches(batch_id, actor_id, summary, created_at) "
                "VALUES (?, ?, ?, ?)",
                (batch_id, actor_id, json.dumps(summary, ensure_ascii=False, sort_keys=True),
                 utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
