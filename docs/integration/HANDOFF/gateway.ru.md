# Handoff: gateway

**Требования:** F16, дефекты 16, 17, 18, 19; R11, R12, R13
**Контракт:** `docs/integration/CONTRACTS.ru.md` §1.2(3,5), §2.3, §4.4, §5.1, §5.5, §6.3, §7.1, §7.2 (версия 1)
**База этого прохода:** ветка `integration/ultra-2026-09-25`, HEAD `c9e7fd8`; предыдущая редакция писалась на `a877c2c`, с тех пор `gui.py`, `api.py`, `proxytool.py`, `reputation.py` и соседние модули изменились — ниже это перепроверено на текущей ревизии.
**Мои файлы:** `proxy_workbench/gateway.py`, `tests/gateway_support.py`, `tests/test_gateway.py`, `tests/test_gateway_reservation.py`, `tests/test_gateway_health.py`, `tests/test_gateway_bind.py`, `tests/test_gateway_denylist.py`, `tests/test_gateway_rotation.py`, `tests/test_gateway_transports.py`

`reputation.py` я **не менял**: шлюзу достаточно `Denylist.match()`, `Denylist.from_file()` и `Denylist.digest`, и это снимает конфликт двух писателей из `HANDOFF/README.ru.md` §1.2/§8 п.1.

---

## 0. Что изменилось в этом проходе

Четыре дыры найдены **после** предыдущей правки и все четыре закрыты. Первая редакция этого handoff утверждала, что дефект 16 закрыт полностью — это было неверно: слот брался до `await`, но **терялся** в окне между успешным `connect()` и входом в `relay()`.

| # | Что было | Стало | Требование |
| --- | --- | --- | --- |
| 1 | `handle_http`/`handle_socks5` брали lease, потом `await writer.drain()` для ответа клиенту. Отмена, дедлайн handshake или оборванный клиент **в этом окне** оставляли `pool.active[proxy] == 1` навсегда | окно закрыто `try/except BaseException` → `Gateway._discard()` возвращает слот и закрывает туннель | дефект 16, R11 |
| 2 | `Pool.rows` вычитался из denylist **на месте**. Удалённое правило не возвращало прокси: пул монотонно сжимался до следующей публикации | `Pool.source_rows` (что дал экспорт) и `Pool.rows` (source минус denylist) разделены; список выводится заново по ключу `(export key, export revision, denylist digest)` | дефект 19, R13 |
| 3 | `max_session` приводился к `int()`, поэтому любой дробный лимит становился `0`, а `0` — это документированное «без лимита» | `max(0.0, float(max_session))`; счётчик `stats['capped']` отделён от `closed_idle` | F16 «долгие соединения имеют понятную политику» |
| 4 | `Pool.revoke_streams(close)` был мёртвым кодом, а `Gateway.set_denylist` держал **вторую** копию решения «рвать ли поток» | политика живёт в одном месте, `Gateway.set_denylist` спрашивает `Pool.revoke_streams()` | дефект 19, R13 |

Позиционный порядок аргументов `start()` и `Background()` сохранён; существующие вызовы продолжают работать.

### 0.1 Что снято из контракта

| Что | Статус | Почему |
| --- | --- | --- |
| `gateway.IDEMPOTENT` | **удалено** | Константа не имела ни одного чтения (`grep` по `proxy_workbench/gateway.py` и `tests/test_gateway*.py`). Она описывала политику «можно повторять идемпотентный метод», которой в коде нет: `ReplayGuard.write_once` запрещает повторную запись **любого** запроса, а `Gateway.connect` повторяет попытку, только пока ничего не записано. Список разрешённых методов вводил в заблуждение — он выглядел как действующий, а действующим было более строгое правило |
| `Pool.revoke_streams(close)` → `Pool.revoke_streams(force=None)` | **сигнатура изменена** | Метод не вызывался ниоткуда, кроме упоминания в докстринге. Теперь он единственный носитель политики `on_deny`. `force` перекрывает настроенную политику на один вызов; `None` означает «спроси у `on_deny`» |
| `Pool.rows` | **остаётся**, рядом появился `Pool.source_rows` | `rows` — то, что клиенту можно выдать; `source_rows` — то, что дал экспорт. Оба публичные, оба без файлового ввода-вывода |
| `Pool.stats['capped']` | **добавлено** | Счётчик виден в `snapshot()` и в `state()`, то есть в GUI и в API |

---

## 1. Прошу внести в чужие файлы

