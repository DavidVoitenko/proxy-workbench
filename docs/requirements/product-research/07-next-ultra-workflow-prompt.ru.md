# Готовый промпт для следующего Ultra Workflow реализации

Текст ниже предназначен для отдельного следующего задания. Его создание сейчас не разрешает менять код в текущем исследовательском задании.

---

Запусти полный многоагентный цикл Ultra Workflow для реализации следующего функционального релиза Proxy Workbench: **«Свои списки, проверяемый результат, законченное подключение»**.

Используй доступные встроенные средства многоагентной работы: независимый аудит, сравнение вариантов реализации, bounded параллельные задачи с владельцами файлов, независимую критику, проверку на локальных стендах и итоговый синтез. Не выдавай недоступный режим/инструмент за запущенный. Не ограничивайся отчётом: после пересверки доведи согласованный срез до работающего результата.

## 0. Сначала перепроверь проект ПОСЛЕ уже работающего workflow

Репозиторий: `/Users/main/Desktop/111/proxy-workbench`.

Материалы продуктового исследования: `/Users/main/Desktop/111/proxy-workbench-product-research-2026-09-25/`.

Сначала прочти `AGENTS.md`, актуальные изменения, текущие tests, READMEs, release/build configuration, затем материалы `00-START-HERE.ru.md`, `02-capability-matrix.ru.md`, `04-feature-cards.ru.md`, `06-roadmap-and-release.ru.md`, `08-critical-synthesis.ru.md`. Текст исследования — план и снимок 25.09.2026, не абсолютная истина о будущем состоянии кода.

Проверь, завершён ли предшествующий workflow, какие его изменения уже приняты и какие процессы ещё работают. Не останавливай их, не меняй их data/settings и не перезаписывай рабочие файлы. Если owner/status неясен, продолжай read-only аудит и подготовку независимых материалов; изменение пересекающихся файлов начинать только при установленной безопасной границе. Следуй актуальному AGENTS относительно ветки/коммитов, не делай reset/clean чужих изменений.

Baseline исследования был `e4bf03cbec4dc478b6c82984726456149e30bb42`, но уже во время исследования менялись GUI/UI. Тогда WIP добавлял country search RU/EN, multi chips, service chips, row test/copy/download/denylist, import conveniences. Перепроверь их полный путь, включая frontend/backend contracts. Нельзя создавать их повторно или заменять свежую работу старым UI из материалов.

Сделай компактную delta-таблицу по F02/F03/F04/F09/F28: **готово и reuse / частично и точный gap / отсутствует / зависит от ещё идущей работы**. Ссылайся на актуальные file:line/символы и тесты. Если функция уже реализована, сними её из реализации, сохрани только недостающую интеграцию и проверку. Не копируй старые статические подозрения как новые баги без повторной проверки.

Не реализуй заново сбор источников, parallel checking, find-N, GeoIP/ASN, service presets, API/get/test, gateway/rotation/sticky, watch, speedtest, exports — они существовали. Проверяй реальный смысл: watch не обязательно поддерживает N; service HTTP probe не доказывает работу приложения; OS packaging-файл не доказывает проверенную установку.

## 1. Целевой законченный пользовательский путь

Создать отдельный список → импортировать TXT/URI из файла/буфера с preview → использовать hostname и HTTP/SOCKS5 proxy authentication → проверить выбранную коллекцию существующим профилем → увидеть evidence и возраст → подключить через действующий локальный gateway либо один поддержанный внешний формат.

Публичная база не подмешивается в свой список. Другая проверка не переключает чужой gateway/API binding. Старый успех не выдаётся за новый. Нельзя отключать TLS, раскрывать пароль или молча использовать DIRECT ради успешного результата.

Реализуй MVP пяти карточек F02/F03/F04/F09/F28 по `04` и финальным уточнениям `06`. При конфликте раннего evidence и итогового плана приоритет имеет `06` плюс актуально проверенный код; противоречие явно разреши до реализации.

## 2. Два внутренних acceptance gate

**R1a — scope и доказательства.** Коллекции/membership/access IDs; миграция legacy; TXT/URI preview и merge/replace; scoped check/results/export; freshness и причины; неразрушающий recheck; согласованные generations; безопасный empty-state. Используй существующие immutable profile hashes/targets, без обязательной полной библиотеки профилей.

