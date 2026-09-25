# Handoff: exportsvc

**Требования:** F28 (экспорты и согласованные snapshots) целиком; F27 в части merge/replace; дефекты 7 и 20; R04, R14.
**Контракт:** `docs/integration/CONTRACTS.ru.md` §1.2(3), §1.2(5), §2.3, §3.3 (миграция 11), §4.1–§4.6, §5.2 (`export.secret`), §5.4 (версия 1).
**База:** ветка `integration/ultra-2026-09-25`, HEAD `47229d0` плюс параллельные коммиты модулей `core.py`, `db.py`, `secrets.py` (на момент написания присутствуют в рабочем дереве как untracked/новые файлы).
**Владею:** `proxy_workbench/exportsvc.py`, `tests/test_exportsvc_*.py`, этот файл.

## 1. Прошу внести в чужие файлы

### 1.1 Интегратор — `proxy_workbench/proxytool.py`

- **`export()` (`proxytool.py:1651-1932`).** Просьба заменить собственную сборку файлов и публикацию на два вызова, разделив их явно:

  ```python
  artifact = exportsvc.write_snapshot(
      directory, rows,                                  # уже отобранные core.select(...).admitted
      scope=exportsvc.ExportScope(identity=core.Scope(collection_id, profile_id, profile_revision),
                                  protocol=protocol, countries=countries, exclude_hosting=exclude_hosting,
                                  query=query, quick=quick, top=top,
                                  selection=tuple(allowed_proxies or ()),   # kind='selection'
                                  merge_mode=..., source_binding=...),
      options=exportsvc.ExportOptions(sort=sort, top=top, limits={...},
                                      credentials='reference' if include_credentials else 'redact',
                                      client_target=client_version, client_binary=sing_box_path,
                                      empty_policy='error' if fail_on_empty else 'fail_closed',
                                      set_ttl_seconds=policy.max_age_seconds),
      policy=policy, kind='selection' if allowed_proxies is not None else 'published',
      grant=grant, selection=core_selection, run_state=run_state, extra={...targets, request_profile...})
  if kind == 'published':
      exportsvc.publish(artifact, directory, confirm=True)     # единственное место, меняющее active pool
  else:
      exportsvc.publish(artifact, directory, confirm=True, pointer_name=exportsvc.DIAGNOSTIC_POINTER_NAME)
  ```

- **Дефект 7 закрывается здесь:** `exportsvc.publish()` физически не может опубликовать `kind='selection'` (бросает `E_VALIDATION_FIELD`), а `write_snapshot()` не пишет указатель вообще. Сегодня `export()` вызывает `atomic(directory/'current.json', ...)` из тела функции при `allowed_proxies is not None` (`proxytool.py:1900-1924`) — это и есть переключение активного пула кнопкой «Скачать».
- **`last-profile.txt`:** `write_snapshot()` его не касается; перенос `active_profile_path` остаётся на стороне `proxytool.py` (его писать нужно только после успешного `publish`).
- **Legacy root-файлы** (`_publish_legacy_files`, `proxytool.py:488-539`): можно оставить как есть — они читают уже записанное поколение, порядок «сначала всё fallible, потом указатель» сохраняется. Менять не требуется, но поле `status['valid_until']` теперь означает срок **набора** (максимум по admitted-строкам, `core.select`), а не `min()` по строкам, как сейчас (`proxytool.py:1867`).
- **`_publish_legacy_files` пишет root `status.json` из `report`.** Оставляйте `report = artifact.status.as_dict()` — он содержит все прежние имена полей (§4.3), поэтому `api.Exports.load()` и GUI продолжают читать.

### 1.2 Интегратор — `proxy_workbench/api.py`

