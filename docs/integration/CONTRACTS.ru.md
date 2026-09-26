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

**КОНТРАКТ.** Один admission-контракт с результатом:

```
admit(row, scope, access, policy, now) -> Admission

Admission {
  admitted: bool,
  reason_code: str | null,     // канон из §5.4
  age_seconds: float | null,   // now - checked_at
  checked_at, valid_until, published_at, # видимые потребителю
  max_age_seconds,             // применённая политика
}
```

`access` — обязательный отдельный аргумент, а не часть `scope`: §1.2 (1) требует, чтобы `access_revision` был частью admission, и без него подпись функции не выражает собственного правила контракта. `scope` несёт collection, `profile_id` и `profile_revision`; `access` несёт `access_id` и `access_revision`; `policy` несёт TTL, exclusions (denylist, hosting, protocol, country) и `min_success`/`min_anonymity`/`strict`, которые приходят из **ревизии профиля**, а не как параметр чтения (сейчас порог приходит аргументом в `export` и в `/api/results`, `proxytool.py:1651` и `gui.py:636`, из-за чего один и тот же снимок даёт pass или пустую выдачу в зависимости от значения параметра).

**Определение «одинаковый состав» (F18, приёмка F29 №7).** Не «примерно то же» и не «та же строка в том же порядке», а **точное равенство отсортированного множества пар `(canonical, admission_reason)`**. Канонизация до сравнения обязательна, иначе сравнение строк неустойчиво. Сравниваются только admitted-строки; отклонённые обязаны совпадать по `reason_code` и по своему отсортированному множеству `(canonical, reason_code)`.

Проверка на одной ревизии — одна команда, сравнивающая выдачу трёх поверхностей на **одном и том же поколении и одной ревизии профиля**:

```
.venv/bin/python -m unittest tests.test_parity
```

Тест обязан: (1) взять одну generation, одну `profile_revision`, один `scope`; (2) получить список из `api.select`, из CLI-пути `get` и из `App.results`; (3) сравнить отсортированные множества `(canonical, admission_reason)` попарно. До интеграции `App.results` выбирает строки своим кодом (`gui.py:620-761`) и к `api.select` не обращается, поэтому равенство недостижимо одной командой — это и есть проверяемый критерий того, что интеграция закончена.

**До интеграции модулям нужна одна общая фикстура.** Пока `admit` живёт в заголовочной части `proxytool.py`, каждый из семнадцати модулей напишет свои тестовые строки сам, и они разойдутся по составу, по именам полей и по кодам причин. `db.py` (первая волна, §HANDOFF) обязан поставить `tests/fixtures/admission.py` с общим конструктором строки и общим списком `(canonical, admission_reason)` для всех тестовых случаев, включая unknown-время, истёкшую строку и blocked. Модули обязаны импортировать её, а не собирать payload заново.

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

- `SCHEMA_VERSION = 14` — целое в Python, и `PRAGMA user_version` — зеркало того же числа в файле. Они обязаны совпадать; расхождение — ошибка чтения базы, а не повод «дописать». Миграции нумеруются `0..14`; после применения всех `user_version = 14`.
- `PRAGMA application_id` — фиксированное магическое число пакета. Несовпадение означает «это не наша база», отказ без записи. **Правило распознавания legacy:** `user_version = 0` **и** `application_id` равен магическому числу — это наша база, созданная до версионирования, и она мигрируется; `user_version = 0` **и** `application_id != 0` — это база, созданная версионированным кодом до миграции 0; `application_id`, не равный ни 0, ни магическому числу, — чужая, отказ. Поэтому миграция 0 обязана входить в диапазон для legacy (§3.4), иначе эти три случаи сливаются.
- Каждая миграция — функция в `proxy_workbench/db.py` с номером, выполняется в одной транзакции, идемпотентна, применяется строго по порядку.
- `PRAGMA foreign_keys=ON` для всех новых соединений, **включая соединение мигратора**. Порядок таблиц в §3.3 этому подчинён: `membership` идёт после `endpoints`.
- Перед первой миграцией, которая что-то меняет в уже существующей базе, — технический backup согласованным путём SQLite (`VACUUM INTO` как минимальная гарантия целостности при закрытом writer, либо `sqlite3.Connection.backup`), плюс `manifest` с checksum, размером, датой и версией схемы.
- Все `INSERT` в общих таблицах указывают список колонок явно. Это убирает зависимость от порядка колонок и делает добавление колонки безопасным.

### 3.3 Порядок миграций

Порядок фиксирован здесь. Каждая строка — отдельная миграция; откат делается восстановлением pre-migration backup, а не обратной DDL. Колонка «пишет в `user_version`» названа явно, потому что без неё нельзя доказать, что legacy-база дошла до конца.

