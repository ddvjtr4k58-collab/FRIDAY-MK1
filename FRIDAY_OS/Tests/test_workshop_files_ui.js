/**
 * Workshop Files panel test suite.
 *
 * Runs the REAL panel source. The Workshop Files block is sliced out of
 * renderer.js by its own section markers and evaluated in a `vm` context with
 * stubbed DOM and socket globals — so what is under test is the production
 * text of those functions, not a copy of them that can drift.
 *
 * Offline and deterministic: no Electron, no socket server, no filesystem. The
 * backend contract those functions depend on is covered separately and against
 * the real Virtual Finder by Tests/test_workshop_files.py.
 *
 * Run from FRIDAY_OS:
 *
 *     node Tests/test_workshop_files_ui.js
 *
 * Covered:
 *   1. folders render as controls that carry the path to navigate to
 *   2. clicking a folder lists it, and nested folders keep working
 *   3. Back and Up are enabled only when there is somewhere to go
 *   4. a text file opens in a read-only viewer showing its contents
 *   5. a file FRIDAY cannot render falls back to metadata plus Finder
 *   6. a failed or slow request says so instead of hanging on "Loading…"
 */

'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const HERE = __dirname;
const RENDERER = path.join(HERE, '..', 'Visual_Interface', 'renderer.js');

const PASSED = [];
const FAILED = [];

function check(name, condition, detail) {
    if (condition) {
        PASSED.push(name);
        console.log('[PASS] ' + name);
    } else {
        FAILED.push(name);
        console.log('[FAIL] ' + name + (detail ? ' — ' + detail : ''));
    }
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Slice the Workshop Files section out of renderer.js.
 *
 * Bounded by the section banner and the next top-level function, so the slice
 * follows the file rather than a pair of line numbers that would rot.
 */
function loadPanelSource() {
    const source = fs.readFileSync(RENDERER, 'utf8');
    const start = source.indexOf('// WORKSHOP FILES');
    const end = source.indexOf('function renderWorkshopContextSection');

    if (start === -1 || end === -1 || end <= start) {
        throw new Error('could not locate the Workshop Files section in renderer.js');
    }

    return source.slice(start, end);
}

/**
 * A DOM small enough to be honest about: the Files section markup the panel
 * writes into, and nothing else.
 */
function makeHarness(handlers) {
    const emitted = [];

    const body = {
        innerHTML: '',
        querySelector: () => null
    };
    const count = { textContent: '0' };
    const toggle = { setAttribute: () => {} };

    const section = {
        classList: {
            _open: false,
            contains(name) { return name === 'open' ? this._open : false; },
            add(name) { if (name === 'open') this._open = true; }
        },
        querySelector(selector) {
            // Mirrors the real markup: the section body is the collapsible grid
            // track and the panel renders into the inner wrapper, so that the
            // wrapper the collapse animation needs is never replaced.
            if (selector === '.ws-section-body-inner') return body;
            if (selector === '.ws-section-count') return count;
            if (selector === '.ws-section-toggle') return toggle;
            return null;
        }
    };

    const context = {
        console: { log: () => {}, warn: () => {}, error: () => {} },
        setTimeout,
        clearTimeout,
        Promise,
        String,
        Number,
        Boolean,
        Array,
        Object,
        Math,
        Date,
        JSON,
        Error,
        require: (name) => {
            if (name === 'electron') return { ipcRenderer: handlers.ipc || { invoke: () => Promise.resolve({ ok: true }) } };
            throw new Error('unexpected require: ' + name);
        },
        document: {
            querySelectorAll: () => [section]
        },
        socket: {
            emit(event, payload, ack) {
                emitted.push({ event, payload });
                const responder = handlers.respond || (() => ({ ok: false }));
                // Asynchronous, like a real ack.
                setTimeout(() => ack && ack(responder(event, payload)), 1);
            }
        },
        // Existing renderer helpers the panel calls. Stubbed rather than sliced:
        // they are shipped, working code and are not what is under test here.
        escapeHtml: (value) => String(value === undefined || value === null ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;'),
        formatVirtualFinderSize: (value, type) => (type === 'folder' ? '' : `${Number(value) || 0} B`),
        safeVirtualFinderImageSource: (preview) => (preview && preview.data_uri) || '',
        virtualDesktopPayload: handlers.desktopPayload || { items: [] }
    };

    // The panel's own timeouts go through window.setTimeout. Capturing them lets
    // a test fire one on demand rather than waiting out a real eight seconds.
    const timers = [];

    context.window = handlers.captureTimers
        ? {
            setTimeout: (fn, ms) => timers.push({ fn, ms, cancelled: false }),
            clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].cancelled = true; }
        }
        : { setTimeout, clearTimeout };

    context.globalThis = context;

    vm.runInNewContext(loadPanelSource(), context, { filename: 'renderer.js#workshop-files' });

    return { context, body, count, section, emitted, timers };
}

