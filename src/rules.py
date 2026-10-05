from datetime import date, datetime, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


# 同意按用途登记：样本库目前认可的用途
PURPOSES = {
    "research": "科研用途",
    "genetic_analysis": "遗传分析用途",
}
KNOWN_PURPOSES = frozenset(PURPOSES)

# 样本仍需处置/召回的中间状态
PENDING_DISPOSAL = "pending_disposal"
PENDING_RECALL = "pending_recall"


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def parse_purposes(value, field="purposes"):
    """校验并去重用途列表。"""
    if not isinstance(value, list) or not value:
        raise ValidationError("%s must be a non-empty list" % field)
    result = []
    for item in value:
        if not isinstance(item, str) or item not in KNOWN_PURPOSES:
            raise ValidationError("unknown purpose in %s: %r" % (field, item))
        if item not in result:
            result.append(item)
    return result


def is_active_consent(consent, today=None):
    """同意当前是否有效：状态 active 且未过期。"""
    today = today or date.today()
    if consent["status"] != "active":
        return False
    expires = consent["data"].get("expires_at")
    if expires:
        expires_on = datetime.fromisoformat(str(expires)[:10]).date()
        if expires_on < today:
            return False
    return True


def consent_purposes(consent):
    return [p for p in (consent["data"].get("scope") or []) if p in KNOWN_PURPOSES]


def coverage_map(consents, withdrawn_purposes=(), today=None):
    """计算 用途 -> 覆盖该用途的有效同意ID列表。"""
    today = today or date.today()
    withdrawn = set(withdrawn_purposes)
    coverage = {}
    for consent in consents:
        if not is_active_consent(consent, today):
            continue
        for purpose in consent_purposes(consent):
            if purpose in withdrawn:
                continue
            coverage.setdefault(purpose, []).append(consent["id"])
    return coverage


def participant_withdrawn(participant):
    return set((participant or {}).get("data", {}).get("withdrawn_purposes", []))


def evaluate_sample(sample, consents, *, withdrawn_purposes=(), today=None):
    """
    评估样本在当前同意覆盖下的可用情况。

    - purposes: 入库时登记的全部用途（身份不变）
    - remaining: 参与者尚未撤回的用途
    - covered: 当下仍有有效同意覆盖的用途（样本可继续用于这些用途）
    - uncovered: 没有有效同意覆盖、不能继续使用的用途
    - covered_by: 每个仍被覆盖用途对应的有效同意
    """
    today = today or date.today()
    withdrawn = set(withdrawn_purposes)
    registered = list(sample["data"].get("purposes", []))
    remaining = [p for p in registered if p not in withdrawn]
    coverage = coverage_map(consents, withdrawn, today)
    covered = [p for p in remaining if p in coverage]
    uncovered = [p for p in remaining if p not in coverage]
    return {
        "purposes": registered,
        "remaining": remaining,
        "covered": covered,
        "uncovered": uncovered,
        "covered_by": {p: sorted(coverage[p]) for p in covered},
        "usable": bool(covered),
    }


def decision_for(sample_status, usable):
    """还有用途被覆盖则维持可用状态，否则按是否在借决定处置路径。"""
    if usable:
        return "retain", sample_status
    if sample_status == "on_loan":
        return PENDING_RECALL, PENDING_RECALL
    return PENDING_DISPOSAL, PENDING_DISPOSAL


