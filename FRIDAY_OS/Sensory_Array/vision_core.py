import os
import time
import threading
from pathlib import Path

import cv2

try:
    from deepface import DeepFace
except Exception:
    DeepFace = None

from Core_Cognition.state_manager import set_presence_state

# ==========================================
# FRIDAY MK.1 VISION CORE
# Presence-only logic. No raw video is sent to HUD.
# ==========================================

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_PROFILE_DIR = os.getenv(
    "FRIDAY_PROFILE_DIR",
    str(PROJECT_ROOT / "Data" / "User_Profiles" / "Jon")
)

AUTHORIZED_NAME = (os.getenv("FRIDAY_AUTHORIZED_USER") or os.getenv("JARVIS_AUTHORIZED_USER", "Jon"))
CAMERA_INDEX = int((os.getenv("FRIDAY_CAMERA_INDEX") or os.getenv("JARVIS_CAMERA_INDEX", "0")))
SCAN_INTERVAL_SECONDS = float((os.getenv("FRIDAY_VISION_SCAN_INTERVAL") or os.getenv("JARVIS_VISION_SCAN_INTERVAL", "3.5")))
UNKNOWN_GRACE_SECONDS = float((os.getenv("FRIDAY_UNKNOWN_GRACE_SECONDS") or os.getenv("JARVIS_UNKNOWN_GRACE_SECONDS", "10.0")))
AUTHORIZED_HOLD_SECONDS = float((os.getenv("FRIDAY_AUTHORIZED_HOLD_SECONDS") or os.getenv("JARVIS_AUTHORIZED_HOLD_SECONDS", "180.0")))
VACANT_HOLD_SECONDS = float((os.getenv("FRIDAY_VACANT_HOLD_SECONDS") or os.getenv("JARVIS_VACANT_HOLD_SECONDS", "120.0")))
FACE_MATCH_THRESHOLD = float((os.getenv("FRIDAY_FACE_MATCH_THRESHOLD") or os.getenv("JARVIS_FACE_MATCH_THRESHOLD", "0.50")))
UNKNOWN_CONSECUTIVE_SCANS = int((os.getenv("FRIDAY_UNKNOWN_CONSECUTIVE_SCANS") or os.getenv("JARVIS_UNKNOWN_CONSECUTIVE_SCANS", "3")))
AUTHORIZED_CONSECUTIVE_SCANS = int((os.getenv("FRIDAY_AUTHORIZED_CONSECUTIVE_SCANS") or os.getenv("JARVIS_AUTHORIZED_CONSECUTIVE_SCANS", "1")))
LOW_LIGHT_THRESHOLD = float((os.getenv("FRIDAY_LOW_LIGHT_THRESHOLD") or os.getenv("JARVIS_LOW_LIGHT_THRESHOLD", "42.0")))
RECENT_AUTH_UNKNOWN_SUPPRESSION_SECONDS = float((os.getenv("FRIDAY_RECENT_AUTH_UNKNOWN_SUPPRESSION_SECONDS") or os.getenv("JARVIS_RECENT_AUTH_UNKNOWN_SUPPRESSION_SECONDS", "45.0")))

VISION_DEBUG = (os.getenv("FRIDAY_VISION_DEBUG") or os.getenv("JARVIS_VISION_DEBUG", "0")) == "1"

vision_stop_event = threading.Event()
vision_thread = None


def _debug(message: str) -> None:
    if VISION_DEBUG:
        print(f"[VisionCore] {message}")


def load_reference_images(profile_dir: str = DEFAULT_PROFILE_DIR):
    root = Path(profile_dir).expanduser()

    if not root.exists():
        _debug(f"Profile directory missing: {root}")
        return []

    valid_extensions = {".jpg", ".jpeg", ".png", ".webp"}
    refs = [
        str(path)
        for path in root.iterdir()
        if path.is_file() and path.suffix.lower() in valid_extensions
    ]

    _debug(f"Loaded {len(refs)} reference image(s).")
    return refs


