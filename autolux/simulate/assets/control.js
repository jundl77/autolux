// Operator commands go straight from the browser to the show's control bridge.
// Payloads mirror control_command / static_control_command in visualizer_app.py.
(function () {
    'use strict';

    const RETRY_DIRECT_MS = 30000;
    const config = () => window.autoluxControlConfig || {};
    const holds = {};
    const latest = {};
    const inflight = {};
    let directDownAt = -Infinity;
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

    // Mirrors static_fx_command: the row already held on the target sends nothing.
    function staticFxCommand(row, target, state) {
        const active = ((state && state.static) || {}).special_fx || {};
        if (!row || (active.name === row && active.group === target)) {
            return null;
        }
        return {action: 'static_fx', row: row, target: target};
    }

    const build = {
        toggle: (paused) => ({action: paused ? 'play' : 'pause'}),
        next: () => ({action: 'next_autoloop'}),
        palette: (name) => ({action: 'palette', name: name}),
        master: (level) => ({action: 'master', level: level}),
        intensity: (level) => ({action: 'intensity', level: level}),
        intensityAuto: (enabled) => ({action: 'intensity_auto', enabled: enabled}),
        fxAuto: (enabled) => ({action: 'fx_auto', enabled: enabled}),
        static: staticCommand,
        staticFx: staticFxCommand,
        staticFxClear: () => ({action: 'static', kind: 'special_fx_clear'}),
    };

    function post(url, command) {
        return fetch(url, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(command),
        });
    }

    // Any non-2xx answer is a refusal (4xx) or a stalled engine (504); either way not applied.
    async function readReply(response) {
        let reply = {};
        try {
            reply = await response.json();
        } catch (error) {}
        if (!response.ok && !(reply && reply.error)) {
            reply = {error: 'the show answered ' + response.status};
        }
        return reply;
    }

    async function send(command) {
        if (!operatorActed) {
            console.warn('[control] dropped a command sent before any operator input',
                command, new Error().stack);
            return {dropped: 'no operator input yet'};
        }
        const {direct, bridgePort} = config();
        if (direct && bridgePort && Date.now() - directDownAt >= RETRY_DIRECT_MS) {
            let response = null;
            try {
                response = await post(
                    location.protocol + '//' + location.hostname + ':' + bridgePort + '/control',
                    command
                );
            } catch (error) {
                // Only a network or CORS failure lands here; a JSON POST is preflighted, so the
                // bridge never ran it and the relay may send it instead.
                directDownAt = Date.now();
            }
            if (response) {
                return readReply(response);
            }
        }
        return readReply(await post(config().relayPath || '/control-relay', command));
    }

    function setStatus(statusId, text) {
        const clientside = window.dash_clientside;
        if (statusId && clientside && clientside.set_props) {
            clientside.set_props(statusId, {children: text});
        }
    }

    function report(reply, statusId, heldKeys) {
        if (reply && reply.error) {
            (heldKeys || []).forEach((key) => delete holds[key]);
            setStatus(statusId, 'Rejected: ' + reply.error);
        } else {
            setStatus(statusId, '');
        }
        return reply;
    }

    function dispatch(command, statusId, heldKeys) {
        return send(command).then(
            (reply) => report(reply, statusId, heldKeys),
            () => report({error: 'the show is not answering'}, statusId, heldKeys)
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

    function hold(values) {
        const at = Date.now();
        Object.entries(values).forEach(([key, value]) => {
            holds[key] = {value: value, at: at};
        });
    }

    // Mirrors ControlExpectations.apply: a held value wins until the show reports it or it expires.
    function view(state) {
        if (!state) {
            return state;
        }
        const now = Date.now();
        const holdMs = config().holdMs || 2000;
        const shown = {...state};
        Object.entries(holds).forEach(([key, {value, at}]) => {
            if (JSON.stringify(state[key]) === JSON.stringify(value) || now - at >= holdMs) {
                delete holds[key];
            } else {
                shown[key] = value;
            }
        });
        return shown;
    }

    // Mirrors _cooldown_bar: a bar elapsedSeconds after the poll that reported remaining.
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

    const FLASH_MS = 500;

    // Whether a group fired between two polls of it ({last, remaining, ready}): a new special,
    // a ready bar going busy, or the time left jumping back up. Polls only ever count down
    // otherwise, so the 0.05 s slack only absorbs float noise.
    function cooldownFired(previous, polled) {
        if (!previous) {
            return false;
        }
        return polled.last !== previous.last
            || (previous.ready && !polled.ready)
            || polled.remaining > previous.remaining + 0.05;
    }

    // Per group, so a fire is told apart from Dash handing this element another group's entry.
    const cooldownSeen = {};

    // Fills the FX Effects bars up between polls and flashes a bar whose group just fired.
    // Dash may reuse a bar's element for another entry, so a poll is spotted by its data
    // attributes changing, not by the node.
    function tickCooldowns() {
        const now = performance.now();
        document.querySelectorAll('.cooldown-row').forEach((row) => {
            const data = row.dataset;
            const polled = {
                last: data.last || '',
                remaining: Number(data.remaining),
                ready: data.ready === 'true',
            };
            const key = data.group + '|' + data.total + '|' + data.remaining + '|' + polled.last;
            const seen = cooldownSeen[data.group];
            if (!seen || seen.key !== key) {
                const flashUntil = cooldownFired(seen && seen.polled, polled)
                    ? now + FLASH_MS : (seen ? seen.flashUntil : 0);
                cooldownSeen[data.group] = {key, polled, polledAt: now, flashUntil};
            }
            const state = cooldownSeen[data.group];
            const shown = cooldownView(Number(data.total), polled.remaining,
                (now - state.polledAt) / 1000);
            const fill = row.querySelector('.cooldown-fill');
            const caption = row.querySelector('.cooldown-caption');
            if (fill) {
                fill.style.width = shown.fill;
            }
            // Edit React's own text node so its next render still lands in the page.
            if (caption && caption.firstChild) {
                caption.firstChild.nodeValue = shown.caption;
            }
            // Re-applied every tick: a Dash render resets className.
            row.classList.toggle('ready', shown.ready);
            row.classList.toggle('flash', now < state.flashUntil);
        });
    }

    if (typeof document !== 'undefined') {
        setInterval(tickCooldowns, 100);
    }

    window.autoluxControl = {
        build, send, dispatch, sendLatest, hold, view, operatorActs, cooldownView,
        cooldownFired,
    };
})();
