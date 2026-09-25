# Единый полный промпт завершения Proxy Workbench

Запусти полный многоэтапный **Ultra Workflow в одном чате** для завершения Proxy Workbench. Используй встроенную многоагентную оркестрацию этого режима. Не заменяй её несколькими пользовательскими чатами или просьбой ко мне вручную координировать исполнителей.

Нужна реализация всего принятого scope ниже: незавершённое из предыдущего цикла, подтверждённые дефекты, 28 принятых функциональных направлений продуктового исследования и новое полноценное управление через API с менеджером ключей.

Приоритеты определяют порядок, а не право молча исключить часть scope. Минимальный законченный срез — единица интеграции, не конечная подмена всей задачи. Не завершай workflow после одного удобного этапа. Продолжай до выполнения обязательных требований либо зафиксированного реального внешнего блокера, выполнив всё независимое от него.

## 0. Источники истины и сохранность

Основной проект в текущей локальной среде: `/Users/main/Desktop/111/proxy-workbench`.

Материалы, если доступны:

- `/Users/main/Desktop/111/proxy-workbench-product-research-2026-09-25/00-START-HERE.ru.md` и документы 01–08, особенно `04-feature-cards.ru.md`, `05-user-scenarios.ru.md`, `06-roadmap-and-release.ru.md`.
- `/Users/main/Desktop/111/proxy-workbench/data/post-workflow-review-2026-09-25/REVIEW.ru.md`: технические замечания R01–R20.
- `/Users/main/Desktop/111/proxy-workbench/data/audit-2026-09-25/AUDIT_AND_ROADMAP.ru.md`, `BACKLOG.ru.md`, `SOURCES.ru.md`: исходный аудит и 140 задач.
- `/Users/main/Desktop/111/proxy-sources-research-2026-09-25/`: каталог, verification, overlap, compatibility, план подключения и исправленные версии исследования источников.
- `/Users/main/Desktop/111/proxy-workbench-sources`: отдельная рабочая ветка источников, если она существует.

Если другой хост/путь — используй предоставленный checkout и приложенные документы; не выдумывай доступ к указанным локальным файлам. Встроенных требований этого промпта достаточно, чтобы начать независимую работу при отсутствии части материалов.

Сначала прочитай применимые AGENTS.md и актуальные исходники. Аудиты статические, состояние могло измениться. Для каждого важного замечания установи: подтверждено / уже исправлено / не подтвердилось / ещё требует проверки. Не воспроизводи автоматически старые выводы.

**Дизайн пользователь правил отдельно.** Не приписывай визуальные изменения предыдущему workflow. Текущие оформление, layout, темы, компоненты и визуальный язык — пользовательская база, которую надо сохранить. Добавляй необходимые новые controls в её стиле. Не запускай общий редизайн и не меняй её из эстетических предпочтений агентов.

Предыдущий workflow — источник технических изменений, а не доказательство завершения всех требований. Проверь его фактические diff/результаты. Не суди о качестве по числу строк, агентов или названиям этапов.

Я явно разрешаю рабочие ветки/worktrees, локальные коммиты результата, изолированные окружения, необходимые зависимости, локальные unit/integration/browser tests на временных данных и mocks, сборки и проверки поставляемых артефактов. Пользовательские базы и реальные credentials для проверки не использовать. Git identity — Anonymous <anonymous@example.invalid>; не менять глобальную identity и не коммитить личные пути/секреты.

Сохрани все исходные пользовательские изменения. Не используй reset/clean/stash для всей чужой рабочей копии. Создай интеграционную ветку от согласованного состояния; переноси только необходимые tracked/untracked изменения с проверкой diff. Ignored `data/` целиком не копировать: отдельные отчёты можно читать/копировать адресно.

Публикация релиза, push/merge в удалённый main, регистрация/оплата аккаунтов, включение системного proxy/автозапуска/сетевого доступа на моей машине в эту реализацию не входят. Соответствующие возможности программы реализуй и проверяй изолированно. Signing credentials не создавай и не покупай; при их отсутствии подготовь всё остальное и укажи конкретный release-blocker.

Ограниченное чтение официальной документации и публичных файлов источников разрешено с deadlines, byte/page budgets, небольшим per-host concurrency и соблюдением Retry-After. Массовые проверки найденных публичных proxy endpoint, DNSBL и сторонних сервисов не выполнять. Продуктовые функции доказывать локальными контролируемыми сценариями; качество живых внешних адресов не выдумывать.

## 1. Максимально полезная параллельность внутри Ultra Workflow

Построй граф из **36 специализированных ролей** ниже. Цель — десятки одновременно работающих агентов на независимых исследованиях, реализациях, fixtures и review, если runtime это поддерживает. Используй фактический максимум доступных слотов. Укажи реальную concurrency; не имитируй 36 одновременно работающих агентов при меньшем лимите. При ограничениях выполняй независимые роли волнами, не сокращая scope.

Количество ролей не самоцель: не создавать пустые задачи ради счётчика. После закрытия одной роли слот получает следующую готовую задачу. Агент не должен ждать зависимости, если у него есть полезная независимая работа по fixtures, contract tests или совместимости.

Роли и основная ответственность:

1. Сверка требований, coverage и фактического baseline.
2. Доменные контракты endpoint/access/collection/profile/job/pool.
3. SQLite, миграции, backup/restore и retention.
4. Freshness, observations и единый отбор пригодных результатов.
5. Job lifecycle, очередь, pause/resume/crash recovery.
6. Сбор/проверка конвейером, parallelism и ресурсные budgets.
7. Коллекции, membership и scope isolation.
8. Импорт/preview/merge/replace и работа с форматами.
9. Hostname/auth transports и DNS policy.
10. Secret store, access revision и безопасная смена credentials.
11. Профили, версии, all/any/K и обязательные проверки.
12. Каталог сервисов и поддерживаемые наборы.
13. Assertions, таймауты, redirects и расширенные параметры.
14. География, endpoint/exit, ASN, exclusions и unknown.
15. Judge, анонимность и DNSBL.
16. Bandwidth, WebSocket, media и sustained probes.
17. Стыковка завершённого sources workflow и обновлений каталога.
18. Source adapters/cache/provenance и жизненный цикл подписок.
19. Maintained pools, refill/reserve/cooldown и квоты.
20. Расписания, уведомления, батарея/сеть и учёт бюджета.
21. Gateway, sessions, transport parity, concurrency и shutdown.
22. Экспорты, совместимость внешних клиентов и immutable snapshots.
23. Публичное API, operations/jobs/events и OpenAPI.
24. API keys, permissions, resource scopes, rate limits и audit.
25. CLI parity, machine-readable output и examples.
26. GUI пользовательских сценариев в существующем дизайне.
27. Списки результатов, bulk actions, saved views и диагностика.
28. Подключение клиентов, recipes, QR, LAN и mobile control UX.
29. Windows packaging/lifecycle/update integration.
30. macOS packaging/lifecycle/update integration.
31. Headless/Docker/Linux совместимость и upgrades.
32. Документация, i18n, accessibility и OSS data packs.
33. Независимая проверка данных/миграций/согласованности.
34. Независимая проверка auth/секретов/границ доступа.
35. Независимая проверка реальных пользовательских сценариев и платформ.
36. Независимая проверка производительности, интеграции и полного scope.

Это роли внутри одного workflow, а не 36 новых чатов. Не назначай всем одновременно переписывать общие файлы. При необходимости раздели роль на конкретные bounded subtasks и используй подходящий слот.

### Обязательная координация на каждом этапе

- Единая версия контрактов: schema/model/API/events/error codes/units/freshness/unknown/capabilities.
- Таблица владения файлами и миграциями. У изменяемого общего файла один writer; другие передают контрактное изменение/узкий patch владельцу.
- `proxytool.py`, `gui.py`, `api.py`, общая БД и центральные UI-файлы нельзя редактировать конкурирующим набором исполнителей. Минимальное выделение реальных модулей допускается для устранения конфликтов, без массовой переписи.
- Для независимой разработки — отдельные worktrees от одной известной базовой ревизии. Само наличие worktrees не решает несовместимость изменений API/БД.
- Каждый handoff: requirement IDs, версия базы/контракта, изменённые файлы, совместимость, выполненные проверки, открытые вопросы.
- Изменение общего контракта обсуждается с непосредственными потребителями и отражается в их проверках до интеграции. Не нужен бесконечный общий совет всех агентов по каждому локальному изменению.
- Review выполняется по зафиксированному diff; автор своего участка не является единственным принимающим.
- Конфликты решаются по поведению, не автоматическим ours/theirs.
- Полный тяжёлый test suite выполняется централизованно на интегрированной ревизии. Исполнители запускают релевантные проверки своих участков, а не 36 одинаковых полных прогонов.

## 2. Scope и доказательства завершения

Создай одну таблицу трассировки: требование → current state → владелец → зависимости → implementation → проверка → статус. Свяжи:

- все 28 принятых карточек продуктового исследования;
- все 20 замечаний последующей сверки;
- исходные 40 замечаний и 140 задач, объединив дубли без потери требований;
- отдельную обязательную функцию F29: управление программой через API и ключи.

Статусы: `todo`, `in_progress`, `implemented_unverified`, `verified`, `external_blocker`, `not_applicable_with_evidence`. «Отложили» не означает `verified`. Если старое требование больше не применимо, укажи конкретное доказательство и эквивалентный закрытый сценарий.

F01–F29 ниже обязательны в указанном законченном объёме. Разделение на релизы/волны не отменяет оставшуюся работу. Исследовательские X01–X08 и отвергнутые R-идеи из мозгового штурма учитываются отдельной матрицей; они не превращаются автоматически в обязательство написать новый VPN/облако или купить сервисы. Remote management и self-hosted probes имеют обязательные части в F29/F20. Остальные X — конкретная оценка/обоснованный ограниченный прототип при необходимости, без фиктивного «сделано».

## 3. Исправить незавершённое и сломанное

Подтверди по актуальному коду и закрой следующие дефекты. Если источники уже исправили часть — интегрируй и проверь, не реализуй заново.

