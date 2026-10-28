// The browser half of tools/gui_check.py: drives headless Chrome over the DevTools protocol.
// Started by gui_check.py, which serves the panel at GUI_URL and the stub's /_harness routes.
import {spawn} from 'node:child_process';
import {existsSync, mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';

const URL_ = process.env.GUI_URL;
const ONLY = process.env.GUI_ONLY || '';
const SHOTS = process.env.GUI_SHOTS || '';
const PROFILE = process.env.GUI_PROFILE;
const ECHO_MS = 1000;
const POLL_MS = 250;
// A click's look must hold from here on: the optimistic flip, the echo and two polls after it.
const SETTLED_MS = 400;
const WATCH_MS = 5000;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---- Chrome and the protocol --------------------------------------------------------------

mkdirSync(PROFILE, {recursive: true});
const chrome = spawn(process.env.GUI_CHROME, [
    '--headless=new', '--remote-debugging-port=0', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--disable-background-timer-throttling',
    '--disable-renderer-backgrounding', `--user-data-dir=${PROFILE}`, '--window-size=1280,900',
    'about:blank'], {stdio: 'ignore'});
const portFile = join(PROFILE, 'DevToolsActivePort');
let devtools;
for (let i = 0; i < 100 && !devtools; i++) {
    await sleep(100);
    if (existsSync(portFile)) {
        const port = readFileSync(portFile, 'utf8').split('\n')[0].trim();
        try {
            const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
            devtools = targets.find((t) => t.type === 'page');
        } catch {}
    }
}
if (!devtools) {
    console.log('FAIL could not reach headless Chrome');
    chrome.kill();
    process.exit(2);
}
const ws = new WebSocket(devtools.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener('open', r));
let nextId = 0;
const pending = {};
const listeners = [];
ws.addEventListener('message', (m) => {
    const msg = JSON.parse(m.data);
    if (msg.id && pending[msg.id]) {
        pending[msg.id](msg);
        delete pending[msg.id];
        return;
    }
    listeners.forEach((listen) => listen(msg));
});
const cdp = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++nextId;
    pending[id] = (msg) => (msg.error ? reject(new Error(method + ': ' + msg.error.message))
        : resolve(msg.result));
    ws.send(JSON.stringify({id, method, params}));
});

const network = [];
const consoleLines = [];
// From a navigation's start to its commit the old page is torn down, and Dash reports each
// callback it had in flight as failed: that is the harness navigating, not a page error.
let leaving = false;
let abortedOnLeave = 0;
const ABORTED_CALLBACK = 'Callback failed: the server did not respond';
async function navigate(method, params = {}) {
    leaving = true;
    await cdp(method, params);
}
listeners.push((msg) => {
    if (msg.method === 'Page.frameNavigated' && !msg.params.frame.parentId) {
        leaving = false;
    } else if (leaving && msg.method === 'Runtime.consoleAPICalled'
            && msg.params.args.some((a) => String(a.value ?? a.description).includes(ABORTED_CALLBACK))) {
        abortedOnLeave += 1;
        return;
    }
    if (msg.method === 'Network.requestWillBeSent') {
        const r = msg.params.request;
        if (r.method === 'POST' && r.url.endsWith('/control')) {
            network.push({t: Date.now(), body: r.postData});
        }
    } else if (msg.method === 'Runtime.consoleAPICalled'
            && ['warning', 'error'].includes(msg.params.type)) {
        consoleLines.push(msg.params.type + ': ' + msg.params.args
            .map((a) => a.value ?? a.description).join(' ').slice(0, 300));
    } else if (msg.method === 'Runtime.exceptionThrown') {
        consoleLines.push('exception: ' + (msg.params.exceptionDetails.exception?.description
            || msg.params.exceptionDetails.text).slice(0, 300));
    }
});
await cdp('Network.enable');
await cdp('Runtime.enable');
await cdp('Page.enable');
// Flashes are 650 ms; an observer catches every one a 100 ms sample could miss.
await cdp('Page.addScriptToEvaluateOnNewDocument', {source: `
    window.__flashes = [];
    new MutationObserver((records) => records.forEach((r) => {
        const el = r.target;
        const flashing = !!el.classList && el.classList.contains('cooldown-row')
            && el.classList.contains('flash');
        if (flashing && !el.__flashing) {
            window.__flashes.push({group: el.dataset.group, at: performance.now()});
        }
        el.__flashing = flashing;
    })).observe(document, {subtree: true, attributes: true, attributeFilter: ['class']});
    // The fixture test's keep-alive is held, not scheduled: the harness fires it by hand
    // (window.__tickKeepAlive) instead of waiting 15 s. Every other timer runs as usual.
    window.__pointer = [];
    for (const type of ['pointerdown', 'pointerup', 'click']) {
        document.addEventListener(type, (e) => window.__pointer.push({type, x: e.clientX, y: e.clientY,
            target: (e.target.tagName || '') + '.' + (e.target.getAttribute?.('class') || '') + (e.target.closest?.('[data-node-key]') ? ' in-mark' : ''),
            at: Math.round(performance.now())}), true);
    }
    window.__keepAlive = new Map();
    window.__keepAliveDelays = [];
    const realSetInterval = window.setInterval.bind(window);
    const realClearInterval = window.clearInterval.bind(window);
    let keepAliveId = 1e9;
    window.setInterval = (fn, ms, ...rest) => {
        if (typeof fn === 'function' && String(fn).includes('dispatchTest')) {
            window.__keepAliveDelays.push(ms);
            window.__keepAlive.set(++keepAliveId, fn);
            return keepAliveId;
        }
        return realSetInterval(fn, ms, ...rest);
    };
    window.clearInterval = (id) => (window.__keepAlive.delete(id) ? undefined : realClearInterval(id));
    window.__tickKeepAlive = () => { window.__keepAlive.forEach((fn) => fn()); return window.__keepAlive.size; };
`});

