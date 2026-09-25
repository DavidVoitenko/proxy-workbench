# Handoff: secrets

**Требования:** F04 (полностью в части модуля), §1.2(1) CONTRACTS, §3.3 миграция 3, §5.1 (`upstream_credential`), §5.4 (коды `E_SECRET_*`), приёмка MASTER-PROMPT §7 п.7 и п.20 (canary secret не просачивается).
**Контракт:** `docs/integration/CONTRACTS.ru.md` версия 1, раздел 1, 3, 5.
**База:** ветка `integration/ultra-2026-09-25`, `db.py` уже в дереве (миграции 0..14, `SCHEMA_VERSION = 14`); тесты работают через `db.open_db`.
**Мои файлы:** `proxy_workbench/secrets.py` (новый), `tests/test_secrets.py`, `tests/test_secrets_verifier.py`, `tests/test_secrets_vault.py`, `tests/test_secrets_access.py`, `tests/test_secrets_endpoint.py`, `tests/test_secrets_policy.py`, `tests/test_secrets_redaction.py`.

---

## 1. Прошу внести в чужие файлы

### 1.1 Интегратор — `proxy_workbench/proxytool.py`

| Место | Что прошу | Почему |
| --- | --- | --- |
| `_normalize_proxy` (`proxytool.py:319`) и `normalize_custom_list` (`proxytool.py:365-383`) | Перед нормализацией пропускать строку импорта через `secrets.split_userinfo(value)`: он возвращает `(bare_url, username, password)` без userinfo, а `bare_url` уходит в существующий `normalize_custom`. Пароль при этом не попадает ни в нормализатор, ни в лог, ни в provenance. | F04 и `07-next-ultra-workflow-prompt.ru.md:80`: у collections нет коллекции для credentialed-адресов, пока userinfo не отделён до нормализации. Сейчас `proxytool.py:319` отвергает userinfo, и весь путь «свой прокси с паролем» обрывается на импорте. |
| `_canonicalize_host` (внутри `_normalize_proxy`, `proxytool.py:340-345`) | Добавить IDNA-кодирование не-ASCII имён: сейчас только `host.rstrip('.').lower()`, а `secrets.parse_endpoint` возвращает punycode (`пример.рф` → `xn--e1afmkfd.xn--p1ai`). | F04 «hostname/IDNA». Сейчас `parse_endpoint` и `normalize_custom` разойдутся для не-ASCII имён; мой тест `tests/test_secrets_endpoint.py::SharedNormalizerTests` сравнивает их на общем наборе и это зафиксирует. |
| `scan` (`proxytool.py:1283-1506`) и `store` (`proxytool.py:1380-1391`) | Ключ измерения — `(profile_id, profile_revision, access_id, access_revision, endpoint_id, job_id)` (миграция 13). Перед записью строки вызывать `secrets.is_superseded(access, access_revision)`; при True — не наследовать прошлый вердикт, а писать новую запись без доказательства. | §1.2(1) и §3.3: без этого «смена пароля отзывает старое доказательство» невыполнимо, потому что смена меняет только номер ревизии. |
| `export` (`proxytool.py:1651-1932`) и `api.py` `public_row` (`api.py:51-89`) | Поле доступа в строке формировать только через `secrets.describe(access, state=...)`. Userinfo в строку не пишется никогда; value появляется исключительно в `export.secret`-артефакте, для которого `secrets.Endpoint.url(resolved)` — единственная функция, дающая такой URL. | §4.4 «Credentials в эту строку не попадают никогда», §5.2 (`export.secret` — отдельное право). |
| `main` (`proxytool.py:2273`), путь worker | Пароль не принимать в argv. Worker получает `--access <id> --ref <secret_ref> --access-revision <n>` и сам берёт значение через `AccessStore.resolve()`. | MASTER-PROMPT F04 и §7.20: argv виден в `ps` и попадает в диагностический bundle. |

### 1.2 Владелец `core.py` — admission

`core.admit` получает `access` отдельным аргументом (это уже в контракте, §2.3) и обязан вызывать `secrets.is_superseded(access, access_revision)`; актуальная пара — `secrets.admission_key(access, access_revision)`. Ничего в `core.py` я не прошу менять по существу: функция уже есть и протестирована, нужна только точка вызова.

