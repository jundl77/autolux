from pathlib import Path

import dash
from dash import ClientsideFunction, Input, Output, State, dcc, html

from autolux.lib.engine.static_mode import HIGHLIGHT_COLOURS
from autolux.lighting import mvr, mvr_schema
from autolux.lighting.fixture_setup import DMX_UNIVERSE_SIZE, MAX_ARTNET_UNIVERSE

REPO_ROOT = Path(__file__).resolve().parents[2]
MAP_COLUMNS = 32
MAX_ADD_QUANTITY = 64
# The swatches are the colours the show accepts for a test, as the fixture emits them.
REPO_RIG_REPLACED = (
    'rig/room.mvr differed from the saved patch (a pull or checkout?) and Save replaced it; '
    'check git diff rig/ before committing.'
)
TEST_COLOURS = {
    name: '#{:02x}{:02x}{:02x}'.format(*(round(255 * c) for c in rgb))
    for name, rgb in HIGHLIGHT_COLOURS.items()
}
FIXTURE_COLORS = ('#527eaa', '#8664ae', '#408e83', '#b38449', '#a75c80', '#667cb2')
FIXTURE_CSS = '''
#config-panel {padding: 18px 0;}
.patch-toolbar,.patch-heading {display:flex;align-items:center;justify-content:space-between;gap:16px;}
.patch-toolbar {margin-bottom:18px;}
.patch-toolbar h2 {margin:0;font-size:19px;}
.patch-workspace {display:grid;grid-template-columns:230px minmax(480px,1fr);gap:16px;align-items:start;}
.patch-card {background:#121a24;border:1px solid #273345;border-radius:6px;padding:16px;min-width:0;}
.patch-card h3 {margin:0 0 14px;font-size:13px;font-weight:600;}
.patch-workspace.has-selection {grid-template-columns:230px minmax(480px,1fr) 380px;}
.patch-library {grid-column:1;grid-row:1;}
/* Positioning leads: the plan comes first and big, the DMX map below it. */
.mvr-card {grid-column:2;grid-row:1;}
.patch-map-card {grid-column:2;grid-row:2;}
#fixture-info {grid-column:3;grid-row:1 / span 2;align-self:stretch;contain:size;overflow:auto;padding:20px;}
.patch-universe-heading {display:flex;align-items:center;gap:14px;}
.patch-universe-heading h3,.patch-info-heading h3 {margin:0;}
.patch-info-heading {margin-bottom:16px;}
.cfg-title {flex:1;min-width:0;}
#config-panel .cfg-title input {box-sizing:border-box;width:100%;background:transparent;color:var(--text);border:1px solid transparent;border-radius:4px;padding:3px 6px;margin-left:-7px;font:inherit;font-size:16px;font-weight:600;}
#config-panel .cfg-title input:hover,#config-panel .cfg-title input:focus {border-color:var(--line);}
.cfg-title h3 {margin:0;font-size:16px;}
.cfg-empty {margin:0;color:var(--muted);font-size:13px;}
/* Nothing selected: one line, no actions to aim at nothing. */
#fixture-info:has(.cfg-empty) :is(#fixture-close,.cfg-footer) {display:none;}
#config-panel[aria-busy="true"] {cursor:progress;}
.patch-blocks {display:contents;}
.patch-drag-label {position:fixed;z-index:1000;pointer-events:none;background:#152333;color:#e6edf3;border:1px solid #64b8a4;border-radius:4px;padding:6px 10px;font:12px Segoe UI;}
.patch-drag-label.invalid {border-color:#e69478;color:#ffbd9f;}
.patch-channel.drop-valid {background:#3a907d;outline:1px solid #a0ebd2;color:white;z-index:3;}
.patch-channel.drop-invalid {background:#984c38;outline:1px solid #f8ad92;color:white;z-index:3;}
#config-panel .patch-block.dragging {opacity:.35;}
#config-panel .patch-block {cursor:grab;user-select:none;touch-action:none;}
#config-panel .patch-block:active {cursor:grabbing;}
.patch-field {display:block;margin:0 0 12px;min-width:0;}
.patch-field>span {display:block;color:#94a6b9;font-size:11px;margin-bottom:5px;}
.patch-field input {box-sizing:border-box;width:100%;background:#0b121c;color:#e6edf3;border:1px solid #344256;border-radius:4px;padding:8px;font:inherit;font-size:12px;}
#config-panel .mvr-editor input,#config-panel .mvr-editor select {box-sizing:border-box;width:100%;background:#0b121c;color:#e6edf3;border:1px solid #344256;border-radius:4px;padding:6px 8px;font:inherit;font-size:12px;}
.patch-pair {display:grid;grid-template-columns:1fr 1fr;gap:10px;}
#config-panel button {background:#1e2c3d;color:#dce8f5;border:1px solid #3b4d62;border-radius:4px;padding:8px 12px;cursor:pointer;font-size:12px;}
#config-panel button:disabled {opacity:.4;cursor:default;}
#config-panel .patch-primary {background:#284d71;border-color:#4a7fab;}
.patch-actions {display:flex;flex-wrap:wrap;justify-content:flex-end;align-items:center;gap:8px;}
.patch-muted,#fixture-status {font-size:12px;color:#93a5b9;line-height:1.6;}
#fixture-status {margin:0;max-width:360px;text-align:right;}
.patch-profile-info {margin:8px 0 16px;font-size:12px;color:#93a5b9;line-height:1.7;}
.patch-map {display:grid;grid-template-columns:repeat(var(--dmx-columns),minmax(0,1fr));grid-auto-rows:29px;gap:2px;margin-top:12px;}
.patch-channel {background:#192432;color:#65798f;font-size:9px;display:flex;align-items:center;justify-content:center;border-radius:2px;}
#config-panel .patch-block {padding:0 4px;border:0;border-radius:2px;color:#fff;font-size:10px;text-align:left;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;z-index:1;}
#config-panel .patch-block.selected {outline:2px solid #e2efff;outline-offset:-2px;z-index:2;}
#config-panel .patch-block.conflict {background:repeating-linear-gradient(135deg,#9b662e,#9b662e 6px,#5d4128 6px,#5d4128 12px)!important;}
.patch-conflicts {color:#e8b56d;font-size:12px;margin-top:10px;}
#mvr-plan svg.plan {display:block;width:100%;height:max(480px,55vh);background:#0b121c;border:1px solid #273345;border-radius:4px;}
#mvr-plan .mvr-object {cursor:pointer;}
#mvr-plan .mvr-object:has(circle) {cursor:grab;}
#mvr-plan svg.plan {touch-action:none;user-select:none;}
#mvr-plan .plan circle {stroke:#e6edf3;}
#mvr-plan .plan circle.ring {stroke:var(--ring);}
.plan-key .plan-role {fill:var(--muted);stroke:var(--muted);}
.plan-key .plan-role.ring {fill:none;stroke-width:2;stroke-dasharray:2,2;}
#mvr-plan .plan-object {fill:#455468;stroke:#6d7f95;}
#mvr-plan .plan-object.hollow {fill:none;}
#mvr-plan .mvr-object.selected circle,#mvr-plan .mvr-object.selected rect {stroke:#ffffff;}
#mvr-plan .plan .plan-name {stroke:#0b121c;stroke-linejoin:round;paint-order:stroke;}
#mvr-plan .plan-pan {fill:none;opacity:.2;pointer-events:none;}
#mvr-plan .mvr-object.selected .plan-pan {opacity:.9;}
#mvr-plan .plan-areas {stroke:#3b4d62;}
#mvr-plan .plan-area-name {fill:var(--muted);}
#mvr-plan .plan-stage-edge {stroke:#6d7f95;}
#mvr-plan .plan-scale line {stroke:#a9bbcf;}
#mvr-plan text {fill:#a9bbcf;pointer-events:none;}
.plan-legend {display:flex;flex-wrap:wrap;gap:4px 12px;margin-bottom:6px;font-size:11px;color:#94a6b9;}
.plan-legend b {font-weight:400;color:var(--text);}
.plan-key {display:inline-flex;align-items:center;gap:4px;}
.plan-key circle {stroke:#e6edf3;stroke-width:1.2;}
@media (max-width:700px) {#mvr-plan svg.plan {height:260px;}
.patch-workspace,.patch-workspace.has-selection {grid-template-columns:minmax(0,1fr);}
.patch-library,.patch-map-card,.mvr-card,#fixture-info {grid-column:1;grid-row:auto;contain:none;}}
.mvr-editor .mvr-link {cursor:pointer;color:#c9d7e6;padding:2px 4px;border-radius:3px;white-space:nowrap;}
.mvr-editor .mvr-link:hover {background:#1b2938;}
.mvr-editor .mvr-tag {color:#7fa7cf;}
.mvr-editor {font-size:12px;}
.mvr-editor h4 {margin:16px 0 8px;font-size:12px;color:#c9d7e6;font-weight:600;}
.mvr-editor .mvr-row {display:grid;grid-template-columns:124px minmax(0,1fr) auto;gap:6px;align-items:center;margin-bottom:6px;}
.mvr-editor .mvr-row label {color:var(--muted);font-size:12px;overflow:hidden;text-overflow:ellipsis;}
.mvr-editor .mvr-helper {display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin:0 0 8px;}
.mvr-editor .mvr-adders {display:flex;flex-wrap:wrap;gap:4px;margin:6px 0;}
#config-panel .mvr-editor button.mvr-small {padding:2px 7px;font-size:11px;}
.mvr-editor details details {margin-left:10px;}
.mvr-editor summary {cursor:pointer;color:#c9d7e6;}
.mvr-editor .mvr-gdtf-attribute {color:#93a5b9;margin-left:14px;}
.mvr-editor .mvr-readonly {color:#93a5b9;}
.cfg-section {border-top:1px solid var(--line);margin-top:20px;padding-top:16px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px 12px;}
.cfg-section>h4 {grid-column:1/-1;display:flex;justify-content:space-between;margin:0;font-size:12px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--text);}
.cfg-section>h4 small {color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0;}
.cfg-field {display:grid;gap:6px;align-content:start;min-width:0;}
.cfg-field.wide {grid-column:1 / -1;}
/* A full-width row of two fields at 2:1, not the section's equal halves. */
.cfg-field-pair {grid-column:1 / -1;display:grid;grid-template-columns:2fr 1fr;gap:14px 12px;}
.cfg-field>span,.cfg-split span {color:var(--muted);font-size:12px;}
.cfg-field>span small {float:right;}
.cfg-split+span {margin-top:8px;}
#config-panel .cfg-section input,#config-panel .cfg-section select {box-sizing:border-box;width:100%;min-width:0;background:#0b121c;color:var(--text);border:1px solid var(--line);border-radius:4px;padding:7px 9px;font:inherit;font-size:12px;}
/* Spinners on hover only would make one field look unlike its neighbours. */
#config-panel .cfg-section input[type="number"] {appearance:textfield;}
#config-panel .cfg-section input::-webkit-inner-spin-button,#config-panel .cfg-section input::-webkit-outer-spin-button {-webkit-appearance:none;margin:0;}
#config-panel .cfg-section input[readonly] {color:var(--muted);border-style:dashed;}
#config-panel [aria-invalid="true"],#config-panel [aria-invalid="true"] input {border-color:#e69478;}
.cfg-note {color:var(--muted);font-size:12px;line-height:1.4;}
.cfg-note.invalid {color:#ffbd9f;}
.cfg-note:empty {display:none;}
.cfg-split {display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;}
.cfg-split.cfg-2 {grid-template-columns:repeat(2,minmax(0,1fr));}
.cfg-split label {display:grid;gap:4px;min-width:0;}
.cfg-group {display:grid;gap:6px;}
.cfg-check {display:flex;align-items:center;gap:8px;color:var(--text);font-size:12px;}
#config-panel .cfg-section .cfg-check input {width:auto;margin:0;accent-color:var(--accent);}
.cfg-test {display:flex;flex-wrap:wrap;align-items:center;gap:6px;}
.cfg-test .cfg-step {margin-left:auto;}
#config-panel .cfg-test button,#config-panel .cfg-cells button {padding:6px 10px;}
#config-panel .cfg-test .cfg-swatch {width:26px;height:26px;padding:0;background:var(--swatch);border-color:#3b4d62;}
#config-panel .cfg-test button.active {border-color:var(--accent);box-shadow:0 0 0 1px var(--accent);}
#config-panel .cfg-test .cfg-test-main.active {background:#284d71;color:#fff;}
.cfg-test-note {margin:8px 0 0;}
.cfg-cells {grid-column:1 / -1;display:flex;flex-wrap:wrap;gap:4px;}
#config-panel .cfg-cells button.selected {border-color:var(--accent);background:#273b51;color:#fff;}
.cfg-advanced {border-top:1px solid var(--line);margin-top:20px;padding-top:14px;}
.cfg-advanced>summary {cursor:pointer;color:var(--muted);font-size:12px;}
.cfg-footer {margin-top:20px;display:flex;justify-content:flex-end;}
@media (max-width:700px) {.cfg-section {grid-template-columns:minmax(0,1fr);}}
'''


