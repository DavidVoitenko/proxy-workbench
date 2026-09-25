"""Named check profiles: target rules, required/optional sets, revisions, safe interchange.

A profile is the user's saved decision about *what is measured* and *what counts
as success*: which targets are selected, which of them are mandatory, how the
optional set is aggregated (``all`` / ``any`` / ``at_least(K)`` / ``none``), the
per-target thresholds and the probe budget.  Probe execution belongs to
``probes.py``; this module owns no network, no clock and no global state, so the
GUI, the CLI and the API decide pass/fail through the very same functions
(F05: "GUI/CLI/API одинаково исполняют профиль").

    spec = ProfileSpec.create(targets=[...], optional_rule='at_least', k=1)
    verdict = evaluate(spec, evidence)          # pure decision
    store = open_store(path)                    # named profiles, revisions
    ref = store.create('API проекта', spec)
    payload = export_profile(store, ref)       # secret-free document

Model taken from ``docs/requirements/product-research/01-product-model.ru.md``
("Правила принятия результатов") and from F05 of MASTER-PROMPT:

* the mandatory set ``M`` is always mandatory - no rule can drop it;
* the optional set ``O`` carries an explicit ``all``/``any``/``at_least(K)``/``none``
  rule; an empty ``O`` is allowed only under the explicit ``none`` policy;
* a pass always rests on at least one successful *measured* probe.  ``all`` over an
  empty set, ``K = 0`` and a fully disabled target set never pass;
* ``unknown`` is not ``fail`` and is not success either;
* unprobed checks are ``skipped``, never invented measurements.

Three invariants the rest of the workbench may rely on:

1. Evidence carries the thresholds it was measured under.  Evidence from another
   threshold set is not evidence for this revision, so lowering a threshold can
   never buy a pass for measurements that were never run under it (F05).
2. ``RunState.stop()`` only reports a stop when *no* completion of the remaining
   probes could pass, so fail-fast can never disagree with the rule (F05).
3. A revision row is immutable.  ``update`` appends a row and links it with
   ``parent_id``; the previous revision and its results stay readable
   (CONTRACTS §3.3, migration 9).

Storage is the ``profiles`` table of the workbench database in the shape declared
by CONTRACTS §3.3 (migration 9): one row per revision, ``name`` groups the
revisions, ``parent_id`` chains them, ``digest`` fixes the content.  The row key
is ``<profile_id>@<revision>`` because the declared primary key is the single
column ``id``; ``ProfileRef`` is what the rest of the code should pass around.
This module writes no DDL: the table is the one ``db.migrate()`` produced, and
``ProfileStore`` refuses a connection whose ``profiles`` table is not at that shape
instead of repairing it.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from . import db

__all__ = [
    'Budget', 'OptionalRule', 'ProfileError', 'ProfileRecord', 'ProfileRef',
    'ProfileSpec', 'ProfileStore', 'RunState', 'StopDecision', 'TargetAssessment',
    'TargetDecision', 'TargetEvidence', 'TargetRule',
    'BUDGET_MIN_PROBES', 'CONFIG_VERSION', 'EXPORT_KIND', 'EXPORT_VERSION', 'KINDS',
    'MAX_ATTEMPTS', 'MAX_DESCRIPTION', 'MAX_NAME', 'MAX_TARGETS', 'OPTIONAL_MODES',
    'CONTRACT_COLUMNS', 'MIGRATION', 'PROBE_STATES', 'REASON_CODES',
    'assert_secret_free', 'clean_name', 'create_spec', 'evaluate',
    'export_profile', 'from_legacy_config', 'import_profile', 'open_store',
    'parse_row_id', 'plan', 'revision_key', 'run_request', 'target_fingerprint',
    'verify_schema',
]

CONFIG_VERSION = 1
EXPORT_KIND = 'proxy-workbench/profile'
EXPORT_VERSION = 1

KINDS = ('required', 'optional')
OPTIONAL_MODES = ('none', 'all', 'any', 'at_least')

# State of a single target under a revision.  ``unmeasured`` is ours: evidence
# exists but is not admissible for this revision (other thresholds, or none).
PROBE_STATES = ('pass', 'fail', 'unknown', 'skipped', 'unmeasured')

MAX_ATTEMPTS = 10
MAX_NAME = 80
MAX_DESCRIPTION = 500
MAX_TARGETS = 64
BUDGET_MIN_PROBES = 1

_ID_RE = re.compile(r'^[a-z0-9][a-z0-9._:-]{0,63}$')
_CONTROL_RE = re.compile(r'[\x00-\x1f\x7f]')
# A profile id is interpolated into a GLOB pattern, so it may not carry a
# wildcard: ``_`` is literal in GLOB, ``*``, ``?`` and ``[`` are not.
_PROFILE_ID_RE = re.compile(r'^[A-Za-z0-9_.:@-]{1,128}$')

# Error codes.  The first five are shared with CONTRACTS §5.4 and core.py; the
# rest are proposed there (docs/integration/HANDOFF/profiles.md).
E_VALIDATION_SCHEMA = 'E_VALIDATION_SCHEMA'
E_VALIDATION_FIELD = 'E_VALIDATION_FIELD'
E_VALIDATION_UNKNOWN_FIELD = 'E_VALIDATION_UNKNOWN_FIELD'
E_CONFLICT_REVISION = 'E_CONFLICT_REVISION'
E_CONFLICT_NAME = 'E_CONFLICT_NAME'
E_CONFLICT_ARCHIVED = 'E_CONFLICT_ARCHIVED'
E_STATE_PROFILE_UNKNOWN = 'E_STATE_PROFILE_UNKNOWN'
E_SECRET_IN_PROFILE = 'E_SECRET_IN_PROFILE'
E_IMPORT_FORMAT = 'E_IMPORT_FORMAT'
E_IMPORT_REVISION = 'E_IMPORT_REVISION'
E_DATA_DB_FOREIGN = 'E_DATA_DB_FOREIGN'
E_LIMIT_BUDGET = 'E_LIMIT_BUDGET'

# Per-target outcome reasons and whole-verdict reasons.  Machine readable: the
# text belongs to i18n, so this table explains the code in English only.
REASON_CODES = {
    'E_TARGET_DISABLED': 'target is not selected for this run',
    'E_TARGET_STALE_EVIDENCE': 'evidence was measured under other thresholds',
    'E_TARGET_UNKNOWN': 'measurement is unknown, not failed and not passed',
    'E_TARGET_SKIPPED': 'target was not measured, fail-fast or budget cut it',
    'E_TARGET_BELOW_MIN_SUCCESS': 'successful probes below the target threshold',
    'E_TARGET_LATENCY_MISSING': 'no successful probe, so latency is unknown',
    'E_TARGET_LATENCY_EXCEEDED': 'latency above the target threshold',
    'E_VERDICT_RULE_SATISFIED': 'every required and optional condition is met',
    'E_VERDICT_REQUIRED_NOT_PASSED': 'a mandatory target is not passed',
    'E_VERDICT_OPTIONAL_NOT_PASSED': 'the optional rule is not satisfied',
    'E_VERDICT_NO_EFFECTIVE_PROBE': 'no successful measured probe at all',
    'E_VERDICT_EMPTY_TARGET_SET': 'no target is selected, nothing can pass',
    'E_VERDICT_EMPTY_OPTIONAL_SET': 'the optional rule needs an optional target',
    'E_VERDICT_K_NOT_POSITIVE': 'at_least needs K >= 1',
    'E_VERDICT_K_UNREACHABLE': 'K is larger than the optional set',
    'E_LIMIT_BUDGET': 'probe or time budget of the profile is exhausted',
}

# Names that may never appear in a stored or exported profile document.  The
# export is assembled from typed fields, so a secret can only arrive through a
# hand-written config; the check makes that an explicit refusal.
_SECRET_KEYS = frozenset({
    'password', 'passwd', 'secret', 'token', 'api_key', 'apikey', 'authorization',
    'auth', 'credential', 'credentials', 'cookie', 'session', 'bearer', 'private_key',
})
_SECRET_PREFIXES = ('basic ', 'bearer ')


class ProfileError(ValueError):
    """Invalid profile input or an unusable store; carries a canonical code."""

    def __init__(self, code: str, message: str = ''):
        super().__init__(message or code)
        self.code = code


def _fail(code: str, message: str) -> ProfileError:
    return ProfileError(code, message)


def _clean_text(value: Any) -> str:
    """A single-line string, or a validation error.  Booleans are not text here."""
    if not isinstance(value, str):
        raise _fail(E_VALIDATION_FIELD, f'ожидается строка, получено {value!r}')
    text = value.strip()
    if _CONTROL_RE.search(text):
        raise _fail(E_VALIDATION_FIELD, 'значение содержит управляющие символы')
    return text


def _number(value: Any, *, minimum: float | None = None, maximum: float | None = None,
            code: str = E_VALIDATION_FIELD) -> float:
    """A finite float inside the closed range; booleans are rejected."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(code, f'ожидается число, получено {value!r}')
    number = float(value)
    if not math.isfinite(number):
        raise _fail(code, 'ожидается конечное число')
    if minimum is not None and number < minimum:
        raise _fail(code, f'значение меньше минимума {minimum}')
    if maximum is not None and number > maximum:
        raise _fail(code, f'значение больше максимума {maximum}')
    return number


