# Area export / geography / diagnostics — handoff

Владелец участка: `proxy_workbench/exportsvc.py`, `proxy_workbench/geo.py`,
`proxy_workbench/diagnostics.py`, `proxy_workbench/formats.py`.
Ветка: `integration/ultra-2026-09-25`.
Требования: F08, F10, F25, F28; дефекты 7 и 20; сценарии §7: 2, 10, 11, 12.

Всё ниже проверено живым прогоном; команды и фактический вывод — в разделе
«Проверки». Ничего не помечено «готово» без вывода.

---

## 1. Дефект 7 — экспорт выделенного не переключает активный пул

**Состояние: сделано, проверено сквозным прогоном через `proxytool.export` +
реальная БД (`db.migrate`) + 100 измеренных строк.**

```
STEP 1  published run  ->  exported = 100 | kind = published | pointer = .generation-57264492ee31fbd8
STEP 2  selection      ->  exported = 2   | kind = selection   | selection_requested = 2
STEP 3  active pointer -> .generation-57264492ee31fbd8 | active rows = 100
        pointer still the published generation: True
        table rows in scope = 100
        artifacts on disk   = ['.generation-57264492ee31fbd8', '.generation-8a50c3fc562a9c6f']
        files of selection = 14
```

Покрыто `tests/test_areaexport_export.py::SelectionDoesNotMoveThePoolTests`
(4 теста) и существующими `tests/test_exportsvc_publish.py`,
`tests/test_exportsvc_selection.py`.

### Что требует чужого файла — не требуется

`proxytool.export` уже делает всё правильно: `kind='selection'` при
`allowed_proxies`, `exportsvc.publish` отказывает выборке, указатель не
трогается. Изменений в `proxytool.py`, `api.py`, `gui.py` для дефекта 7 не
требуется.

---

## 2. Известный пробел: `client_target` / `client_binary` не заполнялись

### Что было (доказано запуском)

```
ExportOptions().client_target = None
ExportOptions().client_binary = None
singbox_target(None) = SingBoxTarget(version='legacy', numeric=None, uses_rule_actions=False,
                                     state='unconfigured', reason=None)
EMPTY set, no target   -> outbounds=['block'] route {"final": "blocked"}
check_singbox(empty config, "1.14.0") = (False, ('E_EXPORT_TARGET_UNVERIFIED',))
compat: target_state = unconfigured | client_check = not_run
```

То есть модуль писал файл, который сам же отвергает для проверенной версии
клиента, помечая его `not_run`. `api.py:3232` вызывает `formats.singbox(selected)`
вообще без цели, поэтому устаревшая форма уходила и в legacy `/singbox`.

Источник в доках (прочитано 2026-09-26, 4 страницы, только официальная документация):

| Страница | Что взято |
| --- | --- |
| `https://sing-box.sagernet.org/migration/` | «Legacy special outbounds are deprecated and can be replaced by rule actions»; миграция `{"outbound":"block"}` → `{"action":"reject"}`; newest = **1.15.0** |
| `https://sing-box.sagernet.org/configuration/route/rule/` | поле `action` у правила — в блоке «Changes in sing-box **1.11.0**» рядом с `outbound` |
| `https://sing-box.sagernet.org/configuration/route/` | «Default outbound tag. the first outbound will be used if empty.» — почему пустой `outbounds` без `final` помечен unverified, а `direct` запрещён |
| `https://sing-box.sagernet.org/configuration/outbound/urltest/` | члены — `outbounds`, `interval` — длительность вида `"5m"` |

`SINGBOX_RULE_ACTIONS_FROM = (1, 11, 0)` подтверждён. `SINGBOX_MAX_VERIFIED`
поднят с `(1, 14, 0)` до `(1, 15, 0)` — прочитанные страницы описывают линию
1.15.0. Страница `route/rule_action/` содержит «Since sing-box 1.13.0», но это
про поля объекта action, а не про появление поля `action` у правила.

### Что сделано в `exportsvc.py` / `formats.py`