- **`Exports.load()` (`api.py:93-195`).** Ключ перечитывания сейчас `(generation, mtime_ns, size)` (`api.py:127-128`). Просьба заменить на проверку `status['schema_version'] in exportsvc.SUPPORTED_SCHEMA_VERSIONS` и `status['manifest']` (sha256 по `ranked.json` и `status.json`), а лучше — перейти на `exportsvc.load_snapshot(directory, generation=bound, verify=True)`, который уже делает и то и другое и бросает `E_STATE_SNAPSHOT_SCHEMA` / `E_STATE_SNAPSHOT_MIXED` / `E_STATE_SNAPSHOT_MANIFEST` вместо тихой очистки.
- **Дефект 3:** `api.py:181-182` обнуляет `rows`, когда `status['stale']`. Для поколения версии 2 `status['expires_at']` — срок набора, поэтому обнуление корректно только когда истёк весь набор; истёкшая отдельная строка обязана оставаться видимой с `freshness='expired'`. Готовый метод: `LoadedSnapshot.rows` / `.fresh_rows` / `.expired_rows` / `.unknown_rows`.
- **`public_row()` (`api.py:51-89`):** `age_seconds` и `admission_reason` приходят из `core.Admission.as_dict()` и уже присутствуют в строках `ranked.json` версии 2; дублирующий расчёт в `api.py` тогда можно убрать.

### 1.3 Интегратор — GUI-путь выделенного экспорта

`gui.py:383-458` → `proxytool.py:2511 export_now(selected=...)` → `export()`. Правки не вношу: `gui.py` принадлежит поверхности `web`, `proxytool.py` — интегратору. Нужно, чтобы обработчик «Download Selected» приводил к `kind='selection'`, а скачивание шло из `artifact.directory`, а не из `exports/current.json`. Детализация — в §5.

### 1.4 Владелец `proxy_workbench/diagnostics.py` — коды ошибок

`EXPORT_CODES` в `exportsvc.py` содержит 18 кодов, которых нет в каноне §5.4. Прошу внести их в справочник и в перевод (`i18n.tr` уже вызывается из модуля, ключи в `messages{}` не нужны — текст лежит рядом с кодом, как в `core.REASON_CODES`):

`E_EXPORT_PROTOCOL_UNSUPPORTED`, `E_EXPORT_TLS_UNSUPPORTED`, `E_EXPORT_AUTH_UNSUPPORTED`, `E_EXPORT_HOSTNAME_UNSUPPORTED`, `E_EXPORT_IPV6_UNSUPPORTED`, `E_EXPORT_DNS_LOCAL`, `E_EXPORT_LIMIT_TRUNCATED`, `E_EXPORT_TARGET_UNKNOWN`, `E_EXPORT_TARGET_UNVERIFIED`, `E_EXPORT_TARGET_UNSUPPORTED`, `E_EXPORT_CREDENTIALS_NOT_GRANTED`, `E_EXPORT_CLIENT_REJECTED`, `E_EXPORT_DIRECT_FORBIDDEN`, `E_EXPORT_CLIENT_MISSING`, `E_EXPORT_EMPTY_OUTBOUNDS_UNVERIFIED`, `E_STATE_PUBLISH_UNCONFIRMED`, `E_STATE_SNAPSHOT_MIXED`, `E_STATE_SNAPSHOT_MANIFEST`.

Канонические коды §5.4, которые я переиспользую без изменений: `E_STATE_NO_SNAPSHOT`, `E_STATE_SNAPSHOT_SCHEMA`, `E_STATE_NO_PROXIES`, `E_VALIDATION_FIELD`, `E_VALIDATION_SCHEMA`, `E_AUTH_SCOPE`, `E_DATA_MIGRATION_FAILED`; коды времени приходят из `core` (`E_TIME_TTL_EXPIRED` и остальные §2.4) и у меня не дублируются.

### 1.5 Владелец `proxy_workbench/gateway.py` (поверхность `gateway`)

`gateway.Pool.refresh()` следует за глобальным `current.json` (дефект 8). Готовый вход: `exportsvc.load_snapshot(directory, generation=bound_name)` — привязка listener'а к имени поколения, и `Pointer.generation` читается один раз. Ничего править в `gateway.py` не прошу, только отмечаю вход.

## 2. Что уже сделано у меня

Публичный API `proxy_workbench/exportsvc.py` (61 верхнеуровневое определение, `__all__` в начале файла):