**R1b — авторизованные прокси end-to-end.** Hostname, HTTP Basic proxy auth и SOCKS5 username/password; secret references/revisions; OS vault/сеансовое хранение; checker и gateway; одна стабильная scope/profile/policy binding; один выбранный прямой внешний формат с доказанной compatibility. Для остальных новых типов — явный unsupported, не тихое урезание.

Релиз можно объявить поддерживающим авторизованные собственные прокси только после R1b. Не прекращай работу после R1a, если исходный объём остаётся выполнимым; если появляется реальный blocker, сообщи точно что готово и что препятствует следующему gate.

Вне объёма: CSV column mapper, private endpoints, provider subscription APIs, новый поиск источников, named maintained pools, полный job scheduler, all/any/K, WS/media, tray/autostart/native macOS app, browser extension, cloud/mobile/VPN/TUN. Не добавляй эти функции ради полноты исследования.

## 3. Обязательные контракты

- Endpoint = protocol/host/port; access = endpoint + credential reference/revision + provider parameters. Collection membership many-to-many. Один IP не равен одному endpoint, access или exit IP.
- Новое/отредактированное credential/access revision требует нового доказательства пригодности; секрет и его хеш не часть публичного ID. Повторный импорт того же endpoint/auth/username/provider parameters не перезаписывает секрет и не создаёт дубликат молча: preview предлагает обновить существующий access с revision++ либо создать отдельный; тот же import ID остаётся идемпотентным.
- Preview не пишет. Commit имеет import ID, collection revision/CAS и компенсируемое согласование vault/SQLite. В R1 можно запрещать commit в коллекцию с активной проверкой; stale preview требует обновлённого diff. Повтор commit не создаёт дубликаты.
- Не сохраняй raw credential URI в gui-settings, gui-input, SQLite, logs, exception, argv, ordinary API или support bundle. Provenance — очищенные поля/line ID. Явно заказанный внешний secret export — отдельное исключение с понятным действием пользователя.
- Session-only secret имеет определённый процесс/broker lifetime. Отдельный worker/gateway получает его через OS adapter или ограниченный локальный IPC; текущий `stdin=DEVNULL` не заменяет проектирование передачи. После завершения сеанса secret_unavailable, без plaintext fallback.
- Hostname проверяется по фактическим IP перед каждым соединением/до auth; DNS fallback не обходит public-address policy R1. Public-source fetch guards не ослабляются.
- Job input scope/profile фиксируются. Неразрушающая перепроверка сохраняет старый completed результат до нового. Fresh failure подавляет old success; прерванная проба не выдумывает новый успех/отказ.
- Admission для consumers и выбор для recheck — разные операции. Stale/failed можно явно перепроверить, даже если они исключены из рабочей выдачи; старый done marker не блокирует новый запуск.
- Freshness проверяется по каждому наблюдению при каждом select/new gateway connect, даже без изменения generation. Стартовая гипотеза max_age=300 s видима/настраиваема, не называется гарантией. Clock rollback/future timestamp → uncertain. До OS network epoch нельзя обещать пригодность в текущей неизвестной сети.
- Обычный expiry запрещает новые соединения; established TCP не мигрирует и не обязан обрываться только из-за TTL. Revoke/смена доступа — отдельная явная policy.
- Gateway/API binding фиксирует collection/profile/selection-policy; другая проверка его не заменяет. Legacy unqualified `/proxies`, `/random` и `get` фиксируют binding к «Ранее собранные»/прежнему profile digest, при отсутствии данных — empty. Default max_age=300 s и allow_stale=false един для выдачи; явно запрошенная помеченная history не влияет на gateway admission. Изменение default документируется. Legacy endpoints не начинают раскрывать новые private collection. Username-фильтры только сужают scope.
- Manifest/current pointer связывает rows/status/scope/profile в одно поколение. Clear/remove/revoke инвалидирует caches; bounded last-known-good допустим только с явным degraded state и реальной freshness. У static TXT нет автоматического TTL enforcement.
- Форматы сохраняются для старых поддержанных unauth rows. Отмена empty DIRECT — намеренное, явно документируемое изменение. Новый unsupported формат должен сообщить причину, не терять credentials/TLS/DNS/IPv6 молча.
- До schema migration — согласованная техническая backup и rollback plan. Rollback восстанавливает pre-migration snapshot, новую БД сохраняет отдельно; это не lossless down-conversion новых auth данных. Старый binary не пишет в новую schema.