### 1.1 `proxy_workbench/gui.py`: `--lan` объявлен, но не доходит до шлюза (дефект 18, R12)

Флаги уже добавлены и правильные: `--gateway-host` по умолчанию `127.0.0.1` (`gui.py:2441`), `--lan` (`gui.py:2445`), `--gateway-token` (`gui.py:2447`), а пароль генерируется на каждый запуск и не берётся из `server.app.token` (`gui.py:2485-2491`). **Осталось одно: `lan` не передаётся.**

**Где:** `gui.py:2497` и `gui.py:916`.

```python
# gui.py:2497 — сейчас
server.app.gateway = gateway.Background(args.data, args.gateway_host, args.gateway_port, token=gateway_token)
# прошу
server.app.gateway = gateway.Background(args.data, args.gateway_host, args.gateway_port,
                                        token=gateway_token, lan=args.lan,
                                        interface=args.gateway_interface)
```

**Что происходит без этого (проверено в этой сессии):**

```
$ .venv/bin/python proxytool.py --data <tmp> --host 0.0.0.0 gateway
Gateway not started: the address 0.0.0.0 is reachable from the network: enable LAN explicitly (bind.lan=True) and choose an interface.
```

`gateway.Bind` сам отказывает не-loopback адресу без `lan=True` (это намеренно: локальный default остаётся локальным), но `--lan` без передачи аргумента означает, что **включить LAN из GUI нельзя вообще** — флаг печатает предупреждение и всё равно не поднимает слушатель. Тот же вызов в `App.start_gateway` (`gui.py:916`) нужен для кнопки «запустить снова»: он берёт `self.gateway_bind`, поэтому `lan` и `interface` надо сохранять в этом словаре рядом с `host`/`port` (`gui.py:2484`).

**Про текст ошибки.** Сообщение `Bind.__post_init__` называет внутреннее имя аргумента (`bind.lan=True`). Для CLI это бесполезно (см. §1.4); прошу в точке вызова ловить `ValueError` и печатать своё, пользовательское сообщение, а внутреннюю формулировку оставить для разработчика.

**Что НЕ ломается:** `--no-gateway`, локальный режим, печать адреса и QR. `api.is_loopback` импортируется как раньше.

### 1.2 `proxy_workbench/gui.py`: выбор интерфейса и видимое состояние (F16, дефект 18)

**Где:** `App.gateway_state` (`gui.py`).

`runner.server.gateway.pool.snapshot(top=5)` продолжает работать (файлов не читает), но в ответе не хватает:

```python
state = dict(snapshot)
state.update(
    lan=runner.lan,                                  # LAN включён или нет
    interface=runner.bind.published_host,            # какой адрес показан телефону
    token_origin=runner.token_origin,                # 'none' | 'explicit' | 'generated'
    interfaces=gateway.lan_interfaces(),             # выбор интерфейса в GUI
    pools=snapshot['binding'],                       # pool_id/generation/profile/policy
    strategies=gateway.STRATEGIES,
    sticky_modes=gateway.STICKY_MODES,
    revoke_policies=gateway.REVOKE_POLICIES,
    on_deny=snapshot['on_deny'],
    denied=snapshot['denied'],
)
```

`--gateway-interface` из §1.1 и поле `interface` в `gateway_bind` — часть этого: без выбора интерфейса на машине с несколькими адаптерами QR указывает адрес, по которому телефон не дойдёт.

**Секреты:** `state()` и `snapshot()` не содержат значения пароля — проверено тестом `tests/test_gateway_bind.py::test_gateway_state_exposes_no_password`.

### 1.3 `proxy_workbench/gui.py`: кнопка «запретить» — отзыв, а не только запрет вперёд (дефект 19, R13)

**Где:** место, где GUI сохраняет `denylist.txt`.

**Прошу** после записи файла сказать пользователю явно, потому что теперь **два разных действия**:

- **запретить впредь** — правило попадёт в `data/denylist.txt`, шлюз подхватит его при следующем обновлении пула (`refresh_interval`, по умолчанию 2 с) и **отзовёт новые допуски**;
- **отозвать из активного пула** — дополнительно вызвать `gateway.set_denylist(...)` у живого `Background`, чтобы отзыв был немедленным.

```python
closed = server.app.gateway.set_denylist(Denylist.from_file(path, normalizer=core.normalize))
# closed — список прокси, чьи открытые потоки закрыты; пусто при on_deny='keep'
```

