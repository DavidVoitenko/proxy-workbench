# Handoff: pipeline

**Требования:** F12 (конвейер и быстрый поиск); частично — дефекты 6, 23 и R01 в той части, где конвейер владеет паузой, отменой и общим сроком.
**Контракт:** `docs/integration/CONTRACTS.ru.md` §2.1 (наблюдение), §2.3 (admission), §5.4 (коды), §5.5 (единицы), §6.1–6.3 (состояния job и item) — версия 1.
**База:** ветка `integration/ultra-2026-09-25`, модуль написан как новый leaf-файл `proxy_workbench/pipeline.py`.

Модуль ничего не менял в чужих файлах. Всё, что нужно от других, — в разделе 1.

---

## 1. Прошу внести

### 1.1 `proxy_workbench/proxytool.py` — функция `scan` (`:1283`)

**Что сделать.** Не переписывать `scan`, а заменить его тело на вызов конвейера и оставить `proxytool` единственной точкой нормализации и сетевого входа.

```python
# proxytool.py, внутри scan: sources вместо готового списка pending
result = await pipeline.run_pipeline(pipeline.PipelineConfig(
    sources=[pipeline.SourceSpec(source_id=source_id,
                                 fetch=make_stream(source.url, byte_budget=...))],
    budgets=pipeline.Budgets(**budgets_from_config(config),
                             deadline_s=whole_probe_timeout_s),
    limits=pipeline.Limits(max_per_host=1,
                           targets=[pipeline.TargetPolicy(t.id, max_inflight=..., max_requests=...)
                                    for t in expensive_targets]),
    find=pipeline.FindPolicy(n=want, what='exit'),
    priors=pipeline.PriorIndex(prior_rows, now=time.time(), max_age_s=max_age_seconds),
    runners=pipeline.Runners(cheap=run_tcp, basic=run_basic, expensive=run_expensive),
    normalize=normalize,            # уже существующая proxytool.normalize
    parse=pipeline.parse_lines,      # одна строка списка — как в collect()
    on_result=publish_observation,   # jobs.events: одно событие на item
), clock=pipeline.SystemClock(), control=control)
```

**Почему здесь.** CONTRACTS §1.2 и HANDOFF §2.2: у конвейера нет своего сетевого входа и своего нормализатора, поэтому подключение обязано остаться в `proxytool.py`, а не появиться вторым путём.

**Что это даёт сверх текущего `scan`:** ограничения по FD/RAM/байтам/запросам вместо числа workers, backpressure, find-N по exit-IP, учёт «одного item не проверяем дважды», пауза и отмена всей цепочки, общий срок задания.

### 1.2 `proxy_workbench/proxytool.py` — точка нормализации

Ничего менять не нужно, но фиксирую договорённость: `pipeline.normalize_default()` вызывает `proxytool.normalize` для публичных адресов, а `proxytool.normalize_custom` для своей коллекции передаётся как `config.normalize`. Второго нормализатора в `pipeline.py` нет и не появится.

### 1.3 `proxy_workbench/jobs.py` — состояния item

`pipeline.EMITTED_ITEM_STATES = ('unreachable', 'done', 'failed')` — подмножество `jobs.ITEM_STATES`. Сопоставление однозначное:

| `ItemResult.state` | Когда | Что отдавать в `job_item.state` |
| --- | --- | --- |
| `unreachable` | дешёвая проба не прошла, либо код стадии `tcp` | `unreachable`, `error.stage='tcp'`, `error.code` |
| `done` | измерение состоялось, вердикт может быть отрицательным | `done` |
| `failed` | раннер бросил или вернул негодный результат | `failed`, `error.code` — код `E_*` раннера либо имя класса |

`partial` и `blocked` конвейер не выдаёт: решение о `blocked` принимается до измерения (денylist, дефект 19), а `partial` — это `jobs`-состояние незавершённого элемента.

### 1.4 `proxy_workbench/diagnostics.py` — коды причин остановки

`RunResult.counters.stage_failures['stop']` содержит машинную причину, по которой цепочка остановилась:

| Значение | Смысл | Код для пользователя |
| --- | --- | --- |
| `E_LIMIT_BUDGET` | исчерпан лимит запросов, адресов или CPU | `E_LIMIT_BUDGET` |
| `E_LIMIT_BODY` | источник обрезан по байтовому лимиту | `E_LIMIT_BODY` |
| `DEADLINE_EXCEEDED` | исчерпан общий срок задания | существующий код задания |
| `paused` / `cancelled` / `stopped` | действие пользователя или обрыв источника | `E_STATE_JOB_INTERRUPTED` |

