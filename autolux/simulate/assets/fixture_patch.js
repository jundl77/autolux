(() => {
    const ds = (window.dash_clientside = window.dash_clientside || {});
    const DRAG_THRESHOLD_PX = 4;
    const LABEL_OFFSET_PX = 16;
    const MATRIX_DECIMALS = 6;
    const ORTHONORMAL_TOLERANCE = 1e-3;
    const GIMBAL_TOLERANCE = 1e-9;
    const MAX_PITCH_DEGREES = 60;
    const UNIVERSE_SIZE = 512;
    const MAX_UNIVERSES = 32768;
    const PLAN_MIN_SPAN_MM = 2000;
    const IDENTITY = [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0];
    let drag = null;
    let suppressedClick = null;
    let requestId = 0;
    let keyCounter = 0;
    const drawn = new WeakMap();

    function* walk(node, parent = null) {
        yield [node, parent];
        for (const child of node.children) yield* walk(child, node);
    }

    function locate(tree, key) {
        for (const [node, parent] of walk(tree)) if (node.key === key) return { node, parent };
        return null;
    }

    function pathTo(tree, key) {
        if (tree.key === key) return [tree];
        for (const child of tree.children) {
            const path = pathTo(child, key);
            if (path) return [tree, ...path];
        }
        return null;
    }

    const childOf = (node, tag) => node?.children.find((child) => child.tag === tag);
    const textOf = (node, tag) => (childOf(node, tag)?.text || "").trim();
    const element = (tag, attributes = {}, text = "", children = []) => ({
        key: `new${++keyCounter}-${Date.now().toString(36)}`,
        tag,
        attributes,
        text,
        children
    });
    const withNewKeys = (tree) => element(tree.tag, { ...tree.attributes }, tree.text, tree.children.map(withNewKeys));

    function newUuid() {
        if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID().toUpperCase();
        const digit = (index) => (index === 12 ? "4" : index === 16 ? "89ab"[Math.floor(Math.random() * 4)] : Math.floor(Math.random() * 16).toString(16));
        const hex = Array.from({ length: 32 }, (_, index) => digit(index)).join("").toUpperCase();
        return hex.replace(/(.{8})(.{4})(.{4})(.{4})(.{12})/, "$1-$2-$3-$4-$5");
    }

    const nodeSchema = (settings, key) => settings.schema.nodes[key];
    const fieldOf = (settings, key, tag) => nodeSchema(settings, key)?.children.find((field) => field.name === tag);

    function childKey(settings, parentKey, node) {
        const autolux = node.tag === "Data" && node.attributes.provider === settings.schema.autolux_provider;
        if (parentKey === "UserData" && autolux) return "AutoluxData";
        const field = fieldOf(settings, parentKey, node.tag);
        return field && !field.kind ? field.key : null;
    }

    function schemaKeys(path, settings) {
        const keys = ["GeneralSceneDescription"];
        for (const node of path.slice(1)) keys.push(keys.at(-1) && childKey(settings, keys.at(-1), node));
        return keys;
    }

    function insertChild(parent, parentKey, child, settings) {
        const order = (nodeSchema(settings, parentKey)?.children || []).map((field) => field.name);
        const rank = order.indexOf(child.tag);
        const position = parent.children.findIndex((other) => order.indexOf(other.tag) > rank);
        parent.children.splice(rank < 0 || position < 0 ? parent.children.length : position, 0, child);
        return child;
    }

    const DEFAULTS = { matrix: () => formatMatrix(IDENTITY), integer: () => "0", float: () => "0", bool: () => "false", uuid: newUuid, dmx_address: () => "1" };
    const defaultValue = (field) => (field.kind === "enum" ? field.values[0] || "" : DEFAULTS[field.kind]?.() ?? "");

    function template(settings, key, tag) {
        const schema = nodeSchema(settings, key);
        const node = element(tag);
        for (const field of schema.attributes) {
            if (field.count === "1" || field.name === "uuid") node.attributes[field.name] = defaultValue(field);
        }
        if (schema.text) node.text = defaultValue(schema.text);
        for (const field of schema.children.filter((child) => child.count === "1")) {
            node.children.push(field.kind ? element(field.name, {}, defaultValue(field)) : template(settings, field.key, field.name));
        }
        return node;
    }

    function finiteNumbers(text, count) {
        const values = text.split(",").map((part) => (part.trim() === "" ? NaN : Number(part)));
        return values.length === count && values.every(Number.isFinite) ? values : null;
    }

    function parseMatrix(text) {
        if (!text || !text.trim()) return [...IDENTITY];
        const rows = [...text.matchAll(/\{([^{}]*)\}/g)].map((row) => finiteNumbers(row[1], 3));
        const clean = rows.length === 4 && !text.replace(/\{([^{}]*)\}/g, "").trim();
        return clean && !rows.includes(null) ? rows.flat() : null;
    }

    function formatNumber(value) {
        const text = String(Number(value.toFixed(MATRIX_DECIMALS)));
        return text === "-0" ? "0" : text;
    }

    const formatMatrix = (matrix) => [0, 3, 6, 9].map((start) => `{${matrix.slice(start, start + 3).map(formatNumber).join(",")}}`).join("");

    function multiply(parent, child) {
        const apply = (vector) => [0, 1, 2].map((row) => [0, 1, 2].reduce((sum, axis) => sum + parent[3 * axis + row] * vector[axis], 0));
        const result = [0, 3, 6, 9].flatMap((start) => apply(child.slice(start, start + 3)));
        return result.map((value, index) => (index < 9 ? value : value + parent[index]));
    }

    const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
    const toDegrees = (radians) => (radians * 180) / Math.PI;

    function canonicalDegrees(angle) {
        let wrapped = angle - 360 * Math.round(angle / 360);
        if (wrapped <= -180) wrapped += 360;
        if (wrapped > 180) wrapped -= 360;
        return wrapped + 0;
    }

    function rotationDegrees(matrix) {
        const axes = [0, 3, 6].map((start) => {
            const axis = matrix.slice(start, start + 3);
            const length = Math.sqrt(dot(axis, axis));
            return length > ORTHONORMAL_TOLERANCE ? axis.map((value) => value / length) : null;
        });
        if (axes.includes(null)) return null;
        const [u, v, w] = axes;
        const cross = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]];
        const skew = Math.max(Math.abs(dot(u, v)), Math.abs(dot(v, w)), Math.abs(dot(u, w)));
        if (skew > ORTHONORMAL_TOLERANCE || dot(cross, w) < 1 - ORTHONORMAL_TOLERANCE) return null;
        const pitch = toDegrees(Math.asin(Math.max(-1, Math.min(1, v[2])))) + 0;
        if (Math.abs(v[2]) > 1 - GIMBAL_TOLERANCE) return [canonicalDegrees(toDegrees(Math.atan2(u[1], u[0]))), pitch, 0];
        return [canonicalDegrees(toDegrees(Math.atan2(-v[0], v[1]))), pitch, canonicalDegrees(toDegrees(Math.atan2(-u[2], w[2])))];
    }

    function homeOrientation(matrix) {
        const rotation = rotationDegrees(matrix);
        if (!rotation) return "Matrix has no plain rotation.";
        if (Math.abs(rotation[1]) > MAX_PITCH_DEGREES) {
            return `Pitch ${rotation[1].toFixed(1)}° exceeds ±${MAX_PITCH_DEGREES}°; save will be refused.`;
        }
        return { pan: rotation[0], tilt: rotation[1], reversed: matrix[8] / Math.hypot(matrix[6], matrix[7], matrix[8]) < 0 };
    }

    function composeMatrix(position, rotation) {
        const [[cy, sy], [cp, sp], [cr, sr]] = rotation.map((value) => [Math.cos((value * Math.PI) / 180), Math.sin((value * Math.PI) / 180)]);
        const values = [
            cy * cr - sy * sp * sr,
            sy * cr + cy * sp * sr,
            -cp * sr,
            -sy * cp,
            cy * cp,
            sp,
            cy * sr + sy * sp * cr,
            sy * sr - cy * sp * cr,
            cp * cr,
            ...position
        ];
        if (!values.every(Number.isFinite)) throw new Error("Position and rotation must be finite numbers.");
        return values.map((value) => Number(value.toFixed(MATRIX_DECIMALS)) + 0);
    }

    function dmxAddress(text) {
        text = (text || "").trim();
        if (/^-?\d+$/.test(text)) {
            const offset = Number(text) - 1;
            return offset < 0 || offset >= MAX_UNIVERSES * UNIVERSE_SIZE ? null : [Math.floor(offset / UNIVERSE_SIZE), (offset % UNIVERSE_SIZE) + 1];
        }
        const parts = text.split(".");
        if (parts.length !== 2 || !parts.every((part) => /^\d+$/.test(part))) return null;
        const [universe, address] = parts.map(Number);
        return universe < 1 || universe > MAX_UNIVERSES || address < 1 || address > UNIVERSE_SIZE ? null : [universe - 1, address];
    }

    const absoluteAddress = (universe, address) => String(universe * UNIVERSE_SIZE + address);
    const gdtfTree = (draft, settings, spec) => draft.gdtf[spec] || settings.library[spec]?.tree || null;

    function modeChannels(tree, mode) {
        const node = tree && [...walk(tree)].map(([entry]) => entry).find((entry) => entry.tag === "DMXMode" && entry.attributes.Name === mode);
        if (!node) return null;
        let channels = 0;
        for (const [channel] of walk(node)) {
            const offsets = channel.attributes.Offset ?? "None";
            if (channel.tag !== "DMXChannel" || (channel.attributes.DMXBreak ?? "1") !== "1" || offsets === "None") continue;
            for (const offset of offsets.split(",")) channels = Math.max(channels, Number(offset));
        }
        return channels;
    }

    function placedObjects(draft, settings) {
        const objects = [];
        const visit = (node, matrix) => {
            for (const child of childOf(node, "ChildList")?.children || []) {
                if (!settings.schema.object_tags.includes(child.tag)) continue;
                const placed = multiply(matrix, parseMatrix(textOf(child, "Matrix")) || [...IDENTITY]);
                objects.push({ node: child, matrix: placed });
                visit(child, placed);
            }
        };
        for (const layer of childOf(childOf(draft.scene, "Scene"), "Layers")?.children || []) {
            if (layer.tag === "Layer") visit(layer, parseMatrix(textOf(layer, "Matrix")) || [...IDENTITY]);
        }
        return objects;
    }

    function classNames(draft) {
        const names = {};
        for (const node of childOf(childOf(draft.scene, "Scene"), "AUXData")?.children || []) {
            if (node.tag === "Class") names[(node.attributes.uuid || "").toUpperCase()] = node.attributes.name || "";
        }
        return names;
    }

    function fixtureRows(draft, settings) {
        const classes = classNames(draft);
        return placedObjects(draft, settings).flatMap(({ node, matrix }) => {
            const spec = textOf(node, "GDTFSpec");
            const addresses = (childOf(node, "Addresses")?.children || []).filter((child) => child.tag === "Address");
            if (node.tag !== "Fixture" || !spec || !addresses.length) return [];
            const mode = textOf(node, "GDTFMode");
            const [universe, address] = dmxAddress(addresses[0].text) || [null, null];
            const breaks = addresses.map((entry) => entry.attributes.break ?? "0");
            const channels = modeChannels(gdtfTree(draft, settings, spec), mode);
            const className = classes[textOf(node, "Classing").toUpperCase()] || "";
            return [{ key: node.key, name: node.attributes.name || "", spec, mode, breaks, addressKey: addresses[0].key, universe, address, channels, className, matrix }];
        });
    }

    function rowError(row, label, settings) {
        if (row.breaks.length !== 1 || Number(row.breaks[0]) !== 0) return `${label}: Starfire renders one Address on break 0 per fixture.`;
        const home = homeOrientation(row.matrix);
        if (typeof home === "string") return `${label}: ${home}`;
        if (row.universe === null) return `${label}: invalid DMX address.`;
        if (row.universe > settings.maxUniverse) return `${label}: universe ${row.universe + 1} exceeds Art-Net.`;
        if (row.channels === null) return `${label}: GDTF mode '${row.mode}' is not in ${row.spec}.`;
        if (row.address + row.channels - 1 > settings.channelCount) return `${label}: ${row.channels} channels do not fit at address ${row.address}.`;
        return null;
    }

    function occupancy(rows, settings, universe) {
        const occupied = Array.from({ length: settings.channelCount }, () => []);
        const errors = [];
        const names = new Set();
        const owners = new Map();
        rows.forEach((row, index) => {
            const label = row.name || `Fixture ${index + 1}`;
            if (!row.name.trim() || names.has(row.name)) errors.push(`${label}: Starfire needs a nonempty, unique, short fixture name.`);
            names.add(row.name);
            const error = rowError(row, label, settings);
            if (error) return errors.push(error);
            const conflicts = new Set();
            for (let channel = row.address; channel < row.address + row.channels; channel++) {
                const key = `${row.universe}:${channel}`;
                if (owners.has(key)) conflicts.add(owners.get(key));
                owners.set(key, index);
                if (row.universe === universe) occupied[channel - 1].push(index);
            }
            conflicts.forEach((other) => errors.push(`${label} overlaps ${rows[other].name} in universe ${row.universe + 1}.`));
        });
        if (!rows.length) errors.push("A rig needs at least one patched fixture.");
        return { occupied, errors };
    }

    function requireValid(draft, settings, universe = 0) {
        const result = occupancy(fixtureRows(draft, settings), settings, universe);
        if (result.errors.length) throw new Error(result.errors[0]);
        return result.occupied;
    }

    function firstFreeAddress(draft, settings, universe, count) {
        let free = 0;
        const occupied = requireValid(draft, settings, universe);
        for (let index = 0; index < occupied.length; index++) {
            free = occupied[index].length ? 0 : free + 1;
            if (free === count) return index - count + 2;
        }
        throw new Error(`No room for ${count} consecutive channels in universe ${universe + 1}.`);
    }

    function firstLayerChildren(draft, settings) {
        const scene = childOf(draft.scene, "Scene");
        const layers = childOf(scene, "Layers") || insertChild(scene, "Scene", element("Layers"), settings);
        const layer = childOf(layers, "Layer") || insertChild(layers, "Layers", template(settings, "Layer", "Layer"), settings);
        return childOf(layer, "ChildList") || insertChild(layer, "Layer", element("ChildList"), settings);
    }

    function addFixtures(draft, settings, spec, quantity, universe, address) {
        const entry = settings.library[spec];
        if (!entry) throw new Error("Select a valid fixture profile.");
        if (!Number.isInteger(quantity) || quantity < 1 || quantity > settings.maxQuantity) {
            throw new Error(`Quantity must be from 1 to ${settings.maxQuantity}.`);
        }
        if (!Number.isInteger(universe) || universe < 1 || universe > settings.maxUniverse + 1) throw new Error("Choose a valid universe.");
        if (!draft.gdtf[spec]) draft.gdtf[spec] = withNewKeys(entry.tree);
        const count = entry.modes[entry.default_mode].channels;
        const children = firstLayerChildren(draft, settings);
        const names = new Set(fixtureRows(draft, settings).map((row) => row.name));
        let fixtureId = Math.max(0, ...[...walk(draft.scene)].filter(([node]) => node.tag === "FixtureIDNumeric").map(([node]) => Number(node.text) || 0));
        const added = [];
        for (let index = 0; index < quantity; index++) {
            const start = address == null ? firstFreeAddress(draft, settings, universe - 1, count) : address;
            let suffix = 1;
            while (names.has(`${entry.name}_${suffix}`)) suffix++;
            const fixture = template(settings, "Fixture", "Fixture");
            fixture.attributes.name = `${entry.name}_${suffix}`;
            names.add(fixture.attributes.name);
            fixtureId += 1;
            for (const [tag, text] of [["Matrix", formatMatrix(IDENTITY)], ["GDTFSpec", spec], ["GDTFMode", entry.default_mode]]) {
                insertChild(fixture, "Fixture", element(tag, {}, text), settings);
            }
            childOf(fixture, "FixtureID").text = childOf(fixture, "FixtureIDNumeric").text = String(fixtureId);
            const addresses = insertChild(fixture, "Fixture", element("Addresses"), settings);
            addresses.children.push(element("Address", { break: "0" }, absoluteAddress(universe - 1, start)));
            children.children.push(fixture);
            added.push(fixture.key);
            if (address != null) address += count;
        }
        return added[0];
    }

    const ACTIONS = {
        move(draft, settings, action) {
            const row = fixtureRows(draft, settings).find((entry) => entry.key === action.key);
            if (!row) throw new Error("The fixture changed. Select it again.");
            if (row.address !== action.original_address || row.universe !== action.original_universe) {
                throw new Error("The fixture moved while dragging. Try again.");
            }
            locate(draft.scene, row.addressKey).node.text = absoluteAddress(action.universe, action.address);
            requireValid(draft, settings);
        },
        "set-attribute"(draft, settings, action, { node, document }) {
            if (document !== draft.scene && !settings.schema.gdtf_editable[node.tag]?.[action.name]) {
                throw new Error(`GDTF ${node.tag}@${action.name} is read-only.`);
            }
            if (action.value === null) delete node.attributes[action.name];
            else node.attributes[action.name] = action.value;
        },
        "set-text"(draft, settings, action, { node }) {
            node.text = action.value;
        },
        "set-child-text"(draft, settings, action, { node }) {
            const key = schemaKeys(pathTo(draft.scene, node.key), settings).at(-1);
            const child = childOf(node, action.tag);
            if (action.value !== null) (child || insertChild(node, key, element(action.tag), settings)).text = action.value;
            else if (child) node.children.splice(node.children.indexOf(child), 1);
        },
        "set-matrix"(draft, settings, action) {
            const values = [...action.position, ...action.rotation].map(Number);
            if (!values.every(Number.isFinite)) throw new Error("Position and rotation must be numbers.");
            const value = formatMatrix(composeMatrix(values.slice(0, 3), values.slice(3)));
            return applyAction(draft, settings, { action: "set-child-text", key: action.key, tag: "Matrix", value });
        },
        "set-address"(draft, settings, action, { node }) {
            const [universe, address] = [Number(action.universe), Number(action.address)];
            if (!Number.isInteger(universe) || universe < 1 || universe > MAX_UNIVERSES) {
                throw new Error(`Universe must be a whole number from 1 to ${MAX_UNIVERSES}.`);
            }
            if (!Number.isInteger(address) || address < 1 || address > UNIVERSE_SIZE) {
                throw new Error(`DMX address must be a whole number from 1 to ${UNIVERSE_SIZE}.`);
            }
            node.text = absoluteAddress(universe - 1, address);
        },
        "new-uuid"(draft, settings, action, { node }) {
            node.attributes.uuid = newUuid();
        },
        "add-child"(draft, settings, action, { node }) {
            const parentKey = schemaKeys(pathTo(draft.scene, node.key), settings).at(-1);
            const field = fieldOf(settings, parentKey, action.tag);
            if (!field || field.kind) throw new Error(`${action.tag} cannot be added here.`);
            const provider = settings.schema.autolux_provider;
            const autolux = action.tag === "Data" && action.provider === provider;
            const created = template(settings, autolux ? "AutoluxData" : field.key, action.tag);
            if (autolux) created.attributes.provider = provider;
            return insertChild(node, parentKey, created, settings).key;
        },
        "remove-node"(draft, settings, action, { node, parent }) {
            if (!parent) throw new Error("The scene root cannot be removed.");
            parent.children.splice(parent.children.indexOf(node), 1);
        }
    };

    function applyAction(draft, settings, action) {
        const document = action.doc && action.doc !== "scene" ? draft.gdtf[action.doc] : draft.scene;
        if (!document) throw new Error("That GDTF description is not in the draft.");
        const found = locate(document, action.key);
        if (!found) throw new Error("The node changed. Select it again.");
        if (!ACTIONS[action.action]) throw new Error("Unknown edit.");
        return ACTIONS[action.action](draft, settings, action, { ...found, document });
    }

    function remapSelection(previous, next, key) {
        const path = key && pathTo(previous.scene, key);
        if (!path) return null;
        const uuid = path.at(-1).attributes.uuid;
        if (uuid) return [...walk(next.scene)].find(([node]) => node.attributes.uuid === uuid)?.[0].key ?? null;
        let cursor = next.scene;
        for (let index = 1; index < path.length && cursor; index++) {
            cursor = cursor.children[path[index - 1].children.indexOf(path[index])];
            if (cursor?.tag !== path[index].tag) return null;
        }
        return cursor.key;
    }

    const component = (type, props) => ({ namespace: "dash_html_components", type, props });

    ds.fixture_patch = {
        describeProfile(spec, settings) {
            const entry = settings.library[spec];
            if (!entry) return "No fixture mode selected.";
            return `${entry.manufacturer} · ${entry.modes[entry.default_mode].channels} DMX channels · ${entry.default_mode} · ${spec}`;
        },

        renderPatch(draft, universe, selection, busy, settings) {
            finish();
            const rows = fixtureRows(draft, settings);
            const { occupied, errors } = occupancy(rows, settings, universe);
            const blocks = [];
            for (const index of new Set(occupied.flat())) {
                const row = rows[index];
                const [start, end] = [row.address - 1, row.address - 1 + row.channels];
                const title = `${row.name} · ${start + 1}–${end}`;
                const conflict = occupied.slice(start, end).some((owners) => owners.length > 1);
                for (let segment = start, part = 0; segment < end; part++) {
                    const stop = Math.min(end, (Math.floor(segment / settings.columns) + 1) * settings.columns);
                    blocks.push(
                        component("Button", {
                            key: `${row.key}:${part}`,
                            children: title,
                            title,
                            "data-fixture-key": row.key,
                            "data-fixture-name": row.name,
                            "data-start-address": row.address,
                            "data-channel-count": row.channels,
                            "data-universe": universe,
                            className: "patch-block" + (selection === row.key ? " selected" : "") + (conflict ? " conflict" : ""),
                            style: {
                                gridRow: Math.floor(segment / settings.columns) + 1,
                                gridColumn: `${(segment % settings.columns) + 1} / span ${stop - segment}`,
                                background: settings.colors[index % settings.colors.length]
                            }
                        })
                    );
                    segment = stop;
                }
            }
            const universes = [...new Set([0, universe, ...rows.map((row) => row.universe).filter((value) => value !== null)])].sort((a, b) => a - b);
            return [
                blocks,
                `${occupied.filter((owners) => owners.length).length} / ${settings.channelCount} channels patched`,
                errors.map((error) => component("Div", { children: error })),
                Boolean(errors.length) || busy === "true",
                universes.map((number) => ({ label: `Universe ${number + 1}`, value: number })),
                universe
            ];
        },

        renderRig(selection, draft, settings) {
            const rows = fixtureRows(draft, settings);
            const path = selection ? pathTo(draft.scene, selection) : null;
            if (typeof document.getElementById === "function") {
                const scene = JSON.stringify(draft.scene);
                for (const [id, draw] of [["mvr-plan", drawPlan], ["mvr-tree", drawTree]]) {
                    const container = document.getElementById(id);
                    if (!container) continue;
                    if (drawn.get(container) !== scene) draw(container, draft, settings);
                    drawn.set(container, scene);
                    for (const node of container.querySelectorAll("[data-node-key]")) node.classList.toggle("selected", node.dataset.nodeKey === selection);
                }
                const editor = document.getElementById("mvr-node-editor");
                if (editor) {
                    const sameNode = drawn.get(editor) === selection;
                    drawn.set(editor, selection);
                    keepingState(editor, sameNode, () => editor.replaceChildren(...(path ? inspector(draft, settings, path) : [])));
                }
            }
            const summary = `${rows.length} patched fixtures · ${placedObjects(draft, settings).length} scene objects · ${Object.keys(draft.gdtf).length} GDTF files`;
            const layout = [{ display: "block" }, "patch-workspace has-selection"];
            if (!path) return [...layout, "Select a fixture in the map, plan or tree.", true, summary];
            const node = path.at(-1);
            const row = rows.find((entry) => entry.key === node.key);
            const detail = row
                ? `${row.channels ?? "?"} channels · ${row.address ?? "?"}–${(row.address ?? 0) + (row.channels ?? 1) - 1} · ${row.spec}`
                : `${node.tag}${node.attributes.name ? ` · ${node.attributes.name}` : ""}`;
            return [...layout, detail, path.length === 1, summary];
        },

        editPatch(_add, _remove, _close, action, response, settings, busy, draft, revision, selection, profile, quantity, universe, address) {
            const unchanged = [ds.no_update, ds.no_update, ds.no_update, ds.no_update];
            const persisted = ds.callback_context.triggered?.some((input) => input.prop_id === "fixture-server-result.data");
            const trigger = persisted ? "fixture-server-result" : ds.callback_context.triggered_id;
            if (trigger === "fixture-server-result") {
                if (!response) return unchanged;
                if (response.error) return [ds.no_update, ds.no_update, `Patch not saved: ${response.error}`, ds.no_update];
                return [
                    response.draft,
                    response.revision,
                    response.reloaded ? "Saved patch loaded." : "Patch saved. It will be used by the next session.",
                    response.reloaded ? null : remapSelection(draft, response.draft, selection)
                ];
            }
            if (busy === "true") return unchanged;
            if (trigger === "fixture-close") return [ds.no_update, ds.no_update, ds.no_update, null];
            const edited = structuredClone(draft);
            try {
                if (trigger === "fixture-grid-action") {
                    if (!action) return unchanged;
                    if (action.action === "select") return locate(draft.scene, action.key) ? [...unchanged.slice(0, 3), action.key] : unchanged;
                    const created = applyAction(edited, settings, action);
                    if (action.action === "remove-node" && action.key === selection) selection = null;
                    if (action.action === "add-child" && created) selection = created;
                } else if (trigger === "fixture-add") {
                    selection = addFixtures(edited, settings, profile, quantity, universe, address);
                    requireValid(edited, settings);
                } else if (trigger === "fixture-remove") {
                    if (!selection) throw new Error("Select a node first.");
                    applyAction(edited, settings, { action: "remove-node", key: selection });
                    selection = null;
                } else return unchanged;
                const problems = occupancy(fixtureRows(edited, settings), settings, 0).errors.length;
                const status = "Unsaved changes · save the patch when finished." + (problems ? ` ${problems} patch problem(s) listed under the map.` : "");
                return [edited, ds.no_update, status, selection];
            } catch (error) {
                return [ds.no_update, ds.no_update, `Patch unchanged: ${error.message}`, ds.no_update];
            }
        }
    };

    ds.fixture_patch_model = {
        parseMatrix,
        formatMatrix,
        composeMatrix,
        rotationDegrees,
        homeOrientation,
        multiply,
        dmxAddress,
        fixtureRows,
        occupancy,
        placedObjects,
        schemaKeys,
        pathTo,
        template
    };

    function sendAction(action) {
        window.dash_clientside.set_props("fixture-grid-action", { data: { ...action, request_id: ++requestId } });
    }

    function h(tag, attributes = {}, ...children) {
        const node = document.createElement(tag);
        for (const [name, value] of Object.entries(attributes)) {
            if (value === undefined || value === null || value === false) continue;
            if (name === "className") node.className = value;
            else if (name === "text") node.textContent = value;
            else node.setAttribute(name, value === true ? "" : String(value));
        }
        node.append(...children.flat().filter(Boolean));
        return node;
    }

    function svg(tag, attributes = {}, text = "") {
        const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
        for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, String(value));
        node.textContent = text;
        return node;
    }

    function keepingState(container, sameNode, draw) {
        if (!sameNode) return draw();
        const open = [...container.querySelectorAll("details")].map((details) => details.open);
        const focused = [...container.querySelectorAll("input, select")].indexOf(document.activeElement);
        draw();
        container.querySelectorAll("details").forEach((details, index) => index < open.length && (details.open = open[index]));
        if (focused >= 0) container.querySelectorAll("input, select")[focused]?.focus({ preventScroll: true });
    }

    function drawPlan(container, draft, settings) {
        const objects = placedObjects(draft, settings);
        const classes = classNames(draft);
        const palette = [...new Set(Object.values(classes))];
        const xs = [0, ...objects.map(({ matrix }) => matrix[9])];
        const ys = [0, ...objects.map(({ matrix }) => -matrix[10])];
        const [minX, minY] = [Math.min(...xs), Math.min(...ys)];
        const span = Math.max(PLAN_MIN_SPAN_MM, Math.max(...xs) - minX, Math.max(...ys) - minY);
        const [pad, r] = [span * 0.12, span * 0.018];
        const width = Math.max(...xs) - minX + 2 * pad;
        const height = Math.max(...ys) - minY + 2 * pad;
        const plan = svg("svg", { viewBox: `${minX - pad} ${minY - pad} ${width} ${height}`, preserveAspectRatio: "xMidYMid meet" });
        plan.append(svg("circle", { cx: 0, cy: 0, r: r * 0.3, fill: "#3b4d62" }));
        for (const { node, matrix } of objects) {
            const [x, y] = [matrix[9], -matrix[10]];
            const group = svg("g", { class: "mvr-object", "data-node-key": node.key });
            if (node.tag === "Fixture") {
                const name = classes[textOf(node, "Classing").toUpperCase()];
                const colour = name ? settings.colors[palette.indexOf(name) % settings.colors.length] : "#7f8ea3";
                const length = Math.hypot(matrix[3], matrix[4]) || 1;
                const [x2, y2] = [x + (matrix[3] / length) * r * 2.4, y - (matrix[4] / length) * r * 2.4];
                group.append(svg("line", { x1: x, y1: y, x2, y2, stroke: colour, "stroke-width": r * 0.35 }));
                group.append(svg("circle", { cx: x, cy: y, r, fill: colour, stroke: "#0b121c", "stroke-width": r * 0.25 }));
            } else {
                const fill = node.tag === "GroupObject" ? "none" : "#455468";
                group.append(svg("rect", { x: x - r * 0.8, y: y - r * 0.8, width: r * 1.6, height: r * 1.6, fill, stroke: "#6d7f95", "stroke-width": r * 0.2 }));
            }
            group.append(svg("text", { x: x + r * 1.4, y: y + r * 0.4, "font-size": r * 1.3 }, node.attributes.name || node.tag));
            plan.append(group);
        }
        container.replaceChildren(plan);
    }

    function nodeLabel(node) {
        const name = node.attributes.name || node.attributes.provider || node.attributes.fileName || "";
        return [h("span", { className: "mvr-tag", text: node.tag }), name ? ` ${name}` : ""];
    }

    function drawTree(container, draft, settings) {
        const rows = (node, key, depth) => [
            h("div", { className: "mvr-tree-row", "data-node-key": node.key, style: `padding-left:${depth * 12 + 4}px` }, nodeLabel(node)),
            ...(nodeSchema(settings, key)?.any_content ? [] : node.children)
                .filter((child) => !(key && fieldOf(settings, key, child.tag)?.kind))
                .flatMap((child) => rows(child, key && childKey(settings, key, child), depth + 1))
        ];
        container.replaceChildren(...rows(draft.scene, "GeneralSceneDescription", 0));
    }

    const button = (text, action, title) => h("button", { type: "button", className: "mvr-small", "data-mvr-action": JSON.stringify(action), title, text });
    const row = (label, input, extra) => h("div", { className: "mvr-row" }, h("label", { text: label, title: label }), input, extra || h("span"));
    const muted = (text) => h("div", { className: "patch-muted", text });

    function choice(options, value, edit, optional) {
        const select = h(
            "select",
            { "data-mvr-edit": JSON.stringify(edit), "data-optional": optional },
            optional || !options.some(([option]) => option === value) ? h("option", { value: "", text: "—" }) : null,
            options.map(([option, label]) => h("option", { value: option, text: label }))
        );
        select.value = value ?? "";
        return select;
    }

    const textInput = (value, edit, optional) => h("input", { type: "text", value: value ?? "", "data-mvr-edit": JSON.stringify(edit), "data-optional": optional, spellcheck: "false" });

    function references(draft, targets) {
        return [...walk(draft.scene)]
            .map(([node]) => node)
            .filter((node) => targets.includes(node.tag) && node.attributes.uuid)
            .map((node) => [node.attributes.uuid, `${node.attributes.name || node.tag} · ${node.attributes.uuid.slice(0, 8)}`]);
    }

    function matrixHelper(owner, text) {
        const matrix = parseMatrix(text);
        const rotation = matrix && rotationDegrees(matrix);
        if (!rotation) return [muted("Matrix has no plain rotation; edit the raw value.")];
        const labels = ["x mm", "y mm", "z mm", "yaw °", "pitch °", "roll °"];
        const inputs = [...matrix.slice(9), ...rotation].map((value, index) => h("input", { type: "number", step: "any", value: formatNumber(value), title: labels[index], placeholder: labels[index] }));
        const home = homeOrientation(matrix);
        const summary = typeof home === "string" ? home : `Starfire home: pan ${formatNumber(home.pan)}°, tilt ${formatNumber(home.tilt)}°` + (home.reversed ? " · standing: pan/tilt reversed" : " · hanging");
        return [h("div", { className: "mvr-helper", "data-mvr-matrix": owner.key }, inputs), muted(summary)];
    }

    function addressHelper(node) {
        const [universe, address] = dmxAddress(node.text) || ["", ""];
        return [
            h(
                "div",
                { className: "mvr-helper", "data-mvr-address": node.key },
                h("input", { type: "number", min: 1, max: MAX_UNIVERSES, step: 1, value: universe === "" ? "" : universe + 1, title: "Universe" }),
                h("input", { type: "number", min: 1, max: UNIVERSE_SIZE, step: 1, value: address, title: "Address" }),
                h("span", { className: "patch-muted", text: "universe · address" })
            )
        ];
    }

    const FIELD_TYPES = {
        enum: (field, value, edit, context) => [choice(field.values.map((entry) => [entry, entry]), value, edit, context.optional)],
        bool: (field, value, edit, context) => [choice([["true", "true"], ["false", "false"]], value, edit, context.optional)],
        uuid: (field, value, edit, context) =>
            field.target.length
                ? [choice(references(context.draft, field.target), value, edit, context.optional)]
                : [textInput(value, edit, context.optional), button("new", { action: "new-uuid", key: context.node.key }, "Generate a new UUID")],
        matrix: (field, value, edit, context) => [textInput(value, edit, context.optional), null, matrixHelper(context.node, value)],
        dmx_address: (field, value, edit, context) => [textInput(value, edit, context.optional), null, addressHelper(context.node)],
        default: (field, value, edit, context) => [textInput(value, edit, context.optional)]
    };

    function fieldRows(label, field, value, edit, context, extra) {
        const [input, own, helpers = []] = (FIELD_TYPES[field.kind] || FIELD_TYPES.default)(field, value, edit, context);
        return [row(label, input, own || extra), ...helpers];
    }

    function gdtfDetails(draft, settings, spec) {
        const tree = gdtfTree(draft, settings, spec);
        if (!tree) return [muted(`${spec} is not embedded or installed.`)];
        const visit = (node, depth) =>
            h(
                "details",
                { open: depth < 2 || node.tag === "DMXModes" },
                h("summary", {}, nodeLabel(node)),
                Object.entries(node.attributes).map(([name, value]) =>
                    settings.schema.gdtf_editable[node.tag]?.[name]
                        ? row(name, textInput(value, { action: "set-attribute", doc: spec, key: node.key, name }))
                        : h("div", { className: "mvr-gdtf-attribute", text: `${name} = ${value}` })
                ),
                node.text ? h("div", { className: "mvr-gdtf-attribute", text: node.text }) : null,
                node.children.map((child) => visit(child, depth + 1))
            );
        return [h("h4", { text: `GDTF · ${spec}` }), muted("Read-only except DMX mode names and channel function/set ranges; a changed GDTF gains a Revision on save."), visit(tree, 0)];
    }

    function inspector(draft, settings, path) {
        const node = path.at(-1);
        const key = schemaKeys(path, settings).at(-1);
        const schema = nodeSchema(settings, key);
        const crumbs = h("div", { className: "patch-muted" }, path.slice(0, -1).map((entry) => h("span", { className: "mvr-link", "data-node-key": entry.key }, entry.tag, " › ")), h("strong", { text: node.tag }));
        if (!schema) return [crumbs, muted(`${node.tag} is not an MVR 1.6 node here; saving will be refused until it is removed.`)];
        const context = { draft, node };
        const parts = [crumbs, h("h4", { text: "Attributes" })];
        for (const field of schema.attributes) {
            const required = field.count === "1";
            const edit = { action: "set-attribute", doc: "scene", key: node.key, name: field.name };
            parts.push(...fieldRows(field.name + (required ? " *" : ""), field, node.attributes[field.name], edit, { ...context, optional: !required }));
        }
        for (const name of Object.keys(node.attributes).filter((name) => !schema.attributes.some((field) => field.name === name))) {
            const remove = button("×", { action: "set-attribute", doc: "scene", key: node.key, name, value: null }, "Remove");
            parts.push(row(`${name} (not MVR)`, h("span", { className: "mvr-readonly", text: node.attributes[name] }), remove));
        }
        if (schema.any_content) return [...parts, muted("Provider data is preserved verbatim and not edited here.")];
        if (schema.text) parts.push(h("h4", { text: "Value" }), ...fieldRows(node.tag, schema.text, node.text, { action: "set-text", doc: "scene", key: node.key }, context));
        const values = schema.children.filter((field) => field.kind);
        if (values.length) parts.push(h("h4", { text: "Fields" }));
        for (const field of values) {
            const label = field.name + (field.count === "1" ? " *" : "");
            const edit = { action: "set-child-text", key: node.key, tag: field.name };
            const present = childOf(node, field.name);
            if (!present) parts.push(row(label, h("span", { className: "mvr-readonly", text: "not set" }), button("+", { ...edit, value: defaultValue(field) }, `Add ${field.name}`)));
            else parts.push(...fieldRows(label, field, present.text, edit, context, field.count === "1" ? null : button("×", { ...edit, value: null }, `Remove ${field.name}`)));
        }
        const structured = schema.children.filter((field) => !field.kind);
        if (structured.length) {
            const adders = structured
                .filter((field) => field.count === "0..n" || !childOf(node, field.name))
                .map((field) => button(`+ ${field.name}`, { action: "add-child", key: node.key, tag: field.name }, `Add ${field.name}`));
            if (key === "UserData") adders.push(button("+ Autolux data", { action: "add-child", key: node.key, tag: "Data", provider: settings.schema.autolux_provider }, "Movement limits"));
            parts.push(
                h("h4", { text: "Children" }),
                ...node.children.filter((child) => structured.some((field) => field.name === child.tag)).map((child) => h("div", { className: "mvr-link", "data-node-key": child.key }, nodeLabel(child))),
                h("div", { className: "mvr-adders" }, adders)
            );
        }
        const spec = textOf(node, "GDTFSpec");
        return spec ? [...parts, ...gdtfDetails(draft, settings, spec)] : parts;
    }

    function geometry(grid) {
        const cell = grid.querySelector(".patch-channel").getBoundingClientRect();
        const style = getComputedStyle(grid);
        return {
            rect: grid.getBoundingClientRect(),
            columns: Number(grid.dataset.columns),
            count: Number(grid.dataset.channelCount),
            width: cell.width + parseFloat(style.columnGap),
            height: cell.height + parseFloat(style.rowGap)
        };
    }

    function channelAt({ rect, columns, count, width, height }, x, y) {
        if (x < rect.left || x >= rect.right || y < rect.top || y >= rect.bottom) return null;
        const channel = Math.floor((y - rect.top) / height) * columns + Math.floor((x - rect.left) / width) + 1;
        return channel <= count ? channel : null;
    }

    const clearPreview = (grid) => grid.querySelectorAll(".drop-valid, .drop-invalid").forEach((cell) => cell.classList.remove("drop-valid", "drop-invalid"));

    function preview(event) {
        const { grid, fixtureName, offset, channelCount, label, cells, ranges, geometry } = drag;
        const channel = channelAt(geometry, event.clientX, event.clientY);
        label.style.left = `${event.clientX + LABEL_OFFSET_PX}px`;
        label.style.top = `${event.clientY + LABEL_OFFSET_PX}px`;
        if (channel === drag.hovered) return channel === null ? null : channel - offset;
        drag.hovered = channel;
        clearPreview(grid);
        if (channel === null) {
            label.textContent = "Drop inside the universe to move";
            label.classList.add("invalid");
            return null;
        }
        const start = channel - offset;
        const end = start + channelCount - 1;
        const valid = start >= 1 && end <= geometry.count && !ranges.some(([low, high]) => start <= high && end >= low);
        for (let address = Math.max(1, start); address <= Math.min(geometry.count, end); address++) {
            cells[address - 1].classList.add(valid ? "drop-valid" : "drop-invalid");
        }
        label.classList.toggle("invalid", !valid);
        label.textContent = `${fixtureName} · ${start}–${end}${valid ? "" : " · unavailable"}`;
        return start;
    }

    function finish() {
        if (!drag) return;
        const current = drag;
        drag = null;
        cancelAnimationFrame(current.frame);
        clearPreview(current.grid);
        current.grid.querySelectorAll(".dragging").forEach((block) => block.classList.remove("dragging"));
        current.label?.remove();
        if (current.moved) {
            suppressedClick = current.block;
            setTimeout(() => suppressedClick === current.block && (suppressedClick = null));
        }
        if (current.grid.hasPointerCapture(current.pointerId)) current.grid.releasePointerCapture(current.pointerId);
    }

    document.addEventListener("pointerdown", (event) => {
        const block = event.target.closest("#fixture-channel-map .patch-block");
        if (!block || event.button !== 0 || !event.isPrimary || block.closest('[aria-busy="true"]')) return;
        finish();
        suppressedClick = null;
        const grid = block.closest(".patch-map");
        const address = Number(block.dataset.startAddress);
        const layout = geometry(grid);
        const channel = channelAt(layout, event.clientX, event.clientY);
        if (channel === null) return;
        const others = [...grid.querySelectorAll(".patch-block")].filter((other) => other.dataset.fixtureKey !== block.dataset.fixtureKey);
        drag = {
            block,
            grid,
            address,
            fixtureKey: block.dataset.fixtureKey,
            fixtureName: block.dataset.fixtureName,
            universe: Number(block.dataset.universe),
            channelCount: Number(block.dataset.channelCount),
            offset: channel - address,
            x: event.clientX,
            y: event.clientY,
            pointerId: event.pointerId,
            moved: false,
            label: null,
            geometry: layout,
            cells: [...grid.querySelectorAll(".patch-channel")],
            ranges: others.map((other) => [Number(other.dataset.startAddress), Number(other.dataset.startAddress) + Number(other.dataset.channelCount) - 1])
        };
    });

    document.addEventListener("pointermove", (event) => {
        if (!drag || drag.pointerId !== event.pointerId) return;
        if (!drag.moved) {
            if (Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < DRAG_THRESHOLD_PX) return;
            drag.moved = true;
            drag.grid.setPointerCapture(event.pointerId);
            drag.label = document.body.appendChild(h("div", { className: "patch-drag-label" }));
            drag.grid.querySelectorAll(".patch-block").forEach((block) => block.dataset.fixtureKey === drag.fixtureKey && block.classList.add("dragging"));
        }
        event.preventDefault();
        drag.event = event;
        drag.frame ||= requestAnimationFrame(() => {
            drag.frame = null;
            preview(drag.event);
        });
    });

    document.addEventListener("pointerup", (event) => {
        if (!drag || drag.pointerId !== event.pointerId) return;
        const current = drag;
        const address = current.moved ? preview(event) : null;
        finish();
        if (!current.moved || address === null) return;
        event.preventDefault();
        sendAction({
            action: "move",
            key: current.fixtureKey,
            original_address: current.address,
            original_universe: current.universe,
            universe: Number(current.grid.dataset.universe),
            address
        });
    });

    document.addEventListener(
        "click",
        (event) => {
            const block = event.target.closest("#fixture-channel-map .patch-block");
            const busy = event.target.closest('#config-panel[aria-busy="true"]');
            const action = event.target.closest("#config-panel [data-mvr-action]");
            if (!busy && action) return sendAction(JSON.parse(action.dataset.mvrAction));
            if (busy || (block && suppressedClick === block)) {
                if (!busy) suppressedClick = null;
                event.preventDefault();
                event.stopImmediatePropagation();
                return;
            }
            const key = block?.dataset.fixtureKey ?? event.target.closest("#config-panel [data-node-key]")?.dataset.nodeKey;
            if (key) sendAction({ action: "select", key });
        },
        true
    );

    document.addEventListener("change", (event) => {
        if (event.target.closest('#config-panel[aria-busy="true"]')) return;
        const edit = event.target.closest("#config-panel [data-mvr-edit]");
        const numbers = (container) => [...container.querySelectorAll("input")].map((input) => Number(input.value));
        if (edit) {
            const optional = edit.dataset.optional !== undefined && edit.dataset.optional !== "false";
            return sendAction({ ...JSON.parse(edit.dataset.mvrEdit), value: edit.value === "" && optional ? null : edit.value });
        }
        const matrix = event.target.closest("#config-panel [data-mvr-matrix]");
        if (matrix) {
            const values = numbers(matrix);
            return sendAction({ action: "set-matrix", key: matrix.dataset.mvrMatrix, position: values.slice(0, 3), rotation: values.slice(3) });
        }
        const address = event.target.closest("#config-panel [data-mvr-address]");
        if (address) {
            const [universe, start] = numbers(address);
            sendAction({ action: "set-address", key: address.dataset.mvrAddress, universe, address: start });
        }
    });

    document.addEventListener("pointercancel", finish);
    document.addEventListener("lostpointercapture", (event) => drag?.pointerId === event.pointerId && finish());
    document.addEventListener("keydown", (event) => {
        if (event.target.closest?.('#config-panel[aria-busy="true"]')) event.preventDefault();
        if (event.key === "Escape" && drag) {
            event.preventDefault();
            finish();
        }
    });
    document.addEventListener("scroll", finish, true);
    window.addEventListener("resize", finish);
    window.addEventListener("blur", finish);
})();
