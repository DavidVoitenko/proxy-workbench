# Handoff: sources (`sourcedesk.py`)

**Требования:** F13 (приёмка чужой работы), F27, дефект 22, R16, R17 не входит в мою зону
**Контракт:** `docs/integration/CONTRACTS.ru.md` §1.1–§1.2, §2.3, §3.2–§3.3, §5.4 (версия 1);
`docs/requirements/sources-research/source-system-design.ru.md` §3, §5, §7, §10, §12.4
**База:** `integration/ultra-2026-09-25`, HEAD `d0c986e`
**Мои файлы:** `proxy_workbench/sourcedesk.py`, `tests/test_sourcedesk_{secrets,import,refresh,store}.py`
**Чужое рабочее дерево:** `/Users/main/Desktop/111/proxy-workbench-sources`, ветка `sources-catalog`,
HEAD `806d13d`. Ни одного файла там не изменено — только чтение.

---

## 0. Что закрыто у меня и что это меняет для приёмки

F27 перечисляет девять вещей: пользовательские URL/подписки, привязка source к коллекции,
refresh/delta, merge/replace, last-good, expiry, диагностика quota и token expiry, секретные
URL/headers через references и redaction, поддерживаемые импорты Clash/sing-box.

Проверено чтением их кода: **первые семь пунктов у них не реализованы, восьмой частично,
девятый не реализован.**

| Пункт F27 | Где в их дереве | Что читаю |
| --- | --- | --- |
| Пользовательские URL | `source_catalog.py:36` `USER_SOURCE_FORMATS` — это девять legacy-видов плюс пять адаптеров; `clash`/`singbox` в списке нет | `:645` `custom_source(url, kind='http')` |
| Подписки | `source_catalog.py:629` `collectable_source()` пропускает только `payload_role == 'proxy_list'`; `source_management.py:71-75` прямо пишет, что запись формата подписки намеренно отсутствует | обе ссылки |
| Привязка к коллекции | сущности коллекции нет: в `source_catalog.py:122,405` это только булев флаг `collection_allowed` (можно ли грузить), а в `source_management.py:219,265` — счётчик `membership`, то есть сколько адресов дал источник, без collection_id | те же строки |
| refresh/delta | `proxytool.py:501-578` — `source_generation`/`source_generation_entry`/`source_state` без коллекции | их DDL |
| merge/replace | нет | — |
| last-good | `proxytool.py:542-549` `source_state.last_good_generation` — да, как поколение источника, но не как набор коллекции | их DDL |
| expiry | `proxytool.py:1425`, `:1645` — `backoff_until`/`quarantine_until`; срока данных источника нет | их DDL |
| quota/token expiry | `source_catalog.py:185` — только строка `quota_text` из каталога, поле для заголовков ответа не разбирается | — |
| секретные URL/headers | `source_management.py:344-348` `_endpoint_url()` вызывает `proxytool.public_url()`, который убирает query/fragment, но **оставляет path** | — |
| Clash/sing-box imports | `formats.py:36,63` — это **экспорт** (`clash(rows)`, `singbox(rows)`), обратного пути нет | — |

Итог: их работа — это каталог, адаптеры и read-only представления. Всё, что в F27 связано с
пользовательским URL, коллекцией и жизненным циклом фида, — зона `sourcedesk.py`, и я её сделал.

---

## 1. Точный контракт приёмки чужой работы

### 1.1 Принять безусловно

1. `proxy_workbench/source_catalog.py` — загрузка и строгая валидация каталога, стабильные ID,
   legacy-алиасы, `accept_catalog()` с отказом на понижение ревизии. Структура каталога
   соответствует `source-system-design.ru.md` §5.
2. `proxy_workbench/source_adapters.py` — чистый реестр адаптеров. Ноль HTTP, ноль SQLite.
   Проверено чтением: `grep -n "eval(\|exec(\|subprocess" proxy_workbench/source_adapters.py` —
   0 совпадений; импортируется только stdlib (`base64`, `csv`, `html.parser`, `io`, `json`, `math`,
   `re`, `urllib.parse`, `datetime`).
