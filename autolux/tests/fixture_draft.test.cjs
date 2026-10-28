const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const data = JSON.parse(fs.readFileSync(process.env.FIXTURE_DRAFT_DATA, "utf8"));
const ds = { no_update: Symbol("unchanged"), callback_context: {} };
// The page's side effects, recorded: draft actions, bridge commands, and a hand-run interval clock.
const actions = [];
const commands = [];
const replies = [];
const timers = new Map();
let timerId = 0;
const labels = [];
ds.set_props = (id, props) => (id === "fixture-grid-action" ? actions.push(props.data) : labels.push([id, props.children]));
const autoluxControl = { dispatch: (command) => (commands.push(command), Promise.resolve(replies.shift() || {})) };
// No DOM: a test that builds the pane lends this page a createElement.
const page = { addEventListener() {} };
vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../simulate/assets/fixture_patch.js"), "utf8"), {
    window: { dash_clientside: ds, addEventListener() {}, autoluxControl },
    document: page,
    structuredClone,
    crypto: globalThis.crypto,
    TextEncoder,
    setInterval: (run, ms) => (timers.set(++timerId, { run, ms }), timerId),
    clearInterval: (id) => timers.delete(id),
    setTimeout: (run, ms) => (timers.set(++timerId, { run, ms }), timerId),
    clearTimeout: (id) => timers.delete(id)
});
const model = ds.fixture_patch_model;
const plain = (value) => JSON.parse(JSON.stringify(value));
const { settings } = data;
const UUID = /^[0-9A-F]{8}-[0-9A-F]{4}-4[0-9A-F]{3}-[89AB][0-9A-F]{3}-[0-9A-F]{12}$/;

const draft = () => structuredClone(data.draft);
// The running show's rig as the control state reports it: the room as committed.
const RUNNING = {
    fixtures: data.patched.map(([fixture_uuid, name, , , cells]) => ({ fixture_uuid, name, cells: cells.length > 1 ? cells.map((geometry) => `${fixture_uuid}/${geometry}`) : [fixture_uuid] }))
};
// Each room fixture's MVR uuid by its committed name.
const FIXTURE_UUID = Object.fromEntries(data.patched.map(([fixture_uuid, name]) => [name, fixture_uuid]));
// A mover's test carries its placement as the server exports it.
const POSES = plain(data.poses);
const posed = (name) => ({ pose: POSES[FIXTURE_UUID[name]] });
const nodes = function* (tree) {
    yield tree;
    for (const child of tree.children) yield* nodes(child);
};
const fixtureNode = (tree, name) => [...nodes(tree.scene)].find((node) => node.tag === "Fixture" && node.attributes.name === name);
const child = (node, tag) => node.children.find((entry) => entry.tag === tag);

function edit(current, action, { trigger = "fixture-grid-action", busy = "false", response = null, selection = null, profile = null, quantity = 1, universe = 1, address = null } = {}) {
    ds.callback_context.triggered_id = trigger;
    return ds.fixture_patch.editPatch(0, 0, 0, action, response, settings, busy, current, "saved-revision", selection, profile, quantity, universe, address);
}

test("patched fixtures match the server's reading of the room rig", () => {
    const rows = model.fixtureRows(draft(), settings);
    assert.deepEqual(plain(rows.map((row) => [row.fixture_uuid, row.name, row.universe, row.address, row.cells])), data.patched);
    assert.deepEqual(plain(model.occupancy(rows, settings, 0).errors), []);
    assert.equal(rows.find((row) => row.name === "spot_left").channels, 12);
    // The uuid is carried as the MVR writes it, as the server's export does.
    const lower = draft();
    const spot = fixtureNode(lower, "spot_left");
    spot.attributes.uuid = spot.attributes.uuid.toLowerCase();
    assert.equal(model.fixtureRows(lower, settings).find((row) => row.key === spot.key).fixture_uuid, spot.attributes.uuid);
});

test("matrix composition and decomposition agree with the server byte for byte", () => {
    for (const { rotation, matrix, decomposed } of data.parity) {
        assert.equal(model.formatMatrix(model.composeMatrix([10, -20, 30], rotation)), matrix);
        const browser = model.rotationDegrees(model.composeMatrix([0, 0, 0], rotation));
        browser.forEach((value, index) => assert.ok(Math.abs(value - decomposed[index]) < 1e-6, `${rotation}`));
    }
    assert.deepEqual(plain(model.rotationDegrees([-1, 0, 0, 0, -1, 0, 0, 0, 1, 0, 0, 0])), [180, 0, 0]);
    assert.equal(model.rotationDegrees([1, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0]), null);
    assert.equal(model.parseMatrix("{1,0,0}{0,1,0}{0,0,1}"), null);
});

test("the inspector's home matches Starfire for hanging, standing and steep fixtures", () => {
    assert.deepEqual(plain(model.homeOrientation([-1, 0, 0, 0, -1, 0, 0, 0, 1, 0, 0, 0])), { pan: 180, tilt: 0, reversed: false });
    assert.deepEqual(plain(model.homeOrientation([1, 0, 0, 0, -1, 0, 0, 0, -1, 0, 0, 0])), { pan: 180, tilt: 0, reversed: true });
    const steep = model.composeMatrix([0, 0, 0], [0, 61, 0]);
    assert.equal(typeof model.homeOrientation(steep), "string");
    const current = draft();
    const spot = fixtureNode(current, "spot_left");
    const [tilted] = edit(current, { action: "set-matrix", key: spot.key, position: [0, 0, 0], rotation: [0, 61, 0] });
    const errors = model.occupancy(model.fixtureRows(tilted, settings), settings, 0).errors;
    assert.ok(errors.some((error) => error.includes("exceeds ±60°")), errors.join());
});

test("dragging a block rewrites the break-0 address and refuses overlaps or stale drags", () => {
    const current = draft();
    const wash = fixtureNode(current, "wash_right");
    const move = (address, original = 89, universe = 0) =>
        edit(current, { action: "move", key: wash.key, original_address: original, original_universe: 0, universe, address });
    const addressText = (tree) => child(child(fixtureNode(tree, "wash_right"), "Addresses"), "Address").text;
    assert.equal(addressText(move(200)[0]), "200");
    assert.equal(addressText(move(200, 89, 1)[0]), "712");
    assert.equal(move(1)[0], ds.no_update);
    assert.equal(move(300, 90)[0], ds.no_update);
    assert.equal(addressText(current), "89");
});

