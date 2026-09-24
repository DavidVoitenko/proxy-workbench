'use strict';

const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="workbench-token"]').content;
const PRODUCT_VERSION = '__PRODUCT_VERSION__';
const LANG_KEY = 'proxy-workbench-lang';
let settings;
let state = {};
let resultTargets = [];
let resultData = null;
let detailRow = null;
let offset = 0;
let currentTab = 'scan';
let lastFinished = null;
let toastTimer;
let polling = false;
let resultBusy = false;

const messages = {
  en: {
    'lang.button': 'RU',
    'lang.label': 'Switch interface to Russian',
    'theme.light': '☀ Light theme',
    'theme.dark': '☾ Dark theme',
    'theme.toLight': 'Switch to light theme',
    'theme.toDark': 'Switch to dark theme',
    'nav.workspace': 'WORKSPACE',
    'nav.workspaceTitle': 'Workspace',
    'nav.scan': 'Scan',
    'nav.results': 'Results',
    'nav.sources': 'Sources',
    'nav.help': 'How it works',
    'sidebar.local': 'Runs on your device',
    'sidebar.data': 'Data and results are stored<br>in the local data folder.',
    'common.saveSettings': 'Save settings',
    'common.close': 'Close',
    'scan.eyebrow': 'FIND. CHECK. SAVE.',
    'scan.title': 'Proxies for your tasks',
    'scan.lead': 'One or more services. The result is proxies that work with every one of them.',
    'targets.title': 'Which services to check',
    'targets.badge': 'Condition: all services',
    'targets.hint': 'Add the exact URL of a page or API. Each service must reach the configured success threshold.',
    'targets.add': '＋ Add service',
    'check.title': 'How to check',
    'check.badge': 'Full sweep',
    'check.attempts': 'Attempts per service',
    'check.attemptsHint': 'Repeated stability measurements',
    'check.timeout': 'Timeout, seconds',
    'check.timeoutHint': 'Limit for a single request',
    'check.threshold': 'Success threshold',
    'check.thresholdHint': 'Applied to each service separately',
    'threshold.twoThirds': 'At least ⅔ of attempts',
    'threshold.all': 'All attempts (strict)',
    'threshold.one': 'At least 1 per service',
    'check.advanced': 'Performance and response size',
    'check.workers': 'Parallel checks',
    'check.rate': 'Requests / second',
    'check.rateHint': '0 — no rate limit',
    'check.maxBytes': 'Maximum response bytes',
    'check.advancedHint': 'The sweep is not limited by time or pool size. A large list can take several hours to check.',
    'identity.title': 'Fingerprint and cleanliness',
    'identity.hint': 'The request profile sets only neutral HTTP metadata. It does not change the TLS/browser fingerprint, does not add personal data and does not guarantee anonymity.',
    'identity.profile': 'Request profile',
    'identity.dnsbl': 'Check public DNSBLs',
    'identity.dnsblHint': 'Optional. Uses DNS queries only, no API keys.',
    'identity.zones': 'DNSBL zones',
    'identity.zonesHint': 'One zone name per line. Empty — DNSBL is off.',
    'identity.dnsblTimeout': 'DNSBL timeout, seconds',
    'identity.dnsblTimeoutHint': 'Per zone and per address.',
    'identity.strict': 'Strict mode',
    'identity.strictHint': 'Do not export if a DNSBL did not respond.',
    'identity.denylist': 'Local denylist',
    'identity.denylistPlaceholder': '11.0.0.0/24\nhttp://11.0.0.1:8080\n# comment',
    'identity.denylistHint': 'IP, CIDR or exact proxy address. Stored only in data/denylist.txt and never included in the code.',
    'identity.applyDenylist': 'Apply the local denylist during collection, checks and export',
    'profile.workbench': 'Workbench profile',
    'profile.standard': 'Standard',
    'profile.minimal': 'Minimal',
    'profile.fallback': 'Request profile',
    'profileDesc.workbench': 'User-Agent ProxyWorkbench/{version}, Accept and Accept-Encoding.',
    'profileDesc.standard': 'User-Agent ProxyWorkbench/{version}, Accept for JSON/text and Accept-Encoding.',
    'profileDesc.minimal': 'User-Agent ProxyWorkbench/{version} only.',
    'profileDesc.fallback': 'Neutral HTTP headers.',
    'output.title': 'What to save',
    'output.ranking': 'Ranking',
    'output.count': 'Number of proxies',
    'output.countHint': '0 — all matching. Does not affect how much is checked.',
    'sort.quality': 'Quality: stability + speed',
    'sort.speed': 'Speed: fastest first',
    'monitor.eyebrow': 'CURRENT CHECK',
    'monitor.speed': 'Proxies / sec',
    'monitor.eta': 'Time remaining (approx.)',
    'monitor.passed': 'Matching proxies',
    'monitor.sources': 'Connected sources',
    'monitor.start': 'Find and check',
    'monitor.resume': 'Continue database',
    'monitor.resumeTitle': 'Check the remaining database addresses with the current settings',
    'monitor.stop': 'Stop',
    'monitor.recheck': 'Recheck the whole database',
    'monitor.callout': 'Progress is saved when you stop. “Continue database” skips checks already completed for the same profile.',
    'mini.title': 'Results that fit your service',
    'mini.text': 'We check the real HTTP response through the proxy. DNSBL and the local denylist flag potentially dirty addresses but do not determine anonymity.',
    'mini.link': 'Open ranking',
    'log.title': 'Execution log',
    'log.empty': 'Check progress will appear here.',
    'phase.ready': 'Ready to start',
    'phase.stopping': 'Stopping…',
    'phase.offline': 'No connection to the application',
    'phase.starting': 'Starting',
    'phase.collecting': 'Collecting sources',
    'phase.scanning': 'Checking',
    'phase.exporting': 'Saving',
    'phase.complete': 'Complete',
    'phase.stopped': 'Stopped',
    'phase.interrupted': 'Interrupted',
    'phase.error': 'Error',
    'progress.allQueued': 'All collected addresses will be checked',
    'progress.sources': 'Sources processed: {done} / {total}',
    'progress.remaining': 'Left to check: {count}',
    'duration.lessThanMinute': '< 1 min',
    'duration.minutes': '{count} min',
    'duration.hours': '{count} h',
    'job.idle': 'Configure services and start the search.',
    'job.summary': '{action} · services: {count}',
    'job.profile': ' · profile: {profile}',
    'job.seeLog': ' · details in the log',
    'action.run': 'Collect + check',
    'action.scan': 'Continue database',
    'action.recheck': 'New check',
    'action.recheck_passing': 'Re-check of matching proxies',
    'action.collect': 'Address collection',
    'action.export': 'Export',
    'action.fallback': 'Check',
    'results.eyebrow': 'SELECTION WITH CLEANLINESS CHECK',
    'results.title': 'Proxy ranking',
    'results.empty': 'Matching addresses will appear here after a check.',
    'results.context': 'Checked for: {targets} · profile: {profile}',
    'results.refresh': 'Refresh table',
    'results.order': 'Order',
    'results.byQuality': 'By quality',
    'results.bySpeed': 'By speed',
    'results.min': 'Success rate for each service',
    'results.minTwoThirds': 'At least ⅔',
    'results.minAll': 'All attempts',
    'results.minOne': 'At least 1',
    'results.top': 'How many to export',
    'results.topHint': '0 — everything that passed',
    'results.export': 'Build export',
    'results.exportNote': 'Exports are built after a check or with the button. Table filters alone do not change the files.',
    'results.exportReady': 'Export ready: {exported} of {passed} matching · checked {checked} / {candidates} · clean: {clean} · blacklist: {listed} · unknown: {unknown}{local}.',
    'results.localFiltered': ' · filtered locally: {count}',
    'results.downloads': 'Download ready files:',
    'results.noneYet': 'No results yet. Start a check on the first tab.',
    'results.noneMatching': 'No matching proxies for these conditions yet.',
    'results.total': '{count} matching',
    'results.details': 'Details ↗',
    'results.footnote': 'Latency is the median of a full successful request, including connection and TLS. Success is the worst value among the selected services. “Cleanliness” shows the local denylist and DNSBL verdict. Click “Details” to see every attempt.',
    'col.proxy': 'Proxy',
    'col.quality': 'Quality',
    'col.latency': 'Latency',
    'col.jitter': 'Jitter',
    'col.success': 'Success',
    'col.cleanliness': 'Cleanliness',
    'col.anonymity': 'Anonymity',
    'monitor.recheckPassing': 'Re-check only matching proxies (fast)',
    'sort.uptime': 'Uptime: passed most re-checks first',
    'results.byUptime': 'By uptime',
    'col.uptime': 'Uptime',
    'col.uptimeHint': 'Passed re-checks / all checks',
    'col.working': 'Working',
    'col.workingHint': 'Matching / checked in the last export',
    'col.country': 'Country',
    'preset.label': 'Preset:',
    'preset.quick': '⚡ Quick',
    'preset.balanced': '⚖ Balanced',
    'preset.thorough': '🔬 Thorough',
    'preset.hint': 'Quick: 1 attempt, short timeouts, more workers. Thorough: 5 attempts, patient timeouts.',
    'preset.applied': 'Preset applied. Save or start a scan to use it.',
    'geo.countries': 'Countries',
    'geo.countriesHint': 'ISO codes. Other countries are skipped before checking, so the scan is much shorter.',
    'want.label': 'Stop after finding',
    'want.hint': '0 — check everything. Otherwise the scan ends as soon as this many proxies match.',
    'geo.title': 'Country database',
    'geo.missing': 'Not downloaded',
    'geo.ready': 'Ready · {count} ranges',
    'geo.hint': 'Needed for country filters and the Country column. The free DB-IP Country Lite file (about 7 MB) is downloaded once into the local data folder; lookups then work offline. Geonode sources already include countries.',
    'geo.download': 'Download / update',
    'geo.downloading': 'Downloading the country database…',
    'geo.updated': 'Country database updated.',
    'geo.attribution': 'IP geolocation by DB-IP',
    'check.connectTimeout': 'Connect timeout, seconds',
    'check.connectTimeoutHint': 'Dead addresses fail sooner; the full request still gets the main timeout.',
    'check.failFast': 'Stop early when a proxy can no longer pass',
    'check.failFastHint': 'Skips the remaining attempts once the success threshold is out of reach. Much faster on big lists.',
    'sort.stability': 'Stability: lowest jitter first',
    'results.byStability': 'By stability',
    'filter.protocol': 'Protocol',
    'filter.allProtocols': 'All protocols',
    'filter.maxLatency': 'Max latency, ms',
    'filter.maxLatencyHint': '0 — no limit',
    'filter.searchPlaceholder': 'Search by address or port',
    'filter.copyPage': 'Copy this page',
    'api.label': 'API for your programs:',
    'api.copy': 'Copy',
    'api.hint': 'Returns the latest export, filtered by protocol, country, latency and anonymity. See README → Local API.',
    'toast.apiCopied': 'API address copied.',
    'toast.copied': 'Copied {count} proxies.',
    'toast.copyEmpty': 'Nothing to copy on this page.',
    'toast.copyFailed': 'The browser blocked clipboard access.',
    'details.aborted': 'Stopped early: this proxy could no longer reach the success threshold.',
    'anon.judge': 'Anonymity judge URL (optional)',
    'anon.judgeHint': 'An echo page that shows the client IP and request headers. Each working proxy is rated transparent, anonymous or elite. Use an http:// judge: over HTTPS a proxy cannot add headers. Your own IP is requested once directly and never saved.',
    'anon.min': 'Minimum anonymity',
    'anon.any': 'Any level',
    'anon.minAnonymous': 'Anonymous or elite',
    'anon.minElite': 'Elite only',
    'anon.minHint': 'Applies only when a judge URL is set.',
    'anon.elite': 'Elite',
    'anon.anonymous': 'Anonymous',
    'anon.transparent': 'Transparent',
    'anon.unknown': 'Unknown',
    'anon.off': '—',
    'anon.details': 'Anonymity:',
    'anon.signals': 'signals: {signals}',
    'anon.realIp': 'your real IP is visible',
    'anon.counts': ' · elite: {elite} · anonymous: {anonymous} · transparent: {transparent}',
    'col.source': 'Source',
    'col.lines': 'Lines',
    'col.rejected': 'Rejected',
    'col.blocked': 'Blocked',
    'col.download': 'Download',
    'col.attempt': 'Attempt',
    'col.response': 'Response',
    'col.time': 'Time',
    'col.bytes': 'Bytes',
    'col.result': 'Result',
    'unit.ms': '{value} ms',
    'reputation.clean': 'Clean',
    'reputation.listed': 'Blacklist',
    'reputation.unknown': 'Unknown',
    'reputation.local_denied': 'Local blacklist',
    'details.cleanliness': 'Cleanliness:',
    'details.localRule': ' · local rule: ',
    'details.service': 'Service {number}',
    'details.success': 'Success',
    'dnsbl.listed': 'listed',
    'dnsbl.clear': 'clear',
    'dnsbl.noAnswer': 'no answer',
    'dnsbl.notChecked': 'not checked',
    'sources.eyebrow': 'TRANSPARENT COLLECTION',
    'sources.title': 'Sources and your own lists',
    'sources.lead': 'Every unique address from the connected lists goes into the database. No first-N-lines limit.',
    'sources.public': 'Public lists',
    'sources.use': 'Load proxies from sources',
    'sources.list': 'Sources: URL or protocol + URL, one per line',
    'sources.timeout': 'Source timeout, seconds',
    'sources.reset': 'Built-in sources',
    'sources.hint': 'A plain URL is an HTTP list. For SOCKS5: socks5 URL. For a paginated JSON API: geonode URL. Errors and incomplete downloads are shown below. Size limits and safe address checks are on by default; local mock sources are available only through a CLI flag.',
    'sources.off': 'Disabled',
    'own.title': 'Add your own list',
    'own.hint': 'Paste addresses or choose a TXT file. Duplicates are merged automatically.',
    'own.file': '↑ Choose TXT file',
    'own.list': 'One proxy per line',
    'own.note': 'HTTP / CONNECT and SOCKS5. Public IPs only, without login or password. The database accumulates: disabling sources does not remove previously collected addresses.',
    'own.collect': 'Collect addresses only',
    'report.title': 'Source report',
    'report.none': 'No downloads yet',
    'report.denylistError': 'Could not read the denylist — the check will be inconclusive',
    'report.loaded': '{done} / {total} fully loaded',
    'report.empty': 'Results of the last collection will appear here.',
    'report.source': 'Source {number}',
    'report.ownList': 'Custom list',
    'report.emptyList': 'Empty list',
    'report.noValid': 'No usable addresses',
    'report.done': 'Done',
    'report.incomplete': 'Not completed',
    'help.eyebrow': 'QUICK START',
    'help.title': 'From a list to working proxies',
    'help.lead': 'Configure once. Come back to checks and results in this window.',
    'help.s1.title': 'Choose services',
    'help.s1.p1': 'Enter the URL of every service you need. A proxy is selected only if it works with all of them. Code 200 is a normal successful response; for APIs that use other codes, specify your own.',
    'help.s1.p2': 'To guard against placeholder pages, specify text that must appear in the response. Redirects are not followed: use the final URL.',
    'help.s2.title': 'Set the request profile',
    'help.s2.p1': 'The preset sets only User-Agent and Accept/Accept-Encoding. It is part of the check profile, so changing it creates separate results. Personal values are never added automatically.',
    'help.s2.p2': 'This is technical identification of the HTTP request, not browser impersonation, TLS/JA3/JA4 masking or a guarantee of anonymity.',
    'help.s3.title': 'Check cleanliness',
    'help.s3.p1': 'The local denylist matches IPs, CIDRs and exact addresses. DNSBL flags responses from public blacklist zones; in strict mode an unknown response is not treated as clean.',
    'help.s3.p2': 'DNSBL is enabled manually and uses DNS queries only. The list and zones are stored locally in data/.',
    'help.s4.title': 'Pick the best and download',
    'help.s4.p1': '“By speed” puts the lowest latency first. “By quality” balances success rate, speed and jitter. Enter any number; 0 keeps every matching proxy.',
    'help.s4.p2': 'TXT contains the addresses, CSV the ranking table, JSON detailed request results and safe cleanliness statuses.',
    'help.s5.title': 'Good to know',
    'help.s5.p1': 'This measures availability and response time, not throughput in Mbit/s. A public proxy may stop working after the check: use “Recheck” for fresh results.',
    'help.s5.p2': 'New URLs, measurement parameters, request profile or cleanliness policy create a separate profile. Settings and history are stored locally. Closing the tab does not stop the application; to exit, press Ctrl+C in its terminal window.',
    'help.clear': 'Delete local results',
    'help.clearHint': 'Deletes the database, profiles and exports but keeps settings and the denylist. Local results cannot be restored.',
    'footer.tagline': 'Full sweep · Repeated measurements · Your services',
    'target.name': 'Service name',
    'target.defaultName': 'Custom service',
    'target.remove': 'Remove service',
    'target.url': 'URL to check',
    'target.statuses': 'Allowed HTTP codes',
    'target.statusesPlaceholder': '200, 204 or 200-299',
    'target.contains': 'Text in response (optional)',
    'target.containsPlaceholder': 'For example: healthy',
    'target.advanced': 'Method, local headers and SHA-256',
    'target.method': 'Method',
    'target.headers': 'Service headers — JSON',
    'target.headersHint': 'Only safe HTTP headers are allowed; values are not exported, but the request still goes through a public proxy.',
    'target.sha256': 'Response body SHA-256 (optional)',
    'target.sha256Placeholder': 'Expected response hash',
    'error.app': 'Application error',
    'error.maxTargets': 'Maximum 20 services.',
    'error.needTarget': 'At least one service is required.',
    'error.statusFormat': 'Enter HTTP codes, for example 200, 204 or 200-299.',
    'error.statusRange': 'HTTP codes must be between 100 and 599.',
    'error.headersJson': 'Service headers must be valid JSON.',
    'error.urlRequired': 'Enter a URL for every service.',
    'error.fileTooLarge': 'Maximum 20 MB per file.',
    'error.fileNotReady': 'The file is not ready yet.',
    'toast.saved': 'Settings saved.',
    'toast.exporting': 'Building files with the selected filters.',
    'toast.started': 'Start accepted. Progress will appear in a few seconds.',
    'toast.stopping': 'Stopping and saving completed checks.',
    'toast.cleared': 'Local results deleted: {count}.',
    'toast.sourcesReset': 'Built-in sources restored. Save the settings.',
    'toast.listLoaded': 'List loaded. It will be added during collection.',
    'confirm.clear': 'Delete the local database, profiles and exports? Settings and the denylist will be kept.'
  },
  ru: {
    'lang.button': 'EN',
    'lang.label': 'Переключить интерфейс на английский',
    'theme.light': '☀ Светлая тема',
    'theme.dark': '☾ Тёмная тема',
    'theme.toLight': 'Включить светлую тему',
    'theme.toDark': 'Включить тёмную тему',
    'nav.workspace': 'РАБОЧЕЕ ПРОСТРАНСТВО',
    'nav.workspaceTitle': 'Рабочее пространство',
    'nav.scan': 'Проверка',
    'nav.results': 'Результаты',
    'nav.sources': 'Источники',
    'nav.help': 'Как это работает',
    'sidebar.local': 'Работает на вашем устройстве',
    'sidebar.data': 'Данные и результаты хранятся<br>в локальной папке data.',
    'common.saveSettings': 'Сохранить настройки',
    'common.close': 'Закрыть',
    'scan.eyebrow': 'НАЙТИ. ПРОВЕРИТЬ. СОХРАНИТЬ.',
    'scan.title': 'Прокси под ваши задачи',
    'scan.lead': 'Один или несколько сервисов. В результате — прокси, работающие с каждым.',
    'targets.title': 'Какие сервисы проверять',
    'targets.badge': 'Условие: все сервисы',
    'targets.hint': 'Добавьте точный URL страницы или API. Каждый сервис должен пройти заданный порог успешности.',
    'targets.add': '＋ Добавить сервис',
    'check.title': 'Как проверять',
    'check.badge': 'Полный обход',
    'check.attempts': 'Попыток на сервис',
    'check.attemptsHint': 'Повторные замеры стабильности',
    'check.timeout': 'Таймаут, секунд',
    'check.timeoutHint': 'Лимит одного запроса',
    'check.threshold': 'Порог успешности',
    'check.thresholdHint': 'Отдельно для каждого сервиса',
    'threshold.twoThirds': 'Не менее ⅔ попыток',
    'threshold.all': 'Все попытки (строго)',
    'threshold.one': 'Хотя бы 1 на сервис',
    'check.advanced': 'Производительность и размер ответа',
    'check.workers': 'Параллельных проверок',
    'check.rate': 'Запросов / секунду',
    'check.rateHint': '0 — без ограничения частоты',
    'check.maxBytes': 'Максимум байт ответа',
    'check.advancedHint': 'Обход не ограничен временем или размером пула. Большой список может проверяться несколько часов.',
    'identity.title': 'Отпечаток и чистота',
    'identity.hint': 'Request-профиль задаёт только нейтральные HTTP-метаданные. Он не меняет TLS/browser fingerprint, не добавляет личные данные и не гарантирует анонимность.',
    'identity.profile': 'Request-профиль',
    'identity.dnsbl': 'Проверять публичные DNSBL',
    'identity.dnsblHint': 'Опционально. Нужны только DNS-запросы, без API-ключей.',
    'identity.zones': 'DNSBL-зоны',
    'identity.zonesHint': 'По одному имени зоны. Пусто — DNSBL выключен.',
    'identity.dnsblTimeout': 'Таймаут DNSBL, секунд',
    'identity.dnsblTimeoutHint': 'На одну зону и один адрес.',
    'identity.strict': 'Строгий режим',
    'identity.strictHint': 'Не экспортировать, если DNSBL не ответил.',
    'identity.denylist': 'Локальный denylist',
    'identity.denylistPlaceholder': '11.0.0.0/24\nhttp://11.0.0.1:8080\n# комментарий',
    'identity.denylistHint': 'IP, CIDR или точный адрес прокси. Хранится только в data/denylist.txt и не включается в код.',
    'identity.applyDenylist': 'Применять локальный denylist при сборе, проверке и экспорте',
    'profile.workbench': 'Рабочий профиль',
    'profile.standard': 'Стандартный',
    'profile.minimal': 'Минимальный',
    'profile.fallback': 'Request-профиль',
    'profileDesc.workbench': 'User-Agent ProxyWorkbench/{version}, Accept и Accept-Encoding.',
    'profileDesc.standard': 'User-Agent ProxyWorkbench/{version}, Accept для JSON/text и Accept-Encoding.',
    'profileDesc.minimal': 'Только User-Agent ProxyWorkbench/{version}.',
    'profileDesc.fallback': 'Нейтральные HTTP-заголовки.',
    'output.title': 'Что сохранить',
    'output.ranking': 'Ранжирование',
    'output.count': 'Количество прокси',
    'output.countHint': '0 — все подходящие. На объём проверки не влияет.',
    'sort.quality': 'Качество: стабильность + скорость',
    'sort.speed': 'Скорость: самые быстрые первыми',
    'monitor.eyebrow': 'ТЕКУЩАЯ ПРОВЕРКА',
    'monitor.speed': 'Прокси / сек',
    'monitor.eta': 'Осталось примерно',
    'monitor.passed': 'Подходящих прокси',
    'monitor.sources': 'Подключено источников',
    'monitor.start': 'Найти и проверить',
    'monitor.resume': 'Продолжить базу',
    'monitor.resumeTitle': 'Проверить оставшиеся адреса базы с текущими настройками',
    'monitor.stop': 'Остановить',
    'monitor.recheck': 'Перепроверить всю базу заново',
    'monitor.callout': 'При остановке прогресс сохраняется. «Продолжить базу» пропускает уже завершённые проверки того же профиля.',
    'mini.title': 'Результат — под ваш сервис',
    'mini.text': 'Проверяем настоящий HTTP-ответ через прокси. DNSBL и локальный denylist отмечают потенциально грязные адреса, но не определяют анонимность.',
    'mini.link': 'Открыть рейтинг',
    'log.title': 'Журнал выполнения',
    'log.empty': 'Здесь появится ход проверки.',
    'phase.ready': 'Готов к запуску',
    'phase.stopping': 'Останавливаем…',
    'phase.offline': 'Нет связи с приложением',
    'phase.starting': 'Запуск',
    'phase.collecting': 'Сбор источников',
    'phase.scanning': 'Проверка',
    'phase.exporting': 'Сохранение',
    'phase.complete': 'Завершено',
    'phase.stopped': 'Остановлено',
    'phase.interrupted': 'Прервано',
    'phase.error': 'Ошибка',
    'progress.allQueued': 'Все собранные адреса попадут в проверку',
    'progress.sources': 'Источников обработано: {done} / {total}',
    'progress.remaining': 'Осталось проверить: {count}',
    'duration.lessThanMinute': '< 1 мин',
    'duration.minutes': '{count} мин',
    'duration.hours': '{count} ч',
    'job.idle': 'Настройте сервисы и запустите поиск.',
    'job.summary': '{action} · сервисов: {count}',
    'job.profile': ' · профиль: {profile}',
    'job.seeLog': ' · подробности в журнале',
    'action.run': 'Сбор + проверка',
    'action.scan': 'Продолжение базы',
    'action.recheck': 'Новая проверка',
    'action.recheck_passing': 'Перепроверка подходящих',
    'action.collect': 'Сбор адресов',
    'action.export': 'Экспорт',
    'action.fallback': 'Проверка',
    'results.eyebrow': 'ОТБОР С ПРОВЕРКОЙ ЧИСТОТЫ',
    'results.title': 'Рейтинг прокси',
    'results.empty': 'После проверки здесь появятся подходящие адреса.',
    'results.context': 'Проверено для: {targets} · профиль: {profile}',
    'results.refresh': 'Обновить таблицу',
    'results.order': 'Порядок',
    'results.byQuality': 'По качеству',
    'results.bySpeed': 'По скорости',
    'results.min': 'Успешность каждого сервиса',
    'results.minTwoThirds': 'Не менее ⅔',
    'results.minAll': 'Все попытки',
    'results.minOne': 'Хотя бы 1',
    'results.top': 'Сколько экспортировать',
    'results.topHint': '0 — все прошедшие',
    'results.export': 'Сформировать экспорт',
    'results.exportNote': 'Экспорт создаётся после проверки или по кнопке. Фильтры таблицы сами по себе не меняют файлы.',
    'results.exportReady': 'Готовый экспорт: {exported} из {passed} подходящих · проверено {checked} / {candidates} · чистых: {clean} · blacklist: {listed} · неизвестных: {unknown}{local}.',
    'results.localFiltered': ' · локально отсечено: {count}',
    'results.downloads': 'Скачать готовые файлы:',
    'results.noneYet': 'Ещё нет результатов. Запустите проверку на первой вкладке.',
    'results.noneMatching': 'По этим условиям пока нет подходящих прокси.',
    'results.total': '{count} подходящих',
    'results.details': 'Детали ↗',
    'results.footnote': 'Задержка — медиана полного успешного запроса, включая соединение и TLS. Успешность — худший показатель среди выбранных сервисов. «Чистота» показывает локальный denylist и DNSBL-вердикт. Нажмите «Детали» для всех попыток.',
    'col.proxy': 'Прокси',
    'col.quality': 'Качество',
    'col.latency': 'Задержка',
    'col.jitter': 'Разброс',
    'col.success': 'Успешность',
    'col.cleanliness': 'Чистота',
    'col.anonymity': 'Анонимность',
    'monitor.recheckPassing': 'Перепроверить только подходящие (быстро)',
    'sort.uptime': 'Живучесть: чаще проходили перепроверки',
    'results.byUptime': 'По живучести',
    'col.uptime': 'Живучесть',
    'col.uptimeHint': 'Пройдено перепроверок / всего проверок',
    'col.working': 'Рабочих',
    'col.workingHint': 'Подходящих / проверено в последнем экспорте',
    'col.country': 'Страна',
    'preset.label': 'Пресет:',
    'preset.quick': '⚡ Быстро',
    'preset.balanced': '⚖ Баланс',
    'preset.thorough': '🔬 Тщательно',
    'preset.hint': 'Быстро: 1 попытка, короткие таймауты, больше воркеров. Тщательно: 5 попыток, терпеливые таймауты.',
    'preset.applied': 'Пресет применён. Сохраните настройки или запустите проверку.',
    'geo.countries': 'Страны',
    'geo.countriesHint': 'ISO-коды. Адреса из других стран пропускаются ещё до проверки, поэтому проход намного короче.',
    'want.label': 'Остановиться после',
    'want.hint': '0 — проверить всё. Иначе проверка завершится, как только найдётся столько подходящих прокси.',
    'geo.title': 'База стран',
    'geo.missing': 'Не скачана',
    'geo.ready': 'Готова · диапазонов: {count}',
    'geo.hint': 'Нужна для фильтра по странам и колонки «Страна». Бесплатный файл DB-IP Country Lite (около 7 МБ) скачивается один раз в локальную папку data, дальше поиск работает офлайн. Источники Geonode уже содержат страну.',
    'geo.download': 'Скачать / обновить',
    'geo.downloading': 'Скачиваем базу стран…',
    'geo.updated': 'База стран обновлена.',
    'geo.attribution': 'Геолокация IP: DB-IP',
    'check.connectTimeout': 'Таймаут подключения, секунд',
    'check.connectTimeoutHint': 'Мёртвые адреса отсеиваются быстрее; на весь запрос по-прежнему действует основной таймаут.',
    'check.failFast': 'Досрочно отбраковывать безнадёжные прокси',
    'check.failFastHint': 'Оставшиеся попытки пропускаются, когда порог успешности уже недостижим. Сильно ускоряет большие списки.',
    'sort.stability': 'Стабильность: минимальный разброс',
    'results.byStability': 'По стабильности',
    'filter.protocol': 'Протокол',
    'filter.allProtocols': 'Все протоколы',
    'filter.maxLatency': 'Макс. задержка, мс',
    'filter.maxLatencyHint': '0 — без ограничения',
    'filter.searchPlaceholder': 'Поиск по адресу или порту',
    'filter.copyPage': 'Скопировать страницу',
    'api.label': 'API для своих программ:',
    'api.copy': 'Скопировать',
    'api.hint': 'Отдаёт последний экспорт с фильтрами по протоколу, стране, задержке и анонимности. Подробнее: README → Local API.',
    'toast.apiCopied': 'Адрес API скопирован.',
    'toast.copied': 'Скопировано прокси: {count}.',
    'toast.copyEmpty': 'На этой странице нечего копировать.',
    'toast.copyFailed': 'Браузер запретил доступ к буферу обмена.',
    'details.aborted': 'Проверка остановлена досрочно: прокси уже не мог достичь порога успешности.',
    'anon.judge': 'Judge-URL для проверки анонимности (необязательно)',
    'anon.judgeHint': 'Echo-страница, которая показывает IP клиента и заголовки запроса. Каждый рабочий прокси получает уровень: прозрачный, анонимный или элитный. Используйте http:// judge: через HTTPS прокси не может добавить заголовки. Ваш IP запрашивается один раз напрямую и не сохраняется.',
    'anon.min': 'Минимальная анонимность',
    'anon.any': 'Любой уровень',
    'anon.minAnonymous': 'Анонимные и элитные',
    'anon.minElite': 'Только элитные',
    'anon.minHint': 'Работает, только если указан judge-URL.',
    'anon.elite': 'Элитный',
    'anon.anonymous': 'Анонимный',
    'anon.transparent': 'Прозрачный',
    'anon.unknown': 'Неизвестно',
    'anon.off': '—',
    'anon.details': 'Анонимность:',
    'anon.signals': 'признаки: {signals}',
    'anon.realIp': 'виден ваш реальный IP',
    'anon.counts': ' · элитных: {elite} · анонимных: {anonymous} · прозрачных: {transparent}',
    'col.source': 'Источник',
    'col.lines': 'Строк',
    'col.rejected': 'Отклонено',
    'col.blocked': 'Заблокировано',
    'col.download': 'Загрузка',
    'col.attempt': 'Попытка',
    'col.response': 'Ответ',
    'col.time': 'Время',
    'col.bytes': 'Байт',
    'col.result': 'Результат',
    'unit.ms': '{value} мс',
    'reputation.clean': 'Чистый',
    'reputation.listed': 'Blacklist',
    'reputation.unknown': 'Неизвестно',
    'reputation.local_denied': 'Локальный blacklist',
    'details.cleanliness': 'Чистота:',
    'details.localRule': ' · локальное правило: ',
    'details.service': 'Сервис {number}',
    'details.success': 'Успешно',
    'dnsbl.listed': 'найден',
    'dnsbl.clear': 'чисто',
    'dnsbl.noAnswer': 'нет ответа',
    'dnsbl.notChecked': 'не проверялись',
    'sources.eyebrow': 'ПРОЗРАЧНЫЙ СБОР',
    'sources.title': 'Источники и свои списки',
    'sources.lead': 'Все уникальные адреса из подключённых списков попадут в базу. Без ограничения первых N строк.',
    'sources.public': 'Публичные списки',
    'sources.use': 'Загружать прокси из источников',
    'sources.list': 'Источники: URL или протокол + URL, по одному на строке',
    'sources.timeout': 'Таймаут источника, секунд',
    'sources.reset': 'Встроенные источники',
    'sources.hint': 'Обычный URL — HTTP-список. Для SOCKS5: socks5 URL. Для JSON API с обходом страниц: geonode URL. Ошибки и неполные загрузки показаны ниже. По умолчанию включены лимиты размера и безопасная проверка адресов; локальные mock-источники доступны только через CLI-флаг.',
    'sources.off': 'Выключены',
    'own.title': 'Добавить свой список',
    'own.hint': 'Вставьте адреса или выберите TXT-файл. Дубликаты объединяются автоматически.',
    'own.file': '↑ Выбрать TXT-файл',
    'own.list': 'Один прокси на строку',
    'own.note': 'HTTP / CONNECT и SOCKS5. Только публичные IP без логина и пароля. База накапливается: отключение источников не удаляет ранее собранные адреса.',
    'own.collect': 'Только собрать адреса',
    'report.title': 'Отчёт по источникам',
    'report.none': 'Загрузки ещё не было',
    'report.denylistError': 'Ошибка чтения denylist — проверка будет неопределённой',
    'report.loaded': '{done} / {total} загружены полностью',
    'report.empty': 'Здесь будут результаты последнего сбора.',
    'report.source': 'Источник {number}',
    'report.ownList': 'Свой список',
    'report.emptyList': 'Пустой список',
    'report.noValid': 'Нет подходящих адресов',
    'report.done': 'Готово',
    'report.incomplete': 'Не завершено',
    'help.eyebrow': 'БЫСТРЫЙ СТАРТ',
    'help.title': 'От списка к рабочим прокси',
    'help.lead': 'Настройте один раз. Возвращайтесь к проверке и результатам в этом окне.',
    'help.s1.title': 'Выберите сервисы',
    'help.s1.p1': 'Введите URL каждого нужного сервиса. Прокси проходит отбор, только если работает со всеми. Код 200 — обычный успешный ответ; для API с другими кодами укажите свои.',
    'help.s1.p2': 'Для защиты от заглушек укажите текст, который должен быть в ответе. Редиректы не выполняются: используйте конечный URL.',
    'help.s2.title': 'Задайте request-профиль',
    'help.s2.p1': 'Пресет задаёт только User-Agent и Accept/Accept-Encoding. Он входит в профиль проверки, поэтому изменение создаёт отдельные результаты. Персональные значения не добавляются автоматически.',
    'help.s2.p2': 'Это техническая идентификация HTTP-запроса, а не browser impersonation, скрытие TLS/JA3/JA4 или гарантия анонимности.',
    'help.s3.title': 'Проверьте чистоту',
    'help.s3.p1': 'Локальный denylist сравнивает IP, CIDR и точные адреса. DNSBL отмечает ответы публичных blacklist-зон; неизвестный ответ не считается чистым в строгом режиме.',
    'help.s3.p2': 'DNSBL включается вручную и использует только DNS-запросы. Список и зоны хранятся локально в data/.',
    'help.s4.title': 'Выберите лучшие и скачайте',
    'help.s4.p1': '«По скорости» — сначала минимальная задержка. «По качеству» — баланс успешности, скорости и разброса. Укажите любое количество; 0 сохраняет все подходящие.',
    'help.s4.p2': 'TXT содержит адреса, CSV — таблицу рейтинга, JSON — подробные результаты запросов и безопасные статусы чистоты.',
    'help.s5.title': 'Что важно понимать',
    'help.s5.p1': 'Это оценка доступности и времени ответа, не пропускной способности в Мбит/с. Публичный прокси может перестать работать после проверки: используйте «Перепроверить» для свежих результатов.',
    'help.s5.p2': 'Новые URL, параметры замеров, request-профиль или политика чистоты создают отдельный профиль. Настройки и история хранятся локально. Закрытие вкладки не останавливает приложение; для выхода закройте его окно терминала через Ctrl+C.',
    'help.clear': 'Удалить локальные результаты',
    'help.clearHint': 'Удаляет базу, профили и экспорты, но сохраняет настройки и denylist. Действие необратимо для локальных результатов.',
    'footer.tagline': 'Полный обход · Повторные замеры · Ваши сервисы',
    'target.name': 'Название сервиса',
    'target.defaultName': 'Свой сервис',
    'target.remove': 'Удалить сервис',
    'target.url': 'URL для проверки',
    'target.statuses': 'Допустимые HTTP-коды',
    'target.statusesPlaceholder': '200, 204 или 200-299',
    'target.contains': 'Текст в ответе (необязательно)',
    'target.containsPlaceholder': 'Например: healthy',
    'target.advanced': 'Метод, локальные заголовки и SHA-256',
    'target.method': 'Метод',
    'target.headers': 'Заголовки сервиса — JSON',
    'target.headersHint': 'Разрешены только безопасные HTTP-заголовки; значения не экспортируются, но запрос всё равно идёт через публичный прокси.',
    'target.sha256': 'SHA-256 тела ответа (необязательно)',
    'target.sha256Placeholder': 'Ожидаемый хеш ответа',
    'error.app': 'Ошибка приложения',
    'error.maxTargets': 'Максимум 20 сервисов.',
    'error.needTarget': 'Нужен хотя бы один сервис.',
    'error.statusFormat': 'Укажите HTTP-коды: например 200, 204 или 200-299.',
    'error.statusRange': 'HTTP-код должен быть от 100 до 599.',
    'error.headersJson': 'Заголовки сервиса должны быть корректным JSON.',
    'error.urlRequired': 'Введите URL каждого сервиса.',
    'error.fileTooLarge': 'Максимум 20 МБ на файл.',
    'error.fileNotReady': 'Файл ещё не готов.',
    'toast.saved': 'Настройки сохранены.',
    'toast.exporting': 'Формируем файлы по выбранным фильтрам.',
    'toast.started': 'Запуск принят. Прогресс появится через несколько секунд.',
    'toast.stopping': 'Останавливаем и сохраняем завершённые проверки.',
    'toast.cleared': 'Локальные результаты удалены: {count}.',
    'toast.sourcesReset': 'Встроенные источники восстановлены. Сохраните настройки.',
    'toast.listLoaded': 'Список загружен. Он будет добавлен при сборе.',
    'confirm.clear': 'Удалить локальную базу, профили и экспорты? Настройки и denylist останутся.'
  }
};

