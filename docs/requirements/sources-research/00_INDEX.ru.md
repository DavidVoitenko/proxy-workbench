# Указатель материалов исследования

**Папка:** `/Users/main/Desktop/111/proxy-sources-research-2026-09-25/`
**Срез проверки источников:** 2026-09-25T12:23:36Z – 12:24:52Z (UTC)
**Проект не изменялся.** Всё лежит в этой отдельной папке; `/Users/main/Desktop/111/proxy-workbench` не редактировался.

## Главное, что нужно понять до чтения каталога

| Статус | Подтверждён этим исследованием |
| --- | --- |
| Найден по документации | да, для всех 150 записей |
| URL доступен | да, 140 из 150 |
| Формат подтверждён | да, по телу ответа |
| Данные выглядят обновляемыми | косвенно, по ETag / Last-Modified |
| **Содержащиеся прокси проверены** | **НЕТ, ни для одного адреса** |

Соединения через найденные прокси не устанавливались, аккаунты не создавались, оплата не производилась.
Надпись `verified` или `checked` в имени файла или на сайте источника — это утверждение поставщика, а не доказательство.

## Запрошенные результаты и где они лежат

| № | Что требовалось | Файл |
| --- | --- | --- |
| 1 | Краткая русскоязычная сводка главных находок | [01_SUMMARY.ru.md](01_SUMMARY.ru.md) |
| 2 | Полный каталог в JSON и читаемой Markdown-таблице | [catalog.json](catalog.json), [catalog.md](catalog.md) |
| 3 | Журнал проверки с датами, доказательствами и ограничениями | [verification-log.md](verification-log.md), сырьё — [verification.json](verification.json) |
| 4 | Карта охвата поиска: что исследовано, что осталось | [04_COVERAGE_MAP.ru.md](04_COVERAGE_MAP.ru.md) |
| 5 | Группы зеркал, дубликатов и связи первоисточников | [overlap.md](overlap.md), сырьё — [overlap.json](overlap.json) |
| 6 | План подключения источников и необходимых адаптеров (разделы A–F) | [notes/integration-plan.ru.md](notes/integration-plan.ru.md) |
| 7 | Приоритизированный список функциональных идей | [notes/functional-ideas.ru.md](notes/functional-ideas.ru.md) |
| 8 | Готовый промпт для будущего Ultra Workflow разработки | [08_NEXT_WORKFLOW_PROMPT.ru.md](08_NEXT_WORKFLOW_PROMPT.ru.md) |
| — | Дополнительно: измеренная совместимость форматов с текущим парсером | [compat-report.md](compat-report.md) |

## Как читать

1. [01_SUMMARY.ru.md](01_SUMMARY.ru.md) — выводы и пять статусов. Начните отсюда.
2. [catalog.md](catalog.md) — каталог. Столбец «Совместимость» содержит результат исторического AST-измерения; это не полный путь `collect()`. Для текущей рабочей копии его дополняет локальный интеграционный тест `../proxy-workbench-sources/tests/test_research_collect_integration.py`, который вызывает настоящий `collect()` через mock HTTP-сервер и сохранённые образцы.
3. [verification-log.md](verification-log.md) — доказательная база: коды, редиректы, заголовки, время, ограничения прохода.
4. [overlap.md](overlap.md) — какие URL отдают одни и те же данные. Читать до любых выводов о ценности источника.
5. [notes/integration-plan.ru.md](notes/integration-plan.ru.md) и [notes/functional-ideas.ru.md](notes/functional-ideas.ru.md) — что с этим делать дальше.

## Как это проверялось

Все числа получены воспроизводимыми скриптами из `tools/`, а не пересказом:

| Скрипт | Что делает | Как запустить |
| --- | --- | --- |
| `verify_sources.py` | ограниченный HTTP GET к публичным файлам списков | `python3 tools/verify_sources.py candidates.json verification.json samples` |
| `analyze_overlap.py` | сравнение множеств `ip:port` по скачанным телам | `python3 tools/analyze_overlap.py verification.json overlap.json overlap.md samples` |
| `render_verification_log.py` | журнал проверки без ручных правок | `python3 tools/render_verification_log.py verification.json verification-log.md` |
| `probe_collector_compat.py` | историческое AST-извлечение функций; не полный путь `collect()` | `python3 tools/probe_collector_compat.py ../proxy-workbench-sources/proxy_workbench/proxytool.py verification.json samples compat-report.json compat-report.md` |
| `reconcile_compat.py` | вносит измерение в каталог и показывает расхождения | `python3 tools/reconcile_compat.py catalog.json compat-report.json catalog.json` |
| `fix_compat_overclaims.py` | понижает завышенную совместимость | `python3 tools/fix_compat_overclaims.py catalog.json` |
| `sync_catalog_md.py` | синхронизирует Markdown-таблицу с JSON | `python3 tools/sync_catalog_md.py catalog.json catalog.md` |
| `check_catalog.py` | **ворота**: каталог не должен утверждать больше, чем показала проверка | `python3 tools/check_catalog.py catalog.json candidates.json verification.json` |

`check_catalog.py` — главная гарантия: он сверяет каждое поле проверки с исходным отчётом,
запрещает утверждать «работает как есть» при нулевом приёме строк, запрещает
`proxies_actually_tested=true` и ищет утечку ключей. Текущий результат: **150 записей, 0 замечаний**.

## Ограничения, которые нельзя потерять при переносе выводов

- Сделан **один срез**. О суждении о долгожичучести источника или выживаемости адресов речи быть не может.
- Ограниченная или частичная загрузка означает «проверено частично», а не «источник не работает».
- Публичный GitHub API во время поиска упёрся в лимит запросов, поэтому часть находок (в том числе
  `Thordata/awesome-free-proxy-list`) не вошла в каталог — это пробел, а не отсутствие источника.
- Размер списка и число звёзд репозитория не являются основанием для приоритета.
- Лицензия кода репозитория не равна лицензии публикуемых им данных.
