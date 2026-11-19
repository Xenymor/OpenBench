
var config   = JSON.parse(document.getElementById('json-config'  ).textContent);
var engines  = JSON.parse(document.getElementById('json-engines' ).textContent);
var networks = JSON.parse(document.getElementById('json-networks').textContent);
var repos    = JSON.parse(document.getElementById('json-repos'   ).textContent);

function create_network_options(field_id, engine) {

    var has_default     = false;
    var network_options = document.getElementById(field_id);

    // Delete all existing Networks
    while (network_options.length)
        network_options.remove(0);

    // Add each Network that matches the given engine
    for (const network of networks) {

        if (network.engine !== engine)
            continue;

        var opt      = document.createElement('option');
        opt.text     = network.name;
        opt.value    = network.sha256;
        opt.selected = network.default;
        network_options.add(opt)

        has_default = has_default || network.default;
    }

    { // Add a None option and set it to default if there was not one yet
        var opt       = document.createElement('option');
        opt.text      = 'None';
        opt.value     = '';
        opt.selected  = !has_default;
        network_options.add(opt);
    }
}

function create_preset_buttons(engine, workload_type) {

    // Clear out all of the existing buttons
    var button_div = document.getElementById('test-mode-buttons');
    while (button_div.hasChildNodes())
        button_div.removeChild(button_div.lastChild);

    const presets = workload_type == 'TEST'    ? engines[engine].test_presets
                  : workload_type == 'TUNE'    ? engines[engine].tune_presets
                  : workload_type == 'DATAGEN' ? engines[engine].datagen_presets : {};

    var index = 0;
    for (let mode in presets) {

        // Don't include the global defaults
        if (mode == 'default')
            continue;

        // Create a new button for the test mode
        var btn       = document.createElement('button')
        btn.innerHTML = mode;
        btn.onclick   = function() { apply_preset(mode, workload_type); autofill_benches(); };

        // Apply all of our CSS bootstrapping
        btn.classList.add('anchorbutton');
        btn.classList.add('btn-preset');
        btn.classList.add('mt-1');
        btn.classList.add('w-100');

        // Put the button in a div, so we can handle padding
        var div = document.createElement('div')
        div.appendChild(btn)
        div.classList.add('col-half');

        // Left pad everything but the first
        if ((index % 4) != 0)
            div.classList.add('pl-half');

        // Right pad everything but the last
        if ((index % 4) != 3)
            div.classList.add('pr-half');

        button_div.append(div);
        index++;
    }
}


function get_dev_engine() {
    const selection = document.getElementById('dev_engine');
    return selection.options[selection.selectedIndex].value;
}

function get_base_engine() {
    const selection = document.getElementById('base_engine');
    return selection.options[selection.selectedIndex].value;
}

function get_presets(engine, preset, workload_type) {
    return workload_type == 'TEST'    ? engines[engine].test_presets[preset]
         : workload_type == 'TUNE'    ? engines[engine].tune_presets[preset]
         : workload_type == 'DATAGEN' ? engines[engine].datagen_presets[preset] : {};
}


function add_defaults_to_preset(engine, preset, workload_type) {

    const default_settings = get_presets(engine, 'default', workload_type);
    const preset_settings  = get_presets(engine, preset, workload_type);

    let settings = {}

    for (const key in default_settings)
        settings[key] = default_settings[key];

    for (const key in preset_settings)
        settings[key] = preset_settings[key];

    return settings;
}

function set_engine(engine, target) {

    document.getElementById(target + '_engine').value = engine;
    document.getElementById(target + '_repo'  ).value = repos[engine] || engines[engine].source

    create_network_options(target + '_network', engine);
}

function set_option(option_name, option_value) {

    const element = document.getElementById(option_name);

    if (element == null)
        console.log(option_name + ' was not found.');

    else if (element.tagName.toLowerCase() != 'select') {

        element.value = option_value;

        if (option_name == 'test_max_games') {
            document.getElementById('test_mode').value = "GAMES";
            document.getElementById('test_bounds').value = 'N/A';
            document.getElementById('test_confidence').value = 'N/A';
        }

        if (option_name == 'test_bounds' || option_name == 'test_confidence') {
            document.getElementById('test_mode').value = "SPRT";
            document.getElementById('test_max_games').value = 'N/A';
        }
    }

    else {
        for (let i = 0; i < element.options.length; i++)
            if (element.options[i].text === option_value || element.options[i].value === option_value)
                element.value = element.options[i].value;
    }
}

