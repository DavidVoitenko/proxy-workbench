# Очищенный каталог функциональных идей

К реализации в дорожной карте приняты 28 законченных направлений F01–F28. Каждому соответствует [карточка](04-feature-cards.ru.md) со сценарием, частотой, текущим механизмом, обходом, минимальным объёмом, UI/backend/данными, зависимостями, рисками, приёмкой и основанием приоритета. Элементы внутри строки — части одной функции, не дополнительные неоценённые проекты. R1 содержит только пять карточек; остальные — последующие этапы.

## Принятые направления и полный состав идей

| ID | Конкретные возможности | Категория / приоритет | Что переиспользовать |
|---|---|---|---|
| F01 | Независимые collect/TCP/handshake/basic/service/custom/recheck; basic без ручного URL; отдельные HTTP и HTTPS capabilities; простые ресурсные режимы; описание силы доказательства | Основа / P1 | collect, prefilter, checker, default target, Quick/Balanced/Thorough |
| F02 | Именованные коллекции; изоляция private/public; many-to-many membership; архивирование; scope задания/выдачи; миграция legacy без выдуманного происхождения | Основа / P0 | SQLite candidates/profiles/results; --data workaround |
| F03 | Drag/drop и clipboard; preview распознанного; причины ошибок по строкам; CSV mapping; merge/replace diff; транзакция; отмена; отчёт импорта | Основа / P0 | textarea/file/--input, normalize, dedup |
| F04 | Hostname endpoint; HTTP Basic/SOCKS5 auth; OS vault/сеансовый secret; разные credentials одного host; редактирование access; private endpoints по явной политике; auth-aware gateway/export | Основа / P0 | HTTPX transport, существующие checker/gateway; дополнить весь путь |
| F05 | Библиотека именованных профилей; immutable versions; clone/diff; обязательные и дополнительные probes; all/any/K; переключение истории; schema validation | Основа / P1 | hash profiles, config JSON, per-target reliability |
| F06 | Категории/поиск сервисов; готовые и свои наборы; probe-type labels; preset revision/maintainer/last validated; deprecated status; review обновлений; сохранение пользовательских forks | Полезные следующие / P2 | Девять presets и существующие quick chips |
| F07 | JSON/content assertions; интервалы и backoff; явные deadlines по поддерживаемым стадиям; bounded redirects; body budget; TLS policy; реальный DNS mode; advanced config preview | Полезные следующие / P2 | GET/HEAD, statuses, contains/hash, connect/total timeout, max_bytes |
| F08 | Страны по имени RU/EN; multi include/exclude; endpoint/exit выбор; unknown policy; ASN/organization/CIDR; endpoint/destination IPv4/IPv6; версия и обновление GeoIP; отображение конфликтов | Полезные следующие / P1 | GeoIP/ASN, Geonode country, exit judge, WIP country picker |
| F09 | Fresh/stale отдельно от pass/fail; minimum evidence; count samples; max_age; общий admission; explanation score; measured/inferred separation; atomic published generation; current vs history | Основа / P0 | checked_at, samples, counters, recommended score, export generations |
| F10 | Воронка нулевой выдачи; причины по стадиям; понятные действия; network/target health; inconclusive при общем сбое; backoff; сохранение score при неподтверждённой общей причине | Основа / P1 | logs, HTTP/content exceptions, progress/source report |
| F11 | Persistent job IDs; очередь с одним writer; pause/resume/cancel; scope snapshot; checkpoint; non-destructive recheck; crash/sleep/network recovery; идемпотентная запись | Основа / P1 | single writer lock, SQLite progress, stop/resume |
| F12 | Streaming collect→scan→results; первый пригодный до EOF; быстрый N; приоритет проверенных; cheap/expensive queues; bounded memory; rate по target/source; adaptation ресурсов | Полезные следующие / P1 | want, prefilter, fit_workers, Rate, scan queues |
| F13 | Source entity/adapter; предпросмотр; conditional requests/cache; source family/mirror; overlap/provenance; parse vs availability health; quarantine; квоты и пагинация; last-good | Полезные следующие / P2 | Sources catalog, text/HTML/Geonode, sources-report, update/prune |
| F14 | Поддерживать N; резерв; refill; cooldown/probation/readmission; min pool size; country/ASN quotas; явный count endpoints vs exit; дефицит и нулевой пул; scope/budgets | Полезные следующие / P1 | watch и gateway pool — строительные блоки, не готовый maintained pool |
| F15 | Расписания/окна; timezone/DST; request/byte/concurrency caps; battery/metered policy; quiet hours; hysteresis notifications; no catch-up storm после сна | Полезные следующие / P2 | watch interval/rate; новые persistent scheduler policies |
| F16 | Gateway binding к pool; strict/failover sessions; upstream-change event; fresh admission; лимиты; task routing через локальные конфиги; pool-empty/limit/capability diagnostics | Полезные следующие / P1 | HTTP/CONNECT/SOCKS5 gateway, rotation, sticky, cooldown, max-per-proxy |
| F17 | Мастер браузер/app/script; recipes; копирование полей; реальная контрольная проверка через gateway; понятное отключение; restore при будущей автоматической настройке | Удобство / P1 | Адреса API/gateway, Telegram button, PAC и config exports |
| F18 | Единые операции и схема; GUI/CLI/API parity; JSON/NDJSON; stable error codes; jobs/pause/resume; pagination; event sequence/reconnect; tokens/scopes; idempotency | Полезные следующие / P1 | CLI get/test/serve, read-only API, GUI routes |
| F19 | Tags/favorites/notes; row select/page/all scope; bulk operations; временные исключения; endpoint/IP/CIDR deny scopes; saved filters/columns; change history/undo | Удобство / P2 | Existing search/filter/copy page/denylist, WIP row actions |
| F20 | WS handshake/ping/duration; API assertions; media manifest/segment; sustained connection; корректные speed units/sample size; отдельные capability facets; self-hosted probe contract | Полезные следующие / P2 | Повторные попытки, jitter, download speed/judge |
| F21 | Сопоставимые source/provider cohorts; unique contribution; overlap; sample size; survival; bytes/cost per admitted; bias labels; сравнение двух поставщиков | Полезные следующие / P2 | source_quality, candidate_seen, counters/ASN |
| F22 | Tray/menu bar; close vs quit; background engine; opt-in автозапуск; повторный запуск в existing instance; sleep/wake; locked-vault handling; bounded shutdown | Удобство / P1 | GUI server/subprocess/instance lock |
| F23 | macOS .app; existing Windows .exe; архитектуры; отдельное окно с общим UI; browser/headless; portable choice; writable paths; update notice; rollback-aware обновления | Удобство / P2 | Packaging, Windows CI, paths, launchers, браузерный UI |
| F24 | Consistent backup; manifest/checksum/schema; restore preview в новый data path; rebind secrets; retention; миграции/rollback; size cleanup preview | Удобство / P1 | SQLite, settings export/import, data paths, clear-data |
| F25 | In-app help по error code; local diagnostic report; redaction/preview; version/scope/health summary; reproducible mock recipe; opt-in передача | Сопровождение / P2 | README/FAQ, privacy/security/issues, logs |
| F26 | Profile/source data packs; схемы/validator; recipes; attribution/version/maintainer; contribution fixtures; RU/EN completeness; keyboard/accessibility; offline help | Сопровождение / P2 | CONTRIBUTING, существующая i18n и OSS документация |
| F27 | URL/subscription для собственного списка; secret-bearing URLs; refresh/delta; merge/replace policy; provider quotas; token expiry; last-known-good; expiration metadata | Полезные следующие / P2 | Source fetch, future adapter/import/access models |
| F28 | Scoped export; format capability matrix; explicit unsupported rows; auth-aware serializers; no hidden DIRECT; immutable generation; consumer consistency; manifest и snapshot age | Основа / P0 | Все существующие форматы и export generation механизм |

