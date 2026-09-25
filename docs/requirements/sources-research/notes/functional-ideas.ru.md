# Функциональные идеи Proxy Workbench: источники, импорт и рабочий результат

**Срез:** 2026-09-25. **Назначение документа:** выбрать следующие законченные пользовательские результаты, а не объявить новые источники или прокси рабочими.

## 0. Рамки и доказательная база

В исследовании разделены пять статусов: «найден по документации», «URL доступен», «формат подтверждён», «данные выглядят обновляемыми» и «содержащиеся прокси проверены». В срезe последний статус имеет значение **нет ни для одного источника**: выполнялись только GET публичных файлов, ни один найденный адрес не использовался для соединения (`verification-log.md:2-8,14-24,242-249`). Поэтому идеи ниже улучшают intake, parsing, обновление и доказательность, но не обещают качество адресов.

В текущем проекте 55 URL на 8 доменов (`proxy_workbench/sources.json:1-55`; локальный подсчёт ниже). Исследовательский каталог содержит 150 записей, из них 55 уже используются, 25 требуют адаптера, 7 помечены как зеркала/копии, 15 требуют аккаунт, а 10 относятся к собственной инфраструктуре (сводный локальный подсчёт `candidates.json` приведён ниже). Числа не означают 150 новых рабочих пулов.

Ссылки на код ниже используют путь от репозитория проекта: `proxy_workbench/...`. Ссылки на исследование — путь от каталога `proxy-sources-research-2026-09-25/`.

### Что уже выполнено в этой сессии

1. Команда из задания была запущена буквально:

   ```text
   python3 -c 'import json; p="/Users/main/Desktop/111/proxy-sources-research-2026-09-25/verification.json"; r=json.load(open(p))["results"]; print("dead",[...]); print("no_address_regex",[...]); print("validators",[...]); print("redirects",[...])'
   ```

   Она **не прошла**: `verification.json["results"]` — список, а не словарь; фактический результат: `TypeError: list indices must be integers or slices, not dict`.

2. После учёта фактической схемы выполнено локальное перепроверяющее чтение (без сети):

   ```text
   python3 -c 'import json; p="/Users/main/Desktop/111/proxy-sources-research-2026-09-25/verification.json"; r=json.load(open(p))["results"]; print("no_nonempty_body",[x["id"] for x in r if not x.get("reachable")]); print("no_address_regex",[x["id"] for x in r if x.get("reachable") and not x.get("ipv4_port_matches") and not x.get("ipv6_port_matches")]); print("validators",sum(x.get("reachable") and bool(x.get("etag") or x.get("last_modified")) for x in r)); print("redirects",[x["id"] for x in r if x.get("redirects")])'
   ```

   Вывод:

   ```text
   no_nonempty_body ['cur-37', 'new-017', 'new-018', 'new-019', 'new-020', 'new-037', 'new-053', 'new-062', 'new-082', 'new-083']
   no_address_regex ['new-001', 'new-002', 'new-003', 'new-005', 'new-007', 'new-010', 'new-015', 'new-027', 'new-028', 'new-050', 'new-052', 'new-059', 'new-086', 'new-088', 'new-089', 'new-091', 'new-094', 'new-095']
   validators 121
   redirects ['cur-53', 'cur-54', 'new-013', 'new-025', 'new-026', 'new-052', 'new-067']
   ```

   `121` считается только среди записей с непустым телом. Объединение ETag/Last-Modified по всем 150 записям даёт 123: validator есть и у `cur-37` с HTTP 200/пустым телом, и у `new-018` с HTTP 404. Это важно для идеи условного обновления: validator не равен доступности или свежести; `cur-37` — пусто на момент проверки 2026-09-25T12:23:58Z, а не доказанно мёртвый URL.

3. Для каталога кандидатов выполнено:

   ```text
   python3 -c 'import json,collections; p="/Users/main/Desktop/111/proxy-sources-research-2026-09-25/candidates.json"; r=json.load(open(p)); print("records",len(r)); print("relation_to_current",dict(collections.Counter(x.get("relation_to_current") for x in r))); print("access",dict(collections.Counter(x.get("access") for x in r))); print("account_required",dict(collections.Counter(x.get("account_required") for x in r))); print("credentials_included",dict(collections.Counter(x.get("credentials_included") for x in r))); print("kinds",dict(collections.Counter((x.get("kind") or "").split("-")[0] for x in r))); print("families",len({x.get("family_id") for x in r})); print("domains",len({__import__("urllib.parse",fromlist=["urlsplit"]).urlsplit(x.get("url","")).hostname for x in r if x.get("url")}))'
   ```

   Вывод:

   ```text
   records 150
   relation_to_current {'already_used': 55, 'needs_adapter': 25, 'new_source': 37, 'rejected': 15, 'mirror_or_copy': 7, 'extra_format_of_existing': 11}
   access {'fully_public_free': 118, 'permanent_free_quota': 2, 'temporary_trial': 3, 'paid': 4, 'unknown': 11, 'free_with_api_key': 1, 'own_infrastructure': 10, 'dead-end': 1}
   account_required {False: 135, True: 15}
   credentials_included {False: 150}
   kinds {'txt': 54, 'html': 9, 'public': 12, 'commercial': 8, 'github': 18, 'country': 6, 'dead': 8, 'structured': 13, 'gitlab': 3, 'aggregator': 2, 'protocol': 3, 'self': 8, 'mirror_or_copy': 1, 'subscription': 5}
   families 128
   domains 56
   ```

