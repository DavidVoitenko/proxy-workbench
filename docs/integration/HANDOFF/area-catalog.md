# Area catalog: importer, profiles, service catalog

Владелец участка: `proxy_workbench/importer.py`, `proxy_workbench/profiles.py`,
`proxy_workbench/servicecatalog.py`, тесты `tests/test_areacatalog_*.py`.
Ветка `integration/ultra-2026-09-25`, база `b43a58c`.

Требования: F02 (коллекции и scope), F03 (импорт), F05 (профили и правила),
F06 (каталог сервисов), дефекты 10 и 24, сценарии приёмки 3, 4, 5, 6, 21
(разделы 3, 4 и 7 MASTER-PROMPT.ru.md).

Сеть в этой работе не использовалась: ни один публичный прокси не проверялся,
ни один endpoint каталога не опрашивался. Всё доказано локальными
контролируемыми сценариями на временной БД. Единственные сетевые чтения — три
страницы официальной документации Twitch (см. §5), с бюджетом: 3 запроса,
без массовых проверок.

---

## 1. Что изменено в коде

### 1.1 `importer.py` — четыре исправления, все с подтверждённым воспроизведением

**(а) Утечка секрета в provenance на диске — дефект F04, найдено canary-тестом.**

Было: `redact()` вырезал userinfo по `@` и всё после `/?# \t`. Строка без
схемы и без `@` вида `host:port:secret` или `user:secret` переживала redacted
целиком, попадала в `sample` отклонённой строки, а оттуда — в
`import_batch.report_json` на диск.

Воспроизведение до правки:

```
rows: [(1, 'rejected', 'E_IMPORT_FORMAT', '45.33.32.156:8080:CANARY-PW-9f3a2b7c'),
       (2, 'rejected', 'E_IMPORT_FORMAT', 'user:CANARY-PW-9f3a2b7c'),
       (3, 'valid', None, '91.198.174.192:3128')]
canary in import_batch.report_json: True
canary found in file: wb.db-wal
```

Стало: `_field_separators` считает разделители полей вне `[]` (чтобы IPv6 в
скобках не пострадал), `_redact_fields` отрезает всё после первого поля, если
authority не являетсяplain `host:port`. `redact()` получил параметр `whole`:
целая запись без точки и без разделителей — это непрозрачный `***`; отдельная
ячейка дополнительно сохраняет чистое число, чтобы порт `8080` в CSV остался
виден рядом с адресом.

```
'45.33.32.156:8080:CANARY-PW-9f3a'  -> '45.33.32.156 …'
'user:CANARY-PW-9f3a'               -> 'user …'
'[2001:db8::1]:8080:CANARY-PW-9f3a' -> '[2001:db8::1] …'
'45.33.32.156:8080'                 -> '45.33.32.156:8080'   (не пострадал)
'8080' (ячейка)                     -> '8080'               (порт сохранён)
```

**(б) `detect_format` misклассифицировал смешанный список как URI.**

Было: `uri` выбирался по наличию `://` в первой строке. Список, где первая
строка со схемой, а остальные — голые адреса, читался как `uri` и отвергал всё,
кроме первой строки.

Воспроизведение до правки:

```
'http://91.198.174.192:3128\n45.33.32.156:8080\n'
  -> [(1, valid, 'http://91.198.174.192:3128'), (2, rejected, E_IMPORT_FORMAT, None)]
```

Стало: `uri` выбирается, только когда схема есть в **каждой** значимой строке;
смешанный список читается как `txt`, который понимает оба вида.

**(в) CSV с заголовком без слова-роли нельзя было разобрать по именам.**

`_is_header_record` узнаёт заголовок только по слову роли (`host`, `ip`, `port`).
Обычный заголовок `a,b,c` считался данными: mapping по именам падал с
`E_IMPORT_FORMAT: В файле нет колонки «a» для «host»`, а по индексам работал, но
строка заголовка становилась отклонённой и блокировала commit
(`E_IMPORT_PARTIAL`).

Стало: `_names_the_header` — если все имена из явного mapping присутствуют в
первой записи, первая запись и есть заголовок. Плюс `preview(..., header=...)`:
явный ответ вызывающего (`True` / `False` / `None` по умолчанию) имеет приоритет
над угадыванием, а `Preview.header` и `to_dict()['header']` показывают
поверхности, какая строка consumed как имена. Поле добавлено в конец
dataclass, поэтому `Preview(...)` с positional-аргументами не ломается.

