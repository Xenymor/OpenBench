function copy_text(text) {

    var area = document.createElement("textarea");
    area.value = text;
    document.body.append(area);
    area.select();

    try {
        document.execCommand("copy");
        document.body.removeChild(area);
    }

    catch (err) {
        document.body.removeChild(area);
        console.error("Unable to copy to Clipboard");
    }
}

function copy_text_from_element(element_id, keep_url) {

    var text = document.getElementById(element_id).innerHTML;
    text = text.replace(/<br>/g, "\n");

    if (keep_url)
        text += "\n" + window.location.href;

    copy_text(text);
}


function populate_results(results) {

    const container = document.getElementById('results-container');

    container.innerHTML = ''; // Clear everything for sanity

    results.forEach(result => {
        const tr = document.createElement('tr');

        // Highlight active rows
        if (result.active) tr.classList.add('active-highlight');

        // Collapse the trinomial won/lost/drawn into the pentanomial tuple and
        // its pair count, mirroring the aggregate summary tables above
        const penta = [result.LL, result.LD, result.DD, result.DW, result.WW];
        const pairs = penta.reduce((a, b) => a + b, 0);

        tr.innerHTML = `
            <td><a href="/machines/${result.machine__id}">${result.machine__id}</a></td>
            <td>${result.machine__user__username.charAt(0).toUpperCase() + result.machine__user__username.slice(1)}</td>
            <td class="numeric">${result.games}</td>
            <td>(${penta.join(', ')})</td>
            <td class="numeric">${pairs}</td>
            <td class="numeric">${result.timeloss}</td>
            <td class="numeric">${result.crashes}</td>
        `;

        container.appendChild(tr);
    });
}

async function fetch_results(workload_id) {
    fetch(`/api/workload/${workload_id}/results/`)
        .then(r => r.json())
        .then(data => populate_results(data.results))
}


function summary_cell(tag, text, class_name) {

    // Keys are free-form (cpu names, isa names), so set everything as text to
    // avoid injecting any markup a Machine might have reported
    const cell = document.createElement(tag);
    cell.textContent = text;
    if (class_name) cell.className = class_name;
    return cell;
}

function format_cpu_name(name) {

    // CPU names as reported by py-cpuinfo can be verbose and noisy.
    // Clean them up here so the table stays readable.

    // Drop the (R) registered-trademark marker.
    name = name.replace(/\(R\)/g, '');

    // "Intel Xeon" is redundant — Xeon already implies Intel, so drop Intel.
    if (/Intel/.test(name) && /Xeon/.test(name))
        name = name.replace(/Intel/g, '');

    // Likewise, "AMD EPYC" and "AMD Ryzen" are redundant — both imply AMD.
    if (/AMD/.test(name) && /EPYC|Ryzen/.test(name))
        name = name.replace(/AMD/g, '');

    // "Processor" and "CPU" add nothing in this context.
    name = name.replace(/Processor/g, '');
    name = name.replace(/CPU/g, '');

    // Core counts, ie "16-Core", are reported separately in the table.
    name = name.replace(/\d+-Core/gi, '');

    // The removals above can leave stray spacing; collapse runs of whitespace
    // to a single space and trim the ends.
    name = name.replace(/\s+/g, ' ').trim();

    return name;
}

function append_summary_section(table, label, rows, key_formatter) {

    // Older workloads don't have NPS tracking stats.
    const is_nps_available =  rows.some(row => row.dev_nps > 0);

    // A header row naming the grouping, then one tbody of data rows. All three
    // sections share the one table, so their columns line up automatically.
    const header = document.createElement('tr');
    header.className = 'table-header';
    header.appendChild(summary_cell('th', label));

    ['Penta', 'Elo', 'Pairs', '%'].forEach(name => {
        header.appendChild(summary_cell('th', name));
    });

    if (is_nps_available) {
        header.appendChild(summary_cell('th', 'KNPS'));
        header.appendChild(summary_cell('th', 'Scaled KNPS'));
    }

    table.appendChild(header);

    const tbody = document.createElement('tbody');

    rows.forEach(row => {
        const tr = document.createElement('tr');

        // The API hands us display-ready fields: the penta tuple as a string,
        // a point-estimate Elo, the pair count, and the % of the group total
        tr.appendChild(summary_cell('td', key_formatter ? key_formatter(row.key) : row.key));
        tr.appendChild(summary_cell('td', row.penta));
        tr.appendChild(summary_cell('td', row.elo,   'numeric'));
        tr.appendChild(summary_cell('td', row.pairs, 'numeric'));
        tr.appendChild(summary_cell('td', row.percent, 'numeric'));

        if (is_nps_available) {
            const format_nps = (nps) => (nps / 1000.0).toFixed(1);

            tr.appendChild(summary_cell('td', `${format_nps(row.dev_nps)} / ${format_nps(row.base_nps)}`));
            tr.appendChild(summary_cell('td', `${format_nps(row.dev_nps_scaled)} / ${format_nps(row.base_nps_scaled)}`));
        }

        tbody.appendChild(tr);
    });

    table.appendChild(tbody);
}