1. **Резолвер цели, доступный из моих файлов** —
   `exportsvc.resolve_client_target(options, directory=..., environ=...)`.
   Порядок: `ExportOptions.client_target` → `PROXY_WORKBENCH_SINGBOX_TARGET` →
   `client.json` рядом со снимками → «ничего не задано» (это состояние, не ошибка).
   Для бинарника то же: `client_binary` → `PROXY_WORKBENCH_SINGBOX_BIN` → `client.json`.
   `write_snapshot` вызывает его сам, если `client=` не передан, поэтому
   **cli/gui/api не обязаны ничего передавать**, чтобы цель заработала.
2. **Версионно-зависимая конструкция не пишется без цели.** Единственная такая
   конструкция — отказ для набора без пригодных outbound. При
   `target_state='unconfigured'` `singbox.json` **не пишется**, в `status.json`
   попадает `compat.files['singbox.json'].written=false`,
   `reasons=['E_EXPORT_TARGET_UNPINNED']` и warning с действием. Остальные файлы
   артефакта (включая `clash.yaml` c `MATCH,REJECT`) пишутся.
3. **Устаревшая форма достижима только поимённо:** `client_target='legacy-block'`
   или закреплённый клиент `<1.11`. Тогда `target_state='legacy_optin'` и warning
   «deprecated since sing-box 1.11.0».
4. **`formats.singbox` больше не пишет `block` по умолчанию** — параметр
   `fail_closed='rule_action'|'block'`, по умолчанию `rule_action`. Это чинит
   и `api.py:3232`, который я править не могу.
5. `_reason_text` теперь подставляет `{name!r}` / `{name!s}` — раньше
   `E_EXPORT_TARGET_UNKNOWN` показывал пользователю буквальное `{target!r}`.

Живой вывод после правки:

```
empty, no target                   state=empty     singbox.json written=False target='legacy' (unconfigured)
                                   reasons=['E_EXPORT_TARGET_UNPINNED']   clash DIRECT=False  pac DIRECT=False
usable rows, no target             state=complete  singbox.json written=True
                                   outbounds=['urltest', 'http'] route={"final": "auto"}
empty, target=1.14.0               state=empty     written=True  outbounds=[] route={"rules": [{"action": "reject"}]}
empty, target=1.10.0               state=empty     written=True  outbounds=['block'] route={"final": "blocked"}
empty, target=1.99.0               REFUSED E_EXPORT_TARGET_UNVERIFIED
empty, target=nightly              REFUSED E_EXPORT_TARGET_UNKNOWN: Client version 'nightly' could not be parsed
empty, target=0.9.0                REFUSED E_EXPORT_TARGET_UNSUPPORTED
```

HTTP-проверка реального read-only эндпоинта (сервер поднят на живом сокете,
`/proxies?format=txt` отдаёт `http://11.0.0.1:8080`):

```
/singbox с адресами   -> outbounds = ['urltest', 'http']  route = {"final": "auto"}
/singbox пустой выбор -> outbounds = []  route = {"rules": [{"action": "reject"}]}
                         deprecated block outbound: False   direct outbound: False
/clash  пустой выбор  -> 'rules:', '  - MATCH,REJECT'   DIRECT: False
```

### Точные строки для чужих файлов

`proxytool.py` — функция `export`, строки 2681–2683 (сигнатура) и 2879–2882
(сборка `ExportOptions`). Нужно добавить два параметра и пробросить их:

```python
# proxytool.py:2681-2683, в сигнатуре export(...)
           access=None, credentials='redact', client_target=None, client_binary=None,
           secret_grant=None):
# -> добавить:
           client_target=None, client_binary=None,
```

```python
# proxytool.py:2879-2882, где создаётся ExportOptions
    options = exportsvc.ExportOptions(sort=sort, top=int(top or 0), credentials=credentials,
                                      client_target=client_target, client_binary=client_binary,
                                      keep_generations=EXPORT_GENERATION_RETENTION,
                                      published_at=published_at, set_ttl_seconds=max_age_seconds)
```

Эти два параметра **уже есть** в сигнатуре и в `ExportOptions(...)`; не хватает
только CLI-флага, который их наполнит. Минимальная строка в парсере рядом с
`proxytool.py:3471` (`--credentials`):

