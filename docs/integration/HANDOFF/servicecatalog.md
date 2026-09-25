# Handoff: servicecatalog

**Требования:** F06 (каталог сервисов и наборов), дефект 24 (presets не оставляют параметров предыдущего сценария; название website-ответа не обещает звонки/4K/весь сервис), R17 (versioned profile presets с явным составом и сбросом полей)
**Контракт:** `docs/integration/CONTRACTS.ru.md` §5.4 (перевод кодов через `i18n.tr`), §7.1 строка F06, §7.2 дефект 24, §7.3 R17 (версия 1)
**База:** `integration/ultra-2026-09-25`, ревизия `d0c986e`

**Мои файлы (единственный writer — я):** `proxy_workbench/servicecatalog.py`, `proxy_workbench/data/service_sets.json`, `tests/test_servicecatalog_catalog.py`, `tests/test_servicecatalog_scenario.py`, `tests/test_servicecatalog_pinning.py`, этот файл.

---

## 1. Прошу внести в чужие файлы

### 1.1 `pyproject.toml` → владелец интегратор (и `desktop.py`)

Строки:

```toml
[tool.setuptools.package-data]
proxy_workbench = ["sources.json", "ui/*"]
```

Заменить на:

```toml
[tool.setuptools.package-data]
proxy_workbench = ["sources.json", "ui/*", "data/service_sets.json"]
```

**Почему:** `load_catalog()` читает `Path(__file__).with_name('data') / 'service_sets.json'`. Без этой строки каталог есть в дереве и в editable-установке, но отсутствует в wheel/sdist, и `load_catalog()` начнёт падать с `CatalogError` только у пользователя после установки. Это ровно тот класс отказа, который CONTRACTS §4.6 запрещает прятать (fail-closed должен быть явным сообщением, а не `FileNotFoundError` у неподготовленного пользователя).

### 1.2 `.gitignore` → владелец интегратор

Первая строка файла — `data/`. Паттерн без ведущего слэша совпадает с каталогом
`data` на любом уровне, поэтому `proxy_workbench/data/service_sets.json` оказался
невидимым для `git add` и был закоммичен принудительно (`git add -f`).

Файл в ревизии уже отслеживается и обычными операциями не потеряется, но
`git clean -X` или пересоздание клона его уберут. Прошу добавить в `.gitignore`:

```
!proxy_workbench/data/
```

Это правка чужого файла, поэтому я её не делал и коммит принудительного добавления
делал только для своего файла.

### 1.3 `proxy_workbench/ui/app.js` → владелец поверхность `web`

Два места, оба найдены чтением, номера строк указаны по текущей dirty-ревизии.

**(а) `TARGET_PRESETS` (`app.js:1511-1520`) — девять встроенных массивом объектов.**

Прошу заменить чтение из встроенного массива на ответ бэкенда и не дублировать определения в JS:

- маршрут: `GET /v1/services` → `servicecatalog.load_catalog().summary()`;
- `fillPresets()` (`app.js:1527`) строит `<option>` из `presets[]`, чипы в `#quick-services-chips` — из `service_sets[]`;
- у каждого сервиса в UI обязаны быть видны `capability`, `not_proved[]` и `definition_checked_on` — иначе R17 («названия обещают больше, чем выполняют probes») воспроизводится снова, уже на новых данных.

**Почему здесь:** `04-feature-cards.ru.md:127` — «Перенести действующие presets в версионируемый data manifest»; девять определений теперь живут в `service_sets.json` с version/maintainer/last-checked/not_proved, и в JS они больше не нужны. **Что НЕ ломается:** внешний вид — те же девять chips в том же порядке, тот же `id` селекта `#target-preset`, те же `toast('presets.added')`.

**(б) Обработчик сценариев (`app.js:4352-4421`, `card.onclick`) — источник дефекта 24.**

Сейчас каждый сценарий пишет часть полей через `setVal`/`setChecked` и не трогает остальные. Воспроизведено чтением: сценарий `anon` (строка 4389-4395) ставит `judge-url`, `dnsbl-zones`, `dnsbl-enabled`, `strict-clean`, `min_anonymity`, а сценарий `youtube` (строка 4382-4388) эти пять полей не сбрасывает. Итог: после «Elite Приватность» переключение на «YouTube и видео» меряет YouTube с judge'ем, strict-DNSBL и требованием elite. Это ровно дефект 24 и R17.

