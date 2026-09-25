# Матрица возможностей и пробелов

**Proxy Workbench уже умеет собирать и проверять прокси, выбирать страны и сервисы, ранжировать, экспортировать, выдавать API и подключать приложения через gateway.** Главные функциональные пробелы — изоляция собственных списков, credentials/hostname, управляемые профили и задания, объяснимая свежесть и настоящее поддержание пула.

Это статическое чтение кода от 25 сентября 2026, **не runtime-проверка**. Приложение, тесты, реальные БД, сетевые и прокси-проверки не запускались. Репозиторий не менялся исследователями. Основа — HEAD `e4bf03cbec4dc478b6c82984726456149e30bb42`; параллельные изменения GUI/UI помечены **WIP**. Ссылки baseline относятся к этой ревизии; у изменяемых файлов ориентироваться также на символ, поскольку строки сдвигаются.

Подробные факты и оговорки: [аудит функций, импортов и данных](evidence/audit-code.md), [аудит gateway, API, desktop и восстановления](evidence/systems-research.md). Предлагаемое будущее устройство продукта отдельно описано в [концепции](01-product-model.ru.md); оно не выдаётся за готовую реализацию.

## Значение статусов

| Статус пользователя | Как используется здесь |
|---|---|
| Уже работает | Прослеживается реализация пользовательского пути; в этом исследовании не запускался. |
| Реализовано частично | Механизм есть, но закрывает только часть обещания/сценария. |
| Существует, но неудобно | Результат достижим, но требует знания внутренних деталей или ручных действий. |
| Отсутствует | В исследованной модели и пользовательском пути механизма нет. |
| Возможно меняется текущим workflow | В рабочем дереве видны изменения; их завершённость не подтверждена. |
| Требует уточнения по актуальному коду | Есть неоднозначность контракта, несогласованность или статический риск; нужна прицельная перепроверка после WIP. |

Ни один из первых трёх статусов не означает, что публичный источник доступен, proxy действительно пригоден сегодня или установленная сборка проверена на Windows/macOS.

## Сводка по всем направлениям

В запросе перечислены разделы А–П. Дополнительная 16-я строка выделяет сквозную согласованность данных и восстановления.

