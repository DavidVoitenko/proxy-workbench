# Handoff: интегратор

**Требования:** F01–F29 (подключение), дефекты 1, 2, 3, 4, 6, 7, 8, 9, 20, F18, F29
**Контракт:** `docs/integration/CONTRACTS.ru.md` §1–§7 (версия 1)
**База:** ветка `integration/ultra-2026-09-25`, HEAD `1630cb8` на момент начала

Строка владения: `proxy_workbench/proxytool.py` и `proxy_workbench/api.py`.
Всё остальное подключалось, а не переписывалось. Ниже — стыки, которые пришлось
починить в чужих файлах, и то, что осталось за моей строкой владения.

## 1. Правки в чужих файлах (стык, а не модуль)

### 1.1 `packaging/smoke.py:77-81` (владелец: `desktop.py`)

**Было:** `sqlite3.connect(...)` + `INSERT INTO candidates VALUES (?)` — позиционная
вставка. После миграции 5 у `candidates` две колонки, и скрипт падал с
`OperationalError: table candidates has 2 columns but 1 values were supplied`.
Это ровно тот отказ, который F24 и CONTRACTS §3.5.2 **требуют**: «старый бинарник
не пишет в новую схему». Откатывать механизм нельзя.

**Стало:** кандидат добавляется тем же публичным путём, что и у пользователя:
`collect --no-sources --input <файл> --allow-private-endpoints`. Loopback-адрес
принимается только из локально переданного списка и только по явному флагу — это
не обход политики, а её штатное явное состояние (см. §1.2).

Две правки в том же файле, обе следуют из чужих изменений:

- `packaging/smoke.py:119-133` — подключение к шлюзу через `copy_address`, а не через
  `http://<address>`. Дефект 18/R12 закрыт: у ротатора теперь **свой** пароль, а не токен
  GUI-сессии; запрос без учётных данных получает 407.
- `packaging/smoke.py:126-137` — ожидание, пока listener подхватит новое поколение.
  `gateway.Pool.refresh` намеренно читает экспорт по своему интервалу (`refresh_interval`,
  по умолчанию 2 с, `gateway.py:968`), а не на каждом запросе; запрос в ту же секунду
  обслуживает предыдущее поколение. Это не ретрай сбоя, а часть сценария.

### 1.2 Новый флаг `--allow-private-endpoints` в `proxytool.py`

Дефект 10 («форма принимает, сборщик отвергает») был закрыт только внутри
`importer.py`. У CLI не было **никакого** пути для своего адреса: `collect`,
`import` и `scan` пользовали `normalize()` (только публичные адреса), а GUI-форма
молча отбрасывала всё, кроме глобальных IP. Теперь:

- `proxytool.py:_normalize_proxy`/`normalize_custom` — как было, две политики;
- `collect(..., allow_private_endpoints=...)` — флаг действует **только** на локальный
  `--input`. Удалённый источник остаётся строго публичным при любом флаге
  (`collect.add(..., public_only=)`, источники зовут `add` с `public_only=True`);
- `import --allow-private-endpoints` — `importer.EndpointPolicy(public_only=False)`;
  credentials в URL отвергаются структурно и флагом не включаются;
- `import --collection ИМЯ` создаёт недостающую коллекцию **пустой** и личной
  (`_resolve_collection(..., create=)`) — раньше имя, которого нет, давало
  «Коллекция не найдена» и у пользователя не было способа начать свой список из CLI.

### 1.3 `_op_exports_create` в `api.py` — была заглушка

`api.py:818-822` отвечала `409 E_STATE_NOT_FOUND` с текстом «export runs on the GUI
or the CLI». По приёмочному сценарию F29 «после bootstrap администратор проходит
полный путь» это делало последний шаг недостижимым для пользователя, у которого есть
только ключ: импортировать и запустить он мог, экспортировать — нет. Маршрут теперь
строит артефакт через единственный сборщик `proxytool.export()` и сам решает, какой
файл отдаёт. `kind='selection'` и `kind='diagnostic'` не трогают активный указатель
(дефект 7), `kind='published'` публикует.

