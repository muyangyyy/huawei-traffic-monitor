import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from typing import Sequence

from .config import DeviceConfig


UTC = timezone.utc


@dataclass(frozen=True)
class InterfaceSample:
    device_id: str
    if_index: int
    if_name: str
    if_alias: str
    oper_status: str
    if_speed_mbps: int | None
    in_octets: int
    out_octets: int
    sampled_at: datetime
    sample_status: str = "ok"
    error_message: str = ""


class MonitorDatabase:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    mgmt_ip TEXT NOT NULL,
                    snmp_version TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    last_status TEXT NOT NULL DEFAULT 'unknown',
                    last_seen_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS interfaces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL,
                    if_index INTEGER NOT NULL,
                    if_name TEXT NOT NULL,
                    if_alias TEXT NOT NULL DEFAULT '',
                    if_speed_mbps INTEGER,
                    oper_status TEXT NOT NULL DEFAULT 'unknown',
                    last_seen_at TEXT NOT NULL,
                    UNIQUE(device_id, if_index),
                    FOREIGN KEY(device_id) REFERENCES devices(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS raw_interface_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL,
                    interface_id INTEGER NOT NULL,
                    sampled_at TEXT NOT NULL,
                    in_octets INTEGER,
                    out_octets INTEGER,
                    in_delta_bytes INTEGER,
                    out_delta_bytes INTEGER,
                    in_bps REAL,
                    out_bps REAL,
                    sample_status TEXT NOT NULL,
                    error_message TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(device_id) REFERENCES devices(id) ON DELETE CASCADE,
                    FOREIGN KEY(interface_id) REFERENCES interfaces(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_raw_device_interface_time
                ON raw_interface_samples(device_id, interface_id, sampled_at);

                CREATE TABLE IF NOT EXISTS aggregate_interface_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    period_type TEXT NOT NULL,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    interface_id INTEGER NOT NULL,
                    in_total_bytes INTEGER NOT NULL,
                    out_total_bytes INTEGER NOT NULL,
                    in_avg_bps REAL,
                    out_avg_bps REAL,
                    in_max_bps REAL,
                    out_max_bps REAL,
                    sample_count INTEGER NOT NULL,
                    UNIQUE(period_type, period_start, device_id, interface_id)
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_time TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    device_id TEXT,
                    interface_id INTEGER,
                    message TEXT NOT NULL
                );
                """
            )

    def upsert_device(self, device_id: str, name: str, host: str, snmp_version: str, enabled: bool) -> None:
        now = iso_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO devices(id, name, mgmt_ip, snmp_version, enabled, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    mgmt_ip=excluded.mgmt_ip,
                    snmp_version=excluded.snmp_version,
                    enabled=excluded.enabled,
                    updated_at=excluded.updated_at
                """,
                (device_id, name, host, snmp_version, 1 if enabled else 0, now, now),
            )

    def sync_devices(self, devices: Sequence[DeviceConfig], reset_interfaces: bool = False) -> None:
        now = iso_now()
        ids = [device.id for device in devices]
        with self.connect() as conn:
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(f"DELETE FROM devices WHERE id NOT IN ({placeholders})", ids)
            else:
                conn.execute("DELETE FROM devices")
            for device in devices:
                conn.execute(
                    """
                    INSERT INTO devices(id, name, mgmt_ip, snmp_version, enabled, created_at, updated_at)
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name=excluded.name,
                        mgmt_ip=excluded.mgmt_ip,
                        snmp_version=excluded.snmp_version,
                        enabled=excluded.enabled,
                        last_status='checking',
                        updated_at=excluded.updated_at
                    """,
                    (device.id, device.name, device.host, device.snmp_version, 1 if device.enabled else 0, now, now),
                )
                if reset_interfaces:
                    conn.execute("DELETE FROM interfaces WHERE device_id=?", (device.id,))

    def record_device_status(self, device_id: str, status: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE devices SET last_status=?, last_seen_at=?, updated_at=? WHERE id=?",
                (status, iso_now(), iso_now(), device_id),
            )

    def add_event(self, severity: str, message: str, device_id: str | None = None, interface_id: int | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO events(event_time, severity, device_id, interface_id, message) VALUES(?, ?, ?, ?, ?)",
                (iso_now(), severity, device_id, interface_id, message),
            )

    def save_samples(self, samples: list[InterfaceSample]) -> None:
        if not samples:
            return
        with self.connect() as conn:
            for sample in samples:
                interface_id = self._upsert_interface(conn, sample)
                prev = self._last_sample(conn, sample.device_id, interface_id)
                delta = calculate_delta(prev, sample)
                conn.execute(
                    """
                    INSERT INTO raw_interface_samples(
                        device_id, interface_id, sampled_at,
                        in_octets, out_octets, in_delta_bytes, out_delta_bytes,
                        in_bps, out_bps, sample_status, error_message
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sample.device_id,
                        interface_id,
                        sample.sampled_at.astimezone(UTC).isoformat(),
                        sample.in_octets,
                        sample.out_octets,
                        delta["in_delta_bytes"],
                        delta["out_delta_bytes"],
                        delta["in_bps"],
                        delta["out_bps"],
                        delta["sample_status"],
                        sample.error_message,
                    ),
                )

    def _upsert_interface(self, conn: sqlite3.Connection, sample: InterfaceSample) -> int:
        conn.execute(
            """
            INSERT INTO interfaces(device_id, if_index, if_name, if_alias, if_speed_mbps, oper_status, last_seen_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id, if_index) DO UPDATE SET
                if_name=excluded.if_name,
                if_alias=excluded.if_alias,
                if_speed_mbps=excluded.if_speed_mbps,
                oper_status=excluded.oper_status,
                last_seen_at=excluded.last_seen_at
            """,
            (
                sample.device_id,
                sample.if_index,
                sample.if_name,
                sample.if_alias,
                sample.if_speed_mbps,
                sample.oper_status,
                sample.sampled_at.astimezone(UTC).isoformat(),
            ),
        )
        row = conn.execute(
            "SELECT id FROM interfaces WHERE device_id=? AND if_index=?",
            (sample.device_id, sample.if_index),
        ).fetchone()
        return int(row["id"])

    def _last_sample(self, conn: sqlite3.Connection, device_id: str, interface_id: int) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT sampled_at, in_octets, out_octets
            FROM raw_interface_samples
            WHERE device_id=? AND interface_id=? AND sample_status IN ('ok', 'first_sample')
            ORDER BY sampled_at DESC
            LIMIT 1
            """,
            (device_id, interface_id),
        ).fetchone()

    def enforce_retention(self, retention_days: int) -> None:
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        with self.connect() as conn:
            conn.execute("DELETE FROM raw_interface_samples WHERE sampled_at < ?", (cutoff,))
            conn.execute("DELETE FROM events WHERE event_time < ?", (cutoff,))

    def rebuild_aggregates(self, period_type: str, since: datetime) -> None:
        start = period_start(period_type, since)
        end = period_end(period_type, start)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    device_id,
                    interface_id,
                    COALESCE(SUM(in_delta_bytes), 0) AS in_total_bytes,
                    COALESCE(SUM(out_delta_bytes), 0) AS out_total_bytes,
                    AVG(in_bps) AS in_avg_bps,
                    AVG(out_bps) AS out_avg_bps,
                    MAX(in_bps) AS in_max_bps,
                    MAX(out_bps) AS out_max_bps,
                    COUNT(*) AS sample_count
                FROM raw_interface_samples
                WHERE sampled_at >= ? AND sampled_at < ? AND sample_status='ok'
                GROUP BY device_id, interface_id
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchall()
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO aggregate_interface_stats(
                        period_type, period_start, period_end, device_id, interface_id,
                        in_total_bytes, out_total_bytes, in_avg_bps, out_avg_bps,
                        in_max_bps, out_max_bps, sample_count
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(period_type, period_start, device_id, interface_id)
                    DO UPDATE SET
                        period_end=excluded.period_end,
                        in_total_bytes=excluded.in_total_bytes,
                        out_total_bytes=excluded.out_total_bytes,
                        in_avg_bps=excluded.in_avg_bps,
                        out_avg_bps=excluded.out_avg_bps,
                        in_max_bps=excluded.in_max_bps,
                        out_max_bps=excluded.out_max_bps,
                        sample_count=excluded.sample_count
                    """,
                    (
                        period_type,
                        start.isoformat(),
                        end.isoformat(),
                        row["device_id"],
                        row["interface_id"],
                        int(row["in_total_bytes"]),
                        int(row["out_total_bytes"]),
                        row["in_avg_bps"],
                        row["out_avg_bps"],
                        row["in_max_bps"],
                        row["out_max_bps"],
                        int(row["sample_count"]),
                    ),
                )

    def overview(self) -> dict:
        with self.connect() as conn:
            devices = conn.execute(
                "SELECT COUNT(*) total, SUM(CASE WHEN last_status='online' THEN 1 ELSE 0 END) online FROM devices"
            ).fetchone()
            interfaces = conn.execute(
                "SELECT COUNT(*) total, SUM(CASE WHEN oper_status='up' THEN 1 ELSE 0 END) up_count FROM interfaces"
            ).fetchone()
            latest = conn.execute(
                """
                SELECT COALESCE(SUM(in_bps), 0) AS in_bps, COALESCE(SUM(out_bps), 0) AS out_bps
                FROM raw_interface_samples
                WHERE id IN (
                    SELECT MAX(id) FROM raw_interface_samples GROUP BY device_id, interface_id
                )
                """
            ).fetchone()
            return {
                "devices_total": int(devices["total"] or 0),
                "devices_online": int(devices["online"] or 0),
                "interfaces_total": int(interfaces["total"] or 0),
                "interfaces_up": int(interfaces["up_count"] or 0),
                "current_in_bps": float(latest["in_bps"] or 0),
                "current_out_bps": float(latest["out_bps"] or 0),
            }

    def list_devices(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, name, mgmt_ip, snmp_version, enabled, last_status, last_seen_at FROM devices ORDER BY name"
            ).fetchall()
            return [dict(row) for row in rows]

    def list_interfaces(self, device_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, device_id, if_index, if_name, if_alias, if_speed_mbps, oper_status, last_seen_at
                FROM interfaces
                WHERE device_id=?
                ORDER BY if_index
                """,
                (device_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def traffic_series(self, device_id: str, interface_id: int, start: datetime, end: datetime) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT sampled_at, in_delta_bytes, out_delta_bytes, in_bps, out_bps, sample_status
                FROM raw_interface_samples
                WHERE device_id=? AND interface_id=? AND sampled_at >= ? AND sampled_at <= ?
                ORDER BY sampled_at
                """,
                (device_id, interface_id, start.isoformat(), end.isoformat()),
            ).fetchall()
            return [dict(row) for row in rows]

    def daily_traffic_totals(self, device_id: str, interface_id: int, start: datetime, end: datetime) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    substr(sampled_at, 1, 10) AS day,
                    COALESCE(SUM(in_delta_bytes), 0) AS in_total_bytes,
                    COALESCE(SUM(out_delta_bytes), 0) AS out_total_bytes,
                    COUNT(*) AS sample_count
                FROM raw_interface_samples
                WHERE device_id=? AND interface_id=? AND sampled_at >= ? AND sampled_at <= ? AND sample_status='ok'
                GROUP BY substr(sampled_at, 1, 10)
                ORDER BY day DESC
                """,
                (device_id, interface_id, start.isoformat(), end.isoformat()),
            ).fetchall()
            return [dict(row) for row in rows]

    def recent_events(self, limit: int = 20) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT event_time, severity, device_id, interface_id, message
                FROM events
                ORDER BY event_time DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]


