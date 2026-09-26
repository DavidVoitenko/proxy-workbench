"""Import of proxy lists: adapters, preview, transactional commit, report.

CONTRACTS.ru.md: F03 (§7.1), error codes (§5.4), schema migrations 1, 2 and 12
(`endpoints`, `collections`, `membership`, `import_batch`).

The module owns no DDL.  The caller passes a connection already migrated by
`db.migrate()`, and this module is the only writer of `membership` rows that
carry `origin='import:<batch_id>'`.

Guarantees this module makes, and where each one is checked:

* preview never writes: it opens no transaction and issues no DML;
* commit is one transaction, so a crash or a cancellation leaves no half-applied
  replace;
* the same preview committed twice is a replay, not a second copy;
* a preview whose collection changed since it was produced is refused with
  `E_IMPORT_REVISION` instead of overwriting somebody else's work;
* every path (file, drag-and-drop, clipboard) and every adapter runs the same
  endpoint check, so a hostname, a private address or credentials are refused
  immediately and with the same code everywhere (defect 10, R06);
* raw input is never stored: rows are reported by line number and redacted
  sample, never by their original text (F03, F04).
"""

from __future__ import annotations

import csv
import hashlib
import io
import ipaddress
import json
import re
import sqlite3
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import urlsplit

from proxy_workbench import db
from proxy_workbench import geoip
from proxy_workbench import proxytool

__all__ = [
    'FORMATS', 'MODES', 'MAX_IMPORT_BYTES', 'MAX_IMPORT_ROWS',
    'ImportSource', 'ColumnMapping', 'MappingSuggestion', 'EndpointPolicy',
    'ImportRow', 'Preview', 'ImportReport',
    'ImportProblem', 'ImportFormatError', 'ImportEncodingError', 'ImportTooLarge',
    'ImportRevisionConflict', 'ImportPartialBlocked', 'ImportCancelled',
    'ImportBusy', 'UnknownCollection', 'ORIGIN_IMPORT',
    'detect_format', 'suggest_mapping', 'redact', 'endpoint_id',
    'create_collection', 'collection_revision', 'load_report',
    'preview', 'commit',
]

# --- limits -----------------------------------------------------------------
# The GUI already refuses a proxy list larger than 20 MB (gui.py:160); the
# import wizard must not be a way around that ceiling.
MAX_IMPORT_BYTES = 20 * 1024 * 1024
MAX_IMPORT_ROWS = 1_000_000
MAX_SAMPLE = 80
DELETE_CHUNK = 500

# --- codes ------------------------------------------------------------------
# E_IMPORT_REVISION / E_IMPORT_FORMAT / E_IMPORT_PARTIAL are the IMPORT domain
# of CONTRACTS §5.4.  The rest are additions this module needs; they are
# requested in docs/integration/HANDOFF/importer.md §5.
CODE_FORMAT = 'E_IMPORT_FORMAT'
CODE_ENCODING = 'E_IMPORT_ENCODING'
CODE_SIZE = 'E_IMPORT_SIZE'
CODE_EMPTY = 'E_IMPORT_EMPTY'
CODE_REVISION = 'E_IMPORT_REVISION'
CODE_PARTIAL = 'E_IMPORT_PARTIAL'
CODE_CANCELLED = 'E_IMPORT_CANCELLED'
CODE_BUSY = 'E_CONFLICT_BUSY'
CODE_NO_COLLECTION = 'E_IMPORT_NO_COLLECTION'
CODE_FIELD = 'E_VALIDATION_FIELD'

# Row level reasons.  They live in preview rows and in the import report, never
# as an exception.
ROW_DUPLICATE_IN_SOURCE = 'E_IMPORT_DUPLICATE_IN_SOURCE'
ROW_ALREADY_MEMBER = 'E_IMPORT_ALREADY_MEMBER'
ROW_CREDENTIALS = 'E_IMPORT_CREDENTIALS'
ROW_HOSTNAME = 'E_IMPORT_HOSTNAME'
ROW_PRIVATE = 'E_IMPORT_PRIVATE'
ROW_MISSING_FIELD = 'E_IMPORT_MISSING_FIELD'

FORMATS = ('txt', 'uri', 'csv', 'json')
MODES = ('merge', 'replace')

VALID = 'valid'
DUPLICATE = 'duplicate'
REJECTED = 'rejected'

#: Column names that mean "this row carries credentials".  A row offering them
#: is refused the same way a URI with userinfo is refused, instead of being
#: stored without the very field the user expected to work.
CREDENTIAL_COLUMNS = frozenset({
    'user', 'username', 'login', 'pass', 'password', 'passwd', 'pwd',
    'secret', 'token', 'api key', 'apikey', 'auth', 'authorization',
    'credential', 'credentials', 'creds',
})

ROLE_ALIASES = {
    'host': ('host', 'hostname', 'host name', 'ip', 'ip address', 'ipaddress',
             'address', 'addr', 'server', 'proxy', 'proxy address', 'endpoint'),
    'port': ('port', 'port number', 'portnumber', 'proxy port', 'tcp port', 'proxyport'),
    'scheme': ('scheme', 'protocol', 'proto', 'type', 'kind'),
    'country': ('country', 'country code', 'countrycode', 'cc', 'geo', 'geo country'),
}
ROLES = ('host', 'port', 'scheme', 'country')

_JSON_LIST_KEYS = ('proxies', 'items', 'rows', 'data', 'list', 'result')
_CUT_CHARS = '/?# \t'


# --- errors -----------------------------------------------------------------
class ImportProblem(Exception):
    """Base class of every refusal this module raises.

    `code` is the stable machine code (CONTRACTS §5.4); `detail` carries the
    machine context an API layer needs to answer a conflict without parsing
    the message.
    """

    code = CODE_FORMAT

    def __init__(self, message: str, *, code: str | None = None, detail: dict | None = None):
        super().__init__(message)
        if code:
            self.code = code
        self.detail = detail or {}


class ImportFormatError(ImportProblem):
    code = CODE_FORMAT


class ImportEncodingError(ImportProblem):
    code = CODE_ENCODING


class ImportTooLarge(ImportProblem):
    code = CODE_SIZE


class ImportRevisionConflict(ImportProblem):
    code = CODE_REVISION


class ImportPartialBlocked(ImportProblem):
    code = CODE_PARTIAL


class ImportCancelled(ImportProblem):
    code = CODE_CANCELLED