function initialLang() {
  let saved = null;
  try { saved = localStorage.getItem(LANG_KEY); } catch {}
  if (saved === 'en' || saved === 'ru') return saved;
  return String(navigator.language || '').toLowerCase().startsWith('ru') ? 'ru' : 'en';
}

let lang = initialLang();

function t(key, values={}) {
  const text = messages[lang][key] ?? messages.en[key] ?? key;
  return text.replace(/\{(\w+)\}/g, (match, name) => name in values ? String(values[name]) : match);
}

// Server validation messages and CLI log lines are written in Russian by the
// Python side. In English mode they are translated here; unknown text is shown as is.
const serverMessagesEn = {
  'Ожидаются настройки проверки.': 'Scan settings are expected.',
  'Неизвестная версия настроек.': 'Unknown settings version.',
  'Неизвестный request-профиль.': 'Unknown request profile.',
  'Список denylist слишком большой: максимум 2 МБ.': 'The denylist is too large: 2 MB maximum.',
  'Настройки чистоты должны быть объектом.': 'Cleanliness settings must be an object.',
  'Настройки чистоты должны быть логическими.': 'Cleanliness settings must be booleans.',
  'Настройки проверки чистоты должны быть логическими.': 'Cleanliness settings must be booleans.',
  'DNSBL-зоны должны быть списком.': 'DNSBL zones must be a list.',
  'DNSBL-зона должна быть строкой.': 'A DNSBL zone must be a string.',
  'Некорректная DNSBL-зона.': 'Invalid DNSBL zone.',
  'Таймаут DNSBL должен быть числом.': 'The DNSBL timeout must be a number.',
  'Таймаут DNSBL должен быть от 0.1 до 30 секунд.': 'The DNSBL timeout must be between 0.1 and 30 seconds.',
  'Неверный режим сортировки или источников.': 'Invalid sort or sources mode.',
  'Список прокси слишком большой: максимум 20 МБ.': 'The proxy list is too large: 20 MB maximum.',
  'Источники должны быть списком URL (до 5000).': 'Sources must be a list of URLs (up to 5000).',
  'Добавьте от 1 до 20 сервисов.': 'Add between 1 and 20 services.',
  'Название сервиса: максимум 160 символов.': 'Service name: 160 characters maximum.',
  'Файл gui-settings.json повреждён или недоступен; исправьте его перед продолжением.': 'gui-settings.json is damaged or unreadable; fix it before continuing.',
  'Не удалось прочитать data/denylist.txt. Исправьте файл перед сохранением.': 'Could not read data/denylist.txt. Fix the file before saving.',
  'Файл gui-settings.json должен содержать объект настроек.': 'gui-settings.json must contain a settings object.',
  'Проверка уже идёт. Сначала остановите её.': 'A scan is already running. Stop it first.',
  'Неизвестное действие.': 'Unknown action.',
  'Сначала запустите проверку.': 'Run a scan first.',
  'Включите источники или добавьте свой список прокси.': 'Enable sources or add your own proxy list.',
  'Не удалось запустить проверку.': 'Could not start the scan.',
  'Сначала остановите текущую операцию.': 'Stop the current operation first.',
  'Неверные параметры рейтинга.': 'Invalid ranking parameters.',
  'Не удалось прочитать локальный denylist; обновите список.': 'Could not read the local denylist; update the list.',
  'Некорректный адрес прокси.': 'Invalid proxy address.',
  'Результаты не найдены.': 'No results found.',
  'Детали прокси не найдены.': 'Proxy details not found.',
  'Неверный адрес приложения.': 'Wrong application address.',
  'Запрос с другого сайта отклонён.': 'Cross-site request rejected.',
  'Обновите страницу приложения.': 'Reload the application page.',
  'Файл не найден.': 'File not found.',
  'Не найдено.': 'Not found.',
  'Не удалось прочитать данные. Повторите после завершения операции.': 'Could not read data. Try again after the current operation finishes.',
  'Слишком большой запрос.': 'Request too large.',
  'Проверьте поля настроек.': 'Check the settings fields.',
  'Не удалось записать настройки. Проверьте доступ к папке data.': 'Could not write settings. Check access to the data folder.',
  'Данные уже используются другим процессом.': 'The data folder is used by another process.',
  'Страны: используйте двухбуквенные ISO-коды, например DE,NL.': 'Countries: use two-letter ISO codes, for example DE,NL.',
  'Страны: ожидается список ISO-кодов, например DE,NL.': 'Countries: a list of ISO codes is expected, for example DE,NL.',
  'Не удалось скачать базу стран.': 'Could not download the country database.',
  'База стран повреждена; выполните update-geoip.': 'The country database is damaged; run update-geoip.',
  'База стран не найдена: страна известна только для адресов из Geonode. Скачайте базу командой update-geoip.': 'Country database not found: countries are known only for Geonode addresses. Download it with update-geoip.',
  'Настройки анонимности должны быть объектом.': 'Anonymity settings must be an object.',
  'anonymity.judge_url: ожидается http(s) URL': 'anonymity.judge_url: an http(s) URL is expected',
  'anonymity.judge_url: нужен http(s) URL без userinfo': 'anonymity.judge_url: an http(s) URL without userinfo is required',
  'anonymity.judge_url: некорректный порт': 'anonymity.judge_url: invalid port',
  'Уровень анонимности: any, anonymous или elite.': 'Anonymity level: any, anonymous or elite.',
  'Остановлено. Завершённые проверки сохранены; scan продолжит проход.': 'Stopped. Finished checks are saved; scan will resume the pass.',
  'Эта папка data уже используется другим запуском.': 'This data folder is already used by another run.',
  'Удалено: ничего': 'Deleted: nothing',
  'нужен HTTP/HTTPS URL': 'an HTTP/HTTPS URL is required',
  'некорректный URL': 'invalid URL',
  'некорректный URL или порт': 'invalid URL or port',
  'нужен HTTP/HTTPS URL без логина и пароля': 'an HTTP/HTTPS URL without login and password is required',
  'fragment в URL источника запрещен': 'fragments are not allowed in source URLs',
  'некорректный порт': 'invalid port',
  'некорректный hostname': 'invalid hostname',
  'слишком длинный hostname': 'hostname is too long'
};
const serverPatternsEn = [
  [/^Недопустимое значение: (.+)\.$/, 'Invalid value: $1.'],
  [/^Источник: (.+)\.$/, match => `Source: ${serverText(match[1])}.`],
  [/^Источник (\d+): строк (\d+), заблокировано (\d+), страниц (\d+), ошибка нет$/, 'Source $1: $2 lines, $3 blocked, $4 pages, no errors'],
  [/^Источник (\d+): строк (\d+), заблокировано (\d+), страниц (\d+), ошибка (.+)$/, 'Source $1: $2 lines, $3 blocked, $4 pages, error $5'],
  [/^Проверено (\d+)\/(\d+); ([\d.]+) прокси\/с; осталось ~([\d.]+) мин$/, 'Checked $1/$2; $3 proxies/s; ~$4 min left'],
  [/^Проверено (\d+)\/(\d+); подходят (\d+); сохранено (\d+)$/, 'Checked $1/$2; matching $3; saved $4'],
  [/^Уникальных кандидатов в базе: (\d+)$/, 'Unique candidates in database: $1'],
  [/^Воркеров: (\d+); полный обход; профиль (.+)$/, 'Workers: $1; full sweep; profile $2'],
  [/^Ошибка: (\w+): проверьте файлы и параметры\.$/, 'Error: $1: check files and parameters.'],
  [/^Ошибка экспорта: (\w+): проверьте data\/ и denylist\.$/, 'Export error: $1: check data/ and the denylist.'],
  [/^Удалено: (.+)$/, 'Deleted: $1'],
  [/^Проверка анонимности: judge (.+)$/, 'Anonymity check: judge $1'],
  [/^Сохранено (\d+)\. Следующая перепроверка рабочих прокси через (\S+) мин\.$/, 'Saved $1. Next re-check of working proxies in $2 min.'],
  [/^Не удалось скачать базу стран: (.+)$/, 'Could not download the country database: $1'],
  [/^База стран обновлена: DB-IP (\S+)\. (.+)$/, 'Country database updated: DB-IP $1. $2'],
  [/^Не удалось определить внешний IP через judge URL: judge URL не показал внешний IP этого устройства$/, 'Could not detect the external IP via the judge URL: the judge did not show this device’s public IP'],
  [/^Не удалось определить внешний IP через judge URL: (.+)$/, 'Could not detect the external IP via the judge URL: $1']
];