**(г) `ProfileStore` не закрывал соединение, которое сам открыл.**

`open_store(path)` вызывает `db.connect(path)` и возвращает store; закрывать
было нечем, утечка дескриптора при каждом открытии. Добавлен
`ProfileStore.close()`, который закрывает соединение **только** если его открыл
`open_store` (`_owned`); store, построенный вокруг чужого соединения, его не
трогает.

### 1.2 `profiles.py` — одно дополнение

`ProfileStore.close()` и `open_store()` переходят на владение соединением
(см. 1.1 г). Больше в `profiles.py` ничего не менялось: все предполагаемые
дефекты при проверке оказались уже закрытыми намеренными контрактами
(раздел 4).

### 1.3 `servicecatalog.py` — изменений нет

Файл остался без правок. Всё, что выглядело дефектом при чтении, при живом
прогоне оказалось работающим намеренным поведением (раздел 4). Данные каталога
лежат в `proxy_workbench/data/service_sets.json`, который **не входит** в мою
область владения; требуемое добавление Twitch описано в §5 и проверено тестом
`tests/test_areacatalog_servicecatalog.py::TwitchPresetProposalTests` без
правки файла.

---

## 2. F02 — коллекции и scope: сделано, проверено живым прогоном

Проверено вызовом `db` + `importer` на временной БД.

| Требование | Статус | Живое доказательство |
| --- | --- | --- |
| Создание, find-or-create | сделано | `create_collection('Мои прокси A','private')` дважды → один и тот же `col-76a520848895`; личная коллекция рождается пустой, публичная база не тронута |
| Переименование, архивирование | сделано | `rename_collection` → «Мои прокси B (рабочие)»; `archive_collection` убирает список из `list_collections()`, но он остаётся в `include_archived=True`, а его members сохраняются |
| Membership, один адрес в нескольких коллекциях | сделано | один адрес в A, B и public-base: `endpoints` = 1 строка, `membership` = 3 строки, `endpoint_collections()` возвращает все три |
| Удаление membership не удаляет адрес | сделано | `remove_member(A, shared)` → A потерял, B и public-base сохранили, строка в `endpoints` осталась |
| Явный выбор scope | сделано (не мой файл) | `core.Scope(collection_id, profile_id, revision, generation)`; `pools.PoolStore.create(collection_id=...)`; `exportsvc.Artifact` несёт `collection_id` |
| Миграция старых данных без выдуманного происхождения | сделано | старая `candidates` → `endpoints` + коллекция `legacy-collected`, `origin='legacy'`, `legacy_summary.public_base_members = 0`, второй `migrate()` ничего не удваивает |

**Приёмка «две собственные коллекции и публичная база не смешиваются»** —
подтверждена: адрес из A отсутствует в выдаче B и наоборот. Живой вывод:

```
ACCEPT: address of A in B listing? False (must be False)
ACCEPT: address of B in A listing? False (must be False)
```

**Приёмка «scan/export в одной не меняет подключённую другую»** — подтверждена
на уровне привязки: пул `pool-b` привязан к B, `replace` в A выполнен, привязка
не сдвинулась и список B не изменился.

```
replace in A: added=('http://104.244.72.115:8080',) removed=('http://185.220.101.5:8080',)
  A     = ['http://104.244.72.115:8080']
  B     = ['http://5.255.255.5:8888', 'http://91.198.174.192:3128'] <- untouched
ACCEPT: pool bound to B while A was replaced -> True
```

**Важная деталь, которую должен знать интегратор.** `DEFAULT_POLICY` в
`importer` — `public_only=True`, и он отвергает всё, что `proxytool.normalize()`
не считает глобально маршрутизируемым. Это включает диапазоны документации
RFC 5737 (`203.0.113.0/24`, `198.51.100.0/24`, `192.0.2.0/24`) и любой
hostname. Для доверенной личной коллекции вызывающий обязан передать
`EndpointPolicy(public_only=False)`. Так уже делают `proxytool._import_policy`
(CLI, флаг `--allow-private-endpoints`) и `gui.py:3160`. Никакой третий путь
(API, фоновое задание, gateway) сам этого не делает — см. §6.

---

