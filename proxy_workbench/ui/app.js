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
let lastExportAt = null;
let lastLiveReload = 0;
let toastTimer;
let polling = false;
let resultBusy = false;

const messages = {
  en: {
    'lang.currentName': 'English',
    'help.btnScan': 'Start proxy search',
    'help.btnGateway': 'Local Gateway',
    'help.btnMobile': 'Mobile & Clients',
    'help.flowTitle': 'How it works: 4 simple steps',
    'help.flowSubtitle': 'Automatic pipeline from discovery to connection',
    'help.flow1': '1. Collection',
    'help.flow1Desc': 'Thousands of free proxies',
    'help.flow2': '2. Your Services',
    'help.flow2Desc': 'YouTube, Telegram, API',
    'help.flow3': '3. Smart Check',
    'help.flow3Desc': 'Speed, ping, DNSBL clean',
    'help.flow4': '4. Ready to Use',
    'help.flow4Desc': '1-click Gateway or export',
    'help.s1.bullet1': 'Preconfigured presets for popular services in 1 click',
    'help.s1.bullet2': 'HTTP 200 response & content verification',
    'help.s1.bullet3': 'Strict matching across all chosen targets',
    'help.linkServices': 'Configure services',
    'help.s2.bullet1': 'Latency measurement in milliseconds (ms)',
    'help.s2.bullet2': 'Multiple attempts to filter out unstable proxies',
    'help.s2.bullet3': 'Real throughput test in Mbit/s',
    'help.linkCheck': 'Adjust parameters',
    'help.s3.bullet1': 'DNSBL real-time spam blacklist checks',
    'help.s3.bullet2': 'Anonymity level rating (Elite / Anonymous)',
    'help.s3.bullet3': 'Local subnet and IP denylist filter',
    'help.linkDenylist': 'Manage denylist',
    'help.s4.bullet1': 'Rotating gateway 127.0.0.1:8899',
    'help.s4.bullet2': 'Instant Telegram & browser connection',
    'help.s4.bullet3': 'Exports to TXT, CSV, JSON, PAC, Clash, Sing-Box',
    'help.linkGateway': 'Open Gateway',
    'help.faqTitle': 'Frequently Asked Questions',
    'help.faqSubtitle': 'Everything you need to know about proxies and privacy',
    'help.faq1Q': 'What is a proxy and why do I need it?',
    'help.faq1A': 'A proxy acts as an intermediary between your device and the internet. Websites see the proxy\'s IP address instead of your real IP, allowing you to bypass regional restrictions, access blocked services, and protect your privacy.',
    'help.faq2Q': 'What is the difference between HTTP and SOCKS5?',
    'help.faq2A': 'HTTP/HTTPS proxies are designed for web browsers and websites. SOCKS5 is universal: it works with any application, handles DNS resolution through the proxy, supports UDP, and is ideal for Telegram, voice calls, and games.',
    'help.faq3Q': 'Why do free proxies stop working after some time?',
    'help.faq3A': 'Public proxies are hosted on servers worldwide and can be overloaded or restarted. Proxy Workbench includes an automatic Keep-Fresh (Watch) mode that continually re-checks working proxies every few minutes in the background, keeping your pool 100% active!',
    'help.faq4Q': 'How does the Local Rotating Gateway (127.0.0.1:8899) work?',
    'help.faq4A': 'Instead of copying dozens of proxy addresses manually into your apps, you configure only one address: 127.0.0.1:8899. The gateway automatically routes each new connection through the best verified proxy, instantly switching if any proxy drops.',
    'help.faq5Q': 'Where is my data stored and is it private?',
    'help.faq5A': '100% of your data, history, and settings are stored locally on your own computer in the data/ folder. The application is completely offline-first, has no tracking, no cloud telemetry, and sends no personal data anywhere.',
    'help.manageSettings': 'Settings Management',
    'help.manageSettingsDesc': 'Save your target URLs and check parameters to a JSON file or restore them anytime.',
    'help.manageData': 'Storage Maintenance',
    'help.manageDataDesc': 'Clear checked candidates and cache while keeping your settings and custom denylist intact.',
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
    'common.cancel': 'Cancel',
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
    'phase.waiting': 'Waiting for the next re-check',
    'progress.nextCheck': 'Next re-check of working proxies at {time}',
    'watch.label': 'Keep fresh: re-check every, min',
    'watch.hint': '0 — off. Otherwise, after the check the app keeps running and re-checks the working proxies on this schedule, so exports, the API and the rotating proxy stay fresh.',
    'phase.complete': 'Complete',
    'phase.partial': 'Partial snapshot',
    'phase.stale': 'Snapshot expired',
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
    'action.test': 'Quick live test',
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
    'results.exportNote': 'Build export applies the table filters. Changing table controls alone does not rebuild files.',
    'results.exportReady': 'Latest export: {exported} of {passed} matching · checked {checked} / {candidates} · clean: {clean} · blacklist: {listed} · unknown: {unknown}{local}.',
    'results.snapshot': 'Snapshot #{generation}.',
    'results.snapshotFresh': 'Snapshot #{generation} · valid until {time}.',
    'results.snapshotStale': 'Snapshot #{generation} expired at {time}. Re-check before using or downloading it.',
    'results.snapshotPartial': 'Partial snapshot: {reason} · checked {checked} of {candidates}.',
    'results.snapshotError': 'Snapshot error: {reason}. The previous published results were kept.',
    'results.reason.complete': 'scope completed',
    'results.reason.want_reached': 'requested count reached',
    'results.reason.recheck_passing': 'matching-proxy refresh',
    'results.reason.stopped': 'stopped before the full scope',
    'results.reason.error': 'operation failed',
    'results.selectionReport': 'Selected export: {exported} of {requested} written · {missing} unavailable, stale or excluded by a filter.',
    'results.diagnostic': 'Last unfinished run: {status} ({reason}) · checked {checked} of {candidates}. The current published export was kept.',
    'results.localFiltered': ' · filtered locally: {count}',
    'results.downloads': 'Download ready files:',
    'results.groupData': 'Data & Tables',
    'results.groupProtocols': 'By Protocol',
    'results.groupClients': 'Client Configs',
    'results.pageStatus': 'Page {page} of {pages} ({total} proxies)',
    'results.copied': 'Copied',
    'results.copyProxy': 'Copy proxy address',
    'gateway.title': 'Rotating Proxy Gateway',
    'gateway.online': 'Online',
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
    'presets.subtitle': 'YouTube, Telegram, Discord, GitHub, Cloudflare...',
    'group.network': 'Network & Concurrency',
    'group.speed': 'Speed & Early Filtering',
    'group.limits': 'Data & Payload Limits',
    'unit.sec': 'sec',
    'unit.attempts': 'tries',
    'unit.workers': 'threads',
    'unit.conns': 'conns',
    'unit.bytes': 'bytes',
    'unit.pcs': 'pcs',
    'unit.min': 'min',
    'unit.ms_badge': 'ms',
    'geo.countries': 'Countries',
    'geo.countriesHint': 'ISO codes. Other countries are skipped before checking, so the scan is much shorter.',
    'want.label': 'Stop after finding',
    'want.hint': '0 — check everything. Otherwise the scan ends as soon as this many proxies match.',
    'geo.title': 'Country database',
    'geo.missing': 'Not downloaded',
    'geo.ready': 'Ready · {count} ranges',
    'geo.providers': ' · providers: {count}',
    'provider.filter': 'Providers',
    'provider.all': 'All providers',
    'provider.hide': 'Hide hosting / data centres',
    'provider.exclude': 'Skip hosting providers and data centres (needs the provider database)',
    'provider.hosting': 'hosting',
    'col.provider': 'Provider',
    'geo.hint': 'Needed for country filters, the Country and Provider columns and hiding hosting providers. The free DB-IP Country Lite and ASN Lite files are downloaded once into the local data folder; lookups then work offline. Geonode sources already include countries.',
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
    'download.pacHint': 'Browser auto-config: the 10 best proxies in order',
    'download.clashHint': 'Clash / Mihomo config with automatic fastest-proxy selection',
    'breakdown.protocols': 'Protocols',
    'breakdown.countries': 'Countries',
    'breakdown.unknown': 'unknown',
    'results.exitIp': 'Exit IP seen by the judge: {ip}',
    'sort.bandwidth': 'Bandwidth: most Mbit/s first',
    'results.byBandwidth': 'By bandwidth',
    'col.mbps': 'Mbit/s',
    'col.mbpsHint': 'Download speed from the speed test',
    'speed.url': 'Speed test file (optional)',
    'speed.hint': 'Every proxy that works for your services downloads this file once, and the table shows its real download speed in Mbit/s. Leave empty to skip; it adds a few seconds per working proxy.',
    'check.prefilter': 'Quick pre-check, connections',
    'check.prefilterHint': 'Drops addresses that do not even accept a connection before the full check. 0 — off.',
    'presets.label': 'Or add a ready-made check',
    'presets.quickServices': 'Quick add service:',
    'presets.choose': 'Choose a service…',
    'presets.added': 'Added a check for {name}. A proxy must pass every service in the list.',
    'geo.selectTitle': 'Select or type countries',
    'geo.inputPlaceholder': 'DE, NL, US or select from list…',
    'geo.typeMore': '+ add…',
    'geo.clearAll': 'Clear all',
    'geo.openDropdown': 'Open country list',
    'geo.searchPlaceholder': 'Search country by name or ISO code…',
    'geo.searchEmpty': 'No countries found',
    'geo.selectedCount': '{count} selected',
    'geo.quickRegions': 'Quick regions:',
    'geo.regionTop': '⭐ Top 5',
    'geo.regionEu': '🇪🇺 Europe',
    'geo.regionNa': '🇺🇸 N. America',
    'geo.regionAsia': '🌏 Asia',
    'geo.regionCis': '🌐 CIS',
    'geo.popularGroup': 'Popular for proxies',
    'geo.allGroup': 'All countries (A–Z)',
    'chip.unlimited': 'No limit',
    'chip.all': 'All (0)',
    'chip.off': 'Off (0)',
    'chip.fast': '1 fast',
    'chip.balanced': '3 balanced',
    'chip.thorough': '5 thorough',
    'chip.clear': 'Clear',
    'gateway.telegram': 'Use in Telegram',
    'sort.recommended': 'Recommended: quality, survival, rare lists, trusted sources',
    'results.byRecommended': 'Recommended',
    'settings.export': 'Save settings to a file',
    'settings.import': 'Load settings from a file',
    'settings.importHint': 'Replaces the current settings with a saved file',
    'toast.settingsImported': 'Settings loaded and saved.',
    'toast.settingsBad': 'This file does not contain Proxy Workbench settings.',
    'gateway.label': 'Rotating proxy for browsers and apps:',
    'gateway.hint': 'Set it as an HTTP or SOCKS5 proxy anywhere. Every new connection goes through the next working proxy from the latest export; failed ones are skipped automatically.',
    'gateway.stats': '{proxies} in rotation · {connections} connections',
    'toast.gatewayCopied': 'Proxy address copied.',
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
    'cat.title': 'Source catalog',
    'cat.rules': 'Five statuses stay separate: found in documentation, URL answered, format confirmed, data looks refreshable, proxies checked. The last one is never set here — this application does not connect to a source\'s proxies. A source row never means working, dead or quality: it means what the app observed. Nothing is selected automatically.',
    'cat.update': 'Update catalog',
    'cat.updating': 'Updating the catalog…',
    'cat.updateStage.downloading': 'Downloading the published catalog',
    'cat.updateStage.validating': 'Checking revision and fields',
    'cat.updateDone': 'Catalog revision {revision}: {added} new, {changed} changed, {retired} retired. Nothing was selected.',
    'cat.updateUnselected': 'Not selected: {count}. Enable the ones you need by hand.',
    'cat.updateNotModified': 'The published catalog did not change.',
    'cat.updateFailed': 'The catalog was not updated: {reason}. The local copy is kept.',
    'cat.sets': 'Sets',
    'cat.setsHint': 'A set is a snapshot of source IDs at the moment you apply it. A later catalog update does not add anything to it. Nothing is ever included without your confirmation.',
    'cat.applySet': 'Use this set',
    'cat.setApplied': 'Set {name} applied: {count} sources selected.',
    'cat.setNew': '{count} new in the set — not added',
    'cat.setMembers': '{count} in the catalog',
    'cat.conditions': 'Access conditions',
    'cat.conditionsHint': 'Groups of providers by their own terms, with the date those terms were checked and a link to the primary source. One free tier found in research is not the only one on the market.',
    'cat.search': 'Name, ID, publisher or host',
    'cat.filterState': 'State',
    'cat.filterCategory': 'Category',
    'cat.filterProtocol': 'Protocol',
    'cat.filterFormat': 'Data format',
    'cat.filterAccess': 'Access',
    'cat.filterSet': 'Set',
    'cat.all': 'All',
    'cat.showMore': 'Show more',
    'cat.shown': 'Showing {shown} of {total}',
    'cat.empty': 'Nothing matches these filters.',
    'cat.loading': 'Loading the catalog…',
    'cat.col.source': 'Source',
    'cat.col.format': 'Format',
    'cat.col.access': 'Access',
    'cat.col.state': 'State',
    'cat.col.data': 'Data',
    'cat.col.choice': 'Your choice',
    'cat.state.never_checked': 'Not checked yet',
    'cat.state.has_data': 'Checked, no data kept',
    'cat.state.last_good': 'Data on disk',
    'cat.state.stale': 'Showing older data',
    'cat.state.failed': 'Last attempt failed',
    'cat.state.quarantined': 'Paused after failures',
    'cat.state.not_proxy_source': 'Not a list of proxy addresses',
    'cat.state.needs_access': 'Needs its own account or plan',
    'cat.state.rights_unresolved': 'Data license not established',
    'cat.state.custom': 'Your own list',
    'cat.state.retired': 'No longer in the catalog',
    'cat.retiredNote': 'The accepted catalog no longer lists this source. It stays in your set until you remove it.',
    'cat.choice.selected': 'In your set',
    'cat.choice.unselected': 'Not in the set',
    'cat.choice.disabled': 'Download paused',
    'cat.action.details': 'Details',
    'cat.action.check': 'Check availability and format',
    'cat.action.pause': 'Pause download',
    'cat.action.resume': 'Resume download',
    'cat.action.include': 'Add to the set',
    'cat.action.remove': 'Remove from the set',
    'cat.action.exclude': 'Exclude its addresses from the scope',
    'cat.action.recover': 'Clear the pause',
    'cat.age': 'Data age',
    'cat.ageNone': 'no data yet',
    'cat.error': 'Reason',
    'cat.retryAfter': 'Retry after',
    'cat.quarantineUntil': 'Paused until',
    'cat.recognized': 'recognized',
    'cat.accepted': 'accepted',
    'cat.rejected': 'rejected',
    'cat.newUnique': 'not seen in other sources in this snapshot',
    'cat.passedProfile': 'passed your profile',
    'cat.checkedByApp': 'checked by the app',
    'cat.noLiveness': 'Proxies of this source were not connected to or checked by this application.',
    'cat.detailTitle': 'Source details',
    'cat.evidence': 'Research evidence',
    'cat.rights': 'Terms and data license',
    'cat.termsLink': 'Primary source of the terms',
    'cat.checkedOn': 'Terms checked',
    'cat.history': 'Last observations',
    'cat.cache': 'Stored data',
    'cat.cacheAge': 'Last complete data set',
    'cat.cacheRecords': 'records',
    'cat.time': 'Time',
    'cat.pages': 'pages',
    'cat.noHistory': 'No observations yet.',
    'cat.reasons': 'Rejected because',
    'cat.previewTitle': 'Preview: {name}',
    'cat.previewNote': 'Availability and format only. These addresses were not checked as proxies and are not added to the database.',
    'cat.previewFailed': 'The check did not finish: {reason}.',
    'cat.previewTruncated': 'The check stopped at its own bound ({bytes} bytes, {records} records); the rest of the list was not read.',
    'cat.addTitle': 'Add your own list address',
    'cat.addUrl': 'Address of the list',
    'cat.addKind': 'Data format',
    'cat.addPrivate': 'Allow local addresses (only for your own test services)',
    'cat.addPreview': 'Preview',
    'cat.addSubmit': 'Add to my set',
    'cat.addDone': 'Added {id}. Preview it before the next collection.',
    'cat.addExists': 'This address with this format is already in your set.',
    'cat.excludeTitle': 'Exclude addresses already received from {id}?',
    'cat.excludeBody': 'Only addresses that no other source offers in this snapshot are excluded by default. The addresses stay in the database and the history is kept — this only removes them from the current scope. You can undo it by clearing the scope exclusions.',
    'cat.excludeShared': 'Also exclude addresses other sources also provide',
    'cat.excludeDone': '{count} of {total} addresses of this source are excluded from the current scope.',
    'cat.scopeClear': 'Clear the scope exclusions',
    'cat.scopeCleared': 'Scope exclusions cleared: {count}.',
    'cat.group.public_free': 'Public free',
    'cat.group.permanent_free_quota': 'Permanent free plan',
    'cat.group.free_with_key': 'Free with an API key',
    'cat.group.trial': 'Trial',
    'cat.group.paid': 'Paid',
    'cat.group.own_infrastructure': 'Your own server',
    'cat.group.snapshot_unavailable': 'Snapshot unavailable',
    'cat.group.unknown': 'Conditions not established',
    'toast.cat.set': 'Set applied: {count} sources.',
    'toast.cat.paused': 'Download paused for {count} sources. They stay in the set.',
    'toast.cat.resumed': 'Download resumed for {count} sources.',
    'toast.cat.removed': 'Removed from the set: {count}. Cached data and history are kept.',
    'toast.cat.added': 'Your list was added and selected.',
    'toast.cat.recovered': 'The pause was cleared. Stored data was not touched.',
    'toast.cat.saved': 'The source selection was saved.',
    'sources.eyebrow': 'TRANSPARENT COLLECTION',
    'sources.title': 'Sources and your own lists',
    'sources.lead': 'Every unique address from the connected lists goes into the database. No first-N-lines limit.',
    'sources.public': 'Public lists',
    'sources.use': 'Load proxies from sources',
    'sources.list': 'Sources: URL or protocol + URL, one per line',
    'sources.timeout': 'Source timeout, seconds',
    'sources.reset': 'Built-in sources',
    'sources.prune': 'Remove dead sources',
    'sources.pruneHint': 'Removes lists that gave at least 20 addresses in the last export and none of them worked.',
    'sources.update': 'Get new sources',
    'sources.updateHint': 'Adds lists published on GitHub after this version.',
    'toast.pruned': 'Removed {count} dead sources.',
    'toast.prunedNone': 'No dead sources: every checked list gave working proxies, or there is no export yet.',
    'toast.sourcesAdded': 'Added {count} new sources.',
    'toast.sourcesCurrent': 'Your source list is up to date.',
    'sources.hint': 'A plain URL is an HTTP list. For SOCKS4 / SOCKS5: socks4 URL / socks5 URL. Unknown protocol: auto URL. Any web page or CSV: text URL. For a paginated JSON API: geonode URL. Errors and incomplete downloads are shown below. Size limits and safe address checks are on by default; local mock sources are available only through a CLI flag.',
    'sources.off': 'Disabled',
    'own.title': 'Add your own list',
    'own.hint': 'Paste addresses or choose a TXT file. Duplicates are merged automatically.',
    'own.file': '↑ Choose TXT file',
    'own.list': 'One proxy per line',
    'own.detect': 'Try HTTP, SOCKS4 and SOCKS5 for addresses without a protocol',
    'own.note': 'HTTP / CONNECT, SOCKS4 and SOCKS5. Public IPs only, without login or password. The database accumulates: disabling sources does not remove previously collected addresses.',
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
    'help.eyebrow': 'QUICK START · USER GUIDE',
    'help.title': 'From a list to working proxies',
    'help.lead': 'Proxy Workbench finds thousands of public proxies, checks them against your targets, and provides a single rotating gateway for all your apps.',
    'help.s1.title': 'Choose services',
    'help.s1.p1': 'Enter the URL of every service you need. A proxy is selected only if it works with all of them. Code 200 is a normal successful response; for APIs that use other codes, specify your own.',
    'help.s1.p2': 'To guard against placeholder pages, specify text that must appear in the response. Redirects are not followed: use the final URL.',
    'help.s2.title': 'Smart testing & speed',
    'help.s2.p1': 'Checks stability and latency with repeated requests. Filters out proxies that drop connections or respond too slowly.',
    'help.s2.p2': 'You can also specify a speed test URL to measure real download throughput in Mbit/s for smooth video streaming.',
    'help.s3.title': 'Cleanliness & privacy',
    'help.s3.p1': 'The local denylist matches IPs and subnets. Real-time DNSBL checks verify that proxies are not listed on public spam blacklists.',
    'help.s3.p2': 'Optional anonymity judge detects whether proxies hide your real IP (Transparent, Anonymous or Elite).',
    'help.s4.title': '1-Click connection & export',
    'help.s4.p1': 'Sort by speed (lowest latency first) or by quality (balanced stability and speed). Export to TXT, CSV, JSON, PAC or Clash.',
    'help.s4.p2': 'Or enable the Local Rotating Gateway (127.0.0.1:8899): configure it once in Telegram or your browser, and it rotates working proxies automatically!',
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
    'sources.lines': 'lines',
    'sources.editorTitle': 'Editor',
    'sources.fileDropHint': 'or drag and drop file here (.txt, .list)',
    'report.statusDone': 'Done',
    'report.statusError': 'Error',
    'report.statusBlocked': 'Blocked',
    'report.statusEmpty': 'Empty',
    'report.statusNoValid': 'No valid addresses',
    'details.attemptNum': 'Attempt #{number}',
    'details.targetService': 'Service {number}',
    'details.time': 'Time',
    'details.bytes': 'Bytes',
    'details.status': 'Status',
    'details.passedRatio': '{passed} / {total} passed',
    'confirm.clear': 'Delete the local database, profiles and exports? Settings and the denylist will be kept.',
    'confirm.denylist': 'Add {count} selected proxies to the local denylist? Future collections, checks and exports will exclude them.',
    'nav.gateway': 'Rotating Gateway',
    'nav.mobile': 'Mobile & Clients',
    'scenario.title': 'One-Click Quick Scenarios',
    'scenario.subtitle': 'Preconfigured smart templates for popular workflows',
    'scenario.telegram': 'Telegram & Calls',
    'scenario.telegramDesc': 'SOCKS5 proxies with low jitter for unblocked messaging and calls',
    'scenario.youtube': 'YouTube & 4K Video',
    'scenario.youtubeDesc': 'Bandwidth testing in Mbit/s with high throughput for 4K streaming',
    'scenario.anon': 'Elite Privacy',
    'scenario.anonDesc': 'Judge verified anonymity with strict DNSBL blacklist filtering',
    'scenario.scrape': 'Fast Scraping',
    'scenario.scrapeDesc': '256 threads, 2s connect timeout, quick prefiltering for big lists',
    'scenario.custom': 'Custom Pro',
    'scenario.customDesc': 'Manual control over all check, identity and export parameters',
    'scenario.applied': 'Scenario applied. Click Find and check to begin.',
    'monitor.liveStream': 'Live Inspection Stream',
    'monitor.liveStreamIdle': 'Checked proxies appear here in real time with ping and status.',
    'monitor.gaugeTitle': 'OVERALL PROGRESS',
    'monitor.proxiesFound': 'Matching proxies',
    'filter.all': 'All Alive',
    'filter.fast': '⚡ Fast (<300ms)',
    'filter.socks5': '🔒 SOCKS5',
    'filter.http': '🌐 HTTP/S',
    'filter.elite': '🛡️ Elite',
    'filter.clean': '🧹 Clean IP',
    'filter.withSpeed': '🚀 With Speed',
    'results.test': 'Test',
    'results.testing': 'Testing…',
    'results.testOk': 'Alive ({ms} ms)',
    'results.testFail': 'Failed: {error}',
    'results.select': 'Select',
    'results.selectAll': 'Select all proxies on this page',
    'results.selectProxy': 'Select proxy {proxy}',
    'results.selectedRegion': 'Actions for selected proxies',
    'results.selectedCount': 'Selected: {count}',
    'results.copySelected': 'Copy Selected',
    'results.exportSelected': 'Download Selected',
    'results.banSelected': 'Add to Denylist',
    'results.clearSelection': 'Clear',
    'results.copyIpPort': 'IP:Port',
    'results.copyUrl': 'Protocol URL',
    'results.copyCurl': 'cURL command',
    'results.copyPython': 'Python snippet',
    'results.copyJson': 'JSON object',
    'results.geoBarTitle': 'Country Distribution',
    'gateway.heading': 'Local Rotating Proxy Gateway',
    'gateway.lead': 'Single local endpoint 127.0.0.1:8899 that rotates every new connection through your pool of verified proxies.',
    'gateway.protocols': 'HTTP & SOCKS5 Simultaneous',
    'gateway.tabTelegram': 'Telegram',
    'gateway.tabCurl': 'cURL',
    'gateway.tabPython': 'Python',
    'gateway.tabBrowser': 'Browser & Apps',
    'gateway.openTelegram': 'Open in Telegram Desktop',
    'gateway.qrHint': 'Scan with phone camera to connect Telegram mobile:',
    'gateway.copyCode': 'Copy Code',
    'gateway.copy': 'Copy address',
    'mobile.heading': 'Mobile Profiles & Client Configs',
    'mobile.lead': 'Ready-to-use configs for sing-box, Clash, Telegram, and mobile devices with split routing.',
    'mobile.singboxTitle': 'sing-box (iOS & Android)',
    'mobile.singboxDesc': 'Smart split routing: Russian banks & domestic services direct, Telegram & blocked traffic through fastest proxies.',
    'mobile.clashTitle': 'Clash / Mihomo',
    'mobile.clashDesc': 'Auto-failover URLTest proxy group with fastest latency selection.',
    'mobile.telegramTitle': 'Telegram Mobile',
    'mobile.telegramDesc': 'Scan the QR code with iOS or Android to immediately add working SOCKS5 proxy.',
    'mobile.copyConfig': 'Copy Config',
    'mobile.downloadConfig': 'Download File',
    'mobile.qrCode': 'QR Code for Phone',
    'mobile.guideTitle': 'How to setup on Mobile',
    'mobile.guideIos': '1. Install sing-box or Shadowrocket from App Store. 2. Import config or scan QR code. 3. Enable TUN VPN mode.',
    'mobile.guideAndroid': '1. Install sing-box or Hiddify from Google Play. 2. Add profile via QR or file. 3. Connect.',
    'toast.banned': 'Added {count} proxies to local denylist.',
    'toast.tested': 'Proxy test completed.',
    'region.top': '⭐ Top 5',
    'region.eu': '🇪🇺 Europe',
    'region.na': '🇺🇸 N. America',
    'region.asia': '🌏 Asia',
    'header.localBadge': 'LOCAL APP',
    'log.terminalTitle': 'Terminal Output — Log',
    'help.pipelineBadge': 'WORKFLOW',
    'region.cis': '🌐 CIS',
  },
  ru: {
    'lang.currentName': 'Русский язык',
    'help.btnScan': 'Начать поиск прокси',
    'help.btnGateway': 'Локальный шлюз',
    'help.btnMobile': 'Для телефона и Telegram',
    'help.flowTitle': 'Схема работы: 4 простых шага',
    'help.flowSubtitle': 'Автоматический путь от поиска до подключения',
    'help.flow1': '1. Сбор источников',
    'help.flow1Desc': 'Тысячи бесплатных прокси',
    'help.flow2': '2. Ваши сервисы',
    'help.flow2Desc': 'YouTube, Telegram, API',
    'help.flow3': '3. Умная проверка',
    'help.flow3Desc': 'Скорость, пинг, DNSBL',
    'help.flow4': '4. Готово к работе',
    'help.flow4Desc': 'Шлюз в 1 клик или экспорт',
    'help.s1.bullet1': 'Готовые пресеты популярных сервисов в 1 клик',
    'help.s1.bullet2': 'Проверка кода 200 OK и ключевого текста',
    'help.s1.bullet3': 'Строгий отбор: прокси должен открывать все выбранные сайты',
    'help.linkServices': 'Настроить сервисы',
    'help.s2.bullet1': 'Замер задержки (Latency) в миллисекундах',
    'help.s2.bullet2': 'Несколько попыток для отсева рвущих соединение',
    'help.s2.bullet3': 'Замер реальной скорости загрузки в Мбит/с',
    'help.linkCheck': 'Настроить параметры',
    'help.s3.bullet1': 'Проверка по публичным спам-базам DNSBL',
    'help.s3.bullet2': 'Определение анонимности (Elite / Anonymous)',
    'help.s3.bullet3': 'Локальный черный список нежелательных сетей',
    'help.linkDenylist': 'Черный список',
    'help.s4.bullet1': 'Ротирующий шлюз 127.0.0.1:8899',
    'help.s4.bullet2': 'Быстрое подключение Telegram и браузера',
    'help.s4.bullet3': 'Экспорт в TXT, CSV, JSON, PAC, Clash, Sing-Box',
    'help.linkGateway': 'Открыть шлюз',
    'help.faqTitle': 'Частые вопросы и ответы',
    'help.faqSubtitle': 'Всё, что нужно знать о прокси и безопасности',
    'help.faq1Q': 'Что такое прокси и для чего они нужны?',
    'help.faq1A': 'Прокси выступает промежуточным узлом между вашим устройством и интернетом. Сайты видят адрес прокси вместо вашего реального IP, что позволяет открывать заблокированные ресурсы, обходить ограничения провайдеров и сохранять приватность.',
    'help.faq2Q': 'В чём разница между HTTP, HTTPS и SOCKS5?',
    'help.faq2A': 'HTTP/HTTPS прокси предназначены для веб-страниц и браузеров. SOCKS5 — универсальный протокол: он работает с любыми программами, пропускает любой сетевой трафик, поддерживает голосовые звонки и идеально подходит для Telegram.',
    'help.faq3Q': 'Почему бесплатные прокси со временем перестают работать?',
    'help.faq3A': 'Публичные прокси работают на серверах по всему миру и могут перегружаться или отключаться. В Proxy Workbench есть функция «Авто-перепроверка» (Watch mode): она в фоне регулярно проверяет рабочие прокси каждые N минут, поэтому в вашем списке всегда только живые адреса!',
    'help.faq4Q': 'Как работает локальный шлюз (127.0.0.1:8899)?',
    'help.faq4A': 'Вам больше не нужно вручную копировать и менять IP в приложениях! Вы указываете один адрес 127.0.0.1:8899 в настройках Telegram или браузера. Шлюз сам автоматически направляет трафик через самый быстрый и стабильный прокси из вашего пула.',
    'help.faq5Q': 'Где хранятся мои данные и это безопасно?',
    'help.faq5A': 'Все 100% данных, история и настройки хранятся исключительно локально на вашем компьютере в папке data/. Приложение работает автономно, не содержит трекеров, не отправляет никакой телеметрии и полностью приватно.',
    'help.manageSettings': 'Настройки приложения',
    'help.manageSettingsDesc': 'Сохраняйте настроенные сервисы и параметры проверки в JSON-файл для переноса или бэкапа.',
    'help.manageData': 'Очистка локальной базы',
    'help.manageDataDesc': 'Удаление кэша и базы найденных прокси с сохранением ваших настроек и персонального черного списка.',
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
    'common.cancel': 'Отмена',
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
    'phase.waiting': 'Ждём следующую перепроверку',
    'progress.nextCheck': 'Следующая перепроверка рабочих прокси в {time}',
    'watch.label': 'Держать свежим: перепроверять каждые, мин',
    'watch.hint': '0 — выключено. Иначе после проверки приложение продолжает работать и перепроверяет рабочие прокси по этому расписанию, чтобы экспорт, API и ротирующий прокси оставались свежими.',
    'phase.complete': 'Завершено',
    'phase.partial': 'Частичный снимок',
    'phase.stale': 'Снимок устарел',
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
    'action.test': 'Быстрая живая проверка',
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
    'results.exportNote': 'Кнопка экспорта применяет фильтры таблицы. Одни изменения фильтров не пересобирают файлы.',
    'results.exportReady': 'Последний экспорт: {exported} из {passed} подходящих · проверено {checked} / {candidates} · чистых: {clean} · blacklist: {listed} · неизвестных: {unknown}{local}.',
    'results.snapshot': 'Снимок #{generation}.',
    'results.snapshotFresh': 'Снимок #{generation} · действует до {time}.',
    'results.snapshotStale': 'Снимок #{generation} истёк в {time}. Перепроверьте его перед использованием и загрузкой.',
    'results.snapshotPartial': 'Частичный снимок: {reason} · проверено {checked} из {candidates}.',
    'results.snapshotError': 'Ошибка снимка: {reason}. Предыдущий опубликованный результат сохранён.',
    'results.reason.complete': 'область проверки завершена',
    'results.reason.want_reached': 'достигнуто заданное количество',
    'results.reason.recheck_passing': 'обновление подходящих прокси',
    'results.reason.stopped': 'остановлено до завершения всей области',
    'results.reason.error': 'операция завершилась ошибкой',
    'results.selectionReport': 'Выбранных экспортировано: {exported} из {requested} · недоступно, устарело или отсечено фильтром: {missing}.',
    'results.diagnostic': 'Последний незавершённый проход: {status} ({reason}) · проверено {checked} из {candidates}. Текущий опубликованный экспорт сохранён.',
    'results.localFiltered': ' · локально отсечено: {count}',
    'results.downloads': 'Центр загрузки файлов:',
    'results.groupData': 'Данные и таблицы',
    'results.groupProtocols': 'По протоколам',
    'results.groupClients': 'Конфигурации клиентов',
    'results.pageStatus': 'Страница {page} из {pages} ({total} прокси)',
    'results.copied': 'Скопировано',
    'results.copyProxy': 'Скопировать адрес прокси',
    'gateway.title': 'Шлюз с ротацией прокси',
    'gateway.online': 'В сети',
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
    'presets.subtitle': 'YouTube, Telegram, Discord, GitHub, Cloudflare...',
    'group.network': 'Сеть и параллелизм',
    'group.speed': 'Скорость и ранний отсев',
    'group.limits': 'Лимиты данных',
    'unit.sec': 'сек',
    'unit.attempts': 'шт.',
    'unit.workers': 'потоков',
    'unit.conns': 'соед.',
    'unit.bytes': 'байт',
    'unit.pcs': 'шт.',
    'unit.min': 'мин',
    'unit.ms_badge': 'мс',
    'geo.countries': 'Страны',
    'geo.countriesHint': 'ISO-коды. Адреса из других стран пропускаются ещё до проверки, поэтому проход намного короче.',
    'want.label': 'Остановиться после',
    'want.hint': '0 — проверить всё. Иначе проверка завершится, как только найдётся столько подходящих прокси.',
    'geo.title': 'База стран',
    'geo.missing': 'Не скачана',
    'geo.ready': 'Готова · диапазонов: {count}',
    'geo.providers': ' · провайдеров: {count}',
    'provider.filter': 'Провайдеры',
    'provider.all': 'Все провайдеры',
    'provider.hide': 'Скрыть хостинг / дата-центры',
    'provider.exclude': 'Пропускать хостинг-провайдеров и дата-центры (нужна база провайдеров)',
    'provider.hosting': 'хостинг',
    'col.provider': 'Провайдер',
    'geo.hint': 'Нужна для фильтра по странам, колонок «Страна» и «Провайдер» и скрытия хостинг-провайдеров. Бесплатные файлы DB-IP Country Lite и ASN Lite скачиваются один раз в локальную папку data, дальше поиск работает офлайн. Источники Geonode уже содержат страну.',
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
    'download.pacHint': 'Автонастройка браузера: 10 лучших прокси по порядку',
    'download.clashHint': 'Конфиг Clash / Mihomo с автоматическим выбором самого быстрого прокси',
    'breakdown.protocols': 'Протоколы',
    'breakdown.countries': 'Страны',
    'breakdown.unknown': 'неизвестно',
    'results.exitIp': 'Выходной IP, который увидел judge: {ip}',
    'sort.bandwidth': 'Пропускная способность: больше Мбит/с первыми',
    'results.byBandwidth': 'По скорости загрузки',
    'col.mbps': 'Мбит/с',
    'col.mbpsHint': 'Скорость загрузки по замеру',
    'speed.url': 'Файл для замера скорости (необязательно)',
    'speed.hint': 'Каждый прокси, прошедший ваши сервисы, один раз скачивает этот файл, и в таблице видна реальная скорость загрузки в Мбит/с. Оставьте пустым, чтобы не замерять; замер добавляет несколько секунд на каждый рабочий прокси.',
    'check.prefilter': 'Быстрая предпроверка, соединений',
    'check.prefilterHint': 'Отсеивает адреса, которые даже не принимают подключение, до полной проверки. 0 — выключено.',
    'presets.label': 'Или добавьте готовую проверку',
    'presets.quickServices': 'Быстро добавить сервис:',
    'presets.choose': 'Выберите сервис…',
    'presets.added': 'Добавлена проверка {name}. Прокси должен пройти все сервисы из списка.',
    'geo.selectTitle': 'Выберите или введите страны',
    'geo.inputPlaceholder': 'DE, NL, US или выберите из списка…',
    'geo.typeMore': '+ добавить…',
    'geo.clearAll': 'Очистить все',
    'geo.openDropdown': 'Открыть список стран',
    'geo.searchPlaceholder': 'Поиск страны по названию или коду…',
    'geo.searchEmpty': 'Страны не найдены',
    'geo.selectedCount': 'Выбрано: {count}',
    'geo.quickRegions': 'Быстрый выбор регионов:',
    'geo.regionTop': '⭐ Топ-5',
    'geo.regionEu': '🇪🇺 Европа',
    'geo.regionNa': '🇺🇸 Сев. Америка',
    'geo.regionAsia': '🌏 Азия',
    'geo.regionCis': '🌐 СНГ',
    'geo.popularGroup': 'Популярные для прокси',
    'geo.allGroup': 'Все страны (А–Я)',
    'chip.unlimited': 'Без лимита',
    'chip.all': 'Все (0)',
    'chip.off': 'Выкл (0)',
    'chip.fast': '1 быстро',
    'chip.balanced': '3 баланс',
    'chip.thorough': '5 тщательно',
    'chip.clear': 'Очистить',
    'gateway.telegram': 'Открыть в Telegram',
    'sort.recommended': 'Рекомендуемые: качество, живучесть, редкие списки, надёжные источники',
    'results.byRecommended': 'Рекомендуемые',
    'settings.export': 'Сохранить настройки в файл',
    'settings.import': 'Загрузить настройки из файла',
    'settings.importHint': 'Заменяет текущие настройки сохранёнными в файле',
    'toast.settingsImported': 'Настройки загружены и сохранены.',
    'toast.settingsBad': 'В этом файле нет настроек Proxy Workbench.',
    'gateway.label': 'Ротирующий прокси для браузера и программ:',
    'gateway.hint': 'Укажите его как HTTP- или SOCKS5-прокси где угодно. Каждое новое соединение идёт через следующий рабочий прокси из последнего экспорта; неработающие пропускаются автоматически.',
    'gateway.stats': 'в ротации {proxies} · соединений {connections}',
    'toast.gatewayCopied': 'Адрес прокси скопирован.',
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
    'cat.title': 'Каталог источников',
    'cat.rules': 'Пять статусов остаются раздельными: найден по документации, URL ответил, формат подтверждён, данные выглядят обновляемыми, прокси проверены. Последний здесь не выставляется — приложение не подключается к прокси источников. Строка каталога не значит ни «рабочий», ни «мёртвый», ни «качественный»: она значит то, что приложение наблюдало. Ничего не включается автоматически.',
    'cat.update': 'Обновить каталог',
    'cat.updating': 'Каталог обновляется…',
    'cat.updateStage.downloading': 'Скачивание опубликованного каталога',
    'cat.updateStage.validating': 'Проверка ревизии и полей',
    'cat.updateDone': 'Ревизия каталога {revision}: новых {added}, изменённых {changed}, ушедших {retired}. Ничего не выбрано.',
    'cat.updateUnselected': 'Не выбрано: {count}. Включите нужные вручную.',
    'cat.updateNotModified': 'Опубликованный каталог не изменился.',
    'cat.updateFailed': 'Каталог не обновлён: {reason}. Локальная копия сохранена.',
    'cat.sets': 'Наборы',
    'cat.setsHint': 'Набор — это снимок ID источников на момент применения. Позднейшее обновление каталога ничего в него не добавляет. Ничего не включается без подтверждения.',
    'cat.applySet': 'Взять этот набор',
    'cat.setApplied': 'Набор «{name}» применён: выбрано источников — {count}.',
    'cat.setNew': 'в наборе появилось новых: {count} — не добавлены',
    'cat.setMembers': 'в каталоге записей: {count}',
    'cat.conditions': 'Условия доступа',
    'cat.conditionsHint': 'Провайдеры сгруппированы по их собственным условиям, с датой проверки этих условий и ссылкой на первоисточник. Один найденный free tier не значит, что других нет на рынке.',
    'cat.search': 'Название, ID, издатель или хост',
    'cat.filterState': 'Состояние',
    'cat.filterCategory': 'Категория',
    'cat.filterProtocol': 'Протокол',
    'cat.filterFormat': 'Формат данных',
    'cat.filterAccess': 'Условия доступа',
    'cat.filterSet': 'Набор',
    'cat.all': 'Все',
    'cat.showMore': 'Показать ещё',
    'cat.shown': 'Показано {shown} из {total}',
    'cat.empty': 'По этим фильтрам ничего нет.',
    'cat.loading': 'Каталог загружается…',
    'cat.col.source': 'Источник',
    'cat.col.format': 'Формат',
    'cat.col.access': 'Условия',
    'cat.col.state': 'Состояние',
    'cat.col.data': 'Данные',
    'cat.col.choice': 'Ваш выбор',
    'cat.state.never_checked': 'Ещё не проверялся',
    'cat.state.has_data': 'Проверен, данных не сохранено',
    'cat.state.last_good': 'Данные на диске',
    'cat.state.stale': 'Показываются более старые данные',
    'cat.state.failed': 'Последняя попытка не удалась',
    'cat.state.quarantined': 'Пауза после сбоев',
    'cat.state.not_proxy_source': 'Не список прокси-адресов',
    'cat.state.needs_access': 'Нужен свой аккаунт или тариф',
    'cat.state.rights_unresolved': 'Лицензия данных не установлена',
    'cat.state.custom': 'Ваш собственный список',
    'cat.state.retired': 'Больше нет в каталоге',
    'cat.retiredNote': 'Принятый каталог больше не содержит этот источник. Он остаётся в наборе, пока вы не уберёте его.',
    'cat.choice.selected': 'Входит в набор',
    'cat.choice.unselected': 'Не входит в набор',
    'cat.choice.disabled': 'Загрузка на паузе',
    'cat.action.details': 'Подробности',
    'cat.action.check': 'Проверить доступность и формат',
    'cat.action.pause': 'Поставить загрузку на паузу',
    'cat.action.resume': 'Возобновить загрузку',
    'cat.action.include': 'Добавить в набор',
    'cat.action.remove': 'Убрать из набора',
    'cat.action.exclude': 'Исключить его адреса из scope',
    'cat.action.recover': 'Снять паузу',
    'cat.age': 'Возраст данных',
    'cat.ageNone': 'данных пока нет',
    'cat.error': 'Причина',
    'cat.retryAfter': 'Повтор после',
    'cat.quarantineUntil': 'Пауза до',
    'cat.recognized': 'распознано',
    'cat.accepted': 'принято',
    'cat.rejected': 'отклонено',
    'cat.newUnique': 'не встречается у других источников в этом срезе',
    'cat.passedProfile': 'прошли ваш профиль',
    'cat.checkedByApp': 'проверено приложением',
    'cat.noLiveness': 'К прокси этого источника приложение не подключалось и не проверяло их.',
    'cat.detailTitle': 'Подробности источника',
    'cat.evidence': 'Доказательства исследования',
    'cat.rights': 'Условия и лицензия данных',
    'cat.termsLink': 'Первоисточник условий',
    'cat.checkedOn': 'Условия проверены',
    'cat.history': 'Последние наблюдения',
    'cat.cache': 'Сохранённые данные',
    'cat.cacheAge': 'Последний полный набор',
    'cat.cacheRecords': 'записей',
    'cat.time': 'Время',
    'cat.pages': 'страниц',
    'cat.noHistory': 'Наблюдений ещё не было.',
    'cat.reasons': 'Отклонено потому что',
    'cat.previewTitle': 'Предпросмотр: {name}',
    'cat.previewNote': 'Только доступность и формат. Эти адреса не проверялись как прокси и не попадают в базу.',
    'cat.previewFailed': 'Проверка не завершилась: {reason}.',
    'cat.previewTruncated': 'Проверка остановилась на своём пределе ({bytes} байт, {records} записей); остальной список не прочитан.',
    'cat.addTitle': 'Добавить адрес своего списка',
    'cat.addUrl': 'Адрес списка',
    'cat.addKind': 'Формат данных',
    'cat.addPrivate': 'Разрешить локальные адреса (только для своих тестовых сервисов)',
    'cat.addPreview': 'Предпросмотр',
    'cat.addSubmit': 'Добавить в мой набор',
    'cat.addDone': 'Добавлен {id}. Посмотрите предпросмотр перед следующим сбором.',
    'cat.addExists': 'Такой адрес с таким форматом уже есть в вашем наборе.',
    'cat.excludeTitle': 'Исключить уже полученные адреса источника {id}?',
    'cat.excludeBody': 'По умолчанию исключаются только адреса, которых нет у других источников в этом срезе. Адреса остаются в базе, история сохраняется — они только исключаются из текущего scope. Отменить можно, очистив исключения scope.',
    'cat.excludeShared': 'Исключить также адреса, которые дают и другие источники',
    'cat.excludeDone': 'Из текущего scope исключено {count} из {total} адресов этого источника.',
    'cat.scopeClear': 'Очистить исключения scope',
    'cat.scopeCleared': 'Исключения scope очищены: {count}.',
    'cat.group.public_free': 'Публичные бесплатные',
    'cat.group.permanent_free_quota': 'Постоянные бесплатные тарифы',
    'cat.group.free_with_key': 'Бесплатные с ключом',
    'cat.group.trial': 'Пробный период (trial), не постоянный тариф',
    'cat.group.paid': 'Платные',
    'cat.group.own_infrastructure': 'Собственный сервер',
    'cat.group.snapshot_unavailable': 'Снимок недоступен',
    'cat.group.unknown': 'Условия не установлены',
    'toast.cat.set': 'Набор применён: источников — {count}.',
    'toast.cat.paused': 'Загрузка на паузе для источников: {count}. Они остаются в наборе.',
    'toast.cat.resumed': 'Загрузка возобновлена для источников: {count}.',
    'toast.cat.removed': 'Убрано из набора: {count}. Кэш и история сохранены.',
    'toast.cat.added': 'Ваш список добавлен и выбран.',
    'toast.cat.recovered': 'Пауза снята. Сохранённые данные не тронуты.',
    'toast.cat.saved': 'Выбор источников сохранён.',
    'sources.eyebrow': 'ПРОЗРАЧНЫЙ СБОР',
    'sources.title': 'Источники и свои списки',
    'sources.lead': 'Все уникальные адреса из подключённых списков попадут в базу. Без ограничения первых N строк.',
    'sources.public': 'Публичные списки',
    'sources.use': 'Загружать прокси из источников',
    'sources.list': 'Источники: URL или протокол + URL, по одному на строке',
    'sources.timeout': 'Таймаут источника, секунд',
    'sources.reset': 'Встроенные источники',
    'sources.prune': 'Убрать мёртвые источники',
    'sources.pruneHint': 'Удаляет списки, которые в последнем экспорте дали хотя бы 20 адресов и ни одного рабочего.',
    'sources.update': 'Новые источники',
    'sources.updateHint': 'Добавляет списки, опубликованные на GitHub после выхода этой версии.',
    'toast.pruned': 'Удалено мёртвых источников: {count}.',
    'toast.prunedNone': 'Мёртвых источников нет: каждый проверенный список дал рабочие прокси, или экспорта ещё нет.',
    'toast.sourcesAdded': 'Добавлено новых источников: {count}.',
    'toast.sourcesCurrent': 'Список источников актуален.',
    'sources.hint': 'Обычный URL — HTTP-список. Для SOCKS4 / SOCKS5: socks4 URL / socks5 URL. Протокол неизвестен: auto URL. Любая веб-страница или CSV: text URL. Для JSON API с обходом страниц: geonode URL. Ошибки и неполные загрузки показаны ниже. По умолчанию включены лимиты размера и безопасная проверка адресов; локальные mock-источники доступны только через CLI-флаг.',
    'sources.off': 'Выключены',
    'own.title': 'Добавить свой список',
    'own.hint': 'Вставьте адреса или выберите TXT-файл. Дубликаты объединяются автоматически.',
    'own.file': '↑ Выбрать TXT-файл',
    'own.list': 'Один прокси на строку',
    'own.detect': 'Пробовать HTTP, SOCKS4 и SOCKS5 для адресов без протокола',
    'own.note': 'HTTP / CONNECT, SOCKS4 и SOCKS5. Только публичные IP без логина и пароля. База накапливается: отключение источников не удаляет ранее собранные адреса.',
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
    'help.eyebrow': 'БЫСТРЫЙ СТАРТ · РУКОВОДСТВО',
    'help.title': 'От списков до проверенных рабочих прокси',
    'help.lead': 'Proxy Workbench автоматически находит тысячи публичных прокси, проверяет их на реальных сайтах и предоставляет единый адрес для всех ваших программ.',
    'help.s1.title': 'Выберите ваши сервисы',
    'help.s1.p1': 'Укажите сайты или API, для которых вам нужны прокси (например, YouTube или Telegram). Прокси отбирается только в том случае, если гарантированно открывает каждый из них.',
    'help.s1.p2': 'Для защиты от страниц-заглушек провайдера можно указать ключевое слово, которое обязательно должно присутствовать в ответе сервиса.',
    'help.s2.title': 'Умная проверка и замер скорости',
    'help.s2.p1': 'Приложение тестирует отклик, стабильность и скорость прокси несколькими повторными запросами. Это отсеивает нестабильные адреса, которые рвут соединение.',
    'help.s2.p2': 'Также можно включить замер реальной скорости загрузки в Мбит/с, чтобы отобрать прокси, идеально подходящие для потокового 4K видео.',
    'help.s3.title': 'Чистота, безопасность и анонимность',
    'help.s3.p1': 'Проверка по спам-базам DNSBL и локальному черному списку отсекает заблокированные адреса, снижая риск капчи или блокировки аккаунтов.',
    'help.s3.p2': 'Судья анонимности (Judge) определяет, скрывает ли прокси ваш реальный IP адрес: прозрачный, анонимный или элитный (Elite).',
    'help.s4.title': 'Подключение в 1 клик и экспорт',
    'help.s4.p1': 'Сортируйте прокси по скорости (минимальный пинг) или качеству. Скачивайте готовые списки или конфигурации для браузеров и клиентов.',
    'help.s4.p2': 'Или используйте локальный шлюз 127.0.0.1:8899: укажите его один раз в Telegram или браузере, и он будет автоматически переключать прокси без вашего участия!',
    'help.s5.title': 'Что важно понимать',
    'help.s5.p1': 'Это оценка доступности и времени ответа, не пропускной способности в Мбит/с. Публичный прокси может перестать работать после проверки: используйте «Перепроверить» для свежих результатов.',
    'help.s5.p2': 'Новые URL, параметры замеров, request-профиль или политика чистоты создают отдельный профиль. Настройки и история хранятся локально. Закрытие вкладки не останавливает приложение; для выхода закройте его окно терминала через Ctrl+C.',
    'help.clear': 'Удалить локальные результаты',
    'help.clearHint': 'Очищает базу проверенных прокси, но сохраняет все ваши персональные настройки и черный список (denylist).',
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
    'sources.lines': 'строк',
    'sources.editorTitle': 'Редактор',
    'sources.fileDropHint': 'или перетащите файл сюда (.txt, .list)',
    'report.statusDone': 'Выполнено',
    'report.statusError': 'Ошибка',
    'report.statusBlocked': 'Заблокировано',
    'report.statusEmpty': 'Пустой список',
    'report.statusNoValid': 'Нет адресов',
    'details.attemptNum': 'Попытка #{number}',
    'details.targetService': 'Сервис {number}',
    'details.time': 'Время',
    'details.bytes': 'Байты',
    'details.status': 'Статус',
    'details.passedRatio': '{passed} из {total} успешно',
    'confirm.clear': 'Удалить локальную базу, профили и экспорты? Настройки и denylist останутся.',
    'confirm.denylist': 'Добавить выбранные прокси ({count}) в локальный denylist? Их исключат из будущего сбора, проверок и экспорта.',
    'nav.gateway': 'Ротирующий шлюз',
    'nav.mobile': 'Мобильные клиенты',
    'scenario.title': 'Умные экспресс-сценарии',
    'scenario.subtitle': 'Готовые смарт-шаблоны под популярные задачи',
    'scenario.telegram': 'Telegram и звонки',
    'scenario.telegramDesc': 'SOCKS5 прокси с минимальным джиттером для звонков и обхода блокировок',
    'scenario.youtube': 'YouTube и видео',
    'scenario.youtubeDesc': 'Замер реальной скорости в Mbit/s для стабильного 1080p/4K видео',
    'scenario.anon': 'Elite Приватность',
    'scenario.anonDesc': 'Проверка скрытности через Judge и жесткая фильтрация по черным спискам',
    'scenario.scrape': 'Турбо-сбор',
    'scenario.scrapeDesc': '256 потоков, 2с таймаут, мгновенный отсев для десятков тысяч адресов',
    'scenario.custom': 'Экспертный',
    'scenario.customDesc': 'Полный ручной контроль всех сетевых параметров и фильтров',
    'scenario.applied': 'Сценарий применён. Нажмите «Найти и проверить» для запуска.',
    'monitor.liveStream': 'Живая лента проверок',
    'monitor.liveStreamIdle': 'Проверяемые адреса отображаются здесь с пингом и статусом.',
    'monitor.gaugeTitle': 'ОБЩИЙ ПРОГРЕСС',
    'monitor.proxiesFound': 'Подходящих прокси',
    'filter.all': 'Все живые',
    'filter.fast': '⚡ Быстрые (<300мс)',
    'filter.socks5': '🔒 SOCKS5',
    'filter.http': '🌐 HTTP/S',
    'filter.elite': '🛡️ Elite',
    'filter.clean': '🧹 Чистый IP',
    'filter.withSpeed': '🚀 Со скоростью',
    'results.test': 'Тест',
    'results.testing': 'Проверка…',
    'results.testOk': 'Работает ({ms} мс)',
    'results.testFail': 'Ошибка: {error}',
    'results.select': 'Выбрать',
    'results.selectAll': 'Выбрать все прокси на странице',
    'results.selectProxy': 'Выбрать прокси {proxy}',
    'results.selectedRegion': 'Действия с выбранными прокси',
    'results.selectedCount': 'Выбрано: {count}',
    'results.copySelected': 'Скопировать',
    'results.exportSelected': 'Скачать',
    'results.banSelected': 'В бан-лист',
    'results.clearSelection': 'Снять',
    'results.copyIpPort': 'IP:Port',
    'results.copyUrl': 'URL с протоколом',
    'results.copyCurl': 'Команда cURL',
    'results.copyPython': 'Код Python',
    'results.copyJson': 'JSON объект',
    'results.geoBarTitle': 'Распределение по странам',
    'gateway.heading': 'Локальный ротирующий шлюз',
    'gateway.lead': 'Единая локальная точка 127.0.0.1:8899, автоматически распределяющая запросы по пулу живых проверенных прокси.',
    'gateway.protocols': 'HTTP и SOCKS5 одновременно',
    'gateway.tabTelegram': 'Telegram',
    'gateway.tabCurl': 'cURL',
    'gateway.tabPython': 'Python',
    'gateway.tabBrowser': 'Браузеры',
    'gateway.openTelegram': 'Открыть в Telegram Desktop',
    'gateway.qrHint': 'Наведите камеру смартфона для подключения Telegram на телефоне:',
    'gateway.copyCode': 'Скопировать код',
    'gateway.copy': 'Скопировать адрес',
    'mobile.heading': 'Мобильные профили и клиенты',
    'mobile.lead': 'Готовые конфигурации для sing-box, Clash, Telegram и телефонов с умной раздельной маршрутизацией.',
    'mobile.singboxTitle': 'sing-box (iOS и Android)',
    'mobile.singboxDesc': 'Раздельное туннелирование: российские сервисы и банки идут напрямую, Telegram и заблокированные сайты — через прокси.',
    'mobile.clashTitle': 'Clash / Mihomo',
    'mobile.clashDesc': 'Группа прокси с автоматическим переключением на самый быстрый узел (URLTest).',
    'mobile.telegramTitle': 'Telegram на смартфоне',
    'mobile.telegramDesc': 'Отсканируйте QR-код камерой iPhone или Android для мгновенного добавления прокси в Telegram.',
    'mobile.copyConfig': 'Скопировать конфиг',
    'mobile.downloadConfig': 'Скачать файл',
    'mobile.qrCode': 'QR-код для телефона',
    'mobile.guideTitle': 'Инструкция по настройке',
    'mobile.guideIos': '1. Установите sing-box или Shadowrocket из App Store. 2. Импортируйте конфиг или QR-код. 3. Включите режим TUN VPN.',
    'mobile.guideAndroid': '1. Установите sing-box или Hiddify из Google Play. 2. Добавьте профиль через QR или файл. 3. Подключитесь.',
    'toast.banned': 'Добавлено {count} прокси в локальный черный список.',
    'toast.tested': 'Проверка прокси завершена.',
    'region.top': '⭐ Топ-5',
    'region.eu': '🇪🇺 Европа',
    'region.na': '🇺🇸 Сев. Америка',
    'region.asia': '🌏 Азия',
    'header.localBadge': 'ЛОКАЛЬНЫЙ РЕЖИМ',
    'log.terminalTitle': 'Терминал — Лог выполнения',
    'help.pipelineBadge': 'ПРОЦЕСС',
    'region.cis': '🌐 СНГ',
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
  if (!key) return '';
  if ((key === 'unit.ms' || key === 'unit.ms_badge') && (!values || values.value === undefined)) {
    return lang === 'ru' ? 'мс' : 'ms';
  }
  const dict = messages[lang] || messages.en || {};
  const fallback = messages.en || {};
  const text = dict[key] ?? fallback[key] ?? String(key);
  if (typeof text !== 'string') return String(text ?? key ?? '');
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
  'Не удалось прочитать активный профиль. Перезапустите проверку.': 'Could not read the active profile. Run the scan again.',
  'Активный профиль не найден. Сначала запустите проверку.': 'Active profile not found. Run a scan first.',
  'Выберите от 1 до 1000 прокси для экспорта.': 'Select between 1 and 1000 proxies to export.',
  'Выберите от 1 до 1000 прокси для локального denylist.': 'Select between 1 and 1000 proxies for the local denylist.',
  'Выбран список содержит некорректный адрес прокси.': 'The selection contains an invalid proxy address.',
  'Нужен публичный IP-адрес, порт и протокол без логина или пароля.': 'A public IP address, port and protocol without credentials are required.',
  'Выбранные адреса можно экспортировать только действием export.': 'Selected addresses can only be used with the export action.',
  'Поиск экспорта слишком длинный: максимум 100 символов.': 'The export search is too long: 100 characters maximum.',
  'Неверный фильтр провайдера для экспорта.': 'Invalid provider filter for export.',
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
  'speedtest.url: ожидается http(s) URL': 'speedtest.url: an http(s) URL is expected',
  'speedtest.url: нужен http(s) URL без userinfo': 'speedtest.url: an http(s) URL without userinfo is required',
  'speedtest.max_bytes: от 10000 до 200000000': 'speedtest.max_bytes: from 10000 to 200000000',
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
  'слишком длинный hostname': 'hostname is too long',
  'Не удалось получить список источников с GitHub.': 'Could not get the source list from GitHub.'
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
  root.querySelectorAll('[data-i18n]').forEach(node => {
    const key = node.dataset.i18n;
    if (key === 'unit.ms') {
      node.textContent = lang === 'ru' ? 'мс' : 'ms';
    } else {
      node.textContent = t(key);
    }
  });
  // Only static dictionary markup is inserted here; user data never reaches this path.
  root.querySelectorAll('[data-i18n-html]').forEach(node => { node.innerHTML = t(node.dataset.i18nHtml); });
  for (const attribute of ['placeholder', 'title', 'aria-label']) {
    root.querySelectorAll(`[data-i18n-${attribute}]`).forEach(node => node.setAttribute(attribute, t(node.getAttribute(`data-i18n-${attribute}`))));
  }
}

const numeric = ['attempts', 'timeout', 'connect_timeout', 'workers', 'prefilter', 'rate', 'max_bytes', 'source_timeout', 'top', 'min_success', 'max_latency', 'want', 'watch', 'reputation-timeout'];
const profileLabel = profile => messages.en['profile.' + profile] ? t('profile.' + profile) : profile;
const fmt = n => Number(n || 0).toLocaleString(lang === 'ru' ? 'ru-RU' : 'en-US');
const ms = value => t('unit.ms', {value});
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));