def _integer(value: Any, *, minimum: int | None = None, maximum: int | None = None,
             code: str = E_VALIDATION_FIELD) -> int:
    """A whole number; a float with no fractional part is accepted."""
    if isinstance(value, bool):
        raise _fail(code, f'ожидается целое число, получено {value!r}')
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int):
        raise _fail(code, f'ожидается целое число, получено {value!r}')
    if minimum is not None and value < minimum:
        raise _fail(code, f'значение меньше минимума {minimum}')
    if maximum is not None and value > maximum:
        raise _fail(code, f'значение больше максимума {maximum}')
    return int(value)


def _mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail(E_VALIDATION_SCHEMA, f'{what}: ожидается объект')
    return value


def _reject_unknown(data: Mapping[str, Any], allowed: Iterable[str], what: str) -> None:
    """An unrecognised field is a caller bug and is refused, not ignored (F07)."""
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise _fail(E_VALIDATION_UNKNOWN_FIELD, f'{what}: неизвестные поля {unknown}')


# --------------------------------------------------------------------------- #
# Configuration objects
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class TargetRule:
    """One selected target and the thresholds that make it pass."""

    id: str
    kind: str = 'required'
    enabled: bool = True
    min_success: float = 1.0
    max_latency_ms: float | None = None

    def __post_init__(self) -> None:
        name = _clean_text(self.id)
        if not _ID_RE.fullmatch(name):
            raise _fail(E_VALIDATION_FIELD, f'target.id: некорректный идентификатор {self.id!r}')
        if self.kind not in KINDS:
            raise _fail(E_VALIDATION_FIELD, f'target.kind: ожидается {KINDS}, получено {self.kind!r}')
        if not isinstance(self.enabled, bool):
            raise _fail(E_VALIDATION_FIELD, 'target.enabled: ожидается true/false')
        threshold = _number(self.min_success, minimum=0.0, maximum=1.0)
        if threshold <= 0:
            raise _fail(E_VALIDATION_FIELD, 'target.min_success: ожидается (0, 1]')
        if self.max_latency_ms is not None:
            _number(self.max_latency_ms, minimum=0.0)
        object.__setattr__(self, 'id', name)
        object.__setattr__(self, 'min_success', threshold)
        object.__setattr__(self, 'max_latency_ms',
                           None if self.max_latency_ms is None else float(self.max_latency_ms))

    @classmethod
    def parse(cls, value: Any) -> 'TargetRule':
        """Accept a mapping (a GUI/CLI/API payload) or an already typed rule."""
        if isinstance(value, cls):
            return value
        data = _mapping(value, 'target')
        _reject_unknown(data, ('id', 'kind', 'enabled', 'min_success', 'max_latency_ms'), 'target')
        return cls(data.get('id'), data.get('kind', 'required'), data.get('enabled', True),
                   data.get('min_success', 1.0), data.get('max_latency_ms'))

    def as_dict(self) -> dict[str, Any]:
        return {'id': self.id, 'kind': self.kind, 'enabled': self.enabled,
                'min_success': self.min_success, 'max_latency_ms': self.max_latency_ms}

    def fingerprint(self) -> str:
        """Identity of the *thresholds* this target is judged by.

        Only the thresholds are hashed, not the kind and not the rule: moving a
        target between the mandatory and the optional set changes how evidence is
        combined, not whether the measurement itself is admissible.
        """
        payload = json.dumps({'min_success': self.min_success, 'max_latency_ms': self.max_latency_ms},
                             sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def target_fingerprint(target: Any) -> str:
    """Fingerprint of a target's thresholds, from a rule or a mapping."""
    return TargetRule.parse(target).fingerprint()


@dataclass(frozen=True)
class OptionalRule:
    """How the optional set O is aggregated.  ``k`` matters for ``at_least``."""

    mode: str = 'none'
    k: int = 1

    def __post_init__(self) -> None:
        if self.mode not in OPTIONAL_MODES:
            raise _fail(E_VALIDATION_FIELD,
                        f'optional_rule: ожидается {OPTIONAL_MODES}, получено {self.mode!r}')
        # K is only an integer here.  ``K <= 0`` is refused where the decision is
        # made - ``ProfileSpec.validate`` on the way in, ``evaluate`` on a document
        # a foreign writer left behind - so the guarantee is visible as a verdict
        # and not only as a parse error (F05).
        object.__setattr__(self, 'k', _integer(self.k))

    @classmethod
    def parse(cls, value: Any = None, k: Any = None) -> 'OptionalRule':
        """Accept ``'at_least'``, ``{'mode': 'at_least', 'k': 2}`` or a rule."""
        if isinstance(value, cls) and k is None:
            return value
        if value is None:
            mode, embedded = 'none', k
        elif isinstance(value, str):
            mode, embedded = value, k
        else:
            data = _mapping(value, 'optional_rule')
            _reject_unknown(data, ('mode', 'k'), 'optional_rule')
            mode = data.get('mode', 'none')
            embedded = data.get('k', 1 if k is None else k)
        return cls(mode, 1 if embedded is None else embedded)

    def need(self, optional_size: int) -> int | None:
        """How many optional targets must pass; ``None`` means "all of them"."""
        if self.mode == 'none':
            return 0
        if self.mode == 'all':
            return None
        if self.mode == 'any':
            return 1
        return max(self.k, 0)

    def as_dict(self) -> dict[str, Any]:
        return {'mode': self.mode, 'k': self.k if self.mode == 'at_least' else None}


@dataclass(frozen=True)
class Budget:
    """The probe budget of one run.  ``None`` means "not limited"."""

    max_probes: int | None = None
    max_duration_s: float | None = None

    def __post_init__(self) -> None:
        if self.max_probes is not None:
            object.__setattr__(self, 'max_probes', _integer(self.max_probes, minimum=BUDGET_MIN_PROBES))
        if self.max_duration_s is not None:
            duration = _number(self.max_duration_s, minimum=0.0)
            if duration <= 0:
                raise _fail(E_VALIDATION_FIELD, 'budget.max_duration_s: ожидается положительное число')
            object.__setattr__(self, 'max_duration_s', duration)

    @classmethod
    def parse(cls, value: Any) -> 'Budget':
        if isinstance(value, cls):
            return value
        if value is None:
            return cls()
        data = _mapping(value, 'budget')
        _reject_unknown(data, ('max_probes', 'max_duration_s'), 'budget')
        return cls(data.get('max_probes'), data.get('max_duration_s'))

    def as_dict(self) -> dict[str, Any]:
        return {'max_probes': self.max_probes, 'max_duration_s': self.max_duration_s}


@dataclass(frozen=True)
class ProfileSpec:
    """The full, self-contained decision of one profile revision."""

    targets: tuple[TargetRule, ...] = ()
    optional_rule: OptionalRule = OptionalRule()
    budget: Budget = Budget()
    attempts: int = 1
    description: str = ''
    version: int = CONFIG_VERSION

    def __post_init__(self) -> None:
        if isinstance(self.targets, (str, bytes)) or not isinstance(self.targets, Iterable):
            raise _fail(E_VALIDATION_SCHEMA, 'targets: ожидается список целей')
        rules = tuple(TargetRule.parse(item) for item in self.targets)
        if len(rules) > MAX_TARGETS:
            raise _fail(E_VALIDATION_FIELD, f'targets: больше {MAX_TARGETS} целей')
        seen = {target.id for target in rules}
        if len(seen) != len(rules):
            raise _fail(E_VALIDATION_FIELD, 'target.id: повторяющиеся идентификаторы')
        object.__setattr__(self, 'targets', rules)
        if not isinstance(self.optional_rule, OptionalRule):
            object.__setattr__(self, 'optional_rule', OptionalRule.parse(self.optional_rule))
        if not isinstance(self.budget, Budget):
            object.__setattr__(self, 'budget', Budget.parse(self.budget))
        object.__setattr__(self, 'attempts',
                           _integer(self.attempts, minimum=1, maximum=MAX_ATTEMPTS))
        if not isinstance(self.description, str):
            raise _fail(E_VALIDATION_FIELD, 'description: ожидается строка')
        if _CONTROL_RE.search(self.description):
            raise _fail(E_VALIDATION_FIELD, 'description: управляющие символы запрещены')
        if len(self.description) > MAX_DESCRIPTION:
            raise _fail(E_VALIDATION_FIELD, f'description: длиннее {MAX_DESCRIPTION} символов')
        object.__setattr__(self, 'description', self.description.strip())
        object.__setattr__(self, 'version', _integer(self.version, minimum=1, code=E_IMPORT_REVISION))

    # -- construction ------------------------------------------------------ #

    @classmethod
    def create(cls, *, targets: Iterable[Any], optional_rule: Any = None, k: Any = None,
               budget: Any = None, attempts: Any = 1, description: Any = '') -> 'ProfileSpec':
        """Build and fully validate a specification."""
        spec = cls(targets, OptionalRule.parse(optional_rule, k), Budget.parse(budget),
                   attempts, '' if description is None else description)
        return spec.validate()

    @classmethod
    def from_dict(cls, payload: Any, *, validate: bool = True) -> 'ProfileSpec':
        """Parse a stored or imported document.

        ``validate=False`` skips the cross-field checks.  It exists for reading a
        row written by an older or foreign writer: such a specification must not
        crash the scanner, and evaluating it can only produce a *fail* (see
        ``evaluate``), never a pass.
        """
        data = _mapping(payload, 'profile')
        _reject_unknown(data, ('version', 'targets', 'optional_rule', 'k', 'budget',
                               'attempts', 'description'), 'profile')
        version = data.get('version', CONFIG_VERSION)
        if isinstance(version, bool) or not isinstance(version, int) or version > CONFIG_VERSION:
            raise _fail(E_IMPORT_REVISION, f'profile.version: поддерживается до {CONFIG_VERSION}')
        targets = data.get('targets', ())
        if isinstance(targets, (str, bytes)) or not isinstance(targets, Sequence):
            raise _fail(E_VALIDATION_SCHEMA, 'profile.targets: ожидается список')
        spec = cls(targets, OptionalRule.parse(data.get('optional_rule'), data.get('k')),
                   Budget.parse(data.get('budget')), data.get('attempts', 1),
                   data.get('description') or '', version)
        return spec.validate() if validate else spec

    @classmethod
    def from_json(cls, text: str, *, validate: bool = True) -> 'ProfileSpec':
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            raise _fail(E_VALIDATION_SCHEMA, 'profile: ожидается JSON') from None
        return cls.from_dict(payload, validate=validate)

    def copy_of(self, **changes: Any) -> 'ProfileSpec':
        """A new specification with the given fields replaced, still validated."""
        base = self.as_dict()
        base.update(changes)
        return ProfileSpec.from_dict(base)

    # -- cross-field validation -------------------------------------------- #

    def validate(self) -> 'ProfileSpec':
        """Reject every configuration that could never produce a pass."""
        enabled = self.enabled_targets
        if not enabled:
            raise _fail(E_VALIDATION_FIELD,
                        'включите хотя бы одну проверку: пустой набор целей не даёт pass')
        if not self.required_targets:
            raise _fail(E_VALIDATION_FIELD,
                        'обязательные проверки обязательны: обязательный набор не может быть пуст')
        if self.optional_rule.mode == 'at_least' and self.optional_rule.k < 1:
            raise _fail(E_VALIDATION_FIELD, 'optional_rule: at_least требует K >= 1')
        if self.optional_rule.mode != 'none' and not self.optional_targets:
            raise _fail(E_VALIDATION_FIELD,
                        'правило optional требует непустой набор дополнительных проверок; '
                        'пустой набор допустим только при политике none')
        need = self.optional_rule.need(len(self.optional_targets))
        if need is not None and need > len(self.optional_targets):
            raise _fail(E_VALIDATION_FIELD,
                        f'optional_rule: K={need} больше числа дополнительных проверок '
                        f'({len(self.optional_targets)})')
        if self.budget.max_probes is not None and self.budget.max_probes < self.min_probes:
            raise _fail(E_VALIDATION_FIELD,
                        f'budget.max_probes={self.budget.max_probes} меньше минимума '
                        f'{self.min_probes}, который нужен хотя бы для одного pass')
        return self

    # -- derived views ------------------------------------------------------ #

    @property
    def enabled_targets(self) -> tuple[TargetRule, ...]:
        return tuple(target for target in self.targets if target.enabled)

    @property
    def required_targets(self) -> tuple[TargetRule, ...]:
        return tuple(t for t in self.enabled_targets if t.kind == 'required')

    @property
    def optional_targets(self) -> tuple[TargetRule, ...]:
        return tuple(t for t in self.enabled_targets if t.kind == 'optional')

    def target(self, target_id: str) -> TargetRule:
        for item in self.targets:
            if item.id == target_id:
                return item
        raise _fail(E_VALIDATION_FIELD, f'цель {target_id!r} отсутствует в профиле')

    @property
    def min_probes(self) -> int:
        """Cheapest number of probes that can still produce a pass."""
        need = self.optional_rule.need(len(self.optional_targets))
        if need is None:
            need = len(self.optional_targets)
        return self.attempts * (len(self.required_targets) + need)

    @property
    def max_probes(self) -> int | None:
        if self.budget.max_probes is None:
            return None
        return max(self.budget.max_probes, self.min_probes)

    def as_dict(self) -> dict[str, Any]:
        return {'version': self.version, 'attempts': self.attempts,
                'description': self.description,
                'optional_rule': self.optional_rule.as_dict(),
                'budget': self.budget.as_dict(),
                'targets': [target.as_dict() for target in self.targets]}

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, ensure_ascii=False, separators=(',', ':'))

    @property
    def digest(self) -> str:
        """Content identity of the revision; stable across surfaces and platforms.

        The target order is part of it: reordering the checks is a real change to
        the probe order, and it gets its own revision instead of being dropped as a
        no-op edit.
        """
        return hashlib.sha256(self.to_json().encode()).hexdigest()

    def plan(self) -> dict[str, Any]:
        """Dry run: what this profile would measure and what it would need."""
        return {
            'version': self.version,
            'digest': self.digest,
            'attempts': self.attempts,
            'description': self.description,
            'targets': [target.as_dict() for target in self.targets],
            'required': [target.id for target in self.required_targets],
            'optional': [target.id for target in self.optional_targets],
            'disabled': [target.id for target in self.targets if not target.enabled],
            'optional_rule': self.optional_rule.as_dict(),
            'optional_needed': _optional_need(self),
            'budget': self.budget.as_dict(),
            'min_probes': self.min_probes,
            'max_probes': self.max_probes,
        }

    # -- evidence ------------------------------------------------------------ #

    def evidence(self, target_id: str, *, ok: int, attempts: int | None = None,
                 latency_ms: float | None = None, unknown: bool = False) -> 'TargetEvidence':
        """Evidence for a target, stamped with this revision's thresholds.

        ``attempts`` defaults to ``max(ok, 1)``: one recorded probe by default,
        and never fewer probes than successes.
        """
        return TargetEvidence(target_id, ok, max(ok, 1) if attempts is None else attempts,
                              latency_ms, unknown, self.target(target_id).fingerprint())

    def assess(self, evidence: Any) -> 'TargetAssessment':
        """Judge one piece of evidence against this revision's thresholds."""
        if isinstance(evidence, (str, bytes)):
            raise _fail(E_VALIDATION_FIELD, 'оценка ожидает TargetEvidence или объект')
        item = evidence if isinstance(evidence, TargetEvidence) else TargetEvidence.parse(evidence)
        target = self.target(item.target_id)
        if not target.enabled:
            return self._verdict(item, target, 'disabled', 'E_TARGET_DISABLED', None)
        if item.fingerprint != target.fingerprint():
            return self._verdict(item, target, 'unmeasured', 'E_TARGET_STALE_EVIDENCE', None)
        if item.unknown:
            return self._verdict(item, target, 'unknown', 'E_TARGET_UNKNOWN', None)
        if item.attempts <= 0:
            return self._verdict(item, target, 'skipped', 'E_TARGET_SKIPPED', None)
        ratio = item.ok / item.attempts
        if ratio + 1e-12 < target.min_success:
            return self._verdict(item, target, 'fail', 'E_TARGET_BELOW_MIN_SUCCESS', ratio)
        if target.max_latency_ms is not None:
            if item.latency_ms is None:
                return self._verdict(item, target, 'fail', 'E_TARGET_LATENCY_MISSING', ratio)
            if item.latency_ms > target.max_latency_ms:
                return self._verdict(item, target, 'fail', 'E_TARGET_LATENCY_EXCEEDED', ratio)
        return self._verdict(item, target, 'pass', None, ratio)

    @staticmethod
    def _verdict(item: 'TargetEvidence', target: TargetRule, state: str, reason: str | None,
                 ratio: float | None) -> 'TargetAssessment':
        return TargetAssessment(item.target_id, target.kind, state, reason, item, ratio, target)

    def assess_all(self, evidence: Any = None) -> dict[str, 'TargetAssessment']:
        """Assess every configured target; a target without evidence is unmeasured.

        Evidence about a target this profile does not contain is a caller bug and
        is refused: dropping it silently would hide a measurement nobody judged.
        """
        items = {item.target_id: item for item in _iter_evidence(evidence)}
        outside = sorted(set(items) - {target.id for target in self.targets})
        if outside:
            raise _fail(E_VALIDATION_FIELD,
                        f'evidence относится к целям вне профиля: {outside}')
        return {target.id: self.assess(items.get(target.id, TargetEvidence(target.id)))
                for target in self.targets}