## 3. F03 — импорт: сделано, кроме одного пробела (см. §6)

### 3.1 Каналы и форматы

Четыре канала (файл, drop файла, drop текста, буфер) на одном содержимом дают
**побайтово одинаковые** решения строк — проверено сравнением сигнатур
`(line, state, reason, canonical)`:

```
file         counts={"total": 9, "valid": 3, "duplicates": 1, ... "skipped": 2}
drop(file)   counts={... те же ...}
drop(text)   counts={... те же ...}
clipboard    counts={... те же ...}
all channels produce the same row decisions: True
```

Форматы: `txt`, `uri`, `csv`, `json` (список строк, список объектов, объект с
ключом `proxies`). Детектор проверен отдельно: `txt` / `uri` / `csv` (запятая) /
`csv` (таб) / `json` (массив) / `json` (объект).

### 3.2 Preview: номера строк, дубликаты, отказы

```
  2   valid     None                          http://45.33.32.156:8080
  4   valid     None                          http://91.198.174.192:3128
  5   valid     None                          socks5://185.220.101.5:1080
  6   rejected  E_IMPORT_FORMAT               None
  7   duplicate E_IMPORT_DUPLICATE_IN_SOURCE  http://45.33.32.156:8080
  8   rejected  E_IMPORT_PRIVATE              None
  9   rejected  E_IMPORT_CREDENTIALS          None
 10   rejected  E_IMPORT_HOSTNAME             None
 11   rejected  E_IMPORT_PRIVATE              None
skipped (blank/comment) = 2
```

`preview` не пишет ничего: таблицы `endpoints`/`membership`/`import_batch`
до и после четырёх preview совпадают, `conn.in_transaction` остаётся `False`.

### 3.3 Приёмка целиком

| Требование | Статус | Живое доказательство |
| --- | --- | --- |
| preview ничего не меняет | сделано | счётчики таблиц и `in_transaction` не изменились |
| crash не оставляет половину replace | сделано | `on_progress`, бросающий на 2-й пробе → состав A **до** и **после** идентичен, батч в состоянии `failed`, повторный commit с того же preview завершается |
| cancel не оставляет половину replace | сделано | `should_cancel` на 2-й пробе → `E_IMPORT_CANCELLED`, состав A не изменился, батч `cancelled`, повторный commit проходит |
| повторный commit не размножает | сделано | `replayed=False`, затем `replayed=True`, тот же `batch_id`, те же `counts`, `membership` = 4 строки |
| частично плохой файл чинится осмысленно | сделано | commit → `E_IMPORT_PARTIAL` и ноль записей; `allow_partial=True` → адреса добавлены, отклонённая строка указана; исправленный файл → `rejected=0`, `already_member=1` |
| revision conflict | сделано | `E_IMPORT_REVISION (было 7, стало 8)` |
| занятая коллекция | сделано | `busy()` → `E_CONFLICT_BUSY: Коллекция занята: scan job 42 running` |
| replace не вычистит коллекцию целиком | сделано | `E_IMPORT_EMPTY`, состав не изменился |
| импортный отчёт | сделано | `load_report(batch_id)`: state, revision before/after, counts, reasons, rejected по номерам строк, source name+digest, `duration_s`; JSON без секретов |

Отчёт пишется для батчей, дошедших до транзакции (`committed` / `cancelled` /
`failed`). Отказы до транзакции (`E_IMPORT_PARTIAL`, `E_IMPORT_EMPTY`,
`E_IMPORT_REVISION`, `E_CONFLICT_BUSY`, непокрытый mapping) отвечают кодом
синхронно и строки батча не создают — это осознанное поведение, не пробел.

### 3.4 Дефект 10 — единый отказ, ничего не теряется молча

Ключевая функция одна — `_classify`; её вызывают все пути. Проверено 4×2
прогона (txt/uri/json × public/private) плюс отдельный CSV:

```
txt  public  [E_IMPORT_HOSTNAME, E_IMPORT_PRIVATE, E_IMPORT_CREDENTIALS, None(valid)]
txt  private [None(valid),      None(valid),      E_IMPORT_CREDENTIALS,      None(valid)]
uri  public  [E_IMPORT_HOSTNAME, E_IMPORT_PRIVATE, E_IMPORT_CREDENTIALS, None(valid)]
uri  private [None(valid),      None(valid),      E_IMPORT_CREDENTIALS,      None(valid)]
json public  [E_IMPORT_HOSTNAME, E_IMPORT_PRIVATE, E_IMPORT_CREDENTIALS, None(valid)]
json private [None(valid),      None(valid),      E_IMPORT_CREDENTIALS,      None(valid)]
csv  любая колонка credential -> весь файл отвергнут E_IMPORT_CREDENTIALS
```