class ImportBusy(ImportProblem):
    code = CODE_BUSY


class UnknownCollection(ImportProblem):
    code = CODE_NO_COLLECTION


# --- secrets ----------------------------------------------------------------
def _field_separators(authority: str) -> list:
    """Positions of the colons that separate fields, ignoring the ones in ``[]``.

    A bracketed IPv6 literal carries its own colons; they are part of the
    address, not field separators.
    """
    positions, depth = [], 0
    for index, char in enumerate(authority):
        if char == '[':
            depth += 1
        elif char == ']':
            depth = max(depth - 1, 0)
        elif char == ':' and depth == 0:
            positions.append(index)
    return positions


def _redact_fields(authority: str) -> str:
    """Cut everything a plain ``host:port`` authority cannot contain.

    Dropping the userinfo at ``@`` is not enough.  ``45.33.32.156:8080:secret``
    and ``user:secret`` are the same leak without a scheme and without an
    ``@``, and neither is a valid endpoint, so the address part is kept for the
    user and every following field goes.
    """
    positions = _field_separators(authority)
    if not positions:
        return authority
    if len(positions) == 1 and authority[positions[0] + 1:].isdigit():
        return authority  # a plain host:port, or ``[v6]:port``
    return authority[:positions[0]] + ' …'


def redact(value: str, *, whole: bool = True) -> str:
    """Reduce a rejected cell to a sample that is safe to log and to store.

    Everything but the scheme and the credential-free authority is dropped: a
    password in `user:pass@host`, in a query string, in a `host:port:password`
    triple or simply trailing on the line must not survive into a report, a log
    line or provenance (F03, F04).

    `whole` says that `value` is a complete record rather than one cell of a
    row.  A record keeps a token that carries a dot (a hostname or an address);
    a cell additionally keeps a pure number, so the ``8080`` of a CSV row stays
    next to the host the user has to look for.  Everything else is an opaque
    ``***``: it is not an address in either reading, and a pasted password line
    is the realistic way it gets there.
    """
    text = ' '.join(value.split())
    if not text:
        return ''
    head, separator, tail = text.partition('://')
    if not separator:
        head, separator, tail = '', '', text
    cut = len(tail)
    for char in _CUT_CHARS:
        found = tail.find(char)
        if found != -1:
            cut = min(cut, found)
    authority, rest = tail[:cut], tail[cut:]
    at = authority.rfind('@')
    if at != -1:
        authority = '***@' + authority[at + 1:]
    bare = not _field_separators(authority) and not any(char in authority for char in '.[')
    if bare and not (not whole and authority.isdigit()):
        return '***'
    dropped = ' …' if rest.strip() else ''
    return (head + separator + _redact_fields(authority) + dropped)[:MAX_SAMPLE]


def _has_credentials(value: str) -> bool:
    target = value if '://' in value else 'http://' + value
    try:
        parsed = urlsplit(target)
    except ValueError:
        return False
    return parsed.username is not None or parsed.password is not None


# --- source -----------------------------------------------------------------
class ImportSource:
    """Raw bytes of an import, in memory only.

    The text is parsed and dropped: it is never written to disk, to a log or to
    the import report, which carries `name` and `digest` instead.
    """

    __slots__ = ('_text', 'name', 'digest', 'size', 'channel')

    def __init__(self, text: str, name: str, digest: str, size: int, channel: str):
        self._text = text
        self.name = name
        self.digest = digest
        self.size = size
        self.channel = channel

    def __repr__(self) -> str:  # the text must never reach a traceback or a log
        return (f'ImportSource(name={self.name!r}, digest={self.digest[:12]!r}, '
                f'size={self.size}, channel={self.channel!r})')

    @property
    def text(self) -> str:
        return self._text

    @staticmethod
    def _decode(data: bytes, name: str) -> str:
        if len(data) > MAX_IMPORT_BYTES:
            raise ImportTooLarge(
                f'Файл «{name}» больше {MAX_IMPORT_BYTES // (1024 * 1024)} МБ.',
                detail={'name': name, 'bytes': len(data), 'limit': MAX_IMPORT_BYTES})
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError as exc:
            raise ImportEncodingError(
                f'Файл «{name}» не в UTF-8; сохраните его в UTF-8 и повторите импорт.',
                detail={'name': name, 'position': exc.start}) from None
        if '\x00' in text:
            raise ImportEncodingError(
                f'Файл «{name}» двоичный, а не список прокси.',
                detail={'name': name})
        return text

    @classmethod
    def from_text(cls, text: str, *, name: str = 'clipboard', channel: str = 'clipboard') -> 'ImportSource':
        """Paste buffer, drag-and-drop of selected text, one-shot API import."""
        if not isinstance(text, str):
            raise ImportProblem('Текст импорта должен быть строкой.', code=CODE_FIELD)
        data = text.encode('utf-8')
        if len(data) > MAX_IMPORT_BYTES:
            raise ImportTooLarge(
                f'Текст импорта больше {MAX_IMPORT_BYTES // (1024 * 1024)} МБ.',
                detail={'name': name, 'bytes': len(data), 'limit': MAX_IMPORT_BYTES})
        return cls(text, name, hashlib.sha256(data).hexdigest(), len(data), channel)

    @classmethod
    def from_bytes(cls, data: bytes, *, name: str = 'import', channel: str = 'drop') -> 'ImportSource':
        if not isinstance(data, (bytes, bytearray)):
            raise ImportProblem('Данные импорта должны быть байтами.', code=CODE_FIELD)
        data = bytes(data)
        return cls(cls._decode(data, name), name, hashlib.sha256(data).hexdigest(), len(data), channel)

    @classmethod
    def from_path(cls, path, *, channel: str = 'file') -> 'ImportSource':
        path = Path(path)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ImportFormatError(
                f'Файл «{path.name}» не прочитан: {exc.strerror or exc}.',
                detail={'name': path.name}) from None
        return cls.from_bytes(data, name=path.name, channel=channel)

    @classmethod
    def from_drop(cls, payload, *, name: str = 'drop', path=None) -> 'ImportSource':
        """Drag-and-drop: a dropped file path or a dropped text selection."""
        if path is not None:
            return cls.from_path(path, channel='drop')
        if isinstance(payload, (bytes, bytearray)):
            return cls.from_bytes(payload, name=name, channel='drop')
        return cls.from_text(payload, name=name, channel='drop')