| № | Что добавляется | Пишет в `user_version` | Владелец модуля |
| --- | --- | --- | --- |
| **0** | `PRAGMA application_id`; `schema_migrations(version INTEGER PRIMARY KEY, applied_at REAL, app_version TEXT, backup_path TEXT)` | 0 | `db.py` |
| **1** | `endpoints(id TEXT PRIMARY KEY, canonical TEXT UNIQUE NOT NULL, host TEXT, port INTEGER, scheme TEXT, ip_version INTEGER, country TEXT, country_source TEXT, country_at REAL, asn INTEGER, provider TEXT, hosting INTEGER, cidr TEXT, first_seen_at REAL, last_seen_at REAL)` | 1 | `db.py` + `geo.py` |
| **2** | `collections(id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, archived_at REAL, created_at REAL)` и `membership(collection_id TEXT REFERENCES collections(id), endpoint_id TEXT REFERENCES endpoints(id), added_at REAL, origin TEXT NOT NULL, PRIMARY KEY(collection_id, endpoint_id))` | 2 | `db.py` |
| **3** | `accesses(id TEXT PRIMARY KEY, endpoint_id TEXT NOT NULL REFERENCES endpoints(id), mode TEXT NOT NULL, secret_ref TEXT, access_revision INTEGER NOT NULL, created_at REAL, rotated_at REAL)` | 3 | `db.py` + `secrets.py` |
| **4** | `observations(id TEXT PRIMARY KEY, job_id TEXT, endpoint_id TEXT NOT NULL REFERENCES endpoints(id), access_id TEXT NOT NULL REFERENCES accesses(id), access_revision INTEGER NOT NULL, profile_id TEXT NOT NULL, profile_revision INTEGER NOT NULL, started_at REAL, finished_at REAL, verdict TEXT NOT NULL, error_code TEXT, error_stage TEXT)` — таблица наблюдения из §2.1, а не колонка | 4 | `db.py` |
| **5** | Колонки в `results`: `observation_id TEXT REFERENCES observations(id)`, `endpoint_id TEXT REFERENCES endpoints(id)`, `access_id TEXT`, `access_revision INTEGER`, `profile_id TEXT`, `profile_revision INTEGER`, `checked_at REAL`, `valid_until REAL`, `error_code TEXT`, `error_stage TEXT`, `job_id TEXT`. **`profile_revision` и `profile_id` объявляются здесь и нигде больше** | 5 | `db.py` |
| **6** | `job(id TEXT PRIMARY KEY, kind TEXT, state TEXT, scope_json TEXT, input_digest TEXT, profile_id TEXT, profile_revision INTEGER, collection_id TEXT, idempotency_key TEXT, created_at REAL, started_at REAL, finished_at REAL)`, `job_item(job_id TEXT NOT NULL REFERENCES job(id), item_id TEXT NOT NULL, endpoint_id TEXT, access_id TEXT, access_revision INTEGER, state TEXT, observation_id TEXT, error_code TEXT, PRIMARY KEY(job_id, item_id))`, `job_event(job_id TEXT, seq INTEGER, at REAL, type TEXT, code TEXT, data_json TEXT, PRIMARY KEY(job_id, seq))`, `checkpoint(job_id TEXT, name TEXT, at REAL, state_json TEXT, PRIMARY KEY(job_id, name))` | 6 | `db.py` + `jobs.py` |
| **7** | `pools(id TEXT PRIMARY KEY, collection_id TEXT, profile_id TEXT, profile_revision INTEGER, policy_json TEXT, desired INTEGER, minimum INTEGER, reserve INTEGER, state TEXT, deficit_reason TEXT, next_attempt_at REAL)`; `pool_member(pool_id TEXT, endpoint_id TEXT, state TEXT, admitted_at REAL, released_at REAL, PRIMARY KEY(pool_id, endpoint_id))`; `schedules(id TEXT PRIMARY KEY, pool_id TEXT, kind TEXT, interval_minutes REAL, window_json TEXT, timezone TEXT, quiet_hours_json TEXT, budgets_json TEXT, next_run_at REAL, enabled INTEGER)`; `schedule_run(id TEXT PRIMARY KEY, schedule_id TEXT, started_at REAL, finished_at REAL, state TEXT, counters_json TEXT)` | 7 | `db.py` + `pools.py`, `scheduler.py` |
| **8** | `api_keys(id TEXT PRIMARY KEY, prefix TEXT NOT NULL, name TEXT, purpose TEXT, created_at REAL, expires_at REAL, last_used_at REAL, revoked_at REAL, permissions_json TEXT NOT NULL, resource_scope_json TEXT NOT NULL, rate_limit_json TEXT, concurrency_json TEXT, rotation_grace_until REAL, verifier TEXT NOT NULL, verifier_salt TEXT NOT NULL, verifier_algo TEXT NOT NULL)` | 8 | `db.py` + `apikeys.py` |
| **9** | `profiles`: `name TEXT`, `revision INTEGER NOT NULL DEFAULT 1`, `parent_id TEXT`, `digest TEXT NOT NULL`, `created_at REAL`, `archived_at REAL`, `is_default INTEGER NOT NULL DEFAULT 0` | 9 | `db.py` + `profiles.py` |
| **10** | `audit_log(at REAL NOT NULL, key_id TEXT, operation TEXT NOT NULL, object_kind TEXT, object_id TEXT, scope_json TEXT, result TEXT, error_code TEXT)` | 10 | `db.py` + `apikeys.py` |
| **11** | `export_artifact(id TEXT PRIMARY KEY, kind TEXT NOT NULL, collection_id TEXT, profile_id TEXT, profile_revision INTEGER, generation TEXT, published_at REAL, expires_at REAL, state TEXT, reason_code TEXT, manifest_json TEXT)` | 11 | `db.py` + `exportsvc.py` |
| **12** | `import_batch(id TEXT PRIMARY KEY, collection_id TEXT, created_at REAL, state TEXT, report_json TEXT, revision INTEGER)` | 12 | `db.py` + `importer.py` |
| **13** | **Пересборка `results` с новым первичным ключом.** Копирование в `results_new` с `PRIMARY KEY(profile_id, profile_revision, access_id, access_revision, endpoint_id, job_id)`, перенос данных, `DROP TABLE results`, `RENAME`. Явное исключение из правила «только аддитивные» — SQLite не меняет `PRIMARY KEY` иначе; это единственное неаддитивное место во всём наборе | 13 | `db.py` |
| **14** | Индексы, каждый — только по уже существующим колонкам: `results(profile_id, valid_until)`, `results(access_id, access_revision)`, `results(endpoint_id, checked_at DESC)`, `membership(endpoint_id)`, `observations(endpoint_id, checked_at DESC)`, `job_item(job_id, state)`, `endpoints(canonical)`, `api_keys(prefix)` | 14 | `db.py` |

Три ошибки, которые были в первой редакции этого документа и теперь закрыты, — проверено воспроизведением на временной БД:

- **(a) Порядок.** `membership` ссылается на `endpoints(id)`, поэтому `endpoints` идёт в миграции 1, а `membership` — в миграции 2. При `PRAGMA foreign_keys=ON` (требование §3.2) обратный порядок не создаётся.
- **(b) Дубль `profile_revision`.** Миграция 5 добавляет `profile_id` и `profile_revision` в `results`; миграция 9 добавляет `revision` в `profiles` и больше ничего в `results` не трогает.
- **(c) Несуществующие цели индексов.** `results(profile_id, valid_until)` теперь указывает на колонку, которую создаёт миграция 5; `observations(endpoint_id, checked_at DESC)` — на таблицу, которую создаёт миграция 4. До правки в пакете не было ни одной строки `observations` (`grep -rn "observations" proxy_workbench/*.py tests/*.py` — ноль совпадений), то есть индекс ссылался на несуществующую таблицу.

**Почему пересборка ключа (миграция 13) обязательна, а не опциональна.** Правило §1.2 (1) — «смена `access_revision` отзывает прошлое доказательство» — невыполнимо одними `ADD COLUMN`. Воспроизведено на временной БД: таблица с `PRIMARY KEY(profile, proxy)` (`proxytool.py:562-564`) после добавления всех колонок миграции 5 сохраняет `PRIMARY KEY(profile, proxy))`; две вставки одного и того же `(profile, proxy)` с `access_revision` 1 и 2 дают **одну** строку (`rows after 2 access revisions: 1`, `max access_revision: 2`). То есть вторая вставка затирает первую, и разделить доказательства разных паролей физически нечем — это ровно тот отказ, который F04 запрещает («Новый пароль не наследует успешную проверку старого», MASTER-PROMPT:166). Ключ `(profile_id, profile_revision, access_id, access_revision, endpoint_id, job_id)` решает и это, и §6.3: «повторная проверка в новом job создаёт новый item, а не перезаписывает старый» — `job_id` входит в ключ, иначе это утверждение невыполнимо.

Правила совместимости:
- Миграции аддитивные, **кроме одной явно названной**: миграции 13. Она обязана быть идемпотентной (повторный запуск на уже пересобранной таблице — no-op) и обязана идти после backup'а из §3.2.
- Миграция обязана быть безопасна при повторном запуске: открытие уже мигрированной базы не меняет ничего.
- Файл с `user_version` ниже текущего — «старая база», она обязана открываться и мигрироваться. Файл с `user_version` выше `SCHEMA_VERSION` — отказ: «база новее этой программы».
- `candidates`/`candidate_meta`/`candidate_seen`/`results`/`profiles` остаются единственными таблицами, к которым имеют право обращаться сбор, скан и экспорт. Всё остальное строится поверх них через `endpoints`/`membership`.

### 3.4 Что происходит со старыми базами

| Ситуация | Поведение |
| --- | --- |
| БД без `user_version` (= 0), созданная текущим кодом | Открывается. Применяются миграции **0–14** по порядку, начиная с 0. До первой миграции, меняющей данные, создаётся pre-migration backup + manifest. Legacy-кандидаты переносятся в `endpoints` и в коллекцию с честным происхождением: `origin='legacy'`, `kind='public'`, имя «Ранее собранные». Метка «свои» не выдумывается (F02, дефект 11) |
| БД с `user_version` равным `SCHEMA_VERSION` | Открывается без изменений, миграции пропускаются |
| БД с `user_version` выше `SCHEMA_VERSION` | Отказ с сообщением «база создана более новой версией программы»; **никаких записей**, включая `user_version` |
| `application_id` не равен ни 0, ни магическому числу | Отказ: «это не база Proxy Workbench» |
| Файл повреждён или открыт в WAL без согласованного пути | Отказ, а не пересоздание. Пересоздание пустого файла на месте существующего запрещено |
| Перенос `data/` при смене пути (frozen ↔ checkout ↔ per-user) | `paths.default_data` (`paths.py:12-28`) переключается между вариантами без переноса содержимого. **КОНТРАКТ:** `db.py` предоставляет `migrate_data_path(old, new) -> preview`, и перенос выполняется только после согласия пользователя и с backup старой папки |

### 3.5 Как старый бинарник не должен писать в новую схему

Требование F24: «Старый binary не пишет в новую schema». Механизмы, каждый из которых нужен, потому что сегодня не работает ни один:

1. **Версия в файле.** Старый код не знает про `user_version` и не проверит его — поэтому защита строится на том, что **новая** схема ломает старый код **немедленно и без записи в БД**. Практически: после миграции 13 таблица `results` имеет 14 колонок, и позиционный `INSERT ... VALUES (?,?,?)` (старый код) перестаёт работать. Проверено на временной БД после пересборки: `OperationalError: table results has 11 columns but 3 values were supplied` (11 — число колонок на том шаге, где пересборка уже добавила `access_id`/`access_revision`; после полного набора миграций сообщение будет о 14 колонках, механизм тот же).
2. **Тот же приём на пути сбора — обязателен.** Первая редакция этого раздела закрывала только `results` и `profiles`, и это была дыра: `collect.add` пишет позиционно и в `candidates` (`proxytool.py:655`, `INSERT OR IGNORE INTO candidates VALUES (?)`), и в `candidate_seen` (`proxytool.py:660`, `INSERT OR IGNORE INTO candidate_seen VALUES (?, ?)`). Проверено: при пересобранной `results` старый воркер на `results` падает, а на этих двух вставках **проходит** и оставляет строки — `candidates now: 1`, `candidate_seen now: 1`. Значит «старый бинарник пишет в новую схему» было верно на всём пути сбора.
   **КОНТРАКТ:** миграция 5 добавляет в `candidates` и `candidate_seen` по одной `NOT NULL DEFAULT` колонке (`endpoint_id` — соответственно `id` и `endpoint_id`), что ломает обе позиционные вставки. `candidate_meta` сегодня пишется **явным списком колонок** (`proxytool.py:663-666`), поэтому старый бинарник там не ломается; это осознанно переносимое отличие: `candidate_meta` не входит в защищаемый путь, потому что его схема и так совместима и в него пишется только `country`/`source` — метаданные источника, а не доказательство пригодности. Проверяемый сценарий на все три таблицы: старый путь вставки в `results`, `candidates`, `candidate_seen` → `OperationalError` в каждом, база не изменена.
