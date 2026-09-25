# Контракты интеграции Proxy Workbench

**База:** ветка `integration/ultra-2026-09-25`, HEAD `9406372`. Документ написан 25 сентября 2026 года.
**Назначение:** единственная версия контрактов, по которой независимо пишутся остальные исполнители (см. `docs/integration/HANDOFF/README.ru.md`).
**Владелец правки:** интегратор (вместе с владельцами `proxytool.py` и `api.py`). Изменение любого контракта ниже — это бамп версии контракта, а не молчаливая правка потребителя.

## Как читать этот документ

Каждый раздел разделён на две части, и их нельзя смешивать:

- **СЕЙЧАС** — что реально есть в коде на указанной ревизии, с ссылкой на файл и строку. Всё в этой части я прочитал в этой сессии.
- **КОНТРАКТ** — что должно стать истиной после интеграции. Это целевая спецификация, а не описание текущего поведения.

Если пункт из MASTER-PROMPT или REVIEW не закрывается одним из этих двух состояний, он помечен как **ОТКРЫТО** и вынесен в раздел 8. Ничего в разделе «КОНТРАКТ» не является утверждением о том, что код уже так работает.

Проверки, выполненные при написании документа (точные команды и их вывод). Ссылки на `proxy_workbench/ui/*` даны поимённо (функция, `id` элемента), а не по номеру строки: на момент написания эти три файла изменены параллельным исполнителем (`git status --porcelain` показывает `M app.js`, `M index.html`, `M style.css`), и номера строк в них нестабильны.


| Что проверялось | Команда | Результат |
| --- | --- | --- |
| Фактическая схема, которую создаёт `open_db` | `.venv/bin/python` со скриптом, вызывающим `proxy_workbench.proxytool.open_db` на временной БД | `user_version = 0`, `application_id = 0`, `journal_mode = wal`; 5 таблиц, индексов кроме автоиндексов нет |
| Хрупкость позиционной вставки | тот же скрипт: `INSERT INTO results VALUES (?,?,?)`, затем `ALTER TABLE results ADD COLUMN generation TEXT` | `OperationalError: table results has 4 columns but 3 values were supplied` |
| Отсутствие технического backup | `grep -rn "\.backup(\|iterdump\|VACUUM INTO\|wal_checkpoint" proxy_workbench/ packaging/ tests/` | ни одного совпадения (код 1) |
| Отсутствие retention SQL | `grep -rn "DELETE FROM\|VACUUM" proxy_workbench/*.py` | ни одного совпадения |

Полный `unittest discover -s tests` в этой сессии не запускался — по условию задачи его выполняет интегратор после сборки всех модулей. Поэтому утверждения ниже о поведении опираются на чтение кода, а не на прогон тестов.

---

## 1. Identity и scope

### 1.1 Что означает каждая сущность

| Сущность | Что это | Идентификатор | Где живёт сейчас | Статус |
| --- | --- | --- | --- | --- |
| **Endpoint** | Адрес прокси, который можно проверить: схема, хост, порт | каноническая строка `scheme://host:port`, host в нижнем регистре без завершающей точки; IPv6 в квадратных скобках | строковое значение в `candidates.proxy` и в `results.proxy`; парсер `_normalize_proxy` (`proxytool.py:310-349`) | СЕЙЧАС: одна строка, без отдельной сущности |
| **Access** | Способ доступа к endpoint (учётные данные, режим private, ревизия) | **не существует** | — | КОНТРАКТ: `access_id` + `access_revision` |
| **Collection** | Именованная область кандидатов: публичная база или личная коллекция | **не существует** | `candidates` — одна таблица без колонки scope (`proxytool.py:556`) | КОНТРАКТ: `collections` + many-to-many `membership` |
| **Profile** | Конфигурация проверки: цели, таймауты, правила, пороги | `profile = sha256(json.dumps(config, sort_keys=True))[:20]` (`proxytool.py:1298-1299`) | `profiles(id, config)` — только `id` и `config`, без имени и ревизии | СЕЙЧАС: content-addressed хеш, безымянный |
| **Job** | Один запуск работы пользователя | `id = secrets.token_hex(8)` (`gui.py:490`), несохраняемое состояние | `gui-job.json` — один перезаписываемый файл (`gui.py:507`) | СЕЙЧАС: ровно один job, история не ведётся |
| **Pool** | Именованный поддерживаемый набор с desired N | **не существует** как сущность | `gateway.Pool` (`gateway.py:85-184`) — это ротация по текущей публикации, а не пул F14 | КОНТРАКТ: `pool_id` + `desired` + `reserve` |
| **Generation** | Опубликованный снимок экспорта | имя каталога `.generation-XXXXXXXX` внутри `exports/generations/` | `exports/current.json` = `{generation, files, state}` (`proxytool.py:1900-1901`) | СЕЙЧАС: поколение проверяется на совпадение имён, версия схемы не проверяется |

### 1.2 Как они связываются

Цепочка, которую обязан воспроизводить любой потребитель:

```
Collection ──membership──▶ Endpoint ──Access(access_id, access_revision)──▶ Row
                                                    │
Profile(profile_id, profile_revision, digest) ─────┤
                                                    ▼
                                              Observation (одно измерение)
                                                    │
                                         scan store() → results(profile, access, endpoint)
                                                    │
                                                    ▼
Generation (export artifact)  ←── публикация по profile + collection + policy
      │
      ├──▶ api.Exports   (read-only API, /v1)
      ├──▶ gateway.Pool  (шлюз, именованные пулы)
      └──▶ GUI таблица   (пользовательский дизайн)
```

Правила, которые следуют из этой цепочки и обязательны для всех:

1. **Endpoint и Access — разные вещи.** Сейчас результат адресуется ключом `(profile, proxy)` (`proxytool.py:562-564`, вставка на `proxytool.py:1386-1387`). Это означает, что два разных пароля одного адреса дают одну и ту же строку результата, и смена пароля наследует успешную проверку старого. Это прямо запрещено F04 («Новый пароль не наследует успешную проверку старого»). **КОНТРАКТ:** ключ строки измерения — `(profile_id, profile_revision, access_id, access_revision, endpoint_id)`, и смена `access_revision` обязана отзывать прошлое доказательство.
2. **Profile и Collection независимы.** Сейчас `scan` обходит всю `candidates` (`proxytool.py:1342`), а `export` фильтрует только по protocol/countries/hosting (`in_scope`, `proxytool.py:1721-1726`). Коллекции нет ни в схеме, ни в выборке. **КОНТРАКТ:** `collection_id` — обязательный компонент scope в scan, export, API, gateway; публичный discovery остаётся ограниченным публичными адресами, trusted private — отдельный явный режим коллекции со своей destination policy (F04).
3. **Generation — единственная точка истины для потребителей.** Сейчас `api.Exports.load()` (`api.py:112-195`) и `gateway.Pool.refresh()` (`gateway.py:109-117`) следуют за глобальным `current.json` на каждом чтении, поэтому публикация любой другой работы переключает выдачу уже подключённого клиента. Это дефект 8. **КОНТРАКТ:** потребитель фиксирует `generation` (шлюз — в привязке listener, GUI — в состоянии страницы, API — в курсоре) и не переходит на новое поколение без явного действия.
4. **Job фиксирует scope на входе.** Сейчас в `gui-job.json` нет ни `profile`, ни `collection`, ни стран, ни `want`, ни хеша входа (`gui.py:490-495`). **КОНТРАКТ:** job сохраняет `scope` (коллекция, профиль+ревизия, фильтры, бюджеты) и `input_digest`; `resume` не расширяет scope (F11).
5. **Pool — над генерацией, а не вместо неё.** `gateway.Pool` сегодня читает `Exports` и фильтрует `protocol in SUPPORTED`, отбрасывая `https://` (`gateway.py:115`). **КОНТРАКТ:** именованный пул (F14) хранит `pool_id`, `generation`/`bind` и `policy`, а `gateway.Pool` становится ротатором внутри заданной привязки.

