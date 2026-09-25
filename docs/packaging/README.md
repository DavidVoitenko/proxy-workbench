# Установка, запуск и поставка

Документ описывает только то, что есть в репозитории, и разделяет «собрано и
проверено» с «подготовлено, но требует ключей или другой ОС».

## Что существует на 25 сентября 2026

| Путь | Состояние | Чем подтверждено |
| --- | --- | --- |
| Исходники: `Start.bat`, `run.sh`, `Start.command` | работает, как и раньше | `packaging/smoke.py`, CI |
| wheel + `pipx` | работает, как и раньше | `packaging/smoke.py` на wheel в CI |
| Docker-образ (только CLI) | работает, как и раньше | `docker run … --version` в CI |
| Windows `.exe` из старого spec (`console=True`) | работает, как и раньше | `packaging/smoke.py` в CI |
| **macOS `.app` (arm64) + `.zip` + `.dmg`** | **собирается и проверен на этой машине** | `packaging/build_macos.py`, включая запуск собранного приложения |
| **Windows GUI `.exe` без консоли + отдельный CLI `.exe`** | **spec и сборщик готовы, на Windows не собирались** | `packaging/build_windows.py`; сборка требует Windows |
| **Per-user installer (Inno Setup)** | **скрипт готов, `iscc` здесь нет** | `packaging/windows-installer.iss` |
| **Подпись и нотаризация** | **пайплайн готов, ключей на этой машине нет** | `desktop.signing_status()` → `available: false` |
| Управляемое обновление с проверкой происхождения, backup и откатом | модуль и тесты есть, публикующего сервера нет | `proxy_workbench/desktop.py`, `tests/test_desktop_update.py` |
| Автообновление «из коробки» при запуске | **не сделано**: есть только `--update-notice` | `proxy_workbench/desktop.py` |

Чего в репозитории **нет**: автозагрузки, трея, собственного окна, фоновой
работы без вкладки браузера (F22), автоматической загрузки обновлений,
установленных ключей подписи.

## Выберите один путь

| Путь | Что запускается | Требования | Где будет `data/` |
| --- | --- | --- | --- |
| macOS, релиз | `Proxy Workbench.app` из `.dmg` или `.zip` | macOS 11+, Apple Silicon | per-user, см. ниже |
| Windows, релиз | `…-windows-x64-setup.exe` | Windows x64; Python не нужен | `%LOCALAPPDATA%\proxy-workbench` |
| Windows, portable | `…-windows-x64-portable.zip` | Windows x64; Python не нужен | per-user, см. ниже |
| Windows, исходники | `Start.bat` | Python 3.11+ с Python Launcher (`py`) | `data/` в папке проекта |
| macOS, исходники | `Start.command` или `./run.sh` | Python 3.11+ | `data/` в папке проекта |
| Любая ОС, pipx | `proxy-workbench` | Python 3.11+ и pipx | профиль пользователя |
| Docker | `python proxytool.py …` внутри образа | Docker | `/app/data` в volume |

### macOS: что собрано и что проверено

```sh
python -m venv .venv-build
.venv-build/bin/python -m pip install --requirement requirements.txt pyinstaller==6.22.3
.venv/bin/python packaging/build_macos.py --dist dist
```

Скрипт собирает `Proxy Workbench.app` (onedir, `ARCHFLAGS=-arch arm64`), затем
запускает его с временным `HOME` и проверяет три вещи, каждая из которых
падает с ненулевым кодом:

1. `Info.plist` объявляет `CFBundleIdentifier`, `CFBundleShortVersionString`
   и `CFBundleExecutable`, и исполняемый файл действительно лежит в bundle;
2. `ui/index.html` и `sources.json` физически лежат внутри bundle — иначе
   интерфейс не запустится;
3. приложение стартует, отдаёт страницу с токеном на loopback-порту, выбранном
   ОС, пишет состояние в per-user-папку и **не меняет ни одного файла внутри
   собственного bundle**.

Последняя проверка — это и есть требование «read-only bundle»: установленное
приложение не пишет рядом с собой.

Получившиеся файлы: `Proxy Workbench.app`, `proxy-workbench-<версия>-macos-arm64.zip`,
`proxy-workbench-<версия>-macos-arm64.dmg` и
`proxy-workbench-<версия>-macos-arm64.manifest.json`.

**Intel и universal2.** `build_macos.py` намеренно отказывается собирать
не-`arm64`: `universal2` требует собрать оба среза и объединить их, и этот
скрипт не делает вид, что делает. Пока universal2 нет, на Intel работает
wheel или `Start.command`.

**Подпись и нотаризация macOS.** На машине, где велась разработка, результат
проверки такой:

```
desktop.signing_status()      -> available: false, 'the keychain holds no code signing identity'
desktop.notarization_status() -> available: false, 'no notarization credentials: APPLE_ID, APPLE_PASSWORD, APPLE_TEAM_ID'
codesign -dv --verbose=4 app  -> Signature=adhoc
spctl -a -vvv app             -> rejected
```