function retain_specific_options(engine, preset, workload_type) {

    // This is not applicable for self-play
    if (get_dev_engine() == get_base_engine())
        return;

    // Extract the Threads and Hash settings from the Dev Options

    const dev_options   = document.getElementById('dev_options').value;

    const threads_match = dev_options.match(/\bThreads\s*=\s*(\d+)\b/);
    const hash_match    = dev_options.match(/\bHash\s*=\s*(\d+)\b/);

    const dev_threads   = threads_match ? threads_match[1] : null;
    const dev_hash      = hash_match    ? hash_match[1]    : null;

    // From the base options, replace the Threads= and Hash=

    const settings = add_defaults_to_preset(engine, preset, workload_type);

    let base_options = settings['base_options'] || settings['both_options'];

    base_options = base_options.replace(/\bThreads\s*=\s*\d+\b/g, 'Threads=' + dev_threads);
    base_options = base_options.replace(/\bHash\s*=\s*\d+\b/g, 'Hash=' + dev_hash);

    set_option('base_options', base_options);

    // Retain the base engine's original base_branch, instead of leeting the dev engine override

    if (settings.hasOwnProperty('base_branch'))
        set_option('base_branch', settings['base_branch']);
}


function apply_preset(preset, workload_type) {

    if (preset != 'default')
        apply_preset('default', workload_type);

    const settings = get_presets(get_dev_engine(), preset, workload_type);

    for (const option in settings) {

        if (!settings.hasOwnProperty(option))
            continue;

        else if (!option.startsWith('both_'))
            set_option(option, settings[option]);

        else {
            set_option(option.replace('both_', 'dev_'), settings[option]);

            if (workload_type == 'TEST' || workload_type == "DATAGEN")
                set_option(option.replace('both_', 'base_'), settings[option]);
        }
    }

    // For cross-engine tests, keep the original Hash/Threads, but
    // add any other settings that might be specific to the engine
    if (workload_type == 'TEST' || workload_type == "DATAGEN") {
        try {
            retain_specific_options(get_base_engine(), preset, workload_type);
        } catch (error) {}
    }
}

function change_engine(engine, target, workload_type) {

    set_engine(engine, target);

    if (target == 'dev')
        create_preset_buttons(engine, workload_type);

    if (target == 'dev' && (workload_type == 'TEST' || workload_type == 'DATAGEN'))
        set_engine(engine, 'base');

    set_option('scale_nps', engines[engine].nps);
    set_option('scale_method', workload_type == 'TUNE' ? 'DEV' : 'BASE');

    apply_preset('STC', workload_type);
    autofill_benches();
}

// Bench autofill, see views.api_bench(). Lookups only start once the form is used, since a
// lookup on every page view would spend Github's API requests on the preset's Base Branch.
var bench_autofill = {
    active : false,
    dev    : { filled : null, lookup : 0 }, // The Bench autofill_bench() filled in, and its latest lookup
    base   : { filled : null, lookup : 0 },
};

// 'engine|repo|branch' -> { sha, message_bench, time }. For a minute, a branch is not resolved again,
// e.g. for another Network or a preset. The sha sent along with the Bench guards against a branch that
// moved on meanwhile, since the server then ignores the Bench filled in for the older commit.
var bench_commits    = {};
const BENCH_COMMIT_TTL = 60 * 1000;

function setup_bench_autofill() {

    // Look the Bench up again whenever something it depends on changes. The first change
    // looks up both sides, since the presets fill in the Base Branch before that.
    for (const target of ['dev', 'base'])
        for (const field of ['branch', 'repo', 'network'])
            document.getElementById(target + '_' + field)?.addEventListener('change', function () {
                if (bench_autofill.active) return autofill_bench(target);
                bench_autofill.active = true;
                autofill_benches();
            });

    for (const target of ['dev', 'base']) {

        const bench = document.getElementById(target + '_bench');
        const sha   = document.getElementById(target + '_bench_sha');

        // A Bench typed in by hand is sent as it is, without the sha of an autofilled one
        bench?.addEventListener('input', function () {
            if (sha && this.value.trim() != bench_autofill[target].filled) sha.value = '';
        });

        // Clearing the Bench by hand asks for it to be filled in again
        bench?.addEventListener('change', function () {
            if (this.value.trim()) return;
            this.value = 'Autofill';
            bench_autofill.active = true;
            autofill_bench(target);
        });
    }
}

