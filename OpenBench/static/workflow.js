// Graphical editor and live view of Workflows, see OpenBench/workflows/graph.py.
//
// The graph is edited in place as { defaults, nodes : { key : { kind, label, config, x, y } }, edges },
// and drawn as absolutely positioned <div>s for the Nodes, with an <svg> below them for the edges.
// In the editor, Nodes are dragged by their header, and connected by dragging from an output port
// onto another Node. The view draws the same graph read-only, coloured by the state of each Node.

const WF_PORTS = {
    SPRT : ['passed', 'failed', 'done'],
    SPSA : ['done'],
    JOIN : ['all', 'some', 'none'],
    IF   : ['true', 'false'],
};

const WF_PORT_COLORS = {
    passed : '#76D576', failed : '#FA9696', done : '#60F0FF',
    all    : '#76D576', some   : '#EFF891', none : '#FA9696',
    true   : '#76D576', false  : '#FA9696',
};

// Settings of each kind of Node: [ config field, label, placeholder ]. Empty fields use the defaults.
const WF_FIELDS = {
    SPRT : [
        ['dev',             'Dev',            'branch, sha, or @trigger.dev'],
        ['dev_bench',       'Dev Bench',      'Autofill'],
        ['dev_options',     'Dev Options',    'Overrides, e.g. Hash=16'],
        ['base',            'Base',           'branch, sha, or @trigger.base'],
        ['base_bench',      'Base Bench',     'Autofill'],
        ['base_options',    'Base Options',   'Overrides, e.g. Hash=16'],
        ['time_control',    'Time Control',   '@dev_time_control'],
        ['test_bounds',     'Bounds',         '@test_bounds'],
        ['test_confidence', 'Confidence',     '@test_confidence'],
        ['priority',        'Priority',       '@priority'],
        ['throughput',      'Throughput',     '@throughput'],
        ['existing_test',   'Existing Test',  'Id of a running SPRT to attach'],
    ],
    SPSA : [
        ['dev',                    'Branch',       'branch, sha, or @trigger.dev'],
        ['dev_bench',              'Bench',        'Autofill'],
        ['dev_options',            'Options',      'Overrides, e.g. Hash=16'],
        ['time_control',           'Time Control', '@dev_time_control'],
        ['spsa_inputs',            'SPSA Inputs',  'name, int, value, min, max, c_end, r_end'],
        ['spsa_iterations',        'Iterations',   ''],
        ['spsa_pairs_per',         'Pairs-Per',    ''],
        ['spsa_alpha',             'Alpha',        ''],
        ['spsa_gamma',             'Gamma',        ''],
        ['spsa_A_ratio',           'A-Ratio',      ''],
        ['spsa_reporting_type',    'Reporting',    'BATCHED or BULK'],
        ['spsa_distribution_type', 'Distribution', 'SINGLE or MULTIPLE'],
        ['at_percent',             'Done at %',    '100'],
        ['priority',               'Priority',     '@priority'],
        ['throughput',             'Throughput',   '@throughput'],
        ['existing_test',          'Existing Test','Id of a running SPSA to attach'],
    ],
    JOIN : [],
    IF   : [
        ['condition', 'Condition', 'failed && elo > 3'],
        ['source',    'Source',    'Key of an SPRT, empty for @trigger'],
    ],
};

const WF_SPSA_DEFAULTS = {
    spsa_iterations : '25000', spsa_pairs_per : '8', spsa_alpha : '0.602', spsa_gamma : '0.101',
    spsa_A_ratio : '0.1', spsa_reporting_type : 'BATCHED', spsa_distribution_type : 'SINGLE', at_percent : '100',
};

const WF_IF_HELP =
    'Decides on the result of an SPRT, and fires "true" or "false". Connect it to the "done" ' +
    'output of an SPRT, which fires on any outcome. Variables: passed, failed, elo, elo_lower, ' +
    'elo_upper (95% bounds), llr, games. Prefix them with a key to use another SPRT, like ' +
    'x.passed && y.failed, and the IF waits until those have finished. Operators: && || ! and ' +
    'or not, < <= > >= == !=, parentheses. For example: failed && elo > 3, or: passed || ' +
    'elo_lower > 0. Nodes after an IF may still use @trigger, which refers to the Node that ' +
    'started the IF.';

