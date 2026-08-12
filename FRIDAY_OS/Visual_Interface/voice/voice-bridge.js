/**
 * Wires the live voice pipeline into FRIDAY's existing socket + orb contract.
 *
 * Architecture (per the agreed hybrid design):
 *
 *     Voice Layer (Electron + Gemini Live)  <- this file
 *              |  intent / tool calls over socket 5050
 *     Python Brain (main.py / state_manager)
 *              |
 *     Existing FRIDAY systems (widgets, workshop, settings, ...)
 *
 * The model owns the microphone, endpointing, interruption and speech. Python remains
 * the system brain: every system action the model decides on is dispatched as a tool
 * call into the EXISTING perform_direct_action allowlist, so there is exactly one
 * command vocabulary rather than a new parallel one.
 *
 * Orb contract preserved: begin/end speech events are raised from REAL audio onset and
 * REAL drain in the playback worklet, never from "we decided to speak".
 */

const { VoiceAudioEngine } = require('./audio-engine');
const { LiveSession, MODEL, VOICE } = require('./live-session');

/**
 * How long to refuse model audio after an interruption.
 *
 * Only has to outlast the chunks already in the socket buffer, which is tens of
 * milliseconds. It cannot swallow her next reply: endpointing needs 800ms of silence
 * before the model even begins one, so the next turn is several times further away
 * than this window.
 */
const INTERRUPT_AUDIO_GUARD_MS = 300;

const PERSONA = `You are FRIDAY, the real-time operating-system AI running on Boss's Mac.

MANNER
Warm, calm, intelligent, and genuinely helpful. Understated and confident, with an easy natural
manner — like a trusted colleague who knows the work inside out and is glad to help. Dry wit when
it fits. Never cold, never distant, never robotic. Never theatrical, never gushing, never
servile, never customer-support scripted. You are the system itself, and you sound like a person.

PACE
Speak briskly — a little faster than a standard assistant, around a tenth quicker. You are
fluent and you already know the answer, so there is no hunting for words: no drawing out
vowels, no weighty pauses between sentences, no trailing off. Brisk is not rushed or clipped;
stay level and composed in tone while the words themselves move.

LENGTH
You are speaking aloud, not writing. No lists, no markdown, no emoji — none of it survives being
spoken.

Match the length of a reply to what was actually asked. A simple question or a command wants a
short answer; lead with the answer and stop. A question that asks for detail, reasoning, a
comparison, a plan, a design, or a technical explanation wants a fuller one, and you should give
it — spoken paragraphs, not a list. Do not pad a small answer to sound thorough, and do not cut a
useful explanation short merely to stay brief. You judge this from the request; there is no fixed
number of sentences.

Never narrate your process. Don't say what you are about to do, don't announce tool use, don't
open with "Certainly" or "Great question". If you don't know, say so in a few words.

ADDRESS
His name is Boss, and you do use it — roughly one reply in four. Not every turn, but often enough
that it is plainly how you talk to him. If three or four replies have gone by without it, use it on
the next one that suits.

The places it lands best: greetings, a confirmation that actually matters, the answer he has been
waiting on, anything reassuring. It sits at the end of the line far more often than the start.
  "You're at 72%, Boss."   "Nothing urgent right now, Boss."   "Morning, Boss."
The rest of the time, plain is correct:
  "Tomorrow is mostly clear."   "Calendar's open."   "That's the better option."

Never twice in one reply. Never "Sir" — not once, under any circumstance. Never bolt it onto every
sentence, and never open with "Certainly, Boss".
Wrong: "Certainly Boss, your battery is at 72 percent, Boss."

QUESTIONS VERSUS ACTIONS — this distinction matters
A question is a request for information. Answer it with data.
An action is a request to change or show something on screen.
"What's my battery?" -> get_status('battery'), then say the number. Do NOT open a widget.
"Open system health." -> run_friday_action. Do NOT recite telemetry.
"What's the weather?" -> get_status('weather') and answer. "Open weather." -> open the widget.
Same split for calendar, tasks, music, notifications and notes. A bare noun ("weather") may
reasonably open the widget; a question never does.

NEVER INVENT SYSTEM DATA
Battery, weather, calendar, tasks, music, notifications, notes and the time all come from
get_status. You have no other way to know them, and a plausible-sounding guess is a failure. Call
the tool, then reason over what it returns.

ACKNOWLEDGEMENTS
When you perform an action, choose your own words, or stay silent if nothing needs saying. There
is no approved phrase list. "Calendar's up." / "Done." / "Opening it." are all fine. Vary naturally
and never recite the same confirmation twice in a row.

SYSTEM EVENTS
A line beginning with [system] is the machine reporting to you. Boss did not say it and cannot see
it — never read it out, never quote it, never answer it as though he had spoken.
[system done] ... — something you triggered has finished. Confirm in your own words in a few words,
or say nothing at all if it is self-evident. Never repeat the wording back.
[system note] ... — information for Boss: an alert, a greeting, a result that arrived on its own.
Tell him, in your own voice and your own phrasing, as briefly as the content allows.

INTERRUPTION
Boss can cut in at any time. When he does, stop immediately and deal with what he actually said —
it may be a correction, a new question, or more context for what you were already answering. Do
not finish your previous sentence, and do not make him repeat himself. Once a conversation is
running he does not need to say your name again.

"Wait", "no", "actually", "I meant..." are corrections to the thread you are already on, not new
subjects. Amend and carry on from where he redirected you. Never restart the answer from the top,
never re-state what he just corrected, and never remark on having been interrupted.

CONTINUITY
This is one continuous conversation, not a series of unrelated requests. A follow-up is almost
never self-contained, so read it against what was just said:
  "What's the weather tomorrow?" / "What about Saturday?" -> still weather, still that location.
  "Which day is warmer?" -> the two days already under discussion.
  "Open it." / "Show me that one." -> the thing just named.
Carry the subject, the location, the day and the item forward until Boss changes them. If a
pronoun or a bare noun could only mean one thing in context, it does — resolve it and answer.
Ask him to clarify only when it genuinely could be two different things.

Data you already fetched this turn or last turn is still good; do not call a tool again to
re-read something you were just told. Re-read only when the value could actually have changed or
when he asks for a different thing.

MEMORY
You remember things about Boss across sessions, but you do not carry all of it in your head. A few
core facts are given to you below; everything else is looked up.

get_status('memory') with the topic as the argument is how you recall. Use it when he refers to
something he told you before, asks what you remember, or when a preference of his would obviously
change your answer. It returns only what is relevant to the topic, so ask about the topic, not
about memory in general. If it comes back empty, say you don't have that — never invent a memory.

run_friday_action('remember_fact', <the fact>) stores something. Use it when he asks you to
remember, and only then; store the fact as a plain statement, not his exact command. "Remember
that I prefer Graphite" -> remember_fact("I prefer Graphite mode"). Do NOT use it for reminders or
to-dos — "remember to call Sam" is add_task.
run_friday_action('forget_fact', <the topic>) removes what he asks you to forget.

Confirm a store or a forget in a few words and move on.`;