class RuleEngine:
    ALIASES = {'participants': 'participant', 'consents': 'consent', 'samples': 'sample', 'withdrawals': 'withdrawal'}
    INITIAL_STATUS = {'participant': 'registered', 'consent': 'draft', 'sample': 'collected', 'withdrawal': 'requested'}
    TRANSITIONS = {
        'participant': {
            'close_participant': (('registered',), 'closed'),
        },
        'consent': {
            'activate': (('draft',), 'active'),
            'supersede': (('active',), 'superseded'),
            'withdraw': (('active',), 'withdrawn'),
        },
        'sample': {
            'store': (('collected',), 'stored'),
            'loan': (('stored',), 'on_loan'),
            # return 的目标状态由评估决定：正常归还回 stored；待召回样本视覆盖而定
            'return': (('on_loan', 'pending_recall'), None),
            'anonymize': (('stored', 'pending_disposal'), 'anonymized'),
            'destroy': (('stored', 'pending_disposal'), 'destroyed'),
        },
        'withdrawal': {
            'approve': (('requested',), 'approved'),
            'execute': (('approved',), 'executed'),
        },
    }
    CREATE_REQUIRED = {
        'participant': ('name',),
        'consent': ('participant_id', 'scope'),
        'sample': ('participant_id', 'sample_code', 'collected_at'),
        'withdrawal': ('participant_id', 'requested_at', 'purposes'),
    }
    ACTION_REQUIRED = {
        ('consent', 'activate'): ('scope', 'version', 'expires_at'),
        ('consent', 'supersede'): ('reason',),
        ('consent', 'withdraw'): ('reason',),
        ('sample', 'store'): ('freezer', 'position', 'purposes'),
        ('sample', 'loan'): ('recipient', 'purpose', 'due_at'),
        ('sample', 'anonymize'): ('reason',),
        ('sample', 'destroy'): ('reason',),
        ('withdrawal', 'approve'): ('reason', 'sample_ids'),
        ('withdrawal', 'execute'): ('executed_at',),
    }
    CREATE_ROLES = {
        'participant': ('admin', 'biobank'),
        'consent': ('admin', 'committee'),
        'sample': ('admin', 'biobank'),
        'withdrawal': ('admin', 'biobank'),
    }
    ROLE_ACTIONS = {
        'close_participant': ('admin', 'biobank'),
        'activate': ('admin', 'committee'),
        'supersede': ('admin', 'committee'),
        'withdraw': ('admin', 'committee'),
        'store': ('admin', 'biobank'),
        'loan': ('admin', 'biobank'),
        'return': ('admin', 'biobank'),
        'anonymize': ('admin', 'biobank'),
        'destroy': ('admin', 'biobank'),
        'approve': ('admin', 'committee'),
        'execute': ('admin', 'biobank'),
    }

    def __init__(self, today=None):
        # today 可注入固定日期，便于测试过期/覆盖计算
        self._today = today if isinstance(today, date) else (today or date.today())

    def today(self):
        return self._today

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    # ---- 跨对象查询辅助 -------------------------------------------------

    def _participant(self, lookup, participant_id):
        return _find_one(lookup, "participant", "id", participant_id)

    def _consents(self, lookup, participant_id):
        if participant_id is None or lookup is None:
            return []
        return lookup("consent", "participant_id", participant_id) or []

    def _coverage(self, lookup, participant):
        return coverage_map(
            self._consents(lookup, participant["id"]),
            participant_withdrawn(participant),
            self._today,
        )

    # ---- 创建校验 -------------------------------------------------------

    def _validate_participant_create(self, actor, data, lookup):
        if len(data.get("name", "")) < 2:
            raise ValidationError("participant name is required")
        data.setdefault("withdrawn_purposes", [])

    def _validate_consent_create(self, actor, data, lookup):
        participant = self._participant(lookup, data.get("participant_id"))
        if not participant or participant["status"] == "closed":
            raise ValidationError("consent requires an active participant")
        data["scope"] = parse_purposes(data.get("scope"), "scope")

    def _validate_withdrawal_create(self, actor, data, lookup):
        participant = self._participant(lookup, data.get("participant_id"))
        if not participant or participant["status"] == "closed":
            raise ValidationError("withdrawal requires an active participant")
        data["purposes"] = parse_purposes(data.get("purposes"), "purposes")

    # ---- 动作校验 -------------------------------------------------------

    def _validate_sample_store(self, actor, sample, data, lookup):
        participant = self._participant(lookup, sample["data"].get("participant_id"))
        if not participant:
            raise ValidationError("sample requires a participant")
        purposes = parse_purposes(data.get("purposes"), "purposes")
        coverage = self._coverage(lookup, participant)
        missing = [p for p in purposes if p not in coverage]
        if missing:
            raise ValidationError(
                "no active consent covering purpose: %s" % ", ".join(missing)
            )
        return {
            "patch": {
                "purposes": purposes,
                # 入库时记下每个用途由哪些同意覆盖
                "consent_snapshot": {p: sorted(coverage[p]) for p in purposes},
                "stored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            "audit": {"purposes": purposes, "covered_by": {p: sorted(coverage[p]) for p in purposes}},
        }

    def _validate_sample_loan(self, actor, sample, data, lookup):
        purpose = data.get("purpose")
        if purpose not in KNOWN_PURPOSES:
            raise ValidationError("unknown loan purpose: %r" % (purpose,))
        participant = self._participant(lookup, sample["data"].get("participant_id"))
        evaluation = evaluate_sample(
            sample,
            self._consents(lookup, participant["id"]),
            withdrawn_purposes=participant_withdrawn(participant),
            today=self._today,
        )
        if purpose not in evaluation["covered"]:
            raise ValidationError("loan purpose is not covered by consent: " + purpose)
        return {"audit": {"purpose": purpose, "covered_by": evaluation["covered_by"].get(purpose, [])}}

    def _validate_sample_return(self, actor, sample, data, lookup):
        if sample["status"] == "on_loan":
            return {"next_status": "stored", "audit": {"decision": "returned"}}
        # pending_recall：归还后重新评估剩余用途的同意覆盖
        participant = self._participant(lookup, sample["data"].get("participant_id"))
        evaluation = evaluate_sample(
            sample,
            self._consents(lookup, participant["id"]),
            withdrawn_purposes=participant_withdrawn(participant),
            today=self._today,
        )
        if evaluation["usable"]:
            next_status, decision = "stored", "retained"
        else:
            next_status, decision = PENDING_DISPOSAL, PENDING_DISPOSAL
        return {
            "next_status": next_status,
            "audit": {
                "decision": decision,
                "purposes": evaluation["purposes"],
                "covered": evaluation["covered"],
                "uncovered": evaluation["uncovered"],
                "covered_by": evaluation["covered_by"],
            },
        }

    def _validate_withdrawal_approve(self, actor, withdrawal, data, lookup):
        sample_ids = data.get("sample_ids") or []
        if len(set(sample_ids)) != len(sample_ids):
            raise ConflictError("sample_ids contains duplicates")
        purposes = parse_purposes(withdrawal["data"].get("purposes"), "purposes")
        participant = self._participant(lookup, withdrawal["data"].get("participant_id"))
        if not participant:
            raise ValidationError("withdrawal participant missing")
        withdrawn = participant_withdrawn(participant)
        already = sorted(set(purposes) & withdrawn)
        if already:
            raise ValidationError("purpose already withdrawn: %s" % ", ".join(already))
        snapshots = {}
        for sample_id in sample_ids:
            sample = _find_one(lookup, "sample", "id", sample_id)
            if not sample:
                raise ValidationError("unknown sample: " + str(sample_id))
            if sample["data"].get("participant_id") != participant["id"]:
                raise ValidationError("sample does not belong to participant: " + str(sample_id))
            if sample["status"] not in ("stored", "on_loan"):
                raise ConflictError("sample %s is not available (%s)" % (sample_id, sample["status"]))
            evaluation = evaluate_sample(
                sample,
                self._consents(lookup, participant["id"]),
                withdrawn_purposes=withdrawn,
                today=self._today,
            )
            missing = [p for p in purposes if p not in evaluation["covered"]]
            if missing:
                raise ValidationError(
                    "sample %s has no consent covering: %s" % (sample_id, ", ".join(missing))
                )
            # 记录审批时的样本版本：执行时若样本已变动（例如并发借出）则冲突
            snapshots[sample_id] = sample["version"]
        return {
            "patch": {
                "sample_ids": list(sample_ids),
                "sample_versions": snapshots,
                "purposes": purposes,
                "approved_by": actor.user_id,
            }
        }

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = {
            "participant": self._validate_participant_create,
            "consent": self._validate_consent_create,
            "withdrawal": self._validate_withdrawal_create,
        }.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = {
            ("sample", "store"): self._validate_sample_store,
            ("sample", "loan"): self._validate_sample_loan,
            ("sample", "return"): self._validate_sample_return,
            ("withdrawal", "approve"): self._validate_withdrawal_approve,
        }.get((kind, action))
        result = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        patch.update(result.get("patch", {}))
        next_status = result.get("next_status", next_status)
        if next_status is None:
            raise InvalidTransition("action %s did not resolve a target status" % action)
        audit_extra = result.get("audit", {})
        return next_status, patch, audit_extra