### 1.3 Что считать legacy

- `data/last-profile.txt` (`gui.py:408,659,778,819`; `proxytool.py:2416,2510`) — единственный сегодня носитель «активного профиля». Он входит в `RUNTIME_FILES` (`maintenance.py:19`) и удаляется при очистке.
- Корневые файлы экспорта (`proxies.txt`, `ranked.json` и т.д.) — legacy-зеркало активной публикации. `export_file` (`proxytool.py:464-485`) читает их только если каталога `generations/` нет; как только `generations/` есть, fallback на изменяемые корневые файлы запрещён, потому что это смешало бы два снимка.
- Таблица `profiles(id, config)` безымянна; перечисления профилей нет ни в одном маршруте (`gui.py:963-1063`, `api.py:293-353`).

---

## 2. Observation, freshness, admission

### 2.1 Что такое наблюдение

**СЕЙЧАС.** Наблюдение — это одна JSON-строка в `results.payload`, ключ `(profile, proxy)`. Пишет её `store()` (`proxytool.py:1380-1391`) через `INSERT OR REPLACE`. Состав payload задаёт `summarize()` (`proxytool.py:1049-1061`): `reliability`, `min_target_reliability`, `latency_ms`, `jitter_ms`, `score`, `successes`, `requests`, `checked_at`, `samples`; плюс `history` (`next_history`, `proxytool.py:1064-1072`), `country`, `reputation`, `anonymity`, `speed`, `error`.

**КОНТРАКТ.** Наблюдение — неизменяемая запись одного замера:

```
observation {
  job_id, item_id,
  endpoint_id, access_id, access_revision,
  profile_id, profile_revision, profile_digest,
  started_at, finished_at,          // время измерения
  observations: [ sample, ... ],    // per-target, с кодом стадии
  verdict: { reliability, min_target_reliability, ... },
  error: null | { code, stage, detail },
  history: { checks, passes, first_checked, last_ok },
}
```

Инварианты:
- `checked_at` пишется **вместе с измерением** и после этого не пересчитывается (дефект 2, вторая половина).
- `valid_until` вычисляется **один раз**, при записи измерения, из `checked_at` и применённой политики max-age. Переэкспорт с другим `--watch` не меняет время жизни уже измеренного адреса.
- Свежий fail не маскируется старым success: `INSERT OR REPLACE` заменяет payload целиком, а история `checks/passes` накапливается (F09).
- Отменённый recheck сохраняет прежний payload: перезапись происходит только по факту завершённого измерения (дефект 6, текущая механика `store()` + `previous` на `proxytool.py:1318-1330`).

### 2.2 checked_at против published_at

**СЕЙЧАС (проверено по коду).**
- `checked_at` ставится в `summarize()` (`proxytool.py:1060`) и в `unreachable_result()` (`proxytool.py:1274`), то есть это время измерения.
- `published_at` — локальная переменная `export()` (`proxytool.py:1691`), попадает в отчёт как `generated_at` (`proxytool.py:1885`).
- **Смешение есть:** `stamp_freshness(row, watch_minutes, published_at)` при отсутствии или битом `checked_at` подставляет `fallback_checked_at`, а в экспорте это именно `published_at` (`proxytool.py:1090-1094`, вызовы на `proxytool.py:1732` и `proxytool.py:1811`). Время публикации выдаётся за время измерения.
- В публикуемой строке `fields` (`proxytool.py:1788-1790`) есть `checked_at` и `valid_until`; в публичном API `public_row` отдаёт `checked_at`, `valid_until`, `stale` (`api.py:85-89`).

**КОНТРАКТ.**
- `checked_at` — только измерение. Значение «неизвестно» не подменяется публикацией.
- `published_at` — только публикация. Он не участвует в расчёте `valid_until` строки.
- `valid_until` строки = `checked_at + max_age`, где `max_age` — зафиксированная в ревизии профиля величина, а не результат текущего запуска.
- Общий срок набора (`status.valid_until`) — **не** `min()` по строкам. Сейчас это именно `min(exported_valid_until, default=published_at)` (`proxytool.py:1867`), и из-за этого один истёкший адрес обнуляет весь API (дефект 3). **КОНТРАКТ:** у набора есть `state` и `expires_at` как **отдельные** понятия: `state ∈ {complete, partial, error, stale, empty}`, где `empty` = «ничего не подошло», `stale` = «истекло». Сегодня `stale = not has_fresh_export` и `empty_export = not has_fresh_export` — одно и то же значение на два разных смысла (`proxytool.py:1868-1871`, `proxytool.py:1881-1882`).

### 2.3 Admission

**СЕЙЧАС.** Отбор строки — это конъюнкция нескольких независимых проверок, разбросанных по файлам:
- `row_fresh(row, now)` (`proxytool.py:1108-1116`) — единственный временной фильтр;
- `result_allowed(row, min_success, denylist, strict, min_anonymity)` (`reputation.py:296-307`);
- `matches_selection(row, protocol, max_latency, countries, country_of, exclude_hosting, provider_of)` (`proxytool.py:1635-1648`);
- `in_scope(proxy)` внутри экспорта (`proxytool.py:1721-1726`);
- в API — `select(rows, query)` (`api.py:229-246`);
- в GUI — своя выборка с другим набором параметров (`gui.py:620-761`), к `api.select` не обращающаяся.

**КОНТРАКТ.** Один admission-контракт `admit(row, scope, policy, now) -> Admission` с результатом:

```
Admission {
  admitted: bool,
  reason_code: str | null,     // канон из §5.4
  age_seconds: float | null,   // now - checked_at
  checked_at, valid_until, published_at, // видимые потребителю
  max_age_seconds,             // применённая политика
}
```

Обязательные входы: scope (collection, profile+revision, access+revision), наблюдения, TTL, exclusions (denylist, hosting, protocol, country), capabilities. `min_success`/`min_anonymity`/`strict` приходят из **ревизии профиля**, а не как параметр чтения (сейчас порог приходит аргументом в `export` и в `/api/results`, `proxytool.py:1651` и `gui.py:636`, из-за чего один и тот же снимок даёт pass или пустую выдачу в зависимости от значения параметра).

GUI, CLI и API для одного `scope` + `profile_revision` обязаны давать одинаковый состав (F18, приёмка F29 №7).

### 2.4 Unknown и аномалии часов

**СЕЙЧАС (проверено чтением).** `stamp_freshness` (`proxytool.py:1089-1105`):
- нечисловой/пустой/отрицательный `checked_at` заменяется на `fallback_checked_at` (в экспорте — `published_at`), то есть аномалия маскируется под измерение;
- будущий `checked_at` принимается как валидный и продлевает `valid_until` — ограничения на «не дальше now» нет;
- `row_fresh` при `valid_until is None` возвращает `True` (`proxytool.py:1110-1112`), то есть отсутствие TTL означает вечную свежесть;
- `row_fresh` при нечисловом `valid_until` возвращает `False`.

**КОНТРАКТ.** Четыре явных состояния времени, каждое со своим кодом из §5.4 и видимое в API, GUI и отчёте:

| Состояние | Условие | Поведение |
| --- | --- | --- |
| `time_ok` | `0 < checked_at <= now` | обычный путь |
| `time_unknown` | `checked_at` отсутствует или не число | `admitted=false`, `reason_code=TIME_UNKNOWN`; измерение обязательно, чтобы получить pass |
| `time_future` | `checked_at > now + tolerance` | `admitted=false`, `reason_code=TIME_FUTURE`; строка помечается подозрительной, а не «очень свежей» |
| `clock_rollback` | `now < last_seen_now` для этого endpoint | `admitted=false`, `reason_code=CLOCK_ROLLBACK`; требуется новое измерение |

Дополнительно: `time_ok`, но `valid_until <= now` → `admitted=false`, `reason_code=TTL_EXPIRED`. Отсутствие `valid_until` у новой строки (дефект 1) **не** даёт вечной свежести — новая строка без записанного срока несостоятельна и требует переизмерения; legacy-строки читаются по правилу ниже.

