import math
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from collections import deque
from pathlib import Path
from typing import Optional

# ==========================================
# gRPC SILENCERS
# Keep these before Google audio/model imports.
# ==========================================
os.environ["GRPC_VERBOSITY"] = "NONE"
os.environ["GRPC_TRACE"] = ""
os.environ["GRPC_POLL_STRATEGY"] = "poll"

try:
    import audioop
except Exception:
    audioop = None

try:
    import pyaudio
except Exception:
    pyaudio = None

import requests

try:
    import speech_recognition as sr
except Exception:
    sr = None

import google.generativeai as genai
from dotenv import load_dotenv

from Core_Cognition import voice_metrics
from Core_Cognition.settings_manager import load_settings, normalize_voice_provider
from Core_Cognition.state_manager import (
    begin_speech_playback,
    emit_audio_level,
    end_speech_playback,
    get_presence_state,
    has_pending_override,
    is_friday_speaking,
    set_ai_status,
    set_voice_phase
)
from Sensory_Array import voice_profile

# Load env from the project root and Core_Cognition/.env.
# The current project stores GEMINI_API_KEY, FISH_API_KEY, and VOICE_ID in Core_Cognition/.env.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=False)
load_dotenv(PROJECT_ROOT / "Core_Cognition" / ".env", override=False)
load_dotenv(override=False)

CYAN = "\033[96m"
GRAY = "\033[90m"
RESET = "\033[0m"