@dataclass(frozen=True)
class TargetEvidence:
    """What one target produced, plus the thresholds it was produced under."""

    target_id: str
    ok: int = 0
    attempts: int = 0
    latency_ms: float | None = None
    unknown: bool = False
    fingerprint: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.target_id, str) or not self.target_id.strip():
            raise _fail(E_VALIDATION_FIELD, 'evidence.target: обязательное поле')
        ok = _integer(self.ok, minimum=0)
        attempts = _integer(self.attempts, minimum=0)
        if ok > attempts:
            raise _fail(E_VALIDATION_FIELD, 'evidence: успешных проб больше, чем попыток')
        if self.latency_ms is not None:
            _number(self.latency_ms, minimum=0.0)
        if not isinstance(self.unknown, bool):
            raise _fail(E_VALIDATION_FIELD, 'evidence.unknown: ожидается true/false')
        if self.fingerprint is not None and not isinstance(self.fingerprint, str):
            raise _fail(E_VALIDATION_FIELD, 'evidence.fingerprint: ожидается строка')
        object.__setattr__(self, 'target_id', self.target_id.strip())
        object.__setattr__(self, 'ok', ok)
        object.__setattr__(self, 'attempts', attempts)
        object.__setattr__(self, 'latency_ms',
                           None if self.latency_ms is None else float(self.latency_ms))

    @classmethod
    def parse(cls, value: Any) -> 'TargetEvidence':
        """Accept a mapping from any surface; ``target``, ``target_id`` and ``id`` are synonyms."""
        if isinstance(value, cls):
            return value
        data = _mapping(value, 'evidence')
        _reject_unknown(data, ('target', 'target_id', 'id', 'ok', 'attempts',
                               'latency_ms', 'unknown', 'fingerprint'), 'evidence')
        target_id = data.get('target', data.get('target_id', data.get('id')))
        return cls(target_id, data.get('ok', 0), data.get('attempts', 0),
                   data.get('latency_ms'), data.get('unknown', False), data.get('fingerprint'))

    def as_dict(self) -> dict[str, Any]:
        return {'target_id': self.target_id, 'ok': self.ok, 'attempts': self.attempts,
                'latency_ms': self.latency_ms, 'unknown': self.unknown,
                'fingerprint': self.fingerprint}