def detect_faces(frame, detectors):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    equalized = cv2.equalizeHist(gray)
    detected = []

    for detector in detectors:
        if detector is None or detector.empty():
            continue

        faces = detector.detectMultiScale(
            equalized,
            scaleFactor=1.08,
            minNeighbors=4,
            minSize=(70, 70)
        )

        for face in faces:
            detected.append(tuple(int(value) for value in face))

    # De-duplicate overlapping detections from frontal/profile cascades.
    unique_faces = []

    for face in sorted(detected, key=lambda item: item[2] * item[3], reverse=True):
        x, y, w, h = face
        duplicate = False

        for existing in unique_faces:
            ex, ey, ew, eh = existing
            center_x = x + w / 2
            center_y = y + h / 2

            if ex <= center_x <= ex + ew and ey <= center_y <= ey + eh:
                duplicate = True
                break

        if not duplicate:
            unique_faces.append(face)

    return unique_faces


def is_low_light_frame(frame) -> bool:
    try:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        brightness = float(gray.mean())
        return brightness < LOW_LIGHT_THRESHOLD
    except Exception:
        return False


def crop_face(frame, face_box):
    x, y, w, h = face_box
    pad = int(max(w, h) * 0.22)
    x1 = max(0, x - pad)
    y1 = max(0, y - pad)
    x2 = min(frame.shape[1], x + w + pad)
    y2 = min(frame.shape[0], y + h + pad)
    return frame[y1:y2, x1:x2]


def verify_authorized(face_crop, reference_images) -> bool:
    if DeepFace is None or not reference_images:
        return False

    for reference_path in reference_images:
        try:
            result = DeepFace.verify(
                img1_path=face_crop,
                img2_path=reference_path,
                model_name="Facenet512",
                detector_backend="opencv",
                enforce_detection=False,
                silent=True
            )

            distance = float(result.get("distance", 1.0))
            verified = bool(result.get("verified", False))
            _debug(f"Face distance against {Path(reference_path).name}: {distance:.4f} verified={verified}")

            if verified or distance <= FACE_MATCH_THRESHOLD:
                return True

        except Exception as error:
            _debug(f"DeepFace verify failed for {reference_path}: {error}")

    return False


def any_authorized_face(frame, faces, reference_images) -> bool:
    """
    If Jon is one of multiple visible people, workstation remains AUTHORIZED.
    This verifies every detected face, not only the largest face.
    """
    if len(faces) == 0:
        return False

    sorted_faces = sorted(faces, key=lambda item: item[2] * item[3], reverse=True)

    for face_box in sorted_faces:
        face_crop = crop_face(frame, face_box)

        if face_crop is not None and verify_authorized(face_crop, reference_images):
            return True

    return False