test("library adds create spec-complete fixtures and embed their GDTF", () => {
    const spec = Object.keys(settings.library).find((name) => name.startsWith("Generic@Dimmer@"));
    const before = draft();
    assert.equal(before.gdtf[spec], undefined);
    const [added, , , selected] = edit(before, null, { trigger: "fixture-add", profile: spec, quantity: 2 });
    const created = [...nodes(added.scene)].filter((node) => node.tag === "Fixture" && child(node, "GDTFSpec").text === spec);
    assert.equal(created.length, 2);
    assert.notEqual(created[0].attributes.uuid, created[1].attributes.uuid);
    assert.equal(selected, created[0].key);
    for (const node of created) {
        assert.match(node.attributes.uuid, UUID);
        assert.ok(!Object.values(FIXTURE_UUID).includes(node.attributes.uuid), "a new fixture never reuses a uuid");
        for (const tag of ["Matrix", "GDTFMode", "FixtureID", "FixtureIDNumeric", "UnitNumber", "Addresses"]) {
            assert.ok(child(node, tag), tag);
        }
    }
    assert.deepEqual(plain(created.map((node) => child(node, "FixtureIDNumeric").text)), ["9", "10"]);
    assert.ok(added.gdtf[spec]);
    const rows = model.fixtureRows(added, settings).filter((row) => row.spec === spec);
    assert.deepEqual(plain(rows.map((row) => row.address)), [25, 26]);
    assert.deepEqual(plain(model.occupancy(model.fixtureRows(added, settings), settings, 0).errors), []);
});

test("rotation helpers rewrite the Matrix and keep position", () => {
    const current = draft();
    const spot = fixtureNode(current, "spot_left");
    const [turned] = edit(current, { action: "set-matrix", key: spot.key, position: [100, 200, 2500], rotation: [90, 0, 0] });
    const matrix = model.parseMatrix(child(fixtureNode(turned, "spot_left"), "Matrix").text);
    assert.deepEqual(plain(matrix.slice(9)), [100, 200, 2500]);
    assert.ok(Math.abs(model.rotationDegrees(matrix)[0] - 90) < 1e-9);
    assert.equal(edit(current, { action: "set-matrix", key: spot.key, position: [0, 0, 0], rotation: [NaN, 0, 0] })[0], ds.no_update);
});

test("GDTF edits are limited to the correctable fields", () => {
    const current = draft();
    const spec = Object.keys(current.gdtf)[0];
    const fn = [...nodes(current.gdtf[spec])].find((node) => node.tag === "ChannelFunction");
    const [edited] = edit(current, { action: "set-attribute", doc: spec, key: fn.key, name: "PhysicalTo", value: "42" });
    assert.equal([...nodes(edited.gdtf[spec])].find((node) => node.key === fn.key).attributes.PhysicalTo, "42");
    assert.equal(edit(current, { action: "set-attribute", doc: spec, key: fn.key, name: "Attribute", value: "Pan" })[0], ds.no_update);
});

test("schema-driven structure edits add, order, clear and remove nodes", () => {
    const current = draft();
    const spot = fixtureNode(current, "spot_left");
    const [withAlignments, , , selected] = edit(current, { action: "add-child", key: spot.key, tag: "Alignments" });
    const tags = fixtureNode(withAlignments, "spot_left").children.map((node) => node.tag);
    assert.ok(tags.indexOf("Alignments") > tags.indexOf("Addresses"));
    assert.equal([...nodes(withAlignments.scene)].find((node) => node.key === selected).tag, "Alignments");
    const [cleared] = edit(current, { action: "set-child-text", key: spot.key, tag: "Classing", value: null });
    assert.equal(child(fixtureNode(cleared, "spot_left"), "Classing"), undefined);
    const [removed] = edit(current, { action: "remove-node", key: spot.key });
    assert.equal(fixtureNode(removed, "spot_left"), undefined);
    assert.equal(edit(current, { action: "remove-node", key: current.scene.key })[0], ds.no_update);
    const [withData] = edit(current, { action: "add-child", key: current.scene.key, tag: "UserData" });
    const userData = child(withData.scene, "UserData");
    assert.equal(withData.scene.children.indexOf(userData), 0);
    const [autolux] = edit(withData, { action: "add-child", key: userData.key, tag: "Data", provider: "Autolux" });
    const data = child(child(autolux.scene, "UserData"), "Data");
    assert.equal(data.attributes.provider, "Autolux");
    const path = model.pathTo(autolux.scene, data.key);
    assert.equal(model.schemaKeys(path, settings).at(-1), "AutoluxData");
});

test("address helper validates universe and address before writing", () => {
    const current = draft();
    const address = child(child(fixtureNode(current, "spot_right"), "Addresses"), "Address");
    const [moved] = edit(current, { action: "set-address", key: address.key, universe: 2, address: 7 });
    assert.equal(child(child(fixtureNode(moved, "spot_right"), "Addresses"), "Address").text, "519");
    assert.equal(edit(current, { action: "set-address", key: address.key, universe: 0, address: 7 })[0], ds.no_update);
    assert.equal(edit(current, { action: "set-address", key: address.key, universe: 1, address: 513 })[0], ds.no_update);
});

test("busy saves freeze edits and responses keep the selection on the same object", () => {
    const current = draft();
    const spot = fixtureNode(current, "spot_left");
    assert.equal(edit(current, { action: "remove-node", key: spot.key }, { busy: "true" })[0], ds.no_update);
    const saved = structuredClone(current);
    for (const node of nodes(saved.scene)) node.key = `saved-${node.key}`;
    ds.callback_context.triggered = [{ prop_id: "fixture-server-result.data" }];
    try {
        const result = edit(current, null, { response: { draft: saved, revision: "next" }, selection: spot.key });
        assert.equal(result[0], saved);
        assert.equal(result[1], "next");
        assert.equal(result[3], `saved-${spot.key}`);
        const failed = edit(current, null, { response: { error: "MVR is not spec-conforming: x" } });
        assert.equal(failed[0], ds.no_update);
        assert.match(failed[2], /spec-conforming/);
    } finally {
        delete ds.callback_context.triggered;
    }
});

test("selecting a node changes only the inspector, never the rig layout", () => {
    const current = draft();
    const spot = fixtureNode(current, "spot_left");
    const idle = ds.fixture_patch.renderRig(null, current, settings);
    const picked = ds.fixture_patch.renderRig(spot.key, current, settings);
    assert.deepEqual(plain(picked.slice(0, 2)), plain(idle.slice(0, 2)));
    assert.equal(picked.at(-1), idle.at(-1));
    assert.equal(idle[2], true);
    assert.equal(picked[2], false);
    assert.equal(picked[3], "Universe 1 · 8 fixtures");
});

