# План подключения источников Proxy Workbench

**Дата планирования:** 2026-09-25. **Срез доступности источников:** 2026-09-25T12:24:52Z.  
**Назначение:** спроектировать последующее подключение, не заменяя и не автоматически переписывая текущий `proxy_workbench/sources.json`. В изолированной рабочей копии уже находятся 55 URL; при подготовке этого уточнения файл не изменялся: `../proxy-workbench-sources/proxy_workbench/sources.json:1-57`.

## Как принимались решения

- **Формат** в исходном исследовании проверялся по извлечённым через AST функциям, а не по реальному `collect()`. В текущей рабочей копии поддерживаются `http`, `https`, `socks4`, `socks5`, `socks5h`, `auto`, `text`, `geonode`, `http-fields` (`../proxy-workbench-sources/proxy_workbench/proxytool.py:476-485`). Обычный список адресов проходит через `normalize()`: IP должен быть глобальным, порт обязателен, credentials/query/fragment/путь запрещены; явная схема авторитетна (`../proxy-workbench-sources/proxy_workbench/proxytool.py:309-327`). Настоящий интеграционный результат описан в § «Проверки, выполненные именно в этой работе».
- **A** означает, что источник можно начать собирать существующим `kind`, не ожидая изменения кода. Это не означает, что все метаданные сохраняются или что адреса рабочие. `text` может извлечь адрес из HTML/CSV, но назначает `http` по умолчанию и теряет поля (`../proxy-workbench-sources/proxy_workbench/proxytool.py:581-590`).
- **B** — нужен небольшой, общий парсер JSON/полей: не новый сетевой протокол и не новый источник данных. **C** — отдельный адаптер формата, HTML или API-пагинации. **D** — нельзя начать без решения владельца аккаунта/ключа или собственной инфраструктуры. **E** — формат/инструмент, который имеет смысл только экспериментировать. **F** — не подключать в текущем цикле.
- **Ни один адрес не проверялся соединением.** Исходный аудит прямо ограничился HTTP GET и не использовал прокси (`verification-log.md:3-9`, `verification-log.md:21-25`). Все утверждения о свежести ниже — только доступность, заголовки и timestamps в теле на указанный срез; ETag/Last-Modified сам по себе не означает свежие прокси.
- Число строк, число звезд и сам размер списка не были основанием приоритета. Приоритет определяется сочетанием **свежести, подтвержденного формата, разнообразия протоколов/географии, полезных metadata и отсутствия неразрешимого правового/технического барьера**.

### Почему 150 URL дают 152 позиции A–F

В таблицах разделов A–F находится 152 позиции: A — 75, B — 15, C — 11, D — 15, E — 8, F — 28. Это не 152 кандидата: `new-094` и `new-095` указаны и в E, и в F, то есть категории пересекаются в двух местах. После дедупликации остаётся ровно 150 уникальных ID, и все 150 записей `candidates.json` покрыты; ошибки подсчёта или пропущенных URL нет. A–F следует считать маршрутами с возможным пересечением, а не непересекающимся разбиением.

## A. Можно рассматривать для текущего сборщика как есть

Здесь «как есть» означает использование существующего `kind`; это не рекомендация немедленно заменить текущий каталог. Для всех `cur-*` с TXT применимы соответствующие `http`/`socks4`/`socks5` (или `auto` только для списка без схемы); HTML — только `text`, а `cur-55` — `geonode`.

### Уже присутствующие в текущем каталоге источники

| ID | Решение и приоритет | Ограничение |
| --- | --- | --- |
| `cur-01`, `cur-02`, `cur-03`, `cur-04`, `cur-05`, `cur-06`, `cur-07`, `cur-08`, `cur-09`, `cur-10`, `cur-11`, `cur-12`, `cur-13`, `cur-14`, `cur-15`, `cur-16` | Обычные публичные TXT/CSV-подобные выгрузки; `cur-04` отдельно даёт свежий HTTP-протокол и `Last-Modified` около времени среза. Это технически совместимый базовый HTTP-слой. | Формат без metadata; источник и текущий каталог не изменять этой работой. Факты доступности и ETag/Last-Modified: `verification-log.md:92-107`. |
| `cur-17`, `cur-18`, `cur-19`, `cur-20`, `cur-21`, `cur-22`, `cur-23`, `cur-24`, `cur-25`, `cur-26`, `cur-27`, `cur-28`, `cur-29`, `cur-30`, `cur-31`, `cur-32`, `cur-33`, `cur-34`, `cur-35`, `cur-36` | Публичные списки без аккаунта, с читаемыми адресами; у части наборы пересекаются, поэтому не расширять базовый набор только из-за количества. | `cur-19` и `cur-30` — HTTP/CONNECT-семантику хранить как `http`, а не выдумывать TLS-to-proxy. Пересечение само по себе не доказывает зеркало или происхождение. `verification-log.md:108-127`. |
| `cur-38`, `cur-39`, `cur-40`, `cur-41`, `cur-42`, `cur-43`, `cur-44`, `cur-45`, `cur-46`, `cur-47`, `cur-48`, `cur-49`, `cur-50`, `cur-51` | Публичные TXT-выгрузки; `cur-46` и `cur-51` закрывают SOCKS5/SOCKS4 и имеют `Last-Modified` непосредственно около среза. | Не делать вывод о работоспособности; наборы `cur-41`/`cur-43` совпали в срезе (21 036), поэтому они не дают независимого наблюдения именно в этом срезе, но общее владение или происхождение не установлены. `verification-log.md:129-142`, `overlap.md:12`. |
| `cur-52`, `cur-53`, `cur-54` | HTML с адресами, который уже совместим с `text`. | `text` допускает ложные числовые совпадения; сохраняется только адрес, а не HTML metadata. `verification-log.md:143-145`, `../proxy-workbench-sources/proxy_workbench/proxytool.py:675-679`. |
| `cur-55` | **Базовый приоритет:** GeoNode — уже реализованный адаптер с `data`, `page`, `total`, фильтрацией протоколов и `lastChecked`; покрывает HTTP/CONNECT, SOCKS4 и SOCKS5. | Не переносить геонодовский `https` в `https` transport: текущий код намеренно сохраняет его как `http` CONNECT (`../proxy-workbench-sources/proxy_workbench/proxytool.py:613-630`). `verification-log.md:146`. |