def vision_loop(profile_dir: str = DEFAULT_PROFILE_DIR):
    reference_images = load_reference_images(profile_dir)

    frontal_detector_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    profile_detector_path = cv2.data.haarcascades + "haarcascade_profileface.xml"
    face_detectors = [
        cv2.CascadeClassifier(frontal_detector_path),
        cv2.CascadeClassifier(profile_detector_path)
    ]

    cap = cv2.VideoCapture(CAMERA_INDEX)

    if not cap.isOpened():
        _debug("Camera failed to open. Marking workstation vacant.")
        set_presence_state("VACANT", authorized_user="", confidence=0.0)
        return

    unknown_started_at = None
    vacant_started_at = None
    last_authorized_at = None
    last_state = None
    unknown_scan_count = 0
    authorized_scan_count = 0

    set_presence_state("SCANNING", authorized_user="", confidence=0.0)

    try:
        while not vision_stop_event.is_set():
            ok, frame = cap.read()

            if not ok or frame is None:
                unknown_started_at = None
                unknown_scan_count = 0
                authorized_scan_count = 0

                if last_authorized_at and time.monotonic() - last_authorized_at < AUTHORIZED_HOLD_SECONDS:
                    if last_state != "AUTHORIZED":
                        set_presence_state("AUTHORIZED", authorized_user=AUTHORIZED_NAME, confidence=0.65)
                        last_state = "AUTHORIZED"
                    time.sleep(SCAN_INTERVAL_SECONDS)
                    continue

                if vacant_started_at is None:
                    vacant_started_at = time.monotonic()

                if time.monotonic() - vacant_started_at >= VACANT_HOLD_SECONDS and last_state != "VACANT":
                    set_presence_state("VACANT", authorized_user="", confidence=0.0)
                    last_state = "VACANT"

                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            faces = detect_faces(frame, face_detectors)
            low_light = is_low_light_frame(frame)

            if len(faces) == 0:
                unknown_started_at = None
                unknown_scan_count = 0
                authorized_scan_count = 0

                if last_authorized_at and time.monotonic() - last_authorized_at < AUTHORIZED_HOLD_SECONDS:
                    if last_state != "AUTHORIZED":
                        set_presence_state("AUTHORIZED", authorized_user=AUTHORIZED_NAME, confidence=0.68)
                        last_state = "AUTHORIZED"
                    time.sleep(SCAN_INTERVAL_SECONDS)
                    continue

                if vacant_started_at is None:
                    vacant_started_at = time.monotonic()

                if low_light and last_state != "SCANNING":
                    set_presence_state("SCANNING", authorized_user="", confidence=0.0)
                    last_state = "SCANNING"

                if time.monotonic() - vacant_started_at >= VACANT_HOLD_SECONDS and last_state != "VACANT":
                    set_presence_state("VACANT", authorized_user="", confidence=0.0)
                    last_state = "VACANT"

                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            vacant_started_at = None

            if any_authorized_face(frame, faces, reference_images):
                unknown_started_at = None
                unknown_scan_count = 0
                authorized_scan_count += 1
                last_authorized_at = time.monotonic()

                if authorized_scan_count >= AUTHORIZED_CONSECUTIVE_SCANS and last_state != "AUTHORIZED":
                    set_presence_state(
                        "AUTHORIZED",
                        authorized_user=AUTHORIZED_NAME,
                        confidence=1.0
                    )
                    last_state = "AUTHORIZED"

                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            authorized_scan_count = 0
            now = time.monotonic()

            if last_authorized_at and now - last_authorized_at < RECENT_AUTH_UNKNOWN_SUPPRESSION_SECONDS:
                unknown_started_at = None
                unknown_scan_count = 0

                if last_state != "AUTHORIZED":
                    set_presence_state("AUTHORIZED", authorized_user=AUTHORIZED_NAME, confidence=0.60)
                    last_state = "AUTHORIZED"

                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            if low_light:
                unknown_started_at = None
                unknown_scan_count = 0

                if last_state != "SCANNING":
                    set_presence_state("SCANNING", authorized_user="", confidence=0.0)
                    last_state = "SCANNING"

                time.sleep(SCAN_INTERVAL_SECONDS)
                continue

            if unknown_started_at is None:
                unknown_started_at = now

            unknown_scan_count += 1

            if (
                unknown_scan_count >= UNKNOWN_CONSECUTIVE_SCANS
                and now - unknown_started_at >= UNKNOWN_GRACE_SECONDS
                and last_state != "UNAUTHORIZED"
            ):
                set_presence_state("UNAUTHORIZED", authorized_user="", confidence=0.0)
                last_state = "UNAUTHORIZED"
            elif last_state != "SCANNING":
                set_presence_state("SCANNING", authorized_user="", confidence=0.0)
                last_state = "SCANNING"

            time.sleep(SCAN_INTERVAL_SECONDS)

    finally:
        cap.release()


def start_vision_core_thread(profile_dir: str = DEFAULT_PROFILE_DIR):
    global vision_thread

    if vision_thread and vision_thread.is_alive():
        return vision_thread

    vision_stop_event.clear()
    vision_thread = threading.Thread(
        target=vision_loop,
        args=(profile_dir,),
        daemon=True
    )
    vision_thread.start()
    return vision_thread


def stop_vision_core() -> None:
    vision_stop_event.set()