### 1.3 Владелец `db.py` — schema

Изменений DDL не требуется: миграция 3 (`db.py:638-648`) создаёт ровно те семь колонок, которые пишет `AccessStore`, и `accesses.secret_ref` у вас уже прокомментирован как «reference resolved by secrets.py». `db.secret_bindings` (`db.py:1495`) и `db.rebind_secrets` (`db.py:1495+`) с моим модулем совместимы, что закреплено тестами `tests/test_secrets_access.py::RebindContractTests`.

Одна просьба (не блокер): `db.rebind_secrets` меняет `secret_ref` в обход staging-протокола. После rebind на entry с чужой `revision` `store.resolve()` честно отказывает с `E_CONFLICT_REVISION`, пока не выполнен `reconcile()`. Если хотите, чтобы rebind был самодостаточным, добавьте в него проверку «новая ссылка обязана нести текущий `access_revision`» — это одна строка, но это ваш файл.

### 1.4 Владелец `desktop.py` — поставка

1. `pyproject.toml`: добавить необязательный extra `keyring` для OS-vault. **Проверено в этой сессии:** `.venv/bin/python -c "import keyring"` → `ModuleNotFoundError`, то есть OS-vault в этой сборке недоступен и `open_vault()` возвращает session-адаптер. macOS-путь через `security` я сознательно **не** реализовывал: `security add-generic-password -w <пароль>` передаёт пароль в argv, а F04 это запрещает.
2. `maintenance.RUNTIME_FILES` (`maintenance.py:14-32`): **добавлять нечего.** Модуль не создаёт ни одного файла с секретами — vault либо в OS-хранилище, либо в памяти процесса (`Vault.storage_path is None`, проверяется тестом `tests/test_secrets_vault.py::MemoryVaultTests::test_vault_has_no_file_backing`).

### 1.5 Поверхность `web` — `gui.py`, `ui/*`

Пароль из формы не должен попадать в `gui-job.json`, `gui-settings.json`, прогресс-файл и диагностический bundle. Правило простое: в этих структурах — `secrets.describe(access, state=...)`, то есть id/ревизия/ссылка. Правки в `ui/*` не прошу — визуальный язык не трогаю.

---

## 2. Что уже сделано у меня (публичный API `proxy_workbench.secrets`)

```python
# схема адреса на пути доступа
split_userinfo(value) -> (bare_url, username, password) | None   # userinfo снимается ДО нормализатора
parse_endpoint(value, *, normalizer=None) -> Endpoint            # hostname, IDNA, IPv4, IPv6
Endpoint.canonical / .authority / .url(resolved=None)            # без userinfo по умолчанию
auth_mode_for(scheme, *, username, password) -> 'none'|'http_basic'|'socks5'
check_mode_scheme(mode, scheme) -> mode
requires_auth(scheme, state) -> bool
classify_upstream(status, *, state, credentials_sent, detail) -> SecretError | None

# хранилища
open_vault(preference='auto'|'os'|'session', *, service, session_id, lifetime_s) -> Vault
Vault.stage/mark_ready/get/state/delete/refs/lock/unlock          # storage_path is None всегда
OsVault           # keyring, когда зависимость установлена; иначе open_vault('os') -> E_SECRET_VAULT_UNAVAILABLE
SessionVault      # память процесса + lifetime; в другом процессе claim() -> E_SECRET_VAULT_LOCKED
MemoryVault       # базовый, без блокировки; используется и в тестах
new_reference() -> 'sec_<32 hex>'

# проверка пароля
make_verifier(secret, *, salt=None, iterations=120_000) -> 'pbkdf2_sha256$<iters>$<salt>$<hash>'
verify_secret(candidate, verifier) -> bool                        # hmac.compare_digest, без исключений

# доступ и ревизия
AccessStore(conn, vault, *, now=None, id_factory=None)
  .create(endpoint_id, scheme, *, username, password, mode, access_id) -> Access
  .get(access_id) / .require / .list_for_endpoint(endpoint_id)
  .state(access) -> 'ready'|'staged'|'missing'|'locked'|'no_ref'
  .usable(access) -> bool
  .find_by_username(endpoint_id, username) -> Access | None       # выбор при конфликте импорта
  .rotate(access_id, *, password=None, username=None, expect_revision=None) -> Access
  .purge(access_id) -> bool
  .resolve(access_id, *, access_revision=None) -> ResolvedAccess   # контекст-менеджер, .scrub()
  .verify_password(access_id, candidate, *, access_revision=None) -> bool
  .reconcile(*, now=None) -> ReconciliationReport
admission_key(access, access_revision) -> (access_id, revision)
is_superseded(access, access_revision) -> bool

# политика назначения
DestinationPolicy.public() / .trusted_private(allow_private_networks, allowed_hosts, allow_loopback, allow_link_local)
  .check_address / .check_endpoint / .check_resolved(host, addresses) -> Decision
authorize_endpoint(policy, endpoint, *, resolver=None, resolved=()) -> Decision

# редактирование выдачи
describe(access, *, state=None) -> dict
scrub_mapping(mapping) -> dict
redact_text(text, secrets) / redact_proxy_url(url, secrets=()) / log_fields(access, *, state, **extra)
```