/** A click on the first element whose markup carries `attribute`. */
function clickTarget(html, attribute, value) {
    const pattern = value === undefined
        ? new RegExp(`${attribute}="([^"]*)"`)
        : new RegExp(`${attribute}="(${value})"`);
    const match = pattern.exec(html);

    if (!match) return null;

    const dataset = {};
    // data-ws-file -> wsFile
    const key = attribute.replace(/^data-/, '').replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    dataset[key] = match[1];

    // The panel also reads data-ws-file-type off the same row.
    const rowType = new RegExp(`${attribute}="${match[1]}"[^>]*data-ws-file-type="([^"]*)"`).exec(html);
    if (rowType) dataset.wsFileType = rowType[1];

    const element = { dataset, closest: (selector) => (selector === `[${attribute}]` ? element : null) };
    return element;
}

const FOLDERS = {
    '': {
        current_path: '',
        parent_path: '',
        items: [
            { name: 'Projects', path: 'Projects', type: 'folder', item_count: 2 },
            { name: 'School', path: 'School', type: 'folder', item_count: 0 }
        ]
    },
    Projects: {
        current_path: 'Projects',
        parent_path: '',
        items: [
            { name: 'Notes', path: 'Projects/Notes', type: 'folder', item_count: 1 },
            { name: 'plan.md', path: 'Projects/plan.md', type: 'file', kind: 'markdown', size: 42, previewable: true },
            { name: 'payload.bin', path: 'Projects/payload.bin', type: 'file', kind: 'file', size: 4, previewable: false }
        ]
    },
    'Projects/Notes': {
        current_path: 'Projects/Notes',
        parent_path: 'Projects',
        items: [{ name: 'idea.txt', path: 'Projects/Notes/idea.txt', type: 'file', kind: 'text', size: 9, previewable: true }]
    }
};

function defaultResponder(event, payload) {
    if (event === 'virtual_finder_list') {
        const folder = FOLDERS[String(payload.path || '')];
        return folder
            ? { ok: true, data: folder }
            : { ok: false, message: 'That folder could not be opened.' };
    }

    if (event === 'virtual_finder_preview') {
        return {
            ok: true,
            data: { preview: { kind: 'text', content: '# Plan\nship it\n' } }
        };
    }

    return { ok: false, message: 'unsupported' };
}

// ==========================================================
// 1. THE LISTING
// ==========================================================
async function testListing() {
    const harness = makeHarness({
        respond: defaultResponder,
        desktopPayload: { items: FOLDERS[''].items }
    });

    const initial = harness.context.renderWorkshopFilesSection();

    check(
        'the panel shows the folders that are really there',
        initial.html.includes('Projects') && initial.html.includes('School'),
        initial.html.slice(0, 160)
    );
    check(
        'folders render as controls rather than inert list items',
        /<button[^>]*class="ws-file-row"[^>]*data-ws-file="Projects"/.test(initial.html),
        'no clickable row found'
    );
    check(
        'each row carries the path to navigate to',
        initial.html.includes('data-ws-file="Projects"') && initial.html.includes('data-ws-file-type="folder"')
    );
    check('the panel offers Back, Up and Refresh',
        initial.html.includes('data-ws-files-nav="back"')
        && initial.html.includes('data-ws-files-nav="up"')
        && initial.html.includes('data-ws-files-nav="refresh"')
    );
    check(
        'Back and Up are disabled at the root, where there is nowhere to go',
        /data-ws-files-nav="back"\s+disabled/.test(initial.html.replace(/\s+/g, ' '))
        || initial.html.includes('data-ws-files-nav="back"\n                    disabled'),
        'navigation buttons are not disabled at root'
    );
}

