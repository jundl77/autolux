"""Fixture library, patch editor and DMX universe map for the Config tab."""

import dash
from dash import ClientsideFunction, Input, Output, State, dcc, html

from autolux.lighting.fixture_setup import (
    DMX_UNIVERSE_SIZE,
    MAX_ARTNET_UNIVERSE,
    channel_occupancy,
    fixture_patch,
    fixture_rows,
)

MAP_COLUMNS = 32
MAX_ADD_QUANTITY = 64
FIXTURE_COLORS = ('#527eaa', '#8664ae', '#408e83', '#b38449', '#a75c80', '#667cb2')
FIXTURE_CSS = '''
#config-panel {padding: 18px 0;}
.patch-toolbar,.patch-heading {display:flex;align-items:center;justify-content:space-between;gap:16px;}
.patch-toolbar {margin-bottom:18px;}
.patch-toolbar h2 {margin:0;font-size:19px;}
.patch-toolbar p {margin:5px 0 0;color:#8293a6;font-size:12px;}
.patch-workspace {display:grid;grid-template-columns:230px minmax(480px,1fr);gap:16px;align-items:start;}
.patch-card {background:#121a24;border:1px solid #273345;border-radius:6px;padding:16px;min-width:0;}
.patch-card h3 {margin:0 0 14px;font-size:13px;font-weight:600;}
.patch-workspace.has-selection {grid-template-columns:230px minmax(480px,1fr) 300px;}
.patch-library {grid-column:1;grid-row:1;}
.patch-map-card {grid-column:2;grid-row:1;}
#fixture-info {grid-column:3;grid-row:1;}
.patch-universe-heading {display:flex;align-items:center;gap:14px;}
.patch-universe-heading h3,.patch-info-heading h3 {margin:0;}
.patch-info-heading {margin-bottom:14px;}
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
.patch-pair {display:grid;grid-template-columns:1fr 1fr;gap:10px;}
#config-panel .Select-control,#config-panel .Select-menu-outer {background:#0b121c;border-color:#344256;}
#config-panel .Select-value-label,#config-panel .Select-input input {color:#e6edf3!important;}
#config-panel .Select-option {background:#0b121c;color:#d5e0ec;}
#config-panel .Select-option.is-focused,#config-panel .Select-option.is-selected {background:#273b51;}
#config-panel button {background:#1e2c3d;color:#dce8f5;border:1px solid #3b4d62;border-radius:4px;padding:8px 12px;cursor:pointer;font-size:12px;}
#config-panel button:disabled {opacity:.4;cursor:default;}
#config-panel .patch-primary {background:#284d71;border-color:#4a7fab;}
.patch-actions {display:flex;gap:8px;}
.patch-muted,#fixture-status {font-size:12px;color:#93a5b9;line-height:1.6;}
#fixture-status {margin:12px 0;min-height:19px;}
.patch-profile-info {min-height:65px;margin:12px 0 18px;font-size:12px;color:#93a5b9;line-height:1.7;}
.patch-map {display:grid;grid-template-columns:repeat(var(--dmx-columns),minmax(0,1fr));grid-auto-rows:29px;gap:2px;margin-top:12px;}
.patch-channel {background:#192432;color:#65798f;font-size:9px;display:flex;align-items:center;justify-content:center;border-radius:2px;}
#config-panel .patch-block {padding:0 4px;border:0;border-radius:2px;color:#fff;font-size:10px;text-align:left;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;z-index:1;}
#config-panel .patch-block.selected {outline:2px solid #e2efff;outline-offset:-2px;z-index:2;}
#config-panel .patch-block.conflict {background:repeating-linear-gradient(135deg,#9b662e,#9b662e 6px,#5d4128 6px,#5d4128 12px)!important;}
.patch-conflicts {color:#e8b56d;font-size:12px;margin-top:10px;}
.patch-map-legend {display:flex;gap:20px;font-size:11px;color:#94a6b9;}
.patch-advanced {margin:12px 0;font-size:12px;color:#94a6b9;}
.patch-advanced summary {cursor:pointer;margin-bottom:12px;}
.patch-advanced label {font-size:11px;}
'''


