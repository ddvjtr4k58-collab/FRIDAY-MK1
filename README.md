# FRIDAY Mk.1

A modular personal AI assistant built with Python, computer vision, and a web-based interface, designed for future robotics and autonomous systems integration.

## Environment Setup

This repository is currently maintained for **Python 3.9 compatibility**. There is no `pyproject.toml`/`setup.py` pinning that yet, so verify your interpreter before creating a virtual environment:

```bash
python3 --version
```

### 1. Create a virtual environment

```bash
python3 -m venv venv
```

### 2. Activate it

```bash
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

`requirements.txt` marks several packages "optional" in comments — FRIDAY degrades the matching feature gracefully (no microphone, no desktop automation, no face-ID, no calendar) if an optional package isn't installed. One exception: `opencv-python` is currently a **hard** requirement for backend startup even with the camera fully disabled — see the note in `requirements.txt` for why.

### 4. macOS: PyAudio needs PortAudio

`PyAudio` links against the PortAudio system library, which `pip` does not install for you. On macOS, install it first via Homebrew:

```bash
brew install portaudio
```

If PortAudio isn't present, PyAudio typically fails to build. This is only required if you want voice input — `Sensory_Array/audio_engine.py` already handles a missing PyAudio by disabling microphone capture rather than crashing.

### 5. Optional: face-ID presence recognition

`deepface` (used by `Sensory_Array/vision_core.py`, off by default via `FRIDAY_FACE_ID_ENABLED`) pulls in TensorFlow/Keras as a heavy transitive dependency and may need additional native build tooling depending on your platform. Only install it if you intend to enable face-ID.

### 6. Configuration

FRIDAY reads its settings from `FRIDAY_OS/Core_Cognition/.env`. That file is **not** in this repository — you create it from the tracked template.

#### Copy the template

```bash
cp FRIDAY_OS/Core_Cognition/.env.example FRIDAY_OS/Core_Cognition/.env
```

#### Fill in your own API keys

Open the new `.env` and replace the placeholders with your own credentials. Three values are required for a full setup:

| Variable | What it is |
| --- | --- |
| `GEMINI_API_KEY` | Google Gemini API key. FRIDAY will not start without it. |
| `FISH_API_KEY` | Fish Audio API key for cloud text-to-speech. Omit to use the local macOS voice. |
| `VOICE_ID` | Fish Audio voice model reference ID — the voice FRIDAY speaks with. |

Everything else in `.env.example` is commented out and optional. Each optional entry documents the default the code already uses, so a freshly copied `.env` behaves exactly like an unconfigured install until you uncomment something. Uncomment a line only when you want to override that default.

`.env.example` also documents every microphone, vision, model-routing, and logging variable the project reads, grouped by subsystem. Note that each `FRIDAY_*` variable accepts a legacy `JARVIS_*` alias of the same name; `FRIDAY_*` takes precedence, and new setups should use it.

Google Calendar is the one feature that is not configured through `.env` — it reads `FRIDAY_OS/Core_Cognition/calendar_credentials.json` (your own OAuth client) and a `calendar_token.json` generated on first authorization by `./reconnect_calendar.sh`. Both are gitignored and absent from this repository; calendar features stay disabled until you supply them.

#### Never commit `.env`

`.env` is listed in `.gitignore` and must stay that way. It holds live credentials — do not commit it, do not force-add it, and do not paste its contents into issues or pull requests. `.env.example` is the only configuration file that belongs in version control, and it must never contain a real key.

This repository **intentionally ships without credentials**. No API keys, tokens, voice IDs, OAuth clients, or personal profile data are included. Every value in `.env.example` is a placeholder, and the paths that hold real secrets or personal data (`.env`, calendar credentials and tokens, user profiles, the memory store) are excluded by `.gitignore`. You are expected to bring your own keys.

### 7. Run the health check

```bash
./check_friday.sh
```

Reports `[PASS]`/`[FAIL]` for required dependencies and repository structure, `[OPTIONAL]` for missing optional dependencies, and runs Python/JavaScript syntax checks. It never installs anything, never modifies files, and never prints environment variable values.

### 8. Launch FRIDAY

```bash
./run_friday.sh
```

Starts the Python backend only (`Core_Cognition.main`). The Electron frontend under `FRIDAY_OS/Visual_Interface` is started separately (`npm start` in that directory).
