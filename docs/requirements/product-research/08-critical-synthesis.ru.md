# Критическая обработка, доказательства и границы уверенности

## Как выполнен цикл

Запрошенный цикл Ultra Workflow выполнен через доступные встроенные исследовательские агенты и последовательный синтез. Отдельной вызываемой команды с именем Ultra Workflow/DWF в наборе инструментов этой сессии не было; запуск такого движка не заявляется. Соседний `.dwf.ts` другого исследования не запускался. Все требуемые стадии — исследование, альтернативы, независимая критика, проверка предположений и финальный синтез — выполнены, а результаты сохранены.

| Стадия | Действие | Материал |
|---|---|---|
| Фиксация фактов | HEAD, dirty state, hashes tracked файлов; чтение AGENTS/кода/тестов/документации без запуска | `evidence/baseline-manifest.json`, `02-capability-matrix.ru.md` |
| Независимое исследование 1 | Capability/data/import/source аудит с символами и строками | `evidence/audit-code.md` |
| Независимое исследование 2 | Персоны, три модели продукта, альтернативные приоритеты и сценарии | `evidence/product-alternatives.md`, `05-user-scenarios.ru.md` |
| Независимое исследование 3 | Надёжность, API/gateway/desktop и официальные подходы зрелых инструментов | `evidence/systems-research.md` |
| Первый синтез | 28 функций, единая модель, R1 из пяти карточек | `01`, `03`, `04` |
| Независимая критика | Три разных reviewer: технические edge cases, факты/недублирование, пользовательская ценность/объём | `evidence/review-systems.md`, `evidence/review-code.md`, `evidence/review-product.md` |
| Исправление и повторная сверка | Закрытие 14 системных замечаний как плана, точечные factual corrections, уменьшение MVP | `evidence/review-closure-systems.md`, итоговые `01/04/06` |
| Стыковка внешнего процесса | Прочитан появившийся предварительный source catalog; metadata использованы, глобальный поиск не повторён | `evidence/source-research-handoff.ru.md` |
| Передача реализации | Явный release cut, dependencies, acceptance и prompt с обязательной пересверкой | `06`, `07` |

Исследовательские ветки и review в `evidence` сохраняют первоначальные альтернативы и замечания; они не являются параллельными утверждёнными backlog. **Итоговое решение — в `01–07`, особенно `06`.** REVIEW PASS означает согласованность плана, не готовность кода и не выполненные release tests.

## Что проверено и каким способом

- Реализация возможностей прослежена статически по исходникам, включая tests как описание замысла. Приложение, scanner, тесты, реальные БД и публичные прокси не запускались/не открывались.
- Утверждения README перепроверены: найденные источники истины — normalizer, schema, actual orchestration, filters, generator/consumer paths. Старый backlog не принимался за факт отсутствия функции.
- Официальная документация использована для ограничений и подходов, без копирования целых продуктов. Поддержка HTTPX proxy auth не доказывает готовность всего проекта; документы TUN не являются аргументом добавить VPN.
- Изменения другого workflow были видны прямо во время чтения. UI улучшения помечены WIP; финальное сравнение hashes показывает сдвиг снимка, а не наши изменения в репозитории.
- Инструмент чтения соседней задачи вернул сведения о ней, но не текст предыдущего аудита. Поэтому не приписывается использование недоступных выводов прежнего чата. Локальный source catalog прочитан отдельно по мере появления.

## Реестр ключевых предположений