**Контрактные значения**
- `ExportScope(identity=core.Scope(...), protocol, countries, exclude_hosting, query, quick, network_id, selection, top, merge_mode, source_binding)`; `.digest()` — digest области таблицы (выборка и `top` в неё **не** входят), `.artifact_digest(kind, policy, rows)` — digest конкретного артефакта, `.core_scope` — объект для `core.admit`.
- `ExportOptions(sort, top, limits, credentials, client_target, client_binary, empty_policy, published_at, set_ttl_seconds, readonly, unsupported_limit, keep_generations)`.
- `SecretGrant(permission='export.secret', allowed, scope_digest, issued_by)`; `.check(scope)` бросает `E_EXPORT_CREDENTIALS_NOT_GRANTED` / `E_AUTH_SCOPE`.
- `SnapshotStatus` + `build_status(...)` / `status_from_dict(data)`; версии схемы `SNAPSHOT_SCHEMA_VERSION = 2`, `SUPPORTED_SCHEMA_VERSIONS = (1, 2)` (v1 — это то, что пишет сегодняшний `proxytool.SNAPSHOT_SCHEMA_VERSION`, он остаётся читаемым).
- `CompatReport` / `Unsupported` / `compat_report(rows, fmt, ...)` — preview до записи; `ROW_FIELDS`; `redact_row(row)`; `attach_admission(rows, core.Selection)`.
- `SingBoxTarget` / `singbox_target(version)` / `render_singbox(rows, target=...)` / `check_singbox(config, target)` / `client_check(text, target, binary=...)`.

**Запись и чтение**
- `write_snapshot(directory, rows, *, scope, options, kind, policy, grant, selection, now, run_state, extra) -> Artifact` — пишет 14 файлов, считает manifest, **не трогает указатель**. `kind ∈ {published, selection, diagnostic}`.
- `publish(artifact, directory, *, confirm=False, pointer_name=POINTER_NAME) -> Pointer` — единственное место, меняющее активный пул; требует `confirm=True`; `kind='selection'` отвергает всегда, `kind='diagnostic'` — только для `diagnostic.json`.
- `read_pointer(directory, name)`, `load_snapshot(directory, generation=None, *, verify=True, now=None)`, `prune_generations(directory, *, keep, current)`, `remove_generation(path)`.
- `record_artifact(db, artifact) -> str` — запись строки в `export_artifact` (миграция 11) с явным списком колонок; отсутствующую или переделанную таблицу **сообщает** (`E_DATA_MIGRATION_FAILED`), DDL не пишет.
- Рендереры: `render_txt`, `render_snapshot_txt`, `render_hostport`, `render_protocol_files`, `render_json`, `render_csv`, `render_proxychains`, `render_pac`, `render_clash`, `render_singbox`.

**Что закрыто по существу**
- **Дефект 7 / R04.** Экспорт выделенного, top-N и поисковой выборки — артефакт вида `selection` со своим поколением, своим manifest и записью в `export_artifact`; `current.json` и область таблицы не меняются. Проверено тестами: после записи выборки `read_pointer()` и `load_snapshot()` дают прежнее поколение и полный состав.
- **Дефект 20 / R14.** Для sing-box reject генерируется по версии клиента: до 1.11.0 — `block` outbound (байт-в-байт совпадает с текущим `formats.singbox`, тест это фиксирует), с 1.11.0 — route rule `{"action": "reject"}`. Версия клиента — часть проверки: без закреплённого бинарника результат `client_check='not_run'`, и это видно в `status.json`; `E_EXPORT_TARGET_UNVERIFIED` для версии новее 1.14.0 и `E_EXPORT_TARGET_UNKNOWN` для неразбираемой — отказ генерации, а не тихий откат на legacy.
- **F28, credentials.** По умолчанию `credentials='redact'`: значение не попадает ни в один файл даже если вызывающая сторона передала строку с паролем (`redact_row` вычищает список ключей и срезает userinfo из `proxy`); `access_ref='access:<id>@<rev>'` появляется только с `SecretGrant(allowed=True)`, `direct_auth_uri` всегда `None`.
- **Пустой/истёкший/unsupported-only.** `empty_policy='fail_closed'` (по умолчанию) даёт совместимый отказ: Clash `MATCH,REJECT`, PAC без `DIRECT`, sing-box reject нужной версии, пустые `proxies.txt`/`ranked.json`, `state='empty'`, `state_detail` различает `empty_no_match` / `all_expired` / `all_failed`; `empty_policy='error'` даёт `E_STATE_NO_PROXIES` до создания каталога. Ни в одной ветке outbound `direct` не появляется (`check_singbox` это проверяет отдельно).
- **Дефект 3.** `expires_at` = `max(valid_until)` по admitted (берётся из `core.Selection`), а не `min()` по строкам; `state` и `stop_reason` разведены; в ридере `rows` / `fresh_rows` / `expired_rows` / `unknown_rows` — одна истёкшая строка не обнуляет набор.
- **Согласованность.** `row` без `valid_until` — `freshness='unknown'`, а не «вечно свежая» (дефект 1 на стороне чтения).

