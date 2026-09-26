# Changelog / История изменений

Формат следует Keep a Changelog и semantic versioning.

## [Unreleased]

### Added

- **Точка входа доходит до фонового слоя.** `proxy-workbench` и `python -m proxy_workbench` без аргументов запускают приложение (`proxy_workbench.desktop`): меню-бар на macOS, один экземпляр, запуск при входе по желанию, сон/пробуждение. `gui` и `--no-desktop` открывают только интерфейс; всё остальное уходит в CLI, как и раньше. Раньше обычный запуск уходил в `gui.main`, и фоновый слой не работал в том виде, в котором его запускает пользователь.
- **Меню-бар входит в собранный `.app`.** Helper на Swift компилируется сборкой (`packaging/tray_helper.py`) и кладётся в bundle; spec без него прерывает сборку. Раньше приложение запускалось и не имело меню-бара.
- **Замороженная сборка умеет выполнять проверку.** Worker — это повторный запуск того же исполняемого файла с командой `scan`; frozen launcher маршрутизирует командные слова в CLI тем же правилом, что и консольный скрипт. Раньше такая попытка заканчивалась `unrecognized arguments: scan`, то есть установленное приложение не могло проверить ни одного прокси.
- **`packaging/verify_delivery.py`** — per-user пути, portable mode и перенос старой папки, проверенные на настоящем собранном артефакте, а не на коде.
- **Windows-поставка проверяется сама.** Сборщик читает PE-subsystem из готового `.exe` и отказывается выпускать пару, в которой GUI-бинарник не windowed или CLI не console; запускает GUI-бинарник с пустым `%LOCALAPPDATA%` и проверяет второй запуск; передаёт установщику абсолютные пути и ищет `iscc` в стандартных папках.
- **CI, который проверяет артефакты.** `.github/workflows/windows.yml` собирает и запускает Windows-поставку на настоящем Windows-runner; job `macos-artifact` в `ci.yml` собирает `.app`, прогоняет smoke по собранному бинарнику и сверяет манифест; `release.yml` отдаёт два Windows-бинарника с per-user installer и macOS-артефакт.
- **Версия пакета приведена к версии продукта:** `pyproject.toml` был `2.2.1`, `branding.PRODUCT_VERSION` — `2.3.0`, и bundle объявлял одно число, а wheel — другое.

- **Версионированный control API `/v1`** — 82 маршрута: коллекции, источники, профили, задания, результаты, пулы, шлюз, расписания, экспорты, бронирования, импорты и ключи. Машиночитаемое описание — `proxy_workbench/openapi.json`, генерируется из той же таблицы маршрутов, по которой отвечает сервер.
- **Менеджер API-ключей** в GUI и CLI (`api-key`): именованные ключи, права, resource scope (коллекции и пулы), лимит частоты, параллелизм, срок, ротация с окном Grace, отзыв и журнал аудита. Первый административный ключ выдаёт только локальный доверенный bootstrap.
- **Именованные профили с ревизиями и пресеты**, **пулы** с реальным циклом поддержания, **расписания**, **менеджер источников** с отчётами по каждому источнику.
- **`diagnose funnel|zero|control|health|bundle`** — счётчики воронки, объяснение нулевого результата, контроль устройства и целей, редактированный диагностический пакет. Слой диагностики существовал и был недостижим.
- **`backup verify|preview|restore|rollback|retention|cleanup|rebind`** — вторая половина F24. Каждое изменяющее действие сначала печатает, что сделает, и выполняется только с `--apply`.
- **Подписки** как узкое, ограниченное по сроку и отзываемое identity, и настольная поставка для macOS (`.app`/`.dmg`), Windows и Linux с per-user путями данных и portable-режимом.
- `include_secrets` публикует **ссылку** на запись хранилища, а не значение секрета.

### Fixed

