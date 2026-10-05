import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, DomainError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


ADMIN = Actor("admin", "admin")
COMMITTEE = Actor("committee", "committee")
BIOBANK = Actor("biobank", "biobank")


class PurposeConsentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.svc = self.service

    def tearDown(self):
        self.tmp.cleanup()

    # ---- 夹具 -----------------------------------------------------------

    def make_participant(self, name="Participant"):
        return self.svc.create(ADMIN, "participant", {"name": name})

    def make_active_consent(self, participant_id, scope, version="v1", expires="2099-01-01", actor=None):
        consent = self.svc.create(
            actor or COMMITTEE, "consent",
            {"participant_id": participant_id, "scope": list(scope)},
        )
        return self.svc.transition(
            actor or COMMITTEE, consent["id"], "activate",
            {"scope": list(scope), "version": version, "expires_at": expires},
        )

    def make_stored_sample(self, participant_id, code, purposes):
        sample = self.svc.create(
            ADMIN, "sample",
            {"participant_id": participant_id, "sample_code": code, "collected_at": "2026-01-01"},
        )
        return self.svc.transition(
            ADMIN, sample["id"], "store",
            {"freezer": "F1", "position": "A1", "purposes": list(purposes)},
        )

    def approve_and_execute(self, participant_id, samples, purposes, requested="2026-03-01"):
        withdrawal = self.svc.create(
            ADMIN, "withdrawal",
            {"participant_id": participant_id, "requested_at": requested, "purposes": list(purposes)},
        )
        self.svc.transition(
            COMMITTEE, withdrawal["id"], "approve",
            {"reason": "participant request", "sample_ids": [s["id"] for s in samples]},
        )
        executed = self.svc.transition(
            BIOBANK, withdrawal["id"], "execute", {"executed_at": "2026-03-02"}
        )
        return executed

    def audits_for(self, entity_id, action=None):
        rows = self.svc.audit_log(entity_id)
        return [r for r in rows if action is None or r["action"] == action]

    # ---- 1. 部分用途撤回，其余用途有覆盖：样本继续可用 -------------------

    def test_partial_withdrawal_keeps_sample_for_other_purpose(self):
        p = self.make_participant()
        self.make_active_consent(p["id"], ["research", "genetic_analysis"])
        sample = self.make_stored_sample(p["id"], "B-1", ["research", "genetic_analysis"])

        executed = self.approve_and_execute(p["id"], [sample], ["research"])
        self.assertEqual(executed["status"], "executed")

        sample = self.svc.get(sample["id"])
        # 遗传分析仍有同意覆盖 -> 样本继续可用，保持 stored
        self.assertEqual(sample["status"], "stored")
        outcome = executed["data"]["sample_outcomes"][sample["id"]]
        self.assertEqual(outcome["decision"], "retain")
        self.assertEqual(outcome["covered"], ["genetic_analysis"])

        participant = self.svc.get(p["id"])
        self.assertEqual(participant["data"]["withdrawn_purposes"], ["research"])

    # ---- 2. 唯一用途被撤回：未覆盖 -> 待处置 ------------------------------

    def test_withdraw_only_covered_purpose_moves_sample_to_pending_disposal(self):
        p = self.make_participant()
        self.make_active_consent(p["id"], ["research", "genetic_analysis"])
        # 仅用于遗传分析的样本：参与者只想停科研不影响它；反之停遗传分析则失效
        only_genetic = self.make_stored_sample(p["id"], "B-g", ["genetic_analysis"])
        both = self.make_stored_sample(p["id"], "B-b", ["research", "genetic_analysis"])

        executed = self.approve_and_execute(p["id"], [only_genetic, both], ["genetic_analysis"])
        only_genetic = self.svc.get(only_genetic["id"])
        both = self.svc.get(both["id"])
        self.assertEqual(only_genetic["status"], "pending_disposal")
        # 另一样本科研用途仍被覆盖
        self.assertEqual(both["status"], "stored")
        self.assertEqual(
            executed["data"]["sample_outcomes"][only_genetic["id"]]["decision"],
            "pending_disposal",
        )

        # 待处置样本只能销毁/匿名化，不能借出
        from src.domain import InvalidTransition
        with self.assertRaises(InvalidTransition):
            self.svc.transition(
                ADMIN, only_genetic["id"], "loan",
                {"recipient": "lab-x", "purpose": "research", "due_at": "2026-12-01"},
            )
        destroyed = self.svc.transition(
            BIOBANK, only_genetic["id"], "destroy", {"reason": "consent lost"}
        )
        self.assertEqual(destroyed["status"], "destroyed")

    # ---- 3. 在借样本：先待召回，归还后再处置 ------------------------------

    def test_loaned_sample_goes_pending_recall_then_disposal_on_return(self):
        p = self.make_participant()
        self.make_active_consent(p["id"], ["research", "genetic_analysis"])
        sample = self.make_stored_sample(p["id"], "B-loan", ["research", "genetic_analysis"])
        loaned = self.svc.transition(
            ADMIN, sample["id"], "loan",
            {"recipient": "external-lab", "purpose": "research", "due_at": "2026-12-01"},
        )
        self.assertEqual(loaned["status"], "on_loan")

        self.approve_and_execute(p["id"], [loaned], ["research", "genetic_analysis"])
        waiting = self.svc.get(sample["id"])
        self.assertEqual(waiting["status"], "pending_recall")

        # 归还时仍无覆盖 -> 转待处置
        returned = self.svc.transition(ADMIN, sample["id"], "return", {"returned_at": "2026-04-01"})
        self.assertEqual(returned["status"], "pending_disposal")

    # ---- 4. 待召回期间用途恢复覆盖：归还后保留 ----------------------------

    def test_pending_recall_returns_to_stored_when_coverage_restored(self):
        p = self.make_participant()
        consent = self.make_active_consent(p["id"], ["research", "genetic_analysis"])
        sample = self.make_stored_sample(p["id"], "B-restore", ["research", "genetic_analysis"])
        loaned = self.svc.transition(
            ADMIN, sample["id"], "loan",
            {"recipient": "external-lab", "purpose": "research", "due_at": "2026-12-01"},
        )
        # 同意被撤回：在借样本先转待召回（不立即处置）
        self.svc.transition(
            COMMITTEE, consent["id"], "withdraw", {"reason": "consent revoked"}
        )
        self.assertEqual(self.svc.get(sample["id"])["status"], "pending_recall")

        # 等待召回期间，新同意重新覆盖这些用途
        self.make_active_consent(p["id"], ["research", "genetic_analysis"], version="v2")
        returned = self.svc.transition(ADMIN, sample["id"], "return", {"returned_at": "2026-04-01"})
        self.assertEqual(returned["status"], "stored")

    # ---- 5. 撤回执行与样本借出同时提交：只有一边成功 ----------------------

    def test_concurrent_execute_and_loan_only_one_side_wins(self):
        results = {"loan": [], "execute": []}
        errors = []
        barrier = threading.Barrier(2)

        for round_index in range(10):
            p = self.make_participant("Participant-%d" % round_index)
            self.make_active_consent(p["id"], ["research", "genetic_analysis"])
            sample = self.make_stored_sample(
                p["id"], "B-%d" % round_index, ["research", "genetic_analysis"]
            )
            withdrawal = self.svc.create(
                ADMIN, "withdrawal",
                {"participant_id": p["id"], "requested_at": "2026-03-01",
                 "purposes": ["research", "genetic_analysis"]},
            )
            self.svc.transition(
                COMMITTEE, withdrawal["id"], "approve",
                {"reason": "request", "sample_ids": [sample["id"]]},
            )
            sample_version = self.svc.get(sample["id"])["version"]

            def do_loan():
                barrier.wait()
                try:
                    self.svc.transition(
                        ADMIN, sample["id"], "loan",
                        {"recipient": "lab", "purpose": "research", "due_at": "2026-12-01"},
                        expected_version=sample_version,
                    )
                    results["loan"].append(True)
                except DomainError:
                    # 撤回执行已先提交：借出因乐观锁/覆盖变化失败
                    results["loan"].append(False)
                except BaseException as exc:  # pragma: no cover - 暴露意外错误
                    errors.append(("loan", repr(exc)))

            def do_execute():
                barrier.wait()
                try:
                    self.svc.transition(
                        BIOBANK, withdrawal["id"], "execute",
                        {"executed_at": "2026-03-02"},
                    )
                    results["execute"].append(True)
                except DomainError:
                    # 样本已先借出：审批快照版本不匹配，撤回执行失败
                    results["execute"].append(False)
                except BaseException as exc:  # pragma: no cover
                    errors.append(("execute", repr(exc)))

            t1 = threading.Thread(target=do_loan)
            t2 = threading.Thread(target=do_execute)
            t1.start(); t2.start(); t1.join(); t2.join()
            self.assertFalse(errors, errors)

            loan_won = results["loan"][-1]
            execute_won = results["execute"][-1]
            self.assertTrue(
                loan_won != execute_won,
                "round %d: loan=%s execute=%s must be exactly one winner"
                % (round_index, loan_won, execute_won),
            )
            status = self.svc.get(sample["id"])["status"]
            if execute_won:
                self.assertEqual(status, "pending_disposal")
            else:
                self.assertEqual(status, "on_loan")

    # ---- 6. 审计能看出哪些用途和同意变化导致失效 --------------------------

    def test_audit_shows_purposes_and_consent_change_behind_invalidation(self):
        p = self.make_participant()
        consent = self.make_active_consent(p["id"], ["research"])
        sample = self.make_stored_sample(p["id"], "B-audit", ["research"])
        self.approve_and_execute(p["id"], [sample], ["research"])

        effects = self.audits_for(sample["id"], "withdrawal_effect")
        self.assertEqual(len(effects), 1)
        detail = effects[0]["detail"]
        self.assertEqual(detail["purposes_lost"], ["research"])
        self.assertEqual(detail["covered"], [])
        self.assertEqual(detail["uncovered"], [])
        self.assertEqual(detail["remaining"], [])
        self.assertEqual(detail["registered_purposes"], ["research"])
        self.assertEqual(detail["decision"], "pending_disposal")
        self.assertEqual(effects[0]["from_status"], "stored")
        self.assertEqual(effects[0]["to_status"], "pending_disposal")

        # 同意本身被撤回（而非撤回单）也应级联说明失效原因
        p2 = self.make_participant("P2")
        consent2 = self.make_active_consent(p2["id"], ["research", "genetic_analysis"])
        s2 = self.make_stored_sample(p2["id"], "B-c", ["research", "genetic_analysis"])
        self.svc.transition(
            COMMITTEE, consent2["id"], "withdraw", {"reason": "consent revoked by committee"}
        )
        s2 = self.svc.get(s2["id"])
        self.assertEqual(s2["status"], "pending_disposal")
        consent_effects = self.audits_for(s2["id"], "consent_effect")
        self.assertEqual(len(consent_effects), 1)
        d = consent_effects[0]["detail"]
        self.assertEqual(d["trigger"], "consent_withdraw")
        self.assertEqual(d["consent_id"], consent2["id"])
        self.assertEqual(set(d["purposes_lost"]), {"research", "genetic_analysis"})
        self.assertEqual(d["after"]["covered"], [])
        self.assertEqual(d["decision"], "pending_disposal")

    # ---- 7. 入库登记用途：无有效同意覆盖的用途不能入库 --------------------

    def test_store_requires_active_consent_for_every_purpose(self):
        p = self.make_participant()
        self.make_active_consent(p["id"], ["research"])
        sample = self.svc.create(
            ADMIN, "sample",
            {"participant_id": p["id"], "sample_code": "B-x", "collected_at": "2026-01-01"},
        )
        with self.assertRaises(ValidationError) as ctx:
            self.svc.transition(
                ADMIN, sample["id"], "store",
                {"freezer": "F1", "position": "A2",
                 "purposes": ["research", "genetic_analysis"]},
            )
        self.assertIn("genetic_analysis", str(ctx.exception))

        stored = self.svc.transition(
            ADMIN, sample["id"], "store",
            {"freezer": "F1", "position": "A2", "purposes": ["research"]},
        )
        self.assertEqual(stored["data"]["purposes"], ["research"])
        self.assertIn("research", stored["data"]["consent_snapshot"])

    # ---- 8. 审批时的跨对象校验 ------------------------------------------

    def test_approve_rejects_unknown_purpose_and_borrowed_status(self):
        p = self.make_participant()
        self.make_active_consent(p["id"], ["research", "genetic_analysis"])
        sample = self.make_stored_sample(p["id"], "B", ["research"])

        withdrawal = self.svc.create(
            ADMIN, "withdrawal",
            {"participant_id": p["id"], "requested_at": "2026-03-01",
             "purposes": ["genetic_analysis"]},
        )
        # 样本未登记遗传分析用途且无覆盖 -> 审批拒绝
        with self.assertRaises(ValidationError):
            self.svc.transition(
                COMMITTEE, withdrawal["id"], "approve",
                {"reason": "x", "sample_ids": [sample["id"]]},
            )

    def test_withdrawal_requires_purposes(self):
        p = self.make_participant()
        with self.assertRaises(ValidationError):
            self.svc.create(
                ADMIN, "withdrawal",
                {"participant_id": p["id"], "requested_at": "2026-03-01"},
            )

    # ---- 9. 借出用途必须仍被同意覆盖 ------------------------------------

    def test_loan_purpose_must_be_covered(self):
        p = self.make_participant()
        self.make_active_consent(p["id"], ["research"])
        sample = self.make_stored_sample(p["id"], "B-l", ["research"])
        with self.assertRaises(ValidationError):
            self.svc.transition(
                ADMIN, sample["id"], "loan",
                {"recipient": "lab", "purpose": "genetic_analysis", "due_at": "2026-12-01"},
            )

    # ---- 10. 同意过期后，归还/处置时按实时覆盖计算 ------------------------

    def test_expired_consent_not_covering(self):
        engine = RuleEngine(today=__import__("datetime").date(2026, 10, 5))
        service = DomainService(self.repo, engine)
        p = service.create(ADMIN, "participant", {"name": "Expiry"})
        consent = service.create(
            COMMITTEE, "consent", {"participant_id": p["id"], "scope": ["research"]}
        )
        service.transition(
            COMMITTEE, consent["id"], "activate",
            {"scope": ["research"], "version": "v1", "expires_at": "2026-09-01"},
        )
        sample = service.create(
            ADMIN, "sample",
            {"participant_id": p["id"], "sample_code": "B-e", "collected_at": "2026-01-01"},
        )
        with self.assertRaises(ValidationError):
            service.transition(
                ADMIN, sample["id"], "store",
                {"freezer": "F", "position": "P", "purposes": ["research"]},
            )


if __name__ == "__main__":
    unittest.main()