### Публичные новые источники, для которых уже достаточно текущих видов

| ID | Решение и приоритет | Ограничение |
| --- | --- | --- |
| `new-021` | Публичный `all.txt`; можно сразу попробовать `auto` для неразмеченных адресов. | Методика проверки не опубликована; это не доказательство живых адресов. `verification-log.md:167`. |
| `new-022` | Публичный `all.txt`; полезен как ещё один TXT-вход при несовпадении наборов. | Заявления verified/honeypot не считать результатом проверки. `verification-log.md:168`, `overlap.md:10`. |
| `new-029` | Выбранный `Countries/http/Afghanistan.txt` совместим с `http`; репозиторий actively pushed в день среза. | Не переносить HTTP-выгрузку на SOCKS без явного `kind`; гео-разбиение нужно расширять только после проверки фактического состава. `candidates.json:5248-5294`, `verification-log.md:175`. |
| `new-031` | `proxy.txt` читается как текст; hourly workflow/commit в день среза делает его хорошим кандидатом на наблюдение. | Это зеркало одного внешнего HTML-источника, а не независимая проверка. `candidates.json:5398-5442`, `verification-log.md:177`. |
| `new-033` | Страновой TXT выбранного HTTP-источника; обычный `http` без кода. | Не обобщать одну проверенную страновую ветку на весь репозиторий. `verification-log.md:179`. |
| `new-034` | Технически обычный TXT, но **расширенный, не базовый**: `Last-Modified` 2025-07-16. | Старое содержимое не повышает приоритет. `verification-log.md:180`. |
| `new-035` | Обычный SOCKS5 TXT; `socks5` даёт ещё один формат совместимости. | В источнике нет гарантии возраста IP; держать в расширенном наблюдаемом наборе. `verification-log.md:181`. |
| `new-036` | Страновой HTTPS/CONNECT-текст; подавать как `http`, как и текущий HTTPS-каталог. | Не выдавать это за отдельный TLS listener. `verification-log.md:182`. |
| `new-038`, `new-039` | Публичные TXT-зеркала, технически совместимы с `http`/`auto`; полезны только как контролируемое дополнение при измерении уникальности. | Высокое пересечение с уже известными семействами (`overlap.md:10`, `overlap.md:45-46`). |
| `new-040` | Технически `http` для HTTPS/CONNECT TXT. | `Last-Modified` 2025-11-10; не включать в базовый набор. `verification-log.md:186`. |
| `new-041` | Обычный HTTP TXT с большим ответом; может расширить coverage при контроле лимитов. | Не считать 417 совпадений доказательством качества; mirror-семейство пересекается с F01. `verification-log.md:187`, `overlap.md:9`. |
| `new-042` | `all.txt` без схем — естественный кандидат для `auto`; полезен как независимое семейство. | Проверка адресов не выполнялась; не включать по одному размеру. `verification-log.md:188`, `overlap.md:79`. |
| `new-043` | Крупный HTTP TXT, технически `http`; оставить только как optional fallback из-за пересечений с существующими большими списками. | В `verification.json` сжатый размер 2.1 МБ, а сохранённое распакованное тело — 13.2 МБ; loose-извлечение даёт верхнюю оценку 506 267 значений. До включения нужны явные byte/line/candidate/time budgets, а также решения по лицензии и пересечению. Это не показатель качества адресов. `verification-log.md:189`, `overlap.md:73-80`. |
| `new-044` | Обычный HTTP TXT; совместим без кода. | Не приоритетнее уже имеющихся HTTP-источников без подтверждения более высокой уникальности. `verification-log.md:190`, `overlap.md:18`. |
| `new-054` | HTTPS/CONNECT TXT, подавать как `http`; простой и наблюдаемый. | Не включать отдельную `https` transport-схему без доказательства её семантики. `verification-log.md:200`. |
| `new-056` | TXT без схем, можно `auto`; потенциально полезен для азиатского/глобального покрытия. | Название файла не доказывает географию и работоспособность; проверять источник и protocol split отдельно. `verification-log.md:202`. |
| `new-058` | **Расширенный приоритет:** публичный TXT с меткой времени и hourly claim; текущий `text` извлекает IP:port, хотя сохраняется только адрес. | Для `proxy.txt` использовать только HTTP-интерпретацию. `socks.txt` нельзя молча подавать тем же `text`: понадобится отдельный режим с протоколом (см. B). `candidates.json:7481-7536`, `verification-log.md:204`. |
| `new-060` | Выбранный HTTP TXT, наблюдаемый `Last-Modified` около времени среза; совместим с `http`. | На publisher/лицензию нельзя опираться без проверки; не включать в базовый набор только из-за 6235 regex-совпадений. `candidates.json:7687-7732`, `verification-log.md:206`. |
| `new-061` | Обычный HTTP TXT, заявленный hourly и недавно pushed. | Upstream и data license не раскрыты; использовать лишь после проверки политики. `candidates.json:7757-7802`, `verification-log.md:207`. |
| `new-067` | HTML country page, совместимая с `text`; технически доступна с адресами. | Пересекается с `cur-54`; дублировать только при измеримой добавочной ценности. `verification-log.md:213`, `overlap.md:17`. |