Собранный без идентичности bundle получает **ad-hoc** подпись, и
`codesign --verify` на ней проходит. Поэтому `desktop.signature_of()`
возвращает для ad-hoc `signed=False, kind='adhoc'`: считать такую сборку
подписанной нельзя. Проверено на реальной сборке этой версии.

Чтобы собрать подписанный и нотарифицированный артефакт, на машине сборки
должны быть **уже имеющиеся** Developer ID Application в связке ключей и
учётные данные нотаризации:

```sh
python packaging/build_macos.py --sign --notarize --dist dist
```

Если запрошена подпись, а идентичности нет, скрипт завершается кодом 3 и
ничего не собирает. Он не создаёт, не покупает и не хранит никаких
учётных данных.

### Windows: что подготовлено

```powershell
python -m pip install --requirement requirements.txt pyinstaller==6.22.3
python packaging/build_windows.py --dist dist --installer
```

Собираются два бинарника:

| Файл | `console` | Назначение |
| --- | --- | --- |
| `proxy-workbench-gui.exe` | `False` | двойной клик открывает интерфейс, лишней консоли нет |
| `proxy-workbench-cli.exe` | `True` | CLI со своим выводом и кодом возврата |

Разделение не косметическое: у windowed-процесса нет консоли, поэтому CLI не
может быть тем же файлом с аргументом. `packaging/smoke.py` проверяет именно
CLI-бинарник.

Installer — Inno Setup, `PrivilegesRequired=lowest`, установка в
`%LOCALAPPDATA%\Programs\Proxy Workbench`. Приложение пишет только в
per-user-папки, поэтому установщику не нужен администратор, и он ничего не
кладёт в `Program Files`, где данные стали бы доступны только для чтения.

Portable-вариант — zip с обоими бинарниками и `portable-README.txt`.

Подпись Windows использует сертификат, **уже установленный** в хранилище
сертификатов, и выбирается по thumbprint из `PROXY_WORKBENCH_SIGN_THUMBPRINT`.
Пароль нигде не передаётся — ни в argv, ни в файл. Без thumbprint
`--sign` завершается кодом 3.

**Эти сборки на Windows в этой сессии не собирались:** машина сборки — macOS.
Spec и сборщик проверены только чтением и тестами на дескрипторах.

## Путь данных

`proxy_workbench.desktop.resolve_layout()` разрешает три папки в фиксированном
порядке. Первые три правила — новые, остальные сохраняют прежнее поведение.

| # | Условие | `data` | `cache` | `logs` |
| --- | --- | --- | --- | --- |
| 1 | задан `PROXY_WORKBENCH_DATA` | оттуда | `$PROXY_WORKBENCH_CACHE` или `data/cache` | `$PROXY_WORKBENCH_LOGS` или `data/logs` |
| 2 | `PROXY_WORKBENCH_PORTABLE=1` | рядом с программой | там же | там же |
| 3 | есть `proxy-workbench-portable.json` рядом с программой | рядом с программой | там же | там же |
| 4 | установленная сборка (frozen или папка не для записи) | per-user | per-user | per-user |
| 5 | исходники проекта | `data/` рядом с проектом | внутри неё | внутри неё |
| 6 | иначе | per-user | per-user | per-user |

Per-user корни: macOS `~/Library/Application Support`, `~/Library/Caches`,
`~/Library/Logs`; Windows `%LOCALAPPDATA%` и `%APPDATA%`; Linux
`$XDG_DATA_HOME`, `$XDG_CACHE_HOME`, `$XDG_STATE_HOME`.

Правило 5 существует, чтобы ничего не сломать у пользователей исходников,
pipx и Docker: `Start.bat`, `run.sh` и wheel продолжают использовать `data/`
рядом с проектом. Правило 4 — то самое изменение, которого требовал R19:
замороженная сборка больше не пишет рядом с исполняемым файлом, потому что
там нельзя писать.

Посмотреть, что выбрала программа:

```sh
proxy-workbench --print-paths
```

Portable mode включается только явно — либо переменной на один запуск, либо
маркером, который переживает перезапуск:

```sh
proxy-workbench --portable      # пишет proxy-workbench-portable.json рядом с программой
```

Writable-папка сама по себе portable mode не включает: два запущенных
экземпляра иначе разошлись бы по вопросу, где лежат данные.

## Перенос старой папки

Сборка, которая писала `data/` рядом с `.exe` или внутри `.app`, оставляет эту
папку на диске. Перенос — в два шага, первый ничего не пишет:

```sh
proxy-workbench --print-paths            # посмотреть, куда переносить
python - <<'PY'
from proxy_workbench import desktop
plan = desktop.plan_migration(desktop.resolve_layout())
print(plan.as_dict())
PY
python - <<'PY'
from proxy_workbench import desktop
layout = desktop.resolve_layout()
result = desktop.apply_migration(desktop.plan_migration(layout), execute=True)
print(result.as_dict())
PY
```