const animatedNumbersMap = new WeakMap();

function animateNumber(element, targetValue) {
  if (!element) return;
  const target = Math.max(0, Math.round(Number(targetValue) || 0));

  if (!animatedNumbersMap.has(element)) {
    animatedNumbersMap.set(element, target);
    element.textContent = fmt(target);
    return;
  }

  const current = animatedNumbersMap.get(element);
  if (current === target) return;

  animatedNumbersMap.set(element, target);

  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    element.textContent = fmt(target);
    return;
  }

  const start = current;
  const diff = target - start;
  const duration = Math.min(450, Math.max(160, Math.abs(diff) * 8));
  const startTime = performance.now();

  function step(now) {
    const elapsed = now - startTime;
    const progress = Math.min(1, elapsed / duration);
    const factor = 1 - Math.pow(1 - progress, 3);
    const val = Math.round(start + diff * factor);
    element.textContent = fmt(val);
    if (progress < 1) {
      requestAnimationFrame(step);
    } else {
      element.textContent = fmt(target);
    }
  }

  requestAnimationFrame(step);
}

function formatLogTerminal(text) {
  if (!text) return '';
  const lines = String(text).split('\n');
  return lines.map(line => {
    let safe = esc(line);

    // 1. Success keywords (OK, PASS, SUCCESS, no errors, matching proxies)
    safe = safe.replace(/\b(ошибка\s+нет|no\s+errors|ошибка:\s*нет|error:\s*none)\b/gi, '<span class="log-ok">$1</span>');
    safe = safe.replace(/\b(OK|PASS|SUCCESS)\b|\[(OK|PASS)\]/g, match => `<span class="log-ok">${match}</span>`);
    safe = safe.replace(/\b(подходят\s+\d+|matching\s+\d+|сохранено\s+\d+|saved\s+\d+)\b/gi, match => `<span class="log-ok">${match}</span>`);

    // 2. Error and failure keywords (ERR, ERROR, FAIL, blocked items)
    safe = safe.replace(/\b(ERR|ERROR|FAIL|FAILED)\b|\[(ERR|ERROR|FAIL)\]/g, match => `<span class="log-err">${match}</span>`);
    safe = safe.replace(/\b(ошибка|error):\s*([^\s<]+)/gi, (match, prefix, val) => {
      if (val.toLowerCase() === 'нет' || val.toLowerCase() === 'none') {
        return `<span class="log-ok">${match}</span>`;
      }
      return `<span class="log-err">${match}</span>`;
    });
    safe = safe.replace(/\b(заблокировано\s+[1-9]\d*|blocked\s+[1-9]\d*)\b/gi, match => `<span class="log-err">${match}</span>`);

    // 3. Warning keywords (WARN, WARNING)
    safe = safe.replace(/\b(WARN|WARNING)\b|\[(WARN|WARNING)\]/g, match => `<span class="log-warn">${match}</span>`);

    // 4. Progress accent highlights
    safe = safe.replace(/\b(Проверено|Checked|Источник|Source)\b/g, match => `<span class="log-accent">${match}</span>`);

    return safe;
  }).join('\n');
}

