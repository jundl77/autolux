(() => {
    const ds = (window.dash_clientside = window.dash_clientside || {});
    const DRAG_THRESHOLD_PX = 4;
    const LABEL_OFFSET_PX = 16;
    let drag = null;
    let suppressedClick = null;
    let requestId = 0;

    // Draft edits stay in this browser. Only Save and Reload contact the server.
    function occupancy(rows, settings, universe) {
        const occupied = Array.from({ length: settings.channelCount }, () => []);
        const errors = [],
            names = new Set(),
            addresses = new Map();
        rows.forEach((row, index) => {
            const name = row.id || `Fixture ${index + 1}`;
            const count = settings.profiles[row.profile]?.channels;
            if (typeof row.id !== "string" || !row.id.trim() || names.has(row.id)) {
                errors.push(`${name}: fixture names must be nonempty and unique.`);
            }
            names.add(row.id);
            if (
                !Number.isInteger(row.universe) ||
                row.universe < 0 ||
                row.universe > settings.maxUniverse
            ) {
                errors.push(`${name}: select a valid universe.`);
                return;
            }
            if (
                !Number.isInteger(row.address) ||
                row.address < 1 ||
                row.address > settings.channelCount
            ) {
                errors.push(
                    `${name}: DMX address must be an integer from 1 to ${settings.channelCount}.`
                );
                return;
            }
            if (!Number.isInteger(count) || count < 1 || count > settings.channelCount) {
                errors.push(`${name}: select a valid fixture profile.`);
                return;
            }
            if (row.address + count - 1 > settings.channelCount) {
                errors.push(`${name}: ${count} channels do not fit at address ${row.address}.`);
                return;
            }
            const conflicts = new Set();
            for (let channel = row.address; channel < row.address + count; channel++) {
                const key = `${row.universe}:${channel}`;
                if (addresses.has(key)) conflicts.add(addresses.get(key));
                addresses.set(key, index);
                if (row.universe === universe) occupied[channel - 1].push(index);
            }
            conflicts.forEach((other) =>
                errors.push(`${name} overlaps ${rows[other].id} in universe ${row.universe + 1}.`)
            );
        });
        return { occupied, errors };
    }

    function requireValid(rows, settings) {
        const { errors } = occupancy(rows, settings, 0);
        if (errors.length) throw new Error(errors[0]);
    }

    function firstFreeAddress(rows, settings, universe, count) {
        const { occupied, errors } = occupancy(rows, settings, universe);
        if (errors.length) throw new Error(errors[0]);
        let free = 0;
        for (let index = 0; index < occupied.length; index++) {
            free = occupied[index].length ? 0 : free + 1;
            if (free === count) return index - count + 2;
        }
        throw new Error(`No room for ${count} consecutive channels in universe ${universe + 1}.`);
    }

    function component(type, props) {
        return { namespace: "dash_html_components", type, props };
    }

    ds.fixture_patch = {
        describeProfile(identifier, settings) {
            const profile = settings.profiles[identifier];
            return profile
                ? `${profile.channels} DMX channels · ${profile.dmx_mode}`
                : "No fixture mode selected.";
        },

        renderPatch(rows, universe, selected, busy, settings) {
            finish();
            const { occupied, errors } = occupancy(rows, settings, universe);
            const blocks = [];
            const owners = [...new Set(occupied.flat())];
            owners.forEach((index) => {
                const row = rows[index];
                const start = row.address - 1,
                    count = settings.profiles[row.profile].channels;
                const end = start + count;
                const title = `${row.id} · ${start + 1}–${end}`;
                const conflict = occupied.slice(start, end).some((owners) => owners.length > 1);
                let segment = start,
                    part = 0;
                while (segment < end) {
                    const stop = Math.min(
                        end,
                        (Math.floor(segment / settings.columns) + 1) * settings.columns
                    );
                    blocks.push(
                        component("Button", {
                            key: `${row.id}:${part++}`,
                            children: title,
                            title,
                            "data-fixture-id": row.id,
                            "data-start-address": row.address,
                            "data-channel-count": count,
                            "data-universe": universe,
                            className:
                                "patch-block" +
                                (selected[0] === index ? " selected" : "") +
                                (conflict ? " conflict" : ""),
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
            const universes = [...new Set([0, universe, ...rows.map((row) => row.universe)])].sort(
                (a, b) => a - b
            );
            return [
                blocks,
                `${occupied.filter((owners) => owners.length).length} / ${settings.channelCount} channels patched`,
                errors.map((error) => component("Div", { children: error })),
                Boolean(errors.length) || busy === "true",
                universes.map((number) => ({ label: `Universe ${number + 1}`, value: number })),
                universe
            ];
        },

        inspectFixture(selected, rows, settings) {
            const row = rows[selected[0]];
            if (!row)
                return [
                    "",
                    null,
                    1,
                    1,
                    "",
                    0,
                    1,
                    [],
                    0,
                    1,
                    [],
                    true,
                    true,
                    { display: "none" },
                    "patch-workspace",
                    ""
                ];
            const values = [row.id, row.profile, row.universe + 1, row.address, row.groups || ""];
            for (const axis of ["pan", "tilt"]) {
                values.push(
                    row[`${axis}_min`],
                    row[`${axis}_max`],
                    row[`${axis}_inverted`] ? ["invert"] : []
                );
            }
            const count = settings.profiles[row.profile].channels;
            return [
                ...values,
                false,
                false,
                { display: "block" },
                "patch-workspace has-selection",
                `${count} channels · ${row.address}–${row.address + count - 1}`
            ];
        },

        editPatch(
            _add,
            _apply,
            _remove,
            _close,
            action,
            response,
            settings,
            busy,
            draft,
            previous,
            revision,
            selected,
            profile,
            quantity,
            universe,
            address,
            ...values
        ) {
            const unchanged = [
                ds.no_update,
                ds.no_update,
                ds.no_update,
                ds.no_update,
                ds.no_update
            ];
            // Dash can batch an edit queued during a save with the save response.
            // Persistence owns that interval: accept its result before any queued edit.
            const persisted = ds.callback_context.triggered?.some(
                (input) => input.prop_id === "fixture-server-result.data"
            );
            const trigger = persisted ? "fixture-server-result" : ds.callback_context.triggered_id;
            if (trigger === "fixture-server-result") {
                if (!response) return unchanged;
                if (response.error)
                    return [
                        ds.no_update,
                        ds.no_update,
                        ds.no_update,
                        `Patch not saved: ${response.error}`,
                        ds.no_update
                    ];
                return [
                    response.draft,
                    response.patch,
                    response.revision,
                    response.reloaded
                        ? "Saved patch loaded."
                        : "Patch saved. It will be used by the next session.",
                    response.reloaded ? [] : selected
                ];
            }
            if (busy === "true") return unchanged;
            if (trigger === "fixture-close") return [...unchanged.slice(0, 4), []];
            const rows = draft.map((row) => ({ ...row }));
            try {
                if (trigger === "fixture-grid-action") {
                    if (!action) return unchanged;
                    const matches = rows.flatMap((row, index) =>
                        row.id === action.fixture_id ? [index] : []
                    );
                    if (matches.length !== 1)
                        throw new Error("The fixture changed. Select it again.");
                    if (action.action === "select") return [...unchanged.slice(0, 4), matches];
                    if (action.action !== "move") throw new Error("Unknown fixture action.");
                    const row = rows[matches[0]];
                    if (
                        row.address !== action.original_address ||
                        row.universe !== action.original_universe
                    ) {
                        throw new Error("The fixture moved while dragging. Try again.");
                    }
                    row.address = action.address;
                    row.universe = action.universe;
                    if (selected.length) selected = matches;
                } else if (trigger === "fixture-add") {
                    if (
                        !Number.isInteger(quantity) ||
                        quantity < 1 ||
                        quantity > settings.maxQuantity
                    ) {
                        throw new Error(`Quantity must be from 1 to ${settings.maxQuantity}.`);
                    }
                    if (
                        !Number.isInteger(universe) ||
                        universe < 1 ||
                        universe > settings.maxUniverse + 1
                    ) {
                        throw new Error("Choose a valid universe.");
                    }
                    const count = settings.profiles[profile]?.channels;
                    if (!count) throw new Error("Select a valid fixture profile.");
                    selected = [rows.length];
                    for (let i = 0; i < quantity; i++) {
                        const start =
                            address == null
                                ? firstFreeAddress(rows, settings, universe - 1, count)
                                : address;
                        let suffix = 1;
                        while (rows.some((row) => row.id === `${profile}_${suffix}`)) suffix++;
                        rows.push({
                            id: `${profile}_${suffix}`,
                            profile,
                            universe: universe - 1,
                            address: start,
                            groups: "",
                            pan_min: 0,
                            pan_max: 1,
                            pan_inverted: false,
                            tilt_min: 0,
                            tilt_max: 1,
                            tilt_inverted: false
                        });
                        if (address != null) address += count;
                    }
                } else if (trigger === "fixture-apply" || trigger === "fixture-remove") {
                    const row = rows[selected[0]];
                    if (!row) throw new Error("Select a fixture first.");
                    if (trigger === "fixture-remove") {
                        rows.splice(selected[0], 1);
                        selected = [];
                    } else {
                        const [name, profile, universe, address, groups, ...limits] = values;
                        if (!Number.isInteger(universe))
                            throw new Error("Universe must be a whole number.");
                        Object.assign(row, {
                            id: (name || "").trim(),
                            profile,
                            universe: universe - 1,
                            address,
                            groups
                        });
                        ["pan", "tilt"].forEach((axis, index) => {
                            const [low, high, inverted] = limits.slice(index * 3, index * 3 + 3);
                            if (
                                !Number.isFinite(low) ||
                                !Number.isFinite(high) ||
                                !(0 <= low && low <= high && high <= 1)
                            ) {
                                throw new Error(`${axis} limits must satisfy 0 ≤ min ≤ max ≤ 1.`);
                            }
                            Object.assign(row, {
                                [`${axis}_min`]: low,
                                [`${axis}_max`]: high,
                                [`${axis}_inverted`]: inverted.includes("invert")
                            });
                        });
                    }
                } else return unchanged;
                requireValid(rows, settings);
                return [
                    rows,
                    ds.no_update,
                    ds.no_update,
                    "Unsaved changes · save the patch when finished.",
                    selected
                ];
            } catch (error) {
                return [
                    ds.no_update,
                    ds.no_update,
                    ds.no_update,
                    `Patch unchanged: ${error.message}`,
                    ds.no_update
                ];
            }
        }
    };

    function sendAction(action) {
        window.dash_clientside.set_props("fixture-grid-action", {
            data: { ...action, request_id: ++requestId }
        });
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
        const { grid, fixtureId, offset, channelCount, label, cells, ranges, geometry } = drag;
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
            start >= 1 &&
            end <= geometry.count &&
            !ranges.some(([otherStart, otherEnd]) => start <= otherEnd && end >= otherStart);
        for (
            let address = Math.max(1, start);
            address <= Math.min(geometry.count, end);
            address++
        ) {
            cells[address - 1].classList.add(valid ? "drop-valid" : "drop-invalid");
        }
        label.classList.toggle("invalid", !valid);
        label.textContent = `${fixtureId} · ${start}–${end}${valid ? "" : " · unavailable"}`;
        return start;
    }

    function finish() {
        if (!drag) return;
        const current = drag;
        drag = null;
        cancelAnimationFrame(current.frame);
        clearPreview(current.grid);
        current.grid
            .querySelectorAll(".dragging")
            .forEach((block) => block.classList.remove("dragging"));
        current.label?.remove();
        if (current.moved) {
            suppressedClick = current.block;
            // Ignore only the click generated by this pointer release, never a later keyboard click.
            setTimeout(() => {
                if (suppressedClick === current.block) suppressedClick = null;
            });
        }
        if (current.grid.hasPointerCapture(current.pointerId)) {
            current.grid.releasePointerCapture(current.pointerId);
        }
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
            fixtureId: block.dataset.fixtureId,
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
                .filter((other) => other.dataset.fixtureId !== block.dataset.fixtureId)
                .map((other) => [
                    Number(other.dataset.startAddress),
                    Number(other.dataset.startAddress) + Number(other.dataset.channelCount) - 1
                ])
        };
    });

    document.addEventListener("pointermove", (event) => {
        if (!drag || drag.pointerId !== event.pointerId) return;
        if (!drag.moved) {
            if (Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < DRAG_THRESHOLD_PX)
                return;
            drag.moved = true;
            drag.grid.setPointerCapture(event.pointerId);
            drag.label = document.createElement("div");
            drag.label.className = "patch-drag-label";
            document.body.appendChild(drag.label);
            drag.grid.querySelectorAll(".patch-block").forEach((block) => {
                if (block.dataset.fixtureId === drag.fixtureId) block.classList.add("dragging");
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
            fixture_id: current.fixtureId,
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
            const block = event.target.closest("#fixture-channel-map .patch-block");
            if (!block) return;
            if (suppressedClick === block) {
                suppressedClick = null;
                event.preventDefault();
                event.stopImmediatePropagation();
                return;
            }
            if (!block.closest('[aria-busy="true"]')) {
                sendAction({ action: "select", fixture_id: block.dataset.fixtureId });
            }
        },
        true
    );

    document.addEventListener("pointercancel", finish);
    document.addEventListener("lostpointercapture", (event) => {
        if (drag?.pointerId === event.pointerId) finish();
    });
    document.addEventListener("keydown", (event) => {
        if (event.target.closest('#config-panel[aria-busy="true"]')) event.preventDefault();
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