**Правило совместимости для legacy-строк.** Чтобы не потерять результаты старых баз, но и не выдать их за свежие: строка без `valid_until` получает синтетический срок `checked_at + MIN_FRESHNESS_SECONDS` (`MIN_FRESHNESS_SECONDS = 2*60*60`, `proxytool.py:420`) **один раз**, при первом чтении новым кодом, и помечается флагом `ttl_backfilled=true`, видимым в API и GUI. Это переходная мера с явной датой удаления; молчаливая «вечная свежесть» запрещена.

**Чего не обещаем.** `freshness_seconds(watch_minutes)` сейчас возвращает `max(7200, 2*watch*60)` (`proxytool.py:1080-1086`), то есть два часа — это нижняя граница, выведенная из `--watch`, а не проверенный оптимум. F09 прямо требует не считать 300 секунд или 2 часа доказанным оптимумом. **КОНТРАКТ:** max-age — настраиваемый и видимый параметр ревизии профиля; значение по умолчанию объявляется как «стартовое», а не как «правильное». Для static TXT автоматический TTL enforcement невозможен — это видно пользователю в тексте, а не обещается (F09).

### 2.5 Что не сделано

- **ОТКРЫТО:** `access_revision` не существует ни в коде, ни в схеме. Пункт приёмки F09 «новая версия доступа одинаково влияет на GUI/API/gateway/new connection» невыполним до появления модели доступа (зависит от `secrets.py` и `apikeys.py`).
- **ОТКРЫТО:** у отчёта нет полей `age`, `admission_reason`, `desired`, `shortfall_reason`, `next_attempt_at`, хотя требования F09 и F14 их требуют.

---

## 3. Схема SQLite

### 3.1 Фактическое состояние (проверено)

`open_db(path)` (`proxytool.py:552-571`) — **единственная** точка создания схемы: `mkdir`, `sqlite3.connect`, `PRAGMA journal_mode=WAL` (`proxytool.py:555`), один `executescript` с пятью `CREATE TABLE IF NOT EXISTS` (`proxytool.py:556-565`) и одна интроспективная проверка колонки (`proxytool.py:566-570`).

Снятая с временной базы, созданной этой функцией, схема:

```
user_version     = 0
application_id   = 0
journal_mode     = wal

candidates(proxy TEXT PRIMARY KEY)
candidate_meta(proxy TEXT PRIMARY KEY, country TEXT, source TEXT)
profiles(id TEXT PRIMARY KEY, config TEXT NOT NULL)
candidate_seen(proxy TEXT NOT NULL, source TEXT NOT NULL, PRIMARY KEY(proxy, source)) WITHOUT ROWID
results(profile TEXT NOT NULL, proxy TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(profile, proxy))
```

Индексов, кроме автоиндексов первичных ключей, нет. За всю историю проекта была **одна** миграция: `ALTER TABLE candidate_meta ADD COLUMN source TEXT` (`proxytool.py:566-570`), идемпотентная по `PRAGMA table_info`. Журнала миграций, порядка, отката и транзакции вокруг DDL нет.

Дополнительно подтверждено:
- **Старый бинарник сегодня не защищён.** Никакой версии в файле нет, а `INSERT OR REPLACE INTO results VALUES (?,?,?)` (`proxytool.py:1386`) и `INSERT OR IGNORE INTO profiles VALUES (?,?)` (`proxytool.py:1300`) не указывают список колонок. Проверено: после `ALTER TABLE results ADD COLUMN generation TEXT` такая вставка падает с `OperationalError: table results has 4 columns but 3 values were supplied`. То есть сегодняшняя «защита» — случайная арифметика SQL, а пользователь получит падение воркера вместо внятного сообщения о версии.
- **Backup до миграции отсутствует.** Ни одного вызова SQLite backup API в проекте нет; `maintenance.py` умеет только удалять (`clear_runtime`, `maintenance.py:72-87`).
- **Retention для БД отсутствует.** Ни одного `DELETE FROM` и `VACUUM` в пакете нет. `results` растёт бессрочно, а `checked_at` лежит внутри JSON payload, поэтому возрастную очистку невозможно даже выразить в SQL.
- **Указатель поколения не защищён.** `exports/current.json` = `{generation, files, state}` (`proxytool.py:1900-1901`); checksum, размера и времени в нём нет. Смена поколения у потребителей определяется `st_mtime_ns`/`st_size` (`api.py:127-128`).
- **Версия снимка пишется, но не проверяется.** `SNAPSHOT_SCHEMA_VERSION = 1` (`proxytool.py:419`) попадает в `status.json` (`proxytool.py:1874`) и эхом в `/status` (`api.py:303`); ни один читатель его не проверяет.
- **Рост БД ускоряется версией продукта.** `profile` — это sha256 от всего конфига, включая `request_profile_digest` (`proxytool.py:1298-1299`, `proxytool.py:982`), который хеширует User-Agent с версией (`branding.py:22,49-52`). Любой бамп версии создаёт новый профиль, и все строки предыдущего становятся нечитаемым мусором.

### 3.2 Целевая схема версионирования

**КОНТРАКТ.**

- `SCHEMA_VERSION` — целое в Python, и `PRAGMA user_version` — зеркало того же числа в файле. Они обязаны совпадать; расхождение — ошибка чтения базы, а не повод «дописать».
- `PRAGMA application_id` — фиксированное магическое число пакета. Несовпадение означает «это не наша база», отказ без записи.
- Каждая миграция — функция в `proxy_workbench/db.py` с номером, выполняется в одной транзакции, идемпотентна, применяется строго по порядку.
- `PRAGMA foreign_keys=ON` для всех новых соединений.
- Перед первой миграцией, которая что-то меняет в уже существующей базе, — технический backup согласованным путём SQLite (`VACUUM INTO` как минимальная гарантия целостности при закрытом writer, либо `sqlite3.Connection.backup`), плюс `manifest` с checksum, размером, датой и версией схемы.
- Все `INSERT` в общих таблицах указывают список колонок явно. Это убирает зависимость от порядка колонок и делает добавление колонки безопасным.

### 3.3 Порядок миграций

Порядок фиксирован здесь. Каждая строка — отдельная миграция; откат делается восстановлением pre-migration backup, а не обратной DDL.

| № | Что добавляется | Почему в этом месте | Владелец модуля |
| --- | --- | --- | --- |
| **0** | Установить `application_id`; создать `schema_migrations(version INTEGER PRIMARY KEY, applied_at REAL, app_version TEXT, backup_path TEXT)` | Журнал нужен до первой настоящей миграции, иначе нечем доказать, что база уже мигрирована | `db.py` |
| **1** | `collections(id, name, kind, archived_at, created_at)` + `membership(collection_id, endpoint_id, added_at, origin, PRIMARY KEY(collection_id, endpoint_id))` | Коллекции нужны раньше импорта и раньше secret/access: импорт с `collection_id` — уже отдельный модуль, а без таблиц он не сможет записать ни одной строки | `db.py` |
| **2** | `endpoints(id, canonical, host, port, scheme, ip_version, country, country_source, country_at, asn, provider, hosting, cidr, first_seen_at, last_seen_at)` | Разделение endpoint и access требует отдельной строки endpoint; без неё §1.2 не выполняется | `db.py` + `geo.py` |
| **3** | `accesses(id, endpoint_id, mode, secret_ref, access_revision, created_at, rotated_at)` | `secret_ref` — **непрозрачная ссылка** на vault, не значение. Значение секрета в SQLite не попадает никогда | `db.py` + `secrets.py` |
| **4** | Колонки в `results`: `observation_id`, `access_id`, `access_revision`, `profile_revision`, `checked_at REAL`, `valid_until REAL`, `error_code`, `error_stage` | Ключ наблюдения перестаёт быть `(profile, proxy)`; `checked_at`/`valid_until` становятся колонками, без чего retention невыразим в SQL | `db.py` |
| **5** | `job`, `job_item`, `job_event`, `checkpoint` | Задания должны пережить перезапуск, иначе не закрывается F11 | `jobs.py` |
| **6** | `pools`, `pool_member`, `schedules`, `schedule_run` | Постоянный пул и расписание — сущности с собственным состоянием | `pools.py`, `scheduler.py` |
| **7** | `api_keys(id, prefix, name, purpose, created_at, expires_at, last_used_at, revoked_at, permissions_json, resource_scope_json, rate_limit_json, concurrency_json, rotation_grace_until, verifier, verifier_salt, verifier_algo)` | `verifier` — только односторонний; plaintext секрета в базе нет | `apikeys.py` |
| **8** | `profiles`: `name`, `revision`, `parent_id`, `digest`, `created_at`, `archived_at`, `is_default` + `profile_revision` в `results` | Именованные профили с историей версий (F05) | `profiles.py` |
| **9** | `audit_log(at, key_id, operation, object_kind, object_id, scope_json, result, error_code)` | Локальный журнал без полных ключей, паролей, тел ответов и трафика | `apikeys.py` |
| **10** | `export_artifact(id, kind, collection_id, profile_id, generation, published_at, expires_at, state, reason_code, manifest_json)` | Артефакты выделенного экспорта должны быть отдельными сущностями (дефект 7) | `exportsvc.py` |
| **11** | `import_batch(id, collection_id, created_at, state, report_json, revision)` | Идемпотентный commit импорта и отчёт (F03) | `importer.py` |
| **12** | Индексы: `results(profile_id, valid_until)`, `results(access_id, access_revision)`, `membership(endpoint_id)`, `job_item(job_id, state)`, `observations(endpoint_id, checked_at DESC)` | Retention и выборки по §2 невыразимы без них | `db.py` |