# --- mapping ----------------------------------------------------------------
@dataclass(frozen=True)
class ColumnMapping:
    """Which column carries which role.  A value is a column name or an index.

    `scheme` and `country` are optional; `host` and `port` are required for
    CSV and for JSON objects.
    """

    host: str | int | None = None
    port: str | int | None = None
    scheme: str | int | None = None
    country: str | int | None = None

    def role(self, name: str) -> str | int | None:
        return getattr(self, name)

    def to_dict(self) -> dict:
        return {role: self.role(role) for role in ROLES}


@dataclass(frozen=True)
class MappingSuggestion:
    mapping: ColumnMapping
    found: dict
    missing: tuple
    ambiguous: tuple

    @property
    def usable(self) -> bool:
        return not self.missing and not self.ambiguous

    def to_dict(self) -> dict:
        return {'mapping': self.mapping.to_dict(), 'found': self.found,
                'missing': list(self.missing), 'ambiguous': list(self.ambiguous)}


def _fold(name) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', str(name).strip().lower()).strip()


def suggest_mapping(columns: Sequence) -> MappingSuggestion:
    """Propose roles for a header row; ambiguous roles stay undecided."""
    folded = [_fold(column) for column in columns]
    found, ambiguous = {}, {}
    for role, aliases in ROLE_ALIASES.items():
        hits = [index for index, name in enumerate(folded) if name in aliases]
        if len(hits) == 1:
            found[role] = columns[hits[0]]
        elif len(hits) > 1:
            ambiguous[role] = tuple(columns[index] for index in hits)
    missing = tuple(role for role in ROLES[:2] if role not in found and role not in ambiguous)
    mapping = ColumnMapping(host=found.get('host'), port=found.get('port'),
                            scheme=found.get('scheme'), country=found.get('country'))
    return MappingSuggestion(mapping, found, missing, tuple(sorted(ambiguous)))


def _resolve(mapping: ColumnMapping, columns: Sequence) -> dict:
    """role -> column index, refusing a mapping that names a missing column."""
    positions, lowered = {}, {}
    for index, column in enumerate(columns):
        lowered.setdefault(_fold(column), index)
    for role in ROLES:
        value = mapping.role(role)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ImportFormatError(f'Колонка для «{role}» должна быть именем или номером.',
                                    detail={'role': role, 'value': repr(value)})
        if isinstance(value, int):
            if not 0 <= value < len(columns):
                raise ImportFormatError(
                    f'Колонка {value} для «{role}» отсутствует: в файле {len(columns)} колонок.',
                    detail={'role': role, 'index': value, 'columns': list(columns)})
            positions[role] = value
            continue
        index = lowered.get(_fold(value))
        if index is None:
            raise ImportFormatError(
                f'В файле нет колонки «{value}» для «{role}».',
                detail={'role': role, 'column': value, 'columns': list(columns)})
        positions[role] = index
    for role in ('host', 'port'):
        if role not in positions:
            raise ImportFormatError(f'Не сопоставлена обязательная колонка «{role}».',
                                    detail={'role': role, 'columns': list(columns)})
    return positions


def _credential_columns(columns: Sequence) -> tuple:
    return tuple(str(column) for column in columns if _fold(column) in CREDENTIAL_COLUMNS)


def _names_the_header(mapping: ColumnMapping, record: Sequence) -> bool:
    """True when every column *name* of a mapping is a cell of the first record.

    `_is_header_record` only recognises a header that spells a role (``host``,
    ``ip``, ``port``…).  A perfectly ordinary ``a,b,c`` header is therefore
    data to the guesser: the wizard could only map positionally, and the header
    line itself became a rejected row that blocked the commit.  When the caller
    names columns, those names can only refer to a header, so the first record
    is treated as one.
    """
    names = [mapping.role(role) for role in ROLES]
    names = [str(name).strip() for name in names
             if isinstance(name, str) and not isinstance(name, bool) and name.strip()]
    return bool(names) and all(name in record for name in names)


# --- endpoint model ---------------------------------------------------------
@dataclass(frozen=True)
class EndpointPolicy:
    """What an import is allowed to contain.

    `public_only=True` (default) refuses hostnames and non-global addresses at
    parse time, because the only end to end consumer today — the collector
    normalizer `proxytool.normalize()` — drops them later, and accepting them
    here would be exactly defect 10.  Setting it to False is an explicit,
    recorded choice of a private collection, and the report says so.
    """

    public_only: bool = True


DEFAULT_POLICY = EndpointPolicy(public_only=True)


@dataclass(frozen=True)
class Parsed:
    canonical: str
    scheme: str
    host: str
    port: int
    ip_version: int | None


def _parts(canonical: str) -> Parsed:
    scheme, _, rest = canonical.partition('://')
    host, _, port = rest.rpartition(':')
    if host.startswith('[') and host.endswith(']'):
        host = host[1:-1]
    try:
        ip_version = ipaddress.ip_address(host).version
    except ValueError:
        ip_version = None
    return Parsed(canonical, scheme, host, int(port), ip_version)


def _classify(value: str, policy: EndpointPolicy):
    """Return (Parsed, None) or (None, reason_code).  One decision for all paths."""
    if _has_credentials(value):
        # F04 owns upstream credentials and there is no secret store yet, so the
        # only honest answer today is an immediate, uniform refusal.
        return None, ROW_CREDENTIALS
    canonical = proxytool.normalize_custom(value)
    if canonical is None:
        return None, CODE_FORMAT
    if policy.public_only and proxytool.normalize(value) is None:
        # the shared parser accepts it; only the public-only rule refuses it
        host = urlsplit(value if '://' in value else 'http://' + value).hostname or ''
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return None, ROW_HOSTNAME
        return None, ROW_PRIVATE
    return _parts(canonical), None


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def _country(value: str) -> str | None:
    """Country code from a mapped column, or None.  Never invented (F02, F08)."""
    if not value:
        return None
    value = value.strip().upper()
    return value if geoip.COUNTRY_CODE.fullmatch(value) else None


#: `membership.origin` vocabulary belongs to db.py (COLLECTION_ORIGINS); the batch
#: that put a row there is the `import_batch` row, not a second origin dialect.
ORIGIN_IMPORT = 'import'


def endpoint_id(canonical: str) -> str:
    """Stable short id, so the same address has one id in every collection."""
    return db.endpoint_id(canonical)


