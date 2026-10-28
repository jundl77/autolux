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
listeners.push((msg) => {
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
    await cdp('Page.navigate', {url: URL_});
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
async function shot(tab) {
    if (!SHOTS) {
        return;
    }
    const {data} = await cdp('Page.captureScreenshot', {format: 'png', captureBeyondViewport: true});
    writeFileSync(join(SHOTS, `gui_${tab}.png`), Buffer.from(data, 'base64'));
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

// Clicks a slider's track at a fraction of its width.
const slider = (selector, fraction) => click(selector, {fx: fraction});

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
        const groups = await text('#special-groups');
        await check('capabilities', 'lists each FX group with its axes', /pars.*colour.*dimmer/.test(groups) && /spots.*pan.*tilt/.test(groups), {groups});
        await shot('config');
        await action('Telemetry tab', 'open', () => tab('Telemetry'), {watch: 2500});
        await check('Telemetry tab', 'shows its panel', await ev(shown('telemetry-panel')));
        await shot('telemetry');
    },

    narrow: async () => {
        await fresh({width: 390});
        const overflow = () => ev(`(() => {
            const w = document.documentElement.clientWidth;
            const wide = [...document.querySelectorAll('body *')].filter((e) => e.getClientRects().length
                && e.getBoundingClientRect().right > w + 1 && !e.closest('[style*="overflow: hidden"], .marquee'))
                .slice(0, 4).map((e) => (e.id ? '#' + e.id : e.tagName.toLowerCase() + '.' + e.className) + ' right=' + Math.round(e.getBoundingClientRect().right));
            return {scroll: document.documentElement.scrollWidth, width: w, wide};
        })()`);
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
    try {
        await suite();
    } catch (error) {
        await check(name, 'suite ran to the end', false, {error: error.stack});
    }
    const noisy = consoleLines.slice(lines);
    await check(name, 'no page errors or warnings', noisy.length === 0, {console: noisy});
}
console.log(`\n${results.length - failed} passed, ${failed} failed`);
chrome.kill();
process.exit(failed ? 1 : 0);