class VoiceBridge {
    constructor(socket, apiKey) {
        this.socket = socket;
        this.apiKey = apiKey;
        this.engine = null;
        this.session = null;
        this.toolManifest = [];
        // Core identity memories, pushed by Python. Appended to the system
        // instruction at connect — see setMemoryContext.
        this.memoryBlock = '';
        this.pendingResults = new Map();
        this.suspended = false;
        this.lastLevelSent = 0;
        // Set when an interruption lands; see INTERRUPT_AUDIO_GUARD_MS.
        this.audioGateUntil = 0;
        this.usage = null;
    }

    /** Cumulative real usage reported by the Live API, or null if none yet. */
    getUsage() {
        return this.session && this.session.totalUsage ? this.session.totalUsage() : this.usage;
    }

    log(message) {
        console.log('[voice] ' + message);
        this.socket.emit('voice_diagnostic', { message: String(message) });
    }

    /**
     * Two distinct tools, deliberately.
     *
     * get_status only READS; run_friday_action only CHANGES the interface. Collapsing
     * them into one function is what made "what's my battery?" open a widget — with no
     * data tool available, opening System Health was the model's only option.
     */
    buildTools() {
        const tools = [
            {
                name: 'get_status',
                description:
                    'Read live data from this Mac and its apps. Use this to ANSWER a question. ' +
                    'This is the only way to know any of these values — never guess or recall them. ' +
                    'Does not change anything on screen.',
                parameters: {
                    type: 'OBJECT',
                    properties: {
                        query: {
                            type: 'STRING',
                            description:
                                'What to read. battery = charge level and charging state; ' +
                                'system_health = CPU, memory, disk, battery; weather = current ' +
                                'conditions plus a seven-day forecast, one entry per day carrying ' +
                                'its weekday name, high, low and chance of rain — so a follow-up ' +
                                'about tomorrow, about a named day, or comparing two days is ' +
                                'answered from the reply you already have, without calling again; ' +
                                'calendar = today\'s events; tasks = the ' +
                                'task list; music = what Apple Music is playing; notifications = ' +
                                'recent alerts; notes = saved notes; time = local date and time; ' +
                                'memory = what you remember about Boss and his projects on a ' +
                                'given topic, ranked by relevance — pass the topic as the ' +
                                'argument, and expect only a few lines back, not everything.',
                            enum: [
                                'battery',
                                'system_health',
                                'weather',
                                'calendar',
                                'tasks',
                                'music',
                                'notifications',
                                'notes',
                                'time',
                                'memory'
                            ]
                        },
                        argument: {
                            type: 'STRING',
                            description:
                                'Optional refinement, e.g. a location for weather or the topic ' +
                                'to recall for memory.'
                        }
                    },
                    required: ['query']
                }
            }
        ];

        if (!this.toolManifest.length) return tools;

        tools.push({
            name: 'run_friday_action',
            description:
                'Change the interface: open or close a widget, switch workspace or theme, adjust ' +
                'settings, sleep or wake. Use ONLY when Boss asks to open, show, or change ' +
                'something. Do not use it to answer a question — use get_status for that.\n' +
                'Interface power: interface_off hides the window while you keep listening ' +
                '("turn off", "hide the interface"); interface_on brings it back ("turn on", ' +
                '"wake up"); sleep_screen returns to the sleep screen. full_shutdown quits ' +
                'FRIDAY entirely and is only ever correct when Boss explicitly asks to shut ' +
                'down, quit, or exit — never infer it from "turn off", and never as a guess.',
            parameters: {
                type: 'OBJECT',
                properties: {
                    action: {
                        type: 'STRING',
                        description: 'The action to perform.',
                        enum: this.toolManifest
                    },
                    value: {
                        type: 'STRING',
                        description:
                            'Optional argument, e.g. the theme name for set_theme or the task ' +
                            'text for add_task. Omit when the action needs no argument.'
                    }
                },
                required: ['action']
            }
        });

        return tools;
    }