## B. Полезны после небольшого изменения парсера

Общий адаптер должен читать **только IP/port/протокол/country** и отбрасывать остальные поля на первом этапе. Он не должен угадывать protocol, брать `exit_ip` вместо адреса или сохранять credentials. Нынешняя ветка `geonode` не является общим JSON-парсером: она жёстко читает `data`, `ip`, `port`, `protocols` и страницу (`../proxy-workbench-sources/proxy_workbench/proxytool.py:610-630`, `../proxy-workbench-sources/proxy_workbench/proxytool.py:662-674`).

| ID | Что нужно изменить | Приоритет после проверки |
| --- | --- | --- |
| `new-008` | JSON records и общий parser; сохранить только канонический адрес и разрешённый протокол. | Высокий в extended: насыщенные metadata и текущие timestamps, но нужен schema contract. |
| `new-009` | Root object с `proxies`, записи `ip/port/protocols`; использовать зеркало без auth. | **Базовый после B:** комбинация протоколов, timestamp и явные metadata; учитывать противоречие 100 calls/day против 2000 в документации и CC BY attribution. `candidates.json:3516-3562`. |
| `new-023` | JSON array `ip/port`; протокол не угадывать без source profile. | Расширенный, только если профиль источника подтверждает default. |
| `new-024` | JSON array `protocol/ip/port`; сохранить protocol и country. | **Базовый после B:** keyless live export, record-level fields и timestamp/latency, сильнее обычного TXT. `candidates.json:4935-4985` описывает dataset; фигура payload подтверждена локальной проверкой ниже. |
| `new-026` | Настоящий CSV: `ip,port,protocol,country,...`; `text` способен извлечь адреса, но назначит всем `http`. Нужен delimiter-aware mapping для protocol/country. | Расширенный: CC0 и разнообразие протоколов, но update claim «daily/weekly» не разрешён. `candidates.json:5025-5067`. |
| `new-032` | JSON array с `ip/port/protocol`, timestamp/metadata. | Высокий extended: полезны country/latency, но источник сам предупреждает волатильность. `candidates.json:5466-5504`. |
| `new-048` | JSON array `ip/port/type`, затем приведение `type` к разрешённым protocol. | Высокий extended: ежедневная/шестичасовая проверка и ASN/latency, но data license отдельно не подтверждена. `candidates.json:6668-6754`. |
| `new-055` | JSON object `countryCode/country/proxies`; country normalise только к ISO alpha-2. | Расширенный: удобная страна, но не приоритет без подтверждения schema и refresh. |
| `new-057` | JSON `data` с `ip/port/countryCode`; протокол не заполнять догадкой. | Расширенный: потенциально полезен, но нужен собственный mapping страна/пагинация. |
| `new-074` | JSON array `proxy/protocol/ip/port`; это structured mirror уже подключённого proxifly. | Высокий extended только как способ получить metadata/согласованный feed; не добавлять как независимый источник. `candidates.json` entry до `8669`; текущая TXT-версия уже в `sources.json`. |
| `new-075` | JSON/CSV records `protocol/ip/port`; Unix-float `last_checked` и ASN. | Высокий extended: timestamps и metadata лучше простого TXT, но это mirror текущего репозитория. `candidates.json:8672-8733`. |
| `new-077` | Records содержат `protocol/host/port`, **отдельный `exit_ip`**, nested ASN/geo. | Расширенный: полезен для обогащения, но нельзя подставлять `exit_ip` вместо `host`; `username/password` должны отбрасываться. `candidates.json:8848-8905`. |
| `new-079` | Records `address/proxy_type/latency_ms/checked_at`; разобрать склеенный `address`. | Высокий extended: в теле есть `checked_at`, но набор 2 037 адресов совпал с `cur-11` в срезе 2026-09-25; это равенство множеств не доказывает копирование или происхождение. `candidates.json:9008-9046`. |
| `new-080` | Object keyed by `http/https/socks4/socks5`, record `proxy/country_name`; протокол берём из ключа, а `country_name` считать кодом только после валидации. | Расширенный; полезна структура, но many records are likely repetitive IP blocks. `candidates.json:9079-9122`. |
| `new-081` | Object keyed by protocol, values arrays of `ip:port` (в sampled JSON — строки). | Расширенный: удобен multi-protocol, но нужен лимит на размер и контроль дубликатов. `candidates.json:9147-9168`. |

## C. Требуют отдельного адаптера

