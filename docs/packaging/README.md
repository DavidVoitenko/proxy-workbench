# Установка, запуск и поставка

Этот документ описывает только реальные пути, которые есть в репозитории. Он не
превращает исходный Python-проект в подписанный настольный установщик: текущий
релиз поставляет Windows executable, wheel, исходный архив и Docker-образ, но
не macOS `.app`/`.dmg`.

## Выберите один путь

| Путь | Что запускается | Требования | Где будет `data/` |
| --- | --- | --- | --- |
| Windows, релиз | `proxy-workbench-…-windows-x64.exe` | Windows x64; Python не нужен | `data/` рядом с `.exe` |
| Windows, исходники | `Start.bat` | Python 3.11+ с Python Launcher (`py`) | `data/` в папке проекта |
| macOS, исходники | `Start.command` или `./run.sh` | Python 3.11+ | `data/` в папке проекта |
| Любая ОС, pipx | `proxy-workbench` | Python 3.11+ и pipx | профиль пользователя (см. ниже) |
| Docker | `python proxytool.py …` внутри образа | Docker | `/app/data` в volume |

### Windows executable

Скачайте `.exe` из release, проверьте SHA-256 из accompanying `.sha256` и
поместите файл в папку, в которую обычный пользователь может писать. Текущая
сборка — один PyInstaller console executable (`packaging/proxy-workbench.spec`),
а не installer, не ярлык Start Menu и не приложение с собственным окном. Без
аргументов он открывает GUI в браузере; с аргументами запускает CLI.

Windows executable сейчас **не подписан**. SmartScreen может показать
предупреждение, а SHA-256 подтверждает целостность файла, но не личность
издателя. Не отключайте защиту Windows глобально ради запуска.

### macOS

Готового `.app` или `.dmg` в текущем release workflow нет. macOS-путь — исходная
папка: установите Python 3.11+, запустите `Start.command` двойным кликом и при
первом Gatekeeper-сбое выберите **Open once** («Открыть»). `run.sh` использует
тот же launcher из Terminal. Не переносите `.venv` между компьютерами: удалите
только `.venv` и запустите launcher снова, когда исходники или Python были
заменены.

`.command` — shell-запуск в Terminal, а не подписанное и notarized macOS
приложение. Инструкция не предлагает отключать Gatekeeper или notarization
защиту. Apple Silicon и Intel собираются отдельно, если отдельно появится
native artifact; текущий wheel не заменяет такой проверенный `.app`.

### pipx

```sh
python -m pip install --user pipx
python -m pipx ensurepath
pipx install git+https://github.com/DavidVoitenko/proxy-workbench
proxy-workbench --version
proxy-workbench
```

Команда без аргументов открывает GUI. `proxy-workbench run`, `scan`, `export`
и остальные CLI-команды используют тот же entry point. Для повторного запуска
после изменения зависимостей можно обновить установленный пакет через
`pipx reinstall proxy-workbench`; это не автообновление и не миграция данных.

### Docker

Docker-образ содержит только CLI и не содержит браузерный GUI:

```sh
docker build -t proxy-workbench:local .
docker run --rm -v "$PWD/data:/app/data" proxy-workbench:local --version
```

Для API или gateway из Compose задайте `PROXY_WORKBENCH_API_TOKEN`; compose
поднимает checker, API и gateway из одного volume. Не передавайте реальные
credentials в команды, compose или issue: используйте локальный mock и
временный secret.

## Путь данных и безопасная переустановка

Порядок выбора реализован в `proxy_workbench.paths.default_data()`:

1. `PROXY_WORKBENCH_DATA`, если переменная задана (путь expands `~`);
2. для frozen executable — `data/` рядом с `.exe`;
3. для checkout — `data/` рядом с проектом, если рядом есть `pyproject.toml` и
   `Start.bat`;
4. Windows per-user — `%LOCALAPPDATA%\proxy-workbench` (или стандартный
   `%LOCALAPPDATA%`, если переменная отсутствует);
5. macOS — `~/Library/Application Support/proxy-workbench`;
6. Linux — `$XDG_DATA_HOME/proxy-workbench` или `~/.local/share/proxy-workbench`.

Для переноса или изоляции данных задайте путь до запуска:

```sh
PROXY_WORKBENCH_DATA="$HOME/Documents/proxy-workbench-data" ./run.sh gui
```

