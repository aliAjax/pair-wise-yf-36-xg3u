import tempfile
import unittest
from pathlib import Path
from threading import Thread

from src.domain import Actor, ConflictError, PermissionDenied
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())

    def tearDown(self):
        self.tmp.cleanup()

    def test_permission_denied(self):
        entity = self.service.create(
            Actor("admin", "admin"), 'participant', {'name': 'Participant'}
        )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("viewer", "viewer"),
                entity["id"],
                'close_participant',
                {},
            )

    def test_version_conflict(self):
        entity = self.service.create(
            Actor("admin", "admin"), 'participant', {'name': 'Participant'}
        )
        with self.assertRaises(ConflictError):
            self.service.transition(
                Actor("admin", "admin"),
                entity["id"],
                'close_participant',
                {},
                expected_version=999,
            )

    def test_duplicate_idempotency_key_returns_same_entity(self):
        first = self.service.create(
            Actor("admin", "admin"),
            'participant',
            {'name': 'Participant'},
            idempotency_key="duplicate-check",
        )
        second = self.service.create(
            Actor("admin", "admin"),
            'participant',
            {'name': 'Participant'},
            idempotency_key="duplicate-check",
        )
        self.assertEqual(first["id"], second["id"])

    def test_withdrawal_execute_and_loan_conflict_only_one_succeeds(self):
        participant = self.service.create(
            Actor("admin", "admin"), 'participant', {'name': 'Participant'}
        )
        consent = self.service.create(
            Actor("admin", "admin"), 'consent',
            {'participant_id': participant["id"], 'scope': ['research']},
        )
        self.service.transition(
            Actor("admin", "admin"), consent["id"], 'activate',
            {'scope': ['research'], 'version': 'v1', 'expires_at': '2099-01-01'},
        )
        sample = self.service.create(
            Actor("admin", "admin"), 'sample',
            {'participant_id': participant["id"], 'sample_code': 'B-001', 'collected_at': '2026-01-01'},
        )
        self.service.transition(
            Actor("admin", "admin"), sample["id"], 'store',
            {'freezer': 'F1', 'position': 'A1', 'consent_id': consent["id"], 'purposes': ['research']},
        )
        sample = self.service.get(sample["id"])
        withdrawal = self.service.create(
            Actor("admin", "admin"), 'withdrawal',
            {'participant_id': participant["id"], 'requested_at': '2026-03-01'},
        )
        self.service.transition(
            Actor("admin", "admin"), withdrawal["id"], 'approve',
            {'reason': 'participant request', 'sample_ids': [sample["id"]], 'purposes': ['research']},
        )
        withdrawal = self.service.get(withdrawal["id"])

        results = {}

        def do_withdrawal():
            try:
                self.service.transition(
                    Actor("admin", "admin"), withdrawal["id"], 'execute',
                    {'executed_at': '2026-03-02'}, expected_version=withdrawal["version"],
                )
                results["withdrawal"] = "ok"
            except Exception as exc:
                results["withdrawal"] = type(exc).__name__

        def do_loan():
            try:
                self.service.transition(
                    Actor("admin", "admin"), sample["id"], 'loan',
                    {'recipient': 'lab-x', 'purpose': 'research', 'due_at': '2026-12-31'},
                    expected_version=sample["version"],
                )
                results["loan"] = "ok"
            except Exception as exc:
                results["loan"] = type(exc).__name__

        t1 = Thread(target=do_withdrawal)
        t2 = Thread(target=do_loan)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # 两边同时提交，只能有一边成功；另一边因状态已被并发改变而失败
        # （版本冲突 ConflictError 或状态不再允许 InvalidTransition，取决于时序）。
        self.assertEqual(list(results.values()).count("ok"), 1)
        failure = [value for value in results.values() if value != "ok"][0]
        self.assertIn(failure, ("ConflictError", "InvalidTransition"))
        sample = self.service.get(sample["id"])
        self.assertIn(sample["status"], ("on_loan", "pending_disposal"))


if __name__ == "__main__":
    unittest.main()