test("the rig plan marks each fixture at its MVR position, coloured by group, accents ringed", () => {
    const plan = model.planMarks(draft(), settings);
    const byName = Object.fromEntries(plan.fixtures.map((mark) => [mark.name, mark]));
    assert.deepEqual(plain([byName.wash_left.x, byName.wash_left.y, byName.wash_left.group]), [-900, -1000, "wash"]);
    assert.equal(byName.kinta.accent, true);
    assert.equal(byName.spot_left.accent, false);
    // Hung turned around (yaw 180): the local Y axis points downstage, toward the audience.
    assert.deepEqual(plain(byName.spot_left.facing.map(Math.round)), [0, -1]);
    const markup = model.planSvg(plan, settings);
    // Nine-area grid over the fixtures' box (X -900..900, SVG y 0..2000), cut in thirds.
    const grid = (markup) => markup.slice(markup.indexOf('class="plan-areas"'), markup.indexOf("</g>", markup.indexOf("plan-areas")));
    assert.match(grid(markup), /<rect x="-900" y="0" width="1800" height="2000"\/>/);
    const cuts = (markup) => [...grid(markup).matchAll(/<line x1="(-?\d+)" y1="(-?\d+)" x2="(-?\d+)" y2="(-?\d+)"/g)].map((match) => match.slice(1).map(Number));
    assert.deepEqual(cuts(markup), [[-300, 0, -300, 2000], [300, 0, 300, 2000], [-900, 667, 900, 667], [-900, 1333, 900, 1333]]);
    // Each area named in its cell's bottom-left corner, inset 0.6 r (r = 36 mm here), upstage
    // (the top of the plan) first.
    const names = (markup) => [...markup.matchAll(/<text class="plan-area-name" x="(-?\d+)" y="(-?\d+)">([^<]+)</g)].map((match) => [Number(match[1]), Number(match[2]), match[3]]);
    assert.deepEqual(names(markup), [
        [-878, 645, "up left"], [-278, 645, "up centre"], [322, 645, "up right"],
        [-878, 1312, "centre left"], [-278, 1312, "centre"], [322, 1312, "centre right"],
        [-878, 1978, "down left"], [-278, 1978, "down centre"], [322, 1978, "down right"]
    ]);
    assert.match(markup, /class="plan-stage-edge"/);
    // A 2 m span gets the largest 1-2-5 step within a fifth of it.
    assert.match(markup, />0.2 m<\/text>/);
    for (const name of ["spot_left", "wash_mid_right", "kinta"]) assert.ok(markup.includes(`>${name}</text>`), name);
    // The audience is at the bottom: the most downstage fixture (the strip, Y -2000) is lowest.
    assert.match(markup, /<circle cx="0" cy="2000"/);
    const kinta = markup.slice(markup.indexOf("<title>kinta"));
    assert.match(kinta.slice(0, kinta.indexOf("</g>")), /fill="none" class="ring"/);
    assert.match(markup, /fx <b>1<\/b><\/span>/);
    const offCentre = { ...byName.kinta, x: 4000 };
    // One row: no depth extent, so only the X cuts (4000..9000 at 5667 and 7333).
    const row = model.planSvg({ fixtures: [offCentre, { ...offCentre, x: 9000 }], others: [] }, settings);
    assert.deepEqual(cuts(row).map(([x]) => x), [5667, 7333]);
    assert.ok(!grid(row).includes("<rect"));
    assert.deepEqual(names(row).map(([, , name]) => name), ["centre left", "centre", "centre right"]);
    // One fixture: no box and no cuts, only its own area, the centre.
    const lone = model.planSvg({ fixtures: [offCentre], others: [] }, settings);
    assert.ok(!grid(lone).includes("<rect"));
    assert.deepEqual(cuts(lone), []);
    assert.deepEqual(names(lone), [[4022, Math.round(-byName.kinta.y - 21.6), "centre"]]);
});