# --- rows -------------------------------------------------------------------
@dataclass(frozen=True)
class ImportRow:
    line: int
    state: str
    reason: str | None = None
    detail: str | None = None
    canonical: str | None = None
    endpoint_id: str | None = None
    scheme: str | None = None
    host: str | None = None
    port: int | None = None
    ip_version: int | None = None
    country: str | None = None
    duplicate_of: int | None = None
    present: bool = False
    sample: str = ''

    def to_dict(self) -> dict:
        data = {'line': self.line, 'state': self.state, 'reason': self.reason,
                'sample': self.sample}
        if self.canonical:
            data['canonical'] = self.canonical
        if self.detail:
            data['detail'] = self.detail
        if self.duplicate_of is not None:
            data['duplicate_of'] = self.duplicate_of
        if self.present:
            data['present_in_collection'] = True
        return data


# --- adapters ---------------------------------------------------------------
def detect_format(source: ImportSource) -> str:
    """Format of an import; 'txt' is the fallback for a plain address list.

    The wizard may always pass the format explicitly; detection only exists so
    that a dropped file without a useful extension is still readable.

    ``uri`` is chosen only when *every* meaningful line carries a scheme.  A
    list whose first line reads ``http://1.2.3.4:8080`` and whose remaining
    lines are bare addresses is an address list, not a URI list, and reading it
    as one rejected all but the first line - while ``txt`` accepts both.
    """
    text = source.text.lstrip()
    if not text:
        raise ImportFormatError(f'Импорт «{source.name}» пуст.', detail={'name': source.name})
    if text[0] == '{':
        return 'json'
    if text[0] == '[' and _is_json(text):
        return 'json'
    lines = [line.strip() for line in text.splitlines()
             if line.strip() and not line.strip().startswith('#')]
    first = lines[0] if lines else ''
    if not first:
        raise ImportFormatError(f'Импорт «{source.name}» не содержит данных.',
                                detail={'name': source.name})
    if _looks_like_header(first):
        return 'csv'
    if '\t' in first or first.count(',') >= 2 or first.count(';') >= 2:
        return 'csv'
    if '://' not in first:
        return 'txt'
    return 'uri' if all('://' in line for line in lines) else 'txt'


def _looks_like_header(line: str) -> bool:
    """A first record with a known role word in it is a header, not data."""
    if max(line.count(','), line.count(';'), line.count('\t')) < 1:
        return False
    return _is_header_record(re.split(r'[,;\t]', line))


def _is_header_record(cells: Sequence) -> bool:
    aliases = set()
    for names in ROLE_ALIASES.values():
        aliases.update(names)
    return any(_fold(cell) in aliases for cell in cells)


def _delimiter(line: str) -> str:
    counts = {char: line.count(char) for char in (',', ';', '\t')}
    best = max(counts, key=lambda char: counts[char])
    return best if counts[best] else ','


def _meaningful(text: str) -> list:
    """(line number, stripped line) for every line the user wrote something on."""
    return [(number, line.strip())
            for number, line in enumerate(text.splitlines(), 1)
            if line.strip() and not line.strip().startswith('#')]


def _line_row(number: int, line: str, require_scheme: bool, policy: EndpointPolicy) -> ImportRow:
    sample = redact(line)
    if require_scheme and '://' not in line:
        return ImportRow(number, REJECTED, CODE_FORMAT,
                         detail='в строке URI обязателен протокол', sample=sample)
    parsed, reason = _classify(line, policy)
    if parsed is None:
        return ImportRow(number, REJECTED, reason, sample=sample)
    return ImportRow(number, VALID, canonical=parsed.canonical,
                     endpoint_id=endpoint_id(parsed.canonical), scheme=parsed.scheme,
                     host=parsed.host, port=parsed.port, ip_version=parsed.ip_version,
                     sample=sample)


def _parse_txt(text: str, require_scheme: bool, policy: EndpointPolicy) -> list:
    rows = [_line_row(number, line, require_scheme, policy)
            for number, line in _meaningful(text)]
    return _mark_duplicates(rows)


def _cell_row(number: int, cells: dict, credentials: tuple, columns: Sequence,
              policy: EndpointPolicy) -> ImportRow:
    """One CSV record or one JSON object, whatever the transport.

    `credentials` names the credential-shaped columns the *file* offers, not the
    ones this row fills in.  A file that carries a ``password`` column is refused
    as a whole even where the cell is empty, because accepting the row and
    dropping the field is the "приняли форму и молча выбросили" case.
    """
    sample = _cells_sample(cells)
    host, port = cells.get('host', ''), cells.get('port', '')
    if credentials or (host and _has_credentials(host)):
        # userinfo in the address itself is a credential, not a missing column
        return ImportRow(number, REJECTED, ROW_CREDENTIALS,
                         detail=(f'колонки: {", ".join(credentials)}' if credentials
                                 else 'userinfo в адресе'), sample=sample)
    if not host or not port:
        missing = [role for role in ('host', 'port') if not cells.get(role)]
        return ImportRow(number, REJECTED, ROW_MISSING_FIELD,
                         detail='нет: ' + ', '.join(missing), sample=sample)
    if '://' in host:
        value = host
    else:
        scheme = cells.get('scheme', '').strip().lower()
        value = f'{scheme}://{host}:{port}' if scheme else f'{host}:{port}'
    parsed, reason = _classify(value, policy)
    if parsed is None:
        return ImportRow(number, REJECTED, reason, sample=sample)
    return ImportRow(number, VALID, canonical=parsed.canonical,
                     endpoint_id=endpoint_id(parsed.canonical), scheme=parsed.scheme,
                     host=parsed.host, port=parsed.port, ip_version=parsed.ip_version,
                     country=_country(cells.get('country', '')), sample=sample)


def _cells_sample(cells: dict) -> str:
    """Redact every cell on its own: a cell is never allowed to hide in a join.

    Each cell is redacted as a cell, not as a whole record, so the port stays
    visible next to the host the user has to look for.
    """
    return ' '.join(redact(cells[role], whole=False) for role in ('host', 'port', 'scheme')
                    if cells.get(role))[:MAX_SAMPLE]