Правила совместимости:
- Миграции только аддитивные. Удаление колонки — отдельная миграция с явным `retention` предыдущего шага, а не «почистить заодно».
- Миграция обязана быть безопасна при повторном запуске (открытие уже мигрированной базы не меняет ничего).
- Ни одна миграция не имеет `user_version` ниже текущего: файл с меньшей версией, чем у кода, — это «старая база», и она обязана открываться и мигрироваться, а не отвергаться. Файл с **большей** версией — отказ: «база новее этой программы».
- `candidates`/`candidate_meta`/`candidate_seen`/`results`/`profiles` остаются единственными таблицами, к которым имеют право обращаться сбор, скан и экспорт. Всё остальное строится поверх них через `endpoints`/`membership`.

### 3.4 Что происходит со старыми базами

| Ситуация | Поведение |
| --- | --- |
| БД без `user_version` (= 0), созданная текущим кодом | Открывается. Применяются миграции 1–12 по порядку. До первой миграции, меняющей данные, создаётся pre-migration backup + manifest. Legacy-кандидаты переносятся в коллекцию с честным происхождением: `origin='legacy'`, `kind='public'`, имя «Ранее собранные». Метка «свои» не выдумывается (F02, дефект 11) |
| БД с `user_version` равным текущей | Открывается без изменений, миграции пропускаются |
| БД с `user_version` выше текущей | Отказ с сообщением «база создана более новой версией программы»; **никаких записей**, включая `user_version` |
| `application_id` не совпадает | Отказ: «это не база Proxy Workbench» |
| Файл повреждён или открыт в WAL без согласованного пути | Отказ, а не пересоздание. Пересоздание пустого файла на месте существующего запрещено |
| Перенос `data/` при смене пути (frozen ↔ checkout ↔ per-user) | `paths.default_data` (`paths.py:12-28`) переключается между вариантами без переноса содержимого. **КОНТРАКТ:** `db.py` предоставляет `migrate_data_path(old, new) -> preview`, и перенос выполняется только после согласия пользователя и с backup старой папки |

### 3.5 Как старый бинарник не должен писать в новую схему

Требование F24: «Старый binary не пишет в новую schema». Механизмы, каждый из которых нужен, потому что сегодня не работает ни один:

1. **Версия в файле.** Старый код не знает про `user_version` и не проверит его — поэтому защита строится на том, что **новый** код виден старому, а не наоборот. Единственный надёжный барьер, доступный обоим: сделать так, чтобы новая схема ломала старый код **немедленно и без записи в БД**. Практически: добавить в `results` колонку с `NOT NULL DEFAULT` так, чтобы позиционный `INSERT ... VALUES (?,?,?)` (старый код) перестал работать. Старый воркер упадёт на первой записи, а не допишет мусор. Проверяемый сценарий: открыть мигрированную базу старым путём вставки → `OperationalError`, база не изменена.
2. **Явные списки колонок в новом коде** — чтобы новая схема не ломала **новый** код при будущих `ADD COLUMN` (сегодня ровно наоборот: `proxytool.py:1386, 1300`).
3. **Проверка версии на чтении и на записи.** `open_db` обязан: при `user_version > SCHEMA_VERSION` — отказ; при `user_version == 0` и `application_id` задан — «это база, созданная до версионирования», мигрировать; при несовпадении `application_id` — отказ.
4. **Отказ = без записи.** Проверка версии выполняется **до** любого `CREATE`/`ALTER`/`INSERT`, включая `INSERT OR IGNORE INTO profiles` (старый код пишет его в начале скана, `proxytool.py:1300`).
5. **Rollback** — восстановление pre-migration backup в отдельный путь; не обещать lossless downgrade неизвестных полей (F24). Мигрированная БД сохраняется отдельно и не затирается.

### 3.6 Retention и очистка

**СЕЙЧАС.** Единственная ретенция — экспортные поколения: `EXPORT_GENERATION_RETENTION = 3` (`proxytool.py:418`), `prune_export_generations` (`proxytool.py:443-461`), вызовы на `proxytool.py:1694` и `proxytool.py:1925`, с явной ошибкой при невозможности удалить (`proxytool.py:1695-1696`). Очистка данных — `clear_runtime` (`maintenance.py:72-87`), деструктивная, без preview и без отчёта о размере; CLI печатает список удалённого уже после удаления (`proxytool.py:2346`).

**КОНТРАКТ.**
- Retention для `observations` и `results` задаётся политикой ревизии профиля и выражается в SQL по колонкам `checked_at`/`valid_until` (миграция 4). Значения по умолчанию — стартовые, не «правильные», и видимы пользователю.
- `clear_runtime` получает режим `preview` (что будет удалено, сколько байт) и `execute`. Разделение runtime/user-файлов сохраняется: `RUNTIME_FILES`/`RUNTIME_DIRS` (`maintenance.py:14-32`) не включают `gui-settings.json` и `denylist.txt`, и это не должно измениться.
- `clear`/`remove` инвалидирует потребителей и кэши явно, а не по факту исчезновения файла. Сегодня инвалидация работает как следствие (`api.Exports._clear`, `api.py:105-110`; пустой пул в шлюзе — `gateway.py:352-354` NO_PROXIES, без скрытого DIRECT), и это поведение нужно сохранить, а не дублировать.
- Секреты при очистке не затрагиваются, если они живут в отдельном vault. Если vault окажется в `data/` — он добавляется в `RUNTIME_FILES` явно, отдельной строкой, владельцем `secrets.py`.

---

## 4. Контракт snapshot и экспорта

### 4.1 Что уже гарантировано кодом (сохранить)

Эти механизмы работают и должны быть унаследованы новым кодом, а не заменены:

1. **Поколение неизменяемо.** `export()` создаёт новый каталог `tempfile.mkdtemp(prefix='.generation-', dir=generations)` (`proxytool.py:1697`) и пишет в него. Файлы поколения не переписываются.
2. **Указатель переключается атомарно и с откатом.** `_publish_legacy_files` (`proxytool.py:488-539`) сначала делает staged-копии, затем rename-backup'ы, и только потом вызывает `before_commit` — запись указателя (`proxytool.py:1913-1915`). При сбое прежний набор остаётся единственным опубликованным (`proxytool.py:1916-1923`), а `last-profile.txt` откатывается через `_restore_bytes` (`proxytool.py:542-549`).
3. **Потребители не смешивают поколения.** `export_file` не откатывается на изменяемые корневые файлы, если есть `generations/` (`proxytool.py:464-485`); `api.Exports.load` отбрасывает поколение при несовпадении `status['generation']` с именем из указателя или отсутствии `status.json` (`api.py:137-138, 142-147`); шлюз читает через тот же `Exports` (`gateway.py:109-117`).
4. **Диагностический снимок не публикуется.** `diagnostic=True` пишет `diagnostic.json` и не трогает `last-profile.txt` (`proxytool.py:1899-1904`).
5. **Fail-closed.** Пустой набор даёт совместимый отказ, а не скрытый DIRECT: `formats.clash` — `MATCH,REJECT` (`formats.py:62-64`), `formats.singbox` — outbound типа `block` (`formats.py:91-96`).
6. **Полный откат снимка при провале.** Неопубликованная генерация удаляется в `finally` (`proxytool.py:1926-1929`).

### 4.2 Контракт поколения

```
generation = .generation-<8+ случайных символов>   # только [A-Za-z0-9-], без '/' и '\\'
```

Указатель (сегодня `exports/current.json`, `proxytool.py:1900-1901`):

```json
{
  "generation": ".generation-XXXXXXXX",
  "files": ["proxies.txt", "ranked.json", "..."],
  "state": "complete",
  "schema_version": 2,
  "scope_digest": "<sha256 от канонического scope>",
  "profile_id": "...", "profile_revision": 1,
  "collection_id": "...",
  "published_at": 1790359369.056174,
  "expires_at": 1790366569.056174,
  "manifest": {"ranked.json": {"bytes": 12345, "sha256": "..."}, "..."}
}
```

**КОНТРАКТ:** `schema_version` **обязан проверяться** каждым читателем. Несовместимая версия → отказ читать строки, а не обслуживание как валидной. Сегодня `SNAPSHOT_SCHEMA_VERSION = 1` (`proxytool.py:419`) пишется, но не проверяется ни API, ни GUI, ни шлюзом.

`files` и `manifest` обязательны: `manifest` с размером и checksum закрывает требование F24 «manifest/checksum» и убирает эвристику смены поколения по `st_mtime_ns`/`st_size` (`api.py:127-128`). Ключ перечитывания у `Exports` становится `(generation, schema_version, manifest_digest)`.

### 4.3 Контракт `status.json`

Поля, которые уже читают потребители, и потому обязаны остаться: `schema_version`, `profile`, `generation`, `state`, `stop_reason`, `scope{protocol,countries,exclude_hosting}`, `scope_candidates`, `checked`, `pending`, `passed`, `candidates`, `local_filtered`, `exported`, `complete`, `generated_at`, `valid_until`, `stale`, `empty_export`, `sort`, `min_success`, `protocol`, `max_latency`, `countries`, `exclude_hosting`, `query`, `quick`, `watch_minutes`, `selection_requested`, `selection_exported`, `selection_missing`, `selection_truncated`, `targets`, `request_profile`, `request_profile_digest`, `reputation`, `anonymity`, `source_quality`, `source_health_basis`, `breakdown` (`proxytool.py:1873-1897`).

Поля, которые добавляются (только новые; существующие не переименовываются и не меняют смысл):

| Поле | Смысл | Закрывает |
| --- | --- | --- |
| `max_age_seconds` | Применённая политика срока, видимая пользователю | F09 «max-age настраиваем и видим» (сегодня `watch_minutes` пишется, но в `/status` не отдаётся — `api.py:305-320`) |
| `expires_at` | Время истечения **набора**, отдельно от `valid_until` строк | дефект 3, первая половина |
| `state_detail` | Машиночитаемая причина: `empty_no_match`, `all_expired`, `all_failed`, `budget_exhausted`, `stopped`, `cancelled`, `crashed` | дефект 3, вторая половина; F10 «почему 0 результатов» |
| `deficit_reasons[]`, `desired`, `next_attempt_at` | Недобор пула с причиной и следующей попыткой | F14 |
| `collection_id`, `profile_id`, `profile_revision` | Явная привязка артефакта | дефект 8 |
| `compat` | Отчёт совместимости: какие строки и почему не поддержаны целевым клиентом | F28 |

Разделение `state` и `stop_reason` (сегодня одно и то же значение на два смысла, `proxytool.py:1868-1871`): `state ∈ {complete, partial, stale, empty, error}`, `stop_reason ∈ {complete, want_reached, recheck_passing, stopped, cancelled, error, expired, budget}`.

### 4.4 Контракт строки

Поля публикуемой строки (сегодня `fields` в `ranked.csv`, `proxytool.py:1788-1790`):

```
proxy, country, exit_ip, exit_country, asn, provider, hosting, cidr,
protocol, latency_ms, jitter_ms, reliability, min_target_reliability,
successes, requests, checks, passes, score, uptime,
checked_at, valid_until, age_seconds, admission_reason,
anonymity, anonymity_signals, reputation_status, reputation_sources,
speed{mbps,bytes,ms,state}, listed_in, source_keys, recommended, tags
```

Правила:
- `proxy` пишется дословно во все девять артефактов (`proxytool.py:1791, 1830-1838`) и в `public_row` (`api.py:65`). Credentials в эту строку **не попадают** никогда; если endpoint имеет доступ, строка ссылается на него через отдельное поле, а не через userinfo.
- `age_seconds` = `now - checked_at`, вычисляется при выдаче, а не хранится.
- `admission_reason` присутствует всегда и объясняет допуск (F09 «объяснение допуска»). Сегодня в `public_row` (`api.py:51-89`) ни `age`, ни причины нет.
- Строка, не прошедшая admission, в выдачу не попадает, но её причина обязана быть видна в агрегатах `status.json` — иначе пользователь не отличит «ничего не нашлось» от «снимок истёк».

### 4.5 Экспорт выделенного, top-N и поисковой выборки

**СЕЙЧАС (подтверждено сверкой).** GUI-путь: обработчик «Download Selected» вызывает `start('export', Array.from(selectedProxies))` (`ui/app.js`, bulk-панель; на момент написания — строка 4261) → `gui.py:383-458` → `proxytool.py:2511` `export_now(selected=allowed_proxies)` с `diagnostic=False` → публикация `current.json` и `last-profile.txt` (`proxytool.py:1900-1924`). То есть экспорт выделенного **переключает активный пул** (дефект 7). Механизм непубликующего артефакта в коде уже есть, но используется только для прерванной проверки (`proxytool.py:1902-1904`). Это поведение зафиксировано тестом `tests/test_selection.py:275`, который после выборочного экспорта ожидает `/api/download/proxies.txt` с одной выбранной строкой; при исправлении дефекта 7 тест переписывается, а не удаляется.

**КОНТРАКТ.**
- Три вида артефакта, различаемых полем `kind`: `published` (активный пул), `selection` (выделенные строки), `diagnostic` (прерванная проверка).
- `kind='selection'` и `kind='diagnostic'` **никогда** не пишут `current.json` и не трогают `last-profile.txt`.
- Активный пул меняется **только** действием `publish`/`bind` — явным, с подтверждением, и только для `kind='published'`.
- `export_artifact` (миграция 10) хранит, какой collection/profile/generation лежит в артефакте, чтобы скачивание всегда отдавало файл той выборки, которую видит пользователь.
- Выделение привязано к scope: смена фильтров, коллекции или generation сбрасывает выделение либо показывает явное предупреждение, что выделение относится к прежнему scope (F19).

### 4.6 Пустой, истёкший и неподдерживаемый набор