function serverText(text) {
  if (lang !== 'en' || typeof text !== 'string') return text;
  const trimmed = text.trim();
  if (Object.hasOwn(serverMessagesEn, trimmed)) return serverMessagesEn[trimmed];
  for (const [pattern, replacement] of serverPatternsEn) {
    const match = trimmed.match(pattern);
    if (match) return typeof replacement === 'function' ? replacement(match) : trimmed.replace(pattern, replacement);
  }
  return text;
}

const serverLog = text => lang === 'en' ? String(text).split('\n').map(serverText).join('\n') : text;

function applyI18n(root=document) {
  root.querySelectorAll('[data-i18n]').forEach(node => { node.textContent = t(node.dataset.i18n); });
  // Only static dictionary markup is inserted here; user data never reaches this path.
  root.querySelectorAll('[data-i18n-html]').forEach(node => { node.innerHTML = t(node.dataset.i18nHtml); });
  for (const attribute of ['placeholder', 'title', 'aria-label']) {
    root.querySelectorAll(`[data-i18n-${attribute}]`).forEach(node => node.setAttribute(attribute, t(node.getAttribute(`data-i18n-${attribute}`))));
  }
}

const numeric = ['attempts', 'timeout', 'connect_timeout', 'workers', 'rate', 'max_bytes', 'source_timeout', 'top', 'min_success', 'max_latency', 'want', 'reputation-timeout'];
const profileLabel = profile => messages.en['profile.' + profile] ? t('profile.' + profile) : profile;
const fmt = n => Number(n || 0).toLocaleString(lang === 'ru' ? 'ru-RU' : 'en-US');
const ms = value => t('unit.ms', {value});
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
  if (!response.ok) throw new Error(value.error ? serverText(value.error) : t('error.app'));
  return value;
}