def _parse_csv(text: str, mapping: ColumnMapping | None, policy: EndpointPolicy,
               header_row: bool | None = None) -> tuple:
    lines = text.splitlines()
    delimiter = _delimiter(next((line for line in lines if line.strip()), ''))
    reader = csv.reader(io.StringIO(text, newline=''), delimiter=delimiter)
    records, start = [], 0
    for record in reader:
        if any(cell.strip() for cell in record):
            records.append((start + 1, record))
        start = reader.line_num
    if not records:
        return [], None, ((), ()), False, None, ()
    first = [cell.strip() for cell in records[0][1]]
    # `header` is the caller's own answer, and it wins: a user who says the first
    # row is a header is believed, and a user who says it is data is believed too.
    # Without an answer the adapter guesses: a record that spells a role, or one
    # that carries every name of an explicit mapping - a mapping names a header by
    # definition, so "a,b,c" can be mapped instead of refused for a missing column.
    if header_row is True:
        has_header = True
    elif header_row is False:
        has_header = False
    else:
        has_header = _is_header_record(records[0][1]) or (
            mapping is not None and _names_the_header(mapping, first))
    header = first if has_header else None
    body = records[1:] if has_header else records
    columns = header if header is not None else [str(index) for index in range(len(records[0][1]))]
    suggestion = None
    if mapping is None:
        if header is None:
            mapping = ColumnMapping(host=0, port=1)
        else:
            suggestion = suggest_mapping(header)
            if not suggestion.usable:
                return [], None, (suggestion.missing, suggestion.ambiguous), True, suggestion, tuple(header or ())
            mapping = suggestion.mapping
    resolved = _resolve(mapping, columns)
    credentials = _credential_columns(header or ())
    rows = [_cell_row(line, {role: (record[index].strip() if index < len(record) else '')
                             for role, index in resolved.items()}, credentials, columns, policy)
            for line, record in body]
    return _mark_duplicates(rows), mapping, ((), ()), False, suggestion, tuple(header or ())


def _json_items(text: str) -> list:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ImportFormatError(f'Импорт JSON не разобран: {exc}.', detail={'error': str(exc)}) from None
    if isinstance(data, dict):
        for key in _JSON_LIST_KEYS:
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            raise ImportFormatError('В JSON ожидался список прокси.',
                                    detail={'keys': sorted(data)})
    if not isinstance(data, list):
        raise ImportFormatError('В JSON ожидался список прокси.',
                                detail={'type': type(data).__name__})
    return data


def _parse_json(text: str, mapping: ColumnMapping | None, policy: EndpointPolicy) -> tuple:
    items = _json_items(text)
    if not items:
        return [], None, ((), ()), False, None, ()
    if all(isinstance(item, str) for item in items):
        return _parse_txt('\n'.join(items), False, policy), None, ((), ()), False, None, ()
    columns = []
    for item in items:
        if isinstance(item, dict):
            for key in item:
                if key not in columns:
                    columns.append(key)
    if not columns:
        raise ImportFormatError('Смешанный список строк и объектов JSON не поддерживается.',
                                detail={'items': len(items)})
    credentials = _credential_columns(columns)
    suggestion = None
    if mapping is None:
        suggestion = suggest_mapping(columns)
        if not suggestion.usable:
            return [], None, (suggestion.missing, suggestion.ambiguous), True, suggestion, ()
        mapping = suggestion.mapping
    resolved = _resolve(mapping, columns)
    rows = []
    for index, item in enumerate(items, 1):
        if isinstance(item, str):
            # a plain string among objects is still one address line
            rows.append(_line_row(index, item, False, policy))
            continue
        if not isinstance(item, dict):
            rows.append(ImportRow(index, REJECTED, CODE_FORMAT, sample=redact(str(item))))
            continue
        cells = {role: str(item.get(columns[position], '')).strip()
                 for role, position in resolved.items()}
        rows.append(_cell_row(index, cells, credentials, columns, policy))
    return _mark_duplicates(rows), mapping, ((), ()), False, suggestion, ()


def _mark_duplicates(rows: list) -> list:
    """Second and later occurrences of one address inside one file."""
    first_seen: dict[str, int] = {}
    result = []
    for row in rows:
        if row.state == VALID and row.canonical in first_seen:
            result.append(replace(row, state=DUPLICATE, reason=ROW_DUPLICATE_IN_SOURCE,
                                  duplicate_of=first_seen[row.canonical]))
        else:
            if row.state == VALID:
                first_seen[row.canonical] = row.line
            result.append(row)
    return result


# --- parse entry point ------------------------------------------------------
def _parse(source: ImportSource, fmt: str | None, mapping: ColumnMapping | None,
           policy: EndpointPolicy, header: bool | None = None) -> tuple:
    """Return (rows, mapping, (missing, ambiguous), needs_mapping, format, suggestion, header)."""
    if len(source.text.splitlines()) > MAX_IMPORT_ROWS:
        raise ImportTooLarge(f'В импорте больше {MAX_IMPORT_ROWS} строк.',
                             detail={'name': source.name, 'limit': MAX_IMPORT_ROWS})
    chosen = (fmt or detect_format(source)).strip().lower()
    if chosen not in FORMATS:
        raise ImportFormatError(f'Неизвестный формат импорта: {chosen}.',
                                detail={'format': chosen, 'formats': list(FORMATS)})
    if chosen in ('txt', 'uri'):
        return _parse_txt(source.text, chosen == 'uri', policy), None, ((), ()), False, chosen, None, ()
    if chosen == 'csv':
        rows, used, problem, needs, suggestion, found_header = _parse_csv(
            source.text, mapping, policy, header)
    else:
        rows, used, problem, needs, suggestion, found_header = _parse_json(source.text, mapping,
                                                                          policy)
    return rows, used, problem, needs, chosen, suggestion, found_header


# --- collections ------------------------------------------------------------
def create_collection(conn: sqlite3.Connection, name: str, kind: str = 'private') -> str:
    """Create a collection, or return the existing one with the same name and kind.

    Row creation belongs to `db.create_collection`; the find-or-create step is
    the wizard's, because a wizard that offers a second "Мои прокси" by accident
    has already lost the user's scope.
    """
    if not isinstance(name, str) or not name.strip():
        raise ImportProblem('Имя коллекции обязательно.', code=CODE_FIELD)
    if kind not in db.COLLECTION_KINDS:
        raise ImportProblem(f'Неизвестный тип коллекции: {kind}.', code=CODE_FIELD,
                            detail={'kind': kind, 'kinds': list(db.COLLECTION_KINDS)})
    name = name.strip()
    row = conn.execute('SELECT id FROM collections WHERE name = ? AND kind = ?',
                       (name, kind)).fetchone()
    if row is not None:
        return row[0]
    return db.create_collection(conn, name, kind=kind)


