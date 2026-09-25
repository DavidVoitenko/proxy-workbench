# Handoff: importer

**Требования:** F03 (полноценный импорт), дефект 10 (форма принимает hostname/private, сборщик отвергает), R06 (то же), частично F02 (collections/membership) и F29 (идемпотентность управляющего действия).
**Контракт:** `docs/integration/CONTRACTS.ru.md` §7.1 F03, §5.4 (коды ошибок), §3.3 миграции 1, 2, 12, §6.4 (идемпотентность), §1.2 (Endpoint/Access). Версия контракта 1; новых таблиц и колонок не запрашиваю, см. §5.4.
**База:** ветка `integration/ultra-2026-09-25`. Модуль написан до появления `db.py` в дереве и переведён на `db.migrate()`/`db.connect()` после того, как `db.py` приземлился; тесты идут против реальной схемы `db.py`, а не против копии DDL.

**Мои файлы:** `proxy_workbench/importer.py` (новый), `tests/test_importer_preview.py`, `tests/test_importer_commit.py`, `tests/test_importer_scope.py`, `tests/test_importer_fixtures.py` (новые), этот документ. Чужие файлы не правились.

---

## 1. Прошу внести

### 1.1 `proxy_workbench/proxytool.py` (владелец интегратор): подключить импорт к коллекциям

- **Что сделать:** в текущем пути импорта (`gui.py:168` → `core.normalize_custom_list`, `proxytool.py:648-667` `collect.add`) заменить прямую запись в `candidates` на `importer.preview()` + `importer.commit()` для коллекции, выбранной пользователем.
- **Точный вызов:** `plan = importer.preview(conn, importer.ImportSource.from_text(text), collection_id=<выбранная коллекция>, mode='merge'|'replace')`, затем `report = importer.commit(conn, plan, allow_partial=<подтверждение пользователя>, busy=<занята ли коллекция>, should_cancel=<флаг отмены>)`.
- **Почему здесь:** миграции 1, 2 и 12 существуют ровно для этого; `candidates`/`candidate_seen` сегодня пишутся позиционно и не изолированы по коллекции, что и есть F02/дефект 11.
- **Условие, без которого подключать нельзя:** сборщик обязан принимать тот же `EndpointPolicy`, что и импорт. Иначе возвращается ровно дефект 10, который этот модуль закрывает.

### 1.2 `proxy_workbench/proxytool.py` (владелец интегратор): закрепить пару нормализаторов как единую точку

- **Что сделать:** `importer._classify()` вызывает публичные `proxytool.normalize_custom()` (принимает hostname/private) и `proxytool.normalize()` (только публичные адреса). Второго нормализатора в модуле нет и я не прошу его добавлять. Прошу зафиксировать в контракте, что **эта пара функций — точка нормализации endpoint для всех путей, включая импорт**.
- **Почему:** §1.2 и `HANDOFF/README.ru.md` §2.2 («точка нормализации — `proxytool.py`»). Пока `normalize_custom` существует только как GUI-хелпер для формы, совпадение поведения формы и импорта держится только на том, что оба зовут одну и ту же функцию.

### 1.3 `proxy_workbench/gui.py` + `proxy_workbench/ui/*` (поверхность `web`): мастер импорта

`ui/*` не переписываю и прошу не переписывать: нужен только новый экран мастера в существующем визуальном языке.

| Шаг пользователя | Вызов | Что отдавать в интерфейс |
| --- | --- | --- |
| файл / drop / вставка | `ImportSource.from_path(path)` / `from_drop(payload, path=...)` / `from_text(text)` | `source.name`, `source.digest`, `source.size`; **текст наружу не отдавать** |
| формат | `detect_format(source)` | `importer.FORMATS` и выбранное значение по умолчанию |
| preview | `preview(conn, source, collection_id=..., mode=..., mapping=..., policy=...)` | `plan.counts`, `plan.reasons`, `plan.rows` (у каждой строки `line`, `state`, `reason`, `sample`), `plan.added/unchanged/removed`, `plan.needs_mapping` |
| сопоставление колонок | `plan.mapping_suggestion` + `ColumnMapping(host=..., port=..., scheme=..., country=...)` | `plan.mapping_problem = (missing, ambiguous)` |
| подтверждение | `commit(conn, plan, allow_partial=..., busy=..., should_cancel=...)` | `ImportReport.to_dict()` |
| отмена | не вызывать commit либо `should_cancel` во время commit | — |