- `state='empty'` + `state_detail` различает «ничего не подошло под фильтр», «все кандидаты провалились» и «бюджет исчерпан». Сегодня все три дают `state='stale', stop_reason='expired'` (`proxytool.py:1868-1872`).
- Пустой набор → совместимый fail-closed (`formats.py:62-64, 91-96`) либо понятная ошибка генерации. Скрытый DIRECT запрещён.
- Внешние конфиги валидируются **целевой версией клиента**, а не только JSON-парсером. `type: block` в `singbox.json` (`formats.py:91-96`) — это правило, совместимость которого с целевой версией sing-box ещё не подтверждена; версия клиента обязана быть частью проверки совместимости (R14, дефект 20).
- Expired static TXT не может отозвать себя сам — это показывается пользователю текстом, а не обещается (F28).

---

## 5. API: права, scope, ошибки, единицы, события, курсоры

### 5.1 Identities — разные и не взаимозаменяемые

**СЕЙЧАС.** Реально существует два секрета, и один из них используется в трёх ролях:
- токен GUI-сессии: `secrets.token_urlsafe(32)` в памяти (`gui.py:227`), вставляется в страницу (`gui.py:969`), проверяется как `X-Workbench-Token` (`gui.py:958`);
- токен API/шлюза: из `--api-token`/`PROXY_WORKBENCH_API_TOKEN` (`api.py:249-252, 276-281`). В CLI один флаг обслуживает и API, и шлюз (`proxytool.py:2143, 2172`). В GUI при не-loopback bind без `--gateway-token` паролем шлюза становится **токен GUI-сессии** (`gui.py:1129-1134`), и он же отдаётся в `gateway_state()` (`gui.py:314-319`) и уходит в QR — в сборке ссылки для телефона `username`/`password` подставляются из `value.gateway` (`ui/app.js`, функция рендера мобильного подключения; на момент написания — строка 3042).
- `--gateway-host` по умолчанию `0.0.0.0` (`gui.py:1089`), то есть LAN-режим включён по умолчанию.

**КОНТРАКТ.** Четыре разные identity, ни одна не подставляется вместо другой (F29, дефект 18, R12):

| Identity | Где живёт | Куда передаётся | Права |
| --- | --- | --- | --- |
| `api_key` | Односторонний verifier в БД, секрет показывается один раз | `Authorization: Bearer` | По §5.2 |
| `gui_session` | Только память процесса GUI | `X-Workbench-Token` + проверка Host/Origin | Локальное управление GUI; **не** администратор API |
| `gateway_password` | Отдельный секрет, не наследуется от `gui_session` | Basic/SOCKS5 от клиента шлюза | Только аутентификация клиента шлюза |
| `upstream_credential` | OS vault или session-only адаптер, в SQLite — только ссылка | Внутренне, worker получает ссылку, а не значение | Никаких прав API |

Правила:
- Default bind — loopback. LAN включается **явно** с видимым состоянием и выбором интерфейса; `0.0.0.0` по умолчанию запрещён (`gui.py:1089` — точка правки).
- `serve --host 0.0.0.0 --api-token` без TLS не выдаётся за удалённый доступ. Либо документированная TLS-конфигурация, либо поддерживаемый reverse-proxy setup (F29 «Сетевая модель»).
- Первичный административный ключ выдаётся только локальным доверенным bootstrap'ом (GUI/CLI на loopback). Read-only ключ не может создать себе admin.
- Для клиентов подписок, не умеющих заголовки, — отдельный узкий read-only subscription secret с expiry/revoke и redaction. Legacy `?token=` (`api.py:280`) — ограниченный compatibility path с предупреждением, а не тихая потеря старой интеграции.

### 5.2 Permissions

Канонический набор (F29 «Права и scope»). Строковые значения, сравнение точное, по умолчанию — ничего не разрешено:

```
read.status          read.results        read.results.detail
read.export.artifact
collections.read     collections.write    import.read       import.commit
profiles.read        profiles.write
sources.read         sources.write
jobs.read            jobs.submit          jobs.control
pools.read           pools.write
gateway.read         gateway.write
schedules.read       schedules.write
export.create        export.secret       # включение credentials — отдельное чувствительное право
admin.settings       admin.keys           admin.audit
```

Инварианты:
- Object-level проверка выполняется у **каждой** операции: batch, job status, event stream, download, export artifact. Нельзя получить чужой scope через counts, errors, filters, `allow_private`, raw path или запущенный job.
- `export.secret` — отдельное право; по умолчанию значения редактированы, а gateway-reference не выдаётся за готовый direct-auth URI (F28).
- Legacy read-only токен сохраняет ровно свои нынешние права и **не** получает admin/приватных полномочий при миграции (F29 «Что уже есть»).
- Отзыв и expiry проверяются на каждом запросе и на новых подписках/leases; активные SSE-сессии завершаются или перепроверяют срок по документированной политике.

### 5.3 Resource scopes

- Ключ ограничивается конкретными `collection_id` и `pool_id`. Пустой список = «все, что разрешено правами»; явный список = «только это».
- Курсор и фильтры **сужают** выдачу, но не расширяют её: клиент не может получить чужой scope, задав `collection_id` чужой коллекции.
- Counts и errors не раскрывают чужие приватные scope: для запрещённого объекта возвращается тот же код, что и для отсутствующего.
- `scope_digest` из §4.2 позволяет потребителю проверить, что его выборка относится к той же области, что и отображаемая таблица.

### 5.4 Error codes

Единый канон, общий для CLI, GUI и API. Формат: `E_<DOMAIN>_<REASON>`, домен в верхнем регистре, причина в верхнем регистре. Текст — локализуемый, код — нет.

| Домен | Примеры кодов | HTTP |
| --- | --- | --- |
| `AUTH` | `E_AUTH_MISSING`, `E_AUTH_INVALID`, `E_AUTH_EXPIRED`, `E_AUTH_REVOKED`, `E_AUTH_SCOPE`, `E_AUTH_PERMISSION`, `E_AUTH_RATE_LIMITED` (с `Retry-After`), `E_AUTH_ROTATION_GRACE` | 401 / 403 / 429 |
| `VALIDATION` | `E_VALIDATION_SCHEMA`, `E_VALIDATION_FIELD`, `E_VALIDATION_UNKNOWN_FIELD` | 400 |
| `CONFLICT` | `E_CONFLICT_REVISION`, `E_CONFLICT_IDEMPOTENCY`, `E_CONFLICT_BUSY` (данные заняты другим запуском — сценарий `workbench.lock`, `proxytool.py:2323-2335`) | 409 |
| `STATE` | `E_STATE_NO_SNAPSHOT`, `E_STATE_SNAPSHOT_STALE`, `E_STATE_SNAPSHOT_SCHEMA`, `E_STATE_NO_PROXIES`, `E_STATE_JOB_RUNNING`, `E_STATE_POOL_EMPTY` | 409 / 410 |
| `TIME` | `E_TIME_UNKNOWN`, `E_TIME_FUTURE`, `E_TIME_CLOCK_ROLLBACK`, `E_TIME_TTL_EXPIRED` | 200 с `admitted=false` либо 409 при ручном экспорте |
| `DATA` | `E_DATA_DB_VERSION_AHEAD`, `E_DATA_DB_FOREIGN`, `E_DATA_MIGRATION_FAILED`, `E_DATA_BACKUP_FAILED` | 409 / 500 |
| `IMPORT` | `E_IMPORT_REVISION`, `E_IMPORT_FORMAT`, `E_IMPORT_PARTIAL` | 400 / 409 |
| `SECRET` | `E_SECRET_VAULT_LOCKED`, `E_SECRET_UPSTREAM_AUTH_REQUIRED`, `E_SECRET_UPSTREAM_AUTH_FAILED`, `E_SECRET_NOT_PROVIDED` | 409 / 502 |
| `LIMIT` | `E_LIMIT_BODY`, `E_LIMIT_QUEUE`, `E_LIMIT_CONCURRENCY`, `E_LIMIT_BUDGET` | 413 / 429 |
| `GATEWAY` | `E_GATEWAY_NO_UPSTREAM`, `E_GATEWAY_DEADLINE`, `E_GATEWAY_SLOT_UNAVAILABLE`, `E_GATEWAY_TRANSPORT_UNSUPPORTED` | 502 / 504 |
| `UPSTREAM` | коды измерения: `UNREACHABLE`, `DNS_TIMEOUT`, `DNS_ERROR`, `NO_DNSBL_ZONES`, `INVALID_PROXY`, `HTTP_3XX/4XX/5XX`, `BODY_TOO_LARGE`, `CONTENT_MISMATCH`, `HASH_MISMATCH`, `SCREEN_ERROR` | — (в данных строки) |