## Полнота по направлениям запроса

| Направление | Карточки | Принципиальный выбор |
|---|---|---|
| А. Режимы | F01, F05, F11, F14 | TCP, передача, сервис и наблюдение имеют разные результаты |
| Б. География | F08, F09, F14 | Endpoint и exit раздельны; unknown не приравнивается к запрету/успеху |
| В. Сервисы | F05, F06, F20 | Сайт/API/WS/media/sustained не смешиваются |
| Г. Параметры | F07, F15 | Advanced по запросу; общий schema validator |
| Д. Скорость | F12, F15 | Время до пригодного результата и расходы, а не максимум workers |
| Е. Источники | F13, F21, F26 | Использовать отдельное исследование, не повторять поиск |
| Ж. Импорт/свои | F02, F03, F04, F27 | Транзакция, изоляция, credentials end-to-end |
| З. Результаты | F02, F09, F19 | Endpoint/access/membership/exit — разные сущности |
| И. Качество | F09, F10, F20, F21 | Измерения и неопределённость видимы; outage не портит весь рейтинг |
| К. Пулы | F11, F14, F15 | Reconcile N с budget и восстановлением, не только таймер |
| Л. Gateway | F16, F17, F28 | Local address стабилен; upstream/exit могут меняться |
| М. CLI/API | F18 и общие контракты F02/F09/F28 | Одна реализация правил, explicit scope и версии |
| Н. Desktop | F22, F23, F24 | Фоновая надёжность перед обещанием бесшовной оболочки |
| О. Расширения | F20/F21 и X01–X08 ниже | Сначала отдельные проверяемые эксперименты |
| П. Open source | F06, F13, F25, F26 | Данные/шаблоны/помощь без аккаунта и hidden telemetry |