4. Для текущего каталога выполнено:

   ```text
   python3 -c 'import json,collections,urllib.parse; p="/Users/main/Desktop/111/proxy-workbench/proxy_workbench/sources.json"; r=json.load(open(p)); kinds=collections.Counter(x.split(" ",1)[0] if " " in x else "http" for x in r); domains=collections.Counter(urllib.parse.urlsplit(x.split(" ",1)[-1]).hostname for x in r); print("urls",len(r)); print("kinds",dict(kinds)); print("domains",len(domains),dict(domains))'
   ```

   Вывод:

   ```text
   urls 55
   kinds {'http': 38, 'http-fields': 2, 'socks5': 6, 'socks4': 5, 'text': 3, 'geonode': 1}
   domains 8 {'raw.githubusercontent.com': 43, 'proxyspace.pro': 1, 'api.proxyscrape.com': 5, 'cdn.jsdelivr.net': 2, 'free-proxy-list.net': 1, 'www.sslproxies.org': 1, 'www.us-proxy.org': 1, 'proxylist.geonode.com': 1}
   ```

Приложение, тесты, GUI, реальные прокси и новые сетевые запросы в этой сессии не запускались. Все критерии приёмки ниже — будущие проверки, а не уже пройденные тесты.

## 1. Сводная таблица идей

| ID | Один законченный результат | Кому прежде всего | Горизонт / приоритет | Сложность | Ключевое основание |
|---|---|---|---|---|---|
| **S0** | Каталог источников с ролями и явным opt-in: пользователь находит кандидата, понимает, что это за объект, и не включает его автоматически | Сопровождающий источников, power user | Ближайшая полезная правка / P0 | M | 150 записей, 25 `needs_adapter`, 15 аккаунтных, 7 зеркал; HTML/config дают ложные совпадения |
| **S1** | Источник обновляется условно, хранит last-good и не превращает сетевую ошибку, пустой ответ или `[]` в пустой пул | Владелец каталога и пользователь регулярного refresh | Ближайшая полезная правка / P0 | M–L | 121 непустой ответ с validator, 10 записей без непустого тела (включая `cur-37` с HTTP 200/0 байт), отдельно `new-086` с HTTP 200/`[]`, 7 redirect; текущий GET не хранит ETag/Last-Modified |
| **S2** | Импорт одного понятного формата завершается preview → commit: CSV/TSV, JSON-записи и фиксированная HTML-таблица видны без тихой потери полей | Любой пользователь списков и автор источника | Ближайшая полезная правка / P0 | M–L | 25 `needs_adapter`; текущий JSON понимает только GeoNode, `text` — loose regex |
| **S3** | Фильтр по стране/протоколу/метаданным различает утверждение источника и наше наблюдение; `unknown` не становится ложным | Пользователь, выбирающий GeoIP/API/exit | Ближайшая полезная правка / P1 | M–L | country-суффиксы теряются; 43 тела имеют record-level metadata, 67 — только лексические слова |
| **S4** | Семейство пересечений показывает primary/fallback и измеренный marginal contribution, не удаляя источник по одному нулевому экспорту | Сопровождающий и пользователь, ограничивающий трафик | Ближайшая полезная правка / P1 | M | 59 семейств в overlap-отчёте; `cur-41`/`cur-43` имеют Jaccard 1.0 в срезе, что не доказывает происхождение |
| **S5** | Первые `N` пригодных результатов появляются во время bounded collect→scan, без ожидания EOF | Все, кто сканирует большие списки | Ближайшая полезная правка / P1 | L | Списки до hundreds of thousands endpoints; текущий `run` завершает collect до scan |
| **S6** | Пользователь видит отдельные доказательства source/TCP/HTTP/service и не получает ложный «рабочий» статус | Все; особенно новичок и automation | Ближайшая полезная правка / P1 | M | Исследование не проверило ни один proxy; текущие `reachable` и `check_proxy` — разные стадии |
| **S7** | Один snapshot generation выдаёт совместимый пакет для gateway/PAC/Clash/sing-box/proxychains/Telegram с причинами пропуска | Пользователь, подключающий приложение | Ближайшая полезная правка / P1 | M | Экспорт уже существует, но `formats.py` молча ограничивает протоколы и может выдать DIRECT при пустом наборе |
| **X1** | Один opt-in адаптер приватного HTTPS-feed/поставщика с secret-ref, квотой и last-good; без signup и покупки | Владелец Webshare/собственного провайдера | Дорогой эксперимент / P2 | L–XL | 15 аккаунтных записей; Webshare — 10 IP/1 GB free, но API free-tier не подтверждён; auth не поддерживается |
| **X2** | Один именованный пул поддерживает desired `N`, reserve, refill и probation с явным дефицитом | Постоянно подключённое приложение | Дорогой эксперимент / P2 | XL | Текущий watch только перепроверяет passing; refill и контроллер desired-state отсутствуют |