То есть: hostname и приватные адреса **либо поддержаны сквозным путём**
(явный `EndpointPolicy(public_only=False)`, адрес попадает в `endpoints` и в
`membership` своей коллекции), **либо отвергнуты сразу** — не «принята форма и
потом выброшено». Учётные данные отвергаются в обоих режимах, флагом это не
отключается. Ни одна отвергнутая строка не попадает в `endpoints` — проверено
отдельным тестом.

### 3.5 Canary: секрета нет на диске

Шесть форм ввода прогнаны через preview + commit, затем просканированы все
файлы каталога данных:

```
uri userinfo  -> http://***@45.33.32.156:8080
csv passcol   -> 45.33.32.156 8080
json pass     -> 45.33.32.156 8080
hostpass      -> '45.33.32.156 …' / 'user …' / '***'
pathquery     -> 'http://45.33.32.156:8080 …'
bareline      -> '***'
canary on disk: NONE
import_batch.report_json clean: True
ImportSource repr: ImportSource(name='list.txt', digest='…', size=…, channel='file')
```

Перед правкой 1.1(а) три из этих форм протекали. `ImportSource.__repr__` и
`__str__` текст не отдают.

---

## 4. F05 и F06: что подтверждено, что оказалось не дефектом

### 4.1 F05 — хранилище, версии, обмен

Именованные профили, копирование (новый `profile_id`, `parent_id` указывает на
исходную ревизию, digest совпадает, источник нетронут), версии (update
дописывает строку, предыдущая остаётся читаемой и неизменной, no-op не плодит
ревизию, `base_revision` даёт `E_CONFLICT_REVISION`, архив скрывает но не
удаляет), diff (added/removed/changed/rule/budget/attempts), default-флаг,
отказ на немigrated-таблице (`E_DATA_DB_FOREIGN`).

Экспорт/импорт без секретов и с историей: проверено, что в документе есть
только `secrets: "none"` и типизированные поля; слово `secret` встречается
ровно один раз — в этом объявлении. Импорт восстанавливает всю цепочку
ревизий с теми же digest.

**Паритет GUI/CLI/API**: три поверхности собирают документ по-разному
(объект / argparse.Namespace / JSON-тело) и дают **побайтово одинаковый**
вердикт; запуск по `profile_id` из хранилища даёт тот же digest и тот же
`pass`/`reason`.

```
GUI  pass=True  reason=E_VERDICT_RULE_SATISFIED  digest=cdd9f67085f7
CLI  pass=True  reason=E_VERDICT_RULE_SATISFIED  digest=cdd9f67085f7
API  pass=True  reason=E_VERDICT_RULE_SATISFIED  digest=cdd9f67085f7
все три поверхности дали одинаковый вердикт: True
```

**Приёмка «изменение/сохранение/экспорт не смешивает версии измерений»**:
после `update` head имеет другой digest и другой budget (`max_probes 8` против
`6`), ревизия 1 остаётся с исходным budget и тем же digest, вердикт по
измерениям не изменился.

### 4.2 F05 — непустое подтверждение

Три обязательных случая проверены и на входе (`ProfileSpec.create` →
`E_VALIDATION_FIELD`), и на документе чужого писателя (`evaluate`):

```
all с пустым optional   pass=False reason=E_VERDICT_EMPTY_OPTIONAL_SET
K=0                     pass=False reason=E_VERDICT_K_NOT_POSITIVE
все probes выключены    pass=False reason=E_VERDICT_EMPTY_TARGET_SET
K больше набора         pass=False reason=E_VERDICT_K_UNREACHABLE
без единого измерения   pass=False reason=E_VERDICT_NO_EFFECTIVE_PROBE
```

**Ни одна конфигурация не даёт pass без успешного измеренного probe**: последняя
проверка в цепочке `evaluate` — `effective == 0 → E_VERDICT_NO_EFFECTIVE_PROBE`.
Дополнительно проверено, что профиль без обязательного набора, дошедший из
непроверенного документа, тоже не проходит «просто так»: при `all`/`any`/
`at_least` нужен реальный успешный optional-probe.