3. `proxy_workbench/source_management.py` — read-only представления (`build_view`, `detail_view`,
   `history`, `cache_state`) для GUI/CLI/API.
4. Исправление URL каталога: `branding.py:18` в их дереве уже указывает на
   `.../main/proxy_workbench/sources.json`, а в main он по-прежнему `.../main/sources.json`.
   **Это единственный пункт R16, который у них закрыт.**

### 1.2 Принять с обязательной переработкой при переносе

5. **DDL из `proxytool.py:501-578` переносится в `db.py` миграциями, а не остаётся в `open_db()`.**
   Их `open_db()` (`proxytool.py:496`) создаёт одиннадцать новых таблиц через `executescript`
   и не пишет `user_version`. Это прямо нарушает `CONTRACTS.ru.md` §3.2 (`user_version` —
   зеркало `SCHEMA_VERSION`) и §3.5 (единственная точка DDL — мигратор) и правило
   `HANDOFF/README.ru.md` §2.2 «Модуль не пишет DDL». В `db.py` сейчас `SCHEMA_VERSION = 14`
   (`db.py:81`), реализованы миграции 0..14, `user_version` после `migrate()` равен 14 —
   проверено запуском. Значит их таблицы становятся **миграцией 15+**, а не `executescript`.

6. **Ключ `proxy` → `endpoint_id`.** Их `source_generation_entry(generation_id, proxy)` и
   `candidate_seen(proxy, source)` адресованы строкой. `CONTRACTS.ru.md` §1.1 требует сущность
   `endpoints(id, canonical)` и связь `membership(collection_id, endpoint_id)`. Перенос без
   переименования в `endpoint_id` даст две несовместимые модели адреса.

7. **`origin='legacy'`, а не выдуманное происхождение.** `CONTRACTS.ru.md` §3.4 требует для
   legacy-кандидатов `origin='legacy'`, `kind='public'`, имя «Ранее собранные» и прямо запрещает
   выдумывать метку «свои» (F02, дефект 11). У них `candidate_seen_meta.legacy` это
   выражает, но перенос на `membership` обязан сохранить то же.

8. **`candidate_seen_meta.legacy = 1` с `first_seen_at IS NULL`.** Дата миграции не выдумывается
   как дата наблюдения. Проверяемо на синтетической старой базе.

### 1.3 Не принять, а зафиксировать как конфликт (см. §2)

9. `proxytool.py:2692 country_resolver(db, geo, origin='effective')`, вызывается на `:3961` —
   до `collect()` на `:1992`. R16 п.3 не закрыт: свежая metadata источника не попадает в
   snapshot того же запуска.
10. `gui.py:366-379 prune_sources()` — по-прежнему необратимо удаляет источник из выбора по
    `passed == 0` при `checked >= PRUNE_MIN_CHECKED = 20` (`gui.py:71`). R16 п.4 не закрыт, хотя
    `source-system-design.ru.md` §12.4 прямо требовал заменить эту семантику.
11. `gui.py:50-56 public_source()` — по-прежнему `kind + proxytool.public_url(url)`, то есть
    host + путь, а не имя издателя/фида. R16 п.2 не закрыт.
12. `proxytool.py:1578`, `source_management.py:368` — «quarantined» есть, но прочитанный
    `status.json` возраст источника не пересчитывает сам (R16 п.4, вторая половина).

---

## 2. Конфликты с main

