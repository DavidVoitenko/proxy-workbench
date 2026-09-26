# area-secrets.md — F04 и F29 (ключи): что доказано, что сломано, что нужно чужому файлу

Ветка `integration/ultra-2026-09-25`. Владелец: `proxy_workbench/secrets.py`,
`proxy_workbench/apikeys.py`. Всё, что ниже помечено «нужен чужой файл»,
**не сделано** — сделано всё, что было возможно сделать из двух своих файлов.
Чужие файлы не правились: `proxytool.py`, `importer.py`, `gui.py`, `api.py`,
`apiv1.py`, `db.py`, `core.py`, `gateway.py`, `pipeline.py`, `exportsvc.py`
остались нетронутыми.

По этим же пунктам уже есть `fix-keys.md` (ключи), `secrets.md` (F04),
`importer.md`, `gateway.ru.md`, `pipeline.md`. Этот документ их не пересказывает,
а заменяет факты на freshly измеренные и добавляет то, чего в них нет.

## Короткая сводка

| Пункт | Состояние | Раздел |
|---|---|---|
| Canary-секрет отсутствует в БД verifiers, логах, argv, файлах, JSON, OpenAPI | **сделано, проверено буквально** | 3 |
| F04: import → worker check → gateway → export, полный путь | **сломано: шаг 1 отказывает у двери** | 1 |
| Свои hostname / IDNA / IPv4 / IPv6 адреса | сделано в `secrets.py`, не подключено | 1.3, 1.5 |
| Два разных пароля одного endpoint не сливаются | **сделано, проверено** | 2.1 |
| Смена пароля отзывает старое доказательство | **сделано, проверено** | 2.2 |
| OS vault / session-only, locked vault, компенсация, reconciliation | **сделано, проверено** | 2.3 |
| Секреты не идут через argv / settings / plaintext-файл / обычный API | **сделано, проверено** | 2.4 |
| Trusted private endpoints + destination policy | сделано в `secrets.py`, не подключено | 1.5 |
| NTLM/Kerberos и provider signup не появились | **подтверждено отсутствие** | 4 |
| (а) `purge_expired_grace` вызывается из рабочего пути | **закрыто, проверено** | 2.5 |
| (б) `rebind_secrets` поднимает `access_revision` | **закрыто, проверено** | 2.2 |
| (в) повторный `POST /v1/keys` отдаёт секрет дважды | **подтверждено; API готов, нужна 1 строка в `apiv1.py`** | 2.7 |
| F29: секрет ровно один раз, энтропия, verifier, права, квоты, SSE, audit | **сделано, кроме (в)** | 5 |
| Дефект 4 (неизвестное время / clock rollback) | вне моих файлов, не трогал | 6 |
| Дефект 10 (hostname/private/auth сквозной путь) | **тот же корень, что F04** | 1 |
| Дефект 18 (разные secrets у gateway/GUI/API) | разные — проверено; LAN — не мой файл | 2.4 |

---

## 1. F04, полный путь — СЛОМАНО на первом шаге

F04 требует: «импорт → worker check → scoped gateway → поддерживаемый
explicit-secret export работают». Живой прогон на настоящей БД (`db.open_db` на
временном файле, `importer.preview`, обе существующие политики):

```
policy=DEFAULT_POLICY (public_only=True)
  hostname + auth (http)   -> rejected  E_IMPORT_CREDENTIALS  sample=http://***@proxy.example.com:8080
  hostname, no auth        -> rejected  E_IMPORT_HOSTNAME      sample=http://proxy.example.com:8080
  IDNA hostname            -> rejected  E_IMPORT_FORMAT        sample=http://пример.рф:8080
  IPv4 literal             -> rejected  E_IMPORT_PRIVATE       sample=http://198.51.100.7:8080
  IPv6 literal             -> rejected  E_IMPORT_PRIVATE       sample=http://[2001:db8::1]:8080
  socks5 + auth            -> rejected  E_IMPORT_CREDENTIALS  sample=socks5://***@proxy.example.com:1080
  private 10.x             -> rejected  E_IMPORT_PRIVATE       sample=http://10.0.0.5:8080
  loopback                 -> rejected  E_IMPORT_PRIVATE       sample=http://127.0.0.1:8080
  link-local metadata      -> rejected  E_IMPORT_PRIVATE       sample=http://169.254.169.254:8080
  socks4 + auth            -> rejected  E_IMPORT_CREDENTIALS  sample=socks4://***@proxy.example.com:1080

policy=EndpointPolicy(public_only=False)   -- своя коллекция
  hostname, no auth        -> valid
  IPv4 / IPv6 literal      -> valid
  private 10.x / loopback  -> valid
  link-local metadata      -> valid      <-- 169.254.169.254 проходит
  IDNA hostname            -> rejected  E_IMPORT_FORMAT
  ЛЮБАЯ строка с логином   -> rejected  E_IMPORT_CREDENTIALS
```