Судьба уже открытых потоков — отдельная явная настройка `on_deny`:
- `'keep'` (по умолчанию) — открытый поток не рвётся: байты уже в проводе, обрывать его должен пользователь;
- `'close'` — шлюз закрывает потоки запрещённых адресов и возвращает их список.

**Что изменилось с прошлой редакции:** правило обратимо. Раньше `Pool.rows` вычитался на месте, и удалённое правило **не возвращало** прокси до следующей публикации. Теперь список выводится заново, поэтому «запретил — потом передумал — снял правило» работает, и кнопка «запретить» больше не может тихо и навсегда вычеркнуть адрес. Это стоит сказать в подписи кнопки явно: отзыв — это отмена правила, а не разовое действие.

### 1.4 `proxy_workbench/proxytool.py`: отдельный токен в CLI (дефект 18, R12)

**Где:** `run_gateway` (`proxytool.py:3089-3118`) и подкоманда `gateway` (`proxytool.py:2820`).

**Сейчас** один `--api-token` обслуживает и API, и шлюз:

```python
server = await gateway.start(args.data, args.host, port, args.api_token, filters, args.rotate,
                             max(0, args.max_per_proxy), max(0.0, args.session_ttl) * 60)
```

**Прошу добавить** и передать в `gateway.start(...)`:

| Флаг | Тип | Куда в `start()` | Зачем |
| --- | --- | --- | --- |
| `--gateway-token` (+ `PROXY_WORKBENCH_GATEWAY_TOKEN`) | строка | `token=` | отдельная identity; если не задан, шлюз сгенерирует свой и напечатает один раз |
| `--gateway-lan` | флаг | `lan=` | без него не-loopback адрес отказывается (см. §1.1) |
| `--gateway-interface` | строка | `bind=Bind(..., interface=...)` | выбор адаптера для QR |
| `--gateway-sticky {failover,strict}` | выбор | `sticky=` | режимы прилипания сессии |
| `--gateway-deny-open-streams {keep,close}` | выбор | `on_deny=` | судьба уже открытых потоков |
| `--max-session` | float, секунды | `max_session=` | верхняя граница жизни одного соединения; сейчас из CLI недостижима вообще |

```python
server = await gateway.start(args.data, args.host, port, args.gateway_token, filters, args.rotate,
                             max(0, args.max_per_proxy), max(0.0, args.session_ttl) * 60,
                             lan=args.gateway_lan, sticky=args.gateway_sticky,
                             on_deny=args.gateway_deny_open_streams,
                             max_session=args.max_session)
if server.gateway.token_origin == 'generated':
    print(tr(f'Пароль шлюза (показывается один раз): {server.gateway.token}',
             f'Gateway password (shown once): {server.gateway.token}'), flush=True)
```

**Единицы (CONTRACTS §5.5):** `--session-ttl` уже в минутах и домножается на 60 в вызове; `--max-session` прошу объявить **в секундах**, как `max_session` в коде, и не смешивать с минутами.

### 1.5 `proxy_workbench/apiv1.py`: маршруты шлюза (F16, F29)

В `apiv1.py:1329-1346` уже объявлены `/v1/gateway/bindings`, `/v1/gateway/listeners`, `/v1/gateway/sessions`, `/v1/gateway/config` (включая `PATCH` для config и `POST` для bind). Им не хватает данных, которые теперь есть:

| Маршрут | Что отдавать | Откуда |
| --- | --- | --- |
| `GET /v1/gateway/listeners` | `Bind.as_dict()` + `token_origin` + `authenticated` | `Background.bind`, `Background.state()` |
| `GET /v1/gateway/bindings` | `pool_id, generation, profile_id, profile_revision, policy` | `Pool.state()['binding']`, `Pool.state()['bindings']` |
| `GET /v1/gateway/config` | `strategies, sticky_modes, supported, revoke_policies, handshake_timeout, connect_timeout, idle_timeout, max_session, max_clients, on_deny` | `Gateway.state()` |
| `GET /v1/gateway/sessions` | число и TTL, **без имён сессий** (имя приходит от клиента) | `Pool.snapshot()['sessions']` |

**Почему:** CONTRACTS §5.2/§5.3 — права `gateway.read`/`gateway.write` объявлены, но нечем наполнить; §5.4 — коды причин должны быть видимы клиенту. `Gateway.state()` уже отдаёт эти поля и **не содержит секретов**.

