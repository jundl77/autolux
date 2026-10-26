(() => {
    const ds = (window.dash_clientside = window.dash_clientside || {});
    const DRAG_THRESHOLD_PX = 4;
    const LABEL_OFFSET_PX = 16;
    const MATRIX_DECIMALS = 6;
    const ORTHONORMAL_TOLERANCE = 1e-3;
    const GIMBAL_TOLERANCE = 1e-9;
    const MAX_PITCH_DEGREES = 60;
    const DMX_UNIVERSE_SIZE = 512;
    const MAX_DMX_ADDRESS = 32768 * DMX_UNIVERSE_SIZE;
    const PLAN_MIN_SPAN_MM = 2000;
    const IDENTITY = [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0];
    let drag = null;
    let suppressedClick = null;
    let requestId = 0;
    let keyCounter = 0;

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

    const childOf = (node, tag) => node.children.find((child) => child.tag === tag);
    const textOf = (node, tag) => (childOf(node, tag)?.text || "").trim();

    function newKey() {
        keyCounter += 1;
        return `new${keyCounter}-${Date.now().toString(36)}`;
    }

    function element(tag, attributes = {}, text = "", children = []) {
        return { key: newKey(), tag, attributes, text, children };
    }

    function withNewKeys(tree) {
        return { ...tree, key: newKey(), attributes: { ...tree.attributes }, children: tree.children.map(withNewKeys) };
    }

    function newUuid() {
        const crypto = globalThis.crypto;
        if (crypto?.randomUUID) return crypto.randomUUID().toUpperCase();
        const hex = Array.from({ length: 32 }, () => Math.floor(Math.random() * 16).toString(16));
        hex[12] = "4";
        hex[16] = "89ab"[Math.floor(Math.random() * 4)];
        const text = hex.join("").toUpperCase();
        return `${text.slice(0, 8)}-${text.slice(8, 12)}-${text.slice(12, 16)}-${text.slice(16, 20)}-${text.slice(20)}`;
    }

    function childDefinition(settings, parentKey, tag) {
        return settings.schema.nodes[parentKey]?.children.find((child) => child.tag === tag);
    }

    function childKey(settings, parentKey, child, definition) {
        if (parentKey === "UserData" && child.tag === "Data" && child.attributes.provider === settings.schema.autolux_provider) {
            return "AutoluxData";
        }
        return definition.key;
    }

    function schemaKeys(path, settings) {
        const keys = ["GeneralSceneDescription"];
        for (let index = 1; index < path.length; index++) {
            const parentKey = keys[index - 1];
            const definition = parentKey && childDefinition(settings, parentKey, path[index].tag);
            keys.push(!definition || definition.kind ? null : childKey(settings, parentKey, path[index], definition));
        }
        return keys;
    }

    function insertChild(parent, parentKey, child, settings) {
        const order = (settings.schema.nodes[parentKey]?.children || []).map((entry) => entry.tag);
        const rank = order.indexOf(child.tag);
        const position = parent.children.findIndex((other) => order.indexOf(other.tag) > rank);
        if (rank < 0 || position < 0) parent.children.push(child);
        else parent.children.splice(position, 0, child);
        return child;
    }

    function defaultValue(kind, values) {
        if (kind === "matrix") return formatMatrix(IDENTITY);
        if (kind === "integer" || kind === "float") return "0";
        if (kind === "bool") return "false";
        if (kind === "uuid") return newUuid();
        if (kind === "dmx_address") return "1";
        if (kind === "enum") return values[0] || "";
        return "";
    }

    function template(settings, key, tag) {
        const schema = settings.schema.nodes[key];
        const node = element(tag);
        for (const attribute of schema.attributes) {
            if (attribute.required || attribute.name === "uuid")
                node.attributes[attribute.name] = defaultValue(attribute.kind, attribute.values);
        }
        if (schema.text) node.text = defaultValue(schema.text, schema.values);
        for (const child of schema.children) {
            if (child.count !== "1") continue;
            node.children.push(
                child.kind
                    ? element(child.tag, {}, defaultValue(child.kind, child.values))
                    : template(settings, child.key, child.tag)
            );
        }
        return node;
    }

    function finiteNumbers(text, count) {
        const parts = text.split(",");
        if (parts.length !== count) return null;
        const values = parts.map((part) => (part.trim() === "" ? NaN : Number(part)));
        return values.every(Number.isFinite) ? values : null;
    }

    function parseMatrix(text) {
        if (!text || !text.trim()) return [...IDENTITY];
        const rows = [...text.matchAll(/\{([^{}]*)\}/g)];
        if (rows.length !== 4 || text.replace(/\{([^{}]*)\}/g, "").trim()) return null;
        const values = rows.map((row) => finiteNumbers(row[1], 3));
        return values.includes(null) ? null : values.flat();
    }

    function formatNumber(value) {
        const text = String(Number(value.toFixed(MATRIX_DECIMALS)));
        return text === "-0" ? "0" : text;
    }

    function formatMatrix(matrix) {
        return [0, 3, 6, 9].map((start) => `{${matrix.slice(start, start + 3).map(formatNumber).join(",")}}`).join("");
    }

    function multiply(parent, child) {
        const apply = (vector) =>
            [0, 1, 2].map((row) => [0, 1, 2].reduce((sum, axis) => sum + parent[3 * axis + row] * vector[axis], 0));
        const result = [0, 3, 6, 9].flatMap((start) => apply(child.slice(start, start + 3)));
        for (let row = 0; row < 3; row++) result[9 + row] += parent[9 + row];
        return result;
    }

    const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];

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
        if (
            Math.max(Math.abs(dot(u, v)), Math.abs(dot(v, w)), Math.abs(dot(u, w))) > ORTHONORMAL_TOLERANCE ||
            dot(cross, w) < 1 - ORTHONORMAL_TOLERANCE
        )
            return null;
        const degrees = (radians) => (radians * 180) / Math.PI;
        const pitch = degrees(Math.asin(Math.max(-1, Math.min(1, v[2]))));
        if (Math.abs(v[2]) > 1 - GIMBAL_TOLERANCE)
            return [canonicalDegrees(degrees(Math.atan2(u[1], u[0]))), pitch + 0, 0];
        return [
            canonicalDegrees(degrees(Math.atan2(-v[0], v[1]))),
            pitch + 0,
            canonicalDegrees(degrees(Math.atan2(-u[2], w[2])))
        ];
    }

    function homeOrientation(matrix) {
        const rotation = rotationDegrees(matrix);
        if (!rotation) return "Matrix has no plain rotation.";
        if (Math.abs(rotation[1]) > MAX_PITCH_DEGREES) {
            return `Pitch ${rotation[1].toFixed(1)}° exceeds ±${MAX_PITCH_DEGREES}°; save will be refused.`;
        }
        const length = Math.hypot(matrix[6], matrix[7], matrix[8]);
        return { pan: rotation[0], tilt: rotation[1], reversed: matrix[8] / length < 0 };
    }

    function composeMatrix(position, rotation) {
        const [yaw, pitch, roll] = rotation.map((value) => (value * Math.PI) / 180);
        const [cy, sy, cp, sp, cr, sr] = [
            Math.cos(yaw),
            Math.sin(yaw),
            Math.cos(pitch),
            Math.sin(pitch),
            Math.cos(roll),
            Math.sin(roll)
        ];
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
            const absolute = Number(text);
            if (absolute < 1 || absolute > MAX_DMX_ADDRESS) return null;
            return [Math.floor((absolute - 1) / DMX_UNIVERSE_SIZE), ((absolute - 1) % DMX_UNIVERSE_SIZE) + 1];
        }
        const parts = text.split(".");
        if (parts.length !== 2 || !parts.every((part) => /^\d+$/.test(part))) return null;
        const [universe, address] = parts.map(Number);
        if (universe < 1 || universe > 32768 || address < 1 || address > DMX_UNIVERSE_SIZE) return null;
        return [universe - 1, address];
    }

    const absoluteAddress = (universe, address) => String(universe * DMX_UNIVERSE_SIZE + address);

    function gdtfTree(draft, settings, spec) {
        return draft.gdtf[spec] || settings.library[spec]?.tree || null;
    }

    function modeChannels(tree, mode) {
        if (!tree) return null;
        for (const [node] of walk(tree)) {
            if (node.tag !== "DMXMode" || node.attributes.Name !== mode) continue;
            let channels = 0;
            for (const [channel] of walk(node)) {
                if (channel.tag !== "DMXChannel") continue;
                const offsets = channel.attributes.Offset ?? "None";
                if ((channel.attributes.DMXBreak ?? "1") !== "1" || offsets === "None") continue;
                for (const offset of offsets.split(",")) channels = Math.max(channels, Number(offset));
            }
            return channels;
        }
        return null;
    }

    function placedObjects(draft, settings) {
        const objects = [];
        const tags = settings.schema.object_tags;
        const visit = (node, matrix, parentKey) => {
            const children = childOf(node, "ChildList");
            for (const child of children ? children.children : []) {
                if (!tags.includes(child.tag)) continue;
                const local = parseMatrix(textOf(child, "Matrix")) || [...IDENTITY];
                const placed = multiply(matrix, local);
                objects.push({ node: child, matrix: placed, parentKey });
                visit(child, placed, child.key);
            }
        };
        const layers = childOf(childOf(draft.scene, "Scene") || element("Scene"), "Layers");
        for (const layer of layers ? layers.children : []) {
            if (layer.tag === "Layer") visit(layer, parseMatrix(textOf(layer, "Matrix")) || [...IDENTITY], layer.key);
        }
        return objects;
    }

    function classNames(draft) {
        const names = {};
        const auxdata = childOf(childOf(draft.scene, "Scene") || element("Scene"), "AUXData");
        for (const node of auxdata ? auxdata.children : []) {
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
            const location = dmxAddress(addresses[0].text) || [null, null];
            return [
                {
                    key: node.key,
                    name: node.attributes.name || "",
                    spec,
                    mode,
                    breaks: addresses.map((address) => address.attributes.break ?? "0"),
                    addressKey: addresses[0].key,
                    universe: location[0],
                    address: location[1],
                    channels: modeChannels(gdtfTree(draft, settings, spec), mode),
                    className: classes[textOf(node, "Classing").toUpperCase()] || "",
                    matrix
                }
            ];
        });
    }

    function occupancy(rows, settings, universe) {
        const occupied = Array.from({ length: settings.channelCount }, () => []);
        const errors = [],
            names = new Set(),
            addresses = new Map();
        rows.forEach((row, index) => {
            const label = row.name || `Fixture ${index + 1}`;
            if (!row.name.trim() || names.has(row.name)) {
                errors.push(`${label}: Starfire needs a nonempty, unique, short fixture name.`);
            }
            names.add(row.name);
            if (row.breaks.length !== 1 || Number(row.breaks[0]) !== 0) {
                errors.push(`${label}: Starfire renders one Address on break 0 per fixture.`);
                return;
            }
            const home = homeOrientation(row.matrix);
            if (typeof home === "string") {
                errors.push(`${label}: ${home}`);
                return;
            }
            if (row.universe === null) {
                errors.push(`${label}: invalid DMX address.`);
                return;
            }
            if (row.universe > settings.maxUniverse) {
                errors.push(`${label}: universe ${row.universe + 1} exceeds Art-Net.`);
                return;
            }
            if (row.channels === null) {
                errors.push(`${label}: GDTF mode '${row.mode}' is not in ${row.spec}.`);
                return;
            }
            if (row.address + row.channels - 1 > settings.channelCount) {
                errors.push(`${label}: ${row.channels} channels do not fit at address ${row.address}.`);
                return;
            }
            const conflicts = new Set();
            for (let channel = row.address; channel < row.address + row.channels; channel++) {
                const key = `${row.universe}:${channel}`;
                if (addresses.has(key)) conflicts.add(addresses.get(key));
                addresses.set(key, index);
                if (row.universe === universe) occupied[channel - 1].push(index);
            }
            conflicts.forEach((other) =>
                errors.push(`${label} overlaps ${rows[other].name} in universe ${row.universe + 1}.`)
            );
        });
        if (!rows.length) errors.push("A rig needs at least one patched fixture.");
        return { occupied, errors };
    }

    function requireValid(draft, settings) {
        const { errors } = occupancy(fixtureRows(draft, settings), settings, 0);
        if (errors.length) throw new Error(errors[0]);
    }

    function firstFreeAddress(draft, settings, universe, count) {
        const { occupied, errors } = occupancy(fixtureRows(draft, settings), settings, universe);
        if (errors.length) throw new Error(errors[0]);
        let free = 0;
        for (let index = 0; index < occupied.length; index++) {
            free = occupied[index].length ? 0 : free + 1;
            if (free === count) return index - count + 2;
        }
        throw new Error(`No room for ${count} consecutive channels in universe ${universe + 1}.`);
    }

    function firstLayerChildren(draft, settings) {
        const scene = childOf(draft.scene, "Scene");
        let layers = childOf(scene, "Layers");
        if (!layers) layers = insertChild(scene, "Scene", element("Layers"), settings);
        let layer = layers.children.find((child) => child.tag === "Layer");
        if (!layer) layer = insertChild(layers, "Layers", template(settings, "Layer", "Layer"), settings);
        return childOf(layer, "ChildList") || insertChild(layer, "Layer", element("ChildList"), settings);
    }

    function addFixtures(draft, settings, spec, quantity, universe, address) {
        const entry = settings.library[spec];
        if (!entry) throw new Error("Select a valid fixture profile.");
        if (!Number.isInteger(quantity) || quantity < 1 || quantity > settings.maxQuantity) {
            throw new Error(`Quantity must be from 1 to ${settings.maxQuantity}.`);
        }
        if (!Number.isInteger(universe) || universe < 1 || universe > settings.maxUniverse + 1) {
            throw new Error("Choose a valid universe.");
        }
        if (!draft.gdtf[spec]) draft.gdtf[spec] = withNewKeys(entry.tree);
        const count = entry.modes[entry.default_mode].channels;
        const children = firstLayerChildren(draft, settings);
        const names = new Set(fixtureRows(draft, settings).map((row) => row.name));
        let fixtureId = 0;
        for (const [node] of walk(draft.scene)) {
            if (node.tag === "FixtureIDNumeric") fixtureId = Math.max(fixtureId, Number(node.text) || 0);
        }
        const added = [];
        for (let index = 0; index < quantity; index++) {
            const start = address == null ? firstFreeAddress(draft, settings, universe - 1, count) : address;
            let suffix = 1;
            while (names.has(`${entry.name}_${suffix}`)) suffix++;
            const name = `${entry.name}_${suffix}`;
            names.add(name);
            fixtureId += 1;
            const fixture = template(settings, "Fixture", "Fixture");
            fixture.attributes.name = name;
            const value = (tag, text) => insertChild(fixture, "Fixture", element(tag, {}, text), settings);
            value("Matrix", formatMatrix(IDENTITY));
            value("GDTFSpec", spec);
            value("GDTFMode", entry.default_mode);
            childOf(fixture, "FixtureID").text = String(fixtureId);
            childOf(fixture, "FixtureIDNumeric").text = String(fixtureId);
            const addresses = insertChild(fixture, "Fixture", element("Addresses"), settings);
            addresses.children.push(element("Address", { break: "0" }, absoluteAddress(universe - 1, start)));
            children.children.push(fixture);
            added.push(fixture.key);
            if (address != null) address += count;
        }
        return added[0];
    }

    function applyAction(draft, settings, action) {
        const document = action.doc && action.doc !== "scene" ? draft.gdtf[action.doc] : draft.scene;
        if (!document) throw new Error("That GDTF description is not in the draft.");
        const found = locate(document, action.key);
        if (!found) throw new Error("The node changed. Select it again.");
        const { node, parent } = found;
        switch (action.action) {
            case "move": {
                const row = fixtureRows(draft, settings).find((entry) => entry.key === action.key);
                if (!row) throw new Error("The fixture changed. Select it again.");
                if (row.address !== action.original_address || row.universe !== action.original_universe) {
                    throw new Error("The fixture moved while dragging. Try again.");
                }
                locate(draft.scene, row.addressKey).node.text = absoluteAddress(action.universe, action.address);
                requireValid(draft, settings);
                return;
            }
            case "set-attribute": {
                if (document !== draft.scene && !settings.schema.gdtf_editable[node.tag]?.[action.name]) {
                    throw new Error(`GDTF ${node.tag}@${action.name} is read-only.`);
                }
                if (action.value === null) delete node.attributes[action.name];
                else node.attributes[action.name] = action.value;
                return;
            }
            case "set-text":
                node.text = action.value;
                return;
            case "set-child-text": {
                const path = pathTo(draft.scene, node.key);
                const key = schemaKeys(path, settings).at(-1);
                const child = childOf(node, action.tag);
                if (action.value === null) {
                    if (child) node.children.splice(node.children.indexOf(child), 1);
                } else (child || insertChild(node, key, element(action.tag), settings)).text = action.value;
                return;
            }
            case "set-matrix": {
                const values = [...action.position, ...action.rotation].map(Number);
                if (!values.every(Number.isFinite)) throw new Error("Position and rotation must be numbers.");
                const text = formatMatrix(composeMatrix(values.slice(0, 3), values.slice(3)));
                return applyAction(draft, settings, { action: "set-child-text", key: action.key, tag: "Matrix", value: text });
            }
            case "set-address": {
                const universe = Number(action.universe),
                    address = Number(action.address);
                if (!Number.isInteger(universe) || universe < 1 || universe > 32768) {
                    throw new Error("Universe must be a whole number from 1 to 32768.");
                }
                if (!Number.isInteger(address) || address < 1 || address > DMX_UNIVERSE_SIZE) {
                    throw new Error("DMX address must be a whole number from 1 to 512.");
                }
                node.text = absoluteAddress(universe - 1, address);
                return;
            }
            case "new-uuid":
                node.attributes.uuid = newUuid();
                return;
            case "add-child": {
                const keys = schemaKeys(pathTo(draft.scene, node.key), settings);
                const definition = childDefinition(settings, keys.at(-1), action.tag);
                if (!definition || definition.kind) throw new Error(`${action.tag} cannot be added here.`);
                const key =
                    action.tag === "Data" && action.provider === settings.schema.autolux_provider
                        ? "AutoluxData"
                        : definition.key;
                const created = template(settings, key, action.tag);
                if (key === "AutoluxData") created.attributes.provider = settings.schema.autolux_provider;
                insertChild(node, keys.at(-1), created, settings);
                return created.key;
            }
            case "remove-node":
                if (!parent) throw new Error("The scene root cannot be removed.");
                parent.children.splice(parent.children.indexOf(node), 1);
                return;
            default:
                throw new Error("Unknown edit.");
        }
    }

    function remapSelection(previous, next, key) {
        if (!key) return null;
        const path = pathTo(previous.scene, key);
        if (!path) return null;
        const uuid = path.at(-1).attributes.uuid;
        if (uuid) {
            for (const [node] of walk(next.scene)) if (node.attributes.uuid === uuid) return node.key;
            return null;
        }
        let cursor = next.scene;
        for (let index = 1; index < path.length; index++) {
            cursor = cursor.children[path[index - 1].children.indexOf(path[index])];
            if (!cursor || cursor.tag !== path[index].tag) return null;
        }
        return cursor.key;
    }

    function component(type, props) {
        return { namespace: "dash_html_components", type, props };
    }

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
            [...new Set(occupied.flat())].forEach((index) => {
                const row = rows[index];
                const start = row.address - 1,
                    end = start + row.channels;
                const title = `${row.name} · ${start + 1}–${end}`;
                const conflict = occupied.slice(start, end).some((owners) => owners.length > 1);
                let segment = start,
                    part = 0;
                while (segment < end) {
                    const stop = Math.min(end, (Math.floor(segment / settings.columns) + 1) * settings.columns);
                    blocks.push(
                        component("Button", {
                            key: `${row.key}:${part++}`,
                            children: title,
                            title,
                            "data-fixture-key": row.key,
                            "data-fixture-name": row.name,
                            "data-start-address": row.address,
                            "data-channel-count": row.channels,
                            "data-universe": universe,
                            className:
                                "patch-block" + (selection === row.key ? " selected" : "") + (conflict ? " conflict" : ""),
                            style: {
                                gridRow: Math.floor(segment / settings.columns) + 1,
                                gridColumn: `${(segment % settings.columns) + 1} / span ${stop - segment}`,
                                background: settings.colors[index % settings.colors.length]
                            }
                        })
                    );
                    segment = stop;
                }
            });
            const universes = [
                ...new Set([0, universe, ...rows.map((row) => row.universe).filter((value) => value !== null)])
            ].sort((a, b) => a - b);
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
                drawPlan(document.getElementById("mvr-plan"), draft, settings, selection);
                drawTree(document.getElementById("mvr-tree"), draft, settings, selection);
                drawEditor(document.getElementById("mvr-node-editor"), draft, settings, path);
            }
            const objects = placedObjects(draft, settings).length;
            const summary = `${rows.length} patched fixtures · ${objects} scene objects · ${Object.keys(draft.gdtf).length} GDTF files`;
            if (!path) return [{ display: "none" }, "patch-workspace", "", true, summary];
            const node = path.at(-1);
            const row = rows.find((entry) => entry.key === node.key);
            const detail = row
                ? `${row.channels ?? "?"} channels · ${row.address ?? "?"}–${(row.address ?? 0) + (row.channels ?? 1) - 1} · ${row.spec}`
                : `${node.tag}${node.attributes.name ? ` · ${node.attributes.name}` : ""}`;
            return [{ display: "block" }, "patch-workspace has-selection", detail, path.length === 1, summary];
        },

        editPatch(_add, _remove, _close, action, response, settings, busy, draft, revision, selection, profile, quantity, universe, address) {
            const unchanged = [ds.no_update, ds.no_update, ds.no_update, ds.no_update];
            // Dash can batch an edit queued during a save with the save response.
            // Persistence owns that interval: accept its result before any queued edit.
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
                    if (action.action === "select") {
                        return locate(draft.scene, action.key) ? [ds.no_update, ds.no_update, ds.no_update, action.key] : unchanged;
                    }
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
                return [
                    edited,
                    ds.no_update,
                    "Unsaved changes · save the patch when finished." +
                        (problems ? ` ${problems} patch problem(s) listed under the map.` : ""),
                    selection
                ];
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
        for (const child of children.flat()) if (child) node.append(child);
        return node;
    }

    const SVG = "http://www.w3.org/2000/svg";
    function svg(tag, attributes = {}) {
        const node = document.createElementNS(SVG, tag);
        for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, String(value));
        return node;
    }

    function drawPlan(container, draft, settings, selection) {
        if (!container) return;
        const objects = placedObjects(draft, settings);
        const classes = classNames(draft);
        const xs = objects.map(({ matrix }) => matrix[9]),
            ys = objects.map(({ matrix }) => -matrix[10]);
        const minX = Math.min(0, ...xs),
            maxX = Math.max(0, ...xs),
            minY = Math.min(0, ...ys),
            maxY = Math.max(0, ...ys);
        const span = Math.max(PLAN_MIN_SPAN_MM, maxX - minX, maxY - minY);
        const pad = span * 0.12;
        const radius = span * 0.018;
        const plan = svg("svg", {
            viewBox: `${minX - pad} ${minY - pad} ${maxX - minX + 2 * pad} ${maxY - minY + 2 * pad}`,
            preserveAspectRatio: "xMidYMid meet"
        });
        plan.append(svg("circle", { cx: 0, cy: 0, r: radius * 0.3, fill: "#3b4d62" }));
        const palette = [...new Set(Object.values(classes))];
        for (const { node, matrix } of objects) {
            const [x, y] = [matrix[9], -matrix[10]];
            const group = svg("g", { class: "mvr-object", "data-node-key": node.key });
            const selected = node.key === selection;
            const stroke = selected ? "#ffffff" : "#0b121c";
            if (node.tag === "Fixture") {
                const className = classes[textOf(node, "Classing").toUpperCase()] || "";
                const colour = className
                    ? settings.colors[palette.indexOf(className) % settings.colors.length]
                    : "#7f8ea3";
                const length = Math.hypot(matrix[3], matrix[4]) || 1;
                group.append(
                    svg("line", {
                        x1: x,
                        y1: y,
                        x2: x + (matrix[3] / length) * radius * 2.4,
                        y2: y - (matrix[4] / length) * radius * 2.4,
                        stroke: colour,
                        "stroke-width": radius * 0.35
                    }),
                    svg("circle", { cx: x, cy: y, r: radius, fill: colour, stroke, "stroke-width": radius * 0.25 })
                );
            } else {
                group.append(
                    svg("rect", {
                        x: x - radius * 0.8,
                        y: y - radius * 0.8,
                        width: radius * 1.6,
                        height: radius * 1.6,
                        fill: node.tag === "GroupObject" ? "none" : "#455468",
                        stroke: selected ? "#ffffff" : "#6d7f95",
                        "stroke-width": radius * 0.2
                    })
                );
            }
            const label = svg("text", { x: x + radius * 1.4, y: y + radius * 0.4, "font-size": radius * 1.3 });
            label.textContent = node.attributes.name || node.tag;
            group.append(label);
            plan.append(group);
        }
        container.replaceChildren(plan);
    }

    function nodeLabel(node) {
        const name = node.attributes.name || node.attributes.provider || node.attributes.fileName || "";
        return [h("span", { className: "mvr-tag", text: node.tag }), name ? ` ${name}` : ""];
    }

    function drawTree(container, draft, settings, selection) {
        if (!container) return;
        const rows = [];
        const visit = (node, key, depth) => {
            rows.push(
                h(
                    "div",
                    {
                        className: "mvr-tree-row" + (node.key === selection ? " selected" : ""),
                        "data-node-key": node.key,
                        style: `padding-left:${depth * 12 + 4}px`
                    },
                    nodeLabel(node)
                )
            );
            if (settings.schema.nodes[key]?.any_content) return;
            for (const child of node.children) {
                const definition = key && childDefinition(settings, key, child.tag);
                if (definition?.kind) continue;
                visit(child, definition ? childKey(settings, key, child, definition) : null, depth + 1);
            }
        };
        visit(draft.scene, "GeneralSceneDescription", 0);
        container.replaceChildren(...rows);
    }

    function editInput(kind, value, payload, { values = [], optional = false, candidates = null } = {}) {
        const data = JSON.stringify(payload);
        if (kind === "enum" || kind === "bool" || candidates) {
            const options = candidates || (kind === "bool" ? ["true", "false"] : values).map((entry) => [entry, entry]);
            const select = h(
                "select",
                { "data-mvr-edit": data, "data-optional": optional },
                optional || !options.some(([option]) => option === value) ? h("option", { value: "", text: "—" }) : null,
                options.map(([option, label]) => h("option", { value: option, text: label }))
            );
            select.value = value ?? "";
            return select;
        }
        return h("input", { type: "text", value: value ?? "", "data-mvr-edit": data, "data-optional": optional, spellcheck: "false" });
    }

    function referenceCandidates(draft, targets) {
        const candidates = [];
        for (const [node] of walk(draft.scene)) {
            if (targets.includes(node.tag) && node.attributes.uuid) {
                candidates.push([node.attributes.uuid, `${node.attributes.name || node.tag} · ${node.attributes.uuid.slice(0, 8)}`]);
            }
        }
        return candidates;
    }

    function button(label, action, title) {
        return h("button", { type: "button", className: "mvr-small", "data-mvr-action": JSON.stringify(action), title, text: label });
    }

    function row(label, input, extra) {
        return h("div", { className: "mvr-row" }, h("label", { text: label, title: label }), input, extra || h("span"));
    }

    function matrixHelper(node, matrixText) {
        const matrix = parseMatrix(matrixText);
        const rotation = matrix && rotationDegrees(matrix);
        if (!matrix || !rotation) return h("div", { className: "patch-muted", text: "Matrix has no plain rotation; edit the raw value." });
        const values = [...matrix.slice(9), ...rotation].map((value) => formatNumber(value));
        const labels = ["x mm", "y mm", "z mm", "yaw °", "pitch °", "roll °"];
        const home = homeOrientation(matrix);
        return h(
            "div",
            {},
            h(
                "div",
                { className: "mvr-helper", "data-mvr-matrix": node.key },
                values.map((value, index) =>
                    h("input", { type: "number", step: "any", value, title: labels[index], placeholder: labels[index] })
                )
            ),
            h("div", {
                className: "patch-muted",
                text:
                    typeof home === "string"
                        ? home
                        : `Starfire home: pan ${formatNumber(home.pan)}°, tilt ${formatNumber(home.tilt)}°` +
                          (home.reversed ? " · standing: pan/tilt reversed" : " · hanging")
            })
        );
    }

    function addressHelper(node) {
        const location = dmxAddress(node.text);
        return h(
            "div",
            { className: "mvr-helper", "data-mvr-address": node.key },
            h("input", { type: "number", min: 1, max: 32768, step: 1, value: location ? location[0] + 1 : "", title: "Universe" }),
            h("input", { type: "number", min: 1, max: DMX_UNIVERSE_SIZE, step: 1, value: location ? location[1] : "", title: "Address" }),
            h("span", { className: "patch-muted", text: "universe · address" })
        );
    }

    function drawGdtf(draft, settings, spec) {
        const tree = gdtfTree(draft, settings, spec);
        if (!tree) return h("p", { className: "patch-muted", text: `${spec} is not embedded or installed.` });
        const editable = settings.schema.gdtf_editable;
        const visit = (node, depth) => {
            const fields = editable[node.tag] || {};
            const attributes = Object.entries(node.attributes).map(([name, value]) =>
                fields[name]
                    ? row(name, editInput("string", value, { action: "set-attribute", doc: spec, key: node.key, name }))
                    : h("div", { className: "mvr-gdtf-attribute", text: `${name} = ${value}` })
            );
            const summary = h("summary", {}, nodeLabel(node));
            const details = h("details", depth < 2 || node.tag === "DMXModes" ? { open: true } : {}, summary, attributes);
            if (node.text) details.append(h("div", { className: "mvr-gdtf-attribute", text: node.text }));
            for (const child of node.children) details.append(visit(child, depth + 1));
            return details;
        };
        return h(
            "div",
            {},
            h("p", { className: "patch-muted", text: "Read-only except DMX mode names and channel function/set ranges; a changed GDTF gains a Revision on save." }),
            visit(tree, 0)
        );
    }

    function drawEditor(container, draft, settings, path) {
        if (!container) return;
        if (!path) return container.replaceChildren();
        const keys = schemaKeys(path, settings);
        const node = path.at(-1);
        const key = keys.at(-1);
        const schema = settings.schema.nodes[key];
        const parts = [
            h(
                "div",
                { className: "patch-muted" },
                path.slice(0, -1).map((ancestor) => h("span", { className: "mvr-link", "data-node-key": ancestor.key }, ancestor.tag, " › ")),
                h("strong", { text: node.tag })
            )
        ];
        if (!schema) {
            parts.push(h("p", { className: "patch-muted", text: `${node.tag} is not an MVR 1.6 node here; saving will be refused until it is removed.` }));
            return container.replaceChildren(...parts);
        }
        parts.push(h("h4", { text: "Attributes" }));
        const declared = new Set(schema.attributes.map((attribute) => attribute.name));
        for (const attribute of schema.attributes) {
            const value = node.attributes[attribute.name];
            const payload = { action: "set-attribute", doc: "scene", key: node.key, name: attribute.name };
            const candidates = attribute.target.length ? referenceCandidates(draft, attribute.target) : null;
            parts.push(
                row(
                    `${attribute.name}${attribute.required ? " *" : ""}`,
                    editInput(attribute.kind, value, payload, { values: attribute.values, optional: !attribute.required, candidates }),
                    attribute.name === "uuid" ? button("new", { action: "new-uuid", key: node.key }, "Generate a new UUID") : null
                )
            );
        }
        for (const name of Object.keys(node.attributes).filter((name) => !declared.has(name))) {
            parts.push(
                row(`${name} (not MVR)`, h("span", { className: "mvr-readonly", text: node.attributes[name] }),
                    button("×", { action: "set-attribute", doc: "scene", key: node.key, name, value: null }, "Remove"))
            );
        }
        if (schema.any_content) {
            parts.push(h("p", { className: "patch-muted", text: "Provider data is preserved verbatim and not edited here." }));
            return container.replaceChildren(...parts);
        }
        if (schema.text) {
            parts.push(h("h4", { text: "Value" }));
            parts.push(row(node.tag, editInput(schema.text, node.text, { action: "set-text", doc: "scene", key: node.key }, { values: schema.values })));
            if (schema.text === "dmx_address") parts.push(addressHelper(node));
        }
        const values = schema.children.filter((child) => child.kind);
        if (values.length) parts.push(h("h4", { text: "Fields" }));
        for (const child of values) {
            const present = childOf(node, child.tag);
            const payload = { action: "set-child-text", key: node.key, tag: child.tag };
            const candidates = child.target.length ? referenceCandidates(draft, child.target) : null;
            if (!present) {
                parts.push(row(`${child.tag}${child.count === "1" ? " *" : ""}`, h("span", { className: "mvr-readonly", text: "not set" }),
                    button("+", { ...payload, value: defaultValue(child.kind, child.values) }, `Add ${child.tag}`)));
                continue;
            }
            parts.push(
                row(child.tag + (child.count === "1" ? " *" : ""),
                    editInput(child.kind, present.text, payload, { values: child.values, candidates }),
                    child.count === "1" ? null : button("×", { ...payload, value: null }, `Remove ${child.tag}`))
            );
            if (child.kind === "matrix") parts.push(matrixHelper(node, present.text));
            if (child.tag === "GDTFMode" && textOf(node, "GDTFSpec")) {
                const tree = gdtfTree(draft, settings, textOf(node, "GDTFSpec"));
                const modes = tree ? [...walk(tree)].map(([entry]) => entry).filter((entry) => entry.tag === "DMXMode").map((entry) => entry.attributes.Name) : [];
                parts.push(h("div", { className: "patch-muted", text: `Modes in this GDTF: ${modes.join(", ") || "none"}` }));
            }
            if (child.tag === "GDTFSpec") {
                const specs = [...new Set([...Object.keys(draft.gdtf), ...Object.keys(settings.library)])];
                parts.push(h("div", { className: "patch-muted", text: `Embedded or installed: ${specs.join(", ")}` }));
            }
        }
        const structured = schema.children.filter((child) => !child.kind);
        if (structured.length) {
            parts.push(h("h4", { text: "Children" }));
            for (const child of node.children) {
                if (structured.some((entry) => entry.tag === child.tag)) {
                    parts.push(h("div", { className: "mvr-link", "data-node-key": child.key }, nodeLabel(child)));
                }
            }
            const adders = structured
                .filter((child) => child.count === "0..n" || !childOf(node, child.tag))
                .map((child) => button(`+ ${child.tag}`, { action: "add-child", key: node.key, tag: child.tag }, `Add ${child.tag}`));
            if (key === "UserData") {
                adders.push(button("+ Autolux data", { action: "add-child", key: node.key, tag: "Data", provider: settings.schema.autolux_provider }, "Movement limits"));
            }
            parts.push(h("div", { className: "mvr-adders" }, adders));
        }
        const spec = textOf(node, "GDTFSpec");
        if (spec) {
            parts.push(h("h4", { text: `GDTF · ${spec}` }));
            parts.push(drawGdtf(draft, settings, spec));
        }
        container.replaceChildren(...parts);
    }

    function geometry(grid) {
        const rect = grid.getBoundingClientRect();
        const cell = grid.querySelector(".patch-channel").getBoundingClientRect();
        const style = getComputedStyle(grid);
        return {
            rect,
            columns: Number(grid.dataset.columns),
            count: Number(grid.dataset.channelCount),
            width: cell.width + parseFloat(style.columnGap),
            height: cell.height + parseFloat(style.rowGap)
        };
    }

    function channelAt({ rect, columns, count, width, height }, x, y) {
        if (x < rect.left || x >= rect.right || y < rect.top || y >= rect.bottom) return null;
        const column = Math.floor((x - rect.left) / width);
        const row = Math.floor((y - rect.top) / height);
        const channel = row * columns + column + 1;
        return channel <= count ? channel : null;
    }

    function clearPreview(grid) {
        grid.querySelectorAll(".drop-valid, .drop-invalid").forEach((cell) => {
            cell.classList.remove("drop-valid", "drop-invalid");
        });
    }

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
        const valid =
            start >= 1 && end <= geometry.count && !ranges.some(([otherStart, otherEnd]) => start <= otherEnd && end >= otherStart);
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
            // Ignore only the click generated by this pointer release, never a later keyboard click.
            setTimeout(() => {
                if (suppressedClick === current.block) suppressedClick = null;
            });
        }
        if (current.grid.hasPointerCapture(current.pointerId)) current.grid.releasePointerCapture(current.pointerId);
    }

    document.addEventListener("pointerdown", (event) => {
        const block = event.target.closest("#fixture-channel-map .patch-block");
        if (!block || event.button !== 0 || !event.isPrimary) return;
        if (block.closest('[aria-busy="true"]')) return;
        finish();
        suppressedClick = null;
        const grid = block.closest(".patch-map");
        const address = Number(block.dataset.startAddress);
        const layout = geometry(grid);
        const channel = channelAt(layout, event.clientX, event.clientY);
        if (channel === null) return;
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
            ranges: [...grid.querySelectorAll(".patch-block")]
                .filter((other) => other.dataset.fixtureKey !== block.dataset.fixtureKey)
                .map((other) => [
                    Number(other.dataset.startAddress),
                    Number(other.dataset.startAddress) + Number(other.dataset.channelCount) - 1
                ])
        };
    });

    document.addEventListener("pointermove", (event) => {
        if (!drag || drag.pointerId !== event.pointerId) return;
        if (!drag.moved) {
            if (Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < DRAG_THRESHOLD_PX) return;
            drag.moved = true;
            drag.grid.setPointerCapture(event.pointerId);
            drag.label = document.createElement("div");
            drag.label.className = "patch-drag-label";
            document.body.appendChild(drag.label);
            drag.grid.querySelectorAll(".patch-block").forEach((block) => {
                if (block.dataset.fixtureKey === drag.fixtureKey) block.classList.add("dragging");
            });
        }
        event.preventDefault();
        drag.event = event;
        if (!drag.frame)
            drag.frame = requestAnimationFrame(() => {
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
            if (event.target.closest('#config-panel[aria-busy="true"]')) {
                event.preventDefault();
                event.stopImmediatePropagation();
                return;
            }
            const action = event.target.closest("#config-panel [data-mvr-action]");
            if (action) {
                sendAction(JSON.parse(action.dataset.mvrAction));
                return;
            }
            const block = event.target.closest("#fixture-channel-map .patch-block");
            if (block) {
                if (suppressedClick === block) {
                    suppressedClick = null;
                    event.preventDefault();
                    event.stopImmediatePropagation();
                    return;
                }
                sendAction({ action: "select", key: block.dataset.fixtureKey });
                return;
            }
            const selectable = event.target.closest("#config-panel [data-node-key]");
            if (selectable) sendAction({ action: "select", key: selectable.dataset.nodeKey });
        },
        true
    );

    document.addEventListener("change", (event) => {
        if (event.target.closest('#config-panel[aria-busy="true"]')) return;
        const edit = event.target.closest("#config-panel [data-mvr-edit]");
        if (edit) {
            const value = edit.value === "" && edit.dataset.optional !== undefined && edit.dataset.optional !== "false" ? null : edit.value;
            sendAction({ ...JSON.parse(edit.dataset.mvrEdit), value });
            return;
        }
        const matrix = event.target.closest("#config-panel [data-mvr-matrix]");
        if (matrix) {
            const values = [...matrix.querySelectorAll("input")].map((input) => Number(input.value));
            sendAction({ action: "set-matrix", key: matrix.dataset.mvrMatrix, position: values.slice(0, 3), rotation: values.slice(3) });
            return;
        }
        const address = event.target.closest("#config-panel [data-mvr-address]");
        if (address) {
            const [universe, start] = [...address.querySelectorAll("input")].map((input) => Number(input.value));
            sendAction({ action: "set-address", key: address.dataset.mvrAddress, universe, address: start });
        }
    });

    document.addEventListener("pointercancel", finish);
    document.addEventListener("lostpointercapture", (event) => {
        if (drag?.pointerId === event.pointerId) finish();
    });
    document.addEventListener("keydown", (event) => {
        if (event.target.closest?.('#config-panel[aria-busy="true"]')) event.preventDefault();
        if (event.key === "Escape" && drag) {
            event.preventDefault();
            finish();
        }
    });
    // Geometry is captured once per gesture; abandon it if the viewport changes.
    document.addEventListener("scroll", finish, true);
    window.addEventListener("resize", finish);
    window.addEventListener("blur", finish);
})();