3. **Явные списки колонок в новом коде** — чтобы новая схема не ломала **новый** код при будущих `ADD COLUMN` (сегодня ровно наоборот: `proxytool.py:655, 660, 1300, 1386`).
4. **Проверка версии на чтении и на записи.** `open_db` обязан: при `user_version > SCHEMA_VERSION` — отказ; при `user_version == 0` и `application_id`, равном магическому числу, — «наша база до версионирования», мигрировать; при `application_id`, не равном ни 0, ни магическому числу, — отказ.
5. **Отказ = без записи.** Проверка версии выполняется **до** любого `CREATE`/`ALTER`/`INSERT`, включая `INSERT OR IGNORE INTO profiles` в начале скана (`proxytool.py:1300`).
6. **Rollback** — восстановление pre-migration backup в отдельный путь; не обещать lossless downgrade неизвестных полей (F24). Мигрированная БД сохраняется отдельно и не затирается.

### 3.6 Retention и очистка

**СЕЙЧАС.** Единственная ретенция — экспортные поколения: `EXPORT_GENERATION_RETENTION = 3` (`proxytool.py:418`), `prune_export_generations` (`proxytool.py:443-461`), вызовы на `proxytool.py:1694` и `proxytool.py:1925`, с явной ошибкой при невозможности удалить (`proxytool.py:1695-1696`). Очистка данных — `clear_runtime` (`maintenance.py:72-87`), деструктивная, без preview и без отчёта о размере; CLI печатает список удалённого уже после удаления (`proxytool.py:2346`).

**КОНТРАКТ.**
- Retention для `observations` и `results` задаётся политикой ревизии профиля и выражается в SQL по колонкам `checked_at`/`valid_until` (миграции 4 и 5). Значения по умолчанию — стартовые, не «правильные», и видимы пользователю.
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
- `export_artifact` (миграция 11) хранит, какой collection/profile/generation лежит в артефакте, чтобы скачивание всегда отдавало файл той выборки, которую видит пользователь.
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
- **Перевод обязателен и делается существующим механизмом.** `E_*`-коды, строки `permissions`, значения `state_detail` (§4.3) и `reason_code` переводятся через уже существующий `i18n.tr(ru, en)` (`proxy_workbench/i18n.py`, функция `tr` в конце файла) — не через новый словарь и не через жёстко зашитый русский текст. Код остаётся машинным, текст — локализуемым; в JSON/CLI/логе код печатается вместе с переводом, в GUI отображается перевод при наличии ключа и сам код как запасной вариант.
- Связка `gui.CHILD_ENV` (`gui.py:76`, `PROXY_WORKBENCH_LANG='ru'`, используется при запуске воркера на `gui.py:501` и `gui.py:572`) — **часть контракта**, а не деталь реализации: GUI переводит лог воркера, поэтому воркер обязан писать по-русски. Это закреплено тестом `tests/test_i18n.py:27` (`assertEqual(gui.CHILD_ENV['PROXY_WORKBENCH_LANG'], 'ru')`). Изменение `CHILD_ENV` или `i18n.detect` без синхронного обновления теста ломает перевод интерфейса и запрещено.
- Владелец `proxy_workbench/i18n.py` — исполнитель поверхности интерфейса (см. `HANDOFF/README.ru.md` §1.2). Он же владеет ключами `messages{}` внутри `ui/app.js`, которые **передаются через handoff**, потому что сам `app.js` принадлежит интегратору.
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
- Поток: `GET /v1/events?stream=…&cursor=…` → `text/event-stream`. События: `job.state`, `job.progress`, `item.observation`, `item.verdict`, `generation.published`, `pool.state`, `quota.state`.
- Формат события: `{"stream": str, "seq": int, "at": unix_seconds, "type": str, "job_id": str|None, "item_id": str|None, "code": str, "data": {...}}`. `code` берётся из §5.4, а не из текста исключения.
- Событие по измерению — одно на item, а не сводка: строка «Проверено 512/1000» (печатается в `proxytool.py:1485`) событием измерения **не является**.
- Персональные данные и секретов в событиях нет; `access_id` передаётся без значения credentials.
- Перевод: `code` переводится по правилам §5.4 через `i18n.tr(ru, en)`.

### 5.7 Курсоры и пагинация — одна схема

В первой редакции этого документа были две несовместимые схемы (§5.6 «`seq` монотонен в пределах потока» и §5.7 «курсор = `(generation, sort, filter_digest, offset)`»), и обе попали в контракт как есть. При параллельных job и именованных пулах это неработоспособно: `seq` разных потоков пересекаются, а курсор пагинации не имеет смысла в потоке событий. Решение — **одна схема курсора для обоих случаев: `(stream_id, seq)`**.

- `stream_id` — строка, однозначно называющая поток: `job:<job_id>`, `pool:<pool_id>`, `generation`, `system`. Поток всегда адресуем и всегда существует до первого события.
- `seq` — целое, строго монотонное **в пределах одного `stream_id`**, без пропусков и повторов, начиная с 1. Именно это гарантирует `job_event` из миграции 6 с `PRIMARY KEY(job_id, seq)`.
- Курсор в обоих случаях — одна и та же непрозрачная строка, кодирующая `(stream_id, seq)`. Никакого `offset` и никакого `filter_digest` в курсоре событий.
- **Правило resume:** клиент передаёт последний полученный курсор; сервер отдаёт все события с `seq > cursor` в этом `stream_id` в порядке возрастания. Если курсор старше минимального хранимого `seq` потока (события вытеснены ограниченной историей) — ответ `E_CONFLICT_REVISION` с указанием, что клиент обязан перечитать состояние ресурса, а не молча получить дыру в потоке. Продолжение подписки по этому правилу — то, что F29 требует от «активных SSE/stream sessions корректно завершаются или перепроверяют срок по documented policy» (§5.2).
- Глобальный порядок между потоками **не гарантируется и не должен выглядеть гарантированным**: `job:A/17` и `pool:B/3` несравнимы. Если потребуется сквозной порядок для отчётности, он реализуется отдельным `system`-потоком с тем же курсором, а не сравнением `seq` разных потоков.
- Для пагинации выдачи (`/v1/proxies`, `/v1/results`) курсор — тот же `(stream_id, seq)`, где `stream_id = generation:<generation>`, а `seq` — **сквозной номер строки в этой generation в порядке сортировки**. Смена `generation`, сортировки или фильтра меняет `stream_id` или `seq`, поэтому старый курсор либо отвергается с `E_VALIDATION_FIELD`, либо однозначно означает другую выдачу. Клиент не может изменить фильтр и остаться в том же курсоре.
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
- Item идентифицируется ключом `(job_id, item_id)` — это `PRIMARY KEY(job_item)` из миграции 6, — и несёт `endpoint_id`, `access_id`, `access_revision`, `profile_revision`. Повторная проверка в новом job создаёт **новый** item, а не перезаписывает старый. Это утверждение невыполнимо без `job_id` в ключе строки результата, поэтому `job_id` входит и в `PRIMARY KEY` пересобранной `results` (миграция 13) — иначе старое измерение и новое для того же `(profile, access, endpoint)` схлопнулись бы в одно, как это воспроизведено в §3.3.