function showTab(name) {
  currentTab = name;
  document.querySelectorAll('.page').forEach(node => node.classList.toggle('active', node.id === 'page-' + name));
  document.querySelectorAll('.nav').forEach(node => node.classList.toggle('active', node.dataset.tab === name));
  $('page-label').textContent = t('nav.' + name);
  if (name === 'results') loadResults();
}

document.querySelectorAll('[data-tab]').forEach(node => node.onclick = () => showTab(node.dataset.tab));
document.querySelectorAll('[data-go]').forEach(node => node.onclick = () => showTab(node.dataset.go));

function addTarget(target={}) {
  if ($('targets').children.length >= 20) {
    toast(t('error.maxTargets'), true);
    return;
  }
  const node = document.createElement('div');
  node.className = 'target';
  const text = key => `data-i18n="${key}">${esc(t(key))}`;
  const attr = (name, key) => `${name}="${esc(t(key))}" data-i18n-${name}="${key}"`;
  node.innerHTML = `<div class="target-head"><span>◎</span><input data-field="name" ${attr('aria-label', 'target.name')} ${attr('placeholder', 'target.name')} value="${esc(target.name || t('target.defaultName'))}"><button data-remove ${attr('title', 'target.remove')} ${attr('aria-label', 'target.remove')}>×</button></div><label class="target-url"><span ${text('target.url')}</span><input data-field="url" type="url" placeholder="https://example.org/health" value="${esc(target.url || '')}"></label><div class="field-grid"><label><span ${text('target.statuses')}</span><input data-field="statuses" ${attr('placeholder', 'target.statusesPlaceholder')} value="${esc(target.statuses ? (target.statuses.length === 100 && target.statuses[0] === 200 ? '200-299' : target.statuses.join(', ')) : '200-299')}"></label><label><span ${text('target.contains')}</span><input data-field="contains" ${attr('placeholder', 'target.containsPlaceholder')} value="${esc(target.contains || '')}"></label></div><details class="advanced"><summary ${text('target.advanced')}</summary><label><span ${text('target.method')}</span><select data-field="method"><option>GET</option><option>HEAD</option></select></label><label><span ${text('target.headers')}</span><textarea data-field="headers" rows="2" spellcheck="false">${esc(JSON.stringify(target.headers || {}))}</textarea><small ${text('target.headersHint')}</small></label><label><span ${text('target.sha256')}</span><input data-field="sha256" value="${esc(target.sha256 || '')}" ${attr('placeholder', 'target.sha256Placeholder')}></label></details>`;
  node.querySelector('[data-field="method"]').value = target.method || 'GET';
  node.querySelector('[data-remove]').onclick = () => {
    if ($('targets').children.length === 1) {
      toast(t('error.needTarget'), true);
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
    if (!match) throw new Error(t('error.statusFormat'));
    const first = Number(match[1]);
    const last = Number(match[2] || match[1]);
    if (first < 100 || last > 599 || last < first) throw new Error(t('error.statusRange'));
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
  copy.anonymity = {judge_url: $('judge-url').value.trim()};
  copy.min_anonymity = $('min_anonymity').value;
  copy.protocol = $('protocol').value;
  copy.countries = $('countries').value;
  copy.fail_fast = $('fail_fast').checked;
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
      throw new Error(t('error.headersJson'));
    }
    if (!value('url')) throw new Error(t('error.urlRequired'));
    return {name:value('name'), url:value('url'), method:value('method'), statuses:statuses(value('statuses')), contains:value('contains') || null, sha256:value('sha256') || null, headers};
  });
  return copy;
}