`RunResult.state` и `RunResult.reason` разведены, как того требует CONTRACTS §4.3: `state='budget'` с `reason='deadline_exceeded'` — это не то же самое, что `state='cancelled'`.

### 1.5 `proxy_workbench/pools.py` — добирание до N

`find-n` теперь умеет различать единицы, и пул должен просить нужную:

```python
policy = pipeline.FindPolicy(n=pool.desired, what='exit')   # подтверждённые exit-IP
```

Для пула, которому важны адреса, а не выходные IP, — `what='endpoint'`. `what='ip'` для пула не подходит: подтверждённый выход и адрес прокси — разные числа (CONTRACTS §1.1, F12).

### 1.6 `proxy_workbench/probes.py` — форма раннера

`probes.run_plan` возвращает `PlanOutcome`, а конвейер ждёт `StageOutcome`. Мост — три строки на стороне интегратора, сам `probes.py` менять не нужно:

```python
async def run_basic(item, *, stage, limit):
    outcome = await probes.run_plan(plan, transport_for(item.endpoint),
                                    endpoint=item.endpoint, fail_fast=False)
    return pipeline.StageOutcome(
        kind=stage, ok=outcome.ok, code=outcome.code, failed_stage=outcome.stage,
        latency_ms=(outcome.finished_at - outcome.started_at) * 1000.0,
        bytes=outcome.bytes, requests=sum(1 for t in outcome.targets),
        exit_ip=last_exit_ip(outcome))
```

`limit` (`pipeline.StageLimit`) — это жёсткая крышка, которую раннер обязан соблюдать: `limit.remaining_s` до общего срока задания, `limit.remaining_bytes` до байтового бюджета, `limit.target_id` — цель, чьи лимиты сейчас действуют. `probes.run_probe` уже держит `whole_probe_timeout_s` сам (`probes.py:1104-1120`), поэтому конвейер второй таймаут не навешивает.

---

## 2. Что уже сделано у меня

Публичный API, на который можно опираться (всё в `proxy_workbench/pipeline.py`):

| Имя | Что это |
| --- | --- |
| `PipelineConfig` | вся конфигурация запуска: источники, бюджеты, лимиты, find-N, prior, раннеры |
| `Pipeline` | сама цепочка; `await run() -> RunResult`, `stream()` — async-генератор результатов по мере поступления |
| `run_pipeline(config, *, clock, control, concurrency)` | обёртка на один запуск |
| `PipelineControl` | `pause()` / `resume()` / `cancel()` для всей цепочки |
| `Budgets` | все потолки и итоги: fds, RAM, байты, запросы, CPU, срок, размер очереди, размер источника |
| `Limits` | per-host (`max_per_host`, `min_host_interval_s`) и per-target (`TargetPolicy`) |
| `AdaptiveConcurrency` | AIMD по окну успешности, потолок которого всё равно режется бюджетами |
| `FindPolicy` / `FindProgress` | поиск N и различение `endpoint` / `ip` / `exit` |
| `PriorIndex` / `PriorItem` / `decay_weight` | prior known-good с поправкой на возраст (полураспад) |
| `Counters` / `RunMetrics` / `RunResult` / `ItemResult` / `StageOutcome` | что измерено, сколько потрачено, чем кончилось |
| `Ledger` | «один item не проверяем дважды», живёт между `run()` |
| `SourceSpec` / `parse_lines` / `parse_delimited` | вход источника и парсеры |
| `benchmark()` / `Benchmark` / `synthetic_endpoints()` | воспроизводимые локальные замеры |

Что гарантируется поведением и покрыто тестами:

* **Ни одного item дважды.** Адрес попадает в `Ledger` только тогда, когда очередь его приняла; снятый и пропущенный из-за остановки элемент возвращается в `pending`, а `resume` не перепроверяет уже измеренное (`resume_skips`).
* **Три разных N.** `unique_endpoints`, `unique_ips`, `confirmed_exit_ips` считаются раздельно, `find.met` всегда равен счётчику, который он двигает, а имя хоста никогда не засчитывается как IP.
* **Ресурсы, а не workers.** `Budgets.worker_ceiling()` выводится из fds и RAM; замер: пик RAM ≈ 400 КБ на корпусе 200 и 20 000 адресов при очереди 16.
* **Пауза и отмена.** `state='paused'` / `'cancelled'`, `remaining` и `feed_complete` показывают, что осталось; отмена задачи asyncio пробрасывается наружу, а состояние цепочки при этом не теряется.
* **Никакой выдуманной скорости.** `Benchmark.synthetic` всегда `True`, а `to_public()` несёт `SYNTHETIC_NOTICE`: замеры сделаны на локальной фикстуре из RFC 5737 / RFC 3849, сети не было.