| # | Конфликт | Где в main | Что должно быть | Владелец |
| --- | --- | --- | --- | --- |
| C1 | URL каталога смотрит в корень репозитория, а файл лежит по package-path | `branding.py:18` `.../main/sources.json`; файл отслеживается как `proxy_workbench/sources.json` (`pyproject.toml:46`, `packaging/proxy-workbench.spec:11`) | `.../main/proxy_workbench/sources.json` | интегратор (уже исправлено в их diff, §1.1 п.4) |
| C2 | display-name источника = host + путь, а не имя издателя | `gui.py:48-56` `public_source()` | имя из каталога (`publisher.name` + `name`), с URL как отдельным полем; при отсутствии каталога — `source_key`, но подписанный как host | поверхность `web` |
| C3 | `country_resolver` строится до `collect` | `proxytool.py:2389` вызов, `proxytool.py:1527` определение | резолвер должен лениво читать `candidate_meta`/`source_metadata` на каждый адрес, либо пересобираться после collect; сейчас свежая metadata источника в том же запуске не видна | интегратор + `geo.py` |
| C4 | prune необратим: удаляет из выбора по `passed == 0` | `gui.py:321-334`, `PRUNE_MIN_CHECKED = 20` на `gui.py:74`; маршрут `/api/sources/prune` на `gui.py:1044` | quarantine/disable с явным откатом; удаление только по явному списку ID; прочитанный статус пересчитывает возраст сам | `sourcedesk.py` + интегратор (маршрут) |
| C5 | `membership` не выражает принадлежность membership источнику | миграция 2, `CONTRACTS.ru.md` §3.3: `PRIMARY KEY(collection_id, endpoint_id)`, колонка `origin` одна. Проверено на БД после `db.migrate()`: `db.primary_key(conn,'membership')` → `['collection_id','endpoint_id']` | отдельная `membership_source(collection_id, endpoint_id, source_id, origin, added_at, last_seen_at)` — **миграция 15**; без неё «replace» одного источника неотличим от удаления чужого membership | `db.py` (см. §3.1) |
| C6 | их DDL в `open_db` мимо мигратора | их `proxytool.py:496-578` против `CONTRACTS.ru.md` §3.2 | перенос в `db.migrate()` | `db.py` + интегратор |
| C7 | их `candidate_seen`/`candidate_meta` продолжают жить рядом с `endpoints`/`membership` | их DDL | одна модель адреса; `CONTRACTS.ru.md` §3.3 «правила совместимости» | `db.py` + интегратор |

---

## 3. Критерии, по которым я приму их diff

Каждый критерий проверяем одной командой. Пока хоть один не выполнен, их работа — `implemented_unverified`,
и F13 остаётся `external_blocker`.

### 3.1 Моя часть, которую надо посадить на их фундамент

1. **Схема.** `db.SCHEMA_VERSION` увеличен, `db.migrate()` создаёт `source_feed` и
   `membership_source` из `sourcedesk.REQUESTED_DDL`, `user_version` совпадает с константой.
   Команда: `.venv/bin/python -m unittest tests.test_sourcedesk_store` (схема берётся из
   `REQUESTED_DDL`, поэтому расхождение мигратора и модуля ломает его сразу).
2. **Никакого DDL вне мигратора.** `grep -n "CREATE TABLE\|CREATE INDEX\|CREATE VIEW" proxy_workbench/*.py`
   даёт исполняемые совпадения **только** в `db.py`; в остальных файлах — исключительно строковые
   константы вроде `sourcedesk.REQUESTED_DDL`, которые никто не выполняет.
3. **Одна модель адреса.** Их `source_generation_entry`/`candidate_seen` ссылаются на `endpoint_id`,
   а не на `proxy`; `endpoints` заполняются при collect.
4. **Секреты.** `grep -rn "token=\|password=" proxy_workbench/*.py` не находит значений секретов в
   коде, и `sourcedesk.find_secret_leaks` пуст на любой публичной выборке.

### 3.2 Их часть, которую я проверяю приёмкой

5. **Real collect через каждый adapter.** `proxytool.collect()` на локальном mock-сервере
   (`http.server` на loopback, `allow_private_sources=True`) даёт одинаковый candidate set для
   `line`, `json-records`, `fields`, `page-json`, `html-table` и сохраняет golden behaviour
   девяти legacy-видов. Проверка: `tests/test_research_collect_integration.py` из их diff.