### 1.4 `_op_exports_download` в `api.py` — `KeyError` на каждом артефакте

`export_artifact` (миграция 11) не имеет колонки `directory`; обработчик читал
`artifact['directory']` и падал на 500 для любого артефакта, который API сам же и
записал. Каталог выводится из `generation`, а allow-list — из `manifest_json` той же
строки: имя не из манифеста даёт 404, даже если файл с таким именем есть.

## 2. Что сделано в моих файлах сверх подключения

| Место | Что |
| --- | --- |
| `proxytool.py: management_command` | Ловится `importer.ImportProblem` (базовый класс), а не `ImportFormatError`. `ImportPartialBlocked`, `ImportBusy`, `ImportRevisionConflict` выходили трассировкой; теперь строка с кодом `E_IMPORT_*` под сообщением |
| `api.py: _module_errors` | Тот же базовый класс вместо перечисления семи подклассов — отказ, который модуль добавит позже, тоже станет кодом |
| `api.py: public_row` | Добавлены `max_age_seconds` и `ttl_backfilled` (CONTRACTS §2.4 требует видеть бэкфилл в API) |
| `api.py: public_row` | `time_state` и `freshness` выводятся из **одного** вызова `core.time_state_of` и в одном месте переводятся в коды `TIME_STATE_CODES`. Раньше `time_state` читался из сохранённой строки, а `freshness` пересчитывался — строка могла одновременно сказать `time_ok` и `freshness='expired'` |
| `api.py: rows_for` / `_freshness_view` | `/v1/results` читал параметр `freshness`, которого маршрут не объявляет, и игнорировал объявленные `include_stale` / `include_unknown`. Истёкшая после публикации строка была недостижима ни при каком запросе |
| `api.py: _endpoint_values` | `endpoint_ids` объявлен маршрутом как строка, а читался как список — значение, посланное по схеме, не доходило. Принимаются оба написания через один разбор |

## 3. Проверки (выполнены в этой сессии)

```
.venv/bin/python -m unittest discover -s tests      Ran 2368 tests … OK (skipped=1)
.venv/bin/python -m unittest tests.test_acceptance  Ran 35 tests … OK
.venv/bin/python packaging/smoke.py .venv/bin/proxy-workbench
  Proxy Workbench 2.2.1
  {"exit_code": 0, "passed": 1}
  smoke test passed
  (код возврата 0)
```

Плюс точечные прогоны на исполняемом коде, локально и без сети:

- **дефект 1/2** — `valid_until` записан при измерении; переэкспорт с другим `--watch`
  не изменил ни `checked_at`, ни `valid_until` ни у одной из трёх строк;
- **дефект 3** — при одной истёкшей строке набор остался `state='partial'`,
  `state_detail='rejected'`, `exported=1`, `expires_at` равен сроку новой строки, API
  отдал 1 строку из того же поколения;
- **дефект 4** — `E_TIME_UNKNOWN`, `E_TIME_FUTURE`, `E_TIME_TTL_EXPIRED`,
  `E_TIME_CLOCK_ROLLBACK` как четыре явных состояния; откат часов ловится и держится;
- **дефект 7** — `proxytool.export` двумя вызовами подряд: указатель до и после
  экспорта выделенного совпал, API continued отдавать опубликованные 2 строки;
- **дефект 9** — битый `current.json` даёт одинаковое явное `E_STATE_NO_SNAPSHOT` в
  GUI, в legacy `/status` и в `/v1` (тест приёмки
  `test_a_broken_pointer_is_the_same_explicit_unavailable_everywhere`).

## 4. Прошу внести в чужие файлы

### 4.1 `proxy_workbench/gui.py` + `ui/app.js` (поверхность `web`)