let toastHideTimer;

function toast(message, error=false) {
  if (!message || !String(message).trim()) return;
  clearTimeout(toastTimer);
  clearTimeout(toastHideTimer);
  const node = $('toast');
  if (!node) return;
  node.classList.remove('toast-hiding');
  node.className = error ? 'error' : '';
  node.innerHTML = `<span class="toast-icon">${error ? '✕' : '✓'}</span><span class="toast-msg">${esc(message)}</span>`;
  node.hidden = false;

  // Restart CSS animation
  node.style.animation = 'none';
  void node.offsetWidth;
  node.style.animation = '';

  toastTimer = setTimeout(() => {
    node.classList.add('toast-hiding');
    toastHideTimer = setTimeout(() => {
      node.hidden = true;
      node.classList.remove('toast-hiding');
      node.textContent = '';
    }, 240);
  }, error ? 8000 : 4000);
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
  document.querySelectorAll('.page').forEach(node => {
    node.classList.toggle('active', node.id === 'page-' + name);
  });
  document.querySelectorAll('.nav').forEach(node => {
    node.classList.toggle('active', node.dataset.tab === name);
  });
  const label = $('page-label');
  if (label) label.textContent = t('nav.' + name) || name;
  window.scrollTo({top: 0, behavior: 'smooth'});
  if (name === 'results') loadResults();
  if (name === 'sources' && typeof reloadCatalog === 'function') reloadCatalog();
}


document.querySelectorAll('[data-tab]').forEach(node => node.onclick = () => showTab(node.dataset.tab));
document.querySelectorAll('[data-go]').forEach(node => {
  const activate = () => showTab(node.dataset.go);
  node.onclick = activate;
  if (node.getAttribute('role') === 'button') {
    node.onkeydown = event => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        activate();
      }
    };
  }
});

// Final URLs only: redirects are not followed. Each check proves the service answers through the proxy.
const TARGET_PRESETS = [
  {name:'Google', url:'https://www.google.com/generate_204', statuses:[204]},
  {name:'YouTube', url:'https://www.youtube.com/generate_204', statuses:[204]},
  {name:'Telegram', url:'https://telegram.org/', statuses:[200], contains:'Telegram'},
  {name:'Discord', url:'https://discord.com/api/v10/gateway', statuses:[200], contains:'gateway.discord.gg'},
  {name:'Instagram', url:'https://www.instagram.com/', statuses:[200], contains:'Instagram'},
  {name:'OpenAI API', url:'https://api.openai.com/v1/models', statuses:[401], contains:'invalid_request_error'},
  {name:'GitHub', url:'https://github.com/', statuses:[200], contains:'GitHub'},
  {name:'Wikipedia', url:'https://www.wikipedia.org/', statuses:[200], contains:'Wikipedia'},
  {name:'Cloudflare', url:'https://www.cloudflare.com/cdn-cgi/trace', statuses:[200], contains:'ip='}
];

function fillPresets() {
  const select = $('target-preset');
  if (select) {
    select.querySelectorAll('option[data-service]').forEach(node => node.remove());
    TARGET_PRESETS.forEach((preset, index) => {
      const option = document.createElement('option');
      option.value = String(index);
      option.dataset.service = '';
      option.textContent = preset.name;
      select.appendChild(option);
    });
    select.onchange = () => {
      const preset = TARGET_PRESETS[Number(select.value)];
      select.value = '';
      if (!preset) return;
      addTarget({...preset, headers:{}, method:'GET'});
      toast(t('presets.added', {name:preset.name}));
    };
  }

  const chipsContainer = $('quick-services-chips');
  if (chipsContainer) {
    chipsContainer.replaceChildren();
    TARGET_PRESETS.forEach(preset => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'quick-service-chip';
      btn.innerHTML = `<span class="service-chip-plus">＋</span> ${esc(preset.name)}`;
      btn.onclick = () => {
        addTarget({...preset, headers:{}, method:'GET'});
        toast(t('presets.added', {name:preset.name}));
      };
      chipsContainer.appendChild(btn);
    });
  }
}
fillPresets();

// ============================================================================
// Countries Database & Smart Country Combobox
// ============================================================================

const COUNTRIES_LIST = [
  {code: 'US', nameEn: 'United States', nameRu: 'США', popular: true, region: 'na'},
  {code: 'DE', nameEn: 'Germany', nameRu: 'Германия', popular: true, region: 'eu'},
  {code: 'NL', nameEn: 'Netherlands', nameRu: 'Нидерланды', popular: true, region: 'eu'},
  {code: 'GB', nameEn: 'United Kingdom', nameRu: 'Великобритания', popular: true, region: 'eu'},
  {code: 'FR', nameEn: 'France', nameRu: 'Франция', popular: true, region: 'eu'},
  {code: 'RU', nameEn: 'Russia', nameRu: 'Россия', popular: true, region: 'cis'},
  {code: 'PL', nameEn: 'Poland', nameRu: 'Польша', popular: true, region: 'eu'},
  {code: 'UA', nameEn: 'Ukraine', nameRu: 'Украина', popular: true, region: 'cis'},
  {code: 'KZ', nameEn: 'Kazakhstan', nameRu: 'Казахстан', popular: true, region: 'cis'},
  {code: 'JP', nameEn: 'Japan', nameRu: 'Япония', popular: true, region: 'asia'},
  {code: 'SG', nameEn: 'Singapore', nameRu: 'Сингапур', popular: true, region: 'asia'},
  {code: 'CA', nameEn: 'Canada', nameRu: 'Канада', popular: true, region: 'na'},
  {code: 'CH', nameEn: 'Switzerland', nameRu: 'Швейцария', popular: true, region: 'eu'},
  {code: 'SE', nameEn: 'Sweden', nameRu: 'Швеция', popular: true, region: 'eu'},
  {code: 'FI', nameEn: 'Finland', nameRu: 'Финляндия', popular: true, region: 'eu'},
  {code: 'NO', nameEn: 'Norway', nameRu: 'Норвегия', popular: true, region: 'eu'},
  {code: 'IT', nameEn: 'Italy', nameRu: 'Италия', popular: true, region: 'eu'},
  {code: 'ES', nameEn: 'Spain', nameRu: 'Испания', popular: true, region: 'eu'},
  {code: 'TR', nameEn: 'Turkey', nameRu: 'Турция', popular: true, region: 'asia'},
  {code: 'KR', nameEn: 'South Korea', nameRu: 'Южная Корея', popular: true, region: 'asia'},
  {code: 'HK', nameEn: 'Hong Kong', nameRu: 'Гонконг', popular: true, region: 'asia'},
  {code: 'TW', nameEn: 'Taiwan', nameRu: 'Тайвань', popular: false, region: 'asia'},
  {code: 'IN', nameEn: 'India', nameRu: 'Индия', popular: true, region: 'asia'},
  {code: 'BR', nameEn: 'Brazil', nameRu: 'Бразилия', popular: true, region: 'sa'},
  {code: 'AU', nameEn: 'Australia', nameRu: 'Австралия', popular: false, region: 'other'},
  {code: 'AT', nameEn: 'Austria', nameRu: 'Австрия', popular: false, region: 'eu'},
  {code: 'BE', nameEn: 'Belgium', nameRu: 'Бельгия', popular: false, region: 'eu'},
  {code: 'CZ', nameEn: 'Czech Republic', nameRu: 'Чехия', popular: false, region: 'eu'},
  {code: 'RO', nameEn: 'Romania', nameRu: 'Румыния', popular: false, region: 'eu'},
  {code: 'BG', nameEn: 'Bulgaria', nameRu: 'Болгария', popular: false, region: 'eu'},
  {code: 'DK', nameEn: 'Denmark', nameRu: 'Дания', popular: false, region: 'eu'},
  {code: 'IE', nameEn: 'Ireland', nameRu: 'Ирландия', popular: false, region: 'eu'},
  {code: 'PT', nameEn: 'Portugal', nameRu: 'Португалия', popular: false, region: 'eu'},
  {code: 'GR', nameEn: 'Greece', nameRu: 'Греция', popular: false, region: 'eu'},
  {code: 'HU', nameEn: 'Hungary', nameRu: 'Венгрия', popular: false, region: 'eu'},
  {code: 'SK', nameEn: 'Slovakia', nameRu: 'Словакия', popular: false, region: 'eu'},
  {code: 'EE', nameEn: 'Estonia', nameRu: 'Эстония', popular: false, region: 'eu'},
  {code: 'LV', nameEn: 'Latvia', nameRu: 'Латвия', popular: false, region: 'eu'},
  {code: 'LT', nameEn: 'Lithuania', nameRu: 'Литва', popular: false, region: 'eu'},
  {code: 'CY', nameEn: 'Cyprus', nameRu: 'Кипр', popular: false, region: 'eu'},
  {code: 'IL', nameEn: 'Israel', nameRu: 'Израиль', popular: false, region: 'asia'},
  {code: 'AE', nameEn: 'United Arab Emirates', nameRu: 'ОАЭ', popular: false, region: 'asia'},
  {code: 'TH', nameEn: 'Thailand', nameRu: 'Таиланд', popular: false, region: 'asia'},
  {code: 'VN', nameEn: 'Vietnam', nameRu: 'Вьетнам', popular: false, region: 'asia'},
  {code: 'ID', nameEn: 'Indonesia', nameRu: 'Индонезия', popular: false, region: 'asia'},
  {code: 'MY', nameEn: 'Malaysia', nameRu: 'Малайзия', popular: false, region: 'asia'},
  {code: 'CN', nameEn: 'China', nameRu: 'Китай', popular: false, region: 'asia'},
  {code: 'AR', nameEn: 'Argentina', nameRu: 'Аргентина', popular: false, region: 'sa'},
  {code: 'MX', nameEn: 'Mexico', nameRu: 'Мексика', popular: false, region: 'na'},
  {code: 'CL', nameEn: 'Chile', nameRu: 'Чили', popular: false, region: 'sa'},
  {code: 'CO', nameEn: 'Colombia', nameRu: 'Колумбия', popular: false, region: 'sa'},
  {code: 'ZA', nameEn: 'South Africa', nameRu: 'ЮАР', popular: false, region: 'other'},
  {code: 'EG', nameEn: 'Egypt', nameRu: 'Египет', popular: false, region: 'other'},
  {code: 'BY', nameEn: 'Belarus', nameRu: 'Беларусь', popular: false, region: 'cis'},
  {code: 'GE', nameEn: 'Georgia', nameRu: 'Грузия', popular: false, region: 'cis'},
  {code: 'AM', nameEn: 'Armenia', nameRu: 'Армения', popular: false, region: 'cis'},
  {code: 'AZ', nameEn: 'Azerbaijan', nameRu: 'Азербайджан', popular: false, region: 'cis'},
  {code: 'UZ', nameEn: 'Uzbekistan', nameRu: 'Узбекистан', popular: false, region: 'cis'},
  {code: 'MD', nameEn: 'Moldova', nameRu: 'Молдова', popular: false, region: 'cis'},
  {code: 'RS', nameEn: 'Serbia', nameRu: 'Сербия', popular: false, region: 'eu'},
  {code: 'HR', nameEn: 'Croatia', nameRu: 'Хорватия', popular: false, region: 'eu'},
  {code: 'IS', nameEn: 'Iceland', nameRu: 'Исландия', popular: false, region: 'eu'},
  {code: 'LU', nameEn: 'Luxembourg', nameRu: 'Люксембург', popular: false, region: 'eu'},
  {code: 'NZ', nameEn: 'New Zealand', nameRu: 'Новая Зеландия', popular: false, region: 'other'}
];