## Объединённые дубли и границы функций

- «Ещё источники», «подписки» и «адаптеры» разделены по проблеме: F13 — получить/объяснить данные; F27 — жизненный цикл приватного feed; глобальный поиск выполняется в другом исследовании.
- «Watch», «автообновление», «всегда N» не одна функция: watch уже есть; F14 поддерживает целевое состояние; F15 определяет когда и сколько ресурсов тратить.
- «Кнопка Connect», «proxy.pac», «extension» не три равноправные основы. F17 завершает пользовательский путь; F28 сохраняет семантику форматов; X01 проверяется только если инструкции недостаточны.
- «Рабочий», «быстрый», «не в DNSBL», «anonymous», «residential» — разные утверждения. F09 объясняет evidence; F20 измеряет отдельные свойства. Нельзя объединять их в единственный зелёный статус.
- «Пресеты» уже есть. F06 — сопровождение/каталог; F05 — составление правил; F20 — действительно новые виды probes.
- «Pause», «перезапустить», «tray» разделены: F11 сохраняет работу, F22 связывает её с ОС. Tray без recovery не решает проблему.
- «Новые колонки» входят в F19, но новый тип данных/измерения относится к F08/F09/F20; не плодить функции вокруг каждого столбца.
- «API v2» сам по себе не ценность. F18 нужен лишь для одинаковых операций и автоматизации; существующие API/get/test сохраняются.

## Экспериментальные направления: не приняты к реализации

Частота ниже — предположение, исследование пользователей не проведено. Минимальный эксперимент требует отдельного решения после приоритетных этапов.

| ID / идея | У кого / как сейчас | Минимальный эксперимент и зависимости | Новая сложность / критерий продолжения / почему не раньше |
|---|---|---|---|
| X01 Browser extension | Новичок часто меняет browser proxy; сейчас ручные настройки/PAC | Один браузер, выбрать local pool, on/off, restore; F16–F18 | Permissions, магазины, политики браузера. Продолжать, если наблюдаемые пользователи не завершают F17, а extension сокращает ошибки без потери контроля |
| X02 Удалённое управление | Владелец домашнего/VPS узла; CLI/SSH/API уже покрывают часть | Read-only dashboard через существующий защищённый канал, затем scoped controls; F18/F25 | Auth/TLS/revocation/multi-host state. Продолжать при реальном remote сценарии; local-only first |
| X03 Мобильный web-доступ к управлению | Проверить состояние своего ПК с телефона, эпизодически | Адаптивный UI для статуса одного узла через X02 | Недоступность ПК, expiring auth, маленький экран. Это control plane, не проксирование трафика телефона; native app не нужен до проверки |
| X04 Дополнительный транспорт | Пользователь собственного SSH/другого upstream; HTTPS уже поддержан checker, требуется отдельная gateway parity | Один выбранный транспорт, capability matrix и mocks; F04/F16/F20 | Lifecycle, auth и формат клиента. Добавлять лишь при подтверждённом импорте, который нельзя завершить существующим транспортом |
| X05 Самостоятельные проверочные endpoints | Разработчик не доверяет/не может использовать общие targets | Документированный echo/WS/media contract и небольшой self-hosted reference; F01/F20 | Эксплуатация/трафик/сертификаты. Продолжать, если закрывает governance basic и воспроизводимость; менеджер остаётся полезен без обязательного собственного сервера |
| X06 VPN/TUN-адаптер внешнего движка | Продвинутому пользователю нужен трафик приложения без proxy settings | Интеграционный proof с готовым engine, один OS, rollback DNS/routes | XL+ стоимость: права, DNS, routes, исключения, UDP, поддержка. Требует отдельного продукта/решения; экспорт в существующие клиенты пока дешевле |
| X07 Синхронизация конфигураций | Пользователь нескольких ПК; backup/manual перенос | Только non-secret templates, merge conflicts, offline-first; F24 | Аккаунт/хранение/ключи/версии. Продолжать после доказанного частого multi-device use; обязательное облако отклонено |
| X08 Поставщик с управляемыми session/rotation API | Частый пользователь конкретного поставщика; сейчас provider dashboard | Один adapter за интерфейсом F27, без purchase; измерить rotate/readiness | Quotas, secrets, vendor lock-in. Нет необходимости реализовывать до выбранного пользователями поставщика |