### 4.3 F05 — правила, пороги, бюджет

`all` / `any` / `at_least(K)` / `none` на одних и тех же измерениях дают
раздельные результаты по каждой цели и разный общий итог; провал обязательной
цели не спасён ни одним правилом. Target-specific thresholds: `min_success` и
`max_latency_ms` судятся отдельно, `E_TARGET_LATENCY_EXCEEDED` /
`E_TARGET_LATENCY_MISSING` различимы. Бюджет: `max_probes` ниже минимума
отвергается на входе; исчерпанный бюджет останавливает прогон с
`E_LIMIT_BUDGET`, но не портит уже достигнутый вердикт.

**Смена порога не покупает pass** — центральное обещание F05:

```
измерение 900ms под порогом 200ms        -> pass=False
то же измерение под порогом 2000ms        -> pass=False state=unmeasured
                                            reason=E_TARGET_STALE_EVIDENCE
то же измерение, ПЕРЕМЕРЕННОЕ под 2000ms  -> pass=True
fingerprint strict=ef26b2c9 loose=37082b8 (разные)
смена kind/правила не меняет fingerprint  -> True
```

### 4.4 F05 — fail-fast согласован с правилом (исчерпывающе)

Для пяти профилей (all / any / at_least(2) / none / одиночная цель с тремя
попытками) перебраны **все** достижимые состояния прогона; для каждого
проверено: если `stop()` вернул правиловый отказ, то **лучшее** завершение
оставшихся проб обязано давать `pass=False`; если `stop()` не остановил, лучшее
завершение обязано давать `pass=True`.

```
states checked = 216, rule stops = 104, contradictions = 0
```

### 4.5 F06 — каталог

17 presets, 6 capabilities, 7 категорий, 7 наборов. Девять presets
предыдущего приложения переиспользованы без изменения происхождения
(`origin='legacy_app_js'`, ссылка на `ui/app.js -> TARGET_PRESETS`).

Состав каждого preset проверен по факту: id, версия, maintainer, probes,
`pass_condition_ru/en`, `not_proved_ru/en`, cost (запросы, байты, вес бюджета,
rate limit), `definition_checked_on`, `verification`, `definition_source`
(kind+ref), `contract_source`, capability, категории, digest. Пустой
`not_proved` манифест не принимает. Проба отдаёт ровно те поля, которые
принимает сканер (`name/url/method/statuses/contains/sha256/headers`), и её
pass-condition собирается из тех же данных.

Различие названий способностей не переиспользуется: `homepage`, `api`,
`status_endpoint`, `websocket`, `media`, `long_connection` — шесть разных
`title_ru`; три не измеряемые объясняют причину, и пресет на них отвергается.
Проверено, что ни один действующий пресет не заявляет не измеряемую способность.

Запрет обещаний работает и на пользовательских наборах: «Набор со звонками
4K», «Twitch 4K calls», «Полностью работающий Reddit», «Весь сервис YouTube»,
«Steam HD», «Гарантируем YouTube» — отказ; при этом обязательное раскрытие
«Не доказывает 4K и звонки» **разрешено** (узкое правило для описаний).

Отказы манифеста явные, а не молчаливые: новая версия схемы, заголовок с 4K,
пустой `not_proved`, не измеряемая способность, `Authorization` в headers,
userinfo в URL, неизвестное поле пробы, неполный состав сценария, поле
пользователя в сценарии, `at_least` с `K=0`, неизвестный сервис, не-дата,
источник без `kind` — 14 кейсов, все отвергнуты с указанием пути.

Мультивыбор: порядок сохраняется, повторы схлопываются, неизвестные/пустой/
слишком много — отказ с кодом; `estimated_cost` виден до сохранения. Свой набор
(`new_user_set`) получает **собственные копии** пресетов (`user_owned=True`),
переживает round-trip через JSON и не меняется от обновления каталога.

### 4.6 F06 — дефект 24, политика сброса