test("a 500-fixture, 30-group plan stays compact and keeps its groups apart", () => {
    const fixtures = Array.from({ length: 500 }, (_, index) => ({
        key: `n${index}`,
        name: `fixture_${index}`,
        group: `group_${String(index % 30).padStart(2, "0")}`,
        accent: index % 30 < 6,
        x: (index % 25) * 1200 - 14400,
        y: Math.floor(index / 25) * 900 - 9000,
        facing: [0, 1]
    }));
    const markup = model.planSvg({ fixtures, others: [] }, settings);
    assert.ok(markup.length < 200 * 1024, `${markup.length} bytes`);
    // Too dense to label: names move to hover titles.
    assert.equal((markup.match(/<title>/g) || []).length, 500);
    assert.ok(!markup.includes(">fixture_0</text>"));
    const legend = markup.slice(markup.indexOf("plan-legend"), markup.indexOf("<svg class=\"plan\""));
    assert.equal(new Set([...legend.matchAll(/fill="([^"]+)"/g)].map((match) => match[1])).size, 30);
    assert.equal((markup.match(/class="ring"/g) || []).length, 6 * 17);
});

test("zooming keeps the point under the cursor, clamps to 0.5x..8x of the fit, and keeps marks their fitted size", () => {
    const fit = { x: -1000, y: 0, w: 2000, h: 1000 };
    const at = [500, 250];
    const near = model.zoomPlan(fit, fit, at, at, 0.5);
    assert.deepEqual(plain(near), { x: -250, y: 125, w: 1000, h: 500 });
    // The cursor point sits at the same fraction of the view before and after.
    assert.deepEqual([(at[0] - near.x) / near.w, (at[1] - near.y) / near.h], [(at[0] - fit.x) / fit.w, (at[1] - fit.y) / fit.h]);
    assert.equal(model.zoomPlan(near, fit, at, at, 1e-3).w, fit.w / 8);
    assert.equal(model.zoomPlan(near, fit, at, at, 1e3).w, fit.w * 2);
    // A pan is a unit scale: the point that was at `at` moves to `anchor`.
    assert.deepEqual(plain(model.zoomPlan(fit, fit, [0, 0], [100, 50], 1)), { x: -1100, y: -50, w: 2000, h: 1000 });

    const plan = model.planMarks(draft(), settings);
    const fitted = model.planSvg(plan, settings);
    const [view, fitBox] = fitted.match(/viewBox="([^"]+)" data-fit="([^"]+)"/).slice(1);
    assert.equal(view, fitBox);
    const [x, y, w, h] = fitBox.split(" ").map(Number);
    const zoomed = model.planSvg(plan, settings, model.zoomPlan({ x, y, w, h }, { x, y, w, h }, [0, 1000], [0, 1000], 0.2));
    const size = (markup, pattern) => Number(markup.slice(markup.indexOf("<svg class=\"plan\"")).match(pattern)[1]);
    for (const pattern of [/<circle cx="[^"]+" cy="[^"]+" r="([^"]+)"/, /font-size="([^"]+)"/]) {
        assert.ok(Math.abs(size(zoomed, pattern) - size(fitted, pattern) / 5) < 0.1, String(pattern));
    }
    assert.match(zoomed, />0.05 m<\/text>/);
    assert.match(zoomed, new RegExp(`data-fit="${fitBox}"`));
});

const matrixOf = (tree, name) => model.parseMatrix(child(fixtureNode(tree, name), "Matrix")?.text ?? "");
const layerOf = (tree, name) => model.placedObjects(tree, settings).find((entry) => entry.node.attributes.name === name).layer;
// 10 mm per screen pixel, SVG origin at pixel (50, 50): a plain inverse screen CTM.
const INVERSE = { a: 10, b: 0, c: 0, d: 10, e: -500, f: -500 };

function yawedLayer() {
    const current = draft();
    const layer = model.pathTo(current.scene, fixtureNode(current, "spot_left").key).find((node) => node.tag === "Layer");
    const value = model.formatMatrix(model.composeMatrix([1000, 0, 0], [90, 0, 0]));
    return edit(current, { action: "set-child-text", key: layer.key, tag: "Matrix", value })[0];
}

test("a plan drag maps screen to world to the rotated parent's frame in whole mm", () => {
    const rotated = yawedLayer();
    const spot = fixtureNode(rotated, "spot_left");
    const before = model.planMarks(rotated, settings).fixtures.find((mark) => mark.key === spot.key);
    const drag = model.startPlanDrag(rotated, settings, spot.key, INVERSE, 100, 100, { x: 0, y: 0, w: 10, h: 10 });
    // +30 px right, -20 px up the screen: world +300 mm X and, SVG y being -Y, +200 mm Y.
    const move = model.planMove(drag, 130.04, 80);
    // The layer is yawed 90°: world +X is the layer's -Y and world +Y its +X.
    const local = matrixOf(rotated, "spot_left");
    assert.deepEqual([move.x_mm, move.y_mm], [local[9] + 200, local[10] - 300]);
    const [moved] = edit(rotated, { action: "set-position", key: spot.key, original_matrix: drag.original, x_mm: move.x_mm, y_mm: move.y_mm });
    const after = model.planMarks(moved, settings).fixtures.find((mark) => mark.key === spot.key);
    assert.deepEqual([Math.round(after.x - before.x), Math.round(after.y - before.y)], [300, 200]);
    assert.equal(model.startPlanDrag(rotated, settings, rotated.scene.key, INVERSE, 0, 0, null), null);
});

test("the plan keeps its frozen viewBox while a drag previews the mark and the area grid", () => {
    const current = draft();
    const spot = fixtureNode(current, "spot_left");
    const drag = model.startPlanDrag(current, settings, spot.key, INVERSE, 0, 0, { x: -1200, y: -400, w: 2400, h: 2900 });
    // Far outside the rig: a live layout would grow the view, the frozen one must not.
    const { markup } = model.planMove(drag, 900, 0);
    assert.match(markup, /viewBox="-1200 -400 2400 2900"/);
    const x = model.placedObjects(current, settings).find((entry) => entry.node === spot).matrix[9] + 9000;
    assert.match(markup, new RegExp(`<circle cx="${x}" `));
    assert.match(markup, new RegExp(`class="plan-areas"[^]*?<rect x="-900" y="0" width="${x + 900}"`));
});

test("a fixture with no Matrix drags from the identity", () => {
    const current = draft();
    const spot = fixtureNode(current, "spot_left");
    spot.children.splice(spot.children.indexOf(child(spot, "Matrix")), 1);
    const drag = model.startPlanDrag(current, settings, spot.key, INVERSE, 100, 100, { x: 0, y: 0, w: 10, h: 10 });
    const move = model.planMove(drag, 110, 100);
    const [moved] = edit(current, { action: "set-position", key: spot.key, original_matrix: drag.original, x_mm: move.x_mm, y_mm: move.y_mm });
    assert.equal(child(fixtureNode(moved, "spot_left"), "Matrix").text, `{1,0,0}{0,1,0}{0,0,1}{${move.x_mm},${move.y_mm},0}`);
});

test("set-position refuses a matrix changed meanwhile and keeps Z and rotation", () => {
    const current = draft();
    const spot = fixtureNode(current, "spot_left");
    const original = child(spot, "Matrix").text;
    const [moved] = edit(current, { action: "set-position", key: spot.key, original_matrix: original, x_mm: 123, y_mm: -456 });
    const [before, after] = [matrixOf(current, "spot_left"), matrixOf(moved, "spot_left")];
    assert.deepEqual(plain(after.slice(9)), [123, -456, before[11]]);
    after.slice(0, 9).forEach((value, index) => assert.ok(Math.abs(value - before[index]) < 1e-6));
    const [turned] = edit(current, { action: "set-matrix", key: spot.key, position: [0, 0, 0], rotation: [10, 0, 0] });
    assert.equal(edit(turned, { action: "set-position", key: spot.key, original_matrix: original, x_mm: 1, y_mm: 1 })[0], ds.no_update);
    assert.equal(edit(current, { action: "set-position", key: spot.key, original_matrix: original, x_mm: 1.5, y_mm: 1 })[0], ds.no_update);
});

test("a pitched parent's plan drag is exact and an on-edge parent refuses it", () => {
    const current = draft();
    const layer = model.pathTo(current.scene, fixtureNode(current, "spot_left").key).find((node) => node.tag === "Layer");
    const tilt = (degrees) => edit(current, { action: "set-child-text", key: layer.key, tag: "Matrix", value: model.formatMatrix(model.composeMatrix([0, 0, 0], [0, degrees, 0])) })[0];
    const pitched = tilt(30);
    const spot = fixtureNode(pitched, "spot_left");
    const before = model.planMarks(pitched, settings).fixtures.find((mark) => mark.key === spot.key);
    const drag = model.startPlanDrag(pitched, settings, spot.key, INVERSE, 100, 100, { x: 0, y: 0, w: 10, h: 10 });
    // 20 px up the screen is world +200 mm Y, which the 30° pitch shortens to cos 30° per local mm.
    const move = model.planMove(drag, 100, 80);
    const [moved] = edit(pitched, { action: "set-position", key: spot.key, original_matrix: drag.original, x_mm: move.x_mm, y_mm: move.y_mm });
    const after = model.planMarks(moved, settings).fixtures.find((mark) => mark.key === spot.key);
    assert.deepEqual([Math.round(after.x - before.x), Math.round(after.y - before.y)], [0, 200]);
    assert.equal(model.startPlanDrag(tilt(90), settings, spot.key, INVERSE, 100, 100, { x: 0, y: 0, w: 10, h: 10 }), null);
});

test("the patch form validates each field and maps it to one draft action", () => {
    const current = draft();
    const key = fixtureNode(current, "wash_right").key;
    const check = (field, value, tree = current, target = key) => model.patchEdit(tree, settings, target, field, value);
    // Address: the room's range for 8 channels, and live overlaps from the occupancy.
    assert.match(check("address", "1").error, /Overlaps spot_left/);
    assert.match(check("address", "506").error, /1–505 for 8 channels/);
    const address = child(child(fixtureNode(current, "wash_right"), "Addresses"), "Address").key;
    assert.deepEqual(plain(check("address", "200")), { error: null, action: { action: "set-address", key: address, universe: 1, address: 200 } });
    // Mode: a 12-channel spot parked at 505 no longer fits; a mode not in the profile is refused.
    const spot = fixtureNode(current, "spot_left");
    const [parked] = edit(current, { action: "set-address", key: child(child(spot, "Addresses"), "Address").key, universe: 1, address: 505 });
    assert.match(check("GDTFMode", "12ch", parked, spot.key).error, /1–501 for 12 channels/);
    assert.match(check("GDTFMode", "nope", current, spot.key).error, /Pick a mode/);
    // A profile change keeping the mode is held to the same fit.
    assert.match(check("GDTFSpec", child(spot, "GDTFSpec").text, parked, spot.key).error, /1–501 for 12 channels/);
    // Name: non-empty, unique, at most 128 bytes; a rename edits the label alone, so nothing warns.
    assert.match(check("name", " ").error, /required/);
    assert.match(check("name", "spot_right").error, /Another fixture/);
    assert.match(check("name", "é".repeat(65)).error, /128 bytes/);
    const renamed = check("name", "wash_far_right");
    assert.deepEqual(plain(renamed), { error: null, action: { action: "set-attribute", doc: "scene", key, name: "name", value: "wash_far_right" } });
    const [relabelled] = edit(current, renamed.action);
    const row = (tree) => plain(model.fixtureRows(tree, settings).find((entry) => entry.key === key));
    assert.deepEqual({ ...row(relabelled), name: "wash_right" }, row(current));
    // Patch number: a whole number, a duplicate only warned; FixtureIDNumeric follows FixtureID.
    assert.match(check("FixtureID", child(spot, "FixtureID").text).warning, /spot_left has the same patch number/);
    assert.match(check("FixtureID", "A-1").error, /whole number/);
    const [numbered] = edit(current, check("FixtureID", "17").action);
    assert.deepEqual(["FixtureID", "FixtureIDNumeric"].map((tag) => child(fixtureNode(numbered, "wash_right"), tag).text), ["17", "17"]);
    assert.match(check("matrix", ["0", "0", "0", "0", "61", "0"]).error, /±60°/);
    // Soft limits in degrees within the GDTF's physical range (the spot pans 0–540°, tilts 0–270°),
    // stored as the share of it Starfire maps; edits past the range clamp to it.
    assert.match(check("Pan", ["400", "100"], current, spot.key).error, /Min is above max/);
    assert.match(check("Pan", ["", "100"], current, spot.key).error, /both limits/);
    const [limited] = edit(current, check("Pan", ["54", "486"], current, spot.key).action);
    const movement = [...nodes(limited.scene)].find((node) => node.tag === "Movement");
    assert.deepEqual(plain(movement.attributes), { fixture: spot.attributes.uuid, axis: "Pan", minimum: "0.1", maximum: "0.9" });
    assert.deepEqual(plain(check("Tilt", ["-20", "300"], current, spot.key)), { error: null, action: { action: "set-movement", key: spot.key, axis: "Tilt", minimum: 0, maximum: 1 } });
    // The range's ends are the share's ends exactly, on both axes.
    for (const [axis, top] of [["Pan", "540"], ["Tilt", "270"]]) {
        assert.deepEqual(plain(check(axis, ["0", top], current, spot.key).action), { action: "set-movement", key: spot.key, axis, minimum: 0, maximum: 1 });
        assert.deepEqual(plain(check(axis, [top, top], current, spot.key).action), { action: "set-movement", key: spot.key, axis, minimum: 1, maximum: 1 });
    }
});

test("the mount toggle turns the fixture 180° about its facing and keeps its rotation relative to the mount", () => {
    const current = draft();
    const spot = fixtureNode(current, "spot_left");
    const matrix = (tree) => model.parseMatrix(child(fixtureNode(tree, "spot_left"), "Matrix").text);
    const toggle = model.patchEdit(current, settings, spot.key, "mount", "standing");
    assert.deepEqual(plain(toggle.action), { action: "set-matrix", key: spot.key, position: [-300, 0, 0], rotation: [180, 0, 0], standing: true });
    const [standing] = edit(current, toggle.action);
    // Hung turned around {-1,0,0}{0,-1,0}{0,0,1}: standing negates its local X and Z, so it
    // still faces downstage and Starfire reads the same yaw, reversed.
    assert.deepEqual(plain(matrix(standing)), [1, 0, 0, 0, -1, 0, 0, 0, -1, -300, 0, 0]);
    assert.deepEqual(plain(model.homeOrientation(matrix(standing))), { pan: 180, tilt: 0, reversed: true });
    // A rotation edit on a standing fixture stays standing.
    const [yawed] = edit(standing, model.patchEdit(standing, settings, spot.key, "matrix", ["-300", "0", "0", "90", "0", "0"]).action);
    assert.ok(matrix(yawed)[8] < 0);
    const [hanging] = edit(standing, model.patchEdit(standing, settings, spot.key, "mount", "hanging").action);
    assert.deepEqual(plain(matrix(hanging)), plain(matrix(current)));
});

test("a role move keeps each fixture where it hangs, out of a nested group and a yawed layer", () => {
    const yawed = yawedLayer();
    const list = model.pathTo(yawed.scene, fixtureNode(yawed, "spot_left").key).at(-2);
    const [withGroup] = edit(yawed, { action: "add-child", key: list.key, tag: "GroupObject" });
    const truss = [...nodes(withGroup.scene)].find((node) => node.tag === "GroupObject");
    const value = model.formatMatrix(model.composeMatrix([250, -400, 3000], [30, 0, 0]));
    let [current] = edit(withGroup, { action: "set-child-text", key: truss.key, tag: "Matrix", value });
    if (!child([...nodes(current.scene)].find((node) => node.tag === "GroupObject"), "ChildList")) {
        [current] = edit(current, { action: "add-child", key: [...nodes(current.scene)].find((node) => node.tag === "GroupObject").key, tag: "ChildList" });
    }
    // Hang spot_left on the truss by hand: the editor has no move-into-group action.
    const spot = fixtureNode(current, "spot_left");
    const parent = model.pathTo(current.scene, spot.key).at(-2);
    parent.children.splice(parent.children.indexOf(spot), 1);
    child([...nodes(current.scene)].find((node) => node.tag === "GroupObject"), "ChildList").children.push(spot);
    const worlds = (tree) => Object.fromEntries(model.placedObjects(tree, settings).filter((entry) => entry.node.tag === "Fixture").map((entry) => [entry.node.attributes.name, entry.matrix]));
    const [moved] = edit(current, model.patchEdit(current, settings, spot.key, "role", "accent").action);
    assert.equal(layerOf(moved, "spot_left"), "accent");
    const [before, after] = [worlds(current), worlds(moved)];
    assert.deepEqual(Object.keys(after).sort(), Object.keys(before).sort());
    for (const [name, matrix] of Object.entries(before)) matrix.forEach((value, index) => assert.ok(Math.abs(after[name][index] - value) < 1e-3, `${name}[${index}]`));
    const inverse = model.invert(model.composeMatrix([250, -400, 3000], [30, 10, 5]));
    model.multiply(inverse, model.composeMatrix([250, -400, 3000], [30, 10, 5])).forEach((value, index) => assert.ok(Math.abs(value - [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0][index]) < 1e-9));
});

test("a role moves its whole group, and a joining fixture lands in its group's layer", () => {
    const current = draft();
    const [accented] = edit(current, model.patchEdit(current, settings, fixtureNode(current, "wash_left").key, "role", "accent").action);
    const washes = ["wash_left", "wash_mid_left", "wash_mid_right", "wash_right"];
    assert.deepEqual(washes.map((name) => layerOf(accented, name)), washes.map(() => "accent"));
    assert.equal(layerOf(accented, "spot_left"), "loop");
    const [joined] = edit(accented, model.patchEdit(accented, settings, fixtureNode(accented, "spot_left").key, "group", "wash").action);
    assert.equal(layerOf(joined, "spot_left"), "accent");
    const kinta = fixtureNode(current, "kinta").key;
    const [created] = edit(current, model.patchEdit(current, settings, kinta, "newGroup", "haze").action);
    assert.equal(model.fixtureRows(created, settings).find((row) => row.name === "kinta").className, "haze");
    assert.match(model.patchEdit(current, settings, kinta, "newGroup", " ").error, /Name the new group/);
    const [ungrouped] = edit(current, { action: "set-class", key: kinta, name: "" });
    assert.match(model.patchEdit(ungrouped, settings, kinta, "role", "loop").error, /group first/);
});

test("the test controls send highlight commands, hold them alive and clear once on leave", async () => {
    const current = draft();
    const spot = fixtureNode(current, "spot_right");
    const live = { rig: RUNNING, paused: false, static: { blackout: false }, highlight: null };
    commands.length = actions.length = 0;
    ds.fixture_patch.renderRig(spot.key, current, settings);
    ds.fixture_patch.syncTest({ ...live, paused: true }, "config");
    assert.equal(model.testControl("red"), null);
    ds.fixture_patch.syncTest({ ...live, static: { blackout: true } }, "config");
    assert.equal(model.testControl("red"), null);
    ds.fixture_patch.syncTest(live, "config");
    assert.deepEqual(plain(model.testControl("red")), { action: "highlight", target: { fixture: FIXTURE_UUID.spot_right }, colour: "red", ...posed("spot_right") });
    // One keep-alive inside Starfire's 60 s dead-man, re-sending the same command, and the 10-minute end.
    assert.deepEqual([...timers.values()].map((timer) => timer.ms), [15000, 600000]);
    [...timers.values()][0].run();
    assert.deepEqual(plain(commands.at(-1)), plain(commands.at(-2)));
    assert.equal(timers.size, 2);
    const sent = commands.length;
    ds.fixture_patch.syncTest(live, "control");
    ds.fixture_patch.syncTest(live, "telemetry");
    assert.deepEqual(plain(commands.slice(sent)), [{ action: "highlight", target: null }]);
    assert.equal(timers.size, 0);
    // Deselecting clears once; a refused test (409) counts as off and needs no clear.
    ds.fixture_patch.syncTest(live, "config");
    model.testControl("blue");
    ds.fixture_patch.renderRig(null, current, settings);
    ds.fixture_patch.renderRig(null, current, settings);
    assert.deepEqual(plain(commands.slice(-2)), [{ action: "highlight", target: { fixture: FIXTURE_UUID.spot_right }, colour: "blue", ...posed("spot_right") }, { action: "highlight", target: null }]);
    ds.fixture_patch.renderRig(spot.key, current, settings);
    replies.push({ error: "no fixture 'spot_right' in the running rig", status: 409 });
    model.testControl("white");
    await new Promise(setImmediate);
    assert.equal(timers.size, 0);
    const refused = commands.length;
    ds.fixture_patch.syncTest(live, "control");
    assert.equal(commands.length, refused);
});

test("the first Test click lights Test at once, and a refusal says why", async () => {
    const current = draft();
    const [spot, kinta] = ["spot_right", "kinta"].map((name) => fixtureNode(current, name));
    const live = { rig: RUNNING, paused: false, static: { blackout: false }, highlight: null };
    const button = (cfgTest, kind) => {
        const classes = new Set([kind]);
        return { dataset: { cfgTest }, disabled: false, classList: { contains: (name) => classes.has(name), toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)) } };
    };
    const buttons = [button("test", "cfg-test-main"), button("red", "cfg-swatch")];
    const note = { textContent: "" };
    const strip = { querySelectorAll: () => buttons };
    page.querySelector = (selector) => ({ "#fixture-info .cfg-test": strip, "#fixture-info .cfg-test-note": note })[selector] ?? null;
    const lit = () => buttons[0].classList.contains("active");
    try {
        timers.clear();
        ds.fixture_patch.renderRig(spot.key, current, settings);
        ds.fixture_patch.syncTest(live, "config");
        const sent = commands.length;
        model.testControl("test");
        // Lit before any echo, and a poll from before the click does not flip it back.
        assert.ok(lit());
        ds.fixture_patch.syncTest(live, "config");
        assert.ok(lit());
        const pose = plain(model.draftPose(current, settings, model.fixtureRows(current, settings).find((entry) => entry.key === spot.key)));
        assert.deepEqual(plain(commands.slice(sent)), [{ action: "highlight", target: { fixture: FIXTURE_UUID.spot_right }, colour: "white", pose }]);
        model.testControl("test");
        assert.ok(!lit());
        // After a selection change the first click is one command and lights Test too.
        ds.fixture_patch.renderRig(kinta.key, current, settings);
        model.testControl("test");
        assert.ok(lit());
        assert.deepEqual(plain(commands.at(-1)), { action: "highlight", target: { fixture: FIXTURE_UUID.kinta }, colour: "white" });
        model.testControl("test");
        replies.push({ error: "the show is paused", status: 409 });
        model.testControl("test");
        await new Promise(setImmediate);
        assert.ok(!lit());
        assert.equal(note.textContent, "Test refused: the show is paused");
    } finally {
        delete page.querySelector;
        ds.fixture_patch.renderRig(null, current, settings);
    }
});

