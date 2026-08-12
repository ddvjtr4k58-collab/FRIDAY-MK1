/**
 * Motion system test suite.
 *
 * Offline and deterministic: no Electron, no DOM engine, no socket. The close
 * helper is sliced out of renderer.js by its section markers and run against
 * stubs, so the production text is what executes; the rest are contract checks
 * over style.css and renderer.js.
 *
 * Run from FRIDAY_OS:
 *
 *     node Tests/test_motion_system.js
 *
 * Why the contract checks exist: a motion system decays by a hundred small
 * additions, not by one big change. Someone adds `transition: opacity 0.3s ease`
 * to a new panel, someone else calls element.remove() on a window because it is
 * one line shorter, and a year later the interface snaps again. These tests fail
 * when that starts happening.
 *
 * Covered:
 *   1. the tokens exist, and the legacy names alias them rather than duplicating
 *   2. UI-chrome durations stay inside the agreed band
 *   3. every way a window leaves goes through closeElementWithMotion
 *   4. the close helper plays out, then removes — and never removes twice
 *   5. a window that never fires transitionend is still removed
 *   6. a closing window is invisible to the state reconcile
 *   7. dragging and resizing are exempt from motion
 *   8. reduced motion is answered globally, without breaking close
 */

'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const HERE = __dirname;
const INTERFACE = path.join(HERE, '..', 'Visual_Interface');
const CSS = fs.readFileSync(path.join(INTERFACE, 'style.css'), 'utf8');
const RENDERER = fs.readFileSync(path.join(INTERFACE, 'renderer.js'), 'utf8');

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

