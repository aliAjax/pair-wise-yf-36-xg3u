from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        if entity["kind"] == "withdrawal" and action == "execute":
            return self._execute_withdrawal(actor, entity, patch, expected)
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def _execute_withdrawal(self, actor, withdrawal, patch, expected_version):
        """按用途定向执行撤回：缩减参与者的同意范围，并据剩余用途重判样本状态。

        样本入库时登记的用途里，未被撤回的用途若仍被有效同意覆盖则继续可用；
        否则在库样本转待处置，借出样本先转待召回、归还后再处置。所有同意变更与
        样本判定在单事务内按乐观版本提交，与样本借出并发时只有一边能成功。
        """
        participant_id = withdrawal["data"]["participant_id"]
        purposes = withdrawal["data"].get("purposes", [])
        sample_ids = withdrawal["data"].get("sample_ids", [])

        # 1. 读取该参与者仍有效的同意，按撤回用途缩减范围。
        consents = [
            consent
            for consent in self._lookup("consent", "participant_id", participant_id)
            if consent["status"] == "active"
        ]
        consent_changes = []
        for consent in consents:
            old_scope = list(consent["data"].get("scope", []))
            new_scope = [purpose for purpose in old_scope if purpose not in purposes]
            consent_changes.append((consent, old_scope, new_scope))

        # 2. 缩减后仍有效的用途并集，用于判定样本是否还有有效同意覆盖。
        covered = set()
        for _, _, new_scope in consent_changes:
            covered.update(new_scope)

        # 3. 逐样本重判（仅在库 / 借出中的样本参与）。
        samples = [self.repository.get_entity(sample_id) for sample_id in sample_ids]
        samples = [sample for sample in samples if sample and sample["kind"] == "sample"]

        updates = []
        audit_entries = []

        # 3a. 同意范围变更（范围为空则同意一并撤回）。
        for consent, old_scope, new_scope in consent_changes:
            consent_data = dict(consent["data"])
            consent_data["scope"] = new_scope
            new_status = "active" if new_scope else "withdrawn"
            updates.append((consent["id"], consent["version"], new_status, consent_data))
            audit_entries.append((
                consent["id"], actor.user_id, actor.role, "withdraw_purposes",
                consent["status"], new_status,
                {"withdrawn_purposes": purposes, "old_scope": old_scope, "new_scope": new_scope},
            ))

        # 3b. 样本状态重判。
        for sample in samples:
            if sample["status"] not in ("stored", "on_loan"):
                continue
            sample_purposes = list(sample["data"].get("purposes", []))
            remaining = [purpose for purpose in sample_purposes if purpose not in purposes]
            still_covered = bool(remaining) and all(purpose in covered for purpose in remaining)

            sample_data = dict(sample["data"])
            sample_data["consent_review"] = {
                "withdrawn_purposes": purposes,
                "remaining_purposes": remaining,
                "covered": still_covered,
            }

            if still_covered:
                new_status = sample["status"]
                reason = "consent still covers remaining purposes"
            elif sample["status"] == "on_loan":
                new_status = "pending_recall"
                reason = "sample on loan; recall before disposal"
            else:
                new_status = "pending_disposal"
                reason = "no valid consent covers remaining purposes"

            updates.append((sample["id"], sample["version"], new_status, sample_data))
            audit_entries.append((
                sample["id"], actor.user_id, actor.role, "consent_review",
                sample["status"], new_status,
                {
                    "withdrawn_purposes": purposes,
                    "remaining_purposes": remaining,
                    "covered": still_covered,
                    "reason": reason,
                    "consent_changes": [
                        {"consent_id": consent["id"], "old_scope": old_scope, "new_scope": new_scope}
                        for consent, old_scope, new_scope in consent_changes
                    ],
                },
            ))

        # 3c. 撤回单本身推进到已执行。
        withdrawal_data = dict(withdrawal["data"])
        withdrawal_data.update(patch)
        updates.append((withdrawal["id"], expected_version, "executed", withdrawal_data))
        audit_entries.append((
            withdrawal["id"], actor.user_id, actor.role, "execute",
            withdrawal["status"], "executed", {"patch": patch},
        ))

        self.repository.apply_withdrawal(updates, audit_entries)
        return self.repository.get_entity(withdrawal["id"])

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