function updateIdentity() {
  const profile = $('request-profile').value;
  const known = Boolean(messages.en['profile.' + profile]);
  $('profile-summary').textContent = known ? t('profile.' + profile) : t('profile.fallback');
  $('profile-description').textContent = known ? t('profileDesc.' + profile, {version:PRODUCT_VERSION}) : t('profileDesc.fallback');
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
  $('judge-url').value = (value.anonymity && value.anonymity.judge_url) || '';
  $('min_anonymity').value = value.min_anonymity || 'any';
  $('protocol').value = value.protocol || 'all';
  $('countries').value = value.countries || '';
  $('fail_fast').checked = value.fail_fast !== false;
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
  $('result-anon').value = $('min_anonymity').value;
  $('result-protocol').value = $('protocol').value;
  $('result-max-latency').value = $('max_latency').value;
  $('result-country').value = $('countries').value;
  $('result-top').value = $('top').value;
}

function updateSourceCount() {
  $('source-count').textContent = $('use_sources').checked ? fmt(new Set($('sources').value.split('\n').map(value => value.trim()).filter(Boolean)).size) : t('sources.off');
}

async function save() {
  try {
    settings = await api('/api/settings', getSettings());
    toast(t('toast.saved'));
    updateIdentity();
    updateSourceCount();
    syncResultControls();
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
      value.min_anonymity = $('result-anon').value;
      value.protocol = $('result-protocol').value;
      value.max_latency = Number($('result-max-latency').value) || 0;
      value.countries = $('result-country').value;
      $('countries').value = value.countries;
      $('min_anonymity').value = value.min_anonymity;
      $('protocol').value = value.protocol;
      $('max_latency').value = value.max_latency;
      $('sort').value = value.sort;
      $('min_success').value = value.min_success;
      $('top').value = value.top;
    } else {
      syncResultControls();
    }
    setBusy(true);
    await api('/api/start', {action, settings:value});
    settings = value;
    toast(action === 'export' ? t('toast.exporting') : t('toast.started'));
    if (action === 'collect') showTab('sources');
    await poll();
  } catch (error) {
    toast(error.message, true);
    setBusy(Boolean(state.running));
  }
}