async function ev(expression) {
    const res = await cdp('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
    if (res.exceptionDetails) {
        throw new Error('page: ' + (res.exceptionDetails.exception?.description
            || res.exceptionDetails.text));
    }
    return res.result.value;
}

const harness = async (path, body) => {
    const res = await fetch(URL_ + '_harness/' + path, body === undefined ? {} : {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    return res.json();
};
const commands = () => harness('commands');

async function waitFor(expression, ms = 8000) {
    const end = Date.now() + ms;
    while (Date.now() < end) {
        if (await ev(expression)) {
            return true;
        }
        await sleep(50);
    }
    return false;
}

// Readable state of every control, for failures.
const DOM_JS = `(() => {
    const q = (s) => document.querySelector(s);
    const cls = (s) => (q(s) || {}).className;
    const text = (s) => ((q(s) || {}).textContent || '').trim();
    const checked = (s) => { const i = document.querySelector(s + ' label[aria-selected="true"] input'); return i ? i.value : null; };
    return {
        intensity: checked('#control-intensity'), intensitySet: cls('#control-intensity-set'),
        intensityAuto: cls('#control-intensity-auto'), fxAuto: cls('#control-fx-auto'),
        fxOff: cls('#control-fx-off'), toggle: text('#control-toggle') + ' ' + cls('#control-toggle'),
        master: text('#control-brightness-value'), status: text('#control-status'),
        staticStatus: text('#static-status'), fx: text('#status-fx .status-text'),
        mode: text('#control-mode .tab--selected'), target: checked('#static-target'),
        fxRow: checked('#static-fx-row'),
        chips: [...document.querySelectorAll('#static-control button')].map((b) => b.id.replace('static-', '') + '=' + b.className).join(' '),
        banner: (q('#static-banner') || {}).style?.display + ' ' + text('#static-banner-text'),
    };
})()`;
const dom = () => ev(DOM_JS);

// ---- checks -------------------------------------------------------------------------------

const results = [];
let failed = 0;
async function check(control, action, ok, detail = {}) {
    results.push({control, action, ok});
    if (ok) {
        console.log(`PASS ${control} :: ${action}`);
        return;
    }
    failed += 1;
    console.log(`FAIL ${control} :: ${action}`);
    for (const [key, value] of Object.entries(detail)) {
        console.log(`     ${key}: ${typeof value === 'string' ? value : JSON.stringify(value)}`);
    }
    console.log(`     dom: ${JSON.stringify(await dom().catch((e) => String(e)))}`);
    console.log(`     last pointer events: ${JSON.stringify(await ev('(window.__pointer || []).slice(-4)').catch((e) => String(e)))}`);
}

const sub = (whole, part) => part === undefined || (part !== null && typeof part === 'object'
    ? whole !== null && typeof whole === 'object' && Object.entries(part).every(([k, v]) => sub(whole[k], v))
    : Math.abs(whole - part) < 1e-6 || whole === part);

let mark = 0;
let netMark = 0;
async function since() {
    const all = await commands();
    return all.slice(mark).map((c) => c.cmd);
}
async function arm() {
    mark = (await commands()).length;
    netMark = network.length;
}

// Runs act, then samples look (a page expression, true when the control shows what the click
// asked for) for WATCH_MS. Expects exactly `expect` bridge commands (each a partial match),
// none after them (expect null: any count), and look true at every sample from SETTLED_MS on.
async function action(control, name, act, {expect = [], look = null, watch = WATCH_MS} = {}) {
    await arm();
    const started = Date.now();
    await act();
    const bad = [];
    let firstBad = null;
    while (Date.now() - started < watch) {
        await sleep(100);
        if (look && Date.now() - started >= SETTLED_MS && !(await ev(look))) {
            bad.push(Date.now() - started);
            if (bad.length === 1) {
                firstBad = await dom();
            }
        }
    }
    const seen = await since();
    if (expect === null) {
        return finish(control, name, look, bad, seen, firstBad);
    }
    const okCount = seen.length === expect.length && expect.every((e, i) => sub(seen[i], e));
    await check(control, name + ' -> ' + expect.length + ' command(s)', okCount,
        {expected: expect, seen, network: network.slice(netMark).map((n) => n.body)});
    return finish(control, name, look, bad, seen, firstBad);
}
async function finish(control, name, look, bad, seen, firstBad) {
    if (look) {
        await check(control, name + ' -> UI holds, no flip-back', bad.length === 0,
            {look, 'false at ms': bad.slice(0, 12).join(',') + (bad.length > 12 ? '...' : ''),
                seen, 'dom at first false': firstBad});
    }
    return seen;
}

async function fresh({blank = false, wait = true, width = 1280} = {}) {
    await harness('reset', {});
    if (blank) {
        await harness('control', {blank: true});
    }
    await cdp('Emulation.setDeviceMetricsOverride', {width, height: 900, deviceScaleFactor: 1,
        mobile: width < 600});
    await navigate('Page.navigate', {url: URL_});
    await waitFor(`!!document.getElementById('control-fx-off')`, 15000);
    if (wait && !blank) {
        await waitFor(`document.getElementById('control-fx-auto').className.includes('active')
            && !document.getElementById('control-intensity-set').disabled`, 15000);
        await sleep(300);
    }
    await arm();
}

async function point(selector, fx = 0.5, fy = 0.5) {
    const p = await ev(`(() => { const e = document.querySelector(${JSON.stringify(selector)});
        if (!e) return null; e.scrollIntoView({block: 'center'}); const b = e.getBoundingClientRect();
        return {x: b.x + b.width * ${fx}, y: b.y + b.height * ${fy}}; })()`);
    if (!p) {
        throw new Error('no element ' + selector);
    }
    return p;
}
async function click(selector, {fx = 0.5, fy = 0.5, count = 1} = {}) {
    const {x, y} = await point(selector, fx, fy);
    await cdp('Input.dispatchMouseEvent', {type: 'mouseMoved', x, y});
    for (let n = 1; n <= count; n++) {
        await cdp('Input.dispatchMouseEvent', {type: 'mousePressed', x, y, button: 'left', clickCount: n});
        await cdp('Input.dispatchMouseEvent', {type: 'mouseReleased', x, y, button: 'left', clickCount: n});
    }
}
const KEYS = {
    Enter: {key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, text: '\r'},
    Space: {key: ' ', code: 'Space', windowsVirtualKeyCode: 32, text: ' '},
    ArrowRight: {key: 'ArrowRight', code: 'ArrowRight', windowsVirtualKeyCode: 39},
    ArrowLeft: {key: 'ArrowLeft', code: 'ArrowLeft', windowsVirtualKeyCode: 37},
    Escape: {key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27},
};
async function key(selector, name) {
    if (selector) {
        await ev(`document.querySelector(${JSON.stringify(selector)}).focus()`);
    }
    const k = KEYS[name];
    await cdp('Input.dispatchKeyEvent', {type: k.text ? 'keyDown' : 'rawKeyDown', ...k});
    await cdp('Input.dispatchKeyEvent', {type: 'keyUp', ...k, text: undefined});
}
async function type(selector, text) {
    await ev(`(() => { const e = document.querySelector(${JSON.stringify(selector)}); e.focus(); e.select(); })()`);
    await cdp('Input.insertText', {text});
}
// The whole page, or one element unclipped by its own scrolling.
async function shot(name, selector = null) {
    if (!SHOTS) {
        return;
    }
    const element = `document.querySelector(${JSON.stringify(selector)})`;
    const clip = selector && await ev(`(() => { const e = ${element}; window.__shotStyle = e.style.cssText;
        e.style.cssText += ';contain:none;overflow:visible;height:auto;align-self:start'; const r = e.getBoundingClientRect();
        return {x: r.x + scrollX, y: r.y + scrollY, width: r.width, height: r.height, scale: 1}; })()`);
    const {data} = await cdp('Page.captureScreenshot', {format: 'png', captureBeyondViewport: true, ...(clip && {clip})});
    if (selector) {
        await ev(`${element}.style.cssText = window.__shotStyle`);
    }
    writeFileSync(join(SHOTS, selector ? `${name}.png` : `gui_${name}.png`), Buffer.from(data, 'base64'));
}

// ---- suites -------------------------------------------------------------------------------

const LEVELS = {calm: 0.3, balanced: 0.7, wild: 1};
const LEVEL_INDEX = {calm: 1, balanced: 2, wild: 3};
const levelLabel = (name) => `#control-intensity label:nth-child(${LEVEL_INDEX[name]})`;
const shows = (name) => `document.querySelector('#control-intensity label[aria-selected="true"] input')?.value === '${LEVELS[name]}'`;
const has = (id, c) => `document.getElementById('${id}').classList.contains('${c}')`;
const not = (e) => `!(${e})`;
const and = (...e) => e.map((x) => `(${x})`).join(' && ');
const text = (selector) => ev(`(document.querySelector(${JSON.stringify(selector)})?.textContent || '').trim()`);
const shown = (id) => `getComputedStyle(document.getElementById('${id}')).display !== 'none'`;
const tab = (label) => click(`#dashboard-tab .tab:nth-child(${['Control', 'Telemetry', 'Config'].indexOf(label) + 1})`);
const modeTab = (mode) => click(`#control-mode .tab:nth-child(${mode === 'auto' ? 1 : 2})`);
const keysOf = (c) => Object.keys(c || {}).sort().join(',');
const flashes = () => ev('window.__flashes.splice(0)');
// The chosen level's fill, and a #show-control colour token resolved the same way.
const levelFill = () => ev(`getComputedStyle(document.querySelector('#control-intensity .selected .dash-options-list-option-text')).backgroundColor`);
const tokenColour = (name) => ev(`(() => { const probe = document.createElement('div');
    probe.style.background = 'var(${name})'; document.getElementById('show-control').appendChild(probe);
    const colour = getComputedStyle(probe).backgroundColor; probe.remove(); return colour; })()`);

const overflow = () => ev(`(() => {
    const w = document.documentElement.clientWidth;
    const wide = [...document.querySelectorAll('body *')].filter((e) => e.getClientRects().length
        && e.getBoundingClientRect().right > w + 1 && !e.closest('[style*="overflow: hidden"], .marquee'))
        .slice(0, 4).map((e) => (e.id ? '#' + e.id : e.tagName.toLowerCase() + '.' + e.className) + ' right=' + Math.round(e.getBoundingClientRect().right));
    return {scroll: document.documentElement.scrollWidth, width: w, wide};
})()`);

// Clicks a slider's track at a fraction of its width.
const slider = (selector, fraction) => click(selector, {fx: fraction});

// ---- fixture configuration helpers --------------------------------------------------------

const STATUS = '#fixture-status';
const UNSAVED = 'Unsaved changes';
const SCENE_XML = 'GeneralSceneDescription.xml';
const APPLY_LABEL = 'Restart show';
const APPLY_ARMED = 'Click again to restart; lights go dark until it is back';
const STALE_RIG = 'The show runs an older rig. Restart show loads the saved patch.';
const NEW_GROUP = '\u0000new';
const rigFiles = () => harness('rig');
const cfg = (name) => `#fixture-info [data-cfg="${name}"]`;
const testButton = (spec) => `#fixture-info [data-cfg-test="${spec}"]`;
// Sorted keys on both sides: Flask sorts the stub's, the expectations are written by hand.
const canon = (value) => JSON.stringify(value, (key, v) => (v && typeof v === 'object' && !Array.isArray(v)
    ? Object.fromEntries(Object.entries(v).sort(([a], [b]) => (a < b ? -1 : 1))) : v));
const same = (a, b) => canon(a) === canon(b);
const lightOn = (target, colour, motion) => ({action: 'highlight', target, colour, sweep: motion === 'sweep', aim: motion === 'aim'});
// The frozen room's MVR uuids: tests target a fixture by its uuid, whatever its name is now.
const ROOM_UUID = {
    spot_left: '75D557BC-32F9-592A-810D-652F0ECF68D7',
    spot_right: '0BD68C96-850C-57EA-86E0-1780E40FF826',
    zq06017: 'B3F12D06-678A-51FE-AD71-BC2BC260EB9F',
};
const CLEAR = {action: 'highlight', target: null};
const posted = () => network.slice(netMark).map((n) => JSON.parse(n.body));
const markOf = (name) => `[...document.querySelectorAll('#mvr-plan .mvr-object')].find((g) =>
    (g.querySelector('title')?.textContent || '').split(' · ')[0] === ${JSON.stringify(name)})`;
const takeActions = () => ev('window.__actions.splice(0)');
const keepAlive = () => ev('({held: window.__keepAlive.size, delays: window.__keepAliveDelays.slice()})');
const statusText = () => text(STATUS);
const noteOf = (name) => ev(`(document.querySelector('${cfg(name)}')?.closest('.cfg-field')
    ?.querySelector('.cfg-note')?.textContent || '').trim()`);

// The Config tab on a page booted against the original rig: the store is restored first, as
// the panel's layout carries the draft the server loaded at start.
async function cfgFresh({width = 1280} = {}) {
    await harness('rig', {restore: true});
    await fresh({width});
    await tab('Config');
    await waitFor(`!!document.querySelector('#mvr-plan svg.plan')`, 8000);
    await planSettled();
    // Records every draft action the page sends (fixture_patch.js sendAction).
    await ev(`(() => {
        window.__actions = [];
        const ds = window.dash_clientside;
        const real = ds.set_props;
        ds.set_props = function (id, props) {
            if (id === 'fixture-grid-action') window.__actions.push(props.data);
            return real.apply(this, arguments);
        };
    })()`);
    await arm();
}

// The Config tab can still move the plan after it opens (fonts, the DMX map filling in); a
// click aimed before that lands beside the mark.
async function planSettled() {
    let last = null;
    for (let stable = 0, i = 0; stable < 3 && i < 100; i++) {
        const now = await ev(`(() => { const r = document.querySelector('#mvr-plan svg.plan')?.getBoundingClientRect();
            return r ? [r.x, r.y + scrollY, r.width, r.height].map(Math.round).join() : null; })()`);
        stable = now && now === last ? stable + 1 : 0;
        last = now;
        await sleep(100);
    }
}

async function selectFixture(name) {
    await planSettled();
    await ev(`(() => {
        document.querySelectorAll('[data-harness-mark]').forEach((e) => e.removeAttribute('data-harness-mark'));
        ${markOf(name)}.querySelector('circle').setAttribute('data-harness-mark', '');
    })()`);
    await click('[data-harness-mark]');
    const ok = await waitFor(`document.querySelector('${cfg('name')}')?.value === ${JSON.stringify(name)}`, 5000);
    await sleep(300);
    const sent = await takeActions();
    if (!ok) {
        console.log('     selectFixture', name, 'sent', JSON.stringify(sent), JSON.stringify(await ev('window.__pointer.slice(-6)')),
            JSON.stringify(await ev(`(() => { const r = document.querySelector('[data-harness-mark]')?.getBoundingClientRect(); return r && [r.x, r.y, r.width, scrollY, performance.now()]; })()`)));
    }
    return ok;
}

// The fixture's plan mark in screen and plan coordinates; the plan's SVG x is MVR X.
async function planMark(name) {
    return ev(`(() => {
        const mark = ${markOf(name)};
        if (!mark) return null;
        mark.scrollIntoView({block: 'center'});
        const circle = mark.querySelector('circle');
        const m = circle.ownerSVGElement.getScreenCTM();
        const [cx, cy] = [Number(circle.getAttribute('cx')), Number(circle.getAttribute('cy'))];
        return {cx, cy, a: m.a, d: m.d, x: m.a * cx + m.c * cy + m.e, y: m.b * cx + m.d * cy + m.f,
            ring: circle.classList.contains('ring')};
    })()`);
}
const areaBox = () => ev(`(() => { const r = document.querySelector('#mvr-plan .plan-areas rect');
    return r && ['x', 'y', 'width', 'height'].map((k) => Number(r.getAttribute(k))); })()`);

const mouse = (type, x, y, extra = {}) => cdp('Input.dispatchMouseEvent', {type, x, y, button: 'left', ...extra});
async function pressAndMove(x0, y0, x1, y1, steps = 8) {
    await mouse('mouseMoved', x0, y0, {button: 'none'});
    await mouse('mousePressed', x0, y0, {clickCount: 1, buttons: 1});
    for (let i = 1; i <= steps; i++) {
        await mouse('mouseMoved', x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps, {buttons: 1});
        await sleep(30);
    }
    // One animation frame draws the preview.
    await sleep(150);
}
const release = (x, y) => mouse('mouseReleased', x, y, {clickCount: 1});

async function typeInto(selector, value) {
    await ev(`(() => { const e = document.querySelector(${JSON.stringify(selector)}); e.focus(); e.select(); })()`);
    await cdp('Input.insertText', {text: value});
    // The page edits on change, which a text field fires when it loses focus.
    await ev('document.activeElement.blur()');
}
async function choose(selector, value) {
    await ev(`(() => { const e = document.querySelector(${JSON.stringify(selector)}); e.focus();
        e.value = ${JSON.stringify(value)}; e.dispatchEvent(new Event('change', {bubbles: true})); e.blur(); })()`);
}
const splitInput = (name, input) => `${cfg(name)} input[name="${input}"]`;

// One form edit: exactly one draft action matching `expected` (partial), the draft taking it.
async function formEdit(what, act, expected) {
    await takeActions();
    await act();
    await waitFor('window.__actions.length > 0', 3000);
    await sleep(500);
    const sent = await takeActions();
    const applied = await waitFor(`document.querySelector('${STATUS}').textContent.startsWith('${UNSAVED}')`, 3000);
    await check('patch form', `${what} -> one draft action`, sent.length === 1 && sub(sent[0], expected),
        {expected, sent});
    await check('patch form', `${what} -> the draft takes it`, applied, {status: await statusText()});
    return sent[0];
}
// One refused form edit: no draft action and the reason beside the field.
async function formRefusal(what, act, field, reason) {
    await takeActions();
    await act();
    await sleep(800);
    const sent = await takeActions();
    const note = await noteOf(field);
    const invalid = await ev(`document.querySelector('${cfg(field)}').getAttribute('aria-invalid') === 'true'`);
    await check('patch form', `${what} -> refused, says "${reason}"`, sent.length === 0 && note === reason && invalid,
        {sent, note, invalid});
}

async function save(expectSaved = true) {
    await click('#fixture-save');
    const want = expectSaved ? 'Patch saved' : 'Patch not saved';
    const ok = await waitFor(`document.querySelector('${STATUS}').textContent.startsWith('${want}')
        && document.getElementById('config-panel').getAttribute('aria-busy') === 'false'`, 15000);
    return ok;
}
async function reloadSaved() {
    await click('#fixture-reload');
    return waitFor(`document.querySelector('${STATUS}').textContent === 'Saved patch loaded.'`, 15000);
}
const fixtureIn = (files, name) => files.fixtures.find((f) => f.name === name);
// A fixture's element in the scene XML, from its name to its closing tag.
function fixtureXml(xml, name) {
    const start = xml.lastIndexOf('<Fixture ', xml.indexOf(`name="${name}"`));
    return xml.slice(start, xml.indexOf('</Fixture>', start) + '</Fixture>'.length);
}
// Every highlight target any page posted must be in the running rig (Starfire stops the show
// on an unknown fixture; the bridge refusing it is the last line, not the first).
async function checkHighlightIds(scenario) {
    const rig = (await harness('state')).rig;
    const ids = new Set(rig.fixtures.flatMap((f) => [`fixture:${f.fixture_uuid}`, ...f.cells.map((c) => `cell:${c}`)]));
    const bad = network.slice(scenarioNet).map((n) => JSON.parse(n.body))
        .filter((c) => c.action === 'highlight' && c.target)
        .filter((c) => !ids.has(Object.entries(c.target).map(([k, v]) => `${k}:${v}`)[0]));
    await check(scenario, 'no highlight posted with an id outside rig.fixtures', bad.length === 0, {bad});
}
let scenarioNet = 0;
// Every text the test note shows from now on, however briefly.
const watchNote = () => ev(`(() => {
    window.__notes = [];
    const note = document.querySelector('#fixture-info .cfg-test-note');
    new MutationObserver(() => window.__notes.push(note.textContent))
        .observe(note, {childList: true, characterData: true, subtree: true});
})()`);

const SUITES = {
    control: async () => {
        await fresh();
        await shot('control');
        await action('polls', 'idle 5 s after load', async () => {});
        const captions = await ev(`[...document.querySelectorAll('#show-status .control-label')].map((e) => e.textContent)`);
        const want = ['Health', 'Autoloop', 'Colors', 'FX', 'BPM', 'Render FPS'];
        await check('status line', 'shows every field', want.every((c) => captions.includes(c)),
            {captions, want});
        await check('status line', 'BPM 124',
            (await ev(`document.getElementById('show-status').textContent`)).includes('BPM124'),
            {status: await text('#show-status')});
        await check('FX row', 'reads "-" while idle', (await text('#status-fx .status-text')) === '-',
            {fx: await text('#status-fx')});
        const bars = await ev(`[...document.querySelectorAll('#decoder div[style*="width"]')].map((e) => e.style.width)`);
        await check('Section Confidence', 'one bar per class at its posterior',
            JSON.stringify(bars) === JSON.stringify(['10%', '30%', '60%']), {bars});

        // The stub reselects the loop on a level change, as the engine does.
        const autoloopText = `[...document.querySelectorAll('#show-status .status-item')]
            .find((e) => e.querySelector('.control-label')?.textContent === 'Autoloop')?.textContent`;
        const autoloopBefore = await ev(autoloopText);
        await click(levelLabel('calm'));
        await check('status Autoloop', 'calm changes the autoloop label within two polls',
            await waitFor(`(${autoloopText} || '').includes('Afterglow')`, ECHO_MS + 2 * POLL_MS),
            {before: autoloopBefore, after: await ev(autoloopText)});
        const autoloopDot = await ev(`[...document.querySelectorAll('#show-status .status-item')]
            .find((e) => e.querySelector('.control-label')?.textContent === 'Autoloop')
            ?.querySelector('.dot')?.style.background`);
        await check('status Autoloop', 'afterglow under DROP shows the breakdown colour',
            autoloopDot === 'rgb(123, 31, 162)', {dot: autoloopDot});
        await fresh();
        for (const name of ['calm', 'wild', 'balanced']) {
            await action(`intensity ${name}`, 'click', () => click(levelLabel(name)),
                {expect: [{action: 'intensity', level: LEVELS[name]}], look: shows(name)});
        }
        await action('intensity balanced', 'click the level already lit', () => click(levelLabel('balanced')),
            {look: shows('balanced')});
        await check('intensity balanced', 'chosen by hand, it wears its own colour',
            (await levelFill()) === (await tokenColour('--balanced')),
            {fill: await levelFill(), balanced: await tokenColour('--balanced')});
        const autoOn = and(has('control-intensity-auto', 'active'), has('control-intensity-set', 'following'));
        await action('intensity AUTO', 'click on', () => click('#control-intensity-auto'),
            {expect: [{action: 'intensity_auto', enabled: true}], look: autoOn});
        await check('intensity AUTO', 'the followed level wears the follow colour',
            (await levelFill()) === (await tokenColour('--follow')),
            {fill: await levelFill(), follow: await tokenColour('--follow')});
        await action('intensity calm', 'click while AUTO is on', () => click(levelLabel('calm')),
            {expect: [{action: 'intensity', level: LEVELS.calm}], look: and(shows('calm'), not(autoOn))});
        await action('intensity AUTO', 'Enter', () => key('#control-intensity-auto', 'Enter'),
            {expect: [{action: 'intensity_auto', enabled: true}], look: autoOn});
        await action('intensity AUTO', 'Space', () => key('#control-intensity-auto', 'Space'),
            {expect: [{action: 'intensity_auto', enabled: false}], look: not(autoOn)});
        await action('intensity wild', 'Space on its radio', () => key(`${levelLabel('wild')} input`, 'Space'),
            {expect: [{action: 'intensity', level: LEVELS.wild}], look: shows('wild')});
        await action('intensity balanced', 'ArrowLeft from wild', () => key(null, 'ArrowLeft'),
            {expect: [{action: 'intensity', level: LEVELS.balanced}], look: shows('balanced')});
        await action('intensity calm', 'Enter on its radio', () => key(`${levelLabel('calm')} input`, 'Enter'),
            {expect: [{action: 'intensity', level: LEVELS.calm}], look: shows('calm')});
        await action('intensity wild', 'double-click', () => click(levelLabel('wild'), {count: 2}),
            {expect: [{action: 'intensity', level: LEVELS.wild}], look: shows('wild')});
        // A double-click is one operator action: the second click may land before Dash runs
        // the first, so one or two toggles are both right as long as the UI ends where the show does.
        const dbl = await action('intensity AUTO', 'double-click', () => click('#control-intensity-auto', {count: 2}),
            {expect: null});
        const endsOn = dbl.length === 1;
        await check('intensity AUTO', 'double-click sends one toggle or a pair and the UI agrees',
            (dbl.length === 1 && dbl[0].enabled === true && await ev(autoOn))
            || (dbl.length === 2 && dbl[1].enabled === false && await ev(not(autoOn))), {seen: dbl});
        if (endsOn) {
            await action('intensity AUTO', 'click off', () => click('#control-intensity-auto'),
                {expect: [{action: 'intensity_auto', enabled: false}], look: not(autoOn)});
        }
        await action('intensity', 'rapid calm, wild, calm, balanced', async () => {
            for (const name of ['calm', 'wild', 'calm', 'balanced']) {
                await click(levelLabel(name));
                await sleep(120);
            }
        }, {expect: ['calm', 'wild', 'calm', 'balanced'].map((n) => ({action: 'intensity', level: LEVELS[n]})),
            look: shows('balanced'), watch: 5500});

        const fxOff = and(has('control-fx-off', 'active'), not(has('control-fx-auto', 'active')));
        const fxAuto = and(has('control-fx-auto', 'active'), not(has('control-fx-off', 'active')));
        await action('FX Off', 'click', () => click('#control-fx-off'),
            {expect: [{action: 'fx_auto', enabled: false}], look: fxOff});
        await action('FX Auto', 'click', () => click('#control-fx-auto'),
            {expect: [{action: 'fx_auto', enabled: true}], look: fxAuto});
        await action('FX Off', 'Enter', () => key('#control-fx-off', 'Enter'),
            {expect: [{action: 'fx_auto', enabled: false}], look: fxOff});
        await action('FX Auto', 'Space', () => key('#control-fx-auto', 'Space'),
            {expect: [{action: 'fx_auto', enabled: true}], look: fxAuto});
        await action('FX Off', 'double-click', () => click('#control-fx-off', {count: 2}),
            {expect: null, look: fxOff});
        await action('FX', 'rapid Auto, Off, Auto', async () => {
            for (const id of ['control-fx-auto', 'control-fx-off', 'control-fx-auto']) {
                await click('#' + id);
                await sleep(120);
            }
        }, {expect: [true, false, true].map((enabled) => ({action: 'fx_auto', enabled})), look: fxAuto});

        await action('pause/play', 'click pause', () => click('#control-toggle'),
            {expect: [{action: 'pause'}], look: has('control-toggle', 'paused')});
        await action('pause/play', 'click play', () => click('#control-toggle'),
            {expect: [{action: 'play'}], look: has('control-toggle', 'playing')});
        await action('pause/play', 'Space', () => key('#control-toggle', 'Space'),
            {expect: [{action: 'pause'}], look: has('control-toggle', 'paused')});
        await action('pause/play', 'Enter', () => key('#control-toggle', 'Enter'),
            {expect: [{action: 'play'}], look: has('control-toggle', 'playing')});

        const fader = '#show-control .control-transport .fader-track';
        const seen = await action('master fader', 'click the track at 30%', () => slider(fader, 0.3),
            {expect: [{action: 'master'}]});
        const level = seen[0]?.level;
        await check('master fader', 'level lands near 30%', Math.abs(level - 0.3) < 0.06, {seen});
        await check('master fader', 'readout shows the sent level',
            (await text('#control-brightness-value')) === Math.round(level * 100) + '%', {seen});
        await arm();
        const {x: x0, y} = await point(fader, 0.6);
        const {x: x1} = await point(fader, 0.9);
        await cdp('Input.dispatchMouseEvent', {type: 'mousePressed', x: x0, y, button: 'left', clickCount: 1});
        for (let i = 1; i <= 6; i++) {
            await cdp('Input.dispatchMouseEvent', {type: 'mouseMoved', x: x0 + (x1 - x0) * i / 6, y, button: 'left', buttons: 1});
            await sleep(60);
        }
        await cdp('Input.dispatchMouseEvent', {type: 'mouseReleased', x: x1, y, button: 'left', clickCount: 1});
        const readout = [];
        for (let i = 0; i < 50; i++) {
            await sleep(100);
            readout.push(await text('#control-brightness-value'));
        }
        const drag = await since();
        const last = drag.at(-1)?.level;
        await check('master fader', 'drag sends only master commands, ending where it stopped',
            drag.length >= 1 && drag.every((c) => c.action === 'master') && Math.abs(last - 0.9) < 0.06,
            {seen: drag});
        await check('master fader', 'drag readout never flips back',
            readout.slice(4).every((r) => r === Math.round(last * 100) + '%'), {readout: readout.join(' ')});
    },

    cooldowns: async () => {
        await fresh();
        await arm();
        const width = (group) => ev(`parseFloat(document.querySelector('.cooldown-row[data-group="${group}"] .cooldown-fill')?.style.width)`);
        const rows = await ev(`[...document.querySelectorAll('.cooldown-row')].map((r) => r.dataset.group)`);
        await check('FX Effects', 'one bar per group', rows.includes('pars') && rows.includes('spots'), {rows});
        await check('FX Effects spots', 'ready bar is full with "ready"',
            (await ev(`document.querySelector('.cooldown-row[data-group="spots"]').classList.contains('ready')`))
            && (await width('spots')) === 100
            && (await text('.cooldown-row[data-group="spots"] .cooldown-caption')) === 'ready',
            {width: await width('spots')});
        await flashes();
        await harness('control', {fire: 'pars'});
        await sleep(1500);
        const fired = await flashes();
        await check('FX Effects pars', 'flashes once on a fire', fired.filter((f) => f.group === 'pars').length === 1, {fired});
        const w1 = await width('pars');
        await sleep(1000);
        const w2 = await width('pars');
        await check('FX Effects pars', 'fills left to right as it recovers', w1 < w2 && w2 < 100,
            {before: w1, after: w2, caption: await text('.cooldown-row[data-group="pars"] .cooldown-caption')});
        await check('FX Effects pars', 'not ready while it recovers',
            !(await ev(`document.querySelector('.cooldown-row[data-group="pars"]').classList.contains('ready')`)) &&
            /^\d+ s$/.test(await text('.cooldown-row[data-group="pars"] .cooldown-caption')),
            {caption: await text('.cooldown-row[data-group="pars"] .cooldown-caption')});
        await harness('control', {lengthen: ['pars', 20]});
        await sleep(1500);
        const lengthened = await flashes();
        await check('FX Effects pars', 'no flash when its cooldown is lengthened', lengthened.length === 0,
            {flashes: lengthened, rows: await ev(`[...document.querySelectorAll('.cooldown-row')].map((r) => ({...r.dataset}))`)});
        await harness('control', {fire: 'spots', last: 'tilt_wave'});
        await sleep(1500);
        const spots = await flashes();
        await check('FX Effects spots', 'a ready group going busy flashes', spots.filter((f) => f.group === 'spots').length === 1,
            {flashes: spots});
        await sleep(1000);
        await check('FX Effects', 'flash clears', !(await ev(`!!document.querySelector('.cooldown-row.flash')`)));

        await harness('control', {special: {name: 'strobe_chase', group: 'pars'}});
        await check('FX row', 'names a running FX', await waitFor(`document.querySelector('#status-fx .status-text')?.textContent === 'Strobe chase · pars' && document.getElementById('status-fx').classList.contains('fx-running')`, 2000),
            {fx: await text('#status-fx')});
        await harness('control', {special: null});
        await check('FX row', 'back to "-" when it ends', await waitFor(`document.querySelector('#status-fx .status-text')?.textContent === '-'`, 2000),
            {fx: await text('#status-fx')});
        await check('FX Effects', 'no commands from the bars', (await since()).length === 0, {seen: await since()});
    },

    static: async () => {
        await fresh();
        await action('mode tabs', 'STATIC tab', () => modeTab('static'),
            {look: and(shown('static-mode-panel'), not(shown('auto-mode-panel')))});
        const ghosts = and(has('static-clear', 'ghost-button'), has('static-banner-clear', 'ghost-button'));
        for (const preset of ['white', 'warm', 'red', 'blue', 'uv', 'blackout', 'off']) {
            await action(`static ${preset}`, 'click (target All)', () => click(`#static-preset-${preset}`),
                {expect: [{action: 'static', kind: 'preset', preset}], look: has(`static-preset-${preset}`, 'active'), watch: 2500});
            await check('Clear all', `keeps its ghost style after ${preset}`, await ev(ghosts),
                {clear: await ev(`document.getElementById('static-clear').className`),
                    banner: await ev(`document.getElementById('static-banner-clear').className`)});
        }
        await check('Clear all', 'enabled while a static look is on', !(await ev(`document.getElementById('static-clear').disabled`)));
        const pickTarget = (n) => click(`#static-target label:nth-child(${n})`);
        const picker = () => ev(`[...document.querySelectorAll('#static-fx-row label')].map((l) => l.textContent.trim())`);
        await action('static target', 'pars', () => pickTarget(2), {look: `document.querySelector('#static-target label[aria-selected="true"] input')?.value === 'pars'`, watch: 1500});
        await check('static FX picker', 'pars lists its rows', JSON.stringify(await picker()) === '["Colour chase","Strobe chase"]', {rows: await picker()});
        await action('static pars Off', 'click', () => click('#static-preset-off'),
            {expect: [{action: 'static', kind: 'look', target: 'pars', dimmer: 0}], look: has('static-preset-off', 'active'), watch: 2500});
        await action('static pars UV', 'click', () => click('#static-preset-uv'),
            {expect: [{action: 'static', kind: 'look', target: 'pars', dimmer: 1, uv: true}], look: has('static-preset-uv', 'active'), watch: 2500});
        await action('static pars red', 'click', () => click('#static-preset-red'),
            {expect: [{action: 'static', kind: 'look', target: 'pars'}], look: has('static-preset-red', 'active'), watch: 2500});
        await action('static FX picker', 'Colour chase on pars', () => click('#static-fx-row label:nth-child(1)'),
            {expect: [{action: 'static', kind: 'special_fx', row: 'colour_chase', target: 'pars'}],
                look: `document.querySelector('#static-fx-row label[aria-selected="true"] input')?.value === 'colour_chase'`});
        await check('FX row', 'names the held static FX', (await text('#status-fx .status-text')) === 'Static · Colour chase · pars',
            {fx: await text('#status-fx')});
        await action('static FX picker', 'Clear FX', () => click('#static-fx-clear'),
            {expect: [{action: 'static', kind: 'special_fx_clear'}], look: `!document.querySelector('#static-fx-row label[aria-selected="true"]')`});
        await action('static target', 'spots', () => pickTarget(3), {watch: 1500});
        await check('static FX picker', 'spots has none and says so',
            (await picker()).length === 0 && (await text('#static-fx-note')) === 'spots has no FX that fits',
            {rows: await picker(), note: await text('#static-fx-note')});
        await action('static target', 'All', () => pickTarget(1), {watch: 1500});
        await check('static FX picker', 'All lists the rig rows', JSON.stringify(await picker()) === '["Strobe chase"]', {rows: await picker()});
        await shot('static');
        await action('Clear all', 'click', () => click('#static-clear'),
            {expect: [{action: 'static', kind: 'clear'}],
                look: `![...document.querySelectorAll('#static-control button')].some((b) => b.classList.contains('active'))`});
        await check('Clear all', 'keeps its ghost style after Clear all', await ev(ghosts),
            {clear: await ev(`document.getElementById('static-clear').className`)});
        await check('Clear all', 'disabled once the show is back on auto', await waitFor(`document.getElementById('static-clear').disabled`, 2500));

        await action('static red', 'click for the banner', () => click('#static-preset-red'),
            {expect: [{action: 'static', kind: 'preset', preset: 'red'}], watch: 2000});
        await action('mode tabs', 'AUTO tab', () => modeTab('auto'), {look: shown('auto-mode-panel'), watch: 1500});
        await check('overlay banner', 'shown with the look count',
            await waitFor(`${shown('static-banner')} && document.getElementById('static-banner-text').textContent === 'Static overlay active · 1 look'`, 2000),
            {banner: await text('#static-banner')});
        const order = await ev(`(() => { const c = document.getElementById('static-banner-clear').getBoundingClientRect();
            const t = document.getElementById('static-banner-text').getBoundingClientRect(); return {clear: c.right, text: t.left}; })()`);
        await check('overlay banner', 'Clear sits left of the text, ghost style', order.clear <= order.text
            && (await ev(has('static-banner-clear', 'ghost-button'))), {order});
        await action('overlay banner', 'Clear', () => click('#static-banner-clear'),
            {expect: [{action: 'static', kind: 'clear'}], look: not(shown('static-banner'))});
    },

    settings: async () => {
        await fresh();
        await action('Settings', 'open the drawer', () => click('#settings-toggle'), {look: shown('settings-drawer'), watch: 1500});
        const field = '#control-crossfade_beats';
        const before = Number(await ev(`document.querySelector('${field}').value`));
        let seen = await action('settings crossfade', 'type a new value', () => type(field, String(before + 1)),
            {expect: [{action: 'settings', settings: {crossfade_beats: before + 1}}],
                look: `document.querySelector('${field}').value === '${before + 1}'`});
        await check('settings crossfade', 'sends only that field, no auto flags', keysOf(seen[0]?.settings) === 'crossfade_beats', {seen});
        seen = await action('settings quiet brightness', 'type 50 (%)', () => type('#control-quiet_brightness', '50'),
            {expect: [{action: 'settings', settings: {quiet_brightness: 0.5}}]});
        await check('settings quiet brightness', 'sends only that field', keysOf(seen[0]?.settings) === 'quiet_brightness', {seen});
        await action('settings crossfade', 'type "abc"', () => type(field, 'abc'), {watch: 2000});
        await check('settings crossfade', 'says why it refused "abc"',
            (await text('#control-crossfade_beats-error')).startsWith('Rejected'), {error: await text('#control-crossfade_beats-error')});
        await type(field, String(before + 1));
        await sleep(1500);
        const masters = await ev(`[...document.querySelectorAll('#group-masters .setting-name')].map((e) => e.textContent)`);
        await check('group masters', 'one fader per rig group', JSON.stringify(masters) === '["pars group master","spots group master"]', {masters});
        seen = await action('group master pars', 'click the track at 50%', () => slider('#group-masters .setting-row:nth-child(1) .fader-track', 0.5),
            {expect: [{action: 'settings'}]});
        const level = seen[0]?.settings?.group_masters?.pars;
        await check('group master pars', 'sends only group_masters, pars near 50%',
            keysOf(seen[0]?.settings) === 'group_masters' && keysOf(seen[0].settings.group_masters) === 'pars'
            && Math.abs(level - 0.5) < 0.06, {seen});
    },

    load: async () => {
        await fresh({blank: true});
        await sleep(1500);
        await check('first load', 'controls wait for the first snapshot',
            await ev(`document.getElementById('control-intensity-set').disabled && document.getElementById('control-toggle').disabled
                && !document.getElementById('control-fx-auto').classList.contains('active')
                && !document.getElementById('control-fx-off').classList.contains('active')`));
        await action('first load', 'click a disabled level and play/pause', async () => {
            await click(levelLabel('calm'));
            await click('#control-toggle');
        }, {watch: 1500});
        await action('first load', 'first snapshot arrives late', () => harness('control', {blank: false}),
            {look: and(shows('balanced'), has('control-fx-auto', 'active'), has('control-toggle', 'playing'))});

        await fresh({blank: true});
        await action('first load', 'FX Off clicked before the first poll', async () => {
            await click('#control-fx-off');
            // Long enough that blank polls land after SETTLED_MS: the lit chip must survive them.
            await sleep(1000);
            await harness('control', {blank: false});
        }, {expect: [{action: 'fx_auto', enabled: false}], look: has('control-fx-off', 'active')});

        await fresh({wait: false});
        await action('first load', 'FX Off clicked as soon as it renders', () => click('#control-fx-off'),
            {expect: [{action: 'fx_auto', enabled: false}], look: has('control-fx-off', 'active')});

        await fresh();
        const fxOffTop = () => ev(`document.getElementById('control-fx-off').getBoundingClientRect().top`);
        const top = await fxOffTop();
        await harness('control', {shed: {level: 'LIGHT'}});
        const noticed = await waitFor(`document.getElementById('telemetry-alerts').textContent.includes('Analysis is holding')`);
        await check('first load', 'a poll adding a notice leaves FX Off where it was',
            noticed && (await fxOffTop()) === top, {noticed, before: top, after: await fxOffTop()});
        await fresh();
        await flashes();
        await action('tabs', 'Telemetry, Config and back to Control', async () => {
            await tab('Telemetry');
            await sleep(1500);
            await tab('Config');
            await sleep(1500);
            await tab('Control');
        }, {look: null, watch: 6000});
        const seenFlashes = await flashes();
        await check('tabs', 'no spurious FX flash on return', seenFlashes.length === 0, {flashes: seenFlashes});
        await check('tabs', 'Control shows its bars again', await ev(`document.querySelectorAll('.cooldown-row').length === 2 && ${shown('control-panel')}`));
    },

    config: async () => {
        await fresh();
        await action('Config tab', 'open', () => tab('Config'), {watch: 2000});
        await check('rig map', 'renders the plan SVG', await waitFor(`!!document.querySelector('#mvr-plan svg.plan')`, 5000));
        const keys = await ev(`[...document.querySelectorAll('#mvr-plan .plan-legend .plan-key')].map((k) => k.textContent)`);
        await check('rig map', 'has a legend', keys.length > 0, {keys});
        await check('rig map', 'has a scale bar', /\d+(\.\d+)? m$/.test(await text('#mvr-plan .plan-scale')), {scale: await text('#mvr-plan .plan-scale')});
        await shot('config');
        await planSettled();
        await action('rig map', 'select a fixture', () => click('#mvr-plan .mvr-object circle'), {watch: 1500});
        await check('rig map', 'a plan click opens the patch form', await waitFor(`${shown('fixture-info')} && !!document.querySelector('#fixture-info .cfg-section')`, 5000));
        await shot('config_fixture');
        await check('rig map', 'no raw MVR outside the closed Advanced', await ev(`!document.querySelector('#mvr-tree')
            && !/GeneralSceneDescription|ChildList/.test(document.getElementById('config-panel').innerText)`));
        const plan = await ev(`(() => {
            const marks = [...document.querySelectorAll('#mvr-plan .mvr-object:has(circle)')];
            return {head: document.getElementById('mvr-summary').textContent, marks: marks.length,
                badges: marks.filter((m) => /^\\d+( ×\\d+)?$/.test(m.querySelector('.plan-badge')?.textContent)).length,
                counts: [...document.querySelectorAll('#mvr-plan .plan-legend b')].reduce((sum, b) => sum + Number(b.textContent), 0),
                caption: [...document.querySelectorAll('#mvr-plan .plan-caption')].filter((c) => getComputedStyle(c).display !== 'none').map((c) => c.textContent)};
        })()`);
        await check('rig map', 'header, legend counts and a badge per mark agree', plan.head === `Universe 1 · ${plan.marks} fixtures`
            && plan.badges === plan.marks && plan.counts === plan.marks, plan);
        await check('rig map', 'only the selected mark shows its DMX caption', plan.caption.length === 1 && /^DMX \d+–\d+ · \w+ · (loop|accent)$/.test(plan.caption[0]), plan);
        await shot('plan_rich', '.mvr-card');
        await action('Telemetry tab', 'open', () => tab('Telemetry'), {watch: 2500});
        await check('Telemetry tab', 'shows its panel', await ev(shown('telemetry-panel')));
        await shot('telemetry');
    },

    cfg_drag: async () => {
        await cfgFresh();
        const before = await rigFiles();
        const original = fixtureIn(before, 'spot_left');
        await check('plan drag', 'spot_left starts at X -300, Y 0 in the store',
            original.matrix[9] === -300 && original.matrix[10] === 0, {matrix: original.matrix});
        await check('plan drag', 'selecting spot_left opens its form', await selectFixture('spot_left'));
        const xField = () => ev(`document.querySelector('${splitInput('matrix', 'x')}').value`);
        await check('plan drag', 'Position X reads -300 before the drag', (await xField()) === '-300', {x: await xField()});

        // Escape mid-drag: the preview moved, then everything is as it was and nothing is sent.
        let m = await planMark('spot_left');
        const grid0 = await areaBox();
        await arm();
        await pressAndMove(m.x, m.y, m.x + 300 * m.a, m.y);
        const mid = await planMark('spot_left');
        await check('plan drag', 'the mark previews 300 mm house-right (+X)', mid?.cx === m.cx + 300, {before: m.cx, mid: mid?.cx});
        await check('plan drag', 'the grid preview keeps its box when the extent holds', same(await areaBox(), grid0),
            {before: grid0, mid: await areaBox()});
        await key(null, 'Escape');
        await sleep(200);
        const back = await planMark('spot_left');
        await check('plan drag', 'Escape puts the mark back', back?.cx === m.cx && back?.cy === m.cy, {before: m, after: back});
        await release(m.x + 300 * m.a, m.y);
        await sleep(800);
        let sent = (await takeActions()).filter((a) => a.action !== 'select');
        await check('plan drag', 'Escape then release sends no draft action', sent.length === 0, {sent});
        await check('plan drag', 'Escape leaves the draft clean', !(await statusText()).startsWith(UNSAVED), {status: await statusText()});

        // The nine-area grid follows the preview: wash_left is the leftmost fixture.
        const w = await planMark('wash_left');
        await pressAndMove(w.x, w.y, w.x + 300 * w.a, w.y);
        const gridMid = await areaBox();
        await check('plan drag', 'the nine-area grid preview moves with the extent',
            gridMid && grid0 && gridMid[0] === grid0[0] + 300 && gridMid[2] === grid0[2] - 300, {before: grid0, mid: gridMid});
        const names = await ev(`[...document.querySelectorAll('#mvr-plan .plan-area-name')].map((t) => Number(t.getAttribute('x')))`);
        await key(null, 'Escape');
        await release(w.x + 300 * w.a, w.y);
        await sleep(600);
        await check('plan drag', 'the area names move with the grid', names.length === 9 && Math.min(...names) > grid0[0],
            {names});
        await check('plan drag', 'Escape restores the grid', same(await areaBox(), grid0), {grid: await areaBox()});
        await takeActions();

        // The committing drag.
        m = await planMark('spot_left');
        await arm();
        await pressAndMove(m.x, m.y, m.x + 300 * m.a, m.y);
        await release(m.x + 300 * m.a, m.y);
        await waitFor('window.__actions.length > 0', 3000);
        await sleep(600);
        sent = await takeActions();
        const moves = sent.filter((a) => a.action === 'set-position');
        await check('plan drag', 'a drag commits one set-position at X 0, Y 0',
            sent.length === 1 && moves.length === 1 && moves[0].x_mm === 0 && moves[0].y_mm === 0
            && moves[0].original_matrix.includes('-300'), {sent});
        await check('plan drag', 'Position X shows the new value', await waitFor(`document.querySelector('${splitInput('matrix', 'x')}').value === '0'`, 3000),
            {x: await xField()});
        await check('plan drag', 'the plan redraws the mark at X 0', (await planMark('spot_left'))?.cx === 0);
        await check('plan drag', 'the draft reads dirty', (await statusText()).startsWith(UNSAVED), {status: await statusText()});
        await check('plan drag', 'a drag sends no bridge command', (await since()).length === 0, {seen: await since()});

        // Zero movement: a press and release in place, and a drag that comes home.
        m = await planMark('spot_left');
        await mouse('mouseMoved', m.x, m.y, {button: 'none'});
        await mouse('mousePressed', m.x, m.y, {clickCount: 1, buttons: 1});
        await release(m.x, m.y);
        await sleep(300);
        await pressAndMove(m.x, m.y, m.x + 40, m.y + 25, 4);
        for (let i = 3; i >= 0; i--) {
            await mouse('mouseMoved', m.x + 10 * i, m.y + 6.25 * i, {buttons: 1});
            await sleep(30);
        }
        await sleep(150);
        await release(m.x, m.y);
        await sleep(800);
        sent = (await takeActions()).filter((a) => a.action !== 'select');
        await check('plan drag', 'a drag with zero movement sends nothing', sent.length === 0, {sent});

        await check('plan drag', 'Save writes the patch', await save(), {status: await statusText()});
        const after = await rigFiles();
        const moved = fixtureIn(after, 'spot_left');
        await check('plan drag', 'the store holds spot_left at X 0', moved.matrix[9] === 0 && moved.matrix[10] === 0
            && same(moved.matrix.slice(0, 9), original.matrix.slice(0, 9)) && moved.matrix[11] === original.matrix[11],
            {matrix: moved.matrix});
        await check('plan drag', 'every other fixture is unchanged in the store',
            same(after.fixtures.filter((f) => f.name !== 'spot_left'), before.fixtures.filter((f) => f.name !== 'spot_left')));
        await check('plan drag', 'the repo mirror is the store, byte for byte', after.repo_same_as_store);
        await check('plan drag', "the repo pb is Starfire's for the saved store", after.repo_pb === after.pb_of_store
            && after.repo_pb !== before.repo_pb, {before: before.repo_pb, after: after.repo_pb, expected: after.pb_of_store});
        const [xml0, xml1] = [before.store[SCENE_XML], after.store[SCENE_XML]];
        const matrix = (xml) => {
            const from = xml.indexOf('<Matrix>', xml.indexOf('name="spot_left"'));
            return [from, xml.indexOf('</Matrix>', from) + '</Matrix>'.length];
        };
        const [a0, a1] = matrix(xml0);
        const [b0, b1] = matrix(xml1);
        await check('plan drag', 'the scene XML differs only in spot_left\'s Matrix',
            xml0.slice(0, a0) === xml1.slice(0, b0) && xml0.slice(a1) === xml1.slice(b1) && xml0.slice(a0, a1) !== xml1.slice(b0, b1),
            {was: xml0.slice(a0, a1), now: xml1.slice(b0, b1), sameLength: xml0.length - xml1.length});
        const others = (entries) => Object.entries(entries).filter(([k]) => k !== SCENE_XML);
        await check('plan drag', 'every other MVR entry is byte-identical', same(others(before.store), others(after.store)));

        await check('plan drag', 'Reload loads the saved patch', await reloadSaved(), {status: await statusText()});
        await selectFixture('spot_left');
        await check('plan drag', 'after Reload spot_left is at X 0', (await xField()) === '0' && (await planMark('spot_left'))?.cx === 0,
            {x: await xField()});
        await check('plan drag', 'no bridge command from the whole drag, save and reload', (await since()).length === 0, {seen: await since()});
    },

    cfg_form: async () => {
        await cfgFresh();
        const before = await rigFiles();
        await selectFixture('spot_left');
        await arm();
        await formEdit('Patch number', () => typeInto(cfg('FixtureID'), '7'), {action: 'set-patch-number', value: '7'});
        await formRefusal('Patch number A-1', () => typeInto(cfg('FixtureID'), 'A-1'), 'FixtureID', 'Use a whole number.');
        await formEdit('Address 25', () => typeInto(cfg('address'), '25'),
            {action: 'set-address', universe: 1, address: 25});
        await formRefusal('Address 13 (spot_right holds 13-24)', () => typeInto(cfg('address'), '13'),
            'address', 'Overlaps spot_right.');
        await formRefusal('Address 505 (12 channels)', () => typeInto(cfg('address'), '505'),
            'address', 'Address must be 1–501 for 12 channels.');
        await formRefusal('rename to spot_right', () => typeInto(cfg('name'), 'spot_right'),
            'name', 'Another fixture is named spot_right.');
        await formRefusal('empty name', () => typeInto(cfg('name'), ' '), 'name', 'A name is required.');
        // The refusals left typed text in the fields; a redraw from the draft puts it back.
        await selectFixture('spot_right');
        await selectFixture('spot_left');
        await check('patch form', 'refused edits leave the draft as it was',
            (await ev(`document.querySelector('${cfg('address')}').value`)) === '25'
            && (await ev(`document.querySelector('${cfg('name')}').value`)) === 'spot_left');
        for (const pitch of ['61', '-61', '60.5']) {
            await formRefusal(`pitch ${pitch}°`, () => typeInto(splitInput('matrix', 'pitch'), pitch), 'matrix', 'Pitch must be within ±60°.');
        }
        await formEdit('pitch 60° (the limit)', () => typeInto(splitInput('matrix', 'pitch'), '60'),
            {action: 'set-matrix', position: [-300, 0, 0], rotation: [180, 60, 0], standing: false});
        await formEdit('Pan limits min 54° (of 0–540°)', () => typeInto(splitInput('Pan', 'min'), '54'),
            {action: 'set-movement', axis: 'Pan', minimum: 0.1, maximum: 1});
        await formRefusal('Tilt limits min 200° above max 100°', async () => {
            await ev(`document.querySelector('${splitInput('Tilt', 'max')}').value = '100'`);
            await typeInto(splitInput('Tilt', 'min'), '200');
        }, 'Tilt', 'Min is above max.');
        await selectFixture('spot_right');
        await selectFixture('spot_left');
        await formEdit('Tilt limits max 243° (of 0–270°)', () => typeInto(splitInput('Tilt', 'max'), '243'),
            {action: 'set-movement', axis: 'Tilt', minimum: 0, maximum: 0.9});
        await formEdit('Invert pan', () => click(cfg('DMXInvertPan')),
            {action: 'set-child-text', tag: 'DMXInvertPan', value: 'true'});
        await formEdit('Invert tilt', () => click(cfg('DMXInvertTilt')),
            {action: 'set-child-text', tag: 'DMXInvertTilt', value: 'true'});
        await check('patch form', 'spot_left is filled (loop) before its group changes', !(await planMark('spot_left')).ring);
        await formEdit('Group movers -> fx', () => choose(cfg('group'), 'fx'), {action: 'set-class', name: 'fx'});
        await check('patch form', 'joining fx moves spot_left to the accent Layer', await waitFor(`(() => {
            const c = ${markOf('spot_left')}?.querySelector('circle'); return !!c && c.classList.contains('ring'); })()`, 3000)
            && (await ev(`document.querySelector('${cfg('role')}').value`)) === 'accent');

        await selectFixture('zq06017');
        await formEdit('Mode 24Channel -> 2Channel', () => choose(cfg('GDTFMode'), '2Channel'),
            {action: 'set-child-text', tag: 'GDTFMode', value: '2Channel'});
        await formEdit('zq06017 Address 505 (2 channels)', () => typeInto(cfg('address'), '505'),
            {action: 'set-address', universe: 1, address: 505});
        await formRefusal('Mode 24Channel at 505', () => choose(cfg('GDTFMode'), '24Channel'),
            'GDTFMode', 'Address must be 1–489 for 24 channels.');
        await selectFixture('spot_right');
        await selectFixture('zq06017');
        await formEdit('zq06017 Address 97', () => typeInto(cfg('address'), '97'),
            {action: 'set-address', universe: 1, address: 97});
        await formEdit('Mode 12Channel', () => choose(cfg('GDTFMode'), '12Channel'),
            {action: 'set-child-text', tag: 'GDTFMode', value: '12Channel'});

        await selectFixture('wash_left');
        const washSpec = await ev(`document.querySelector('${cfg('GDTFSpec')}').value`);
        const spotSpec = 'Chauvet DJ@Intimidator Spot 110@1.0.gdtf';
        await formEdit('Profile -> the spot', () => choose(cfg('GDTFSpec'), spotSpec),
            {action: 'set-child-text', tag: 'GDTFSpec', value: spotSpec});
        await check('patch form', 'a profile without the mode blocks Save', await waitFor(`document.getElementById('fixture-save').disabled`, 2000));
        await formEdit('Profile back', () => choose(cfg('GDTFSpec'), washSpec),
            {action: 'set-child-text', tag: 'GDTFSpec', value: washSpec});
        await check('patch form', 'Save is enabled again', await waitFor(`!document.getElementById('fixture-save').disabled`, 2000));
        await formEdit('Role of wash -> accent', () => choose(cfg('role'), 'accent'), {action: 'set-role', role: 'accent'});
        await check('patch form', 'the whole wash group moves to the accent Layer', await waitFor(`['wash_left', 'wash_mid_left', 'wash_mid_right', 'wash_right']
            .every((name) => [...document.querySelectorAll('#mvr-plan .mvr-object')].find((g) => (g.querySelector('title')?.textContent || '').split(' · ')[0] === name)
                ?.querySelector('circle').classList.contains('ring'))`, 3000));
        await selectFixture('wash_right');
        await takeActions();
        await choose(cfg('group'), NEW_GROUP);
        await sleep(500);
        await check('patch form', '"new…" opens the name field and sends nothing',
            (await takeActions()).length === 0 && (await ev(`!document.querySelector('${cfg('newGroup')}').hidden`)));
        await formEdit('New group solo', () => typeInto(cfg('newGroup'), 'solo'), {action: 'set-class', name: 'solo'});
        await check('patch form', 'no bridge command from any form edit', (await since()).length === 0, {seen: await since()});

        await check('patch form', 'Save after the form edits', await save(), {status: await statusText()});
        const after = await rigFiles();
        const spot = fixtureIn(after, 'spot_left');
        const zq = fixtureIn(after, 'zq06017');
        const spotXml = fixtureXml(after.store[SCENE_XML], 'spot_left');
        await check('patch form', 'the store holds spot_left as edited', spot.address === 25 && spot.class === 'fx'
            && spot.layer === 'accent' && spot.pan.min === 0.1 && spot.tilt.max === 0.9 && spot.pan.inverted && spot.tilt.inverted
            && spotXml.includes('<FixtureID>7</FixtureID>') && spotXml.includes('<FixtureIDNumeric>7</FixtureIDNumeric>'),
            {spot, spotXml: spotXml.slice(0, 600)});
        const pitch = Math.asin(Math.max(-1, Math.min(1, spot.matrix[5]))) * 180 / Math.PI;
        await check('patch form', 'the stored spot_left is pitched 60° (|Matrix z of local Y|)', Math.abs(Math.abs(pitch) - 60) < 1e-3,
            {matrix: spot.matrix, pitch});
        await check('patch form', 'the store holds zq06017 at 97 in 12Channel', zq.address === 97 && zq.gdtf_mode === '12Channel', {zq});
        await check('patch form', 'the wash group sits in accent; wash_right is in solo',
            ['wash_left', 'wash_mid_left', 'wash_mid_right', 'wash_right'].every((n) => fixtureIn(after, n).layer === 'accent')
            && fixtureIn(after, 'wash_right').class === 'solo' && fixtureIn(after, 'wash_left').gdtf_spec === washSpec,
            {washes: after.fixtures.filter((f) => f.name.startsWith('wash')).map((f) => [f.name, f.class, f.layer])});
        await check('patch form', 'untouched fixtures are unchanged', same(fixtureIn(after, 'spot_right'), fixtureIn(before, 'spot_right'))
            && same(fixtureIn(after, 'kinta'), fixtureIn(before, 'kinta')));
        await check('patch form', 'repo mirror and pb follow the save', after.repo_same_as_store && after.repo_pb === after.pb_of_store);

        // Stale revision: the store changes behind the page; the page's Save must not land.
        await selectFixture('spot_left');
        await formEdit('Patch number 9 (to save over a moved store)', () => typeInto(cfg('FixtureID'), '9'),
            {action: 'set-patch-number', value: '9'});
        await harness('rig', {bump: true});
        const bumped = await rigFiles();
        await check('stale save', 'Save over a changed store is refused', await save(false), {status: await statusText()});
        await check('stale save', 'the refusal says to reload',
            (await statusText()) === 'Patch not saved: Fixture patch changed. Reload it before saving your edits.', {status: await statusText()});
        const refused = await rigFiles();
        await check('stale save', 'the refused save writes nothing', refused.revision === bumped.revision
            && refused.repo_pb === bumped.repo_pb && same(refused.repo, bumped.repo) && refused.revision !== after.revision,
            {revision: [after.revision, bumped.revision, refused.revision]});
        await check('stale save', 'the draft keeps the unsaved edit', (await ev(`document.querySelector('${cfg('FixtureID')}').value`)) === '9');

        // Swapping two names swaps labels only: everything else stays with each uuid.
        await cfgFresh();
        const unswapped = await rigFiles();
        for (const [from, to] of [['spot_left', 'spot_tmp'], ['spot_right', 'spot_left'], ['spot_tmp', 'spot_right']]) {
            await selectFixture(from);
            await formEdit(`rename ${from} -> ${to}`, () => typeInto(cfg('name'), to), {action: 'set-attribute', doc: 'scene', name: 'name', value: to});
        }
        await check('name swap', 'Save', await save(), {status: await statusText()});
        const swapped = await rigFiles();
        const byUuid = (files) => Object.fromEntries(files.fixtures.map(({name, ...rest}) => [rest.fixture_uuid, {name, rest}]));
        const [was, now] = [byUuid(unswapped), byUuid(swapped)];
        await check('name swap', 'the two names traded uuids', now[ROOM_UUID.spot_left].name === 'spot_right' && now[ROOM_UUID.spot_right].name === 'spot_left',
            {now: [now[ROOM_UUID.spot_left]?.name, now[ROOM_UUID.spot_right]?.name]});
        await check('name swap', 'Matrices, addresses and groups stay with the uuids',
            Object.keys(was).length === Object.keys(now).length && Object.entries(was).every(([id, fixture]) => same(fixture.rest, now[id]?.rest)),
            {was: was[ROOM_UUID.spot_left]?.rest, now: now[ROOM_UUID.spot_left]?.rest});
        await selectFixture('spot_right');
        await arm();
        await click(testButton('white'));
        await sleep(1500);
        await check('name swap', 'Test on "spot_right" lights the old spot_left uuid', same(await since(), [lightOn({fixture: ROOM_UUID.spot_left}, 'white')]),
            {seen: await since()});
        await click(testButton('off'));
        await sleep(800);
    },

    cfg_locate: async () => {
        await cfgFresh();
        await check('locate', 'select spot_left', await selectFixture('spot_left'),
            {form: await ev(`document.getElementById('fixture-info').innerHTML.slice(0, 1500)`)});
        const exactly = async (control, name, act, expected, watch = 2500) => {
            await arm();
            await act();
            await sleep(watch);
            const seen = await since();
            await check(control, `${name} -> ${expected.length} command(s), exactly`,
                seen.length === expected.length && expected.every((e, i) => same(seen[i], e)), {expected, seen});
            return seen;
        };
        const note = () => text('#fixture-info .cfg-test-note');
        await exactly('locate Test', 'click', () => click(testButton('white')), [lightOn({fixture: ROOM_UUID.spot_left}, 'white')]);
        await check('locate Test', 'lights its button; the note stays empty', await waitFor(`document.querySelector('${testButton('white')}').classList.contains('active')`, 3000)
            && (await note()) === '', {note: await note()});
        await check('locate Test', 'the keep-alive is held every 15 s', same(await keepAlive(), {held: 1, delays: [15000]}), await keepAlive());
        await exactly('locate keep-alive', 'the 15 s tick re-sends the test', () => ev('window.__tickKeepAlive()'), [lightOn({fixture: ROOM_UUID.spot_left}, 'white')]);
        for (const colour of ['warm', 'red', 'blue', 'green']) {
            await exactly(`locate swatch ${colour}`, 'click', () => click(testButton(colour)), [lightOn({fixture: ROOM_UUID.spot_left}, colour)]);
        }
        await check('locate swatch', 'the lit swatch wears the active ring', await waitFor(`document.querySelector('${testButton('green')}').classList.contains('active')
            && !document.querySelector('${testButton('blue')}').classList.contains('active')`, 3000));
        await check('locate swatch', 'one keep-alive, whatever the clicks', (await keepAlive()).held === 1, await keepAlive());
        await exactly('locate ›', 'next in patch order (spot_right), no clear first', () => click(testButton('1')), [lightOn({fixture: ROOM_UUID.spot_right}, 'green')]);
        await check('locate ›', 'the selection follows the test', await waitFor(`document.querySelector('${cfg('name')}').value === 'spot_right'`, 2000));
        await exactly('locate ‹', 'back to spot_left', () => click(testButton('-1')), [lightOn({fixture: ROOM_UUID.spot_left}, 'green')]);
        await exactly('locate ‹', 'wraps from the first to the last (zq06017 at 97)', () => click(testButton('-1')), [lightOn({fixture: ROOM_UUID.zq06017}, 'green')]);
        await exactly('locate ›', 'wraps back to spot_left', () => click(testButton('1')), [lightOn({fixture: ROOM_UUID.spot_left}, 'green')]);
        // The operator's latest colour must travel with ›, even before the show echoes it.
        await exactly('locate ›', 'right after a swatch carries that swatch', async () => {
            await click(testButton('red'));
            await click(testButton('1'));
        }, [lightOn({fixture: ROOM_UUID.spot_left}, 'red'), lightOn({fixture: ROOM_UUID.spot_right}, 'red')]);
        for (const motion of ['sweep', 'aim']) {
            await exactly(`locate ${motion}`, 'click', () => click(testButton(motion)), [lightOn({fixture: ROOM_UUID.spot_right}, 'red', motion)]);
            await check(`locate ${motion}`, 'lights its button', await waitFor(`document.querySelector('${testButton(motion)}').classList.contains('active')`, 3000));
        }
        await exactly('locate Off', 'clears the aim', () => click(testButton('off')), [CLEAR]);
        await check('locate Off', 'the motion buttons go dark', await waitFor(`!document.querySelector('${testButton('aim')}').classList.contains('active')`, 3000));
        await exactly('locate sweep', 'from dark lights white', () => click(testButton('sweep')), [lightOn({fixture: ROOM_UUID.spot_right}, 'white', 'sweep')]);
        await exactly('locate Off', 'clears the sweep', () => click(testButton('off')), [CLEAR]);
        await check('locate Off', 'drops the keep-alive', (await keepAlive()).held === 0, await keepAlive());
        await exactly('locate Off', 'a tick after Off sends nothing', () => ev('window.__tickKeepAlive()'), []);

        await selectFixture('zq06017');
        await check('locate cells', 'a fixture without pan offers no Sweep or Aim', await ev(`!document.querySelector('${testButton('sweep')}') && !document.querySelector('${testButton('aim')}')`));
        const rig = (await harness('state')).rig;
        const cells = rig.fixtures.find((f) => f.name === 'zq06017').cells;
        const listing = `[...document.querySelectorAll('#fixture-info [data-cfg-cell]')].map((b) => b.dataset.cfgCell)`;
        // The list fills from the show's rig on the poll after the form draws.
        await waitFor(`JSON.stringify(${listing}) === ${JSON.stringify(JSON.stringify(cells))}`, 3000);
        const listed = await ev(listing);
        await check('locate cells', "lists the running rig's cells for zq06017", same(listed, cells),
            {listed, cells, shown: await ev(`document.querySelector('#fixture-info .cfg-cells')?.outerHTML.slice(0, 400)`)});
        const shown = await ev(`[...document.querySelectorAll('#fixture-info [data-cfg-cell]')].map((b) => b.textContent)`);
        await check('locate cells', 'each cell reads by the fixture name, not its uuid',
            same(shown, cells.map((id) => id.replace(ROOM_UUID.zq06017, 'zq06017'))), {shown});
        const cell = cells[2];
        await exactly('locate cells', `picking ${cell} alone sends nothing`, () => click(`#fixture-info [data-cfg-cell="${cell}"]`), [], 800);
        await exactly('locate cells', 'Test lights the picked cell', () => click(testButton('white')), [lightOn({cell}, 'white')]);
        await exactly('locate cells', 'a swatch keeps the cell', () => click(testButton('blue')), [lightOn({cell}, 'blue')]);
        await check('locate cells', 'the picked cell is marked', await ev(`document.querySelector('#fixture-info [data-cfg-cell="${cell}"]').classList.contains('selected')`));

        await exactly('locate deselect', 'Close sends one clear', () => click('#fixture-close'), [CLEAR]);
        await check('locate deselect', 'drops the keep-alive', (await keepAlive()).held === 0, await keepAlive());

        await selectFixture('spot_left');
        await exactly('locate Test', 'again before leaving the tab', () => click(testButton('white')), [lightOn({fixture: ROOM_UUID.spot_left}, 'white')]);
        await exactly('locate tab switch', 'Control sends one clear and nothing after', async () => {
            await tab('Control');
            await sleep(2000);
            await ev('window.__tickKeepAlive()');
        }, [CLEAR], 4000);

        await tab('Config');
        await waitFor(`!!document.querySelector('${testButton('white')}') && !document.querySelector('${testButton('white')}').disabled`, 3000);
        await planSettled();
        await harness('control', {refuse_paused: true});
        await arm();
        await watchNote();
        await click(testButton('white'));
        await sleep(1500);
        const history = await ev('window.__notes.slice()');
        await check('locate 409', 'shows the reason', history.includes('Test refused: the show is paused'), {history});
        await check('locate 409', 'still shows the reason 1.5 s later (polls do not wipe it)',
            (await note()) === 'Test refused: the show is paused', {note: await note(), history});
        await check('locate 409', 'the page posted one highlight; the show took none',
            posted().length === 1 && posted()[0].action === 'highlight' && (await since()).length === 0, {posted: posted(), seen: await since(),
                pointer: await ev('window.__pointer.slice(-3)'), button: await ev(`(() => { const b = document.querySelector('${testButton('white')}'); const r = b.getBoundingClientRect(); return [b.disabled, r.x, r.y, scrollY]; })()`)});
        await check('locate 409', 'turns the test off (no keep-alive)', (await keepAlive()).held === 0, await keepAlive());
        await exactly('locate 409', 'a deselect after the refusal has nothing to clear', () => click('#fixture-close'), []);
        await harness('control', {refuse_paused: false});

        await selectFixture('spot_left');
        await harness('control', {paused: true});
        const disabled = await waitFor(`[...document.querySelectorAll('#fixture-info [data-cfg-test]')].every((b) => b.disabled)`, 3000);
        await check('locate paused', 'every test control is disabled', disabled);
        await check('locate paused', 'says why', (await note()) === 'Test is off while the show is paused.', {note: await note()});
        await arm();
        await click(testButton('white'));
        await click(testButton('red'));
        await sleep(800);
        await check('locate paused', 'clicks on the disabled controls post nothing', posted().length === 0, {posted: posted()});
        await harness('control', {paused: false});
        await check('locate paused', 'play enables them again', await waitFor(`[...document.querySelectorAll('#fixture-info [data-cfg-test]')].every((b) => !b.disabled)`, 3000)
            && (await note()) === '', {note: await note()});
        await checkHighlightIds('locate');
    },

    cfg_apply: async () => {
        await cfgFresh();
        const applyDisabled = () => ev(`document.getElementById('fixture-apply').disabled`);
        const banner = () => text('#fixture-show-rig');
        const label = () => text('#fixture-apply');
        await sleep(1000);
        await check('apply', 'enabled without a banner while the show runs the saved rig', !(await applyDisabled()) && (await banner()) === '',
            {banner: await banner()});
        await selectFixture('wash_left');
        await formEdit('Patch number 42', () => typeInto(cfg('FixtureID'), '42'), {action: 'set-patch-number', value: '42'});
        await check('apply', 'an unsaved edit raises no banner', (await banner()) === '', {banner: await banner()});
        await check('apply', 'Save', await save(), {status: await statusText()});
        await check('apply', 'after a Save the banner names the older rig', await waitFor(`!document.getElementById('fixture-apply').disabled
            && document.getElementById('fixture-show-rig').textContent === ${JSON.stringify(STALE_RIG)}`, 3000), {banner: await banner()});
        await arm();
        await click('#fixture-apply');
        await check('apply', 'the first click arms', await waitFor(`document.getElementById('fixture-apply').textContent === ${JSON.stringify(APPLY_ARMED)}`, 1000),
            {label: await label()});
        await sleep(5500);
        await check('apply', '5 s later it disarms', (await label()) === APPLY_LABEL, {label: await label()});
        await check('apply', 'arming and disarming send no command', (await since()).length === 0 && posted().length === 0,
            {seen: await since(), posted: posted()});
        const running = (await harness('state')).rig_revision;
        await arm();
        await click('#fixture-apply');
        await sleep(400);
        await click('#fixture-apply');
        await sleep(300);
        await check('apply', 'the second click within 5 s says it restarts', (await banner()) === 'Restarting the show on the saved patch.'
            && (await applyDisabled()), {banner: await banner()});
        await sleep(2500);
        const seen = await since();
        await check('apply', 'two clicks send exactly one restart_show', seen.length === 1 && same(seen[0], {action: 'restart_show'}), {seen});
        const files = await rigFiles();
        const state = await harness('state');
        await check('apply', 'the stub rebooted on the saved store', state.rig_revision === files.revision && running !== files.revision,
            {running, now: state.rig_revision, saved: files.revision});
        await check('apply', 'the new revision clears the banner and Restart stays enabled', await waitFor(`!document.getElementById('fixture-apply').disabled
            && document.getElementById('fixture-show-rig').textContent === ''`, 3000), {banner: await banner()});
        await check('apply', 'the label is back', (await label()) === APPLY_LABEL, {label: await label()});
    },

    cfg_robust: async () => {
        const NAME = `spot <"left"> & 'co'`;
        await cfgFresh();
        await selectFixture('spot_left');
        let typingNote = null;
        await formEdit('rename with < & and quotes', async () => {
            await ev(`(() => { const e = document.querySelector('${cfg('name')}'); e.focus(); e.select(); })()`);
            await cdp('Input.insertText', {text: NAME});
            typingNote = await noteOf('name');
            await ev('document.activeElement.blur()');
        }, {action: 'set-attribute', doc: 'scene', name: 'name', value: NAME});
        await check('odd name', 'typing the rename is a label edit, so nothing warns', typingNote === '', {note: typingNote});
        await check('odd name', 'survives the form', (await ev(`document.querySelector('${cfg('name')}').value`)) === NAME);
        const svg = await ev(`(() => { const g = ${markOf(NAME)}; return g && {label: g.querySelector('text')?.textContent, title: g.querySelector('title').textContent}; })()`);
        await check('odd name', 'survives the plan SVG (label and tooltip)', svg?.label === NAME && svg?.title === NAME + ' · movers', {svg});
        await check('odd name', 'survives the DMX map', (await ev(`[...document.querySelectorAll('#fixture-channel-map .patch-block')].some((b) => b.dataset.fixtureName === ${JSON.stringify(NAME)} && b.textContent.startsWith(${JSON.stringify(NAME + ' · ')}))`)));
        await check('odd name', 'leaves no stray markup in the plan', (await ev(`document.querySelectorAll('#mvr-plan svg.plan').length === 1 && !document.querySelector('#mvr-plan svg.plan left, #mvr-plan svg.plan co')`)));
        // A rename keeps the uuid the running rig knows, so its test lights the same fixture.
        await arm();
        await click(testButton('white'));
        await sleep(1500);
        const renamedTest = posted();
        await check('odd name', 'Test on the renamed, unapplied fixture lights its uuid', same(renamedTest, [lightOn({fixture: ROOM_UUID.spot_left}, 'white')]),
            {posted: renamedTest, note: await text('#fixture-info .cfg-test-note')});
        await click(testButton('off'));
        await sleep(800);
        await check('odd name', 'Save', await save(), {status: await statusText()});
        const files = await rigFiles();
        const xml = files.store[SCENE_XML];
        const attr = `name="spot &lt;&quot;left&quot;&gt; &amp; 'co'"`;
        await check('odd name', 'the saved XML escapes it exactly once', xml.includes(attr) && !/&amp;(lt|gt|quot|amp|apos|#)/.test(xml),
            {around: xml.slice(Math.max(0, xml.indexOf('spot ') - 40), xml.indexOf('spot ') + 80)});
        const parsed = await ev(`(() => { const doc = new DOMParser().parseFromString(${JSON.stringify(xml)}, 'application/xml');
            return {error: !!doc.querySelector('parsererror'), names: [...doc.querySelectorAll('Fixture')].map((f) => f.getAttribute('name'))}; })()`);
        await check('odd name', 'the saved XML parses back to the name', !parsed.error && parsed.names.filter((n) => n === NAME).length === 1, parsed);
        await check('odd name', 'the store reads it back', !!fixtureIn(files, NAME) && files.repo_same_as_store && files.repo_pb === files.pb_of_store);
        await check('odd name', 'Reload brings it back', await reloadSaved());
        await check('odd name', 'and the form shows it again', await selectFixture(NAME));

        await cfgFresh({width: 390});
        await check('phone width', 'select a fixture on the Config tab', await selectFixture('spot_left'));
        await sleep(800);
        const o = await overflow();
        await check('phone width Config form', 'no horizontal scroll', o.scroll <= o.width, o);
        const columns = await ev(`getComputedStyle(document.querySelector('#fixture-info .cfg-section')).gridTemplateColumns`);
        await check('phone width Config form', 'the form is a single column', columns.trim().split(/\s+/).length === 1, {columns});
        await cdp('Emulation.setDeviceMetricsOverride', {width: 1280, height: 900, deviceScaleFactor: 1, mobile: false});

        // Reload mid-test. Behaviour: the unloading page turns hidden, and its visibilitychange
        // handler clears the lit test (one clear reaches the show); nothing else is sent, and
        // the reloaded page shows no test and holds no keep-alive.
        await cfgFresh();
        await selectFixture('spot_left');
        await click(testButton('white'));
        await waitFor(`document.querySelector('${testButton('white')}').classList.contains('active')`, 3000);
        await arm();
        await navigate('Page.reload');
        await waitFor(`!!document.getElementById('control-fx-off')`, 15000);
        await sleep(3000);
        await check('reload mid-test', 'the unload sends exactly one clear and nothing else', same(await since(), [CLEAR]),
            {seen: await since()});
        await check('reload mid-test', 'the show holds no test', (await harness('state')).highlight === null);
        await tab('Config');
        await waitFor(`!!document.querySelector('#mvr-plan svg.plan')`, 8000);
        await ev(`window.__actions = []; (() => { const ds = window.dash_clientside; const real = ds.set_props;
            ds.set_props = function (id, props) { if (id === 'fixture-grid-action') window.__actions.push(props.data); return real.apply(this, arguments); }; })()`);
        await check('reload mid-test', 'the reloaded page selects the fixture', await selectFixture('spot_left'));
        await sleep(800);
        await check('reload mid-test', 'and shows no test lit, no keep-alive', !(await ev(`document.querySelector('${testButton('white')}').classList.contains('active')`))
            && (await text('#fixture-info .cfg-test-note')) === '' && (await keepAlive()).held === 0, {note: await text('#fixture-info .cfg-test-note')});
        await arm();
        await click(testButton('white'));
        await sleep(1500);
        await check('reload mid-test', 'Test works again with one command', same(await since(), [lightOn({fixture: ROOM_UUID.spot_left}, 'white')]),
            {seen: await since()});
        await click(testButton('off'));
        await sleep(1000);

        // A browser reload after a Save must show the saved patch, and say the show runs an older rig.
        await cfgFresh();
        await selectFixture('wash_left');
        await formEdit('Patch number 42', () => typeInto(cfg('FixtureID'), '42'), {action: 'set-patch-number', value: '42'});
        await check('reload after save', 'Save', await save());
        await navigate('Page.reload');
        await waitFor(`!!document.getElementById('control-fx-off')`, 15000);
        await tab('Config');
        await waitFor(`!!document.querySelector('#mvr-plan svg.plan')`, 8000);
        await ev(`window.__actions = []`);
        await selectFixture('wash_left');
        await sleep(1000);
        await check('reload after save', 'the reloaded page shows the saved patch',
            (await ev(`document.querySelector('${cfg('FixtureID')}').value`)) === '42',
            {shown: await ev(`document.querySelector('${cfg('FixtureID')}').value`)});
        await check('reload after save', 'and offers Restart show for it', !(await ev(`document.getElementById('fixture-apply').disabled`))
            && (await text('#fixture-show-rig')) === STALE_RIG, {banner: await text('#fixture-show-rig')});
        await checkHighlightIds('robust');
    },

    cfg_pane: async () => {
        await cfgFresh();
        const hidden = (selector) => ev(`getComputedStyle(document.querySelector('${selector}')).display === 'none'`);
        await check('pane', 'with nothing selected it reads one line and offers no actions', (await text('#fixture-info .cfg-empty')) === 'Select a fixture on the plan'
            && (await hidden('#fixture-close')) && (await hidden('#fixture-info .cfg-footer')));
        await check('pane', 'select spot_left', await selectFixture('spot_left'));
        const headings = () => ev(`[...document.querySelectorAll('#fixture-info .cfg-section > h4')].map((h) => h.firstChild.textContent)`);
        await check('pane', 'a single-cell mover shows every section but Cells', same(await headings(), ['Patch', 'Placement', 'Group and role', 'Movement']),
            {headings: await headings()});
        await check('pane', 'the name is the heading', !!(await ev(`document.querySelector('#fixture-title [data-cfg="name"]')`)));
        await check('pane', 'the uuid is no field outside Advanced', await ev(`![...document.querySelectorAll('#fixture-info input')]
            .some((i) => !i.closest('.cfg-advanced') && i.value.toUpperCase() === '${ROOM_UUID.spot_left}')`));
        const advancedOpen = () => ev(`document.querySelector('#fixture-info .cfg-advanced').open`);
        await check('pane', 'Advanced is closed by default', (await advancedOpen()) === false);
        await click('#fixture-info .cfg-advanced > summary');
        await check('pane', 'Advanced opens on a click, showing the raw fields', await waitFor(`document.querySelector('#fixture-info .cfg-advanced').open
            && !!document.querySelector('#fixture-info .cfg-advanced .mvr-row')`, 2000));
        await check('pane', 'Advanced stays open for the next fixture', (await selectFixture('spot_right')) && (await advancedOpen()) === true);
        await click('#fixture-info .cfg-advanced > summary');
        await waitFor(`!document.querySelector('#fixture-info .cfg-advanced').open`, 2000);

        // The pan arc: drawn bright for the selected mover, faint for the other, none for a par.
        const arc = (name) => ev(`(() => { const p = ${markOf(name)}?.querySelector('.plan-pan'); return p && Number(getComputedStyle(p).opacity); })()`);
        await check('pan arc', 'the selected mover has a bright arc, the other a faint one, a par none',
            (await arc('spot_right')) > 0.5 && (await arc('spot_left')) < 0.5 && (await arc('spot_left')) > 0 && (await arc('wash_left')) === null,
            {spot_right: await arc('spot_right'), spot_left: await arc('spot_left'), wash_left: await arc('wash_left')});
        await shot('pane_impl_mover', '#fixture-info');

        await selectFixture('wash_left');
        await check('pane', 'a static fixture shows no Movement', same(await headings(), ['Patch', 'Placement', 'Group and role']),
            {headings: await headings()});
        await shot('pane_impl_static', '#fixture-info');

        // Soft limits in degrees, clamped to the GDTF's 0–540° pan.
        await selectFixture('spot_left');
        const arcPath = () => ev(`${markOf('spot_left')}?.querySelector('.plan-pan')?.getAttribute('d')`);
        const fullArc = await arcPath();
        await check('soft limits', 'the pan field shows the physical range and degrees', (await text('#fixture-info .cfg-field:has([data-cfg="Pan"]) > span small')) === '0–540'
            && (await ev(`document.querySelector('${splitInput('Pan', 'max')}').value`)) === '540');
        await formEdit('Pan max 600° clamps to 540°', () => typeInto(splitInput('Pan', 'max'), '600'), {action: 'set-movement', axis: 'Pan', minimum: 0, maximum: 1});
        await formEdit('Pan min -20° clamps to 0°, max 270°', async () => {
            await ev(`document.querySelector('${splitInput('Pan', 'min')}').value = '-20'`);
            await typeInto(splitInput('Pan', 'max'), '270');
        }, {action: 'set-movement', axis: 'Pan', minimum: 0, maximum: 0.5});
        await check('soft limits', 'the fields redraw at the clamped degrees', await waitFor(`document.querySelector('${splitInput('Pan', 'min')}').value === '0'
            && document.querySelector('${splitInput('Pan', 'max')}').value === '270'`, 2000));
        await check('pan arc', 'the arc follows the limits', !!fullArc && (await arcPath()) !== fullArc, {before: fullArc, after: await arcPath()});

        // Hanging -> standing turns the head 180° about its facing; it survives Save and Reload.
        await check('mount', 'spot_left reads hanging', (await ev(`document.querySelector('${cfg('mount')}').value`)) === 'hanging');
        await formEdit('mount standing', () => choose(cfg('mount'), 'standing'),
            {action: 'set-matrix', position: [-300, 0, 0], rotation: [180, 0, 0], standing: true});
        await check('mount', 'the rotation fields keep their values', (await ev(`document.querySelector('${splitInput('matrix', 'yaw')}').value`)) === '180');

        // The heading renames the label; the uuid, and with it everything else, stays.
        const before = await rigFiles();
        await formEdit('heading rename', () => typeInto(cfg('name'), 'spot_hero'), {action: 'set-attribute', doc: 'scene', name: 'name', value: 'spot_hero'});
        await check('pane', 'Save', await save(), {status: await statusText()});
        const after = await rigFiles();
        const hero = after.fixtures.find((f) => f.fixture_uuid.toUpperCase() === ROOM_UUID.spot_left);
        const was = before.fixtures.find((f) => f.fixture_uuid.toUpperCase() === ROOM_UUID.spot_left);
        await check('heading rename', 'the uuid keeps its patch under the new name', hero?.name === 'spot_hero'
            && same({...hero, name: 0, matrix: 0, pan: 0}, {...was, name: 0, matrix: 0, pan: 0}), {hero, was});
        await check('mount', 'the saved Matrix is standing: local X and Z negated', same(hero?.matrix, [1, 0, 0, 0, -1, 0, 0, 0, -1, -300, 0, 0]),
            {matrix: hero?.matrix});
        await check('soft limits', 'the saved pan limits are the clamped share', hero?.pan?.min === 0 && hero?.pan?.max === 0.5, {pan: hero?.pan});
        await check('pane', 'Reload loads the saved patch', await reloadSaved());
        await selectFixture('spot_hero');
        await check('mount', 'after Reload spot_hero reads standing', (await ev(`document.querySelector('${cfg('mount')}').value`)) === 'standing');
        await check('pane', 'no bridge command from the pane edits', (await since()).length === 0, {seen: await since()});
    },

    narrow: async () => {
        await fresh({width: 390});
        const views = [
            ['Control AUTO', async () => {}],
            ['Settings open', () => click('#settings-toggle')],
            ['Control STATIC', () => modeTab('static')],
            ['Telemetry', () => tab('Telemetry')],
            ['Config', () => tab('Config')],
        ];
        for (const [name, open] of views) {
            await open();
            await sleep(1200);
            const o = await overflow();
            await check(`phone width ${name}`, 'no horizontal scroll', o.scroll <= o.width, o);
        }
        await check('phone width', 'no commands from layout', (await since()).length === 0, {seen: await since()});
    },
};

// ---- run ----------------------------------------------------------------------------------

for (const [name, suite] of Object.entries(SUITES)) {
    if (ONLY && !ONLY.split(',').includes(name)) {
        continue;
    }
    console.log(`== ${name}`);
    const lines = consoleLines.length;
    scenarioNet = network.length;
    try {
        await suite();
    } catch (error) {
        await check(name, 'suite ran to the end', false, {error: error.stack});
    }
    const noisy = consoleLines.slice(lines);
    await check(name, 'no page errors or warnings', noisy.length === 0, {console: noisy});
}
console.log(`aborted Dash callbacks dropped during navigations: ${abortedOnLeave}`);
console.log(`\n${results.length - failed} passed, ${failed} failed`);
chrome.kill();
process.exit(failed ? 1 : 0);