Итог: **trusted private работает для адреса без credentials** — это единственный
режим, где что-то импортируется вообще. Строка с credentials отклоняется **во
всех** режимах, включая явный `public_only=False`. Первая же буква F04 — «импорт
с auth-прокси» — упирается в дверь.

### 1.1 Отказ корректен по дефекту 10, но не по F04

Дефект 10: «Hostname/private/auth input поддержан сквозным путём **либо**
отклонён сразу; нельзя принимать форму и потом молча выбрасывать записи».
Импортёр отклоняет сразу и показывает редактированный образец — **эта половина
дефекта 10 выполнена**. Вторая половина (сквозной путь) не выполнена. Это не
противоречие, а два разных требования: одно соблюдено за счёт отказа.

Комментарий в коде, который это фиксирует, устарел — он и есть суть задачи:

```python
# proxy_workbench/importer.py:480-483, функция _classify
if _has_credentials(value):
    # F04 owns upstream credentials and there is no secret store yet, so the
    # only honest answer today is an immediate, uniform refusal.
    return None, ROW_CREDENTIALS
```

«there is no secret store yet» больше не верно: `secrets.py` есть и держит всё
нужное. Менять импортёр нужно сейчас.

### 1.2 Что нужно в `importer.py` — построчно

Две точки отказа, обе в `proxy_workbench/importer.py`.

**(а) `_classify`, строки 480-483.** Вместо отказа раз peel-ить userinfo:

```python
def _classify(value: str, policy: EndpointPolicy):
    split = secrets.split_userinfo(value)          # из proxy_workbench.secrets
    if split is not None:
        bare, username, password = split
        # Канал доставки credential-а: колбэк, который пишет в vault при commit.
        # Preview обязан его сохранить, иначе пароль теряется безвозвратно.
        return _parts_or_raise(bare, policy), (username, password)
    if _has_credentials(value):                     # не разобрали как URL
        return None, ROW_CREDENTIALS                # оставить как fallback
    ...
```

Ключевое требование: **preview ничего не меняет** (F03), но и не теряет пароль.
Значит credential из preview обязан либо доходить до вызывающего в памяти
(`ImportSource` уже умеет не писать текст на диск — держит `_text` в памяти и
отдаёт наружу только `name`/`digest`), либо превращаться в требование «введите
пароль при commit». Ни в том, ни в другом случае пароль не должен попасть в
отчёт, в `detail`, в `sample` и в provenance.

**(б) `_cell_row`, строки 648-652.** CSV/JSON с колонками credentials:

```python
if credentials or (host and _has_credentials(host)):
    return ImportRow(number, REJECTED, ROW_CREDENTIALS,
                     detail=(...), sample=sample)
```

Здесь `credentials` — уже найденные колонки (`CREDENTIAL_COLUMNS`, строка 98).
Их не надо отбрасывать, а надо передать в
`secrets.AccessStore.create(..., username=..., password=...)` на commit.
`ImportRow` уже несёт `state`/`reason`/`detail`/`sample`; нужен ещё один
закрытый признак — «у этой строки есть credential, он в vault» — чтобы commit
знал, какие строки требуют `AccessStore.create`, а какие нет.

**(в) `EndpointPolicy`** сейчас — один булев `public_only`. Для F04 нужен режим
trusted private из `secrets.DestinationPolicy`, у которого есть `allowed_hosts`,
`allow_loopback`, `allow_link_local` и проверка **каждого** адреса, куда
резолвится имя (DNS-rebind). Готовый объект уже есть:

```python
secrets.DestinationPolicy.trusted_private(allowed_hosts=[...],
                                          allow_private_networks=True,
                                          allow_loopback=False,    # loopback НЕ по умолчанию
                                          allow_link_local=False)  # 169.254.169.254 НЕ по умолчанию
```

`allow_link_local` по умолчанию `False` — это отдельная находка, а не опечатка:
в режиме `public_only=False` импортёр сейчас **пропускает 169.254.169.254**.
Адрес cloud-metadata не становится прокси-адресом оттого, что коллекция
доверенная. `secrets.DestinationPolicy` это уже закрывает (`REASON_NOT_PUBLIC`),
и `tests/test_areasecrets_path.py` это фиксирует
(`test_loopback_and_link_local_are_a_second_explicit_opt_in`).

### 1.3 Что нужно в `gateway.py` — построчно

`secrets.py` теперь умеет построить всё, что нужно транспорту (1.4), но **ни
один вызывающий этого не делает**. Проверено поиском по пакету: `secretstore`
встречается только в `proxytool.py` (2387, 3076-3080, 3904), `api.py` (2566),
`desktop.py` (2902). `gateway.py`, `pipeline.py`, `exportsvc.py` — ноль
упоминаний.