function setBusy(active) {
  ['start', 'resume', 'recheck', 'recheck-passing', 'collect', 'export'].forEach(id => { $(id).disabled = active; });
  $('stop').disabled = !active;
}

const phases = ['starting', 'collecting', 'scanning', 'exporting', 'complete', 'stopped', 'interrupted', 'error'];
const actions = ['run', 'scan', 'recheck', 'recheck_passing', 'collect', 'export'];

function duration(seconds) {
  if (seconds == null) return '—';
  if (seconds < 60) return t('duration.lessThanMinute');
  if (seconds < 3600) return t('duration.minutes', {count:Math.ceil(seconds / 60)});
  return t('duration.hours', {count:(seconds / 3600).toLocaleString(lang === 'ru' ? 'ru-RU' : 'en-US', {minimumFractionDigits:1, maximumFractionDigits:1})});
}

function reputationStatus(row) {
  return (row && row.reputation && row.reputation.status) || 'clean';
}

function reputationLabel(status) {
  return messages.en['reputation.' + status] ? t('reputation.' + status) : status;
}

function anonymityBadge(row) {
  const level = row && row.anonymity && row.anonymity.level;
  if (!level) return `<span class="anonymity anonymity-off">${esc(t('anon.off'))}</span>`;
  const label = messages.en['anon.' + level] ? t('anon.' + level) : level;
  return `<span class="anonymity anonymity-${esc(level)}">${esc(label)}</span>`;
}

