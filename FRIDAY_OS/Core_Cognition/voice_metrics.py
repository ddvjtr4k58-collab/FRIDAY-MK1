"""
Lightweight voice-pipeline timing instrumentation.

One turn = one microphone listen through to the end of FRIDAY speech playback.
Marks are timestamps recorded by the audio engine and the brain loop; the report
is printed once per turn as concise [VOICE PERF] lines.

Nothing here ever prints transcript or response content: only stage names and
millisecond deltas, so enabling the logs never widens what the terminal shows.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional, Tuple


# Ordered stage marks. Each entry is (mark_name, human_label).
TURN_STAGES = (
    "listen_start",
    "speech_detected",
    "last_voiced_chunk",
    "speech_ended",
    "attention_done",
    "transcription_done",
    "routing_done",
    "ai_request_start",
    "ai_response_done",
    "tts_start",
    "tts_audio_ready",
    "player_spawned",
    "audio_playback_start",
    "audio_playback_end",
)

# Reported spans: (label, from_mark, to_mark)
TURN_SPANS = (
    ("speech_capture_ms", "speech_detected", "speech_ended"),
    ("end_of_speech_ms", "last_voiced_chunk", "speech_ended"),
    ("transcription_ms", "speech_ended", "transcription_done"),
    ("attention_ms", "transcription_done", "attention_done"),
    ("routing_ms", "attention_done", "routing_done"),
    ("model_ms", "ai_request_start", "ai_response_done"),
    ("fish_request_ms", "tts_start", "tts_audio_ready"),
    ("audio_decode_ms", "tts_audio_ready", "player_spawned"),
    ("playback_start_ms", "player_spawned", "audio_playback_start"),
    ("tts_startup_ms", "tts_start", "audio_playback_start"),
    # The number that matters to the user: silence after they stop talking.
    ("end_speech_to_first_audio_ms", "speech_ended", "audio_playback_start"),
    ("speech_playback_ms", "audio_playback_start", "audio_playback_end"),
)

_lock = threading.RLock()
_current = None  # type: Optional["VoiceTurn"]

_enabled_cache = (0.0, False)
_ENABLED_TTL_SECONDS = 2.0


def _voice_logs_enabled() -> bool:
    """
    Gated by the existing performance/debug settings so voice timings follow the
    same switch as the rest of the [PERF] output. Cached briefly because this is
    consulted on every stage mark.
    """
    global _enabled_cache

    now = time.time()
    cached_at, cached_value = _enabled_cache

    if now - cached_at < _ENABLED_TTL_SECONDS:
        return cached_value

    value = False

    try:
        import os

        from Core_Cognition.settings_manager import get_setting

        env_enabled = (os.getenv("FRIDAY_PERF_LOG") or os.getenv("JARVIS_PERF_LOG", "1")).strip().lower() in {"1", "true", "yes", "on"}
        value = bool(
            env_enabled
            and bool(get_setting("performance_logs", True))
            and bool(get_setting("voice_performance_logs", True))
        )
    except Exception:
        value = False

    _enabled_cache = (now, value)
    return value


class VoiceTurn:
    """Timestamps for a single voice turn."""

    def __init__(self) -> None:
        self.created_at = time.time()
        self.marks = {}  # type: Dict[str, float]
        self.notes = []  # type: List[Tuple[str, str]]
        self.reported = False

    def mark(self, name: str, when: Optional[float] = None) -> float:
        stamp = float(when if when is not None else time.time())
        self.marks.setdefault(str(name), stamp)
        return stamp

    def remark(self, name: str, when: Optional[float] = None) -> float:
        """Overwrite an existing mark (used when a turn re-enters a stage)."""
        stamp = float(when if when is not None else time.time())
        self.marks[str(name)] = stamp
        return stamp

    def note(self, key: str, value: str) -> None:
        self.notes.append((str(key), str(value)))

    def span_ms(self, from_mark: str, to_mark: str) -> Optional[float]:
        start = self.marks.get(from_mark)
        end = self.marks.get(to_mark)

        if start is None or end is None or end < start:
            return None

        return (end - start) * 1000.0

    def total_ms(self) -> float:
        start = self.marks.get("listen_start", self.created_at)
        end = max(self.marks.values()) if self.marks else time.time()
        return max(0.0, (end - start) * 1000.0)

    def report(self) -> None:
        if self.reported:
            return

        self.reported = True

        if not _voice_logs_enabled():
            return

        lines = []

        listen_start = self.marks.get("listen_start")

        if listen_start is not None:
            lines.append("listen_to_speech_start: {0:.0f} ms".format(
                (self.marks.get("speech_detected", listen_start) - listen_start) * 1000.0
            ))

        for label, from_mark, to_mark in TURN_SPANS:
            value = self.span_ms(from_mark, to_mark)

            if value is not None:
                lines.append("{0}: {1:.0f} ms".format(label, value))

        for key, value in self.notes:
            lines.append("{0}: {1}".format(key, value))

        lines.append("total_turn_ms: {0:.0f} ms".format(self.total_ms()))

        for line in lines:
            print("[VOICE PERF] {0}".format(line))


def start_turn() -> VoiceTurn:
    global _current

    with _lock:
        _current = VoiceTurn()
        return _current


def current_turn() -> Optional[VoiceTurn]:
    with _lock:
        return _current


def mark(name: str, when: Optional[float] = None) -> None:
    turn = current_turn()

    if turn is not None:
        turn.mark(name, when)


def note(key: str, value: str) -> None:
    turn = current_turn()

    if turn is not None:
        turn.note(key, value)


def finish_turn() -> None:
    global _current

    with _lock:
        turn = _current
        _current = None

    if turn is not None:
        try:
            turn.report()
        except Exception:
            pass


def abandon_turn() -> None:
    """Drop the turn without reporting (silence, noise, or ignored speech)."""
    global _current

    with _lock:
        _current = None


def logs_enabled() -> bool:
    return _voice_logs_enabled()