6. **304/пустой/битый/большой ответ.** Локальная последовательность `200 → 304`, `200 → empty`,
   `200 → HTML`, `200 → invalid JSON`, `429 → 200`, `timeout → 200` даёт разные наблюдаемые
   состояния, и **ни одно из них не удаляет последний полный набор** (§10.3). Ключевой сценарий
   уже закрыт у меня: `tests/test_sourcedesk_store.AcceptanceFailedUpdateTests` доказывает, что
   моя часть ведёт себя так при `mode='replace'`, их часть должна вести себя так же на своей
   стороне границы.
7. **Миграция старых URL/overrides.** Все 55 legacy-спецификаций из нынешнего
   `proxy_workbench/sources.json` получают однозначный алиас; неизвестный URL сохраняется как
   custom-запись с точным URL, kind и выбором адаптера; удалённый встроенный источник не
   возвращается; новый ID каталога не добавляется сам.
8. **Стабильные ID.** `source_id` не вычисляется из URL: смена URL у существующей записи
   создаёт новую запись, а не переименовывает старую (моя проверка этого же правила —
   `tests/test_sourcedesk_secrets.UserSourceTests.test_source_id_is_stable_for_the_same_binding_and_url`).
9. **Пять доказательных статусов.** `proxy_liveness` у всех записей `not_run`; `url_reachable`
   не бывает `confirmed`; валидатор каталога отклоняет `true`/`confirmed` на уровне источника.
10. **Update не выбирает.** `POST /api/sources/update` возвращает `new_unselected` и не дописывает
    URL в settings (в main это `gui.py:336-354`, где он дописывает).
11. **Prune без необратимого удаления** (R16 п.4, дефект 22). Маршрут принимает только явные ID
    и операцию remove/disable; `passed == 0` не является основанием для удаления.
12. **Display-name** (R16 п.2). Имя издателя/фида из каталога, а не host+путь.
13. **Country resolver после collect** (R16 п.3).
14. **Права и redaction.** `GET /sources` и `/sources/{id}` — read-only, `Authorization`/`Cookie`
    в ответе нет, `public_url` без query и без длинных сегментов пути.
15. **Budgets.** `page-json` и `html-table` уважают пределы из §10.5, включая повтор страницы
    (`SOURCE_PAGINATION_REPEAT`) и запрет внешнего `next`-хоста.

---

## 4. Мой публичный API (на что опираться)

Всё в `proxy_workbench/sourcedesk.py`. Модуль не делает HTTP, не пишет DDL, не имеет своего
нормализатора адресов и ничего не исполняет.

### 4.1 Секреты и redaction

```python
ref = sourcedesk.make_ref('url' | 'href' | 'auth' | 'ref', *parts)   # 'url-<16 hex>'
sourcedesk.redact_url(url)        # query/fragment/userinfo убраны, длинный сегмент пути → '…'
sourcedesk.redact_headers(headers)  # имена видны, значения всегда '<redacted>'
sourcedesk.resolve_url(ref, resolver)          # значение только по запросу
sourcedesk.resolve_headers(refs, resolver)
sourcedesk.find_secret_leaks(value, needles)   # пути, где секрет утёк
```

Правило, которое стоит запомнить: **значение заголовка, данное в `user_source(headers=...)`,
передаётся в `header_put(name, value)` и дальше не хранится.** Без `header_put` вызов падает с
`E_SECRET_VAULT_LOCKED`, а не сохраняет значение. Связка с `secrets.py` — прямой вызов:
`header_put` = `vault.stage` + `vault.mark_ready` (проверено на `secrets.MemoryVault`),
`resolver` = `vault.get`.

### 4.2 Пользовательский источник

```python
source = sourcedesk.user_source(binding_id='b1', url=..., name=..., source_format='clash',
                                headers={...}, header_put=put,
                                access_ref=auth_ref, access_id='access-1')
source.public_view()   # единственное представление, которое можно сериализовать
```

`source_format` ∈ `USER_SOURCE_FORMATS`. `url_ref`/`header_refs`/`access_ref` — ссылки, не значения.

### 4.3 Импорты Clash и sing-box