def _profile_name(profile):
    return profile['name'].replace('_', ' ')


def _field(label, component):
    return html.Label([html.Span(label), component], className='patch-field')


def _number(identifier, value=None, minimum=1, maximum=DMX_UNIVERSE_SIZE):
    return dcc.Input(id=identifier, type='number', value=value, min=minimum, max=maximum, step=1)


def build_fixture_panel(app, store):
    patch, revision = store.load()
    profiles = store.profiles()
    profile_options = [
        {'label': f'{_profile_name(profile)} · {profile["dmx_mode"]}', 'value': identifier}
        for identifier, profile in profiles.items()
    ]
    first_profile = next(iter(profiles), None)
    rows = fixture_rows(patch)
    panel = html.Section(
        [
            html.Div(
                [
                    html.Div(
                        [
                            html.H2('Fixture setup'),
                            html.P('Patch fixtures, assign groups and inspect your DMX universes.'),
                        ]
                    ),
                    html.Div(
                        [
                            html.Button('Reload saved', id='fixture-reload'),
                            html.Button('Save patch', id='fixture-save', className='patch-primary'),
                        ],
                        className='patch-actions',
                    ),
                ],
                className='patch-toolbar',
            ),
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
                                'Start address · blank = first free', _number('fixture-add-address')
                            ),
                            html.Button(
                                'Add to patch', id='fixture-add', className='patch-primary'
                            ),
                            html.P(
                                'Only installed, validated fixture modes are listed.',
                                className='patch-muted',
                            ),
                        ],
                        className='patch-card patch-library',
                    ),
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.H3('Fixture details'),
                                    html.Button('Close', id='fixture-close'),
                                ],
                                className='patch-heading patch-info-heading',
                            ),
                            html.P(id='fixture-detail-summary', className='patch-muted'),
                            _field('Name', dcc.Input(id='fixture-name', type='text')),
                            _field(
                                'Fixture / mode',
                                dcc.Dropdown(
                                    id='fixture-profile', options=profile_options, clearable=False
                                ),
                            ),
                            html.Div(
                                [
                                    _field(
                                        'Universe',
                                        _number(
                                            'fixture-universe', maximum=MAX_ARTNET_UNIVERSE + 1
                                        ),
                                    ),
                                    _field('Start address', _number('fixture-address')),
                                ],
                                className='patch-pair',
                            ),
                            _field(
                                'Groups · comma separated',
                                dcc.Input(id='fixture-groups', type='text'),
                            ),
                            html.Details(
                                [
                                    html.Summary('Movement limits'),
                                    html.P('Normalized travel: 0–1.', className='patch-muted'),
                                    *[
                                        html.Div(
                                            [
                                                html.Div(
                                                    [
                                                        _field(
                                                            f'{axis.title()} min',
                                                            dcc.Input(
                                                                id=f'fixture-{axis}-min',
                                                                type='number',
                                                                min=0,
                                                                max=1,
                                                                step=0.01,
                                                            ),
                                                        ),
                                                        _field(
                                                            f'{axis.title()} max',
                                                            dcc.Input(
                                                                id=f'fixture-{axis}-max',
                                                                type='number',
                                                                min=0,
                                                                max=1,
                                                                step=0.01,
                                                            ),
                                                        ),
                                                    ],
                                                    className='patch-pair',
                                                ),
                                                dcc.Checklist(
                                                    id=f'fixture-{axis}-inverted',
                                                    options=[
                                                        {
                                                            'label': f' Invert {axis}',
                                                            'value': 'invert',
                                                        }
                                                    ],
                                                    value=[],
                                                ),
                                            ]
                                        )
                                        for axis in ('pan', 'tilt')
                                    ],
                                ],
                                className='patch-advanced',
                            ),
                            html.Div(
                                [
                                    html.Button('Apply edit', id='fixture-apply'),
                                    html.Button('Remove', id='fixture-remove'),
                                ],
                                className='patch-actions',
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
                                style={'--dmx-columns': MAP_COLUMNS},
                                **{
                                    'data-columns': MAP_COLUMNS,
                                    'data-channel-count': DMX_UNIVERSE_SIZE,
                                    'data-universe': 0,
                                },
                            ),
                            html.Div(id='fixture-conflicts', className='patch-conflicts'),
                            html.Div(
                                [
                                    html.Span('Colour = fixture footprint'),
                                    html.Span('Striped = address conflict'),
                                    html.Span('Drag to move · click for details'),
                                ],
                                className='patch-map-legend',
                            ),
                        ],
                        className='patch-card patch-map-card',
                    ),
                ],
                id='fixture-workspace',
                className='patch-workspace',
            ),
            html.P('Changes are saved for the next session.', id='fixture-status', role='status'),
            dcc.Store(id='fixture-draft', data=rows),
            dcc.Store(id='fixture-selection', data=[]),
            dcc.Store(id='fixture-grid-action'),
            dcc.Store(id='fixture-server-result'),
            dcc.Store(
                id='fixture-settings',
                data={
                    'profiles': profiles,
                    'channelCount': DMX_UNIVERSE_SIZE,
                    'maxUniverse': MAX_ARTNET_UNIVERSE,
                    'maxQuantity': MAX_ADD_QUANTITY,
                    'columns': MAP_COLUMNS,
                    'colors': FIXTURE_COLORS,
                },
            ),
            dcc.Store(id='fixture-patch', data=patch),
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

    edit_fields = [
        'name',
        'profile',
        'universe',
        'address',
        'groups',
        'pan-min',
        'pan-max',
        'pan-inverted',
        'tilt-min',
        'tilt-max',
        'tilt-inverted',
    ]

    app.clientside_callback(
        ClientsideFunction(namespace='fixture_patch', function_name='inspectFixture'),
        *[Output(f'fixture-{name}', 'value') for name in edit_fields],
        Output('fixture-apply', 'disabled'),
        Output('fixture-remove', 'disabled'),
        Output('fixture-info', 'style'),
        Output('fixture-workspace', 'className'),
        Output('fixture-detail-summary', 'children'),
        Input('fixture-selection', 'data'),
        Input('fixture-draft', 'data'),
        State('fixture-settings', 'data'),
    )
    app.clientside_callback(
        ClientsideFunction(namespace='fixture_patch', function_name='editPatch'),
        Output('fixture-draft', 'data'),
        Output('fixture-patch', 'data'),
        Output('fixture-revision', 'data'),
        Output('fixture-status', 'children'),
        Output('fixture-selection', 'data'),
        *[Input(f'fixture-{action}', 'n_clicks') for action in ('add', 'apply', 'remove', 'close')],
        Input('fixture-grid-action', 'data'),
        Input('fixture-server-result', 'data'),
        State('fixture-settings', 'data'),
        State('config-panel', 'aria-busy'),
        State('fixture-draft', 'data'),
        State('fixture-patch', 'data'),
        State('fixture-revision', 'data'),
        State('fixture-selection', 'data'),
        State('fixture-library-profile', 'value'),
        State('fixture-quantity', 'value'),
        State('fixture-add-universe', 'value'),
        State('fixture-add-address', 'value'),
        *[State(f'fixture-{name}', 'value') for name in edit_fields],
        prevent_initial_call=True,
    )

    @app.callback(
        Output('fixture-server-result', 'data'),
        Input('fixture-reload', 'n_clicks'),
        Input('fixture-save', 'n_clicks'),
        State('fixture-draft', 'data'),
        State('fixture-patch', 'data'),
        State('fixture-revision', 'data'),
        running=[(Output('config-panel', 'aria-busy'), 'true', 'false')],
        prevent_initial_call=True,
    )
    def persist_patch(_reload, _save, draft, previous, expected_revision):
        try:
            reloading = dash.ctx.triggered_id == 'fixture-reload'
            if reloading:
                saved, version = store.load()
            else:
                _, errors = channel_occupancy(draft, profiles, 0)
                if errors:
                    raise ValueError(errors[0])
                saved = fixture_patch(draft, previous)
                version = store.save(saved, expected_revision)
            return {
                'draft': fixture_rows(saved),
                'patch': saved,
                'revision': version,
                'reloaded': reloading,
            }
        except (KeyError, AttributeError, TypeError, ValueError, OSError, RuntimeError) as error:
            return {'error': str(error)}

    return panel