- **Поколение снова множество адресов.** Повторная проверка добавляла вторую строку того же адреса, каждый потребитель судил строки независимо, и адрес попадал дважды в `proxies.txt`, `ranked.json`, `ranked.csv`, `proxy.pac`, `clash.yaml`, `singbox.json` и в `/proxies`. Теперь у `results` одна строка на (профиль, доступ, адрес), схлопнутая история суммируется, `job_id` остаётся колонкой последнего измерения. Версия схемы 15.
- **Свежий провал отменяет старый успех.** Строка с ошибкой отвергалась, а устаревшая сохраняла свой не истёкший `valid_until`, и опубликованный набор продолжал отдавать адрес, который последнее измерение только что отвергло.
- **Обновление промежуточной сборки обратимо.** Техническая копия с manifest берётся перед любой неаддитивной миграцией, а не только для legacy-файла с `user_version = 0`: сборка линии 1.x переписывала `results` без страховки, и `list_backups()` показывал пусто.
- **Legacy-результаты остаются читаемыми.** Миграция 13 писала восстановленное происхождение в колонки, которые никто не читает, и каждая historic-строка отказывала по коллекции, в которой не измерялась. Теперь оно доходит до payload; то, чего файл 2.x не содержал (сеть, lifetime), отсутствует по-прежнему, и отказ называет эту настоящую причину.
- **Штатная retention-политика выполняется.** Она удаляла `observations` раньше ссылающихся на них `results`, SQLite отвергал, откат оставлял данные на месте — после того, как preview уже пообещал удаление.
- **Object-level scope проверяется.** Чтение и скачивание артефакта чужой коллекции отвечали 200; список заданий, профилей, источников или расписаний возвращался ключу, ограниченному одной коллекцией, потому что фильтр спрашивал вид scope, которого у ключа нет. Один объект вне scope — 404; список теряет только чужие строки.
- **Чувствительный экспорт работает.** `include_secrets` передавал имя режима вне словаря движка, запрос отвергался после уже пройденной проверки права, а отказ выдавался за «операция не подключена к сервисному слою». Доменный отказ теперь отвечает своим кодом.
- **Пароль шлюза не равен токену API.** `serve` и `gateway` использовали одно значение `--api-token` и как пароль ротирующего прокси, и как read-only bearer. У шлюза свой `--gateway-token` / `PROXY_WORKBENCH_GATEWAY_TOKEN`; поставляемый compose использует обе переменные.
- **`POST /v1/pools/{id}/refill` наполняет пул.** Он не читал путь, ставил в очередь проверку коллекции из тела и оставлял пул на 0/desired, отвечая «job queued». `/start` теперь запускает пул, `/recheck` измеряет его собственную коллекцию, а `/start`, `/pause` и `/members` больше не отвечают 500.
- **`/v1/results/{id}` и `/v1/results/{id}/observations` возвращают данные.** Сегмент пути не может нести полный адрес, а строка не публиковала `endpoint_id`, поэтому detail был всегда 404, а observations — пустым списком.
- **`--max-requests` и `--run-max-bytes` считают.** Ресурсный шлюз не начислял потраченное измерением, и оба лимита были молчаливыми no-op. Исчерпанный бюджет теперь останавливает прогон, а не записывает нетронутый адрес как недостижимый.
- **N endpoint, N уникальных IP и N подтверждённых exit-IP — три разных числа.** Последние два были одним выражением и всегда давали 0, потому что подтверждённый адрес выхода искали не там. Прогон, закончившийся недобором `--want`, называет недостижимую единицу вместо `complete`.

### Changed

- Схема базы на версии 15; за ней следуют константа настольной поставки и `openapi.json`.
- Лимиты по умолчанию на источник — 32 МиБ и 500 000 кандидатов, чтобы крупнейшие публичные списки не обрезались.

### Known gaps