const WF_JOIN_HELP =
    'Waits until every incoming edge is decided. Fires "all" if every one of them fired, ' +
    '"some" if at least one did, and "none" otherwise. Edges from skipped Nodes are not ' +
    'counted, so "all" after two alternative branches means that the one which ran passed.';

const WF_REF_HELP =
    'Engines are a branch, tag or sha, resolved once the Node starts, or a reference to the exact ' +
    'engine another Node tested: @trigger.dev, @trigger.base, @trigger.tuned (SPSA values), or ' +
    '@<key>.dev, @<key>.base, @<key>.tuned. Options of a reference are inherited, and the Options ' +
    'field only adds overrides.';

let wf = {
    graph     : { defaults : {}, nodes : {}, edges : [] },
    editable  : false,
    locked    : new Set(), // Nodes of a running Workflow that have started, and can no longer change
    state     : null,  // Live state of each Node, only in the view
    selected  : null,  // { node : key } or { edge : index }
    canvas    : null,
    svg       : null,
    panel     : null,
    drag      : null,  // Node being moved, or edge being drawn
    on_change : null,
};

// # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
//                                   HELPERS
// # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

function wf_el(tag, cls, text) {
    const element = document.createElement(tag);
    if (cls)              element.className   = cls;
    if (text !== undefined) element.textContent = text;
    return element;
}

function wf_label(key) {
    const node = wf.graph.nodes[key];
    return (node && node.label) || key;
}

function wf_value(config, field) {
    const value = config[field];
    return value === undefined || value === null ? '' : String(value).trim();
}

function wf_default_value(field) {

    // Placeholders like '@dev_time_control' show the current default of that field
    const element = document.getElementById(field);
    if (element && element.classList.contains('wf-default'))
        return element.value;
    return (wf.graph.defaults || {})[field] || '';
}

function wf_placeholder(placeholder) {
    if (!placeholder.startsWith('@'))
        return placeholder;
    const value = wf_default_value(placeholder.slice(1));
    return value ? 'Default: ' + value : '';
}

function wf_next_key() {
    let index = 1;
    while (('n' + index) in wf.graph.nodes) index++;
    return 'n' + index;
}

function wf_incoming(key) {
    return wf.graph.edges.filter(edge => edge.to == key);
}

function wf_condition_names(condition) {

    // Keys named as key.variable, which the IF waits for, and whether plain variables use its own SPRT
    return {
        named : [...new Set([...condition.matchAll(/([A-Za-z0-9_-]{1,32})\.[A-Za-z_]+/g)].map(match => match[1]))],
        plain : /(^|[^A-Za-z0-9_.-])(passed|failed|elo|elo_lower|elo_upper|llr|games)\b/.test(condition),
    };
}

// # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
//                                   DRAWING
// # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

function wf_node_summary(key) {

    const node   = wf.graph.nodes[key];
    const config = node.config || {};
    const lines  = [];

    if (node.kind == 'JOIN')
        lines.push('Waits for all inputs');

    else if (node.kind == 'IF') {

        // Names like x.passed are SPRTs the IF waits for, plain names use its own SPRT
        const condition        = wf_value(config, 'condition');
        const { named, plain } = wf_condition_names(condition);

        lines.push(condition || '?');
        if (plain || !named.length) lines.push('on ' + (wf_value(config, 'source') || '@trigger'));
        if (named.length) lines.push('waits for ' + named.join(', '));
    }

    else if (config.existing_test)
        lines.push('Attaches Test #' + config.existing_test);

    else if (node.kind == 'SPRT') {
        lines.push((wf_value(config, 'dev') || '?') + ' vs ' + (wf_value(config, 'base') || '?'));
        lines.push((wf_value(config, 'time_control') || wf_default_value('dev_time_control'))
            + ' ' + (wf_value(config, 'test_bounds') || wf_default_value('test_bounds')));
    }

    else if (node.kind == 'SPSA') {
        const count = wf_value(config, 'spsa_inputs').split('\n').filter(line => line.trim()).length;
        lines.push(wf_value(config, 'dev') || '?');
        lines.push(count + ' params, ' + (wf_value(config, 'spsa_iterations') || '?') + ' iterations');
    }

    return lines;
}