test("only a mover's test row offers Sweep and Aim, which toggle its motion and keep the swatch", () => {
    const element = () => ({ attributes: {}, children: [], setAttribute(name, value) { this.attributes[name] = value; }, append(...children) { this.children.push(...children); } });
    const specs = (nodes) => nodes.flatMap(function visit(node) { return typeof node === "object" ? [node.attributes["data-cfg-test"], ...node.children.flatMap(visit)] : []; }).filter(Boolean);
    const current = draft();
    page.createElement = element;
    try {
        ds.fixture_patch.renderRig(fixtureNode(current, "spot_right").key, current, settings);
        assert.ok(["sweep", "aim"].every((spec) => specs(model.patchForm(current, settings, fixtureNode(current, "spot_right"))).includes(spec)));
        for (const still of ["kinta", "wash_left"]) assert.ok(!specs(model.patchForm(current, settings, fixtureNode(current, still))).some((spec) => spec === "sweep" || spec === "aim"), still);
    } finally {
        delete page.createElement;
    }
    ds.fixture_patch.syncTest({ rig: RUNNING, paused: false, static: { blackout: false }, highlight: null }, "config");
    const spot = { action: "highlight", target: { fixture: FIXTURE_UUID.spot_right }, ...posed("spot_right") };
    assert.deepEqual(plain(model.testControl("sweep")), { ...spot, colour: "white", sweep: true });
    assert.deepEqual(plain(model.testControl("red")), { ...spot, colour: "red", sweep: true });
    assert.deepEqual(plain(model.testControl("aim")), { ...spot, colour: "red", aim: true });
    // A second click on the lit motion holds the light still; a second Test clears it all.
    assert.deepEqual(plain(model.testControl("aim")), { ...spot, colour: "red" });
    assert.deepEqual(plain(model.testControl("test")), { action: "highlight", target: null });
    assert.deepEqual(plain(model.testControl("test")), { ...spot, colour: "white" });
    assert.deepEqual(plain(model.testControl("test")), { action: "highlight", target: null });
});