## 2. Ближайшие полезные правки

### S0. Каталог источников с ролями и явным opt-in

**Кому нужен.** Сопровождающему каталог и пользователю, который хочет добавить URL, но не хочет случайно включить коммерческую страницу, конфигурацию или зеркало.

**Пользовательский сценарий.** Открыть «Добавить источник», найти `new-008`, `new-047` или `new-089`, увидеть exact URL, формат, publisher/family, `already_used`/`needs_adapter`/`rejected`, access (`fully_public_free`, `account_required`, `paid`, `unknown`), наличие credentials в записи, дату доказательства, sample preview и статус лицензии. Пользователь выбирает роль `proxy-list` и явно добавляет источник в draft; preview использует сохранённый sample, а сетевой fetch запускается только после подтверждения.

**На чём основана идея.** В каталоге 150 записей и 128 `family_id`; 25 требуют адаптера, 15 требуют аккаунт, 7 — mirror/copy (`candidates.json`, локальный подсчёт выше). Исследование отдельно обнаружило, что Tor Onionoo — реестр реле, MTProto — конфигурация узла, Mihomo provider и sing-box/V2Fly — форматы конфигурации, а не универсальные HTTP/SOCKS-списки (`notes/discovery/subscription-formats.json:5-51,55-85,89-115,124-195`). В текущем проекте каталог — массив строк, а `update_sources` только добавляет новые строки (`proxy_workbench/sources.json:1-55`; `proxy_workbench/gui.py:263-285`).

**Что уже есть и что переиспользуется.** `source_spec()` уже проверяет kind и URL (`proxy_workbench/proxytool.py:471-482`), `source_key()` даёт стабильный короткий ID (`proxy_workbench/proxytool.py:438-440`), а GUI уже показывает источники и отчёты (`proxy_workbench/gui.py:443-462`). Добавляется версионируемый manifest, а не новый сетевой клиент или автоматический парсер.

**Минимальный законченный вариант.** Один `source-manifest.json` с полями `id`, `exact_url`, `publisher`, `family_id`, `relation_to_current`, `payload_role`, `adapter_kind`, `access`, `account_required`, `quota_text`, `license_status`, `evidence_checked_at`, `sample_ref`; фильтры в GUI/CLI; команда «включить» только для `payload_role=proxy-list`; `unknown` и `needs_adapter` видны и не превращаются в рабочий источник. Никаких автоматических регистраций, покупок или перепубликации IP.

**Зависимости и сложность.** Нужны версия manifest, безопасная идентичная URL-нормализация и разделение draft/enabled. Сложность **M**. Это не требует поддержки новых протоколов или изменения checker.

**Критерий приёмки (будущий тест).** Импорт 150 записей даёт 150 уникальных draft/source ID и ни одного автоматически включённого URL; запись `rejected`/`dead-end` не проходит gate; `account_required=true` не вызывает анонимный запрос; неизвестная лицензия видна как `unknown`; повторный импорт не создаёт дубль; выбор пользователя сохраняет точный URL и дату доказательства.

**Приоритет.** P0, ближайшая правка. Это inexpensive основа для S1–S4 и не обещает новые прокси.

### S1. Условное обновление источника, last-good и карантин

**Кому нужен.** Тому, кто обновляет 55 текущих URL или экспериментальные 150 URL и не хочет скачивать полный список при каждом запуске.

**Пользовательский сценарий.** Нажать «Обновить источник» или выполнить CLI refresh: Workbench отправляет ETag/Last-Modified, получает `304` и оставляет последнюю удачную выборку; при `200` сначала показывает новый hash/число строк и только затем применяет; при таймауте, битом JSON, redirects или превышении лимита показывает карантин и продолжает использовать last-good с явной меткой stale. Ручное «снять карантин» доступно после просмотра причины.

**На чём основана идея.** В срезе 121 запись с непустым телом имеет ETag/Last-Modified, ещё 10 не дали пригодного непустого ответа и 7 отвечают с redirect (`verification-log.md:28-35,60-85`). Среди десяти `cur-37` вернул HTTP 200 с пустым телом — это отдельный статус момента проверки, а не доказательство постоянной недоступности; `new-086` вернул JSON `[]`. Исследование специально предупреждает, что validator не доказывает свежесть: например, старые `Last-Modified` встречаются у `new-034`, `new-040`, `new-059`, `new-072`, `new-094` (материал задания; также `verification-log.md:242-249`). Текущий `_source_stream` делает только `client.stream('GET', url)` без headers (`../proxy-workbench-sources/proxy_workbench/proxytool.py:219-232`), а retry/redirect и redirect limits уже реализованы (`../proxy-workbench-sources/proxy_workbench/proxytool.py:654-698`).

