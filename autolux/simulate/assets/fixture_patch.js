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
    const PLAN_LABEL_LIMIT = 48;
    const IDENTITY = [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0];
    const GEOMETRY_TAGS = new Set(["Geometry", "Axis", "FilterBeam", "FilterColor", "FilterGobo", "FilterShaper", "Beam", "MediaServerLayer", "MediaServerCamera", "MediaServerMaster", "Display", "GeometryReference", "Laser", "WiringObject", "Inventory", "Structure", "Support", "Magnet"]);
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

    // Mirrors mvr.mode_cells: the geometries Starfire addresses as cells, by first offset.
    function modeCells(tree, mode) {
        const node = tree && [...walk(tree)].map(([entry]) => entry).find((entry) => entry.tag === "DMXMode" && entry.attributes.Name === mode);
        if (!node) return [];
        const paths = {};
        const visit = (entry, parent) => {
            if (!GEOMETRY_TAGS.has(entry.tag)) return;
            const path = [...parent, entry.attributes.Name ?? ""];
            paths[path.at(-1)] = path;
            for (const child of entry.children) visit(child, path);
        };
        const geometries = [...walk(tree)].map(([entry]) => entry).find((entry) => entry.tag === "Geometries");
        for (const child of geometries?.children || []) visit(child, []);
        const owned = [];
        for (const [channel] of walk(node)) {
            const offsets = channel.attributes.Offset ?? "None";
            if (channel.tag !== "DMXChannel" || (channel.attributes.DMXBreak ?? "1") !== "1" || offsets === "None") continue;
            const geometry = channel.attributes.Geometry ?? "";
            owned.push([paths[geometry] || [geometry], offsets.split(",").map(Number)]);
        }
        const within = (path, outer) => outer.length <= path.length && outer.every((name, index) => path[index] === name);
        const leaves = owned.map(([path]) => path).filter((path) => !owned.some(([other]) => other.length > path.length && within(other, path)));
        const cells = new Map();
        for (const [path, offsets] of owned) {
            const below = new Set(leaves.filter((leaf) => within(leaf, path)).map((leaf) => leaf.at(-1)));
            const name = below.size === 1 ? [...below][0] : path.at(-1);
            cells.set(name, [...(cells.get(name) || []), ...offsets]);
        }
        return [...cells]
            .map(([geometry, offsets]) => ({ geometry, first: Math.min(...offsets) }))
            .sort((a, b) => a.first - b.first || (a.geometry < b.geometry ? -1 : 1))
            .map((cell) => cell.geometry);
    }

    function placedObjects(draft, settings) {
        const objects = [];
        const visit = (node, matrix, layer) => {
            for (const child of childOf(node, "ChildList")?.children || []) {
                if (!settings.schema.object_tags.includes(child.tag)) continue;
                const placed = multiply(matrix, parseMatrix(textOf(child, "Matrix")) || [...IDENTITY]);
                objects.push({ node: child, matrix: placed, layer });
                visit(child, placed, layer);
            }
        };
        for (const layer of childOf(childOf(draft.scene, "Scene"), "Layers")?.children || []) {
            if (layer.tag === "Layer") visit(layer, parseMatrix(textOf(layer, "Matrix")) || [...IDENTITY], layer.attributes.name || "");
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
            const tree = gdtfTree(draft, settings, spec);
            const channels = modeChannels(tree, mode);
            const cells = modeCells(tree, mode);
            const className = classes[textOf(node, "Classing").toUpperCase()] || "";
            return [{ key: node.key, name: node.attributes.name || "", spec, mode, breaks, addressKey: addresses[0].key, universe, address, channels, cells, className, matrix }];
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
                const title = `${row.name} · ${start + 1}–${end}${row.cells.length > 1 ? ` · ${row.cells.length} cells` : ""}`;
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
                ? `${row.channels ?? "?"} channels · ${row.cells.length > 1 ? `${row.cells.length} cells: ${row.cells.join(", ")} · ` : ""}${row.address ?? "?"}–${(row.address ?? 0) + (row.channels ?? 1) - 1} · ${row.spec}`
                : `${node.tag}${node.attributes.name ? ` · ${node.attributes.name}` : ""}`;
            return [...layout, detail, path.length === 1, summary];
        },

        editPatch(_add, _remove, _close, action, response, settings, busy, draft, revision, selection, profile, quantity, universe, address) {
            const unchanged = [ds.no_update, ds.no_update, ds.no_update, ds.no_update];
            // Dash can batch an edit queued during a save with its response; the response wins.
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
        planMarks,
        planSvg,
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


    function keepingState(container, sameNode, draw) {
        if (!sameNode) return draw();
        const open = [...container.querySelectorAll("details")].map((details) => details.open);
        const focused = [...container.querySelectorAll("input, select")].indexOf(document.activeElement);
        draw();
        container.querySelectorAll("details").forEach((details, index) => index < open.length && (details.open = open[index]));
        if (focused >= 0) container.querySelectorAll("input, select")[focused]?.focus({ preventScroll: true });
    }

    // Plan view in standard plot orientation: MVR/GDTF -Y (downstage, toward the audience) at
    // the bottom, so the SVG y is -Y.
    function planMarks(draft, settings) {
        const classes = classNames(draft);
        const fixtures = [];
        const others = [];
        for (const { node, matrix, layer } of placedObjects(draft, settings)) {
            const name = node.attributes.name || node.tag;
            if (node.tag !== "Fixture") {
                others.push({ key: node.key, name, x: matrix[9], y: matrix[10], hollow: node.tag === "GroupObject" });
                continue;
            }
            const length = Math.hypot(matrix[3], matrix[4]);
            fixtures.push({
                key: node.key,
                name,
                group: classes[textOf(node, "Classing").toUpperCase()] || "",
                accent: layer === settings.accentRole,
                x: matrix[9],
                y: matrix[10],
                // The local Y axis in plan, i.e. the yaw; a fixture aimed straight up or down has none.
                facing: length > 1e-6 ? [matrix[3] / length, matrix[4] / length] : null
            });
        }
        return { fixtures, others };
    }

    // Past the configured colours, golden-angle hues keep dozens of groups apart.
    function groupColour(index, colors) {
        return index < colors.length ? colors[index] : `hsl(${Math.round((index * 137.508) % 360)},55%,58%)`;
    }

    const escapeXml = (text) => String(text).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

    // The largest 1, 2 or 5 × 10^k metres within a fifth of the plan.
    function scaleMetres(spanMm) {
        const target = spanMm / 5000;
        const power = 10 ** Math.floor(Math.log10(target));
        return [5, 2, 1].map((unit) => unit * power).find((value) => value <= target);
    }

    // Draws the nine-area stage grid (equal thirds of the fixtures' bounding box) and its area
    // names; it only draws lines and labels and never assigns areas, which come from
    // stage_areas in cli/starfire_assets.py. An axis without extent has no cuts and only its
    // centre names.
    const AREA_ROWS = ["up", "centre", "down"];
    const AREA_COLUMNS = ["left", "centre", "right"];
    const areaName = (row, column) => (row === "centre" && column === "centre" ? "centre" : `${row} ${column}`);
    function areaGrid(fixtures, n, r, font) {
        if (!fixtures.length) return "";
        const xs = fixtures.map((mark) => mark.x);
        const ys = fixtures.map((mark) => -mark.y);
        const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
        const cuts = (low, high) => (high > low ? [1, 2].map((k) => low + ((high - low) * k) / 3) : []);
        // Each third's centre and index.
        const thirds = (low, high) => (high > low ? [0, 1, 2].map((k) => [low + ((high - low) * (2 * k + 1)) / 6, k]) : [[low, 1]]);
        const line = (ax, ay, bx, by) => `<line x1="${n(ax)}" y1="${n(ay)}" x2="${n(bx)}" y2="${n(by)}"/>`;
        // SVG y grows downstage, so the top row is upstage.
        const names = thirds(y0, y1).flatMap(([y, row]) =>
            thirds(x0, x1).map(([x, column]) => `<text class="plan-area-name" x="${n(x)}" y="${n(y)}">${areaName(AREA_ROWS[row], AREA_COLUMNS[column])}</text>`)
        );
        const box = x1 > x0 && y1 > y0 ? `<rect x="${n(x0)}" y="${n(y0)}" width="${n(x1 - x0)}" height="${n(y1 - y0)}"/>` : "";
        return [
            `<g class="plan-areas" stroke-width="${n(r * 0.2)}" stroke-dasharray="${n(r)} ${n(r * 0.6)}" fill="none">`,
            box,
            ...cuts(x0, x1).map((x) => line(x, y0, x, y1)),
            ...cuts(y0, y1).map((y) => line(x0, y, x1, y)),
            "</g>",
            `<g font-size="${n(font * 0.7)}" text-anchor="middle" dominant-baseline="middle">${names.join("")}</g>`
        ].join("");
    }

    function planSvg({ fixtures, others }, settings) {
        const all = [...fixtures, ...others];
        const xs = all.length ? all.map((mark) => mark.x) : [0];
        const ys = all.length ? all.map((mark) => -mark.y) : [0];
        const [minX, maxX, minY, maxY] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
        const span = Math.max(PLAN_MIN_SPAN_MM, maxX - minX, maxY - minY);
        // Marks shrink as the plan fills; names show while they fit, else on hover or tap.
        const r = span * 0.018 * Math.min(1, Math.sqrt(PLAN_LABEL_LIMIT / Math.max(1, fixtures.length)));
        const labelled = fixtures.length <= PLAN_LABEL_LIMIT;
        const pad = span * 0.12;
        const [left, top, width, height] = [minX - pad, minY - pad, maxX - minX + 2 * pad, maxY - minY + 3 * pad];
        const n = Math.round;
        const font = n(Math.max(r * 1.3, span * 0.014));
        const groups = [...new Set(fixtures.map((mark) => mark.group))].sort();
        const colour = Object.fromEntries(groups.map((group, index) => [group, group ? groupColour(index, settings.colors) : "#7f8ea3"]));
        const accents = new Set(fixtures.filter((mark) => mark.accent).map((mark) => mark.group));
        const edge = n(maxY + r * 2);
        const metres = scaleMetres(span);
        const [barX, barY] = [n(left + width - pad * 0.3 - metres * 1000), n(top + height - pad * 0.4)];
        const parts = [
            `<svg class="plan" xmlns="http://www.w3.org/2000/svg" viewBox="${n(left)} ${n(top)} ${n(width)} ${n(height)}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Rig plan, audience at the bottom" font-size="${font}">`,
            areaGrid(fixtures, n, r, font),
            // MVR has no stage edge; the plan draws it at the most downstage object.
            `<line class="plan-stage-edge" x1="${n(left)}" y1="${edge}" x2="${n(left + width)}" y2="${edge}" stroke-width="${n(r * 0.3)}"/>`,
            `<text x="${n(left + pad * 0.2)}" y="${n(edge + font * 1.4)}">audience</text>`,
            `<g class="plan-scale"><line x1="${barX}" y1="${barY}" x2="${n(barX + metres * 1000)}" y2="${barY}" stroke-width="${n(r * 0.3)}"/><text x="${barX}" y="${n(barY - font * 0.5)}">${metres} m</text></g>`,
            `<g stroke-width="${n(r * 0.25)}">`
        ];
        for (const mark of others) {
            const [x, y, side] = [n(mark.x - r * 0.8), n(-mark.y - r * 0.8), n(r * 1.6)];
            parts.push(`<g class="mvr-object" data-node-key="${escapeXml(mark.key)}"><title>${escapeXml(mark.name)}</title><rect class="plan-object${mark.hollow ? " hollow" : ""}" x="${x}" y="${y}" width="${side}" height="${side}"/></g>`);
        }
        for (const mark of fixtures) {
            const [x, y, fill] = [n(mark.x), n(-mark.y), colour[mark.group]];
            const arrow = mark.facing ? `<line x1="${x}" y1="${y}" x2="${n(x + mark.facing[0] * r * 2.4)}" y2="${n(y - mark.facing[1] * r * 2.4)}" stroke="${fill}"/>` : "";
            const dash = mark.accent ? ` stroke-dasharray="${n(r * 0.5)} ${n(r * 0.35)}"` : "";
            const label = labelled ? `<text x="${n(x + r * 1.4)}" y="${n(y + r * 0.4)}">${escapeXml(mark.name)}</text>` : "";
            const title = mark.group ? `${mark.name} · ${mark.group}` : mark.name;
            parts.push(`<g class="mvr-object" data-node-key="${escapeXml(mark.key)}"><title>${escapeXml(title)}</title>${arrow}<circle cx="${x}" cy="${y}" r="${n(r)}" fill="${fill}"${dash}/>${label}</g>`);
        }
        parts.push("</g></svg>");
        const legend = groups.map((group) => {
            const dash = accents.has(group) ? ' stroke-dasharray="2 1.5"' : "";
            return `<span class="plan-key"><svg viewBox="0 0 12 12" width="12" height="12"><circle cx="6" cy="6" r="4.5" fill="${colour[group]}"${dash}/></svg>${escapeXml(group || "no group")}${accents.has(group) ? " · accent" : ""}</span>`;
        });
        return parts.join("") + `<div class="plan-legend">${legend.join("")}</div>`;
    }

    function drawPlan(container, draft, settings) {
        container.innerHTML = planSvg(planMarks(draft, settings), settings);
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
        GDTFSpec: (field, value, edit, { draft, settings, optional }) => {
            const specs = [...new Set([...Object.keys(draft.gdtf), ...Object.keys(settings.library)])];
            return [choice(specs.map((spec) => [spec, spec]), value, edit, optional)];
        },
        GDTFMode: (field, value, edit, { draft, settings, node, optional }) => {
            const tree = gdtfTree(draft, settings, textOf(node, "GDTFSpec"));
            const modes = tree ? [...walk(tree)].filter(([entry]) => entry.tag === "DMXMode").map(([entry]) => entry.attributes.Name) : [];
            return [choice(modes.map((mode) => [mode, mode]), value, edit, optional)];
        },
        default: (field, value, edit, context) => [textInput(value, edit, context.optional)]
    };

    function fieldRows(label, field, value, edit, context, extra) {
        const render = FIELD_TYPES[field.name] || FIELD_TYPES[field.kind] || FIELD_TYPES.default;
        const [input, own, helpers = []] = render(field, value, edit, context);
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
        const context = { draft, settings, node };
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
            // Swallow only the click this pointer release generates, never a later keyboard click.
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