Диалог с upstream в `gateway.py` (функция подключения, строки 826-895) всегда
идёт без credentials:

* HTTP/HTTPS CONNECT, строка 849: `CONNECT {target} HTTP/1.1\r\nHost: ...` —
  заголовка `Proxy-Authorization` нет;
* SOCKS5, строка 870: `writer.write(b'\x05\x01\x00')` — предлагается только
  метод 0x00, поэтому SOCKS5 с логином всегда даёт `UpstreamError('SOCKS5_AUTH')`
  (строка 873);
* ветка 407 (строки 1128-1132) честно помечает upstream как `upstream_refused`.

Минимальная правка, подготовленная с моей стороны:

```python
creds = store.transport_credentials(access, store.resolve(access.id), scheme=scheme)
# HTTP CONNECT / forward:
if creds.proxy_authorization:
    writer.write(('CONNECT %s HTTP/1.1\r\nHost: %s\r\nProxy-Authorization: %s\r\n\r\n'
                  % (target, target, creds.proxy_authorization)).encode())
# SOCKS5:
writer.write(creds.socks5_greeting)          # 05 01 02 при наличии логина
...
if creds.socks5_auth:
    writer.write(creds.socks5_auth)           # RFC 1929
```

**Это сознательное проектное решение, которое надо подтвердить, а не ускорить.**
F04 говорит «Vault+SQLite не одна транзакция» и «секреты не через обычные
настройки», а F28 говорит, что экспорт по умолчанию даёт
redacted/gateway-reference. Если gateway начнёт доставать credential из vault на
каждый CONNECT, то (а) у долгоживущего соединения в руках окажется пароль и
(b) любой 407 станет «у нас есть пароль, он не подошёл» вместо «upstream просит
auth». Второе — утечка по side channel через health-статистику пула.

Поэтому рекомендация: **gateway остаётся credential-free**, а credentialed
endpoint в нём честно rest-ится уже существующим `upstream_refused`, и
поддержанный путь для credentialed-прокси — explicit-secret export с правом
`export.secret`, который уже существует (`api.py:1050-1055`,
`tests/test_exportsvc_secrets_export.py`). Если владелец `gateway.py` решит
иначе, контракт `transport_credentials` к этому готов и покрыт тестами.

### 1.4 Что нужно в `pipeline.py` — построчно

Worker check сейчас не умеет аутентифицировать upstream (ни одного обращения к
`AccessStore`) и не умеет классифицировать ответ: `secrets.classify_upstream`
(407 без credentials / 407 с credentials / locked store / `staged` / `missing`)
не вызывается ни разу за весь пакет. Подключение:

```python
from . import secrets as secretstore
try:
    with store.resolve(access_id) as resolved:
        creds = store.transport_credentials(access, resolved, scheme=scheme)
        ...                       # отдать creds транспорту
except secretstore.SecretError as exc:
    return exc.describe()        # {code, message, action, detail} — уже редактировано
```

`secretstore.SecretError` уже экспортируется наружу как
`proxytool.secrets_error()` (`proxytool.py:3903-3905`), так что импорт ничего
нового не вводит.

### 1.5 Что добавлено в `secrets.py` для этого пути

Раньше модуль умел сказать «этот доступ — HTTP Basic» и «этот доступ — SOCKS5»,
но **не умел произвести ни байта** для обоих механизмов, поэтому путь не мог
дозавершить никто. Добавлено четыре чистые функции и один объект:

| Что | Что делает |
|---|---|
| `proxy_authorization(username, password)` | значение `Proxy-Authorization: Basic ...` по RFC 7617; для открытого прокси — пустая строка вместо заголовка с пустым credential |
| `socks5_greeting(*, with_auth)` | приветствие SOCKS5, предлагающее **только** нужный метод (0x00 или 0x02); оба сразу предлагать нельзя — прокси выберет слабый |
| `socks5_username_password(username, password)` | субсогласование RFC 1929; отказ, если поле не влезает в 1..255 байт |
| `transport_credentials(access, resolved, *, scheme=None)` | единственная разрешённая точка «resolved access → то, что можно отправить в сокет»; проверяет, что секрет от того же `access_id` и той же `access_revision` |
| `TransportCredentials` | результат: `mode`, `scheme`, `access_id`, `access_revision`, `proxy_authorization`, `socks5_greeting`, `socks5_auth`; `as_json()` **не** несёт ни логина, ни пароля |

Живой вывод:

```
HTTP Basic header    : Basic YWxpY2U6Q0FOQVJZLXB3LTdmMmE=   -> alice:CANARY-pw-7f2a
open proxy           : ''
SOCKS5 greeting auth : 050102        SOCKS5 greeting none: 050100
SOCKS5 auth bytes    : 0105616c6963650e43414e4152592d70772d37663261
socks5 identity to an http proxy   -> refused E_VALIDATION_FIELD
http_basic identity to socks5      -> refused E_VALIDATION_FIELD
unsupported scheme (ntlm)          -> refused E_VALIDATION_UNKNOWN / UNSUPPORTED
oversized SOCKS5 field             -> refused E_VALIDATION_FIELD
resolved value of another access   -> refused E_CONFLICT_REVISION
stale resolved revision            -> refused E_CONFLICT_REVISION
```

Запрещённые механизмы — по-прежнему отказ, а не игнор: `socks4` с логином →
`SecretUnsupportedError`, `ntlm`/`kerberos`/`gssapi` → `SecretUnsupportedError`
по имени схемы.

---

## 2. Что сделано и проверено в моих двух файлах

### 2.1 Два разных пароля одного endpoint физически различимы

`tests/test_areasecrets_path.py::TwoCredentialsOneEndpointTests`:

```
created access acc-1 | revision 1 | ref sec_fc3404030bfb29996afc5fe6d85d0f7d
resolve() gives the old canary back: True
access acc-1 / acc-2: разные id, разные secret_ref, 2 строки в accesses
verify_password('acc-a', CANARY)      -> True
verify_password('acc-a', CANARY_2)    -> False
verify_password('acc-b', CANARY_2)    -> True
verify_password('acc-b', CANARY)      -> False
canary scan of the on-disk database: old=False new=False
```

Два доступа — две строки `accesses` (никогда не перезапись), два `secret_ref`,
два verifier с разной солью. `find_by_username` предлагает конфликт выбора
(крутить этот доступ или создать второй), а не перезаписывает молча и не
дублирует.

### 2.2 Смена credentials отзыва старое доказательство

Живой прогон восстановления, где доступ указывает на **другую** запись vault
с новым паролем (сценарий rebind):

```
rebind report.revisions: (('acc-1', 1, 2, ...),)
access revision after rebind: 2 (было 1)
access secret_ref moved      : True

resolve(access_revision=1) -> E_CONFLICT_REVISION | access acc-1 is at revision 2, revision 1 was superseded
old canary as current password   : False
new canary as current password   : True
old canary on a stale revision  : False
resolve() now returns           : NEW canary

reconcile(): removed_orphan_refs=[старый ref], clean=False, resolve() после reconcile работает

core admission (network_id присутствует):
  результат, измеренный с revision 1, доступ теперь на 2:
      ('E_CONFLICT_ACCESS_REVISION', {'access_revision': 1, 'required': 2})
  результат, измеренный с revision 2:
      None   (допускается)
```

**Новый пароль не наследует успешную проверку старого** — и по
`AccessStore.resolve`, и по `core._identity_reason` (`core.py:452`), который и
есть тот admission-гейт, который читает `core.py`.

### 2.3 Vault и SQLite — не одна транзакция

`tests/test_areasecrets_path.py::VaultAndDatabaseAreNotOneTransactionTests`:

* упавший `INSERT INTO accesses` → staged-ссылка удалена, `vault.refs() == []`,
  строк нет (компенсация в `AccessStore._insert`);
* сирота в vault, на который нет строки → `reconcile()` убирает, реальный
  credential не трогает;
* строка, оставшаяся `staged` → `reconcile()` доводит до `ready`, второй
  вызов `clean`;
* строка, чья ссылка пропала → попадает в `missing_refs`, **не выдумывается**;
* `reconcile()` при locked vault возвращает `locked_refs` и не трогает vault.

### 2.4 Секреты не идут через argv, settings, plaintext-файл и обычный API

| Путь | Чем проверено | Результат |
|---|---|---|
| argv | `grep add_argument` по `proxytool.py` | ни одного параметра для пароля/credential прокси; есть только `--admin-token` — это API-ключ, и у него есть env-вариант `PROXY_WORKBENCH_ADMIN_KEY` |
| обычные settings | `SessionVault` держит `_SESSIONS` в памяти процесса, `OsVault` пишет в keychain, `open_vault('os')` **не** деградирует молча | подтверждено |
| временный plaintext-файл | у `MemoryVault`/`SessionVault` нет `storage_path`; `OsVault` намеренно не использует CLI `security`, потому что он принимает пароль аргументом — это и есть утечка в argv, которую F04 запрещает | подтверждено |
| обычная выдача API | `describe()` возвращает `access_id` + непрозрачный `secret_ref`; `redact_text` / `redact_proxy_url` / `scrub_mapping` вычищают userinfo из сообщений и словарей | подтверждено, тесты в `test_secrets_redaction.py` и новые в `test_areasecrets_path.py` |
| дефект 18 (разные secrets) | `api_key` (apikeys), `gui_session` (token GUI), `gateway_password` (token gateway), `upstream_credential` (vault) — четыре независимых значения в четырёх независимых местах | разные; LAN-включение — не мой файл |

