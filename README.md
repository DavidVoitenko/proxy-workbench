# Proxy Workbench 1.2.0

> **Status:** public source-only beta. The project measures reachability and latency; it does not promise anonymity, safety, or stable availability of third-party proxies.

A local, privacy-conscious workbench for collecting public proxy candidates, checking them against user-selected HTTP(S) services, filtering local denylist rules, and recording optional DNSBL reputation signals. The interface runs on `127.0.0.1`; there is no account system, telemetry, advertising SDK, or cloud backend.

## English overview

Proxy Workbench is a small Python 3.11+ tool for repeatable proxy benchmarking. It streams editable public source lists, validates HTTP/HTTPS/CONNECT and SOCKS5 candidates, runs several local measurements per candidate, supports resumable profiles, and exports TXT/CSV/JSON results. It is designed for engineering experiments and defensive quality checks, not for bypassing access controls or hiding online identity.

**Responsible use:** use only sources and endpoints you are allowed to test, respect provider terms and applicable law, and do not send credentials through untrusted public proxies. See [SECURITY.md](SECURITY.md) and [PRIVACY.md](PRIVACY.md).

## Data flow

```text
editable source URLs / local TXT
              │
              ▼
       candidate normalization
              │
              ▼
     local denylist / DNSBL ──► clean | listed | unknown
              │
              ▼
       HTTP(S) checks via proxy
              │
              ▼
   SQLite profile + TXT/CSV/JSON export
```

The GUI is loopback-only. Source hosts, DNS resolvers, the tested proxy, and the tested service each observe different parts of a request. The `data/` directory is local, ignored by Git, and may contain URLs, proxy addresses, timings, headers, and results; protect or delete it according to your own policy.

## What is included

- resumable CLI and local browser GUI in English and Russian (header toggle; defaults to the browser language and remembers the choice);
- all-service checks with status, body substring, and SHA-256 conditions;
- bounded worker queue, explicit stop/resume, and profile-aware exports;
- local IP/CIDR/proxy denylist and optional user-selected DNSBL zones;
- neutral request profiles and explicit safe-header policy;
- local mock-based tests and cross-platform CI;
- MIT license, security policy, privacy policy, contribution guide, and changelog.

## Project status and roadmap

The current public line is a feature-complete development snapshot. The next release gate is a clean cross-platform CI run, reproducible source archive/checksum, and a reviewed release tag. Planned hardening work includes stricter source-fetch limits, crash-safe export generations, explicit local-data cleanup, and large-result GUI coverage.

Самостоятельный сборщик и проверяльщик публичных прокси. Папку можно перенести отдельно: исходный проект, его аккаунты и runtime не нужны. Требуется Python 3.11+ и доступ в интернет для установки зависимости.

## Отпечаток приложения

В настройках выбирается нейтральный request-профиль: `workbench` (по умолчанию), `standard` или `minimal`. Пресет задаёт только User-Agent, Accept и Accept-Encoding; он добавляется к заголовкам сервиса без личных данных и без browser impersonation. Изменение пресета создаёт отдельный профиль результатов.

Профиль не меняет TLS/JA3/JA4, HTTP-транспорт или гарантии анонимности. Пользовательские заголовки сервиса остаются локальной настройкой, не выводятся в экспорт и не включаются в код. Credential-like заголовки отклоняются. В отчётах URL показываются только без query, fragment и path, чтобы токены в ссылках не попадали в status/result metadata. Для CLI можно выбрать пресет флагом `--request-profile standard`.

## Интерфейс — запуск двойным кликом

- **macOS:** откройте `Start.command`.
- **Windows:** откройте `Start.bat` (нужен Python 3.11+ с Python Launcher).
- **Linux / терминал:** `./run.sh` или `./run.sh gui`.