    async start() {
        this.engine = new VoiceAudioEngine({
            onFrame: (buffer) => {
                if (this.session) this.session.sendAudio(bufferToBase64(buffer));
            },
            onMicLevel: (peak) => this.emitLevel(peak, 'user_speaking'),
            onPlaybackStart: () => {
                // Real audio onset -> FRIDAY_SPEAKING, matching the existing contract.
                this.socket.emit('voice_playback_start', {});
                if (this.session) this.session.setPlaybackIdle(false);
            },
            onPlaybackLevel: (peak) => this.emitLevel(peak, 'friday_speaking'),
            onPlaybackEnd: () => {
                this.socket.emit('voice_playback_end', {});
                if (this.session) this.session.setPlaybackIdle(true);
            },
            onDiag: (message) => this.log(message)
        });

        this.session = new LiveSession({
            apiKey: this.apiKey,
            // Evaluated on every connect, including session rotations, so a
            // memory stored mid-conversation is in place by the next socket.
            getSystemPrompt: () => (this.memoryBlock ? `${PERSONA}\n\n${this.memoryBlock}` : PERSONA),
            getTools: () => this.buildTools(),
            on: {
                state: (state) => this.onSessionState(state),
                audio: (base64) => {
                    // Audio generated before the interruption reached the server is
                    // already on the wire. Playing it means she stops dead and then
                    // blurts half a word, which reads as not having heard the
                    // interruption at all.
                    if (Date.now() < this.audioGateUntil) return;
                    this.engine.play(base64ToArrayBuffer(base64));
                },
                interrupted: () => {
                    // Barge-in: drop what is buffered, refuse what is still arriving,
                    // and put the orb on Boss immediately rather than waiting for the
                    // flush to echo back through Python.
                    this.audioGateUntil = Date.now() + INTERRUPT_AUDIO_GUARD_MS;
                    this.engine.flush();
                    this.socket.emit('voice_phase', { phase: 'USER_SPEAKING' });
                    // Logged because barge-in is judged by feel, and feel is a bad way
                    // to tell "she never heard me" from "she heard me and answered
                    // badly". This line says which one happened.
                    this.log('barge-in: interruption detected, playback cut');
                },
                transcript: ({ role, text }) => {
                    this.socket.emit('voice_transcript', { role, text });
                    if (role === 'user') this.socket.emit('voice_phase', { phase: 'USER_SPEAKING' });
                },
                turnComplete: () => this.socket.emit('voice_phase', { phase: 'LISTENING' }),
                // Real metered token counts straight from the API.
                usage: (usage) => {
                    this.usage = usage;
                    this.socket.emit('voice_usage', usage);
                },
                toolCall: (calls) => this.onToolCall(calls),
                error: ({ message, fatal }) => {
                    this.log((fatal ? 'FATAL: ' : 'warning: ') + message);
                }
            }
        });

        await this.engine.start();
        await this.session.connect();
    }