Прошу перевести сценарии на один вызов:

```python
# backend (gui.py)
applied = servicecatalog.apply_scenario(current_settings, pinned.scenario,
                                        catalog.user_fields(), targets=pinned.targets())
```

и на фронте показывать `applied.report()` (`set` / `cleared` / `carried_over`), а не подсветку только изменённых inputs. Отдельно прошу убрать обещание из подписи карточки: `'scenario.youtubeDesc'` (`app.js:1106`) обещает «стабильный 1080p/4K», что 204-эндпоинт не доказывает; новая строка — «доступность видеосервиса по контрольному 204-эндпоинту; разрешение и DRM не измеряются».

**Почему здесь:** единственный writer на `ui/app.js` — поверхность `web` (HANDOFF §1.3); правку чужого файла я не делаю.

### 1.4 `proxy_workbench/gui.py` → владелец поверхность `web`

Две точки, по одной на каждое направление:

- **чтение каталога для UI**: добавить в существующий dispatch `GET`-маршрут ответ `servicecatalog.load_catalog().summary()` и `servicecatalog.search_presets(...)` по `q`/`category`/`capability`. Структура ответа уже готова (`Catalog.summary()`, `Preset.to_dict()`), отдельного формата не нужно.
- **сохранение ревизии профиля**: `App.start`/сохранение настроек должны писать в конфиг профиля `servicecatalog.pin_set(catalog, set_id).to_dict()` целиком, а не только `set_id`. `PinnedSet.from_dict()` читает его обратно.

**Почему:** `PinnedSet` самодостаточен (см. §2), и только такая запись даёт F06 «обновление presets не меняет молча уже сохранённый profile revision». `profiles.py` (см. 1.5) тоже это требует — нужно одно поле, а не два.

### 1.5 `proxy_workbench/profiles.py` → владелец исполнитель `profiles.py`

Колонки/поле для снимка каталога в ревизии профиля. По CONTRACTS §3.3 миграция 9 добавляет в `profiles` `name/revision/parent_id/digest/created_at/archived_at/is_default` — места под полный снимок там нет.

**Прошу:** хранить снимок внутри `profiles.config` (это уже `TEXT NOT NULL`) как ключ `"service_set"` со значением `PinnedSet.to_dict()`, либо добавить колонку через `db.py` отдельной миграцией **свыше 14** и объявить её в `CONTRACTS.ru.md §3.3` до интеграции.

- почему не в `config` как есть: `config` участвует в `profile = sha256(json.dumps(config, sort_keys=True))[:20]` (`proxytool.py:1298-1299`), то есть снимок входит в `profile_id` и не будет молча меняться — это плюс; минус в том, что любой новый снимок меняет `profile_id` и инвалидирует строки результатов. Решение за владельцем `profiles.py`, я не настаиваю на конкретном варианте, но не предлагаю хранить только `set_id` + `version`: этого недостаточно, снимок нужен, чтобы доказательство воспроизводилось без каталога.

### 1.6 `proxy_workbench/proxytool.py` → владелец интегратор

Изменений DDL и валидатора **не требуется**, это проверено тестом `test_generated_targets_pass_the_real_scanner_validator` (`tests/test_servicecatalog_catalog.py`): каждый target, который строит `PinnedSet.targets()`, принимается существующим `core.validate_targets` без послаблений.

Требуется только **вызов** при сборке конфига скана: брать `targets` из сохранённого `PinnedSet`, а не из массива в JS. Сейчас путь единственный — `target_config()` (`proxytool.py:893`), он читает `config['targets']`; то есть после 1.4 достаточно положить `targets` в настройки, и CLI/GUI/API получат один и тот же список (F18).

---

## 2. Что уже сделано у меня

Публичный API `proxy_workbench/servicecatalog.py`:

| Вызов | Аргументы | Возвращает |
| --- | --- | --- |
| `load_catalog(path=None)` | путь к манифесту или `None` | `Catalog` — уже валидированный, неизменяемый |
| `parse_catalog(data)` | декодированный манифест | `Catalog`; бросает `ManifestError` с точным путём поля |
| `search_presets(catalog, query='', category=None, capability=None, include_deprecated=False)` | как выше | `tuple[Preset, ...]`, порядок каталога |
| `select_presets(catalog, preset_ids, limit=20)` | список id | `tuple[Preset, ...]` в порядке вызова, без повторов |
| `build_targets(presets)` | пресеты | `list[dict]` — ровно форма `core.validate_targets` |
| `estimated_cost(presets)` | пресеты | `dict` с `requests_per_pass` / `max_bytes` / `budget_weight` |
| `pin_set(catalog, set_id)` | каталог, id набора | `PinnedSet` — самодостаточный снимок |
| `PinnedSet.targets()` | — | `list[dict]`; строится **только из снимка**, каталог не читается |
| `PinnedSet.to_dict()` / `from_dict(payload)` | — | JSON-совместимый снимок |
| `preview_update(catalog, pinned)` | — | `UpdatePreview` с `changes[]` и `scenario_changes`; ничего не применяет |
| `upgrade_set(catalog, preview)` | — | новый `PinnedSet`; **явный** шаг после показа diff |
| `apply_scenario(settings, scenario, user_fields=(), targets=None)` | текущие настройки, полный состав набора, поля пользователя, цели | `AppliedScenario` с `settings`, `set_fields`, `cleared_fields`, `unchanged_fields`, `carried_over_fields`, `report()` |
| `new_user_set(catalog, set_id, title_ru, title_en, preset_ids, ...)` | мультивыбор | `PinnedSet` с `origin='user'`, копии сервисов внутри |

Типы данных: `Probe`, `Cost`, `Capability`, `Category`, `Preset`, `ServiceSet`, `Catalog`, `PinnedSet`, `PresetChange`, `UpdatePreview`, `AppliedScenario`. Ошибки: `CatalogError` (базовый) и `ManifestError` (манифест).

Три инварианта, которые держит парсер, а не автор манифеста:

1. `not_proved_ru` и `not_proved_en` обязательны и непусты; в заголовках запрещены обещания (`\b4k\b`, `\bзвонк\w*`, `\bfull service\b`, …) — в описаниях запрещено только обещание всего сервиса, потому что «не доказывает 4K» — это обязательное раскрытие, а не обещание.
2. Способности `websocket`, `media`, `long_connection` объявлены как `probeable: false` с причиной; preset с такой способностью **отклоняется**. Ни один preset каталога их не заявляет.
3. Набор обязан объявить полный состав полей сценария (`field_scope` манифеста: 18 полей сценария, 1 производное `targets`, 14 полей пользователя). Отсутствующее поле — `ManifestError`, а не «дописываем дефолт».

Политика сброса дефекта 24: значение `null` в `scenario` означает сброс в документированное значение из `SCENARIO_DEFAULTS`; `AppliedScenario.cleared_fields` перечисляет именно сброшенные поля. `SCENARIO_DEFAULTS` — единственная таблица сброса, и тест сверяет её с `gui.defaults()`, чтобы поверхностный дефолт не разошёлся с каталогом молча.

Манифест: 17 сервисов (9 существующих без изменения уровня доказательства + 8 исследованных), 7 наборов (`basic`, `search`, `messengers`, `social`, `video`, `development`, `api`), категории совпадают с именами наборов плюс поиск по названию/алиасу/хосту.

---

## 3. Совместимость

**Что ломается, если не внести 1.1:** установленный wheel/sdist не содержит `data/service_sets.json`; первый вызов `load_catalog()` у пользователя падает. В дереве разработки и в editable-установке всё работает, то есть поломка проявится только после сборки — поэтому пункт внесён первым.

**Что ломается, если не внести 1.2(б):** дефект 24 остаётся открытым ровно в текущем виде — это воспроизведено тестом `test_switching_from_elite_to_video_leaves_no_judge_or_strict` на backend-пути и чтением `app.js:4382-4395` на UI-пути.

**Что НЕ ломается:**

- `proxytool.py`, `api.py`, `db.py` не требуют изменений для валидации: тест гоняет `core.validate_targets` по всем семи наборам.
- Существующие девять presets сохраняют URL, коды и `contains` ровно как в `app.js:1512-1520`; тест `test_nine_legacy_presets_survive_with_their_evidence_level` это фиксирует.
- `gui.defaults()`, `gui.validate()` и `core` не тронуты. Модуль только **читает** `i18n.tr` (CONTRACTS §5.4 требует перевод через существующий механизм) и не пишет ни в БД, ни в файлы.
- Ни одного DDL, ни одной миграции, ни одной новой сетевой точки: `servicecatalog` не открывает сокет. Живая проверка preset'ов не выполняется и не должна выполняться в CI (`04-feature-cards.ru.md:127`).
- Секретов в модуле, манифесте и тестах нет; `headers` ограничены списком `SAFE_PROBE_HEADERS`, URL с userinfo отклоняются (F04/F07), тест `test_probe_with_credentials_in_url_is_refused` это фиксирует.