| ID | Отдельный адаптер и причина | Приоритет |
| --- | --- | --- |
| `new-010` | HTML tables + country/protocol query; regex `text` не сохраняет классы/фильтры и может вытащить случайные числа. Нужен DOM/table parser с явной пагинацией. | Extended после проверки terms; гео-разнообразие полезно. `candidates.json:3668-3718`. |
| `new-011` | HTML country tables, Last Check/Uptime/Speed, без документированного export. | Extended: свежие claim и geography, но нет машинного контракта. `candidates.json:3743-3793`. |
| `new-012` | HTML protocol/country browser без известного refresh и безTerms. | Experimental until rights/refresh confirmed. `candidates.json:3818-3859`. |
| `new-016` | HTML table можно извлечь `text`, но для country/anonymity/latency и стабильной структуры нужен table adapter. | Не включать до terms review. `verification-log.md:162`. |
| `new-045` | **Отдельный page-JSON adapter:** `data/page/limit/total`, `protocol` scalar, `iso/country`; generic geonode не подходит. Нужны page loop, `total`, empty/repeat guard, record limit. | **Базовый после C:** keyless, `Last-Modified` около 12:24 UTC, metadata; не переиспользовать имя `geonode` автоматически. `candidates.json:6513` и локальный fixture output ниже. |
| `new-050` | HTML list с пагинацией (`start=64`, 260 pages) и protocol/country controls. Нужны page bounds и DOM extraction. | Extended, если подтверждены terms и page cadence. `candidates.json:6916-6964`. |
| `new-051` | HTML protocol tables; простой `text` теряет protocol, а глобальный footer update не per-record. | Extended только после HTML adapter; не смешивать SSL/HTTPS с TLS transport. `candidates.json:6989-7008`. |
| `new-063` | Page-JSON `data/total/page/limit`, но `protocols` надо извлекать отдельно; generic geonode ожидает `ip/port/protocols` и не примет record mapping как есть. | Высокий extended после C: `lastChecked`/latency, но нет подтверждённой protocol policy. `candidates.json:7886-7921`. |
| `new-064` | JSON envelope с `table_html/pagination_html`; нужен bounded HTML parser и проверка `page/totalPages`. Нельзя пускать `text` по JSON/HTML и считать все числа адресами. | Высокий experimental: нет terms/refresh contract. `candidates.json:7946-7971`. |
| `new-065` | Goodips HTML table; нет ключа, но нет и устойчивого machine-readable API/terms. | Experimental; сначала получить rights/terms и DOM fixture. `verification-log.md:211`. |
| `new-066` | Kuaidaili HTML list; нужен отдельный parser китайской HTML-таблицы, не regex. | Experimental; без terms и независимого freshness contract. `verification-log.md:212`. |

## D. Требуют пользовательского аккаунта или ключа

**Правило:** такие записи не надо добавлять в `sources.json` с придуманными credentials. URL пользователя запрещает логин/пароль, а HTTP-запрос сейчас не получает authorization headers (`../proxy-workbench-sources/proxy_workbench/proxytool.py:113-131`, `../proxy-workbench-sources/proxy_workbench/proxytool.py:219-231`). Карта/ключи должны жить в отдельном секрет-хранилище, если формат поддержка будет одобрена.

| ID | Барьер и что нужно до интеграции |
| --- | --- |
| `new-001` | Webshare: free account, 10 shared datacenter proxies и 1 GB/month; API entitlement для free tier не подтверждён. Нужны аккаунт и решение, покупать ли API. |
| `new-002` | Bright Data proxy products: отдельный temporary $2 trial; $5 bonus требует payment method, KYC для residential недоступна в Limited Trial. Нужна оплачиваемая/ограниченная trial-политика, не free proxy pool. |
| `new-003` | Decodo: temporary 3-day/100 MB trial; нужен аккаунт, payment method по договору и явное решение об auto-conversion. |
| `new-004` | IPRoyal: нет подтверждённого free tier; заявлен только неопределённый limited trial после проверки личности/company registration/payment method. |
| `new-005` | SOAX: нет free credits; Sandbox — 0 credits и top-up. Не начинать integration без оплаты. |
| `new-006` | Oxylabs: temporary trial, но inventory выдаётся через dashboard, не подтверждён анонимный feed. Нужна учётная запись и проверка trial mechanics. |
| `new-007` | ProxyRack: $5 paid 3-day test; не free trial. Нужна платная активация и API entitlement. |
| `new-015` | Proxy-Daily: free table пуста, login/register и javascript-заглушки; endpoint/direct list entitlement не подтверждены. |
| `new-047` | Proxy11: обязательный query key; максимум 50 записей в free response, частота/permanent lifetime не доказаны. Нужна регистрация, key storage, а также решение о no-redistribution terms. |
| `new-068` | 3proxy: нужен собственный VPS и закрытый/authenticated listener; конфиг-файл сам не является feed. |
| `new-069` | Tinyproxy: нужен VPS/оплаченный host; сам software не предоставляет IP pool. |
| `new-070` | Squid: нужен VPS; CONNECT не создаёт новый exit IP. |
| `new-071` | Xray-core: нужны VPS, runtime configuration и credentials; public open inbound запрещён документацией без защиты. |
| `new-072` | WireGuard — VPN peer, не HTTP/SOCKS feed; нужен отдельный routing/proxy runtime. Не включать как источник адресов. |
| `new-082` | `jhao104/proxy_pool` — локальный API на `127.0.0.1:5010`; требует own Python/Redis/Docker/API server. В текущем срез не отвечает, а private-source bypass предназначен только для mock (`../proxy-workbench-sources/proxy_workbench/proxytool.py:184-216`, `../proxy-workbench-sources/proxy_workbench/proxytool.py:1615-1616`). |