## 3. Совместимость

- **Что ломается, если §1.1 не внести:** дефект 7 остаётся. Пока `export()` сам пишет `current.json`, кнопка «Скачать выделенное» переключает активный пул, и `exportsvc` будет вторым неиспользуемым слоем. Мои тесты этого не покрывают — они проверяют контракт модуля, а не вызов из `proxytool.py`.
- **Что не ломается:** `formats.py`, `api.py`, `gui.py` и `proxytool.py` я не менял; `formats.pac/clash/singbox` вызываются как есть, поведение для версии без закреплённого клиента совпадает с сегодняшним байт-в-байт. Поколение версии 1 читается и помечается `legacy: true`. Имена полей `status.json` не переименованы и не меняли смысл; новые поля только добавлены. Root-файлы экспорта и `_publish_legacy_files` не тронуты.
- **`core.py` — единственный источник admission.** Пороги, `denylist`, анонимность и часы не дублируются: `exportsvc` вызывает `core.select`/`core.pin_generation` и только переносит `Admission.as_dict()` в строку. Единственное исключение, о котором стоит знать: `LoadedSnapshot._row_freshness()` отвечает на вопрос «наступил ли `valid_until`» для **чтения**, решение о допуске принимает `core`.
- **`db.py`:** миграция 11 (`db.py:782-785`) совпадает с `ARTIFACT_COLUMNS` посимвольно — сверил, правок не нужно. DDL модуль не пишет. Наблюдение для владельца `db.py`: `db.migrate()` оставляет `ResourceWarning: unclosed database` из `db.py:473` (`describe`), на результат тестов не влияет.
- **`tests/fixtures/admission.py`:** используется в `tests/test_exportsvc_reader.py::SharedFixtureTests` (5 тестов на общих строках, включая mixed-age и `unknown_time`), как требует HANDOFF §2.1.

## 4. Проверки

Команды, выполненные в этой сессии из корня репозитория, каждая отдельно:

```
.venv/bin/python -m unittest tests.test_exportsvc_contract     → Ran 18 tests … OK
.venv/bin/python -m unittest tests.test_exportsvc_publish      → Ran 13 tests … OK
.venv/bin/python -m unittest tests.test_exportsvc_formats      → Ran 13 tests … OK
.venv/bin/python -m unittest tests.test_exportsvc_compat       → Ran 13 tests … OK
.venv/bin/python -m unittest tests.test_exportsvc_singbox      → Ran 18 tests … OK
.venv/bin/python -m unittest tests.test_exportsvc_selection    → Ran 10 tests … OK
.venv/bin/python -m unittest tests.test_exportsvc_reader       → Ran 17 tests … OK
```

Итого 102 теста, все зелёные. Полный `unittest discover -s tests` **не запускался** — по условию задачи его выполняет интегратор; красный там сейчас ожидаем и к моему участку не относится.