### 6.4 Идемпотентность и параллелизм

- Управляющие действия несут `Idempotency-Key`. Один ключ не создаёт второй job: повтор возвращает тот же `job_id` (F11, приёмка F29 №3).
- Одновременно изменяющих БД исполнителей — ровно один. Это обеспечивается эксклюзивным `workbench.lock` (`proxytool.py:2323-2335`) и он должен остаться; конфликт даёт `E_CONFLICT_BUSY`, а не «упавший воркер».
- **ОСТОРОЖНО (дефект 5 / R03):** read-путь GUI берёт тот же эксклюзивный лок (`data_lock`, `gui.py:363-367`, вызовы на `gui.py:669, 788, 1001`), поэтому таблица, детали и скачивание блокируются на всё время скана и ожидания watch. Это ограничение зашито в контракт блокировок и **должно быть пересмотрено вместе с коллекциями**: read-only путь не должен требовать writer-lock. Правка — за владельцем `gui.py`.

---

## 7. Таблица трассировки (MASTER-PROMPT §2)

Семь колонок, как требует MASTER-PROMPT:98: **требование → current state → владелец → зависимости → implementation → проверка → статус**. Статусы: `todo`, `in_progress`, `implemented_unverified`, `verified`, `external_blocker`, `not_applicable_with_evidence`. «Отложили» не означает `verified`.

Владельцы в колонке «владелец» — имена модулей и поверхностей из `HANDOFF/README.ru.md` §1–§3. `интегратор` = `proxytool.py` + `api.py` + `gui.py` + `ui/*`. Проверка в колонке «проверка» — либо команда, либо `не выполнено`; выполненной считается только реально запущенная в этой сессии.

### 7.1 Функциональные направления F01–F29

Таблица обновлена после цикла интеграции и цикла ревью. Статусы: `todo`, `in_progress`,
`implemented_unverified`, `verified`, `external_blocker`, `not_applicable_with_evidence`.
По §7.4 **ни одно требование не может быть `verified`**: четыре слота независимого ревью
не отработали. Максимум честного статуса — `implemented_unverified`, и он означает «модуль
написан и его тесты проходят», а не «проверено четырьмя ревьюерами».

Колонка «проверка» содержит только команды, реально выполненные в этой сессии, с их
результатом. Всё, чего нет в этой колонке как `выполнено`, не выполнено.