function anonymityDetails(row) {
  const judged = row.anonymity;
  if (!judged) return '';
  const label = messages.en['anon.' + judged.level] ? t('anon.' + judged.level) : judged.level;
  const signals = (judged.signals || []).map(signal => signal === 'real_ip' ? t('anon.realIp') : signal).join(', ');
  return ` · <strong>${esc(t('anon.details'))}</strong> ${esc(label)}${signals ? ' (' + esc(t('anon.signals', {signals})) + ')' : ''}${judged.error ? ' · ' + esc(judged.error) : ''}`;
}

function reputationBadge(row) {
  const status = reputationStatus(row);
  return `<span class="cleanliness cleanliness-${esc(status)}">${esc(reputationLabel(status))}</span>`;
}

function anonymityCounts(report) {
  const info = report.anonymity;
  if (!info || !info.enabled) return '';
  const counts = info.counts || {};
  return t('anon.counts', {elite:fmt(counts.elite || 0), anonymous:fmt(counts.anonymous || 0), transparent:fmt(counts.transparent || 0)});
}

function renderState(value) {
  state = value;
  const progress = value.progress || {};
  const job = value.job || {};
  setBusy(value.running);
  $('phase').textContent = job.stopping && value.running ? t('phase.stopping') : phases.includes(progress.phase) ? t('phase.' + progress.phase) : t('phase.ready');
  if (value.running && progress.phase === 'exporting') $('stop').disabled = true;
  $('checked').textContent = fmt(progress.checked);
  $('candidates').textContent = fmt(progress.candidates);
  const percent = progress.candidates ? Math.min(100, 100 * (progress.checked || 0) / progress.candidates) : 0;
  $('percent').textContent = percent.toFixed(1) + '%';
  $('progress-bar').style.width = percent + '%';
  $('speed').textContent = value.running && progress.phase === 'scanning' ? String(progress.speed ?? '—') : '—';
  $('eta').textContent = value.running && progress.phase === 'scanning' ? duration(progress.eta_seconds) : '—';
  $('progress-text').textContent = progress.phase === 'collecting' ? t('progress.sources', {done:progress.sources_done || 0, total:progress.sources_total || 0}) : progress.candidates ? t('progress.remaining', {count:fmt(Math.max(0, progress.candidates - (progress.checked || 0)))}) : t('progress.allQueued');
  $('job-detail').textContent = job.id ? `${t('job.summary', {action:t(actions.includes(job.action) ? 'action.' + job.action : 'action.fallback'), count:job.targets?.length || 0})}${job.request_profile ? t('job.profile', {profile:profileLabel(job.request_profile)}) : ''}${progress.phase === 'error' ? t('job.seeLog') : ''}` : t('job.idle');
  $('log').textContent = value.log ? serverLog(value.log) : t('log.empty');
  const exportReport = value.export || {};
  $('nav-count').textContent = fmt(progress.passed ?? exportReport.passed ?? 0);
  $('live-passed').textContent = fmt(progress.passed ?? exportReport.passed ?? 0);
  if (exportReport.profile) {
    const counts = exportReport.reputation?.counts || {};
    $('export-note').textContent = t('results.exportReady', {exported:fmt(exportReport.exported), passed:fmt(exportReport.passed), checked:fmt(exportReport.checked), candidates:fmt(exportReport.candidates), clean:fmt(counts.clean || 0), listed:fmt((counts.listed || 0) + (counts.local_denied || 0)), unknown:fmt(counts.unknown || 0), local:(exportReport.local_filtered ? t('results.localFiltered', {count:fmt(exportReport.local_filtered)}) : '') + anonymityCounts(exportReport)});
  }
  $('api-line').classList.toggle('hidden', !value.api);
  $('api-example').textContent = value.api ? `${value.api}/random?protocol=socks5&format=txt` : '';
  document.querySelectorAll('[data-download]').forEach(node => { node.disabled = !(value.downloads || []).includes(node.dataset.download) || (value.running && progress.phase === 'exporting'); });
  const report = progress.sources ? progress : value.sources || {};
  renderSources(report, value.source_urls || [], value.source_keys || [], (value.export || {}).source_quality || {});
  const finished = job.id && !value.running ? job.id : null;
  if (finished && finished !== lastFinished) {
    lastFinished = finished;
    if (job.action !== 'collect') loadResults();
  }
}

function renderSources(report, urls, keys=[], quality={}) {
  const sourceRows = report.sources || [];
  $('sources-status').textContent = report.denylist_error ? t('report.denylistError') : (sourceRows.length ? t('report.loaded', {done:sourceRows.filter(row => row.complete).length, total:sourceRows.length}) : t('report.none'));
  $('source-rows').innerHTML = sourceRows.length ? sourceRows.map(row => {
    const label = row.source ? urls[row.source - 1] || t('report.source', {number:row.source}) : t('report.ownList');
    const stats = quality[row.source ? keys[row.source - 1] : 'local'];
    const working = stats ? `${fmt(stats.passed)} / ${fmt(stats.checked)}` : '—';
    return `<tr><td title="${esc(label)}" style="max-width:440px;overflow:hidden;text-overflow:ellipsis">${esc(label)}</td><td>${fmt(row.rows)}</td><td>${fmt(row.invalid)}</td><td>${fmt(row.blocked || 0)}</td><td class="${row.complete ? '' : 'status-error'}">${esc(row.complete ? (row.rows === 0 ? t('report.emptyList') : row.rows === row.invalid ? t('report.noValid') : t('report.done')) : row.error || t('report.incomplete'))}</td><td>${esc(working)}</td></tr>`;
  }).join('') : `<tr><td colspan="6" class="empty">${esc(t('report.empty'))}</td></tr>`;
}

async function poll() {
  if (polling) return;
  polling = true;
  try { renderState(await api('/api/state')); }
  catch { $('phase').textContent = t('phase.offline'); }
  finally { polling = false; }
}

function renderResults(data) {
  const page = data ? data.rows : [];
  const start = data ? data.offset : 0;
  const total = data ? data.total : 0;
  $('result-context').textContent = data && data.profile ? t('results.context', {targets:data.targets.map(target => target.name ? `${target.name} (${target.url})` : target.url).join(' + '), profile:profileLabel(data.request_profile || 'workbench')}) : t('results.empty');
  $('result-total').textContent = t('results.total', {count:fmt(total)});
  $('page-number').textContent = `${fmt(Math.floor(start / 50) + 1)} / ${fmt(Math.max(1, Math.ceil(total / 50)))}`;
  $('result-rows').innerHTML = page.length ? page.map((row, index) => `<tr><td>${fmt(start + index + 1)}</td><td>${esc(row.proxy)}</td><td><span class="score">${Number(row.score).toFixed(1)}</span></td><td>${esc(ms(Number(row.latency_ms).toFixed(0)))}</td><td>${esc(ms(Number(row.jitter_ms).toFixed(0)))}</td><td>${(Number(row.min_target_reliability) * 100).toFixed(0)}%</td><td>${row.history ? esc(`${fmt(row.history.passes)}/${fmt(row.history.checks)}`) : '1/1'}</td><td>${reputationBadge(row)}</td><td>${anonymityBadge(row)}</td><td class="country">${esc(row.country || '—')}</td><td><button class="text-link" data-details="${index}">${esc(t('results.details'))}</button></td></tr>`).join('') : `<tr><td colspan="11" class="empty">${esc(t(data ? 'results.noneMatching' : 'results.noneYet'))}</td></tr>`;
  $('result-rows').querySelectorAll('[data-details]').forEach(node => node.onclick = () => details(page[Number(node.dataset.details)]));
}