- **Нет переключателя коллекции.** `App.start` собирает командную строку воркера
  (`gui.py:1066-1080`) и **не передаёт `--collection`**. В результате у пользователя
  нет пути «свой список из CLI → проверка из GUI»: `--allow-private-endpoints` и
  `--collection` живут только в CLI. Прошу добавить выбор коллекции в форму и
  передавать `--collection` в воркер.
- **Нет флага доверенного private.** Даже после выбора коллекции GUI не может сказать
  воркеру, что его список локальный и допускает имя хоста. Нужен один чекбокс,
  передающий `--allow-private-endpoints`.
- **Дубликаты состояния времени.** `gui.py:826` строит `admission.Scope` из
  `status['collection_id']`, а строка таблицы теперь несёт `time_state`,
  `max_age_seconds` и `ttl_backfilled`. Экран «почему 0 результатов» должен читать
  их, а не считать возраст заново.

### 4.2 `proxy_workbench/apiv1.py`

- `endpoint_ids` объявлен `kind='string'`, но `max_items=512` на строке бессмысленен.
  Либо `kind='list'`, либо `max_items` убрать. Сейчас `api.py` принимает оба написания,
  чтобы маршрут и обработчик не расходились.
- В теле `/v1/checks/*` объявлено `budget object` и одновременно `max_seconds int`;
  `budget` не документирован перечнем полей. Одно из двух лишнее.

### 4.3 `proxy_workbench/gateway.py` (поверхность `gateway`)

- `E_GATEWAY_DENIED` и `E_GATEWAY_SESSION_STRICT` в справочнике кодов отсутствуют
  (подтверждено и в моём проходе: `grep -c` по `diagnostics.py` даёт 0). Шлюз их
  возвращает, перевода нет.

### 4.4 `proxy_workbench/db.py`

- `export_artifact` не хранит `directory`, и это правильно (иммутабельное поколение
  выводится из `generation`). Стоит зафиксировать это в `HANDOFF/db.md`, чтобы никто
  не прочитал колонку, которой нет.
- `schedules` не имеет колонок, которые просит `scheduler.py` (`last_run_at`,
  `paused`, `dst_policy`, `catch_up`, `budgets_json`, `counters_json`). Перезапуск
  процесса теряет интервальную сетку и накопленный бюджет расписания.

## 5. Чего я не закрывал и почему

- **`access_revision` end-to-end.** Сущность появилась (`secrets.py`, миграции 3 и 13),
  и приёмка «смена пароля отзывает старое доказательство» проходит на уровне
  admission-контракта (тест `test_a_password_change_revokes_the_previous_proof`).
  Но в `gateway.Binding` поля `access_revision` по-прежнему нет, поэтому «новая версия
  доступа одинаково влияет на GUI/API/gateway/new connection» на шлюзе не доказана.
  Файл `gateway.py` — не мой.
- **F13 (каталог источников).** Внешний блокер: приёмка чужой ветки
  `sources-catalog` невозможна, пока та не присоединена. `sourcedesk.py` в дереве и
  подключён к CLI (`source list|health|redact`) и к `/v1/sources*`.
- **Значения без строки в CONTRACTS §5.4.** `E_TIME_TTL_MISSING`,
  `E_STATE_SNAPSHOT_STATIC_TTL`, `state_detail='all_untrusted'` (из `core`),
  `E_STATE_JOB_NOT_FOUND`, `E_STATE_ITEM_NOT_FOUND`, `E_STATE_JOB_TRANSITION`,
  `E_STATE_JOB_INTERRUPTED`, `OBSOLETE_MEMBERSHIP` (из `jobs`),
  `E_AUTH_DISABLED` (из `apikeys`), `E_CONFLICT_NAME`, `E_CONFLICT_ARCHIVED`,
  `E_STATE_PROFILE_UNKNOWN` (из `profiles`), `E_SECRET_VAULT_UNAVAILABLE` (из `secrets`).
  Формат у всех `E_<DOMAIN>_<REASON>`; канон §5.4 их не перечисляет. Нужна одна
  правка таблицы, а не семнадцать решений по одному.