1. Срок годности должен проходить scanner → storage → GUI/API/export/gateway; отсутствие `valid_until` у новых строк не означает вечную свежесть.
2. Сканер повторно проверяет истёкшие результаты. Переэкспорт и смена watch не создают новое время измерения.
3. Истечение одного участника mixed-age snapshot не скрывает остальных свежих.
4. Неизвестное время/future timestamp/clock rollback дают явное состояние, не фиктивный fresh.
5. Рейтинг, детали и скачивание доступны во время scan/watch; убрать ненужный эксклюзивный writer-lock из read-only пути.
6. Recheck/cancel/crash не уничтожает историю, последний завершённый результат и очередь ещё не законченных items.
7. Экспорт выделенного/top-N/поисковой выборки — отдельный артефакт; не меняет активный пул и область таблицы без явного publish/bind.
8. Scope/profile/generation фиксированы у consumers: проверка B не перенаправляет подключение A.
9. Повреждённый/удалённый current pointer не вызывает несовместимый fallback GUI к произвольным SQLite rows.
10. Hostname/private/auth input поддержан сквозным путём либо отклонён сразу; нельзя принимать форму и потом молча выбрасывать записи.
11. Собственный список действительно изолирован от старой публичной базы.
12. Watch/refill восстанавливает пул, а не навсегда исключает отвалившиеся адреса.
13. Валидация judge до elite; invalid/empty/CAPTCHA ответ → unknown; требование anonymity не сбрасывается молча.
14. DNSBL: правильный IPv6 reverse, zone-specific коды, различие quota/access error/listed/clear/unknown.
15. Скорость: корректное временное окно, минимальный sample, partial/недостаточный замер; один chunk не даёт выдуманный Mbps.
16. Gateway резервирует слот до await; cancel/error освобождает его; deadline покрывает весь handshake, а не первый байт.
17. HTTP upstream health не приравнивается к TCP-open; не повторять уже отправленные неидемпотентные запросы.
18. Gateway/GUI/API имеют разные secrets. LAN включается явно, default loopback сохраняется; проверка изолированно.
19. Denylist применяется до prefilter и отзывает новые admissions; судьба уже открытых streams задаётся отдельно.
20. Empty pool никогда не включает скрытый DIRECT; внешние configs валидируются целевой версией клиента, а не только JSON-парсером.
21. Browser download: picker в пользовательской активации, одно закрытие stream, cancel без ошибки, рабочий fallback.
22. Источники: правильный путь update, понятные имена, metadata после collect, quarantine вместо необоснованного окончательного удаления.
23. Quick test подписан реальным объёмом; full recheck отдельно; нет обхода общей policy или бессрочного вызова.
24. Presets не оставляют неожиданные параметры предыдущего сценария; website response не обещает звонки/4K/весь сервис.
25. Live feed использует реальные события измерений. Строки агрегированного лога не выдаются за поток проверок каждого proxy.
26. Реальная Mac-поставка, writable paths, Windows GUI/CLI lifecycle и достоверные release notes вместо одних инструкций.

## 4. Все обязательные функциональные направления

### F01. Понятные режимы проверки

Реализуй collect-only, TCP-доступность, protocol handshake, basic HTTP/HTTPS-передачу без ввода URL, выбранные сервисы, custom profile и recheck. Мониторинг — режим задания/пула, а не новый противоречивый verdict.

Basic использует явно описанные небольшие probes с валидацией ответа и ограниченным fallback. Отличай отказ нейтрального target от отказа всех прокси. Не отправляй массовую нагрузку на случайный demo endpoint. Пока публичный operator не подтверждён, поддерживается пользовательский/self-hosted contract и честное обозначение предустановленных probes; это не отменяет реализации режима.

Приёмка: новичок выбирает «найти 5 рабочих», не вводит URL, получает результаты с точным уровнем доказательства; TCP-only endpoint не считается передающим HTTP proxy.

### F02. Коллекции и scope

Создание, переименование, архивирование, membership, публичные/свои списки, общий endpoint в нескольких коллекциях, явный выбор scope для scan/results/API/gateway/export. Legacy-данные мигрируют без выдуманного происхождения. Удаление membership не удаляет адрес из другого списка.

Приёмка: две собственные коллекции и публичная база не смешиваются; scan/export в одной не меняет подключённую другую.

### F03. Полноценный импорт

Файл, drag-and-drop, буфер, TXT/URI/CSV/JSON через доступные adapters; preview valid/duplicates/rejected с номерами строк; mapping нужных колонок; merge/replace только выбранной коллекции; отмена, revision conflict, идемпотентный commit, импортный отчёт. Пароли не попадают в raw input/log/provenance на диск.

Приёмка: preview ничего не меняет; crash/cancel не оставляет половину replace; повтор commit не размножает записи; частично плохой файл можно исправить осмысленно.

### F04. Собственные hostname/auth-прокси

Полный путь HTTP Basic proxy auth и SOCKS5 username/password, hostname/IDNA, IPv4/IPv6, отдельные access identities для разных credentials одного endpoint. Ввести OS vault или согласованный session-only secret adapter, защищённый доступ worker, смену credentials/access_revision, обработку locked vault и auth errors.

Секреты не передавать через argv, обычные settings, временный plaintext файл или обычную выдачу API. Vault+SQLite не одна транзакция: staging/compensation/reconciliation. Новый пароль не наследует успешную проверку старого.

Trusted private endpoints — отдельный явный режим собственной коллекции с destination policy; public discovery остаётся ограниченным публичными адресами. Не реализовывать автоматически NTLM/Kerberos или provider signup.

Приёмка: импорт → worker check → scoped gateway → поддерживаемый explicit-secret export работают; обычные логи/экспорты/БД не содержат canary secret.

### F05. Профили и правила целей

Именованные профили, копирование, версии, import/export без secrets, выбранные targets, required/optional, all/any/at-least-K, нужные target-specific thresholds и budget. Изменения сохраняют историю предыдущей версии. Непустое подтверждение обязательно: all(empty), K=0 и все выключенные probes не дают pass.