| Требование | Current state | Владелец | Implementation | Проверка (выполнено в этой сессии) | Статус |
| --- | --- | --- | --- | --- | --- |
| F01 Режимы проверки | Реализованы: collect-only, TCP-prefilter, таргеты, recheck, отдельный `basic` в `probes.py` | `probes.py` | `probes.py`, `proxytool.scan` | `tests.test_pipeline_*` (110) — OK | implemented_unverified |
| F02 Коллекции и scope | Реализованы: `collections`, `membership`, scope в адмиссии, выбор коллекции в GUI | `db.py` + `importer.py` | миграции 1–2, `core.Scope` | `tests.test_db_collections` — OK; воспроизведение дублей адреса в артефактах закрыто миграцией 15 | verified-кандидат → implemented_unverified |
| F03 Импорт | `importer.py` (1162 строки) доступен из CLI и `/v1/collections/{id}/imports`. **В GUI недостижим**: в `gui.py` нет ни одного упоминания importer; textarea `#proxies` + file-drop пишут напрямую через `App.add_collection_members` | `importer.py` (+ GUI — отдельный исполнитель) | `importer.py` | `tests.test_importer_preview/commit/scope/fixtures` — OK. GUI-путь не закрыт | implemented_unverified (GUI-часть todo) |
| F04 hostname/auth-прокси | **Не реализовано.** Ни один движок проб не умеет Proxy-Authorization/SOCKS5 auth; userinfo в URL отвергается. Идентичность доступа теперь принимается в любой форме (`proxytool.as_access`) и записывается в строку, но секрет не отправляется | `secrets.py` + транспорт проб | `secrets.py`, `core.Access` | `tests.test_scan_budgets` — OK (as_access); полного пути с паролем нет | todo |
| F05 Профили и правила целей | Реализованы: именованные профили, ревизии, пресеты каталога | `profiles.py` | `profiles.py`, `servicecatalog.py` | `tests.test_profiles_*` — OK | implemented_unverified |
| F06 Каталог сервисов | Реализован, есть в GUI (выбор каталога), переведён | `servicecatalog.py` | `servicecatalog.py` | `tests.test_gui_collections_catalog` — OK | implemented_unverified |
| F07 Расширенные параметры | connect/timeout/attempts/max_bytes/statuses/contains/sha256/protocol | `probes.py` | `probes.py` | `tests.test_pipeline_*` — OK | implemented_unverified |
| F08 География | picker, unknown-policy, exit-фильтры, даты знания | `geo.py` | `geo.py` | `tests.test_geo_*` — OK | implemented_unverified |
| F09 Freshness | TTL пишется один раз при измерении. **Исправлено в этой сессии**: повторная проверка больше не оставляет старую успешную строку рядом со свежим провалом — ключ `results` сужен до (профиль, доступ, адрес), миграция 15 | `core.py` + `db.py` | `core.apply_measurement`, миграция 15 | `/tmp/rfx/repro_dup.py`, `/tmp/rfx/repro_mixed.py` — воспроизведено до и не воспроизводится после; `tests.test_scan_budgets`, `tests.test_db_migrations` — OK | implemented_unverified |
| F10 Диагностика | **Достижима с этой сессии**: `diagnose funnel\|zero\|control\|health\|bundle` в CLI. В GUI панели воронки по-прежнему нет | `diagnostics.py` + интегратор | `proxytool._cmd_diagnose` | `tests.test_diagnose_cli` (6) — OK | implemented_unverified |
| F11 Задания | Реализованы: `job`/`job_item`/`job_event`/`checkpoint`, очередь, cancel, восстановление, idempotency | `jobs.py` | `jobs.py` | `tests.test_jobs_*` — OK | implemented_unverified |
| F12 Конвейер | Бюджеты и gate работают в `proxytool.scan`; `Pipeline`/`run_pipeline` по-прежнему не вызываются продуктом. **Исправлено в этой сессии**: gate начисляет requests/bytes, три N разделены | `pipeline.py` | `pipeline.ResourceGate`, `proxytool.scan` | `/tmp/rfx/repro_budget.py`, `/tmp/rfx/repro_count.py` — воспроизведено до, не воспроизводится после; `tests.test_scan_budgets` (12) — OK | implemented_unverified (полный конвейер todo) |
| F13 Источники | **Причина блокера в таблице была неверной**: `proxy_workbench/source_adapters.py`, `source_catalog.py`, `source_management.py` в дереве есть. Настоящая причина — не перенесён финальный handoff ветки источников (два более поздних коммита `e1d57ce`, `8eca9a3`) | внешний handoff | `sourcedesk.py`, `source_*.py` | `ls proxy_workbench/source_*.py` — три файла; `diff -q source_adapters.py` с веткой источников — совпадает | external_blocker (формулировка исправлена) |
| F14 Постоянный пул | Реализован. **Исправлено в этой сессии**: `/v1/pools/{id}/refill` наполняет названный пул, `/start` запускает, `/recheck` измеряет его коллекцию; `pools_state_for` и `members` больше не падают с TypeError | `pools.py` + интегратор | `pools.refill`, `api.pool_candidate_source` | `tests.test_pools_api` (11) — OK | implemented_unverified |
| F15 Расписания | Реализованы: `schedules`, окна, тихие часы, DST, бюджеты | `scheduler.py` | `scheduler.py` | `tests.test_scheduler_*` — OK | implemented_unverified |
| F16 Gateway | Ротация, sticky, cooldown, health, резервы | `gateway.py` | `gateway.py` | `tests.test_gateway_*` — OK | implemented_unverified |
| F17 Путь подключения | LAN включается явно, QR, страницы. **Исправлено в этой сессии**: пароль шлюза больше не равен токену API (дефект 18) | `gui.py`, `gateway.py` | `--gateway-token`, `PROXY_WORKBENCH_GATEWAY_TOKEN` | `compose.yml` и `proxytool.run_gateway` — разделены; отдельный тест на равенство не написан (см. HANDOFF) | implemented_unverified |
| F18 Единое управление | Один сервисный слой; backup/restore/retention/cleanup/rebind **стали достижимы из CLI в этой сессии** | интегратор | `proxytool._cmd_backup` | `tests.test_scan_budgets.BackupCommandTests` (4) — OK | implemented_unverified |
| F19 Список результатов | Таблица, детали, массовые действия, выбор коллекции | `ui/*` | `gui.py`, `ui/*` | `tests.test_gui`, `tests.test_gui_collections_catalog` — OK | implemented_unverified |
| F20 Дополнительные измерения | Bandwidth, anonymity judge, окно измерения | `probes.py` | `probes.py` | `tests.test_anonymity`, `tests.test_bandwidth` — OK | implemented_unverified |
| F21 Сравнение источников | **Не реализовано.** Ни сравнения, ни cohort, ни survival, ни overlap, ни cost-per-admitted в `sourcedesk.py` (1642 строки) нет | `sourcedesk.py` | — | `grep -rn "cohort\|survival\|overlap\|cost_per" proxy_workbench/*.py` — пусто | todo |
| F22 Фоновая работа | **Не реализовано.** Нет трея, автозапуска, sleep/wake, повторного запуска в работающий экземпляр. `Scheduler.mark_wake` и `jobs.mark_awake` существуют, но из продукта их никто не зовёт | `desktop.py` | — | `grep -rn "mark_wake\|mark_awake" --include=*.py .` — только определения и тесты | todo |
| F23 Поставка | Windows `.exe`, macOS `.app`/`.dmg`, Linux, per-user paths, portable | `desktop.py` | `desktop.py`, `packaging/` | `tests.test_desktop_*`, `tests.test_packaging_release` — OK | implemented_unverified |
| F24 Данные, backup, restore | **Исправлено в этой сессии**: копия перед любой неаддитивной миграцией; retention по порядку внешних ключей; весь путь backup в CLI | `db.py` + интегратор | `DESTRUCTIVE_MIGRATIONS`, `_retention_order`, `_cmd_backup` | `/tmp/rfx/repro_backup.py`, `/tmp/rfx/repro_retention.py` — воспроизведено до; `tests.test_db_backup`, `tests.test_db_retention`, `tests.test_scan_budgets` — OK | implemented_unverified |
| F25 Помощь и диагностика | Пакет достижим из CLI в этой сессии; справочник кодов и кнопка в GUI — нет | `diagnostics.py` | `proxytool._cmd_diagnose` | `tests.test_diagnose_cli` — OK | implemented_unverified |
| F26 OSS/доступность | **Исправлено в этой сессии**: `CHANGELOG.md` `[Unreleased]` заполнен, `PRODUCT_VERSION` 2.3.0, README/README.ru получили разделы про `/v1`, ключи и отдельный пароль шлюза | `branding.py`, `docs/` | `CHANGELOG.md`, `README.md`, `README.ru.md` | `tests.test_packaging_release`, `tests.test_i18n` — OK | implemented_unverified |
| F27 URL-подписки | Импорт по URL, delta, expiry, привязка к коллекции | `sourcedesk.py` | `sourcedesk.py` | `tests.test_sourcedesk*` — OK | implemented_unverified |
| F28 Экспорты и snapshots | Поколения иммутабельны, fail-closed. **Исправлено в этой сессии**: поколение — множество адресов, а не измерений; `include_secrets` работает и публикует ссылку | `exportsvc.py` | миграция 15, `exportsvc.SecretGrant` | `/tmp/rfx/repro_dup.py` — воспроизведено до; `tests.test_exportsvc_secrets_export` (7) — OK | implemented_unverified |
| F29 API и ключи | Менеджер ключей есть в CLI и `/v1`; **исправлено в этой сессии**: object-level scope на артефактах и списках, `raw_body` проходит redaction, доменный отказ не выдаётся за сбой службы; добавлена страница README про `/v1` и ключи | `apikeys.py` + `apiv1.py` | `api._scope_values`, `apiv1._guard_scope`, `apiv1._domain_refusal` | `tests.test_apiv1_scope` (10), `tests.test_exportsvc_secrets_export` (7) — OK | implemented_unverified |