### 1.4 Контракт: коды ошибок, которых в §5.4 ещё нет

§5.4 задаёт домен IMPORT как `E_IMPORT_REVISION`, `E_IMPORT_FORMAT`, `E_IMPORT_PARTIAL`. Модулю нужны ещё четыре, и без них API не может ответить пользоватению:

| Код | Когда | Почему нельзя без него |
| --- | --- | --- |
| `E_IMPORT_CANCELLED` | `commit()` отменён через `should_cancel` | отмена и провал commit — разные исходы, §6.2 |
| `E_IMPORT_EMPTY` | `replace`, который очистил бы коллекцию целиком | «файл целиком отклонён» и «результат пуст» неразличимы |
| `E_IMPORT_ENCODING` | файл не в UTF-8 | `E_IMPORT_FORMAT` не говорит, что пересохранить |
| `E_IMPORT_SIZE` | файл больше 20 МБ или больше 1 000 000 строк | то же |

Коды уровня строки (в `plan.rows[].reason` и в `report.reasons`, никогда не исключения): `E_IMPORT_DUPLICATE_IN_SOURCE`, `E_IMPORT_ALREADY_MEMBER`, `E_IMPORT_CREDENTIALS`, `E_IMPORT_HOSTNAME`, `E_IMPORT_PRIVATE`, `E_IMPORT_MISSING_FIELD`; плюс существующие `E_IMPORT_FORMAT` для нечитаемой строки и `E_CONFLICT_BUSY` для занятой коллекции.

Прошу внести их в `CONTRACTS.ru.md` §5.4 или явно отклонить: переводчики (`i18n.py`, владелец `web`) не знают ни одного из них.

---

## 2. Что уже сделано у меня

```python
ImportSource.from_text(text, name=..., channel=...)   # вставка, drag&drop текстом
ImportSource.from_path(path, channel='file')          # файл
ImportSource.from_bytes(data, name=...)               # байты
ImportSource.from_drop(payload, name=..., path=None)  # drop: файл или текст
detect_format(source) -> 'txt'|'uri'|'csv'|'json'
suggest_mapping(columns) -> MappingSuggestion
preview(conn, source, *, collection_id, mode='merge', mapping=None, policy=None,
        fmt=None, idempotency_key=None, now=None) -> Preview
commit(conn, plan, *, allow_partial=False, allow_empty=False,
       busy=None, should_cancel=None, on_progress=None, now=None) -> ImportReport
load_report(conn, batch_id) -> ImportReport | None
create_collection(conn, name, kind='private') -> str
collection_revision(conn, collection_id) -> int
redact(value) -> str
endpoint_id(canonical) -> str     # делегирует db.endpoint_id
```

`Preview`: `batch_id`, `collection_id`, `collection_revision`, `format`, `mode`, `mapping`, `policy`, `rows`, `added`, `unchanged`, `removed`, `removed_ids`, `needs_mapping`, `mapping_problem`, `mapping_suggestion`, `skipped`; свойства `valid`/`duplicates`/`rejected`/`keep`/`keep_ids`/`partial`/`reasons`/`counts`; `to_dict()` — JSON-сериализуемый.
`ImportReport`: те же счётчики плюс `state`, `revision_before/after`, `rejected[{line, reason, sample}]`, `source_name`, `source_digest`, `public_only`, `duration_s`, `replayed`; `to_dict()`/`to_json()`.

Решения, которые стоит знать потребителю:

- **Схема принадлежит `db.py`.** Модуль не пишет DDL и работает на соединении из `db.connect()`: `PRAGMA foreign_keys=ON`, `isolation_level=None`, поэтому `commit()` сам открывает и закрывает единственную транзакцию и сам откатывает её при любой ошибке. `endpoints` пишется апсертом с явными колонками, `membership.origin` — ровно `'import'` из `db.COLLECTION_ORIGINS`, id адреса — `db.endpoint_id`.
- **Политика endpoint по умолчанию — публичная.** `EndpointPolicy(public_only=True)` отклоняет hostname и неглобальные адреса сразу, кодами `E_IMPORT_HOSTNAME` / `E_IMPORT_PRIVATE`. Это осознанный выбор: единственный сквозной потребитель сегодня — `collect.add()` с `normalize()` (публичные IP), и принять hostname в импорте означало бы ровно дефект 10. `public_only=False` существует и пишется в отчёт (`policy.public_only`), но сквозную поддержку собственных адресов закрывает F04 (`secrets.py`), а не этот модуль.
- **Credentials отклоняются всегда и одинаково**: userinfo в адресе, колонки `user/username/login/pass/password/secret/token/api key/auth/credentials` в CSV и одноимённые поля в JSON. Строка с ними отклоняется целиком, а не «импортируется без пароля» — иначе пользователь получил бы адрес, который не работает, без единого предупреждения.
- **Секретов нет нигде**: `ImportSource.__repr__` не показывает текст, в отчёт и в provenance идут только `name`, `digest` и `redact(sample)`; модуль не пишет файлов, не логирует и не запускает процессов.
- **Ревизия коллекции** = `max(import_batch.revision)` по committed-батчам этой коллекции: у `collections` в §3.3 нет колонки ревизии, и я новую не прошу. Поэтому `commit` дополнительно сверяет фактический состав `membership` с preview — если состав меняли в обход импорта, commit всё равно откажется (`E_IMPORT_REVISION`).
- **Идемпотентность по умолчанию** — от содержимого: `batch_id = sha256(collection_id, digest, format, mapping, mode)`. Повторный commit того же файла в ту же коллекцию возвращает тот же отчёт с `replayed=True` и ничего не пишет. Для повторной поставки с другим содержимым передайте `idempotency_key` (F29 §6.4).
- **Отмена и провал** записываются в `import_batch` отдельной транзакцией после отката, чтобы след был; commit после отмены того же preview разрешён.
- **Форматы**: TXT — список `host:port` (схема необязательна), URI — строки с обязательной схемой, CSV — с сопоставлением колонок или по позициям, JSON — список строк, список объектов или объект с ключом `proxies/items/rows/data/list/result`.

---

## 3. Совместимость

- **Что ломается, если §1 не внести:** F03 остаётся закрытым только в модуле: существующий путь импорта (`gui.py:168`, `proxytool.py:648-667`) по-прежнему пишет в общие `candidates` без коллекции, без preview и без отчёта. Пользователь по-прежнему не видит, что именно принято, и merge/replace для коллекций не появляется.
- **Что НЕ ломается:** `importer.py` никто не импортирует; `proxytool.py`/`gui.py`/`api.py`/`ui/*`/`db.py` не тронуты; `candidates`/`results` не меняются; чужие тесты не затронуты.
- **Зависимость от миграции 12 снята:** `db.py` уже в дереве и создаёт `import_batch`, `endpoints`, `collections`, `membership`; тесты импорта идут против `db.migrate()`.

---

## 4. Проверки

Команды запускались из корня репозитория, по одной на модуль (общий `discover -s tests` в это время гоняют другие исполнители):

```
.venv/bin/python -m unittest tests.test_importer_preview   → Ran 19 tests … OK
.venv/bin/python -m unittest tests.test_importer_commit    → Ran 20 tests … OK
.venv/bin/python -m unittest tests.test_importer_scope     → Ran 13 tests … OK
```

Все три идут против схемы, созданной `db.migrate()` во временном файле, и против соединения `db.connect()`.

Что тесты доказывают (содержание, а не «прогоняется»):