Приёмка: GUI/CLI/API одинаково исполняют профиль; fail-fast согласован с правилом; изменение порога не приписывает недовыполненным измерениям доказательство полного теста.

### F06. Каталог сервисов и наборов

Категории, поиск, мультивыбор, готовые и пользовательские наборы: basic/search/messengers/social/video/development/API. Переиспользовать девять текущих presets; исследовать нужные дополнения вроде Reddit/Twitch/TikTok/X/Steam/Microsoft по реальным endpoint contracts.

Preset содержит ID/version/maintainer, конкретные probes/assertions, условия прохождения, ограничения/стоимость, дату проверки определения и совместимость. Названия homepage/API/WebSocket/media/длительное соединение различаются. Обновление presets не меняет молча уже сохранённый profile revision.

Приёмка: пользователь выбирает набор, видит проверки каждого сервиса, сохраняет свой и обновляет определения с понятным diff.

### F07. Расширенные параметры

Timeouts connect/handshake/read/whole probe, attempts/retry intervals/backoff, per-target policy, statuses, contains/not-contains, SHA-256, JSON assertions/content-type, body size, bounded redirects, TLS diagnostics/custom CA для доверенного scope, DNS-mode. Пресеты «быстро / баланс / тщательно / экономно» и visible overrides.

POST/body/API-auth нужны только в явно настроенном собственном target-профиле с отдельными secrets и запретом небезопасных повторов; ordinary public probes не получают credentials. Включение новой возможности не отключает TLS verification по умолчанию.

Приёмка: параметры имеют единицы, лимиты и одинаковую валидацию во всех клиентах; непредусмотренные параметры не игнорируются молча.

### F08. География и характеристики

Сохранить пользовательский country picker. Добавить понятные включения/исключения, endpoint-country/observed exit-country, происхождение/дата знания, unknown policy, GeoIP update/status/version, ASN/organization/CIDR, IPv4/IPv6 endpoint и target capabilities. Квоты включаются в F14.

Endpoint DE не отбрасывать заранее только из-за желаемого выхода NL. Hostname geo основана на реально использованном IP и времени. Hosting regex — эвристика, не доказательство residential/mobile.

Приёмка: GUI/API/export применяют один критерий страны, read-only фильтр сам не запускает сеть, неизвестное не выдаётся за проверенное.

### F09. Freshness и доказательства

Единый admission по scope, access/profile revision, observations, TTL, exclusions, capabilities. Отдельные checked_at/published_at, возраст и объяснение допуска. Сохранять историю и последний завершённый результат без удаления до нового измерения. Mixed-age rows фильтруются независимо. Fresh fail не маскируется старым success.

Max-age настраиваем и видим; 300 секунд или 2 часа не считать доказанным оптимумом. Unknown time/clock anomalies/смена сети явно учитываются. Для static TXT не обещать автоматического TTL enforcement.

Приёмка: time advancement, republish, неизменная generation, revoke/clear и новая версия доступа одинаково влияют на GUI/API/gateway/new connection.

### F10. Диагностика

Разделять source/download/parser, сеть устройства, DNS, TCP, handshake, TLS, target response, assertion, rate limit, auth, budget, scope/filter, stale/unknown/empty. Воронка counters и конкретное восстановительное действие.

Контроль сети/target выполняется ограниченно в рамках задания и privacy-политики; глобальный outage не портит репутацию всех адресов. Error codes/parameters отдельно от перевода.

Приёмка: пользователь понимает, почему 0 результатов, и исправляет причину без чтения traceback.

### F11. Задания и восстановление

Persisted job/item states, зафиксированный input scope/profile revision, очередь, pause/resume/cancel/retry, checkpoints, crash recovery и sleep handling. Ограничение числа одновременно изменяющих БД исполнителей. Идемпотентные управляющие действия и structured progress/events.

Приёмка: остановка на любой стадии не теряет завершённые observations, повтор запроса не создаёт дубликат job, resume не расширяет scope и не использует obsolete membership.

### F12. Конвейер и быстрый поиск

Развить asyncio engine: bounded source fetch → parser → normalization/dedup → cheap probe → basic → необходимые expensive probes. Первые проверки и результаты по мере поступления, prior known-good с поправкой на возраст, find-N, backpressure, adaptive concurrency и per-host/target limits.

Управлять CPU/RAM/FD/байтами/requests, а не просто увеличивать workers. Различать N endpoint, N уникальных IP и N подтверждённых exit-IP.

Приёмка: воспроизводимые benchmarks time-to-first/time-to-N/RAM, пауза и cancellation всей цепочки, отсутствие дублирующей проверки одного item.

### F13. Полный source manager

Принять результат отдельного sources workflow по конкретной итоговой ревизии. Каталог ID/publisher/family/mirror, поддерживаемые text/JSON/CSV/page-JSON/HTML adapters, preview, категории/наборы, enabled/quarantine, ETag/Last-Modified/304, last-good/cache TTL, Retry-After/backoff, byte/page/decompression budgets.

Все исследованные записи доступны с честными supported/needs-adapter/needs-auth/unsupported/experimental статусами; не включать всё автоматически. 8 рекомендуемых источников не заменяют весь расширенный каталог. Большие GFP-подобные наборы имеют memory/budget limits и partial report.

Происхождение many-to-many, first/last-seen, raw/valid/duplicate/new/checked/passed, одинаковый dataset не считается независимым publisher. Metadata поставщика не превращаются в наши observations.