**Что в этой таблице не закрыто и не может быть закрыто «почти готово»:** F04 (аутентификация
на прокси), F21 (сравнение источников), F22 (трей/фоновая работа), полный F12
(`Pipeline`/`run_pipeline` не вызываются), GUI-часть F03 (импортёра в интерфейсе нет) и
GUI-часть F10/F25 (панели диагностики в интерфейсе нет). Подробности и точный объём
оставшейся работы — `HANDOFF/review-fixes.ru.md`.

### 7.2 Дефекты 1–26

| # | Current state | Владелец | Прямое следствие |
| --- | --- | --- | --- |
| 1 | TTL не пишется в БД (`proxytool.py:1380-1391`) | интегратор + `core.py` | Приёмка §7.7 не выполняется |
| 2 | `done` опирается на `row_fresh` без TTL (`proxytool.py:1335-1340`) | интегратор + `core.py` | То же |
| 3 | Статус обнуляет построчный список (`api.py:181-182`) | интегратор | Один истёкший убивает пул |
| 4 | Будущий `checked_at` принимается (`proxytool.py:1092-1094`) | `core.py` | Фиктивная свежесть на 2–26 ч |
| 5 | read-путь берёт writer-lock (`gui.py:363-367`) | поверхность `web` | Таблица и скачивание заблокированы |
| 6 | Отмена сохраняет payload, но состояний нет | `jobs.py` | Crash recovery невозможен |
| 7 | Экспорт выделенного публикует `current.json` | интегратор + `exportsvc.py` | Активный пул переключается |
| 8 | Потребители следуют за глобальным указателем | интегратор + `gateway` | Проверка B перенаправляет A |
| 9 | GUI откатывается к SQLite при битом указателе (`gui.py:626-653`) | поверхность `web` | GUI показывает то, чего не признаёт API |
| 10 | Форма принимает hostname/private, сборщик отвергает | `importer.py` + интегратор | Записи теряются молча |
| 11 | Коллекций нет | `db.py` | Свой список не изолирован |
| 12 | Watch не передаёт `want` (`proxytool.py:2500-2511`) | интегратор + `pools.py` | Пул монотонно убывает |
| 13 | `classify` не валидирует judge до elite; anonymity молча сбрасывается в any | `anonymity.py` → назначен в HANDOFF §1.2 | Ложный elite |
| 14 | IPv6 reverse неверен; любой `127.*` = listed | `reputation.py` → назначен в HANDOFF §1.2 | Ложное «чисто» и ложный «listed» |
| 15 | Окно скорости начинается с первого chunk | `probes.py` | Завышение Mbps в ~9 раз на loopback |
| 16 | Резерв слота после `await` (`gateway.py:336-354`) | поверхность `gateway` | Превышение `max_per_proxy` |
| 17 | Health = TCP-open (`gateway.py:350`) | поверхность `gateway` | Мёртвый прокси не отдыхает |
| 18 | Один секрет на API/шлюз/GUI; LAN по умолчанию | интегратор + поверхность `gateway` | Управляющий секрет в QR |
| 19 | Denylist не применяется в шлюзе | поверхность `gateway` | Отзыв не работает |
| 20 | `singbox` не валидируется целевой версией | `exportsvc.py` | R14 открыт |
| 21 | `pipeTo` + `close()` дважды; picker после `await` | поверхность `web` | Успех выглядит как сбой |
| 22 | URL каталога; prune необратим | `sourcedesk.py` | R16 п.1, п.4 |
| 23 | Quick test без общего deadline | интегратор + поверхность `web` | Один клик на десятки минут |
| 24 | Сценарии не сбрасывают поля предыдущего | поверхность `web` | Elite-сценарий мерит YouTube |
| 25 | Лента парсит агрегированный лог | поверхность `web` + `jobs.py` | Нет событий измерений |
| 26 | Нет macOS-поставки; release notes пусты | `desktop.py` | R19, R20 |

### 7.3 Замечания REVIEW R01–R20