const COUNTRIES_BY_CODE = new Map(COUNTRIES_LIST.map(c => [c.code, c]));

const REGION_PRESETS = {
  top: ['US', 'DE', 'NL', 'GB', 'FR'],
  eu: ['DE', 'NL', 'FR', 'GB', 'PL', 'SE', 'CH', 'IT', 'ES', 'FI', 'AT', 'CZ'],
  na: ['US', 'CA'],
  asia: ['JP', 'SG', 'KR', 'HK', 'IN', 'TW'],
  cis: ['RU', 'KZ', 'BY', 'AM', 'GE', 'UZ']
};

function getCountryFlag(code) {
  if (!code || typeof code !== 'string' || code.length !== 2) return '🌐';
  const c = code.toUpperCase();
  if (!/^[A-Z]{2}$/.test(c)) return '🌐';
  return String.fromCodePoint(...[...c].map(ch => 127397 + ch.charCodeAt(0)));
}

function getCountryName(code) {
  const item = COUNTRIES_BY_CODE.get(code.toUpperCase());
  if (item) return lang === 'ru' ? item.nameRu : item.nameEn;
  return code.toUpperCase();
}

class SmartCountryCombobox {
  constructor(rootElement, targetInputId, badgeCountId) {
    this.root = rootElement;
    this.targetInputId = targetInputId;
    this.targetInput = this.root.querySelector('.country-native-input') || $(targetInputId);
    this.badgeCount = badgeCountId ? $(badgeCountId) : null;
    this.selected = new Set();
    this.isOpen = false;
    this.searchQuery = '';
    SmartCountryCombobox.instances.push(this);

    this.bindElements();
    this.attachEvents();
    this.syncFromInput();
  }

  bindElements() {
    this.comboboxBox = this.root.querySelector('.country-input-box');
    this.chipsList = this.root.querySelector('.country-chips-list');
    this.typeaheadInput = this.root.querySelector('.country-typeahead-input');
    this.clearBtn = this.root.querySelector('.country-clear-btn');
    this.toggleBtn = this.root.querySelector('.country-toggle-btn');
    this.dropdown = this.root.querySelector('.country-dropdown');
    this.searchInput = this.root.querySelector('.country-search-input');
    this.searchClearBtn = this.root.querySelector('.country-search-clear');
    this.countLabel = this.root.querySelector('.country-count-label');
    this.actionClearBtn = this.root.querySelector('.country-action-clear-btn');
    this.listScroll = this.root.querySelector('.country-list-scroll');
    this.doneBtn = this.root.querySelector('.country-done-btn');
  }

  attachEvents() {
    if (this.comboboxBox) {
      this.comboboxBox.onclick = (e) => {
        if (e.target.closest('.country-chip-remove') || e.target.closest('.country-clear-btn')) return;
        if (!this.isOpen) {
          this.open();
        } else {
          if (!e.target.closest('.country-typeahead-input')) {
            this.close();
          }
        }
      };
    }

    if (this.toggleBtn) {
      this.toggleBtn.onclick = (e) => {
        e.stopPropagation();
        this.toggle();
      };
    }

    if (this.clearBtn) {
      this.clearBtn.onclick = (e) => {
        e.stopPropagation();
        this.clear();
        if (this.typeaheadInput) this.typeaheadInput.focus();
      };
    }

    if (this.actionClearBtn) {
      this.actionClearBtn.onclick = (e) => {
        e.stopPropagation();
        this.clear();
      };
    }

    if (this.doneBtn) {
      this.doneBtn.onclick = (e) => {
        e.stopPropagation();
        this.close();
      };
    }

    if (this.typeaheadInput) {
      this.typeaheadInput.onfocus = () => {
        if (!this.isOpen) this.open();
      };

      this.typeaheadInput.oninput = () => {
        const val = this.typeaheadInput.value;
        if (val.includes(',') || val.includes(';') || (val.length === 2 && /^[A-Za-z]{2}$/.test(val) && val.includes(' '))) {
          this.addRawString(val);
          this.typeaheadInput.value = '';
          return;
        }
        this.searchQuery = val.trim();
        if (this.searchInput) {
          this.searchInput.value = this.searchQuery;
          if (this.searchClearBtn) this.searchClearBtn.classList.toggle('hidden', !this.searchQuery);
        }
        this.renderList();
        if (!this.isOpen) this.open();
      };

      this.typeaheadInput.onkeydown = (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          const val = this.typeaheadInput.value.trim();
          if (val) {
            this.handleEnterAdd(val);
            this.typeaheadInput.value = '';
            this.searchQuery = '';
            if (this.searchInput) this.searchInput.value = '';
            this.renderList();
          }
        } else if (e.key === 'Backspace' && !this.typeaheadInput.value && this.selected.size > 0) {
          const lastCode = [...this.selected].pop();
          if (lastCode) this.removeCode(lastCode);
        } else if (e.key === 'Escape') {
          this.close();
        } else if (e.key === 'ArrowDown') {
          e.preventDefault();
          if (!this.isOpen) this.open();
          if (this.searchInput) this.searchInput.focus();
        }
      };

      this.typeaheadInput.onpaste = (e) => {
        const text = (e.clipboardData || window.clipboardData).getData('text');
        if (text && (text.includes(',') || text.includes(' ') || text.length === 2)) {
          e.preventDefault();
          this.addRawString(text);
          this.typeaheadInput.value = '';
        }
      };
    }

    if (this.searchInput) {
      this.searchInput.oninput = () => {
        this.searchQuery = this.searchInput.value.trim();
        if (this.searchClearBtn) this.searchClearBtn.classList.toggle('hidden', !this.searchQuery);
        this.renderList();
      };

      this.searchInput.onkeydown = (e) => {
        if (e.key === 'Escape') {
          this.close();
        } else if (e.key === 'Enter') {
          e.preventDefault();
          const q = this.searchQuery.toLowerCase();
          const match = COUNTRIES_LIST.find(c =>
            c.code.toLowerCase() === q ||
            c.nameRu.toLowerCase() === q ||
            c.nameEn.toLowerCase() === q ||
            c.nameRu.toLowerCase().startsWith(q) ||
            c.nameEn.toLowerCase().startsWith(q)
          );
          if (match) {
            this.toggleCode(match.code);
            this.searchInput.value = '';
            this.searchQuery = '';
            if (this.searchClearBtn) this.searchClearBtn.classList.add('hidden');
            this.renderList();
          }
        }
      };
    }

    if (this.searchClearBtn) {
      this.searchClearBtn.onclick = () => {
        if (this.searchInput) {
          this.searchInput.value = '';
          this.searchInput.focus();
        }
        this.searchQuery = '';
        this.searchClearBtn.classList.add('hidden');
        this.renderList();
      };
    }

    this.root.querySelectorAll('.region-chip').forEach(btn => {
      btn.onclick = (e) => {
        e.stopPropagation();
        this.toggleRegion(btn.dataset.reg);
      };
    });

    document.addEventListener('click', (e) => {
      if (!this.root.contains(e.target)) {
        this.close();
      }
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && this.isOpen) {
        this.close();
      }
    });

    if (this.targetInput) {
      this.targetInput.addEventListener('change', () => this.syncFromInput());
      this.targetInput.addEventListener('input', () => this.syncFromInput());
    }
  }

  handleEnterAdd(query) {
    const q = query.toUpperCase();
    if (/^[A-Z]{2}$/.test(q)) {
      this.addCode(q);
      return;
    }
    const match = COUNTRIES_LIST.find(c =>
      c.code === q ||
      c.nameRu.toLowerCase().includes(query.toLowerCase()) ||
      c.nameEn.toLowerCase().includes(query.toLowerCase())
    );
    if (match) {
      this.addCode(match.code);
    }
  }

  addRawString(str) {
    const tokens = str.split(/[,;\s]+/).map(s => s.trim().toUpperCase()).filter(Boolean);
    tokens.forEach(tok => {
      if (/^[A-Z]{2}$/.test(tok)) {
        this.selected.add(tok);
      } else {
        const match = COUNTRIES_LIST.find(c =>
          c.nameRu.toLowerCase() === tok.toLowerCase() ||
          c.nameEn.toLowerCase() === tok.toLowerCase()
        );
        if (match) this.selected.add(match.code);
      }
    });
    this.update();
  }

  toggleRegion(regionKey) {
    const codes = REGION_PRESETS[regionKey];
    if (!codes) return;
    const allSelected = codes.every(c => this.selected.has(c));
    if (allSelected) {
      codes.forEach(c => this.selected.delete(c));
    } else {
      codes.forEach(c => this.selected.add(c));
    }
    this.update();
  }

  addCode(code) {
    const c = code.toUpperCase();
    if (/^[A-Z]{2}$/.test(c)) {
      this.selected.add(c);
      this.update();
    }
  }

  removeCode(code) {
    const c = code.toUpperCase();
    this.selected.delete(c);
    this.update();
  }

  toggleCode(code) {
    const c = code.toUpperCase();
    if (this.selected.has(c)) {
      this.selected.delete(c);
    } else {
      this.selected.add(c);
    }
    this.update();
  }

  clear() {
    this.selected.clear();
    this.searchQuery = '';
    if (this.searchInput) this.searchInput.value = '';
    if (this.searchClearBtn) this.searchClearBtn.classList.add('hidden');
    this.update();
  }

  open() {
    if (this.isOpen) return;
    SmartCountryCombobox.instances.forEach(inst => { if (inst !== this) inst.close(); });
    this.isOpen = true;
    if (this.dropdown) this.dropdown.classList.remove('hidden');
    if (this.comboboxBox) {
      this.comboboxBox.classList.add('focused');
      this.comboboxBox.setAttribute('aria-expanded', 'true');
    }
    this.renderList();
    setTimeout(() => {
      if (this.searchInput && this.isOpen) this.searchInput.focus();
    }, 50);
  }

  close() {
    if (!this.isOpen) return;
    this.isOpen = false;
    if (this.dropdown) this.dropdown.classList.add('hidden');
    if (this.comboboxBox) {
      this.comboboxBox.classList.remove('focused');
      this.comboboxBox.setAttribute('aria-expanded', 'false');
    }
  }

  toggle() {
    if (this.isOpen) this.close();
    else this.open();
  }

  update(triggerEvent = true) {
    const arrayCodes = [...this.selected];
    const valString = arrayCodes.join(', ');

    if (!this.targetInput) this.targetInput = this.root.querySelector('.country-native-input') || $(this.targetInputId);
    if (this.targetInput && this.targetInput.value !== valString) {
      this.targetInput.value = valString;
      if (triggerEvent) {
        this.targetInput.dispatchEvent(new Event('input', {bubbles: true}));
        this.targetInput.dispatchEvent(new Event('change', {bubbles: true}));
      }
    }

    this.renderChips();
    this.renderList();
    this.updateStats();

    SmartCountryCombobox.instances.forEach(inst => {
      if (inst !== this && inst.targetInput && this.targetInput && inst.targetInput.value === this.targetInput.value) {
        inst.syncFromInput(false);
      }
    });
  }

  syncFromInput(render = true) {
    if (!this.targetInput) this.targetInput = this.root.querySelector('.country-native-input') || $(this.targetInputId);
    const val = (this.targetInput ? this.targetInput.value : '') || '';
    const codes = val.split(/[,;\s]+/).map(s => s.trim().toUpperCase()).filter(s => /^[A-Z]{2}$/.test(s));
    this.selected = new Set(codes);
    if (render) {
      this.renderChips();
      this.renderList();
      this.updateStats();
    }
  }

  renderChips() {
    if (!this.chipsList) return;
    this.chipsList.replaceChildren();
    this.selected.forEach(code => {
      const chip = document.createElement('span');
      chip.className = 'country-chip';
      chip.dataset.code = code;
      const flag = getCountryFlag(code);
      const name = getCountryName(code);
      chip.title = `${flag} ${name} (${code})`;
      chip.innerHTML = `
        <span class="chip-flag">${flag}</span>
        <span class="chip-code">${code}</span>
        <button type="button" class="country-chip-remove" aria-label="Remove ${code}">×</button>
      `;
      chip.querySelector('.country-chip-remove').onclick = (e) => {
        e.stopPropagation();
        this.removeCode(code);
      };
      this.chipsList.appendChild(chip);
    });

    const hasItems = this.selected.size > 0;
    if (this.clearBtn) this.clearBtn.classList.toggle('hidden', !hasItems);
    if (this.typeaheadInput) {
      this.typeaheadInput.placeholder = hasItems ? t('geo.typeMore') : t('geo.inputPlaceholder');
    }
  }

  updateStats() {
    const count = this.selected.size;
    const text = t('geo.selectedCount', {count});
    if (this.countLabel) this.countLabel.textContent = text;
    if (this.badgeCount) {
      this.badgeCount.textContent = count > 0 ? `${count}` : '';
      this.badgeCount.classList.toggle('hidden', count === 0);
    }
    this.root.querySelectorAll('.region-chip').forEach(btn => {
      const regCodes = REGION_PRESETS[btn.dataset.reg];
      if (regCodes) {
        const active = regCodes.length > 0 && regCodes.every(c => this.selected.has(c));
        btn.classList.toggle('active', active);
      }
    });
    if (this.targetInput) {
      document.querySelectorAll(`.country-quick-chips[data-for-country="${this.targetInput.id}"] .region-chip-btn`).forEach(btn => {
        const regCodes = REGION_PRESETS[btn.dataset.region];
        if (regCodes) {
          const active = regCodes.length > 0 && regCodes.every(c => this.selected.has(c));
          btn.classList.toggle('active', active);
        }
      });
    }
  }

  renderList() {
    if (!this.listScroll) return;
    this.listScroll.setAttribute('role', 'listbox');
    this.listScroll.setAttribute('aria-multiselectable', 'true');
    this.listScroll.setAttribute('aria-label', t('geo.selectTitle'));
    this.listScroll.replaceChildren();

    const q = this.searchQuery.toLowerCase();
    let filtered = COUNTRIES_LIST;
    if (q) {
      filtered = COUNTRIES_LIST.filter(c =>
        c.code.toLowerCase().includes(q) ||
        c.nameRu.toLowerCase().includes(q) ||
        c.nameEn.toLowerCase().includes(q)
      );
    }

    if (filtered.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'country-list-empty';
      empty.textContent = t('geo.searchEmpty');
      this.listScroll.appendChild(empty);
      return;
    }

    if (!q) {
      const popular = filtered.filter(c => c.popular);
      const all = filtered;

      const popGroupTitle = document.createElement('div');
      popGroupTitle.className = 'country-group-title';
      popGroupTitle.textContent = t('geo.popularGroup');
      this.listScroll.appendChild(popGroupTitle);

      popular.forEach(c => this.listScroll.appendChild(this.createCountryRow(c)));

      const allGroupTitle = document.createElement('div');
      allGroupTitle.className = 'country-group-title';
      allGroupTitle.textContent = t('geo.allGroup');
      this.listScroll.appendChild(allGroupTitle);

      all.forEach(c => this.listScroll.appendChild(this.createCountryRow(c)));
    } else {
      filtered.forEach(c => this.listScroll.appendChild(this.createCountryRow(c)));
    }
  }

  createCountryRow(c) {
    const row = document.createElement('div');
    const isSelected = this.selected.has(c.code);
    row.className = `country-item ${isSelected ? 'selected' : ''}`;
    const flag = getCountryFlag(c.code);
    const name = lang === 'ru' ? c.nameRu : c.nameEn;
    const secondaryName = lang === 'ru' ? c.nameEn : c.nameRu;

    row.innerHTML = `
      <div class="country-checkbox ${isSelected ? 'checked' : ''}">
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
      </div>
      <span class="country-flag">${flag}</span>
      <div class="country-info">
        <span class="country-name">${esc(name)}</span>
        <span class="country-secondary">${esc(secondaryName)}</span>
      </div>
      <span class="country-code-pill">${c.code}</span>
    `;

    row.setAttribute('role', 'option');
    row.setAttribute('aria-selected', String(isSelected));
    row.tabIndex = 0;
    row.onclick = (e) => {
      e.stopPropagation();
      this.toggleCode(c.code);
    };
    row.onkeydown = (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        e.stopPropagation();
        this.toggleCode(c.code);
      }
    };

    return row;
  }
}
SmartCountryCombobox.instances = [];

let countriesComboboxInstance = null;
let resultCountryComboboxInstance = null;

function setupCountryComboboxes() {
  const c1 = $('countries-combobox-wrap') || $('countries-combobox');
  if (c1 && !countriesComboboxInstance) {
    try {
      countriesComboboxInstance = new SmartCountryCombobox(c1, 'countries', 'countries-count-badge');
    } catch (e) {
      console.error('Failed to init countries combobox', e);
    }
  }
  const c2 = $('result-country-combobox-wrap') || $('result-country-combobox');
  if (c2 && !resultCountryComboboxInstance) {
    try {
      resultCountryComboboxInstance = new SmartCountryCombobox(c2, 'result-country', 'result-countries-count-badge');
    } catch (e) {
      console.error('Failed to init result-country combobox', e);
    }
  }

  document.querySelectorAll('.region-chip-btn').forEach(btn => {
    if (!btn) return;
    btn.onclick = (e) => {
      e.preventDefault();
      const parent = btn.closest('[data-for-country]');
      const targetId = parent ? parent.dataset.forCountry : 'countries';
      const inst = targetId === 'result-country' ? resultCountryComboboxInstance : countriesComboboxInstance;
      if (inst) {
        inst.toggleRegion(btn.dataset.region);
      }
    };
  });
}

function setupFieldPresetChips() {
  document.querySelectorAll('.field-preset-chips[data-for]').forEach(container => {
    if (!container) return;
    const targetId = container.dataset.for;
    const input = $(targetId);
    if (!input) return;

    container.querySelectorAll('.preset-chip').forEach(chip => {
      if (!chip) return;
      chip.onclick = (e) => {
        e.preventDefault();
        input.value = chip.dataset.val;
        input.dispatchEvent(new Event('input', {bubbles: true}));
        input.dispatchEvent(new Event('change', {bubbles: true}));
        syncPresetChipsForInput(input, container);
      };
    });

    input.addEventListener('input', () => syncPresetChipsForInput(input, container));
    input.addEventListener('change', () => syncPresetChipsForInput(input, container));
    syncPresetChipsForInput(input, container);
  });

  document.querySelectorAll('.field-preset-chips[data-for-url]').forEach(container => {
    if (!container) return;
    const targetId = container.dataset.forUrl;
    const input = $(targetId);
    if (!input) return;

    container.querySelectorAll('.url-preset-btn').forEach(btn => {
      if (!btn) return;
      btn.onclick = (e) => {
        e.preventDefault();
        input.value = btn.dataset.url;
        input.dispatchEvent(new Event('input', {bubbles: true}));
        input.dispatchEvent(new Event('change', {bubbles: true}));
      };
    });
  });
}

function syncPresetChipsForInput(input, container) {
  if (!input || !container) return;
  const currentVal = String(input.value).trim();
  container.querySelectorAll('.preset-chip').forEach(chip => {
    const chipVal = String(chip.dataset.val).trim();
    chip.classList.toggle('active', chipVal === currentVal);
  });
}

function syncAllPresetChips() {
  document.querySelectorAll('.field-preset-chips[data-for]').forEach(container => {
    const input = $(container.dataset.for);
    if (input) syncPresetChipsForInput(input, container);
  });
}