def collection_revision(conn: sqlite3.Connection, collection_id: str) -> int:
    """Revision of a collection: how many import batches have been committed.

    It is derived from `import_batch.revision` (migration 12) because the
    contract puts no revision column on `collections`; a member edited by any
    other writer would not move it, which is why `commit` also compares the
    actual membership set.
    """
    row = conn.execute('SELECT max(revision) FROM import_batch WHERE collection_id = ? '
                     "AND state = 'committed'", (collection_id,)).fetchone()
    return int(row[0] or 0)


def _exists(conn: sqlite3.Connection, sql: str, args: tuple) -> bool:
    return conn.execute(sql, args).fetchone() is not None


def _members(conn: sqlite3.Connection, collection_id: str) -> dict:
    return {row[0]: row[1] for row in conn.execute(
        'SELECT e.id, e.canonical FROM membership m JOIN endpoints e ON e.id = m.endpoint_id '
        'WHERE m.collection_id = ?', (collection_id,))}


# --- preview ----------------------------------------------------------------
@dataclass(frozen=True)
class Preview:
    """A computed, not yet applied import.  Building it changes nothing."""
    batch_id: str
    collection_id: str
    collection_revision: int
    source_name: str
    source_digest: str
    channel: str
    format: str
    mode: str
    mapping: ColumnMapping | None
    policy: EndpointPolicy
    rows: tuple
    added: tuple
    unchanged: tuple
    removed: tuple
    removed_ids: tuple
    needs_mapping: bool
    mapping_problem: tuple = ()
    mapping_suggestion: MappingSuggestion | None = None
    skipped: int = 0
    created_at: float = 0.0
    header: tuple = ()
    """The first record the adapter consumed as a column header, if any.

    A surface shows it so the user can see that row 1 was consumed as names and
    not as data, instead of wondering where a record went.
    """

    # --- counters
    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def valid(self) -> list:
        return [row for row in self.rows if row.state == VALID]

    @property
    def duplicates(self) -> list:
        return [row for row in self.rows if row.state == DUPLICATE]

    @property
    def rejected(self) -> list:
        return [row for row in self.rows if row.state == REJECTED]

    @property
    def keep(self) -> list:
        """Rows that belong to the collection after this import.

        A row already in the collection is a duplicate for the report, but a
        `replace` must still keep it: "duplicate" says "no new membership",
        not "drop it".
        """
        return [row for row in self.rows
                if row.state == VALID or (row.state == DUPLICATE and row.reason == ROW_ALREADY_MEMBER)]

    @property
    def keep_ids(self) -> list:
        return [row.endpoint_id for row in self.keep]

    @property
    def partial(self) -> bool:
        return bool(self.rejected)

    @property
    def reasons(self) -> dict:
        counts: dict[str, int] = {}
        for row in self.rows:
            if row.reason:
                counts[row.reason] = counts.get(row.reason, 0) + 1
        return dict(sorted(counts.items()))

    @property
    def counts(self) -> dict:
        in_source = sum(row.reason == ROW_DUPLICATE_IN_SOURCE for row in self.duplicates)
        return {'total': self.total, 'valid': len(self.valid),
                'duplicates': len(self.duplicates), 'duplicate_in_source': in_source,
                'already_member': len(self.duplicates) - in_source,
                'rejected': len(self.rejected), 'skipped': self.skipped,
                'added': len(self.added), 'unchanged': len(self.unchanged),
                'removed': len(self.removed)}

    def to_dict(self) -> dict:
        return {'batch_id': self.batch_id, 'collection_id': self.collection_id,
                'collection_revision': self.collection_revision, 'state': 'preview',
                'mode': self.mode, 'format': self.format, 'channel': self.channel,
                'source': {'name': self.source_name, 'digest': self.source_digest},
                'policy': {'public_only': self.policy.public_only},
                'mapping': self.mapping.to_dict() if self.mapping else None,
                'header': list(self.header),
                'needs_mapping': self.needs_mapping,
                'mapping_problem': {'missing': list(self.mapping_problem[0]),
                                    'ambiguous': list(self.mapping_problem[1])},
                'counts': self.counts, 'reasons': self.reasons,
                'added': list(self.added), 'unchanged': list(self.unchanged),
                'removed': list(self.removed),
                'rows': [row.to_dict() for row in self.rows]}


def preview(conn: sqlite3.Connection, source: ImportSource, *, collection_id: str,
            mode: str = 'merge', mapping: ColumnMapping | None = None,
            policy: EndpointPolicy | None = None, fmt: str | None = None,
            header: bool | None = None, idempotency_key: str | None = None,
            now: float | None = None) -> Preview:
    """Read an import and say what it would do.  Writes nothing at all.

    `header` is the caller's answer for a CSV: `True` makes the first record a
    column header, `False` makes it data, and `None` (the default) lets the
    adapter decide.  JSON objects carry their own keys, so it applies to CSV only.
    """
    if not isinstance(source, ImportSource):
        raise ImportProblem('Нужен ImportSource.', code=CODE_FIELD)
    if mode not in MODES:
        raise ImportProblem(f'Режим импорта должен быть merge или replace, получено {mode!r}.',
                            code=CODE_FIELD, detail={'mode': mode, 'modes': list(MODES)})
    policy = DEFAULT_POLICY if policy is None else policy
    if not isinstance(policy, EndpointPolicy):
        raise ImportProblem('policy должен быть EndpointPolicy.', code=CODE_FIELD)
    if not _exists(conn, 'SELECT 1 FROM collections WHERE id = ?', (collection_id,)):
        raise UnknownCollection(f'Коллекция {collection_id} не найдена.',
                                detail={'collection_id': collection_id})
    revision = collection_revision(conn, collection_id)
    rows, used, problem, needs, chosen, suggestion, found_header = _parse(source, fmt, mapping,
                                                                        policy, header)
    present = _members(conn, collection_id)
    result = []
    for row in rows:
        if row.state == VALID and row.endpoint_id in present:
            result.append(replace(row, state=DUPLICATE, reason=ROW_ALREADY_MEMBER, present=True))
        else:
            result.append(row)
    added = tuple(row.canonical for row in result if row.state == VALID)
    kept = {row.endpoint_id for row in result
            if row.state == VALID or (row.state == DUPLICATE and row.reason == ROW_ALREADY_MEMBER)}
    unchanged = tuple(present[identifier] for identifier in _sorted(present, kept & set(present)))
    gone = _sorted(present, set(present) - kept) if mode == 'replace' else []
    removed = tuple(present[identifier] for identifier in gone)
    skipped = _skipped(source.text, chosen)
    return Preview(
        batch_id=_batch_id(collection_id, source, chosen, used, mode, idempotency_key),
        collection_id=collection_id, collection_revision=revision,
        source_name=source.name, source_digest=source.digest, channel=source.channel,
        format=chosen, mode=mode, mapping=used, policy=policy, rows=tuple(result),
        added=added, unchanged=unchanged, removed=removed, removed_ids=tuple(gone),
        needs_mapping=needs, mapping_suggestion=suggestion, mapping_problem=problem,
        skipped=skipped, created_at=time.time() if now is None else now, header=found_header)