```python
    p.add_argument('--client-target', default=os.environ.get('PROXY_WORKBENCH_SINGBOX_TARGET') or None,
                   help=tr('версия клиента sing-box для проверки совместимости: 1.11.0, latest, legacy-block',
                           'sing-box client version the export is checked against: 1.11.0, latest, legacy-block'))
    p.add_argument('--client-binary', default=os.environ.get('PROXY_WORKBENCH_SINGBOX_BIN') or None,
                   help=tr('путь к sing-box для проверки сгенерированного файла',
                           'path to sing-box used to check the generated file'))
```

и передача в вызов (рядом со строкой 3471, где объявляется `--credentials`
`command.extend`).

`api.py` / `apiv1.py` — **менять не обязательно**: резолвер уже читает
`client.json` и переменные окружения. Если нужен параметр запроса, достаточно
прокинуть его в `exportsvc.ExportOptions(client_target=...)` в существующей
точке создания опций; отдельная сигнатура не требуется.

`gui.py` — при желании одно поле «версия клиента sing-box», значение которого
уходит в тот же `ExportOptions`; формат файлов менять не нужно.

**Обратная совместимость.** Если файл `singbox.json` отсутствует, потребитель
должен смотреть `status.compat.files['singbox.json'].written`. Список файлов
артефакта теперь отражает реальность (`Artifact.files`).

---

## 3. Дефект 20 — пустой / expired / unsupported-only никогда не даёт DIRECT

**Состояние: сделано, проверено.**

- `DIRECT` не появляется ни в одном формате при любом наборе — проверено на
  пустом, `unsupported-only` (https-only) и usable наборах
  (`tests/test_areaexport_export.py::UnpinnedTargetTests::test_no_artifact_ever_carries_a_direct_outbound`).
- `empty_policy='error'` даёт `E_STATE_NO_PROXIES` и не оставляет поколения.
- `unsupported` объясняется построчно: `E_EXPORT_TLS_UNSUPPORTED` в `clash` и
  `singbox`, `fail_closed=true`, `state_detail='empty_no_match'`, при этом
  строка не потеряна — `proxies.txt` её содержит.
- Для статического TXT: `snapshot.txt` пишет `# generation`, `# expires_at` и
  строку «This static file cannot revoke itself. Re-check rows past expires_at.»
  Срок набора отделён от срока строки: в коде набора `expires_at = published + ttl`,
  у строки остаётся собственный `valid_until`.

**Нужно ли что-то в `api.py`:** нет, `/singbox` и `/clash` уже не могут выдать
DIRECT (проверено на живом сокете).

---

## 4. Credentials

**Состояние: сделано, проверено.**

```
default redact          : SECRET-PASSWORD в файлах артефакта -> False
credentials=reference без grant : E_EXPORT_CREDENTIALS_NOT_GRANTED
grant на чужой scope            : E_AUTH_SCOPE
с grant (allowed)               : 'access:acc-1@3' есть, значения нет,
                                  direct_auth_uri = None, userinfo в TXT/PAC отсутствует
```

Покрыто существующим `tests/test_exportsvc_secrets_export.py` и новым
`tests/test_areaexport_export.py`.

---

## 5. F08 — география

### Подтверждено живым прогоном

- **Главный сценарий.** Endpoint DE при желаемом выходе NL:
  `basis=exit` → `drop=False`, `verdict.matched=True`, `country='NL'`;
  `plan_measurement` не требует judge. Тот же адрес при `basis=endpoint` и
  неизвестном выходе не отбрасывается «заранее», а помечается как требующий
  измерения (`PLAN_JUDGE_FOR_EXIT`).
- **Включения/исключения, происхождение, дата.** `CountryFact` несёт `code`,
  `source`, `at`, `database_version`, `address`; `merge_facts` при конфликте
  отдаёт `conflict` и оставляет остальные источники видимыми.
- **Unknown-политика** — всегда значение (`exclude` / `include_unverified` /
  `require_measurement`), ни одна ветка не отдаёт `verified=True` при
  неизвестной стране.
- **Hostname geo** — от реально использованного IP и времени. Без записанного
  разрешения страна неизвестна; адрес и время разрешения сохраняются
  (`country_address`, `country_at`) даже когда база не смогла классифицировать
  адрес — это исправление в `geo.CountryFact.unknown` (раньше время терялось).
- **Hosting** — эвристика: `ProviderFact.claims()` отдаёт
  `residential: None, mobile: None, verified: False` и
  `note='org_name_heuristic_is_not_residential_evidence'`; `as_dict()` вообще не
  содержит таких полей, только `hosting` + `hosting_basis`.