- **preview ничего не меняет** — `test_preview_writes_nothing`: полный дамп четырёх таблиц и `sqlite3.Connection.total_changes` до и после preview равны, открытой транзакции нет.
- **счётчики и номера строк** карточки F03 — `test_card_fixture_counts_and_line_numbers`: 10 valid / 2 duplicates / 3 rejected, дубликаты ссылаются на первую строку, отклонённые — точные номера 13, 14, 15.
- **форматы и каналы** — `test_txt_and_uri_reach_the_same_addresses`, `test_csv_header_is_mapped_and_country_is_kept`, `test_semicolon_and_tab_delimiters`, `test_json_list_of_objects_and_of_strings`, `test_file_drag_and_clipboard_reach_the_same_plan`, `test_utf8_bom_and_crlf_are_read`, `test_non_utf8_file_is_named_not_guessed`.
- **mapping** — `test_json_needs_mapping_when_roles_are_missing`, `test_csv_ambiguous_column_is_not_guessed`, `test_mapping_naming_a_missing_column_is_refused`, `test_suggest_mapping_reports_ambiguity`.
- **merge/replace только выбранной коллекции** — `test_merge_touches_only_the_chosen_collection`, `test_replace_shows_a_diff_and_replaces_only_its_collection`, `test_replace_keeps_members_the_file_lists_again`, `test_replace_that_would_empty_the_collection_is_refused`.
- **отмена и crash** — `test_cancellation_leaves_no_half_replace` (отмена на пятой строке восьмистрочного replace: состав коллекции не изменился, батч помечен `cancelled`, повторный commit того же preview проходит) и `test_crash_mid_commit_rolls_back_everything` (`on_progress` роняет соединение на третьей записи: состав прежний, батч `failed`).
- **revision conflict** — `test_stale_preview_is_refused_with_the_revision`, `test_membership_changed_behind_our_back_is_refused`.
- **идемпотентный commit** — `test_repeated_commit_does_not_duplicate`, `test_explicit_idempotency_key_groups_two_previews`.
- **дефект 10 одинаково во всех путях** — `test_every_format_gives_the_same_reason_per_row` (одни и те же четыре адреса в TXT/URI/CSV/JSON дают один и тот же набор кодов отказа и ровно одну принятую строку), `test_every_channel_gives_the_same_result` (clipboard / drop-байты / drop-путь), `test_a_refused_row_is_never_written`.
- **частично плохой файл** — `test_a_partially_bad_file_can_be_fixed_and_imported_again`, `test_partial_file_needs_an_explicit_decision`, `test_partial_import_keeps_the_rejected_lines_for_the_report`.
- **секреты** — `SecretHygieneTests`: canary `CanaryPwd-4f2a9c` не найден ни в отчёте, ни в preview-dict, ни в `import_batch`, ни в байтах файла БД и его `-wal`, ни в одной записи логгера, ни в `repr(ImportSource)`; импорт не создаёт ни одного файла и не обращается к `subprocess`.

Не проверялось: полный `unittest discover -s tests` не запускался (по условию задачи его гоняет интегратор); интеграция с `gui.py`/`api.py` не проверялась, потому что это чужие файлы; сетевых обращений не было — тесты локальные, на временной БД и фикстурах.

---

## 5. Открытые вопросы

1. **Коды §1.4**: принять в `CONTRACTS.ru.md` §5.4 или отклонить? Без них переводчики и API не ответят пользователю.
2. **`membership` и батч**: в `membership.origin` пишется `'import'` (словарь `db.COLLECTION_ORIGINS`), связь с конкретной поставкой — в `import_batch(collection_id)`. Если нужна provenance по каждой строке membership («какой батч добавил этот адрес»), нужна колонка `membership.import_batch_id`; **не прошу**, потому что §3.3 её не объявляет, а добавление миграции — решение владельца контракта.
3. **`public_only=False`**: сейчас это запись политики в отчёт, но сквозной путь для собственных hostname/адресов закрывает F04. Когда `secrets.py` определит, как credentials попадают в импорт, `_classify` придётся расширять — это будет изменение моего файла по вашему решению, а не по моей инициативе.
4. **`accesses` (миграция 3)**: сущности ещё нет, поэтому один endpoint с двумя паролями для импорта неразличим и требование карточки F03 «credential variants не сливаются» не выполнимо на моей стороне. Это зависимость от `secrets.py` + `db.py`, а не от этого модуля.
5. **F27 (URL-подписки) и YAML (F13)** в модуль намеренно не входят: карточка F03 выносит их за минимальный объём. `FORMATS` расширяемый список; добавление адаптера — одна функция `_parse_*` плюс ветка в `_parse`.