**Что уже есть и что переиспользуется.** Переиспользуются `source_key`, существующие bounded budgets (32 MiB/64 KiB/500 000 по умолчанию и абсолютные потолки), отчёт `collect`, redirect policy и две retry-попытки с паузой (`proxy_workbench/proxytool.py:45-54,485-510,637-709`). В отличие от `prune_sources`, который смотрит только на неработающие записи в export-quality (`proxy_workbench/gui.py:252-261`), новый статус различает fetch, parse, redirect и отсутствие данных.

**Минимальный законченный вариант.** Хранить `etag`, `last_modified`, `final_url`, `last_fetch_at`, `last_success_at`, `last_body_sha256`, `last_parse_status`, `last_good_generation`, `quarantine_reason`; отправлять условный GET; `304` не меняет список кандидатов и не повышает свежесть proxy-проверок; невалидное тело не заменяет last-good; один source неудачно не удаляет family. Без общего API для произвольных заголовков и без auth.

**Зависимости и сложность.** Нужна миграция source manifest, кэш last-good и тестовый HTTP fixture с ETag/304/redirect/500. Сложность **M–L**.

**Критерий приёмки (будущий тест).** Последовательность `200 → 304` не меняет тело, candidate count и generation; битый JSON сохраняет прошлую выборку и показывает stale; downgrade/too-many/missing Location имеют разные причины; 18 no-address и 11 HTML/config false positives не превращаются автоматически в «мёртвый источник» без контекста; два недоступных источника с validators не считаются обновлёнными только из-за заголовка.

**Приоритет.** P0, ближайшая правка. Сначала стабилизирует жизненный цикл, затем S2 добавляет новые форматы.

### S2. Фиксированные адаптеры форматов и транзакционный preview импорта

**Кому нужен.** Пользователю, импортирующему CSV/TSV, JSON или HTML-таблицу, и сопровождающему, который хочет добавить источник без молчаливого преобразования полей.

**Пользовательский сценарий.** Вставить файл → выбрать `delimited`, `json-records` или `html-table` → увидеть первые строки, число accepted/invalid/duplicate, нормализованный protocol/IPv6/port и сохранённые `country`, `last_checked`, `anonymity`, `latency` → выбрать `merge` или `replace` только для выбранной коллекции → подтвердить одну транзакцию. Неподдерживаемый вложенный объект или JavaScript-выражение получает понятную причину, а не превращается в «нулевой адрес».

**На чём основана идея.** 25 записей каталога помечены `needs_adapter`. В реальных телах есть CSV без заголовка, JSON с вложенным ASN/geolocation, разные имена `lastChecked`/`last_checked`/`pingAt`, missing fields и отдельные HTML-таблицы (`notes/discovery/structured-exports.json:31-46,59-98,101-139,142-180,184-223,227-255`). В текущем коде `json.loads` вызывается только в ветке `kind == 'geonode'`, а `consume_record` жёстко читает `ip`, `port`, `protocols`, `country` (`proxy_workbench/proxytool.py:610-630,662-674`). `LOOSE_ADDRESS` умеет только пунктирный IPv4 и порт; country-suffix вроде `ip:port:country` отбрасывается (`proxy_workbench/proxytool.py:459-468,600-606`).

**Что уже есть и что переиспользуется.** Существующие `loose_addresses`, `normalize`, `detect-protocols`, `source reports`, локальный `--input`, bounded line reader и GeoNode pagination (`proxy_workbench/proxytool.py:256-307,466-468,542-557,610-630,688-739`). Не добавляется исполняемый plugin: только типизированные адаптеры и явная карта колонок.

**Минимальный законченный вариант.** Четыре фиксированных профиля: существующий `line`, `delimited` (CSV/TSV/semicolon с header/no-header map), `json-records` (flat record или явно заданные пути к `ip`/`host`, `port`, `protocol`, `country`, `last_checked`), `html-table` (фиксированные колонки). Для не-UTF-8 — явный выбор `cp1251`/`latin-1`, а не молчаливая замена. Preview не пишет БД; commit атомарен и выдаёт отчёт по строкам/записям. Base64, произвольный YAML, MTProto и конфигурации клиентов остаются `unsupported`, пока не будет отдельной доказанной схемы.

**Зависимости и сложность.** Нужны общий `ImportItem` с provenance, versioned schema и тестовые fixtures; HTML/CSV parser можно сделать без исполняемого кода. Сложность **M–L**.

**Критерий приёмки (будущий тест).** Фикстуры `cur-08`/`cur-32` сохраняют country; `new-008`/`new-009` показывают заявленные поля без превращения их в наши измерения; headerless CSV требует явного mapping; `new-011` с JavaScript-выражением получает `UNSUPPORTED_HTML_EXPRESSION`; cp1251 выбран явно и не даёт `SOURCE_INVALID_UTF8`; cancel/replace crash не оставляет половину membership; GUI и CLI возвращают одинаковые counts/reasons.

**Приоритет.** P0, ближайшая правка. Это самый прямой путь к 25 `needs_adapter`, но без обещания, что каждый из них одинаково полезен.

### S3. Provenance-aware metadata и честные географические фильтры

