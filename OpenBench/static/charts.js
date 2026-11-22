// Small line charts as inline SVG, for the SPSA history of a tune and the course of a Ladder.
//
//   ob_chart(container, {
//       series : [ { name, points : [[x, y], ...], color, width, errors : [[low, high], ...], dots, line : false } ],
//       x_label, y_label, x_format(x), y_format(y), zero : true,
//   })
//
// Every point gets a tooltip. Points with a y of null leave a gap in their line.

const OB_CHART_COLORS = ['#60F0FF', '#76D576', '#FA9696', '#EFF891', '#C792EA', '#F78C6C', '#82AAFF', '#FFCB6B'];

function ob_svg(tag, attributes, text) {
    const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const name in attributes)
        element.setAttribute(name, attributes[name]);
    if (text !== undefined)
        element.textContent = text;
    return element;
}

function ob_ticks(low, high, count) {

    // Round steps of 1, 2 or 5 times a power of ten
    const span = high - low || 1;
    const raw  = span / count;
    const base = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 5, 10].map(f => f * base).find(s => s >= raw);
    const ticks = [];
    for (let tick = Math.ceil(low / step) * step; tick <= high + step * 1e-9; tick += step)
        ticks.push(Math.abs(tick) < step * 1e-9 ? 0 : tick);
    return ticks;
}

function ob_chart(container, options) {

    // Drawn at a fixed size, and scaled to the width of the page, so that it never overflows it
    const width = options.width || 960, height = options.height || 360;
    const pad   = { left : 64, right : 16, top : 16, bottom : 44 };
    const xs = [], ys = [];

    for (const series of options.series)
        series.points.forEach((point, index) => {
            if (point[1] === null || point[1] === undefined) return;
            xs.push(point[0]); ys.push(point[1]);
            if (series.errors && series.errors[index]) ys.push(...series.errors[index]);
        });

    while (container.firstChild) container.removeChild(container.firstChild);

    if (!xs.length) {
        container.textContent = options.empty || 'Nothing to show yet';
        return;
    }

    if (options.zero) ys.push(0);

    let [x0, x1] = [Math.min(...xs), Math.max(...xs)];
    let [y0, y1] = [Math.min(...ys), Math.max(...ys)];
    if (x0 == x1) { x0 -= 1; x1 += 1; }
    if (y0 == y1) { y0 -= 1; y1 += 1; }
    const margin = (y1 - y0) * 0.05; y0 -= margin; y1 += margin;

    const sx = x => pad.left + (x - x0) / (x1 - x0) * (width - pad.left - pad.right);
    const sy = y => pad.top + (1 - (y - y0) / (y1 - y0)) * (height - pad.top - pad.bottom);
    const xf = options.x_format || (x => String(Math.round(x)));
    const yf = options.y_format || (y => y.toFixed(2));

    const svg = ob_svg('svg', { viewBox : '0 0 ' + width + ' ' + height, width : '100%', class : 'ob-chart',
                                style : 'max-width: ' + width + 'px; display: block;' });

    for (const tick of ob_ticks(y0, y1, 6)) {
        svg.append(ob_svg('line', { x1 : pad.left, x2 : width - pad.right, y1 : sy(tick), y2 : sy(tick),
                                    stroke : tick == 0 ? '#949BA4' : '#3B3B3B', 'stroke-width' : 1 }));
        svg.append(ob_svg('text', { x : pad.left - 6, y : sy(tick) + 4, 'text-anchor' : 'end', fill : '#949BA4', 'font-size' : 11 }, yf(tick)));
    }

    // A single point gets one tick, and labels that repeat, like the same day for several hours, are shown once
    const x_ticks = xs.every(x => x == xs[0]) ? [xs[0]] : (options.x_ticks || ob_ticks(x0, x1, 6));
    let previous  = null;
    for (const tick of x_ticks) {
        const text = xf(tick);
        if (text == previous) continue;
        previous = text;
        svg.append(ob_svg('text', { x : sx(tick), y : height - pad.bottom + 16, 'text-anchor' : 'middle', fill : '#949BA4', 'font-size' : 11 }, text));
    }

    if (options.x_label)
        svg.append(ob_svg('text', { x : (pad.left + width - pad.right) / 2, y : height - 6, 'text-anchor' : 'middle', fill : '#DBDEE1', 'font-size' : 12 }, options.x_label));

    if (options.y_label)
        svg.append(ob_svg('text', { x : 14, y : (pad.top + height - pad.bottom) / 2, 'text-anchor' : 'middle', fill : '#DBDEE1', 'font-size' : 12,
                                    transform : 'rotate(-90 14 ' + (pad.top + height - pad.bottom) / 2 + ')' }, options.y_label));

    options.series.forEach((series, number) => {

        const color = series.color || OB_CHART_COLORS[number % OB_CHART_COLORS.length];
        let path = '', open = false;

        series.points.forEach((point, index) => {

            if (point[1] === null || point[1] === undefined) { open = false; return; }
            path += (open ? ' L ' : ' M ') + sx(point[0]).toFixed(1) + ' ' + sy(point[1]).toFixed(1);
            open = true;

            if (series.errors && series.errors[index]) {
                const [low, high] = series.errors[index];
                svg.append(ob_svg('line', { x1 : sx(point[0]), x2 : sx(point[0]), y1 : sy(low), y2 : sy(high), stroke : color, 'stroke-width' : 1, opacity : 0.6 }));
            }

            if (series.dots) {
                const dot = ob_svg('circle', { cx : sx(point[0]), cy : sy(point[1]), r : 4, fill : color });
                dot.append(ob_svg('title', {}, (series.labels && series.labels[index]) || (series.name + ': ' + yf(point[1]) + ' at ' + xf(point[0]))));
                svg.append(dot);
            }
        });

        // Series of separate marks, like Tests that were stopped early, have no line
        if (series.line !== false) {
            const line = ob_svg('path', { d : path, fill : 'none', stroke : color, 'stroke-width' : series.width || 1.5, opacity : series.opacity || 1 });
            line.append(ob_svg('title', {}, series.name));
            svg.append(line);
        }
    });

    container.append(svg);
}