## Идеи, которые сейчас не делать

| ID | Решение и причина отказа | Когда можно пересмотреть |
|---|---|---|
| R01 Собственный VPN/TUN стек | Очень большой отдельный сетевой продукт; не нужен для менеджера HTTP/SOCKS | Только отдельный обоснованный проект; сначала X06 внешнего engine |
| R02 Обязательный аккаунт/облако/скрытая телеметрия | Противоречит локальному OSS и добавляет постоянную эксплуатацию | Не требуется для заявленных сценариев; optional X07 имеет отдельные условия |
| R03 Native мобильный клиент с проксированием телефона | Два новых OS, фон/сетевые разрешения, другой сценарий | После X03 и отдельного подтверждённого mobile-data-plane запроса |
| R04 Гарантировать fixed IP/анонимность/«чистый residential» по текущим признакам | Данных недостаточно: sticky endpoint, headers и ASN не доказывают эти свойства | Только ограниченные измеряемые утверждения, не общий сертификат |
| R05 Автоматически покупать/регистрировать прокси и маркетплейс | Неподтверждённый спрос, платежи и коммерческая поддержка не часть core | Отдельный бизнес-кейс, без зависимости базового продукта |
| R06 CAPTCHA bypass, хранение аккаунтов сервисов ради массовых проверок | Не нужно для проверки прокси, усложняет секреты и поддержку | Прикладной тест собственного API с explicit profile можно сделать F07/F20 |
| R07 Удалять источник после одной неудачной выдачи | Причина может быть фильтр, сеть, target или маленькая выборка | Только quarantine и контекстные метрики F13/F21 |
| R08 Скрытый DIRECT, автоослабление страны и подмешивание public в private | Изменяет задачу пользователя и смысл подключения | Только отдельная явно выбранная политика с видимым состоянием |
| R09 Максимум workers и полный speedtest каждого кандидата | Нагрузка/трафик растут быстрее полезности, мешают первым результатам | Адаптивный pipeline и expensive checks после admission |
| R10 ML/AI-рейтинг без наблюдений и truth labels | Добавляет непрозрачность, не решает freshness/sample bias | После накопления opt-in качественных данных и сравнения с простым правилом |
| R11 Произвольные исполняемые парсеры/скрипты presets | Усложняет доверие, sandboxing и воспроизводимость | Типизированных adapters/assertions достаточно для MVP |
| R12 Слить всё по IP или hostname resolved IP | Теряются порт/протокол/аккаунт/выход/принадлежность | Использовать разные уровни identity, никогда не глобальный IP dedup |
| R13 Считать сервисный homepage probe подтверждением звонков/видео | Проверяется другой протокол и маршрут | Отдельные bounded probes F20, без общего обещания |
| R14 Полный переписанный native UI и новая backend-платформа до функций | Высокая миграционная стоимость, уже есть работающий UI/движок | Thin host F23; переписывание только после доказанного технического ограничения |

## Как проверять спрос без обязательной телеметрии

Провести короткие наблюдаемые задания с представителями трёх ролей: свой файл с ошибками, 5 прокси без URL, две коллекции, «почему 0», подключение клиента, восстановление после сна. Фиксировать completion, вмешательства и места непонимания локально с согласия. Для F14 отдельно наблюдать один длительный сценарий с отказами и ограниченным бюджетом. Для X-направлений сначала проверить, что принятые функции не закрывают ту же потребность.

Решение «часто нужно» пока основано на характере задачи и текущем разрыве в коде. Оценки приоритета пересматривать после этих наблюдений, а не придумывать проценты спроса.