Откроется локальная страница в вашем браузере. По умолчанию включена тёмная тема; кнопка вверху переключает на светлую. Рядом кнопка EN/RU переключает язык интерфейса: по умолчанию используется язык браузера (русский для `ru`, иначе английский). Оба выбора сохраняются в браузере. Сообщения об ошибках от локального сервера и журнал выполнения пока только на русском. Сервер доступен только на этом устройстве. Повторный запуск открывает уже работающий интерфейс.

1. В разделе **Проверка** добавьте один или несколько сервисов, HTTP-коды и при необходимости текст ответа. Условие **«все сервисы»** действует всегда.
2. При необходимости откройте **Источники**: редактируйте URL, добавьте свои списки вставкой или TXT-файлом. SOCKS5-списки указываются как `socks5 URL`, Geonode JSON API — как `geonode URL`.
3. Нажмите **Найти и проверить**. Справа: прогресс, скорость обхода, оставшееся время и число подходящих прокси. **Остановить** сохраняет завершённые результаты; **Продолжить базу** продолжает текущий профиль без повторной загрузки источников.
4. В **Результатах** выберите порядок, порог успешности и количество, нажмите **Сформировать экспорт**, затем TXT / CSV / JSON. Кнопка **Детали** показывает каждую попытку по каждому сервису. Таблица обновляется при открытии, завершении проверки или кнопкой **Обновить таблицу**.

Кнопка **Перепроверить всю базу заново** заменяет результаты текущего профиля новыми. Изменение URL или параметров замера создаёт другой профиль. Настройки сохраняются по кнопке и при запуске. Завершённый экспорт остаётся доступным при следующем открытии.

Не закрывайте окно терминала, пока пользуетесь интерфейсом. Закрытие вкладки браузера не останавливает работу. Для выхода нажмите Ctrl+C в окне приложения: проверка остановится с сохранением прогресса.

Если переносите папку на другое устройство, не переносите `.venv`: на новом устройстве запускающий файл создаст окружение заново. ZIP содержит только исходники и инструкции, без баз, настроек и секретов.

## Запуск через командную строку

В терминале, находясь в этой папке:

```sh
./run.sh run
```

Первый запуск создаст `.venv` и установит зависимость. Launcher сохраняет SHA-256 `requirements.txt` и повторно обновляет окружение при изменении зависимостей. Затем загрузит публичные списки из `sources.json`, удалит дубликаты и проверит **все** адреса. Число кандидатов зависит от источников: 190 тысяч не гарантированы. Нет ограничения на количество проверяемых адресов, общего таймера прохода или остановки при заполнении пула. Фильтры по стране, ChatGPT и инвойсам не выполняются; отдельно доступны локальный denylist и опциональные DNSBL-сигналы.

По умолчанию: HTTPS-запрос к `https://example.com/`, 3 независимых замера на адрес, таймаут 8 секунд на запрос, 128 параллельных проверок, до 100 стартов запросов/секунду. Нужны успешные ответы в двух из трёх попыток. При ограничении файловых дескрипторов число воркеров автоматически снижается.

На Windows или при ручной установке:

```sh
python -m venv .venv
# Windows:
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python proxytool.py run
# macOS/Linux:
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python proxytool.py run
```

## Проверять свой сервис

```sh
./run.sh run --url https://example.org/health
```

Для проверки определённого HTTP-кода, содержимого или нескольких адресов скопируйте `service.example.json` в `data/service.json`, укажите свои URL и запустите:

```sh
./run.sh run --config data/service.json
```

`targets` — список проверяемых адресов. Для каждого доступны:

- `url`: обязательный HTTP/HTTPS URL;
- `method`: `GET` или `HEAD`, по умолчанию `GET`;
- `statuses`: допустимые коды, по умолчанию 200–299;
- `contains`: строка, которая должна присутствовать в теле ответа в UTF-8;
- `sha256`: ожидаемый SHA-256 полного тела ответа, необязательно;
- `headers`: необязательные безопасные HTTP-заголовки (`Accept*`, `Cache-Control`, `Pragma`, `User-Agent`, `X-Request-ID`, `X-Client-Version`); произвольные credential-like заголовки отклоняются;
- `request_profile`: необязательный нейтральный пресет `workbench`, `standard` или `minimal`;
- `reputation`: необязательные параметры локального denylist и DNSBL (`local_enabled`, `dnsbl_enabled`, `dnsbl_zones`, `timeout`, `strict`).