async function fetch_summary(workload_id) {
    fetch(`/api/workload/${workload_id}/summary/`)
        .then(r => r.json())
        .then(data => {
            const container = document.getElementById('summary-container');
            container.innerHTML = ''; // Rebuild the whole table each fetch

            const table = document.createElement('table');
            table.className = 'stripes wrappable summary-table';

            append_summary_section(table, 'User', data.summary.user);
            append_summary_section(table, 'CPU',  data.summary.cpu_name, format_cpu_name);
            append_summary_section(table, 'ISA',  data.summary.isa_name);

            container.appendChild(table);
        })
}


async function copy_spsa_inputs(workload_id) {
    const resp = await fetch(`/api/spsa/${workload_id}/inputs/`)
    const text = await resp.text()
    copy_text(text)
}

async function copy_spsa_outputs(workload_id) {
    const resp = await fetch(`/api/spsa/${workload_id}/outputs/`)
    const text = await resp.text()
    copy_text(text)
}

async function fetch_spsa_digest(workload_id) {
    const resp  = await fetch(`/api/spsa/${workload_id}/digest/`)
    const text  = await resp.text()
    const lines = text.trim().split('\n')

    // Skip the header line (index 0) and process data rows
    const tbody = document.getElementById('spsa-digest-body-container')
    tbody.innerHTML = ''

    for (let i = 1; i < lines.length; i++) {
        const values = lines[i].split(',')
        const tr = document.createElement('tr')

        values.forEach(value => {
            const td = document.createElement('td')
            td.textContent = value
            tr.appendChild(td)
        })

        tbody.appendChild(tr)
    }

    // Show the data and hide the button
    tbody.style.display = ''
    const buttonContainer = document.getElementById('spsa-digest-button-container')
    buttonContainer.style.display = 'none'
}

// The course of every parameter of a tune, drawn with charts.js. All of them together, as their
// distance from the start in units of their C_end, or one of them in its own units.
async function fetch_spsa_history(workload_id) {

    const resp = await fetch(`/api/spsa/${workload_id}/history/`)
    const data = await resp.json()
    const root = document.getElementById('spsa-history-container')
    root.innerHTML = ''

    const info = document.createElement('p')
    info.style.whiteSpace = 'normal'
    const parts = [`${data.games.length - 1} snapshots`]
    if (data.drift !== null)
        parts.push(`largest drift over the last ${Math.round(100 * data.window)}% of the games: ${data.drift.toFixed(2)} C_end`)
    if (data.auto_stop)
        parts.push(`stops below ${data.auto_stop} C_end`)
    if (data.converged_at !== null)
        parts.push(`converged after ${data.converged_at} games`)
    info.textContent = parts.join(' · ')
    root.appendChild(info)

    const select = document.createElement('select')
    select.appendChild(new Option('All parameters, in C_end from their start', ''))
    data.params.forEach((param, index) => select.appendChild(new Option(param.name, index)))
    root.appendChild(select)

    const chart = document.createElement('div')
    chart.className = 'mt-1'
    root.appendChild(chart)

    function draw() {

        let series, label

        if (select.value === '') {
            series = data.params.map(param => ({
                name   : param.name,
                width  : 1,
                points : data.games.map((games, index) => [games,
                    param.values[index] === null || !param.c_end ? null : (param.values[index] - param.start) / param.c_end]),
            }))
            label = 'Drift from the start, in C_end'
        }

        else {
            const param = data.params[Number(select.value)]
            series = [{ name : param.name, width : 2, points : data.games.map((games, index) => [games, param.values[index]]) }]
            label  = `${param.name} (start ${param.start}, range ${param.min} to ${param.max})`
        }

        ob_chart(chart, { series : series, x_label : 'Games', y_label : label, zero : select.value === '' })
    }

    select.onchange = draw
    draw()
}