Ключевые решения, которые видны только из кода:

* **Ссылка не меняется при ротации.** `secret_ref` стабилен, меняется `access_revision` и содержимое vault-entry. Старая ревизия опознаётся по номеру — поэтому «новый пароль не наследует успешную проверку старого» выполняется структурно, а не проверкой в потребителе (`tests/test_secrets_access.py::RotationTests::test_a_new_password_does_not_inherit_the_old_evidence`).
* **Staging-протокол.** `stage → commit → finalize`. Отказ на БД компенсируется удалением staged-записи; отказ на finalize оставляет не-`ready` доступ, который чинит `reconcile()` (идемпотентен, при запертом vault ничего не чинит и это рапортует). Orphan-ref, на который не ссылается ни одна строка, удаляется.
* **`previous_verifier`** в payload позволяет отличить «пароль отвергнут» от «пароль сменился», не храня старый пароль.
* **`split_userinfo` вместо второго нормализатора.** `parse_endpoint(..., normalizer=proxytool.normalize_custom)` делегирует канонизацию общему нормализатору; без него модуль падает назад на собственную строгую канонизацию, и на общих адресах результаты обязаны совпадать (тест `SharedNormalizerTests`).

---

## 3. Совместимость

* **Ничего не ломается:** новый файл, ни один существующий модуль не импортируется и не меняется. `import proxy_workbench.secrets` не имеет побочных эффектов (сеть, файлы, keychain не трогаются).
* **DDL не пишется.** Работа идёт только с таблицей, которую создаёт `db.py`; набор колонок закреплён тестом `SchemaContractTests::test_accesses_has_exactly_the_contract_columns`.
* **Что не сделано и не будет сделано без заказа:** автоматический NTLM/Kerberos/Digest, SOCKS4-password, регистрация у провайдеров, выдача credentials в экспорте (это отдельное право `export.secret`, §5.2), запись секретов в settings/argv/временный файл.
* **Известное ограничение:** строку в Python нельзя обнулить, поэтому `ResolvedAccess` короткоживущий и имеет `scrub()`/контекст-менеджер; сам секрет остаётся в vault. Это отмечено в docstring `ResolvedAccess.scrub`.

---

## 4. Проверки

Запущено в этой сессии из корня репозитория:

```
.venv/bin/python -m unittest tests.test_secrets
→ Ran 138 tests in 0.906s — OK

.venv/bin/python -m unittest tests.test_secrets_verifier   → Ran 10  OK
.venv/bin/python -m unittest tests.test_secrets_vault      → Ran 23  OK
.venv/bin/python -m unittest tests.test_secrets_access     → Ran 49  OK
.venv/bin/python -m unittest tests.test_secrets_endpoint   → Ran 25  OK
.venv/bin/python -m unittest tests.test_secrets_policy     → Ran 20  OK
.venv/bin/python -m unittest tests.test_secrets_redaction  → Ran 11  OK
```

Что эти тесты доказывают (не «структуру», а поведение):