@dataclass(frozen=True)
class TargetAssessment:
    """The state of one target under this revision."""

    target_id: str
    kind: str
    state: str
    reason: str | None
    evidence: TargetEvidence
    ratio: float | None
    target: TargetRule

    @property
    def passed(self) -> bool:
        return self.state == 'pass'

    def as_dict(self) -> dict[str, Any]:
        return {'target_id': self.target_id, 'kind': self.kind, 'state': self.state,
                'reason': self.reason, 'ok': self.evidence.ok, 'attempts': self.evidence.attempts,
                'ratio': self.ratio, 'min_success': self.target.min_success,
                'max_latency_ms': self.target.max_latency_ms}


@dataclass(frozen=True)
class StopDecision:
    """Whether the executor may stop now, and why."""

    stop: bool
    reason: str | None = None
    live: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {'stop': self.stop, 'reason': self.reason, 'live': list(self.live)}


@dataclass(frozen=True)
class TargetDecision:
    """Per-target execution facts, for a checkpoint or a report."""

    target_id: str
    done: int = 0
    ok: int = 0
    latency_ms: float | None = None
    unknown: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {'target_id': self.target_id, 'done': self.done, 'ok': self.ok,
                'latency_ms': self.latency_ms, 'unknown': self.unknown}


# --------------------------------------------------------------------------- #
# Decisions: evaluate, plan, fail-fast
# --------------------------------------------------------------------------- #

def _iter_evidence(evidence: Any) -> list[TargetEvidence]:
    """Accept a list, a mapping ``{target_id: ...}`` or typed evidence."""
    if evidence is None:
        return []
    if isinstance(evidence, Mapping):
        items = []
        for key, value in evidence.items():
            parsed = TargetEvidence.parse(value)
            if parsed.target_id != key:
                raise _fail(E_VALIDATION_FIELD, 'evidence: ключ не совпадает с target_id')
            items.append(parsed)
        return items
    if isinstance(evidence, (str, bytes)) or not isinstance(evidence, Iterable):
        raise _fail(E_VALIDATION_SCHEMA, 'evidence: ожидается список или объект')
    return [TargetEvidence.parse(item) for item in evidence]


def _optional_need(profile: ProfileSpec) -> int | None:
    need = profile.optional_rule.need(len(profile.optional_targets))
    return len(profile.optional_targets) if need is None else need


def _optional_satisfied(profile: ProfileSpec, passed: Sequence[TargetAssessment]) -> bool:
    need = _optional_need(profile)
    if need is None:
        need = len(profile.optional_targets)
    return need == 0 or len(passed) >= need