// ==========================================================
// 2. NAVIGATION
// ==========================================================
async function testNavigation() {
    const harness = makeHarness({
        respond: defaultResponder,
        desktopPayload: { items: FOLDERS[''].items }
    });
    const ctx = harness.context;

    // Into Projects.
    const row = clickTarget(ctx.renderWorkshopFilesSection().html, 'data-ws-file', 'Projects');
    check('a folder row is clickable', Boolean(row));
    ctx.handleWorkshopFilesClick(row);
    await wait(30);

    check(
        'clicking a folder asks the Virtual Finder for it',
        harness.emitted.some((entry) => entry.event === 'virtual_finder_list' && entry.payload.path === 'Projects'),
        JSON.stringify(harness.emitted)
    );
    check(
        'and the panel then shows that folder',
        harness.body.innerHTML.includes('plan.md') && harness.body.innerHTML.includes('Notes'),
        harness.body.innerHTML.slice(0, 200)
    );
    check(
        'the panel opens itself so the result is visible',
        harness.section.classList.contains('open')
    );

    // Into a nested folder.
    const nested = clickTarget(harness.body.innerHTML, 'data-ws-file', 'Projects/Notes');
    ctx.handleWorkshopFilesClick(nested);
    await wait(30);

    check(
        'nested folders open too',
        harness.body.innerHTML.includes('idea.txt'),
        harness.body.innerHTML.slice(0, 200)
    );

    // Up -> Projects.
    ctx.handleWorkshopFilesClick(clickTarget(harness.body.innerHTML, 'data-ws-files-nav', 'up'));
    await wait(30);

    check(
        'Up returns to the parent folder',
        harness.body.innerHTML.includes('plan.md'),
        harness.body.innerHTML.slice(0, 200)
    );

    // Back -> Notes, because that is where we came from.
    ctx.handleWorkshopFilesClick(clickTarget(harness.body.innerHTML, 'data-ws-files-nav', 'back'));
    await wait(30);

    check(
        'Back returns to where you actually were, not to the parent',
        harness.body.innerHTML.includes('idea.txt'),
        harness.body.innerHTML.slice(0, 200)
    );
}

// ==========================================================
// 3. THE READ-ONLY VIEWER
// ==========================================================
async function testViewer() {
    const harness = makeHarness({ respond: defaultResponder, desktopPayload: { items: [] } });
    const ctx = harness.context;

    ctx.handleWorkshopFilesClick({ dataset: { wsFilesNav: 'refresh' }, closest: (s) => (s === '[data-ws-files-nav]' ? { dataset: { wsFilesNav: 'refresh' } } : null) });
    await wait(30);
    ctx.handleWorkshopFilesClick(clickTarget(harness.body.innerHTML, 'data-ws-file', 'Projects'));
    await wait(30);

    const file = clickTarget(harness.body.innerHTML, 'data-ws-file', 'Projects/plan.md');
    check('a file row is clickable', Boolean(file));
    ctx.handleWorkshopFilesClick(file);
    await wait(30);

    check(
        'clicking a text file asks for a preview',
        harness.emitted.some((entry) => entry.event === 'virtual_finder_preview' && entry.payload.path === 'Projects/plan.md'),
        JSON.stringify(harness.emitted.map((e) => e.event))
    );
    check(
        'the viewer shows the file contents',
        harness.body.innerHTML.includes('ship it'),
        harness.body.innerHTML.slice(0, 300)
    );
    check('the viewer names the file', harness.body.innerHTML.includes('plan.md'));
    check(
        'the viewer says plainly that it is read-only',
        harness.body.innerHTML.includes('Read-only')
    );
    check(
        'the viewer offers Finder for anything it cannot do',
        harness.body.innerHTML.includes('data-ws-file-reveal="Projects/plan.md"')
    );

    // Closing returns to the listing.
    ctx.handleWorkshopFilesClick({ dataset: {}, closest: (s) => (s === '[data-ws-file-close]' ? {} : null) });
    await wait(10);

    check(
        'closing the viewer returns to the folder',
        harness.body.innerHTML.includes('plan.md') && !harness.body.innerHTML.includes('Read-only'),
        harness.body.innerHTML.slice(0, 200)
    );
}