test("a mover's test pose is the placement the server exports, and a focus edit adds the MVR FocusPoint", () => {
    const current = draft();
    for (const row of model.fixtureRows(current, settings)) {
        const pose = model.draftPose(current, settings, row);
        assert.deepEqual(pose && plain(pose), row.name.startsWith("spot_") ? POSES[row.fixture_uuid] : undefined, row.name);
    }
    const spot = fixtureNode(current, "spot_left");
    // Without a FocusPoint the pane shows the stage centre: the fixtures' plan box centre on the floor.
    const plan = model.fixtureRows(current, settings).map((row) => row.matrix.slice(9, 11));
    const middle = (axis) => (Math.min(...plan.map((p) => p[axis])) + Math.max(...plan.map((p) => p[axis]))) / 2;
    const shown = (nodes) => nodes.flatMap(function visit(node) { return typeof node !== "object" ? [] : node.attributes["data-cfg"] === "focus" ? [node] : node.children.flatMap(visit); });
    page.createElement = (tag) => ({ tag, attributes: {}, children: [], setAttribute(name, value) { this.attributes[name] = value; }, append(...children) { this.children.push(...children); } });
    try {
        const [row0] = shown(model.patchForm(current, settings, spot));
        const inputs = (function all(node) { return typeof node !== "object" ? [] : node.tag === "input" ? [node] : node.children.flatMap(all); })(row0);
        assert.deepEqual(inputs.map((input) => Number(input.attributes.value)), [middle(0), middle(1), 0]);
    } finally {
        delete page.createElement;
    }
    assert.notDeepEqual([middle(0), middle(1)], [plan[0][0], plan[0][1]]);
    const focus = model.patchEdit(current, settings, spot.key, "focus", ["-300", "2500", "0"]);
    assert.deepEqual(plain(focus.action), { action: "set-focus", key: spot.key, position: [-300, 2500, 0] });
    assert.equal(model.patchEdit(current, settings, spot.key, "focus", ["1", "", "2"]).error, "Enter X, Y and Z in mm.");
    const [focused] = edit(current, focus.action);
    const point = [...nodes(focused.scene)].find((node) => node.tag === "FocusPoint");
    assert.match(point.attributes.uuid, UUID);
    assert.equal(child(fixtureNode(focused, "spot_left"), "Focus").text, point.attributes.uuid);
    assert.deepEqual(plain(point.children.map((entry) => entry.tag)), ["Matrix", "Geometries"]);
    const row = model.fixtureRows(focused, settings).find((entry) => entry.key === spot.key);
    assert.deepEqual(plain(model.draftPose(focused, settings, row).focus), [-300, 2500, 0]);
    // A second edit moves the same point.
    const [moved] = edit(focused, model.patchEdit(focused, settings, spot.key, "focus", ["0", "0", "0"]).action);
    assert.equal([...nodes(moved.scene)].filter((node) => node.tag === "FocusPoint").length, 1);
});