def _sorted(present: dict, identifiers) -> list:
    """Diff lists are ordered by address, not by an opaque id."""
    return sorted(identifiers, key=lambda identifier: present[identifier])


def _skipped(text: str, fmt: str) -> int:
    if fmt not in ('txt', 'uri'):
        return 0
    return sum(1 for line in text.splitlines()
               if not line.strip() or line.strip().startswith('#'))


def _batch_id(collection_id: str, source: ImportSource, fmt: str,
              mapping: ColumnMapping | None, mode: str, idempotency_key: str | None) -> str:
    if idempotency_key:
        key = idempotency_key
    else:
        payload = json.dumps([collection_id, source.digest, fmt, mode,
                              mapping.to_dict() if mapping else None],
                             sort_keys=True, ensure_ascii=False)
        key = payload
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# --- report -----------------------------------------------------------------
@dataclass(frozen=True)
class ImportReport:
    batch_id: str
    collection_id: str
    mode: str
    format: str
    state: str
    revision_before: int
    revision_after: int
    counts: dict
    reasons: dict
    rejected: tuple
    added: tuple
    removed: tuple
    source_name: str
    source_digest: str
    public_only: bool
    created_at: float
    duration_s: float
    replayed: bool = False
    note: str | None = None

    def to_dict(self) -> dict:
        return {'batch_id': self.batch_id, 'collection_id': self.collection_id,
                'mode': self.mode, 'format': self.format, 'state': self.state,
                'revision_before': self.revision_before, 'revision_after': self.revision_after,
                'counts': self.counts, 'reasons': self.reasons,
                'rejected': [dict(item) for item in self.rejected],
                'added': list(self.added), 'removed': list(self.removed),
                'source': {'name': self.source_name, 'digest': self.source_digest},
                'policy': {'public_only': self.public_only},
                'created_at': self.created_at, 'duration_s': self.duration_s,
                'replayed': self.replayed, 'note': self.note}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


def load_report(conn: sqlite3.Connection, batch_id: str) -> ImportReport | None:
    row = conn.execute('SELECT id, collection_id, created_at, state, report_json, revision '
                     'FROM import_batch WHERE id = ?', (batch_id,)).fetchone()
    if row is None or not row[4]:
        return None
    data = json.loads(row[4])
    return ImportReport(
        batch_id=row[0], collection_id=row[1], mode=data['mode'], format=data['format'],
        state=row[3], revision_before=data['revision_before'], revision_after=row[5] or 0,
        counts=data['counts'], reasons=data['reasons'],
        rejected=tuple(data['rejected']), added=tuple(data['added']), removed=tuple(data['removed']),
        source_name=data['source']['name'], source_digest=data['source']['digest'],
        public_only=data['policy']['public_only'], created_at=row[2],
        duration_s=data['duration_s'], note=data.get('note'))


# --- commit -----------------------------------------------------------------
def commit(conn: sqlite3.Connection, plan: Preview, *, allow_partial: bool = False,
           allow_empty: bool = False, busy: Callable[[], str | None] | None = None,
           should_cancel: Callable[[], bool] | None = None,
           on_progress: Callable[[str, int, int], None] | None = None,
           now: float | None = None) -> ImportReport:
    """Apply a preview in one transaction.

    The connection must be migrated (`db.migrate()`) with
    `PRAGMA foreign_keys=ON`, and must not be inside a transaction of its own:
    this call opens the only one, and rolls it back on any error.

    `busy` is the integrator's hook for "this collection is being scanned right
    now" (R1: such a commit must be deferred).  `should_cancel` is checked
    between rows, so a cancellation rolls back instead of leaving half a
    replace.  `on_progress` reports (phase, done, total) and is how a crash is
    simulated in tests.  A batch that was cancelled or failed can be committed
    again from the same preview; a committed one replays and changes nothing.
    """
    if not isinstance(plan, Preview):
        raise ImportProblem('Нужен Preview из preview().', code=CODE_FIELD)
    started = time.monotonic()
    stamp = time.time() if now is None else now
    existing = load_report(conn, plan.batch_id)
    if existing is not None and existing.state == 'committed':
        # F29 idempotency: the same preview committed twice is one batch.
        return replace(existing, replayed=True)
    if plan.needs_mapping:
        missing = ', '.join(plan.mapping_problem[0]) or ', '.join(plan.mapping_problem[1])
        raise ImportFormatError(f'Не сопоставлены колонки: {missing}.',
                                detail={'missing': list(plan.mapping_problem[0]),
                                        'ambiguous': list(plan.mapping_problem[1]),
                                        'suggestion': plan.mapping_suggestion.to_dict()
                                        if plan.mapping_suggestion else None})
    if busy is not None:
        reason = busy()
        if reason:
            raise ImportBusy(f'Коллекция занята: {reason}',
                             detail={'collection_id': plan.collection_id, 'reason': reason})
    current = collection_revision(conn, plan.collection_id)
    if current != plan.collection_revision:
        raise ImportRevisionConflict(
            f'Коллекция изменилась после предпросмотра (было {plan.collection_revision}, '
            f'стало {current}); повторите предпросмотр.',
            detail={'collection_id': plan.collection_id, 'expected': plan.collection_revision,
                    'current': current})
    if plan.mode == 'replace' and not plan.keep and not allow_empty:
        raise ImportProblem('Импорт replace очистил бы коллекцию целиком; это не сделано.',
                            code=CODE_EMPTY,
                            detail={'collection_id': plan.collection_id,
                                    'rejected': len(plan.rejected)})
    if plan.rejected and not allow_partial:
        raise ImportPartialBlocked(
            f'В файле {len(plan.rejected)} непригодных строк; исправьте файл или '
            f'подтвердите импорт только пригодных.',
            detail={'rejected': len(plan.rejected), 'reasons': plan.reasons})
    before = _members(conn, plan.collection_id)
    if conn.in_transaction:
        raise ImportBusy('Соединение уже в транзакции; импорт требует её сам.',
                         detail={'collection_id': plan.collection_id})
    keep_ids = plan.keep_ids
    removed_ids = _sorted(before, set(before) - set(keep_ids)) if plan.mode == 'replace' else []
    total = len(plan.valid) + (1 if plan.mode == 'replace' else 0)
    try:
        conn.execute('BEGIN IMMEDIATE')
        for index, row in enumerate(plan.valid, 1):
            _check_cancel(should_cancel)
            _upsert_endpoint(conn, row, stamp)
            if on_progress:
                on_progress('endpoints', index, total)
        if plan.mode == 'replace':
            if set(removed_ids) != set(plan.removed_ids):
                raise ImportRevisionConflict(
                    'Состав коллекции изменился после предпросмотра; повторите его.',
                    detail={'collection_id': plan.collection_id, 'expected': len(plan.removed_ids),
                            'current': len(removed_ids)})
            _delete_absent(conn, plan.collection_id, keep_ids)
            if on_progress:
                on_progress('replace', total, total)
        origin = ORIGIN_IMPORT
        for index, row in enumerate(plan.valid, 1):
            _check_cancel(should_cancel)
            conn.execute('INSERT OR IGNORE INTO membership(collection_id, endpoint_id, added_at, origin)'
                       ' VALUES (?, ?, ?, ?)', (plan.collection_id, row.endpoint_id, stamp, origin))
            if on_progress:
                on_progress('membership', index, total)
        revision = current + 1
        report = _report(plan, 'committed', current, revision,
                         [before[identifier] for identifier in removed_ids], stamp, started)
        conn.execute('INSERT INTO import_batch(id, collection_id, created_at, state, report_json, revision)'
                   ' VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET state = excluded.state,'
                   ' report_json = excluded.report_json, revision = excluded.revision,'
                   ' created_at = excluded.created_at',
                   (plan.batch_id, plan.collection_id, report.created_at, report.state,
                    report.to_json(), revision))
        conn.commit()
        return report
    except ImportCancelled as exc:
        _rollback(conn, plan, 'cancelled', exc, stamp)
        raise
    except Exception as exc:
        _rollback(conn, plan, 'failed', exc, stamp)
        raise


