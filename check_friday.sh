#!/bin/bash
# FRIDAY MK1 health check.
#
# Read-only: never installs packages, never modifies files, and never
# prints the value of any environment variable (it doesn't read any).
# Exits nonzero only for failures that would actually block FRIDAY
# from starting: missing repository structure, a missing REQUIRED
# third-party dependency, or a real Python/JavaScript syntax error.
# Missing OPTIONAL dependencies are reported but never fail the check,
# since the corresponding FRIDAY feature already degrades gracefully
# without them (see requirements.txt for the same required/optional
# split, kept in sync with this script by hand).

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FRIDAY_DIR="$SCRIPT_DIR/FRIDAY_OS"
PYTHON_BIN="$SCRIPT_DIR/venv/bin/python3"

if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="python3"
fi

FAILURES=0

echo "FRIDAY MK1 health check"
echo "Using interpreter: $PYTHON_BIN"
echo ""

# ------------------------------------------------------------------
# 1. Repository structure
# ------------------------------------------------------------------
echo "== Repository structure =="
STRUCTURE_OK=1
for path in \
  "$FRIDAY_DIR" \
  "$FRIDAY_DIR/Core_Cognition" \
  "$FRIDAY_DIR/Sensory_Array" \
  "$FRIDAY_DIR/Visual_Interface" \
  "$FRIDAY_DIR/Core_Cognition/main.py" \
  "$FRIDAY_DIR/Core_Cognition/state_manager.py" \
  "$FRIDAY_DIR/Visual_Interface/main.js" \
  "$FRIDAY_DIR/Visual_Interface/renderer.js"
do
  if [ ! -e "$path" ]; then
    echo "[FAIL] missing: ${path#"$SCRIPT_DIR"/}"
    STRUCTURE_OK=0
  fi
done

if [ "$STRUCTURE_OK" -eq 0 ]; then
  echo ""
  echo "FRIDAY_OS structure is incomplete — cannot continue."
  exit 1
fi
echo "[PASS] FRIDAY_OS structure looks intact"
echo ""

# ------------------------------------------------------------------
# 2. Required third-party dependencies (import name : pip name)
#    These are imported unguarded somewhere in the startup chain —
#    missing any one of them currently prevents FRIDAY from starting.
# ------------------------------------------------------------------
echo "== Required dependencies =="
REQUIRED_DEPS=(
  "flask:Flask"
  "flask_socketio:Flask-SocketIO"
  "dotenv:python-dotenv"
  "google.generativeai:google-generativeai"
  "requests:requests"
  "speech_recognition:SpeechRecognition"
  "cv2:opencv-python"
  "psutil:psutil"
  "PIL:Pillow"
)

for entry in "${REQUIRED_DEPS[@]}"; do
  import_name="${entry%%:*}"
  pip_name="${entry##*:}"
  if "$PYTHON_BIN" -c "import ${import_name}" >/dev/null 2>&1; then
    echo "[PASS] ${import_name}"
  else
    echo "[FAIL] ${import_name} — install package: ${pip_name}"
    FAILURES=$((FAILURES + 1))
  fi
done
echo ""

# ------------------------------------------------------------------
# 3. Optional dependencies — FRIDAY degrades the matching feature
#    gracefully if these are missing, so they never fail the check.
# ------------------------------------------------------------------
echo "== Optional dependencies =="
OPTIONAL_DEPS=(
  "pyaudio:PyAudio:microphone capture disabled"
  "pyautogui:PyAutoGUI:desktop keyboard/mouse automation disabled"
  "deepface:deepface:face-ID presence recognition disabled"
  "googleapiclient:google-api-python-client:Google Calendar disabled"
  "google_auth_oauthlib:google-auth-oauthlib:Google Calendar disabled"
  "google.auth:google-auth:Google Calendar disabled"
)

for entry in "${OPTIONAL_DEPS[@]}"; do
  import_name="${entry%%:*}"
  rest="${entry#*:}"
  pip_name="${rest%%:*}"
  feature="${rest#*:}"
  if "$PYTHON_BIN" -c "import ${import_name}" >/dev/null 2>&1; then
    echo "[PASS] ${import_name}"
  else
    echo "[OPTIONAL] ${import_name} unavailable — ${feature} (install package: ${pip_name})"
  fi
done
echo ""

# ------------------------------------------------------------------
# 4. Python syntax check
# ------------------------------------------------------------------
echo "== Python syntax =="
cd "$FRIDAY_DIR" || exit 1

PY_FILES=(
  "Core_Cognition/main.py"
  "Core_Cognition/state_manager.py"
  "Core_Cognition/settings_manager.py"
  "Core_Cognition/tasks_manager.py"
  "Core_Cognition/proactive_manager.py"
  "Core_Cognition/presence_manager.py"
  "Core_Cognition/camera_presence.py"
  "Core_Cognition/attention_gate.py"
  "Core_Cognition/fast_router.py"
  "Core_Cognition/voice_metrics.py"
  "Core_Cognition/memory_manager.py"
  "Core_Cognition/memory_learning.py"
  "Core_Cognition/tool_registry.py"
  "Core_Cognition/tool_planner.py"
  "Sensory_Array/audio_engine.py"
  "Sensory_Array/voice_profile.py"
  "Sensory_Array/system_tools.py"
  "Sensory_Array/vision_core.py"
  "Sensory_Array/calendar_tools.py"
  "Sensory_Array/file_tools.py"
)