| Предположение | Вывод проверки | Основание | Решение / будущая проверка |
|---|---|---|---|
| Базовая проверка без URL отсутствует | **Опровергнуто** | `target_config` default; GUI defaults | F01 развивает имя/контракт, не создаёт заново |
| Нужно с нуля делать countries, parallel, API, gateway и exports | **Опровергнуто** | Прослежены код и тесты всех путей | Reuse, добавить недостающий scope/semantics |
| Свой файл уже изолирован при `--no-sources` | **Опровергнуто** | Общая candidates schema, scan по БД | F02; local mock public+A+B |
| Достаточно разрешить userinfo в normalizer | **Опровергнуто** | Hostname reject; serializers, gateway handshake, raw GUI inputs | F04 end-to-end, не parser-only |
| Watch держит N | **Опровергнуто** | Recheck только passing; desired-state loop отсутствует | F14 после jobs/admission |
| Больше workers всегда ускоряет полезный поиск | **Не доказано; механизм ограничивает** | Sequential collect/scan, resource cap/rate | F12 benchmark time-to-first/useful-N, не обещание процента ускорения |
| Service label означает полноценную работу приложения | **Опровергнуто** | Telegram homepage, Discord discovery, YouTube 204, OpenAI 401 | F06/F20 facet labels и раздельные engines |
| Sticky гарантирует fixed exit IP | **Опровергнуто** | `Pool.pick` хранит endpoint, failover допускается | F16 strict/failover, без fixed-IP promise |
| Страна endpoint равна стране выхода | **Опровергнуто как общее правило** | Отдельный exit judge; selection по endpoint | F08 separate fields/unknown, не скрытый probe при GET |
| ASN hosting heuristic доказывает residential | **Не доказано** | Regexp по имени организации, GeoIP limitations | Показывать как эвристику, не certification |
| Редкость адреса в списках доказывает меньшую загрузку | **Не доказано** | Формула ranking использует rarity, измерения чужого спроса нет | Label experimental, оценить predictive value по будущим данным |
| Текущий source score — объективный вклад | **Опровергнуто** | First attribution + export-filter bias | F13/F21 сравнимый denominator/family/marginal |
| Последний export обязательно свежий и согласованный | **Опровергнуто по коду** | Отсутствие TTL, отдельные rows/status, cache invalidation gap | F09/F28; synthetic publication fault/TTL tests |
| SQLite resume равен восстановлению recheck/sleep | **Частично, остальное не подтверждено** | DELETE перед recheck, history in memory, нет network epoch | Узкая сохранность в R1; полный F11 позже |
| Смена secret ref data не требует новых probes | **Опровергнуто логикой доступа** | Новый пароль/права — иное проверяемое условие | access_revision, invalidation, local auth mocks |
| Полный native desktop нужен до пользы | **Не подтверждено** | Browser/CLI/Windows packaging уже доступны | Thin shell после engine recovery; Mac .app отдельно |
| BYOP важнее public maintained pool для большинства | **Открытая продуктовая гипотеза** | Реальный кодовый gap есть; интервью/usage нет | R1 chosen bet, наблюдаемые задания трёх ролей; разрешён иной приоритет при данных |
| Любой контрольный probe доказывает клиент подключён | **Опровергнуто** | Проба Workbench проверяет свой route, не настройки клиента | Actual client request отдельно от control route |
| Можно выдать pass для пустого all/none профиля | **Отвергнуто как ошибка дизайна** | Vacuous truth против смысла проверки | ≥1 effective successful probe, K≥1; collect отдельно |
| 300 секунд — измеренный оптимальный TTL | **Нет, предлагаемая гипотеза** | Полевых данных нет | Видимый default, менять по наблюдениям; не гарантия работоспособности |

## Что изменилось благодаря независимой критике

| Замечания | Исходный риск | Принятое изменение |
|---|---|---|
| SR01, product C03 | Scope был в списке, но gateway продолжал бы читать global latest | Binding collection/profile/policy обязательна R1; B не переключает A |
| SR02, R-C01/02, product C04 | Current/history обещали раньше сохранного recheck | Non-destructive write и fixed input в R1; measurement selection отделена от admission |
| SR03/04/07, product C05 | Secret leaks, старый pass для нового пароля, секрет только в GUI памяти | Sanitized provenance, access revision, чёткий IPC/session lifetime, ordinary JSON vs explicit secret export |
| SR05/06 | «Одна транзакция» не охватывает vault; stale preview/replace гоняется с job | Staging/compensation, import ID, collection revision; R1 откладывает commit активной коллекции |
| SR08/09 | Hostname обходил public guard; TTL считался только при reload | Connect-time IP policy; per-row TTL каждое read/new connect; explicit established-flow policy |
| SR10 | Pools→scheduler→tray→pools давал цикл | Hard budgets внутри F14; scheduler и tray необязательны для работы открытого приложения |
| SR11/12 | Lossless downgrade и «проверено в текущей сети» были слишком сильны | Pre-migration restore snapshot, new DB отдельно; age/conditions до network epoch; clock anomaly→uncertain |
| SR13, R-C03 | All(empty)/K=0 мог дать рабочую выдачу | Непустое доказательство; none требует meaningful mandatory |
| SR14, product C06/07 | «Все форматы+auth» раздувало R1 и противоречило совместимости | TXT/URI, public endpoints, один direct формат+gateway; empty DIRECT change явно документирован |
| R-C04/05/06 | Неточные current threshold/test и target auth scope | Один общий threshold; test positional-only; target API auth вне R1 |
| R-C07/09/10 | Read filter мог запустить probe; control route путался с client; HTTPS считался новым | Pure read, отдельный measurement job; actual-client verification; HTTPS checker reuse/gateway parity |
| Product C01/02 | Необоснованный массовый auth-first и незаконченный последний шаг | Честная аудитория BYOP, XL, два gate R1a/b; copy/test/disconnect recipe в R1 |
| Product дальнейшие уточнения | F18 выпал из этапов; legacy TTL/переимпорт пароля не определены | F18 в R2; legacy pinned binding/max_age policy; explicit update-access vs separate-access choice |