### 2.5 (а) `purge_expired_grace` вызывается из рабочего пути — ЗАКРЫТО

Живой прогон с фейковыми часами:

```
after rotate:                             previous_verifier set? True  | grace_until 1700000120.0
after ONE authenticate past the window:   previous_verifier set? False | grace_until None
after a later authenticate:               previous_verifier set? False
after explicit purge_expired_grace:       previous_verifier set? False | rows purged 0
```

Первый же `authenticate` после закрытия окна убирает superseded verifier;
явный вызов потом возвращает 0, то есть идемпотентен и ничего не делает.
`_sweep_grace` троттлится (`grace_sweep_interval_s`), поэтому хеш может
пережить закрытие окна максимум на 60 секунд — но `_match_previous` с этого
момента уже не отдаёт, то есть риска нет, только мёртвый вес. Тот же вызов
срабатывает в `assert_active` (то есть на новых lease и на recheck потока) и в
`rotate`/`revoke`.

### 2.6 (б) `rebind_secrets` поднимает `access_revision` — ЗАКРЫТО

См. 2.2: ревизия 1 → 2, старый результат отклоняется с
`E_CONFLICT_ACCESS_REVISION`, старый пароль не проходит. Замечание
`db.rebind_secrets` ссылается на `secrets.Coordinator.rotate` — класса
`Coordinator` в модуле нет, есть `AccessStore.rotate`. Это опечатка в чужом
docstring, на поведение не влияет; владельцу `db.py` стоит поправить текст.

### 2.7 (в) Повторный `POST /v1/keys` отдаёт секрет дважды — ПОДТВЕРЖДЕНО

Живой прогон настоящего `ApiV1` с настоящим `ApiKeyStore`:

```
1st POST /v1/keys  status 200 | secret: pwk_c530beb612bd_HXpFLqzDrTTqmUINvirN7KLRZEZvTM6yzgdaWNp_qSw
2nd POST /v1/keys  status 200 | secret: pwk_c530beb612bd_HXpFLqzDrTTqmUINvirN7KLRZEZvTM6yzgdaWNp_qSw
SAME SECRET DELIVERED TWICE: True

rotate #1 secret: pwk_ca7341fae9ea_wzYQinFebohbNSWAJlkPvy4mWAxHOGuOMGZAi0Tbhqo
rotate #2 secret: pwk_ca7341fae9ea_wzYQinFebohbNSWAJlkPvy4mWAxHOGuOMGZAi0Tbhqo
ROTATE SECRET REPLAYED: True

the cache holds raw response bytes containing the secret: True
rows in api_keys: 2  (одна строка на два ключа админа + один выданный, две выдачи)
```

Ключ при этом минтится один раз (идемпотентность для мутации работает) — но
**секрет** отдаётся повторно из кэша. `IdempotencyStore` хранит готовый
`Response` (frozen dataclass с `body: bytes`), то есть к моменту попадания в
кэш маркер `OneShotBody` уже потерян: остались байты с полем `pwk_...`.

#### Что сделано в `apikeys.py`

`apikeys.without_one_shot_response(response)` — копия ответа, пригодная для
хранения, из которой удалено значение, показываемое один раз. Работает по
**сериализованному** виду намеренно: маркер сохранять не нужно, достаточно
`response.body` — это то, что реально лежит в кэше. Дешёвый пред-фильтр
(`b'"secret"' in raw`) отсекает весь обычный трафик без разбора JSON.

Точность: снимается **только** поле, значение которого — выпущенный нами ключ
(префикс `pwk_`). JSON-ответ, у которого поле `secret` или `secrets` содержит
что-то другое (ссылку на vault, список ссылок), не трогается — иначе молча
сломался бы маршрут скачивания экспорта с `include_secrets`. Некорректный JSON
и не-JSON тело не переписываются. Ответ не-dataclass приходит с явной ошибкой,
а не молча возвращается в кэш как есть.

Живой вывод после применения правки (в памяти, `apiv1.py` на диске не тронут):

```
POST /v1/keys, тот же Idempotency-Key, С патчем:
  1st  status 200 | secret: pwk_95ec22eccf15_3-ytkFQXxI6ctisWYJ64M52dWs7rUMWFFTJ-QyUU0vI
  2nd  status 200 | secret: None
  second answer says it was already shown: True
  the same key id is still reported     : True
  rows in api_keys                     : 2

POST /v1/keys/{id}/rotate, тот же Idempotency-Key, С патчем:
  1st  secret: pwk_95ec22eccf15_6VR4uePimwVxo-vb_AK55FMkVbmxo91gWbfTvunH0mM
  2nd  secret: None | already_shown: True

nothing left in the cache:
  any pwk_ left in cached bytes: False
```

