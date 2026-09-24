# Changelog / История изменений

Формат следует Keep a Changelog и semantic versioning.

## [Unreleased]

### Added

- Docker image for the headless CLI (`Dockerfile`, non-root user, `/app/data` volume), built and smoke-tested in CI and published to the GitHub Container Registry on every release.
- Animated 15-second demo at the top of both READMEs.
- Project website in `docs/index.html` for GitHub Pages (Settings → Pages → Deploy from branch `main`, folder `/docs`).

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