def _field(label, component, title=None):
    return html.Label([html.Span(label), component], className='patch-field', title=title)


def _number(identifier, value=None, minimum=1, maximum=DMX_UNIVERSE_SIZE, placeholder=None):
    return dcc.Input(
        id=identifier,
        type='number',
        value=value,
        min=minimum,
        max=maximum,
        step=1,
        placeholder=placeholder,
    )


def browser_library(library: dict) -> dict:
    return {
        spec: {
            'name': entry['name'],
            'manufacturer': entry['manufacturer'],
            'default_mode': entry['default_mode'],
            'modes': entry['modes'],
            'tree': mvr.to_tree(mvr.gdtf_root(entry['gdtf_xml'], spec), f'lib{index}-'),
        }
        for index, (spec, entry) in enumerate(library.items())
    }


def library_folder(path: Path) -> str:
    try:
        return Path(path).resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def build_fixture_panel(app, store):
    draft, revision = store.load()
    library = browser_library(store.library())
    profile_options = [
        {
            'label': f'{entry["manufacturer"]} {entry["name"]} · {entry["default_mode"]}',
            'value': spec,
        }
        for spec, entry in library.items()
    ]
    first_profile = next(iter(library), None)
    panel = html.Section(
        [
            html.Div(
                [
                    html.H2('Fixture setup'),
                    html.Div(
                        [
                            # The dirty state sits where the operator saves.
                            html.P(id='fixture-status', role='status'),
                            html.Button('Reload saved', id='fixture-reload'),
                            html.Button('Save patch', id='fixture-save', className='patch-primary'),
                            html.Button('Restart show', id='fixture-apply', disabled=True),
                        ],
                        className='patch-actions',
                    ),
                ],
                className='patch-toolbar',
            ),
            html.P(id='fixture-show-rig', className='patch-conflicts'),
            html.Div(
                [
                    html.Div(
                        [
                            html.H3('Fixture library'),
                            _field(
                                'Fixture / mode',
                                dcc.Dropdown(
                                    id='fixture-library-profile',
                                    options=profile_options,
                                    value=first_profile,
                                    clearable=False,
                                ),
                                title=f'GDTF archives in {library_folder(store.library_path)}',
                            ),
                            html.Div(id='fixture-profile-info', className='patch-profile-info'),
                            html.Div(
                                [
                                    _field(
                                        'Quantity',
                                        _number('fixture-quantity', 1, maximum=MAX_ADD_QUANTITY),
                                    ),
                                    _field(
                                        'Universe',
                                        _number(
                                            'fixture-add-universe',
                                            1,
                                            maximum=MAX_ARTNET_UNIVERSE + 1,
                                        ),
                                    ),
                                ],
                                className='patch-pair',
                            ),
                            _field(
                                'Start address',
                                _number('fixture-add-address', placeholder='first free'),
                            ),
                            html.Button(
                                'Add to patch', id='fixture-add', className='patch-primary'
                            ),
                        ],
                        className='patch-card patch-library',
                    ),
                    html.Div(
                        [
                            html.Div(
                                [
                                    # fixture_patch.js draws the name here, as its edit field.
                                    html.Div(id='fixture-title', className='cfg-title'),
                                    html.Button('Close', id='fixture-close'),
                                ],
                                className='patch-heading patch-info-heading',
                            ),
                            html.Div(id='mvr-node-editor', className='mvr-editor'),
                            html.Div(
                                [html.Button('Remove fixture', id='fixture-remove')],
                                className='cfg-footer',
                            ),
                        ],
                        id='fixture-info',
                        className='patch-card',
                        style={'display': 'none'},
                    ),
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.H3('MVR rig'),
                                    html.Span(id='mvr-summary', className='patch-muted'),
                                ],
                                className='patch-heading',
                            ),
                            html.Div(id='mvr-plan'),
                        ],
                        className='patch-card mvr-card',
                    ),
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.Div(
                                        [
                                            html.H3('DMX universe'),
                                            dcc.Dropdown(
                                                id='fixture-map-universe',
                                                options=[{'label': 'Universe 1', 'value': 0}],
                                                value=0,
                                                clearable=False,
                                                style={'width': '170px'},
                                            ),
                                        ],
                                        className='patch-universe-heading',
                                    ),
                                    html.Span(id='fixture-map-summary', className='patch-muted'),
                                ],
                                className='patch-heading',
                            ),
                            html.Div(
                                [
                                    *[
                                        html.Div(
                                            str(index + 1),
                                            className='patch-channel',
                                            **{'data-channel': index + 1},
                                            style={
                                                'gridRow': index // MAP_COLUMNS + 1,
                                                'gridColumn': index % MAP_COLUMNS + 1,
                                            },
                                        )
                                        for index in range(DMX_UNIVERSE_SIZE)
                                    ],
                                    html.Div(id='fixture-blocks', className='patch-blocks'),
                                ],
                                id='fixture-channel-map',
                                className='patch-map',
                                title='Drag to move, click to edit',
                                style={'--dmx-columns': MAP_COLUMNS},
                                **{
                                    'data-columns': MAP_COLUMNS,
                                    'data-channel-count': DMX_UNIVERSE_SIZE,
                                    'data-universe': 0,
                                },
                            ),
                            html.Div(id='fixture-conflicts', className='patch-conflicts'),
                        ],
                        className='patch-card patch-map-card',
                    ),
                ],
                id='fixture-workspace',
                className='patch-workspace',
            ),
            dcc.Store(id='fixture-draft', data=draft),
            dcc.Store(id='fixture-selection', data=None),
            dcc.Store(id='fixture-grid-action'),
            dcc.Store(id='fixture-server-result'),
            dcc.Store(
                id='fixture-settings',
                data={
                    'library': library,
                    'schema': mvr_schema.describe(),
                    'channelCount': DMX_UNIVERSE_SIZE,
                    'maxUniverse': MAX_ARTNET_UNIVERSE,
                    'maxQuantity': MAX_ADD_QUANTITY,
                    'columns': MAP_COLUMNS,
                    'colors': FIXTURE_COLORS,
                    'accentRole': mvr_schema.ACCENT_ROLE,
                    'roles': mvr_schema.ROLES,
                    'testColours': TEST_COLOURS,
                },
            ),
            dcc.Store(id='fixture-revision', data=revision),
        ],
        id='config-panel',
        **{'aria-busy': 'false'},
        style={'display': 'none'},
    )

    app.clientside_callback(
        ClientsideFunction(namespace='fixture_patch', function_name='describeProfile'),
        Output('fixture-profile-info', 'children'),
        Input('fixture-library-profile', 'value'),
        State('fixture-settings', 'data'),
    )
    app.clientside_callback(
        ClientsideFunction(namespace='fixture_patch', function_name='renderPatch'),
        Output('fixture-blocks', 'children'),
        Output('fixture-map-summary', 'children'),
        Output('fixture-conflicts', 'children'),
        Output('fixture-save', 'disabled'),
        Output('fixture-map-universe', 'options'),
        Output('fixture-channel-map', 'data-universe'),
        Input('fixture-draft', 'data'),
        Input('fixture-map-universe', 'value'),
        Input('fixture-selection', 'data'),
        Input('config-panel', 'aria-busy'),
        State('fixture-settings', 'data'),
    )
    app.clientside_callback(
        ClientsideFunction(namespace='fixture_patch', function_name='renderRig'),
        Output('fixture-info', 'style'),
        Output('fixture-workspace', 'className'),
        Output('fixture-remove', 'disabled'),
        Output('mvr-summary', 'children'),
        Input('fixture-selection', 'data'),
        Input('fixture-draft', 'data'),
        State('fixture-settings', 'data'),
    )
    # The Control tab's poll also serves the Config tab: the test controls and the Restart banner
    # follow the show's state from it.
    app.clientside_callback(
        ClientsideFunction(namespace='fixture_patch', function_name='syncTest'),
        Output('fixture-show-rig', 'children'),
        Output('fixture-apply', 'disabled'),
        Input('control-state', 'data'),
        Input('dashboard-tab', 'value'),
        Input('fixture-revision', 'data'),
    )
    app.clientside_callback(
        ClientsideFunction(namespace='fixture_patch', function_name='editPatch'),
        Output('fixture-draft', 'data'),
        Output('fixture-revision', 'data'),
        Output('fixture-status', 'children'),
        Output('fixture-selection', 'data'),
        *[Input(f'fixture-{action}', 'n_clicks') for action in ('add', 'remove', 'close')],
        Input('fixture-grid-action', 'data'),
        Input('fixture-server-result', 'data'),
        State('fixture-settings', 'data'),
        State('config-panel', 'aria-busy'),
        State('fixture-draft', 'data'),
        State('fixture-revision', 'data'),
        State('fixture-selection', 'data'),
        State('fixture-library-profile', 'value'),
        State('fixture-quantity', 'value'),
        State('fixture-add-universe', 'value'),
        State('fixture-add-address', 'value'),
        prevent_initial_call=True,
    )

    @app.callback(
        Output('fixture-server-result', 'data'),
        Input('fixture-reload', 'n_clicks'),
        Input('fixture-save', 'n_clicks'),
        State('fixture-draft', 'data'),
        State('fixture-revision', 'data'),
        running=[(Output('config-panel', 'aria-busy'), 'true', 'false')],
        prevent_initial_call=True,
    )
    def persist_patch(_reload, _save, edited, expected_revision):
        try:
            reloading = dash.ctx.triggered_id == 'fixture-reload'
            diverged = not reloading and store.repo_rig_diverged()
            saved, version = store.load() if reloading else store.save(edited, expected_revision)
            result = {'draft': saved, 'revision': version, 'reloaded': reloading}
            if diverged:
                result['warning'] = REPO_RIG_REPLACED
            return result
        except (KeyError, AttributeError, TypeError, ValueError, OSError, RuntimeError) as error:
            return {'error': str(error)}

    return panel