- Аутентификация на прокси (HTTP Basic и SOCKS5 username/password) не реализована ни в одном движке проб: credentials в URL отвергаются, а идентичность доступа записывается, но сам секрет не отправляется. Поэтому `results.access_id` всегда называет identity, под которым строка измерена, а для встроенного скана это публичная, без пароля.
- **Windows-поставка не собрана и не запущена.** Машина сборки — macOS, а PyInstaller собирает под свою машину. Проверено без Windows: разбор и фактический запуск всех spec'ов, entry point, `console=False`/`console=True`, наличие ресурсов, чтение PE-subsystem, передача путей установщику, поиск `iscc`. Воспроизводимая проверка добавлена в `.github/workflows/windows.yml`, но ещё ни разу не выполнялась. Матрица сборок и точные команды — в `docs/packaging/RELEASE-NOTES.ru.md`.
- **Артефакты не подписаны и не нотарифицированы.** Signing credentials у проекта нет, и пайплайн их не создаёт; macOS-сборка получает ad-hoc подпись, Windows остаётся без Authenticode. Пайплайн подхватит уже имеющийся Developer ID и учётные данные нотаризации, когда они появятся.
- Меню-бар есть только на macOS. Sleep/wake на Windows не определяется: сон входит в монотонные часы, а нативного наблюдателя для Windows в слое нет. На Linux работает сравнение часов, но и оно не проверялось на живой машине.
- Автоматической загрузки обновлений нет: есть проверка происхождения, backup, проверка схемы, откат и `--update-notice`. Публикующего сервера у проекта нет.
- Карточка сравнения источников и провайдеров (F21) не реализована.
- `pipeline.Pipeline`/`run_pipeline` проверяются своими бенчмарками, но продукт сканирует через `proxytool.scan`, который делит с ними бюджеты, шлюз, ledger и политику find-N.

## [2.2.1] — 2026-09-25

### Fixed

- **Collecting from public sources returned nothing.** The transport that pins a source to its checked IP sent two `Host` headers, so every hostname-based list failed with `LocalProtocolError`. Tests only used IP-literal sources, which skip that path; new tests cover hostname sources.
- **A proxy with a malformed SOCKS5 reply stopped the whole scan.** socksio's parse error escaped as an `ExceptionGroup`. Any error of a single proxy is now recorded as that proxy's failure.
- Speed tests use a high-resolution timer; on Windows very fast downloads could lose their Mbit/s value.

### Changed

- Default per-source limits are 32 MiB and 500,000 candidates, so the largest public lists are no longer cut off.

## [2.2.0] — 2026-09-24

### Added

- **Recommended sort** (now the default) combines four signals:
  - quality;
  - survival across re-checks;
  - how rare a proxy is across lists: an address offered by one niche list is less crowded than one in twenty lists;
  - the track record of the list it came from.

  Exports gain `listed_in` and `recommended`.
- **`proxy-workbench get`** prints working proxies from the latest export for shell scripts (`--protocol`, `--country`, `--top`, `--random`, `--format txt|hostport|json`, `--no-hosting`).
- **`proxy-workbench test PROXY…`** checks your own proxies against the usual targets without storing anything.
- **sing-box config:** `singbox.json` in every export and `/singbox` in the API.
- **Use in Telegram** button for the rotating proxy (opens `tg://socks` with its address).
- The results table follows a running check every few seconds.
- Settings can be saved to a file and loaded back (Help tab).

## [2.1.0] — 2026-09-24

### Added

- **Rotating proxy for scrapers.**
  - The user name picks what you need: `country-de_nl`, `protocol-socks5`, `latency-800`, `anonymity-elite`.
  - `session-<id>` keeps one proxy for a whole session while it works.
  - `--max-per-proxy` caps simultaneous connections per proxy.
  - `GET /status` on the gateway shows the pool, sessions and per-proxy counts.
- **Real download speed:** an optional speed test file (`--speedtest-url`, GUI field) is downloaded through each working proxy.
  - The Mbit/s column, the bandwidth sort, `mbps` in exports and the API, and an API `min_mbps` filter show the result.