```python
result = sourcedesk.import_subscription(document, 'clash' | 'singbox')
# либо sourcedesk.import_clash(document) / import_singbox(document)
result.outcome, result.endpoints, result.canonical, result.rejected, result.ignored, result.summary()
```

Поддерживается объявленное подмножество: JSON и ограниченный block-YAML (блочные отображения,
блочные списки, простые и кавычные скаляры, однострочный flow). Якоря, алиасы, теги, блочные
скаляры и многострочный flow **отклоняются** кодом `E_IMPORT_FORMAT`, а не приближаются.

Читается только список endpoint'ов. Всё, что рулит трафиком, попадает в `ignored` и не читается:
`rules`, `rule-providers`, `proxy-groups`, `proxy-providers`, `script`, `listeners`, `tun`, `dns`,
`sniffer`, `hosts`, `profile`, `route`. Группа `selector`/`urltest` **не рекурсивно** обходится.
Неизвестный транспорт (`ss`, `vmess`, `shadowsocks`, `trojan`, …) даёт `E_IMPORT_FORMAT`, а не
молчаливую потерю; отсутствие `version` у sing-box `socks` — отказ, а не догадка. Запись с
credentials отклоняется с `E_SECRET_CREDENTIALS`.

Нормализация адресов **внедряется**: по умолчанию `proxytool.normalize_custom` (собственная
подписка пользователя может содержать hostname или приватный адрес), можно передать
`normalize=proxytool.normalize` для публичного сбора.

### 4.4 Жизненный цикл фида

```python
plan = sourcedesk.plan_refresh(state, result, policy=FeedPolicy(mode='merge'|'replace'),
                               now=..., foreign=(...))
plan.added, plan.removed, plan.kept, plan.retained_shared, plan.delta
plan.applied, plan.applied_removals, plan.promote_last_good, plan.serve_from_last_good
plan.expires_at, plan.next_attempt_at, plan.diagnostics, plan.next_state, plan.summary()

rotation, state2, diagnostics = sourcedesk.plan_rotation(state, access_id=...,
                                                          new_access_ref=auth_ref, now=...)
rotation.invalidates   # ((access_id, старая_ревизия),) — ровно те admissions, которые должны отозваться

sourcedesk.feed_diagnostics(state, now=..., policy=..., quota=...)   # tuple[Diagnostic]
sourcedesk.quota_from_response(headers, now=..., token_expires_at=...)  # QuotaInfo | None
```

Инварианты `plan_refresh`, в порядке важности:

1. отказ не удаляет membership и не двигает last-good;
2. пустой ответ не удаляет membership — рабочая коллекция переживает пустое обновление;
3. обрезанный (`partial`) ответ не удаляет membership;
4. удаление трогает только строки **этого** источника; адрес, который добавил другой источник,
   остаётся в коллекции и попадает в `retained_shared`;
5. `304` подтверждает только транспорт: ни membership, ни поколения, ни продления `expires_at`;
6. `expires_at` пишется один раз, из полного успеха, и не меняется ни отказом, ни редиректом.

`unknown` остаётся `unknown`: `quota_from_response` возвращает `None`, когда заголовка нет, и
`E_SOURCE_QUOTA_UNKNOWN` — когда у фида есть `access_ref`, а квота не объявлена. Нулевая квота
никогда не выдумывается.

### 4.5 Хранилище

```python
desk = sourcedesk.SourceDesk(conn)             # DML только; таблицы создаёт db.migrate()
desk.bind(source, collection_id=..., mode=...)
desk.get / list_feeds / feed_row
desk.membership(source_id, collection_id)
desk.foreign_membership(source_id, collection_id, endpoints)
desk.contributors(endpoint_id, collection_id)
desk.apply_plan(plan, now=...)   -> сохранённый FeedState
desk.apply_rotation(plan, state, now=...) -> invalidates
desk.apply_import(result, source_id=..., collection_id=..., mode=..., now=...) -> RefreshPlan
```

---

## 5. Прошу внести в чужие файлы