---

## 3. Совместимость

* **Что ломается, если не внести.** Ничего: `pipeline.py` — новый файл, чужие модули не тронуты. Без интеграции F12 остаётся незакрытым, а `scan` сохраняет нынешнее поведение: без find-N по exit-IP, без учёта FD/RAM/байтов, без паузы и отмены цепочки, с повторной проверкой одного item после `resume`.
* **Что НЕ ломается.** `scan`, `store`, `export`, `publish` и все дефекты 1–9, 12, 23 в `proxytool.py` остаются как есть, пока подключения нет. Схема БД не меняется: модуль не пишет DDL и не обращается к диску.
* **Замечание для `tests/test_freshness.py`.** `pipeline` не трогает БД, поэтому дефект 1 (TTL не пишется при записи) он не закрывает и не должен закрывать: запись наблюдения остаётся у `store` и `core`.

---

## 4. Проверки

Команды, запущенные в этой сессии, из корня репозитория:

```
$ .venv/bin/python -m unittest tests.test_pipeline_chain
Ran 28 tests in 0.039s — OK

$ .venv/bin/python -m unittest tests.test_pipeline_findn
Ran 28 tests in 0.037s — OK

$ .venv/bin/python -m unittest tests.test_pipeline_limits
Ran 35 tests in 0.706s — OK

$ .venv/bin/python -m unittest tests.test_pipeline_bench
Ran 19 tests in 0.393s — OK

$ .venv/bin/python -m unittest tests.test_pipeline_chain tests.test_pipeline_findn \
      tests.test_pipeline_limits tests.test_pipeline_bench
Ran 110 tests in 1.216s — OK
```

Полный набор `unittest discover -s tests` не запускался: по условию задачи его гоняет интегратор, а параллельно его пишут другие исполнители.

Что осталось непроверенным:

* Модуль не подключён к `proxytool.py`, поэтому его замеры получены на фикстуре, а не на живых публичных прокси. **Числа `benchmark()` нельзя цитировать как скорость живых прокси** — это записано и в `SYNTHETIC_NOTICE`, и в тесте `test_a_synthetic_number_is_never_readable_as_a_live_proxy_measurement`.
* Сетевой путь (`probes.run_plan` → транспорт) не проверялся: по условию задачи массовые проверки публичных прокси запрещены, а локального транспорта `probes.py` у меня нет. Проверялась только форма `StageOutcome`, которую вернёт мост из §1.6.
* Поведение под реальной нагрузкой (тысячи элементов, реальные таймауты) не измерялось: замеры сделаны на 400–20 000 элементах фикстуры.

---

## 5. Открытые вопросы

1. **`access_id` в ключе item.** CONTRACTS §6.3 требует, чтобы item нёс `access_id` и `access_revision`. Конвейер работает с `Item(endpoint, …)` и ничего не знает про доступ: учёт доступа принадлежит `jobs.py` и `secrets.py`. Если `jobs` захочет отдавать конвейеру готовые items с `access_id`, нужен вход вида «item = кортеж (endpoint, access)»; сейчас это не сделано намеренно, чтобы не изобретать модель доступа раньше её владельца.
2. **Два независимых ответа: что считать «достаточно быстро».** `AdaptiveConcurrency` реагирует на успешность и задержку, но у него нет представления о целевой задержке профиля. Если профили (`profiles.py`) будут нести `max_latency_ms`, его стоит передавать сюда как цель AIMD, а не как запрет.
3. **Дефект 23 (общий срок).** `Budgets.deadline_s` закрывает его на уровне конвейера. Значение по умолчанию не выбрано намеренно: по F09 «300 секунд или 2 часа не считать доказанным оптимумом», поэтому дефолт `None`, и срок задаёт ревизия профиля.
4. **Имя `find` для «N уникальных IP».** Оно считает IP *адреса прокси*, а не подтверждённый выход. Если пулу нужен именно второй смысл, это `what='exit'`; отдельного имени не заводил, чтобы не плодить почти одинаковые единицы.