### 1.6 `proxy_workbench/diagnostics.py`: два кода шлюза ещё не описаны (CONTRACTS §5.4)

`diagnostics.py:444-445` уже содержит весь набор §5.4 для шлюза: `E_GATEWAY_NO_UPSTREAM`, `E_GATEWAY_DEADLINE`, `E_GATEWAY_SLOT_UNAVAILABLE`, `E_GATEWAY_TRANSPORT_UNSUPPORTED`. **Этот пункт с прошлой редакции закрыт — прошу не переписывать.**

Не хватает двух кодов, которые шлюз уже различает на практике, но сообщить о них нечем:

| Код | Когда | Действие для пользователя |
| --- | --- | --- |
| `E_GATEWAY_DENIED` | адрес попал под denylist, поэтому допуска нет | «адрес запрещён правилом» |
| `E_GATEWAY_SESSION_STRICT` | strict-сессия осталась без своего адреса и получила отказ вместо смены | «сессия не может сменить адрес» |

Перевод — существующим `i18n.tr(ru, en)`, как требует §5.4.

---

## 2. Что уже сделано у меня

### 2.1 Публичный API `gateway.py`

```python
# конфигурация слушателя
Bind(host='127.0.0.1', port=8899, lan=False, interface=None)   # .local .published_host .as_dict()
Binding(pool_id='default', generation=None, profile_id=None,
        profile_revision=None, policy={'max_per_proxy': 2, 'sticky': 'strict',
                                       'countries': ('DE',), ...})
resolve_token(bind, token=None) -> (token, origin)   # origin: none|explicit|generated
new_gateway_token() -> str
lan_interfaces() -> list[str]
display_host(bind_host, interface=None) -> str

# пул
Pool(data, filters=None, strategy='round-robin', max_failures=2, cooldown=300,
     max_per_proxy=0, session_ttl=600, *, sticky='failover', denylist=None,
     denylist_path=None, denylist_normalizer=None, on_deny='keep',
     cache_limit=256, bindings=None, default_binding=None, healthy_latency_ms=0)
  .load_denylist() / .set_denylist(denylist) / .denied_proxies() / .allowed(proxy)
  .refresh() -> list[str]            # файловый ввод-вывод, только не с event loop
  await .arefresh() -> list[str]     # off-loop
  .matching(request=None, binding=None) / .available(request=None, now=None, binding=None)
  .pick(...) -> str                  # без слота
  .reserve(...) -> Lease | None      # выбор + слот одним шагом
  .release(proxy) / .active_for(proxy)
  .revoke_streams(force=None) -> list[str]   # источник правды о судьбе открытых потоков
  .connected(proxy, connect_ms=0)    # НЕ успех
  .outcome(proxy, kind, detail=None) # response|tunnel_bytes|upstream_unavailable|no_response|
                                     # handshake_failed|upstream_refused|closed_empty
  .ok(proxy) / .failed(proxy)
  .health_score(proxy, now=None) -> float
  .report(proxy) -> dict
  .snapshot(top=20) -> dict          # без файлов, безопасно из любого потока
  await .asnapshot(top=20) -> dict
  .state() -> dict
  .binding_for(name=None) -> Binding  # 'default' всегда адресуем, иначе KeyError
  # поля: .source_rows (что дал экспорт), .rows (минус denylist), .denied, .revoked

# шлюз
Gateway(pool, token=None, attempts=3, connect_timeout=8, idle_timeout=300, *,
        handshake_timeout=30, response_timeout=15, max_clients=512, max_session=0,
        bind=None, token_origin=None, drain_timeout=2.0, ssl_context=None, refresh_interval=2.0)
  await .connect(host, port, forward=False, request=None, session=None,
                 binding=None, sticky=None) -> (Lease, (reader, writer))
  .set_denylist(denylist) -> list[str]   # закрытые потоки при on_deny='close'
  await .shutdown(grace=None) -> {'drained': n, 'forced': m, 'closed_sessions': k}
  .state() -> dict
  .refresh_loop(interval=None)

Lease(pool, proxy)  # .release() ровно один раз, .proxy
ReplayGuard()       # .write_once(lease, writer, payload) -> UpstreamError('REPLAY_REFUSED')

async def start(data, host='127.0.0.1', port=8899, token=None, filters=None,
                strategy='round-robin', max_per_proxy=0, session_ttl=600, *, ...)
Background(data, host='127.0.0.1', port=8899, token=None, **options)
  # .bind .lan .token .token_origin .display_host .state() .shutdown_report
```