| Направление | Что уже есть | Статус и реальный пробел | Доказательства: file:line и символ |
|---|---|---|---|
| **А. Режимы проверки** | `collect`, `scan`, `run`, export; HTTP target по умолчанию без ввода URL; повторная проверка; TCP-prefilter. | **Уже работает / частично.** Нет самостоятельных режимов порт/protocol handshake с отдельным смыслом результата; basic-проверка не названа понятным пользовательским режимом. Улучшение — сделать контракт явным, сохранив существующий движок. | `proxy_workbench/proxytool.py:757` (`target_config`), `:1073` (`reachable`), `:1587` (`parser`), `:1998` (`main`); `gui.py:78` (`defaults`) |
| **Б. География и характеристики** | Несколько ISO-стран, offline GeoIP, ASN/provider, hosting heuristic, endpoint и observed exit country. | **Уже работает / меняется.** WIP добавляет поиск названий RU/EN, chips и региональные наборы. **Отсутствуют** exit-country selection, исключения, квоты и явная unknown policy. Hosting heuristic не доказывает residential/mobile. | `proxy_workbench/geoip.py:31` (`parse_countries`), `:140` (`HOSTING`); `proxytool.py:1313`, `:1325`, `:1382`; **WIP** `ui/app.js:1443` (`SmartCountryCombobox`) |
| **В. Каталог сервисов** | Девять presets; до 20 GUI targets; all-condition. WIP добавляет quick chips. | **Частично / меняется.** Это конкретные GET/HEAD пробы. **Отсутствуют** версии contracts, категории/поиск, свои наборы, required/optional, any/K, проверки WebSocket/media/long connection. | `proxy_workbench/ui/app.js:905` (`TARGET_PRESETS`, baseline); `gui.py:144` (`validate`); `proxytool.py:913` (`summarize`); **WIP** `ui/app.js:1331` (`fillPresets`) |
| **Г. Параметры проверки** | Attempts, общий min-success для каждого target, whole-request/connect timeout, fail-fast, status/substring/hash, safe headers, TLS verify, response limit, простые presets. | **Уже работает / частично.** Нет независимых thresholds/таймаутов на target, retry intervals, JSON assertions, POST/body/auth target, redirects/DNS policy. Новые advanced-параметры должны жить в общем profile contract. | `proxy_workbench/proxytool.py:796` (`validate_targets`), `:872` (`request_timeout`), `:944` (`allowed_failures`); `ui/app.js:1325` (`presets`, baseline) |
| **Д. Скорость работы** | Bounded workers/queues, rate, file-descriptor fitting, предфильтр TCP→HTTP, приоритет известных успешных, find-N. | **Уже работает / частично.** Collect→scan последовательны; нет pipeline первых проверок во время загрузки, budgets и адаптации по target/сети. Нужна оптимизация времени до пригодного результата, а не ещё один workers slider. | `proxy_workbench/proxytool.py:1046` (`fit_workers`), `:1155`, `:1175`, `:1212` (`scan`), `:2002`, `:2011` (`main`) |
| **Е. Источники** | Каталог, свои URL, несколько форматов, pagination/retry/лимиты, report, reset/update/prune, source attribution и overlaps в БД. | **Уже работает / частично / требует уточнения.** Update добавляет записи из upstream каталога, не ищет интернет. Source quality зависит от первого источника и фильтров; нет нейтрального marginal contribution, timestamps/cache policy и mirror groups. Prune может спутать неподходящий профиль с плохим источником. | `proxy_workbench/proxytool.py:485` (`collect`), `:526` (`add`), `:1333` (`listed_counts`), `:1459` (`export`); `gui.py:251`, `:262` (`prune_sources`, `update_sources`) |
| **Ж. Импорт и свои прокси** | Файлы CLI, вставка/TXT GUI, нормализация, endpoint-dedup, public IPv4/IPv6, detect-protocols. | **Частично / неудобно.** Auth, hostname и private endpoints **отсутствуют** в основном импорте. Нет preview с причинами ошибок, secret store, subscriptions как сущностей, merge/replace коллекции. Отдельная `--data` нужна для изоляции своего файла от старой базы. | `proxy_workbench/proxytool.py:310` (`normalize`), `:542` (`collect`), `:1606` (`parser`); `gui.py:234` (`save`), `:312` (`start`) |
| **З. Организация результатов** | Filters/search/sort, pagination, copy page, detailed attempts, rich exports. | **Уже работает / меняется.** В WIP уже есть handlers row/bulk selection, copy/download selected, quick test; bulk denylist пока несовместим по payload/response. **Отсутствуют** named collections, membership, favorites/tags/notes, saved filters и история изменений списка. Не сводить эту задачу к новой таблице. | `proxy_workbench/gui.py:469` (`results`), `:567` (`detail`); `proxytool.py:416` (`open_db`); **WIP** `ui/app.js:2695`, `:3438`, `:3452`, `:3473`; `gui.py:608` (`add_denylist`) |
| **И. Качество и объяснимость** | Latency/jitter/success, aggregate survival, checked_at, speedtest, reputation/judge, recommended score, samples ошибок. | **Частично.** Нет freshness TTL, многомерной evidence-карточки, временного ряда и health target/network; failed endpoints скрыты обычной таблицей. Рекомендация «редкость = меньше загруженность» — гипотеза. | `proxy_workbench/proxytool.py:913`, `:928`, `:979`, `:1341`; `gui.py:522` (`results`); `reputation.py:249`; `anonymity.py:99` |
| **К. Постоянные пулы** | Watch и recheck-passing; gateway cooldown для отказов соединения. | **Частично.** Поддержание N, резерв, refill, re-entry, quotas/budgets и depleted-state controller **отсутствуют**. Watch перепроверяет только выживших и может навсегда сжаться до нуля. | `proxy_workbench/proxytool.py:1135` (`scan`), `:2051` (`main`); `tests/test_freshness.py:51`; `gateway.py:122` (`Pool.failed`) |
| **Л. Gateway и приложения** | Local HTTP/SOCKS5 gateway, rotation, endpoint sticky sessions, connection caps, per-client фильтры, cooldown, status, PAC/Clash/sing-box и Telegram recipe. | **Уже работает / частично.** Нет стабильного named pool binding и полноценного мастера проверки подключения. Sticky не гарантирует exit IP; TCP gateway не переносит UDP/активный поток. Пустые экспорты имеют разный direct fallback. | `proxy_workbench/gateway.py:39` (`Pool`), `:87` (`pick`), `:139` (`client_options`), `:280` (`Gateway.connect`), `:424` (`handle_socks5`); `formats.py:19`, `:36`, `:63` |
| **М. GUI, CLI, API и автоматизация** | GUI управляет одним worker; CLI scan/export/get/test; public read-only API latest export с фильтрами и token для non-loopback. | **Уже работает / частично.** GUI internal HTTP actions не равны стабильному публичному job API. Нет job queue/pause/event subscriptions, scoped tokens, pool/profile scope и cursor pagination; `test` не проводит полный screen pipeline. | `proxy_workbench/gui.py:295` (`start`); `proxytool.py:1815`, `:1838`; `api.py:108`, `:138`, `:158`, `:202` |
| **Н. Desktop Windows/macOS** | Browser GUI, launchers, pip installation, per-user/portable data paths, Windows exe build definition, повторное открытие своего экземпляра. | **Частично / неудобно.** Нативное окно, tray/menu bar, autostart, updater, safe migration/backup, supervisor, sleep/network recovery не обнаружены. Наличие packaging-кода не подтверждает установленный runtime и подпись релиза. | `proxy_workbench/paths.py:12` (`default_data`); `gui.py:730` (`main`, baseline); `packaging/proxy-workbench.spec:22`; `.github/workflows/release.yml:98` |
| **О. Дополнительные направления** | Рабочая основа для self-hosting: serve/gateway, Docker; экспорты внешним клиентам. Внешний mobile plan наблюдается отдельно как untracked документ. | **Частично / отсутствует.** Extension, безопасный remote management, mobile controller/client, TUN/VPN/cloud не реализованы в исследованном продукте. Non-loopback token ≠ TLS; мобильный документ не равен готовой функции. Большие расширения требуют самостоятельной проверки спроса и стоимости сопровождения. | `proxy_workbench/api.py:158`, `:185`, `:244`; `gateway.py:438`; `compose.yml:26`; `formats.py:63`; `docs/mobile-connectivity-plan.ru.md` (WIP plan) |
| **П. Open source и сопровождение** | README RU/EN, in-app help, i18n, config example, source catalog, contribution/privacy/security docs, local mock-test conventions; account/telemetry не требуются по архитектуре и политике. | **Уже работает / частично.** Нужны синхронизация документации с контрактами, безопасный diagnostic bundle, версии presets/source packs, guide миграций и community validation. Не добавлять обязательный аккаунт/скрытую telemetry ради метрик. | `proxy_workbench/i18n.py:12` (`detect`); `ui/app.js:21` (`messages`); `CONTRIBUTING.md:5`, `:34`; `PRIVACY.md:15`; `README.ru.md:290` (устаревшее утверждение) |
| **16. Сквозное: данные и восстановление** | SQLite WAL, profile fingerprint, checkpoints, cooperative stop/resume, export generations. | **Частично / требует уточнения.** Recheck удаляет старые rows до новых; history держит в памяти. API не читает generation manifest и может смешивать rows/status; очистка exports может не инвалидировать прогретый gateway. Нужны наблюдения и публикация как целостная операция. | `proxy_workbench/proxytool.py:416`, `:1135`, `:1185`, `:1540`, `:1713`; `api.py:87` (`Exports.load`); `gateway.py:62` (`Pool.refresh`) |