test("an edit to the lit mover's yaw or focus re-sends its test with the new pose once settled", () => {
    const current = draft();
    const spot = fixtureNode(current, "spot_right");
    const live = { rig: RUNNING, paused: false, static: { blackout: false }, highlight: null };
    timers.clear();
    ds.fixture_patch.renderRig(spot.key, current, settings);
    ds.fixture_patch.syncTest(live, "config");
    model.testControl("aim");
    const settling = () => [...timers.values()].filter((timer) => timer.ms === 150);
    const sent = commands.length;
    ds.fixture_patch.renderRig(spot.key, current, settings);
    assert.equal(settling().length, 0, "an unchanged draft sends nothing");
    const yaw = (tree, degrees) => edit(tree, model.patchEdit(tree, settings, spot.key, "matrix", ["300", "0", "0", String(degrees), "0", "0"]).action)[0];
    const [turned, more] = [yaw(current, 90), yaw(current, 120)];
    ds.fixture_patch.renderRig(spot.key, turned, settings);
    ds.fixture_patch.renderRig(spot.key, more, settings);
    assert.equal(settling().length, 1, "edits in quick succession send once");
    assert.equal(commands.length, sent);
    settling()[0].run();
    const row = (tree) => model.fixtureRows(tree, settings).find((entry) => entry.key === spot.key);
    assert.deepEqual(plain(commands.at(-1)), { action: "highlight", target: { fixture: FIXTURE_UUID.spot_right }, colour: "white", aim: true, pose: plain(model.draftPose(more, settings, row(more))) });
    assert.equal(Math.round(model.homeOrientation(commands.at(-1).pose.matrix).pan), 120);
    const [focused] = edit(more, model.patchEdit(more, settings, spot.key, "focus", ["0", "3000", "0"]).action);
    ds.fixture_patch.renderRig(spot.key, focused, settings);
    settling()[0].run();
    assert.deepEqual(plain(commands.at(-1).pose.focus), [0, 3000, 0]);
    assert.equal(commands.at(-1).aim, true);
    model.testControl("off");
    assert.equal(settling().length, 0);
});

test("Restart show restarts the show only on a second click within 5 s", async () => {
    const sent = commands.length;
    timers.clear();
    assert.equal(model.applyClick(), null);
    assert.deepEqual(labels.at(-1), ["fixture-apply", "Click again to restart; lights go dark until it is back"]);
    // Left alone, the arm lapses and the next click only arms again.
    const [[id, lapse]] = [...timers];
    assert.equal(lapse.ms, 5000);
    timers.delete(id);
    lapse.run();
    assert.deepEqual(labels.at(-1), ["fixture-apply", "Restart show"]);
    assert.equal(model.applyClick(), null);
    assert.equal(commands.length, sent);
    assert.deepEqual(plain(model.applyClick()), { action: "restart_show" });
    assert.deepEqual(plain(commands.slice(sent)), [{ action: "restart_show" }]);
    assert.equal(timers.size, 0);
    await new Promise(setImmediate);
    assert.deepEqual(labels.slice(-2), [
        ["fixture-apply", "Restart show"],
        ["fixture-show-rig", "Restarting the show on the saved patch."]
    ]);
});