async function loadResults() {
  if (resultBusy) return;
  resultBusy = true;
  $('refresh-results').disabled = true;
  try {
    const query = new URLSearchParams({sort:$('result-sort').value, min_success:$('result-min').value, min_anonymity:$('result-anon').value, protocol:$('result-protocol').value, max_latency:Number($('result-max-latency').value) || 0, country:$('result-country').value.trim(), q:$('result-search').value.trim(), offset});
    const data = await api('/api/results?' + query);
    resultTargets = data.targets;
    resultData = {...data, offset};
    renderResults(resultData);
    $('prev').disabled = offset === 0;
    $('next').disabled = offset + 50 >= data.total;
  } catch (error) {
    toast(error.message, true);
  } finally {
    resultBusy = false;
    $('refresh-results').disabled = false;
  }
}

function renderDetails(row) {
  $('details-title').textContent = row.proxy;
  const verdict = row.reputation || {status:'clean', dnsbl:[]};
  const dnsbl = (verdict.dnsbl || []).map(item => `${esc(item.zone)}: ${esc(t(item.status === 'listed' ? 'dnsbl.listed' : item.status === 'clear' ? 'dnsbl.clear' : 'dnsbl.noAnswer'))}`).join(' · ') || esc(t('dnsbl.notChecked'));
  const head = ['col.attempt', 'col.response', 'col.time', 'col.bytes', 'col.result'].map(key => `<th>${esc(t(key))}</th>`).join('');
  $('details-body').innerHTML = `<div class="detail-reputation"><strong>${esc(t('details.cleanliness'))}</strong> ${esc(reputationLabel(verdict.status))} · <strong>DNSBL:</strong> ${dnsbl}${verdict.local_rule ? esc(t('details.localRule')) + esc(verdict.local_rule) : ''}${anonymityDetails(row)}${row.aborted ? ' · ' + esc(t('details.aborted')) : ''}</div>` + resultTargets.map((target, index) => `<h3>${esc(target.name || t('details.service', {number:index + 1}))} · ${esc(target.url)}</h3><div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${(row.samples || []).filter(sample => sample.target === index).map(sample => `<tr><td>${esc(sample.attempt)}</td><td>${esc(sample.status ?? '—')}</td><td>${esc(ms(sample.ms))}</td><td>${fmt(sample.bytes)}</td><td class="${sample.ok ? '' : 'status-error'}">${esc(sample.ok ? t('details.success') : sample.error)}</td></tr>`).join('')}</tbody></table></div>`).join('');
}

async function details(summary) {
  try {
    const row = await api('/api/result-detail?proxy=' + encodeURIComponent(summary.proxy));
    detailRow = row;
    renderDetails(row);
    $('details-dialog').showModal();
  } catch (error) {
    toast(error.message, true);
  }
}

$('close-details').onclick = () => $('details-dialog').close();
$('add-target').onclick = () => addTarget();
$('save-settings').onclick = save;
$('save-sources').onclick = save;
$('start').onclick = () => start('run');
$('resume').onclick = () => start('scan');
$('recheck').onclick = () => start('recheck');
$('recheck-passing').onclick = () => start('recheck_passing');
$('collect').onclick = () => start('collect');
$('export').onclick = () => start('export');
$('stop').onclick = async () => { try { $('stop').disabled = true; await api('/api/stop', {}); toast(t('toast.stopping')); await poll(); } catch (error) { toast(error.message, true); } };
$('clear-data').onclick = async () => { if (!confirm(t('confirm.clear'))) return; try { const result = await api('/api/clear-data', {}); toast(t('toast.cleared', {count:result.removed.length})); await poll(); } catch (error) { toast(error.message, true); } };
$('refresh-results').onclick = () => { offset = 0; loadResults(); };
['result-sort', 'result-min', 'result-anon', 'result-protocol', 'result-max-latency'].forEach(id => $(id).onchange = () => { offset = 0; loadResults(); });
let searchTimer;
$('result-search').oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { offset = 0; loadResults(); }, 300); };
$('result-country').oninput = $('result-search').oninput;

const presets = {
  quick: {attempts:1, timeout:5, connect_timeout:2, workers:256, fail_fast:true},
  balanced: {attempts:3, timeout:8, connect_timeout:4, workers:128, fail_fast:true},
  thorough: {attempts:5, timeout:12, connect_timeout:6, workers:128, fail_fast:true}
};
document.querySelectorAll('[data-preset]').forEach(node => node.onclick = () => {
  const preset = presets[node.dataset.preset];
  for (const [key, value] of Object.entries(preset)) {
    if (typeof value === 'boolean') $(key).checked = value; else $(key).value = value;
  }
  toast(t('preset.applied'));
});

function renderGeo(status) {
  $('geo-status').textContent = status.available ? t('geo.ready', {count:fmt(status.ranges)}) : t('geo.missing');
}
async function loadGeo() {
  try { renderGeo(await api('/api/geoip')); } catch {}
}
$('geo-update').onclick = async () => {
  $('geo-update').disabled = true;
  toast(t('geo.downloading'));
  try {
    renderGeo(await api('/api/geoip/update', {}));
    toast(t('geo.updated'));
    if (currentTab === 'results') loadResults();
  } catch (error) {
    toast(error.message, true);
  } finally {
    $('geo-update').disabled = false;
  }
};
loadGeo();
$('copy-page').onclick = async () => {
  const proxies = ((resultData && resultData.rows) || []).map(row => row.proxy);
  if (!proxies.length) { toast(t('toast.copyEmpty'), true); return; }
  try {
    await navigator.clipboard.writeText(proxies.join('\n') + '\n');
    toast(t('toast.copied', {count:fmt(proxies.length)}));
  } catch {
    toast(t('toast.copyFailed'), true);
  }
};
$('copy-api').onclick = async () => {
  try {
    await navigator.clipboard.writeText($('api-example').textContent);
    toast(t('toast.apiCopied'));
  } catch {
    toast(t('toast.copyFailed'), true);
  }
};
$('prev').onclick = () => { offset = Math.max(0, offset - 50); loadResults(); };
$('next').onclick = () => { offset += 50; loadResults(); };
$('sources').oninput = updateSourceCount;
$('use_sources').onchange = updateSourceCount;
$('request-profile').onchange = updateIdentity;
$('dnsbl-enabled').onchange = updateIdentity;
$('reset-sources').onclick = async () => { try { const value = await api('/api/defaults'); $('sources').value = value.sources.join('\n'); updateSourceCount(); toast(t('toast.sourcesReset')); } catch (error) { toast(error.message, true); } };
$('import-file').onchange = async event => { const file = event.target.files[0]; if (!file) return; if (file.size > 20_000_000) { toast(t('error.fileTooLarge'), true); return; } $('proxies').value = await file.text(); toast(t('toast.listLoaded')); };
async function downloadFile(name, node) {
  try {
    node.disabled = true;
    const response = await fetch('/api/download/' + name, {headers:{'X-Workbench-Token':token}});
    if (!response.ok) throw new Error(t('error.fileNotReady'));
    if (window.showSaveFilePicker && response.body) {
      const handle = await window.showSaveFilePicker({suggestedName:name});
      const writable = await handle.createWritable();
      await response.body.pipeTo(writable);
      await writable.close();
    } else {
      const url = URL.createObjectURL(await response.blob());
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = name;
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    }
  } catch (error) {
    toast(error.message, true);
  } finally {
    node.disabled = false;
  }
}
document.querySelectorAll('[data-download]').forEach(node => node.onclick = () => downloadFile(node.dataset.download, node));

function renderTheme() {
  const dark = document.documentElement.dataset.theme === 'dark';
  $('theme-toggle').textContent = dark ? t('theme.light') : t('theme.dark');
  $('theme-toggle').setAttribute('aria-label', dark ? t('theme.toLight') : t('theme.toDark'));
}
$('theme-toggle').onclick = () => { const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'; document.documentElement.dataset.theme = theme; try { localStorage.setItem('proxy-workbench-theme', theme); } catch {} renderTheme(); };

function renderLang() {
  document.documentElement.lang = lang;
  applyI18n();
  $('lang-toggle').textContent = t('lang.button');
  $('lang-toggle').setAttribute('aria-label', t('lang.label'));
  $('lang-toggle').title = t('lang.label');
  $('page-label').textContent = t('nav.' + currentTab);
  renderTheme();
  updateIdentity();
  updateSourceCount();
  renderState(state);
  renderResults(resultData);
  if (detailRow && $('details-dialog').open) renderDetails(detailRow);
}
$('lang-toggle').onclick = () => { lang = lang === 'ru' ? 'en' : 'ru'; try { localStorage.setItem(LANG_KEY, lang); } catch {} renderLang(); };
renderLang();

(async () => {
  try {
    fill(await api('/api/settings'));
    await poll();
    setInterval(poll, 2000);
  } catch (error) {
    toast(error.message, true);
  }
})();
