const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const data = JSON.parse(fs.readFileSync(process.env.FIXTURE_DRAFT_DATA, "utf8"));
const ds = { no_update: Symbol("unchanged"), callback_context: {} };
vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../simulate/assets/fixture_patch.js"), "utf8"), {
    window: { dash_clientside: ds, addEventListener() {} },
    document: { addEventListener() {} },
    structuredClone,
    crypto: globalThis.crypto
});
const model = ds.fixture_patch_model;
const plain = (value) => JSON.parse(JSON.stringify(value));
const { settings } = data;
const UUID = /^[0-9A-F]{8}-[0-9A-F]{4}-4[0-9A-F]{3}-[89AB][0-9A-F]{3}-[0-9A-F]{12}$/;

const draft = () => structuredClone(data.draft);
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
    assert.deepEqual(plain(rows.map((row) => [row.name, row.universe, row.address, row.cells])), data.patched);
    assert.deepEqual(plain(model.occupancy(rows, settings, 0).errors), []);
    assert.equal(rows.find((row) => row.name === "spot_left").channels, 12);
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
    assert.equal(selected, created[0].key);
    for (const node of created) {
        assert.match(node.attributes.uuid, UUID);
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
    assert.equal(idle[3], true);
    assert.equal(picked[3], false);
    assert.match(picked[2], /channels/);
});
