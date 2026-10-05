import tempfile
import unittest
from pathlib import Path

from src.domain import Actor
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _resolve(value, created):
    if isinstance(value, str):
        for key, item in created.items():
            value = value.replace("{" + key + "}", str(item))
        return value
    if isinstance(value, list):
        return [_resolve(item, created) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, created) for key, item in value.items()}
    return value


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _setup_consent(self, scope):
        participant = self.service.create(self.actor, "participant", {"name": "Participant"})
        consent = self.service.create(
            self.actor, "consent",
            {"participant_id": participant["id"], "scope": scope},
        )
        self.service.transition(
            self.actor, consent["id"], "activate",
            {"scope": scope, "version": "v1", "expires_at": "2099-01-01"},
        )
        return participant, consent

    def _store_sample(self, participant, consent, code, purposes):
        sample = self.service.create(
            self.actor, "sample",
            {"participant_id": participant["id"], "sample_code": code, "collected_at": "2026-01-01"},
        )
        self.service.transition(
            self.actor, sample["id"], "store",
            {"freezer": "F1", "position": "A1", "consent_id": consent["id"], "purposes": purposes},
        )
        return sample

    def test_full_workflow(self):
        created = {}
        steps = [{'op': 'create', 'as': 'participant', 'kind': 'participant', 'data': {'name': 'Participant One'}}, {'op': 'create', 'as': 'consent', 'kind': 'consent', 'data': {'participant_id': '{participant}', 'scope': ['research']}}, {'op': 'transition', 'target': 'consent', 'action': 'activate', 'data': {'scope': ['research'], 'version': 'v1', 'expires_at': '2099-01-01'}, 'expect': 'active'}, {'op': 'create', 'as': 'sample', 'kind': 'sample', 'data': {'participant_id': '{participant}', 'sample_code': 'B-001', 'collected_at': '2026-01-01'}}, {'op': 'transition', 'target': 'sample', 'action': 'store', 'data': {'freezer': 'F1', 'position': 'A1', 'consent_id': '{consent}', 'purposes': ['research']}, 'expect': 'stored'}, {'op': 'create', 'as': 'withdrawal', 'kind': 'withdrawal', 'data': {'participant_id': '{participant}', 'requested_at': '2026-03-01'}}, {'op': 'transition', 'target': 'withdrawal', 'action': 'approve', 'data': {'reason': 'participant request', 'sample_ids': ['{sample}'], 'purposes': ['research']}, 'expect': 'approved'}, {'op': 'transition', 'target': 'withdrawal', 'action': 'execute', 'data': {'executed_at': '2026-03-02'}, 'expect': 'executed'}]
        for step in steps:
            if step["op"] == "create":
                entity = self.service.create(
                    self.actor,
                    step["kind"],
                    _resolve(step.get("data", {}), created),
                    step.get("idempotency_key"),
                )
                created[step["as"]] = entity["id"]
            else:
                entity = self.service.transition(
                    self.actor,
                    created[step["target"]],
                    step["action"],
                    _resolve(step.get("data", {}), created),
                    step.get("expected_version"),
                )
            if "expect" in step:
                self.assertEqual(entity["status"], step["expect"])

    def test_withdrawal_by_purpose_keeps_covered_samples(self):
        # 参与者只想停掉科研用途：科研样本转待处置，遗传分析样本仍被同意覆盖、继续可用。
        participant, consent = self._setup_consent(["research", "genetic_analysis"])
        research = self._store_sample(participant, consent, "B-001", ["research"])
        genetic = self._store_sample(participant, consent, "B-002", ["genetic_analysis"])

        withdrawal = self.service.create(
            self.actor, "withdrawal",
            {"participant_id": participant["id"], "requested_at": "2026-03-01"},
        )
        self.service.transition(
            self.actor, withdrawal["id"], "approve",
            {"reason": "participant request", "sample_ids": [research["id"], genetic["id"]],
             "purposes": ["research"]},
        )
        self.service.transition(
            self.actor, withdrawal["id"], "execute", {"executed_at": "2026-03-02"}
        )

        research = self.service.get(research["id"])
        genetic = self.service.get(genetic["id"])
        consent = self.service.get(consent["id"])
        self.assertEqual(research["status"], "pending_disposal")
        self.assertEqual(genetic["status"], "stored")
        self.assertEqual(consent["status"], "active")
        self.assertEqual(consent["data"]["scope"], ["genetic_analysis"])

    def test_withdrawn_consent_marks_sample_pending_disposal(self):
        participant, consent = self._setup_consent(["research"])
        sample = self._store_sample(participant, consent, "B-001", ["research"])

        withdrawal = self.service.create(
            self.actor, "withdrawal",
            {"participant_id": participant["id"], "requested_at": "2026-03-01"},
        )
        self.service.transition(
            self.actor, withdrawal["id"], "approve",
            {"reason": "participant request", "sample_ids": [sample["id"]], "purposes": ["research"]},
        )
        self.service.transition(
            self.actor, withdrawal["id"], "execute", {"executed_at": "2026-03-02"}
        )

        sample = self.service.get(sample["id"])
        consent = self.service.get(consent["id"])
        self.assertEqual(sample["status"], "pending_disposal")
        self.assertEqual(consent["status"], "withdrawn")
        self.assertEqual(consent["data"]["scope"], [])

    def test_on_loan_sample_recalled_then_disposed(self):
        participant, consent = self._setup_consent(["research"])
        sample = self._store_sample(participant, consent, "B-001", ["research"])
        self.service.transition(
            self.actor, sample["id"], "loan",
            {"recipient": "lab-x", "purpose": "research", "due_at": "2026-12-31"},
        )

        withdrawal = self.service.create(
            self.actor, "withdrawal",
            {"participant_id": participant["id"], "requested_at": "2026-03-01"},
        )
        self.service.transition(
            self.actor, withdrawal["id"], "approve",
            {"reason": "participant request", "sample_ids": [sample["id"]], "purposes": ["research"]},
        )
        self.service.transition(
            self.actor, withdrawal["id"], "execute", {"executed_at": "2026-03-02"}
        )
        sample = self.service.get(sample["id"])
        self.assertEqual(sample["status"], "pending_recall")

        # 归还后再处置。
        self.service.transition(self.actor, sample["id"], "return", {})
        sample = self.service.get(sample["id"])
        self.assertEqual(sample["status"], "pending_disposal")

        self.service.transition(self.actor, sample["id"], "destroy", {"reason": "disposed"})
        sample = self.service.get(sample["id"])
        self.assertEqual(sample["status"], "destroyed")

    def test_audit_shows_purposes_and_consent_changes(self):
        participant, consent = self._setup_consent(["research", "genetic_analysis"])
        sample = self._store_sample(participant, consent, "B-001", ["research"])

        withdrawal = self.service.create(
            self.actor, "withdrawal",
            {"participant_id": participant["id"], "requested_at": "2026-03-01"},
        )
        self.service.transition(
            self.actor, withdrawal["id"], "approve",
            {"reason": "participant request", "sample_ids": [sample["id"]], "purposes": ["research"]},
        )
        self.service.transition(
            self.actor, withdrawal["id"], "execute", {"executed_at": "2026-03-02"}
        )

        review = [
            entry for entry in self.service.audit_log(entity_id=sample["id"])
            if entry["action"] == "consent_review"
        ]
        self.assertEqual(len(review), 1)
        detail = review[0]["detail"]
        self.assertEqual(detail["withdrawn_purposes"], ["research"])
        self.assertEqual(detail["remaining_purposes"], [])
        self.assertFalse(detail["covered"])
        self.assertEqual(review[0]["to_status"], "pending_disposal")

        consent_entries = [
            entry for entry in self.service.audit_log(entity_id=consent["id"])
            if entry["action"] == "withdraw_purposes"
        ]
        self.assertEqual(len(consent_entries), 1)
        self.assertEqual(consent_entries[0]["detail"]["old_scope"], ["research", "genetic_analysis"])
        self.assertEqual(consent_entries[0]["detail"]["new_scope"], ["genetic_analysis"])


if __name__ == "__main__":
    unittest.main()