def _report(plan: Preview, state: str, before: int, after: int, removed, stamp: float,
            started: float) -> ImportReport:
    counts = dict(plan.counts)
    counts['added'] = len(plan.added)
    counts['removed'] = len(removed)
    rejected = tuple({'line': row.line, 'reason': row.reason, 'sample': row.sample}
                     for row in plan.rejected)
    return ImportReport(
        batch_id=plan.batch_id, collection_id=plan.collection_id, mode=plan.mode,
        format=plan.format, state=state, revision_before=before, revision_after=after,
        counts=counts, reasons=plan.reasons, rejected=rejected, added=plan.added,
        removed=tuple(removed), source_name=plan.source_name, source_digest=plan.source_digest,
        public_only=plan.policy.public_only, created_at=stamp,
        duration_s=round(time.monotonic() - started, 6))


def _upsert_endpoint(conn: sqlite3.Connection, row: ImportRow, stamp: float) -> None:
    country_at = stamp if row.country else None
    conn.execute('''INSERT INTO endpoints(id, canonical, host, port, scheme, ip_version, country,
                     country_source, country_at, first_seen_at, last_seen_at)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                  ON CONFLICT(canonical) DO UPDATE SET last_seen_at = excluded.last_seen_at,
                      country = COALESCE(excluded.country, endpoints.country),
                      country_at = COALESCE(excluded.country_at, endpoints.country_at),
                      country_source = COALESCE(excluded.country_source, endpoints.country_source)''',
               (row.endpoint_id, row.canonical, row.host, row.port, row.scheme, row.ip_version,
                row.country, 'import' if row.country else None, country_at, stamp, stamp))


def _delete_absent(conn: sqlite3.Connection, collection_id: str, keep: list) -> None:
    if not keep:
        conn.execute('DELETE FROM membership WHERE collection_id = ?', (collection_id,))
        return
    for start in range(0, len(keep), DELETE_CHUNK):
        chunk = keep[start:start + DELETE_CHUNK]
        placeholders = ','.join('?' * len(chunk))
        conn.execute(f'DELETE FROM membership WHERE collection_id = ? '
                   f'AND endpoint_id NOT IN ({placeholders})', (collection_id, *chunk))


def _check_cancel(should_cancel: Callable[[], bool] | None) -> None:
    if should_cancel is not None and should_cancel():
        raise ImportCancelled('Импорт отменён пользователем; коллекция не изменилась.')


def _rollback(conn: sqlite3.Connection, plan: Preview, state: str, exc: Exception, stamp: float) -> None:
    """Undo the whole batch, then record the attempt on its own."""
    try:
        conn.rollback()
    except sqlite3.Error:
        pass
    if conn.in_transaction:
        return
    note = f'{type(exc).__name__}: {exc}'[:200]
    try:
        conn.execute('INSERT INTO import_batch(id, collection_id, created_at, state, report_json, revision)'
                   ' VALUES (?, ?, ?, ?, ?, ?)'
                   ' ON CONFLICT(id) DO UPDATE SET state = excluded.state,'
                   ' report_json = excluded.report_json, created_at = excluded.created_at',
                   (plan.batch_id, plan.collection_id, stamp, state,
                    json.dumps({'mode': plan.mode, 'format': plan.format, 'state': state,
                                'counts': plan.counts, 'reasons': plan.reasons, 'added': [],
                                'removed': [], 'rejected': [],
                                'source': {'name': plan.source_name, 'digest': plan.source_digest},
                                'policy': {'public_only': plan.policy.public_only},
                                'revision_before': plan.collection_revision, 'revision_after': 0,
                                'duration_s': 0.0, 'note': note}, ensure_ascii=False),
                    plan.collection_revision))
        conn.commit()
    except sqlite3.Error:
        pass