function wf_render_node(key) {

    const node    = wf.graph.nodes[key];
    const live    = wf.state && wf.state.nodes[key];
    const element = wf_el('div', 'wf-node wf-kind-' + node.kind);

    element.dataset.key = key;
    element.style.left  = node.x + 'px';
    element.style.top   = node.y + 'px';

    if (live)
        element.classList.add('wf-state-' + live.state);

    if (wf.locked.has(key))
        element.classList.add('wf-locked');

    if (wf.selected && wf.selected.node == key)
        element.classList.add('wf-selected');

    const head = wf_el('div', 'wf-node-head');
    head.append(wf_el('span', 'wf-node-label', wf_label(key)));
    // The kind shows in the colour of the header, so the label gets the room. The key stays, since references use it
    const kind = wf_el('span', 'wf-node-kind', key);
    kind.title = node.kind + ' · ' + key;
    head.append(kind);
    element.append(head);

    const body = wf_el('div', 'wf-node-body');
    for (const line of wf_node_summary(key))
        body.append(wf_el('div', '', line));

    if (live) {

        let text = live.state + (live.outcome ? ': ' + live.outcome : '');
        if (live.test && live.state == 'RUNNING' && !live.test.approved)
            text += ' (awaiting approval)';
        body.append(wf_el('div', 'wf-node-state', text));

        if (live.test) {
            const link = wf_el('a', '', '#' + live.test.id + ' · ' + live.test.games + ' games · ' + live.test.summary);
            link.href = live.test.url;
            link.addEventListener('mousedown', event => event.stopPropagation());
            body.append(link);

            if (live.test.elo)
                body.append(wf_el('div', 'wf-node-elo', live.test.elo));
        }

        if (live.detail)
            body.append(wf_el('div', 'wf-node-detail', live.detail));

        if (live.error)
            body.append(wf_el('div', 'wf-node-error', live.error));
    }

    element.append(body);

    if (node.kind != 'SPRT' && node.kind != 'SPSA' || !(node.config || {}).existing_test)
        element.append(wf_el('div', 'wf-port-in'));

    const ports = wf_el('div', 'wf-ports-out');
    for (const port of WF_PORTS[node.kind]) {
        const row = wf_el('div', 'wf-port-out');
        row.dataset.port = port;
        row.append(wf_el('span', '', port));
        const dot = wf_el('i', 'wf-dot');
        dot.style.background = WF_PORT_COLORS[port];
        row.append(dot);
        ports.append(row);
    }
    element.append(ports);

    return element;
}

function wf_port_point(key, port) {

    // Centre of an output dot, or of the input of a Node, relative to the canvas
    const node = wf.canvas.querySelector('.wf-node[data-key="' + key + '"]');
    if (!node) return null;

    const target = port ? node.querySelector('.wf-port-out[data-port="' + port + '"] .wf-dot')
                        : node.querySelector('.wf-port-in') || node;
    const box    = target.getBoundingClientRect();
    const origin = wf.canvas.getBoundingClientRect();

    if (!port && !node.querySelector('.wf-port-in'))
        return { x : box.left - origin.left, y : box.top - origin.top + 20 };

    return { x : box.left - origin.left + box.width / 2, y : box.top - origin.top + box.height / 2 };
}

function wf_curve(a, b) {
    const dx = Math.max(40, Math.abs(b.x - a.x) / 2);
    return 'M ' + a.x + ' ' + a.y + ' C ' + (a.x + dx) + ' ' + a.y + ', ' + (b.x - dx) + ' ' + b.y + ', ' + b.x + ' ' + b.y;
}

function wf_edge_fired(edge) {

    // In the view: true if fired, false if not taken or skipped, null if undecided
    const live = wf.state && wf.state.nodes[edge.from];
    if (!live) return null;

    // An SPRT fires 'done' with either outcome, as ALWAYS in graph.py
    const always = wf.graph.nodes[edge.from].kind == 'SPRT' && edge.port == 'done';
    if (live.state == 'FINISHED') return live.outcome == edge.port || always;
    return live.state == 'SKIPPED' ? false : null;
}

