import threading
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import (
    PENDING_DISPOSAL,
    PENDING_RECALL,
    RuleEngine,
    coverage_map,
    decision_for,
    evaluate_sample,
    participant_withdrawn,
)


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)
        # 进程内写互斥：撤回执行与样本借出同时提交时，只允许一边成功
        self._mutation_lock = threading.RLock()

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        with self._mutation_lock:
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

    # ---- 动作分派 -------------------------------------------------------

    COMPOUND_ACTIONS = {
        ("sample", "return"): "_transition_sample_return",
        ("consent", "withdraw"): "_transition_consent_change",
        ("consent", "supersede"): "_transition_consent_change",
        ("withdrawal", "execute"): "_transition_withdrawal_execute",
    }

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = self.rules.normalize_kind(entity["kind"])
        payload = dict(data or {})
        with self._mutation_lock:
            handler_name = self.COMPOUND_ACTIONS.get((kind, action))
            if handler_name:
                handler = getattr(self, handler_name)
                return handler(actor, entity, action, payload, expected_version)
            return self._transition_simple(actor, entity, action, payload, expected_version)

    def _transition_simple(self, actor, entity, action, data, expected_version):
        """单实体动作：状态更新与审计在同一事务内提交。"""
        next_status, patch, audit_extra = self.rules.validate_transition(
            actor, entity, action, data, self._lookup
        )
        expected = entity["version"] if expected_version is None else int(expected_version)
        merged = dict(entity["data"])
        merged.update(patch)
        with self.repository.unit_of_work() as tx:
            updated = tx.update(entity["id"], expected, next_status, merged)
            tx.audit(
                entity["id"],
                actor.user_id,
                actor.role,
                action,
                entity["status"],
                updated["status"],
                {"patch": patch, **audit_extra},
            )
        return updated

    # ---- 样本归还：待召回样本归还后重新评估 ------------------------------

    def _transition_sample_return(self, actor, sample, action, data, expected_version):
        _, patch, audit_extra = self.rules.validate_transition(
            actor, sample, action, data, self._lookup
        )
        expected = sample["version"] if expected_version is None else int(expected_version)
        with self.repository.unit_of_work() as tx:
            current = tx.get(sample["id"])
            participant = tx.find("participant", "id", current["data"].get("participant_id"))
            consents = tx.find("consent", "participant_id", participant[0]["id"]) if participant else []
            withdrawn = participant_withdrawn(participant[0]) if participant else set()
            if current["status"] == "pending_recall":
                evaluation = evaluate_sample(
                    current, consents, withdrawn_purposes=withdrawn, today=self.rules.today()
                )
                if evaluation["usable"]:
                    next_status, decision = "stored", "retained"
                else:
                    next_status, decision = PENDING_DISPOSAL, PENDING_DISPOSAL
                detail = {
                    "decision": decision,
                    "purposes": evaluation["purposes"],
                    "covered": evaluation["covered"],
                    "uncovered": evaluation["uncovered"],
                    "covered_by": evaluation["covered_by"],
                    "withdrawn_purposes": sorted(withdrawn),
                }
            else:
                next_status, decision = "stored", "returned"
                detail = {"decision": decision}
            merged = dict(current["data"])
            merged.update(patch)
            updated = tx.update(current["id"], expected, next_status, merged)
            tx.audit(
                current["id"],
                actor.user_id,
                actor.role,
                action,
                current["status"],
                updated["status"],
                {"patch": patch, **audit_extra, **detail},
            )
        return updated

    # ---- 同意撤回/版本失效：按用途级联评估样本 ----------------------------

    def _transition_consent_change(self, actor, consent, action, data, expected_version):
        next_status, patch, audit_extra = self.rules.validate_transition(
            actor, consent, action, data, self._lookup
        )
        expected = consent["version"] if expected_version is None else int(expected_version)
        participant_id = consent["data"].get("participant_id")
        affected_outcomes = []
        with self.repository.unit_of_work() as tx:
            current_consent = tx.get(consent["id"])
            participant = tx.find("participant", "id", participant_id)
            withdrawn = participant_withdrawn(participant[0]) if participant else set()
            consents_before = tx.find("consent", "participant_id", participant_id)
            samples = [
                s for s in tx.find("sample", "participant_id", participant_id)
                if s["status"] in ("stored", "on_loan")
            ]
            before_map = {
                s["id"]: evaluate_sample(
                    s, consents_before, withdrawn_purposes=withdrawn, today=self.rules.today()
                )
                for s in samples
            }
            merged = dict(current_consent["data"])
            merged.update(patch)
            updated_consent = tx.update(current_consent["id"], expected, next_status, merged)

            consents_after = [
                updated_consent if c["id"] == consent["id"] else c for c in consents_before
            ]
            for sample in samples:
                before = before_map[sample["id"]]
                after = evaluate_sample(
                    sample, consents_after, withdrawn_purposes=withdrawn, today=self.rules.today()
                )
                lost = [p for p in before["covered"] if p not in after["covered"]]
                if not lost:
                    continue
                decision, target = decision_for(sample["status"], after["usable"])
                effect_detail = {
                    "trigger": "consent_%s" % action,
                    "consent_id": consent["id"],
                    "consent_status": next_status,
                    "purposes_lost": lost,
                    "withdrawn_purposes": sorted(withdrawn),
                    "before": {
                        "covered": before["covered"],
                        "uncovered": before["uncovered"],
                        "covered_by": before["covered_by"],
                    },
                    "after": {
                        "covered": after["covered"],
                        "uncovered": after["uncovered"],
                        "covered_by": after["covered_by"],
                    },
                    "decision": decision,
                }
                if target == sample["status"]:
                    tx.audit(
                        sample["id"], actor.user_id, actor.role,
                        "consent_effect", sample["status"], sample["status"], effect_detail,
                    )
                else:
                    moved = tx.update(sample["id"], sample["version"], target, sample["data"])
                    tx.audit(
                        sample["id"], actor.user_id, actor.role,
                        "consent_effect", sample["status"], moved["status"], effect_detail,
                    )
                affected_outcomes.append({"sample_id": sample["id"], "decision": decision, "purposes_lost": lost})
            tx.audit(
                consent["id"],
                actor.user_id,
                actor.role,
                action,
                consent["status"],
                updated_consent["status"],
                {"patch": patch, **audit_extra, "affected_samples": affected_outcomes},
            )
        return updated_consent

    # ---- 撤回执行：按用途定向，只停用未被覆盖的用途 ------------------------

    def _transition_withdrawal_execute(self, actor, withdrawal, action, data, expected_version):
        next_status, patch, audit_extra = self.rules.validate_transition(
            actor, withdrawal, action, data, self._lookup
        )
        expected = withdrawal["version"] if expected_version is None else int(expected_version)
        purposes = withdrawal["data"].get("purposes")
        sample_ids = withdrawal["data"].get("sample_ids", [])
        snapshots = withdrawal["data"].get("sample_versions", {})
        participant_id = withdrawal["data"].get("participant_id")
        outcomes = {}
        with self.repository.unit_of_work() as tx:
            current = tx.get(withdrawal["id"])
            participant = tx.find("participant", "id", participant_id)
            if not participant:
                raise NotFoundError("participant not found: " + str(participant_id))
            participant = participant[0]
            withdrawn_before = participant_withdrawn(participant)
            added = [p for p in purposes if p not in withdrawn_before]
            withdrawn_after = sorted(set(withdrawn_before) | set(purposes))
            consents = tx.find("consent", "participant_id", participant_id)

            sample_updates = []
            for sample_id in sample_ids:
                sample = tx.get(sample_id)
                if sample is None:
                    raise NotFoundError("sample not found: " + sample_id)
                # 审批后样本若被借出/归还/处置（版本变化），撤回执行失败，两边只成一边
                approved_version = snapshots.get(sample_id)
                if approved_version is not None and sample["version"] != int(approved_version):
                    raise ConflictError(
                        "sample %s changed after approval (loan/return/disposal); "
                        "withdrawal execution rejected" % sample_id
                    )
                if sample["status"] not in ("stored", "on_loan"):
                    raise ConflictError(
                        "sample %s is not available for withdrawal (%s)"
                        % (sample_id, sample["status"])
                    )
                evaluation = evaluate_sample(
                    sample,
                    consents,
                    withdrawn_purposes=withdrawn_after,
                    today=self.rules.today(),
                )
                lost = [p for p in purposes if p in sample["data"].get("purposes", [])]
                decision, target = decision_for(sample["status"], evaluation["usable"])
                sample_updates.append((sample, target, decision, evaluation, lost))

            # 全部校验通过后统一落库
            for sample, target, decision, evaluation, lost in sample_updates:
                effect_detail = {
                    "trigger": "withdrawal_execute",
                    "withdrawal_id": withdrawal["id"],
                    "purposes_requested": list(purposes),
                    "purposes_lost": lost,
                    "withdrawn_purposes_before": sorted(withdrawn_before),
                    "withdrawn_purposes_after": withdrawn_after,
                    "registered_purposes": evaluation["purposes"],
                    "remaining": evaluation["remaining"],
                    "covered": evaluation["covered"],
                    "uncovered": evaluation["uncovered"],
                    "covered_by": evaluation["covered_by"],
                    "decision": decision,
                }
                # 无论样本是否改状态都推进版本：使并发借出（乐观锁）必定失败，
                # 保证撤回执行与样本借出同时提交时只有一边成功。
                sample_data = dict(sample["data"])
                sample_data["last_effect"] = {
                    "withdrawal_id": withdrawal["id"],
                    "decision": decision,
                    "purposes_lost": lost,
                }
                moved = tx.update(sample["id"], sample["version"], target, sample_data)
                tx.audit(
                    sample["id"], actor.user_id, actor.role,
                    "withdrawal_effect", sample["status"], moved["status"], effect_detail,
                )
                outcomes[sample["id"]] = {
                    "decision": decision,
                    "from_status": sample["status"],
                    "to_status": target,
                    "lost_purposes": lost,
                    "covered": evaluation["covered"],
                }

            participant_data = dict(participant["data"])
            participant_data["withdrawn_purposes"] = withdrawn_after
            updated_participant = tx.update(
                participant["id"], participant["version"], participant["status"], participant_data
            )
            tx.audit(
                participant["id"], actor.user_id, actor.role,
                "purposes_withdrawn", participant["status"], updated_participant["status"],
                {
                    "withdrawal_id": withdrawal["id"],
                    "added_purposes": added,
                    "withdrawn_purposes": withdrawn_after,
                },
            )

            merged = dict(current["data"])
            merged.update(patch)
            merged["sample_outcomes"] = outcomes
            updated = tx.update(withdrawal["id"], expected, next_status, merged)
            tx.audit(
                withdrawal["id"],
                actor.user_id,
                actor.role,
                action,
                withdrawal["status"],
                updated["status"],
                {"patch": patch, **audit_extra, "purposes": list(purposes), "outcomes": outcomes},
            )
        return updated

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