test("a lit test follows the selection; late or unanswered replies never turn it off", async () => {
    const current = draft();
    const [spot, kinta] = ["spot_right", "kinta"].map((name) => fixtureNode(current, name));
    const live = { rig: RUNNING, paused: false, static: { blackout: false }, highlight: null };
    timers.clear();
    ds.fixture_patch.renderRig(spot.key, current, settings);
    ds.fixture_patch.syncTest(live, "config");
    // An older test's late 409 must not stop the test that replaced it.
    replies.push({ error: "no fixture 'spot_right' in the running rig", status: 409 });
    model.testControl("red");
    ds.fixture_patch.renderRig(kinta.key, current, settings);
    assert.deepEqual(plain(commands.at(-1)), { action: "highlight", target: { fixture: FIXTURE_UUID.kinta }, colour: "red" });
    await new Promise(setImmediate);
    assert.equal(timers.size, 2, "kinta's keep-alive survives spot_right's late refusal");
    // No answer at all may mean lit: the keep-alive stays and a deselect still clears.
    replies.push({ error: "the show is not answering" });
    [...timers.values()][0].run();
    await new Promise(setImmediate);
    assert.equal(timers.size, 2);
    ds.fixture_patch.renderRig(null, current, settings);
    assert.deepEqual(plain(commands.at(-1)), { action: "highlight", target: null });
    assert.equal(timers.size, 0);
});

test("the Restart banner names an older rig and holds until the next session reports", async () => {
    const live = { rig: RUNNING, paused: false, static: { blackout: false }, highlight: null };
    const banner = (loaded, session = "one", saved = "saved") =>
        plain(ds.fixture_patch.syncTest(loaded === null ? null : { ...live, rig_revision: loaded, session }, "config", saved));
    const stale = "The show runs an older rig. Restart show loads the saved patch.";
    const restarting = "Restarting the show on the saved patch.";
    assert.deepEqual(banner("loaded"), [stale, false]);
    // Restart works on the saved rig too; only no show at all disables it.
    assert.deepEqual(banner("saved"), ["", false]);
    assert.deepEqual(banner(null), ["", true]);
    banner("old");
    timers.clear();
    model.applyClick();
    model.applyClick();
    await new Promise(setImmediate);
    // The old session, or none, reads as restarting; the next session ends it.
    assert.deepEqual(banner("old"), [restarting, true]);
    assert.deepEqual(banner(null), [restarting, true]);
    assert.deepEqual(banner("saved", "two"), ["", false]);
    // A restart without a Save comes back on the same revision, and the new session still ends it.
    timers.clear();
    model.applyClick();
    model.applyClick();
    await new Promise(setImmediate);
    assert.deepEqual(banner("saved", "two"), [restarting, true]);
    assert.deepEqual(banner("saved", "three"), ["", false]);
    // An engine that never takes the restart: the hold lapses after 60 s so Restart works again.
    timers.clear();
    model.applyClick();
    model.applyClick();
    await new Promise(setImmediate);
    assert.deepEqual(banner("old", "three"), [restarting, true]);
    const [[id, hold]] = [...timers];
    assert.equal(hold.ms, 60000);
    timers.delete(id);
    hold.run();
    assert.deepEqual(banner("old", "three"), [stale, false]);
});

test("a held test ends after 10 minutes, and stops when the show drops it unasked", () => {
    const current = draft();
    const spot = fixtureNode(current, "spot_right");
    const live = { rig: RUNNING, paused: false, static: { blackout: false }, highlight: null };
    const lit = { ...live, highlight: { target: { fixture: FIXTURE_UUID.spot_right }, colour: "white" } };
    timers.clear();
    ds.fixture_patch.renderRig(spot.key, current, settings);
    ds.fixture_patch.syncTest(live, "config");
    model.testControl("white");
    const [end] = [...timers.values()].filter((timer) => timer.ms === 600000);
    end.run();
    assert.deepEqual(plain(commands.at(-1)), { action: "highlight", target: null });
    assert.equal(timers.size, 0);
    // Standby, a sound stop or a restart clears the show's highlight: no relight, no clear.
    model.testControl("white");
    ds.fixture_patch.syncTest(lit, "config");
    const sent = commands.length;
    ds.fixture_patch.syncTest(live, "config");
    assert.equal(timers.size, 0);
    ds.fixture_patch.renderRig(null, current, settings);
    assert.equal(commands.length, sent);
});

test("a plan drag writes the Matrix the server's area and group-order test reads", () => {
    // data/plan_drag.json is this result; test_fixture_setup applies it on the server.
    const fixture = JSON.parse(fs.readFileSync(path.join(__dirname, "data/plan_drag.json"), "utf8"));
    const current = draft();
    const spot = fixtureNode(current, fixture.fixture);
    const drag = model.startPlanDrag(current, settings, spot.key, INVERSE, 100, 100, { x: 0, y: 0, w: 10, h: 10 });
    const move = model.planMove(drag, 100 + fixture.right_px, 100);
    const [moved] = edit(current, { action: "set-position", key: spot.key, original_matrix: drag.original, x_mm: move.x_mm, y_mm: move.y_mm });
    assert.equal(child(fixtureNode(moved, fixture.fixture), "Matrix").text, fixture.matrix);
});

test("a rename or a name swap keeps each test on its uuid; a new, unapplied fixture is never sent", () => {
    const current = draft();
    const live = { rig: RUNNING, paused: false, static: { blackout: false }, highlight: null };
    const rename = (tree, from, to) => edit(tree, { action: "set-attribute", doc: "scene", key: fixtureNode(tree, from).key, name: "name", value: to })[0];
    const swapped = rename(rename(rename(current, "spot_left", "spot_tmp"), "spot_right", "spot_left"), "spot_tmp", "spot_right");
    timers.clear();
    ds.fixture_patch.syncTest(live, "config");
    // The label spot_right now sits on the old spot_left: its test lights that uuid.
    ds.fixture_patch.renderRig(fixtureNode(swapped, "spot_right").key, swapped, settings);
    assert.deepEqual(plain(model.testControl("red")), { action: "highlight", target: { fixture: FIXTURE_UUID.spot_left }, colour: "red", ...posed("spot_left") });
    ds.fixture_patch.renderRig(fixtureNode(swapped, "spot_left").key, swapped, settings);
    assert.deepEqual(plain(commands.at(-1)), { action: "highlight", target: { fixture: FIXTURE_UUID.spot_right }, colour: "red", ...posed("spot_right") });
    const renamed = rename(current, "kinta", "kinta_new");
    ds.fixture_patch.renderRig(fixtureNode(renamed, "kinta_new").key, renamed, settings);
    assert.deepEqual(plain(commands.at(-1)), { action: "highlight", target: { fixture: FIXTURE_UUID.kinta }, colour: "red" });
    // A fixture added since the show started has a uuid the show does not know.
    const spec = Object.keys(settings.library).find((name) => name.startsWith("Generic@Dimmer@"));
    const [added, , , fresh] = edit(renamed, null, { trigger: "fixture-add", profile: spec });
    ds.fixture_patch.renderRig(fixtureNode(added, "kinta_new").key, added, settings);
    const sent = commands.length;
    // Selecting the new dimmer cannot light it: the lit kinta is cleared instead.
    ds.fixture_patch.renderRig(fresh, added, settings);
    assert.deepEqual(plain(commands.slice(sent)), [{ action: "highlight", target: null }]);
    assert.equal(model.testControl("red"), null);
    assert.equal(commands.length, sent + 1);
});
