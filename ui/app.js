'use strict';

const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="workbench-token"]').content;
const PRODUCT_VERSION = '__PRODUCT_VERSION__';
let settings;
let state = {};
let rows = [];
let resultTargets = [];
let offset = 0;
let resultTotal = 0;
let lastFinished = null;
let toastTimer;
let polling = false;
let resultBusy = false;

const numeric = ['attempts', 'timeout', 'workers', 'rate', 'max_bytes', 'source_timeout', 'top', 'min_success', 'reputation-timeout'];
const profileLabels = {workbench: 'Рабочий профиль', standard: 'Стандартный', minimal: 'Минимальный'};
const profileDescriptions = {
  workbench: `User-Agent ProxyWorkbench/${PRODUCT_VERSION}, Accept и Accept-Encoding.`,
  standard: `User-Agent ProxyWorkbench/${PRODUCT_VERSION}, Accept для JSON/text и Accept-Encoding.`,
  minimal: `Только User-Agent ProxyWorkbench/${PRODUCT_VERSION} и Accept.`
};
const reputationLabels = {clean: 'Чистый', listed: 'Blacklist', unknown: 'Неизвестно', local_denied: 'Локальный blacklist'};
const fmt = n => Number(n || 0).toLocaleString('ru-RU');
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));

function toast(message, error=false) {
  $('toast').textContent = message;
  $('toast').className = error ? 'error' : '';
  $('toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, error ? 9000 : 4500);
}

async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: {'X-Workbench-Token': token, 'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || 'Ошибка приложения');
  return value;
}

function showTab(name) {
  document.querySelectorAll('.page').forEach(node => node.classList.toggle('active', node.id === 'page-' + name));
  document.querySelectorAll('.nav').forEach(node => node.classList.toggle('active', node.dataset.tab === name));
  $('page-label').textContent = {scan:'Проверка', results:'Результаты', sources:'Источники', help:'Как это работает'}[name];
  if (name === 'results') loadResults();
}

document.querySelectorAll('[data-tab]').forEach(node => node.onclick = () => showTab(node.dataset.tab));
document.querySelectorAll('[data-go]').forEach(node => node.onclick = () => showTab(node.dataset.go));

function addTarget(target={}) {
  if ($('targets').children.length >= 20) {
    toast('Максимум 20 сервисов.', true);
    return;
  }
  const node = document.createElement('div');
  node.className = 'target';
  node.innerHTML = `<div class="target-head"><span>◎</span><input data-field="name" aria-label="Название сервиса" placeholder="Название сервиса" value="${esc(target.name || 'Свой сервис')}"><button data-remove title="Удалить сервис" aria-label="Удалить сервис">×</button></div><label class="target-url">URL для проверки<input data-field="url" type="url" placeholder="https://example.org/health" value="${esc(target.url || '')}"></label><div class="field-grid"><label>Допустимые HTTP-коды<input data-field="statuses" placeholder="200, 204 или 200-299" value="${esc(target.statuses ? (target.statuses.length === 100 && target.statuses[0] === 200 ? '200-299' : target.statuses.join(', ')) : '200-299')}"></label><label>Текст в ответе (необязательно)<input data-field="contains" placeholder="Например: healthy" value="${esc(target.contains || '')}"></label></div><details class="advanced"><summary>Метод, локальные заголовки и SHA-256</summary><label>Метод<select data-field="method"><option>GET</option><option>HEAD</option></select></label><label>Заголовки сервиса — JSON<textarea data-field="headers" rows="2" spellcheck="false">${esc(JSON.stringify(target.headers || {}))}</textarea><small>Сохраняются локально и не попадают в экспорт, но передаются через проверяемый публичный прокси — не добавляйте токены.</small></label><label>SHA-256 тела ответа (необязательно)<input data-field="sha256" value="${esc(target.sha256 || '')}" placeholder="Ожидаемый хеш ответа"></label></details>`;
  node.querySelector('[data-field="method"]').value = target.method || 'GET';
  node.querySelector('[data-remove]').onclick = () => {
    if ($('targets').children.length === 1) {
      toast('Нужен хотя бы один сервис.', true);
      return;
    }
    node.remove();
  };
  $('targets').appendChild(node);
}

function statuses(raw) {
  const values = new Set();
  for (const part of raw.split(',')) {
    const match = part.trim().match(/^(\d{3})(?:\s*-\s*(\d{3}))?$/);
    if (!match) throw new Error('Укажите HTTP-коды: например 200, 204 или 200-299.');
    const first = Number(match[1]);
    const last = Number(match[2] || match[1]);
    if (first < 100 || last > 599 || last < first) throw new Error('HTTP-код должен быть от 100 до 599.');
    for (let code = first; code <= last; code += 1) values.add(code);
  }
  return [...values];
}

function zones() {
  return $('dnsbl-zones').value.split(/[\s,]+/).map(value => value.trim()).filter(Boolean);
}

function getSettings() {
  const copy = {...settings};
  for (const key of numeric) copy[key.replace('-', '_')] = Number($(key).value);
  copy.sort = $('sort').value;
  copy.use_sources = $('use_sources').checked;
  copy.sources = $('sources').value.split('\n').map(value => value.trim()).filter(Boolean);
  copy.proxies = $('proxies').value;
  copy.denylist = $('denylist').value;
  copy.request_profile = $('request-profile').value;
  const zoneValues = zones();
  copy.reputation = {
    local_enabled: $('local-denylist-enabled').checked,
    dnsbl_enabled: $('dnsbl-enabled').checked && zoneValues.length > 0,
    dnsbl_zones: zoneValues,
    timeout: Number($('reputation-timeout').value),
    strict: $('strict-clean').checked
  };
  copy.targets = [...$('targets').children].map(node => {
    const value = key => node.querySelector(`[data-field="${key}"]`).value.trim();
    let headers;
    try {
      headers = JSON.parse(value('headers') || '{}');
    } catch {
      throw new Error('Заголовки сервиса должны быть корректным JSON.');
    }
    if (!value('url')) throw new Error('Введите URL каждого сервиса.');
    return {name:value('name'), url:value('url'), method:value('method'), statuses:statuses(value('statuses')), contains:value('contains') || null, sha256:value('sha256') || null, headers};
  });
  return copy;
}

function updateIdentity() {
  const profile = $('request-profile').value;
  $('profile-summary').textContent = profileLabels[profile] || 'Request-профиль';
  $('profile-description').textContent = profileDescriptions[profile] || 'Нейтральные HTTP-заголовки.';
  $('dnsbl-fields').classList.toggle('hidden', !$('dnsbl-enabled').checked);
}

function fill(value) {
  settings = value;
  const reputation = {...(value.reputation || {local_enabled:true, dnsbl_enabled:false, dnsbl_zones:[], timeout:2.5, strict:false})};
  for (const key of numeric) {
    const element = $(key);
    if (element.tagName === 'SELECT' && ![...element.options].some(option => option.value === String(value[key.replace('-', '_')]))) {
      element.add(new Option(`${Math.round(value[key.replace('-', '_')] * 100)}%`, value[key.replace('-', '_')]));
    }
    element.value = key === 'reputation-timeout' ? reputation.timeout : value[key.replace('-', '_')];
  }
  $('sort').value = value.sort;
  $('use_sources').checked = value.use_sources;
  $('sources').value = value.sources.join('\n');
  $('proxies').value = value.proxies || '';
  $('denylist').value = value.denylist || '';
  $('request-profile').value = value.request_profile || 'workbench';
  $('dnsbl-zones').value = (reputation.dnsbl_zones || []).join('\n');
  $('dnsbl-enabled').checked = !!reputation.dnsbl_enabled && $('dnsbl-zones').value.trim().length > 0;
  $('local-denylist-enabled').checked = reputation.local_enabled !== false;
  $('strict-clean').checked = !!reputation.strict;
  $('targets').replaceChildren();
  value.targets.forEach(addTarget);
  updateIdentity();
  syncResultControls();
  updateSourceCount();
}

function syncResultControls() {
  $('result-sort').value = $('sort').value;
  const value = $('min_success').value;
  if (![...$('result-min').options].some(option => option.value === value)) $('result-min').add(new Option(`${Math.round(Number(value) * 100)}%`, value));
  $('result-min').value = value;
  $('result-top').value = $('top').value;
}

function updateSourceCount() {
  $('source-count').textContent = $('use_sources').checked ? fmt(new Set($('sources').value.split('\n').map(value => value.trim()).filter(Boolean)).size) : 'Выключены';
}

async function save() {
  try {
    settings = await api('/api/settings', getSettings());
    toast('Настройки сохранены.');
    updateIdentity();
    updateSourceCount();
  } catch (error) {
    toast(error.message, true);
  }
}

async function start(action) {
  try {
    let value = getSettings();
    if (action === 'export') {
      value.sort = $('result-sort').value;
      value.min_success = Number($('result-min').value);
      value.top = Number($('result-top').value);
      $('sort').value = value.sort;
      $('min_success').value = value.min_success;
      $('top').value = value.top;
    } else {
      syncResultControls();
    }
    setBusy(true);
    await api('/api/start', {action, settings:value});
    settings = value;
    toast(action === 'export' ? 'Формируем файлы по выбранным фильтрам.' : 'Запуск принят. Прогресс появится через несколько секунд.');
    if (action === 'collect') showTab('sources');
    await poll();
  } catch (error) {
    toast(error.message, true);
    setBusy(Boolean(state.running));
  }
}

function setBusy(active) {
  ['start', 'resume', 'recheck', 'collect', 'export'].forEach(id => { $(id).disabled = active; });
  $('stop').disabled = !active;
}

const phases = {starting:'Запуск', collecting:'Сбор источников', scanning:'Проверка', exporting:'Сохранение', complete:'Завершено', stopped:'Остановлено', interrupted:'Прервано', error:'Ошибка'};

function duration(seconds) {
  if (seconds == null) return '—';
  if (seconds < 60) return '< 1 мин';
  if (seconds < 3600) return Math.ceil(seconds / 60) + ' мин';
  return (seconds / 3600).toFixed(1) + ' ч';
}

function reputationStatus(row) {
  return (row && row.reputation && row.reputation.status) || 'clean';
}

function reputationBadge(row) {
  const status = reputationStatus(row);
  return `<span class="cleanliness cleanliness-${esc(status)}">${esc(reputationLabels[status] || status)}</span>`;
}

function renderState(value) {
  state = value;
  const progress = value.progress || {};
  const job = value.job || {};
  setBusy(value.running);
  $('phase').textContent = job.stopping && value.running ? 'Останавливаем…' : phases[progress.phase] || 'Готов к запуску';
  $('checked').textContent = fmt(progress.checked);
  $('candidates').textContent = fmt(progress.candidates);
  const percent = progress.candidates ? Math.min(100, 100 * (progress.checked || 0) / progress.candidates) : 0;
  $('percent').textContent = percent.toFixed(1) + '%';
  $('progress-bar').style.width = percent + '%';
  $('speed').textContent = value.running && progress.phase === 'scanning' ? String(progress.speed ?? '—') : '—';
  $('eta').textContent = value.running && progress.phase === 'scanning' ? duration(progress.eta_seconds) : '—';
  $('progress-text').textContent = progress.phase === 'collecting' ? `Источников обработано: ${progress.sources_done || 0} / ${progress.sources_total || 0}` : progress.candidates ? `Осталось проверить: ${fmt(Math.max(0, progress.candidates - (progress.checked || 0)))}` : 'Все собранные адреса попадут в проверку';
  $('job-detail').textContent = job.id ? `${{run:'Сбор + проверка', scan:'Продолжение базы', recheck:'Новая проверка', collect:'Сбор адресов', export:'Экспорт'}[job.action] || 'Проверка'} · сервисов: ${job.targets?.length || 0}${job.request_profile ? ' · профиль: ' + (profileLabels[job.request_profile] || job.request_profile) : ''}${progress.phase === 'error' ? ' · подробности в журнале' : ''}` : 'Настройте сервисы и запустите поиск.';
  $('log').textContent = value.log || 'Здесь появится ход проверки.';
  const exportReport = value.export || {};
  $('nav-count').textContent = fmt(progress.passed ?? exportReport.passed ?? 0);
  $('live-passed').textContent = fmt(progress.passed ?? exportReport.passed ?? 0);
  if (exportReport.profile) {
    const counts = exportReport.reputation?.counts || {};
    $('export-note').textContent = `Готовый экспорт: ${fmt(exportReport.exported)} из ${fmt(exportReport.passed)} подходящих · проверено ${fmt(exportReport.checked)} / ${fmt(exportReport.candidates)} · чистых: ${fmt(counts.clean || 0)} · blacklist: ${fmt((counts.listed || 0) + (counts.local_denied || 0))} · неизвестных: ${fmt(counts.unknown || 0)}${exportReport.local_filtered ? ` · локально отсечено: ${fmt(exportReport.local_filtered)}` : ''}.`;
  }
  document.querySelectorAll('[data-download]').forEach(node => { node.disabled = !(value.downloads || []).includes(node.dataset.download) || (value.running && progress.phase === 'exporting'); });
  const report = progress.sources ? progress : value.sources || {};
  renderSources(report, value.source_urls || []);
  const finished = job.id && !value.running ? job.id : null;
  if (finished && finished !== lastFinished) {
    lastFinished = finished;
    if (job.action !== 'collect') loadResults();
  }
}

function renderSources(report, urls) {
  const sourceRows = report.sources || [];
  $('sources-status').textContent = report.denylist_error ? 'Ошибка чтения denylist — проверка будет неопределённой' : (sourceRows.length ? `${sourceRows.filter(row => row.complete).length} / ${sourceRows.length} загружены полностью` : 'Загрузки ещё не было');
  $('source-rows').innerHTML = sourceRows.length ? sourceRows.map(row => {
    const label = row.source ? urls[row.source - 1] || 'Источник ' + row.source : 'Свой список';
    return `<tr><td title="${esc(label)}" style="max-width:440px;overflow:hidden;text-overflow:ellipsis">${esc(label)}</td><td>${fmt(row.rows)}</td><td>${fmt(row.invalid)}</td><td>${fmt(row.blocked || 0)}</td><td class="${row.complete ? '' : 'status-error'}">${row.complete ? (row.rows === 0 ? 'Пустой список' : row.rows === row.invalid ? 'Нет подходящих адресов' : 'Готово') : esc(row.error || 'Не завершено')}</td></tr>`;
  }).join('') : '<tr><td colspan="5" class="empty">Здесь будут результаты последнего сбора.</td></tr>';
}

async function poll() {
  if (polling) return;
  polling = true;
  try { renderState(await api('/api/state')); }
  catch { $('phase').textContent = 'Нет связи с приложением'; }
  finally { polling = false; }
}

async function loadResults() {
  if (resultBusy) return;
  resultBusy = true;
  $('refresh-results').disabled = true;
  try {
    const query = new URLSearchParams({sort:$('result-sort').value, min_success:$('result-min').value, offset});
    const data = await api('/api/results?' + query);
    rows = data.rows;
    resultTargets = data.targets;
    resultTotal = data.total;
    $('result-context').textContent = data.profile ? `Проверено для: ${data.targets.map(target => target.name ? `${target.name} (${target.url})` : target.url).join(' + ')} · профиль: ${profileLabels[data.request_profile] || data.request_profile || 'workbench'}` : 'После проверки здесь появятся подходящие адреса.';
    $('result-total').textContent = fmt(data.total) + ' подходящих';
    $('page-number').textContent = `${Math.floor(offset / 50) + 1} / ${Math.max(1, Math.ceil(data.total / 50))}`;
    $('prev').disabled = offset === 0;
    $('next').disabled = offset + 50 >= data.total;
    $('result-rows').innerHTML = rows.length ? rows.map((row, index) => `<tr><td>${fmt(offset + index + 1)}</td><td>${esc(row.proxy)}</td><td><span class="score">${Number(row.score).toFixed(1)}</span></td><td>${Number(row.latency_ms).toFixed(0)} мс</td><td>${Number(row.jitter_ms).toFixed(0)} мс</td><td>${(Number(row.min_target_reliability) * 100).toFixed(0)}%</td><td>${reputationBadge(row)}</td><td><button class="text-link" data-details="${index}">Детали ↗</button></td></tr>`).join('') : '<tr><td colspan="8" class="empty">По этим условиям пока нет подходящих прокси.</td></tr>';
    $('result-rows').querySelectorAll('[data-details]').forEach(node => node.onclick = () => details(rows[Number(node.dataset.details)]));
  } catch (error) {
    toast(error.message, true);
  } finally {
    resultBusy = false;
    $('refresh-results').disabled = false;
  }
}

function details(row) {
  $('details-title').textContent = row.proxy;
  const verdict = row.reputation || {status:'clean', dnsbl:[]};
  const dnsbl = (verdict.dnsbl || []).map(item => `${esc(item.zone)}: ${esc(item.status === 'listed' ? 'найден' : item.status === 'clear' ? 'чисто' : 'нет ответа')}`).join(' · ') || 'не проверялись';
  const body = $('details-body');
  body.innerHTML = `<div class="detail-reputation"><strong>Чистота:</strong> ${esc(reputationLabels[verdict.status] || verdict.status)} · <strong>DNSBL:</strong> ${dnsbl}${verdict.local_rule ? ' · локальное правило: ' + esc(verdict.local_rule) : ''}</div>` + resultTargets.map((target, index) => `<h3>${esc(target.name || 'Сервис ' + (index + 1))} · ${esc(target.url)}</h3><div class="table-wrap"><table><thead><tr><th>Попытка</th><th>Ответ</th><th>Время</th><th>Байт</th><th>Результат</th></tr></thead><tbody>${row.samples.filter(sample => sample.target === index).map(sample => `<tr><td>${sample.attempt}</td><td>${sample.status ?? '—'}</td><td>${sample.ms} мс</td><td>${fmt(sample.bytes)}</td><td class="${sample.ok ? '' : 'status-error'}">${sample.ok ? 'Успешно' : esc(sample.error)}</td></tr>`).join('')}</tbody></table></div>`).join('');
  $('details-dialog').showModal();
}

$('close-details').onclick = () => $('details-dialog').close();
$('add-target').onclick = () => addTarget();
$('save-settings').onclick = save;
$('save-sources').onclick = save;
$('start').onclick = () => start('run');
$('resume').onclick = () => start('scan');
$('recheck').onclick = () => start('recheck');
$('collect').onclick = () => start('collect');
$('export').onclick = () => start('export');
$('stop').onclick = async () => { try { $('stop').disabled = true; await api('/api/stop', {}); toast('Останавливаем и сохраняем завершённые проверки.'); await poll(); } catch (error) { toast(error.message, true); } };
$('refresh-results').onclick = () => { offset = 0; loadResults(); };
['result-sort', 'result-min'].forEach(id => $(id).onchange = () => { offset = 0; loadResults(); });
$('prev').onclick = () => { offset = Math.max(0, offset - 50); loadResults(); };
$('next').onclick = () => { offset += 50; loadResults(); };
$('sources').oninput = updateSourceCount;
$('use_sources').onchange = updateSourceCount;
$('request-profile').onchange = updateIdentity;
$('dnsbl-enabled').onchange = updateIdentity;
$('reset-sources').onclick = async () => { try { const value = await api('/api/defaults'); $('sources').value = value.sources.join('\n'); updateSourceCount(); toast('Встроенные источники восстановлены. Сохраните настройки.'); } catch (error) { toast(error.message, true); } };
$('import-file').onchange = async event => { const file = event.target.files[0]; if (!file) return; if (file.size > 20_000_000) { toast('Максимум 20 МБ на файл.', true); return; } $('proxies').value = await file.text(); toast('Список загружен. Он будет добавлен при сборе.'); };
document.querySelectorAll('[data-download]').forEach(node => node.onclick = async () => { try { node.disabled = true; const response = await fetch('/api/download/' + node.dataset.download, {headers:{'X-Workbench-Token':token}}); if (!response.ok) throw new Error('Файл ещё не готов.'); const url = URL.createObjectURL(await response.blob()); const anchor = document.createElement('a'); anchor.href = url; anchor.download = node.dataset.download; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 60000); } catch (error) { toast(error.message, true); } finally { node.disabled = false; } });

(async () => {
  try {
    fill(await api('/api/settings'));
    await poll();
    setInterval(poll, 2000);
  } catch (error) {
    toast(error.message, true);
  }
})();

function renderTheme() {
  const dark = document.documentElement.dataset.theme === 'dark';
  $('theme-toggle').textContent = dark ? '☀ Светлая тема' : '☾ Тёмная тема';
  $('theme-toggle').setAttribute('aria-label', dark ? 'Включить светлую тему' : 'Включить тёмную тему');
}
$('theme-toggle').onclick = () => { const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'; document.documentElement.dataset.theme = theme; try { localStorage.setItem('proxy-workbench-theme', theme); } catch {} renderTheme(); };
renderTheme();