// ==========================================================
// 4. FILES FRIDAY CANNOT RENDER
// ==========================================================
async function testUnsupportedFile() {
    const revealed = [];
    const harness = makeHarness({
        respond: defaultResponder,
        desktopPayload: { items: [] },
        ipc: {
            invoke: (channel, value) => {
                revealed.push({ channel, value });
                return Promise.resolve({ ok: true });
            }
        }
    });
    const ctx = harness.context;

    ctx.handleWorkshopFilesClick({ dataset: { wsFilesNav: 'refresh' }, closest: (s) => (s === '[data-ws-files-nav]' ? { dataset: { wsFilesNav: 'refresh' } } : null) });
    await wait(30);
    ctx.handleWorkshopFilesClick(clickTarget(harness.body.innerHTML, 'data-ws-file', 'Projects'));
    await wait(30);

    const binary = clickTarget(harness.body.innerHTML, 'data-ws-file', 'Projects/payload.bin');
    ctx.handleWorkshopFilesClick(binary);
    await wait(30);

    check(
        'an unsupported file is not sent for a preview that would fail',
        !harness.emitted.some((entry) => entry.event === 'virtual_finder_preview'),
        JSON.stringify(harness.emitted.map((e) => e.event))
    );
    check(
        'it explains that the format cannot be displayed',
        harness.body.innerHTML.includes('cannot display this format'),
        harness.body.innerHTML.slice(0, 300)
    );
    check(
        'and offers Finder as the way to use it',
        harness.body.innerHTML.includes('data-ws-file-reveal="Projects/payload.bin"')
    );

    ctx.handleWorkshopFilesClick({
        dataset: { wsFileReveal: 'Projects/payload.bin' },
        closest: (s) => (s === '[data-ws-file-reveal]' ? { dataset: { wsFileReveal: 'Projects/payload.bin' } } : null)
    });
    await wait(10);

    check(
        'Open in Finder sends the VIRTUAL path, never an absolute one',
        revealed.length === 1
        && revealed[0].channel === 'files:reveal'
        && revealed[0].value === 'Projects/payload.bin',
        JSON.stringify(revealed)
    );
}

// ==========================================================
// 5. FAILURE IS REPORTED, NEVER HUNG
// ==========================================================
async function testFailures() {
    const harness = makeHarness({
        respond: (event) => (event === 'virtual_finder_list'
            ? { ok: false, message: 'That folder could not be opened.' }
            : { ok: false, message: 'nope' }),
        desktopPayload: { items: FOLDERS[''].items }
    });
    const ctx = harness.context;

    ctx.handleWorkshopFilesClick(clickTarget(ctx.renderWorkshopFilesSection().html, 'data-ws-file', 'Projects'));
    await wait(30);

    check(
        'a folder that will not open says so',
        harness.body.innerHTML.includes('Could not open that folder'),
        harness.body.innerHTML.slice(0, 200)
    );
    check(
        'and the panel still offers a way out rather than being stuck',
        harness.body.innerHTML.includes('data-ws-files-nav="refresh"')
    );

    // A socket that never answers must not leave the panel on "Loading…".
    const silent = makeHarness({
        respond: null,
        desktopPayload: { items: FOLDERS[''].items },
        captureTimers: true
    });
    silent.context.socket.emit = (event, payload) => {
        silent.emitted.push({ event, payload });
        // No ack, ever. This is a socket that has quietly gone away.
    };

    silent.context.handleWorkshopFilesClick(
        clickTarget(silent.context.renderWorkshopFilesSection().html, 'data-ws-file', 'Projects')
    );

    check('a request in flight shows progress', silent.body.innerHTML.includes('Loading'));

    const pending = silent.timers.filter((timer) => !timer.cancelled);
    check(
        'the request is given a deadline rather than waiting forever',
        pending.length === 1 && pending[0].ms > 0 && pending[0].ms <= 15000,
        JSON.stringify(silent.timers.map((t) => t.ms))
    );

    // Fire it, as the browser would once the deadline passes.
    pending[0].fn();
    await wait(5);

    check(
        'when nothing answers, the panel reports it instead of hanging on Loading',
        !silent.body.innerHTML.includes('Loading')
        && silent.body.innerHTML.includes('Could not open that folder'),
        silent.body.innerHTML.slice(0, 220)
    );
}

async function main() {
    const tests = [
        ['the listing', testListing],
        ['navigation', testNavigation],
        ['read-only viewer', testViewer],
        ['files FRIDAY cannot render', testUnsupportedFile],
        ['failures are reported', testFailures]
    ];

    for (const [label, test] of tests) {
        console.log('\n== ' + label + ' ==');

        try {
            await test();
        } catch (err) {
            FAILED.push(label);
            console.log('[FAIL] ' + label + ' raised ' + (err && err.stack ? err.stack : err));
        }
    }

    console.log('\n' + '='.repeat(52));
    console.log('Workshop Files UI: ' + PASSED.length + ' passed, ' + FAILED.length + ' failed');

    if (FAILED.length) {
        FAILED.forEach((name) => console.log('  failed: ' + name));
    }

    process.exit(FAILED.length ? 1 : 0);
}

main();