## E. Экспериментальные

Здесь полезность не доказана, но формат может быть отдельным будущим продуктом. Нельзя выдавать наличие конфигурации за наличие прокси-IP.

| ID | Гипотеза для отдельного эксперимента | Условие допуска |
| --- | --- | --- |
| `new-030` | Sing-box JSON может содержать outbounds, но это конфигурация, а не гарантированный inventory. | Выбрать конкретный public config, проверить license и извлекать только HTTP/SOCKS server+port с утверждённой семантикой. |
| `new-089` | Mihomo/Clash provider YAML — отдельный schema adapter. | Сначала найти конкретный provider URL; текущая запись — документация, не feed. |
| `new-090` | V2Fly JSON client/server configuration. | Нужен публичный fixture и server inbound; конфигурация сама не даёт endpoint. |
| `new-091` | sing-box mixed inbound/outbound. | Нужны runtime, credentials и отдельный parser; `mixed` — локальный listener, не free proxy inventory. |
| `new-092` | Sub-Converter base64/mixed output. | Сначала определить точный source-to-target contract; сам инструмент не создаёт proxy nodes. |
| `new-093` | Sub-Store subscription manager. | Нужен конкретный subscription feed; сейчас нет подтверждённого URL и rights. |
| `new-094` | Hiddify compatibility page. | Нет подтверждённого публичного feed; не интегрировать. |
| `new-095` | v2rayN base64 subscription documentation. | Нет подтверждённого public payload; не интегрировать. |

## F. Устаревшие, неподтверждённые или отклонённые — с причиной

| ID | Причина отказа в текущем цикле |
| --- | --- |
| `cur-37` | **Пустой на момент проверки 2026-09-25T12:23:58Z:** HTTP 200, 0 байт, 0 строк, 0 адресов. В текущем цикле запись не использовалась как источник данных, но это не доказательство окончательной недоступности; нужен повторный запрос. `verification-log.md:64-65`. |
| `new-013` | Redirect на коммерческий PX6; анонимного списка нет. `candidates.json:3887-3908`. |
| `new-014` | Заявление hourly не соответствует последнему видимому commit 2023-05-05; не приоритетный snapshot. `candidates.json:3942-3977`. |
| `new-017` | HTTP 403. `verification-log.md:66`. |
| `new-018` | HTTP 404. `verification-log.md:67`. |
| `new-019` | HTTP 403. `verification-log.md:68`. |
| `new-020` | HTTP 502, Retry-After 60. `verification-log.md:69`. |
| `new-025` | Явно historical snapshot 2026-09-15, не live feed; полезен только для offline research. `candidates.json:4935-4991`. |
| `new-027` | Kaggle catalog copy, exact anonymous data download не подтверждён. `candidates.json:5100-5154`. |
| `new-028` | Kaggle files от 2023, нет cadence/licence, verified HTML — без адресов. `candidates.json:5183-5219`. |
| `new-037` | HTTP 404. `verification-log.md:70`. |
| `new-046` | Технически keyless, но Terms ограничивают copying/distribution/redistribution lists; для проекта, который хранит и выдаёт адреса, нужен written permission. `candidates.json:6516-6564`. |
| `new-049` | Web snapshot содержит данные, но terms Socks5Proxies запрещают automated scraping/harvesting; не подключать без отдельного разрешения. `candidates.json:6827-6876`. |
| `new-052` | Redirect и HTML без regex-адресов. `verification-log.md:76-86`, `verification-log.md:198`. |
| `new-053` | HTTP 404, `.json` URL возвращает `404: Not Found`. `verification-log.md:71`. |
| `new-059` | Единственная строка — sentinel `303.303.303:8888`; regex-адресов нет, `Last-Modified` 2025-05-07. `verification-log.md:205`. |
| `new-062` | Не удалось подключиться к `www.docip.net:443`. `verification-log.md:72`. |
| `new-073` | Tor exit addresses — relay onion-routing/exit addresses, не универсальные HTTP/SOCKS proxies; нельзя автоматически превращать их в proxy URLs. `candidates.json:9613-9656`. |
| `new-076` | Заявленный 5-минутный refresh не подтверждён: `last_checked` около 2026-09-24 17:38 UTC и data license отсутствует. `candidates.json:8758-8813`. |
| `new-078` | 18.5 MB snapshot, `last_checked` 2026-08-29 и repository push 2026-09-13; freshness claim daily не подтверждена. `candidates.json:8930-8975`. |
| `new-083` | DNS не разрешает `proxy.010438.xyz`. `verification-log.md:74`. |
| `new-084` | Mirror Socks5Proxies/webunblocker, условия запрещают automated harvest/distribution; mirror license не снимает upstream restriction. `candidates.json:9475` entry boundary and `new-049` terms. |
| `new-085` | Последний push 2025-10-22, почти 11 месяцев до среза; metadata отсутствует. `candidates.json:9478-9512`. |
| `new-086` | HTTP 200, но тело `[]`; схема и данные не наблюдаются. `candidates.json:9546-9582`. |
| `new-087` | Oniono — relay metadata, а не HTTP/SOCKS inventory; verification body также partial/oversize. `candidates.json:9613-9656`, `verification-log.md:233`. |
| `new-088` | Telegram MTProto documentation не содержит public list/subscription. `candidates.json:9687-9716`. |
| `new-094` | Hiddify documentation page, не data export. `verification-log.md:240`. |
| `new-095` | v2rayN documentation page, не data export. `verification-log.md:241`. |