/** Strip comments so prose about durations is never mistaken for a rule. */
const CSS_RULES = CSS.replace(/\/\*[\s\S]*?\*\//g, '');

// ==========================================================
// 1. THE TOKENS
// ==========================================================
function testTokens() {
    const tokens = {};

    // The reduced-motion block redefines the same names at ~0, so only the
    // primary declarations are read here — the override is checked separately.
    const primary = CSS_RULES.replace(/@media\s*\(prefers-reduced-motion[\s\S]*?\n\}/g, '');

    for (const [, name, value] of primary.matchAll(/(--(?:motion|ease)-[a-z-]+)\s*:\s*([^;]+);/g)) {
        tokens[name] = value.trim();
    }

    for (const name of ['--motion-fast', '--motion-normal', '--motion-slow']) {
        check(`${name} is defined`, Boolean(tokens[name]), 'missing');
    }

    for (const name of ['--ease-standard', '--ease-out', '--ease-in']) {
        check(`${name} is defined`, Boolean(tokens[name]), 'missing');
        check(
            `${name} does not overshoot`,
            !/cubic-bezier\([^)]*-\d/.test(tokens[name] || ''),
            tokens[name]
        );
    }

    // 120–240ms: fast enough not to feel like latency, slow enough to read.
    for (const name of ['--motion-fast', '--motion-normal', '--motion-slow']) {
        const ms = Number(String(tokens[name] || '').replace('ms', ''));
        check(
            `${name} is within the 120-240ms band`,
            Number.isFinite(ms) && ms >= 120 && ms <= 240,
            tokens[name]
        );
    }

    // The legacy names must ALIAS the tokens. If they carry their own literal
    // values again there are two systems, and retuning one leaves the other.
    for (const [legacy, expected] of [['--dur-fast', 'motion-fast'], ['--dur-base', 'motion-normal'], ['--dur-slow', 'motion-slow']]) {
        const declared = new RegExp(`${legacy}\\s*:\\s*var\\(--${expected}\\)`).test(CSS_RULES);
        check(`${legacy} aliases var(--${expected}) rather than duplicating it`, declared);
    }
}

// ==========================================================
// 2. NO STRAY DURATIONS
// ==========================================================
function testNoStrayDurations() {
    // Signature and data-driven motion, deliberately outside the system: the orb
    // travelling to its dock, audio meters tracking real level, progress fills
    // tracking real playback position, showcase scene changes.
    const ALLOWED_LONG = 0.28;
    const offenders = [];

    for (const [, body] of CSS_RULES.matchAll(/transition:([^;{}]*);/g)) {
        if (body.includes('linear')) continue;

        for (const [, literal] of body.matchAll(/(?<![\d.])(\d*\.?\d+)s(?!\w)/g)) {
            const seconds = Number(literal);

            if (seconds > 0 && seconds < ALLOWED_LONG) {
                offenders.push(`${literal}s in "${body.trim().slice(0, 60)}"`);
            }
        }
    }

    check(
        'no UI-chrome transition uses a literal duration instead of a token',
        offenders.length === 0,
        offenders.slice(0, 3).join(' | ')
    );

    const tokenUses = (CSS_RULES.match(/var\(--(?:motion|dur|ease)-[a-z-]+\)/g) || []).length;
    check('the tokens are actually used across the stylesheet', tokenUses > 200, `${tokenUses} references`);
}

// ==========================================================
// 3. EVERY EXIT GOES THROUGH THE HELPER
// ==========================================================
function testCloseContract() {
    check(
        'the shared close helper exists',
        /function closeElementWithMotion\s*\(/.test(RENDERER)
    );

    // Every loop over windows that REMOVES one. Loops that only measure or
    // reposition are not interesting here, so the filter is on `.remove()`
    // appearing inside the block rather than on the selector.
    const loops = [...RENDERER.matchAll(/querySelectorAll\('(\.hud-card[^']*)'\)\s*\.forEach\(([\s\S]{0,400}?)\n\s{0,8}\}\s*\)\s*;/g)];
    const removing = loops.filter(([, , body]) => /\.remove\(\)/.test(body));

    check(
        'no loop over windows removes one without playing it out',
        removing.length === 0,
        removing.map(([, selector]) => selector).join(' | ')
    );

    // Loops that DO play windows out must skip ones already on their way, or a
    // reconcile arriving mid-close restarts the animation on a dying node.
    const closing = loops.filter(([, , body]) => /closeElementWithMotion/.test(body));
    const unguarded = closing.filter(([, selector]) => !selector.includes(':not([data-closing])'));

    check('window close loops were found', closing.length >= 2, `${closing.length} found`);
    check(
        'every close loop skips windows that are already closing',
        unguarded.length === 0,
        unguarded.map(([, selector]) => selector).join(' | ')
    );

    // The same for the bulk helper's callers.
    const bulk = [...RENDERER.matchAll(/closeAllWithMotion\([^,]+,\s*'([^']+)'/g)].map((m) => m[1]);
    const unguardedBulk = bulk.filter((selector) => !selector.includes(':not([data-closing])'));
    check(
        'bulk closes also skip windows that are already closing',
        unguardedBulk.length === 0,
        unguardedBulk.join(' | ')
    );

    // Clearing the workspace must not blank the container out from under the
    // windows that are mid-animation.
    check(
        'clearing the workspace plays each window out rather than emptying the container',
        !/container\.innerHTML\s*=\s*''/.test(RENDERER)
    );

    // Lookups must not adopt a dying node as a live one.
    const lookups = RENDERER.match(/\.hud-card[^`'"]*\[data-card-id=/g) || [];
    const unsafe = lookups.filter((l) => !l.includes(':not([data-closing])'));
    check(
        'window lookups never match a window that is closing',
        unsafe.length === 0,
        unsafe.join(' | ')
    );

    // The command must not wait for the animation.
    const closeButton = /closeButton\.addEventListener\('click'[\s\S]*?\n    \}\);/.exec(RENDERER);
    check('the close button handler was found', Boolean(closeButton));
    check(
        'the close button tells the backend immediately, not after the animation',
        Boolean(closeButton) && !/setTimeout[\s\S]*socket\.emit\('close_hud_card'/.test(closeButton[0]),
        'close is delayed behind a timer'
    );
}

// ==========================================================
// 4-6. THE HELPER ITSELF
// ==========================================================
/** Slice the MOTION section out of renderer.js and run it against stubs. */
function loadMotionHelpers() {
    const start = RENDERER.indexOf('// MOTION\n');
    const end = RENDERER.indexOf('function beginWindowGesture');

    if (start === -1 || end === -1 || end <= start) {
        throw new Error('could not locate the MOTION section in renderer.js');
    }

    const timers = [];
    const context = {
        console: { log: () => {}, warn: () => {}, error: () => {} },
        window: {
            setTimeout: (fn, ms) => timers.push({ fn, ms })
        },
        Number,
        String,
        Boolean,
        Object
    };
    context.globalThis = context;

    vm.runInNewContext(RENDERER.slice(start, end), context, { filename: 'renderer.js#motion' });
    return { context, timers };
}

function makeElement(overrides = {}) {
    const listeners = {};
    const element = {
        dataset: {},
        isConnected: true,
        offsetParent: {},
        offsetHeight: 100,
        removed: false,
        classes: new Set(),
        classList: {
            add: (name) => element.classes.add(name),
            remove: (name) => element.classes.delete(name),
            contains: (name) => element.classes.has(name)
        },
        addEventListener: (name, fn) => { (listeners[name] = listeners[name] || []).push(fn); },
        removeEventListener: (name, fn) => {
            listeners[name] = (listeners[name] || []).filter((f) => f !== fn);
        },
        remove: () => { element.removed = true; element.isConnected = false; },
        fire: (name, event) => (listeners[name] || []).slice().forEach((fn) => fn(event)),
        listenerCount: (name) => (listeners[name] || []).length,
        ...overrides
    };
    return element;
}

async function testCloseHelper() {
    const { context, timers } = loadMotionHelpers();
    const { closeElementWithMotion } = context;

    // Plays out, then removes.
    const element = makeElement();
    closeElementWithMotion(element);

    check('closing marks the element synchronously', element.dataset.closing === 'true');
    check('closing applies the exit class', element.classes.has('widget-closing'));
    check('the element is NOT removed before the animation runs', element.removed === false);

    element.fire('transitionend', { target: element, propertyName: 'transform' });
    check('a transform finishing does not end the close early', element.removed === false);

    element.fire('transitionend', { target: {}, propertyName: 'opacity' });
    check("a child's transition does not end the close early", element.removed === false);

    element.fire('transitionend', { target: element, propertyName: 'opacity' });
    check('the element is removed once its own fade completes', element.removed === true);
    check('the transition listener is cleaned up', element.listenerCount('transitionend') === 0);

    // Idempotent: a second click, or a reconcile arriving mid-close, must not
    // restart the animation or remove the node twice.
    const twice = makeElement();
    closeElementWithMotion(twice);
    const classesAfterFirst = twice.classes.size;
    closeElementWithMotion(twice);
    check('closing an element twice is ignored', twice.classes.size === classesAfterFirst);

    let removals = 0;
    const counted = makeElement({ remove() { removals += 1; this.isConnected = false; } });
    closeElementWithMotion(counted);
    counted.fire('transitionend', { target: counted, propertyName: 'opacity' });
    counted.fire('transitionend', { target: counted, propertyName: 'opacity' });
    check('an element is never removed twice', removals === 1, `${removals} removals`);

    // A window that never fires transitionend — a hidden tab, a display:none
    // ancestor — must still go away.
    const stuck = makeElement();
    closeElementWithMotion(stuck);
    check('a fallback timer is armed', timers.length > 0);

    const fallback = timers[timers.length - 1];
    check('the fallback is longer than the animation but still short', fallback.ms >= 240 && fallback.ms <= 800, `${fallback.ms}ms`);
    fallback.fn();
    check('a window that never reports transitionend is removed anyway', stuck.removed === true);

    // An element that was never rendered should not wait on a transition that
    // will never start.
    const offscreen = makeElement({ offsetParent: null, offsetHeight: 0 });
    closeElementWithMotion(offscreen);
    check('an element that was never visible is removed immediately', offscreen.removed === true);

    // onRemoved fires exactly once, after removal.
    let notified = 0;
    const notifying = makeElement();
    closeElementWithMotion(notifying, { onRemoved: () => { notified += 1; } });
    notifying.fire('transitionend', { target: notifying, propertyName: 'opacity' });
    check('the caller is notified once the element is gone', notified === 1, `${notified}`);
}

// ==========================================================
// 7-8. GESTURES AND REDUCED MOTION
// ==========================================================
function testGesturesAndReducedMotion() {
    check(
        'a window being dragged or resized has motion disabled',
        /\.hud-card\.dragging[\s\S]{0,200}?transition:\s*none\s*!important/.test(CSS_RULES),
        'no gesture exemption found'
    );

    const reduced = /@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{([\s\S]*?)\n\}/g;
    const blocks = [...CSS_RULES.matchAll(reduced)].map((m) => m[1]);
    check('reduced motion is answered', blocks.length > 0);

    const global = blocks.some((b) => /\*,?\s*[\s\S]*?transition-duration:\s*1ms/.test(b));
    check(
        'reduced motion is answered globally rather than per component',
        global,
        'no universal rule found'
    );

    // The renderer waits for transitionend before removing a closed window.
    // `transition: none` would mean that event never fires and the window would
    // stay on screen forever, so durations must collapse to ~0, not to nothing.
    const killsTransitions = blocks.some((b) => /\*[\s\S]{0,120}transition:\s*none/.test(b));
    check(
        'reduced motion shortens transitions instead of removing them, so close still completes',
        !killsTransitions,
        'a universal `transition: none` would strand closing windows'
    );
}

// ==========================================================
// 9. HIDDEN PANELS MUST NOT MOVE THE WORKSTATION BOUNDARY
// ==========================================================
// The window manager decides where the workstation begins by measuring the
// bottom of every element in WORKSTATION_TOP_CONTROL_SELECTORS. Those shells
// contain popovers, so a popover that is merely INVISIBLE rather than out of
// flow still grows its shell — and the top boundary for every widget moves down
// by the height of a panel nobody can see.
//
// That is not hypothetical. Giving the notification centre a fade meant changing
// it from `display: none` to `display: flex`, which put a 520px invisible panel
// back into the launcher shell and pushed the widget boundary to the middle of
// the screen. Widgets could be dragged around the lower half and nowhere else.
//
// `display: none` used to guarantee this for free. Now it has to be asserted.
function testHiddenPanelsAreOutOfFlow() {
    const listed = /WORKSTATION_TOP_CONTROL_SELECTORS\s*=\s*\[([^\]]+)\]/.exec(RENDERER);
    check('the measured top controls were found', Boolean(listed));

    const selectors = listed ? [...listed[1].matchAll(/'([^']+)'/g)].map((m) => m[1]) : [];
    check('the notification launcher is one of them', selectors.includes('.notification-launcher-shell'));

    // Every popover that lives inside a measured shell.
    for (const panel of ['.notification-center-panel', '.widget-launcher-panel']) {
        // A selector can be declared more than once — .widget-launcher-panel is —
        // so the union of its rules is what actually applies. Reading only the
        // first would report a panel as unpositioned when a later rule positions
        // it perfectly well.
        const rules = [...CSS_RULES.matchAll(new RegExp(`(?:^|[},])\\s*\\${panel}\\s*\\{([^}]*)\\}`, 'gm'))];
        check(`${panel} has at least one rule`, rules.length > 0, `${rules.length} rules`);

        if (!rules.length) continue;

        const body = rules.map((r) => r[1]).join(';');
        check(
            `${panel} is positioned, so a hidden panel cannot grow its shell`,
            /position:\s*(absolute|fixed)/.test(body),
            (/position:\s*\w+/.exec(body) || ['no position declared'])[0]
        );
        check(
            `${panel} does not sit in flow while hidden`,
            !/display:\s*none/.test(body),
            'display:none would remove its motion'
        );
    }
}

async function main() {
    const tests = [
        ['motion tokens', testTokens],
        ['no stray durations', testNoStrayDurations],
        ['close contract', testCloseContract],
        ['the close helper', testCloseHelper],
        ['gestures and reduced motion', testGesturesAndReducedMotion],
        ['hidden panels stay out of flow', testHiddenPanelsAreOutOfFlow]
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
    console.log('Motion system: ' + PASSED.length + ' passed, ' + FAILED.length + ' failed');

    if (FAILED.length) {
        FAILED.forEach((name) => console.log('  failed: ' + name));
    }

    process.exit(FAILED.length ? 1 : 0);
}

main();
