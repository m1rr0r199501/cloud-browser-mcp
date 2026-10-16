"""SQLite persistence.

This store exists for one reason above all others: **a crashed gateway must not
leave a paid cloud browser running**. So every platform session is written down
*before* it is created (``record_create_intent``) and the row survives restarts.
Combined with the owner tag in session metadata, that gives two independent ways
to find an orphan: our own intent log, and the platform's session list.

Everything is keyed on ``lease_id``. The capability token is never stored -- only
its SHA-256 -- so a leaked database cannot be used to drive someone's browser.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from .log import get_logger

log = get_logger("store")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS leases (
    lease_id              TEXT PRIMARY KEY,
    token_sha256          TEXT NOT NULL,
    state                 TEXT NOT NULL,
    owner                 TEXT,
    task_label            TEXT,
    idempotency_key       TEXT,
    provider              TEXT NOT NULL,
    platform_session_id   TEXT,
    scope_json            TEXT,
    network_mode          TEXT,
    created_at            REAL NOT NULL,
    activated_at          REAL,
    last_activity_at      REAL,
    expires_at            REAL,
    platform_timeout_at   REAL,
    released_at           REAL,
    release_reason        TEXT,
    cost_reserved_usd     REAL NOT NULL DEFAULT 0,
    cost_actual_usd       REAL NOT NULL DEFAULT 0,
    proxy_mb              REAL NOT NULL DEFAULT 0,
    instance_id           TEXT,
    meta_json             TEXT
);
CREATE INDEX IF NOT EXISTS idx_leases_state ON leases(state);
CREATE INDEX IF NOT EXISTS idx_leases_token ON leases(token_sha256);
CREATE INDEX IF NOT EXISTS idx_leases_idem ON leases(idempotency_key);

-- Write-ahead intent log: a row appears here BEFORE the provider API call.
CREATE TABLE IF NOT EXISTS create_intents (
    intent_id             TEXT PRIMARY KEY,
    lease_id              TEXT,
    provider              TEXT NOT NULL,
    owner_tag             TEXT,
    created_at            REAL NOT NULL,
    resolved              INTEGER NOT NULL DEFAULT 0,
    platform_session_id   TEXT,
    note                  TEXT
);
CREATE INDEX IF NOT EXISTS idx_intents_unresolved ON create_intents(resolved, created_at);

CREATE TABLE IF NOT EXISTS platform_sessions (
    platform_session_id   TEXT PRIMARY KEY,
    provider              TEXT NOT NULL,
    lease_id              TEXT,
    state                 TEXT NOT NULL,
    created_at            REAL NOT NULL,
    last_seen_at          REAL,
    stopped_at            REAL,
    stop_confirmed        INTEGER NOT NULL DEFAULT 0,
    cost_usd              REAL NOT NULL DEFAULT 0,
    proxy_mb              REAL NOT NULL DEFAULT 0,
    duration_seconds      REAL NOT NULL DEFAULT 0,
    instance_id           TEXT,
    note                  TEXT
);
CREATE INDEX IF NOT EXISTS idx_psessions_state ON platform_sessions(state);
CREATE INDEX IF NOT EXISTS idx_psessions_confirm ON platform_sessions(stop_confirmed);

CREATE TABLE IF NOT EXISTS usage_daily (
    day                   TEXT PRIMARY KEY,
    sessions              INTEGER NOT NULL DEFAULT 0,
    browser_seconds       REAL NOT NULL DEFAULT 0,
    cost_usd              REAL NOT NULL DEFAULT 0,
    proxy_mb              REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                    REAL NOT NULL,
    lease_id              TEXT,
    actor                 TEXT,
    action                TEXT NOT NULL,
    target                TEXT,
    outcome               TEXT,
    detail_json           TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
CREATE INDEX IF NOT EXISTS idx_audit_lease ON audit(lease_id);

CREATE TABLE IF NOT EXISTS counters (
    name                  TEXT PRIMARY KEY,
    value                 REAL NOT NULL DEFAULT 0
);
"""

# Lease states. ACTIVE/CREATING/RELEASING/UNKNOWN all occupy a concurrency slot:
# an "unknown" session may still be burning money, so it must keep its slot until
# reconciliation proves otherwise.
ST_QUEUED = "queued"
ST_CREATING = "creating"
ST_ACTIVE = "active"
ST_RELEASING = "releasing"
ST_RELEASED = "released"
ST_FAILED = "failed"
ST_UNKNOWN = "unknown"

SLOT_STATES = (ST_CREATING, ST_ACTIVE, ST_RELEASING, ST_UNKNOWN)
TERMINAL_STATES = (ST_RELEASED, ST_FAILED)


def new_lease_id() -> str:
    return "lse_" + secrets.token_hex(8)