function wf_render_edges() {

    while (wf.svg.firstChild)
        wf.svg.removeChild(wf.svg.firstChild);

    wf.graph.edges.forEach((edge, index) => {

        const a = wf_port_point(edge.from, edge.port);
        const b = wf_port_point(edge.to, null);
        if (!a || !b) return;

        const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        path.setAttribute('d', wf_curve(a, b));
        path.setAttribute('class', 'wf-edge');
        path.style.stroke = WF_PORT_COLORS[edge.port];

        const fired = wf_edge_fired(edge);
        if (fired === false) path.classList.add('wf-edge-dead');
        if (fired === true)  path.classList.add('wf-edge-fired');

        if (wf.selected && wf.selected.edge === index)
            path.classList.add('wf-edge-selected');

        // A wider, invisible path on top, so that edges are easy to click
        const hit = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        hit.setAttribute('d', wf_curve(a, b));
        hit.setAttribute('class', 'wf-edge-hit');
        hit.addEventListener('mousedown', event => {
            event.stopPropagation();
            wf_select({ edge : index });
        });

        wf.svg.append(path);
        wf.svg.append(hit);
    });

    wf_render_waits();

    if (wf.drag && wf.drag.type == 'connect') {
        const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        path.setAttribute('d', wf_curve(wf.drag.start, wf.drag.end));
        path.setAttribute('class', 'wf-edge wf-edge-drawing');
        path.style.stroke = WF_PORT_COLORS[wf.drag.port];
        wf.svg.append(path);
    }
}

function wf_render_waits() {

    // Dotted lines from the 'done' output of every SPRT that an IF names in its condition.
    // Only drawn, never stored: they follow the condition, and cannot be selected or removed.
    for (const key in wf.graph.nodes) {

        const node = wf.graph.nodes[key];
        if (node.kind != 'IF')
            continue;

        for (const source of wf_condition_names(wf_value(node.config || {}, 'condition')).named) {

            if (!wf.graph.nodes[source] || wf.graph.nodes[source].kind != 'SPRT')
                continue;

            if (wf.graph.edges.some(edge => edge.from == source && edge.port == 'done' && edge.to == key))
                continue; // Already connected for real

            const a = wf_port_point(source, 'done');
            const b = wf_port_point(key, null);
            if (!a || !b) continue;

            const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
            path.setAttribute('d', wf_curve(a, b));
            path.setAttribute('class', 'wf-edge wf-edge-wait');
            path.style.stroke = WF_PORT_COLORS.done;

            // In the view, a wait is met once the SPRT has finished
            const live = wf.state && wf.state.nodes[source];
            if (live && live.state == 'FINISHED')
                path.classList.add('wf-edge-wait-met');

            const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
            title.textContent = wf_label(key) + ' waits for ' + wf_label(source);
            path.append(title);

            wf.svg.append(path);
        }
    }
}

function wf_resize_canvas() {

    let width = 1200, height = 520;
    for (const key in wf.graph.nodes) {
        width  = Math.max(width,  wf.graph.nodes[key].x + 460);
        height = Math.max(height, wf.graph.nodes[key].y + 320);
    }

    wf.canvas.style.width  = width  + 'px';
    wf.canvas.style.height = height + 'px';
    wf.svg.setAttribute('width',  width);
    wf.svg.setAttribute('height', height);
}

function wf_render() {

    for (const element of Array.from(wf.canvas.querySelectorAll('.wf-node')))
        element.remove();

    for (const key in wf.graph.nodes)
        wf.canvas.append(wf_render_node(key));

    wf_resize_canvas();
    wf_render_edges();
}

// # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
//                               SIDE PANEL
// # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

function wf_select(selection) {
    wf.selected = selection;
    wf_render();
    wf_render_panel();
}

function wf_panel_row(label, input) {
    const row = wf_el('div', 'wf-panel-row');
    row.append(wf_el('label', '', label));
    row.append(input);
    return row;
}

function wf_ref_suggestions(key) {

    const list = document.getElementById('wf-refs');
    while (list.firstChild) list.removeChild(list.firstChild);

    const values = ['@trigger.dev', '@trigger.base', '@trigger.tuned'];
    for (const other in wf.graph.nodes) {
        const kind = wf.graph.nodes[other].kind;
        if (other == key || kind == 'JOIN' || kind == 'IF') continue;
        values.push('@' + other + '.dev');
        values.push(kind == 'SPRT' ? '@' + other + '.base' : '@' + other + '.tuned');
    }

    for (const value of values) {
        const option = document.createElement('option');
        option.value = value;
        list.append(option);
    }
}