function addTarget(target={}) {
  if ($('targets').children.length >= 20) {
    toast(t('error.maxTargets'), true);
    return;
  }
  const node = document.createElement('div');
  node.className = 'target';
  const text = key => `data-i18n="${key}">${esc(t(key))}`;
  const attr = (name, key) => `${name}="${esc(t(key))}" data-i18n-${name}="${key}"`;
  const initialMethod = (target.method || 'GET').toUpperCase();

  node.innerHTML = `
    <div class="target-head">
      <div class="target-head-left">
        <span class="target-icon">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="10"/>
            <line x1="2" y1="12" x2="22" y2="12"/>
            <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>
          </svg>
        </span>
        <input data-field="name" class="target-name-input" ${attr('aria-label', 'target.name')} ${attr('placeholder', 'target.name')} value="${esc(target.name || t('target.defaultName'))}">
      </div>
      <div class="target-head-actions">
        <div class="target-method-badge" data-method="${initialMethod}">
          <select data-field="method" class="target-method-select" title="${esc(t('target.method'))}">
            <option value="GET">GET</option>
            <option value="HEAD">HEAD</option>
          </select>
        </div>
        <button type="button" data-remove class="target-remove-btn" ${attr('title', 'target.remove')} ${attr('aria-label', 'target.remove')}>
          <svg class="target-remove-icon icon-cross" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
            <line x1="18" y1="6" x2="6" y2="18"></line>
            <line x1="6" y1="6" x2="18" y2="18"></line>
          </svg>
          <svg class="target-remove-icon icon-trash" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="3 6 5 6 21 6"></polyline>
            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
            <line x1="10" y1="11" x2="10" y2="17"></line>
            <line x1="14" y1="11" x2="14" y2="17"></line>
          </svg>
        </button>
      </div>
    </div>
    <div class="target-body">
      <label class="target-url-label">
        <span ${text('target.url')}</span>
        <div class="url-input-wrap">
          <span class="url-prefix-badge">https://</span>
          <input data-field="url" class="mono-input" type="url" placeholder="https://..." value="${esc(target.url || '')}">
        </div>
      </label>
      <div class="target-compact-grid">
        <label>
          <span ${text('target.statuses')}</span>
          <div class="input-unit-wrap compact">
            <input data-field="statuses" class="mono-input" ${attr('placeholder', 'target.statusesPlaceholder')} value="${esc(target.statuses ? (target.statuses.length === 100 && target.statuses[0] === 200 ? '200-299' : target.statuses.join(', ')) : '200-299')}">
            <span class="unit-badge code-badge">HTTP</span>
          </div>
        </label>
        <label>
          <span ${text('target.contains')}</span>
          <div class="input-unit-wrap compact">
            <input data-field="contains" class="mono-input" ${attr('placeholder', 'target.containsPlaceholder')} value="${esc(target.contains || '')}">
            <span class="unit-badge text-badge">text</span>
          </div>
        </label>
      </div>
      <details class="advanced target-advanced">
        <summary class="target-advanced-summary">
          <span class="target-advanced-summary-text" ${text('target.advanced')}</span>
          <span class="target-accordion-chevron" aria-hidden="true">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
              <path d="m6 9 6 6 6-6"/>
            </svg>
          </span>
        </summary>
        <div class="target-advanced-anim">
          <div class="target-advanced-content">
            <label>
              <span ${text('target.headers')}</span>
              <textarea data-field="headers" rows="2" spellcheck="false" class="mono-textarea">${esc(JSON.stringify(target.headers || {}))}</textarea>
              <small ${text('target.headersHint')}</small>
            </label>
            <label>
              <span ${text('target.sha256')}</span>
              <input data-field="sha256" class="mono-input" value="${esc(target.sha256 || '')}" ${attr('placeholder', 'target.sha256Placeholder')}>
            </label>
          </div>
        </div>
      </details>
    </div>`;

  const methodSelect = node.querySelector('[data-field="method"]');
  const methodBadge = node.querySelector('.target-method-badge');
  methodSelect.value = initialMethod;
  methodBadge.dataset.method = initialMethod;
  methodSelect.onchange = () => {
    methodBadge.dataset.method = methodSelect.value;
  };

  const urlInput = node.querySelector('[data-field="url"]');
  const prefixBadge = node.querySelector('.url-prefix-badge');
  const updatePrefix = () => {
    if (!prefixBadge) return;
    const v = urlInput.value.trim();
    if (/^http:\/\//i.test(v)) {
      prefixBadge.textContent = 'http://';
      prefixBadge.classList.add('http-mode');
    } else {
      prefixBadge.textContent = 'https://';
      prefixBadge.classList.remove('http-mode');
    }
  };
  urlInput.oninput = updatePrefix;
  updatePrefix();

  node.querySelector('[data-remove]').onclick = () => {
    if ($('targets').children.length === 1) {
      toast(t('error.needTarget'), true);
      return;
    }
    node.classList.add('removing');
    setTimeout(() => node.remove(), 220);
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
  copy.detect_protocols = $('detect_protocols').checked;
  copy.exclude_hosting = $('exclude_hosting').checked;
  copy.sources = $('sources').value.split('\n').map(value => value.trim()).filter(Boolean);
  copy.proxies = $('proxies').value;
  copy.denylist = $('denylist').value;
  copy.request_profile = $('request-profile').value;
  copy.anonymity = {judge_url: $('judge-url').value.trim()};
  copy.speedtest = {url: $('speedtest-url').value.trim(), max_bytes: (settings.speedtest && settings.speedtest.max_bytes) || 5000000};
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
    let url = value('url');
    if (!url) throw new Error(t('error.urlRequired'));
    if (!/^https?:\/\//i.test(url)) url = 'https://' + url;
    return {name:value('name'), url, method:value('method'), statuses:statuses(value('statuses')), contains:value('contains') || null, sha256:value('sha256') || null, headers};
  });
  return copy;
}

function updateIdentity() {
  const reqProfile = $('request-profile');
  if (!reqProfile) return;
  const profile = reqProfile.value;
  const known = Boolean(messages.en['profile.' + profile]);
  const profSummary = $('profile-summary');
  if (profSummary) profSummary.textContent = known ? t('profile.' + profile) : t('profile.fallback');
  const profDesc = $('profile-description');
  if (profDesc) profDesc.textContent = known ? t('profileDesc.' + profile, {version:PRODUCT_VERSION}) : t('profileDesc.fallback');
  const dnsblFields = $('dnsbl-fields');
  const dnsblEnabled = $('dnsbl-enabled');
  if (dnsblFields && dnsblEnabled) dnsblFields.classList.toggle('hidden', !dnsblEnabled.checked);
}

function fill(value) {
  settings = value;
  const reputation = {...(value.reputation || {local_enabled:true, dnsbl_enabled:false, dnsbl_zones:[], timeout:2.5, strict:false})};
  for (const key of numeric) {
    const element = $(key);
    if (!element) continue;
    if (element.tagName === 'SELECT' && ![...element.options].some(option => option.value === String(value[key.replace('-', '_')]))) {
      element.add(new Option(`${Math.round(value[key.replace('-', '_')] * 100)}%`, value[key.replace('-', '_')]));
    }
    element.value = key === 'reputation-timeout' ? reputation.timeout : value[key.replace('-', '_')];
  }
  if ($('sort')) $('sort').value = value.sort;
  if ($('use_sources')) $('use_sources').checked = Boolean(value.use_sources);
  if ($('detect_protocols')) $('detect_protocols').checked = Boolean(value.detect_protocols);
  if ($('exclude_hosting')) $('exclude_hosting').checked = Boolean(value.exclude_hosting);
  if ($('sources')) $('sources').value = (value.sources || []).join('\n');
  if ($('proxies')) $('proxies').value = value.proxies || '';
  if ($('denylist')) $('denylist').value = value.denylist || '';
  if ($('request-profile')) $('request-profile').value = value.request_profile || 'workbench';
  if ($('dnsbl-zones')) $('dnsbl-zones').value = (reputation.dnsbl_zones || []).join('\n');
  if ($('dnsbl-enabled')) $('dnsbl-enabled').checked = !!reputation.dnsbl_enabled && ($('dnsbl-zones') ? $('dnsbl-zones').value.trim().length > 0 : false);
  if ($('local-denylist-enabled')) $('local-denylist-enabled').checked = reputation.local_enabled !== false;
  if ($('strict-clean')) $('strict-clean').checked = !!reputation.strict;
  if ($('judge-url')) $('judge-url').value = (value.anonymity && value.anonymity.judge_url) || '';
  if ($('speedtest-url')) $('speedtest-url').value = (value.speedtest && value.speedtest.url) || '';
  if ($('min_anonymity')) $('min_anonymity').value = value.min_anonymity || 'any';
  if ($('protocol')) $('protocol').value = value.protocol || 'all';
  if ($('countries')) $('countries').value = value.countries || '';
  if ($('fail_fast')) $('fail_fast').checked = value.fail_fast !== false;
  if ($('targets')) {
    $('targets').replaceChildren();
    if (Array.isArray(value.targets)) value.targets.forEach(addTarget);
  }
  updateIdentity();
  syncResultControls();
  updateSourceCount();
  updateLineCounts();
  updateSegmentedGlider();
  syncAllPresetChips();
  if (countriesComboboxInstance) countriesComboboxInstance.syncFromInput();
  if (resultCountryComboboxInstance) resultCountryComboboxInstance.syncFromInput();
  if (typeof updateCodeEditors === 'function') updateCodeEditors();
}

function syncResultControls() {
  if ($('result-sort') && $('sort')) $('result-sort').value = $('sort').value;
  const minSuccess = $('min_success');
  const resultMin = $('result-min');
  if (minSuccess && resultMin) {
    const value = minSuccess.value;
    if (![...resultMin.options].some(option => option.value === value)) resultMin.add(new Option(`${Math.round(Number(value) * 100)}%`, value));
    resultMin.value = value;
  }
  if ($('result-anon') && $('min_anonymity')) $('result-anon').value = $('min_anonymity').value;
  if ($('result-protocol') && $('protocol')) $('result-protocol').value = $('protocol').value;
  if ($('result-max-latency') && $('max_latency')) $('result-max-latency').value = $('max_latency').value;
  if ($('result-country') && $('countries')) $('result-country').value = $('countries').value;
  if ($('result-top') && $('top')) $('result-top').value = $('top').value;
  syncAllPresetChips();
  if (countriesComboboxInstance) countriesComboboxInstance.syncFromInput();
  if (resultCountryComboboxInstance) resultCountryComboboxInstance.syncFromInput();
}

function updateSourceCount() {
  const sc = $('source-count');
  const us = $('use_sources');
  const src = $('sources');
  if (!sc || !us || !src) return;
  sc.textContent = us.checked ? fmt(new Set(src.value.split('\n').map(value => value.trim()).filter(Boolean)).size) : t('sources.off');
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

async function start(action, selection=null) {
  try {
    let value = getSettings();
    const request = {action, settings:value};
    if (action === 'export') {
      value.sort = $('result-sort') ? $('result-sort').value : 'recommended';
      value.min_success = Number($('result-min') ? $('result-min').value : 1);
      value.top = Number($('result-top') ? $('result-top').value : 0);
      value.min_anonymity = $('result-anon') ? $('result-anon').value : 'any';
      value.protocol = $('result-protocol') ? $('result-protocol').value : 'all';
      value.max_latency = Number($('result-max-latency') ? $('result-max-latency').value : 0) || 0;
      value.countries = $('result-country') ? $('result-country').value : '';
      request.q = $('result-search') ? $('result-search').value.trim() : '';
      request.hosting = $('result-hosting') ? $('result-hosting').value : 'any';
      if (selection !== null) request.selection = selection;
    } else {
      syncResultControls();
    }
    setBusy(true);
    await api('/api/start', request);
    if (action !== 'export') settings = value;
    toast(action === 'export' ? t('toast.exporting') : t('toast.started'));
    if (action === 'collect') showTab('sources');
    await poll();
  } catch (error) {
    toast(error.message, true);
    setBusy(Boolean(state.running));
  }
}

function setBusy(active) {
  ['start', 'resume', 'recheck', 'recheck-passing', 'collect', 'export'].forEach(id => {
    const el = $(id);
    if (el) el.disabled = active;
  });
  if ($('action-export-selected')) $('action-export-selected').disabled = active;
  if ($('action-ban-selected')) $('action-ban-selected').disabled = active;
  const stopBtn = $('stop');
  if (stopBtn) {
    stopBtn.disabled = !active;
    stopBtn.classList.toggle('active', Boolean(active));
  }
}

const phases = ['starting', 'collecting', 'scanning', 'exporting', 'waiting', 'complete', 'partial', 'stale', 'stopped', 'interrupted', 'error'];
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
  let icon = '🔒';
  if (level === 'transparent') icon = '🔓';
  else if (level === 'unknown') icon = '❓';
  return `<span class="anonymity anonymity-${esc(level)}"><span class="badge-icon">${icon}</span><span>${esc(label)}</span></span>`;
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
  let icon = '🛡️';
  if (status === 'listed' || status === 'local_denied') icon = '🚫';
  else if (status === 'unknown') icon = '⚠️';
  return `<span class="cleanliness cleanliness-${esc(status)}"><span class="badge-icon">${icon}</span><span>${esc(reputationLabel(status))}</span></span>`;
}

function latencyBadge(valMs) {
  if (valMs == null || isNaN(valMs) || valMs === '') return '<span class="cell-dim">—</span>';
  const num = Number(valMs);
  const cls = num < 300 ? 'lat-good' : num < 800 ? 'lat-warn' : 'lat-bad';
  return `<span class="latency-cell ${cls}"><span class="lat-dot"></span><span class="lat-val">${esc(ms(num.toFixed(0)))}</span></span>`;
}

function anonymityCounts(report) {
  const info = report.anonymity;
  if (!info || !info.enabled) return '';
  const counts = info.counts || {};
  return t('anon.counts', {elite:fmt(counts.elite || 0), anonymous:fmt(counts.anonymous || 0), transparent:fmt(counts.transparent || 0)});
}

function makeQR(dataStr) {
  if (!dataStr) return '';
  const bytes = new TextEncoder().encode(dataStr);
  const len = bytes.length;
  const VERSIONS = [
    {v:1, total:26, ec:10, cap:14, align:[]},
    {v:2, total:44, ec:16, cap:26, align:[6,18]},
    {v:3, total:70, ec:26, cap:42, align:[6,22]},
    {v:4, total:100, ec:36, cap:62, align:[6,26]},
    {v:5, total:134, ec:48, cap:84, align:[6,30]},
    {v:6, total:172, ec:64, cap:106, align:[6,34]},
    {v:7, total:196, ec:72, cap:122, align:[6,22,38]},
    {v:8, total:242, ec:88, cap:152, align:[6,24,42]}
  ];
  let ver = VERSIONS.find(v => v.cap >= len);
  if (!ver) ver = VERSIONS[VERSIONS.length - 1];
  const size = ver.v * 4 + 17;

  const bits = [];
  const addBits = (val, count) => {
    for (let i = count - 1; i >= 0; i--) bits.push((val >> i) & 1);
  };
  addBits(4, 4);
  addBits(len, 8);
  for (const b of bytes) addBits(b, 8);
  for (let i = 0; i < 4 && bits.length < ver.cap * 8; i++) bits.push(0);
  while (bits.length % 8 !== 0) bits.push(0);
  let pad = 0xec;
  while (bits.length < ver.cap * 8) {
    addBits(pad, 8);
    pad = pad === 0xec ? 0x11 : 0xec;
  }

  const data = [];
  for (let i = 0; i < bits.length; i += 8) {
    let byte = 0;
    for (let j = 0; j < 8; j++) byte = (byte << 1) | bits[i + j];
    data.push(byte);
  }

  const exp = new Uint8Array(512);
  const log = new Uint8Array(256);
  let x = 1;
  for (let i = 0; i < 255; i++) {
    exp[i] = x;
    exp[i + 255] = x;
    log[x] = i;
    x = (x << 1) ^ (x >= 128 ? 0x11d : 0);
  }
  const gfMul = (a, b) => (a === 0 || b === 0) ? 0 : exp[log[a] + log[b]];

  let gen = [1];
  for (let i = 0; i < ver.ec; i++) {
    const next = new Array(gen.length + 1).fill(0);
    for (let j = 0; j < gen.length; j++) {
      next[j] ^= gfMul(gen[j], exp[i]);
      next[j + 1] ^= gen[j];
    }
    gen = next;
  }

  const rem = new Array(ver.ec).fill(0);
  for (let i = 0; i < data.length; i++) {
    const factor = data[i] ^ rem[0];
    for (let j = 0; j < ver.ec - 1; j++) {
      rem[j] = rem[j + 1] ^ gfMul(gen[j + 1], factor);
    }
    rem[ver.ec - 1] = gfMul(gen[ver.ec], factor);
  }

  const allCodewords = [...data, ...rem];
  const grid = Array.from({length: size}, () => new Int8Array(size).fill(-1));
  const isFunction = Array.from({length: size}, () => new Uint8Array(size));

  const setFinder = (r, c) => {
    for (let dr = -1; dr <= 7; dr++) {
      for (let dc = -1; dc <= 7; dc++) {
        const nr = r + dr, nc = c + dc;
        if (nr < 0 || nr >= size || nc < 0 || nc >= size) continue;
        const inBox = dr >= 0 && dr <= 6 && dc >= 0 && dc <= 6;
        const isBlack = inBox && (dr === 0 || dr === 6 || dc === 0 || dc === 6 || (dr >= 2 && dr <= 4 && dc >= 2 && dc <= 4));
        grid[nr][nc] = isBlack ? 1 : 0;
        isFunction[nr][nc] = 1;
      }
    }
  };
  setFinder(0, 0);
  setFinder(0, size - 7);
  setFinder(size - 7, 0);

  for (let i = 8; i < size - 8; i++) {
    if (!isFunction[6][i]) { grid[6][i] = (i % 2 === 0) ? 1 : 0; isFunction[6][i] = 1; }
    if (!isFunction[i][6]) { grid[i][6] = (i % 2 === 0) ? 1 : 0; isFunction[i][6] = 1; }
  }

  if (ver.align.length) {
    for (const ar of ver.align) {
      for (const ac of ver.align) {
        if (isFunction[ar][ac]) continue;
        for (let dr = -2; dr <= 2; dr++) {
          for (let dc = -2; dc <= 2; dc++) {
            const isB = Math.max(Math.abs(dr), Math.abs(dc)) !== 1;
            grid[ar + dr][ac + dc] = isB ? 1 : 0;
            isFunction[ar + dr][ac + dc] = 1;
          }
        }
      }
    }
  }

  grid[size - 8][8] = 1; isFunction[size - 8][8] = 1;
  for (let i = 0; i < 9; i++) {
    if (i !== 6) { isFunction[8][i] = 1; isFunction[i][8] = 1; }
  }
  for (let i = 0; i < 8; i++) {
    isFunction[8][size - 1 - i] = 1;
    isFunction[size - 1 - i][8] = 1;
  }

  let bitIdx = 0;
  const totalBits = allCodewords.length * 8;
  for (let right = size - 1; right > 0; right -= 2) {
    if (right === 6) right--;
    const upward = ((right + 1) / 2) % 2 === 1;
    for (let vert = 0; vert < size; vert++) {
      const r = upward ? size - 1 - vert : vert;
      for (let c = right; c >= right - 1; c--) {
        if (isFunction[r][c]) continue;
        let bit = 0;
        if (bitIdx < totalBits) {
          bit = (allCodewords[bitIdx >> 3] >> (7 - (bitIdx & 7))) & 1;
          bitIdx++;
        }
        if ((r + c) % 2 === 0) bit ^= 1;
        grid[r][c] = bit;
      }
    }
  }

  const fmtBits = [1,0,1,0,1,0,0,0,0,0,1,0,0,1,0];
  for (let i = 0; i < 6; i++) grid[8][i] = fmtBits[i];
  grid[8][7] = fmtBits[6]; grid[8][8] = fmtBits[7]; grid[7][8] = fmtBits[8];
  for (let i = 0; i < 6; i++) grid[5 - i][8] = fmtBits[9 + i];
  for (let i = 0; i < 8; i++) grid[size - 1 - i][8] = fmtBits[i];
  for (let i = 0; i < 7; i++) grid[8][size - 7 + i] = fmtBits[8 + i];

  const padUnits = 3;
  const fullSize = size + padUnits * 2;
  let paths = '';
  for (let r = 0; r < size; r++) {
    for (let c = 0; c < size; c++) {
      if (grid[r][c] === 1) {
        paths += `M${c + padUnits},${r + padUnits}h1v1h-1z `;
      }
    }
  }
  return `<svg viewBox="0 0 ${fullSize} ${fullSize}" width="160" height="160" xmlns="http://www.w3.org/2000/svg" class="qr-svg"><rect width="${fullSize}" height="${fullSize}" fill="#ffffff" rx="10"/><path d="${paths}" fill="#0f172a"/></svg>`;
}

function snapshotTime(value) {
  let timestamp = Number(value);
  if (!Number.isFinite(timestamp) && typeof value === 'string') timestamp = Date.parse(value) / 1000;
  if (!Number.isFinite(timestamp)) return '—';
  const milliseconds = timestamp > 1e12 ? timestamp : timestamp * 1000;
  return new Date(milliseconds).toLocaleString(lang === 'ru' ? 'ru-RU' : 'en-US');
}

function snapshotView(report) {
  const rawGeneration = String(report.generation || '').replace(/^\.generation-/, '');
  const generation = rawGeneration ? rawGeneration.slice(0, 16) : '—';
  const validUntil = Number(report.valid_until);
  const expiredAt = Number.isFinite(validUntil) && validUntil > 0
    ? (validUntil > 1e12 ? validUntil : validUntil * 1000)
    : null;
  const stale = report.stale === true || Boolean(expiredAt && expiredAt <= Date.now());
  const stateName = ['complete', 'partial', 'error'].includes(report.state) ? report.state
    : report.complete === false ? 'partial' : 'complete';
  const reasonKey = 'results.reason.' + String(report.stop_reason || (stateName === 'partial' ? 'stopped' : stateName));
  const reason = messages.en[reasonKey] ? t(reasonKey) : String(report.stop_reason || '—');
  return {generation, validUntil: expiredAt, stale, state:stateName, reason};
}

function snapshotNotes(report) {
  if (!report || !report.profile) return '';
  const snapshot = snapshotView(report);
  const notes = [];
  if (snapshot.state === 'error') {
    notes.push(t('results.snapshotError', {reason:snapshot.reason}));
  }
  if (snapshot.state === 'partial') {
    notes.push(t('results.snapshotPartial', {reason:snapshot.reason, checked:fmt(report.checked), candidates:fmt(report.scope_candidates ?? report.candidates)}));
  }
  if (snapshot.stale) {
    notes.push(t('results.snapshotStale', {generation:snapshot.generation, time:snapshotTime(snapshot.validUntil)}));
  } else if (snapshot.generation !== '—') {
    notes.push(snapshot.validUntil
      ? t('results.snapshotFresh', {generation:snapshot.generation, time:snapshotTime(snapshot.validUntil)})
      : t('results.snapshot', {generation:snapshot.generation}));
  }
  if (Number(report.selection_requested) > 0) {
    const missing = Array.isArray(report.selection_missing) ? report.selection_missing.length : Number(report.selection_missing) || 0;
    notes.push(t('results.selectionReport', {
      requested:fmt(report.selection_requested), exported:fmt(report.selection_exported), missing:fmt(missing)
    }));
  }
  return notes.join(' ');
}

function diagnosticNote(report, progress) {
  if (!report || !report.profile || !['stopped', 'interrupted', 'error'].includes(progress.phase)) return '';
  const snapshot = snapshotView(report);
  const status = t(snapshot.state === 'error' ? 'phase.error' : 'phase.partial');
  return t('results.diagnostic', {
    status, reason:snapshot.reason, checked:fmt(report.checked), candidates:fmt(report.scope_candidates ?? report.candidates)
  });
}

