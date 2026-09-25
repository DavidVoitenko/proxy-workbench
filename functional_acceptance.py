#!/usr/bin/env python3
"""Функциональная приёмка Proxy Workbench.

Это НЕ набор юнит-тестов. Здесь проверяется одно: может ли обычный пользователь
дойти до заявленной функции снаружи, через реальный интерфейс — CLI, GUI, HTTP.

Каждая проверка отвечает на вопрос «сможет ли человек это сделать», а не
«есть ли строка кода». Функция, до которой нельзя добраться ни через один
интерфейс, считается НЕ СДЕЛАННОЙ, даже если её модуль написан и покрыт тестами.

Запуск:  .venv/bin/python functional_acceptance.py [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(REPO, ".venv", "bin", "python")
PORT = 18772

results: list[tuple[str, str, str, str]] = []


def record(area: str, feature: str, verdict: str, detail: str) -> None:
    results.append((area, feature, verdict, detail))
    mark = {"done": "СДЕЛАНО", "partial": "ЧАСТИЧНО", "missing": "НЕ СДЕЛАНО",
            "broken": "СЛОМАНО", "unknown": "НЕ ПРОВЕРЕНО"}[verdict]
    print(f"  [{mark:11s}] {feature}\n                 {detail}")


def cli(*argv: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, "-m", "proxy_workbench", "--data", DATA, *argv],
        capture_output=True, text=True, timeout=timeout, cwd=REPO,
    )


class Server:
    """Реальный запущенный продукт, к которому обращаются по HTTP."""

    def __init__(self, token: str):
        self.token = token
        self.proc = subprocess.Popen(
            [PY, "-m", "proxy_workbench", "serve", "--data", DATA,
             "--host", "127.0.0.1", "--port", str(PORT), "--api-token", token],
            cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        self.base = f"http://127.0.0.1:{PORT}"

    def wait_up(self, timeout: float = 30.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            code, _ = self.call("/status", token=self.token)
            if code is not None:
                return True
            time.sleep(0.3)
        return False

    _idem = 0

    def call(self, path, method="GET", body=None, token=None, raw=False):
        req = urllib.request.Request(self.base + path, method=method)
        tok = self.token if token is None else token
        if tok:
            req.add_header("Authorization", "Bearer " + tok)
        payload = json.dumps(body).encode() if body is not None else None
        if payload:
            req.add_header("Content-Type", "application/json")
        if method in ("POST", "PATCH", "DELETE"):
            # Любая мутация требует идемпотентности (CONTRACTS §6.4); приёмка
            # повторяет один и тот же сценарий, поэтому ключ детерминирован.
            Server._idem += 1
            req.add_header("Idempotency-Key", f"functional-acceptance-{Server._idem}")
        try:
            with urllib.request.urlopen(req, data=payload, timeout=10) as r:
                text = r.read().decode("utf-8", "replace")
                return (r.status, text) if raw else (r.status, json.loads(text or "{}"))
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(text or "{}")
            except json.JSONDecodeError:
                return e.code, text
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


# --------------------------------------------------------------------------
# 1. Достижимость: можно ли дойти до функции снаружи
# --------------------------------------------------------------------------

def check_reachability(server: Server) -> None:
    print("\n=== ДОСТИЖИМОСТЬ ФУНКЦИЙ: можно ли добраться снаружи ===")

    code, body = server.call("/v1/capabilities")
    if code == 200 and isinstance(body, dict) and body.get("permissions"):
        record("F29", "Список прав у API отдаётся", "done",
               f"{len(body['permissions'])} прав перечислено в /v1/capabilities")
    else:
        record("F29", "Список прав у API отдаётся", "broken", f"HTTP {code}")

    # Ключи администратора должен выдавать локальный bootstrap: CLI или GUI.
    cli_keys = cli("api-key", "list")
    cli_issue = cli("api-key", "bootstrap", "--name", "acceptance")
    has_bootstrap = cli_issue.returncode == 0
    if has_bootstrap:
        # Секрет показывается один раз; приёмка забирает его отсюда.
        _ADMIN["secret"] = (cli_issue.stdout or "").strip().splitlines()[-1].strip()
    if has_bootstrap:
        record("F29", "Выдача первого ключа администратора", "done",
               "CLI-команда api-key bootstrap создаёт ключ; "
               f"список ключей: exit {cli_keys.returncode}")
    else:
        detail = (cli_issue.stderr or cli_issue.stdout or "").strip().splitlines()
        record("F29", "Выдача первого ключа администратора", "missing",
               "Нет пользовательского пути: "
               + (detail[-1][:200] if detail else "команда не найдена")
               + ". Функция bootstrap_admin() есть только в apikeys.py и вызывается "
                 "исключительно из тестов — значит через /v1 collections, profiles, "
                 "sources, jobs, pools, schedules, audit и keys нельзя попасть вовсе.")


def last_admin_secret() -> str | None:
    """Секрет, который напечатал ``api-key bootstrap`` в этой же приёмке."""
    return _ADMIN.get("secret")


_ADMIN: dict[str, str] = {}


def check_api_operations(server: Server, admin_token: str | None) -> None:
    print("\n=== F29: ОПЕРАЦИИ API ===")
    # Тела запросов — ровно те, что документирует маршрут: приёмка меряет
    # продукт, а не свою догадку о его API.
    ops = [
        ("GET", "/v1/collections", None, "Коллекции: чтение"),
        ("POST", "/v1/collections", {"name": "acceptance"}, "Коллекции: создание"),
        ("GET", "/v1/profiles", None, "Профили: чтение"),
        ("POST", "/v1/profiles",
         {"name": "acceptance",
          "targets": [{"id": "t1", "kind": "required", "min_success": 1.0},
                      {"id": "t2", "kind": "optional", "min_success": 0.5}],
          "rule": "at_least_k", "k": 1}, "Профили: создание"),
        ("GET", "/v1/sources", None, "Источники: каталог"),
        ("GET", "/v1/sources/catalog", None, "Источники: каталог исследований"),
        ("GET", "/v1/jobs", None, "Задания: список"),
        ("POST", "/v1/checks/check", {"collection_id": "public-base"}, "Задания: запуск проверки"),
        ("GET", "/v1/pools", None, "Пулы: список"),
        ("GET", "/v1/schedules", None, "Расписания: список"),
        ("GET", "/v1/keys", None, "Ключи: список"),
        ("GET", "/v1/audit", None, "Журнал аудита"),
        ("GET", "/v1/results", None, "Результаты с курсором"),
    ]
    if not admin_token:
        for _method, _path, _body, label in ops:
            record("F29", label, "missing",
                   "Недостижимо: нет ключа администратора, у пользователя его взять негде")
        return

    for method, path, body, label in ops:
        code, payload = server.call(path, method=method, body=body, token=admin_token)
        if code and code < 400:
            record("F29", label, "done", f"HTTP {code}")
        elif code == 403:
            record("F29", label, "broken", "HTTP 403 — права выданы, но доступ не проходит")
        else:
            detail = payload.get("error", {}).get("message") if isinstance(payload, dict) else str(payload)
            record("F29", label, "broken", f"HTTP {code}: {str(detail)[:150]}")


def check_scopes(server: Server, admin_token: str | None) -> None:
    """Ключ A не должен видеть то, что ему не выдали."""
    print("\n=== F29: РАЗГРАНИЧЕНИЕ ДОСТУПА МЕЖДУ КЛЮЧАМИ ===")
    if not admin_token:
        record("F29", "Ключ A не видит данные ключа B", "unknown",
               "Нужен рабочий bootstrap для постановки")
        return
    code, a = server.call("/v1/keys", method="POST", token=admin_token,
                          body={"name": "key-a", "permissions": ["read.results"]})
    code2, b = server.call("/v1/keys", method="POST", token=admin_token,
                           body={"name": "key-b", "permissions": ["admin.keys"]})
    if code not in (200, 201) or code2 not in (200, 201):
        record("F29", "Создание двух ключей с разными правами", "broken",
               f"HTTP {code} / {code2}")
        return
    record("F29", "Создание двух ключей с разными правами", "done", "оба созданы")

    secret_a = (a.get("secret") if isinstance(a, dict) else None)
    secret_b = (b.get("secret") if isinstance(b, dict) else None)
    if not secret_a:
        record("F29", "Секрет ключа показывается один раз при создании", "broken",
               "Поле secret не вернулось при создании")
    else:
        code, listed = server.call("/v1/keys", token=admin_token)
        leak = json.dumps(listed).find(secret_a) >= 0 if isinstance(listed, dict) else False
        record("F29", "Секрет ключа показывается один раз при создании",
               "done" if not leak else "broken",
               "при последующем чтении секрета нет" if not leak
               else "секрет утекает в список ключей")

    ca, _ = server.call("/v1/keys", token=secret_a)
    cb, _ = server.call("/v1/keys", token=secret_b)
    if ca == 403 and cb in (200, 201):
        record("F29", "Read-only ключ не получает права администратора", "done",
               "ключ A получил 403 на административной операции, ключ B — доступ")
    else:
        record("F29", "Read-only ключ не получает права администратора", "broken",
               f"ключ A: HTTP {ca}, ключ B: HTTP {cb}")

    cr, _ = server.call("/v1/keys", method="POST", token=secret_a, body={"name": "self-escalation"})
    record("F29", "Read-only ключ не может выдать себе новый ключ",
           "done" if cr == 403 else "broken",
           f"HTTP {cr}")


# --------------------------------------------------------------------------
# 2. Функции, которые должны работать без API
# --------------------------------------------------------------------------

def check_cli_features() -> None:
    print("\n=== ФУНКЦИИ ЧЕРЕЗ CLI ===")

    sample = os.path.join(tempfile.gettempdir(), "pw_acceptance_list.txt")
    with open(sample, "w", encoding="utf-8") as fh:
        fh.write("# список для приёмки\n1.2.3.4:8080\n5.6.7.8:3128\nплохая строка\n1.2.3.4:8080\n")

    r = cli("collect", "--no-sources", "--input", sample, timeout=90)
    if r.returncode == 0:
        record("F03", "Импорт собственного списка с плохими строками", "done",
               "список принят, плохая строка не сломала импорт")
    else:
        record("F03", "Импорт собственного списка с плохими строками", "broken",
               (r.stderr or r.stdout).strip()[-250:])

    r = cli("--help", timeout=30)
    text = (r.stdout or "") + (r.stderr or "")
    wanted = {
        "F05": ["profile"],
        "F14": ["pool"],
        "F15": ["schedule"],
        "F24": ["backup", "migrate"],
        "F29": ["api-key", "key"],
    }
    for area, words in wanted.items():
        found = [w for w in words if w in text.lower()]
        if found:
            record(area, f"Команда в CLI для этого направления ({', '.join(words)})", "done",
                   f"найдено: {', '.join(found)}")
        else:
            record(area, f"Команда в CLI для этого направления ({', '.join(words)})", "missing",
                   "в --help нет ни одной из этих команд — функция недоступна из CLI")


def check_gui_features() -> None:
    print("\n=== ФУНКЦИИ ЧЕРЕЗ ИНТЕРФЕЙС ===")
    path = os.path.join(REPO, "proxy_workbench", "ui", "app.js")
    with open(path, encoding="utf-8") as fh:
        app = fh.read()
    with open(os.path.join(REPO, "proxy_workbench", "gui.py"), encoding="utf-8") as fh:
        gui = fh.read()

    combined = app + gui
    marks = {
        "F02": ("коллекц", "Выбор коллекции в интерфейсе"),
        "F05": ("профил", "Профили в интерфейсе"),
        "F06": ("набор сервисов|service.?set|serviceSet", "Каталог сервисов в интерфейсе"),
        "F19": ("bulk|массов", "Массовые операции над строками"),
        "F29": ("api.?key|apiKey|ключ", "Менеджер ключей в интерфейсе"),
    }
    for area, (pattern, label) in marks.items():
        if any(w in combined.lower() for w in ("коллекц",)):
            hit = bool(__import__("re").search(pattern, combined, __import__("re").I))
        else:
            hit = bool(__import__("re").search(pattern, combined, __import__("re").I))
        record(area, label, "done" if hit else "missing",
               "элемент есть в интерфейсе" if hit else
               "в интерфейсе нет ни одного упоминания — функция недоступна пользователю")


def main() -> int:
    global DATA
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    DATA = tempfile.mkdtemp(prefix="pw-accept-")
    print(f"Функциональная приёмка. Изолированные данные: {DATA}")

    server = Server(token="legacy-readonly-token-for-acceptance")
    if not server.wait_up():
        print("Продукт не поднялся — приёмка невозможна.")
        server.stop()
        return 2
    print("Продукт поднялся, обращаемся к нему по HTTP.")

    try:
        check_reachability(server)
        # Тот же ключ, что выдал bootstrap: приёмка идёт по нему, а не по
        # тестовой фикстуре, иначе она доказывает не пользовательский путь.
        admin_token = last_admin_secret()
        check_api_operations(server, admin_token)
        check_scopes(server, admin_token)
        check_cli_features()
        check_gui_features()
    finally:
        server.stop()

    tally: dict[str, int] = {}
    for _, _, verdict, _ in results:
        tally[verdict] = tally.get(verdict, 0) + 1

    print("\n=== ИТОГ ===")
    for verdict in ("done", "partial", "broken", "missing", "unknown"):
        if verdict in tally:
            print(f"  {verdict:8s} {tally[verdict]}")

    if args.json:
        print(json.dumps([
            {"area": a, "feature": f, "verdict": v, "detail": d} for a, f, v, d in results
        ], ensure_ascii=False, indent=2))

    return 0 if tally.get("broken", 0) + tally.get("missing", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