Приёмка: реальный collect через каждый adapter, migration старых URL/overrides, 304/пустой/битый/большой ответ, восстановление и управление через GUI/CLI/API. Если source workflow ещё идёт, не вмешиваться в его рабочую копию; делать независимые контракты, затем принять конечный handoff. Отсутствие конечного результата — внешняя зависимость, а не разрешение затереть его промежуточный код.

### F14. Постоянный пул

Desired N, minimum, резерв, collection/profile/policy binding, refill из резерва/известных/разрешённых sources, cooldown/probation/re-admission, per-pool limits, несколько именованных пулов после общего engine. Квоты страна/protocol/ASN/unique exit с честным unknown.

Hard budgets уже здесь, без зависимости от полноценного scheduler/tray. Empty/degraded показывает количество/причину/следующую попытку; без скрытого direct, ослабления страны или public в private.

Приёмка: N=5 → два отказа → восстановление до 5; отсутствие резерва → честное 3/5; budget=0 останавливает refill; после crash сохраняется целевое состояние.

### F15. Расписания и уведомления

Интервалы и временные окна с timezone/DST, пауза/возобновление, request/byte/time budgets, quiet hours, no-catchup после сна, optional metered/battery policy там, где ОС предоставляет сигнал. Значимые state-change уведомления с dedup/hysteresis.

Probe/source/retry/judge/speedtest payload считать отдельно от relay/billable traffic. In-flight остаток после лимита конечен и объясним. Не отправлять внешние email/webhooks без выбранной пользователем конфигурации.

### F16. Gateway и именованные пулы

Binding listener/client к pool+profile+policy, rotation round-robin/random/обоснованный health-aware выбор, strict/failover sticky modes, session TTL, concurrency reservation, bounded cache, безопасный snapshot из event loop, transport parity HTTP/CONNECT/HTTPS-to-proxy/SOCKS4/4a/SOCKS5 где поддержано, DNS modes и IPv6.

Client filters только сужают разрешённый scope. Healthy TCP ≠ successful target request. Нельзя прозрачно переносить открытый TCP-stream на новый upstream. Долгие соединения и shutdown имеют понятную политику.

Приёмка: concurrent limits, отказ/cancel/deadline, смена generation, удаление пула, изоляция clients, auth и реальный local transport.

### F17. Законченный путь подключения

Выбрать пул → приложение/браузер/скрипт → точные поля/recipe → контроль маршрута → понятное отключение. Переиспользовать пользовательские страницы и QR, а не рисовать заново.

Отличать probe самого Workbench от подтверждения трафика реального клиента. Для QR проверять round-trip decode, IPv6/escaping и не публиковать управляющий API/GUI secret. LAN отдельный opt-in; phone TCP gateway не называется VPN/UDP-звонками.

### F18. Единое управление GUI/CLI/API

Один service layer для validate/import/check/job/select/publish/pool/schedule; одинаковые units, error codes, permissions и policy. CLI subcommands, JSON/NDJSON, exit codes; API jobs/events/cursor pagination; GUI использует те же операции. Сохранить старые команды через compatibility adapters.

Новый API-key manager и полнота публичного control API обязательны по F29 ниже.

### F19. Работа со списком результатов

Сохранить уже существующие row/bulk controls. Довести page/selected/all-matching scope, bulk recheck/export/copy/tag/exclude/denylist, tags/favorites/notes, saved filters/columns, историю и отмену recoverable операций. Compact default columns, expand details, матрица proxy×target, fresh/stale/failed/unknown views.

Выделенное не должно переноситься на другой scope незаметно. Массовые операции на больших списках не требуют загрузки всего результата в браузер. Избранное не обходит freshness.

### F20. Дополнительные измерения

Исправить нынешние bandwidth/judge; добавить отдельные bounded capabilities: WebSocket handshake/ping, длительность соединения, HTTP API assertions и ограниченный media manifest/segment probe при подходящем контракте. Измерения cold/reused connection не смешивать.

Предоставить небольшой self-hosted reference probe для локальной проверки/собственного использования, без обязательной облачной инфраструктуры. Возможности UDP/HTTP2/новых transports фиксировать в capability matrix и реализовывать согласованным adapter, не объявляя их поддержанными по HTTP GET.

Приёмка: каждый новый вид измерения имеет контролируемый endpoint, budgets, distinct outcome и локальные positive/negative сценарии. Нет общего обещания «все звонки/видео работают».

### F21. Сравнение источников и провайдеров

Сопоставимые временные окна/profile cohorts, выборка и confidence, survival, overlap, уникальный вклад family, время/байты/попытки на пригодный адрес. Свои два поставщика можно сравнить на одинаковых условиях. Отличать bias из-за find-N, фильтров, географии и порядка источников.

Приёмка: одинаковые fixtures в другом порядке дают сопоставимый результат; unknown не превращается в нулевую надёжность; claims основаны на наблюдениях.

### F22. Удобная фоновая работа

Tray/menu bar, close vs quit, повторный запуск в existing instance, видимый статус/пауза/выход, optional autostart, sleep/wake/network-change, доступность vault и bounded termination workers/listeners. Фон и контроль пользователя не зависят от открытой вкладки браузера.

Приёмка: нет orphan processes; после сна нет шквала catch-up checks или массового ложного fail; автозапуск включается только пользователем.

### F23. Поставка Windows/macOS/Linux