function renderState(value) {
  state = value;
  const progress = value.progress || {};
  const job = value.job || {};
  const exportReport = value.export || {};
  const snapshot = snapshotView(exportReport);
  setBusy(value.running);
  const snapshotPhase = !value.running && exportReport.profile
    ? (snapshot.state === 'error' ? 'error' : snapshot.state === 'partial' ? 'partial' : snapshot.stale ? 'stale' : '')
    : '';
  const progressPhase = job.stopping && value.running ? 'stopping' : phases.includes(progress.phase) ? progress.phase : 'ready';
  const currentPhase = snapshotPhase || progressPhase;
  const phaseEl = $('phase');
  if (phaseEl) {
    phaseEl.textContent = currentPhase === 'stopping' ? t('phase.stopping') : phases.includes(currentPhase) ? t('phase.' + currentPhase) : t('phase.ready');
    phaseEl.dataset.phase = currentPhase;
  }
  const isScanning = value.running && (progress.phase === 'scanning' || progress.phase === 'starting' || progress.phase === 'collecting');
  const progBar = $('progress-bar');
  if (progBar) progBar.classList.toggle('scanning', isScanning);
  const monitorCard = document.querySelector('.monitor');
  if (monitorCard) {
    monitorCard.dataset.phase = currentPhase;
    monitorCard.classList.toggle('is-scanning', isScanning);
  }
  if (value.running && progress.phase === 'exporting') {
    const stopBtn = $('stop');
    if (stopBtn) {
      stopBtn.disabled = true;
      stopBtn.classList.remove('active');
    }
  }
  if ($('checked')) animateNumber($('checked'), progress.checked);
  if ($('candidates')) $('candidates').textContent = fmt(progress.candidates);
  const percent = progress.candidates ? Math.min(100, 100 * (progress.checked || 0) / progress.candidates) : 0;
  if ($('percent')) $('percent').textContent = percent.toFixed(1) + '%';
  if (progBar) progBar.style.width = percent + '%';

  // Radial speed gauge
  const radialFill = $('radial-progress-fill');
  if (radialFill) {
    const circumference = 314.16;
    const offsetCirc = circumference * (1 - percent / 100);
    radialFill.style.strokeDasharray = `${circumference}`;
    radialFill.style.strokeDashoffset = `${offsetCirc}`;
  }
  const gaugePercent = $('gauge-percent');
  if (gaugePercent) gaugePercent.textContent = percent.toFixed(1) + '%';
  const gaugeCaption = $('gauge-caption');
  if (gaugeCaption) gaugeCaption.textContent = currentPhase === 'ready' ? t('phase.ready') : t('phase.' + currentPhase);

  if ($('speed')) $('speed').textContent = value.running && progress.phase === 'scanning' ? String(progress.speed ?? '—') : '—';
  if ($('eta')) $('eta').textContent = value.running && progress.phase === 'scanning' ? duration(progress.eta_seconds) : '—';
  if ($('progress-text')) $('progress-text').textContent = progress.phase === 'waiting' && progress.next_check_at ? t('progress.nextCheck', {time:new Date(progress.next_check_at * 1000).toLocaleTimeString()}) : progress.phase === 'collecting' ? t('progress.sources', {done:progress.sources_done || 0, total:progress.sources_total || 0}) : progress.candidates ? t('progress.remaining', {count:fmt(Math.max(0, progress.candidates - (progress.checked || 0)))}) : t('progress.allQueued');
  if ($('job-detail')) $('job-detail').textContent = job.id ? `${t('job.summary', {action:t(actions.includes(job.action) ? 'action.' + job.action : 'action.fallback'), count:job.targets?.length || 0})}${job.request_profile ? t('job.profile', {profile:profileLabel(job.request_profile)}) : ''}${progress.phase === 'error' ? t('job.seeLog') : ''}` : t('job.idle');
  const logEl = $('log');
  if (logEl) {
    const wasScrolledToBottom = logEl.scrollHeight - logEl.clientHeight <= logEl.scrollTop + 40;
    const rawLog = value.log ? serverLog(value.log) : t('log.empty');
    logEl.innerHTML = formatLogTerminal(rawLog);
    if (value.running && wasScrolledToBottom) {
      logEl.scrollTop = logEl.scrollHeight;
    }
  }

  // Live ticker updates
  const tickerContainer = $('live-ticker-list');
  if (tickerContainer) {
    if (value.running && value.log) {
      const logLines = String(value.log).split('\n').filter(l => l.trim().length > 0);
      const testLines = logLines.filter(l => /\b(OK|PASS|SUCCESS|FAIL|ERR|ERROR)\b/i.test(l)).slice(-6);
      if (testLines.length) {
        tickerContainer.innerHTML = testLines.map(line => {
          const isPass = /\b(OK|PASS|SUCCESS)\b/i.test(line);
          const badgeClass = isPass ? 'pass' : 'fail';
          const badgeLabel = isPass ? 'PASS' : 'FAIL';
          return `<div class="ticker-item ${badgeClass}"><span class="ticker-badge">${badgeLabel}</span><span class="ticker-text">${esc(line)}</span></div>`;
        }).join('');
      }
    } else if (!value.running && tickerContainer.children.length === 0) {
      tickerContainer.innerHTML = `<div class="ticker-empty">${esc(t('monitor.liveStreamIdle'))}</div>`;
    }
  }

  const passedCount = progress.passed ?? exportReport.passed ?? 0;
  if ($('nav-count')) {
    $('nav-count').textContent = fmt(passedCount);
    $('nav-count').classList.toggle('has-results', passedCount > 0);
  }
  if ($('live-passed')) {
    animateNumber($('live-passed'), passedCount);
  }
  if (exportReport.profile && $('export-note')) {
    const counts = exportReport.reputation?.counts || {};
    const reportText = t('results.exportReady', {exported:fmt(exportReport.exported), passed:fmt(exportReport.passed), checked:fmt(exportReport.checked), candidates:fmt(exportReport.scope_candidates ?? exportReport.candidates), clean:fmt(counts.clean || 0), listed:fmt((counts.listed || 0) + (counts.local_denied || 0)), unknown:fmt(counts.unknown || 0), local:(exportReport.local_filtered ? t('results.localFiltered', {count:fmt(exportReport.local_filtered)}) : '') + anonymityCounts(exportReport)});
    const snapshotText = snapshotNotes(exportReport);
    const diagnosticText = diagnosticNote(value.diagnostic, progress);
    $('export-note').textContent = reportText + (snapshotText ? ' ' + snapshotText : '') + (diagnosticText ? ' ' + diagnosticText : '');
  }
  renderBreakdown((value.export || {}).breakdown);
  if ($('api-line')) $('api-line').classList.toggle('hidden', !value.api);
  if ($('gateway-line')) $('gateway-line').classList.toggle('hidden', !value.gateway);

  // Gateway screen & Mobile Hub QR / Links
  if (value.gateway) {
    if ($('gateway-address')) $('gateway-address').textContent = value.gateway.address;
    const [gatewayHost, gatewayPort] = value.gateway.address.split(':');
    const tgUrl = `tg://socks?server=${encodeURIComponent(gatewayHost)}&port=${encodeURIComponent(gatewayPort)}`;
    if ($('telegram-gateway')) $('telegram-gateway').href = tgUrl;
    if ($('gateway-stats')) $('gateway-stats').innerHTML = `<span class="pool-dot"></span>${esc(t('gateway.stats', {proxies:fmt(value.gateway.proxies), connections:fmt(value.gateway.connections)}))}`;

    const gwPool = $('gw-pool-size');
    if (gwPool) animateNumber(gwPool, value.gateway.proxies || 0);
    const gwConns = $('gw-conns-size');
    if (gwConns) animateNumber(gwConns, value.gateway.connections || 0);

    const gwTgLink = $('gw-tg-link');
    if (gwTgLink) gwTgLink.href = tgUrl;
    const mobileTgLink = $('mobile-tg-btn-link');
    if (mobileTgLink) mobileTgLink.href = tgUrl;

    const gwQr = $('gw-tg-qr');
    if (gwQr && !gwQr.hasChildNodes()) gwQr.innerHTML = makeQR(tgUrl);
    const mobileQr = $('mobile-tg-qr-box');
    if (mobileQr && !mobileQr.hasChildNodes()) mobileQr.innerHTML = makeQR(tgUrl);
  }
  if ($('api-example')) $('api-example').textContent = value.api ? `${value.api}/random?protocol=socks5&format=txt` : '';
  const snapshotUnavailable = Boolean(exportReport.profile && (snapshot.stale || snapshot.state === 'error'));
  document.querySelectorAll('[data-download]').forEach(node => { node.disabled = snapshotUnavailable || !(value.downloads || []).includes(node.dataset.download) || (value.running && progress.phase === 'exporting'); });
  if ($('copy-page')) $('copy-page').disabled = snapshotUnavailable || value.running;
  if ($('action-copy-selected')) $('action-copy-selected').disabled = snapshotUnavailable || value.running;
  // A stale/error snapshot cannot be rebuilt into a useful export from the UI;
  // the user must re-check first. A partial snapshot remains explicitly labelled
  // and can still be exported as a partial result.
  if ($('export')) $('export').disabled = snapshotUnavailable || value.running;
  if ($('action-export-selected')) $('action-export-selected').disabled = snapshotUnavailable || value.running;
  const report = progress.sources ? progress : value.sources || {};
  renderSources(report, value.source_urls || [], value.source_keys || [], (value.export || {}).source_quality || {});
  const finished = job.id && !value.running ? job.id : null;
  // While a check runs, the open results table follows it every few seconds.
  if (value.running && progress.phase === 'scanning' && currentTab === 'results' && Date.now() - lastLiveReload > 5000) {
    lastLiveReload = Date.now();
    loadResults();
  }
  // In keep-fresh mode the job never finishes; reload the table whenever a new export lands.
  const exportedAt = exportReport.generated_at || null;
  if (exportedAt && lastExportAt && exportedAt !== lastExportAt && !finished) loadResults();
  lastExportAt = exportedAt;
  if (finished && finished !== lastFinished) {
    lastFinished = finished;
    if (job.action !== 'collect') loadResults();
  }
}

const providerCell = row => row.provider ? `${esc(row.provider.org.length > 28 ? row.provider.org.slice(0, 27) + '…' : row.provider.org)}${row.provider.hosting ? ` <span class="badge subtle">${esc(t('provider.hosting'))}</span>` : ''}` : '—';
// "DE → NL" when the proxy sends traffic out from another country than its own address.
const countryLabel = row => row.exit_country && row.exit_country !== row.country ? `${row.country || '?'} → ${row.exit_country}` : (row.country || '—');

function renderBreakdown(breakdown) {
  const node = $('breakdown');
  const groups = breakdown ? [['protocols', breakdown.protocols || {}], ['countries', breakdown.countries || {}]] : [];
  const html = groups.map(([name, counts]) => {
    const items = Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 12);
    if (!items.length) return '';
    const label = key => key === '??' ? t('breakdown.unknown') : name === 'protocols' ? key.toUpperCase() : key;
    return `<div><b>${esc(t('breakdown.' + name))}</b>${items.map(([key, count]) => `<span class="pill">${esc(label(key))} <em>${esc(fmt(count))}</em></span>`).join('')}</div>`;
  }).join('');
  node.innerHTML = html;
  node.classList.toggle('hidden', !html);
}

function renderSources(report, urls, keys=[], quality={}) {
  const sourceRows = report.sources || [];
  const doneCount = sourceRows.filter(row => row.complete).length;
  const statusEl = $('sources-status');
  if (report.denylist_error) {
    statusEl.className = 'badge fail';
    statusEl.textContent = t('report.denylistError');
  } else if (sourceRows.length) {
    statusEl.className = doneCount === sourceRows.length ? 'badge success' : 'badge subtle';
    statusEl.textContent = t('report.loaded', {done: doneCount, total: sourceRows.length});
  } else {
    statusEl.className = 'badge subtle';
    statusEl.textContent = t('report.none');
  }

  $('source-rows').innerHTML = sourceRows.length ? sourceRows.map(row => {
    const label = row.source ? urls[row.source - 1] || t('report.source', {number:row.source}) : t('report.ownList');
    const stats = quality[row.source ? keys[row.source - 1] : 'local'];
    const working = stats ? `<span class="text-teal font-semibold">${fmt(stats.passed)}</span> <span class="text-muted">/ ${fmt(stats.checked)}</span>` : '—';

    let statusBadge = '';
    if (row.complete) {
      if (row.rows === 0) {
        statusBadge = `<span class="badge status-badge subtle">${esc(t('report.statusEmpty'))}</span>`;
      } else if (row.rows === row.invalid) {
        statusBadge = `<span class="badge status-badge warn"><span class="badge-icon">!</span> ${esc(t('report.statusNoValid'))}</span>`;
      } else if (row.blocked && row.blocked >= row.rows) {
        statusBadge = `<span class="badge status-badge warn"><span class="badge-icon">⊘</span> ${esc(t('report.statusBlocked'))}</span>`;
      } else {
        statusBadge = `<span class="badge status-badge pass"><span class="badge-icon">✓</span> ${esc(t('report.statusDone'))}</span>`;
      }
    } else {
      const errText = row.error || t('report.statusError');
      statusBadge = `<span class="badge status-badge fail" title="${esc(row.error || '')}"><span class="badge-icon">✕</span> ${esc(errText.length > 20 ? errText.slice(0, 19) + '…' : errText)}</span>`;
    }

    const blockedBadge = (row.blocked && row.blocked > 0)
      ? `<span class="badge status-badge warn">${fmt(row.blocked)}</span>`
      : `<span>0</span>`;

    const rejectedVal = row.invalid > 0
      ? `<span class="text-muted-warn">${fmt(row.invalid)}</span>`
      : `${fmt(0)}`;

    return `<tr>
      <td title="${esc(label)}" class="source-url-cell">${esc(label)}</td>
      <td>${fmt(row.rows)}</td>
      <td>${rejectedVal}</td>
      <td>${blockedBadge}</td>
      <td>${statusBadge}</td>
      <td>${working}</td>
    </tr>`;
  }).join('') : `<tr><td colspan="6" class="empty">${esc(t('report.empty'))}</td></tr>`;
}

async function poll() {
  if (polling) return;
  polling = true;
  try { renderState(await api('/api/state')); }
  catch {
    const phaseEl = $('phase');
    if (phaseEl) {
      phaseEl.textContent = t('phase.offline');
      phaseEl.dataset.phase = 'offline';
    }
    const monitorCard = document.querySelector('.monitor');
    if (monitorCard) monitorCard.dataset.phase = 'offline';
  }
  finally { polling = false; }
}

const selectedProxies = new Set();

function updateSelectionUI() {
  const bar = $('selection-action-bar');
  const countBadge = $('selection-count');
  const selectAll = $('select-all-proxies');
  const count = selectedProxies.size;

  if (bar) bar.classList.toggle('hidden', count === 0);
  if (countBadge) countBadge.textContent = t('results.selectedCount', {count: fmt(count)});

  const checkboxes = document.querySelectorAll('.proxy-select-box');
  if (checkboxes.length && selectAll) {
    const allChecked = Array.from(checkboxes).every(cb => cb.checked);
    const someChecked = Array.from(checkboxes).some(cb => cb.checked);
    selectAll.checked = allChecked;
    selectAll.indeterminate = someChecked && !allChecked;
  }
}

function renderCountryDistribution(rows, breakdown) {
  const bar = $('country-distribution-bar');
  const stats = $('country-bar-stats');
  if (!bar || !stats) return;

  const counts = {};
  let total = 0;
  if (breakdown && breakdown.countries && Object.keys(breakdown.countries).length) {
    for (const [c, cnt] of Object.entries(breakdown.countries)) {
      counts[c] = cnt;
      total += cnt;
    }
  } else if (rows && rows.length) {
    for (const r of rows) {
      const c = r.country || '??';
      counts[c] = (counts[c] || 0) + 1;
      total++;
    }
  }

  if (total === 0) {
    bar.innerHTML = '<div class="country-bar-seg empty" style="width:100%"></div>';
    stats.textContent = '';
    return;
  }

  const sorted = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  const colors = ['#3b82f6', '#10b981', '#f59e0b', '#8b5cf6', '#ec4899', '#06b6d4', '#64748b'];

  bar.innerHTML = sorted.slice(0, 10).map(([c, cnt], i) => {
    const pct = Math.max(2, (cnt / total * 100)).toFixed(1);
    const color = colors[i % colors.length];
    return `<div class="country-bar-seg" style="width:${pct}%;background:${color}" title="${esc(c)}: ${fmt(cnt)} (${(cnt/total*100).toFixed(1)}%)"></div>`;
  }).join('');

  stats.innerHTML = sorted.slice(0, 5).map(([c, cnt]) => {
    const flag = getCountryFlag(c);
    const pct = (cnt / total * 100).toFixed(0);
    return `<span class="country-stat-item">${flag} <b>${esc(c)}</b> <em>${pct}%</em></span>`;
  }).join(' ');
}

function renderResults(data) {
  const page = data ? data.rows : [];
  const start = data ? data.offset : 0;
  const total = data ? data.total : 0;
  if ($('result-context')) $('result-context').textContent = data && data.profile ? t('results.context', {targets:data.targets.map(target => target.name ? `${target.name} (${target.url})` : target.url).join(' + '), profile:profileLabel(data.request_profile || 'workbench')}) : t('results.empty');
  if ($('result-total')) $('result-total').textContent = t('results.total', {count:fmt(total)});
  const curPage = Math.floor(start / 50) + 1;
  const totalPages = Math.max(1, Math.ceil(total / 50));
  if ($('page-number')) $('page-number').textContent = t('results.pageStatus', {page:fmt(curPage), pages:fmt(totalPages), total:fmt(total)});
  const copyTitle = esc(t('results.copyProxy') || 'Copy proxy address');

  // Country distribution bar
  renderCountryDistribution(page, (state.export || {}).breakdown);

  const rowsContainer = $('result-rows');
  if (!rowsContainer) return;

  rowsContainer.innerHTML = page.length ? page.map((row, index) => {
    const scoreVal = Number(row.score);
    const scoreClass = scoreVal >= 75 ? 'high' : scoreVal >= 45 ? 'med' : 'low';
    const proxyCell = `<div class="proxy-cell"><span class="proxy-text">${esc(row.proxy)}</span><button class="copy-proxy-btn" data-copy-proxy="${esc(row.proxy)}" title="${copyTitle}" aria-label="${copyTitle}"><svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg></button></div>`;
    const detailsBtn = `<button class="button chip details-btn" data-details="${index}"><span class="details-icon">↗</span> <span>${esc(t('results.details'))}</span></button>`;
    const isChecked = selectedProxies.has(row.proxy) ? 'checked' : '';
    const flag = getCountryFlag(row.country);

    const isSocks5 = row.proxy.startsWith('socks5://');
    let tgBtn = '';
    if (isSocks5) {
      const clean = row.proxy.replace('socks5://', '');
      const [h, p] = clean.split(':');
      tgBtn = `<a href="tg://socks?server=${encodeURIComponent(h)}&port=${encodeURIComponent(p || '1080')}" class="button chip row-tg-btn" title="Telegram" target="_blank">TG</a>`;
    }

    const testBtn = `<button type="button" class="button chip quick-test-btn" data-test-proxy="${esc(row.proxy)}" title="${esc(t('action.test') || 'Test')}">⚡</button>`;

    return `<tr>
      <td class="td-check"><input type="checkbox" class="proxy-select-box" data-proxy="${esc(row.proxy)}" aria-label="${esc(t('results.selectProxy', {proxy:row.proxy}))}" ${isChecked}></td>
      <td>${fmt(start + index + 1)}</td>
      <td>${proxyCell}</td>
      <td><span class="score ${scoreClass}">${scoreVal.toFixed(1)}</span></td>
      <td>${latencyBadge(row.latency_ms)}</td>
      <td>${latencyBadge(row.jitter_ms)}</td>
      <td>${row.speed && row.speed.mbps != null ? esc(Number(row.speed.mbps).toFixed(1)) : '—'}</td>
      <td>${(Number(row.min_target_reliability) * 100).toFixed(0)}%</td>
      <td>${row.history ? esc(`${fmt(row.history.passes)}/${fmt(row.history.checks)}`) : '1/1'}</td>
      <td>${reputationBadge(row)}</td>
      <td>${anonymityBadge(row)}</td>
      <td class="country" title="${esc(row.anonymity && row.anonymity.exit_ip ? t('results.exitIp', {ip:row.anonymity.exit_ip}) : '')}"><span class="country-flag">${flag}</span> ${esc(countryLabel(row))}</td>
      <td class="provider" title="${esc(row.provider ? `AS${row.provider.asn} ${row.provider.org}` : '')}">${providerCell(row)}</td>
      <td class="td-actions">
        <div class="row-actions-group">
          ${testBtn}
          ${tgBtn}
          ${detailsBtn}
        </div>
      </td>
    </tr>`;
  }).join('') : `<tr><td colspan="14" class="empty">${esc(t(data ? 'results.noneMatching' : 'results.noneYet'))}</td></tr>`;

  $('result-rows').querySelectorAll('[data-details]').forEach(node => node.onclick = () => details(page[Number(node.dataset.details)]));

  // Selection checkboxes
  $('result-rows').querySelectorAll('.proxy-select-box').forEach(cb => {
    cb.onchange = e => {
      const p = e.target.dataset.proxy;
      if (e.target.checked) selectedProxies.add(p); else selectedProxies.delete(p);
      updateSelectionUI();
    };
  });

  // Inline Quick Test handler
  $('result-rows').querySelectorAll('.quick-test-btn').forEach(btn => {
    btn.onclick = async e => {
      e.stopPropagation();
      const proxy = btn.dataset.testProxy;
      btn.disabled = true;
      btn.innerHTML = '⏳';
      try {
        const res = await api('/api/test-proxy', {proxy});
        if (res.ok) {
          btn.innerHTML = `✓ ${Math.round(res.latency_ms)}ms`;
          btn.className = 'button chip pass test-badge';
          toast(`${proxy} · ${Math.round(res.latency_ms)}ms · OK`);
        } else {
          btn.innerHTML = '✕ Err';
          btn.className = 'button chip fail test-badge';
          toast(`${proxy} · ${res.error || 'Failed'}`, true);
        }
      } catch (err) {
        btn.innerHTML = '✕';
        toast(err.message, true);
      } finally {
        setTimeout(() => {
          btn.disabled = false;
          if (!btn.classList.contains('pass') && !btn.classList.contains('fail')) {
            btn.innerHTML = '⚡';
          }
        }, 5000);
      }
    };
  });

  updateSelectionUI();

  if (!$('result-rows')._copyDelegated) {
    $('result-rows')._copyDelegated = true;
    $('result-rows').addEventListener('click', async event => {
      const btn = event.target.closest('[data-copy-proxy]');
      if (!btn) return;
      event.stopPropagation();
      const proxy = btn.dataset.copyProxy;
      try {
        await navigator.clipboard.writeText(proxy);
        btn.classList.add('copied');
        btn.innerHTML = '✓';
        setTimeout(() => {
          btn.classList.remove('copied');
          btn.innerHTML = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>';
        }, 1200);
        toast(proxy + ' · ' + (t('results.copied') || 'Copied!'));
      } catch {
        toast(t('toast.copyFailed'), true);
      }
    });
  }
}

async function loadResults() {
  if (resultBusy) return;
  resultBusy = true;
  const refreshBtn = $('refresh-results');
  if (refreshBtn) refreshBtn.disabled = true;
  try {
    const query = new URLSearchParams({
      sort: $('result-sort') ? $('result-sort').value : 'recommended',
      min_success: $('result-min') ? $('result-min').value : '1',
      min_anonymity: $('result-anon') ? $('result-anon').value : 'any',
      protocol: $('result-protocol') ? $('result-protocol').value : 'all',
      max_latency: Number($('result-max-latency') ? $('result-max-latency').value : 0) || 0,
      country: $('result-country') ? $('result-country').value.trim() : '',
      hosting: $('result-hosting') ? $('result-hosting').value : 'any',
      q: $('result-search') ? $('result-search').value.trim() : '',
      offset
    });
    const data = await api('/api/results?' + query);
    resultTargets = data.targets || [];
    resultData = {...data, offset};
    renderResults(resultData);
    if ($('prev')) $('prev').disabled = offset === 0;
    if ($('next')) $('next').disabled = offset + 50 >= (data.total || 0);
  } catch (error) {
    toast(error.message, true);
  } finally {
    resultBusy = false;
    if (refreshBtn) refreshBtn.disabled = false;
  }
}