def calculate_delta(prev: sqlite3.Row | None, sample: InterfaceSample) -> dict[str, int | float | str | None]:
    if prev is None:
        return {
            "in_delta_bytes": None,
            "out_delta_bytes": None,
            "in_bps": None,
            "out_bps": None,
            "sample_status": "first_sample" if sample.sample_status == "ok" else sample.sample_status,
        }
    prev_time = datetime.fromisoformat(str(prev["sampled_at"]))
    seconds = max(0.001, (sample.sampled_at.astimezone(UTC) - prev_time).total_seconds())
    in_delta = sample.in_octets - int(prev["in_octets"])
    out_delta = sample.out_octets - int(prev["out_octets"])
    if in_delta < 0 or out_delta < 0:
        return {
            "in_delta_bytes": None,
            "out_delta_bytes": None,
            "in_bps": None,
            "out_bps": None,
            "sample_status": "counter_reset",
        }
    return {
        "in_delta_bytes": in_delta,
        "out_delta_bytes": out_delta,
        "in_bps": in_delta * 8 / seconds,
        "out_bps": out_delta * 8 / seconds,
        "sample_status": sample.sample_status,
    }


def iso_now() -> str:
    return datetime.now(UTC).isoformat()


def period_start(period_type: str, value: datetime) -> datetime:
    value = value.astimezone(UTC)
    if period_type == "day":
        return value.replace(hour=0, minute=0, second=0, microsecond=0)
    if period_type == "week":
        base = value - timedelta(days=value.weekday())
        return base.replace(hour=0, minute=0, second=0, microsecond=0)
    if period_type == "month":
        return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if period_type == "year":
        return value.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(f"unsupported period type: {period_type}")


def period_end(period_type: str, start: datetime) -> datetime:
    if period_type == "day":
        return start + timedelta(days=1)
    if period_type == "week":
        return start + timedelta(days=7)
    if period_type == "month":
        year = start.year + (1 if start.month == 12 else 0)
        month = 1 if start.month == 12 else start.month + 1
        return start.replace(year=year, month=month)
    if period_type == "year":
        return start.replace(year=start.year + 1)
    raise ValueError(f"unsupported period type: {period_type}")