## 4. Организация реализации

После актуального аудита сравни минимум два небольших варианта модели данных/границ общего selector; выбери тот, который переиспользует текущий engine и минимизирует миграции. Не переписывай приложение на другой стек без доказанного ограничения.

Параллельные агентские задачи разрешены только после согласования схемы и владельцев файлов. Например: один агент data/import, другой transports/secret adapter, третий consumer contracts/fixtures; интегратор отвечает за общий admission и сквозной сценарий. Не поручай нескольким агентам одновременно менять один монолит без границы.

Отдельный reviewer не должен быть автором проверяемого пути. Проверить изоляцию collections, revision invalidation, vault/DB failure, DNS rebind, mixed-age snapshot, stop во время recheck, stale cache после clear и отсутствие secret leakage. Возражения превратить в проверки или обоснованные решения, не просто перечислить.

Результаты исследования источников из соседней папки разрешается читать, если они готовы. Их каталог не является частью R1 и не должен запускать новый скан или автоматически менять sources.json.

## 5. Верификация — разрешена только изолированно

Для ЭТОГО будущего задания реализации разрешены необходимые локальные тесты: временные каталоги данных, synthetic fixtures и local mock proxy/HTTP/SOCKS/DNS/TLS endpoints. Не использовать реальные пользовательские БД, действующие настройки, реальные proxy credentials и публичные proxy endpoints. Не выполнять массовые сетевые проверки и не измерять чужие сервисы без отдельного прямого запроса.

Проверь как минимум:

1. Public + две private коллекции, общее membership и два аккаунта одного endpoint; scan/export/binding не смешиваются.
2. Preview valid/duplicate/invalid, cancel, conflict revision и crash между vault staging/DB commit/finalize; повтор commit.
3. HTTP Basic/SOCKS5 правильный/неправильный пароль, hostname A/AAAA, недоступный vault, ephemeral lifetime, rotation того же ref.
4. Canary secret отсутствует в managed files/DB plaintext/logs/argv/обычном API. Только явный выбранный secret export содержит ожидаемые поля.
5. Recheck старого/failed набора действительно делает новую пробу; stop на середине сохраняет прежние завершённые observations; country/collection subset не стирает соседний scope.
6. Freeze/advance/rollback clock, mixed-age rows, неизменная generation и истёкший TTL; API/get/GUI/gateway одинаково допускают новые соединения.
7. Publish fault между файлами, current pointer missing/corrupt, clear после прогрева gateway; нет смешанных generations и бесконечного старого cache.
8. Empty и unsupported-only PAC/Clash/sing-box не дают скрытый DIRECT; выбранный внешний формат валидируется в закреплённой версии клиента.
9. Новый пользователь завершает GUI import→check→connect→test→disconnect instructions; control route Workbench отличается от подтверждения реального client request, а CLI/API для того же scope/config дают ту же семантику. Quick test не выдаёт свой короткий probe за полный profile pass.
10. Windows/macOS path/packaging regressions проверены доступным способом. Не объявляй прохождение ОС-тестов без запуска на соответствующей среде; фиксируй остающиеся условия выпуска.

Сначала тесты затронутых контрактов, затем требуемые репозиторием проверки. Не повторяй прошедшие тяжёлые тесты без новых изменений/оснований. Никакого изменения активного `data/` ради демонстрации.

## 6. Завершение

Обнови пользовательские инструкции и migration/release notes под фактическое поведение, особенно empty fallback, private scope, freshness и limitations поддержанных форматов. Не оставляй в README прежние более сильные обещания, которые код не доказывает.

Дай краткий итог по-русски: законченный пользовательский результат; что уже было и переиспользовано; что добавлено; какие проверки действительно прошли; ограничения/блокеры. Детали реализации и evidence сохрани отдельно. Не публикуй релиз, не изменяй production deployment и не отправляй сообщения другим людям без отдельного запроса.

Начинай с пересверки актуального состояния и исключения уже готовых функций.