**Кому нужен.** Пользователю, которому нужен не просто адрес, а страна, протокол, анонимность, latency или ASN, и который не хочет выдавать claims источника за наши измерения.

**Пользовательский сценарий.** В результатах видно `source_declared.country`, `source_declared.anonymity`, `source_declared.last_checked`, `observed.exit_country`, `observed.latency` и provenance. Фильтр «Германия» может выбрать endpoint-country или измеренный exit-country; `unknown` показывается отдельно и не превращается в «не DE» без явной политики. Фильтр по `last_checked` показывает возраст заявления источника, а не age нашего proxy-check.

**На чём основана идея.** `cur-08` и `cur-32` теряют третье поле country; `new-008`, `new-009`, `new-011`, `new-012` содержат более богатые поля; в новом каталоге действительно найдено 43 тела с record-level metadata, а 67 — лишь лексические совпадения (материал задания; `verification-log.md:35,248`). Исследовательские записи показывают, что `last_checked` может быть ISO, Unix float или API-именем, а `exit_ip` не равен endpoint (`notes/discovery/structured-exports.json:142-180,184-223,227-255`). Текущий `candidate_meta` хранит в основном `country` и `source`, а результат уже имеет отдельные `country`/`exit_country`/ASN (`proxy_workbench/proxytool.py:416-435,512-530`; `proxy_workbench/api.py:47-73`).

**Что уже есть и что переиспользуется.** GeoIP, ASN, anonymity judge, country filter, protocol/latency filter и отображаемые exit fields уже есть (`proxy_workbench/proxytool.py:1382-1395`; `proxy_workbench/gui.py:470-562`). Не создаётся новый geo-поиск или новый источник истины.

**Минимальный законченный вариант.** Хранить `declared` и `observed` раздельно, с `source_id`, `field`, `observed_at`, `valid_until/unknown`; добавить include/exclude и выбор endpoint/exit; source metadata не перезаписывает результат нашей проверки. Фильтры работают одинаково в GUI, CLI и read-only API.

**Зависимости и сложность.** Нужны миграция metadata, политика unknown и конфликтующие значения; сложность **M–L**. Для exit-фильтра, который сам требует judge, нужен отдельный явный probe, а не скрытая проверка при чтении.

**Критерий приёмки (будущий тест).** Endpoint DE/exit NL выбирается по exit=NL и не по endpoint=NL; `unknown` не исчезает и не становится отрицательным DE; ISO/Unix timestamp нормализуется с сохранением исходного значения; старый `last_checked` помечается stale; отсутствующий ASN не превращается в hosting/residential; три интерфейса дают одинаковый filter result.

**Приоритет.** P1, ближайшая полезная правка после S2.

### S4. Семейства зеркал и marginal contribution

**Кому нужен.** Сопровождающему источниками и пользователю, который хочет меньше одинаковых запросов и понимает, действительно ли новый URL добавляет адреса.

**Пользовательский сценарий.** Открыть family `cur-41/cur-43`, увидеть 21 036 общих endpoint и Jaccard 1.0, выбрать primary и fallback. Family health показывается отдельно от вклада; переключение primary не удаляет наблюдения и не меняет уже опубликованный generation без явного обновления.

**На чём основана идея.** `overlap.md` описывает 59 семейств и метод set comparison (`overlap.md:2,4-16`); среди сильных пересечений есть exact/near duplicates `cur-41`/`cur-43` с Jaccard 1.0 и тысячами общих endpoint (`overlap.md:68-90`). Каталог уже различает 7 `mirror_or_copy`, а текущая БД хранит `candidate_seen` и первый источник (`proxy_workbench/proxytool.py:421-429,512-530`).

**Что уже есть и что переиспользуется.** `candidate_seen`, source reports, `source_key` и существующий export-quality считаются повторно, а не создаются с нуля. Нужна лишь family/mirror identity, один set-comparison и понятная политика выбора.

**Минимальный законченный вариант.** Один family manifest с `primary`, `fallback`, `mirror_of`, `observed_overlap`, `unique_contribution`; один refresh может использовать fallback, но не объединяет их как независимые источники. Один неуспешный запуск не удаляет family автоматически.

**Зависимости и сложность.** Нужна детерминированная identity endpoint (не только URL) и bounded set representation; сложность **M**.

**Критерий приёмки (будущий тест).** Exact mirror получает zero unique contribution; union не удваивает shared endpoints; перестановка A/B не меняет marginal counts; отказ primary показывает fallback, но не создаёт новую family; source quality и overlap не смешиваются.

**Приоритет.** P1, полезная следующая правка.

### S5. Bounded streaming «первые N» вместо ожидания EOF

**Кому нужен.** Всем, кто запускает большие списки и хочетпервые результатов без полной загрузки и без превышения ресурсных лимитов.

**Пользовательский сценарий.** Выбрать `N=5` и профиль; видетьпервые кандидатов/результаты, пока медленный источник ещё отдаёт тело; после достижения N планировать новые probes нельзя, уже начатые ограничены и показываются как `goal_reached`. Оставшийся ввод сохраняется для следующего запуска, а не теряется.