function renderDetails(row) {
  $('details-title').textContent = row.proxy;
  const verdict = row.reputation || {status:'clean', dnsbl:[]};
  const repStatus = verdict.status || 'clean';
  const dnsblItems = verdict.dnsbl || [];

  const dnsblBadges = dnsblItems.length ? dnsblItems.map(item => {
    const isListed = item.status === 'listed';
    const isClear = item.status === 'clear';
    const cls = isListed ? 'dnsbl-pill listed' : isClear ? 'dnsbl-pill clear' : 'dnsbl-pill unk';
    const label = t(isListed ? 'dnsbl.listed' : isClear ? 'dnsbl.clear' : 'dnsbl.noAnswer');
    return `<span class="${cls}"><span class="dnsbl-zone">${esc(item.zone)}</span> <span class="dnsbl-status">${esc(label)}</span></span>`;
  }).join(' ') : `<span class="detail-val-dim">${esc(t('dnsbl.notChecked'))}</span>`;

  let html = `<div class="detail-reputation-card">
    <div class="reputation-card-grid">
      <div class="rep-stat-col">
        <span class="rep-stat-label">${esc(t('details.cleanliness'))}</span>
        <div class="rep-stat-val">${reputationBadge(row)}</div>
      </div>
      <div class="rep-stat-col">
        <span class="rep-stat-label">DNSBL / Denylist</span>
        <div class="dnsbl-pill-group">${dnsblBadges}</div>
      </div>
      ${verdict.local_rule ? `<div class="rep-stat-col"><span class="rep-stat-label">${esc(t('details.localRule'))}</span><code class="local-rule-code">${esc(verdict.local_rule)}</code></div>` : ''}
      <div class="rep-stat-col">
        <span class="rep-stat-label">${esc(t('col.anonymity'))}</span>
        <div class="rep-stat-val">${anonymityBadge(row)}${row.anonymity && row.anonymity.exit_ip ? ` <span class="exit-ip-badge">IP: ${esc(row.anonymity.exit_ip)}</span>` : ''}</div>
      </div>
    </div>
    ${row.aborted ? `<div class="detail-aborted-banner">${esc(t('details.aborted'))}</div>` : ''}
  </div>`;

  const samples = row.samples || [];
  html += resultTargets.map((target, index) => {
    const targetSamples = samples.filter(sample => sample.target === index);
    const passedCount = targetSamples.filter(s => s.ok).length;
    const totalCount = targetSamples.length;
    const targetPassedClass = totalCount > 0 && passedCount === totalCount ? 'pass-all' : passedCount > 0 ? 'pass-part' : 'pass-none';

    const cardsHtml = targetSamples.map(sample => {
      let codeClass = 'code-err';
      let codeLabel = esc(sample.error || 'Error');
      const code = sample.status;
      if (code >= 200 && code < 300) {
        codeClass = 'code-2xx';
        codeLabel = 'OK';
      } else if (code >= 300 && code < 400) {
        codeClass = 'code-3xx';
        codeLabel = 'Redirect';
      } else if (code >= 400 && code < 500) {
        codeClass = 'code-4xx';
        codeLabel = 'Client Err';
      } else if (code >= 500) {
        codeClass = 'code-5xx';
        codeLabel = 'Server Err';
      } else if (!code) {
        const isTimeout = String(sample.error || '').toLowerCase().includes('timeout');
        codeClass = isTimeout ? 'code-timeout' : 'code-err';
        codeLabel = isTimeout ? 'Timeout' : esc(sample.error || 'Error');
      }

      const latencyClass = sample.ms < 800 ? 'stat-fast' : sample.ms < 2000 ? 'stat-med' : 'stat-slow';

      return `<div class="attempt-card ${sample.ok ? 'attempt-ok' : 'attempt-fail'}">
        <div class="attempt-card-header">
          <span class="attempt-title">${esc(t('details.attemptNum', {number: sample.attempt}))}</span>
          <span class="badge attempt-verdict ${sample.ok ? 'pass' : 'fail'}">${sample.ok ? '✓ ' + esc(t('details.success')) : '✕ ' + esc(sample.error || 'Fail')}</span>
        </div>
        <div class="status-code-pill ${codeClass}">
          <span class="status-code-num">${code ?? 'ERR'}</span>
          <span class="status-code-label">${codeLabel}</span>
        </div>
        <div class="attempt-metrics">
          <div class="attempt-metric">
            <span class="metric-label">${esc(t('details.time'))}</span>
            <span class="metric-value ${latencyClass}">${esc(ms(sample.ms))}</span>
          </div>
          <div class="attempt-metric">
            <span class="metric-label">${esc(t('details.bytes'))}</span>
            <span class="metric-value">${fmt(sample.bytes)} B</span>
          </div>
        </div>
        ${!sample.ok && sample.error ? `<div class="attempt-error-detail" title="${esc(sample.error)}">${esc(sample.error)}</div>` : ''}
      </div>`;
    }).join('');

    return `<div class="target-detail-section">
      <div class="target-detail-header">
        <div class="target-title-group">
          <span class="target-num-badge">${index + 1}</span>
          <div class="target-title-texts">
            <h3 class="target-name">${esc(target.name || t('details.targetService', {number:index + 1}))}</h3>
            <span class="target-url" title="${esc(target.url)}">${esc(target.url)}</span>
          </div>
        </div>
        <span class="badge target-ratio-badge ${targetPassedClass}">${esc(t('details.passedRatio', {passed: fmt(passedCount), total: fmt(totalCount)}))}</span>
      </div>
      <div class="attempt-grid">${cardsHtml || `<div class="no-attempts-hint">${esc(t('report.empty'))}</div>`}</div>
    </div>`;
  }).join('');

  $('details-body').innerHTML = html;
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

const detailsDialog = $('details-dialog');
$('close-details').onclick = () => detailsDialog.close();

detailsDialog.addEventListener('click', event => {
  if (event.target === detailsDialog) {
    const rect = detailsDialog.getBoundingClientRect();
    const isInDialog = (
      rect.top <= event.clientY &&
      event.clientY <= rect.bottom &&
      rect.left <= event.clientX &&
      event.clientX <= rect.right
    );
    if (!isInDialog) {
      detailsDialog.close();
    }
  }
});

window.addEventListener('keydown', event => {
  if (event.key === 'Escape' && detailsDialog.open) {
    detailsDialog.close();
  }
});

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
async function clearLocalData() {
  if (!confirm(t('confirm.clear'))) return;
  try {
    const result = await api('/api/clear-data', {});
    toast(t('toast.cleared', {count:result.removed.length}));
    await poll();
  } catch (error) {
    toast(error.message, true);
  }
}
document.querySelectorAll('[data-action="clear-data"]').forEach(button => { button.onclick = clearLocalData; });
$('refresh-results').onclick = () => { offset = 0; loadResults(); };
['result-sort', 'result-min', 'result-anon', 'result-protocol', 'result-max-latency', 'result-hosting'].forEach(id => $(id).onchange = () => { offset = 0; loadResults(); });
let searchTimer;
$('result-search').oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { offset = 0; loadResults(); }, 300); };
$('result-country').oninput = $('result-search').oninput;

const presets = {
  quick: {attempts:1, timeout:5, connect_timeout:2, workers:256, prefilter:1024, fail_fast:true},
  balanced: {attempts:3, timeout:8, connect_timeout:4, workers:128, prefilter:512, fail_fast:true},
  thorough: {attempts:5, timeout:12, connect_timeout:6, prefilter:256, workers:128, fail_fast:true}
};
function markCopied(button, html) {
  if (!button) return;
  button.classList.add('copied');
  if (html) {
    if (!button.dataset.origHtml) button.dataset.origHtml = button.innerHTML;
    button.innerHTML = html;
  }
  setTimeout(() => {
    button.classList.remove('copied');
    if (html && button.dataset.origHtml) {
      button.innerHTML = button.dataset.origHtml;
      delete button.dataset.origHtml;
    }
  }, 1400);
}

function updateSegmentedGlider() {
  const control = document.querySelector('.segmented-control');
  if (!control) return;
  const active = control.querySelector('.segmented-btn.active');
  const glider = control.querySelector('.segmented-glider');
  if (!active || !glider) return;
  const cRect = control.getBoundingClientRect();
  const aRect = active.getBoundingClientRect();
  if (aRect.width === 0) return;
  const left = aRect.left - cRect.left;
  glider.style.width = `${aRect.width}px`;
  glider.style.transform = `translateX(${left}px)`;
  glider.style.opacity = '1';
}

function updateLineCounts() {
  const countLines = val => (val || '').split('\n').map(s => s.trim()).filter(s => s.length > 0 && !s.startsWith('#')).length;
  const pluralLines = c => {
    if (lang === 'ru') {
      const mod10 = c % 10;
      const mod100 = c % 100;
      if (mod100 >= 11 && mod100 <= 19) return `${c} строк`;
      if (mod10 === 1) return `${c} строка`;
      if (mod10 >= 2 && mod10 <= 4) return `${c} строки`;
      return `${c} строк`;
    }
    return c === 1 ? '1 line' : `${c} lines`;
  };
  const dZones = $('dnsbl-zones');
  const dCount = $('dnsbl-lines-count');
  if (dZones && dCount) {
    const c = countLines(dZones.value);
    dCount.textContent = pluralLines(c);
    dCount.classList.toggle('has-lines', c > 0);
  }
  const deny = $('denylist');
  const denyCount = $('denylist-lines-count');
  if (deny && denyCount) {
    const c = countLines(deny.value);
    denyCount.textContent = pluralLines(c);
    denyCount.classList.toggle('has-lines', c > 0);
  }
}

if ($('dnsbl-zones')) $('dnsbl-zones').addEventListener('input', updateLineCounts);
if ($('denylist')) $('denylist').addEventListener('input', updateLineCounts);
window.addEventListener('resize', updateSegmentedGlider);

document.querySelectorAll('[data-preset]').forEach(node => node.onclick = () => {
  const preset = presets[node.dataset.preset];
  for (const [key, value] of Object.entries(preset)) {
    if (typeof value === 'boolean') $(key).checked = value; else $(key).value = value;
  }
  document.querySelectorAll('[data-preset]').forEach(b => b.classList.remove('active'));
  node.classList.add('active');
  updateSegmentedGlider();
  toast(t('preset.applied'));
});

let lastGeoStatus = null;
function renderGeo(status) {
  lastGeoStatus = status;
  const statusEl = $('geo-status');
  if (!statusEl) return;
  if (status && status.available) {
    statusEl.className = 'badge success geo-ready-badge';
    statusEl.innerHTML = `<span class="status-dot green"></span> ` + esc(t('geo.ready', {count:fmt(status.ranges)}) + (status.providers ? t('geo.providers', {count:fmt(status.provider_ranges)}) : ''));
  } else {
    statusEl.className = 'badge subtle';
    statusEl.innerHTML = `<span class="status-dot gray"></span> ` + esc(t('geo.missing'));
  }
}
async function loadGeo() {
  try { renderGeo(await api('/api/geoip')); } catch {}
}
$('geo-update').onclick = async () => {
  const btn = $('geo-update');
  const spinner = btn.querySelector('.btn-spinner');
  const label = btn.querySelector('.btn-label') || btn;
  btn.disabled = true;
  if (spinner) spinner.classList.remove('hidden');
  label.textContent = t('geo.downloading');
  toast(t('geo.downloading'));
  try {
    renderGeo(await api('/api/geoip/update', {}));
    toast(t('geo.updated'));
    if (currentTab === 'results') loadResults();
  } catch (error) {
    toast(error.message, true);
  } finally {
    btn.disabled = false;
    if (spinner) spinner.classList.add('hidden');
    label.textContent = t('geo.download');
  }
};
loadGeo();
$('copy-page').onclick = async () => {
  const proxies = ((resultData && resultData.rows) || []).map(row => row.proxy);
  if (!proxies.length) { toast(t('toast.copyEmpty'), true); return; }
  try {
    await navigator.clipboard.writeText(proxies.join('\n') + '\n');
    markCopied($('copy-page'), '<span class="btn-icon">✓</span> <span>' + esc(t('results.copied') || 'Скопировано!') + '</span>');
    toast(t('toast.copied', {count:fmt(proxies.length)}));
  } catch {
    toast(t('toast.copyFailed'), true);
  }
};
function exportSettingsFile() {
  const link = document.createElement('a');
  link.href = URL.createObjectURL(new Blob([JSON.stringify(getSettings(), null, 2)], {type:'application/json'}));
  link.download = 'proxy-workbench-settings.json';
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}
document.querySelectorAll('[data-action="export-settings"]').forEach(button => { button.onclick = exportSettingsFile; });

async function importSettingsFile(event) {
  const file = event.target.files[0];
  event.target.value = '';
  if (!file) return;
  try {
    let parsed;
    try { parsed = JSON.parse(await file.text()); } catch { throw new Error(t('toast.settingsBad')); }
    if (!parsed || typeof parsed !== 'object' || !Array.isArray(parsed.targets)) throw new Error(t('toast.settingsBad'));
    fill(await api('/api/settings', {...settings, ...parsed}));
    toast(t('toast.settingsImported'));
  } catch (error) {
    toast(error.message, true);
  }
}
document.querySelectorAll('[data-action="import-settings"]').forEach(input => { input.onchange = importSettingsFile; });
async function copyGatewayAddress(button) {
  const addr = $('gateway-address') ? $('gateway-address').textContent : '127.0.0.1:8899';
  try {
    await navigator.clipboard.writeText(addr);
    markCopied(button, '<span class="btn-icon">✓</span> <span class="btn-text">' + esc(t('results.copied')) + '</span>');
    toast(t('toast.gatewayCopied'));
  } catch {
    toast(t('toast.copyFailed'), true);
  }
}
if ($('copy-gateway')) $('copy-gateway').onclick = event => copyGatewayAddress(event.currentTarget);
if ($('copy-gateway-hero')) $('copy-gateway-hero').onclick = event => copyGatewayAddress(event.currentTarget);
if ($('copy-api')) {
  $('copy-api').onclick = async () => {
    const apiText = $('api-example') ? $('api-example').textContent : '';
    try {
      await navigator.clipboard.writeText(apiText);
      markCopied($('copy-api'), '<span class="btn-icon">✓</span> <span class="btn-text">' + esc(t('results.copied') || 'Скопировано!') + '</span>');
      toast(t('toast.apiCopied'));
    } catch {
      toast(t('toast.copyFailed'), true);
    }
  };
}

$('prev').onclick = () => { offset = Math.max(0, offset - 50); loadResults(); };
$('next').onclick = () => { offset += 50; loadResults(); };
function bindCodeEditor(textareaId, gutterId, counterId) {
  const textarea = $(textareaId);
  const gutter = $(gutterId);
  const counter = $(counterId);
  if (!textarea || !gutter) return () => {};

  function update() {
    const text = textarea.value || '';
    const lines = text.length ? text.split('\n').length : 0;
    if (counter) counter.textContent = `${fmt(lines)} ${t('sources.lines')}`;
    const count = Math.max(1, lines);
    gutter.innerHTML = Array.from({length: count}, (_, i) => i + 1).join('<br>');
  }

  textarea.addEventListener('input', update);
  textarea.addEventListener('scroll', () => {
    gutter.scrollTop = textarea.scrollTop;
  });
  gutter.addEventListener('wheel', e => {
    textarea.scrollTop += e.deltaY;
  }, { passive: true });

  return update;
}

const updateSourcesLines = bindCodeEditor('sources', 'sources-gutter', 'sources-line-count');
const updateProxiesLines = bindCodeEditor('proxies', 'proxies-gutter', 'proxies-line-count');

function updateCodeEditors() {
  if (updateSourcesLines) updateSourcesLines();
  if (updateProxiesLines) updateProxiesLines();
}

$('sources').oninput = () => { updateSourceCount(); updateCodeEditors(); };
$('use_sources').onchange = () => { updateSourceCount(); updateCodeEditors(); };
$('proxies').oninput = updateCodeEditors;
$('request-profile').onchange = updateIdentity;
$('dnsbl-enabled').onchange = updateIdentity;

async function sourceAction(path, button, message) {
  button.disabled = true;
  try {
    const value = await api(path, getSettings());
    settings = value.settings;
    $('sources').value = settings.sources.join('\n');
    updateSourceCount();
    updateCodeEditors();
    toast(message(value));
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

$('prune-sources').onclick = () => sourceAction('/api/sources/prune', $('prune-sources'), value => value.removed.length ? t('toast.pruned', {count:fmt(value.removed.length)}) : t('toast.prunedNone'));
$('update-sources').onclick = () => sourceAction('/api/sources/update', $('update-sources'), value => value.added.length ? t('toast.sourcesAdded', {count:fmt(value.added.length)}) : t('toast.sourcesCurrent'));
$('reset-sources').onclick = async () => {
  try {
    const value = await api('/api/defaults');
    $('sources').value = value.sources.join('\n');
    updateSourceCount();
    updateCodeEditors();
    toast(t('toast.sourcesReset'));
  } catch (error) {
    toast(error.message, true);
  }
};

$('import-file').onchange = async event => {
  const file = event.target.files[0];
  if (!file) return;
  if (file.size > 20_000_000) { toast(t('error.fileTooLarge'), true); return; }
  const text = await file.text();
  $('proxies').value = text;
  updateCodeEditors();
  const lineCount = text.split('\n').filter(l => l.trim()).length;
  toast(t('toast.listLoaded') + ` (${fmt(lineCount)})`);
  const dropZone = document.querySelector('.file-drop');
  if (dropZone) {
    dropZone.classList.add('file-loaded');
    setTimeout(() => dropZone.classList.remove('file-loaded'), 1200);
  }
  $('import-file').value = '';
};

// Global safeguards: prevent browser from navigating/opening file when dragged anywhere on window
window.addEventListener('dragover', e => { e.preventDefault(); }, false);
window.addEventListener('drop', e => { e.preventDefault(); }, false);

const dropZone = document.querySelector('.file-drop');
if (dropZone) {
  let dragCounter = 0;
  dropZone.addEventListener('dragenter', e => {
    e.preventDefault();
    e.stopPropagation();
    dragCounter++;
    dropZone.classList.add('drag-over');
  });
  dropZone.addEventListener('dragover', e => {
    e.preventDefault();
    e.stopPropagation();
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
    dropZone.classList.add('drag-over');
  });
  dropZone.addEventListener('dragleave', e => {
    e.preventDefault();
    e.stopPropagation();
    dragCounter--;
    if (dragCounter <= 0) {
      dragCounter = 0;
      dropZone.classList.remove('drag-over');
    }
  });
  dropZone.addEventListener('drop', async e => {
    e.preventDefault();
    e.stopPropagation();
    dragCounter = 0;
    dropZone.classList.remove('drag-over');
    const file = e.dataTransfer && e.dataTransfer.files[0];
    if (!file) return;
    if (file.size > 20_000_000) { toast(t('error.fileTooLarge'), true); return; }
    const text = await file.text();
    $('proxies').value = text;
    updateCodeEditors();
    const lineCount = text.split('\n').filter(l => l.trim()).length;
    toast(t('toast.listLoaded') + ` (${fmt(lineCount)})`);
    dropZone.classList.add('file-loaded');
    setTimeout(() => dropZone.classList.remove('file-loaded'), 1200);
  });
}

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
  const langBtn = $('lang-toggle');
  if (langBtn) {
    langBtn.textContent = t('lang.button');
    langBtn.setAttribute('aria-label', t('lang.label'));
    langBtn.title = t('lang.label');
  }
  const sidebarLabel = $('sidebar-lang-label');
  if (sidebarLabel) {
    sidebarLabel.textContent = t('lang.currentName');
  }
  $('page-label').textContent = t('nav.' + currentTab);
  if (catalogData) renderCatalog(catalogData);
  renderTheme();
  updateIdentity();
  updateSourceCount();
  updateCodeEditors();
}
function setupEnhancedListeners() {
  // Select All Proxies
  const selectAll = $('select-all-proxies');
  if (selectAll) {
    selectAll.onchange = e => {
      const isChecked = e.target.checked;
      document.querySelectorAll('.proxy-select-box').forEach(cb => {
        cb.checked = isChecked;
        const p = cb.dataset.proxy;
        if (isChecked) selectedProxies.add(p); else selectedProxies.delete(p);
      });
      updateSelectionUI();
    };
  }

  // Floating Selection Bar Actions
  const btnCopySel = $('action-copy-selected');
  if (btnCopySel) {
    btnCopySel.onclick = async () => {
      if (!selectedProxies.size) return;
      try {
        await navigator.clipboard.writeText(Array.from(selectedProxies).join('\n') + '\n');
        markCopied(btnCopySel, '<span class="btn-icon">✓</span> <span>' + esc(t('results.copied') || 'Copied!') + '</span>');
        toast(t('toast.copied', {count: fmt(selectedProxies.size)}));
      } catch {
        toast(t('toast.copyFailed'), true);
      }
    };
  }

  const btnExportSel = $('action-export-selected');
  if (btnExportSel) {
    btnExportSel.onclick = () => {
      if (!selectedProxies.size || state.running) return;
      start('export', Array.from(selectedProxies));
    };
  }

  const btnBanSel = $('action-ban-selected');
  if (btnBanSel) {
    btnBanSel.onclick = async () => {
      if (!selectedProxies.size) return;
      if (!confirm(t('confirm.denylist', {count:fmt(selectedProxies.size)}))) return;
      btnBanSel.disabled = true;
      try {
        const res = await api('/api/denylist/add', {proxies:Array.from(selectedProxies)});
        const added = Array.isArray(res.added) ? res.added : [];
        toast(t('toast.banned', {count:fmt(res.count ?? added.length)}));
        selectedProxies.clear();
        updateSelectionUI();
        if ($('denylist') && added.length) {
          const cur = $('denylist').value.trim();
          $('denylist').value = cur ? cur + '\n' + added.join('\n') : added.join('\n');
          updateLineCounts();
        }
        loadResults();
      } catch (err) {
        toast(err.message, true);
      } finally {
        btnBanSel.disabled = false;
      }
    };
  }

  const btnClearSel = $('action-clear-selection');
  if (btnClearSel) {
    btnClearSel.onclick = () => {
      selectedProxies.clear();
      document.querySelectorAll('.proxy-select-box').forEach(cb => cb.checked = false);
      if ($('select-all-proxies')) $('select-all-proxies').checked = false;
      updateSelectionUI();
    };
  }

  // Filter Chips in Results
  document.querySelectorAll('#results-filter-chips .filter-chip').forEach(chip => {
    chip.onclick = () => {
      document.querySelectorAll('#results-filter-chips .filter-chip').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      const f = chip.dataset.filter;
      if (f === 'all') {
        $('result-protocol').value = 'all';
        $('result-anon').value = 'any';
        $('result-max-latency').value = 0;
        $('result-sort').value = 'recommended';
      } else if (f === 'fast') {
        $('result-max-latency').value = 300;
        $('result-sort').value = 'speed';
      } else if (f === 'socks5') {
        $('result-protocol').value = 'socks5';
      } else if (f === 'http') {
        $('result-protocol').value = 'http';
      } else if (f === 'elite') {
        $('result-anon').value = 'elite';
      } else if (f === 'clean') {
        $('result-sort').value = 'quality';
      } else if (f === 'speed') {
        $('result-sort').value = 'bandwidth';
      }
      offset = 0;
      loadResults();
    };
  });

  // Quick Scenarios in Scan tab
  document.querySelectorAll('.scenario-card').forEach(card => {
    card.onclick = () => {
      document.querySelectorAll('.scenario-card').forEach(c => c.classList.remove('active'));
      card.classList.add('active');
      const s = card.dataset.scenario;
      if (s === 'telegram') {
        if ($('protocol')) $('protocol').value = 'socks5';
        if ($('connect_timeout')) $('connect_timeout').value = 3;
        if ($('timeout')) $('timeout').value = 6;
        if ($('speedtest-url')) $('speedtest-url').value = '';
        if ($('request-profile')) $('request-profile').value = 'workbench';
      } else if (s === 'youtube') {
        if ($('protocol')) $('protocol').value = 'all';
        if ($('connect_timeout')) $('connect_timeout').value = 4;
        if ($('timeout')) $('timeout').value = 10;
        if ($('speedtest-url')) $('speedtest-url').value = 'https://speed.cloudflare.com/__down?bytes=5000000';
        if ($('request-profile')) $('request-profile').value = 'workbench';
      } else if (s === 'anon') {
        if ($('protocol')) $('protocol').value = 'socks5';
        if ($('dnsbl-enabled')) $('dnsbl-enabled').checked = true;
        if ($('strict-clean')) $('strict-clean').checked = true;
        if ($('min_anonymity')) $('min_anonymity').value = 'elite';
        if ($('request-profile')) $('request-profile').value = 'workbench';
      } else if (s === 'scrape') {
        if ($('workers')) $('workers').value = 256;
        if ($('connect_timeout')) $('connect_timeout').value = 2;
        if ($('timeout')) $('timeout').value = 5;
        if ($('prefilter')) $('prefilter').value = 1024;
        if ($('watch')) $('watch').value = 60;
        if ($('request-profile')) $('request-profile').value = 'minimal';
      }
      updateIdentity();
      toast(t('scenario.applied'));
    };
    card.setAttribute('role', 'button');
    card.tabIndex = 0;
    card.onkeydown = event => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        card.click();
      }
    };
  });

  document.querySelectorAll('[data-copy-text]').forEach(button => {
    button.onclick = async () => {
      try {
        await navigator.clipboard.writeText(button.dataset.copyText || '');
        markCopied(button, '<span class="btn-icon">✓</span> <span>' + esc(t('results.copied')) + '</span>');
        toast(t('toast.copied', {count:fmt(1)}));
      } catch {
        toast(t('toast.copyFailed'), true);
      }
    };
  });

  // Gateway screen tabs
  document.querySelectorAll('.gw-tab-btn').forEach(btn => {
    btn.onclick = () => {
      document.querySelectorAll('.gw-tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.gw-tab-content').forEach(c => c.classList.remove('active'));
      btn.classList.add('active');
      const tab = btn.dataset.gwTab;
      const target = $('gw-tab-' + tab);
      if (target) target.classList.add('active');
    };
  });

  // Mobile Hub config copy
  async function copyConfigDownload(filename) {
    try {
      const res = await fetch('/api/download/' + filename, {headers: {'X-Workbench-Token': token}});
      if (res.ok) {
        const text = await res.text();
        await navigator.clipboard.writeText(text);
        toast(t('toast.copied', {count: 1}));
        return;
      }
    } catch {}
    toast(t('error.fileNotReady'), true);
  }

  if ($('btn-copy-singbox')) $('btn-copy-singbox').onclick = () => copyConfigDownload('singbox.json');
  if ($('btn-copy-clash')) $('btn-copy-clash').onclick = () => copyConfigDownload('clash.yaml');
}

// --- Source catalog -------------------------------------------------------
// One view over the same catalog the CLI and the read-only API read.  A row
// shows what the app observed, never "working"/"dead"/"quality": those words
// have no evidence behind them in this project.
const CATALOG_PAGE = 50;
const ACCESS_GROUPS = ['public_free', 'permanent_free_quota', 'free_with_key', 'trial', 'paid',
                       'own_infrastructure', 'snapshot_unavailable', 'unknown'];
const SOURCE_FORMATS = ['http', 'https', 'socks4', 'socks5', 'socks5h', 'auto', 'text', 'geonode',
                        'http-fields', 'line', 'json-records', 'fields', 'page-json', 'html-table'];
let catalogData = null;
let catalogLimit = CATALOG_PAGE;
let catalogDetailId = null;
let catalogTimer = null;

const catalogFilters = () => ({
  q: $('catalog-q').value.trim(),
  state: $('catalog-state').value,
  category: $('catalog-category').value,
  protocol: $('catalog-protocol').value,
  format: $('catalog-format').value,
  access: $('catalog-access').value,
  set: $('catalog-set-filter').value
});

