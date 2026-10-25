"""Fixture library, patch editor and DMX universe map for the Config tab."""

import dash
from dash import ALL, Input, Output, State, dash_table, dcc, html

from autolux.lighting.fixture_setup import (
    DMX_UNIVERSE_SIZE,
    MAX_ARTNET_UNIVERSE,
    channel_occupancy,
    fixture_patch,
    fixture_rows,
    next_free_address,
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
.patch-workspace {display:grid;grid-template-columns:250px minmax(480px,1fr) 285px;gap:16px;align-items:start;}
.patch-card {background:#121a24;border:1px solid #273345;border-radius:6px;padding:16px;min-width:0;}
.patch-card h3 {margin:0 0 14px;font-size:13px;font-weight:600;}
.patch-library {grid-row:span 2;}
.patch-map-card {grid-column:2 / 4;}
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
.patch-map {display:grid;grid-template-columns:repeat(32,minmax(0,1fr));grid-auto-rows:23px;gap:2px;margin-top:12px;}
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


def _channel_map(rows, occupied, selected):
    cells = [
        html.Div(
            str(index + 1),
            className='patch-channel',
            style={'gridRow': index // MAP_COLUMNS + 1, 'gridColumn': index % MAP_COLUMNS + 1},
        )
        for index in range(DMX_UNIVERSE_SIZE)
    ]
    for row_index in sorted({owner for owners in occupied for owner in owners}):
        channels = [index for index, owners in enumerate(occupied) if row_index in owners]
        start, end = channels[0], channels[-1]
        conflict = any(len(occupied[channel]) > 1 for channel in channels)
        name = rows[row_index]['id']
        title = f'{name} · {start + 1}–{end + 1}'
        segment = start
        while segment <= end:
            stop = min(end + 1, (segment // MAP_COLUMNS + 1) * MAP_COLUMNS)
            cells.append(
                html.Button(
                    title,
                    id={'type': 'fixture-map-block', 'row': row_index, 'segment': segment},
                    title=title,
                    className='patch-block'
                    + (' selected' if selected == row_index else '')
                    + (' conflict' if conflict else ''),
                    style={
                        'gridRow': segment // MAP_COLUMNS + 1,
                        'gridColumn': f'{segment % MAP_COLUMNS + 1} / span {stop - segment}',
                        'background': FIXTURE_COLORS[row_index % len(FIXTURE_COLORS)],
                    },
                    n_clicks=0,
                )
            )
            segment = stop
    return cells


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
                                'Manufacturer',
                                dcc.Dropdown(
                                    id='fixture-manufacturer',
                                    clearable=False,
                                    value='*',
                                    options=[{'label': 'All manufacturers', 'value': '*'}]
                                    + [
                                        {'label': name, 'value': name}
                                        for name in sorted(
                                            {
                                                profile['manufacturer']
                                                for profile in profiles.values()
                                            }
                                        )
                                    ],
                                ),
                            ),
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
                                    html.H3('Patched fixtures'),
                                    html.Span(id='fixture-count', className='patch-muted'),
                                ],
                                className='patch-heading',
                            ),
                            dash_table.DataTable(
                                id='fixture-table',
                                data=[],
                                row_selectable='single',
                                selected_rows=[0] if rows else [],
                                columns=[
                                    {'name': label, 'id': key}
                                    for label, key in (
                                        ('Fixture', 'name'),
                                        ('Mode', 'mode'),
                                        ('Universe', 'universe'),
                                        ('DMX address', 'address'),
                                        ('Groups', 'groups'),
                                    )
                                ],
                                style_table={
                                    'height': '314px',
                                    'overflowY': 'auto',
                                    'overflowX': 'auto',
                                },
                                style_cell={
                                    'backgroundColor': '#121a24',
                                    'color': '#dce8f5',
                                    'borderColor': '#273345',
                                    'fontFamily': 'Segoe UI',
                                    'fontSize': '12px',
                                    'padding': '10px',
                                    'textAlign': 'left',
                                },
                                style_header={'backgroundColor': '#0b121c', 'fontWeight': '600'},
                                style_data_conditional=[
                                    {
                                        'if': {'state': 'selected'},
                                        'backgroundColor': '#23394f',
                                        'border': '1px solid #527eaa',
                                    }
                                ],
                            ),
                        ],
                        className='patch-card',
                    ),
                    html.Div(
                        [
                            html.H3('Selected fixture'),
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
                        className='patch-card',
                    ),
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
                                    html.Span(id='fixture-map-summary', className='patch-muted'),
                                ],
                                className='patch-heading',
                            ),
                            html.Div(id='fixture-channel-map', className='patch-map'),
                            html.Div(id='fixture-conflicts', className='patch-conflicts'),
                            html.Div(
                                [
                                    html.Span('Colour = fixture footprint'),
                                    html.Span('Striped = address conflict'),
                                    html.Span('Click a fixture to edit'),
                                ],
                                className='patch-map-legend',
                            ),
                        ],
                        className='patch-card patch-map-card',
                    ),
                ],
                className='patch-workspace',
            ),
            html.P('Changes are saved for the next session.', id='fixture-status', role='status'),
            dcc.Store(id='fixture-draft', data=rows),
            dcc.Store(id='fixture-patch', data=patch),
            dcc.Store(id='fixture-revision', data=revision),
        ],
        id='config-panel',
        style={'display': 'none'},
    )

    @app.callback(
        Output('fixture-library-profile', 'options'),
        Output('fixture-library-profile', 'value'),
        Input('fixture-manufacturer', 'value'),
    )
    def filter_library(manufacturer):
        options = [
            option
            for option in profile_options
            if manufacturer == '*' or profiles[option['value']]['manufacturer'] == manufacturer
        ]
        return options, options[0]['value'] if options else None

    @app.callback(
        Output('fixture-profile-info', 'children'), Input('fixture-library-profile', 'value')
    )
    def describe_profile(identifier):
        profile = profiles.get(identifier)
        return (
            [
                html.Strong(profile['manufacturer']),
                html.Br(),
                f'{profile["channels"]} DMX channels · {profile["dmx_mode"]}',
            ]
            if profile
            else 'No fixture mode selected.'
        )

    @app.callback(
        Output('fixture-table', 'data'),
        Output('fixture-channel-map', 'children'),
        Output('fixture-map-summary', 'children'),
        Output('fixture-conflicts', 'children'),
        Output('fixture-save', 'disabled'),
        Output('fixture-count', 'children'),
        Output('fixture-map-universe', 'options'),
        Input('fixture-draft', 'data'),
        Input('fixture-map-universe', 'value'),
        Input('fixture-table', 'selected_rows'),
    )
    def render_patch(draft, universe, selected):
        occupied, errors = channel_occupancy(draft, profiles, universe)
        table = []
        for row in draft:
            profile = profiles.get(row['profile'], {})
            count = profile.get('channels', 0)
            table.append(
                {
                    'name': row['id'],
                    'mode': profile.get('dmx_mode', '?'),
                    'universe': row['universe'] + 1,
                    'address': f'{row["address"]}–{row["address"] + count - 1}',
                    'groups': row.get('groups', ''),
                }
            )
        universes = sorted({universe, 0, *(row['universe'] for row in draft)})
        used = sum(bool(owners) for owners in occupied)
        return (
            table,
            _channel_map(draft, occupied, selected[0] if selected else None),
            f'{used} / {DMX_UNIVERSE_SIZE} channels patched',
            [html.Div(error) for error in errors],
            bool(errors),
            f'{len(draft)} fixtures',
            [{'label': f'Universe {number + 1}', 'value': number} for number in universes],
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

    @app.callback(
        *[Output(f'fixture-{name}', 'value') for name in edit_fields],
        Output('fixture-apply', 'disabled'),
        Output('fixture-remove', 'disabled'),
        Input('fixture-table', 'selected_rows'),
        Input('fixture-draft', 'data'),
    )
    def inspect_fixture(selected, draft):
        if not selected or selected[0] >= len(draft):
            return '', None, 1, 1, '', 0, 1, [], 0, 1, [], True, True
        row = draft[selected[0]]
        values = [
            row['id'],
            row['profile'],
            row['universe'] + 1,
            row['address'],
            row.get('groups', ''),
        ]
        for axis in ('pan', 'tilt'):
            values.extend(
                [
                    row[f'{axis}_min'],
                    row[f'{axis}_max'],
                    ['invert'] if row[f'{axis}_inverted'] else [],
                ]
            )
        return *values, False, False

    @app.callback(
        Output('fixture-draft', 'data'),
        Output('fixture-patch', 'data'),
        Output('fixture-revision', 'data'),
        Output('fixture-status', 'children'),
        Output('fixture-table', 'selected_rows'),
        *[
            Input(f'fixture-{action}', 'n_clicks')
            for action in ('add', 'apply', 'remove', 'reload', 'save')
        ],
        Input({'type': 'fixture-map-block', 'row': ALL, 'segment': ALL}, 'n_clicks'),
        State('fixture-draft', 'data'),
        State('fixture-patch', 'data'),
        State('fixture-revision', 'data'),
        State('fixture-table', 'selected_rows'),
        State('fixture-library-profile', 'value'),
        State('fixture-quantity', 'value'),
        State('fixture-add-universe', 'value'),
        State('fixture-add-address', 'value'),
        *[State(f'fixture-{name}', 'value') for name in edit_fields],
        prevent_initial_call=True,
    )
    def edit_patch(
        _add,
        _apply,
        _remove,
        _reload,
        _save,
        map_clicks,
        draft,
        previous,
        expected_revision,
        selected,
        profile,
        quantity,
        universe,
        address,
        *values,
    ):
        unchanged = (dash.no_update, dash.no_update, dash.no_update)
        action = dash.ctx.triggered_id
        try:
            if isinstance(action, dict):
                if not any(map_clicks):
                    raise dash.exceptions.PreventUpdate
                return *unchanged, dash.no_update, [action['row']]
            if action == 'fixture-reload':
                saved, version = store.load()
                return (
                    fixture_rows(saved),
                    saved,
                    version,
                    'Saved patch loaded.',
                    [0] if saved['fixtures'] else [],
                )
            if action == 'fixture-save':
                _, errors = channel_occupancy(draft, profiles, 0)
                if errors:
                    raise ValueError(errors[0])
                saved = fixture_patch(draft, previous)
                version = store.save(saved, expected_revision)
                return (
                    fixture_rows(saved),
                    saved,
                    version,
                    'Patch saved. It will be used by the next session.',
                    selected,
                )
            draft = [dict(row) for row in draft]
            if action == 'fixture-add':
                if type(quantity) is not int or not 1 <= quantity <= MAX_ADD_QUANTITY:
                    raise ValueError(f'Quantity must be from 1 to {MAX_ADD_QUANTITY}.')
                if type(universe) is not int or not 1 <= universe <= MAX_ARTNET_UNIVERSE + 1:
                    raise ValueError('Choose a valid universe.')
                count = profiles[profile]['channels']
                selected = [len(draft)]
                for _ in range(quantity):
                    start = (
                        next_free_address(draft, profiles, universe - 1, count)
                        if address is None
                        else address
                    )
                    suffix = 1
                    while f'{profile}_{suffix}' in {row['id'] for row in draft}:
                        suffix += 1
                    row = fixture_rows(
                        {
                            'fixtures': [
                                {
                                    'id': f'{profile}_{suffix}',
                                    'profile': profile,
                                    'universe': universe - 1,
                                    'address': start,
                                }
                            ]
                        }
                    )[0]
                    row.pop('_original_id')
                    draft.append(row)
                    if address is not None:
                        address += count
            elif action in ('fixture-apply', 'fixture-remove'):
                if not selected or selected[0] >= len(draft):
                    raise ValueError('Select a fixture first.')
                index = selected[0]
                if action == 'fixture-remove':
                    draft.pop(index)
                    selected = [min(index, len(draft) - 1)] if draft else []
                else:
                    edits = dict(zip(edit_fields, values, strict=True))
                    if type(edits['universe']) is not int:
                        raise ValueError('Universe must be a whole number.')
                    row = draft[index]
                    row.update(
                        id=(edits['name'] or '').strip(),
                        profile=edits['profile'],
                        universe=edits['universe'] - 1,
                        address=edits['address'],
                        groups=edits['groups'],
                    )
                    for axis in ('pan', 'tilt'):
                        low, high = edits[f'{axis}-min'], edits[f'{axis}-max']
                        if (
                            not isinstance(low, (float, int))
                            or not isinstance(high, (float, int))
                            or not 0 <= low <= high <= 1
                        ):
                            raise ValueError(
                                f'{axis.title()} limits must satisfy 0 ≤ min ≤ max ≤ 1.'
                            )
                        row.update(
                            {
                                f'{axis}_min': low,
                                f'{axis}_max': high,
                                f'{axis}_inverted': bool(edits[f'{axis}-inverted']),
                            }
                        )
            _, errors = channel_occupancy(draft, profiles, 0)
            if errors:
                raise ValueError(errors[0])
            return (
                draft,
                dash.no_update,
                dash.no_update,
                'Unsaved changes · save the patch when finished.',
                selected,
            )
        except (KeyError, AttributeError, TypeError, ValueError, OSError, RuntimeError) as error:
            return *unchanged, f'Patch unchanged: {error}', dash.no_update

    return panel