function wf_render_panel() {

    const panel = wf.panel;
    while (panel.firstChild) panel.removeChild(panel.firstChild);

    if (!wf.selected) {
        panel.append(wf_el('h3', '', wf.editable ? 'Workflow Editor' : 'Workflow'));
        panel.append(wf_el('p', 'wf-help', wf.editable
            ? 'Add Nodes with the buttons above. Drag a Node by its header, and drag from an output on its right onto another Node to connect them. Click a Node or an edge to edit or remove it.'
            : 'Click a Node to see its details.'));
        panel.append(wf_el('p', 'wf-help', WF_REF_HELP));
        return;
    }

    if (wf.selected.edge !== undefined) {

        const edge = wf.graph.edges[wf.selected.edge];
        panel.append(wf_el('h3', '', 'Edge'));
        panel.append(wf_el('p', '', wf_label(edge.from) + ' --' + edge.port + '--> ' + wf_label(edge.to)));

        if (wf.editable && !wf.locked.has(edge.to)) {
            const button = wf_el('button', 'anchorbutton btn-red w-100', 'Remove Edge');
            button.onclick = () => wf_delete_selection();
            panel.append(button);
        }
        return;
    }

    const key  = wf.selected.node;
    const node = wf.graph.nodes[key];

    panel.append(wf_el('h3', '', node.kind + ' Node'));

    if (wf.editable && wf.locked.has(key))
        wf_render_locked_editor(panel, key, node);
    else if (wf.editable)
        wf_render_node_editor(panel, key, node);
    else
        wf_render_node_details(panel, key, node);
}

function wf_render_locked_editor(panel, key, node) {

    // Started Nodes keep their settings and incoming edges, but may be renamed and get new successors
    panel.append(wf_el('p', 'wf-help',
        'This Node has already started, so only its label can change. New edges may still leave it.'));

    const label_input = wf_el('input');
    label_input.value = node.label || '';
    label_input.addEventListener('input', () => { node.label = label_input.value; wf_render(); });
    panel.append(wf_panel_row('Label', label_input));

    wf_render_node_details(panel, key, node);
}

function wf_render_node_editor(panel, key, node) {

    const config = node.config = node.config || {};

    const key_input = wf_el('input');
    key_input.value = key;
    key_input.addEventListener('change', () => wf_rename(key, key_input.value.trim()));
    panel.append(wf_panel_row('Key', key_input));

    const label_input = wf_el('input');
    label_input.value = node.label || '';
    label_input.addEventListener('input', () => { node.label = label_input.value; wf_render(); });
    panel.append(wf_panel_row('Label', label_input));

    if (node.kind == 'JOIN')
        panel.append(wf_el('p', 'wf-help', WF_JOIN_HELP));

    if (node.kind == 'IF')
        panel.append(wf_el('p', 'wf-help', WF_IF_HELP));

    wf_ref_suggestions(key);

    for (const [field, label, placeholder] of WF_FIELDS[node.kind]) {

        const input = field == 'spsa_inputs' ? wf_el('textarea') : wf_el('input');
        input.value       = wf_value(config, field);
        input.placeholder = wf_placeholder(placeholder);

        if (field == 'spsa_inputs') input.rows = 8;
        if (field == 'dev' || field == 'base') input.setAttribute('list', 'wf-refs');

        input.addEventListener('input', () => {
            config[field] = input.value;
            wf_render();
        });

        panel.append(wf_panel_row(label, input));
    }

    const button = wf_el('button', 'anchorbutton btn-red w-100 mt-2', 'Remove Node');
    button.onclick = () => wf_delete_selection();
    panel.append(button);
}

function wf_render_node_details(panel, key, node) {

    const live   = wf.state.nodes[key] || {};
    const config = node.config || {};
    const table  = wf_el('table', 'wf-details');

    function row(label, value) {
        if (value === '' || value === undefined || value === null) return;
        const tr = wf_el('tr');
        tr.append(wf_el('td', 'td-label', label));
        tr.append(wf_el('td', '', String(value)));
        table.append(tr);
    }

    row('Key', key);
    if (!wf.editable) // The editor shows it as an input already
        row('Label', node.label);
    row('State', live.state + (live.outcome ? ': ' + live.outcome : ''));
    row('Started by', live.trigger ? wf_label(live.trigger) : '');
    row('Error', live.error);

    for (const [field, label] of WF_FIELDS[node.kind])
        if (field != 'spsa_inputs')
            row(label, wf_value(config, field));

    row('Elo', live.test && live.test.elo);
    row('Decided on', live.detail);
    row('Tuned', live.tuned);
    panel.append(table);

    if (config.spsa_inputs) {
        panel.append(wf_el('h4', '', 'SPSA Inputs'));
        panel.append(wf_el('pre', 'wf-pre', config.spsa_inputs));
    }

    if (live.test) {
        const link = wf_el('a', 'anchorbutton btn-blue w-100 mt-2', 'Open Test #' + live.test.id);
        link.href = live.test.url;
        panel.append(link);
    }

    if (!wf.can_modify || ['FINISHED', 'ABORTED'].includes(wf.state.status))
        return;

    if (live.state == 'ERROR')
        panel.append(wf_action_form('retry', key, null, 'Retry', 'btn-yellow'));

    if (live.state == 'ERROR' || live.state == 'STOPPED') {
        panel.append(wf_el('p', 'wf-help', 'Restart the Test to continue, or decide this Node by hand:'));
        for (const port of live.ports)
            panel.append(wf_action_form('resolve', key, port, 'Decide as ' + port, 'btn-blue'));
    }
}