В таблице краткие `gui.py`, `proxytool.py`, `api.py`, `gateway.py`, `geoip.py`, `formats.py`, `ui/...` находятся внутри `proxy_workbench/`. Показанные пути/символы однозначно раскрыты в подробных evidence; отрицательные выводы относятся к исследованному коду, а не ко всем внешним приложениям.

## Самые важные границы поведения

| Если пользователь видит… | Корректная интерпретация сейчас |
|---|---|
| «Свой список» | Он добавляется в общие candidates этой data directory. Отключение источников не изолирует уже накопленные записи. |
| «Профиль» | Fingerprint конфигурации измерений; GUI не является библиотекой именованных проектов/профилей. |
| «Работает YouTube/Telegram/Discord» | Прошла конкретная HTTP-проба; media/MTProto/WebSocket/voice не доказаны. |
| «OpenAI API: успешно» | В текущем preset ожидается 401 с определённой строкой. Авторизованный успешный API-запрос не выполняется. |
| «Страна DE» | Страна endpoint по источнику/GeoIP. Наблюдённый выход может быть другим или неизвестным. |
| «Не hosting» | Не сработала эвристика имени ASN либо данных не было; residential/mobile не подтверждены. |
| «Find 20» | Можно переиспользовать старые подходящие результаты без новых запросов; свежесть пока не ограничена TTL. |
| «Keep fresh» | Повторно измеряются прежние passing; отсутствует автоматическое пополнение до исходного числа. |
| «Uptime 10/10» | Успех в десяти наблюдениях, не 100% доступности времени и не независимая оценка всех адресов. |
| «Хороший источник» | Эвристическая оценка по первому источнику endpoint и текущим условиям экспорта. |
| «Закреплённая сессия» | Привязка к upstream endpoint для новых подключений с TTL; смена upstream/его выхода возможна. |
| «Экспорт пуст» | PAC закрывает путь, но пустые Clash/sing-box конфиги могут дать DIRECT. Это нуждается в явной общей политике. |