- **GeoIP update** — `install_database` валидирует до записи, при отказе
  сохраняет прошлую рабочую базу и её версию; `database_status` отдаёт
  `available/version/stale/age_seconds`, отсутствие — `version_label() == 'absent'`.
- **ASN/CIDR/IPv4/IPv6** — `ProviderIndex.lookup` отдаёт `asn`, `provider`,
  `cidr` (минимальная сеть диапазона), `ip_version`; `range_cidr` покрывает и v4, и v6.
- **Read-only фильтр сам не запускает сеть** — `filter_rows` прогнан под
  подменой `socket.socket` / `create_connection` / `getaddrinfo` на
  `AssertionError`: 0 сокетов.
- **Один критерий в GUI/API/export** — `parse_criterion` от строки, от mapping'а
  и от объекта даёт один digest; `geo.filter_rows` и `proxytool.matches_selection`
  дают одинаковый ответ на одних и тех же строках (проверено для `DE`, `NL`,
  `DE,NL`, `US`, `FR` и для случая без GeoIP-базы).

### Найденный и исправленный дефект

`geo.Resolver.from_row` **не читал `row['country']`**. Экспортный движок
(`proxytool.geo_country_verdict`) читал, поэтому строка, не найденная в базе,
проходила в экспорте и отбрасывалась в GUI/API:

```
geo filter (resolver с пустой базой): []
export engine (country_of -> None)   : ['http://11.0.0.2:8080']
equal: False
```

Исправлено в `geo.py::Resolver.from_row`: стратагия строки читается как
`CountryFact(..., SOURCE_SOURCE)`, с той же датировкой и тем же приоритетом, что
и в экспортном пути. Приоритет базы над стратагией сохранён
(`SOURCE_PRECEDENCE`: geoip выше source).

**Что осталось за чужой файл (не мой):** экспортный движок
`proxytool.matches_selection` / `core.Policy.countries` умеет только
`include`-список по endpoint. Он не выражает `basis=exit`, `exclude` и
`unknown`, поэтому GUI/API-фильтр и экспорт совпадают только на подмножестве
`basis=endpoint`. Для полного паритета в `proxytool.py` нужно передавать
`geo.CountryCriterion` в `core.Policy` вместо `countries: frozenset`:

```python
# proxytool.py:2745-2746 (core.Policy(...)): countries=countries,
# -> добавить рядом:
        country_criterion=geo.parse_criterion(countries, basis=..., unknown=...,
                                              max_age_seconds=max_age_seconds),
# и в core.Policy принять поле country_criterion, заменяющее country_of
```

`core.py` и `proxytool.py` — не мои файлы, поэтому этого не сделано; текущая
точчка расхождения зафиксирована в тесте
`tests/test_areaexport_geo.py::ParityWithTheExportEngineTests`
(сравниваются оба решения, а не только свой результат).

**GUI/API и hosting.** `gui.py`, `apiv1.py`, `desktop.py` не упоминают
`residential`/`mobile` ни разу (проверено grep) — то есть UI не может выдать
эвристику за доказательство, потому что нечего выдавать. Но и `hosting_basis`
они не показывают, поэтому пользователь видит голый `hosting: true` без
основания. Рекомендация (одна строка в `gui.py`/`apiv1.py` при отдаче строки
результата): отдавать `row['hosting_basis']` рядом с `row['hosting']` —
значение уже формируется в `geo.ProviderFact.as_dict()`.

---

## 6. F10 — нулевые результаты

### Найденный и исправленный дефект

`Funnel.zero_result()` требовал `lost_total() == 0`, когда у прогона нет
счётчика `exported`. Прогон, в котором потеряно **всё**, объявлялся
«не-нулевым», и `explain_zero()` возвращал `None`. Живой прогон 12 способов
получить ноль до правки дал **6 молчаний**:

```
=== 1. every source download failed ===   (results exist, no zero-result explanation)
=== 2. sources answered but nothing parsed === (results exist, no zero-result explanation)
=== 6. no measurement time at all ===      (results exist, no zero-result explanation)
=== 7. the device had no network ===        (results exist, no zero-result explanation)
=== 8. the target was down for everyone === (results exist, no zero-result explanation)
=== 9. the budget ran out ===              (results exist, no zero-result explanation)
```

