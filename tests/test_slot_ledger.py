import sys, tempfile, threading, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, DroneAirspaceService, iso, utcnow


class SlotLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = DroneAirspaceService(Path(self.tmp.name) / "test.db")
        # 对齐到整点（走廊时隙边界），使 1 小时计划窗恰好落在一个槽内
        self.start = (utcnow() + timedelta(hours=2, minutes=30)).replace(minute=0, second=0, microsecond=0)
        self.corr = self.svc.create_corridor("reviewer", "airspace_reviewer", {
            "name": "京津走廊", "min_lon": 116.0, "min_lat": 39.7, "max_lon": 116.4, "max_lat": 40.0,
            "min_altitude": 0, "max_altitude": 150, "capacity": 1, "slot_seconds": 3600})

    def tearDown(self): self.tmp.cleanup()

    def plan(self, callsign, route=None, start=None, hours=1, operator="OP1", altitude=100):
        s = start or self.start
        return self.svc.create_plan("op-user", "operator", operator, {"callsign": callsign, "drone_model": "M400", "payload_kg": 5,
            "route": route or [[116.1, 39.8], [116.3, 39.9]], "starts_at": iso(s), "ends_at": iso(s + timedelta(hours=hours)),
            "max_altitude": altitude, "population_risk": 1, "emergency_plan": "返回起降点", "region": "BJ"})

    def approve(self, pid, oid, role="airspace_reviewer", actor="reviewer", **extra):
        return self.svc.approve(pid, actor, role, {"expected_revision": 1, "offline_id": oid, "reason": "审核", **extra})

    def submit_approve(self, callsign, oid, **kw):
        p = self.plan(callsign, **kw)
        self.svc.submit(p["id"], "op-user", "operator", "OP1", {})
        return p, self.approve(p["id"], oid)

    def test_capacity_full_queues_with_earliest_release_then_release_notifies(self):
        p1, a1 = self.submit_approve("Q1", "o1")
        self.assertEqual(a1["plan"]["status"], "approved")
        p2 = self.plan("Q2"); self.svc.submit(p2["id"], "op-user", "operator", "OP1", {})
        with self.assertRaises(ApiError) as ctx:
            self.approve(p2["id"], "o2")
        self.assertEqual(ctx.exception.code, "slot_capacity_full")
        queued = ctx.exception.details["queued"]
        self.assertEqual(queued[0]["queue_position"], 1)
        self.assertEqual(queued[0]["remaining"], 0)
        # 最早释放时间等于已占用计划的槽位结束时间
        self.assertEqual(queued[0]["earliest_release"], a1["corridor_ledgers"][0]["slots"][0]["slot_end"])
        # 等待记录已落库，计划仍是 submitted
        fresh = self.svc.get_plan(p2["id"], "airspace_reviewer", "")
        self.assertEqual(fresh["status"], "submitted"); self.assertEqual(len(fresh["slot_waitlist"]), 1)
        # 释放：取消第一个计划后，排队方收到 slot_available 通知
        self.svc.cancel(p1["id"], "op-user", "operator", "OP1", {"reason": "取消"})
        notes = [n for n in self.svc.notifications("op-user", "operator", "OP1")["notifications"] if n["kind"] == "slot_available"]
        self.assertTrue(notes)
        # 释放后可按队列顺序批过
        a2 = self.approve(p2["id"], "o2")
        self.assertEqual(a2["plan"]["status"], "approved")

    def test_concurrent_reviewers_only_one_wins(self):
        p1 = self.plan("C1"); p2 = self.plan("C2")
        for p in (p1, p2): self.svc.submit(p["id"], "op-user", "operator", "OP1", {})
        results: list = []
        barrier = threading.Barrier(2)

        def worker(pid, oid):
            barrier.wait()
            try: results.append(("ok", self.approve(pid, oid)))
            except ApiError as exc: results.append(("err", exc.code, exc.details))

        t1 = threading.Thread(target=worker, args=(p1["id"], "c1")); t2 = threading.Thread(target=worker, args=(p2["id"], "c2"))
        t1.start(); t2.start(); t1.join(); t2.join()
        oks = [r for r in results if r[0] == "ok"]
        errs = [r for r in results if r[0] == "err"]
        self.assertEqual(len(oks), 1); self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0][1], "slot_capacity_full")
        self.assertEqual(errs[0][2]["queued"][0]["remaining"], 0)
        # 台账版本：成功的一次使其从 0 变成 1
        ledger = self.svc.corridor_ledger(self.corr["id"], "airspace_reviewer")
        self.assertGreaterEqual(ledger["corridor"]["ledger_version"], 1)
        self.assertEqual(len(ledger["waitlist"]), 1)

    def test_ledger_version_optimistic_conflict(self):
        p1 = self.plan("V1"); self.svc.submit(p1["id"], "op-user", "operator", "OP1", {})
        self.approve(p1["id"], "v1")
        p2 = self.plan("V2"); self.svc.submit(p2["id"], "op-user", "operator", "OP1", {})
        with self.assertRaises(ApiError) as ctx:
            self.approve(p2["id"], "v2", expected_ledger_versions={str(self.corr["id"]): 0})
        self.assertEqual(ctx.exception.code, "ledger_version_conflict")
        self.assertIn(self.corr["id"], ctx.exception.details["stale"])
        # 后到审核员能看到最新剩余容量和版本
        slot = ctx.exception.details["corridor_ledgers"][0]["slots"][0]
        self.assertEqual(slot["remaining"], 0)

    def test_emergency_jumps_queue_but_hard_limits_remain(self):
        p1, _ = self.submit_approve("E1", "e1")
        p2 = self.plan("E2"); self.svc.submit(p2["id"], "op-user", "operator", "OP1", {})
        with self.assertRaises(ApiError): self.approve(p2["id"], "e2")
        # 普通审核员不能发紧急授权
        with self.assertRaises(ApiError) as ctx:
            self.approve(p2["id"], "e2x", emergency=True, override_reason="急救")
        self.assertEqual(ctx.exception.code, "emergency_forbidden")
        # 指挥官紧急插队：容量为 1 也能超售，占用单独标记
        em = self.approve(p2["id"], "e3", role="commander", actor="boss", emergency=True, override_reason="应急救援")
        self.assertTrue(em["emergency"]); self.assertEqual(em["override_kind"], "emergency_authority")
        ledger = self.svc.corridor_ledger(self.corr["id"], "airspace_reviewer")
        first = ledger["slots"][0]
        self.assertEqual(first["used"], 2); self.assertEqual(first["emergency_used"], 1); self.assertEqual(first["remaining"], -1)
        # 载荷/高度硬限制：紧急授权也批不过
        heavy = self.plan("E3", altitude=130); self.svc.submit(heavy["id"], "op-user", "operator", "OP1", {})
        with self.assertRaises(ApiError) as ctx:
            self.approve(heavy["id"], "e4", role="commander", actor="boss", emergency=True, override_reason="高层应急")
        self.assertEqual(ctx.exception.code, "hard_constraint_violation")
        self.assertIn("altitude_limit", [v["code"] for v in ctx.exception.details["hard_violations"]])

    def test_no_fly_invalidates_approved_and_releases_slots(self):
        p, approved = self.submit_approve("N1", "n1")
        self.assertEqual(approved["plan"]["status"], "approved")
        result = self.svc.create_restriction("commander", "commander", {
            "name": "新增禁飞", "kind": "no_fly", "min_lon": 116.05, "min_lat": 39.75, "max_lon": 116.35, "max_lat": 39.95,
            "min_altitude": 0, "max_altitude": 150, "starts_at": iso(self.start - timedelta(minutes=10)),
            "ends_at": iso(self.start + timedelta(hours=3)), "reason": "突发事件"})
        self.assertEqual(result["invalidated_plan_ids"], [p["id"]])
        self.assertEqual(self.svc.get_plan(p["id"], "airspace_reviewer", "")["status"], "invalidated")
        # 占用已释放：新计划走走廊内但禁飞区外的航段，可在同一时隙批过
        p2 = self.plan("N2", route=[[116.1, 39.96], [116.3, 39.98]])
        self.svc.submit(p2["id"], "op-user", "operator", "OP1", {})
        a2 = self.approve(p2["id"], "n2")
        self.assertEqual(a2["plan"]["status"], "approved")
        # 失效计划收到通知且不能再改
        notes = [n for n in self.svc.notifications("op-user", "operator", "OP1")["notifications"] if n["kind"] == "approval_invalidated"]
        self.assertTrue(notes)
        with self.assertRaises(ApiError) as ctx:
            self.svc.change(p["id"], "op-user", "operator", "OP1", {"expected_revision": 1, "region": "TJ"})
        self.assertEqual(ctx.exception.code, "plan_closed")

    def test_fifo_queue_blocks_late_plan_until_front_clears(self):
        _, _ = self.submit_approve("F1", "f1")
        p2 = self.plan("F2"); self.svc.submit(p2["id"], "op-user", "operator", "OP1", {})
        with self.assertRaises(ApiError): self.approve(p2["id"], "f2")
        # p2 已在队首；p3 更晚提交，必须排在后面，不能越过
        p3 = self.plan("F3"); self.svc.submit(p3["id"], "op-user", "operator", "OP1", {})
        with self.assertRaises(ApiError) as ctx:
            self.approve(p3["id"], "f3")
        self.assertIn(ctx.exception.code, {"ahead_in_queue", "slot_capacity_full"})
        self.assertEqual(ctx.exception.details["queued"][0]["queue_position"], 2)
        ledger = self.svc.corridor_ledger(self.corr["id"], "airspace_reviewer")
        first_slot = iso(self.start)
        ordered = [w["plan_id"] for w in ledger["waitlist"] if w["slot_start"] == first_slot]
        self.assertEqual(ordered, [p2["id"], p3["id"]])


if __name__ == "__main__": unittest.main()