Идемпотентность сохранена: тот же `id`, тот же статус, столько же строк в базе.
Первый ответ по-прежнему отдаёт секрет — ровно один раз.

#### Точная правка в `apiv1.py` — одна строка

`proxy_workbench/apiv1.py`, метод `_invoke`, строка
`self.idempotency.put(bucket, idem_key, digest, response)` (на момент
проверки — 2044; файл правят другие владельцы, ищите по тексту):

```python
-            self.idempotency.put(bucket, idem_key, digest, response)
+            self.idempotency.put(bucket, idem_key, digest,
+                                 apikeys.without_one_shot_response(response))
```

и один импорт рядом с существующими (строка 52-53, `from .branding ...`,
`from .i18n import tr`):

```python
+from . import apikeys
```

Больше ничего не нужно: ни нового метода, ни `dataclasses.replace` в `apiv1.py`,
ни правки `IdempotencyStore.get`. Вариант из `fix-keys.md` (метод `_cacheable`
на `result` + `replace`) делает то же самое, но требует, чтобы маркер
`OneShotBody` дожил до `_invoke`; `without_one_shot_response` от маркера не
зависит, поэтому он устойчивее к тому, что `_redact`/`_guard_scope` копируют
словарь.

Тест, который это фиксирует и который **уже зелёный**:
`tests/test_areasecrets_keys.py::OneShotThroughTheApiTests`. Он подменяет
`apiv1.IdempotencyStore.put` обёрткой, вызывающей
`apikeys.without_one_shot_response`, и прогоняет настоящий конвейер `/v1`. Как
только `apiv1.py` примет вызов, тест останется зелёным и начнёт проверять
настоящий метод, а не обёртку. Там же лежит проверка, что в кэше после всего
не остаётся ни байта `pwk_`, и что кэш вообще не выключен.

Закрывает ли это `POST /v1/subscriptions`? Да: `subscriptions.create` идёт через
тот же `ApiKeyStore.create_key` и несёт тот же секрет, а правка стоит в
`_invoke`, то есть на уровне маршрута.

---

## 3. Canary-секрет: проверка буквально, а не на словах

Два разных canary, ни один не настоящий: `CANARY-ADMIN-4b7d2e-do-not-leak`
(выпущенный API-ключ) и `CANARY-PROXY-PASSWORD-do-not-leak` (credential прокси).
Прогон: bootstrap admin, десятки выдач, ротации, намеренные отказы, гонки по
`/v1/keys`, `/v1/events`, экспорты.

| Место | Как проверено | Результат |
|---|---|---|
| БД `api_keys`, verifiers как plaintext | байтовый поиск canary в файле `proxies.sqlite3` | admin secret: **False**, second secret: **False**; ни одна колонка не содержит `pwk_` |
| verifier как таковой | `SELECT verifier, verifier_salt, verifier_algo` | `pbkdf2_sha256$210000`, соль 22 символа, digest 43 символа; секрет не является префиксом verifier и не содержится в нём |
| audit log | `scope_json` / `object_id` LIKE `%pwk_%`, байтовый поиск `CANARY` | 0 строк; полного ключа, пароля и тела запроса нет |
| обычные файлы под data | `rglob('*')`, байтовый поиск по каждому файлу | **CLEAN** |
| обычный JSON под data | `rglob('*.json')` | ни одного `pwk_` |
| `proxy_workbench/openapi.json` | 342 962 байта, подсчёт `pwk_` и `CANARY` | `pwk_`: 0, `CANARY`: 0; единственное вхождение подстроки `secret": "` — это имя документированного поля `subscription_secret`, не значение |
| логи | конфигурация логирования в прогоне | файлов логов не создано; ни одна строка не содержит canary |
| argv | `add_argument` по `proxytool.py` | параметра для пароля/credential нет (см. 2.4) |
| credential прокси в БД | байтовый поиск обоих proxy-canary в файле SQLite после `rebind_secrets` | **old: False, new: False** |
| credential прокси в логах ошибок | `SecretError.describe()` на сообщении вида `rejected by http://alice:<canary>@host:8080` | canary отсутствует в `{code, message, action, detail}` |

Дополнительно: `tests/test_areasecrets_keys.py` и
`tests/test_areasecrets_path.py` проверяют canary в каждом тесте, который его
касается, — не «где-то в конце», а рядом с местом, где он мог утечь.

---

## 4. Чего НЕ должно было появиться — подтверждено отсутствие

```
grep -rni "ntlm|kerberos|gssapi|spnego|proxy-ntlm" proxy_workbench/ *.py *.json *.md
  -> единственное вхождение: комментарий secrets.py:35, что они вне scope
grep -rni "signup|sign_up|register_account|free_trial|api.register|create_account" proxy_workbench/
  -> пусто
```

То есть автоматический NTLM/Kerberos и provider signup не появились ни в коде,
ни в схеме, ни в примерах. `auth_mode_for` отказывает по имени схемы, а не
игнорирует, и `socks4` с логином — тоже явный отказ.