**Не выполнено (важно для приёмки):**
- **Реальный бинарник sing-box не запускался.** Ни `sing-box check`, ни любая другая проверка настоящим клиентом в этой сессии не выполнялись: бинаря в окружении нет, установка — сетевое действие. `client_check()` покрыт двумя фейковыми исполняемыми файлами (`exit 0` и `exit 1`), которые проверяют ветвление `passed` / `failed` / отказ генерации, но **не** проверяют, что настоящий sing-box принимает сгенерированный конфиг. Статус R14 по этой части: `implemented_unverified`, а не `verified`.
- **Clash, PAC, proxychains, v2ray-подобные клиенты настоящими программами не проверялись.** Для них проверка структурная: формат вызывается из `formats.py`, а `compat_report` объясняет, что в файл не попало и почему. Проверку настоящим mihomo прошу внести в приёмку вместе с pinned sing-box.
- **Зависимости от GUI/API не проверены:** новые маршруты `/v1/export/...` не писались (модуль `apiv1.py` — чужой файл), интеграция описана только в §1.

**Источники для версионных правил (прочитаны в этой сессии, ограниченное чтение официальной документации):**
- `https://sing-box.sagernet.org/migration/` — 1.11.0, «Migrate legacy special outbounds to rule actions», `"outbound": "block"` → `"action": "reject"`.
- `https://sing-box.sagernet.org/configuration/route/rule_action/` — `reject` валидное действие правила, для `route` обязателен `outbound`.
- `https://sing-box.sagernet.org/configuration/outbound/urltest/` — поле называется `outbounds`, `interval` принимает `"5m"`.
- `https://sing-box.sagernet.org/configuration/route/` — `final` присутствует, признаков устаревания нет.
- Чего эти страницы **не** подтверждают: допустим ли пустой список `outbounds`. Поэтому пустая выборка для 1.11+ помечается `E_EXPORT_EMPTY_OUTBOUNDS_UNVERIFIED` и выносится на pinned client, а не объявляется совместимой.

## 5. Открытые вопросы

1. **Пустой `outbounds` для sing-box ≥ 1.11.** Нужен ответ владельца документации/приёмки: подтверждён ли пустой список outbounds с catch-all `action: reject` (тогда конфиг fail-closed можно публиковать как совместимый), или нужен другой способ выразить отказ без special outbound. До ответа эта ветка помечена unverified.
2. **Версия 1.14.0 как верхняя проверенная.** Правила читались по документации, актуальной на 1.14.0. Если приёмка решит поддерживать другие версии, `SINGBOX_MAX_VERIFIED` / `SINGBOX_RULE_ACTIONS_FROM` правятся одной строкой, но каждая новая версия требует прочтения её migration-заметок.
3. **`credentials='reference'` и форматы, которые умеют значение.** Clash и sing-box умеют `username`/`password`, но подставлять их должен владелец vault по своему permission check; `exportsvc` отдаёт только ссылку. Нужен ответ от `secrets.py`: какая структура ссылки (`access:<id>@<rev>` сейчас) будет использоваться на его стороне при разрешении скачивания.
4. **`E_STATE_SNAPSHOT_IMMUTABLE`** объявлен в `EXPORT_CODES`, но нигде не вызывается: неизменяемость держится на том, что `write_snapshot` пишет поколение один раз, а файлы получают режим `0444` (`remove_generation` снимает его перед удалением). Если интегратору нужен явный отказ при повторной записи в опубликованное поколение — код готов, вызов не сделан; вносить его в канон имеет смысл только вместе с этим вызовом.
5. **Скачивание выделенного из GUI** требует, чтобы `gui.py` знал `artifact.directory` выбранного артефакта, а не путь через `current.json`. Правки в `gui.py`/`ui/*` не вношу (владелец — поверхность `web`); контракт подсказки — §1.3.
6. **`extras` из движка.** `build_status(extra=...)` остаётся единственной точкой, где движок передаёт `targets`, `request_profile`, `reputation.counts`, `breakdown` и т. п. Список имён полей, которые обязан остаться в `status.json`, взят из §4.3 и проверяется тестом `test_legacy_field_names_survive`.