| # | Current state | Владелец | Связан с |
| --- | --- | --- | --- |
| R01 | TTL не проходит scanner → БД → выдача | интегратор + `core.py` | дефект 1 |
| R02 | Первый истёкший опустошает snapshot | интегратор | дефект 3 |
| R03 | Таблица и скачивание заблокированы | поверхность `web` | дефект 5 |
| R04 | Экспорт выделенного меняет пул | интегратор + `exportsvc.py` | дефект 7 |
| R05 | GUI обходит отказ reader'а | поверхность `web` | дефект 9 |
| R06 | Hostname принимается формой, отбрасывается сборщиком | `importer.py` | дефект 10 |
| R07 | Watch только уменьшает пул | `pools.py` | дефект 12 |
| R08 | Анонимность может быть ложной | `anonymity.py` | дефект 13 |
| R09 | IPv6 DNSBL и коды | `reputation.py` | дефект 14 |
| R10 | Измерение Mbps прежнее | `probes.py` | дефект 15 |
| R11 | Reservation и handshake deadline | поверхность `gateway` | дефект 16 |
| R12 | LAN по умолчанию, общий секрет | интегратор + `gateway` | дефект 18 |
| R13 | Denylist не отзывает выданный пул | поверхность `gateway` | дефект 19 |
| R14 | Fail-closed sing-box без versioned validation | `exportsvc.py` | дефект 20 |
| R15 | Скачивание в Chromium | поверхность `web` | дефект 21 |
| R16 | Источники в main улучшены частично | `sourcedesk.py` | дефект 22 |
| R17 | Presets и «живая лента» сильнее backend | поверхность `web` + `jobs.py` | дефекты 24, 25 |
| R18 | Quick test диагностический, не обновление | интегратор + `web` | дефект 23 |
| R19 | Desktop-цель не закрыта | `desktop.py` | дефект 26 |
| R20 | Инженерная приёмка и документация отстают | `desktop.py` + интегратор | полный набор, §8.4 |

### 7.4 Четыре независимых ревьюера

Ревью выполняется по зафиксированному diff; **автор своего участка не является единственным и не является принимающим по умолчанию**. Четыре слота обязательны и не могут быть заняты авторами соответствующих модулей:

| Слот | Зона | Что обязан отвергнуть |
| --- | --- | --- |
| Данные, миграции, согласованность | §3 целиком, §4.1–4.3 | Расхождение между тем, что схема объявляет, и тем, что делает; миграция, которая не идемпотентна; «старый бинарник пишет» |
| Доступ, секреты, границы | §5.1–5.3, §1.2 | Ключ A, получающий данные ключа B; секрет в БД/логах/argv/JSON; молчаливое расширение прав |
| Пользовательские сценарии и платформы | §4.4–4.5, §6 | Обещание шире проверки; потерянное наблюдение; обход freshness «избранным» |
| Производительность, интеграция, полнота объёма | §2.3, §5.7, §7 | API, блокирующийся на время скана; растущий курсор; незакрытое требование, помеченное «почти готово» |

Пока эти четыре слота не отработали, ни один результат не переходит в `verified`: максимум `implemented_unverified`.

## 8. Открытые вопросы и внешние зависимости

1. **Источники — блокер остаётся, но прежняя формулировка была фактически неверна.** Файлы `proxy_workbench/source_adapters.py`, `source_catalog.py` и `source_management.py` в дереве **есть**; `diff -q proxy_workbench/source_adapters.py` с веткой источников `/Users/main/Desktop/111/proxy-workbench-sources` — совпадает побайтно, а `source_catalog.py` и `source_management.py` от неё отличаются. Настоящая причина блокера: **не перенесён финальный handoff ветки источников** — там два более поздних коммита (`e1d57ce`, `8eca9a3`, «Make the source selection actually drive collection…»), которых нет здесь. Приёмка её работы остаётся **внешней зависимостью** (F13, MASTER-PROMPT §6 «Этап I»). Всё, что от неё не зависит, делается здесь: коллекции, импорт, provenance-схема. `proxy-workbench-sources` не редактируется.
2. **`access_revision` существует; настоящий F04 — не реализованная аутентификация на прокси.** Предыдущая редакция этого пункта утверждала, что сущности нет и `grep -rn "access_revision" proxy_workbench/` даёт ноль совпадений. Это неверно: колонка `accesses.access_revision` объявлена миграцией 3, `core.Access` несёт её, ключ `results` различает две ревизии одного адреса, а `secrets.AccessStore.verify_password`/`resolve` умеют проверять и отзывать. **Что действительно не реализовано:** ни `probes.py`, ни `proxytool.request_once` не умеют отправить `Proxy-Authorization` или логин/пароль SOCKS5, а userinfo в URL отвергается намеренно. Поэтому измеренные строки всегда несут публичную, беспарольную идентичность, и «смена пароля отзывает старое доказательство» на сетевом пути не наблюдаемо — не потому, что сущности нет, а потому, что пароль некуда передать. **Владелец: `secrets.py` совместно с транспортом `probes.py`.** Смотри `HANDOFF/review-fixes.ru.md`.
3. **Репозиторий тестов сейчас фиксирует часть дефектного поведения.** `tests/test_freshness.py:87-99` (`test_resume_rechecks_unreachable_and_expired_rows`) вручную дописывает `valid_until = 1` в строку, полученную от реального `scan()`, — такой тест проходит и при текущем неверном поведении, и не поймал бы отсутствие TTL при записи. `tests/test_selection.py:275` закрепляет переключение активного пула при экспорте выделенного. `tests/test_anonymity.py:206-210` (`test_min_anonymity_is_ignored_without_judge`) закрепляет молчаливый сброс требования anonymity как правильное поведение. При смене контракта эти тесты должны быть переписаны **под правильное поведение**, а не удалены. Переписывание — работа владельца теста, не «помощь» со стороны.
4. **Замечание к AGENTS.md.** В `AGENTS.md` есть требование не запускать тесты без запроса пользователя. MASTER-PROMPT §0 явно разрешает локальные unit/integration/browser-тесты на временных данных и моках. При расхождении считать действующим MASTER-PROMPT. Кто именно выполняет полный набор — см. `HANDOFF/README.ru.md` §5: в исполняемом workflow он запускается **трижды безусловно** и **дважды условно**, а не «один раз интегратором».
5. **Версия контракта.** Это документ версии 1. Любое изменение идентичностей, схемы, состояний или кодов, которое ломает уже написанные модули, требует бампа версии здесь и уведомления потребителей в `HANDOFF/` до интеграции — иначе параллельная работа разойдётся по двум разным схемам.
