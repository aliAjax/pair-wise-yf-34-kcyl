import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, DroneAirspaceService, iso, utcnow


class SlotLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = DroneAirspaceService(Path(self.tmp.name) / "test.db", slot_capacity=3, slot_minutes=30)
        self.start = utcnow() + timedelta(hours=2)

    def tearDown(self): self.tmp.cleanup()

    def plan(self, callsign="D100", altitude=100, payload=5, route=None, start_off=0):
        s = self.start + timedelta(minutes=start_off)
        return self.svc.create_plan("op-user", "operator", "OP1", {
            "callsign": callsign, "drone_model": "M400", "payload_kg": payload,
            "route": route or [[116.1, 39.8], [116.3, 39.9]],
            "starts_at": iso(s), "ends_at": iso(s + timedelta(minutes=60)),
            "max_altitude": altitude, "population_risk": 1, "emergency_plan": "返航", "region": "BJ"})

    def submit(self, p): return self.svc.submit(p["id"], "op-user", "operator", "OP1", {})["plan"]

    def approve(self, p, cid, role="airspace_reviewer", override=None, capver=None):
        body = {"expected_revision": p["revision"], "offline_id": cid, "reason": "符合要求"}
        if override: body["override_reason"] = override
        if capver is not None: body["expected_capacity_version"] = capver
        return self.svc.approve(p["id"], "rev", role, body)

    def occupied(self):
        return sum(s["occupied"] for s in self.svc.ledger("airspace_reviewer")["slots"])

    def test_capacity_queues_with_earliest_release_and_version(self):
        plans = [self.submit(self.plan(f"D{i}")) for i in range(4)]
        results = [self.approve(p, f"off-{i}") for i, p in enumerate(plans)]
        self.assertTrue(all(not r["queued"] for r in results[:3]))
        queued = results[3]
        self.assertTrue(queued["queued"])
        self.assertEqual(queued["plan"]["status"], "queued")
        self.assertEqual(queued["remaining_capacity"], 0)
        self.assertIsNotNone(queued["earliest_release_at"])
        # 最早释放时间不应早于当前批次计划结束时刻
        self.assertGreaterEqual(queued["earliest_release_at"], plans[0]["ends_at"])
        ledger = self.svc.ledger("airspace_reviewer")
        self.assertEqual(len(ledger["queued"]), 1)
        self.assertEqual(ledger["queued"][0]["id"], plans[3]["id"])
        self.assertGreater(ledger["capacity_version"], 0)

    def test_concurrent_reviewers_only_one_approves_later_sees_version(self):
        p1 = self.submit(self.plan("D1")); p2 = self.submit(self.plan("D2")); p3 = self.submit(self.plan("D3"))
        self.approve(p1, "off-1")
        before = self.svc.ledger("airspace_reviewer")["capacity_version"]
        self.approve(p2, "off-2"); self.approve(p3, "off-3")  # 占满全部名额
        p4 = self.submit(self.plan("D4"))
        # 后到的审核员携带旧容量版本 -> 版本冲突，且能看到剩余容量与新版本
        with self.assertRaises(ApiError) as ctx:
            self.approve(p4, "off-4", capver=before)
        self.assertEqual(ctx.exception.code, "capacity_version_conflict")
        self.assertEqual(ctx.exception.details["remaining_capacity"], 0)
        self.assertEqual(ctx.exception.details["capacity_version"], before + 2)
        # 不带版本号也不会超卖：排队而非批准
        r = self.approve(p4, "off-5")
        self.assertTrue(r["queued"])
        self.assertEqual(self.svc.get_plan(p4["id"], "airspace_reviewer", "")["status"], "queued")

    def test_emergency_jump_queue_marked_but_hard_limit_holds(self):
        plans = [self.submit(self.plan(f"D{i}")) for i in range(3)]
        for i, p in enumerate(plans): self.approve(p, f"off-{i}")
        full = self.submit(self.plan("FULL"))
        # 常规审核在满员时排队
        self.assertTrue(self.approve(full, "off-full")["queued"])
        # 指挥官紧急授权可插队占用，但单独标记
        emergency = self.submit(self.plan("EMERG"))
        r = self.approve(emergency, "off-emerg", role="commander", override="应急救援")
        self.assertFalse(r["queued"])
        self.assertEqual(r["plan"]["status"], "approved")
        self.assertEqual(r["override_kind"], "emergency_authority")
        # 紧急授权也不能绕过高度硬限制
        hard = self.submit(self.plan("HARD", altitude=150))
        with self.assertRaises(ApiError) as ctx:
            self.approve(hard, "off-hard", role="commander", override="应急救援")
        self.assertEqual(ctx.exception.code, "hard_constraint_violation")

    def test_cancel_releases_slot_and_allows_follow_up(self):
        plans = [self.submit(self.plan(f"D{i}")) for i in range(3)]
        for i, p in enumerate(plans): self.approve(p, f"off-{i}")
        self.assertEqual(self.occupied(), 9)
        self.svc.cancel(plans[0]["id"], "op-user", "operator", "OP1", {"reason": "任务取消"})
        self.assertEqual(self.occupied(), 6)
        follow = self.submit(self.plan("FOLLOW"))
        r = self.approve(follow, "off-follow")
        self.assertFalse(r["queued"])
        self.assertEqual(r["plan"]["status"], "approved")

    def test_no_fly_update_invalidates_and_releases_occupancy(self):
        plans = [self.submit(self.plan(f"D{i}")) for i in range(3)]
        for i, p in enumerate(plans): self.approve(p, f"off-{i}")
        self.assertEqual(self.occupied(), 9)
        res = self.svc.create_restriction("rev", "airspace_reviewer", {
            "name": "临时禁飞", "kind": "no_fly", "min_lon": 116.0, "min_lat": 39.7,
            "max_lon": 116.5, "max_lat": 40.1, "min_altitude": 0, "max_altitude": 150,
            "starts_at": iso(self.start - timedelta(minutes=30)),
            "ends_at": iso(self.start + timedelta(hours=3)), "reason": "活动"})
        self.assertEqual(len(res["invalidated_plan_ids"]), 3)
        self.assertEqual(self.occupied(), 0)
        for pid in res["invalidated_plan_ids"]:
            self.assertEqual(self.svc.get_plan(pid, "airspace_reviewer", "")["status"], "submitted")
        notes = self.svc.notifications("rev", "airspace_reviewer", "")["notifications"]
        self.assertTrue(any(n["kind"] == "airspace_change" for n in notes))
        # 限制更新接口同样会让重叠的已批准计划失效（此计划经紧急特批占用）
        again = self.submit(self.plan("AGAIN"))
        self.approve(again, "off-again", role="commander", override="特批任务")
        self.assertEqual(self.occupied(), 3)
        upd = self.svc.update_restriction(1, "rev", "airspace_reviewer", {"reason": "禁飞延期"})
        self.assertIn(again["id"], upd["invalidated_plan_ids"])
        self.assertEqual(self.occupied(), 0)


if __name__ == "__main__": unittest.main()