def new_lease_token() -> str:
    # 32 bytes of entropy. This is a capability: possession == control.
    return "lst_" + secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def today_key(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), UTC).strftime("%Y-%m-%d")


def month_key(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), UTC).strftime("%Y-%m")


@dataclass
class LeaseRow:
    lease_id: str
    state: str
    provider: str
    created_at: float
    owner: str | None = None
    task_label: str | None = None
    idempotency_key: str | None = None
    platform_session_id: str | None = None
    scope: list[str] = field(default_factory=list)
    network_mode: str | None = None
    activated_at: float | None = None
    last_activity_at: float | None = None
    expires_at: float | None = None
    platform_timeout_at: float | None = None
    released_at: float | None = None
    release_reason: str | None = None
    cost_reserved_usd: float = 0.0
    cost_actual_usd: float = 0.0
    proxy_mb: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> LeaseRow:
        return cls(
            lease_id=row["lease_id"],
            state=row["state"],
            provider=row["provider"],
            created_at=row["created_at"],
            owner=row["owner"],
            task_label=row["task_label"],
            idempotency_key=row["idempotency_key"],
            platform_session_id=row["platform_session_id"],
            scope=json.loads(row["scope_json"] or "[]"),
            network_mode=row["network_mode"],
            activated_at=row["activated_at"],
            last_activity_at=row["last_activity_at"],
            expires_at=row["expires_at"],
            platform_timeout_at=row["platform_timeout_at"],
            released_at=row["released_at"],
            release_reason=row["release_reason"],
            cost_reserved_usd=row["cost_reserved_usd"],
            cost_actual_usd=row["cost_actual_usd"],
            proxy_mb=row["proxy_mb"],
            meta=json.loads(row["meta_json"] or "{}"),
        )


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        log.info("store opened at %s", self.path)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("store not opened")
        return self._db

    # ----- leases ----------------------------------------------------------

    async def insert_lease(
        self,
        *,
        lease_id: str,
        token: str,
        provider: str,
        state: str,
        owner: str | None,
        task_label: str | None,
        idempotency_key: str | None,
        scope: list[str],
        network_mode: str,
        expires_at: float | None,
        instance_id: str,
        meta: dict[str, Any] | None = None,
    ) -> None:
        now = time.time()
        await self.db.execute(
            """INSERT INTO leases (lease_id, token_sha256, state, owner, task_label,
                   idempotency_key, provider, scope_json, network_mode, created_at,
                   last_activity_at, expires_at, instance_id, meta_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                lease_id,
                token_hash(token),
                state,
                owner,
                task_label,
                idempotency_key,
                provider,
                json.dumps(scope),
                network_mode,
                now,
                now,
                expires_at,
                instance_id,
                json.dumps(meta or {}),
            ),
        )
        await self.db.commit()

    async def update_lease(self, lease_id: str, **fields: Any) -> None:
        if not fields:
            return
        if "scope" in fields:
            fields["scope_json"] = json.dumps(fields.pop("scope"))
        if "meta" in fields:
            fields["meta_json"] = json.dumps(fields.pop("meta"))
        cols = ", ".join(f"{k}=?" for k in fields)
        await self.db.execute(
            f"UPDATE leases SET {cols} WHERE lease_id=?", (*fields.values(), lease_id)
        )
        await self.db.commit()

    async def get_lease(self, lease_id: str) -> LeaseRow | None:
        cur = await self.db.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,))
        row = await cur.fetchone()
        return LeaseRow.from_row(row) if row else None

    async def find_lease_by_token(self, token: str) -> LeaseRow | None:
        cur = await self.db.execute(
            "SELECT * FROM leases WHERE token_sha256=?", (token_hash(token),)
        )
        row = await cur.fetchone()
        return LeaseRow.from_row(row) if row else None

    async def find_live_lease_by_idempotency(self, key: str) -> LeaseRow | None:
        marks = ",".join("?" * len(SLOT_STATES))
        cur = await self.db.execute(
            f"""SELECT * FROM leases WHERE idempotency_key=? AND state IN ({marks})
                ORDER BY created_at DESC LIMIT 1""",
            (key, *SLOT_STATES),
        )
        row = await cur.fetchone()
        return LeaseRow.from_row(row) if row else None

    async def list_leases(self, states: tuple[str, ...] | None = None) -> list[LeaseRow]:
        if states:
            marks = ",".join("?" * len(states))
            cur = await self.db.execute(
                f"SELECT * FROM leases WHERE state IN ({marks}) ORDER BY created_at", states
            )
        else:
            cur = await self.db.execute("SELECT * FROM leases ORDER BY created_at DESC LIMIT 200")
        return [LeaseRow.from_row(r) for r in await cur.fetchall()]

    async def count_slot_holders(self) -> int:
        marks = ",".join("?" * len(SLOT_STATES))
        cur = await self.db.execute(
            f"SELECT COUNT(*) AS n FROM leases WHERE state IN ({marks})", SLOT_STATES
        )
        row = await cur.fetchone()
        return int(row["n"]) if row else 0

    # ----- create intents (orphan prevention) ------------------------------

    async def record_create_intent(
        self, *, lease_id: str, provider: str, owner_tag: str
    ) -> str:
        intent_id = "int_" + secrets.token_hex(8)
        await self.db.execute(
            """INSERT INTO create_intents (intent_id, lease_id, provider, owner_tag, created_at)
               VALUES (?,?,?,?,?)""",
            (intent_id, lease_id, provider, owner_tag, time.time()),
        )
        await self.db.commit()
        return intent_id

    async def resolve_create_intent(
        self, intent_id: str, platform_session_id: str | None, note: str = ""
    ) -> None:
        await self.db.execute(
            "UPDATE create_intents SET resolved=1, platform_session_id=?, note=? WHERE intent_id=?",
            (platform_session_id, note, intent_id),
        )
        await self.db.commit()

    async def unresolved_intents(self, older_than: float) -> list[aiosqlite.Row]:
        cur = await self.db.execute(
            "SELECT * FROM create_intents WHERE resolved=0 AND created_at < ?", (older_than,)
        )
        return list(await cur.fetchall())

    # ----- platform sessions ----------------------------------------------

    async def upsert_platform_session(
        self,
        *,
        platform_session_id: str,
        provider: str,
        state: str,
        lease_id: str | None = None,
        instance_id: str | None = None,
        cost_usd: float | None = None,
        proxy_mb: float | None = None,
        duration_seconds: float | None = None,
        stopped: bool | None = None,
        stop_confirmed: bool | None = None,
        note: str | None = None,
    ) -> None:
        now = time.time()
        await self.db.execute(
            """INSERT INTO platform_sessions
                 (platform_session_id, provider, lease_id, state, created_at,
                  last_seen_at, instance_id, note)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(platform_session_id) DO UPDATE SET
                 state=excluded.state,
                 last_seen_at=excluded.last_seen_at,
                 lease_id=COALESCE(excluded.lease_id, platform_sessions.lease_id),
                 note=COALESCE(excluded.note, platform_sessions.note)""",
            (platform_session_id, provider, lease_id, state, now, now, instance_id, note),
        )
        sets: dict[str, Any] = {}
        if cost_usd is not None:
            sets["cost_usd"] = cost_usd
        if proxy_mb is not None:
            sets["proxy_mb"] = proxy_mb
        if duration_seconds is not None:
            sets["duration_seconds"] = duration_seconds
        if stopped:
            sets["stopped_at"] = now
        if stop_confirmed is not None:
            sets["stop_confirmed"] = 1 if stop_confirmed else 0
        if sets:
            cols = ", ".join(f"{k}=?" for k in sets)
            await self.db.execute(
                f"UPDATE platform_sessions SET {cols} WHERE platform_session_id=?",
                (*sets.values(), platform_session_id),
            )
        await self.db.commit()

    async def known_platform_session_ids(self) -> set[str]:
        cur = await self.db.execute("SELECT platform_session_id FROM platform_sessions")
        return {r["platform_session_id"] for r in await cur.fetchall()}

    async def unconfirmed_stops(self) -> list[aiosqlite.Row]:
        cur = await self.db.execute(
            """SELECT * FROM platform_sessions
               WHERE stop_confirmed=0 AND state != 'stopped' ORDER BY created_at"""
        )
        return list(await cur.fetchall())

    async def get_platform_session(self, sid: str) -> aiosqlite.Row | None:
        cur = await self.db.execute(
            "SELECT * FROM platform_sessions WHERE platform_session_id=?", (sid,)
        )
        return await cur.fetchone()

    # ----- usage / budget --------------------------------------------------

    async def add_usage(
        self, *, cost_usd: float, browser_seconds: float, proxy_mb: float, sessions: int = 0
    ) -> None:
        day = today_key()
        await self.db.execute(
            """INSERT INTO usage_daily (day, sessions, browser_seconds, cost_usd, proxy_mb)
               VALUES (?,?,?,?,?)
               ON CONFLICT(day) DO UPDATE SET
                 sessions=sessions+excluded.sessions,
                 browser_seconds=browser_seconds+excluded.browser_seconds,
                 cost_usd=cost_usd+excluded.cost_usd,
                 proxy_mb=proxy_mb+excluded.proxy_mb""",
            (day, sessions, browser_seconds, cost_usd, proxy_mb),
        )
        await self.db.commit()

    async def usage_for_day(self, day: str | None = None) -> dict[str, float]:
        day = day or today_key()
        cur = await self.db.execute("SELECT * FROM usage_daily WHERE day=?", (day,))
        row = await cur.fetchone()
        if not row:
            return {"sessions": 0, "browser_seconds": 0.0, "cost_usd": 0.0, "proxy_mb": 0.0}
        return {
            "sessions": row["sessions"],
            "browser_seconds": row["browser_seconds"],
            "cost_usd": row["cost_usd"],
            "proxy_mb": row["proxy_mb"],
        }

    async def usage_for_month(self, month: str | None = None) -> dict[str, float]:
        month = month or month_key()
        cur = await self.db.execute(
            """SELECT COALESCE(SUM(sessions),0) s, COALESCE(SUM(browser_seconds),0) bs,
                      COALESCE(SUM(cost_usd),0) c, COALESCE(SUM(proxy_mb),0) p
               FROM usage_daily WHERE day LIKE ?""",
            (f"{month}-%",),
        )
        row = await cur.fetchone()
        return {
            "sessions": row["s"],
            "browser_seconds": row["bs"],
            "cost_usd": row["c"],
            "proxy_mb": row["p"],
        }

    async def reserved_total(self) -> float:
        """Sum of money already committed by in-flight leases but not yet settled.

        Without this, N concurrent acquires all read the same "spent so far" and
        all pass the budget check.
        """
        marks = ",".join("?" * len(SLOT_STATES))
        cur = await self.db.execute(
            f"""SELECT COALESCE(SUM(MAX(cost_reserved_usd - cost_actual_usd, 0)),0) AS r
                FROM leases WHERE state IN ({marks})""",
            SLOT_STATES,
        )
        row = await cur.fetchone()
        return float(row["r"] or 0.0)

    # ----- counters & audit ------------------------------------------------

    async def bump(self, name: str, delta: float = 1.0) -> None:
        await self.db.execute(
            """INSERT INTO counters (name, value) VALUES (?,?)
               ON CONFLICT(name) DO UPDATE SET value=value+excluded.value""",
            (name, delta),
        )
        await self.db.commit()

    async def counters(self) -> dict[str, float]:
        cur = await self.db.execute("SELECT name, value FROM counters")
        return {r["name"]: r["value"] for r in await cur.fetchall()}

    async def audit(
        self,
        action: str,
        *,
        lease_id: str | None = None,
        actor: str | None = None,
        target: str | None = None,
        outcome: str | None = None,
        **detail: Any,
    ) -> None:
        await self.db.execute(
            """INSERT INTO audit (ts, lease_id, actor, action, target, outcome, detail_json)
               VALUES (?,?,?,?,?,?,?)""",
            (
                time.time(),
                lease_id,
                actor,
                action,
                target,
                outcome,
                json.dumps(detail, default=str) if detail else None,
            ),
        )
        await self.db.commit()

    async def recent_audit(self, limit: int = 50, lease_id: str | None = None) -> list[dict]:
        if lease_id:
            cur = await self.db.execute(
                "SELECT * FROM audit WHERE lease_id=? ORDER BY id DESC LIMIT ?", (lease_id, limit)
            )
        else:
            cur = await self.db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        out = []
        for r in await cur.fetchall():
            out.append(
                {
                    "ts": datetime.fromtimestamp(r["ts"], UTC).isoformat(timespec="seconds"),
                    "lease_id": r["lease_id"],
                    "actor": r["actor"],
                    "action": r["action"],
                    "target": r["target"],
                    "outcome": r["outcome"],
                    "detail": json.loads(r["detail_json"]) if r["detail_json"] else None,
                }
            )
        return out

    # ----- startup recovery ------------------------------------------------

    async def mark_stale_on_startup(self, instance_id: str) -> list[LeaseRow]:
        """Leases left behind by a previous process cannot be driven any more.

        Their browser connection died with that process, so they are moved to
        ``unknown``: still holding a slot, still owing a reconciliation check, so
        the reaper will chase their platform session until a stop is confirmed.
        """
        marks = ",".join("?" * len(SLOT_STATES))
        cur = await self.db.execute(
            f"""SELECT * FROM leases
                WHERE state IN ({marks}) AND (instance_id IS NULL OR instance_id != ?)""",
            (*SLOT_STATES, instance_id),
        )
        rows = [LeaseRow.from_row(r) for r in await cur.fetchall()]
        if rows:
            await self.db.execute(
                f"""UPDATE leases SET state=?, release_reason=?
                    WHERE state IN ({marks}) AND (instance_id IS NULL OR instance_id != ?)""",
                (ST_UNKNOWN, "gateway_restart", *SLOT_STATES, instance_id),
            )
            await self.db.commit()
        return rows
