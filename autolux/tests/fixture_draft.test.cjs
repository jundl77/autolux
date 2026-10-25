// Draft integrity tests: no browser, DOM rendering or UI snapshots.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const ds = { no_update: Symbol("unchanged"), callback_context: {} };
vm.runInNewContext(
    fs.readFileSync(path.join(__dirname, "../simulate/assets/fixture_patch.js"), "utf8"),
    {
        window: { dash_clientside: ds, addEventListener() {} },
        document: { addEventListener() {} }
    }
);
const settings = {
    profiles: { spot: { channels: 12, movement: ["pan", "tilt"] }, rgb: { channels: 3 } },
    channelCount: 512,
    maxUniverse: 32767,
    maxQuantity: 64,
    offsetLimits: { pan: 540, tilt: 270 }
};
const fixture = (id, address, universe = 0) => ({
    id,
    address,
    universe,
    profile: "spot",
    _original_id: id,
    groups: "movers",
    pan_min: 0,
    pan_max: 1,
    pan_inverted: false,
    tilt_min: 0,
    tilt_max: 1,
    tilt_inverted: false
});

function edit(
    rows,
    action,
    {
        trigger = "fixture-grid-action",
        busy = "false",
        response = null,
        selected = [],
        profile = "spot",
        quantity = 1,
        universe = 1,
        address = null,
        values = []
    } = {}
) {
    ds.callback_context.triggered_id = trigger;
    return ds.fixture_patch.editPatch(
        0,
        0,
        0,
        0,
        action,
        response,
        settings,
        busy,
        rows,
        {},
        "saved-revision",
        selected,
        profile,
        quantity,
        universe,
        address,
        ...values
    );
}

function move(rows, address, universe = 0, options) {
    const row = rows[0];
    return edit(
        rows,
        {
            action: "move",
            fixture_id: row.id,
            original_address: row.address,
            original_universe: row.universe,
            address,
            universe
        },
        options
    );
}

test("rapid moves preserve identity and all other draft edits without mutating the source", () => {
    const original = [fixture("left", 1), fixture("right", 13)];
    let rows = original;
    for (const address of [31, 201, 501, 31, 1]) {
        rows = move(rows, address)[0];
        assert.equal(rows[0].address, address);
        assert.equal(rows[0]._original_id, "left");
        assert.equal(rows[0].groups, "movers");
        assert.deepEqual(JSON.parse(JSON.stringify(rows[1])), original[1]);
    }
    assert.equal(original[0].address, 1);
});

test("self overlap and equal addresses in different universes are valid", () => {
    assert.equal(move([fixture("left", 1)], 2)[0][0].address, 2);
    const moved = move([fixture("left", 1), fixture("right", 13)], 13, 1)[0];
    assert.equal(moved[0].universe, 1);
});

test("collisions, overflow and malformed addresses cannot change a draft", () => {
    const rows = [fixture("left", 1), fixture("right", 13)];
    for (const [address, universe] of [
        [13, 0],
        [502, 0],
        [0, 0],
        [1.5, 0],
        [true, 0],
        [1, -1],
        [1, 32768]
    ]) {
        const result = move(rows, address, universe);
        assert.equal(result[0], ds.no_update);
        assert.match(result[3], /Patch unchanged/);
        assert.equal(rows[0].address, 1);
    }
});

test("stale or removed fixture drag cannot overwrite a newer edit", () => {
    const rows = [fixture("left", 31)];
    for (const fixture_id of ["left", "deleted"]) {
        const result = edit(rows, {
            action: "move",
            fixture_id,
            original_address: 1,
            original_universe: 0,
            address: 201,
            universe: 0
        });
        assert.equal(result[0], ds.no_update);
    }
});

test("a batch that runs out of channels is rejected atomically", () => {
    const rows = [fixture("left", 1)];
    const result = edit(rows, null, { trigger: "fixture-add", quantity: 2, address: 501 });
    assert.equal(result[0], ds.no_update);
    assert.equal(rows.length, 1);
});

