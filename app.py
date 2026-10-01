#!/usr/bin/env python3
"""Drone flight-plan approval and airspace coordination service (standard library only)."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PORT = 8205
ROLES = {"viewer", "operator", "airspace_reviewer", "commander", "auditor"}
ACTIVE_STATUSES = {"submitted", "approved"}
CLOSED_STATUSES = {"canceled", "expired", "invalidated"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message); self.status, self.code, self.message, self.details = status, code, message, details


def utcnow() -> datetime: return datetime.now(timezone.utc)
def iso(value: datetime | None = None) -> str: return (value or utcnow()).replace(microsecond=0).isoformat().replace("+00:00", "Z")
def parse_time(value: str | None) -> datetime:
    if not value: raise ApiError(400, "time_required", "必须提供 ISO 8601 时间")
    try: parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc: raise ApiError(400, "invalid_time", f"时间格式错误: {value}") from exc
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def route_bbox(route: list[list[float]]) -> tuple[float, float, float, float]:
    xs = [float(point[0]) for point in route]; ys = [float(point[1]) for point in route]
    return min(xs), min(ys), max(xs), max(ys)


def boxes_overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float], buffer: float = 0.0) -> bool:
    return a[0] <= b[2] + buffer and a[2] + buffer >= b[0] and a[1] <= b[3] + buffer and a[3] + buffer >= b[1]


def times_overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool: return a_start < b_end and b_start < a_end


def time_slots(start: datetime, end: datetime, slot_seconds: int) -> list[tuple[datetime, datetime]]:
    """Split [start,end) into fixed-width ledger buckets aligned to the unix epoch."""
    width = timedelta(seconds=slot_seconds)
    epoch_seconds = start.timestamp()
    floor = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=(epoch_seconds // slot_seconds) * slot_seconds)
    slots: list[tuple[datetime, datetime]] = []
    cursor = floor
    while cursor < end:
        nxt = cursor + width
        slots.append((max(cursor, start), min(nxt, end)))
        cursor = nxt
    return slots


def corridor_overlap(plan_bbox: tuple[float, float, float, float], plan_altitude: float, corr: sqlite3.Row) -> bool:
    cbox = (corr["min_lon"], corr["min_lat"], corr["max_lon"], corr["max_lat"])
    if not boxes_overlap(plan_bbox, cbox): return False
    return plan_altitude > corr["min_altitude"] and corr["max_altitude"] > 0


def validate_route(route: Any) -> list[list[float]]:
    if not isinstance(route, list) or len(route) < 2: raise ApiError(400, "invalid_route", "航线至少需要两个经纬度点")
    normalized: list[list[float]] = []
    for point in route:
        if not isinstance(point, list) or len(point) != 2 or not all(isinstance(v, (int, float)) for v in point): raise ApiError(400, "invalid_route_point", "每个航线点必须是 [经度,纬度]")
        lon, lat = float(point[0]), float(point[1])
        if not -180 <= lon <= 180 or not -90 <= lat <= 90: raise ApiError(400, "invalid_coordinates", "经纬度超出范围")
        normalized.append([lon, lat])
    return normalized


class Repository:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self.conn  # 触发首个连接并完成建表

    def _new_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=True, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            with self._init_lock:
                c = self._new_conn()
                self._bootstrap(c)
            self._local.conn = c
        return c
    def _bootstrap(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS restrictions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL, min_lon REAL NOT NULL, min_lat REAL NOT NULL,
            max_lon REAL NOT NULL, max_lat REAL NOT NULL, min_altitude REAL NOT NULL DEFAULT 0, max_altitude REAL NOT NULL,
            starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS flight_plans(
            id INTEGER PRIMARY KEY AUTOINCREMENT, operator_id TEXT NOT NULL, callsign TEXT NOT NULL, drone_model TEXT NOT NULL,
            payload_kg REAL NOT NULL, route_json TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, max_altitude REAL NOT NULL,
            population_risk INTEGER NOT NULL, emergency_plan TEXT NOT NULL, region TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
            revision INTEGER NOT NULL DEFAULT 1, created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(operator_id,callsign,starts_at)
        );
        CREATE TABLE IF NOT EXISTS approvals(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES flight_plans(id), plan_revision INTEGER NOT NULL,
            reviewer TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL, offline_id TEXT UNIQUE,
            override_kind TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notifications(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES flight_plans(id), kind TEXT NOT NULL,
            message TEXT NOT NULL, created_at TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS audit_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, actor TEXT NOT NULL, role TEXT NOT NULL, action TEXT NOT NULL,
            detail_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS corridors(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, min_lon REAL NOT NULL, min_lat REAL NOT NULL,
            max_lon REAL NOT NULL, max_lat REAL NOT NULL, min_altitude REAL NOT NULL DEFAULT 0, max_altitude REAL NOT NULL,
            capacity INTEGER NOT NULL, slot_seconds INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'active',
            ledger_version INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS slot_reservations(
            id INTEGER PRIMARY KEY AUTOINCREMENT, corridor_id INTEGER NOT NULL REFERENCES corridors(id), plan_id INTEGER NOT NULL REFERENCES flight_plans(id),
            slot_start TEXT NOT NULL, slot_end TEXT NOT NULL, emergency INTEGER NOT NULL DEFAULT 0,
            released_at TEXT, release_reason TEXT, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_slot_lookup ON slot_reservations(corridor_id,slot_start);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_active_slot ON slot_reservations(corridor_id,plan_id,slot_start) WHERE released_at IS NULL;
        CREATE TABLE IF NOT EXISTS slot_waitlist(
            id INTEGER PRIMARY KEY AUTOINCREMENT, corridor_id INTEGER NOT NULL REFERENCES corridors(id), plan_id INTEGER NOT NULL REFERENCES flight_plans(id),
            slot_start TEXT NOT NULL, slot_end TEXT NOT NULL, queued_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'waiting',
            notified_at TEXT, served_at TEXT, canceled_at TEXT, cancel_reason TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_waitlist_lookup ON slot_waitlist(corridor_id,slot_start,status);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_waiting_slot ON slot_waitlist(corridor_id,plan_id,slot_start) WHERE status='waiting';
        """)

    @contextmanager
    def tx(self):
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try: conn.execute("ROLLBACK")
            except sqlite3.OperationalError: pass
            raise

    @staticmethod
    def audit(conn: sqlite3.Connection, plan_id: int | None, actor: str, role: str, action: str, detail: dict[str, Any]) -> None:
        conn.execute("INSERT INTO audit_log(plan_id,actor,role,action,detail_json,created_at) VALUES(?,?,?,?,?,?)",
                     (plan_id, actor, role, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), iso()))

    @staticmethod
    def notify(conn: sqlite3.Connection, plan_id: int, kind: str, message: str) -> None:
        conn.execute("INSERT INTO notifications(plan_id,kind,message,created_at) VALUES(?,?,?,?)", (plan_id, kind, message, iso()))