Папка должна быть доступна на запись. Не помещайте `.exe` в `Program Files`,
если не задали writable `PROXY_WORKBENCH_DATA`: frozen-режим по умолчанию
создаёт `data/` рядом с executable. В checkout `data/` игнорируется Git и
содержит SQLite, настройки, denylist, отчёты и exports; не переносите её
случайно вместе с `.venv`.

При первом запуске `Start.bat` и `run.sh` создают `.venv`, проверяют Python
3.11+ и устанавливают pinned dependency из `requirements.txt`. Launcher
запоминает digest requirements и повторяет установку только при его изменении.
Прерванную установку можно безопасно повторить; удаляйте `.venv`, а не
`data/`, если окружение повреждено.

## Первый запуск и остановка

1. Запустите launcher из writable-папки и дождитесь сообщения с адресом
   `http://127.0.0.1:<port>/`.
2. Не закрывайте окно Terminal, пока работает GUI: browser tab можно закрыть,
   это не остановит сервер и worker.
3. Для остановки нажмите `Ctrl+C` в окне launcher. GUI сначала просит worker
   остановиться через stop marker, затем закрывает API и gateway. Worker
   обычно завершает текущую пакетную запись и возвращает управляемый код
   остановки; если не успевает, GUI ждёт до 30 секунд и только затем
   принудительно завершает процесс.
4. После `Ctrl+C` повторный запуск той же операции продолжает незавершённые
   кандидаты. Закрытие Terminal окном или `kill -9` не гарантирует такой
   путь.

Один `data/` защищён instance/data lock: второй GUI или CLI, одновременно
работающий с той же папкой, должен быть остановлен. Для изолированных
экспериментов всегда задавайте отдельный `PROXY_WORKBENCH_DATA`.

## Что проверяет поставка

`packaging/smoke.py` запускает установленную команду в изолированной временной
папке, поднимает loopback mock proxy, выполняет локальный scan, читает
результат через API и проверяет gateway. Порт GUI, API и gateway выбирает OS
(`:0`), поэтому тест не требует заранее свободные 18731–18733. Публичные
прокси и внешние endpoint-ы не используются.

Локальная проверка исходного пакета:

```sh
env ALL_PROXY= HTTP_PROXY= HTTPS_PROXY= NO_PROXY=127.0.0.1,localhost \
  .venv/bin/python packaging/smoke.py .venv/bin/python -m proxy_workbench
```

Ожидаемый финальный вывод содержит `smoke test passed`. В CI wheel устанавливается
в чистый venv и тот же smoke запускается на Ubuntu, macOS и Windows; Windows
дополнительно собирает и проверяет PyInstaller `.exe`. Это проверка локального
mock-сценария, а не проверка публичных прокси, физического Mac/Windows E2E,
Gatekeeper/SmartScreen или подписи.

## Ограничения release и подписи

- `.github/workflows/ci.yml` проверяет unit suite и локальный package smoke;
  GitHub Actions не заменяет ручную проверку скачанного файла.
- `release.yml` публикует исходный архив, wheel, Windows x64 executable и
  container image. macOS native app/dmg, installer, auto-updater и rollback
  в текущем workflow отсутствуют.
- Windows executable не code-signed; macOS artifact не notarized, потому что
  native macOS artifact не выпускается. Наличие checksum не является notarization
  или подтверждением издателя.
- Не загружайте реальные credentials, пользовательские базы или публичные
  proxy lists в CI, smoke, issue или release artifact. Для проверки запуска
  используйте `--no-sources`, loopback target и временные данные.

## Если первый запуск не удался

- **Python не найден:** установите Python 3.11+; на Windows включите Python
  Launcher, на macOS проверьте `python3 --version`.
- **Gatekeeper на macOS:** запускайте `Start.command` из локальной папки и
  подтвердите **Open once**; не отключайте системную проверку глобально.
- **Permission denied:** перенесите проект в writable-папку или задайте
  `PROXY_WORKBENCH_DATA` на пользовательскую.
- **Data folder is busy:** закройте другое GUI/CLI-окно для этой папки; не
  удаляйте lock-файл вручную.
- **Сломанное окружение:** удалите только `.venv` и запустите launcher снова;
  база и настройки в `data/` сохранятся.