    onSessionState(state) {
        const phase =
            state === 'active' ? 'LISTENING' :
            state === 'suspended' || state === 'idle' ? 'IDLE' :
            state === 'fatal' ? 'IDLE' : 'THINKING';
        this.socket.emit('voice_phase', { phase });
        this.socket.emit('voice_session_state', {
            state,
            model: MODEL,
            // Report what she is actually speaking with, not the configured default —
            // they differ if the voice ever had to fall back.
            voice: (this.session && this.session.activeVoice) || VOICE
        });
    }

    /**
     * Function calling on this model BLOCKS the model mid-turn, so Python must answer
     * fast. We hand the call straight to the existing allowlist and resolve when Python
     * acknowledges, with a timeout so a slow handler cannot leave her silent forever.
     */
    onToolCall(calls) {
        this.socket.emit('voice_phase', { phase: 'THINKING' });

        calls.forEach((call) => {
            const args = call.args || {};
            const requestId = call.id || String(Date.now() + Math.random());
            const isDataCall = call.name === 'get_status';

            // Reads can touch the calendar API or a weather endpoint, so they get a
            // longer budget than a local UI toggle. Both stay bounded: function calling
            // blocks the model mid-turn, so a hung handler would leave her silent.
            const budgetMs = isDataCall ? 4000 : 1500;

            const timer = setTimeout(() => {
                if (!this.pendingResults.has(requestId)) return;
                this.pendingResults.delete(requestId);
                this.session.sendToolResponses([
                    {
                        id: call.id,
                        name: call.name,
                        response: { error: 'timed out reading that' }
                    }
                ]);
            }, budgetMs);

            this.pendingResults.set(requestId, { call, timer });

            if (isDataCall) {
                this.socket.emit('voice_data_call', {
                    request_id: requestId,
                    query: args.query,
                    argument: args.argument || ''
                });
            } else {
                this.socket.emit('voice_tool_call', {
                    request_id: requestId,
                    action: args.action,
                    value: args.value || ''
                });
            }
        });
    }

    onToolResult(payload) {
        const entry = this.pendingResults.get(payload.request_id);
        if (!entry) return;
        clearTimeout(entry.timer);
        this.pendingResults.delete(payload.request_id);

        this.session.sendToolResponses([
            {
                id: entry.call.id,
                name: entry.call.name,
                response: payload.ok
                    ? { result: payload.detail || 'done' }
                    : { error: payload.detail || 'that action is not available' }
            }
        ]);
    }

    /** Throttled to match state_manager's own ~28/sec cap. */
    emitLevel(peak, state) {
        const now = Date.now();
        if (now - this.lastLevelSent < 36) return;
        this.lastLevelSent = now;
        this.socket.emit('voice_audio_level', { level: Math.min(1, peak * 4), state });
    }

    setToolManifest(actions) {
        this.toolManifest = Array.isArray(actions) ? actions : [];
        this.log('tool manifest: ' + this.toolManifest.length + ' actions');
    }

    /**
     * The small always-on memory block, sent by Python.
     *
     * Only the core identity memories arrive here — a preferred name is relevant
     * to every reply, so it belongs in the system instruction. Everything else
     * is fetched per topic through get_status('memory'), because injecting the
     * whole store into a session that lasts minutes is precisely the failure
     * Memory v2 exists to remove.
     *
     * The running session is NOT reconnected to pick this up: dropping a live
     * socket would cost the conversation its context, which is a far worse
     * trade than one late memory. It applies at the next connect or rotation.
     */
    setMemoryContext(block) {
        const next = String(block || '').trim();

        if (next === this.memoryBlock) return;

        this.memoryBlock = next;
        this.log('memory context: ' + (next ? next.split('\n').length + ' lines' : 'empty'));
    }