Основания: `proxy_workbench/proxytool.py:310`, `:1113`, `:1182`, `:1313`, `:1341`, `:1459`, `:2051`; `proxy_workbench/ui/app.js:905` (baseline); `proxy_workbench/gateway.py:87`; `proxy_workbench/formats.py:28`, `:48`, `:80`.

## Что проверить после текущего workflow

Не ждать завершения параллельной разработки для исследования, но начинать будущую реализацию с повторной сверки:

1. **Удобства уже меняются.** На последней сверке есть RU/EN `SmartCountryCombobox`, региональные наборы, quick service chips, copy row, drag-and-drop import, selection/select-all и реальные JS handlers copy/download selected и quick test. В bulk denylist frontend отправляет `entries` и ждёт массив `added`, а backend ждёт `proxies` и возвращает число `added` (`ui/app.js:3473–3480`, `gui.py:611`, `:626`). Поэтому оно пока «меняется», не подтверждено работающим. Quick test использует отдельный Google GET, не текущий профиль и не новое persisted измерение. Проверить законченный код прежде новой карточки picker/bulk actions; расширять существующий механизм.
2. **Единый basic-контракт.** GUI default требует 200 + Example Domain, CLI default — любые 2xx без substring. WIP test добавляет отдельный фиксированный GET-конфиг. Согласовать правило, не создать четвёртый вариант.
3. **История при recheck/stop.** Проверить preservation прежних observations и профиля с country subset. Статический порядок DELETE→commit→probe требует отдельного локального mock-сценария.
4. **Геоданные одного run.** `country_resolver` создаётся до collect; новые Geonode countries могут не использоваться в первом проходе без DB-IP. Проверить именно composition `main`, не только отдельно resolver.
5. **Источник против фильтра.** Не удалять хороший источник только потому, что его результаты не подошли текущей стране/профилю/анонимности. Проверить source quality denominator и pruning context.
6. **Публикация.** API/gateway/download должны ссылаться на одно generation, корректно инвалидироваться после clear и различать fresh/stale/corrupt/missing.

Это будущие критерии верификации, не выполненные тесты. Результаты текущих незавершённых UI-изменений не принимались автоматически за работающий продукт.

## Вывод для выбора релиза

Следующий функциональный шаг должен расширить существующее ядро: **списки с явной областью → воспроизводимые профили и задания → наблюдения со свежестью и объяснением → поддерживаемый пул → стабильное подключение потребителя**. Auth/hostname/secret storage — отдельная сквозная возможность, а не снятие одного запрета в parser. Подробный принятый объём и зависимости находятся в [карточках функций](04-feature-cards.ru.md).

Числа источников, доступность presets, качество публичных прокси, реальное время до N, installed desktop behavior и спрос разных групп пользователей этим статическим аудитом не измерялись. Их нельзя превращать в гарантии следующего релиза.
