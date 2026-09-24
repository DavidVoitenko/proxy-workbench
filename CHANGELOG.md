# Changelog / История изменений

Формат следует Keep a Changelog и semantic versioning.

## [Unreleased]

### Added

- English `README.md` with screenshots, feature overview, quick start, FAQ and roadmap; full Russian documentation moved to `README.ru.md`.
- Screenshots and social preview image in `docs/assets/` (synthetic data from documentation IP ranges).
- Pull request template, question issue template, Dependabot configuration, `.editorconfig` and `.gitattributes` (CRLF for `Start.bat`, LF for shell launchers).
- Pip caching and superseded-run cancellation in CI.

- MIT license с нейтральным copyright holder.
- Public security, privacy, contribution и code of conduct policies.
- Source-only project metadata с Python 3.11+ и pinned `httpx[socks]` dependency.
- Cross-platform CI matrix для локальных mock-based unit tests.
- Structured bug report и feature request issue templates.
- English translation of the browser GUI with an EN/RU toggle next to the theme switch. The language defaults to the browser locale, is remembered in `localStorage`, and also localizes number formatting. Server-side error messages and the execution log are still Russian only.
- Bounded source downloads, redirect validation with pinned validated IPs, safe-header policy, crash-safe export generations with bounded retention, on-demand result details, and explicit local-data cleanup.

## [1.2.0] — In development

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