### 2.2 Имя binding в user name клиента

`pool-<id>` в имени пользователя выбирает привязку: `http://pool-de:x@127.0.0.1:8899`. Неизвестный `id` — `400`, а не тихий возврат к пулу по умолчанию. Остальные опции (`country-`, `protocol-`, `latency-`, `anonymity-`, `session-`) только **сужают** привязку, расширить её нельзя.

### 2.3 Политика долгих соединений и shutdown

- `idle_timeout` (по умолчанию 300 с) — бездействие в любую сторону, счётчик `closed_idle`;
- `max_session` (0 = без предела, **секунды, float**) — верхняя граница жизни одного соединения, счётчик `capped`. Значение **не округляется**: `0.5` остаётся `0.5` (закреплено тестом);
- `handshake_timeout` — один **абсолютный** дедлайн на весь handshake: первый байт, разбор запроса, согласование SOCKS5 и туннель к апстриму;
- `max_clients` — верхняя граница одновременных клиентских соединений, лишние получают `503`;
- `shutdown(grace)` — сначала ждёт до `grace`, потом отменяет остаток и возвращает отчёт.

### 2.4 Модель здоровья (дефект 17)

| Исход | Что означает | Влияние |
| --- | --- | --- |
| `response` | пришёл HTTP-ответ с кодом < 500 | `ok`, снимает серию неудач |
| `tunnel_bytes` | по туннелю пришли байты | `ok` |
| `upstream_unavailable` | апстрим ответил 5xx — не работает цель | **не наказывает прокси** |
| `no_response` | апстрим завис | **не наказывает прокси** |
| `upstream_refused` | 407, не-HTTP мусор, закрылся без ответа | отдых после `max_failures` |
| `closed_empty` | клиент говорил, в ответ тишина | отдых после `max_failures` |
| `handshake_failed` | не соединился / отверг рукопожатие | отдых после `max_failures` |

`connected()` (TCP+handshake) **не** является успехом и **не** снимает серию неудач. Повтор запроса после того, как хоть один байт ушёл в апстрим, невозможен конструктивно: `ReplayGuard.write_once` отказывает во второй записи, а `Gateway.connect` повторяет попытку, только пока ничего не записано.

---

## 3. Тесты

`tests/gateway_support.py` — общие локальные фикстуры: фейковые апстримы (`socks4/4a/5/5h`, `http` в пяти режимах, HTTPS через локальный `openssl`), цель на 127.0.0.1 и ::1, экспорт поколения, и **`ScriptedClient`** — клиент, у которого `drain()` можно удержать на выбранном маркере. Он нужен для оконга между «туннель открыт» и «начался relay»: настоящий клиент пришлось бы race'ить, а этот позволяет отменить или истечь ровно в этом `await`. Ни одного обращения к публичному прокси, DNSBL или стороннему сервису.

Тесты, добавленные в этом проходе:

| Тест | Что доказывает |
| --- | --- |
| `test_gateway_reservation.ReservationTests.test_a_cancelled_socks5_grant_gives_the_slot_back` | отмена при записи SOCKS5-гранта возвращает слот |
| `...test_a_cancelled_connect_grant_gives_the_slot_back` | то же для HTTP CONNECT |
| `...test_the_handshake_deadline_gives_the_granted_slot_back` | дедлайн handshake в этом же окне возвращает слот |
| `test_gateway_denylist.DenylistTests.test_removing_a_rule_gives_the_address_back` | снятое правило возвращает адрес в ротацию |
| `...test_an_explicit_empty_denylist_restores_everything` | `set_denylist(Denylist.empty())` отменяет и себя |
| `...test_the_revoke_policy_lives_in_one_place` | решение «рвать ли поток» принимается в одном месте, `force` перекрывает политику |
| `test_gateway_health.HealthTests.test_a_capped_long_connection_is_counted_apart_from_an_idle_one` | лимит сессии и простой считаются раздельно |
| `...test_a_fractional_session_cap_is_not_truncated_into_no_cap` | дробный лимит не превращается в «без лимита» |

---

## 4. Проверки

Команды запускались из корня репозитория, каждая — свой модуль:

```
.venv/bin/python -m unittest tests.test_gateway             → Ran 13 tests, OK
.venv/bin/python -m unittest tests.test_gateway_reservation → Ran 12 tests, OK
.venv/bin/python -m unittest tests.test_gateway_health      → Ran 14 tests, OK
.venv/bin/python -m unittest tests.test_gateway_bind        → Ran 12 tests, OK
.venv/bin/python -m unittest tests.test_gateway_denylist    → Ran 11 tests, OK
.venv/bin/python -m unittest tests.test_gateway_rotation    → Ran 22 tests, OK
.venv/bin/python -m unittest tests.test_gateway_transports  → Ran 16 tests, OK
```

Соседние модули, чтобы убедиться, что правка шлюза ничего не сломала:

```
.venv/bin/python -m unittest tests.test_api         → Ran  8 tests, OK
.venv/bin/python -m unittest tests.test_gui         → Ran 24 tests, OK
.venv/bin/python -m unittest tests.test_extras      → Ran  5 tests, OK
.venv/bin/python -m unittest tests.test_web_connect → Ran 24 tests, OK
```

Три падения, которые предыдущая редакция этого handoff фиксировала как «не мои», на текущей ревизии **зелёные** — их починили поверхность `web` и интегратор; править их файлы мне не пришлось.

**Про воспроизводимость interleaving (R11).** Три новых теста запускались против поведения без починки, подставленного скриптом: `Gateway._discard = lambda lease, upstream: None` — то есть ровно то, что было до правки (окно без единого `release`). Результат:

```
FAIL: test_a_cancelled_socks5_grant_gives_the_slot_back
AssertionError: False is not true : a slot reserved before the grant leaked on cancel: {'socks5://127.0.0.1:55291': 1}
FAIL: test_a_cancelled_connect_grant_gives_the_slot_back
AssertionError: False is not true : a slot reserved before the CONNECT reply leaked on cancel: {'socks5://127.0.0.1:55305': 1}
FAIL: test_the_handshake_deadline_gives_the_granted_slot_back
AssertionError: False is not true : the handshake deadline left a slot taken: {'socks5://127.0.0.1:55320': 1}
Ran 3 tests in 15.361s
FAILED (failures=3)
```

С починкой те же три теста зелёные. Это именно тот тест, которого не хватало: он не «последовательный pick/acquire», а три клиента, остановленные в одном и том же `await` внутри шлюза.

Поведение CLI на не-loopback адресе проверено отдельно (см. §1.1): слушатель не открывается, печатается отказ, приложение продолжает работать.

**Что осталось непрочитанным/непроверенным:**
- Полный `unittest discover -s tests` не запускался — по условию задачи его гоняют другие исполнители и интегратор.
- Реальная доставка (QR, выбор интерфейса в UI, firewall) не проверялась: это `gui.py`/`ui/*`.
- HTTPS-апстрим проверяется сертификатом, который тест создаёт локально через `openssl`; если `openssl` недоступен, четыре теста помечаются `skipTest`, а не проходят молча. `test_https_upstream_verifies_the_proxy_host_name` от `openssl` не зависит и выполняется всегда.
- `access_revision` в привязке по-прежнему отсутствует (см. §5.3).

---

## 5. Открытые вопросы

1. **Политика закрытия уже открытых потоков при добавлении правила.** Сделано `'keep'` по умолчанию и `'close'` по явному выбору, потому что R13 говорит, что это отдельная политика. Если продукт решит, что массовое «запретить» в GUI обязано рвать и открытые потоки, дефолт меняется одной строкой (`Pool(..., on_deny='close')`) и текстом кнопки в §1.3.

2. **Имя пула против привязки.** Сейчас binding — это `pool_id` + generation + profile + policy поверх экспорта. `pools.py` уже умеет именованные пулы в БД (миграция 7), но у них другая природа (desired-контроллер, а не снимок строк). Соединить их — работа интегратора; шлюз к этому готов через `bindings=` и `Pool.state()['bindings']`.

3. **`access_revision` в привязке.** CONTRACTS §1.2(1) требует, чтобы смена ревизии доступа отзывала прошлое доказательство и в шлюзе. Сущности `access` ещё нет (`grep -rn "access_revision" proxy_workbench/` — ноль совпадений на момент написания), поэтому в `Binding` есть только `profile_id`/`profile_revision`. Когда появится `access`, нужен ещё один компонент в `_bound_rows`; место для этого — тот же метод.

4. **Таймаут ожидания ответа апстрима (`response_timeout`, 15 с).** Для CONNECT/SOCKS он не применяется: там доказательство приходит с первыми байтами клиента. Если для длинных CONNECT-запросов нужен свой лимит, это отдельная настройка, а не переиспользование `response_timeout`.