**На чём основана идея.** В overlap-отчёте есть источники с сотнями тысяч endpoint и полными наборами (`overlap.md:72-90`), а bounded collector допускает до 500 000 кандидатов и 32 MiB по умолчанию (`proxy_workbench/proxytool.py:45-54,485-497`). Текущий `run` сначала полностью завершает `collect`, затем запускает `scan` (`proxy_workbench/proxytool.py:1992-2011`), хотя `scan` уже имеет `want`, bounded queue и prefilter (`proxy_workbench/proxytool.py:1098-1105,1155-1164,1175-1211`).

**Что уже есть и что переиспользуется.** `want`, `Rate`, worker fitting, TCP prefilter, `unreachable_result`, progress и batch commits. Нужна только оркестрация adapter batch → bounded queue → scan и явная семантика незавершённых batches.

**Минимальный законченный вариант.** Один pipeline для одного запуска: source adapter отдаёт bounded batches, cheap TCP/HTTP probe идёт по очереди, результаты видны в GUI/CLI, а достижение N останавливает producer. Никаких обещаний процентного ускорения и никакого снятия лимитов.

**Зависимости и сложность.** Зависит от S2 для batch parsing, S3 для selection и существующей generation publication; сложность **L**.

**Критерий приёмки (будущий тест).** На локальном slow-source mock первый результат появляется до EOF; bounded memory/queue соблюдаются; после N новые probes не планируются, in-flight ограничены; отмена сохраняет pending input; 8 и 128 workers дают одинаковую критерию acceptance, а не одинаковое время.

**Приоритет.** P1, полезная следующая правка; измерять следует time-to-first и overshoot, а не только total workers.

### S6. Раздельные доказательства source / TCP / HTTP / service

**Кому нужен.** Новичку, который получает «0 результатов», и скрипту, который должен отличить недоступный URL от мёртвого прокси.

**Пользовательский сценарий.** В отчёте видны `source_available`, `format_parsed`, `tcp_reachable`, `http_transfer_ok`, `service_profile_ok`, `anonymity_observed`, `speed_observed`; каждая стадия имеет target/profile и timestamp. Статус «200 без адресов» остаётся `source_available`, а не `working`; TCP-only не попадает в basic working pool; provider words `verified/checked` остаются claims.

**На чём основана идея.** Исследование специально разделяет эти статусы и говорит, что ни один proxy endpoint не проверялся (`verification-log.md:14-24,242-245`). В коде уже существуют отдельные `reachable()` и `check_proxy()`/`summarize()` (`proxy_workbench/proxytool.py:877-925,954-976,1073-1083`), но пользовательский отчёт не обязан явно сохранять семантическую разницу.

**Что уже есть и что переиспользуется.** Текущий target profile, attempts, reliability, latency/jitter, anonymity и speedtest. Не добавляется новый «универсальный green score».

**Минимальный законченный вариант.** Один typed evidence object в result/report и единый label в GUI/CLI/API; aggregation разрешён только из явно выбранных probe capabilities. Не меняем старый target URL и не обещаем WS/media/voice.

**Зависимости и сложность.** Нужна общая схема результата и миграция payload; сложность **M**.

**Критерий приёмки (будущий тест).** 200/no-address, TCP-success/HTTP-fail и HTTP-success дают разные статусы; один и тот же profile даёт одинаковые labels во всех интерфейсах; отсутствие наблюдения — `unknown`, а не `0%` или `working`; quick test не получает более сильный статус, чем реально выполненный pipeline.

**Приоритет.** P1, ближайшая полезная правка; это контракт, который должен предшествовать автоматическому пулу.

### S7. Совместимый immutable-пакет для приложений

**Кому нужен.** Пользователю, который передаёт адреса браузеру, CLI, Clash, sing-box, proxychains или Telegram-клиенту.

**Пользовательский сценарий.** Выбрать одну generation и consumer → получить recipe, capability report и control test: какие строки поддержаны, какие отброшены и почему, какой протокол/тип адреса/credential requirement, как проверить route и как отключить его. В отчёте нет скрытого `DIRECT`; IPv6, hostname и unsupported protocol видны до копирования.

**На чём основана идея.** Экспорт уже создаёт `proxies.txt`, ranked JSON/CSV, protocol files, PAC, Clash, sing-box и proxychains (`proxy_workbench/proxytool.py:1471-1526`). Но `formats.py` ограничивает списки типами, а пустые Clash/sing-box конфиги используют `DIRECT` (`proxy_workbench/formats.py:5-9,18-32,35-56,62-84`). Исследование показало, что JSON/HTML/CSV несут IPv6, protocol и metadata, которые нельзя потерять между collector и consumer (`notes/discovery/structured-exports.json:31-46,101-139,227-255`).

**Что уже есть и что переиспользуется.** Текущие atomic generations, `current.json`, API filters, local gateway и Telegram button/recipe. Добавляется только manifest совместимости и общий read path.

**Минимальный законченный вариант.** `compatibility.json` с `generation`, `profile_digest`, `age`, `consumer`, `supported`, `unsupported_reasons`, `row_ids`; существующие форматы продолжают генерироваться, но несовместимые строки не исчезают молча. Один control test и инструкция disconnect — обязательная часть результата.