class DroneAirspaceService:
    def __init__(self, path: str | Path): self.repo = Repository(path)

    @staticmethod
    def identity(headers: Any) -> tuple[str, str, str]:
        actor, role, operator = headers.get("X-User-Id", "").strip(), headers.get("X-Role", "").strip(), headers.get("X-Operator", "").strip()
        if not actor or role not in ROLES: raise ApiError(401, "unauthorized", "需要 X-User-Id 和有效 X-Role")
        if role == "operator" and not operator: raise ApiError(401, "operator_required", "运营方角色必须提供 X-Operator")
        return actor, role, operator

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None: return dict(row) if row else None

    def create_restriction(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "restriction_forbidden", "只有空域审核员或指挥官可以维护限制")
        name, kind, reason = str(body.get("name", "")).strip(), str(body.get("kind", "")).strip(), str(body.get("reason", "")).strip()
        if kind not in {"no_fly", "temporary_limit"} or not name or not reason: raise ApiError(400, "invalid_restriction", "名称、类型和原因必填")
        try:
            min_lon, min_lat, max_lon, max_lat = map(float, (body.get("min_lon"), body.get("min_lat"), body.get("max_lon"), body.get("max_lat")))
            min_alt, max_alt = float(body.get("min_altitude", 0)), float(body.get("max_altitude"))
        except (TypeError, ValueError): raise ApiError(400, "invalid_restriction", "空域范围和高度必须为数字")
        start, end = parse_time(body.get("starts_at")), parse_time(body.get("ends_at"))
        if min_lon >= max_lon or min_lat >= max_lat or min_alt < 0 or max_alt <= min_alt or end <= start:
            raise ApiError(400, "invalid_restriction", "空域范围、高度或时间无效")
        with self.repo.tx() as conn:
            cur = conn.execute("""INSERT INTO restrictions(name,kind,min_lon,min_lat,max_lon,max_lat,min_altitude,max_altitude,starts_at,ends_at,reason,created_at)
                                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (name, kind, min_lon, min_lat, max_lon, max_lat, min_alt, max_alt, iso(start), iso(end), reason, iso()))
            restriction_id = cur.lastrowid
            result = dict(conn.execute("SELECT * FROM restrictions WHERE id=?", (restriction_id,)).fetchone())
            invalidated: list[int] = []
            # 禁飞区更新立即生效：时间/空间/高度重叠的已批准计划马上失效并释放占用
            if kind == "no_fly":
                rbox = (min_lon, min_lat, max_lon, max_lat)
                affected: set[int] = set()
                candidates = conn.execute("SELECT * FROM flight_plans WHERE status='approved'")
                for plan in candidates:
                    p_start, p_end = parse_time(plan["starts_at"]), parse_time(plan["ends_at"])
                    if not times_overlap(p_start, p_end, start, end): continue
                    if not boxes_overlap(route_bbox(self._route(plan)), rbox): continue
                    if not (plan["max_altitude"] > min_alt and max_alt > 0): continue
                    affected |= self._release_holds(conn, plan["id"], f"no_fly_restriction:{restriction_id}", iso())
                    self._cancel_waiting(conn, plan["id"], f"no_fly_restriction:{restriction_id}", iso())
                    conn.execute("UPDATE flight_plans SET status='invalidated',updated_at=? WHERE id=?", (iso(), plan["id"]))
                    invalidated.append(plan["id"])
                    Repository.audit(conn, plan["id"], actor, role, "approval_invalidated_by_restriction", {"restriction_id": restriction_id, "name": name})
                    Repository.notify(conn, plan["id"], "approval_invalidated", f"禁飞区「{name}」已生效，飞行计划 {plan['callsign']} 的批准立即失效，走廊时隙已释放")
                if affected:
                    self._bump_ledger(conn, affected)
                    self._pump_waitlist(conn, affected)
            result["invalidated_plan_ids"] = invalidated
            return result

    def create_plan(self, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "plan_forbidden", "只有运营方可以创建飞行计划")
        required = ("callsign", "drone_model", "starts_at", "ends_at", "emergency_plan", "region")
        if any(body.get(key) in (None, "") for key in required): raise ApiError(400, "missing_fields", "飞行计划字段不完整")
        route = validate_route(body.get("route")); start, end = parse_time(body["starts_at"]), parse_time(body["ends_at"])
        try: payload, altitude = float(body.get("payload_kg")), float(body.get("max_altitude"))
        except (TypeError, ValueError): raise ApiError(400, "invalid_numbers", "payload_kg 和 max_altitude 必须为数字")
        risk = body.get("population_risk")
        if not 0 <= payload <= 25 or altitude <= 0 or not isinstance(risk, int) or not 0 <= risk <= 5:
            raise ApiError(400, "invalid_plan", "载荷、高度或人口风险无效")
        if end <= start or start <= utcnow(): raise ApiError(400, "invalid_time", "飞行时间必须在未来且结束晚于开始")
        bbox = route_bbox(route)
        with self.repo.tx() as conn:
            try:
                cur = conn.execute("""INSERT INTO flight_plans(operator_id,callsign,drone_model,payload_kg,route_json,starts_at,ends_at,max_altitude,population_risk,emergency_plan,region,created_by,created_at,updated_at)
                                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                   (operator, str(body["callsign"]).upper(), body["drone_model"], payload, json.dumps(route), iso(start), iso(end), altitude, risk, body["emergency_plan"], body["region"], actor, iso(), iso()))
            except sqlite3.IntegrityError as exc: raise ApiError(409, "plan_duplicate", "同一运营方、呼号和起飞时间的计划已存在") from exc
            plan_id = cur.lastrowid; Repository.audit(conn, plan_id, actor, role, "plan_created", {"bbox": bbox, "revision": 1})
            return self.get_plan(plan_id, role, operator)

    def _plan_row(self, conn: sqlite3.Connection, plan_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM flight_plans WHERE id=?", (plan_id,)).fetchone()
        if not row: raise ApiError(404, "plan_not_found", "飞行计划不存在")
        return row

    @staticmethod
    def _route(row: sqlite3.Row) -> list[list[float]]: return json.loads(row["route_json"])

    def check_conflicts(self, plan_id: int, role: str, operator: str) -> dict[str, Any]:
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if role == "operator" and plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能查看其他运营方计划")
            if role not in {"operator", "airspace_reviewer", "commander", "auditor", "viewer"}: raise ApiError(403, "check_forbidden", "无权检查冲突")
            return self._conflict_report(conn, plan)

    def _active_corridors(self, conn: sqlite3.Connection, bbox: tuple[float, float, float, float], altitude: float) -> list[sqlite3.Row]:
        return [c for c in conn.execute("SELECT * FROM corridors WHERE status='active' ORDER BY id") if corridor_overlap(bbox, altitude, c)]

    def _slot_usage(self, conn: sqlite3.Connection, corridor_id: int, slot_start: datetime) -> sqlite3.Row:
        return conn.execute("""SELECT COUNT(*) AS used, COALESCE(SUM(emergency),0) AS emergency_used
                               FROM slot_reservations WHERE corridor_id=? AND slot_start=? AND released_at IS NULL""",
                            (corridor_id, iso(slot_start))).fetchone()

    def _older_waiters(self, conn: sqlite3.Connection, corridor_id: int, slot_start: datetime, slot_end: datetime, plan_id: int, queued_before: str | None = None) -> list[sqlite3.Row]:
        rows = conn.execute("""SELECT * FROM slot_waitlist WHERE corridor_id=? AND status='waiting' AND plan_id!=?
                               AND slot_start<? AND slot_end>? AND (queued_at<? OR queued_at=?)
                               ORDER BY queued_at,id""",
                            (corridor_id, plan_id, iso(slot_end), iso(slot_start), queued_before or iso(utcnow()), queued_before or iso(utcnow())))
        return [r for r in rows]

    def _earliest_release(self, conn: sqlite3.Connection, corridor_id: int, slot_start: datetime) -> str | None:
        row = conn.execute("""SELECT MIN(slot_end) AS earliest FROM slot_reservations
                              WHERE corridor_id=? AND slot_start=? AND released_at IS NULL""", (corridor_id, iso(slot_start))).fetchone()
        return row["earliest"]

    def _ledger_entry(self, conn: sqlite3.Connection, corr: sqlite3.Row, plan_id: int, start: datetime, end: datetime, queued_before: str | None = None) -> dict[str, Any]:
        capacity = corr["capacity"]
        slots: list[dict[str, Any]] = []
        full_slots = 0
        blocked_by_queue = False
        for s_start, s_end in time_slots(start, end, corr["slot_seconds"]):
            usage = self._slot_usage(conn, corr["id"], s_start)
            used, emergency_used = usage["used"], usage["emergency_used"]
            remaining = capacity - used
            full = remaining <= 0
            if full: full_slots += 1
            waiters = self._older_waiters(conn, corr["id"], s_start, s_end, plan_id, queued_before)
            if waiters: blocked_by_queue = True
            slots.append({"slot_start": iso(s_start), "slot_end": iso(s_end), "capacity": capacity, "used": used,
                          "emergency_used": emergency_used, "remaining": remaining,
                          "waiting_ahead": len(waiters),
                          "earliest_release": self._earliest_release(conn, corr["id"], s_start) if full else None})
        return {"corridor_id": corr["id"], "name": corr["name"], "capacity": capacity, "ledger_version": corr["ledger_version"],
                "slots": slots, "full_slot_count": full_slots, "blocked_by_queue": blocked_by_queue}

    def _conflict_report(self, conn: sqlite3.Connection, plan: sqlite3.Row) -> dict[str, Any]:
        route = self._route(plan); bbox = route_bbox(route); start, end = parse_time(plan["starts_at"]), parse_time(plan["ends_at"])
        hard: list[dict[str, Any]] = []; blocking: list[dict[str, Any]] = []
        if plan["payload_kg"] > 25: hard.append({"code": "payload_limit", "message": "载荷超过 25kg 硬限制"})
        if plan["max_altitude"] > 120: hard.append({"code": "altitude_limit", "message": "常规计划高度不得超过 120m"})
        if plan["population_risk"] > 3: blocking.append({"code": "population_risk", "risk": plan["population_risk"], "message": "人口风险超过常规批准阈值"})
        for restriction in conn.execute("SELECT * FROM restrictions WHERE status='active'"):
            rbox = (restriction["min_lon"], restriction["min_lat"], restriction["max_lon"], restriction["max_lat"])
            if not boxes_overlap(bbox, rbox): continue
            if not times_overlap(start, end, parse_time(restriction["starts_at"]), parse_time(restriction["ends_at"])): continue
            altitude_overlap = plan["max_altitude"] > restriction["min_altitude"] and restriction["max_altitude"] > 0
            if altitude_overlap:
                item = {"code": "airspace_restriction", "restriction_id": restriction["id"], "name": restriction["name"], "kind": restriction["kind"], "reason": restriction["reason"]}
                blocking.append(item)
        adjacent: list[dict[str, Any]] = []
        corridor_ids = {c["id"] for c in self._active_corridors(conn, bbox, plan["max_altitude"])}
        active_restrictions = list(conn.execute("SELECT * FROM restrictions WHERE status='active'"))
        for other in conn.execute("SELECT * FROM flight_plans WHERE id!=? AND status IN ('submitted','approved') AND starts_at<? AND ends_at>?", (plan["id"], iso(end), iso(start))):
            if boxes_overlap(bbox, route_bbox(self._route(other)), 0.002):
                # 同一活动走廊内的交通由时隙容量台账裁决，避免双重计数
                if corridor_ids and {c["id"] for c in self._active_corridors(conn, route_bbox(self._route(other)), other["max_altitude"])} & corridor_ids:
                    continue
                # 对方计划若已与某条活动限制（禁飞/临时限制）时空高度重叠，不再计作相邻交通
                other_grounded = False
                for restriction in active_restrictions:
                    rbox = (restriction["min_lon"], restriction["min_lat"], restriction["max_lon"], restriction["max_lat"])
                    if boxes_overlap(route_bbox(self._route(other)), rbox) and times_overlap(parse_time(other["starts_at"]), parse_time(other["ends_at"]), parse_time(restriction["starts_at"]), parse_time(restriction["ends_at"])) and other["max_altitude"] > restriction["min_altitude"] and restriction["max_altitude"] > 0:
                        other_grounded = True; break
                if other_grounded: continue
                adjacent.append({"plan_id": other["id"], "callsign": other["callsign"], "operator_id": other["operator_id"], "status": other["status"], "starts_at": other["starts_at"], "ends_at": other["ends_at"]})
        if adjacent: blocking.append({"code": "adjacent_traffic", "plans": adjacent, "message": "相邻航路与有效计划重叠"})
        ledgers = [self._ledger_entry(conn, c, plan["id"], start, end) for c in self._active_corridors(conn, bbox, plan["max_altitude"])]
        full_corridors = [{"corridor_id": l["corridor_id"], "name": l["name"], "full_slot_count": l["full_slot_count"],
                           "ledger_version": l["ledger_version"],
                           "earliest_release": next((s["earliest_release"] for s in l["slots"] if s["earliest_release"]), None)}
                          for l in ledgers if l["full_slot_count"]]
        queued_corridors = [{"corridor_id": l["corridor_id"], "name": l["name"]} for l in ledgers if l["blocked_by_queue"]]
        if full_corridors: blocking.append({"code": "slot_capacity_full", "corridors": full_corridors, "message": "走廊时隙容量已满，需要排队"})
        if queued_corridors: blocking.append({"code": "ahead_in_queue", "corridors": queued_corridors, "message": "走廊队列前面还有等待计划，FIFO 顺序不能越过"})
        return {"plan_id": plan["id"], "revision": plan["revision"], "hard_violations": hard, "blocking_conflicts": blocking,
                "corridor_ledgers": ledgers, "approvable": not hard and not blocking}

    @staticmethod
    def _bump_ledger(conn: sqlite3.Connection, corridor_ids: set[int]) -> None:
        for cid in sorted(corridor_ids):
            conn.execute("UPDATE corridors SET ledger_version=ledger_version+1 WHERE id=?", (cid,))

    @staticmethod
    def _upsert_waiting(conn: sqlite3.Connection, corridor_id: int, plan_id: int, s_start: datetime, s_end: datetime) -> None:
        conn.execute("""INSERT INTO slot_waitlist(corridor_id,plan_id,slot_start,slot_end,queued_at,status)
                        VALUES(?,?,?,?,?,'waiting')
                        ON CONFLICT DO NOTHING""",
                     (corridor_id, plan_id, iso(s_start), iso(s_end), iso()))

    @staticmethod
    def _release_holds(conn: sqlite3.Connection, plan_id: int, reason: str, now: str) -> set[int]:
        rows = conn.execute("SELECT DISTINCT corridor_id FROM slot_reservations WHERE plan_id=? AND released_at IS NULL", (plan_id,)).fetchall()
        conn.execute("UPDATE slot_reservations SET released_at=?,release_reason=? WHERE plan_id=? AND released_at IS NULL", (now, reason, plan_id))
        return {r["corridor_id"] for r in rows}

    @staticmethod
    def _cancel_waiting(conn: sqlite3.Connection, plan_id: int, reason: str, now: str) -> set[int]:
        rows = conn.execute("SELECT DISTINCT corridor_id FROM slot_waitlist WHERE plan_id=? AND status='waiting'", (plan_id,)).fetchall()
        conn.execute("UPDATE slot_waitlist SET status='canceled',canceled_at=?,cancel_reason=? WHERE plan_id=? AND status='waiting'", (now, reason, plan_id))
        return {r["corridor_id"] for r in rows}

    @staticmethod
    def _mark_waitlist_served(conn: sqlite3.Connection, plan_id: int, now: str) -> None:
        conn.execute("UPDATE slot_waitlist SET status='served',served_at=? WHERE plan_id=? AND status='waiting'", (now, plan_id))

    def _pump_waitlist(self, conn: sqlite3.Connection, corridor_ids: set[int]) -> None:
        """Notify waiting plans whose whole window is bookable; one notification per plan per release round."""
        if not corridor_ids: return
        now = iso()
        rows = conn.execute("""SELECT w.*,p.callsign FROM slot_waitlist w JOIN flight_plans p ON p.id=w.plan_id
                               WHERE w.status='waiting' AND w.notified_at IS NULL AND w.corridor_id IN (%s)
                               ORDER BY w.queued_at,w.id""" % ",".join("?" * len(corridor_ids)),
                            tuple(sorted(corridor_ids))).fetchall()
        # 按 (走廊, 计划) 聚合：计划横跨多个槽位时，必须所有等待槽都有剩余容量且前方无更早排队者
        groups: dict[tuple[int, int], list[sqlite3.Row]] = {}
        for w in rows: groups.setdefault((w["corridor_id"], w["plan_id"]), []).append(w)
        for (cid, plan_id), entries in groups.items():
            corr = conn.execute("SELECT * FROM corridors WHERE id=?", (cid,)).fetchone()
            if not corr or corr["status"] != "active": continue
            ready = True
            for w in entries:
                s_start, s_end = parse_time(w["slot_start"]), parse_time(w["slot_end"])
                if s_start <= utcnow(): ready = False; break
                usage = self._slot_usage(conn, cid, s_start)
                if corr["capacity"] - usage["used"] <= 0: ready = False; break
                if self._older_waiters(conn, cid, s_start, s_end, plan_id, w["queued_at"]): ready = False; break
            if not ready: continue
            conn.execute("UPDATE slot_waitlist SET notified_at=? WHERE corridor_id=? AND plan_id=? AND status='waiting'", (now, cid, plan_id))
            earliest = min(w["slot_start"] for w in entries)
            Repository.notify(conn, plan_id, "slot_available",
                              f"走廊 {corr['name']} 自 {earliest} 起的等待时隙已全部空闲（台账版本 {corr['ledger_version']}），请按队列顺序审批 {entries[0]['callsign']}")

    def _close_plan_slots(self, conn: sqlite3.Connection, plan_id: int, reason: str) -> set[int]:
        now = iso()
        affected = self._release_holds(conn, plan_id, reason, now)
        affected |= self._cancel_waiting(conn, plan_id, reason, now)
        if affected:
            self._bump_ledger(conn, affected)
            self._pump_waitlist(conn, affected)
        return affected

    def get_plan(self, plan_id: int, role: str, operator: str = "") -> dict[str, Any]:
        conn = self.repo.conn; row = self._plan_row(conn, plan_id)
        if role == "operator" and row["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能查看其他运营方计划")
        result = dict(row); result["route"] = json.loads(result.pop("route_json")); result["route_bbox"] = route_bbox(result["route"])
        if role == "viewer":
            result = {key: result[key] for key in ("id", "callsign", "starts_at", "ends_at", "max_altitude", "region", "status", "valid_until" if "valid_until" in result else "updated_at")}
        else:
            waiting = conn.execute("SELECT * FROM slot_waitlist WHERE plan_id=? AND status='waiting' ORDER BY queued_at,id", (plan_id,)).fetchall()
            result["slot_waitlist"] = [dict(w) for w in waiting]
        if role in {"airspace_reviewer", "commander", "auditor"}: result["approvals"] = [dict(r) for r in conn.execute("SELECT * FROM approvals WHERE plan_id=? ORDER BY id", (plan_id,))]
        return result

    def submit(self, plan_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "submit_forbidden", "只有运营方可以提交计划")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能提交其他运营方计划")
            if plan["status"] == "submitted": return {"plan": self.get_plan(plan_id, role, operator), "idempotent": True}
            if plan["status"] not in {"draft", "rejected"}: raise ApiError(409, "invalid_transition", "当前状态不能提交")
            if parse_time(plan["starts_at"]) <= utcnow(): raise ApiError(409, "plan_expired", "计划起飞时间已过")
            conn.execute("UPDATE flight_plans SET status='submitted',updated_at=? WHERE id=?", (iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_submitted", {"revision": plan["revision"]})
            return {"plan": self.get_plan(plan_id, role, operator), "idempotent": False}

    def approve(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "review_forbidden", "只有空域审核员或指挥官可以批准")
        expected, offline_id = body.get("expected_revision"), str(body.get("offline_id", "")).strip()
        reason, override = str(body.get("reason", "")).strip(), str(body.get("override_reason", "")).strip()
        emergency = bool(body.get("emergency", False))
        expected_ledgers = body.get("expected_ledger_versions", {})
        if not isinstance(expected, int) or not offline_id or not reason: raise ApiError(400, "review_details_required", "expected_revision、offline_id 和 reason 必填")
        if emergency and role != "commander": raise ApiError(403, "emergency_forbidden", "紧急插队授权只能由指挥官签发")
        if emergency and not override: raise ApiError(400, "emergency_reason_required", "紧急授权必须提供 override_reason")
        if expected_ledgers and not isinstance(expected_ledgers, dict): raise ApiError(400, "invalid_expected_ledger_versions", "expected_ledger_versions 必须为 {走廊id: 版本号}")
        queued_error: ApiError | None = None
        with self.repo.tx() as conn:
            prior = conn.execute("SELECT * FROM approvals WHERE offline_id=?", (offline_id,)).fetchone()
            if prior:
                if prior["plan_id"] == plan_id and prior["plan_revision"] == expected and prior["decision"] == "approved":
                    return {"plan": self.get_plan(plan_id, role, ""), "idempotent": True, "approval_id": prior["id"]}
                raise ApiError(409, "offline_id_conflict", "该离线审核编号已经用于其他决定")
            plan = self._plan_row(conn, plan_id)
            if plan["status"] == "approved": return {"plan": self.get_plan(plan_id, role, ""), "idempotent": True}
            if plan["status"] != "submitted": raise ApiError(409, "invalid_transition", "只有已提交计划可以批准")
            if plan["revision"] != expected: raise ApiError(409, "revision_conflict", "计划版本已变化，审核决定不能套用")
            report = self._conflict_report(conn, plan)
            # 载荷与高度是硬限制：紧急授权也不能绕过
            if report["hard_violations"]: raise ApiError(409, "hard_constraint_violation", "计划违反不可覆盖的安全约束，紧急授权也不能豁免", report)
            # 台账乐观版本校验：后到审核员看到版本变化和最新剩余容量
            stale = [l for l in report["corridor_ledgers"] if str(l["corridor_id"]) in expected_ledgers and expected_ledgers[str(l["corridor_id"])] != l["ledger_version"]]
            if stale: raise ApiError(409, "ledger_version_conflict", "走廊台账版本已变化，请按最新剩余容量重新决定", {"corridor_ledgers": report["corridor_ledgers"], "stale": [l["corridor_id"] for l in stale]})
            airspace_blockers = [b for b in report["blocking_conflicts"] if b["code"] in {"airspace_restriction", "adjacent_traffic", "population_risk"}]
            ledger_blockers = [b for b in report["blocking_conflicts"] if b["code"] in {"slot_capacity_full", "ahead_in_queue"}]
            if airspace_blockers and not (role == "commander" and override):
                raise ApiError(409, "airspace_conflict", "计划存在空域或相邻交通冲突", report)
            if ledger_blockers and not emergency:
                # 容量满或队列前方有计划：进入 FIFO 排队而不是超卖
                queued: list[dict[str, Any]] = []
                bbox = route_bbox(self._route(plan)); start, end = parse_time(plan["starts_at"]), parse_time(plan["ends_at"])
                for l in report["corridor_ledgers"]:
                    if l["full_slot_count"] == 0 and not l["blocked_by_queue"]: continue
                    corr = conn.execute("SELECT * FROM corridors WHERE id=?", (l["corridor_id"],)).fetchone()
                    contended_slots = [s for s in l["slots"] if s["remaining"] <= 0 or s["waiting_ahead"]]
                    for s_start, s_end in time_slots(start, end, corr["slot_seconds"]):
                        if any(s["slot_start"] == iso(s_start) for s in contended_slots):
                            self._upsert_waiting(conn, corr["id"], plan_id, s_start, s_end)
                    first = contended_slots[0]
                    queued.append({"corridor_id": corr["id"], "name": corr["name"], "queue_position": first["waiting_ahead"] + 1,
                                   "remaining": first["remaining"], "earliest_release": first["earliest_release"], "ledger_version": l["ledger_version"]})
                self._bump_ledger(conn, {q["corridor_id"] for q in queued})
                Repository.audit(conn, plan_id, actor, role, "plan_queued", {"queued": queued})
                code = "slot_capacity_full" if any(b["code"] == "slot_capacity_full" for b in ledger_blockers) else "ahead_in_queue"
                queued_error = ApiError(409, code, "走廊时隙已满，计划已加入排队；容量释放后将收到 slot_available 通知",
                                        {"queued": queued, "corridor_ledgers": report["corridor_ledgers"]})
            else:
                override_kind = "emergency_authority" if (emergency or airspace_blockers) else None
                allocated: list[dict[str, Any]] = []
                bbox = route_bbox(self._route(plan)); start, end = parse_time(plan["starts_at"]), parse_time(plan["ends_at"])
                affected: set[int] = set()
                for corr in self._active_corridors(conn, bbox, plan["max_altitude"]):
                    for s_start, s_end in time_slots(start, end, corr["slot_seconds"]):
                        conn.execute("""INSERT INTO slot_reservations(corridor_id,plan_id,slot_start,slot_end,emergency,created_at)
                                        VALUES(?,?,?,?,?,?)""", (corr["id"], plan_id, iso(s_start), iso(s_end), 1 if emergency else 0, iso()))
                    affected.add(corr["id"])
                self._bump_ledger(conn, affected)
                self._mark_waitlist_served(conn, plan_id, iso())
                for cid in sorted(affected):
                    corr = conn.execute("SELECT * FROM corridors WHERE id=?", (cid,)).fetchone()
                    allocated.append(self._ledger_entry(conn, corr, plan_id, start, end))
                cur = conn.execute("""INSERT INTO approvals(plan_id,plan_revision,reviewer,decision,reason,offline_id,override_kind,created_at)
                                      VALUES(?,?,?,?,?,?,?,?)""", (plan_id, expected, actor, "approved", reason, offline_id, override_kind, iso()))
                conn.execute("UPDATE flight_plans SET status='approved',updated_at=? WHERE id=?", (iso(), plan_id))
                audit_detail: dict[str, Any] = {"override_reason": override, "conflicts": report["blocking_conflicts"], "emergency": emergency}
                if override_kind: Repository.audit(conn, plan_id, actor, role, "emergency_override_used", audit_detail)
                Repository.audit(conn, plan_id, actor, role, "plan_approved", {"revision": expected, "offline_id": offline_id, "emergency": emergency})
                Repository.notify(conn, plan_id, "approved" + ("_emergency" if emergency else ""),
                                  f"飞行计划 {plan['callsign']} 已批准" + ("（紧急插队授权，已单独标记）" if emergency else ""))
                return {"plan": self.get_plan(plan_id, role, ""), "idempotent": False, "approval_id": cur.lastrowid,
                        "override_kind": override_kind, "emergency": emergency, "corridor_ledgers": allocated}
        if queued_error is not None: raise queued_error
        raise ApiError(500, "internal_error", "审批事务异常结束")

    def reject(self, plan_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "review_forbidden", "当前角色不能拒绝计划")
        expected, offline_id, reason = body.get("expected_revision"), str(body.get("offline_id", "")).strip(), str(body.get("reason", "")).strip()
        if not isinstance(expected, int) or not offline_id or not reason: raise ApiError(400, "review_details_required", "expected_revision、offline_id 和 reason 必填")
        with self.repo.tx() as conn:
            prior = conn.execute("SELECT * FROM approvals WHERE offline_id=?", (offline_id,)).fetchone()
            if prior:
                if prior["plan_id"] == plan_id and prior["plan_revision"] == expected and prior["decision"] == "rejected": return {"plan": self.get_plan(plan_id, role, ""), "idempotent": True}
                raise ApiError(409, "offline_id_conflict", "该离线审核编号已经被使用")
            plan = self._plan_row(conn, plan_id)
            if plan["status"] != "submitted" or plan["revision"] != expected: raise ApiError(409, "revision_conflict", "计划状态或版本不匹配")
            conn.execute("INSERT INTO approvals(plan_id,plan_revision,reviewer,decision,reason,offline_id,created_at) VALUES(?,?,?,?,?,?,?)", (plan_id, expected, actor, "rejected", reason, offline_id, iso()))
            self._cancel_waiting(conn, plan_id, f"计划被拒绝：{reason}", iso())
            conn.execute("UPDATE flight_plans SET status='rejected',updated_at=? WHERE id=?", (iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_rejected", {"reason": reason, "offline_id": offline_id})
            Repository.notify(conn, plan_id, "rejected", f"飞行计划 {plan['callsign']} 被拒绝：{reason}")
            return {"plan": self.get_plan(plan_id, role, ""), "idempotent": False}

    def change(self, plan_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "change_forbidden", "只有运营方可以变更计划")
        expected = body.get("expected_revision")
        if not isinstance(expected, int): raise ApiError(400, "revision_required", "expected_revision 必填")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能修改其他运营方计划")
            if plan["status"] in CLOSED_STATUSES: raise ApiError(409, "plan_closed", "已取消、失效或过期计划不能修改")
            if plan["revision"] != expected: raise ApiError(409, "revision_conflict", "计划版本已变化")
            previous_status = plan["status"]
            route = validate_route(body.get("route", self._route(plan)))
            start = parse_time(body.get("starts_at", plan["starts_at"])); end = parse_time(body.get("ends_at", plan["ends_at"]))
            if end <= start or start <= utcnow(): raise ApiError(400, "invalid_time", "新飞行时间无效")
            payload = float(body.get("payload_kg", plan["payload_kg"])); altitude = float(body.get("max_altitude", plan["max_altitude"]))
            risk = body.get("population_risk", plan["population_risk"])
            if not 0 <= payload <= 25 or altitude <= 0 or not isinstance(risk, int) or not 0 <= risk <= 5: raise ApiError(400, "invalid_plan", "变更后的载荷、高度或风险无效")
            revision = expected + 1
            if previous_status == "approved":
                self._close_plan_slots(conn, plan_id, "plan_changed")
            else:
                self._cancel_waiting(conn, plan_id, "plan_changed", iso())
            conn.execute("""UPDATE flight_plans SET route_json=?,starts_at=?,ends_at=?,payload_kg=?,max_altitude=?,population_risk=?,emergency_plan=?,region=?,status='draft',revision=?,updated_at=? WHERE id=?""",
                         (json.dumps(route), iso(start), iso(end), payload, altitude, risk, body.get("emergency_plan", plan["emergency_plan"]), body.get("region", plan["region"]), revision, iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_changed", {"from_revision": expected, "to_revision": revision, "previous_status": previous_status})
            if previous_status == "approved": Repository.notify(conn, plan_id, "approval_invalidated", f"飞行计划 {plan['callsign']} 已修改，原批准自动失效，走廊时隙占用已释放")
            else: Repository.notify(conn, plan_id, "changed", f"飞行计划 {plan['callsign']} 已更新，需重新提交审核")
            return self.get_plan(plan_id, role, operator)

    def cancel(self, plan_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        reason = str(body.get("reason", "")).strip()
        if not reason: raise ApiError(400, "reason_required", "取消原因必填")
        with self.repo.tx() as conn:
            plan = self._plan_row(conn, plan_id)
            if role == "operator" and plan["operator_id"] != operator: raise ApiError(403, "plan_forbidden", "不能取消其他运营方计划")
            if role not in {"operator", "airspace_reviewer", "commander"}: raise ApiError(403, "cancel_forbidden", "当前角色不能取消计划")
            if plan["status"] == "canceled": return {"plan": self.get_plan(plan_id, role, operator), "idempotent": True}
            if plan["status"] in CLOSED_STATUSES: raise ApiError(409, "plan_closed", "已失效或过期计划不能取消")
            self._close_plan_slots(conn, plan_id, f"plan_canceled:{reason}")
            conn.execute("UPDATE flight_plans SET status='canceled',updated_at=? WHERE id=?", (iso(), plan_id))
            Repository.audit(conn, plan_id, actor, role, "plan_canceled", {"reason": reason})
            Repository.notify(conn, plan_id, "canceled", f"飞行计划 {plan['callsign']} 已取消：{reason}，走廊时隙已释放")
            return {"plan": self.get_plan(plan_id, role, operator), "idempotent": False}

    def notifications(self, actor: str, role: str, operator: str) -> dict[str, Any]:
        if role == "operator":
            rows = self.repo.conn.execute("""SELECT n.* FROM notifications n JOIN flight_plans p ON p.id=n.plan_id WHERE p.operator_id=? ORDER BY n.id DESC""", (operator,))
        elif role in {"airspace_reviewer", "commander", "auditor"}: rows = self.repo.conn.execute("SELECT * FROM notifications ORDER BY id DESC")
        else: raise ApiError(403, "notifications_forbidden", "当前角色不能读取通知")
        return {"notifications": [dict(r) for r in rows]}

    def expire_plans(self, actor: str, role: str) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "expire_forbidden", "当前角色不能执行到期处理")
        now = iso()
        with self.repo.tx() as conn:
            rows = list(conn.execute("SELECT * FROM flight_plans WHERE status='approved' AND ends_at<=?", (now,)))
            affected: set[int] = set()
            for row in rows:
                affected |= self._release_holds(conn, row["id"], "plan_expired", now)
                affected |= self._cancel_waiting(conn, row["id"], "plan_expired", now)
                conn.execute("UPDATE flight_plans SET status='expired',updated_at=? WHERE id=?", (now, row["id"]))
                Repository.audit(conn, row["id"], actor, role, "plan_expired", {})
                Repository.notify(conn, row["id"], "expired", f"飞行计划 {row['callsign']} 已过期，走廊时隙已释放")
            if affected:
                self._bump_ledger(conn, affected)
                self._pump_waitlist(conn, affected)
        return {"expired": len(rows)}

    def create_corridor(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"airspace_reviewer", "commander"}: raise ApiError(403, "corridor_forbidden", "只有空域审核员或指挥官可以维护走廊")
        name = str(body.get("name", "")).strip()
        if not name: raise ApiError(400, "invalid_corridor", "走廊名称必填")
        try:
            min_lon, min_lat, max_lon, max_lat = map(float, (body.get("min_lon"), body.get("min_lat"), body.get("max_lon"), body.get("max_lat")))
            min_alt, max_alt = float(body.get("min_altitude", 0)), float(body.get("max_altitude"))
            capacity, slot_seconds = int(body.get("capacity")), int(body.get("slot_seconds"))
        except (TypeError, ValueError): raise ApiError(400, "invalid_corridor", "走廊范围、容量或时隙粒度必须为数字")
        if min_lon >= max_lon or min_lat >= max_lat or min_alt < 0 or max_alt <= min_alt or capacity <= 0 or slot_seconds <= 0:
            raise ApiError(400, "invalid_corridor", "走廊范围、高度、容量或时隙粒度无效")
        with self.repo.tx() as conn:
            try:
                cur = conn.execute("""INSERT INTO corridors(name,min_lon,min_lat,max_lon,max_lat,min_altitude,max_altitude,capacity,slot_seconds,created_at)
                                      VALUES(?,?,?,?,?,?,?,?,?,?)""",
                                   (name, min_lon, min_lat, max_lon, max_lat, min_alt, max_alt, capacity, slot_seconds, iso()))
            except sqlite3.IntegrityError as exc: raise ApiError(409, "corridor_duplicate", "同名走廊已存在") from exc
            Repository.audit(conn, None, actor, role, "corridor_created", {"corridor_id": cur.lastrowid, "capacity": capacity, "slot_seconds": slot_seconds})
            return dict(conn.execute("SELECT * FROM corridors WHERE id=?", (cur.lastrowid,)).fetchone())

    def list_corridors(self, role: str) -> dict[str, Any]:
        if role not in {"operator", "airspace_reviewer", "commander", "auditor"}: raise ApiError(403, "corridor_forbidden", "当前角色不能查看走廊台账")
        conn = self.repo.conn
        return {"corridors": [dict(r) for r in conn.execute("SELECT * FROM corridors WHERE status='active' ORDER BY id")], "server_time": iso()}

    def corridor_ledger(self, corridor_id: int, role: str) -> dict[str, Any]:
        if role not in {"operator", "airspace_reviewer", "commander", "auditor"}: raise ApiError(403, "corridor_forbidden", "当前角色不能查看走廊台账")
        conn = self.repo.conn
        corr = conn.execute("SELECT * FROM corridors WHERE id=?", (corridor_id,)).fetchone()
        if not corr: raise ApiError(404, "corridor_not_found", "走廊不存在")
        rows = conn.execute("""SELECT r.slot_start,r.slot_end,COUNT(*) AS used,COALESCE(SUM(r.emergency),0) AS emergency_used,
                               GROUP_CONCAT(CASE WHEN r.emergency=1 THEN r.plan_id END) AS emergency_plan_ids
                               FROM slot_reservations r WHERE r.corridor_id=? AND r.released_at IS NULL GROUP BY r.slot_start ORDER BY r.slot_start""",
                            (corridor_id,)).fetchall()
        slots = [{"slot_start": r["slot_start"], "slot_end": r["slot_end"], "capacity": corr["capacity"],
                  "used": r["used"], "emergency_used": r["emergency_used"], "remaining": corr["capacity"] - r["used"],
                  "emergency_plan_ids": [int(x) for x in r["emergency_plan_ids"].split(",")] if r["emergency_plan_ids"] else []} for r in rows]
        waits = conn.execute("""SELECT w.*,p.callsign,p.operator_id FROM slot_waitlist w JOIN flight_plans p ON p.id=w.plan_id
                                WHERE w.corridor_id=? AND w.status='waiting' ORDER BY w.slot_start,w.queued_at,w.id""", (corridor_id,)).fetchall()
        waitlist = []
        slot_positions: dict[str, int] = {}
        for w in waits:
            pos = slot_positions.get(w["slot_start"], 0) + 1
            slot_positions[w["slot_start"]] = pos
            item = {"id": w["id"], "plan_id": w["plan_id"], "slot_start": w["slot_start"], "slot_end": w["slot_end"],
                    "queued_at": w["queued_at"], "queue_position": pos, "notified_at": w["notified_at"]}
            if role in {"airspace_reviewer", "commander", "auditor"}: item.update({"callsign": w["callsign"], "operator_id": w["operator_id"]})
            waitlist.append(item)
        return {"corridor": dict(corr), "slots": slots, "waitlist": waitlist, "server_time": iso()}

    def state(self, role: str, operator: str) -> dict[str, Any]:
        conn = self.repo.conn
        if role == "operator": rows = conn.execute("SELECT * FROM flight_plans WHERE operator_id=? ORDER BY id DESC", (operator,))
        elif role in {"airspace_reviewer", "commander", "auditor"}: rows = conn.execute("SELECT * FROM flight_plans ORDER BY id DESC")
        else: rows = conn.execute("SELECT * FROM flight_plans WHERE status='approved' ORDER BY id DESC")
        plans = []
        for row in rows:
            item = self.get_plan(row["id"], role, operator); plans.append(item)
        restrictions = [dict(r) for r in conn.execute("SELECT * FROM restrictions WHERE status='active' ORDER BY id DESC")] if role in {"airspace_reviewer", "commander", "auditor"} else []
        corridors = [dict(r) for r in conn.execute("SELECT id,name,min_lon,min_lat,max_lon,max_lat,min_altitude,max_altitude,capacity,slot_seconds,ledger_version FROM corridors WHERE status='active' ORDER BY id")] if role in {"operator", "airspace_reviewer", "commander", "auditor"} else []
        return {"plans": plans, "restrictions": restrictions, "corridors": corridors, "server_time": iso()}


def send_json(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    raw = json.dumps(payload, ensure_ascii=False, default=str).encode(); handler.send_response(status); handler.send_header("Content-Type", "application/json; charset=utf-8"); handler.send_header("Content-Length", str(len(raw))); handler.end_headers(); handler.wfile.write(raw)


class Handler(BaseHTTPRequestHandler):
    service: DroneAirspaceService; web_root: Path
    def log_message(self, fmt: str, *args: Any) -> None: print(f"{self.address_string()} - {fmt % args}")
    def body(self) -> dict[str, Any]:
        size = int(self.headers.get("Content-Length", "0"))
        if not size: return {}
        try: value = json.loads(self.rfile.read(size))
        except json.JSONDecodeError as exc: raise ApiError(400, "invalid_json", "请求体不是有效 JSON") from exc
        if not isinstance(value, dict): raise ApiError(400, "invalid_json", "请求体必须是对象")
        return value
    def get_api(self, path: str) -> tuple[int, Any]:
        if path == "/health": return 200, {"status": "ok", "service": "drone-airspace"}
        actor, role, operator = self.service.identity(self.headers)
        if path == "/api/state": return 200, self.service.state(role, operator)
        if path == "/api/notifications": return 200, self.service.notifications(actor, role, operator)
        if path == "/api/corridors": return 200, self.service.list_corridors(role)
        parts = [p for p in path.split("/") if p]
        if len(parts) == 4 and parts[:2] == ["api", "corridors"] and parts[2].isdigit() and parts[3] == "ledger":
            return 200, self.service.corridor_ledger(int(parts[2]), role)
        if len(parts) == 3 and parts[:2] == ["api", "plans"] and parts[2].isdigit(): return 200, self.service.get_plan(int(parts[2]), role, operator)
        if len(parts) == 4 and parts[:2] == ["api", "plans"] and parts[2].isdigit() and parts[3] == "check": return 200, self.service.check_conflicts(int(parts[2]), role, operator)
        raise ApiError(404, "not_found", "接口不存在")
    def post_api(self, path: str) -> tuple[int, Any]:
        actor, role, operator = self.service.identity(self.headers); body = self.body(); parts = [p for p in path.split("/") if p]
        if path == "/api/restrictions": return 201, self.service.create_restriction(actor, role, body)
        if path == "/api/corridors": return 201, self.service.create_corridor(actor, role, body)
        if path == "/api/plans": return 201, self.service.create_plan(actor, role, operator, body)
        if path == "/api/expire": return 200, self.service.expire_plans(actor, role)
        if len(parts) == 4 and parts[:2] == ["api", "plans"] and parts[2].isdigit():
            pid, action = int(parts[2]), parts[3]
            routes = {
                "submit": lambda: self.service.submit(pid, actor, role, operator, body),
                "approve": lambda: self.service.approve(pid, actor, role, body),
                "reject": lambda: self.service.reject(pid, actor, role, body),
                "change": lambda: self.service.change(pid, actor, role, operator, body),
                "cancel": lambda: self.service.cancel(pid, actor, role, operator, body),
            }
            if action in routes: return 200, routes[action]()
        raise ApiError(404, "not_found", "接口不存在")
    def handle_request(self, method: str) -> None:
        parsed = urlparse(self.path)
        try:
            if method == "GET" and parsed.path == "/":
                raw = (self.web_root / "index.html").read_bytes(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
            status, payload = self.get_api(parsed.path) if method == "GET" else self.post_api(parsed.path); send_json(self, status, payload)
        except ApiError as exc:
            payload = {"error": exc.code, "message": exc.message}
            if exc.details is not None: payload["details"] = exc.details
            send_json(self, exc.status, payload)
        except Exception as exc: print(f"unhandled error: {exc!r}"); send_json(self, 500, {"error": "internal_error", "message": str(exc)})
    def do_GET(self) -> None: self.handle_request("GET")
    def do_POST(self) -> None: self.handle_request("POST")


def create_server(db_path: str | Path, host: str = "127.0.0.1", port: int = PORT) -> ThreadingHTTPServer:
    service = DroneAirspaceService(db_path); handler = type("DroneHandler", (Handler,), {"service": service, "web_root": Path(__file__).resolve().parent / "static"}); return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=PORT); parser.add_argument("--db", default=os.environ.get("DRONE_DB", "drone_airspace.db")); args = parser.parse_args()
    server = create_server(args.db, args.host, args.port); print(f"drone-airspace listening on http://{args.host}:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__ == "__main__": main()
