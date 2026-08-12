/**
 * FRIDAY OS — top system bar.
 *
 * Deliberately standalone: it reads state that already exists and never writes to
 * the voice layer, the socket, or the widget system. Nothing here can affect what
 * FRIDAY hears or says, which is why it can sit alongside the live pipeline safely.
 *
 * Sources, all read-only:
 *   clock     — the machine clock
 *   battery   — navigator.getBattery(), which Chromium exposes inside Electron
 *   network   — navigator.onLine plus the Network Information API where present
 *   island    — the existing #status-label, observed rather than hooked, so the
 *               renderer's voice-phase code stays untouched
 *   cost      — elapsed time the live session has spent connected
 */

(function () {
    'use strict';

    // ── Session cost ──────────────────────────────────────────────────────────
    // Gemini Live bills by audio duration rather than tokens, so connected time is
    // the right basis. The RATE BELOW IS A PLACEHOLDER — set the real published
    // per-minute price for the model in use before treating the figure as money:
    //
    //     localStorage.setItem('friday-live-rate-usd-min', '0.0123')
    //
    // Until then the bar prefixes the value with ~ to mark it an estimate.
    const DEFAULT_RATE_USD_PER_MINUTE = 0.06;
    const COST_TICK_MS = 1000;

    function ratePerMinute() {
        const stored = Number(localStorage.getItem('friday-live-rate-usd-min'));
        return Number.isFinite(stored) && stored > 0 ? stored : DEFAULT_RATE_USD_PER_MINUTE;
    }

    /** USD per million tokens. 0 means "unknown", and no cost is then claimed. */
    function tokenRate() {
        const stored = Number(localStorage.getItem('friday-live-rate-usd-mtok'));
        return Number.isFinite(stored) && stored > 0 ? stored : 0;
    }

    function formatTokens(count) {
        if (count >= 1e6) return `${(count / 1e6).toFixed(2)}M`;
        if (count >= 1e3) return `${(count / 1e3).toFixed(1)}k`;
        return String(count);
    }

    let connectedSeconds = 0;

    function liveSessionActive() {
        // `session.ready` is the flag set when setupComplete actually arrives.
        //
        // This used to test session.state against a list of names. The state
        // string lags and does not always settle on 'active' — a rotating socket
        // sits in 'setup_pending' while fully live — so the counter frequently
        // never accrued at all and the bar stayed pinned at zero.
        const session = window.__fridayVoice?.session;
        return Boolean(session && session.ready && !window.__fridayVoice?.suspended);
    }

    /** Real metered token counts from the Live API, or null when none reported. */
    function meteredUsage() {
        const usage = window.__fridayVoice?.getUsage?.();
        return usage && usage.metered ? usage : null;
    }

    /**
     * Three decimals and an explicit "~".
     *
     * Two decimals rendered every sub-cent session as a flat $0.00, which read as
     * "nothing is being counted" rather than "less than a cent". The tilde is not
     * decoration: this figure is derived from CONNECTED TIME, not from metered
     * audio or token usage, so it is an estimate of the right order of magnitude
     * and nothing more.
     */
    function formatCost(dollars) {
        if (!Number.isFinite(dollars) || dollars <= 0) {
            return '~$0.000';
        }

        return `~$${dollars.toFixed(dollars < 100 ? 3 : 2)}`;
    }

    // ── Markup ────────────────────────────────────────────────────────────────

    function buildBar() {
        const bar = document.createElement('div');
        bar.id = 'system-bar';
        bar.className = 'system-bar';
        bar.setAttribute('role', 'banner');
        bar.setAttribute('aria-label', 'FRIDAY system status');

        bar.innerHTML = `
            <div class="system-bar-left">
                <span class="sysbar-brand">FRIDAY</span>
                <span class="sysbar-state" data-state="online">
                    <span class="sysbar-state-dot"></span>
                    <span class="sysbar-state-text">ONLINE</span>
                </span>
            </div>

            <div class="system-bar-center">
                <div class="dynamic-island" id="dynamic-island" data-mode="idle" aria-live="polite">
                    <span class="island-glyph"></span>
                    <span class="island-text">Standing by</span>
                </div>
            </div>

            <div class="system-bar-right">
                <span class="sysbar-item" id="sysbar-network" title="Network">
                    <span class="sysbar-icon" data-icon="wifi"></span>
                    <span class="sysbar-value">—</span>
                </span>
                <span class="sysbar-item" id="sysbar-battery" title="Battery">
                    <span class="sysbar-battery-shell"><span class="sysbar-battery-fill"></span></span>
                    <span class="sysbar-value">—</span>
                </span>
                <span class="sysbar-item sysbar-cost" id="sysbar-cost"
                      title="Approximate Gemini Live spend this session">
                    <span class="sysbar-value">$0.00</span>
                </span>
                <span class="sysbar-item sysbar-clock" id="sysbar-clock">
                    <span class="sysbar-value">--:--</span>
                </span>
            </div>
        `;

        return bar;
    }

    // ── Clock ─────────────────────────────────────────────────────────────────

    function startClock(root) {
        const value = root.querySelector('#sysbar-clock .sysbar-value');

        const tick = () => {
            value.textContent = new Date().toLocaleTimeString([], {
                hour: 'numeric',
                minute: '2-digit'
            });
        };

        tick();
        setInterval(tick, 1000);
    }

    // ── Battery ───────────────────────────────────────────────────────────────

    function startBattery(root) {
        const item = root.querySelector('#sysbar-battery');
        const value = item.querySelector('.sysbar-value');
        const fill = item.querySelector('.sysbar-battery-fill');

        if (typeof navigator.getBattery !== 'function') {
            item.style.display = 'none';
            return;
        }

        navigator.getBattery().then((battery) => {
            const render = () => {
                const percent = Math.round((battery.level || 0) * 100);
                value.textContent = `${percent}%`;
                fill.style.width = `${Math.max(4, percent)}%`;
                item.dataset.charging = battery.charging ? 'true' : 'false';
                item.dataset.low = !battery.charging && percent <= 20 ? 'true' : 'false';
                item.title = battery.charging
                    ? `Battery ${percent}% — charging`
                    : `Battery ${percent}%`;
            };

            render();
            battery.addEventListener('levelchange', render);
            battery.addEventListener('chargingchange', render);
        }).catch(() => {
            item.style.display = 'none';
        });
    }

    // ── Network ───────────────────────────────────────────────────────────────

    function startNetwork(root) {
        const item = root.querySelector('#sysbar-network');
        const value = item.querySelector('.sysbar-value');

        // Only two states are actually knowable from here: reachable or not.
        //
        // This used to print navigator.connection.effectiveType — "4G", "3G". That
        // value is a BANDWIDTH CLASS, an estimate of throughput, and says nothing
        // about the transport. On a Mac with no cellular radio it rendered as a
        // cellular connection that does not exist. Anything more specific than
        // "Wi-Fi" needs the OS to tell us, and it is not being asked.
        const render = () => {
            const online = navigator.onLine !== false;
            item.dataset.online = online ? 'true' : 'false';
            value.textContent = online ? 'Wi-Fi' : 'Offline';
            item.title = online ? 'Network reachable' : 'No network connection';
        };

        render();
        window.addEventListener('online', render);
        window.addEventListener('offline', render);
    }

    // ── Session cost ──────────────────────────────────────────────────────────

    function startCost(root) {
        const item = root.querySelector('#sysbar-cost');
        const value = item.querySelector('.sysbar-value');

        setInterval(() => {
            const active = liveSessionActive();

            if (active) {
                connectedSeconds += COST_TICK_MS / 1000;
            }

            item.dataset.active = active ? 'true' : 'false';

            // Prefer what the API actually metered over anything we infer.
            const usage = meteredUsage();

            if (usage) {
                const rate = tokenRate();

                if (rate > 0) {
                    // Real tokens x a rate the user supplied: a real figure.
                    value.textContent = `$${((usage.totalTokens / 1e6) * rate).toFixed(3)}`;
                    item.title = `${usage.totalTokens.toLocaleString()} tokens metered by the Live API `
                        + `at $${rate}/million.\n`
                        + Object.entries(usage.byModality)
                            .map(([k, v]) => `  ${k}: ${v.toLocaleString()}`).join('\n');
                } else {
                    // Real usage exists but no price to value it at. Showing the
                    // real number beats inventing a dollar amount.
                    value.textContent = `${formatTokens(usage.totalTokens)} tok`;
                    item.title = `${usage.totalTokens.toLocaleString()} tokens metered by the Live API.\n`
                        + 'No price is configured, so no cost is shown. Set one with:\n'
                        + "localStorage.setItem('friday-live-rate-usd-mtok', '<usd per million tokens>')\n"
                        + Object.entries(usage.byModality)
                            .map(([k, v]) => `  ${k}: ${v.toLocaleString()}`).join('\n');
                }

                return;
            }

            // Nothing metered yet. A correct zero — not an estimate dressed up as one.
            value.textContent = '$0.000';
            item.title = active
                ? 'Live session connected. No usage reported by the API yet.'
                : 'No live session. Nothing has been used this session.';
        }, COST_TICK_MS);
    }

    // ── Dynamic island ────────────────────────────────────────────────────────
    // Mirrors the voice phase the renderer already publishes into #status-label.
    // Observing it keeps this module completely decoupled from the voice code.

    const ISLAND_MODES = {
        IDLE: { mode: 'idle', text: 'Standing by' },
        OFFLINE: { mode: 'offline', text: 'Voice offline' },
        LISTENING: { mode: 'listening', text: 'Listening' },
        USER_SPEAKING: { mode: 'listening', text: 'Listening' },
        THINKING: { mode: 'thinking', text: 'Thinking' },
        FRIDAY_SPEAKING: { mode: 'speaking', text: 'Speaking' },
        SPEAKING: { mode: 'speaking', text: 'Speaking' }
    };

    function startIsland(root) {
        const island = root.querySelector('#dynamic-island');
        const text = island.querySelector('.island-text');
        const label = document.getElementById('status-label');
        const core = document.getElementById('core-container');

        const render = () => {
            // Voice health outranks the phase label. A dead microphone with the
            // orb still saying IDLE is precisely the misleading state to avoid:
            // "standing by" implies she is hearing you, and she is not.
            const health = window.__fridayVoiceHealth;
            const voiceDead = health && health.ok === false && !health.suspended;

            const raw = voiceDead
                ? 'OFFLINE'
                : String(core?.dataset?.status || label?.textContent || 'IDLE').trim().toUpperCase();
            const next = ISLAND_MODES[raw] || ISLAND_MODES.IDLE;

            island.title = voiceDead && health.reason
                ? `Voice offline — ${health.reason}`
                : '';

            if (island.dataset.mode !== next.mode) {
                island.dataset.mode = next.mode;
                // Retrigger the entrance so a phase change reads as a change.
                island.classList.remove('island-pulse');
                void island.offsetWidth;
                island.classList.add('island-pulse');
            }

            text.textContent = next.text;
        };

        render();

        if (typeof MutationObserver === 'function') {
            const observer = new MutationObserver(render);

            if (label) {
                observer.observe(label, { childList: true, characterData: true, subtree: true });
            }

            if (core) {
                observer.observe(core, { attributes: true, attributeFilter: ['data-status'] });
            }
        }

        setInterval(render, 2000);
    }

    // ── Boot ──────────────────────────────────────────────────────────────────

    function init() {
        if (document.getElementById('system-bar')) {
            return;
        }

        // The workshop displays are secondary surfaces; only the primary HUD wears
        // the system bar, exactly as a second monitor has no menu bar of its own.
        if (document.body.dataset.interfaceMode === 'workshop') {
            return;
        }

        const bar = buildBar();
        document.body.appendChild(bar);
        document.body.classList.add('has-system-bar');

        startClock(bar);
        startBattery(bar);
        startNetwork(bar);
        startCost(bar);
        startIsland(bar);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