Требования к кодам:
- `E_*` стабильны и документированы; перевод существует отдельно от кода (F10, F25).
- Сегодня коды измерения существуют только литералами в коде (`proxytool.py:1043` кладёт `type(exc).__name__`, то есть `ConnectError`, `ConnectTimeout`, `ReadTimeout`, `ProxyError`, `SSLError` сливаются в одно) и не описаны ни в одном документе. **ОТКРЫТО:** справочника кодов в репозитории нет.
- Каждая полезная ошибка содержит действие, а не только класс исключения (F25).

### 5.5 Единицы

Единицы фиксируются один раз и одинаковы во всех трёх интерфейсах (F07, F18):

| Величина | Единица | Имя в контракте | Диапазон, заданный сейчас |
| --- | --- | --- | --- |
| Время ожидания connect/handshake/read | секунды, float | `timeout_s`, `connect_timeout_s` | `timeout > 0`, `connect_timeout > 0` (`proxytool.py:2276-2277`) |
| Общая длительность пробы | секунды | `whole_probe_timeout_s` | **не существует** (дефект 23) |
| Интервал watch | минуты | `watch_minutes` | `0 <= watch`, в GUI `0..1440` (`gui.py:153`) |
| Срок годности результата | секунды | `max_age_seconds` | сейчас выводится из watch (`proxytool.py:1080-1086`) |
| Полоса пропускания | Мбит/с, float | `mbps` | вместе с `bytes` и `ms` обязательны (F20) |
| Задержка | мс, float или null | `latency_ms` | null, если нет успешных проб (`proxytool.py:1057`) |
| Тело ответа | байты, int | `max_bytes` | валидируется в одном месте (`proxytool.py:916-929`) |
| Число попыток | целое | `attempts` | `>= 1` |
| Размер тела API-запроса | байты, int | `max_body_bytes` | GUI: 32 МиБ (`gui.py:37`) |

Правила: любая новая величина имеет имя, единицу и диапазон в одном месте; три интерфейса используют одно и то же имя; молчаливое игнорирование непредусмотренного параметра запрещено (F07).

### 5.6 События

- Structured events, а не разбор агрегированного лога (дефект 25, R17). Сегодня `update_progress` (`proxytool.py:2400-2403`) атомарно **перезаписывает** один агрегированный `gui-progress.json`, а живая лента в UI парсит `value.log` регуляркой `/\b(OK|PASS|SUCCESS|FAIL|ERR|ERROR)\b/i` (`ui/app.js`, контейнер `#live-ticker-list`; на момент написания — строка 2981).
- Поток: `GET /v1/events?job_id=…&cursor=…` → `text/event-stream`. События: `job.state`, `job.progress`, `item.observation`, `item.verdict`, `generation.published`, `pool.state`, `quota.state`.
- Формат события: `{"seq": int, "at": unix_seconds, "type": str, "job_id": str|None, "item_id": str|None, "code": str, "data": {...}}`.
- `seq` монотонен в пределах потока и используется как курсор. Событие содержит `code` из §5.4, а не текст исключения.
- Событие по измерению — одно на item, а не сводка: строка «Проверено 512/1000» (печатается в `proxytool.py:1485`) событием измерения **не является**.
- Персональные данные и секретов в событиях нет; `access_id` передаётся без значения credentials.

### 5.7 Курсоры и пагинация

- Курсор — непрозрачная строка, кодирующая `(generation, sort, filter_digest, offset)`. Клиент не может изменить `filter_digest` и выйти за пределы выданного scope: несовпадение → `E_VALIDATION_FIELD`.
- `limit` ограничен сверху. Сегодня `MAX_LIMIT = 1_000_000` (`api.py:31`) и `/proxies` сериализует весь массив за один запрос (`api.py:345`) — это запрещено («API отзывчиво во время scan», приёмка F29 №8).
- Сортировка — из закрытого списка (`EXPORT_ORDERS` уже существует в `proxytool.py`; в API и GUI наборы различаются, что само по себе нарушает «один scope — один состав»).
- `max_age` — фильтр по возрасту, выраженный в секундах (§2), а не только по `valid_until` поколения.
- Режимы `unknown` и `stale` — явные параметры, а не молчаливое исключение строк.

### 5.8 Совместимость

- Версионированный префикс `/v1` обязателен. Сегодня ни одного маршрута с версией нет (`api.py:293, 320`).
- `Exports` (`api.py:93-195`) импортируется извне (`gui.py:234`, `proxytool.py:2195-2196`), поэтому его публичные `load()` и поля `rows`/`status` нельзя переименовывать или менять семантику без синхронной правки этих мест. Расширять — можно.
- `schema_version` в `/status` относится к схеме снимка, а не к API; эти две версии не должны путаться. Сейчас обе отсутствуют.
- OpenAPI — отдельный артефакт, генерируется из одного описания маршрутов, примеры — без canary-секретов (приёмка F29 №6).

---

## 6. Состояния job и item

### 6.1 Что есть сегодня

- Ровно один job: subprocess. `App.start` под `self.mutex` отказывает, если job уже идёт (`gui.py:373-374`), пишет `gui-job.json` и запускает `_wait` в daemon-потоке (`gui.py:507-508`). `stop()` — единственное управляющее действие: создание stop-файла (`gui.py:522-527`).
- Per-item состояние **неявное**: наличие строки в `results(profile, proxy)` плюс поля `error`/`checked_at`/`valid_until`. Явной машины состояний нет.
- «Очередь» пересчитывается каждый запуск: `pending` строится из `candidates` минус `done` (`proxytool.py:1341-1348`), а `done` — это «не ошибка и `row_fresh`» (`proxytool.py:1335-1340`). Bounded backpressure есть: `asyncio.Queue(maxsize=workers*2)` и `incoming` (`proxytool.py:1371-1372`).
- Checkpoint — per-row commit каждые 100 строк или ≥1 с (`proxytool.py:1394-1396`) плюс `db.commit()` в `finally` (`proxytool.py:1504`). Именованного checkpoint-артефакта нет.
- Structured progress — один агрегированный снапшот: `publish()` (`proxytool.py:1457-1485`) и `update_progress` (`proxytool.py:2400-2403`).
- Идемпотентности управляющих действий нет: повторный `POST /api/start` после завершения создаёт новый job с новым id (`gui.py:490`); во время работы возвращается 400 вместо существующего job.

### 6.2 Контракт состояний job

```
created → queued → running ──→ succeeded
                     │  ├──→ partial        (остановлен, часть не закончена)
                     │  ├──→ failed         (ошибка, есть диагностика)
                     │  ├──→ cancelled      (явная отмена)
                     │  └──→ paused ──→ running | cancelled
                     └──→ timed_out
```

| Состояние | Что означает | Что сохраняется | Разрешено |
| --- | --- | --- | --- |
| `created` | Запрос принят, job записан | Запись job, idempotency key | `cancel` |
| `queued` | Ждёт единственного writer'а или слота | Scope, профиль, очередь items | `cancel`, `pause` |
| `running` | Идёт работа | Все завершённые observations, периодические checkpoints | `pause`, `cancel` |
| `paused` | Корректная остановка, resumable | Всё, что было измерено; очередь незаконченных items | `resume`, `cancel` |
| `succeeded` | Все items терминальны, результат опубликован или явно не опубликован | Полный результат | Только чтение |
| `partial` | Часть items не закончена | Всё измеренное; явная причина | `resume`, `retry` |
| `failed` | Ошибка выполнения | Диагностика, всё измеренное до ошибки | `retry` |
| `cancelled` | Явная отмена пользователем | Всё измеренное | `retry` (новый job) |
| `timed_out` | Исчерпан внешний общий срок | Всё измеренное | `retry` |

