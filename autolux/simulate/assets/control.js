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

    function staticCommand(trigger, target, presetColours) {
        const all = config().allFixtures || 'all';
        const colours = presetColours || config().presetColours || {};
        if (trigger.startsWith('static-preset-')) {
            const preset = trigger.slice('static-preset-'.length);
            if (target !== all && preset in colours) {
                return {action: 'static', kind: 'look', target: target, colour: colours[preset]};
            }
            if (target !== all && preset === 'off') {
                return {action: 'static', kind: 'look', target: target, dimmer: 0.0};
            }
            return {action: 'static', kind: 'preset', preset: preset};
        }
        if (trigger === 'static-blacklight') {
            return {action: 'static', kind: 'blacklight'};
        }
        if (trigger === 'static-strobe') {
            return {action: 'static', kind: 'strobe'};
        }
        return null;
    }

    const build = {
        toggle: (paused) => ({action: paused ? 'play' : 'pause'}),
        next: () => ({action: 'next_autoloop'}),
        palette: (name) => ({action: 'palette', name: name}),
        master: (level) => ({action: 'master', level: level}),
        intensity: (level) => ({action: 'intensity', level: level}),
        mode: (mode) => ({action: 'mode', mode: mode}),
        static: staticCommand,
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

    window.autoluxControl = {build, send, dispatch, sendLatest, hold, view};
})();