Это ровно тот случай, который F10 требует объяснить без traceback.

После правки те же 12 сценариев:

```
1  Sources could not be downloaded [SOURCE_UNAVAILABLE]      Action: Check the network and the source URL; the last good source data is kept.
2  Source answered without addresses [SOURCE_EMPTY]          Action: The response format changed or the list was withdrawn. Update the source adapter.
3  Sources produced no candidate [NO_SOURCES]                 Action: Add or enable a source and collect again; ...
4  No address is inside the check scope [SCOPE_EMPTY]          Action: Drop the filters or add candidates: ...
5  The row lifetime expired [E_TIME_TTL_EXPIRED]               Action: Expired rows await a recheck; raise max-age or run a recheck.
6  The row time is unknown [E_TIME_UNKNOWN]                   Action: A row without a measured time proves nothing and must be measured again.
7  This device has no network [DEVICE_NETWORK_DOWN]           Action: Reconnect the network and retry. No address is judged bad: the network was down here, not there.
8  The target is down for every address [TARGET_OUTAGE]       Action: Change the target or retry later. This is not an address error and must not lower their reputation.
9  The job budget is exhausted [E_LIMIT_BUDGET]               Action: Raise the request, byte or time budget. ...
10 Everything passed the check, nothing reached the export [SCOPE_FILTERED_ALL]  Action: Relax the export filters ...
11 Address is on the local denylist [SCOPE_DENYLIST]           Action: Remove the address from the local denylist ...
12 explain_zero -> None   (ненулевой прогон не объясняется)
```

**Глобальный outage не портит репутацию адресов** — проверено по счётчикам:

```
device down : attribution = {'device': 2}   tcp: {'device': 2}
target down : attribution = {'target': 4}   target: {'target': 4}
без control : attribution = {'proxy': 1}
```

`ControlVerdict.blame()` относит к устройству `device_network/dns/tcp/handshake/tls`,
к цели — `target/assertion/rate_limit`, всё остальное — к адресу. Контроль
ограничен: `total_requests=0` → `skipped`, 0 сокетов (проверено подменой
`socket`); число обращений к checker'у не превышает бюджет.

### Разделение стадий

`STAGES` (17) и `FUNNEL_STAGES` (15) покрывают source / download / parser /
scope / device_network / dns / tcp / handshake / tls / target / assertion /
auth / rate_limit / budget / freshness (+ environment, export). У каждой
потери есть код и действие: `Funnel.losses()` → `(stage, code, count)`,
`ZeroResult.to_dict()` отдаёт `code/stage/summary/action/counters/params/related`.
`ENVIRONMENT_STAGES` помечает стадии, потеря на которых не должна стать
репутацией адреса.

---

## 7. F25 — помощь и диагностика

### Найденный и починенный дефект

`FixtureRecipe.to_dict()` — это то, что отдаёт страница помощи и API — отдавал
`source.proxy` и ошибки образцов **без редактирования**:

```
canary in recipe.to_dict() : 1
source.proxy = "http://user:C4NARY-9f3a2b7c-PASSWORD@11.0.0.1:8080"
```

При этом `config_json()` и диагностический пакет редактировали секрет
правильно. Теперь `source` и `samples` редактируются в `to_dict()`:

```
canary in recipe.to_dict()    : 0
canary in recipe.config_json(): 0
canary in diagnostic bundle   : 0
```

### Второе улучшение: конкретный HTTP-статус получил help

`classification('HTTP_503')` намеренно отдаёт конкретный код (это ключ
счётчика, его проверяет `tests/test_diagnostics_codes.py`), но
`help_for('HTTP_503')` отдавал «Unknown error code: HTTP_503» — то есть на самую
частую ошибку цели у пользователя не было действия. Добавлено
`resolve_help_code()`: конкретный статус идёт в help по документированному
семейственному коду с параметром `status`; 429 и 407 сохраняют свои узкие коды
(`RATE_LIMITED`, `AUTH_FAILED`), потому что «цель отклонила запрос» — не то,
с чем пользователь действует.