## Рекомендованный базовый набор по умолчанию

Это набор для **будущего feature-gated профиля**, а не изменение текущего каталога:

1. `cur-02` — отдельный HTTP TXT и `Last-Modified` 12:20 UTC вблизи среза.
2. `cur-04` — отдельный HTTP transport/API export с `Last-Modified` 12:22 UTC.
3. `cur-46` — SOCKS5 с `Last-Modified` 12:23 UTC.
4. `cur-51` — SOCKS4 с `Last-Modified` 12:23 UTC.
5. `cur-55` — уже реализованный GeoNode pagination/multi-protocol adapter.
6. `new-024` — keyless live JSON с protocol/geo/latency/last_checked; включать **после B**.
7. `new-009` — multi-protocol metadata mirror с timestamps; включать **после B**, с учётом CC BY attribution и неразрешённого quota discrepancy.
8. `new-045` — keyless page-JSON с protocol/geo/latency/lastChecked; включать **после C**.

**Почему именно так:** базовый набор даёт четыре явных transport-класса, два отдельных HTTP-URL, два структурированных источника и не требует credentials. Он не оптимизируется на максимальный count: `new-043`/`new-074` и большие TXT-семейства имеют заметные пересечения, а `new-058`/`new-060` имеют неясные rights.

**Обязательный gate перед включением:** источник должен иметь fixture-тест, 2xx с непустым телом, корректный schema/format parse, country/protocol semantics, отсутствие credentials и documented/approved data-use basis. Ни один из этих gates не доказывает proxy liveness; liveness остаётся отдельной фазой текущего scanner.

## Расширенный каталог сверх базового

Подключать по одному feature flag и измерять **accepted unique / total**, invalid, blocked, overlap и proxy-check pass rate:

- **B после общего JSON parser:** `new-008`, `new-023`, `new-026`, `new-032`, `new-048`, `new-055`, `new-057`, `new-074`, `new-075`, `new-077`, `new-079`, `new-080`, `new-081`.
- **C после HTML/page-JSON adapter:** `new-010`, `new-011`, `new-012`, `new-016`, `new-050`, `new-051`, `new-063`, `new-064`, `new-065`, `new-066`.
- **A для контролируемого расширения TXT:** `new-021`, `new-022`, `new-029`, `new-031`, `new-033`, `new-034`, `new-035`, `new-036`, `new-038`, `new-039`, `new-040`, `new-041`, `new-042`, `new-043`, `new-044`, `new-054`, `new-056`, `new-058`, `new-060`, `new-061`, `new-067`.
- Не включать автоматически D/E/F; для них нужны отдельные решения по аккаунту, self-hosting, формату или rights.

## Порядок интеграции и проверяемый критерий

| Шаг | Изменение | Критерий выполнения |
| --- | --- | --- |
| 1. Зафиксировать baseline | Снять checksum текущего `sources.json`, текущие unit/integration tests и отчёт collect. Ничего не подменять. | До/после checksum совпадает; текущий catalog не изменён; есть baseline parser/collect report. |
| 2. Выделить source profiles | Ввести декларативный профиль: `kind`, URL(s), protocol default, `https_is_connect`, country field, list root, max pages. | Для `cur-*` профиль не меняет behavior; static validation отклоняет неизвестный protocol и credentials. |
| 3. B: generic JSON records | Обработать bounded JSON: array, `{data:[...]}`, `{proxies:[...]}`; field map для `ip/host/address`, `port`, `protocol/type/proxy_type`; country только alpha-2; unknown protocol пропускать/ошибка по профилю. | Fixtures `new-009`, `new-024`, `new-048`, `new-074`, `new-077`, `new-079`, `new-080`, `new-081` дают только canonical candidates; `exit_ip`, `username/password` и неизвестные protocol не попадают в БД. |
| 4. B: fields/CSV parser | Добавить отдельный delimiter-aware режим, не ломая `http-fields`; принимать `ip:port` с последующими колонками, сохранять protocol только из профиля. | Fixture `new-026` и `new-058` не превращают SOCKS/UNKNOWN записи в HTTP; обычный `cur-*` результат не меняется. |
| 5. C: page-JSON | Вынести page loop из `geonode` в generic profile: page query, list key, total, empty page, repeated page, max pages/candidates, protocol scalar/list. | `new-045` и `new-063` проходят fixture с двумя страницами; неверный total/repeat/empty page дают контролируемую ошибку; budget соблюдён. |
| 6. C: HTML table | DOM-парсер с allowlisted host/path/selector, pagination cap, country/protocol columns и reject numeric-only matches. | Fixtures `new-010`, `new-011`, `new-050`, `new-051`, `new-065`, `new-066` не принимают произвольные числа; address/protocol/country извлекаются только из ожидаемых колонок. |
| 7. Политика и секреты | Добавить отдельный credential resolver только после одобрения D; `allow-private-sources` не использовать для production public feed. | D-источники без secret/config дают `SOURCE_CONFIG_MISSING`, а не URL с password; private destination bypass остаётся только mock-only. |
| 8. Метрики и feature flag | Включать сначала по одному ID, затем core/extended; считать parser accepted, unique, overlap, source freshness, later — реальные scanner passes. | Каждый источник имеет report `rows/accepted/invalid/blocked/complete/error`; ни один source не добавляется только потому, что `rows` велико. |
| 9. Долговременная свежесть | После интеграции сделать 2–3 timestamped GET на ETag/Last-Modified/file timestamps; затем отдельный proxy-check по workload. | Наблюдаемая смена либо обновлённый timestamp подтверждены; отсутствие смены не маркируется как «вечный» источник. |