Самодостаточный Mac arm64 `.app` и `.dmg`/installable archive; Intel/universal2 только после подходящей сборки/проверки. Windows per-user installer, portable вариант, GUI без лишней консоли и отдельный CLI. Существующие browser/headless пути сохранить.

Per-user data/cache/logs, explicit portable mode, миграция старой папки, корректные ресурсы/cwd/worker command, Unicode paths/non-admin/read-only bundle. Thin desktop host поверх текущего UI; новый framework только по проверенному ограничению.

Update notice и управляемое обновление с проверкой происхождения, backup/schema compatibility/rollback; signing/notarization pipeline с проверкой доступности ключей. Не публиковать неподписанный artifact как подписанный. Linux/Docker/pipx не ломать.

### F24. Данные, резервные копии и восстановление

Версионированная schema, технический backup до миграции через согласованный SQLite путь, manifest/checksum, restore-preview в новый data path, секреты отдельно с rebind, retention/cleanup preview и размер данных. Старый binary не пишет в новую schema.

Rollback восстанавливает pre-migration backup и сохраняет новую БД отдельно; не обещать lossless downgrade неизвестных полей. Clear/remove инвалидирует consumers и caches, не удаляет лишние настройки/секреты.

### F25. Помощь и диагностика

In-app help по error codes, локальный diagnostic bundle с preview/redaction, версии/scope/job health, reproducible fixture recipe. Никакой скрытой отправки. Полезная ошибка содержит действие, а не один exception class.

### F26. Open source, шаблоны и доступность

Data-only profile/source packs с schema/version/attribution/compatibility, diff-import, validator и минимум три законченных recipes. RU/EN всех новых сценариев и ошибок, keyboard/focus/screen reader/progress semantics, reduced motion, обычный ноутбук/200% zoom, offline help.

Обновить README/CHANGELOG/SECURITY/PRIVACY/CONTRIBUTING под реальные возможности. Никакой обязательной телеметрии/аккаунта. Release artifacts, dependencies, SBOM/checksums/provenance и CI должны соответствовать фактической поставке; release публикуется только отдельным действием после приёмки.

### F27. URL-подписки и жизненный цикл feed

Пользовательские URL/подписки, source binding к коллекции, refresh/delta, merge/replace, last-good, expiry, quota/token-expiry diagnostics. Секретные URL/headers через references и redaction. Поддерживаемые Clash/sing-box imports извлекают только разрешённые endpoint/metadata, не исполняя rules/scripts.

Приёмка: ошибка обновления не очищает рабочую коллекцию; смена credentials инвалидирует нужные admissions; пересечения не удаляют чужой membership.

### F28. Экспорты и согласованные snapshots

Общий snapshot contract rows/status/scope/profile/policy/generation; immutable publication и atomic pointer. Отдельные export jobs/artifacts для выбранных строк и фильтров; active pool меняется только явным publish/bind.

Compatibility preview: protocol/auth/DNS/TLS/IPv6; unsupported rows объясняются без молчаливого упрощения. TXT/CSV/JSON, protocol files, PAC/Clash/sing-box/proxychains сохранить и проверить. Credentials включаются только отдельным разрешённым действием; default redacted/gateway-reference не выдаётся за готовый direct-auth URI.

Пустой/expired/unsupported-only набор — совместимый fail-closed либо понятная ошибка генерации. Новые файлы валидировать реальными поддерживаемыми версиями клиентов. Expired static TXT не может отозваться сам — это видно пользователю.

## 5. F29 — полноценное API и менеджер ключей

Это отдельное обязательное требование пользователя, а не необязательная документация старого `/proxies`.

### Что уже есть и что нужно сохранить

В актуальном исходном состоянии имеется read-only API: `/proxies`, `/random`, `/status`, `/pac`, `/clash`, `/singbox`; один token через `--api-token`/`PROXY_WORKBENCH_API_TOKEN`. Внутренние GUI POST routes не равны полноценному поддерживаемому внешнему control API.

Переиспользуй engine/selection и обеспечь совместимость legacy чтения через явные policy; не расширяй права старого токена до администратора молча.

### Менеджер API-ключей в GUI и CLI

- Создать ключ, имя/назначение, optional срок действия, scope коллекций/пулов, permissions и rate/concurrency quotas.
- Показать полный случайный secret один раз; далее только ID/prefix/метаданные. Генерация криптографическая с достаточной энтропией.
- Хранить односторонний verifier/hash для API keys; сравнение без timing-leak. Upstream passwords, которым нужен обратный доступ transport, живут в OS vault отдельно.
- Список ключей, created/expires/last-used, переименование metadata, disable/revoke/delete/rotate; возможность узкого transition window при ротации по явной настройке.
- API-key, GUI-session, gateway-password и upstream-credentials — разные identities. Ни один нельзя подставлять вместо другого.
- Первичная выдача административного ключа только через локальный доверенный GUI/CLI bootstrap. Read-only ключ не создаёт себе новый admin.
- Отзыв/expiry проверяются на каждом запросе и новых подписках/leases. Активные SSE/stream sessions корректно завершаются или перепроверяют срок по documented policy.

### Права и scope

Нужны отдельные права на чтение результатов/status, коллекции/import, профили, sources, запуск/управление заданиями, пулы/gateway, расписания, экспорт, admin/settings/key management. Secret-bearing export — отдельное чувствительное право/явный параметр.