```
HTTP_503 -> HTTP_5XX (status=503)   HTTP_404 -> HTTP_4XX   HTTP_302 -> HTTP_3XX
HTTP_429 -> RATE_LIMITED             HTTP_407 -> AUTH_FAILED   HTTP_999 -> unknown (честно)
```

### Проверено живым прогоном

- **Bundle:** 0 вхождений canary, 18 заметок о редактировании (без значения),
  `preview()` до записи, `save()` отказывается перезаписывать без `overwrite`,
  `describe()` говорит «saved locally and sent nowhere».
- **In-app help по кодам:** 66 кодов, у каждого есть `stage`, `title`, `action`;
  код и его параметры отделены от текста (`values={'scheme':'socks5'}`); ни один
  `E_*`-код не выдуман сверх `CONTRACT_CODES`; неизвестный код честно помечается
  `known: false`.
- **Health:** `version/schema/scope/job/freshness`, у каждой проблемы код и
  действие; `render('en')` содержит 5 `Action:` и ни одного `Traceback`.
- **Recipe:** без профиля — `RECIPE_NO_PROFILE`, без образцов —
  `RECIPE_NO_SAMPLES`; полный recipe повторяет targets/attempts/timeout/
  connect_timeout и редактированные заголовки.

---

## 8. Проверки

```
tests/test_areaexport_export.py        40 тестов  OK
tests/test_areaexport_geo.py           25 тестов  OK
tests/test_areaexport_diagnostics.py   38 тестов  OK
tests/test_export*.py                 113 тестов  OK
tests/test_geo*.py                    106 тестов  OK (skipped=1)
tests/test_diagnostics*.py             89 тестов  OK
tests/test_freshness.py                13 тестов  OK
tests/test_acceptance.py               35 тестов  OK
tests/test_workbench.py                17 тестов  OK
```

Полный прогон репозитория на «чистом HEAD + мои четыре файла + три
поправленных теста» — `2483 теста, 1 ошибка`. Ошибка
(`test_config`, `test_random_spreads_over_every_candidate`) воспроизводится и на
неизменённом HEAD (три прогона подряд: падает, проходит, проходит) — это
существующая нестабильность окружения, не регрессия.

Оставшиеся падения в общем дереве (`test_probes_reference`,
`test_profiles_evaluate`, `test_scheduler_budget`, `test_bandwidth`) вызваны
незакоммиченными правками других участников (`probes.py`, `scheduler.py`,
`pools.py`, `jobs.py`, `anonymity.py`, `reputation.py`, `geoip.py` в
`git status` изменены не мной) — изолированная проверка это подтвердила.

## 9. Изменённые файлы

Изменены: `proxy_workbench/exportsvc.py`, `proxy_workbench/geo.py`,
`proxy_workbench/diagnostics.py`, `proxy_workbench/formats.py`.
Новые: `tests/test_areaexport_export.py`, `tests/test_areaexport_geo.py`,
`tests/test_areaexport_diagnostics.py`.
Поправлены существующие тесты, фиксировавшие старое поведение:
`tests/test_exportsvc_singbox.py` (устаревшая форма как «файл, который
продукт пишет сегодня»), `tests/test_freshness.py` и
`tests/test_acceptance.py` (пустой экспорт без `singbox.json`).

## 10. Открытые вопросы

1. `proxytool.py` / `core.py` — передать `geo.CountryCriterion` в `core.Policy`,
   чтобы экспорт выражал `basis=exit`, `exclude`, `unknown` (раздел 5).
2. `gui.py` / `apiv1.py` — отдавать `hosting_basis` рядом с `hosting`; одно
   поле для версии sing-box (раздел 2).
3. Справочник кодов: `exportsvc.EXPORT_CODES` не попадает в
   `diagnostics.help_entries()` (модули не связаны, чтобы не было цикла).
   Для страницы помощи по экспорту нужно либо регистрировать эти коды в
   `diagnostics.CODES`, либо отдавать `EXPORT_CODES` отдельным разделом.
4. `api.py:3232` (`/singbox`) не может сообщить, что целевой версии нет: у
   `formats.singbox` нет параметра цели. Файл теперь не содержит устаревшей
   формы, но и не помечен как непроверенный. Если нужна маркировка — это
   отдельная правка `api.py` (переход на `exportsvc.render_singbox`).