# ==========================================
# AUDIO / VOICE CONFIGURATION
# ==========================================
# Fish Audio credentials are loaded from Core_Cognition/.env.
# Current .env already uses FISH_API_KEY and VOICE_ID, so no extra env fields are required.
def _clean_env_value(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip().strip('"').strip("'")


FISH_API_KEY = _clean_env_value("FISH_API_KEY")
VOICE_ID = _clean_env_value("VOICE_ID")
FISH_MODEL_ID = _clean_env_value("FISH_MODEL_ID") or VOICE_ID
FISH_TTS_URL = os.getenv("FISH_TTS_URL", "https://api.fish.audio/v1/tts")
# mp3 measured at roughly a fifth of the transfer size of wav for the same line,
# and correspondingly faster to receive. Envelope decoding handles either.
FISH_AUDIO_FORMAT = os.getenv("FISH_AUDIO_FORMAT", "mp3")
FISH_TTS_TIMEOUT = float(os.getenv("FISH_TTS_TIMEOUT", "12"))
FISH_CONNECT_TIMEOUT = float(os.getenv("FISH_CONNECT_TIMEOUT", "4"))
FISH_MIN_AUDIO_BYTES = int(os.getenv("FISH_MIN_AUDIO_BYTES", "512"))

# Backoff shape: a couple of consecutive failures before pausing at all, then a
# short pause that doubles, so a transient blip never parks FRIDAY on the local
# voice and a genuine outage does not hammer the service.
FISH_FAILURES_BEFORE_BACKOFF = int(os.getenv("FISH_FAILURES_BEFORE_BACKOFF", "2"))
FISH_BACKOFF_START_SECONDS = float(os.getenv("FISH_BACKOFF_START_SECONDS", "45"))
FISH_BACKOFF_MAX_SECONDS = float(os.getenv("FISH_BACKOFF_MAX_SECONDS", "300"))

# GOOGLE = speech_recognition Google recognizer
# GEMINI = Gemini multimodal audio understanding
# HYBRID = Google first, Gemini fallback
AUDIO_INPUT_MODE = (os.getenv("FRIDAY_AUDIO_MODE") or os.getenv("JARVIS_AUDIO_MODE", "GOOGLE")).upper()

CONVERSATION_PRESENCE_STATES = {"AUTHORIZED", "SCANNING"}

# Post-capture rejection floor. Well below normal speech, high enough that an
# empty room never reaches Google.
MIN_AUDIO_RMS = int((os.getenv("FRIDAY_MIN_AUDIO_RMS") or os.getenv("JARVIS_MIN_AUDIO_RMS", "12")))
# Mean phrase RMS is compared against this fraction of the trigger threshold.
PHRASE_ENERGY_RATIO = float((os.getenv("FRIDAY_PHRASE_ENERGY_RATIO") or os.getenv("JARVIS_PHRASE_ENERGY_RATIO", "0.55")))
MIN_COMMAND_CHARS = int((os.getenv("FRIDAY_MIN_COMMAND_CHARS") or os.getenv("JARVIS_MIN_COMMAND_CHARS", "2")))

# Google returns a confidence score for the top alternative. Short, low
# confidence results are almost always room noise and never reach Gemini.
MIN_TRANSCRIPT_CONFIDENCE = float((os.getenv("FRIDAY_MIN_TRANSCRIPT_CONFIDENCE") or os.getenv("JARVIS_MIN_TRANSCRIPT_CONFIDENCE", "0.45")))
LOW_CONFIDENCE_WORD_LIMIT = 3

# Speech detection is now derived from the room the microphone actually hears,
# not from an absolute RMS number.
#
# Why this changed: RMS is proportional to the input gain of the device. On this
# machine the macOS input gain sits at 24/100, where measured ambient RMS is 8
# and normal speech lands far below the old absolute floor of 120. The threshold
# was therefore pinned ~15x above the noise floor and normal conversational
# speech never triggered capture. Deriving it from measured ambient makes the
# pipeline correct at any gain or on any microphone.
MIC_ENERGY_FLOOR = int((os.getenv("FRIDAY_MIC_ENERGY_FLOOR") or os.getenv("JARVIS_MIC_ENERGY_FLOOR", "16")))
MIC_ENERGY_CEILING = int((os.getenv("FRIDAY_MIC_ENERGY_CEILING") or os.getenv("JARVIS_MIC_ENERGY_CEILING", "2200")))
# Normal conversational speech comfortably exceeds this; a calibrated
# threshold above it means calibration caught noise, not the room.
MIC_SPEECH_TRIGGER_CAP = int((os.getenv("FRIDAY_MIC_TRIGGER_CAP") or os.getenv("JARVIS_MIC_TRIGGER_CAP", "320")))
# Speech has to clear the noise floor by this ratio, with an additive margin so
# a near-silent room does not produce an absurdly twitchy threshold.
MIC_AMBIENT_RATIO = float((os.getenv("FRIDAY_MIC_AMBIENT_RATIO") or os.getenv("JARVIS_MIC_AMBIENT_RATIO", "3.2")))
MIC_AMBIENT_MARGIN = float((os.getenv("FRIDAY_MIC_AMBIENT_MARGIN") or os.getenv("JARVIS_MIC_AMBIENT_MARGIN", "14")))
MIC_AMBIENT_ADJUST_SECONDS = float((os.getenv("FRIDAY_MIC_AMBIENT_ADJUST") or os.getenv("JARVIS_MIC_AMBIENT_ADJUST", "0.6")))
MIC_DEVICE_INDEX_ENV = (os.getenv("FRIDAY_MIC_DEVICE_INDEX") or os.getenv("JARVIS_MIC_DEVICE_INDEX", "")).strip()

# Google's recogniser is natively 16 kHz. Capturing at the device default of
# 48 kHz tripled the upload payload (187 KB vs 62 KB for a 2 s utterance) for no
# accuracy gain. The smaller chunk keeps end-of-speech granularity at ~32 ms.
MIC_SAMPLE_RATE = int((os.getenv("FRIDAY_MIC_SAMPLE_RATE") or os.getenv("JARVIS_MIC_SAMPLE_RATE", "16000")))
MIC_CHUNK_SIZE = int((os.getenv("FRIDAY_MIC_CHUNK_SIZE") or os.getenv("JARVIS_MIC_CHUNK_SIZE", "512")))

# Measured ambient noise floor, filled in by calibration.
_ambient_state = {"rms": 0.0, "measured": False}

# Speech must last at least this long to count as a phrase rather than a click.
MIC_PHRASE_THRESHOLD = float((os.getenv("FRIDAY_MIC_PHRASE_THRESHOLD") or os.getenv("JARVIS_MIC_PHRASE_THRESHOLD", "0.2")))

TTS_VOICE = (os.getenv("FRIDAY_TTS_VOICE") or os.getenv("JARVIS_TTS_VOICE", ""))
# FRIDAY's voice. Moira is Irish, which matches the character, and is present
# on a stock macOS install. Enhanced/Premium variants are preferred when the
# user has downloaded them (System Settings > Accessibility > Spoken Content).
FRIDAY_VOICE_PREFERENCES = [
    "Moira (Enhanced)",
    "Moira (Premium)",
    "Moira",
    "Karen",
    "Samantha",
    "Tessa",
    "Kathy"
]

# Retained so a user who explicitly prefers the legacy voice still resolves.
TTS_MALE_FALLBACK_VOICES = [
    "Daniel (Enhanced)",
    "Daniel (Premium)",
    "Daniel",
    "Oliver",
    "Alex",
    "Tom",
    "Aaron",
    "Fred"
]

# Measured priming cost between spawning the player and audible output on this
# class of hardware. Used only to align the orb with real audio; it never delays
# the audio itself, which is already being produced while we wait.
LOCAL_PLAYBACK_START_OFFSET = float((os.getenv("FRIDAY_LOCAL_PLAYBACK_OFFSET") or os.getenv("JARVIS_LOCAL_PLAYBACK_OFFSET", "0.20")))
AFPLAY_PLAYBACK_START_OFFSET = float((os.getenv("FRIDAY_AFPLAY_PLAYBACK_OFFSET") or os.getenv("JARVIS_AFPLAY_PLAYBACK_OFFSET", "0.30")))

AUDIO_LEVEL_INTERVAL = 1.0 / 25.0
LEVEL_SMOOTHING_PREVIOUS = 0.65
LEVEL_SMOOTHING_CURRENT = 0.35

INTERRUPT_PHRASES = {
    "friday stop",
    "friday stop talking",
    "friday stop speaking",
    "friday quiet",
    "friday be quiet",
    "friday cancel",
    "friday enough",
    "friday silence",
    "stop",
    "jarvis stop",
    "stop it",
    "stop talking",
    "jarvis stop talking",
    "stop speaking",
    "jarvis stop speaking",
    "quiet",
    "be quiet",
    "jarvis quiet",
    "jarvis be quiet",
    "cancel",
    "jarvis cancel",
    "enough",
    "jarvis enough",
    "that's enough",
    "thats enough",
    "shut up",
    "silence"
}

tts_lock = threading.Lock()
speech_state_lock = threading.Lock()
current_speech_id = 0

_playback_process_lock = threading.Lock()
_playback_process = None
_stop_requested_for_speech_id = -1

_settings_cache = {"at": 0.0, "value": {}}
_SETTINGS_TTL_SECONDS = 1.5

_calibration_state = {"calibrated": False, "device": None, "unavailable": False}
_level_reference = {"value": 900.0}

_installed_voice_cache = {"at": 0.0, "voices": [], "running": False}
_resolved_local_voice = {"name": None, "requested": None}

_fish_failure_count = 0
_fish_disabled_until = 0.0
_fish_backoff_seconds = 0.0
_fish_last_error = ""

# One pooled HTTPS connection for Fish. Re-establishing TLS on every utterance
# measured at roughly 340 ms of pure overhead per request, and about 3.5 s on the
# very first cold call of a session.
_fish_session = None
_fish_session_lock = threading.Lock()

_playback_stall_count = 0
_audio_output_warning_at = 0.0


def _now() -> float:
    return time.time()


def _settings() -> dict:
    """Voice settings with a short cache. Called from the capture loop, so it
    must never hit the settings file on every audio chunk."""
    now = _now()

    if now - _settings_cache["at"] < _SETTINGS_TTL_SECONDS and _settings_cache["value"]:
        return _settings_cache["value"]

    try:
        value = load_settings()
    except Exception:
        value = _settings_cache["value"] or {}

    _settings_cache["at"] = now
    _settings_cache["value"] = value
    return value


def _setting_float(key: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(_settings().get(key, default))
    except (TypeError, ValueError):
        value = default

    return max(minimum, min(value, maximum))


def _setting_int(key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(float(_settings().get(key, default)))
    except (TypeError, ValueError):
        value = default

    return max(minimum, min(value, maximum))


def _setting_bool(key: str, default: bool = True) -> bool:
    value = _settings().get(key, default)

    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}

    return bool(value)


def voice_provider() -> str:
    env_override = (os.getenv("FRIDAY_VOICE_PROVIDER") or os.getenv("JARVIS_VOICE_PROVIDER")) or (os.getenv("FRIDAY_VOICE_MODE") or os.getenv("JARVIS_VOICE_MODE"))

    if env_override and env_override.strip():
        return normalize_voice_provider(env_override)

    return normalize_voice_provider(_settings().get("voice_provider", "local"))


def audio_reactive_enabled() -> bool:
    return _setting_bool("voice_audio_reactive_orb", True)


def echo_settle_seconds() -> float:
    return _setting_int("voice_echo_settle_ms", 220, 0, 1500) / 1000.0


def _perf_enabled() -> bool:
    return voice_metrics.logs_enabled()


def _next_speech_id() -> int:
    global current_speech_id

    with speech_state_lock:
        current_speech_id += 1
        return current_speech_id


def _speech_is_current(speech_id: Optional[int]) -> bool:
    if speech_id is None:
        return True

    with speech_state_lock:
        return speech_id == current_speech_id


# ==========================================
# MICROPHONE HELPERS
# ==========================================
_pyaudio_warning_printed = False
_microphone_warning_printed = False
_recognition_warning_printed = False


def _warn_pyaudio_unavailable() -> None:
    global _pyaudio_warning_printed

    if not _pyaudio_warning_printed:
        print("[Audio] PyAudio unavailable, microphone input disabled. Manual override still works.")
        _pyaudio_warning_printed = True


def _warn_microphone_unavailable(error) -> None:
    global _microphone_warning_printed

    if not _microphone_warning_printed:
        print(f"{GRAY}[Audio] Microphone unavailable, voice input disabled: {error}{RESET}")
        print(f"{GRAY}[Audio] Manual override and the interface remain fully functional.{RESET}")
        _microphone_warning_printed = True


def _warn_recognition_unavailable() -> None:
    global _recognition_warning_printed

    if not _recognition_warning_printed:
        print(f"{GRAY}[Audio] SpeechRecognition unavailable, voice input disabled.{RESET}")
        _recognition_warning_printed = True


def audio_has_voice_energy(audio, energy_threshold: Optional[float] = None) -> bool:
    """
    Rejects obvious low-energy non-speech audio like breathing, eating, and room noise.

    The floor tracks the calibrated energy threshold as well as the absolute
    minimum, so a room-noise blip that just clipped the trigger never reaches the
    transcription service or flashes the thinking state on the orb.
    """
    if audioop is None:
        return True

    # The trigger threshold is compared against per-chunk peaks, but this check
    # looks at the mean RMS of the whole phrase - which includes the leading and
    # trailing silence and every gap between words, and is therefore always well
    # below the peak. Requiring the mean to exceed 1.25x the peak threshold was
    # backwards and silently discarded genuine speech. A fraction of the
    # threshold is the correct comparison.
    floor = MIN_AUDIO_RMS

    if energy_threshold:
        floor = max(floor, float(energy_threshold) * PHRASE_ENERGY_RATIO)

    try:
        rms = audioop.rms(audio.frame_data, audio.sample_width)
        return rms >= floor
    except Exception:
        return True


def clean_transcript_candidate(text: str) -> str:
    cleaned = (text or "").strip()

    if len(cleaned) < MIN_COMMAND_CHARS:
        return ""

    noise_phrases = {
        "um",
        "uh",
        "hmm",
        "mm",
        "mmm",
        "ah",
        "oh",
        "breath",
        "background noise",
        "service",
        "cuckoo",
        "george",
        "exit",
        "ex"
    }

    if cleaned.lower() in noise_phrases:
        return ""

    return cleaned


def _microphone_device_index() -> Optional[int]:
    raw = MIC_DEVICE_INDEX_ENV or str(_settings().get("voice_microphone_index", "") or "").strip()

    if not raw:
        return None

    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _describe_microphone(mic) -> str:
    try:
        import speech_recognition as _sr

        names = _sr.Microphone.list_microphone_names()
        index = mic.device_index

        if index is None:
            import pyaudio as _pa

            handle = _pa.PyAudio()

            try:
                index = handle.get_default_input_device_info().get("index")
            finally:
                handle.terminate()

        return "[{0}] {1}".format(index, names[index]) if index is not None else "system default"
    except Exception:
        return "unknown"


def _build_microphone(device_index):
    """Capture at the recogniser's native rate rather than the device default."""
    kwargs = {"sample_rate": MIC_SAMPLE_RATE, "chunk_size": MIC_CHUNK_SIZE}

    if device_index is not None:
        kwargs["device_index"] = device_index

    try:
        return sr.Microphone(**kwargs)
    except Exception:
        # Some devices refuse the requested rate; fall back to their default.
        if device_index is not None:
            return sr.Microphone(device_index=device_index)

        return sr.Microphone()


def _measure_ambient_rms(source, seconds: float) -> float:
    """Median chunk RMS of the quiet room, used to place the speech threshold."""
    if audioop is None:
        return 0.0

    samples = []
    chunks = max(4, int(seconds * source.SAMPLE_RATE / source.CHUNK))

    try:
        for _ in range(chunks):
            buffer = source.stream.read(source.CHUNK)

            if not buffer:
                break

            samples.append(audioop.rms(buffer, source.SAMPLE_WIDTH))
    except Exception:
        return 0.0

    if not samples:
        return 0.0

    # A low percentile, not the median: if anything transient happens during the
    # calibration window (a door, a keystroke, FRIDAY's own tail audio) the upper
    # percentiles jump but the quiet floor does not. Using the median let one
    # noisy calibration pin the threshold high for the whole session, which is
    # exactly the "it stopped hearing me" failure mode.
    samples.sort()
    return float(samples[max(0, int(len(samples) * 0.25))])


def _threshold_for_ambient(fallback: float) -> float:
    """
    Speech trigger derived from the measured noise floor.

    Falls back to whatever adjust_for_ambient_noise produced when calibration
    has not run yet, still clamped into a sane band.
    """
    ambient = _ambient_state["rms"] if _ambient_state["measured"] else 0.0

    if ambient > 0:
        derived = max(ambient * MIC_AMBIENT_RATIO, ambient + MIC_AMBIENT_MARGIN)
    else:
        derived = float(fallback or MIC_ENERGY_FLOOR)

    # Cap the trigger, but never below the room itself: a flat cap in a genuinely
    # loud environment would sit under the noise floor and fire continuously.
    # This keeps the threshold reachable by normal speech in a quiet room while
    # still clearing sustained noise in a loud one.
    cap = max(float(MIC_SPEECH_TRIGGER_CAP), ambient * 1.5) if ambient > 0 else float(MIC_ENERGY_CEILING)

    return max(MIC_ENERGY_FLOOR, min(derived, cap, MIC_ENERGY_CEILING))


def configure_microphone_threshold(recognizer, source=None, calibrate: bool = False) -> None:
    """
    Recognition tuning.

    End-of-speech is governed by pause_threshold. The previous build used 1.75s,
    which meant every finished sentence sat in silence for nearly two seconds
    before anything happened. 0.6s (configurable) still tolerates the natural
    pauses inside a sentence while ending a genuinely finished one quickly.
    """
    if recognizer is None:
        return

    pause_threshold = _setting_float("voice_pause_threshold", 0.6, 0.3, 2.0)

    recognizer.dynamic_energy_threshold = True
    recognizer.dynamic_energy_adjustment_damping = 0.15
    recognizer.dynamic_energy_ratio = 1.4
    recognizer.pause_threshold = pause_threshold
    recognizer.non_speaking_duration = min(0.3, pause_threshold)
    recognizer.phrase_threshold = MIC_PHRASE_THRESHOLD
    recognizer.operation_timeout = None

    if calibrate and source is not None:
        ambient = _measure_ambient_rms(source, MIC_AMBIENT_ADJUST_SECONDS)

        if ambient > 0:
            _ambient_state["rms"] = ambient
            _ambient_state["measured"] = True
        else:
            try:
                recognizer.adjust_for_ambient_noise(source, duration=MIC_AMBIENT_ADJUST_SECONDS)
            except Exception:
                pass

    configured_threshold = _setting_int("voice_energy_threshold", 0, 0, 4000)

    if configured_threshold > 0:
        recognizer.dynamic_energy_threshold = False
        recognizer.energy_threshold = configured_threshold
        return

    recognizer.energy_threshold = _threshold_for_ambient(
        getattr(recognizer, "energy_threshold", MIC_ENERGY_FLOOR)
    )


def create_recognizer_and_mic():
    if sr is None:
        _warn_recognition_unavailable()
        return None, None

    if pyaudio is None:
        _warn_pyaudio_unavailable()
        return None, None

    recognizer = sr.Recognizer()
    configure_microphone_threshold(recognizer)

    device_index = _microphone_device_index()

    try:
        mic = _build_microphone(device_index)
    except Exception as error:
        if device_index is not None:
            # A stale configured device must never take the microphone offline.
            try:
                mic = _build_microphone(None)
                print(f"{GRAY}[Audio] Microphone index {device_index} unavailable, using system default.{RESET}")
            except Exception as fallback_error:
                _warn_microphone_unavailable(fallback_error)
                return None, None
        else:
            _warn_microphone_unavailable(error)
            return None, None

    _calibration_state["calibrated"] = False
    _calibration_state["unavailable"] = False
    _calibration_state["device"] = device_index
    return recognizer, mic


# Opening a capture stream normally takes well under a tenth of a second. If the
# system audio service is wedged the open blocks indefinitely, and startup must
# not wait on it - the interface and manual override have to come up regardless.
MIC_CALIBRATION_TIMEOUT_SECONDS = float((os.getenv("FRIDAY_MIC_CALIBRATION_TIMEOUT") or os.getenv("JARVIS_MIC_CALIBRATION_TIMEOUT", "8")))


def live_voice_enabled() -> bool:
    """True when the Electron renderer owns the microphone and speaker.

    The live pipeline captures through Chromium so it can use WebRTC echo
    cancellation. Opening PyAudio here as well would put two processes on the same
    input device and defeat that, so the Python capture path stands down entirely.

    Set FRIDAY_LIVE_VOICE=0 to fall back to this engine.
    """
    return str((os.getenv("FRIDAY_LIVE_VOICE") or os.getenv("JARVIS_LIVE_VOICE", "1"))).strip().lower() not in {"0", "false", "no"}


def calibrate_microphone(recognizer, mic) -> bool:
    """
    Short ambient calibration, run once at startup (or when the device changes).
    Per-command recalibration used to cost 350 ms of every single turn and made
    the threshold jump around between sentences.

    Returns True when the microphone is usable for this session.
    """
    if live_voice_enabled():
        # Returning False makes the brain loop treat the mic as unavailable, so it
        # never calls listen_with_gemini_audio and never opens a competing stream.
        print(f"{CYAN}[VOICE] Live voice layer active — Python microphone path disabled.{RESET}")
        return False

    if recognizer is None or mic is None:
        if sr is None:
            _warn_recognition_unavailable()
        elif pyaudio is None:
            _warn_pyaudio_unavailable()

        energy_threshold = getattr(recognizer, "energy_threshold", MIC_ENERGY_FLOOR)
        print(
            f"{GRAY}[Audio mode: {AUDIO_INPUT_MODE} | "
            f"Microphone: disabled | "
            f"Energy threshold: {energy_threshold} | "
            f"Min RMS: {MIN_AUDIO_RMS}]{RESET}"
        )
        return False

    print(f"{CYAN}SYSTEM: Calibrating ambient acoustic sensors...{RESET}")
    configure_microphone_threshold(recognizer)

    outcome = {"done": False, "error": None}

    def run_calibration():
        try:
            with mic as source:
                configure_microphone_threshold(recognizer, source=source, calibrate=True)

            outcome["done"] = True
        except Exception as error:
            outcome["error"] = error

    worker = threading.Thread(target=run_calibration, daemon=True)
    worker.start()
    worker.join(timeout=MIC_CALIBRATION_TIMEOUT_SECONDS)

    if worker.is_alive():
        _calibration_state["calibrated"] = False
        _calibration_state["unavailable"] = True
        print(f"{GRAY}[Audio] Microphone did not respond within "
              f"{MIC_CALIBRATION_TIMEOUT_SECONDS:.0f}s, voice input disabled for this session.{RESET}")
        print(f"{GRAY}[Audio] The interface, widgets, and manual override remain fully functional.{RESET}")
        return False

    if outcome["error"] is not None:
        _calibration_state["unavailable"] = True
        _warn_microphone_unavailable(outcome["error"])
        return False

    _calibration_state["calibrated"] = True
    _calibration_state["unavailable"] = False

    print(
        f"{GRAY}[Audio mode: {AUDIO_INPUT_MODE} | "
        f"Voice: {describe_voice_provider()} | "
        f"Energy threshold: {int(recognizer.energy_threshold)} | "
        f"Pause: {recognizer.pause_threshold:.2f}s | "
        f"Min RMS: {MIN_AUDIO_RMS}]{RESET}"
    )

    if _perf_enabled():
        print("[VOICE PERF] selected_mic: {0}".format(_describe_microphone(mic)))
        print("[VOICE PERF] sample_rate: {0} Hz, chunk {1} ({2:.0f} ms)".format(
            getattr(mic, "SAMPLE_RATE", 0), getattr(mic, "CHUNK", 0),
            1000.0 * getattr(mic, "CHUNK", 0) / max(1, getattr(mic, "SAMPLE_RATE", 1))))
        print("[VOICE PERF] ambient_rms: {0:.0f}".format(_ambient_state["rms"]))
        print("[VOICE PERF] energy_threshold: {0:.0f}  (ambient x{1:.1f})".format(
            recognizer.energy_threshold,
            recognizer.energy_threshold / _ambient_state["rms"] if _ambient_state["rms"] else 0.0))
        print("[VOICE PERF] phrase_reject_floor: {0:.0f}".format(
            max(MIN_AUDIO_RMS, recognizer.energy_threshold * PHRASE_ENERGY_RATIO)))

    return True


def microphone_is_available() -> bool:
    return not bool(_calibration_state.get("unavailable"))


def ensure_calibrated(recognizer, mic) -> None:
    if _calibration_state.get("calibrated") or _calibration_state.get("unavailable"):
        return

    calibrate_microphone(recognizer, mic)


# ==========================================
# LIVE CAPTURE
# ==========================================

def _normalized_level(rms: float, energy_threshold: float) -> float:
    """
    Map a raw RMS reading onto 0..1 using a slowly decaying peak reference so the
    orb responds the same way regardless of microphone gain.
    """
    reference = max(
        float(rms),
        _level_reference["value"] * 0.995,
        float(energy_threshold) * 4.0,
        300.0
    )
    _level_reference["value"] = reference

    if reference <= 0:
        return 0.0

    level = float(rms) / reference

    # Background noise should register, but must not drive a strong animation.
    if rms < energy_threshold:
        level *= 0.22

    return max(0.0, min(level, 1.0))


def _stream_listen(recognizer, source, timeout: float, phrase_time_limit: float, on_state=None):
    """
    Chunk-level capture loop.

    This mirrors the reference recognizer.listen() algorithm but exposes the two
    things the rest of the system needs and the library call cannot provide:
    live amplitude for the orb, and the ability to abandon a capture the instant
    FRIDAY starts speaking or a typed command arrives.

    Returns (AudioData, speech_started_at, speech_ended_at) or (None, None, None).
    """
    if audioop is None or sr is None:
        return None, None, None

    seconds_per_buffer = float(source.CHUNK) / float(source.SAMPLE_RATE)
    pause_threshold = _setting_float("voice_pause_threshold", 0.6, 0.3, 2.0)
    phrase_threshold = MIC_PHRASE_THRESHOLD
    non_speaking_duration = min(0.3, pause_threshold)

    pause_buffer_count = int(math.ceil(pause_threshold / seconds_per_buffer))
    phrase_buffer_count = int(math.ceil(phrase_threshold / seconds_per_buffer))
    non_speaking_buffer_count = int(math.ceil(non_speaking_duration / seconds_per_buffer))

    # A single loud chunk is a click, a chair, or a keystroke. Speech needs a few
    # consecutive frames above the threshold before the turn starts.
    onset_buffer_count = max(2, int(math.ceil(0.055 / seconds_per_buffer)))

    reactive = audio_reactive_enabled()
    smoothed_level = 0.0
    last_level_emit = 0.0
    elapsed_time = 0.0

    def publish_level(rms: float, state: str) -> None:
        nonlocal smoothed_level
        nonlocal last_level_emit

        if not reactive:
            return

        raw = _normalized_level(rms, recognizer.energy_threshold)
        smoothed_level = (smoothed_level * LEVEL_SMOOTHING_PREVIOUS) + (raw * LEVEL_SMOOTHING_CURRENT)

        now = _now()

        if now - last_level_emit < AUDIO_LEVEL_INTERVAL:
            return

        last_level_emit = now
        emit_audio_level(smoothed_level, state)

    while True:
        frames = deque()
        onset_count = 0

        # --- wait for a phrase to start ---
        while True:
            if is_friday_speaking() or has_pending_override():
                return None, None, None

            if timeout and elapsed_time > timeout:
                return None, None, None

            try:
                buffer = source.stream.read(source.CHUNK)
            except Exception:
                return None, None, None

            elapsed_time += seconds_per_buffer

            if len(buffer) == 0:
                return None, None, None

            frames.append(buffer)

            if len(frames) > non_speaking_buffer_count:
                frames.popleft()

            energy = audioop.rms(buffer, source.SAMPLE_WIDTH)
            publish_level(energy, "listening")

            if energy > recognizer.energy_threshold:
                onset_count += 1

                if onset_count >= onset_buffer_count:
                    break

                continue

            onset_count = 0

            if recognizer.dynamic_energy_threshold:
                damping = recognizer.dynamic_energy_adjustment_damping ** seconds_per_buffer
                target = energy * recognizer.dynamic_energy_ratio
                adjusted = recognizer.energy_threshold * damping + target * (1 - damping)
                recognizer.energy_threshold = max(
                    MIC_ENERGY_FLOOR,
                    min(adjusted, MIC_ENERGY_CEILING)
                )

        speech_started_at = _now()
        last_voiced_at = speech_started_at

        if callable(on_state):
            try:
                on_state("speech_started")
            except Exception:
                pass

        # --- read until the phrase ends ---
        pause_count = 0
        phrase_count = 0
        phrase_start_time = elapsed_time

        while True:
            if is_friday_speaking():
                return None, None, None

            if phrase_time_limit and elapsed_time - phrase_start_time > phrase_time_limit:
                break

            try:
                buffer = source.stream.read(source.CHUNK)
            except Exception:
                break

            elapsed_time += seconds_per_buffer

            if len(buffer) == 0:
                break

            frames.append(buffer)
            phrase_count += 1

            energy = audioop.rms(buffer, source.SAMPLE_WIDTH)
            publish_level(energy, "user_speaking")

            if energy > recognizer.energy_threshold:
                pause_count = 0
                last_voiced_at = _now()
            else:
                pause_count += 1

                if pause_count > pause_buffer_count:
                    break

        speech_ended_at = _now()
        phrase_count -= pause_count

        if phrase_count >= phrase_buffer_count:
            break

        # Too short to be speech: drop it and keep waiting inside this listen.
        if timeout and elapsed_time > timeout:
            return None, None, None

    if reactive:
        emit_audio_level(0.0, "listening", force=True)

    # Trim the trailing silence used to detect the end of the phrase.
    for _ in range(min(pause_count, max(len(frames) - 1, 0))):
        frames.pop()

    frame_data = b"".join(frames)

    if not frame_data:
        return None, None, None

    voice_metrics.mark("last_voiced_chunk", last_voiced_at)

    return (
        sr.AudioData(frame_data, source.SAMPLE_RATE, source.SAMPLE_WIDTH),
        speech_started_at,
        speech_ended_at
    )


# ==========================================
# SPEECH TO TEXT
# ==========================================
def _google_transcript_with_confidence(audio, recognizer):
    """Returns (text, confidence). Confidence is 1.0 when the service omits it."""
    try:
        result = recognizer.recognize_google(audio, show_all=True)
    except Exception:
        return "", 0.0

    if isinstance(result, str):
        return result.strip(), 1.0

    if not isinstance(result, dict):
        return "", 0.0

    alternatives = result.get("alternative") or []

    if not alternatives:
        return "", 0.0

    best = alternatives[0]

    if not isinstance(best, dict):
        return "", 0.0

    text = str(best.get("transcript") or "").strip()

    try:
        confidence = float(best.get("confidence", 1.0))
    except (TypeError, ValueError):
        confidence = 1.0

    return text, confidence


def transcribe_with_google(audio, recognizer) -> str:
    text, confidence = _google_transcript_with_confidence(audio, recognizer)
    cleaned = clean_transcript_candidate(text)

    if not cleaned:
        return ""

    # Short low-confidence results are room noise. Never route them onward.
    word_count = len(cleaned.split())

    if confidence and confidence < MIN_TRANSCRIPT_CONFIDENCE and word_count <= LOW_CONFIDENCE_WORD_LIMIT:
        if _perf_enabled():
            print(f"[VOICE PERF] low_confidence_rejected: {confidence:.2f}")

        return ""

    voice_metrics.note("asr_confidence", "{0:.2f}".format(confidence))
    return cleaned


def transcribe_with_gemini_audio(audio) -> str:
    wav_bytes = audio.get_wav_data()
    tmp_path = None
    uploaded_file = None

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
            tmp.write(wav_bytes)
            tmp_path = tmp.name

        uploaded_file = genai.upload_file(path=tmp_path, mime_type="audio/wav")

        for _ in range(20):
            uploaded_file = genai.get_file(uploaded_file.name)
            state_name = getattr(uploaded_file.state, "name", str(uploaded_file.state))

            if state_name == "ACTIVE":
                break

            if state_name == "FAILED":
                raise RuntimeError("Gemini audio upload failed.")

            time.sleep(0.25)

        transcription_model = genai.GenerativeModel("gemini-3-flash-preview")
        response = transcription_model.generate_content([
            "Transcribe this microphone audio into the user's command text only. "
            "If the audio is only breathing, eating, typing, room noise, or unclear background sound, "
            "return exactly: NO_COMMAND. No commentary.",
            uploaded_file
        ])

        text = clean_transcript_candidate((response.text or "").strip())

        if text.upper() == "NO_COMMAND":
            return ""

        return text

    except Exception as e:
        print(f"{GRAY}[Gemini audio transcription failed: {e}]{RESET}")
        return ""

    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

        if uploaded_file:
            try:
                genai.delete_file(uploaded_file.name)
            except Exception:
                pass


def _wait_out_friday_speech() -> None:
    """Hold the microphone closed until playback plus the echo-settle window ends."""
    while is_friday_speaking():
        time.sleep(0.03)


def listen_with_gemini_audio(recognizer, mic) -> str:
    """
    One microphone turn. Name kept for the existing call site in the brain loop.
    """
    if recognizer is None or mic is None:
        if sr is None:
            _warn_recognition_unavailable()
        elif pyaudio is None:
            _warn_pyaudio_unavailable()

        time.sleep(0.25)
        return ""

    if not microphone_is_available():
        time.sleep(0.25)
        return ""

    if get_presence_state() not in CONVERSATION_PRESENCE_STATES:
        time.sleep(0.2)
        return ""

    # Never open the microphone while FRIDAY is audible.
    _wait_out_friday_speech()

    if has_pending_override():
        return ""

    ensure_calibrated(recognizer, mic)
    configure_microphone_threshold(recognizer)

    timeout = _setting_int("voice_listen_timeout", 8, 2, 30)
    phrase_time_limit = _setting_int("voice_phrase_time_limit", 18, 4, 60)

    turn = voice_metrics.start_turn()
    turn.mark("listen_start")
    set_voice_phase("LISTENING")

    speech_started_at = None
    speech_ended_at = None
    audio = None

    def on_state(event: str) -> None:
        if event == "speech_started":
            set_voice_phase("USER_SPEAKING")

    try:
        with mic as source:
            audio, speech_started_at, speech_ended_at = _stream_listen(
                recognizer,
                source,
                timeout=timeout,
                phrase_time_limit=phrase_time_limit,
                on_state=on_state
            )
    except Exception as error:
        _warn_microphone_unavailable(error)
        voice_metrics.abandon_turn()
        set_voice_phase("IDLE")
        return ""

    if audio is None:
        voice_metrics.abandon_turn()

        if audio_reactive_enabled():
            emit_audio_level(0.0, "listening", force=True)

        set_voice_phase("IDLE")
        return ""

    if speech_started_at:
        turn.mark("speech_detected", speech_started_at)

    if speech_ended_at:
        turn.mark("speech_ended", speech_ended_at)

    if get_presence_state() not in CONVERSATION_PRESENCE_STATES:
        voice_metrics.abandon_turn()
        set_voice_phase("IDLE")
        return ""

    if not audio_has_voice_energy(audio, recognizer.energy_threshold):
        voice_metrics.abandon_turn()
        set_voice_phase("IDLE")
        return ""

    set_voice_phase("THINKING")

    if AUDIO_INPUT_MODE == "GEMINI":
        text = transcribe_with_gemini_audio(audio)
    elif AUDIO_INPUT_MODE == "HYBRID":
        text = transcribe_with_google(audio, recognizer) or transcribe_with_gemini_audio(audio)
    else:
        text = transcribe_with_google(audio, recognizer)

    turn.mark("transcription_done")

    if text:
        print(f"Jon: {text}")
        return text

    voice_metrics.abandon_turn()
    set_voice_phase("IDLE")
    return ""


# ==========================================
# VOICE SELECTION
# ==========================================

def _probe_installed_voices() -> None:
    """
    Enumerate installed voices off the calling thread.

    `say -v ?` normally returns in milliseconds, but a wedged audio service can
    leave it blocked in a state where even SIGKILL does not reap it, which would
    otherwise hang whatever asked for the voice list - including the settings
    widget and the brain loop.
    """
    voices = []

    try:
        result = subprocess.run(
            ["say", "-v", "?"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=6
        )
        output = (result.stdout or b"").decode("utf-8", errors="ignore")
    except Exception:
        output = ""

    for line in output.splitlines():
        match = re.match(r"^(.+?)\s{2,}([a-z]{2}_[A-Z]{2})", line)

        if match:
            voices.append((match.group(1).strip(), match.group(2).strip()))

    _installed_voice_cache["voices"] = voices
    _installed_voice_cache["at"] = _now()
    _installed_voice_cache["running"] = False


def installed_say_voices():
    """
    Cached listing of installed macOS voices. Never assumes a voice exists, and
    never blocks: an unfinished probe simply reports nothing yet.
    """
    now = _now()

    if _installed_voice_cache["voices"] and now - _installed_voice_cache["at"] < 300:
        return _installed_voice_cache["voices"]

    if not _installed_voice_cache["running"]:
        _installed_voice_cache["running"] = True
        threading.Thread(target=_probe_installed_voices, daemon=True).start()

    return _installed_voice_cache["voices"]


def resolve_local_voice() -> str:
    """
    Pick the best available installed voice.

    Returns "" when nothing suitable is installed, which makes `say` use the
    system default rather than failing.
    """
    requested = str(_settings().get("voice_local_name") or TTS_VOICE or "").strip()

    if _resolved_local_voice["requested"] == requested and _resolved_local_voice["name"] is not None:
        return _resolved_local_voice["name"]

    voices = installed_say_voices()
    names = [name for name, _ in voices]

    if not names:
        # The voice list is not known yet. Use the configured name as-is, or let
        # `say` pick the system default, and resolve properly once the probe
        # lands rather than caching an uninformed choice.
        return requested

    resolved = ""

    if requested:
        if not names or requested in names:
            resolved = requested
        else:
            lowered = {name.lower(): name for name in names}
            resolved = lowered.get(requested.lower(), "")

            if not resolved:
                print(f"{GRAY}[Audio] Voice '{requested}' is not installed, selecting an available voice.{RESET}")

    if not resolved:
        for candidate in FRIDAY_VOICE_PREFERENCES:
            if candidate in names:
                resolved = candidate
                break

    if not resolved and names:
        english = [name for name, locale in voices if locale.startswith("en_")]

        if english:
            resolved = english[0]

    _resolved_local_voice["requested"] = requested
    _resolved_local_voice["name"] = resolved
    return resolved


def resolve_male_tts_voice() -> str:
    """Kept for compatibility with older call sites."""
    return resolve_local_voice()


def describe_voice_provider() -> str:
    """Human-readable description of what will actually speak the next line."""
    provider = voice_provider()
    local_voice = resolve_local_voice() or "system default"

    if provider == "friday_local":
        return f"FRIDAY Local ({local_voice})"

    state = fish_status().get("state")

    if state == "unconfigured":
        return f"Fish Audio not configured, using local ({local_voice})"

    if state == "backoff":
        return f"Fish Audio unavailable, using local ({local_voice})"

    return "Fish Audio" if provider == "fish" else "Auto (Fish Audio, FRIDAY Local fallback)"


def fish_configured() -> bool:
    return bool(FISH_API_KEY and FISH_MODEL_ID)


def fish_available() -> bool:
    return fish_configured() and _now() >= _fish_disabled_until


def fish_status() -> dict:
    """Concise health for the Settings widget. Never includes credentials."""
    if not fish_configured():
        return {
            "state": "unconfigured",
            "label": "Not configured",
            "retry_in_seconds": 0,
            "detail": "FISH_API_KEY or VOICE_ID missing"
        }

    remaining = max(0.0, _fish_disabled_until - _now())

    if remaining > 0:
        return {
            "state": "backoff",
            "label": "Unavailable - using local fallback",
            "retry_in_seconds": int(remaining) + 1,
            "detail": _fish_last_error or "recent failures"
        }

    return {
        "state": "connected",
        "label": "Connected",
        "retry_in_seconds": 0,
        "detail": ""
    }


def _get_fish_session():
    global _fish_session

    with _fish_session_lock:
        if _fish_session is None:
            session = requests.Session()
            session.headers.update({
                "Authorization": "Bearer {0}".format(FISH_API_KEY),
                "Content-Type": "application/json"
            })
            _fish_session = session

        return _fish_session


def _reset_fish_session() -> None:
    """Drop a connection that may be half-open after a transport failure."""
    global _fish_session

    with _fish_session_lock:
        session = _fish_session
        _fish_session = None

    if session is not None:
        try:
            session.close()
        except Exception:
            pass


def prewarm_voice_backend() -> None:
    """
    Open the Fish connection ahead of the first utterance so the DNS lookup and
    TLS handshake are not paid for in the middle of the first reply. Safe to
    call at startup: it costs no synthesis credits and never raises.
    """
    def worker() -> None:
        if not fish_configured() or voice_provider() == "friday_local":
            return

        try:
            host = FISH_TTS_URL.split("/v1/")[0]
            _get_fish_session().get(host, timeout=(3.05, 5))
        except Exception:
            # A cold first request is the only cost of failing here.
            pass

    threading.Thread(target=worker, daemon=True).start()


# ==========================================
# PLAYBACK LIFECYCLE
# ==========================================

def _register_playback_process(process) -> None:
    global _playback_process

    with _playback_process_lock:
        _playback_process = process


def _clear_playback_process(process=None) -> None:
    global _playback_process

    with _playback_process_lock:
        if process is None or _playback_process is process:
            _playback_process = None


def stop_speaking() -> None:
    """
    Authoritative stop. Ends playback, ends the orb animation, and clears the
    speaking guard so listening resumes after the echo-settle window.
    """
    global _stop_requested_for_speech_id

    with speech_state_lock:
        _stop_requested_for_speech_id = current_speech_id

    with _playback_process_lock:
        process = _playback_process

    if process is not None:
        try:
            process.terminate()
        except Exception:
            pass

        try:
            process.wait(timeout=0.5)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

        # Only reached when FRIDAY actually owned a player. The sweep stays as a
        # safety net for an orphaned child, but never runs when FRIDAY is silent,
        # so it cannot terminate unrelated `say` or `afplay` processes.
        for process_name in ["afplay", "say"]:
            try:
                subprocess.run(
                    ["pkill", "-x", process_name],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
            except Exception:
                pass

    _clear_playback_process()

    try:
        if audio_reactive_enabled():
            emit_audio_level(0.0, "friday_speaking", force=True)

        end_speech_playback("", echo_settle_seconds())
    except Exception:
        try:
            set_ai_status("IDLE")
        except Exception:
            pass


def _stop_requested(speech_id: Optional[int]) -> bool:
    if speech_id is None:
        return False

    with speech_state_lock:
        return _stop_requested_for_speech_id == speech_id


# ==========================================
# AMPLITUDE ENVELOPES
# ==========================================

class TextEnvelope:
    """
    Speech-shaped amplitude timeline derived from the prepared text.

    Used for the streaming local voice, where no sample buffer exists to read.
    Word timings come from the requested speaking rate and the explicit
    [[slnc]] pauses, so the animation tracks the real cadence of the sentence
    rather than looping a fixed pattern.
    """

    VOWEL_GROUPS = re.compile(r"[aeiouy]+", re.IGNORECASE)

    # `say -r N` delivers a little slower than its nominal words-per-minute on
    # short utterances. Measured against rendered audio on this platform.
    RATE_CALIBRATION = 1.05

    def __init__(self, text: str, rate: int) -> None:
        self.segments = []
        self.total = 0.0

        words_per_second = max(1.2, float(rate or 175) / 60.0)
        tokens = re.findall(r"\[\[slnc\s+(\d+)\]\]|([^\s\[\]]+)", str(text or ""))

        # Pass one: relative word weights and fixed pauses.
        plan = []
        weight_total = 0.0
        word_count = 0

        for pause_ms, word in tokens:
            if pause_ms:
                plan.append(("pause", max(0.02, int(pause_ms) / 1000.0), 0))
                continue

            spoken = word.strip()

            if not spoken:
                continue

            core = spoken.strip(".,?")

            if not core:
                continue

            syllables = max(1, len(self.VOWEL_GROUPS.findall(core)))
            weight = 0.55 + 0.45 * syllables
            weight_total += weight
            word_count += 1
            plan.append(("word", weight, syllables))

            if spoken.endswith(","):
                plan.append(("pause", 0.09, 0))
            elif spoken.endswith((".", "?")):
                plan.append(("pause", 0.13, 0))

        # Pass two: scale the weights so the spoken portion matches the real
        # speaking rate, then lay the timeline out.
        if weight_total > 0 and word_count > 0:
            target_seconds = (word_count / words_per_second) * self.RATE_CALIBRATION
            scale = target_seconds / weight_total
        else:
            scale = 0.0

        cursor = 0.0

        for kind, value, syllables in plan:
            duration = value if kind == "pause" else max(0.09, value * scale)
            self.segments.append((kind, cursor, cursor + duration, syllables))
            cursor += duration

        self.total = cursor

    def level_at(self, elapsed: float) -> float:
        if not self.segments:
            return 0.45

        if elapsed > self.total:
            # Rate estimate ran short: keep a calm speech-like level until the
            # player actually exits so the orb never freezes or stops early.
            wobble = 0.5 + 0.5 * math.sin(elapsed * 7.5)
            return 0.32 + 0.18 * wobble

        for kind, start, end, syllables in self.segments:
            if start <= elapsed < end:
                if kind == "pause":
                    return 0.06

                span = max(1e-4, end - start)
                phase = (elapsed - start) / span
                shape = math.sin(math.pi * max(0.0, min(phase, 1.0))) ** 0.45
                syllable_wave = 0.5 + 0.5 * math.sin(
                    (2.0 * math.pi * max(1, syllables) * phase) - (math.pi / 2.0)
                )
                return max(0.0, min(0.34 + 0.62 * shape * (0.55 + 0.45 * syllable_wave), 1.0))

        return 0.08


class SampleEnvelope:
    """Real amplitude envelope decoded from a rendered audio file."""

    def __init__(self, levels, window_seconds: float, duration: float) -> None:
        self.levels = levels or []
        self.window_seconds = max(1e-3, window_seconds)
        self.total = duration

    def level_at(self, elapsed: float) -> float:
        if not self.levels:
            return 0.4

        index = int(elapsed / self.window_seconds)

        if index < 0:
            return self.levels[0]

        if index >= len(self.levels):
            return 0.0

        return self.levels[index]


def _audio_file_duration(path: str) -> float:
    try:
        output = subprocess.check_output(
            ["afinfo", path],
            stderr=subprocess.DEVNULL,
            timeout=4
        ).decode("utf-8", errors="ignore")
    except Exception:
        return 0.0

    match = re.search(r"estimated duration:\s*([\d.]+)", output)
    return float(match.group(1)) if match else 0.0


def _decode_sample_envelope(path: str, window_seconds: float = 0.04):
    """
    Decode an audio file into normalized RMS windows using only native macOS
    tooling. Returns None when decoding is unavailable, and the caller falls
    back to a progress envelope.
    """
    if audioop is None:
        return None

    wav_path = "{0}.env.wav".format(path)

    try:
        result = subprocess.run(
            [
                "afconvert",
                "-f", "WAVE",
                "-d", "LEI16@16000",
                "-c", "1",
                path,
                wav_path
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=6
        )

        if result.returncode != 0 or not os.path.exists(wav_path):
            return None

        with wave.open(wav_path, "rb") as handle:
            sample_rate = handle.getframerate()
            sample_width = handle.getsampwidth()
            channels = handle.getnchannels()
            total_frames = handle.getnframes()
            frames_per_window = max(1, int(sample_rate * window_seconds))
            levels = []
            peak = 1.0

            while True:
                chunk = handle.readframes(frames_per_window)

                if not chunk:
                    break

                if channels > 1:
                    chunk = audioop.tomono(chunk, sample_width, 0.5, 0.5)

                rms = audioop.rms(chunk, sample_width)
                levels.append(float(rms))
                peak = max(peak, float(rms))

        if not levels:
            return None

        normalized = [max(0.0, min((value / peak) ** 0.62, 1.0)) for value in levels]
        duration = total_frames / float(sample_rate or 1)
        return SampleEnvelope(normalized, window_seconds, duration)

    except Exception:
        return None

    finally:
        try:
            if os.path.exists(wav_path):
                os.remove(wav_path)
        except Exception:
            pass


def _warn_audio_output_stalled() -> None:
    """
    One concise warning per minute when the audio device stops accepting
    playback. FRIDAY keeps running, keeps listening, and keeps showing text; it
    simply cannot be heard until the output device recovers.
    """
    global _audio_output_warning_at

    now = _now()

    if now - _audio_output_warning_at < 60.0:
        return

    _audio_output_warning_at = now
    print(f"{GRAY}[Audio] Playback did not finish, the system audio output appears unresponsive.{RESET}")

    if _playback_stall_count >= 2:
        print(f"{GRAY}[Audio] Responses will continue on screen. Restarting Core Audio usually clears this.{RESET}")


def _playback_deadline(expected_seconds: float) -> float:
    """
    Upper bound on how long a player may run.

    A wedged macOS audio device leaves `say` or `afplay` blocked forever. Without
    this, FRIDAY would sit in the speaking state with the orb animating against
    silence and never listen again.
    """
    try:
        expected = max(0.5, float(expected_seconds or 0.0))
    except (TypeError, ValueError):
        expected = 0.5

    return min(90.0, expected + max(4.0, expected * 0.6))


def _drive_playback(
    process,
    envelope,
    speech_id,
    spoken_text,
    start_offset: float,
    expected_seconds: float = 0.0
) -> bool:
    """
    Single playback lifecycle shared by every provider.

    Speech-start is emitted once real audio is under way, amplitude is streamed
    while the player runs, and speech-end fires only when the player has
    actually exited.
    """
    global _playback_stall_count

    reactive = audio_reactive_enabled()
    started = False
    started_at = _now()
    smoothed = 0.0
    playback_started_at = None
    deadline = _playback_deadline(expected_seconds or getattr(envelope, "total", 0.0))
    stalled = False

    try:
        while True:
            if process.poll() is not None:
                break

            if _stop_requested(speech_id) or not _speech_is_current(speech_id):
                try:
                    process.terminate()
                except Exception:
                    pass

                break

            if _now() - started_at > deadline:
                stalled = True

                try:
                    process.terminate()
                    process.wait(timeout=0.5)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass

                break

            elapsed = _now() - started_at

            if not started and elapsed >= start_offset:
                started = True
                playback_started_at = _now()
                voice_metrics.mark("audio_playback_start", playback_started_at)
                begin_speech_playback(spoken_text)

            if started and reactive and envelope is not None:
                raw = envelope.level_at(_now() - playback_started_at)
                smoothed = (smoothed * LEVEL_SMOOTHING_PREVIOUS) + (raw * LEVEL_SMOOTHING_CURRENT)
                emit_audio_level(smoothed, "friday_speaking")

            time.sleep(AUDIO_LEVEL_INTERVAL)

        try:
            process.wait(timeout=2)
        except Exception:
            pass

        if stalled:
            _playback_stall_count += 1
            _warn_audio_output_stalled()
        else:
            _playback_stall_count = 0

        if not started:
            # Very short utterance: the player finished before the priming
            # offset elapsed. Still report a real start so the orb is never
            # left without a matching start event.
            voice_metrics.mark("audio_playback_start")
            begin_speech_playback(spoken_text)

        return not stalled

    finally:
        _clear_playback_process(process)

        if reactive:
            emit_audio_level(0.0, "friday_speaking", force=True)

        voice_metrics.mark("audio_playback_end")


# ==========================================
# TEXT TO SPEECH PROVIDERS
# ==========================================

def clean_tts_text(text: str) -> str:
    """
    Public helper kept for existing callers. Sentence punctuation is preserved
    now: the synthesizer needs it to phrase the line naturally.
    """
    return voice_profile.sanitize_for_speech(text)


def _speak_local(spoken_text: str, style: str, speech_id: int) -> bool:
    """Fast native macOS voice. First audio in roughly a fifth of a second."""
    base_rate = _setting_int("voice_rate", 178, 90, 300)
    rate = voice_profile.speech_rate_for_style(style, base_rate)
    prepared = voice_profile.prepare_speech_text(spoken_text, style, provider="local")

    if not prepared:
        return False

    command = ["say", "-r", str(rate)]
    voice_name = resolve_local_voice()

    if voice_name:
        command.extend(["-v", voice_name])

    command.append(prepared)

    voice_metrics.mark("tts_start")

    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE
        )
    except Exception as error:
        print(f"{GRAY}[Local voice failed to start: {error}]{RESET}")
        return False

    _register_playback_process(process)
    timeline = TextEnvelope(prepared, rate)
    envelope = timeline if audio_reactive_enabled() else None
    completed = _drive_playback(
        process,
        envelope,
        speech_id,
        spoken_text,
        LOCAL_PLAYBACK_START_OFFSET,
        expected_seconds=timeline.total
    )

    if not completed:
        # The player stalled and was cut. Another audio path would stall too.
        return True

    if process.returncode not in (0, None) and not _stop_requested(speech_id):
        stderr_output = ""

        try:
            if process.stderr is not None:
                stderr_output = process.stderr.read().decode("utf-8", errors="ignore").strip()
        except Exception:
            stderr_output = ""

        if stderr_output:
            print(f"{GRAY}[Local voice error: {stderr_output[:120]}]{RESET}")

        return False

    return True


def friday_speak(text: str, blocking: bool = True, speech_id: int = None, style: str = None) -> bool:
    """
    Fish Audio TTS - the production FRIDAY voice.

    Uses FISH_MODEL_ID first, then the VOICE_ID fallback from .env, so the
    configured FRIDAY voice is used exactly as set up. Any genuine failure
    returns False and the caller falls back to the local voice for that
    utterance only.
    """
    style_key = style or voice_profile.classify_delivery_style(text)
    prepared = voice_profile.prepare_speech_text(text, style_key, provider="fish")

    if not prepared:
        return False

    if not FISH_API_KEY:
        print(f"{GRAY}[Fish Audio skipped: FISH_API_KEY missing]{RESET}")
        return False

    if not FISH_MODEL_ID:
        print(f"{GRAY}[Fish Audio skipped: VOICE_ID missing]{RESET}")
        return False

    if _now() < _fish_disabled_until:
        return False

    audio_path = os.path.join(
        tempfile.gettempdir(),
        f"friday_fish_{uuid.uuid4().hex}.{FISH_AUDIO_FORMAT}"
    )

    base_speed = _setting_float("voice_speed", 0.85, 0.65, 1.1)

    payload = {
        "text": prepared,
        "reference_id": FISH_MODEL_ID,
        "format": FISH_AUDIO_FORMAT,
        "prosody": {
            "speed": voice_profile.voice_speed_for_style(style_key, base_speed)
        }
    }

    voice_metrics.mark("tts_start")
    request_started_at = _now()

    try:
        # Streamed so bytes land on disk as they arrive instead of after the
        # whole response has been buffered in memory.
        response = _get_fish_session().post(
            FISH_TTS_URL,
            json=payload,
            timeout=(FISH_CONNECT_TIMEOUT, FISH_TTS_TIMEOUT),
            stream=True
        )

        if response.status_code != 200:
            response.close()
            _register_fish_failure("HTTP {0}".format(response.status_code))
            print(f"{GRAY}[Fish Audio TTS failed: HTTP {response.status_code}, using local voice]{RESET}")
            return False

        received = 0

        with open(audio_path, "wb") as audio_file:
            for chunk in response.iter_content(chunk_size=16384):
                if chunk:
                    audio_file.write(chunk)
                    received += len(chunk)

        response.close()

        if received < FISH_MIN_AUDIO_BYTES:
            _register_fish_failure("empty response")
            print(f"{GRAY}[Fish Audio returned no usable audio, using local voice]{RESET}")
            return False

        voice_metrics.mark("tts_audio_ready")
        _reset_fish_backoff()

        if _perf_enabled():
            print("[VOICE PERF] fish_generation_ms: {0:.0f} ms".format(
                (_now() - request_started_at) * 1000
            ))

        # Playback starts first; the amplitude envelope is decoded during the
        # player's own priming window, so it costs no additional latency.
        process = subprocess.Popen(
            ["afplay", audio_path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        _register_playback_process(process)
        voice_metrics.mark("player_spawned")

        envelope = _decode_sample_envelope(audio_path) if audio_reactive_enabled() else None
        expected_seconds = getattr(envelope, "total", 0.0) or _audio_file_duration(audio_path)

        _drive_playback(
            process,
            envelope,
            speech_id,
            text,
            AFPLAY_PLAYBACK_START_OFFSET,
            expected_seconds=expected_seconds
        )

        # Fish delivered usable audio. If the player then stalled, that is an
        # unresponsive audio device rather than a Fish failure, so this still
        # counts as handled and no other provider retries into the same wall.
        return True

    except Exception as error:
        _reset_fish_session()
        _register_fish_failure(type(error).__name__)
        print(f"{GRAY}[Fish Audio unavailable: {error}, using local voice]{RESET}")
        return False

    finally:
        try:
            if os.path.exists(audio_path):
                os.remove(audio_path)
        except Exception:
            pass


def _reset_fish_backoff() -> None:
    global _fish_failure_count
    global _fish_disabled_until
    global _fish_backoff_seconds
    global _fish_last_error

    recovered = _fish_backoff_seconds > 0 or _fish_failure_count > 0

    _fish_failure_count = 0
    _fish_disabled_until = 0.0
    _fish_backoff_seconds = 0.0
    _fish_last_error = ""

    if recovered:
        print(f"{GRAY}[Fish Audio recovered, FRIDAY voice restored]{RESET}")


def _register_fish_failure(reason: str = "") -> None:
    """
    Short, escalating backoff.

    A single transient failure never takes Fish out of service: the next
    utterance tries it again. Only a run of consecutive failures pauses it, and
    then only briefly, so FRIDAY is never silently stranded on the local voice.
    """
    global _fish_failure_count
    global _fish_disabled_until
    global _fish_backoff_seconds
    global _fish_last_error

    _fish_failure_count += 1
    _fish_last_error = str(reason or "unknown error")[:60]

    if _fish_failure_count < FISH_FAILURES_BEFORE_BACKOFF:
        return

    _fish_backoff_seconds = (
        FISH_BACKOFF_START_SECONDS
        if _fish_backoff_seconds <= 0
        else min(_fish_backoff_seconds * 2.0, FISH_BACKOFF_MAX_SECONDS)
    )
    _fish_disabled_until = _now() + _fish_backoff_seconds
    _fish_failure_count = 0

    print(
        f"{GRAY}[Fish Audio paused for {int(_fish_backoff_seconds)}s "
        f"after repeated failures ({_fish_last_error}), local voice active]{RESET}"
    )


# ==========================================
# INTERRUPTION
# ==========================================

def _normalize_interrupt_text(value: str) -> str:
    text = re.sub(r"[^\w\s']", " ", str(value or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def _is_interrupt_command(transcript: str, spoken_text: str) -> bool:
    candidate = _normalize_interrupt_text(transcript)

    if not candidate or candidate not in INTERRUPT_PHRASES:
        return False

    # Never let FRIDAY interrupt itself: if every word came from what it is
    # currently saying, treat it as leaked playback rather than a command.
    spoken_words = set(_normalize_interrupt_text(spoken_text).split())
    candidate_words = set(candidate.split())

    if candidate_words and candidate_words.issubset(spoken_words):
        return False

    return True


def _interrupt_listener(spoken_text: str, speech_id: int) -> None:
    if sr is None or pyaudio is None:
        return

    try:
        recognizer = sr.Recognizer()
        recognizer.dynamic_energy_threshold = False
        recognizer.pause_threshold = 0.35
        recognizer.non_speaking_duration = 0.2
        recognizer.phrase_threshold = 0.15

        base_threshold = _setting_int("voice_energy_threshold", 0, 0, 4000) or MIC_ENERGY_FLOOR
        recognizer.energy_threshold = max(int(base_threshold * 2.2), 320)

        device_index = _microphone_device_index()
        mic = _build_microphone(device_index)
    except Exception:
        return

    try:
        with mic as source:
            while _speech_is_current(speech_id) and not _stop_requested(speech_id):
                try:
                    audio = recognizer.listen(source, timeout=0.8, phrase_time_limit=2.4)
                except Exception:
                    continue

                if not _speech_is_current(speech_id) or _stop_requested(speech_id):
                    return

                if audioop is not None:
                    try:
                        rms = audioop.rms(audio.frame_data, audio.sample_width)
                    except Exception:
                        rms = recognizer.energy_threshold

                    if rms < recognizer.energy_threshold:
                        continue

                text, _confidence = _google_transcript_with_confidence(audio, recognizer)

                if not text:
                    continue

                if _is_interrupt_command(text, spoken_text):
                    print(f"{GRAY}[Voice interrupt: stopping playback]{RESET}")
                    stop_speaking()
                    return
    except Exception:
        return


# Barge-in only runs for responses long enough to be worth interrupting. Short
# acknowledgements are over before a stop command could be spoken, and skipping
# them avoids opening the microphone (and calling the recognition service) on
# every single reply.
INTERRUPT_MIN_WORDS = 12


def _start_interrupt_listener(spoken_text: str, speech_id: int):
    if not _setting_bool("voice_interrupt_enabled", True):
        return None

    if sr is None or pyaudio is None:
        return None

    if len(str(spoken_text or "").split()) < INTERRUPT_MIN_WORDS:
        return None

    thread = threading.Thread(
        target=_interrupt_listener,
        args=(spoken_text, speech_id),
        daemon=True
    )
    thread.start()
    return thread


# ==========================================
# PUBLIC SPEAK
# ==========================================

def speak(text, style: str = None, follow_up: bool = False, event: str = "announce") -> str:
    """
    Speak one response.

    Returns the delivery style that was used so the brain loop can decide
    whether to hold a follow-up conversation window open.

    `event` distinguishes information to relay ("announce") from a completed
    action ("acknowledge"); it only affects the live voice layer, where FRIDAY
    words the result herself.
    """
    display_text = voice_profile.sanitize_for_speech(text)

    if not display_text:
        return voice_profile.NEUTRAL

    style_key = style or voice_profile.classify_delivery_style(display_text)

    print(f"{CYAN}FRIDAY: {display_text}{RESET}")

    # With the live voice layer active, the Electron renderer owns the speaker, so
    # text generated here (proactive alerts, greetings, completed actions) is handed
    # to the live session rather than to a second synthesiser talking over it.
    #
    # It goes out as a system event, never as microphone text: anything pushed into a
    # Live session as plain input is treated as Boss having said it, which turned
    # "Calendar opened" into something she replied to instead of something she said.
    if live_voice_enabled():
        try:
            from Core_Cognition.state_manager import send_voice_system_event

            send_voice_system_event(display_text, mode=event)
        except Exception:
            pass
        return style_key

    with tts_lock:
        speech_id = _next_speech_id()
        provider = voice_provider()
        interrupt_thread = _start_interrupt_listener(display_text, speech_id)
        spoke = False

        try:
            # friday_local speaks immediately from the native synthesiser.
            # fish / auto try the cloud voice first and fall back to local.
            if provider in {"fish", "auto"} and fish_available():
                spoke = friday_speak(display_text, speech_id=speech_id, style=style_key)

            if not spoke and not _stop_requested(speech_id):
                spoke = _speak_local(display_text, style_key, speech_id)

            if not spoke and not _stop_requested(speech_id):
                # Last resort: the system default voice with no options at all.
                try:
                    voice_metrics.mark("tts_start")
                    process = subprocess.Popen(
                        ["say", display_text],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL
                    )
                    _register_playback_process(process)
                    spoke = _drive_playback(
                        process,
                        None,
                        speech_id,
                        display_text,
                        LOCAL_PLAYBACK_START_OFFSET,
                        expected_seconds=len(display_text.split()) / 3.0
                    )
                except Exception as error:
                    print(f"{GRAY}[Speech output unavailable: {error}]{RESET}")

        except Exception as error:
            print(f"{GRAY}[TTS error: {error}]{RESET}")

        finally:
            if not _stop_requested(speech_id):
                end_speech_playback(display_text, echo_settle_seconds())

            if interrupt_thread is not None:
                interrupt_thread.join(timeout=0.05)

    return style_key


def speak_async(text, style: str = None) -> None:
    """Background speech for proactive announcements."""
    threading.Thread(target=speak, args=(text, style), daemon=True).start()
