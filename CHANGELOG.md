# Changelog / История изменений

The format follows Keep a Changelog and semantic versioning.

## [3.0.2] — 2026-09-27

### Fixed

- Source retries now publish candidates only from a completed response. Byte and line limits keep valid following addresses, and the scan enforces each target's request cap even when one probe makes several requests.
- The API rejects invalid numbers and cursors cleanly, executes concurrent retries with the same idempotency key once, pages scoped jobs and events correctly, and releases SSE slots when clients leave.
- API key forms submit the selected permissions. The import wizard offers every CSV/JSON column, accepts the first or duplicate column, and discards previews after the input changes. Switched tabs remain visible when a browser pauses animations.
- Docker includes the service preset catalog. Its Compose gateway can start on the configured network bind, and the CLI shows a generated password or usable authenticated examples without printing a supplied secret.

## [3.0.1] — 2026-09-27

### Fixed

- **Checks run up to 16× faster on large lists.** Every scan was held at 64 checks in flight whatever the worker setting said (the memory reserved for response bodies had a fixed 64 MiB budget); the budget now grows with the workers up to 1 GiB. Ports of one IP no longer queue up together either: the first port of every host is checked first and further ports follow host by host. A full check of 663,000 collected addresses went from 25 to about 390 addresses per second with 512 workers.
- **macOS app: collecting and HTTPS checks work again.** The 3.0.0 macOS builds looked for trusted certificates in a folder of the build machine, so every HTTPS source failed with a connection error and no proxies could be collected. Every HTTPS connection (sources, HTTPS checks, the gateway's HTTPS upstreams) now also trusts the certificate bundle shipped inside the app, while the system store is still used.
- **The rotating proxy is far more reliable with flaky free proxies.** A proxy that does not answer within 2 seconds no longer blocks the request: the next proxy is tried alongside it and the first working tunnel wins (up to 4 proxies; a failed request now gives up after about 14 s instead of 24 s). When every proxy in a small pool was resting after failures, the gateway answered 502 instantly for five minutes; it now falls back to the proxy that is due back first. On 11 real public proxies right after a restart: 18 of 20 requests succeeded instead of 6 of 10.
- Simultaneous gateway connect outcomes now record failed proxies before serving the winner. Proxies still connecting after another wins are marked slow, so they stop delaying each request. A failover session is pinned to the tunnel that actually won, and strict sessions keep their single-proxy rule.
- The results view looks up source provenance only for its selected profile, avoiding a scan of every collected candidate on each request.
- **“This data folder is already used by another run” when starting a check.** The desktop app's background job runner and scheduler hold the data lock for a moment every second, and a check started from the interface gave up on the first try. A run now waits up to 10 seconds for such a short hold; a folder that stays busy is still refused.
- The interface server reads or closes the body of a request it refuses, so a refused request no longer shows up as a reset connection on Windows or as a garbled next request.

## [3.0.0] — 2026-09-27

The biggest release so far: a new interface, a source catalog of 150 lists, a desktop app that lives in the menu bar, persistent proxy pools with schedules, API keys, diagnostics that explain an empty result, and backups you can preview before restoring. Existing data folders are upgraded in place, with an automatic copy taken first.

### Highlights

- **Redesigned interface.** A new dark-first design layer with a reworked light theme, consistent typography, micro-animations and a live feed of the running check. The layout adapts to phones and tablets (a side rail on tablets, a compact layout on phones), and heavy tables stay smooth on large result sets.
- **12 interface languages.** English and Russian are joined by German, Spanish, French, Italian, Japanese, Polish, Portuguese, Turkish, Ukrainian and Chinese, every one of them complete.
- **Source catalog: 150 sources, 117 of them fully free and public; 106 feeds are collected out of the box.** Every entry says who publishes it, what it serves (proxy list, subscription config, API), whether an account is needed, which protocols it carries and how it was verified. Ready-made sets (quick, all supported, extended, by protocol), one-click enable/disable, per-source reports and a comparison that spots lists which republish each other. Only sources marked safe to collect are fetched; commercial, trial and rejected entries are shown for reference.
- **Desktop app.** Starting `proxy-workbench` without arguments now launches the desktop app: a macOS menu-bar icon, a single running instance, optional start at login, and correct recovery after sleep/wake. `proxy-workbench gui` (or `--no-desktop`) opens only the web interface, as before.
- **Proper installers.** A macOS `.app`/`.dmg` (Apple Silicon and Intel) and Windows GUI and CLI executables with a per-user installer; Linux installs with `pipx` or Docker. Data lives in per-user folders, with a portable mode and automatic migration of an old `data/` folder.

### Added

- **Pools.** A named pool keeps N working proxies for a profile, with a reserve, quotas and resource budgets; it refills and re-checks itself, and tells you why it is short.
- **Schedules.** Re-check collections or pools on an interval, in your time zone, with quiet hours, request/byte budgets and notifications when a proxy's state changes.
- **Named profiles with revisions and presets.** Target rules, success criteria and settings are saved as profiles; every change is a revision, and profiles can be shared without secrets.
- **Collections and import.** Bring your own lists from TXT, URI, CSV, JSON, Clash or sing-box files with a preview, column mapping and a report of every rejected line; imports are transactional and merge or replace.
- **Service catalog.** Ready-made checks for popular services, grouped into sets, with an explicit rule for which fields a preset overwrites.
- **Geography and exit country.** Filter by the proxy's own country, the country traffic actually exits from, or either; exclude countries; choose how unknown locations are treated.
- **Rotating gateway, upgraded.** Bind the gateway to a pool from the GUI, serve it on your LAN (`--lan`, `--gateway-interface`), and use upstream proxies that need a login (HTTP Basic, SOCKS5 username/password). The gateway has its own password (`--gateway-token`), separate from the API token.
- **API keys.** Create named keys with permissions, collection/pool scope, rate and concurrency limits, expiry, rotation with a grace window, revocation and an audit log — in the GUI (Keys page) and the CLI (`api-key`). A secret is shown exactly once.
- **Secret store and access identities.** Proxy credentials are stored separately and referenced, never copied into exports or diagnostics.
- **Diagnostics.** A funnel shows where candidates were lost; "why 0 results" explains an empty run in plain words; a health check; and a redacted diagnostic bundle you can review before saving (`diagnose funnel|zero|control|health|bundle`).
- **Backup, restore and retention with preview.** `backup create|list|verify|preview|restore|rollback|retention|cleanup|rebind`; every change is previewed first and runs only with `--apply` (or the confirm button in the GUI).
- **Run budgets and "find N".** `--want N` with `--count-what endpoint|ip|exit`, `--deadline`, `--max-requests`, `--run-max-bytes`; a run that falls short says which unit it could not reach.
- **Export targets.** `--client-target` / `--client-binary` validate sing-box output for a specific client version.
- **Versioned control API `/v1`** for scripts and integrations: collections, sources, profiles, jobs, results, pools, gateway, schedules, exports, imports and keys. A machine-readable description ships as `proxy_workbench/openapi.json`.

### Changed

- **Breaking:** running `proxy-workbench` / `python -m proxy_workbench` with no arguments opens the desktop app instead of only the web interface. Use `proxy-workbench gui` for the old behaviour. All CLI commands are unchanged.
- **Breaking:** the database schema moves to version 20. Older data folders (1.x and 2.x) are migrated on first start; a backup is taken before any non-additive step and can be restored with `backup rollback`. Do not open an upgraded folder with 2.x.
- **Breaking:** the rotating gateway no longer accepts the API token as its password; set `--gateway-token` / `PROXY_WORKBENCH_GATEWAY_TOKEN`. The bundled `compose.yml` already does.
- Each proxy address is now one row per profile and access identity, so an address no longer appears twice in `proxies.txt`, `ranked.*`, `proxy.pac`, `clash.yaml`, `singbox.json` or `/proxies`.
- Python 3.11 or newer; CI tests 3.11, 3.12 and 3.14 on Linux, macOS and Windows.

### Fixed

- **Checks are much faster on real-world lists.** A scan no longer waits on its own database lock for every job item, and dead proxies no longer push the number of parallel checks down to one: 3,000 mostly dead candidates now take seconds instead of hours. Running out of file descriptors is no longer recorded as a dead proxy.
- **The database no longer grows without limit under `--watch`.** Each source keeps its last three downloaded lists; older ones are pruned right after a collection, and `backup retention` cleans up history left by earlier versions. A list the server reports as unchanged is re-applied to collections that lost it, and a retry after a broken download no longer counts the first attempt against the size limit.
- **Collecting is faster and reads more lists.** Addresses are written in batches (a full collection of the 106 default feeds went from 133 s to 79 s); four sources that returned nothing (hideip.me, spys.me and others with `ip:port` lines and comments) now return addresses; a list that exceeds the size limit is reported as a partial read instead of a failing provider and is no longer put into backoff; a list without country data no longer erases a country learned elsewhere; cached lists are re-read after a failed or refused download.
- **Control API checked operation by operation.** The audit log is written; a key limited to one collection or pool can no longer act on others through body or query fields; event streams deliver events; result paging moves past the first page and every declared filter and sort works; unknown jobs, pools and sources answer 404; refreshing a source returns a job; PUT and wrong methods get JSON errors; `localhost` reaches `/v1`.
- The web interface no longer puts the administrator key in a URL.
- Collections can be renamed, archived and restored through the API with revision checks; merge and replace work; members are validated; pools and schedules refuse unknown profiles and collections; gateway settings and bindings set through the API are stored and listed; an active key must be revoked before it is deleted; with `serve --host 0.0.0.0` the API accepts the machine's own addresses and hostname.
- **Stopping and resuming works.** A stopped or crashed check no longer leaves its job running, which made every later check fail with “Busy”; running the same command again continues where it stopped without re-measuring finished addresses.
- **`--watch` really re-checks.** Every round now measures the passing proxies again; before, rounds after the first measured nothing and republished old results.
- **A busy host no longer holds up other hosts**, and `--want` stops as soon as the target is reached even when workers waited for a host (30 ports on one IP with `--want 5`: 8 measured instead of 30).
- `hostport.txt` lists an address once even when it passed as several protocols; a speed test sample below 1 MiB is refused because it can never give a result.
- The chosen interface language is kept after a restart; before, ten of the twelve languages fell back to English on the next start.
- Results table cells stay under their own headers when some columns are hidden.
- Results table text is readable in the light theme.
- A fresh failure now withdraws an older success, so a published list no longer serves an address the latest check rejected.
- Retention cleanup runs instead of being rolled back by SQLite.
- Keys limited to one collection can no longer read other collections' jobs, profiles, sources, schedules or artifacts.
- `--max-requests` and `--run-max-bytes` actually limit a run.
- Pool refill, start, pause and member listing through the API work.
- Collecting treats HTTP 304 as a cached answer, not an empty list; oversized sources are capped without losing already parsed data.
- Windows: time zones, HTTP handling and coarse-clock timing issues.
- Windows installer: the “Command line” shortcuts open a console with the CLI instead of doing nothing, and a new PATH entry works in new consoles without signing out.
- Windows: the app can be quit from the Start menu (“Quit Proxy Workbench”, or `proxy-workbench --quit`), and uninstalling quits a running app first.

### Known limitations

- Checking your own proxies that require a login is not supported yet: the checker refuses credentials in proxy URLs (the gateway can use them).
- The menu-bar icon exists on macOS only; sleep/wake is not detected natively on Windows.
- Builds are not code-signed or notarized: macOS will ask you to confirm the first launch, Windows SmartScreen may warn.
- There is no automatic updater; the app can tell you an update exists.
- Gateway settings saved through the API are stored and listed but not yet applied when the gateway starts; the command line and interface options are used.
- The Windows uninstaller leaves the optional PATH entry in place.
- Figures in this release come from local tests and mock services; the quality of public proxies was not measured.

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