Каждое из 17 scenario-полей имеет документированное значение сброса в
`SCENARIO_DEFAULTS`; каждое объявленное множество (`scenario_fields` минус
`derived_fields`) заполнено целиком. Переключение elite → video → api → basic
не оставляет ни одного условия предыдущего сценария, а поля пользователя
(`workers`, `rate`, `watch`, `want`, `denylist`) и поля вне набора
(`reputation.timeout`) сохраняются. Значение `null` в наборе — это явный сброс
в `SCENARIO_DEFAULTS`, а не «оставить как было»; поле без значения сброса
отвергается, иначе сброс был бы молчаливым.

Живой вывод после переключения elite → video:

```
ОСТАТКИ ПРЕДЫДУЩЕГО СЦЕНАРИЯ: нет    (то же для api и basic)
поля пользователя сохранены: {'workers': 64, 'rate': 0.5, 'watch': True, ...}
reputation = {'local_enabled': True, 'dnsbl_enabled': False, 'dnsbl_zones': [], 'strict': False, 'timeout': 2.5}
```

### 4.7 F06 — обновление определений

`preview_update` против неизменённого каталога: `set_state='current'`,
`has_changes=False`. Против изменённого манифеста (версия пресета, снятый
deprecated, добавленный сервис) — `has_changes=True`, `breaking=2`, и diff
называет **поля**:

```
reddit-home  updated     v1->v2 breaking=True  полей изменено=3
   pass_condition_ru  'HTTP 200 на главной странице.' -> 'HTTP 200 или 429 на /r/popular.json.'
   probes             '…' -> '…'
   version            1 -> 2
x-home       deprecated  v1->v1 breaking=True  'Публичный API X требует bearer-токен, ...'
instagram-home unchanged v1->v1 breaking=False полей изменено=0
```

**Закреплённый набор не меняется молча**: до и после `preview_update` снимок
дал один и тот же `targets()` и один и тот же digest; round-trip через JSON
даёт то же. `upgrade_set` — единственный способ получить новый снимок, и он
даёт другой digest. Набор, исчезнувший из каталога, даёт `set_state='missing'`,
`upgrade_set` отказывается, а прежний снимок продолжает работать. Пресета,
удалённого из каталога, в снимке тоже нет, и снимок при этом цел.

---

## 5. Требует чужого файла: `proxy_workbench/data/service_sets.json`

Из шести названных в задании сервисов **пять уже в каталоге** и проверены:
`reddit-home`, `tiktok-home`, `x-home`, `steam-store-home`,
`microsoft-oidc-discovery`. У всех `origin='researched'`, `live_checked_on=null`
(живых обращений не делалось — это честно, а не «проверено»), `verification`
у первых четырёх `unverified_live`, у Microsoft `documented` с
`definition_source.kind='official_doc'` и ссылкой на документацию Entra.

**Twitch в каталоге отсутствует.** Файл манифеста не входит в мою область
владения, поэтому я его не правил. Готовое к внесению определение — с
проверкой по официальной документации — лежит в тесте
`tests/test_areacatalog_servicecatalog.py::TwitchPresetProposalTests`
(константа `TWITCH_PRESET`), который уже доказывает, что:

* `parse_catalog` принимает манифест с этим пресетом;
* `definition_source.kind='official_doc'`, `contract_source` указывает на
  `https://dev.twitch.tv/docs/api/reference/`;
* `verification='unverified_live'` и `live_checked_on=null` — без живой
  проверки дата обязана быть пустой;
* пресет находится поиском по «twitch» и по «твич», попадает в категорию
  `video`, участвует в мультивыборе и даёт цель
  `{"url": "https://www.twitch.tv/", "method": "GET", "statuses": [200]}`;
* добавление в набор видно как `set_state='outdated'` с новым `set_version`;
* пользовательский набор из Twitch хранит собственную копию.

**Основание выбора capability.** Чтение официальной документации
(`https://dev.twitch.tv/docs/api/reference/`, один запрос) показало: каждый
эндпоинт Helix требует OAuth-токена, и `Client-Id` в заголовке должен
совпадать с ID в токене. Неаутентифицированного эндпоинта не существует.
Поэтому единственный честный preset — `homepage`, а `not_proved` прямо
перечисляет то, что не доказывается: трансляции и видеопоток, Twitch API и
GraphQL, чат, подписки, региональные ограничения. Способности `websocket`,
`media` и `long_connection` каталог не измеряет, и пресет на них был бы отвергнут
парсером — это проверено отдельным утверждением.