Переходы, которые обязательны:
- `cancel` и `failed` **не удаляют** ни историю, ни последний завершённый результат, ни очередь незаконченных items (дефект 6). Сегодня это обеспечивается тем, что `store()` пишет payload по факту измерения, а recheck не стирает прежнее значение (`proxytool.py:1318-1330`); это поведение сохраняется и переносится в `job_item`.
- `resume` не расширяет scope и не использует obsolete membership. Сегодня resume — побочный эффект `done`/`row_fresh`, а не сущность.
- Изменение настроек между `resume` порождает **новый** job, а не продолжение старого: `profile` — content-addressed (`proxytool.py:1298-1299`), и `INSERT OR IGNORE INTO profiles` на новом id делает `done` пустым, то есть начинается полный перескан. Это надо сделать явным (`E_CONFLICT_REVISION`), а не молчаливым.
- `timeout` — общий срок всего задания, а не только per-request. Сегодня `check_proxy` выполняет `attempts × len(targets)` запросов последовательно без внешнего `asyncio.timeout` (`proxytool.py:1139-1161`), что подтверждено локальным прогоном из 5 целей × 3 попытки без срабатывания early-abort.

### 6.3 Контракт состояний item

```
pending → prefiltered → probing → done
                        │  ├──→ unreachable
                        │  ├──→ blocked       (denylist / DNSBL)
                        │  ├──→ partial
                        │  └──→ failed
```

| Состояние | Значение | Что хранится |
| --- | --- | --- |
| `pending` | В очереди, ещё не начинался | Только scope item |
| `prefiltered` | Прошёл дешёвый TCP-префильтр | Время префильтра |
| `probing` | Идёт измерение | Периодический прогресс по целям |
| `done` | Есть вердикт | Полное наблюдение по §2.1 |
| `unreachable` | Недоступен на сетевом уровне | Наблюдение с `error.stage='tcp'`, `error.code='UNREACHABLE'` |
| `blocked` | Отклонён политикой до измерения | Причина блокировки, без ложного «проверен» |
| `partial` | Часть целей измерена | Что успели |
| `failed` | Ошибка измерения | Код и стадия из §5.4 |

Инварианты:
- `unreachable` и `blocked` **не** обновляют `reputation` на `listed`. Сегодня при недоступности прежние сведения о чистоте теряются целиком (замена payload), что неверно ни в одну сторону: адрес не становится «listed», но и прежнее знание не должно бесследно исчезать.
- `blocked` до измерения требует, чтобы denylist применялся **до** prefilter (дефект 19) — иначе заблокированный адрес успевает получить сетевое соединение.
- `done` означает «есть наблюдение с явным временем», а не «свежо»: свежесть — отдельная ось (§2.3).
- Item идентифицируется парой `(job_id, endpoint_id, access_id, profile_revision)`; повторная проверка в новом job создаёт новый item, а не перезаписывает старый.

### 6.4 Идемпотентность и параллелизм

- Управляющие действия несут `Idempotency-Key`. Один ключ не создаёт второй job: повтор возвращает тот же `job_id` (F11, приёмка F29 №3).
- Одновременно изменяющих БД исполнителей — ровно один. Это обеспечивается эксклюзивным `workbench.lock` (`proxytool.py:2323-2335`) и он должен остаться; конфликт даёт `E_CONFLICT_BUSY`, а не «упавший воркер».
- **ОСТОРОЖНО (дефект 5 / R03):** read-путь GUI берёт тот же эксклюзивный лок (`data_lock`, `gui.py:363-367`, вызовы на `gui.py:669, 788, 1001`), поэтому таблица, детали и скачивание блокируются на всё время скана и ожидания watch. Это ограничение зашито в контракт блокировок и **должно быть пересмотрено вместе с коллекциями**: read-only путь не должен требовать writer-lock. Правка — за владельцем `gui.py`.

---

## 7. Как контракты соотносятся с требованиями

| Требование | Контракт | Разрыв, который закрывает |
| --- | --- | --- |
| F02, дефект 11 | §1.2 (2), §3.3 миграции 1–2 | Коллекции и membership; legacy → «Ранее собранные» с `origin='legacy'` |
| F03 | §4.5, §3.3 миграция 11 | Preview/commit/merge-replace по коллекции; артефакт импорта |
| F04 | §1.2 (1), §5.1 | `access_id` + `access_revision`; четыре разные identity; секрет только как ссылка |
| F05 | §1.1 (Profile), §3.3 миграция 8 | Именованные профили, ревизии, история версий |
| F07, F18 | §5.5 | Единицы, лимиты, одна валидация на три интерфейса |
| F08 | §4.4 (`country_source`, `country_at`, `cidr`) | Происхождение и дата знания, явный unknown |
| F09, дефекты 1–4 | §2 целиком | TTL в БД, построчный отбор, явные состояния времени, разделение `empty`/`stale` |
| F10, F25 | §5.4 | Канон кодов ошибок с действием |
| F11, дефект 6 | §6 целиком | Persisted job/item, идемпотентность, checkpoints |
| F14, дефект 12 | §1.2 (5), §4.3 (`desired`, `deficit_reasons`, `next_attempt_at`) | Постоянный пул и watch/refill, который восстанавливает пул |
| F16, дефект 8 | §1.2 (3), §1.2 (5) | Фиксация generation/pool/profile у потребителей |
| F18 | §2.3, §5.4, §5.5 | Один service layer, общие коды и единицы |
| F24 | §3 целиком | Версия схемы, backup, manifest, retention, гейт для старого бинарника |
| F28, дефекты 7, 20 | §4 целиком | Артефакты вместо одного публикующего пути, fail-closed, совместимость |
| F29 | §5 целиком | Ключи, права, scope, ошибки, события, курсоры |

## 8. Открытые вопросы и внешние зависимости

1. **Источники.** Ветка `sources-catalog` в `/Users/main/Desktop/111/proxy-workbench-sources` не входит в `integration/ultra-2026-09-25` (`ls proxy_workbench/source_*.py` — пусто, `proxytool.py` не содержит ни `source_observation`, ни `source_scan_stat`). Приёмка её работы — **внешняя зависимость** (F13, MASTER-PROMPT §6 «Этап I»). Всё, что от неё не зависит, делается здесь: коллекции, импорт, provenance-схема. `proxy-workbench-sources` не редактируется.
2. **`access_revision` не существует.** Приёмка F09 «новая версия доступа одинаково влияет на GUI/API/gateway/new connection» невыполнима до появления модели доступа. Это следствие §5.1, а не отдельный блокер.
3. **Репозиторий тестов сейчас фиксирует часть дефектного поведения.** `tests/test_freshness.py:87-99` (`test_resume_rechecks_unreachable_and_expired_rows`) вручную дописывает `valid_until = 1` в строку, полученную от реального `scan()`, — такой тест проходит и при текущем неверном поведении, и не поймал бы отсутствие TTL при записи. `tests/test_selection.py:275` закрепляет переключение активного пула при экспорте выделенного. `tests/test_anonymity.py:206-210` (`test_min_anonymity_is_ignored_without_judge`) закрепляет молчаливый сброс требования anonymity как правильное поведение. При смене контракта эти тесты должны быть переписаны **под правильное поведение**, а не удалены. Переписывание — работа владельца теста, не «помощь» со стороны.
4. **Замечание к AGENTS.md.** В `AGENTS.md` есть требование не запускать тесты без запроса пользователя. MASTER-PROMPT §0 явно разрешает локальные unit/integration/browser-тесты на временных данных и моках. При расхождении считать действующим MASTER-PROMPT; в любом случае полный `discover -s tests` выполняет интегратор.
5. **Версия контракта.** Это документ версии 1. Любое изменение идентичностей, схемы, состояний или кодов, которое ломает уже написанные модули, требует бампа версии здесь и уведомления потребителей в `HANDOFF/` до интеграции — иначе параллельная работа разойдётся по двум разным схемам.