После повторного systems-review P1 замечаний к согласованности плана не осталось. Неблокирующее замечание об единицах budgets также учтено: проверочный payload учитывается отдельно от relay/billable traffic, in-flight остаток ограничен. Редакционные дубли удалены. Остаточные фактические уточнения отражены в итоговых документах и дополнительной code-review сверке.

## Осознанные продуктовые решения

**Принят BYOP-inclusive R1, но не parser-only auth.** Ветка «сначала public pool» имела основание: watch действительно сужается. Она отложена потому, что требует тех же scope/freshness/recovery, а auth+hostname открывает новый пользовательский вход. Если проверка спроса покажет обратный приоритет, R1a остаётся полезной общей основой и не теряется.

**Полная basic-инфраструктура отложена до решения probe governance.** Режим без ввода URL уже есть. Производственное обещание нельзя строить на случайном demo endpoint. Это не повод блокировать собственный список с существующим явным target; новые режимы должны честно показывать, что именно измерено.

**Нет обязательного облака, VPN/TUN и мобильного data plane.** Сценарии управления и подключения разобраны; эти расширения имеют самостоятельную стоимость и отдельные experiments. Минимум текущего продукта — общие правила и пригодные прокси, а не универсальный сетевой стек.

**Scope, evidence и lifecycle раньше косметики.** Новые country chips/row actions другого процесса не отвергаются: их надо принять и проверить, а не переделывать. Их наличие не заменяет изоляцию и корректный admission.

## Официальные источники сравнения

Документы прочитаны 25.09.2026. Ни один из них не подтверждает готовность новой реализации Workbench.

| Первичный документ | Как использован |
|---|---|
| [IANA Example Domains](https://www.iana.org/help/example-domains) | Ограничение production зависимости от default example.com |
| [HTTPX Proxies](https://www.python-httpx.org/advanced/proxies/) | Транспорт поддерживает proxy auth; ограничение проекта шире одного parser |
| [curl manual](https://curl.se/docs/manpage.html) | DNS mode и proxy auth должны быть явными, безопасные recipes |
| [Mihomo proxy providers](https://wiki.metacubex.one/en/config/proxy-providers/) | Раздельные refresh inventory и health-check policies |
| [sing-box TUN](https://sing-box.sagernet.org/configuration/inbound/tun/) | Самостоятельная сложность routes/DNS/OS integration |
| [Chrome proxy API](https://developer.chrome.com/docs/extensions/reference/api/proxy) | Permissions/effective owner/PAC fallback учитываются в extension experiment |
| [Apple SMAppService](https://developer.apple.com/documentation/servicemanagement/smappservice/register%28%29) | Видимая user-controlled background registration, не скрытый daemon |
| [Apple Keychain Services](https://developer.apple.com/documentation/Security/keychain-services) | Платформенный секретный storage, отдельно от redaction/logging |
| [Microsoft CryptProtectData](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata) | OS-bound secret protection не равно переносимому backup |
| [DB-IP Lite](https://db-ip.com/db/lite.php) | Geo data coverage/accuracy/обновление/attribution, сохранение unknown |

Подробные короткие выводы и ограничения — в [systems-research](evidence/systems-research.md). Из документов взяты принципы применимости, а не чужой список функций целиком.

## Что остаётся неизвестным

Реальная конверсия первого запуска, доля пользователей собственных поставщиков, подходящие defaults TTL/budget, доступность production probe operator, выбранный стек OS vault/host, поддержка конкретных release artifacts/подписей и фактическое качество public proxies. Нужны будущие локальные integration tests и наблюдения сценариев; пока ни сроки, ни speedup, ни SLA не обещаются.

Финальные материалы — завершённый продуктовый штурм и задания разработки. Они не означают, что новые функции реализованы. Исследование не изменяло приложение, настройки или БД; все собственные файлы записаны в отдельный каталог исследования.