**Что сделать владельцу файла:** добавить объект `TWITCH_PRESET` в
`presets`, увеличить `manifest_version`, и (по желанию) добавить
`'twitch-home'` в `optional` набора `social` с bump его `version` — тогда
`preview_update` покажет пользователю осмысленный diff. Ничего из этого
не требует правок `servicecatalog.py`.

---

## 6. Что требует другого файла (не сделано мной)

1. **API-путь импорта должен передавать policy.** `importer.DEFAULT_POLICY`
   отвергает hostname, приватные адреса и RFC 5737. `proxytool._import_policy`
   (CLI) и `gui.py:3160` это учитывают, но ни один вызов из `api.py` не
   передаёт `EndpointPolicy(public_only=False)`. Нужен явный параметр запроса
   (например `allow_private_endpoints`, как в CLI) с записью выбора в аудит.
   Владелец: `api.py` (+ контракт `openapi.json`).

2. **API/GUI должны уметь показать `header` и переключать его.**
   `Preview.to_dict()['header']` и параметр `preview(..., header=True/False)`
   готовы; поверхности пока их не показывают, поэтому у пользователя нет
   переключателя «первая строка — заголовок» при импорте CSV без слов-ролей.
   Владелец: `gui.py`, `api.py`.

3. **Доступ к `service_sets.json`.** См. §5. Владелец: тот, кому принадлежит
   `proxy_workbench/data/`.

4. **F02 для gateway/export «до конца».** Привязка пула к коллекции и
   `core.Scope` проверены; полный сценарий «scan A при подключённом B» требует
   прогона через `jobs.py`/`gateway.py` в интеграции — там нужен ещё один
   владелец тестов.

5. **F04 вне моей области.** Импортер доводит учётные данные до явного отказа
   (дефект 10), а сквозной путь `импорт → worker → gateway → export` с
   secret-store принадлежит `secrets.py`/`probes.py`/`gateway.py`. Canary в
   этой части не проверялся.

---

## 7. Проверки и состояние

Мои тесты:

```
python3 -m pytest tests/test_areacatalog_importer.py \
                         tests/test_areacatalog_profiles.py \
                         tests/test_areacatalog_servicecatalog.py -q
93 passed, 455 subtests passed
```

Регрессия по моей области (существующие файлы):

```
tests/test_importer_*.py tests/test_profiles_*.py tests/test_servicecatalog_*.py
269 passed, 51 subtests passed
```

Полный прогон репозитория: `2907 passed, 6 failed`. Все шесть падений
воспроизводятся **с неизменёнными моими модулями** (проверено подменой на
версии из `HEAD`) и относятся к чужой области:
`test_probes_reference.py` (матрица измерений в `probes.py`),
`test_extras.py::SingboxTests::test_config` (sing-box в `exportsvc.py`),
`test_freshness.py::ExportFormatTests::test_empty_formats_and_publication_fail_closed`,
`test_acceptance.py::SelectionExportTests::test_an_empty_export_never_selects_a_direct_route`.
Своими изменениями я их не вызываю и не чинил.

## 8. Что при проверке оказалось НЕ дефектом

Чтобы не переписать намеренные контракты, трижды проверял и откатывал:

* **Пустой обязательный набор в `evaluate`.** Форма отвергается
  `ProfileSpec.validate` на входе, а на непроверенном документе проход всё
  равно требует успешного измерения (`E_VERDICT_NO_EFFECTIVE_PROBE` при K=0 /
  all на пустом optional / выключенных probes). Существующий тест
  `test_the_mandatory_set_may_not_be_emptied_by_making_everything_optional`
  намеренно фиксирует `pass=True, effective_probes=1` — это гарантия
  «опирается на измерение», а не «форма должна быть такая». Оставил как есть.
* **Колонка пароля в CSV/JSON отвергает весь файл, даже где ячейка пуста.**
  Осознанно (иначе принимаем строку и молча выбрасываем поле) —
  `test_credentials_column_refuses_the_row_instead_of_dropping_the_field`.
* **Порядок `unknown` / `attempts<=0` / fingerprint в `assess`.** Probe с
  результатом `unknown` не имеет счётчиков, и «skipped» означал бы «проба не
  выполнялась». Три существующих теста фиксируют этот порядок. Оставил, добавил
  комментарий.

Переписывать тест под собственное понимание поведения не стал.