Ключ можно ограничить конкретными collection/pool IDs. Object-level права проверяются у каждой операции, batch, job status, event stream, download и export artifact. Counts/errors не раскрывают чужие приватные scope. Клиент не расширяет себе доступ через filters, `allow_private`, raw path или запущенный job.

### Обязательные операции

Согласуй versioned API (например `/v1`) с OpenAPI до параллельной реализации. Точные пути можно адаптировать к проекту, но операции должны существовать:

| Область | Операции |
| --- | --- |
| Сервис | version/capabilities, health, readiness, состояние очереди и разрешённых пулов |
| Ключи | list/create/rotate/revoke и metadata update по административному праву |
| Коллекции | CRUD/membership, preview import, commit import, merge/replace |
| Источники | каталог/list/detail, пользовательские definitions, enable/disable, preview/refresh job и status |
| Профили | list/create/read/update version/clone/archive, validation и presets |
| Проверки | submit collect/check/recheck/quick-test как job, scope/profile/budget, progress/result |
| Задания | list/detail, pause/resume/cancel/retry, bounded history и SSE/events |
| Результаты | cursor filters/sort/max-age, детали/observations, random/top/selection; unknown/stale modes явно |
| Пулы | create/update target/reserve/policy, start/pause/refill/recheck, members/status |
| Gateway | разрешённые bindings/listeners/session metadata и управление конфигурацией без произвольного network exposure |
| Расписания | CRUD/enable/disable/next-run/counters |
| Экспорт | create artifact, poll status, compatibility report, protected download выбранного scope |
| Резервирование | acquire/lease/release с TTL и лимитами для потребителей; optional bounded target-aware feedback без возможности испортить глобальную репутацию |

Долгие операции возвращают job ID и HTTP 202, не держат управляющий HTTP-запрос на время всего scan. Mutations с idempotency key, revision/ETag conflict, понятными errors, schema validation и конечными пределами тела/очереди. GET не запускает проверки тайно.

### Сетевая модель и документация

Default API остаётся loopback. Remote/LAN включается явно пользователем и требует корректного auth/защищённого канала. Реализуй документированную TLS-конфигурацию или supported reverse-proxy setup; не обещай шифрование от наличия Bearer token. Host/Origin/CORS проверяются по реальной модели, а не отключаются ради удобства.

Новые управляющие ключи передаются через Authorization header, не URL. Для клиентов подписок, не умеющих header, отдельный узкий read-only subscription secret с expiry/revoke, без admin прав и с redaction. Legacy token-in-query либо ограниченный compatibility path с предупреждением, либо явная миграция — без тихой потери старой интеграции.

Локальный audit log: key ID, операция, объект/scope, результат и timestamp; без полных ключей, паролей, response bodies и пользовательского трафика. Настраиваемые quotas/rate-limit с 429/Retry-After, таймауты и ограниченные потоки/leases.

Поставь OpenAPI, краткую страницу «API и ключи», примеры curl/Python/JS и законченный пример: создать scoped key → импортировать список → запустить проверку → получить события/job status → запросить свежие прокси → экспортировать → отозвать ключ. Проверять на mocks, а не на публичных endpoints.

### Приёмка API

1. Reader читает разрешённую коллекцию и не запускает scan/не читает чужую.
2. Operator создаёт ограниченный job, получает ID/progress/result и отменяет его.
3. Один idempotency key не запускает второй scan; устаревшая revision даёт conflict.
4. Revoked/expired key перестаёт работать; admin-only операции и secret export отдельно защищены.
5. Key A не получает B через список, random, job ID, SSE, export/download, batch или counts.
6. Canary secrets отсутствуют в БД API verifiers как plaintext, logs, argv, обычных files/JSON/OpenAPI examples.
7. GUI, CLI и API для одного scope/profile возвращают эквивалентный outcome и состав.
8. Rate/concurrency/body limits действуют; API отзывчиво во время scan.
9. Legacy read token не получает новые admin/приватные полномочия при миграции.

## 6. Этапы workflow и контрольные точки

### Этап I. Параллельная сверка

Все профильные роли читают свои участки и входные исследования. На выходе — current capability matrix, ownership, исходный diff, воспроизводимые дефекты и coverage F01–F29. Новая общая дорожная карта не заменяет код. Не тратить весь запуск на повторение прежних исследований.

### Этап II. Согласование минимальных общих контрактов

Зафиксировать identity/scope/profile/observation/freshness/job/pool/API permissions, schema migration order и error/event contracts. Сравнить небольшие варианты, выбрать переиспользующий существующий engine. Ревьюеры проверяют, что каждый заявленный сценарий выразим и нет циклов зависимостей.

Не проектировать десятки абстрактных пакетов заранее. Выделять границы для действительно независимых implementations. Approval этой точки — внутренний review workflow, без требования пользовательского одобрения каждой обычной детали.

### Этап III. Параллельная реализация основы

Storage/freshness, import/collections, checker correctness, gateway, sources handoff, API-key service, platform paths — независимыми задачами после общих контрактов. UI writers используют готовые contracts/fixtures, не выдумывают ответы backend. Интеграция короткими проверяемыми изменениями.

### Этап IV. Параллельная реализация функциональных направлений

Все оставшиеся F01–F29 исполняются по готовности зависимостей. Не прекращать после первой волны. Sources workflow не дублировать; принять его результат и устранить реальные конфликты. Профили/сервисы, дополнительные probes, pools/schedules, API operations, UI lists, OS packaging, docs/tests идут независимо там, где это возможно.