function autofill_benches() {

    if (!bench_autofill.active)
        return;

    autofill_bench('dev');
    autofill_bench('base');
}

function autofill_bench(target) {

    // Fills in the Bench that the server would use for 'Autofill': the one of an earlier
    // Workload of the same commit and Network, or else the one in the commit message
    const state  = bench_autofill[target];
    const bench  = document.getElementById(target + '_bench');
    const branch = document.getElementById(target + '_branch');
    const sha    = document.getElementById(target + '_bench_sha');

    if (!bench || !branch)
        return;

    // Answers to any earlier lookup are stale from here on
    const lookup = ++state.lookup;

    // Never replace a Bench that was typed in by hand
    const value = bench.value.trim();
    if (value && value.toLowerCase() != 'autofill' && value != state.filled)
        return;

    bench.value  = 'Autofill';
    bench.title  = '';
    state.filled = null;
    if (sha) sha.value = '';

    const name = branch.value.trim();
    if (!name)
        return;

    const engine  = document.getElementById(target + '_engine').value;
    const repo    = document.getElementById(target + '_repo').value.trim();
    const network = document.getElementById(target + '_network').value;
    const key     = [engine, repo, name].join('|');
    const commit  = bench_commits[key];
    const cached  = commit && Date.now() - commit.time < BENCH_COMMIT_TTL ? commit : null;

    const params = new URLSearchParams({ engine : engine, repo : repo, network : network, branch : name });
    if (cached) params.set('sha', cached.sha);

    fetch('/api/bench/?' + params.toString(), { credentials : 'same-origin' })
        .then(response => response.json())
        .then(data => {

            // A newer lookup was started, or the Bench was typed in meanwhile
            if (lookup != state.lookup || bench.value.trim().toLowerCase() != 'autofill')
                return;

            if (data.error || !data.sha) {
                bench.title = data.error || '';
                return;
            }

            if (!cached)
                bench_commits[key] = { sha : data.sha, message_bench : data.message_bench, time : Date.now() };

            const message_bench = cached ? cached.message_bench : data.message_bench;
            const short         = data.sha.substring(0, 8);

            if (data.known) {
                bench.value = String(data.known.bench);
                bench.title = 'Carried over from ' + data.known.workload.type + ' #' + data.known.workload.id
                            + ', which ran ' + short + ' with this Network';
            }

            else if (message_bench) {
                bench.value = String(message_bench);
                bench.title = 'From the commit message of ' + short;
            }

            else {
                bench.title = 'Unable to parse a Bench for ' + name;
                return;
            }

            state.filled = bench.value;
            if (sha) sha.value = data.sha;
        })
        .catch(() => {}); // The server still autofills it when the form is sent
}

function set_test_type() {

    // When swapping from SPRT -> FIXED, we disable test_bounds and test_confidence
    // When swapping from FIXED -> SPRT, we disable test_max_games
    //
    // Attempt to fill SPRT fields using default settings, then STC settings.
    // Attempt to fill FIXED fields using default settings, then just use 40,000

    var selectA  = document.getElementById('test_mode');
    var mode     = selectA.options[selectA.selectedIndex].value;

    var selectB  = document.getElementById('dev_engine');
    var engine   = selectB.options[selectB.selectedIndex].value;

    var base = get_presets(engine, 'default', 'TEST');
    var stc  = get_presets(engine, 'STC', 'TEST');

    if (!stc) // If there are no STC settings, re-use the defaults
        stc = base;

    if (mode == 'SPRT') {
        document.getElementById('test_bounds'    ).value = base.test_bounds || stc.test_bounds;
        document.getElementById('test_confidence').value = base.test_confidence || stc.test_confidence;
        document.getElementById('test_max_games' ).value = 'N/A';
    }

    if (mode == 'GAMES') {
        document.getElementById('test_bounds'    ).value = 'N/A';
        document.getElementById('test_confidence').value = 'N/A';
        document.getElementById('test_max_games' ).value = base.test_max_games || stc.test_max_games || 40000;
    }
}