function wf_action_form(action, key, outcome, text, cls) {

    const form = wf_el('form');
    form.method = 'POST';
    form.action = '/workflow/' + wf.workflow_id + '/' + action + '/';

    for (const [name, value] of [['csrfmiddlewaretoken', wf.csrf], ['key', key], ['outcome', outcome || '']]) {
        const input = wf_el('input');
        input.type  = 'hidden';
        input.name  = name;
        input.value = value;
        form.append(input);
    }

    const button = wf_el('button', 'anchorbutton w-100 mt-1 ' + cls, text);
    button.type  = 'submit';
    form.append(button);
    return form;
}

// # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
//                                   EDITING
// # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

function wf_add_node(kind) {

    const key   = wf_next_key();
    const count = Object.keys(wf.graph.nodes).length;
    const scroll = wf.canvas.parentElement;

    const config = kind == 'SPSA' ? Object.assign({ dev : '' }, WF_SPSA_DEFAULTS)
                 : kind == 'SPRT' ? { dev : '', base : wf_default_value('base_branch') || 'main' }
                 : kind == 'IF'   ? { condition : 'failed && elo > 3', source : '' } : {};

    wf.graph.nodes[key] = {
        kind   : kind,
        label  : kind + ' ' + key.slice(1),
        config : config,
        x      : scroll.scrollLeft + 40 + 30 * (count % 6),
        y      : scroll.scrollTop  + 40 + 30 * (count % 6),
    };

    wf_select({ node : key });
}

function wf_rename(old_key, new_key) {

    if (new_key == old_key)
        return;

    if (!/^[A-Za-z0-9_-]{1,32}$/.test(new_key) || new_key in wf.graph.nodes) {
        alert('Keys must be unique, and consist of at most 32 letters, digits, "-" or "_"');
        return wf_render_panel();
    }

    // Move the Node, and repoint its edges and every reference to it
    wf.graph.nodes[new_key] = wf.graph.nodes[old_key];
    delete wf.graph.nodes[old_key];

    for (const edge of wf.graph.edges) {
        if (edge.from == old_key) edge.from = new_key;
        if (edge.to   == old_key) edge.to   = new_key;
    }

    for (const key in wf.graph.nodes) {
        const config = wf.graph.nodes[key].config || {};
        for (const field of ['dev', 'base'])
            if (typeof config[field] == 'string')
                config[field] = config[field].replace(new RegExp('^@' + old_key + '\\.'), '@' + new_key + '.');

        // Conditions of IF Nodes name SPRTs as key.variable, and may name one as their source
        if (typeof config.condition == 'string')
            config.condition = config.condition.replace(new RegExp('(^|[^A-Za-z0-9_-])' + old_key + '\\.', 'g'), '$1' + new_key + '.');
        if (config.source == old_key)
            config.source = new_key;
    }

    wf_select({ node : new_key });
}

function wf_delete_selection() {

    if (!wf.editable || !wf.selected)
        return;

    if (wf.selected.edge !== undefined) {
        if (wf.locked.has(wf.graph.edges[wf.selected.edge].to))
            return alert('The target of this edge has already started, so the edge has to stay');
        wf.graph.edges.splice(wf.selected.edge, 1);
    }

    else {
        const key = wf.selected.node;
        if (wf.locked.has(key))
            return alert('This Node has already started, and cannot be removed');
        delete wf.graph.nodes[key];
        wf.graph.edges = wf.graph.edges.filter(edge => edge.from != key && edge.to != key);
    }

    wf_select(null);
}