Правила переноса:

- план по умолчанию — preview, он ничего не пишет;
- файл, который уже есть в целевой папке и отличается по содержимому, —
  **конфликт**, а не повод его перезаписать;
- перед копированием старая папка копируется в backup, база — через SQLite
  backup API, а не побайтово;
- старая папка **не удаляется**: после переноса она остаётся, и в
  `data/migration-receipt.json` записано, какая копия рабочая.

## Управляемое обновление

Есть проверка происхождения, backup, проверка схемы и откат. Нет автоматической
загрузки: публикующего сервера у проекта нет, поэтому сделано только то, что
можно проверить локально.

```sh
export PROXY_WORKBENCH_UPDATE_MANIFEST=/path/to/update.json
proxy-workbench --update-notice
```

Манифест — это JSON:

```json
{
  "version": "2.3.0",
  "url": "https://example.org/proxy-workbench-2.3.0-macos-arm64.dmg",
  "sha256": "…",
  "size": 12345678,
  "min_data_schema": 14,
  "max_data_schema": 14,
  "signature": "…",
  "key_id": "…"
}
```

`sha256` обязателен: манифест без него отвергается. Порядок проверки при
обновлении — четыре независимых отказа, потому что каждый ломается по-своему:

1. **происхождение** — размер и SHA-256 файла совпадают с манифестом; подпись
   проверяется инструментом платформы, и её отсутствие или ad-hoc означают
   `signed=False`;
2. **совместимость схемы** — `PRAGMA user_version` базы пользователя должна
   попадать в `[min_data_schema, max_data_schema]` манифеста и не превышать
   `db.SCHEMA_VERSION` этой сборки;
3. **backup** — папка `data` копируется целиком, база через SQLite backup API;
4. **откат** — предыдущая программа остаётся рядом, а база, которую успела
   переписать более новая сборка, откладывается как `data-from-newer-<время>`,
   а не удаляется. F24 обещает восстановление, а не lossless downgrade.

Публичный API: `desktop.find_update`, `desktop.verify_artifact`,
`desktop.schema_compatible`, `desktop.plan_update`, `desktop.apply_update`,
`desktop.rollback`. Каждая функция по умолчанию только читает: `execute=False`
не трогает ни данных, ни папки программы.

## Что проверяет поставка

`packaging/smoke.py` запускает установленную команду в изолированной временной
папке, поднимает loopback mock proxy, выполняет локальный scan, читает
результат через API и проверяет gateway. Публичные прокси и внешние
endpoint-ы не используются.

```sh
env ALL_PROXY= HTTP_PROXY= HTTPS_PROXY= NO_PROXY=127.0.0.1,localhost \
  .venv/bin/python packaging/smoke.py .venv/bin/python -m proxy_workbench
```

Ожидаемый финальный вывод содержит `smoke test passed`.

`packaging/build_macos.py` дополнительно запускает **собранный** `.app` и
проверяет ресурсы, per-user-путь и неизменность bundle.

`packaging/verify_release.py` пересчитывает SHA-256 каждого артефакта по
манифесту и заново проверяет подпись:

```sh
.venv/bin/python packaging/verify_release.py dist/proxy-workbench-2.2.1-macos-arm64.manifest.json
```

Ненулевой код выхода означает, что публиковать нельзя: артефакт изменился,
пропал, либо манифест заявляет подпись, которую локальные инструменты не
подтверждают.

## Ограничения подписи и публикации

- macOS-сборка на машине без Developer ID **ad-hoc, а не подписана**;
  `spctl` такую сборку отклоняет. Gatekeeper покажет предупреждение, и это
  ожидаемо, а не повод отключать проверку.
- Нотаризация требует `APPLE_ID` + `APPLE_PASSWORD` + `APPLE_TEAM_ID` или
  `NOTARYTOOL_PROFILE`. Пока их нет, notarization не выполняется и в манифесте
  стоит `notarized: false`.
- Windows `.exe` не подписан, пока не задан
  `PROXY_WORKBENCH_SIGN_THUMBPRINT`. SHA-256 подтверждает целостность файла,
  но не личность издателя.
- Наличие checksum не является notarization и не подтверждением издателя.
- Этот проект не создаёт и не покупает signing credentials. Всё, что делает
  пайплайн, — проверяет, есть ли уже имеющиеся.

## Если первый запуск не удался

- **Gatekeeper на macOS:** для сборки без нотаризации выберите **Open once**
  («Открыть»). Не отключайте системную проверку глобально.
- **Permission denied:** проверьте `proxy-workbench --print-paths`. Если
  `data` указывает в read-only-папку (например, внутрь `.app` или
  `Program Files`), задайте `PROXY_WORKBENCH_DATA` или включите portable mode.
- **Data folder is busy:** закройте другое окно GUI/CLI для этой папки; не
  удаляйте lock-файл вручную.
- **Сломанное окружение исходников:** удалите только `.venv` и запустите
  launcher снова; база и настройки в `data/` сохранятся.