# Tool modules are discovered, never listed. Adding a tool means dropping a file
# into Core_Cognition/tools/ and nothing else — including here.
for f in Core_Cognition/tools/*.py; do
  [ -e "$f" ] || continue
  PY_FILES+=("$f")
done

for f in "${PY_FILES[@]}"; do
  if [ ! -f "$f" ]; then
    echo "[FAIL] $f — file not found"
    FAILURES=$((FAILURES + 1))
    continue
  fi
  if "$PYTHON_BIN" -m py_compile "$f" >/dev/null 2>&1; then
    echo "[PASS] $f"
  else
    echo "[FAIL] $f — syntax error"
    FAILURES=$((FAILURES + 1))
  fi
done
echo ""

# ------------------------------------------------------------------
# 5. JavaScript syntax check
# ------------------------------------------------------------------
echo "== JavaScript syntax =="
if command -v node >/dev/null 2>&1; then
  JS_FILES=(
    "Visual_Interface/main.js"
    "Visual_Interface/renderer.js"
  )
  for f in "${JS_FILES[@]}"; do
    if [ ! -f "$f" ]; then
      echo "[FAIL] $f — file not found"
      FAILURES=$((FAILURES + 1))
      continue
    fi
    if node --check "$f" >/dev/null 2>&1; then
      echo "[PASS] $f"
    else
      echo "[FAIL] $f — syntax error"
      FAILURES=$((FAILURES + 1))
    fi
  done
  for f in Visual_Interface/voice/*.js; do
    [ -e "$f" ] || continue
    if node --check "$f" >/dev/null 2>&1; then
      echo "[PASS] $f"
    else
      echo "[FAIL] $f — syntax error"
      FAILURES=$((FAILURES + 1))
    fi
  done
else
  echo "[WARN] node not found on PATH — skipping JavaScript syntax check"
  echo "       (this does not block the Python backend, so it is not counted as a failure)"
fi
echo ""

# ------------------------------------------------------------------
# 5b. Voice startup suite
#     bootstrap.js is evaluated against stubbed renderer globals and
#     LiveSession is driven with a fake transport, so this needs no
#     API key, network, microphone or Electron. It exists to catch the
#     two silent failures that leave FRIDAY deaf: a boot race that
#     builds more than one voice session, and a connect that hangs
#     forever without one.
# ------------------------------------------------------------------
echo "== Voice startup =="
if ! command -v node >/dev/null 2>&1; then
  echo "[WARN] node not found on PATH — skipping voice startup suite"
elif [ ! -f "Tests/test_voice_startup.js" ]; then
  echo "[WARN] Tests/test_voice_startup.js not found — skipping"
elif VOICE_OUTPUT="$(node Tests/test_voice_startup.js 2>&1)"; then
  echo "[PASS] $(echo "$VOICE_OUTPUT" | grep -E '^Voice startup:' || echo "Tests/test_voice_startup.js passed")"
else
  echo "[FAIL] Tests/test_voice_startup.js failed:"
  echo "$VOICE_OUTPUT" | grep -E '^\[FAIL\]|^  failed:|^Voice startup:'
  FAILURES=$((FAILURES + 1))
fi
echo ""

# ------------------------------------------------------------------
# 6. Memory v2 suite
#    Offline and deterministic: it runs entirely against a temporary
#    store (FRIDAY_MEMORY_DIR), so it never reads or writes real
#    memory, and needs no API key, network or microphone.
# ------------------------------------------------------------------
# ------------------------------------------------------------------
# 5c. Motion system suite
#     Contract checks over style.css and renderer.js plus the close
#     helper run against stubs. No Electron, no DOM engine. It exists
#     to stop the motion system decaying back into scattered literal
#     durations and bare element.remove() calls.
# ------------------------------------------------------------------
echo "== Motion system =="
if ! command -v node >/dev/null 2>&1; then
  echo "[WARN] node not found on PATH — skipping motion system suite"
elif [ ! -f "Tests/test_motion_system.js" ]; then
  echo "[WARN] Tests/test_motion_system.js not found — skipping"
elif MOTION_OUTPUT="$(node Tests/test_motion_system.js 2>&1)"; then
  echo "[PASS] $(echo "$MOTION_OUTPUT" | grep -E '^Motion system:' || echo "Tests/test_motion_system.js passed")"
else
  echo "[FAIL] Tests/test_motion_system.js failed:"
  echo "$MOTION_OUTPUT" | grep -E '^\[FAIL\]|^  failed:|^Motion system:'
  FAILURES=$((FAILURES + 1))
fi
echo ""

echo "== Memory =="
for suite in "Tests/test_memory_v2.py:Tests.test_memory_v2" "Tests/test_memory_v25.py:Tests.test_memory_v25"; do
  suite_path="${suite%%:*}"
  suite_module="${suite##*:}"

  if [ ! -f "$suite_path" ]; then
    echo "[WARN] ${suite_path} not found — skipping"
    continue
  fi

  if MEMORY_OUTPUT="$("$PYTHON_BIN" -m "$suite_module" 2>&1)"; then
    echo "[PASS] $(echo "$MEMORY_OUTPUT" | grep -E '^Memory v2' || echo "${suite_module} passed")"
  else
    echo "[FAIL] ${suite_module} failed:"
    echo "$MEMORY_OUTPUT" | grep -E '^\[FAIL\]|^  failed:|^Memory v2'
    FAILURES=$((FAILURES + 1))
  fi
done
echo ""

# ------------------------------------------------------------------
# 7. Tool Intelligence suite
#    Resolution only: it asks the intent layer what each request MEANS
#    and never runs a tool handler, so it touches no real state and
#    needs no API key, network, microphone or Ollama.
# ------------------------------------------------------------------
echo "== Tool Intelligence =="
if [ ! -f "Tests/test_tool_intelligence.py" ]; then
  echo "[WARN] Tests/test_tool_intelligence.py not found — skipping"
elif TOOLS_OUTPUT="$("$PYTHON_BIN" -m Tests.test_tool_intelligence 2>&1)"; then
  echo "[PASS] $(echo "$TOOLS_OUTPUT" | grep -E '^Tool Intelligence:' || echo "Tests.test_tool_intelligence passed")"
else
  echo "[FAIL] Tests.test_tool_intelligence failed:"
  echo "$TOOLS_OUTPUT" | grep -E '^\[FAIL\]|^  failed:|^Tool Intelligence:'
  FAILURES=$((FAILURES + 1))
fi
echo ""

# ------------------------------------------------------------------
# 8. Multi-Step Planning suite
#    Planning runs against the real registry; execution runs against
#    fixture tools, so nothing here opens a widget, calls Google
#    Calendar or reaches the weather API. No key, network or
#    microphone needed.
# ------------------------------------------------------------------
echo "== Workshop Files =="
if [ ! -f "Tests/test_workshop_files.py" ]; then
  echo "[WARN] Tests/test_workshop_files.py not found — skipping"
elif FILES_OUTPUT="$("$PYTHON_BIN" -m Tests.test_workshop_files 2>&1)"; then
  echo "[PASS] $(echo "$FILES_OUTPUT" | grep -E '^Workshop Files:' || echo "Tests.test_workshop_files passed")"
  # The suite works inside a scratch folder in the REAL Virtual Finder. If it
  # ever fails to remove it, say so on a passing run too — otherwise it quietly
  # leaves its litter in Jon's file store.
  echo "$FILES_OUTPUT" | grep -E '^\[WARN\]' || true
else
  echo "[FAIL] Tests.test_workshop_files failed:"
  echo "$FILES_OUTPUT" | grep -E '^\[FAIL\]|^\[WARN\]|^  failed:|^Workshop Files:'
  FAILURES=$((FAILURES + 1))
fi

# The panel itself, sliced out of renderer.js and run against stubs, so the
# navigation and the viewer are exercised rather than only the backend.
if ! command -v node >/dev/null 2>&1; then
  echo "[WARN] node not found on PATH — skipping Workshop Files panel suite"
elif [ ! -f "Tests/test_workshop_files_ui.js" ]; then
  echo "[WARN] Tests/test_workshop_files_ui.js not found — skipping"
elif FILES_UI_OUTPUT="$(node Tests/test_workshop_files_ui.js 2>&1)"; then
  echo "[PASS] $(echo "$FILES_UI_OUTPUT" | grep -E '^Workshop Files UI:' || echo "Tests/test_workshop_files_ui.js passed")"
else
  echo "[FAIL] Tests/test_workshop_files_ui.js failed:"
  echo "$FILES_UI_OUTPUT" | grep -E '^\[FAIL\]|^  failed:|^Workshop Files UI:'
  FAILURES=$((FAILURES + 1))
fi
echo ""

echo "== Multi-Step Planning =="
if [ ! -f "Tests/test_multi_step_planning.py" ]; then
  echo "[WARN] Tests/test_multi_step_planning.py not found — skipping"
elif PLAN_OUTPUT="$("$PYTHON_BIN" -m Tests.test_multi_step_planning 2>&1)"; then
  echo "[PASS] $(echo "$PLAN_OUTPUT" | grep -E '^Multi-Step Planning:' || echo "Tests.test_multi_step_planning passed")"
else
  echo "[FAIL] Tests.test_multi_step_planning failed:"
  echo "$PLAN_OUTPUT" | grep -E '^\[FAIL\]|^  failed:|^Multi-Step Planning:'
  FAILURES=$((FAILURES + 1))
fi
echo ""

# ------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------
if [ "$FAILURES" -eq 0 ]; then
  echo "FRIDAY health check passed."
  exit 0
else
  echo "FRIDAY health check failed: ${FAILURES} startup-blocking issue(s) found above."
  exit 1
fi
