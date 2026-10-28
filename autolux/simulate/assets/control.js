// Operator commands go straight from the browser to the show's control bridge.
(function () {
    'use strict';

    // Long enough for the show's echo of a click to land, so older polls cannot flip it back.
    const HOLD_MS = 2000;
    const config = () => window.autoluxControlConfig || {};
    const holds = {};
    const latest = {};
    const inflight = {};
    // Nothing leaves the page before the operator's first pointer or key press: a restored
    // value, the first poll or a component settling on load must never command the show.
    let operatorActed = false;
    const operatorActs = () => {
        operatorActed = true;
    };
    if (typeof document !== 'undefined') {
        ['pointerdown', 'keydown'].forEach((type) =>
            document.addEventListener(type, operatorActs, {capture: true, once: true}));
    }

    // The FX chips, Intensity Auto and the levels send only what a real click pressed, read
    // off the element before Dash's own handler queues DIRECT_AUTO_JS. Dash can run that
    // callback with an input no click changed (the poll's set_props on the level radio,
    // merged requests); such a run finds no press and sends nothing.
    const presses = {};
    // A level press no run takes within this window (its callback never fired) must not be
    // spent by a later poll's run: the poll's set_props on the level radio re-runs that
    // callback. A chip press is taken only by a real n_clicks run, so it never expires;
    // a stalled page that runs it late still sends it.
    const PRESS_TTL_MS = 1000;
    const press = (action, value) => {
        presses[action] = {value: value, at: performance.now()};
    };
    // One click, one command: the first callback run takes the press.
    const takePress = (action) => {
        const taken = presses[action];
        delete presses[action];
        return taken && (action !== 'intensity' || performance.now() - taken.at <= PRESS_TTL_MS)
            ? taken.value
            : undefined;
    };
    function pressOf(target, type) {
        const level = target.closest('#control-intensity label');
        const radio = level && level.querySelector('input');
        // Before the first settings poll the levels sit in a disabled fieldset: no callback.
        // A level already chosen changes nothing either.
        if (radio && !radio.matches(':disabled')
                && level.getAttribute('aria-selected') !== 'true') {
            return ['intensity', Number(radio.value)];
        }
        if (type !== 'click') {
            return null;
        }
        const chip = target.closest('#control-fx-auto, #control-fx-off');
        if (chip) {
            return ['fx_auto', chip.id === 'control-fx-auto'];
        }
        const follow = target.closest('#control-intensity-auto');
        if (follow) {
            // What the operator sees lit, not the last poll, decides which way Auto flips.
            return ['intensity_auto', !follow.classList.contains('active')];
        }
        return null;
    }
    if (typeof document !== 'undefined') {
        // Space on a button or radio and arrow keys between levels arrive as a click, but
        // Dash's RadioItems picks the focused level on Enter's keyup with no click.
        const record = (event) => {
            const pressed = event.isTrusted && event.target instanceof Element
                && (event.type === 'click' || event.key === 'Enter')
                && pressOf(event.target, event.type);
            if (pressed) {
                press(...pressed);
            }
        };
        document.addEventListener('click', record, true);
        document.addEventListener('keyup', record, true);
    }

    function staticCommand(trigger, target, colour) {
        const all = config().allFixtures || 'all';
        const colours = config().presetColours || {};
        if (trigger.startsWith('static-preset-')) {
            const preset = trigger.slice('static-preset-'.length);
            if (target !== all && preset in colours) {
                return {action: 'static', kind: 'look', target: target, colour: colours[preset]};
            }
            if (target !== all && preset === 'off') {
                return {action: 'static', kind: 'look', target: target, dimmer: 0.0};
            }
            if (target !== all && preset === 'uv') {
                return {
                    action: 'static', kind: 'look', target: target,
                    colour: '#000000', dimmer: 1.0, uv: true,
                };
            }
            return {action: 'static', kind: 'preset', preset: preset};
        }
        if (trigger === 'static-clear' || trigger === 'static-banner-clear') {
            return {action: 'static', kind: 'clear'};
        }
        if (trigger === 'static-colour') {
            const text = (colour || '').trim();
            if (!/^#?[0-9a-fA-F]{6}$/.test(text)) {
                return null;
            }
            return {
                action: 'static', kind: 'look', target: target,
                colour: '#' + text.replace(/^#/, '').toLowerCase(),
            };
        }
        return null;
    }

    // The row already held on the target sends nothing, so a poll syncing the picker never
    // re-fires it.
    function staticFxCommand(row, target, state) {
        const active = ((state && state.static) || {}).special_fx || {};
        if (!row || (active.name === row && active.group === target)) {
            return null;
        }
        return {action: 'static', kind: 'special_fx', row: row, target: target};
    }

    const build = {
        toggle: (paused) => ({action: paused ? 'play' : 'pause'}),
        next: () => ({action: 'next_autoloop'}),
        palette: (name) => ({action: 'palette', name: name}),
        master: (level) => ({action: 'master', level: level}),
        intensity: (level) => ({action: 'intensity', level: level}),
        intensityAuto: (enabled) => ({action: 'intensity_auto', enabled: enabled}),
        fxAuto: (enabled) => ({action: 'fx_auto', enabled: enabled}),
        settings: (changes) => ({action: 'settings', settings: changes}),
        static: staticCommand,
        staticFx: staticFxCommand,
        staticFxClear: () => ({action: 'static', kind: 'special_fx_clear'}),
    };

    // A host that drops packets silently would otherwise hang the fader's one request in flight.
    const POST_TIMEOUT_MS = 3000;

    function post(url, command) {
        return fetch(url, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(command),
            signal: AbortSignal.timeout(POST_TIMEOUT_MS),
        });
    }

    // Any non-2xx answer is a refusal (4xx) or a stalled engine (504); either way not applied.
    async function readReply(response) {
        let reply = {};
        try {
            reply = await response.json();
        } catch (error) {}
        if (!response.ok) {
            // The status tells a refusal (409: the command cannot apply now) from a stall.
            reply = {error: (reply && reply.error) || 'the show answered ' + response.status, status: response.status};
        }
        return reply;
    }

    async function send(command) {
        if (!operatorActed) {
            console.warn('[control] dropped a command sent before any operator input',
                command, new Error().stack);
            return {dropped: 'no operator input yet'};
        }
        const url = location.protocol + '//' + location.hostname + ':' + config().bridgePort;
        return readReply(await post(url + '/control', command));
    }

    function setStatus(statusId, text) {
        const clientside = window.dash_clientside;
        if (statusId && clientside && clientside.set_props) {
            clientside.set_props(statusId, {children: text});
        }
    }

    // A refused command drops only what it held: the fields of a settings hold that other
    // clicks still hold stay held.
    function release(held) {
        Object.entries(held || {}).forEach(([key, value]) => {
            const kept = holds[key] && holds[key].value;
            if (isDict(value) && isDict(kept)) {
                Object.keys(value).forEach((field) => delete kept[field]);
                if (Object.keys(kept).length) {
                    return;
                }
            }
            delete holds[key];
        });
    }

    function report(reply, statusId, held) {
        if (reply && reply.error) {
            release(held);
            setStatus(statusId, 'Rejected: ' + reply.error);
        } else {
            setStatus(statusId, '');
        }
        return reply;
    }

    // held: what the caller passed to hold() for this command, released if it fails.
    function dispatch(command, statusId, held) {
        return send(command).then(
            (reply) => report(reply, statusId, held),
            () => report({error: 'the show is not answering'}, statusId, held)
        );
    }

    // One request in flight per key; while it runs only the newest command waits.
    function sendLatest(key, command, statusId) {
        latest[key] = command;
        if (inflight[key]) {
            return inflight[key];
        }
        inflight[key] = (async () => {
            while (latest[key]) {
                const next = latest[key];
                latest[key] = null;
                await dispatch(next, statusId);
            }
            inflight[key] = null;
        })();
        return inflight[key];
    }

    const isDict = (value) => !!value && typeof value === 'object' && !Array.isArray(value);
    // A dict holds only the fields it names (one setting), merged over what the poll reports.
    const merged = (base, value) => (isDict(base) && isDict(value) ? {...base, ...value} : value);

    function hold(values) {
        const at = Date.now();
        Object.entries(values).forEach(([key, value]) => {
            const live = holds[key] && at - holds[key].at < HOLD_MS ? holds[key].value : undefined;
            holds[key] = {value: merged(live, value), at: at};
        });
        return values;
    }

    // A held value wins over every poll for HOLD_MS, so an echo of an older click arriving
    // late cannot flip the control back.
    function view(state) {
        if (!state) {
            return state;
        }
        const now = Date.now();
        const shown = {...state};
        Object.entries(holds).forEach(([key, {value, at}]) => {
            if (now - at >= HOLD_MS) {
                delete holds[key];
            } else {
                shown[key] = merged(state[key], value);
            }
        });
        return shown;
    }

    const FX_CHIPS = ['control-fx-auto', 'control-fx-off'];
    const FOLLOW_CHIPS = ['control-intensity-auto', 'control-intensity-set'];
    const fxClasses = (on) => [on ? 'active' : '', on ? '' : 'active'];
    // Following the music greys the segments but keeps the chosen level lit.
    const followClasses = (on) => [on ? 'active' : '', on ? 'following' : ''];

    // Both the click and the poll paint the chips through set_props, which applies at once: a
    // poll's Output computed before a click would land after it and flip the chip back.
    function paintFx(on) {
        FX_CHIPS.forEach((id, index) => window.dash_clientside.set_props(
            id, {className: on === null ? '' : fxClasses(on)[index]}));
    }
    function paintFollow(on) {
        FOLLOW_CHIPS.forEach((id, index) => window.dash_clientside.set_props(
            id, {className: followClasses(on)[index]}));
    }

    // A bar elapsedSeconds after the poll that reported remaining.
    function cooldownView(total, remaining, elapsedSeconds) {
        const left = Math.max(0, remaining - elapsedSeconds);
        const ready = left <= 0;
        const filled = ready || total <= 0 ? 1 : 1 - Math.min(1, left / total);
        return {
            fill: (filled * 100).toFixed(1) + '%',
            caption: ready ? 'ready' : Math.ceil(left) + ' s',
            ready: ready,
        };
    }

    // Outlasts the cooldown-fire animation in control.css.
    const FLASH_MS = 650;

    // Whether a group fired between two polls of it ({last, remaining, ready, total}): a new
    // special, a ready bar going busy, or the time left jumping back up. A changed total is the
    // operator editing the cooldown, not a fire. Polls only ever count down otherwise, so the
    // 0.05 s slack only absorbs float noise.
    function cooldownFired(previous, polled) {
        if (!previous) {
            return false;
        }
        return polled.last !== previous.last
            || (previous.ready && !polled.ready)
            || (polled.total === previous.total && polled.remaining > previous.remaining + 0.05);
    }

    // Per group, so a fire is told apart from Dash handing this element another group's entry.
    const cooldownSeen = {};

    // Draws the FX Effects bars from their data attributes, filling them up between polls, and
    // flashes a bar whose group just fired. Dash may reuse a bar's element for another entry, so
    // a poll is spotted by its data attributes changing, not by the node.
    function tickCooldowns() {
        const now = performance.now();
        document.querySelectorAll('.cooldown-row').forEach((row) => {
            const data = row.dataset;
            const polled = {
                last: data.last || '',
                remaining: Number(data.remaining),
                ready: data.ready === 'true',
                total: Number(data.total),
            };
            const key = data.group + '|' + data.total + '|' + data.remaining + '|' + polled.last;
            const seen = cooldownSeen[data.group];
            if (!seen || seen.key !== key) {
                const flashUntil = cooldownFired(seen && seen.polled, polled)
                    ? now + FLASH_MS : (seen ? seen.flashUntil : 0);
                cooldownSeen[data.group] = {key, polled, polledAt: now, flashUntil};
            }
            const state = cooldownSeen[data.group];
            const shown = cooldownView(polled.total, polled.remaining,
                (now - state.polledAt) / 1000);
            // Python renders neither, so React never overwrites them.
            row.querySelector('.cooldown-fill').style.width = shown.fill;
            row.querySelector('.cooldown-caption').textContent = shown.caption;
            row.classList.toggle('ready', shown.ready);
            row.classList.toggle('flash', now < state.flashUntil);
        });
    }

    if (typeof document !== 'undefined') {
        setInterval(tickCooldowns, 100);
    }

    window.autoluxControl = {
        build, send, dispatch, sendLatest, hold, view, operatorActs, press, takePress,
        paintFx, paintFollow, cooldownView, cooldownFired,
    };
})();