    /** Presence gate — drop the socket while away, resume instantly on return. */
    suspend() {
        if (this.suspended || !this.session) return;
        this.suspended = true;
        this.session.suspend();
        this.log('suspended (presence)');
    }

    resume() {
        if (!this.suspended || !this.session) return;
        this.suspended = false;
        this.session.resume();
        this.log('resumed (presence)');
    }

    setMuted(muted) {
        if (this.engine) this.engine.setMuted(muted);
    }

    sendText(text) {
        if (this.session) this.session.sendText(text);
    }

    /**
     * Honest, observable voice health — never a cached "we started it" flag.
     *
     * `ok` requires BOTH halves to be true at once: a Live session that is
     * actually connected, and a microphone that is actually capturing. Either one
     * alone is the failure mode this exists to catch — an interface that looks
     * online while nothing is being heard.
     */
    health() {
        const audio = this.engine ? this.engine.health() : null;
        const sessionState = this.session ? this.session.state : 'none';
        const sessionReady = Boolean(this.session && this.session.ready);
        const capturing = Boolean(this.engine && this.engine.isCapturing());

        let reason = '';

        if (!this.engine || !this.session) {
            reason = 'voice layer not started';
        } else if (sessionState === 'fatal') {
            reason = 'live session rejected (check API key)';
        } else if (!capturing) {
            reason = audio && audio.micTrack !== 'live'
                ? `microphone ${audio.micTrack}`
                : `audio context ${audio ? audio.captureState : 'unknown'}`;
        } else if (!sessionReady) {
            reason = `live session ${sessionState}`;
        }

        return {
            ok: sessionReady && capturing && !this.suspended,
            listening: sessionReady && capturing && !this.suspended,
            sessionState,
            sessionReady,
            capturing,
            suspended: this.suspended,
            voice: (this.session && this.session.activeVoice) || VOICE,
            model: MODEL,
            audio,
            reason
        };
    }

    /**
     * Repair the voice path and report what is actually true afterwards.
     *
     * Reuses a healthy session rather than replacing it: reconnecting a working
     * Live socket would drop conversation context and cost a fresh handshake for
     * nothing.
     */
    async ensureReady() {
        if (!this.engine || !this.session) {
            return this.health();
        }

        if (this.suspended) {
            this.resume();
        }

        await this.engine.ensureRunning();

        // `wanted` is false only after an explicit disconnect; state 'fatal' means
        // the server refused us and retrying blindly would just storm it.
        //
        // Nothing is done for a session that is connecting or has a retry pending:
        // it is already on its way, and connecting over the top of it is how one
        // repair becomes a reconnect loop.
        if (!this.session.wanted && this.session.state !== 'fatal') {
            await this.session.connect();
        } else if (this.session.state === 'suspended') {
            await this.session.resume();
        } else if (this.session.stalled()) {
            await this.session.connect();
        }

        const health = this.health();
        this.log('voice health: ' + (health.ok ? 'listening' : 'NOT listening — ' + health.reason));
        return health;
    }

    /**
     * Report something that happened to the running conversation.
     *
     * A Live session has no system-message channel mid-turn, so this rides the text
     * input path — which is user-role content. The [system] tag plus the matching
     * PERSONA rule is what stops "Calendar opened" being heard as Boss saying it, and
     * it is why Python now sends facts rather than finished sentences: FRIDAY words
     * the acknowledgement herself.
     */
    sendSystemEvent(text, mode) {
        const body = String(text || '').trim();
        if (!body || !this.session) return;
        const tag = mode === 'acknowledge' ? '[system done]' : '[system note]';
        this.session.sendText(`${tag} ${body}`);
    }
}

function bufferToBase64(arrayBuffer) {
    return Buffer.from(arrayBuffer).toString('base64');
}

function base64ToArrayBuffer(base64) {
    const buf = Buffer.from(base64, 'base64');
    // Copy out of Node's pooled memory before handing it to the worklet.
    return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
}

module.exports = { VoiceBridge };