def evaluate(spec: Any, evidence: Any = None) -> dict[str, Any]:
    """Decide pass/fail for one profile revision.  Pure: no clock, no I/O, no state.

    The order of the checks is fixed, so the reason code is reproducible and
    identical in GUI, CLI and API.  The non-empty guarantee comes first: an empty
    rule, ``K <= 0`` and a fully disabled target set can only produce a fail.
    """
    profile = _as_spec(spec)
    assessments = profile.assess_all(evidence)
    required = [assessments[target.id] for target in profile.required_targets]
    optional = [assessments[target.id] for target in profile.optional_targets]
    passed_required = [item for item in required if item.passed]
    passed_optional = [item for item in optional if item.passed]
    effective = len(passed_required) + len(passed_optional)
    used = sum(item.evidence.attempts for item in assessments.values()
               if item.state != 'disabled')
    reason, passed = 'E_VERDICT_RULE_SATISFIED', True

    if not required and not optional:
        reason, passed = 'E_VERDICT_EMPTY_TARGET_SET', False
    elif profile.optional_rule.mode == 'at_least' and profile.optional_rule.k < 1:
        reason, passed = 'E_VERDICT_K_NOT_POSITIVE', False
    elif profile.optional_rule.mode != 'none' and not optional:
        reason, passed = 'E_VERDICT_EMPTY_OPTIONAL_SET', False
    elif profile.optional_rule.mode == 'at_least' and profile.optional_rule.k > len(optional):
        reason, passed = 'E_VERDICT_K_UNREACHABLE', False
    elif len(passed_required) < len(required):
        reason, passed = 'E_VERDICT_REQUIRED_NOT_PASSED', False
    elif not _optional_satisfied(profile, passed_optional):
        reason, passed = 'E_VERDICT_OPTIONAL_NOT_PASSED', False
    elif effective == 0:
        # A pass always rests on at least one successful measured probe.
        reason, passed = 'E_VERDICT_NO_EFFECTIVE_PROBE', False

    return {
        'pass': passed,
        'reason': reason,
        'digest': profile.digest,
        'attempts': profile.attempts,
        'required': [item.target_id for item in required],
        'optional': [item.target_id for item in optional],
        'disabled': [item.target_id for item in assessments.values() if item.state == 'disabled'],
        'required_passed': [item.target_id for item in passed_required],
        'required_missing': [_missing(item) for item in required if not item.passed],
        'optional_passed': [item.target_id for item in passed_optional],
        'optional_missing': [_missing(item) for item in optional if not item.passed],
        'optional_rule': profile.optional_rule.as_dict(),
        'optional_needed': _optional_need(profile),
        'effective_probes': effective,
        'probes_used': used,
        'targets': {item.target_id: item.as_dict() for item in assessments.values()},
    }


def _missing(item: TargetAssessment) -> dict[str, Any]:
    return {'target_id': item.target_id, 'state': item.state, 'reason': item.reason}


def plan(spec: Any) -> dict[str, Any]:
    """Dry-run description of a profile: what it measures and what it needs."""
    return _as_spec(spec).plan()


class RunState:
    """Probe results of one run plus the fail-fast decision.

    ``record`` is called after every probe.  ``stop`` answers the only question
    that matters for consistency with the rule: can *any* completion of the
    remaining probes still pass?  A target with attempts left counts as live only
    while its best reachable ratio still reaches its threshold, which is exactly
    the per-target allowance ``proxytool.allowed_failures`` computes today.
    """

    def __init__(self, spec: Any):
        self.spec = _as_spec(spec)
        self.probes_used = 0
        self._done: dict[str, int] = {}
        self._ok: dict[str, int] = {}
        self._latency: dict[str, float | None] = {}
        self._unknown: dict[str, bool] = {}
        for target in self.spec.enabled_targets:
            self._done[target.id] = 0
            self._ok[target.id] = 0
            self._latency[target.id] = None
            self._unknown[target.id] = False

    def record(self, target_id: Any, *, ok: Any = 0, latency_ms: Any = None,
               unknown: Any = False) -> 'RunState':
        """Record one probe of one target; ``ok`` is 0 or 1 for that single probe."""
        if not isinstance(target_id, str):
            raise _fail(E_VALIDATION_FIELD, 'target_id: ожидается строка')
        target = self.spec.target(target_id)
        if not target.enabled:
            raise _fail(E_VALIDATION_FIELD, f'цель {target_id!r} выключена в этом профиле')
        good = _integer(ok, minimum=0, maximum=1)
        if latency_ms is not None:
            _number(latency_ms, minimum=0.0)
        if not isinstance(unknown, bool):
            raise _fail(E_VALIDATION_FIELD, 'unknown: ожидается true/false')
        if self._left(target_id) <= 0:
            raise _fail(E_CONFLICT_REVISION, f'цель {target_id!r}: попытки исчерпаны')
        self._done[target_id] += 1
        self._ok[target_id] += good
        if latency_ms is not None:
            self._latency[target_id] = float(latency_ms)
        self._unknown[target_id] = bool(unknown or self._unknown[target_id])
        self.probes_used += 1
        return self

    def _left(self, target_id: str) -> int:
        return self.spec.attempts - self._done.get(target_id, 0)

    def evidence(self, target_id: str) -> TargetEvidence:
        """Evidence recorded so far for a target, stamped for this revision."""
        target = self.spec.target(target_id)
        return TargetEvidence(target_id, self._ok.get(target_id, 0), self._done.get(target_id, 0),
                              self._latency.get(target_id), self._unknown.get(target_id, False),
                              target.fingerprint())

    def state(self, target_id: str) -> str:
        """State of a target under this revision, from what is recorded so far."""
        return self.spec.assess(self.evidence(target_id)).state

    def _live(self, target: TargetRule) -> bool:
        """Whether this target can still end in state ``pass``."""
        done, ok, left = self._done[target.id], self._ok[target.id], self._left(target.id)
        if left > 0:
            # Best ratio reachable with the attempts that are left.
            return (ok + left) / (done + left) + 1e-12 >= target.min_success
        return self.state(target.id) == 'pass'

    def can_pass(self) -> bool:
        """Whether any completion of the remaining probes could produce a pass."""
        required = self.spec.required_targets
        if not required:
            return False
        if sum(self._live(target) for target in required) < len(required):
            return False
        if _optional_need(self.spec) == 0:
            return True
        return sum(self._live(target) for target in self.spec.optional_targets) >= _optional_need(self.spec)

    def stop(self, *, elapsed_s: float | None = None) -> StopDecision:
        """Fail-fast decision.  A stop is only allowed when no pass is reachable."""
        required = self.spec.required_targets
        live = tuple(target.id for target in required if self._live(target))
        if not self.can_pass():
            if not required:
                reason = 'E_VERDICT_EMPTY_TARGET_SET'
            elif len(live) < len(required):
                reason = 'E_VERDICT_REQUIRED_NOT_PASSED'
            else:
                reason = 'E_VERDICT_OPTIONAL_NOT_PASSED'
            return StopDecision(True, reason, live)
        budget = self.spec.budget
        if budget.max_probes is not None and self.probes_used >= budget.max_probes:
            return StopDecision(True, E_LIMIT_BUDGET, live)
        if (elapsed_s is not None and budget.max_duration_s is not None
                and elapsed_s >= budget.max_duration_s):
            return StopDecision(True, E_LIMIT_BUDGET, live)
        return StopDecision(False, None, live)

    def pending_targets(self) -> list[str]:
        """Targets with attempts left; after a stop they must be stored as ``skipped``."""
        return [target.id for target in self.spec.enabled_targets if self._left(target.id) > 0]

    def as_decisions(self) -> list[TargetDecision]:
        """Per-target facts for a checkpoint or a report."""
        return [TargetDecision(target.id, self._done[target.id], self._ok[target.id],
                               self._latency[target.id], self._unknown[target.id])
                for target in self.spec.enabled_targets]

    def evaluate(self) -> dict[str, Any]:
        """Verdict from what is recorded so far; unfinished targets count as unmeasured."""
        return evaluate(self.spec, [self.evidence(target.id)
                                    for target in self.spec.enabled_targets])


def _as_spec(spec: Any) -> ProfileSpec:
    """Accept a ProfileSpec or a raw document; an unvalidated document stays usable."""
    if isinstance(spec, ProfileSpec):
        return spec
    return ProfileSpec.from_dict(spec, validate=False)


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