### 5.1 `proxy_workbench/db.py` — миграция 15

`db.SCHEMA_VERSION` сейчас 14 (`db.py:81`). Прошу добавить миграцию 15 ровно с этим DDL —
он лежит константой `sourcedesk.REQUESTED_DDL`, чтобы мигратор и тесты не разошлись:

```sql
CREATE TABLE source_feed(
    source_id TEXT NOT NULL, collection_id TEXT NOT NULL, mode TEXT NOT NULL,
    url_ref TEXT NOT NULL, public_url TEXT NOT NULL, source_format TEXT NOT NULL,
    adapter_kind TEXT, adapter_profile TEXT, header_refs_json TEXT NOT NULL,
    access_ref TEXT, access_id TEXT, access_revision INTEGER NOT NULL DEFAULT 1,
    etag TEXT, last_modified TEXT, body_sha256 TEXT,
    active_json TEXT NOT NULL, last_good_json TEXT NOT NULL,
    last_attempt_at REAL, last_success_at REAL, last_good_at REAL, last_validated_at REAL,
    expires_at REAL, next_attempt_at REAL, retry_after REAL, quarantine_until REAL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0, last_outcome TEXT, last_error TEXT,
    PRIMARY KEY(source_id, collection_id));
CREATE TABLE membership_source(
    collection_id TEXT NOT NULL, endpoint_id TEXT NOT NULL, source_id TEXT NOT NULL,
    origin TEXT NOT NULL, added_at REAL NOT NULL, last_seen_at REAL,
    PRIMARY KEY(collection_id, endpoint_id, source_id)) WITHOUT ROWID;
CREATE INDEX membership_source_by_source ON membership_source(collection_id, source_id);
CREATE INDEX source_feed_by_collection ON source_feed(collection_id);
```

Почему именно так: `membership` из миграции 2 имеет `PRIMARY KEY(collection_id, endpoint_id)` и
одну колонку `origin` (проверено: `db.primary_key(conn, 'membership')` → `['collection_id','endpoint_id']`).
Из этого ключа нельзя выразить «эту membership создал источник S», а без этого «replace» одного
источника неотличим от удаления чужой membership — это ровно тот отказ, который запрещает приёмка F27.

### 5.2 `proxy_workbench/proxytool.py`

- сделать `parse_source_url` публичным (сейчас `_parse_source_url`, `proxytool.py:112`).
  `sourcedesk` делает только синтаксическую проверку URL пользователя и **не дублирует** SSRF-гейт;
  окончательная проверка обязана остаться в `_parse_source_url`/`collect`.
- `country_resolver` (`proxytool.py:1527`, вызов `proxytool.py:2389`) — не строить snapshot до
  `collect`; читать `candidate_meta` лениво. R16 п.3.
- `collect` после переноса их DDL должен писать `endpoints` и `membership_source`, а не только
  `candidate_seen`.
- `/api/sources/prune` и `/api/sources/update` (`gui.py:1044-1046`) перестают быть авторами выбора;
  маршрут остаётся совместимым, но вызывает сервис `sourcedesk`, а не правит settings сам.
  R16 п.4, дефект 22.

### 5.3 `proxy_workbench/api.py`

`GET /v1/sources` и `GET /v1/sources/{id}` читают `source_feed` через `SourceDesk.feed_row()`,
`GET /v1/collections/{id}/members` — `membership_source`. `export.secret`-подобное право
(`CONTRACTS.ru.md` §5.2) требуется, чтобы отдать `url_ref`/`header_refs`: без него наружу уходят
только `public_url` и имена заголовков.

### 5.4 `proxy_workbench/gui.py` и `proxy_workbench/ui/*`

Экран Sources: у каждой строки имя издателя из каталога, затем `public_url`, затем состояние
`http_state`/`cache_state`/возраст last-good/`retry_after`. Запрещённые слова — `dead source`,
`working source`, `quality source` (R16 п.2 и `source-system-design.ru.md` §12.4). Разметку и
стили не переписывать: добавить колонки в существующую таблицу источников.