---

## 4. Проверки

Команды запускались из корня репозитория, ровно по одному модулю за раз (полный `discover -s tests` не запускался — он в это время гоняют другие исполнители).

```
$ .venv/bin/python -m unittest tests.test_servicecatalog_catalog
Ran 58 tests in 0.021s — OK

$ .venv/bin/python -m unittest tests.test_servicecatalog_scenario
Ran 27 tests in 0.009s — OK

$ .venv/bin/python -m unittest tests.test_servicecatalog_pinning
Ran 33 tests in 0.078s — OK
```

Дополнительно выполнено:

```
$ .venv/bin/python -m json.tool proxy_workbench/data/service_sets.json
(вывод не показан, код 0 — валидный JSON)
```

**Что осталось непрочитанным / невыполненным:**

- Живая проверка определений через прокси **не выполнялась** — прямая проверка сторонних сервисов запрещена условиями задачи. Все `live_checked_on` в манифесте равны `null`, поле `verification` честно различает `documented` / `legacy_definition` / `unverified_live`.
- Хост эндпоинта Steamworks Web API официальной документацией в этой сессии **не подтверждён**: `partner.steamgames.com/doc/webapi/ISteamUserStats` дважды отдал в примере `https://partner.steam-api.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/`, а `doc/features/api` и `doc/webapi/overview` базовый URI не содержат. Поэтому в каталог включена только витрина `https://store.steampowered.com/` (`steam-store-home`), а в `definition_source` записано, что API-проба не добавлена именно из-за этого. Это единственный пункт задачи «исследуй дополнения по реальным endpoint contracts», где результат исследования отрицательный, и он зафиксирован в данных, а не в комментарии.
- Reddit: `support.reddithelp.com/.../14945211791892` вернул HTTP 403, `redditinc.com/policies/data-api-terms` не отдал текст. Подтверждённого неаутентифицированного Data API эндпоинта у меня нет, поэтому `reddit-home` — homepage-проба без утверждений по содержимому, и это записано в `limitations_ru`.
- Twitch: подтверждено только «The Twitch API uses OAuth 2.0 for authentication» (`dev.twitch.tv/docs/api/`), неаутентифицированный эндпоинт не задокументирован. В каталог вошёл `signal-home` вместо Twitch API, а `discord-gateway` — единственный проверяемый discovery.
- TikTok/X: официальная документация не подтвердила неаутентифицированный эндпоинт; оба — homepage-пробы с явным раскрытием в `not_proved`.

---

## 5. Открытые вопросы

1. **Где хранить снимок каталога в ревизии профиля** — внутри `profiles.config` (меняет `profile_id` при каждом обновлении) или отдельной колонкой выше миграции 14. Нужен выбор владельца `profiles.py` + `db.py`; я реализовал оба варианта на своей стороне (`PinnedSet.to_dict()`/`from_dict()`), выбор только за схемой.
2. **Кто показывает diff обновления пользователю.** `preview_update()` уже отдаёт построчный diff (`PresetChange.changed_fields` с old/new), но место показа — это `ui/app.js` и `gui.py`, то есть не мой файл. Прошу назначить: карточка «доступны обновления определений» рядом со списком сервисов, кнопка «применить» вызывает `upgrade_set`.
3. **Судья анонимности в наборах.** Все семь наборов декларируют `anonymity.judge_url: null` и `min_anonymity: 'any'`, потому что ни один каталог не содержит подтверждённого публичного judge. Если владелец `reputation.py`/`probes.py` добавит в каталог сервис для собственного judge, это будет обычный preset категории `basic` — отдельного механизма не нужно.
4. **Конфликт категорий и наборов.** Сейчас id категории и id набора совпадают (`basic`, `search`, …), потому что так их назвали в MASTER-PROMPT. Если владелец GUI захочет, чтобы «видео» было и категорией, и набором с другим id, структура это позволяет (категории и наборы — независимые таблицы), менять манифест не потребуется.