# CONTRACTS §3.3, migration 9, is the owner of this table.  ``id`` and ``config``
# are the columns proxytool.open_db() has always created; migration 9 adds the
# rest.  The tuple below is a *verification* list, never a DDL statement set:
# this module writes no DDL and calls ``db.migrate()`` instead (HANDOFF §2.2).
MIGRATION = 9
CONTRACT_COLUMNS = ('id', 'config', 'name', 'revision', 'parent_id', 'digest',
                    'created_at', 'archived_at', 'is_default')
_INSERT = ('INSERT INTO profiles(id, name, revision, parent_id, digest, created_at, '
           'archived_at, is_default, config) VALUES (?,?,?,?,?,?,?,?,?)')
_SELECT = ('SELECT id, name, revision, parent_id, digest, created_at, archived_at, '
           'is_default, config FROM profiles')
_ROW_ID = re.compile(r'^(?P<profile_id>[^@]+)@(?P<revision>\d+)$')


def revision_key(profile_id: str, revision: int) -> str:
    """Primary key of one revision row.

    The declared primary key is the single column ``id``, so a profile and its
    revisions cannot share one row: the key carries both parts, and
    :func:`parse_row_id` splits it back.
    """
    return f'{profile_id}@{revision}'


def parse_row_id(row_id: str) -> tuple[str, int]:
    """Inverse of :func:`revision_key`.  A legacy content hash has no revision."""
    match = _ROW_ID.match(row_id or '')
    if not match:
        raise _fail(E_DATA_DB_FOREIGN, f'идентификатор ревизии не разобран: {row_id!r}')
    return match.group('profile_id'), int(match.group('revision'))


def verify_schema(conn: sqlite3.Connection) -> None:
    """Refuse a database whose ``profiles`` table is not at the contract shape.

    The caller is told to migrate, not helped: a missing column is a database
    problem, and silently working around it would write a row nobody else can read.
    """
    present = {row[1] for row in conn.execute('PRAGMA table_info(profiles)')}
    if not present:
        raise _fail(E_DATA_DB_FOREIGN,
                    'в базе нет таблицы profiles; выполните db.migrate()')
    missing = [name for name in CONTRACT_COLUMNS if name not in present]
    if missing:
        raise _fail(E_DATA_DB_FOREIGN,
                    f'таблица profiles без колонок {missing}; выполните db.migrate()')


@dataclass(frozen=True)
class ProfileRef:
    """What a consumer passes around: the profile and one immutable revision."""

    profile_id: str
    revision: int = 1

    def __post_init__(self) -> None:
        _clean_id(self.profile_id)
        object.__setattr__(self, 'revision', _integer(self.revision, minimum=1))

    @property
    def row_id(self) -> str:
        return revision_key(self.profile_id, self.revision)

    def as_dict(self) -> dict[str, Any]:
        return {'profile_id': self.profile_id, 'profile_revision': self.revision}


@dataclass(frozen=True)
class ProfileRecord:
    """One revision row together with its parsed specification."""

    ref: ProfileRef
    name: str
    parent_id: str | None
    digest: str
    created_at: float | None
    archived_at: float | None
    is_default: bool
    spec: ProfileSpec

    @property
    def profile_id(self) -> str:
        return self.ref.profile_id

    @property
    def revision(self) -> int:
        return self.ref.revision

    @property
    def row_id(self) -> str:
        """The primary key this revision occupies in the table."""
        return self.ref.row_id

    @property
    def archived(self) -> bool:
        return self.archived_at is not None

    def as_dict(self, *, include_plan: bool = False) -> dict[str, Any]:
        """The public record: one shape for GUI, CLI and API."""
        data = {
            'profile_id': self.ref.profile_id,
            'name': self.name,
            'profile_revision': self.ref.revision,
            'parent_id': self.parent_id,
            'digest': self.digest,
            'created_at': self.created_at,
            'archived_at': self.archived_at,
            'archived': self.archived,
            'is_default': self.is_default,
            'config': self.spec.as_dict(),
        }
        if include_plan:
            data['plan'] = self.spec.plan()
        return data