function wf_connect(from, port, to) {

    if (from == to || !(to in wf.graph.nodes))
        return;

    if (wf.locked.has(to))
        return alert('This Node has already started, so it cannot get new incoming edges');

    if (wf.graph.edges.some(edge => edge.from == from && edge.port == port && edge.to == to))
        return;

    wf.graph.edges.push({ from : from, port : port, to : to });

    // Most Nodes continue with the engine that got them started
    const target = wf.graph.nodes[to];
    const config = target.config = target.config || {};
    if (['SPRT', 'SPSA'].includes(target.kind) && wf.graph.nodes[from].kind != 'JOIN' && !wf_value(config, 'dev'))
        config.dev = '@trigger.dev';

    wf_select({ node : to });
}

function wf_canvas_point(event) {
    const origin = wf.canvas.getBoundingClientRect();
    return { x : event.clientX - origin.left, y : event.clientY - origin.top };
}

function wf_mousedown(event) {

    const node_element = event.target.closest('.wf-node');
    const port_element = event.target.closest('.wf-port-out');

    if (!node_element) {
        if (event.target == wf.canvas || event.target == wf.svg)
            wf_select(null);
        return;
    }

    const key = node_element.dataset.key;

    if (port_element && wf.editable) {
        event.preventDefault();
        const start = wf_port_point(key, port_element.dataset.port);
        wf.drag = { type : 'connect', from : key, port : port_element.dataset.port, start : start, end : start };
        return;
    }

    if (event.target.closest('.wf-node-head') && wf.editable) {
        event.preventDefault();
        const point = wf_canvas_point(event);
        const node  = wf.graph.nodes[key];
        wf.drag = { type : 'move', key : key, dx : point.x - node.x, dy : point.y - node.y };
    }

    if (!wf.selected || wf.selected.node != key)
        wf_select({ node : key });
}

function wf_mousemove(event) {

    if (!wf.drag)
        return;

    const point = wf_canvas_point(event);

    if (wf.drag.type == 'move') {
        const node = wf.graph.nodes[wf.drag.key];
        node.x = Math.max(0, Math.round((point.x - wf.drag.dx) / 10) * 10);
        node.y = Math.max(0, Math.round((point.y - wf.drag.dy) / 10) * 10);
        const element = wf.canvas.querySelector('.wf-node[data-key="' + wf.drag.key + '"]');
        element.style.left = node.x + 'px';
        element.style.top  = node.y + 'px';
        wf_render_edges();
    }

    if (wf.drag.type == 'connect') {
        wf.drag.end = point;
        wf_render_edges();
    }
}

function wf_mouseup(event) {

    if (!wf.drag)
        return;

    const drag = wf.drag;
    wf.drag = null;

    if (drag.type == 'connect') {
        const target = document.elementFromPoint(event.clientX, event.clientY);
        const node   = target && target.closest('.wf-node');
        if (node) return wf_connect(drag.from, drag.port, node.dataset.key);
    }

    wf_render();
}

function wf_keydown(event) {
    const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName);
    if (!typing && (event.key == 'Delete' || event.key == 'Backspace') && wf.selected) {
        event.preventDefault();
        wf_delete_selection();
    }
}

function wf_attach(container) {

    wf.canvas = wf_el('div', 'wf-canvas');
    wf.svg    = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    wf.svg.setAttribute('class', 'wf-edges');
    wf.canvas.append(wf.svg);
    container.append(wf.canvas);

    wf.canvas.addEventListener('mousedown', wf_mousedown);
    document.addEventListener('mousemove', wf_mousemove);
    document.addEventListener('mouseup', wf_mouseup);
    document.addEventListener('keydown', wf_keydown);
}

// # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
//                                 ENTRY POINTS
// # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

function wf_collect_defaults() {
    const defaults = {};
    for (const element of document.querySelectorAll('.wf-default'))
        defaults[element.id] = element.value;
    return defaults;
}

function wf_apply_defaults(defaults) {

    // Engines first, since they decide which Networks can be selected
    for (const side of ['dev', 'base'])
        if (defaults[side + '_engine'] && engines[defaults[side + '_engine']])
            change_engine(defaults[side + '_engine'], side, 'TEST');

    for (const field in defaults) {
        const element = document.getElementById(field);
        if (element && element.classList.contains('wf-default'))
            element.value = defaults[field];
    }
}