### 5.5 `proxy_workbench/core.py`

`admit(row, scope, access, policy, now)` должен принимать `access = (access_id, access_revision)`
и **обязан** отзывать прошлое доказательство при смене `access_revision` (`CONTRACTS.ru.md` §1.2(1)).
`sourcedesk.plan_rotation` отдаёт список `invalidates = ((access_id, старая_ревизия),)`, который
является входом этого отзыва; сейчас `RotationPlan` формируется, но `core.py` его ещё не читает,
потому что `core.py` на момент написания отсутствовал.

### 5.6 `proxy_workbench/secrets.py`

Ничего менять не нужно. Нужна только фиксация связки: `sourcedesk` не хранит секрет, он просит
`header_put(name, value) -> ref` и получает значение обратно через `resolver(ref) -> str`.
Реализуется поверх `Vault.stage` / `Vault.mark_ready` / `Vault.get` без правок `secrets.py`.

---

## 6. Проверки

Команды выполнены из корня репозитория в этой сессии, на `.venv`:

```
.venv/bin/python -m unittest tests.test_sourcedesk_secrets   → Ran 34 tests … OK
.venv/bin/python -m unittest tests.test_sourcedesk_import    → Ran 33 tests … OK
.venv/bin/python -m unittest tests.test_sourcedesk_refresh   → Ran 39 tests … OK
.venv/bin/python -m unittest tests.test_sourcedesk_store     → Ran 25 tests … OK
```

Плюс интеграционная проверка, выполненная скриптом в этой сессии (не тест репозитория): мой
`SourceDesk` против **настоящей** схемы `db.migrate()` и настоящего `secrets.MemoryVault` —
`user_version` 14, `source_feed` и `membership_source` создаются из `REQUESTED_DDL`,
`membership` из миграции 2 остаётся, canary-секрет в строках `source_feed` не найден,
`resolve_headers` отдаёт значение только по запросу, после невалидного импорта membership
не изменился, ротация вернула `(('access-1', 1),)` и подняла ревизию до 2.

Что **не** выполнено: полный `unittest discover -s tests` (по условию задачи его гоняет
интегратор), прогон тестов в `/Users/main/Desktop/111/proxy-workbench-sources` и любые сетевые
проверки. Соседние тесты (`tests/test_providers.py`, `tests/test_sources_gui.py`) мной не
запускались — я не менял ни одного существующего файла, `git status --porcelain` показывает
изменёнными только `ui/app.js`, `ui/index.html`, `ui/style.css` (чужой поверхности `web`) плюс
новые файлы.

## 7. Открытые вопросы

1. **Номер миграции.** Прошу интегратора выбрать 15 (следующий свободный) и внести DDL из §5.1
   дословно. Если `membership_source` решится вкладывать в `membership` четвёртой колонкой —
   это будет конфликт двух писателей, и я против: первичный ключ `membership` тогда придётся
   менять, а это запрещено правилом «только аддитивные миграции» (кроме названной 13).
2. **Имя `origin` для membership источника.** Я использую `'source_subscription'`
   (`sourcedesk.ORIGIN_SUBSCRIPTION`) и `SourceDesk(conn, origin=...)` позволяет переопределить.
   Каталог и подписка — разные происхождения; если `db.py` заведёт свой перечень, прошу
   согласовать, чтобы значения не расходились.
3. **Кто переносит DDL соседнего workflow в `db.py`.** Это общая работа `db.py` и интегратора;
   я не могу сделать её за них, потому что `db.py` мне не принадлежит, а правило одного
   writer'а сильнее удобства.
4. **`sources.json` в main я не трогал.** Файл мне формально принадлежит, но менять его —
   значит завести второй каталог рядом с их `schema_version: 1` (см. `sources.json:1-8` в их
   дереве). Текущий плоский массив из 55 URL читают `gui.py:101` и `proxytool.py:1989`, и он
   остаётся в силе до тех пор, пока их каталог не станет источником истины.
