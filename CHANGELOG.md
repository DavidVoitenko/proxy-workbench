# Changelog / История изменений

Формат следует Keep a Changelog и semantic versioning.

## [Unreleased]

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