## Какие адаптеры нужны и что меняется в коде

### 1. `json-records` (B): минимальный общий structured-record adapter

**Покрывает:** `new-008`, `new-009`, `new-023`, `new-024`, `new-032`, `new-048`, `new-055`, `new-057`, `new-074`, `new-075`, `new-077`, `new-079`, `new-080`, `new-081`.

**Изменения:**

1. Вынести bounded body + UTF-8 + JSON validation из исключительно `geonode` в общий внутренний parser, сохранив текущие лимиты 32 MiB/500k и строгие ошибки (`../proxy-workbench-sources/proxy_workbench/proxytool.py:256-268`, `../proxy-workbench-sources/proxy_workbench/proxytool.py:662-674`).
2. Ввести `record_adapter(profile)`: root (`list`, `data`, `proxies`, protocol-map), address (`ip`/`host`/`address`), port, protocol (`protocol`/`protocols`/`type`/`proxy_type`), country (`country_code`/`countryCode`/validated alpha-2).
3. Нормализовать один record в список candidates: например, `protocols=['http','socks5']` даёт два адреса, `protocol='http'` — один. `https` из fields, означающий CONNECT, преобразовывать в `http` только по профилю, а не глобальным догадкой.
4. `normalize()` оставить финальным шлюзом: private/reserved, hostname, credentials, port/path/query/fragment и socks4 IPv6 отбрасываются (`../proxy-workbench-sources/proxy_workbench/proxytool.py:310-328`).
5. Писать source key, `candidate_seen` и `candidate_meta.country`; не сохранять пока ASN/latency/uptime, если не вводить отдельную проверенную metadata schema.

### 2. `fields` / CSV (B): small parser change

**Покрывает:** `new-026`, `new-058`; частично формат `new-075`/`new-076` после выбора CSV.

**Изменения:** заменить/дополнить regex `http-fields` (`../proxy-workbench-sources/proxy_workbench/proxytool.py:600-602`) на profile-aware column parser; принимать разделитель/поля, извлекать leading `ip:port`, но **не** считать `country` именем. На первом этапе write только IP/port/protocol/country. Нужны fixtures с quoted CSV, header и multi-space TXT; invalid rows должны давать controlled `invalid`, а не ломать весь source.

### 3. `page-json` (C): generic API pagination

**Покрывает:** `new-045`, `new-063`, `new-064` (HTML envelope), частично будущие public APIs.

**Изменения:** page/limit injection, list root, total/page validation, repeat signature, empty-page, max-pages и shared budget. Нельзя просто передать `new-063` в `geonode`: текущий `consume_record` ожидает `protocols` и не принимает произвольный record mapping (`../proxy-workbench-sources/proxy_workbench/proxytool.py:610-630`). `new-064` требует bounded HTML parser, не JSON-record only.

### 4. `html-table` (C): отдельный DOM parser

**Покрывает:** `new-010`, `new-011`, `new-012`, `new-016`, `new-050`, `new-051`, `new-065`, `new-066`; допустимо использовать для controlled fixtures.

**Изменения:** `html.parser`/ vetted DOM dependency, selector allowlist, row validation, pagination extraction/cap, protocol/country columns. `text` остаётся только как простой fallback, не как парсер таблицы: он читает страницу целиком и ищет IPv4 regex (`../proxy-workbench-sources/proxy_workbench/proxytool.py:675-679`).

### 5. Authenticated request profile (D): только после product/security decision

**Покрывает:** `new-001`–`new-007`, `new-015`, `new-047`.

**Изменения:** передавать headers из secret resolver, не разрешать credentials в URL, не печатать header values в report/log, применять retry-after и quota. Текущий `_source_stream` делает только `client.stream('GET', url)` без headers (`../proxy-workbench-sources/proxy_workbench/proxytool.py:219-231`); это изменение не является основанием добавлять ключи сейчас.

### 6. Self-hosted/runtime source (D) и subscription adapters (E): отдельные продукты

**Покрывает:** `new-068`–`new-072`, `new-082`; экспериментально `new-030`, `new-089`–`new-095`.

**Изменения:** D требует инфраструктурного provisioning, auth listener, private endpoint policy и отдельного threat model. E требует runtime/parser для соответствующего формата, но не должен переиспользовать `http`/`socks` автоматически. Ни один из них не расширяет текущий бесплатный public-list каталог без отдельного решения.