class ProfileStore:
    """Named profiles with immutable revisions in the workbench database.

    A write appends a row.  Nothing already written is changed in place except the
    ``archived_at``/``is_default`` bookkeeping of a whole profile.  The connection
    is the workbench one from ``db.connect``; this module writes no DDL, so the
    table has to be at the shape of migration 9 already.
    """

    def __init__(self, conn: sqlite3.Connection, *, verify: bool = True):
        if not isinstance(conn, sqlite3.Connection):
            raise _fail(E_VALIDATION_SCHEMA, 'ожидается sqlite3.Connection')
        if verify:
            verify_schema(conn)
        self.conn = conn

    @contextlib.contextmanager
    def _write(self):
        """One write transaction, on either kind of connection.

        ``db.connect`` opens the connection in autocommit mode
        (``isolation_level=None``), where ``with conn`` would commit nothing, so
        the transaction is stated explicitly.  A plain ``sqlite3.connect`` keeps
        its own implicit transaction and is closed by ``with conn``.
        """
        if self.conn.isolation_level is None:
            self.conn.execute('BEGIN IMMEDIATE')
            try:
                yield
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    self.conn.execute('ROLLBACK')
                raise
            self.conn.execute('COMMIT')
        else:
            with self.conn:
                yield

    # -- reading ------------------------------------------------------------ #

    def _row(self, ref: ProfileRef) -> Sequence[Any] | None:
        return self.conn.execute(f'{_SELECT} WHERE id = ?', (ref.row_id,)).fetchone()

    def get(self, profile_id: str, revision: int | None = None) -> ProfileRecord:
        """One revision, or the head revision when ``revision`` is omitted."""
        if revision is None:
            return self.head(profile_id)
        row = self._row(ProfileRef(profile_id, revision))
        if row is None:
            raise _fail(E_STATE_PROFILE_UNKNOWN,
                        f'профиль {_clean_id(profile_id)!r} ревизии {revision} не найден')
        return self._record(row)

    def head(self, profile_id: str) -> ProfileRecord:
        """The newest revision of a profile."""
        profile_id = _clean_id(profile_id)
        row = self.conn.execute(f"{_SELECT} WHERE id GLOB ? ORDER BY revision DESC LIMIT 1",
                                (f'{profile_id}@*',)).fetchone()
        if row is None:
            raise _fail(E_STATE_PROFILE_UNKNOWN, f'профиль {profile_id!r} не найден')
        return self._record(row)

    def history(self, profile_id: str) -> list[ProfileRecord]:
        """Every revision, oldest first.  Nothing is ever dropped."""
        profile_id = _clean_id(profile_id)
        rows = self.conn.execute(f'{_SELECT} WHERE id GLOB ? ORDER BY revision ASC',
                                 (f'{profile_id}@*',)).fetchall()
        if not rows:
            raise _fail(E_STATE_PROFILE_UNKNOWN, f'профиль {profile_id!r} не найден')
        return [self._record(row) for row in rows]

    def list(self, *, include_archived: bool = False) -> list[ProfileRecord]:
        """Head revision of every named profile, ordered by name.

        Rows without a name are the legacy content-addressed ones; they stay out
        of the library, because naming them would be inventing an origin (F02).
        """
        sql = f'{_SELECT} WHERE name IS NOT NULL'
        if not include_archived:
            sql += ' AND archived_at IS NULL'
        heads: dict[str, ProfileRecord] = {}
        for row in self.conn.execute(sql).fetchall():
            record = self._record(row)
            current = heads.get(record.profile_id)
            if current is None or record.revision > current.revision:
                heads[record.profile_id] = record
        return sorted(heads.values(), key=lambda item: (item.name, item.revision))

    def find(self, name: str, *, include_archived: bool = False) -> ProfileRecord | None:
        """Head record of the live (or, on request, archived) profile with this name."""
        wanted = clean_name(name)
        for record in self.list(include_archived=include_archived):
            if record.name == wanted:
                return record
        return None

    def default(self) -> ProfileRecord | None:
        """The profile marked as default, if there is one."""
        for record in self.list():
            if record.is_default:
                return record
        return None

    def free_name(self, name: str) -> str:
        """``name`` itself, or the first free ``name (n)``."""
        wanted = clean_name(name)
        if self.find(wanted) is None:
            return wanted
        for number in range(2, 1000):
            candidate = f'{wanted} ({number})'
            if len(candidate) <= MAX_NAME and self.find(candidate) is None:
                return candidate
        raise _fail(E_CONFLICT_NAME, f'не удалось подобрать свободное имя для {wanted!r}')

    def diff(self, profile_id: str, left: int, right: int) -> dict[str, Any]:
        """What changed between two revisions: targets, thresholds, rule, budget."""
        first, second = self.get(profile_id, left), self.get(profile_id, right)
        old = {target.id: target for target in first.spec.targets}
        new = {target.id: target for target in second.spec.targets}
        changed = [{'target_id': target_id, 'before': old[target_id].as_dict(),
                    'after': new[target_id].as_dict()}
                   for target_id in sorted(set(old) & set(new))
                   if old[target_id].as_dict() != new[target_id].as_dict()]
        return {
            'profile_id': _clean_id(profile_id),
            'from_revision': first.revision,
            'to_revision': second.revision,
            'added': sorted(set(new) - set(old)),
            'removed': sorted(set(old) - set(new)),
            'changed': changed,
            'optional_rule': {'before': first.spec.optional_rule.as_dict(),
                              'after': second.spec.optional_rule.as_dict()},
            'budget': {'before': first.spec.budget.as_dict(), 'after': second.spec.budget.as_dict()},
            'attempts': {'before': first.spec.attempts, 'after': second.spec.attempts},
            'digest_changed': first.digest != second.digest,
        }

    # -- writing ------------------------------------------------------------ #

    def create(self, name: str, spec: Any, *, is_default: bool = False,
               at: float | None = None) -> ProfileRef:
        """Create a named profile at revision 1."""
        clean = clean_name(name)
        profile = _as_spec(spec).validate()
        moment = _now(at)
        profile_id = 'p_' + secrets.token_hex(8)
        with self._write():
            self._assert_name_free(clean, None)
            self.conn.execute(_INSERT, (revision_key(profile_id, 1), clean, 1, None,
                                        profile.digest, moment, None, int(is_default),
                                        profile.to_json()))
            if is_default:
                self._clear_default(keep=profile_id)
        return ProfileRef(profile_id, 1)

    def copy(self, profile_id: str, *, new_name: str | None = None, revision: int | None = None,
             at: float | None = None) -> ProfileRef:
        """Copy a profile into a new one at revision 1, leaving the source intact.

        ``parent_id`` of the new revision points at the source revision, so the
        library can show "copied from X rev N" without a second table.
        """
        source = self.get(profile_id, revision)
        name = clean_name(new_name) if new_name is not None else self.free_name(source.name)
        moment = _now(at)
        with self._write():
            self._assert_name_free(name, None)
            copied = 'p_' + secrets.token_hex(8)
            self.conn.execute(_INSERT, (revision_key(copied, 1), name, 1, source.ref.row_id,
                                        source.spec.digest, moment, None, 0, source.spec.to_json()))
        return ProfileRef(copied, 1)

    def update(self, profile_id: str, spec: Any, *, base_revision: int | None = None,
               name: str | None = None, at: float | None = None) -> ProfileRef:
        """Append a revision.  The previous revision stays readable and unchanged.

        ``base_revision`` is the optimistic-concurrency check: a caller that read
        revision N says so, and gets ``E_CONFLICT_REVISION`` if somebody appended
        N+1 in the meantime.  A write that changes nothing returns the head
        revision instead of adding an empty one.
        """
        head = self.head(profile_id)
        if head.archived:
            raise _fail(E_CONFLICT_ARCHIVED,
                        f'профиль {head.name!r} в архиве; сначала восстановите его')
        if base_revision is not None and _integer(base_revision, minimum=1) != head.revision:
            raise _fail(E_CONFLICT_REVISION,
                        f'ожидалась ревизия {base_revision}, актуальна {head.revision}')
        profile = _as_spec(spec).validate()
        clean = clean_name(name) if name is not None else head.name
        if profile.digest == head.digest and clean == head.name:
            return head.ref
        moment = _now(at)
        with self._write():
            self._assert_name_free(clean, head.profile_id)
            revision = head.revision + 1
            self.conn.execute(_INSERT, (revision_key(head.profile_id, revision), clean, revision,
                                        head.ref.row_id, profile.digest, moment, None,
                                        int(head.is_default), profile.to_json()))
        return ProfileRef(head.profile_id, revision)

    def archive(self, profile_id: str, *, at: float | None = None) -> None:
        """Retire a whole profile.  Its history stays readable on request."""
        profile_id = self.head(profile_id).profile_id
        with self._write():
            self.conn.execute('UPDATE profiles SET archived_at = ?, is_default = 0 WHERE id GLOB ?',
                              (_now(at), f'{profile_id}@*'))

    def unarchive(self, profile_id: str) -> None:
        """Bring a profile back into the library."""
        head = self.head(profile_id)
        with self._write():
            self._assert_name_free(head.name, head.profile_id)
            self.conn.execute('UPDATE profiles SET archived_at = NULL WHERE id GLOB ?',
                              (f'{head.profile_id}@*',))

    def set_default(self, profile_id: str) -> ProfileRef:
        """Mark one profile as the default; exactly one profile holds the flag."""
        head = self.head(profile_id)
        if head.archived:
            raise _fail(E_CONFLICT_ARCHIVED, f'профиль {head.name!r} в архиве')
        with self._write():
            self._clear_default(keep=head.profile_id)
            self.conn.execute('UPDATE profiles SET is_default = 1 WHERE id GLOB ?',
                              (f'{head.profile_id}@*',))
        return head.ref

    # -- helpers ------------------------------------------------------------ #

    def _record(self, row: Sequence[Any]) -> ProfileRecord:
        (row_id, name, revision, parent_id, digest, created_at, archived_at,
         is_default, config) = tuple(row)
        profile_id, number = parse_row_id(row_id)
        return ProfileRecord(ProfileRef(profile_id, number), name or '', parent_id, digest,
                             created_at, archived_at, bool(is_default),
                             ProfileSpec.from_json(config, validate=False))

    def _assert_name_free(self, name: str, keep: str | None) -> None:
        for record in self.list():
            if record.name == name and record.profile_id != keep:
                raise _fail(E_CONFLICT_NAME, f'имя профиля {name!r} уже занято')

    def _clear_default(self, keep: str | None) -> None:
        if keep is None:
            self.conn.execute('UPDATE profiles SET is_default = 0 WHERE is_default = 1')
        else:
            self.conn.execute(
                'UPDATE profiles SET is_default = 0 WHERE is_default = 1 AND id NOT GLOB ?',
                (f'{keep}@*',))


def open_store(path: Any) -> ProfileStore:
    """Open the workbench database at ``path`` (or its data folder) as a store.

    ``db.migrate()`` brings the file to the contract schema and ``db.connect()``
    opens it with this package's settings.  This module adds no DDL of its own, so
    the table it uses is always the one migration 9 produced.
    """
    db.migrate(path)
    return ProfileStore(db.connect(path))


# --------------------------------------------------------------------------- #
# Interchange, and the one entry point for GUI, CLI and API
# --------------------------------------------------------------------------- #