function catalogQuery(extra={}) {
  const params = new URLSearchParams();
  const filters = {...catalogFilters(), ...extra};
  for (const [key, value] of Object.entries(filters)) if (value) params.set(key, value);
  params.set('limit', String(catalogLimit));
  return params.toString();
}

function fillCatalogSelect(id, values, labelOf) {
  const node = $(id);
  if (!node || node.dataset.filled === String(values.length)) return;
  const current = node.value;
  const all = node.querySelector('option[value=""]');
  node.innerHTML = '';
  node.appendChild(all);
  for (const value of values) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = labelOf ? labelOf(value) : value;
    node.appendChild(option);
  }
  node.value = values.includes(current) ? current : '';
  node.dataset.filled = String(values.length);
}

function renderCatalogFacets(view) {
  const facet = value => Object.keys(value || {});
  fillCatalogSelect('catalog-category', facet(view.facets.categories).sort());
  fillCatalogSelect('catalog-protocol', ['http', 'https', 'socks4', 'socks5']);
  fillCatalogSelect('catalog-format', facet(view.facets.formats).sort());
  fillCatalogSelect('catalog-access', ACCESS_GROUPS.filter(group => (view.facets.access_groups || {})[group]),
                    group => t(`cat.group.${group}`));
  fillCatalogSelect('catalog-set-filter', (view.sets || []).map(item => item.id), item => {
    const set = (view.sets || []).find(entry => entry.id === item);
    return set ? `${set.id} (${set.members})` : item;
  });
  const states = view.facets.states || {};
  const node = $('catalog-state');
  const wanted = Object.keys(states).filter(value => states[value]).sort();
  if (node.dataset.filled !== String(wanted.join(','))) {
    const current = node.value;
    const all = node.querySelector('option[value=""]');
    node.innerHTML = '';
    node.appendChild(all);
    for (const value of wanted) {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = `${t(`cat.state.${value}`)} (${states[value]})`;
      node.appendChild(option);
    }
    node.value = wanted.includes(current) ? current : '';
    node.dataset.filled = String(wanted.join(','));
  }
}

function renderCatalogSets(view) {
  $('catalog-sets').innerHTML = (view.sets || []).map(item => `
    <div class="catalog-set${item.applied ? ' applied' : ''}">
      <div class="catalog-set-main">
        <strong>${esc(item.id)}</strong>
        <span class="catalog-set-name">${esc(item.name)}</span>
        <span class="badge subtle">${esc(t('cat.setMembers', {count: fmt(item.members)}))}</span>
        ${item.applied ? `<span class="badge success">${esc(t('cat.choice.selected'))}</span>` : ''}
      </div>
      <div class="catalog-set-meta">
        ${item.new_members.length ? `<span class="badge warn">${esc(t('cat.setNew', {count: fmt(item.new_members.length)}))}</span>` : ''}
        <button class="button light" data-catalog-set="${esc(item.id)}" data-i18n="cat.applySet">${esc(t('cat.applySet'))}</button>
      </div>
    </div>`).join('');
  $('catalog-sets').querySelectorAll('[data-catalog-set]').forEach(button => {
    button.onclick = () => catalogAction('/api/sources/set', {set: button.dataset.catalogSet}, 'toast.cat.set');
  });
}

function renderCatalogGroups(view) {
  $('catalog-groups').innerHTML = (view.access_groups || []).map(group => `
    <button class="catalog-group" data-catalog-access="${esc(group.id)}" title="${esc(group.checked_at || '')}">
      <span class="catalog-group-name">${esc(t(`cat.group.${group.id}`))}</span>
      <span class="catalog-group-count">${fmt(group.count)}</span>
      ${group.checked_at ? `<span class="catalog-group-date">${esc(group.checked_at.slice(0, 10))}</span>` : ''}
    </button>`).join('');
  $('catalog-groups').querySelectorAll('[data-catalog-access]').forEach(button => {
    button.onclick = () => {
      $('catalog-access').value = $('catalog-access').value === button.dataset.catalogAccess ? '' : button.dataset.catalogAccess;
      renderLang();
      reloadCatalog();
    };
  });
}

function relativeAge(seconds) {
  if (seconds < 60) return `${seconds} s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h`;
  return `${Math.floor(seconds / 86400)} d`;
}

function runtimeCell(runtime) {
  if (!runtime || !runtime.observed_at) {
    return runtime && runtime.error
      ? `<span class="catalog-err" title="${esc(runtime.error)}">${esc(runtime.error)}</span>`
      : '<span class="text-muted">—</span>';
  }
  const parts = [
    `<span title="${esc(t('cat.accepted'))}">${esc(t('cat.accepted'))} ${fmt(runtime.accepted)}</span>`,
    `<span title="${esc(t('cat.rejected'))}">${esc(t('cat.rejected'))} ${fmt(runtime.rejected)}</span>`,
    `<span title="${esc(t('cat.recognized'))}">${esc(t('cat.recognized'))} ${fmt(runtime.recognized)}</span>`
  ];
  const contribution = runtime.contribution;
  if (contribution && contribution.accepted) {
    parts.push(`<span class="text-muted" title="${esc(t('cat.newUnique'))}">${esc(t('cat.newUnique'))}: ${fmt(contribution.exclusive)}</span>`);
  }
  if (runtime.checked_by_app !== null && runtime.checked_by_app !== undefined) {
    parts.push(`<span class="text-muted" title="${esc(t('cat.checkedByApp'))}">${esc(t('cat.checkedByApp'))} ${fmt(runtime.checked_by_app)}, ${esc(t('cat.passedProfile'))} ${fmt(runtime.passed_profile)}</span>`);
  }
  if (runtime.cache_state && runtime.cache_state !== 'none') parts.push(`<span class="badge subtle">${esc(runtime.cache_state)}</span>`);
  if (runtime.error) parts.push(`<span class="catalog-err" title="${esc(t('cat.error'))}">${esc(runtime.error)}</span>`);
  if (runtime.quarantine_until) parts.push(`<span class="badge warn">${esc(t('cat.quarantineUntil'))} ${esc(new Date(runtime.quarantine_until * 1000).toLocaleString())}</span>`);
  return parts.join(' ');
}

function catalogRowHtml(row) {
  const runtime = row.runtime || {};
  const stateLabel = t(`cat.state.${row.state}`);
  const stateClass = ['failed', 'quarantined'].includes(row.state) ? 'fail'
    : ['stale'].includes(row.state) ? 'warn'
    : ['last_good', 'has_data'].includes(row.state) ? 'pass' : 'subtle';
  const choiceClass = row.selection_state === 'selected' ? 'pass' : row.selection_state === 'disabled' ? 'warn' : 'subtle';
  const accessNote = row.access_blocked_reason || row.not_proxy_source_reason || row.access_note;
  const age = runtime.last_good_age_seconds;
  return `<div class="catalog-row" data-source-id="${esc(row.id)}">
    <div class="catalog-cell catalog-cell-source">
      <button class="catalog-link" data-catalog-details="${esc(row.id)}">${esc(row.name)}</button>
      <div class="catalog-id">${esc(row.id)}</div>
      <div class="catalog-publisher">${esc(row.publisher.name || row.publisher.id || '—')} · ${esc(row.category)}</div>
      ${row.custom ? `<span class="badge subtle">${esc(t('cat.state.custom'))}</span>` : ''}
      ${row.retired ? `<div class="text-muted-warn">${esc(t('cat.retiredNote'))}</div>` : ''}
      ${(row.sets || []).length ? `<div class="catalog-sets-inline">${row.sets.map(set => `<span class="badge subtle">${esc(set)}</span>`).join('')}</div>` : ''}
    </div>
    <div class="catalog-cell catalog-cell-format">
      <span class="badge subtle">${esc(row.adapter)}</span>
      <div>${(row.formats || []).map(value => `<span class="badge subtle">${esc(value)}</span>`).join(' ')}</div>
      <div class="text-muted">${(row.protocols || []).map(value => `<span class="badge subtle">${esc(value)}</span>`).join(' ')}</div>
      <div class="text-muted" title="${esc(accessNote || '')}">${esc(accessNote ? accessNote.slice(0, 60) : '')}</div>
    </div>
    <div class="catalog-cell catalog-cell-access">
      <div title="${esc(t('cat.termsLink'))}">${esc(t(`cat.group.${row.access_group}`))}</div>
      <div class="text-muted">${esc(row.access)}</div>
      ${row.checked_at ? `<div class="text-muted">${esc(row.checked_at.slice(0, 10))}</div>` : ''}
      ${row.terms_url ? `<a class="catalog-terms" href="${esc(row.terms_url)}" target="_blank" rel="noopener">${esc(t('cat.termsLink'))}</a>` : ''}
    </div>
    <div class="catalog-cell catalog-cell-state">
      <span class="badge status-badge ${stateClass}" title="${esc(accessNote || '')}">${esc(stateLabel)}</span>
      <div class="text-muted">${esc(t('cat.age'))}: ${esc(age === null || age === undefined ? t('cat.ageNone') : relativeAge(age))}</div>
      ${!row.collectable ? `<div class="text-muted-warn">${esc(row.not_proxy_source_reason || row.access_blocked_reason || '')}</div>` : ''}
    </div>
    <div class="catalog-cell catalog-cell-data">${runtimeCell(runtime)}</div>
    <div class="catalog-cell catalog-cell-choice">
      <span class="badge ${choiceClass}">${esc(t(`cat.choice.${row.selection_state}`))}</span>
      <div class="catalog-actions">
        ${row.collectable ? `<button class="button chip" data-catalog-check="${esc(row.id)}" title="${esc(t('cat.action.check'))}">${esc(t('cat.action.check'))}</button>` : ''}
        ${row.selected ? (row.download_disabled
          ? `<button class="button chip" data-catalog-resume="${esc(row.id)}" title="${esc(t('cat.action.resume'))}">${esc(t('cat.action.resume'))}</button>`
          : `<button class="button chip" data-catalog-toggle="${esc(row.id)}" title="${esc(t('cat.action.pause'))}">${esc(t('cat.action.pause'))}</button>`)
          : `<button class="button chip" data-catalog-select="${esc(row.id)}" title="${esc(t('cat.action.include'))}">${esc(t('cat.action.include'))}</button>`}
        ${row.selected ? `<button class="button chip" data-catalog-remove="${esc(row.id)}" title="${esc(t('cat.action.remove'))}">${esc(t('cat.action.remove'))}</button>` : ''}
        ${row.selected ? `<button class="button chip" data-catalog-exclude="${esc(row.id)}" title="${esc(t('cat.action.exclude'))}">⊘</button>` : ''}
        ${row.runtime && row.runtime.error ? `<button class="button chip" data-catalog-recover="${esc(row.id)}" title="${esc(t('cat.action.recover'))}">↻</button>` : ''}
      </div>
    </div>
  </div>`;
}

function renderCatalog(view) {
  catalogData = view;
  $('catalog-revision').textContent = `${view.revision} · ${view.published_at ? String(view.published_at).slice(0, 10) : ''}`;
  renderCatalogFacets(view);
  renderCatalogSets(view);
  renderCatalogGroups(view);
  const rows = view.sources || [];
  $('catalog-list').innerHTML = rows.length
    ? rows.map(catalogRowHtml).join('')
    : `<p class="hint">${esc(t('cat.empty'))}</p>`;
  $('catalog-count').textContent = t('cat.shown', {shown: fmt(rows.length), total: fmt(view.total)});
  $('catalog-more').classList.toggle('hidden', rows.length >= view.total);
  bindCatalogRows();
  if (catalogDetailId) loadCatalogDetail(catalogDetailId);
}

function bindCatalogRows() {
  const handlers = {
    'catalog-details': id => loadCatalogDetail(id),
    'catalog-check': id => previewSource(id),
    'catalog-toggle': id => catalogToggle(id, true),
    'catalog-resume': id => catalogToggle(id, false),
    'catalog-select': id => catalogSelect(id, true),
    'catalog-remove': id => catalogSelect(id, false),
    'catalog-recover': id => catalogAction('/api/sources/recover', {id}, null, 'toast.cat.recovered'),
    'catalog-exclude': id => openScopeDialog(id)
  };
  for (const [action, run] of Object.entries(handlers)) {
    $('catalog-list').querySelectorAll(`[data-${action}]`).forEach(button => {
      button.onclick = () => run(button.dataset[action.replace(/-([a-z])/g, (_, c) => c.toUpperCase())]);
    });
  }
}

async function reloadCatalog() {
  try {
    renderCatalog(await api('/api/source-catalog?' + catalogQuery()));
  } catch (error) {
    $('catalog-list').innerHTML = `<p class="hint">${esc(error.message)}</p>`;
  }
}

function catalogToggle(id, disabled) {
  return catalogAction('/api/sources/toggle', {id, disabled}, null, disabled ? 'toast.cat.paused' : 'toast.cat.resumed');
}

function catalogSelect(id, selected) {
  return catalogAction('/api/sources/select', {id, selected}, null, selected ? 'toast.cat.saved' : 'toast.cat.removed');
}

async function catalogAction(path, body, message, toastKey) {
  try {
    const value = await api(path, body);
    if (value && value.settings) {
      settings = value.settings;
      $('sources').value = settings.sources.join('\n');
      updateSourceCount();
      updateCodeEditors();
    }
    if (toastKey) toast(t(toastKey, {count: fmt((value && (value.members || [1]).length) || 1)}));
    else if (message) toast(message(value));
    await reloadCatalog();
  } catch (error) {
    toast(error.message, true);
  }
}

async function loadCatalogDetail(sourceId) {
  catalogDetailId = sourceId;
  const node = $('catalog-detail');
  node.hidden = false;
  try {
    const row = await api('/api/source-catalog/' + encodeURIComponent(sourceId));
    const runtime = row.runtime || {};
    const evidence = Object.entries(row.evidence || {}).map(([key, value]) =>
      `<tr><td>${esc(key)}</td><td>${esc(value.state || '—')}</td><td>${esc(value.checked_at || '—')}</td></tr>`).join('');
    const history = (row.history || []).map(item => `<tr>
        <td>${esc(new Date((item.ended_at || item.started_at || 0) * 1000).toLocaleString())}</td>
        <td>${esc(item.http_state)}</td><td>${esc(item.parse_state)}</td><td>${esc(item.cache_state)}</td>
        <td>${fmt(item.accepted)} / ${fmt(item.rejected)}</td><td>${esc(item.error || '—')}</td></tr>`).join('');
    node.innerHTML = `
      <div class="dialog-heading">
        <h2>${esc(t('cat.detailTitle'))}: ${esc(row.name)}</h2>
        <button class="button light" data-catalog-close>✕</button>
      </div>
      <p class="hint">${esc(t('cat.noLiveness'))}</p>
      <div class="catalog-detail-grid">
        <div>
          <h3 data-i18n="cat.evidence">Research evidence</h3>
          <table><tbody>${evidence}</tbody></table>
          <h3 data-i18n="cat.rights">Terms and data license</h3>
          <p class="hint">${esc(t('cat.checkedOn'))}: ${esc((row.rights || {}).checked_at || row.checked_at || '—')}</p>
          <p class="hint">data_license: ${esc((row.rights || {}).data_license || 'unknown')} · code_license: ${esc((row.rights || {}).code_license || 'unknown')}</p>
          ${row.terms_url ? `<a class="catalog-terms" href="${esc(row.terms_url)}" target="_blank" rel="noopener">${esc(t('cat.termsLink'))}</a>` : ''}
        </div>
        <div>
          <h3 data-i18n="cat.cache">Stored data</h3>
          <p class="hint">${esc(t('cat.cacheAge'))}: ${esc(runtime.last_good_age_seconds === null || runtime.last_good_age_seconds === undefined ? t('cat.ageNone') : relativeAge(runtime.last_good_age_seconds))}
             · ${esc(t('cat.cacheRecords'))}: ${fmt((row.cache || {}).last_good ? (row.cache.last_good.record_count || 0) : 0)}</p>
          <p class="hint">HTTP ${esc(runtime.http_state || '—')} · ${esc(runtime.parse_state || '—')} · ${esc(runtime.cache_state || '—')}</p>
          ${runtime.error ? `<p class="catalog-err">${esc(t('cat.error'))}: ${esc(runtime.error)}</p>` : ''}
          ${runtime.etag || runtime.last_modified ? `<p class="hint">ETag: ${esc(runtime.state_etag || '—')} · Last-Modified: ${esc(runtime.state_last_modified || '—')}</p>` : ''}
        </div>
      </div>
      <h3 data-i18n="cat.history">Last observations</h3>
      ${history ? `<table><thead><tr><th>${esc(t('cat.time'))}</th><th>HTTP</th><th>${esc(t('cat.col.format'))}</th><th>${esc(t('cat.cache'))}</th><th>${esc(t('cat.accepted'))} / ${esc(t('cat.rejected'))}</th><th>${esc(t('cat.error'))}</th></tr></thead><tbody>${history}</tbody></table>`
        : `<p class="hint">${esc(t('cat.noHistory'))}</p>`}`;
    node.querySelector('[data-catalog-close]').onclick = () => { node.hidden = true; catalogDetailId = null; };
    applyI18n(node);
  } catch (error) {
    node.innerHTML = `<p class="catalog-err">${esc(error.message)}</p>`;
  }
}

function previewHtml(row) {
  const reasons = Object.entries(row.reject_reasons || {}).map(([reason, count]) => `<span class="badge subtle">${esc(reason)}: ${fmt(count)}</span>`).join(' ');
  return `<div class="catalog-preview-inner">
    <strong>${esc(t('cat.previewTitle', {name: row.name || row.source_id}))}</strong>
    <p class="hint">${esc(t('cat.previewNote'))}</p>
    <p>${esc(t('cat.recognized'))}: <b>${fmt(row.recognized)}</b> · ${esc(t('cat.accepted'))}: <b>${fmt(row.accepted)}</b> · ${esc(t('cat.rejected'))}: <b>${fmt(row.rejected)}</b></p>
    ${reasons ? `<p>${esc(t('cat.reasons'))}: ${reasons}</p>` : ''}
    ${row.truncated ? `<p class="text-muted-warn">${esc(t('cat.previewTruncated', {bytes: fmt((row.limits || {}).max_bytes), records: fmt((row.limits || {}).max_candidates)}))}</p>` : ''}
    <p class="hint">HTTP ${esc(row.http_state)} · ${esc(row.parse_state)} · ${esc(row.cache_state)} · ${esc(row.format || '—')} · ${esc(t('cat.pages'))} ${fmt(row.pages)}${row.error ? ` · <span class="catalog-err">${esc(row.error)}</span>` : ''}</p>
    ${(row.sample || []).length ? `<pre class="catalog-sample">${esc(row.sample.join('\n'))}</pre>` : ''}
  </div>`;
}

async function previewSource(sourceId) {
  const node = $('catalog-list');
  try {
    const row = await api('/api/sources/check', {id: sourceId});
    toast(t('cat.accepted') + ': ' + fmt(row.accepted));
    await loadCatalogDetail(sourceId);
    const detail = $('catalog-detail');
    const extra = document.createElement('div');
    extra.innerHTML = previewHtml(row);
    detail.prepend(extra);
    applyI18n(extra);
  } catch (error) {
    toast(t('cat.previewFailed', {reason: error.message}), true);
  }
}

function openScopeDialog(sourceId) {
  const dialog = $('scope-dialog');
  $('scope-title').textContent = t('cat.excludeTitle', {id: sourceId});
  $('scope-body').innerHTML = `<p class="hint">${esc(t('cat.excludeBody'))}</p>
    <label class="check-label"><input type="checkbox" id="scope-shared"><span>${esc(t('cat.excludeShared'))}</span></label>
    <div class="button-row">
      <button class="button primary" id="scope-confirm">${esc(t('cat.action.exclude'))}</button>
      <button class="button light" id="scope-cancel">${esc(t('common.cancel'))}</button>
    </div>`;
  $('scope-cancel').onclick = () => dialog.close();
  $('scope-confirm').onclick = async () => {
    try {
      const value = await api('/api/sources/exclude-scope', {id: sourceId, confirm: true, include_shared: $('scope-shared').checked});
      dialog.close();
      toast(t('cat.excludeDone', {count: fmt(value.excluded), total: fmt(value.delivered)}));
    } catch (error) {
      toast(error.message, true);
    }
  };
  dialog.showModal();
}
$('close-scope').onclick = () => $('scope-dialog').close();

$('catalog-refresh').onclick = async () => {
  const button = $('catalog-refresh');
  const spinner = button.querySelector('.btn-spinner');
  const label = button.querySelector('.btn-label');
  const note = $('catalog-update-note');
  button.disabled = true;
  if (spinner) spinner.classList.remove('hidden');
  note.hidden = false;
  note.textContent = t('cat.updating');
  try {
    await api('/api/sources/refresh', {});
    for (let attempt = 0; attempt < 60; attempt += 1) {
      const job = await api('/api/sources/update-status');
      if (job.stage === 'downloading') note.textContent = t('cat.updateStage.downloading');
      if (job.stage === 'validating') note.textContent = t('cat.updateStage.validating');
      if (!job.running) {
        if (job.error) {
          note.textContent = t('cat.updateFailed', {reason: serverText(job.error)});
          toast(note.textContent, true);
        } else if (job.not_modified) {
          note.textContent = t('cat.updateNotModified');
          toast(note.textContent);
        } else {
          note.textContent = t('cat.updateDone', {revision: job.revision, added: fmt(job.added), changed: fmt(job.changed), retired: fmt(job.retired)});
          toast(note.textContent);
        }
        break;
      }
      await new Promise(resolve => setTimeout(resolve, 400));
    }
    await reloadCatalog();
  } catch (error) {
    note.textContent = t('cat.updateFailed', {reason: error.message});
    toast(note.textContent, true);
  } finally {
    button.disabled = false;
    if (spinner) spinner.classList.add('hidden');
    if (label) label.textContent = t('cat.update');
  }
};

for (const id of ['catalog-q', 'catalog-state', 'catalog-category', 'catalog-protocol', 'catalog-format', 'catalog-access', 'catalog-set-filter']) {
  const node = $(id);
  if (!node) continue;
  node.oninput = () => { catalogLimit = CATALOG_PAGE; clearTimeout(catalogTimer); catalogTimer = setTimeout(reloadCatalog, 250); };
  node.onchange = () => { catalogLimit = CATALOG_PAGE; reloadCatalog(); };
}
$('catalog-more').onclick = () => { catalogLimit += CATALOG_PAGE; reloadCatalog(); };

$('catalog-add-toggle').onclick = () => { $('catalog-add-form').hidden = !$('catalog-add-form').hidden; };
$('catalog-add-kind').innerHTML = SOURCE_FORMATS.map(value => `<option value="${esc(value)}">${esc(value)}</option>`).join('');

function addPayload() {
  return {url: $('catalog-add-url').value.trim(), kind: $('catalog-add-kind').value,
          allow_private: $('catalog-add-private').checked};
}

$('catalog-add-preview').onclick = async () => {
  const button = $('catalog-add-preview');
  button.disabled = true;
  try {
    const row = await api('/api/sources/preview', addPayload());
    $('catalog-add-result').innerHTML = previewHtml(row);
  } catch (error) {
    $('catalog-add-result').innerHTML = `<p class="catalog-err">${esc(t('cat.previewFailed', {reason: error.message}))}</p>`;
  } finally {
    button.disabled = false;
  }
};
$('catalog-add-submit').onclick = async () => {
  try {
    const value = await api('/api/sources/add', {url: $('catalog-add-url').value.trim(), kind: $('catalog-add-kind').value});
    toast(t('cat.addDone', {id: value.id}));
    $('catalog-add-url').value = '';
    $('catalog-add-result').innerHTML = '';
    await reloadCatalog();
  } catch (error) {
    toast(error.message, true);
  }
};

$('lang-toggle').onclick = () => { lang = lang === 'ru' ? 'en' : 'ru'; try { localStorage.setItem(LANG_KEY, lang); } catch {} renderLang(); };
const sidebarLangBtn = $('sidebar-lang-toggle');
if (sidebarLangBtn) sidebarLangBtn.onclick = $('lang-toggle').onclick;

setupCountryComboboxes();
setupFieldPresetChips();
setupEnhancedListeners();

renderLang();
requestAnimationFrame(updateSegmentedGlider);
setTimeout(updateSegmentedGlider, 100);

(async () => {
  try {
    fill(await api('/api/settings'));
    await poll();
    setInterval(poll, 2000);
  } catch (error) {
    toast(error.message, true);
  }
})();