## Проверки, выполненные именно в этой работе

1. **Историческое AST-измерение, которое нельзя считать интеграционным:**

   ```bash
   python3 tools/probe_collector_compat.py ../proxy-workbench-sources/proxy_workbench/proxytool.py verification.json samples /tmp/proxy-workbench-compat.json /tmp/proxy-workbench-compat.md
   ```

   В исходном исследовании этот инструмент извлёк `normalize`, `loose_addresses`, `LOOSE_ADDRESS` и `SCHEMES` из исходника и выполнил их отдельно; пакет приложения и `collect()` не запускались. Сохранённый результат `{"samples": 140, "works_now": 83, "parser_tweak": 12, "new_adapter": 45}` относится только к этому изолированному разбору, не к HTTP-загрузке, `source_spec()` и реальным `SOURCE_KINDS`. При уточнении команда выше не перезапускалась.

2. **Настоящий локальный интеграционный тест через `collect()` и mock HTTP-сервер:**

   ```bash
   cd ../proxy-workbench-sources
   .venv/bin/python -m unittest tests.test_research_collect_integration -v
   ```

   Фактический итог команды: `Ran 2 tests in 3.196s` и `OK`. Тест отдаёт сохранённые реальные тела `cur-01`, `cur-08`, `cur-19`, `cur-42`, `cur-43`, `cur-47`, `cur-52`, `cur-55`, `new-009`, `new-010`, `new-026` и `new-045` без внешней сети. Факты, которые он зафиксировал:

   - `source_spec()` отклонил `json-records`, `fields`, `page-json` и `html-table` как неизвестные виды, не выполнив HTTP-запрос;
   - fallback `text` принял 0 строк из `new-009`; `http-fields` не принял ни одной строки CSV `new-026`; `geonode` вернул `rows=0`, `error=INVALID_PAGINATION` для `new-045`; `text` принял 0 строк из HTML `new-010`;
   - все девять текущих `SOURCE_KINDS` — `http`, `https`, `socks4`, `socks5`, `socks5h`, `auto`, `text`, `geonode`, `http-fields` — добавили хотя бы один кандидат из назначенных им сохранённых образцов.

   `geonode` читал первую страницу `cur-55`, затем ожидаемо сообщил `INCOMPLETE_PAGINATION`, когда mock вернул пустую вторую страницу при `total=3088`; тест проверяет принятие кандидатов, а не полноту многостраничного источника. Никакой адрес из образцов не использовался для соединения.

3. **Локальная инспекция сохранённых JSON fixtures, без HTTP и без подключения к прокси:**

   ```bash
   python3 -c 'import json,pathlib; b=pathlib.Path("samples"); ids=["new-009","new-024","new-045","new-063","new-064","new-074","new-075","new-076","new-077","new-078","new-079","new-080","new-081"]; [(lambda d: print(i, "root=", list(d) if isinstance(d,dict) else "list", "rows=", list((d.get("data") or d.get("proxies") or d)[0]) if isinstance(d,dict) and isinstance(d.get("data"),list) and d["data"] else (list(d["proxies"][0]) if isinstance(d,dict) and isinstance(d.get("proxies"),list) and d["proxies"] else (list(d[0]) if isinstance(d,list) and d else []))))(json.loads((b/(i+".bin")).read_text(encoding="utf-8"))) for i in ids]'
   ```

   Вывод подтвердил, например: `new-009` root=`['source', 'updated_at', 'count', 'license', 'proxies']`; `new-045` root=`['data', 'page', 'limit', 'total']`; `new-063` root=`['data', 'total', 'page', 'limit']`; `new-064` содержит `table_html`; `new-077` имеет отдельные `host` и `exit_ip`; `new-080`/`new-081` — protocol-keyed objects. Именно эти различия потребовали разных адаптеров, а не только смены уже существующего `kind`.

4. **Проверка покрытия A–F по первым ячейкам таблиц:**

   ```bash
   python3 - <<'PY'
   import json, re
   from pathlib import Path
   text = Path('notes/integration-plan.ru.md').read_text(encoding='utf-8')
   heads = list(re.finditer(r'^## ([A-F])\.', text, re.M))
   positions = []
   for i, match in enumerate(heads):
       end = heads[i+1].start() if i+1 < len(heads) else text.index('\n## Рекомендованный базовый набор', match.end())
       for line in text[match.start():end].splitlines():
           if line.startswith('| `'):
               positions += [(match.group(1), value) for value in re.findall(r'`((?:cur|new)-\d+)`', line.split('|', 2)[1])]
   unique = {value for _, value in positions}
   expected = {entry['id'] for entry in json.loads(Path('candidates.json').read_text(encoding='utf-8'))}
   print(len(positions), len(unique), len(expected - unique), len(unique - expected))
   PY
   ```

   Фактический вывод: `152 150 0 0`. Подробный разбор показал `A=75`, `B=15`, `C=11`, `D=15`, `E=8`, `F=28`; пересечение — только `new-094` и `new-095`, присутствующие в E и F.

**Не выполнялось:** новый HTTP GET к внешним источникам, новый proxy-liveness check, регистрация, ввод API-ключей, оплата, запуск HTML/JSON adapter в production и автоматическая замена `sources.json`. Поэтому все плановые критерии интеграции — будущие проверяемые шаги, а не уже пройденные тесты.