test("automatic allocation uses gaps and includes channel 512", () => {
    const result = edit([fixture("left", 1), fixture("right", 25)], null, {
        trigger: "fixture-add",
        quantity: 2
    })[0];
    assert.equal(result[2].address, 13);
    assert.equal(result[3].address, 37);
    assert.equal(result[2]._original_id, undefined);
    const last = edit([], null, { trigger: "fixture-add", address: 501 })[0][0];
    assert.equal(last.address + settings.profiles[last.profile].channels - 1, 512);
});

test("save failure keeps both the draft and its saved revision unchanged", () => {
    const rows = [fixture("left", 31)];
    const result = edit(rows, null, {
        trigger: "fixture-server-result",
        response: { error: "stale revision" }
    });
    assert.equal(result[0], ds.no_update);
    assert.equal(result[1], ds.no_update);
    assert.equal(result[2], ds.no_update);
    assert.match(result[3], /stale revision/);
    assert.equal(rows[0].address, 31);
});

test("edits are held while persistence is in flight, but its result can finish", () => {
    const rows = [fixture("left", 1)];
    assert.equal(move(rows, 31, 0, { busy: "true" })[0], ds.no_update);
    const response = {
        draft: [fixture("renamed", 31)],
        patch: { groups: {} },
        revision: "new-revision"
    };
    const result = edit(rows, null, { trigger: "fixture-server-result", busy: "true", response });
    assert.equal(result[0], response.draft);
    assert.equal(result[2], response.revision);
});

test("a queued edit cannot discard the save response and its new revision", () => {
    const rows = [fixture("left", 31)];
    const response = { draft: rows, patch: {}, revision: "new-revision" };
    ds.callback_context.triggered = [
        { prop_id: "fixture-grid-action.data" },
        { prop_id: "fixture-server-result.data" }
    ];
    try {
        const result = move(rows, 201, 0, { response });
        assert.equal(result[0], response.draft);
        assert.equal(result[2], "new-revision");
    } finally {
        delete ds.callback_context.triggered;
    }
});

test("invalid detail edits leave the fixture and identity intact", () => {
    const rows = [fixture("left", 1), fixture("right", 13)];
    for (const values of [
        ["right", "spot", 1, 31, "movers", 0, 1, [], 0, 0, 1, [], 0],
        ["left", "spot", 1, 31, "movers", 0.8, 0.2, [], 0, 0, 1, [], 0],
        ["left", "spot", null, 31, "movers", 0, 1, [], 0, 0, 1, [], 0],
        ["left", "spot", true, 31, "movers", 0, 1, [], 0, 0, 1, [], 0],
        ["left", "spot", 1, 31, "movers", 0, 1, [], 541, 0, 1, [], 0],
        ["left", "spot", 1, 31, "movers", 0, 1, [], 0, 0, 1, [], -271],
        ["left", "spot", 1, 31, "movers", 0, 1, [], null, 0, 1, [], 0]
    ]) {
        assert.equal(
            edit(rows, null, { trigger: "fixture-apply", selected: [0], values })[0],
            ds.no_update
        );
    }
    assert.equal(rows[0].address, 1);
});

test("orientation offsets apply in degrees and are dropped for profiles without movement", () => {
    const rows = [fixture("left", 1)];
    const apply = (profile, pan) =>
        edit(rows, null, {
            trigger: "fixture-apply",
            selected: [0],
            values: ["left", profile, 1, 1, "", 0, 1, [], pan, 0, 1, [], -45.5]
        })[0][0];
    const turned = apply("spot", 180);
    assert.equal(turned.pan_offset_degrees, 180);
    assert.equal(turned.tilt_offset_degrees, -45.5);
    const fixed = apply("rgb", 180);
    assert.equal(fixed.pan_offset_degrees, 0);
    assert.equal(fixed.tilt_offset_degrees, 0);
});