**Зависимости и сложность.** Зависит от S3 (metadata/provenance) и S6 (evidence); авторизованные hostname/credentials — только после отдельного secret-safe пути. Сложность **M**.

**Критерий приёмки (будущий тест).** API, gateway, PAC, Clash, sing-box и proxychains ссылаются на одну generation; IPv6 bracketing и socks4/https support показаны явно; пустой export даёт fail-closed diagnostic, а не DIRECT; отключение recipe не меняет чужие настройки; control test не выдаётся за проверку реального клиента.

**Приоритет.** P1, полезная следующая правка.

## 3. Дорогие экспериментальные направления

### X1. Один opt-in адаптер приватного feed/поставщика

**Кому нужен.** Пользователю Webshare или собственного провайдера, который готов сам получить token/credential и не хочет, чтобы Workbench регистрировался, покупал тариф или перепубликовывал IP.

**Пользовательский сценарий.** Создать private provider profile: HTTPS URL или один заранее выбранный endpoint, secret reference, ожидаемая схема, quota/refresh policy. Workbench один раз получает feed, показывает delta и last-known-good, затем обновляет по расписанию/ручной команде. Ошибки `401/403`, quota и истёкший token отличаются; исходный access остаётся private и не попадает в public export.

**На чём основана идея.** 15 из 150 записей требуют аккаунт, 10 — own infrastructure; Webshare заявляет permanent free tier на 10 proxies и 1 GB/month, но не подтверждает, что API и SOCKS5 входят именно в free tier (`notes/discovery/commercial-tiers.json:21-45`; материал задания). У Proxio есть противоречие 100 calls/day против 2 000 calls/day, а у Proxy11 неясен смысл `Free limit 50` (материал задания; `notes/discovery/commercial-tiers.json` и соответствующие записи каталога). Текущий collector не умеет Bearer/Basic/API key: URL с userinfo запрещён (`proxy_workbench/proxytool.py:113-131`), а stream GET не получает headers (`:220-230`).

**Что уже есть и что переиспользуется.** `source_spec`, bounded fetch, reports, atomic local data и текущий GUI/CLI source lifecycle. Не переиспользуются target headers как секретный канал: у них другой scope.

**Минимальный законченный вариант.** Один generic private-HTTPS-feed adapter с `secret_ref`, а не автоматическая поддержка семи вендоров; local mock 200/401/403/429/5xx, `304`, malformed body и last-good; никаких signup/payment/auto-conversion. Provider-specific adapter появляется только после выбора пользователя и подтверждённой схемы.

**Зависимости и сложность.** Нужны OS vault или session-only secret, безопасная передача секрета worker'у, rate/quota policy, private scope и Terms acknowledgement. Сложность **L–XL**.

**Критерий приёмки (будущий тест).** Секрет отсутствует в URL, argv, обычном JSON, логах и diagnostic bundle; неверный secret и quota не превращаются в пустой replace; malformed response оставляет last-good; feed не экспортируется в public scope без явного действия; ни один реальный provider token в этой сессии не использовался.

**Приоритет.** Дорогой эксперимент P2. Нельзя обещать бесплатный API Webshare или качество чужих прокси по материалам pricing/docs.

### X2. Контроллер maintained pool `N + reserve`

**Кому нужен.** Скрипту или постоянно подключённому приложению, которому нужен не «лучший текущий export», а контролируемое число допустимых upstream.

**Пользовательский сценарий.** Задать один именованный пул: `N=20`, reserve=5, профиль, freshness, страны/ASN quota и budget. При отказе checked member он уходит в cooldown/probation; refill берёт локальные pending/last-good кандидаты и только затем разрешённые источники. Если достигнут `3/5`, UI/API показывают дефицит и причину, не ослабляя страну и не подмешивая public в private.

**На чём основана идея.** В watcher сейчас после публикации выполняется только `recheck_passing=True` и повторная проверка выживших (`proxy_workbench/proxytool.py:2051-2058`); `want` умеет остановить поиск после N, но refill-контроллера нет. Gateway уже имеет rotation, cooldown и per-proxy limits (`proxy_workbench/gateway.py:38-68,81-135`), то есть это строительные блоки, а не готовый maintained pool. Исследование не подтверждает liveness ни одного адреса, поэтому сначала нужен локальный fault-injection prototype.

**Что уже есть и что переиспользуется.** SQLite candidates/results/profiles, `want`, bounded queues, progress, generation publication и gateway cooldown. Не обещается distributed scheduler или внешний orchestrator.

**Минимальный законченный вариант.** Один пул, один профиль, N+reserve, локальный refill из уже разрешённого scope, request/byte/concurrency budget, persisted state и zero/degraded status. Источники не расширяются автоматически в первой версии эксперимента.

**Зависимости и сложность.** Зависит от S1, S3, S5, S6 и durable job/checkpoint. Сложность **XL**.