- **Provider of every proxy:** `update-geoip` also fetches DB-IP ASN Lite. Each proxy gets its AS number, organisation and a hosting flag.
  - `--no-hosting` and the GUI option skip cloud and data-centre ranges before checking.
  - The results table gets a Provider column and a filter; exports and the API get `asn`, `provider`, `hosting` and `hosting=0/1`.

## [2.0.0] — 2026-09-24

### Added

- **Windows executable:** every release ships `proxy-workbench-…-windows-x64.exe` built with PyInstaller. It opens the GUI on double-click, runs the CLI with arguments, and keeps its data in a `data` folder next to itself; no Python needed.
- **`pipx install`** (#3): `pipx install git+https://github.com/DavidVoitenko/proxy-workbench` gives a `proxy-workbench` command. Without arguments it opens the GUI; with arguments it runs the CLI (`python -m proxy_workbench` works too). Releases also attach the wheel.
- **Docker Compose:** `compose.yml` runs a checker that keeps 100 proxies fresh, the API and the rotating proxy from one shared volume.
- **Quick pre-check** (`--prefilter`, on by default with 512 connections; GUI: Performance → Quick pre-check). A plain TCP connect runs before the full check, so addresses that do not even accept a connection (most of any public list) are dropped at a fraction of the cost. They are stored as `UNREACHABLE`, so a stopped scan resumes where it left off.
- **End-to-end package check:** `packaging/smoke.py` installs nothing and touches no public network. It starts the built app, runs a check through a local mock proxy and reads the result back through the API and the gateway. CI runs it for the wheel on Linux and Windows and for the Windows executable; releases run it before publishing.

### Fixed

- On Windows, background checks started from an environment without `PYTHONUTF8` (the new `.exe` and pipx installs) failed on the first Cyrillic log line. Terminal output is now always UTF-8.

### Changed

- The code now lives in the `proxy_workbench` package. `proxytool.py`, `gui.py`, `Start.bat`, `Start.command` and `run.sh` keep working from a source folder, and an existing `data/` folder there is used as before.
- The data folder is chosen as follows: `PROXY_WORKBENCH_DATA`, then `data/` in a source checkout or next to the `.exe`, then the user profile for pipx installs.
- The Docker image sets `PROXY_WORKBENCH_DATA=/app/data`.
- The GUI starts its checks as `python -m proxy_workbench …`, or through the executable itself when frozen.

## [1.9.0] — 2026-09-24

### Added

- **Rotating proxy gateway** on `127.0.0.1:8899`. It starts with the GUI (`--gateway-port`, `--no-gateway`) and runs on servers as the `gateway` command (`--rotate`, filters, `--api-token` password for network binds).
  - One address accepts HTTP, CONNECT and SOCKS5 clients.
  - Each new connection goes through the next working proxy, with retries through other proxies and a rest period for failing ones.
- **Browser and client configs:** `proxy.pac` (10 best proxies, no direct fallback) and `clash.yaml` (Clash / Mihomo with an automatic url-test group). Both are in every export and live at `/pac` and `/clash` in the API with its filters.
- **Ready-made checks** for Google, YouTube, Telegram, Discord, Instagram, the OpenAI API, GitHub, Wikipedia and Cloudflare in the GUI.
- **Exit IP and exit country** from the anonymity judge: `exit_ip` / `exit_country` in CSV, JSON and the API; the Country column shows `DE → NL` when they differ.
- **Keep fresh** in the GUI: re-check working proxies every N minutes while the app stays open; the results table, API and gateway follow each new export.
- **Protocol and country summary** of matching proxies above the download buttons and in `status.json` (`breakdown`).

## [1.8.0] — 2026-09-24

### Added

- **SOCKS4 proxies:** collected from five new built-in lists and Geonode, checked through a built-in SOCKS4 connector (httpx has none), exported to `socks4.txt` and `proxychains.txt`, and available in every protocol filter and the API.
- **Protocol detection:** `auto URL` sources and the **Try HTTP, SOCKS4 and SOCKS5** option for your own list (`--detect-protocols`) check addresses without a protocol as all three; whichever works is kept.
- **Any web page as a source:** `text URL` pulls every `ip:port` out of HTML tables, CSV and free text, including rows split across lines. free-proxy-list.net, sslproxies.org and us-proxy.org are built in (55 sources in total).
- **Source upkeep in one click:** **Remove dead sources** drops lists that gave at least 20 checked addresses and none working; **Get new sources** adds lists published on GitHub after your version without bringing back the ones you removed.
- **English terminal messages and `--help`** with examples; the language follows the system and `PROXY_WORKBENCH_LANG` overrides it (#2).

## [1.7.0] — 2026-09-24

### Added

- **Local API for your own programs:** `GET /random`, `/proxies` and `/status` return the latest working proxies with filters for protocol, country, maximum latency, anonymity, limit and format (`json`, `txt`, `hostport`). It starts together with the GUI on `127.0.0.1:8765` (`--api-port`, `--no-api`) and as the `serve` command for servers and Docker. It re-reads each new export automatically, so it can run next to `run --watch` without locking the data folder.
- The Results tab shows the API address with a **Copy** button.
- Network-facing API requires a token (`--api-token` or `PROXY_WORKBENCH_API_TOKEN`); the loopback API checks the `Host` header against DNS rebinding.

## [1.6.0] — 2026-09-24

### Added

- **Re-check only matching proxies** (`--recheck-passing`, GUI button): refreshes the current list in minutes instead of re-scanning the whole database; everything else in the profile stays untouched.
- **Keep the list fresh automatically** (`--watch MINUTES`): after a scan, publish the export, wait, re-check only matching proxies, repeat until stopped.
- **Uptime history:** every re-check (full or passing-only) keeps `checks`, `passes`, first check and last success per proxy; new `uptime` sort, Uptime column in the GUI and `checks`/`passes` in CSV/JSON.
- **Source ratings:** each candidate remembers the list that first delivered it; `status.json` reports checked and matching counts per source (keyed by a hash, no URLs), and the Sources tab shows a “Working” column so dead lists are easy to drop.
- **More export formats:** `hostport.txt` (all protocols, `host:port`) and `proxychains.txt` (ready for the `[ProxyList]` section).

### Changed

- The candidate metadata table gains a `source` column; existing databases are migrated automatically on open.

## [1.5.1] — 2026-09-24

### Fixed

- Full sweeps are fast again: 1.5.0 shuffled every pending address, and random inserts into the large results index slowed SQLite commits (the 190,000-candidate test went from ~1.5 to ~12 minutes on Windows). Shuffling now happens only in find-N mode (`--want`); full sweeps check previously working addresses first and then go in key order.

## [1.5.0] — 2026-09-24

### Added

- **Find N and stop** (`--want N`, GUI “Stop after finding”): the scan ends as soon as enough proxies match; the rest of the list stays pending and a later run continues it.
- **Smarter order:** addresses that already worked in another profile are checked first, the rest in shuffled order, so the first matches arrive quickly and are not all from one subnet or source.
- **Countries:** `--country DE,NL` checks only addresses from those countries (others are skipped before any request), plus a Country column, filter and CSV/JSON field. Countries come from Geonode source data and from the free offline DB-IP Country Lite database (`update-geoip` command or the GUI button; CC BY 4.0).
- **Presets** in the GUI: Quick (1 attempt, short timeouts, 256 workers), Balanced and Thorough (5 attempts).

- **Fail-fast** (on by default, `--no-fail-fast` to disable): a proxy stops being tested as soon as one target can no longer reach the success threshold; rows are marked `aborted`. Aborted rows can never pass the threshold they were checked against.
- **Connect timeout** (`--connect-timeout`, default 4 s, GUI field): dead addresses fail sooner while the whole request keeps the main timeout. With the defaults a worst-case sweep of 190,000 dead candidates drops from about 10 to about 3.5 hours.
- **Selection:** `--protocol all|http|https|socks5`, `--max-latency MS` and `--sort stability` for scans and exports; the same filters, a search box and a “copy this page” button in the results table.

### Changed

- Scan settings now include the connect timeout and fail-fast policy, so results measured with them form a separate profile.

## [1.4.0] — 2026-09-24

### Added

- Docker image for the headless CLI (`Dockerfile`, non-root user, `/app/data` volume), built and smoke-tested in CI and published to the GitHub Container Registry on every release.
- Animated 15-second demo at the top of both READMEs.
- Project website in `docs/index.html` for GitHub Pages (Settings → Pages → Deploy from branch `main`, folder `/docs`).

### Fixed

- Windows: writing progress/status files no longer fails with `PermissionError` when the GUI reads the same file at that moment; the atomic replace is retried briefly.

### Changed

- The release workflow can also be started from the Actions tab with a version tag; it checks the tag against `branding.PRODUCT_VERSION`, creates the tag and uses the matching CHANGELOG section as release notes.

## [1.3.0] — 2026-09-24

### Added

- **Anonymity levels.** Optional judge URL (`--judge-url`, GUI field, config key `anonymity`) rates every working proxy as `transparent`, `anonymous` or `elite`. The machine's own public IP is learned with one direct judge request and kept in memory only.
- `--min-anonymity any|anonymous|elite` filter for scans, exports and the results table; anonymity column, details and level counts in `status.json`.
- Per-protocol `host:port` exports: `http.txt`, `https.txt`, `socks5.txt` (also downloadable from the GUI).
- English interface with an EN/RU toggle next to the theme switch; defaults to the browser language, remembered in `localStorage`, localized number formatting. Server messages and the run log are translated in English mode.
- English `README.md` with screenshots, feature overview, quick start, FAQ and roadmap; full Russian documentation in `README.ru.md`.
- Screenshots and social preview image in `docs/assets/` (synthetic data from documentation IP ranges).
- Pull request template, question issue template, Dependabot configuration, `.editorconfig` and `.gitattributes` (CRLF for `Start.bat`, LF for shell launchers).

### Changed

- CI caches pip downloads and cancels superseded runs.
- Profiles without a judge URL keep their previous identity, so existing results stay valid after the upgrade.

## [1.2.0] — 2026-09-24

### Added

- MIT license с нейтральным copyright holder.
- Public security, privacy, contribution и code of conduct policies.
- Source-only project metadata с Python 3.11+ и pinned `httpx[socks]` dependency.
- Cross-platform CI matrix для локальных mock-based unit tests.
- Structured bug report и feature request issue templates.
- Bounded source downloads, redirect validation with pinned validated IPs, safe-header policy, crash-safe export generations with bounded retention, on-demand result details, and explicit local-data cleanup.

### Changed

- Versioned product identity and reproducible launcher dependency checks.
- Local result details are loaded only when requested; legacy root export names remain compatible with the atomic generation pointer.

## [1.1.0] — Baseline

### Added

- Автономный сборщик и resumable validator публичных HTTP, HTTPS/CONNECT и SOCKS5 proxy candidates.
- Локальный GUI, CLI, SQLite history и exports в TXT, CSV и JSON.
- Несколько service targets, request profiles, expected status/body/hash checks и configurable timeouts/attempts.
- Local denylist, optional DNSBL reputation signals и повторная проверка по выбранному профилю.
- Unit tests с временными базами и локальными HTTP/DNS mocks.

### Security and privacy

- Credential-like request headers отклоняются.
- Custom headers и service configuration остаются локальными.
- Отчёты о URL не сохраняют query, fragment и path.
- Пользовательские настройки и runtime results находятся в игнорируемой папке `data/`.