Каждый адрес проверяется во всех попытках. Порог успешности применяется **к каждому** адресу сервиса, а не только к средней доле ответов. Редиректы не выполняются автоматически: задавайте конечный URL либо явно разрешите код редиректа. TLS-сертификаты проверяются. Секретные заголовки и конфиги храните только в `data/`; конфигурация проверки также сохраняется в локальной базе. Для HEAD не задавайте проверку тела.

Для замеров выбирайте небольшой стабильный endpoint. При большой странице увеличьте `--max-bytes`: превышение лимита считается ошибкой, а не успешным частичным ответом. Без `contains`/`sha256` проверяется только HTTP-код, поэтому страница заглушки с кодом 200 может пройти.

## Проверка чистоты и локальный denylist

В `data/denylist.txt` можно записать IP, IPv4/IPv6 CIDR, точный адрес прокси и комментарии с `#`. Список применяется при сборе, перед проверкой и повторно перед экспортом; совпадения считаются отдельно от невалидных строк. Файл находится в игнорируемой локальной папке и не содержит аккаунтов или секретов. Политику можно принудительно переключить флагами `--local-denylist` и `--no-local-denylist`.

Публичные DNSBL-проверки выключены по умолчанию. Включить их можно в GUI или CLI:

```sh
./run.sh run --dnsbl --dnsbl-zone bl.example.org --reputation-timeout 2.5
./run.sh run --dnsbl --dnsbl-zone bl.example.org --strict-clean
```

Каждая зона проверяется DNS-запросом без API-ключей; если список зон пуст, DNSBL фактически выключается. В результате отображаются `clean`, `listed`, `unknown` и `local_denied`; `listed` и `local_denied` не попадают в экспорт, а `unknown` исключается только в строгом режиме. Ошибка чтения denylist показывается как `unknown` и блокирует экспорт до исправления файла. Для новой политики или после её изменения используйте «Перепроверить»; это также обновляет профиль результатов. DNSBL — только сигнал репутации, а не доказательство анонимности или безопасности.

## Сохранить сколько нужно и выбрать рейтинг

Все проверки сохраняются в `data/proxies.sqlite3`. Экспорт находится в `data/exports/`:

- `proxies.txt`: отсортированные рабочие адреса;
- `ranked.csv`: рейтинг, задержка, разброс и доля успешных запросов;
- `ranked.json`: то же с деталями каждой попытки;
- `status.json`: сколько кандидатов, проверено, осталось и сохранено.

```sh
# Сохранить все подходящие, самые быстрые первыми:
./run.sh export --top 0 --sort speed
# Сохранить 500 лучших по качеству:
./run.sh export --top 500 --sort quality
# Только адреса, прошедшие каждую попытку:
./run.sh export --top 0 --min-success 1
# Адреса с хотя бы одним успехом на КАЖДОМ сервисе:
./run.sh export --top 0 --min-success 0
```

Экспорт использует последний запущенный профиль и не повторяет запросы. `--top` ограничивает только экспорт, **никогда не обход**. Если подходящих меньше, сохраняется доступное количество. Те же параметры можно передать команде `run` или `scan`.

`speed` сортирует по медиане времени успешного полного запроса, включая соединение и TLS. `quality` сортирует по формуле `100 × минимальная успешность среди targets / (1 + (медиана + стандартное отклонение) / 1000)`. Время указано в миллисекундах. Это текущая оценка доступности и задержки, не измерение пропускной способности в Мбит/с и не гарантия анонимности. Для осмысленного сравнения используйте одинаковый профиль.

## Очистка локальных данных

Удалить базу, профили, экспорты и runtime-отчёты можно явно:

```sh
./run.sh clear-data --yes
```

Команда не удаляет `gui-settings.json` и `denylist.txt`, если они уже настроены. Экспорт хранит crash-safe generation-каталоги и автоматически оставляет только последние три; перед удалением убедитесь, что процессы GUI и CLI остановлены. Резервные копии и browser storage приложение не очищает.

## Полный проход и продолжение

```sh
# Только сбор:
./run.sh collect
# Проверка уже собранной базы:
./run.sh scan --config data/service.json
# Ctrl+C: сохранить завершённое и остановиться.
# Та же команда продолжит незавершённые адреса.
./run.sh scan --config data/service.json
# Свежая проверка всех адресов того же профиля:
./run.sh scan --config data/service.json --recheck
```

URL, содержимое, заголовки, request-профиль, политика denylist/DNSBL, число попыток, таймаут и размер ответа определяют профиль. В профиль также входит digest фактического набора заголовков пресета, поэтому изменение версии продукта не смешивает старые и новые результаты. Повторный запуск того же профиля продолжает проход; для обновления старых оценок нужен `--recheck`. Профили версии 1 после обновления получают новый профиль версии 2, поэтому первый запуск может начать новый проход. Прерванный посреди попыток адрес проверяется заново. Завершённые результаты пишутся в SQLite пакетами; Ctrl+C сохраняет текущий пакет, аварийное завершение может потребовать повторить последний пакет.

Прогресс и оценка оставшегося времени обновляются каждые 2 секунды. При 190 тысячах неотвечающих адресов, трёх попытках и таймауте 8 секунд проход на 128 воркерах занимает около 10 часов; быстрые отказы существенно сокращают время. Несколько targets увеличивают число запросов. Ограничение скорости и параллельности настраиваются:

```sh
./run.sh run --workers 256 --rate 100 --timeout 8 --attempts 3
```

Не создаётся задача на каждый из 190 тысяч адресов: очередь ограничена удвоенным числом воркеров. Нет предварительного TCP-фильтра, который мог бы отсеять адрес до проверки сервиса. Все попытки выполняются даже после первой ошибки. Одновременно разрешён один процесс на одну папку `data`.

## Свои списки и независимые наборы

```sh
./run.sh run --no-sources --input data/my-proxies.txt --url https://example.org/health
./run.sh collect --sources data/my-sources.json --input data/extra.txt
./run.sh run --data data/another-service --url https://example.net/status
```

`sources.json` — редактируемый JSON-массив источников. Обычный URL означает HTTP-список. Формат `IP:порт:страна` поддерживается через префикс `http-fields URL` (используется встроенными списками HideIP). Для SOCKS5 без схемы у адресов используйте строку `socks5 https://example.org/list.txt`. Для постраничного JSON API Geonode используйте строку `geonode https://proxylist.geonode.com/api/proxy-list?limit=500&protocols=http%2Csocks5`. Сборщик обходит все страницы до заявленного API количества, отмечает повторённые/недостающие страницы и повторяет неудачную загрузку один раз. Каждая строка списка: `IP:port`, `http://IP:port` или `socks5://IP:port` / `socks5h://IP:port`. Поддерживается IPv6 в квадратных скобках. Явный `https://` сохраняется как TLS-соединение к самому прокси. Обычные списки HTTPS/CONNECT с адресами без схемы читаются как `http://`. Метаданные HTTPS в Geonode означают CONNECT и преобразуются в HTTP. SOCKS4, авторизованные прокси, доменные имена вместо IP и непубличные IP отклоняются. Встроены HTTP/CONNECT, HTTPS и SOCKS5-источники. Все источники остаются редактируемыми.

База накапливает уникальные адреса. `--no-sources` отключает загрузку, но не удаляет ранее собранные адреса: для полностью отдельного списка используйте новую `--data`.