---

## 5. F29, менеджер ключей — что проверено

Прогон против настоящего `api.WorkbenchService` + `apiv1.ApiV1` +
`apikeys.ApiKeyManager` на временной data-папке.

| Пункт F29 | Результат |
|---|---|
| Первичный admin-ключ только через локальный доверенный bootstrap | `bootstrap_admin(local_trusted=False)` → `E_AUTH_PERMISSION`, `state=not_local`, в таблице 0 строк |
| Read-only ключ не создаёт себе admin | 5 попыток (`create` с `admin.keys`, `revoke`, `rotate`, `read_audit`, `list_keys`) — все `E_AUTH_PERMISSION`; ни одна строка не создана |
| Полный секрет ровно один раз | см. 2.7; первый ответ полный, повтор — `secret_already_shown` |
| Криптографическая генерация с достаточной энтропией | 200 выпусков: 200 разных секретов, 200 разных 32-байтных тел, 200 разных 12-символьных handle'ов; `secrets.token_bytes` |
| Однонаправленный verifier, сравнение без timing-leak | `pbkdf2_sha256$210000`; неизвестный префикс проходит полную деривацию (`_match_unknown_prefix`), чтобы стоить столько же; два отказа на неизвестный и на почти-верный секрет дают **одинаковый** detail |
| Список ключей, created/expires/last-used, переименование metadata, disable/revoke/delete | полный цикл в `LifecycleTests`; `last_used_at` пишется при использовании; удалённый ключ даёт `E_VALIDATION_FIELD` и исчезает из списка |
| Узкое transition window при ротации по ЯВНОЙ настройке | `grace_s=0` → окна нет; `grace_s=60` → `rotation_grace_until`; внутри окна старое даёт `E_AUTH_ROTATION_GRACE` (в том числе на management-вызове), после — `E_AUTH_INVALID`; окно — всегда per-key и никогда не дефолт |
| Отзыв и expiry на каждом запросе | `authenticate` и `assert_active` читают состояние каждый раз; проверено на `authenticate`, на новом lease и на recheck потока |
| Активные SSE/stream sessions завершаются по документированной policy | revoke → `sweep()` даёт `[('s-1', 'E_AUTH_REVOKED')]`, `active_ids()` пуст, `close_code` выставлен, `recheck()` после закрытия → `E_AUTH_INVALID`; expiry → `[('s-2', 'E_AUTH_EXPIRED')]`; `due()` пуст до интервала и не пуст после |
| Права: отдельные на каждую область | `PERMISSIONS` = read ∪ write ∪ sensitive ∪ admin; наборы не пересекаются; `export.secret` — отдельное чувствительное право |
| `export.secret` отдельно от `export.create` | без `export.secret` → `E_AUTH_PERMISSION` (HTTP 403); с ним — доходит до движка |
| Object-level права у каждой операции | фильтр только сужает: `effective_collections(principal, ['col-b'])` при scope `['col-a']` → `E_AUTH_SCOPE`; чужой объект и отсутствующий отвечают одинаково (`object_visible` → False в обоих случаях); объект без названного владельца невидим |
| Клиент не расширяет доступ через `allow_private` / raw path / запущенный job | key без `admin.keys` читает чужой ключ → 403; читает чужой job по id → 403; качает чужой export → 403; reader без `jobs.submit` шлёт `POST /v1/checks/check` → 403 |
| Квоты | 6 запросов при бюджете 3 → `[200, 200, 200, 429, 429, 429]`; `concurrency.max_active=1` → `E_LIMIT_CONCURRENCY` на втором lease |
| Локальный audit log: key ID, операция, объект/scope, результат, timestamp | колонки ровно `at, key_id, operation, object_kind, object_id, scope, result, error_code`; лог ограничен `audit_retention` |
| Никаких NTLM/Kerberos/signup | см. раздел 4 |

### 5.1 Найденный дефект в моём файле — закрыт: отказ по правам не попадал в audit

Проверено до правки:

```
permission denial -> E_AUTH_PERMISSION
 audit: key.create  ok   None
 audit: key.bootstrap ok None
denied rows recorded: 0
```

Валидный ключ, спрашивающий право, которого у него нет, отказывался правильно,
но **не оставлял следа**: в лог попадали только плохой секрет и непригодный
ключ. F29 требует в логе «key ID, операцию, объект/scope, результат, timestamp»,
и отказ по правам — ровно то, что оператору нужно видеть.

Исправлено в `ApiKeyManager.authenticate`: при отказе `authorize` пишется строка
`operation='authorize'`, `object_kind='permission'`, `object_id=<право>`,
`result='denied'`, `error_code=<код>`, `scope={collection_id, pool_id,
include_secrets}` — только заданные поля, без полных ключей и без тела запроса.
После правки:

```
refused admin.keys     -> E_AUTH_PERMISSION
refused read.results   -> E_AUTH_SCOPE
refused export.create  -> E_AUTH_PERMISSION
 audit: key=44970445 op=authorize kind=permission obj=export.create code=E_AUTH_PERMISSION scope={'include_secrets': True}
 audit: key=44970445 op=authorize kind=permission obj=read.results  code=E_AUTH_SCOPE          scope={'collection_id': 'col-z'}
 audit: key=44970445 op=authorize kind=permission obj=admin.keys    code=E_AUTH_PERMISSION    scope={}
denied rows now recorded: 3
no pwk_ in the denial rows: True
```

### 5.2 Найденный дефект в моём файле — закрыт: неканонический verifier проходил

`verify_secret` декодировал base64 и сравнивал байты, не проверяя, что строка
записана канонически. У поля base64 есть неиспользуемые биты в последнем
символе, поэтому digest из 32 байт (43 символа) записывается четырьмя разными
строками, и три из них принимались как валидный verifier того же секрета.
Доказ: подмена последнего значимого символа на `A`↔`B` даёт ту же пару байт.

```
first accepted tamper:
  stored digest tail : '5yA=' len 44
  flipped tail       : '5yB='
  b64decode equal    : True
  canonical re-encode: False
tamper accepted 125/2000  -> rate 6.2%
```

Из-за этого `tests/test_secrets_verifier.py::test_comparison_does_not_stop_at_the_first_wrong_digest`
падал примерно в 6% прогонов — это был флаки, а не поломка безопасности, но
пара «сделать/проверить» переставала быть обратной функцией, и переписанная
строка в БД проходила как нетронутая.

Исправлено в `secrets.verify_secret`: добавлен `_b64_exact`, который требует
канонической записи. Проверка идёт **до** деривации, то есть на совпадении
стоит ноль. Тест стал детерминированным (8 прогонов подряд — OK, раньше
2 из 8 падали).

---

## 6. Что НЕ в моей зоне и что я не трогал

* **Дефект 4** (неизвестное время / future timestamp / clock rollback) — это
  `db.py` (`checked_at`/`valid_until`) и `core.py` (admission). Формально в
  моём handoff не лезу: правка потребовала бы изменения чужого файла без
  измеримого выигрыша в моей области. Если владелец `core.py` захочет, точка
  входа — `core._identity_reason` / `Policy.max_age_seconds`, и она уже
  принимает `checked_at` снаружи.
* **Дефект 18** (разные secrets у gateway / GUI / API, LAN opt-in) — значения
  действительно разные и разделены (2.4), но `allow_local_without_auth` и
  LAN-привязка живут в `gateway.py:966-1479`. Не мой файл.
* **Идемпотентность vs одноразовость в других ответах.** `apiv1` сейчас
  кэширует любой ответ мутации. Для `POST /v1/subscriptions` тот же секрет —
  закрывается правкой из 2.7 автоматически. Если в будущем появится ещё один
  маршрут с `OneShotBody`, он тоже закрывается автоматически, потому что
  защита стоит на уровне `_invoke`, а не на уровне маршрута.

---

## 7. Новые тесты

Все четыре файла — новые, ни один существующий тест не менялся.

| Файл | Тестов | Что закрывает |
|---|---|---|
| `tests/test_areasecrets_verifier.py` | 7 | канонический verifier, подмена в любой позиции, «одна функция — одно направление», ошибки с `action` |
| `tests/test_areasecrets_path.py` | 40 | F04 целиком: import line → два доступа одного endpoint → ротация → admission → компенсация/reconciliation → locked vault → четыре различных auth-отказа → trusted private + DNS-rebind → редакция |
| `tests/test_areasecrets_transport.py` | 15 | HTTP Basic и SOCKS5 на проводе, отказ при несовпадении режима и схемы, отказ для чужого access и для устаревшей ревизии |
| `tests/test_areasecrets_keys.py` | 40 | одноразовость секрета (в т.ч. через настоящий `/v1` с обёрткой), энтропия, verifier, права, квоты, потоки и lease, окно ротации, object scope, audit log |

Прогон целиком по моей области (472 существующих + 102 новых):

```
Ran 609 tests in 18.036s
OK
```

Полный suite проекта — `Ran 2750 tests`, ноль новых падений. Падающие тесты
(`test_probes_reference`, `test_profiles_evaluate`, `test_areacatalog_profiles`,
`test_probearea_modes`, `test_acceptance.SelectionExportTests`,
`test_freshness.ExportFormatTests`, `test_extras.SingboxTests`) воспроизводятся
и **без** моих правок — проверено `git stash` на своих двух файлах.
`test_areajobs_scheduler` чувствителен к порядку прогонов и тоже не связан с
этой областью.