function wf_example_graph() {

    // The kind of Workflow this was built for: A/B test, then the winner against main,
    // and a tune of whichever branch did not make it
    const base = wf_default_value('base_branch') || 'main';
    return {
        defaults : {},
        nodes : {
            ab     : { kind : 'SPRT', label : 'X vs Y',    x : 40,  y : 160, config : { dev : 'branch-x', base : 'branch-y' } },
            x_main : { kind : 'SPRT', label : 'X vs main', x : 360, y : 40,  config : { dev : '@ab.dev',  base : base } },
            y_main : { kind : 'SPRT', label : 'Y vs main', x : 360, y : 300, config : { dev : '@ab.base', base : base } },
            tune   : { kind : 'SPSA', label : 'Tune',      x : 700, y : 160, config : Object.assign({ dev : '@trigger.dev', spsa_inputs : '' }, WF_SPSA_DEFAULTS) },
        },
        edges : [
            { from : 'ab',     port : 'passed', to : 'x_main' },
            { from : 'ab',     port : 'failed', to : 'y_main' },
            { from : 'x_main', port : 'failed', to : 'tune' },
            { from : 'y_main', port : 'failed', to : 'tune' },
        ],
    };
}

function wf_init_editor(clone_graph, edit) {

    // edit holds the submit_url, and for a running Workflow its locked Nodes and their state
    wf.editable   = true;
    wf.panel      = document.getElementById('wf-panel');
    wf.submit_url = edit.submit_url;
    wf.locked     = new Set(edit.locked || []);
    wf.state      = edit.state;
    wf_attach(document.getElementById('wf-canvas-scroll'));

    if (clone_graph) {
        wf_apply_defaults(clone_graph.defaults || {});
        wf.graph = clone_graph;
    }

    for (const kind of ['SPRT', 'SPSA', 'JOIN', 'IF'])
        document.getElementById('wf-add-' + kind).onclick = () => wf_add_node(kind);

    const example = document.getElementById('wf-example');
    if (example) example.onclick = () => {
        if (Object.keys(wf.graph.nodes).length && !confirm('Replace the current Workflow with the example?'))
            return;
        wf.graph = wf_example_graph();
        wf_select(null);
    };

    // Summaries and placeholders show the defaults, so keep them current
    for (const element of document.querySelectorAll('.wf-default'))
        element.addEventListener('change', () => { wf_render(); wf_render_panel(); });

    document.getElementById('wf-submit').onclick = wf_submit;

    wf_render();
    wf_render_panel();
}

function wf_submit() {

    const errors = document.getElementById('wf-errors');
    const button = document.getElementById('wf-submit');
    wf.graph.defaults = wf_collect_defaults();

    button.disabled = true;
    errors.hidden   = true;

    fetch(wf.submit_url, {
        method  : 'POST',
        headers : { 'Content-Type' : 'application/json', 'X-CSRFToken' : wf.csrf },
        body    : JSON.stringify({ name : document.getElementById('wf-name').value, graph : wf.graph }),
    })
    .then(response => response.json())
    .then(data => {
        if (data.id)
            return window.location.href = '/workflow/' + data.id + '/';
        errors.textContent = (data.errors || ['Unknown error']).join('\n');
        errors.hidden      = false;
        button.disabled    = false;
        window.scrollTo(0, 0);
    })
    .catch(error => {
        errors.textContent = String(error);
        errors.hidden      = false;
        button.disabled    = false;
    });
}

function wf_init_view(workflow_id, graph, state, can_modify) {

    wf.workflow_id = workflow_id;
    wf.graph       = graph;
    wf.state       = state;
    wf.can_modify  = can_modify;
    wf.panel       = document.getElementById('wf-panel');
    wf_attach(document.getElementById('wf-canvas-scroll'));

    wf_render();
    wf_render_panel();

    // Follow the progress, until there is nothing left to happen
    const timer = setInterval(() => {

        if (['FINISHED', 'ABORTED'].includes(wf.state.status))
            return clearInterval(timer);

        fetch('/workflow/' + workflow_id + '/state/')
            .then(response => response.json())
            .then(data => {
                const changed = data.status != wf.state.status;
                wf.state = data;
                wf_render();
                if (!document.activeElement || document.activeElement.tagName != 'INPUT')
                    wf_render_panel();
                if (changed)
                    document.getElementById('wf-status').textContent = data.status;
            })
            .catch(() => {}); // The Server may be restarting, just try again later
    }, 20000);
}