**Критерий приёмки (будущий тест).** На mock candidates два отказа компенсируются до N; после cooldown восстановившийся member проходит probation; budget=0 останавливает refill; private pool не получает public; отсутствие кандидатов даёт явный `3/5`, а не DIRECT; kill/restart не дублирует observation и не воскрешает stale pass.

**Приоритет.** Дорогой эксперимент P2. Не включать в ближайший этап, пока не подтверждены scope, freshness и стоимость обслуживания.

## 4. Рекомендуемый набор следующего функционального этапа

### Этап S-A: «источник понятен, обновляется безопасно, импорт не теряет данные»

Порядок реализации:

1. **S0 — source manifest и intake gate.** Сначала дать каждой записи идентичность, роль и режим `draft/enabled`, не добавляя 150 URL в активную выдачу.
2. **S1 — conditional refresh + last-good.** После появления manifest хранить validators, redirect/parse status и карантин, не удаляя последнюю хорошую выборку.
3. **S2 — фиксированные адаптеры + preview/commit.** Сначала покрыть реально наблюдаемые CSV/TSV, JSON и HTML-таблицы, затем расширять только новыми схемами.
4. **S6 + S7 — evidence и единый consumer package.** Не позволять источнику, TCP, HTTP и пустому экспорту называться одним и тем же «рабочим» результатом.

**Почему именно так.** Эти четыре среза решают наиболее подтверждённые проблемы текущего среза: ложные HTML/config совпадения, отсутствие адаптеров, потерю metadata и смешение source health с proxy health. Они переиспользуют collector, bounded limits, отчёты, SQLite и generation export; не требуют новой сетевой инфраструктуры.

**Обязательные gate перед следующим этапом:**

- ни один кандидат не включается автоматически только потому, что URL вернул 2xx;
- `304`,Last-Modified, timestamp и marketing `verified` не повышают proxy pass;
- unsupported format, auth-required и unknown license видны как статусы;
- source failure, parse failure, target failure и zero filter имеют разные причины;
- preview/commit и generation manifest не теряют provenance и не смешивают поколения;
- критерии S0–S2 проверяются локальными fixtures. Они в этой сессии не запускались.

### Этап S-B: после стабильного intake

S3 (geo/provenance), S4 (семейства зеркал), S5 (first-N pipeline) и S7 (полный compatibility matrix) можно делать последовательно. S3 и S4 дают наибольшую пользу при большом числе источников; S5 — при больших телах и медленных fetch; S7 завершает пользовательский путь.

### Что сознательно не ставить в ближайшую очередь

- Автоматически включать все 150 кандидатов: 15 требуют аккаунт, 25 адаптера, 7 являются зеркалами, а proxy liveness не проверялась.
- Автоматически регистрироваться, покупать тарифы или обходить KYC/лимиты провайдеров.
- Считать `residential`, размер маркетингового пула или fixed exit IP доказанными: в исследовании этого нет.
- Делать ML-рейтинг без собранных observation labels: source score сейчас зависит от первого источника и фильтра экспорта (`proxy_workbench/proxytool.py:1431-1466`).
- Автоматически интерпретировать Base64, MTProto, WireGuard, Mihomo/sing-box-конфигурации как обычные `ip:port`: исследование показывает, что это разные сущности (`notes/discovery/subscription-formats.json:5-235`; `notes/discovery/self-hosted.json:175-206`).
- Заменять уже существующие gateway/PAC/Clash/sing-box/Telegram-интеграции новым сетевым стеком; нужен совместимый manifest вокруг них.

## 5. Покрытие направлений запроса

| Направление | Где закрывается |
|---|---|
| Поиск и подключение источников | S0 intake gate, S1 refresh, X1 private feed |
| Обновление и расход источников | S1 conditional/last-good, S4 families, S5 budgeted pipeline |
| Новые форматы импорта | S2 fixed adapters и preview |
| Бесплатные и собственные провайдеры | S0 access/terms labels, X1 opt-in adapter; без автоматического signup |
| Metadata и фильтры | S3 declared/observed и geo policy |
| Ускорение сбора/проверки | S5 first-N, существующие bounded workers/prefilter |
| Географический выбор | S3 endpoint/exit/unknown, country-aware S2 |
| Разные режимы проверки | S6 source/TCP/HTTP/service evidence |
| Рабочий пул | X2 desired N/reserve/refill |
| Удобство списков | S2 import diff и S4 family/mirror view |
| GUI/CLI/API/фон | S0–S2 имеют единый manifest; S1/S3/S6 — общий evidence contract; X2 — отдельный coordinator |
| Интеграции с приложениями | S7 immutable compatibility package; существующие gateway/formats переиспользуются |

## 6. Итог

Самая сильная следующая функция — не «ещё 50 URL», а **один проверяемый контур источника**: понятная роль → безопасное обновление → форматный preview → раздельное доказательство → один generation для потребителя. Он прямо отвечает на реальные 25 `needs_adapter`, 10 недоступных URL, 18 ответов без адресов, 11 HTML/config ложных совпадений, 7 redirect и 121 reachable validator, не превращая эти наблюдения в ложное обещание качества прокси.