### Этап V. Интеграция и независимая критика

После каждого законченного среза: согласовать version, интегрировать, выполнить contract и нужные пользовательские проверки. После общей интеграции четыре независимых reviewers проверяют данные, доступ, пользовательский путь, платформы/ресурсы. Не ограничивать искусственно число существенных findings пятью, если найдены другие.

Замечание содержит trigger, место, последствия и воспроизведение. Неподтверждённый риск не выдаётся за доказанный сбой. Исправление принимает reviewer по новой зафиксированной версии. Не закрывать дефект изменением теста под неправильное поведение.

### Этап VI. Финальная приёмка всего scope

Проверить coverage всех обязательных IDs и пользовательские сценарии ниже. Доступные checks проходят на объединённой версии. Windows/macOS/Linux исполнение подтверждать соответствующим runner; локальный Mac не доказывает Windows. CI configuration не равна выполненному CI.

Подготовить artifacts и release notes. Отсутствие signing keys, недоступный ОС-runner или незавершённый внешний sources workflow отмечаются точным `external_blocker`; они не превращают остальные незавершённые функции в «готово».

## 7. Полная сквозная приёмка

1. Новичок без ручного URL находит несколько базово пригодных прокси.
2. Выбор страны по имени, endpoint/exit semantics и unknown работают одинаково в интерфейсах.
3. Набор сервисов all/any/K даёт раздельные результаты и правильный общий итог.
4. Изменение/сохранение/экспорт профиля не смешивает версии измерений.
5. Собственный импорт после большого публичного сбора проверяет ровно выбранную коллекцию.
6. CSV/JSON/TXT preview, ошибки, duplicates, cancel, replace и повтор commit корректны.
7. Hostname/auth проходят весь путь; два доступа к одному endpoint не сливаются; смена пароля отзывает старое доказательство.
8. Источники обновляются 200/304/429/partial; bad update не стирает last-good и не делает старые proxy-проверки свежими.
9. Большой источник ограничен памятью/временем; первые результаты доступны до полного окончания, stop/resume сохраняется.
10. Recheck/cancel/crash, устаревшие timestamps, mixed-age generation и corrupt pointer согласованы во всех consumers.
11. Экспорт выбранных строк не переключает активный пул и не скрывает остальные результаты.
12. Empty/unsupported/expired export не выбирает DIRECT; совместимость подтверждается целевым клиентом.
13. Maintained N восстанавливается из резерва/источников; нехватка объясняется, budgets и scope соблюдены.
14. Gateway выдерживает concurrent max-per-proxy, partial handshake, cancel, долгие streams и shutdown.
15. Read-only и operator API keys, ресурсные scopes, revocation, expiry, idempotency, SSE, quotas и protected exports проходят negative tests.
16. Полный API-сценарий выполняется внешним клиентом без ручных операций в GUI после выдачи ключа.
17. QR декодируется, recipes и client route действительно проверяемы; loopback/LAN/remote роли и tokens различаются.
18. Windows/Mac запуск без Python, resources, per-user paths, второй запуск, close/quit, tray и обновление/restore проверены доступными средствами.
19. Сон/смена сети/недоступный vault не повреждают историю и не дают ложную массовую оценку прокси.
20. Backup/migration/rollback/retention безопасны; канареечные secrets нигде не просачиваются.
21. RU/EN, клавиатура, screen reader labels, 200% zoom и основные браузеры проверены при сохранённом дизайне пользователя.
22. 35 сценариев продуктового исследования сопоставлены с реализованными операциями или честно отмеченными экспериментальными границами.

Тесты должны проверять поведение, не исходный текст реализации. Не писать тест ради счётчика и не гонять полный suite бесконечно. Производительность измерять на согласованных fixtures; не выдавать synthetic throughput за скорость живых публичных прокси.

## 8. Условия завершения и отчёт

Поддерживай один актуальный progress/coverage документ и конкретные evidence проверок. Не создавать десятки повторных отчётов. При переключении контекста сохраняй checkpoint: выполнено, принятые contracts, commits, оставшиеся задачи и блокеры; продолжай без повторения всей работы.

Нельзя объявить завершение, если:

- агент закончил свою ветку, но она не интегрирована;
- есть кнопка без работающего backend;
- API выдаёт только список, но key manager/control jobs отсутствуют;
- `.app` существует только в инструкции;
- весь scope незаметно сокращён до выбранных пяти карточек;
- тесты проверяли старую ревизию или fixtures вместо заявленного внешнего клиента/платформы;
- реализация требует ручных действий, которых нет в пользовательском сценарии;
- существенные подтверждённые дефекты остались без исправления.

Финальный результат:

1. Интегрированная рабочая ветка с осмысленными commits, сохранённым пользовательским дизайном и данными.
2. Полная таблица F01–F29 и закрытия технических замечаний с evidence.
3. Рабочие GUI/CLI/API сценарии и документация менеджера ключей.
4. Подготовленные платформенные artifacts и фактически выполненные проверки.
5. Короткий список реальных внешних blockers/экспериментальных границ; никакой фиктивной общей готовности.

Рутинные обратимые решения принимай самостоятельно. Критичные внешние ограничения сообщай кратко, продолжая независимую работу. Отчёт по-русски: что пользователь теперь может сделать, что проверено и что объективно осталось.

Начинай с baseline и распределения 36 ролей по реальной доступной concurrency, затем запускай цепочку Ultra Workflow и доводи весь обязательный scope до результата.