| Сценарий F04 | Тест |
| --- | --- |
| Секрет не в БД | `test_secrets_access.SchemaContractTests::test_the_database_file_never_holds_a_password` — читает байты `db.sqlite3*` после create + rotate; `test_secrets_redaction.CanaryLeakTests::test_the_canary_is_absent_from_every_file_the_run_produces` |
| Секрет не в JSON/логах | `CanaryLeakTests::test_nothing_the_module_produces_contains_the_canary` (describe, reconcile, log_fields, строка таблицы, error.describe) |
| Секрет не в argv | `CanaryLeakTests::test_the_worker_needs_a_reference_and_a_revision_but_not_a_password`, `test_secrets_vault.SessionVaultTests::test_a_password_is_never_needed_to_reach_the_session` |
| Два credentials одного endpoint не сливаются | `CreateTests::test_two_credentials_of_one_endpoint_are_two_accesses` |
| Смена пароля отзывает старое доказательство | `RotationTests::test_a_new_password_does_not_inherit_the_old_evidence` |
| Locked vault отличим | `LockedVaultTests` (3 теста), `test_secrets_vault.MemoryVaultTests::test_locked_vault_refuses_every_operation`, `OsVaultTests::test_locked_keychain_maps_to_the_vault_locked_code` |
| Auth errors различимы | `test_secrets_endpoint.UpstreamClassificationTests` — 407 без отправленного пароля → `E_SECRET_UPSTREAM_AUTH_REQUIRED`, с паролем → `E_SECRET_UPSTREAM_AUTH_FAILED` |
| Vault + SQLite не одна транзакция | `StagingProtocolTests` — 10 тестов: компенсация на БД, staged после отказа finalize, восстановление старого пароля при отказе ротации, orphan-ref, исчезнувший ref, идемпотентность, запертый vault ничего не чинит |
| Hostname/IDNA/IPv4/IPv6, HTTP Basic и SOCKS5 | `test_secrets_endpoint` (25 тестов), включая эквивалентность `proxytool.normalize_custom` |
| Trusted private с destination policy, public остаётся публичным | `test_secrets_policy` (20 тестов), включая блокировку DNS-fallback на loopback/link-local **до** передачи секрета (`AuthorizeBeforeCredentialsTests`) |
| Сессионный секрет не переживает процесс | `test_secrets_vault.SessionVaultTests::test_another_process_cannot_claim_a_session_secret` — реальный `subprocess` получает только session id и получает `E_SECRET_VAULT_LOCKED` |

Не выполнено (и не заявляется): полный `unittest discover -s tests` (по условию задачи его гоняют другие), Windows/macOS-специфика keychain вживую (на этой машине `keyring` не установлен, OS-путь покрыт стабом), реальный сетевой прогон через gateway/checker (вне зоны этого модуля).

---

## 5. Открытые вопросы

1. **Новый код `E_SECRET_VAULT_UNAVAILABLE`.** В CONTRACTS §5.4 в домене SECRET перечислены четыре кода, и «vault недоступен в этой установке» среди них нет. Я ввёл один код и не стал зашивать его в `E_SECRET_NOT_PROVIDED`, потому что это разные ситуации и разные действия. Прошу владельца контракта либо добавить его в §5.4, либо назвать канон.
2. **Кто владеет вызовом `secrets.open_vault()`.** Модуль ничего не создаёт при импорте; выбор хранилища (`auto`/`os`/`session`) — решение GUI/CLI. Прошу интегратора решить, где это происходит один раз за процесс, и передать `session_id` в worker, если сессионный режим выбран.
3. **Политика trusted private и коллекции.** `DestinationPolicy.trusted_private()` — это моя модель; `db.collections.kind` (`db.py:1557`, значения `private`/`public`) я не читал как источник истины. Нужно решение владельца `db.py`/интегратора, какой `kind` коллекции соответствует `trusted_private` и где хранится `allow_private_networks`/`allow_loopback`/`allow_link_local`/`allowed_hosts`: в схеме для этого колонок нет, а править `§3.3` — это бамп версии контракта.
4. **Windows OS-vault.** На Windows нет stdlib-пути прочитать пароль обратно; без `keyring` там будет session-адаптер. Если нужен Credential Manager — это отдельная задача, здесь не сделано.