Списки читаются потоково с безопасными пределами по умолчанию: до 8 MiB на один удалённый источник, 64 KiB на строку, 100 000 кандидатов на источник и 5 redirect hops. Параметры меняются через `--source-max-bytes`, `--source-max-line-bytes`, `--source-max-candidates` и `--source-max-redirects`. На загрузку текстового источника или одной страницы API отведено 60 секунд (`--source-timeout`), с одним повтором при ошибке. Ошибки, незавершённые загрузки, исходные и отклонённые строки отражаются в `data/sources-report.json`; уже прочитанные валидные адреса остаются в базе. Полный обход означает проверку всех собранных кандидатов, а не гарантию доступности всех внешних источников.

По умолчанию источники не должны указывать на loopback, private/link-local/reserved/metadata адреса. Для локальных mock-сервисов есть явный `--allow-private-sources`; этот флаг небезопасен для обычного запуска. Автоматические redirects отключены, каждый hop проверяется, а выбранный DNS address закрепляется в transport на время запроса.

## Структура проекта

- `proxytool.py` — CLI, collector, scanner, reputation и export pipeline.
- `gui.py` / `ui/` — loopback-only browser interface.
- `reputation.py` — denylist/DNSBL verdicts.
- `branding.py` — versioned neutral request profiles.
- `tests/` — isolated unit and local mock tests.
- `.github/` — CI and issue templates.
- `data/` — ignored local settings, databases, reports and exports.

## Если что-то не запускается

- **Port/Gatekeeper:** запускайте `Start.command` или `Start.bat`; при первом старте macOS может запросить разрешение на запуск локального Python.
- **Python version:** требуется Python 3.11+; удалите только `.venv/`, затем запустите launcher снова.
- **Stale dependencies:** launcher сравнивает digest `requirements.txt` и переустанавливает их при изменении.
- **Data folder is busy:** остановите GUI и CLI-процессы перед `clear-data`; активные симлинки других проектов не трогайте.
- **No results:** проверьте source report, denylist, DNSBL-зоны и доступность выбранного service endpoint.

## Как продвигать open-source проект

1. Опубликуйте первый проверенный release с changelog, screenshots и коротким demo video.
2. Добавьте topics и описание GitHub, используйте Discussions для вопросов, а Issues — только для воспроизводимых багов.
3. Поделитесь не рекламным сообщением, а проверяемым техническим кейсом: что измеряется, какие ограничения и как воспроизвести на local mock.
4. Подходящие каналы: GitHub topics/releases, Lobsters, r/Python, r/networking, Hacker News при наличии сильной инженерной истории, тематические Telegram/Discord-сообщества.
5. Не обещайте анонимность и не публикуйте реальные proxy endpoints, credentials или пользовательские данные.

## Проверки разработки

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Тесты используют временные базы, имитацию 190 000 кандидатов, локальные mock-DNSBL и HTTP-прокси. Внешних сервисов и рабочих аккаунтов не касаются. Документация транспорта: [HTTPX proxy support](https://www.python-httpx.org/advanced/proxies/).

## Дополнения из каталога источников

Добавлены SOCKS5-ленты [hookzof](https://github.com/hookzof/socks5_list), [TheSpeedX](https://github.com/TheSpeedX/PROXY-List), [Proxifly](https://github.com/proxifly/free-proxy-list), [monosans](https://github.com/monosans/proxy-list), [IPLocate](https://github.com/iplocate/free-proxy-list), открытый ProxyScrape v4 и постраничный [Geonode](https://geonode.com/free-proxy-list). Доступность источников проверялась 20.09.2026; их объём и актуальность меняются. Число адресов из нескольких списков нельзя складывать без удаления дублей.

Триалы коммерческих провайдеров требуют отдельной регистрации и не являются публичными списками. Регистрация, платёжные действия и активация промокодов не автоматизированы; указанные в стороннем каталоге объёмы и промокод не считаются проверенными. MTProto-прокси предназначены для другого протокола и не включены в HTTP/SOCKS-проверку. Авторизованные провайдерские прокси пока не поддерживаются.