def assert_secret_free(payload: Any) -> None:
    """Refuse a profile document that carries anything credential-shaped.

    An export is assembled from typed fields, so this is the second line of
    defence: it catches a hand-written config that came from somewhere with
    credentials in it.
    """
    def walk(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if str(key).lower() in _SECRET_KEYS:
                    raise _fail(E_SECRET_IN_PROFILE,
                                f'{path}.{key}: профиль не хранит и не экспортирует секреты')
                walk(item, f'{path}.{key}')
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                walk(item, f'{path}[{index}]')
        elif isinstance(value, str):
            if '://' in value and '@' in value.split('://', 1)[1].split('/', 1)[0]:
                raise _fail(E_SECRET_IN_PROFILE, f'{path}: адрес с учётными данными')
            if value.lower().startswith(_SECRET_PREFIXES):
                raise _fail(E_SECRET_IN_PROFILE, f'{path}: значение выглядит как секрет')

    walk(payload, 'profile')


def export_profile(store: Any, profile_id: str, *, revision: int | None = None,
                   history: bool = False, at: float | None = None) -> dict[str, Any]:
    """A secret-free JSON document for one profile or for its whole history."""
    if isinstance(store, ProfileStore):
        reader = store
    elif isinstance(store, sqlite3.Connection):
        reader = ProfileStore(store, verify=False)
    else:
        raise _fail(E_VALIDATION_SCHEMA, 'ожидается ProfileStore или sqlite3.Connection')
    record = reader.get(profile_id, revision)
    records = reader.history(record.profile_id) if history else [record]
    payload = {
        'kind': EXPORT_KIND,
        'version': EXPORT_VERSION,
        'exported_at': _now(at),
        'secrets': 'none',
        'name': record.name,
        'digest': record.digest,
        'revisions': [{'revision': item.revision,
                       'parent_revision': item.revision - 1 if item.revision > 1 else None,
                       'created_at': item.created_at, 'digest': item.digest,
                       'config': item.spec.as_dict()} for item in records],
    }
    assert_secret_free(payload)
    return payload


def import_profile(store: ProfileStore, payload: Any, *, name: str | None = None,
                   on_conflict: str = 'fail', at: float | None = None) -> ProfileRef:
    """Install a document from :func:`export_profile` as a new profile.

    History is restored when the document carries it.  An existing profile is
    never overwritten: ``fail`` refuses, ``rename`` picks a free name, and
    ``reuse`` is idempotent - a document with the same name and the same content
    returns the existing profile instead of creating a second one.  Changing a
    profile is :meth:`ProfileStore.update`, which keeps the history.
    """
    if not isinstance(store, ProfileStore):
        raise _fail(E_VALIDATION_SCHEMA, 'import_profile ожидает ProfileStore')
    if on_conflict not in ('fail', 'rename', 'reuse'):
        raise _fail(E_VALIDATION_FIELD, 'on_conflict: ожидается fail, rename или reuse')
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            raise _fail(E_IMPORT_FORMAT, 'ожидается JSON-документ профиля') from None
    data = _mapping(payload, 'profile document')
    _reject_unknown(data, ('kind', 'version', 'exported_at', 'secrets', 'name',
                           'digest', 'revisions'), 'profile document')
    if data.get('kind') != EXPORT_KIND:
        raise _fail(E_IMPORT_FORMAT, f'ожидается документ вида {EXPORT_KIND}')
    if data.get('version') != EXPORT_VERSION:
        raise _fail(E_IMPORT_REVISION,
                    f'версия документа {data.get("version")!r} этой версией не поддерживается')
    revisions = data.get('revisions')
    if isinstance(revisions, (str, bytes)) or not isinstance(revisions, Sequence) or not revisions:
        raise _fail(E_IMPORT_FORMAT, 'profile document: revisions должен быть непустым списком')
    assert_secret_free(data)

    configs = [ProfileSpec.from_dict(_mapping(_mapping(item, 'revision').get('config'), 'revision.config'))
               for item in revisions]

    wanted = clean_name(name) if name is not None else clean_name(data.get('name') or '')
    existing = store.find(wanted)
    if existing is not None:
        if on_conflict == 'fail':
            raise _fail(E_CONFLICT_NAME, f'имя профиля {wanted!r} уже занято')
        if on_conflict == 'reuse':
            if existing.digest == configs[-1].digest:
                return existing.ref
            raise _fail(E_CONFLICT_NAME,
                        f'профиль {wanted!r} существует с другим содержимым; изменение '
                        'выполняется через update(), оно сохраняет историю')
        wanted = store.free_name(wanted)
    # Every revision of the document is replayed in order, so the chain - and with
    # it the history of the previous version - is restored instead of flattened to
    # one row.  The last revision is the head.
    ref = store.create(wanted, configs[0], at=at)
    for config in configs[1:]:
        ref = store.update(ref.profile_id, config, base_revision=ref.revision, at=at)
    return ref


def run_request(payload: Any, *, store: Any = None) -> dict[str, Any]:
    """The single entry point GUI, CLI and API call to run a profile.

    ``payload`` is ``{'profile': <spec or document>, 'evidence': [...]}``, or
    ``{'profile_id': ..., 'profile_revision': ..., 'evidence': [...]}`` with a
    ``store``.  All three surfaces send the same document and therefore get the
    same verdict, which is what F05 requires.
    """
    data = _mapping(payload, 'request')
    _reject_unknown(data, ('profile', 'profile_id', 'profile_revision', 'evidence'), 'request')
    evidence = _iter_evidence(data.get('evidence'))
    if data.get('profile') is not None:
        spec = _as_spec(data['profile'])
    else:
        if store is None:
            raise _fail(E_VALIDATION_FIELD, 'запуск по profile_id требует store')
        reader = store if isinstance(store, ProfileStore) else ProfileStore(store, verify=False)
        record = reader.get(data.get('profile_id'), data.get('profile_revision'))
        spec = record.spec
    verdict = evaluate(spec, evidence)
    verdict['profile'] = spec.plan()
    return verdict


# --------------------------------------------------------------------------- #
# Compatibility bridge
# --------------------------------------------------------------------------- #

def from_legacy_config(config: Any) -> ProfileSpec:
    """Convert a stored content-addressed config into a named-profile revision.

    The old ``profiles`` table keyed a row by ``sha256(config)`` with no name and
    one global ``min_success`` applied to every target.  Such a row becomes
    revision 1 of a named profile with every target mandatory and that global
    threshold copied into each target, so the acceptance rule is unchanged.
    """
    data = _mapping(config, 'legacy config')
    targets = data.get('targets')
    if isinstance(targets, (str, bytes)) or not isinstance(targets, Sequence):
        raise _fail(E_VALIDATION_SCHEMA, 'legacy config.targets: ожидается список')
    fail_fast = data.get('fail_fast')
    threshold = fail_fast.get('min_success') if isinstance(fail_fast, Mapping) else None
    rules = []
    for index, target in enumerate(targets):
        entry = _mapping(target, 'legacy target')
        target_id = str(entry.get('id') or f'target-{index}')
        if not _ID_RE.fullmatch(target_id):
            target_id = f'target-{index}'
        rules.append(TargetRule.parse({'id': target_id, 'kind': 'required', 'enabled': True,
                                       'min_success': 1.0 if threshold is None else threshold}))
    if not rules:
        raise _fail(E_VALIDATION_FIELD, 'legacy config: нет ни одной цели')
    return ProfileSpec.create(targets=rules, attempts=data.get('attempts', 1))


def create_spec(**kwargs: Any) -> ProfileSpec:
    """Alias of :meth:`ProfileSpec.create` for callers that prefer a function."""
    return ProfileSpec.create(**kwargs)


# --------------------------------------------------------------------------- #
# Small shared helpers
# --------------------------------------------------------------------------- #

def _now(at: float | None) -> float:
    if at is None:
        return time.time()
    return _number(at, minimum=0.0)


def _clean_id(value: Any) -> str:
    if not isinstance(value, str) or not _PROFILE_ID_RE.fullmatch(value.strip()):
        raise _fail(E_VALIDATION_FIELD, f'некорректный идентификатор профиля: {value!r}')
    return value.strip()


def clean_name(value: Any) -> str:
    """A profile name: single line, bounded, not empty."""
    name = _clean_text(value)
    if not name:
        raise _fail(E_VALIDATION_FIELD, 'имя профиля не может быть пустым')
    if len(name) > MAX_NAME:
        raise _fail(E_VALIDATION_FIELD, f'имя профиля длиннее {MAX_NAME} символов')
    return name
