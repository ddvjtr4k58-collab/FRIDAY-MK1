import os
import sys
import re
import json
import time
import base64
import datetime
import platform
import shutil
import subprocess
import tempfile
import threading
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import uuid
from pathlib import Path

import cv2
import psutil
try:
    import pyautogui
except Exception:
    pyautogui = None
import PIL.Image
import PIL.ImageDraw as ImageDraw

from Core_Cognition.state_manager import (
    set_telemetry,
    set_hud_mode,
    ensure_workstation_visible,
    dock_orb_for_workspace,
    add_hud_card,
    remove_hud_card,
    clear_hud_cards,
    broadcast_state,
    broadcast_to_hud,
    update_hud_display,
    get_hud_state_snapshot,
    route_workshop_widget,
    add_workshop_widget
)
from Core_Cognition.settings_manager import get_setting

DEFAULT_LOCATION = (os.getenv("FRIDAY_DEFAULT_LOCATION") or os.getenv("JARVIS_DEFAULT_LOCATION", "Warrenville, IL"))
DEFAULT_LATITUDE = float((os.getenv("FRIDAY_DEFAULT_LATITUDE") or os.getenv("JARVIS_DEFAULT_LATITUDE", "41.8178")))
DEFAULT_LONGITUDE = float((os.getenv("FRIDAY_DEFAULT_LONGITUDE") or os.getenv("JARVIS_DEFAULT_LONGITUDE", "-88.1734")))
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA_DIR = os.path.join(PROJECT_ROOT, "Data")
NOTES_FILE = os.path.join(DATA_DIR, "notes.json")

CYAN = "\033[96m"
GRAY = "\033[90m"
RED = "\033[91m"
RESET = "\033[0m"

WIDGET_CACHE = {}
WIDGET_CACHE_LOCK = threading.Lock()
_pyautogui_warning_printed = False


def activate_widget_surface(reason: str = "") -> None:
    """Make sure the Workstation is actually on screen before a widget lands on it.

    Delegates to the single authority in state_manager. This used to flip the mode
    in Python with broadcast=False and never show the window, so a widget opened
    from the sleep screen was created correctly and then hidden behind it.
    """
    ensure_workstation_visible(reason)


def _warn_pyautogui_unavailable() -> None:
    global _pyautogui_warning_printed

    if not _pyautogui_warning_printed:
        print("[System] pyautogui unavailable, screen/keyboard automation disabled.")
        _pyautogui_warning_printed = True


def current_default_location() -> str:
    return str(get_setting("default_location", DEFAULT_LOCATION) or DEFAULT_LOCATION).strip() or DEFAULT_LOCATION


def _cache_get(key: str, ttl_seconds: float):
    now = time.time()

    with WIDGET_CACHE_LOCK:
        entry = WIDGET_CACHE.get(key)

        if not entry:
            return None

        timestamp, payload = entry

        if ttl_seconds is not None and now - timestamp > ttl_seconds:
            return None

        return payload


def _cache_set(key: str, payload):
    with WIDGET_CACHE_LOCK:
        WIDGET_CACHE[key] = (time.time(), payload)

    return payload


def _refresh_async(label: str, refresh_fn) -> None:
    def runner():
        try:
            refresh_fn()
        except Exception as error:
            print(f"{GRAY}[Async widget refresh failed: {label}: {error}]{RESET}")

    threading.Thread(target=runner, daemon=True).start()


def _weather_placeholder(location: str = None) -> dict:
    target_location = location or current_default_location()

    return {
        "location": target_location,
        "temperature": None,
        "feels_like": None,
        "wind": None,
        "precipitation": None,
        "condition": "Loading",
        "forecast": [],
        "source": "Refreshing"
    }


def _system_health_placeholder() -> dict:
    return {
        "generated_at": _now_iso(),
        "components": [
            _health_component("Diagnostics", "ONLINE", "Refreshing telemetry")
        ],
        "metrics": {}
    }


def _news_placeholder(active_tab: str = "briefing") -> dict:
    tab = (active_tab or "briefing").lower().strip() or "briefing"
    item = {
        "title": "Refreshing intel feed.",
        "source": "FRIDAY",
        "published": "",
        "link": ""
    }

    return {
        "headline": "INTEL BRIEFING",
        "summary": "Refreshing live sources.",
        "active_tab": tab,
        "tabs": {
            tab: {
                "label": tab.replace("_", " ").upper(),
                "region": "Live sources",
                "items": [item],
                "live_feed": None,
                "visual": None,
                "stats": {
                    "headline_count": 1,
                    "source_count": 1,
                    "top_sources": [{"name": "FRIDAY", "count": 1}]
                }
            }
        },
        "items": [item["title"]],
        "source": "Refreshing",
        "timestamp": _now_iso(),
        "layout": "intel_screen_ready"
    }


def _calendar_placeholder(status: str = "Refreshing calendar") -> dict:
    return {
        "connected": False,
        "status": status,
        "months": [],
        "today_events": [],
        "tomorrow_events": [],
        "upcoming_events": [],
        "priority_reminders": [],
        "birthdays": []
    }


def _music_placeholder() -> dict:
    return {
        "app": "Apple Music",
        "player_state": "stopped",
        "state": "stopped",
        "is_playing": False,
        "track": "No track loaded",
        "title": "No track loaded",
        "artist": "",
        "album": "",
        "shuffle": False,
        "repeat": "off",
        "source": "Apple Music"
    }


# ==========================================
# GENERAL HELPERS
# ==========================================
def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def store_information(info: str) -> str:
    """Save a durable fact about Jon.

    This used to append a line to memory.txt, which was then pasted whole into
    every system prompt. It now goes into the structured store (Memory v2), so
    the fact can be ranked, corrected, scoped and forgotten — and only reaches a
    prompt when it is actually relevant. See Core_Cognition/memory_manager.py.
    """
    try:
        from Core_Cognition import memory_manager

        record = memory_manager.remember(
            info,
            scope=memory_manager.SCOPE_USER,
            importance=3,
            source="tool"
        )

        if not record:
            return "Error: nothing to store."

        return "SUCCESS: stored." if record["created"] else "SUCCESS: existing memory updated."

    except Exception as e:
        return f"Error: {e}"


def load_notes() -> list[dict]:
    try:
        if not os.path.exists(NOTES_FILE):
            return []

        with open(NOTES_FILE, "r") as notes_file:
            notes = json.load(notes_file)

        if not isinstance(notes, list):
            return []

        return [note for note in notes if isinstance(note, dict)]
    except Exception:
        return []


def save_notes(notes: list[dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)

    with open(NOTES_FILE, "w") as notes_file:
        json.dump(notes[-200:], notes_file, indent=2)


def add_note(text: str, source: str = "voice") -> dict:
    clean_text = re.sub(r"\s+", " ", str(text or "")).strip(" \t\n\r,.;:!?")

    if not clean_text:
        return {}

    note = {
        "id": f"note_{uuid.uuid4().hex[:10]}",
        "text": clean_text,
        "created_at": _now_iso(),
        "source": source or "voice",
        "pinned": False
    }
    notes = load_notes()
    notes.append(note)
    save_notes(notes)
    return note


def clear_notes() -> None:
    save_notes([])


def delete_note(note_id: str) -> bool:
    target = str(note_id or "").strip()

    if not target:
        return False

    notes = load_notes()
    filtered = [note for note in notes if str(note.get("id") or "") != target]

    if len(filtered) == len(notes):
        return False

    save_notes(filtered)
    return True


def create_notes_widget() -> None:
    activate_widget_surface()
    add_hud_card(
        card_id="sticky_notes",
        title="NOTES",
        card_type="sticky_notes",
        x=72,
        y=90,
        width=420,
        height=520,
        broadcast=False,
        data={"notes": load_notes()}
    )
    broadcast_state()


def _health_component(name: str, status: str = "ONLINE", detail: str = "") -> dict:
    safe_status = str(status or "ONLINE").upper()

    if safe_status not in {"ONLINE", "OFFLINE", "DEGRADED"}:
        safe_status = "DEGRADED"

    return {
        "name": name,
        "status": safe_status,
        "detail": detail
    }


def _pmset_battery_status() -> str:
    try:
        result = subprocess.run(
            ["pmset", "-g", "batt"],
            capture_output=True,
            text=True,
            timeout=2
        )
        output = result.stdout.strip()
        match = re.search(r"(\d+)%", output)

        if match:
            state = "charging" if "charging" in output.lower() else "battery"
            return f"{match.group(1)} percent, {state}"

        return output.splitlines()[-1] if output else "unavailable"
    except Exception:
        return "unavailable"


def get_system_health_payload() -> dict:
    components = []
    metrics = {}

    try:
        presence_state = "SCANNING"
        presence_payload = {}

        try:
            from Core_Cognition.presence_manager import build_presence_payload
            presence_payload = build_presence_payload()
            presence_state = presence_payload.get("user_presence") or "Unknown"
        except Exception:
            try:
                from Core_Cognition.state_manager import get_presence_state
                presence_state = get_presence_state()
            except Exception:
                pass

        try:
            from Core_Cognition.camera_presence import build_camera_presence_payload
            camera_presence_payload = build_camera_presence_payload()
        except Exception:
            camera_presence_payload = {}

        metrics = {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "cpu_percent": psutil.cpu_percent(interval=0.2),
            "memory_percent": psutil.virtual_memory().percent,
            "disk_free_gb": round(shutil.disk_usage(PROJECT_ROOT).free / (1024 ** 3), 1),
            "disk_total_gb": round(shutil.disk_usage(PROJECT_ROOT).total / (1024 ** 3), 1),
            "battery": _pmset_battery_status(),
            "presence": presence_state,
            "presence_mode": presence_payload.get("presence_mode", "Disabled"),
            "presence_source": presence_payload.get("presence_source", "Mac idle"),
            "presence_user": presence_state,
            "presence_idle_seconds": presence_payload.get("idle_seconds"),
            "presence_away_timeout_minutes": presence_payload.get("away_timeout_minutes", 10),
            "presence_last_seen": presence_payload.get("last_seen_at"),
            "camera_presence_mode": camera_presence_payload.get("mode", "IDLE"),
            "camera_presence_status": camera_presence_payload.get("camera_status", "Unavailable"),
            "camera_presence_active_camera": camera_presence_payload.get("active_camera_label", "None"),
            "camera_presence_motion_score": camera_presence_payload.get("last_motion_score", 0)
        }

        components.append(_health_component("Brain", "ONLINE", "Core loop responding"))
        components.append(_health_component("HUD", "ONLINE", "Socket bridge on port 5050"))
        components.append(_health_component(
            "Presence Mode",
            "ONLINE" if presence_payload.get("presence_mode_enabled") else "OFFLINE",
            (
                f"{presence_state}, source Mac idle, timeout "
                f"{presence_payload.get('away_timeout_minutes', 10)} minutes"
            )
        ))
        components.append(_health_component(
            "Camera Presence",
            (
                "ONLINE"
                if camera_presence_payload.get("enabled") and camera_presence_payload.get("opencv_available")
                else "DEGRADED"
                if camera_presence_payload.get("enabled")
                else "OFFLINE"
            ),
            (
                f"{camera_presence_payload.get('mode', 'IDLE')}, "
                f"{camera_presence_payload.get('camera_status', 'Unavailable')}, "
                f"active {camera_presence_payload.get('active_camera_label', 'None')}"
            )
        ))

        fish_ready = bool(os.getenv("FISH_API_KEY") and os.getenv("VOICE_ID"))
        components.append(_health_component(
            "Fish Voice",
            "ONLINE" if fish_ready else "DEGRADED",
            "Voice credentials present" if fish_ready else "Voice credentials incomplete"
        ))

        components.append(_health_component("Microphone", "ONLINE", "Listening pipeline configured"))

        face_id_enabled = (os.getenv("FRIDAY_FACE_ID_ENABLED") or os.getenv("JARVIS_FACE_ID_ENABLED", "0")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on"
        }
        components.append(_health_component(
            "Camera / Vision",
            "ONLINE" if face_id_enabled else "OFFLINE",
            f"Presence {presence_state}" if face_id_enabled else "Face ID disabled"
        ))

        try:
            from Sensory_Array.calendar_tools import is_calendar_connected
            calendar_online = is_calendar_connected()
        except Exception:
            calendar_online = False

        components.append(_health_component(
            "Calendar",
            "ONLINE" if calendar_online else "OFFLINE",
            "Read-only Google Calendar" if calendar_online else "Calendar not connected"
        ))

        try:
            weather_payload = get_weather_payload(current_default_location())
            weather_online = weather_payload.get("temperature") is not None
        except Exception:
            weather_online = False

        components.append(_health_component(
            "Weather",
            "ONLINE" if weather_online else "DEGRADED",
            current_default_location() if weather_online else "Weather query unavailable"
        ))

        components.append(_health_component("Intel Briefing", "ONLINE", "News payload handlers loaded"))
        components.append(_health_component("Proactive Monitor", "ONLINE", "Background monitor available"))
        components.append(_health_component("Notifications", "ONLINE", "Toast and history channel available"))
        components.append(_health_component("Music Control", "ONLINE", "macOS Music bridge available"))
        components.append(_health_component("Tactical Map", "ONLINE", "OpenStreetMap renderer available"))

        return {
            "generated_at": _now_iso(),
            "components": components,
            "metrics": metrics
        }
    except Exception as error:
        return {
            "generated_at": _now_iso(),
            "components": [
                _health_component("Diagnostics", "DEGRADED", str(error))
            ],
            "metrics": metrics
        }


def create_system_health_widget(use_cache: bool = False, refresh_async: bool = False) -> None:
    cache_key = "system_health"
    payload = _cache_get(cache_key, 30) if use_cache else None

    if not payload:
        if use_cache or refresh_async:
            payload = _system_health_placeholder()
        else:
            payload = _cache_set(cache_key, get_system_health_payload())

    activate_widget_surface()
    add_hud_card(
        card_id="system_health",
        title="SYSTEM HEALTH",
        card_type="system_health",
        x=520,
        y=88,
        width=620,
        height=560,
        broadcast=False,
        data=payload
    )
    broadcast_state()

    if refresh_async:
        def refresh():
            fresh_payload = _cache_set(cache_key, get_system_health_payload())
            add_hud_card(
                card_id="system_health",
                title="SYSTEM HEALTH",
                card_type="system_health",
                x=520,
                y=88,
                width=620,
                height=560,
                broadcast=True,
                data=fresh_payload
            )

        _refresh_async("system_health", refresh)


def get_workshop_analytics_payload() -> dict:
    payload = get_system_health_payload()

    def weather_summary(weather_payload: dict) -> str:
        location = weather_payload.get("location") or "Location"
        temp = weather_payload.get("temperature")
        condition = weather_payload.get("condition") or "Unavailable"

        if temp is None:
            return f"{location}, unavailable"

        return f"{location}, {temp} degrees, {condition}"

    try:
        chicago_weather = get_weather_payload("Chicago, IL")
    except Exception:
        chicago_weather = {"location": "Chicago, IL", "condition": "Unavailable"}

    try:
        prishtina_weather = get_weather_payload("Prishtina, Kosovo")
    except Exception:
        prishtina_weather = {"location": "Prishtina, Kosovo", "condition": "Unavailable"}

    try:
        from Sensory_Array.calendar_tools import is_calendar_connected
        calendar_connected = is_calendar_connected()
    except Exception:
        calendar_connected = False

    try:
        from Core_Cognition.proactive_manager import get_watch_status
        proactive_status = get_watch_status()
    except Exception:
        proactive_status = "Proactive monitor available"

    fish_voice = "online" if os.getenv("FISH_API_KEY") and os.getenv("VOICE_ID") else "degraded"

    # Voice provider comes from the saved setting rather than audio_engine, so the
    # Workshop analytics card never drags the audio stack into a telemetry request.
    voice_provider = "friday_local"
    voice_local_name = ""

    try:
        from Core_Cognition.settings_manager import get_setting
        voice_provider = str(get_setting("voice_provider", "friday_local") or "friday_local")
        voice_local_name = str(get_setting("voice_local_name", "") or "")
    except Exception:
        pass

    payload["weather"] = {
        "chicago": weather_summary(chicago_weather),
        "prishtina": weather_summary(prishtina_weather)
    }
    # Compact, card-sized view of the local reading. The full forecast payload is
    # deliberately not embedded here — this is broadcast on a 45s analytics poll.
    payload["weather_card"] = {
        "location": chicago_weather.get("location") or "Location",
        "temperature": chicago_weather.get("temperature"),
        "condition": chicago_weather.get("condition") or "Unavailable"
    }
    payload["calendar_connected"] = calendar_connected
    payload["proactive_status"] = proactive_status
    payload["fish_voice"] = fish_voice
    payload["voice_provider"] = voice_provider
    payload["voice_local_name"] = voice_local_name
    payload["desk_view_available"] = False
    return payload


# ==========================================
# SYSTEM TELEMETRY
# ==========================================
def get_hardware_telemetry() -> str:
    cpu = psutil.cpu_percent(interval=0.5)
    ram = psutil.virtual_memory().percent
    battery = "Unknown"

    try:
        info = subprocess.check_output(["pmset", "-g", "batt"]).decode("utf-8")
        match = re.search(r"(\d+)%", info)

        if match:
            battery = f"{match.group(1)}%"
    except Exception:
        pass

    payload = {
        "cpu_percent": cpu,
        "ram_percent": ram,
        "battery": battery,
        "timestamp": _now_iso()
    }

    set_telemetry(payload)
    return f"CPU: {cpu}% | RAM: {ram}% | Battery: {battery}"


# ==========================================
# NEWS / WEATHER / MAP WIDGET DATA
# ==========================================
def fetch_google_news_items(url: str, limit: int = 6) -> list:
    items = []

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw_xml = urllib.request.urlopen(req, timeout=10).read()
        root = ET.fromstring(raw_xml)
        channel = root.find("channel")

        if channel is None:
            return []

        for item in channel.findall("item")[:limit]:
            title = item.findtext("title") or "Untitled story"
            source = item.findtext("source") or "News"
            published = item.findtext("pubDate") or ""
            link = item.findtext("link") or ""
            clean_title = re.sub(r"\s+-\s+[^-]+$", "", title).strip()

            items.append({
                "title": clean_title,
                "source": source,
                "published": published,
                "link": link
            })

    except Exception:
        return []

    return items


# ==========================================
# VISUAL PAYLOAD HELPERS
# ==========================================
def get_market_visual_payload() -> dict:
    """
    Lightweight visual market telemetry for the Intel Briefing.
    This is chart-ready static/synthetic trend data for HUD visuals.
    Real quote APIs can replace this later without changing renderer.js.
    """
    return {
        "title": "MARKET WATCH",
        "cards": [
            {
                "symbol": "S&P 500",
                "label": "US EQUITIES",
                "change": "+0.42%",
                "direction": "up",
                "trend": [42, 46, 44, 51, 49, 55, 59, 62, 58, 64, 67, 71]
            },
            {
                "symbol": "NASDAQ",
                "label": "TECH INDEX",
                "change": "+0.68%",
                "direction": "up",
                "trend": [36, 39, 41, 45, 43, 50, 54, 57, 61, 63, 66, 72]
            },
            {
                "symbol": "DOW",
                "label": "BLUE CHIPS",
                "change": "-0.12%",
                "direction": "down",
                "trend": [61, 59, 58, 60, 56, 54, 55, 51, 49, 50, 47, 45]
            },
            {
                "symbol": "BTC",
                "label": "CRYPTO",
                "change": "+1.84%",
                "direction": "up",
                "trend": [28, 34, 31, 39, 44, 42, 51, 49, 58, 63, 61, 74]
            }
        ],
        "macro": [
            {"label": "Inflation Watch", "value": "Elevated", "level": 72},
            {"label": "Fed Sensitivity", "value": "High", "level": 81},
            {"label": "Risk Appetite", "value": "Mixed", "level": 54}
        ]
    }


def get_conflict_visual_payload() -> dict:
    """
    Tactical conflict-map metadata for HUD visuals.
    Coordinates are approximate visual anchors, not live targeting data.
    """
    return {
        "title": "CONFLICT WATCH",
        "map_label": "GLOBAL HOTSPOTS",
        "hotspots": [
            {
                "label": "Ukraine",
                "region": "Eastern Europe",
                "severity": "high",
                "x": 56,
                "y": 35,
                "pulse": 92
            },
            {
                "label": "Gaza / Israel",
                "region": "Middle East",
                "severity": "high",
                "x": 58,
                "y": 46,
                "pulse": 88
            },
            {
                "label": "Iran / Gulf",
                "region": "Middle East",
                "severity": "elevated",
                "x": 63,
                "y": 48,
                "pulse": 74
            },
            {
                "label": "Red Sea",
                "region": "Maritime Corridor",
                "severity": "elevated",
                "x": 58,
                "y": 55,
                "pulse": 68
            }
        ],
        "threat_bars": [
            {"label": "Military", "level": 78},
            {"label": "Diplomatic", "level": 61},
            {"label": "Energy", "level": 53},
            {"label": "Cyber", "level": 48}
        ]
    }


def get_news_payload(active_tab: str = "briefing") -> dict:
    feeds = {
        "briefing": {
            "label": "BRIEFING",
            "region": "Global / United States",
            "url": "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en",
            "limit": 8,
            "live_feed": {
                "label": "LIVE FEED",
                "provider": "PBS NewsHour",
                "embed_url": "",
                "live_page_url": "https://www.pbs.org/newshour/",
                "fallback_url": "https://www.youtube.com/@PBSNewsHour/streams"
            }
        },
        "kosovo": {
            "label": "KOSOVO",
            "region": "Kosovo / Balkans",
            "url": "https://news.google.com/rss/search?q=Kosovo%20OR%20Kosova%20OR%20Pristina%20OR%20Prishtina%20OR%20Kurti%20OR%20Vucic%20when:2d&hl=en-US&gl=US&ceid=US:en",
            "limit": 10,
            "live_feed": None
        },
        "markets": {
            "label": "MARKETS",
            "region": "Finance / Economy",
            "url": "https://news.google.com/rss/search?q=markets%20OR%20stocks%20OR%20inflation%20OR%20Federal%20Reserve%20OR%20economy%20when:1d&hl=en-US&gl=US&ceid=US:en",
            "limit": 8,
            "live_feed": None
        },
        "conflict": {
            "label": "ACTIVE WARS",
            "region": "Wars / Security",
            "url": "https://news.google.com/rss/search?q=(Ukraine%20war%20OR%20Gaza%20war%20OR%20Iran%20conflict%20OR%20Red%20Sea%20attacks%20OR%20ceasefire)%20when:1d&hl=en-US&gl=US&ceid=US:en",
            "limit": 8,
            "live_feed": None
        },
        "technology": {
            "label": "TECH",
            "region": "Technology / AI",
            "url": "https://news.google.com/rss/search?q=technology%20OR%20AI%20OR%20cybersecurity%20OR%20OpenAI%20OR%20Apple%20OR%20Google%20when:1d&hl=en-US&gl=US&ceid=US:en",
            "limit": 8,
            "live_feed": None
        }
    }

    visual_payloads = {
        "markets": get_market_visual_payload(),
        "conflict": get_conflict_visual_payload()
    }

    tabs = {}

    for key, feed in feeds.items():
        feed_items = fetch_google_news_items(feed["url"], limit=feed["limit"])

        if not feed_items:
            feed_items = [
                {
                    "title": f"{feed['label']} feed unavailable. Check network connectivity.",
                    "source": "FRIDAY",
                    "published": "",
                    "link": ""
                }
            ]

        source_counts = {}

        for item in feed_items:
            source_name = item.get("source") or "News"
            source_counts[source_name] = source_counts.get(source_name, 0) + 1

        tabs[key] = {
            "label": feed["label"],
            "region": feed["region"],
            "items": feed_items,
            "live_feed": feed.get("live_feed"),
            "visual": visual_payloads.get(key),
            "stats": {
                "headline_count": len(feed_items),
                "source_count": len(source_counts),
                "top_sources": [
                    {"name": source, "count": count}
                    for source, count in sorted(source_counts.items(), key=lambda pair: pair[1], reverse=True)[:4]
                ]
            }
        }

    normalized_tab = (active_tab or "briefing").lower().strip()

    tab_aliases = {
        "us": "briefing",
        "usa": "briefing",
        "headlines": "briefing",
        "latest": "briefing",
        "finance": "markets",
        "financial": "markets",
        "market": "markets",
        "war": "conflict",
        "wars": "conflict",
        "active wars": "conflict",
        "conflict": "conflict",
        "conflicts": "conflict",
        "security": "conflict",
        "financial markets": "markets",
        "stock market": "markets",
        "stocks": "markets",
        "economy": "markets",
        "u.s.": "briefing",
        "united states": "briefing",
        "america": "briefing",
        "ai": "technology",
        "tech": "technology",
        "kosova": "kosovo"
    }

    normalized_tab = tab_aliases.get(normalized_tab, normalized_tab)

    if normalized_tab not in tabs:
        normalized_tab = "briefing"

    active_items = tabs[normalized_tab]["items"]

    return {
        "headline": "INTEL BRIEFING",
        "summary": "Live multi-source scan complete.",
        "active_tab": normalized_tab,
        "tabs": tabs,
        "items": [item["title"] for item in active_items],
        "source": "Google News RSS",
        "timestamp": _now_iso(),
        "layout": "intel_screen_ready"
    }


def create_intel_widget(
    active_tab: str = "briefing",
    use_cache: bool = False,
    refresh_async: bool = False,
    preferred_workspace: str = None
) -> None:
    activate_widget_surface("intel")
    normalized_tab = (active_tab or "briefing").lower().strip() or "briefing"
    cache_key = f"news:{normalized_tab}"
    payload = _cache_get(cache_key, 300) if use_cache else None

    if not payload:
        if use_cache or refresh_async:
            payload = _news_placeholder(normalized_tab)
        else:
            payload = _cache_set(cache_key, get_news_payload(active_tab=normalized_tab))

    add_hud_card(
        card_id="news_briefing",
        title="INTEL BRIEFING",
        card_type="news",
        x=520,
        y=52,
        width=860,
        height=650,
        broadcast=False,
        data=payload,
        preferred_workspace=preferred_workspace
    )
    broadcast_state()

    if refresh_async:
        def refresh():
            fresh_payload = _cache_set(cache_key, get_news_payload(active_tab=normalized_tab))

            if not any(
                card.get("id") == "news_briefing"
                for card in get_hud_state_snapshot().get("active_cards", [])
                if isinstance(card, dict)
            ):
                return

            add_hud_card(
                card_id="news_briefing",
                title="INTEL BRIEFING",
                card_type="news",
                x=520,
                y=52,
                width=860,
                height=650,
                broadcast=False,
                data=fresh_payload,
                preferred_workspace=preferred_workspace
            )
            broadcast_state()

        _refresh_async("intel", refresh)


# ==========================================
# MUSIC PAYLOAD (Apple Music)
# ==========================================
_MUSIC_ARTWORK_CACHE = {"key": None, "uri": ""}
_MUSIC_PLAYLIST_CACHE = {"at": 0.0, "names": []}


def _music_artwork_data_uri(title: str, album: str) -> str:
    """Real cover art for the current track, as a data URI.

    Extracted only when the track actually changes: pulling raw artwork costs an
    AppleScript round trip plus a file read, and the music payload is polled far
    more often than the track changes. Returns "" when Music has no artwork, so
    the widget shows its own placeholder rather than a broken image.
    """
    key = f"{title}|{album}"

    if _MUSIC_ARTWORK_CACHE["key"] == key:
        return _MUSIC_ARTWORK_CACHE["uri"]

    _MUSIC_ARTWORK_CACHE["key"] = key
    _MUSIC_ARTWORK_CACHE["uri"] = ""

    if not title or title == "No track loaded":
        return ""

    target = Path(tempfile.gettempdir()) / "friday_music_artwork.bin"
    script = f'''
    tell application "Music"
        if not running then return "none"
        try
            set artworkData to raw data of artwork 1 of current track
        on error
            return "none"
        end try
    end tell

    try
        set outFile to open for access POSIX file "{target}" with write permission
        set eof outFile to 0
        write artworkData to outFile
        close access outFile
        return "ok"
    on error
        try
            close access POSIX file "{target}"
        end try
        return "none"
    end try
    '''

    try:
        result = subprocess.check_output(
            ["osascript", "-e", script],
            stderr=subprocess.DEVNULL,
            timeout=6
        ).decode("utf-8", errors="ignore").strip()

        if result != "ok" or not target.exists():
            return ""

        raw = target.read_bytes()

        try:
            target.unlink()
        except OSError:
            pass

        # Sniff the container rather than trusting Music's reported format.
        if raw[:3] == b"\xff\xd8\xff":
            mime = "image/jpeg"
        elif raw[:8] == b"\x89PNG\r\n\x1a\n":
            mime = "image/png"
        else:
            return ""

        # A cover larger than this is not worth pushing through the socket on
        # every widget refresh; the widget renders it at a few hundred pixels.
        if len(raw) > 3 * 1024 * 1024:
            return ""

        _MUSIC_ARTWORK_CACHE["uri"] = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
        return _MUSIC_ARTWORK_CACHE["uri"]

    except Exception:
        return ""


def _music_playlists(limit: int = 24) -> list:
    """User playlist names, refreshed at most once a minute."""
    now = time.time()

    if now - _MUSIC_PLAYLIST_CACHE["at"] < 60 and _MUSIC_PLAYLIST_CACHE["names"]:
        return _MUSIC_PLAYLIST_CACHE["names"]

    script = '''
    tell application "Music"
        if not running then return ""
        try
            set output to ""
            repeat with p in user playlists
                set output to output & (name of p) & "\n"
            end repeat
            return output
        on error
            return ""
        end try
    end tell
    '''

    try:
        output = subprocess.check_output(
            ["osascript", "-e", script],
            stderr=subprocess.DEVNULL,
            timeout=6
        ).decode("utf-8", errors="ignore")

        names = [line.strip() for line in output.splitlines() if line.strip()][:limit]
        _MUSIC_PLAYLIST_CACHE["at"] = now
        _MUSIC_PLAYLIST_CACHE["names"] = names
        return names

    except Exception:
        _MUSIC_PLAYLIST_CACHE["at"] = now
        return []


def get_music_payload() -> dict:
    """
    Reads Apple Music state through AppleScript.
    Does not require streaming library data to the HUD.
    """
    default_payload = {
        "app": "Apple Music",
        "player_state": "stopped",
        "state": "stopped",
        "is_playing": False,
        "track": "No track loaded",
        "title": "No track loaded",
        "artist": "",
        "album": "",
        "shuffle": False,
        "repeat": "off",
        "source": "Apple Music"
    }

    script = r'''
    tell application "Music"
        if not running then
            return "stopped|No track loaded|||false|off"
        end if

        set playerStateText to player state as text
        set trackName to "No track loaded"
        set artistName to ""
        set albumName to ""
        set shuffleText to shuffle enabled as text
        set repeatText to song repeat as text
        set trackDuration to 0
        set trackPosition to 0

        try
            set trackName to name of current track
            set artistName to artist of current track
            set albumName to album of current track
            set trackDuration to duration of current track
        end try

        try
            set trackPosition to player position
        end try

        return playerStateText & "|" & trackName & "|" & artistName & "|" & albumName & "|" & shuffleText & "|" & repeatText & "|" & (trackDuration as text) & "|" & (trackPosition as text)
    end tell
    '''

    try:
        output = subprocess.check_output(
            ["osascript", "-e", script],
            stderr=subprocess.DEVNULL,
            timeout=5
        ).decode("utf-8", errors="ignore").strip()

        parts = output.split("|")

        while len(parts) < 8:
            parts.append("")

        state, title, artist, album, shuffle_text, repeat_text, duration_text, position_text = parts[:8]
        state = str(state or "stopped").lower()

        if state not in {"playing", "paused", "stopped"}:
            state = "stopped"

        def as_seconds(value):
            try:
                seconds = float(str(value).strip())
            except (TypeError, ValueError):
                return None

            return seconds if seconds > 0 else None

        return {
            "app": "Apple Music",
            "player_state": state,
            "state": state,
            "is_playing": state == "playing",
            "track": title or "No track loaded",
            "title": title or "No track loaded",
            "artist": artist or "",
            "album": album or "",
            "shuffle": str(shuffle_text).lower() == "true",
            "repeat": repeat_text or "off",
            # None rather than 0 when Music does not report them, so the widget can
            # hide the progress bar instead of drawing a fake 0:00 / 0:00.
            "duration": as_seconds(duration_text),
            "position": as_seconds(position_text) or (0.0 if as_seconds(duration_text) else None),
            "artwork": _music_artwork_data_uri(title, album),
            "playlists": _music_playlists(),
            "source": "Apple Music"
        }

    except Exception:
        return default_payload


def create_music_widget(use_cache: bool = False, refresh_async: bool = False) -> None:
    activate_widget_surface("music")
    cache_key = "music"
    payload = _cache_get(cache_key, 5) if use_cache else None

    if not payload:
        if use_cache or refresh_async:
            payload = _music_placeholder()
        else:
            payload = _cache_set(cache_key, get_music_payload())

    add_hud_card(
        card_id="music_controls",
        title="MUSIC CONTROL",
        card_type="music",
        x=64,
        y=500,
        width=520,
        height=300,
        broadcast=False,
        data=payload
    )
    broadcast_state()

    if refresh_async:
        def refresh():
            fresh_payload = _cache_set(cache_key, get_music_payload())
            add_hud_card(
                card_id="music_controls",
                title="MUSIC CONTROL",
                card_type="music",
                x=64,
                y=500,
                width=520,
                height=300,
                broadcast=True,
                data=fresh_payload
            )

        _refresh_async("music", refresh)


def get_map_payload(destination: str) -> dict:
    destination_clean = (destination or "").strip()

    if not destination_clean or destination_clean.lower() in {"map", "maps", "open map", "open maps", "planet earth", "earth", "world"}:
        return {
            "destination": "Planet Earth",
            "query": "Planet Earth",
            "lat": 20.0,
            "lon": 0.0,
            "zoom": 2,
            "image_url": "",
            "map_url": "https://www.openstreetmap.org/#map=2/20.0/0.0",
            "mode": "overview",
            "source": "OpenStreetMap Tiles"
        }

    encoded_destination = urllib.parse.quote(destination_clean)

    lat = None
    lon = None
    display_name = destination_clean

    try:
        geocode_url = f"https://nominatim.openstreetmap.org/search?q={encoded_destination}&format=json&limit=1"
        req = urllib.request.Request(
            geocode_url,
            headers={"User-Agent": "FRIDAY-MK1-Local-Assistant/1.0"}
        )
        results = json.loads(urllib.request.urlopen(req, timeout=8).read().decode("utf-8"))

        if results:
            lat = float(results[0].get("lat"))
            lon = float(results[0].get("lon"))
            display_name = results[0].get("display_name", destination_clean).split(",")[0]
    except Exception:
        lat = None
        lon = None

    display_name = normalize_map_destination_label(display_name, lat, lon)

    return {
        "destination": display_name,
        "query": destination_clean,
        "lat": lat,
        "lon": lon,
        "zoom": 11,
        "image_url": "",
        "map_url": f"https://www.openstreetmap.org/search?query={encoded_destination}",
        "mode": "travel",
        "source": "OpenStreetMap Tiles"
    }


def normalize_map_destination_label(label: str, lat=None, lon=None) -> str:
    value = (label or "").strip()
    lower = value.lower()

    if is_within_kosovo(lat, lon):
        return "Kosovo"

    if (
        lower == "kosovo" or
        lower == "republic of kosovo" or
        lower == "kosova" or
        lower == "republic of kosova" or
        "kosovo" in lower or
        "kosova" in lower or
        "republic of serbia" in lower or
        "autonomous province of kosovo" in lower or
        "autonomous province of kosovo and metohija" in lower or
        "kosovo and metohija" in lower
    ):
        return "Kosovo"

    return value or DEFAULT_LOCATION


def is_within_kosovo(lat, lon) -> bool:
    try:
        lat_value = float(lat)
        lon_value = float(lon)
    except Exception:
        return False

    return (
        41.80 <= lat_value <= 43.35 and
        20.00 <= lon_value <= 21.90
    )


def extract_map_destination(command: str):
    if not command:
        return None

    cmd = command.lower().strip()

    blocked_phrases = [
        "go to sleep",
        "stand by mode",
        "standby mode",
        "sleep screen",
        "turn off",
        "jarvis turn off",
        "go offline",
        "shutdown",
        "clear hud",
        "clear widgets",
        "remove widgets",
        "close widget",
        "close the widget"
    ]

    for blocked in blocked_phrases:
        if blocked in cmd:
            return None

    open_map_phrases = {
        "open map",
        "open maps",
        "open the map",
        "open the maps",
        "show map",
        "show maps",
        "show me the map",
        "show me maps",
        "launch map",
        "launch maps",
        "tactical map",
        "open tactical map",
        "friday open map",
        "friday open maps",
        "friday open the map",
        "friday open the maps",
        "jarvis open map",
        "jarvis open maps",
        "jarvis open the map",
        "jarvis open the maps"
    }

    if cmd in open_map_phrases:
        return "Planet Earth"

    patterns = [
        r"(?:go to|travel to|navigate to|show me|open map for|map of)\s+([a-zA-Z\s,]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, cmd, re.IGNORECASE)

        if match:
            destination = match.group(1).strip()

            if len(destination) < 2:
                return None

            return destination.title()

    return None


def _weather_condition(code) -> str:
    if code in {0, 1}:
        return "Clear"

    if code in {2, 3}:
        return "Cloudy"

    try:
        return "Rain" if code is not None and int(code) >= 51 else "Conditions"
    except (TypeError, ValueError):
        return "Conditions"


def _build_daily_forecast(daily: dict) -> list:
    """Seven days keyed by weekday name.

    The hourly list only covers a few hours, so a question about tomorrow or about a
    named day had no data behind it at all — and with nothing to answer from, the only
    move left was to open the widget. Carrying the weekday name means "what about
    Saturday" resolves without her having to work out the date.
    """
    if not isinstance(daily, dict):
        return []

    dates = daily.get("time") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    chances = daily.get("precipitation_probability_max") or []
    codes = daily.get("weather_code") or []

    def at(values, index):
        return values[index] if index < len(values) else None

    def rounded(value):
        try:
            return round(float(value))
        except (TypeError, ValueError):
            return None

    days = []

    for index, date_value in enumerate(dates[:7]):
        try:
            parsed = datetime.datetime.strptime(str(date_value), "%Y-%m-%d").date()
            weekday = parsed.strftime("%A")
        except (TypeError, ValueError):
            weekday = ""

        if index == 0:
            label = "today"
        elif index == 1:
            label = "tomorrow"
        else:
            label = weekday.lower()

        days.append({
            "date": str(date_value),
            "day": weekday,
            "when": label,
            "high": rounded(at(highs, index)),
            "low": rounded(at(lows, index)),
            "precip_chance": at(chances, index),
            "condition": _weather_condition(at(codes, index)),
        })

    return days


def get_weather_payload(location: str = None) -> dict:
    target_location = location or current_default_location()

    try:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={DEFAULT_LATITUDE}"
            f"&longitude={DEFAULT_LONGITUDE}"
            "&current=temperature_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m"
            # Humidity, pressure, visibility and UV are all real Open-Meteo fields and
            # cost nothing extra on a request already being made. Without them the
            # weather app would have had to either omit those readings or invent
            # them, and inventing them is not an option.
            ",relative_humidity_2m,surface_pressure,wind_direction_10m,is_day"
            "&hourly=temperature_2m,precipitation_probability,weather_code,visibility,uv_index"
            # Named days come from the same request, so a follow-up like "what about
            # Saturday?" or "which day is warmer?" is answered from what is already in
            # hand rather than costing another round trip mid-conversation.
            "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code"
            ",sunrise,sunset,uv_index_max"
            "&forecast_days=7"
            "&temperature_unit=fahrenheit"
            "&wind_speed_unit=mph"
            "&timezone=auto"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        payload = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
        current = payload.get("current", {})
        hourly = payload.get("hourly", {})
        daily = payload.get("daily", {})

        temp = current.get("temperature_2m")
        feels = current.get("apparent_temperature")
        wind = current.get("wind_speed_10m")
        precip = current.get("precipitation")
        code = current.get("weather_code")

        condition = _weather_condition(code)

        # Start the hourly strip at the CURRENT hour, not at midnight.
        #
        # This used to slice [:6] from the top of the array, which is always
        # 00:00-05:00 — so the widget showed last night's weather all day. The
        # index of "now" is found by matching the API's own timestamps.
        hourly_times = hourly.get("time", [])
        hourly_temps = hourly.get("temperature_2m", [])
        hourly_precip = hourly.get("precipitation_probability", [])
        hourly_codes = hourly.get("weather_code", [])
        hourly_visibility = hourly.get("visibility", [])
        hourly_uv = hourly.get("uv_index", [])

        # Match against the API's OWN clock, not this Mac's.
        #
        # Open-Meteo returns times in the forecast location's timezone
        # (timezone=auto). Comparing them to local time silently failed whenever
        # the two differed — which is the normal case here, since the configured
        # location is in Central time — so the match never hit and the strip fell
        # back to index 0: midnight, all day long.
        start = 0
        now_prefix = str(current.get("time") or "")[:13]

        if not now_prefix:
            now_prefix = datetime.datetime.now().strftime("%Y-%m-%dT%H")

        for index, stamp in enumerate(hourly_times):
            if str(stamp).startswith(now_prefix):
                start = index
                break

        def hourly_at(values, index):
            return values[index] if index < len(values) else None

        forecast = []

        for offset in range(24):
            index = start + offset

            if index >= len(hourly_times):
                break

            stamp = hourly_times[index]
            label = stamp.split("T")[-1][:5] if "T" in stamp else stamp
            temp = hourly_at(hourly_temps, index)

            forecast.append({
                "time": label,
                "iso": stamp,
                "temp": round(float(temp)) if temp is not None else None,
                "precip": hourly_at(hourly_precip, index),
                "condition": _weather_condition(hourly_at(hourly_codes, index)),
                "is_now": offset == 0
            })

        # Present-moment readings the hourly series carries but `current` does not.
        visibility_m = hourly_at(hourly_visibility, start)
        uv_now = hourly_at(hourly_uv, start)

        return {
            "location": target_location,
            "temperature": round(float(temp)) if temp is not None else None,
            "feels_like": round(float(feels)) if feels is not None else None,
            "wind": round(float(wind)) if wind is not None else None,
            "precipitation": precip,
            "condition": condition,
            "humidity": current.get("relative_humidity_2m"),
            "pressure": round(float(current["surface_pressure"])) if current.get("surface_pressure") is not None else None,
            "wind_direction": current.get("wind_direction_10m"),
            "is_day": bool(current.get("is_day", 1)),
            # Open-Meteo reports visibility in metres; miles is what the rest of
            # this payload speaks (mph, Fahrenheit).
            "visibility_miles": round(float(visibility_m) / 1609.34, 1) if visibility_m is not None else None,
            "uv_index": round(float(uv_now), 1) if uv_now is not None else None,
            "sunrise": (daily.get("sunrise") or [None])[0],
            "sunset": (daily.get("sunset") or [None])[0],
            "forecast": forecast,
            "daily": _build_daily_forecast(daily),
            "source": "Open-Meteo"
        }

    except Exception:
        return {
            "location": target_location,
            "temperature": None,
            "feels_like": None,
            "wind": None,
            "precipitation": None,
            "condition": "Unavailable",
            "forecast": [],
            "source": "Unavailable"
        }


def create_weather_widget(location: str = None, use_cache: bool = False, refresh_async: bool = False) -> None:
    target_location = location or current_default_location()
    cache_key = f"weather:{target_location.lower()}"
    payload = _cache_get(cache_key, 300) if use_cache else None

    if not payload:
        if use_cache or refresh_async:
            payload = _weather_placeholder(target_location)
        else:
            payload = _cache_set(cache_key, get_weather_payload(target_location))

    activate_widget_surface()
    add_hud_card(
        card_id="weather_current",
        title="LOCAL WEATHER",
        card_type="weather",
        x=64,
        y=76,
        width=520,
        height=390,
        broadcast=False,
        data=payload
    )
    broadcast_state()

    if refresh_async:
        def refresh():
            fresh_payload = _cache_set(cache_key, get_weather_payload(target_location))
            add_hud_card(
                card_id="weather_current",
                title="LOCAL WEATHER",
                card_type="weather",
                x=64,
                y=76,
                width=520,
                height=390,
                broadcast=True,
                data=fresh_payload
            )

        _refresh_async("weather", refresh)


def get_weather(location: str = None) -> str:
    target_location = location or current_default_location()
    payload = get_weather_payload(target_location)
    _cache_set(f"weather:{target_location.lower()}", payload)

    activate_widget_surface()

    add_hud_card(
        card_id="weather_current",
        title="LOCAL WEATHER",
        card_type="weather",
        x=64,
        y=76,
        width=520,
        height=390,
        broadcast=False,
        data=payload
    )

    broadcast_state()

    if payload.get("temperature") is not None:
        feels = payload.get("feels_like")

        if feels is not None:
            return f"{target_location} is {payload.get('temperature')} degrees. Feels like {feels} degrees."

        return f"{target_location} is {payload.get('temperature')} degrees."

    return "Weather telemetry unavailable"


def open_map_widget(destination: str, preferred_workspace: str = None) -> str:
    target = (destination or "").strip() or "Planet Earth"
    payload = get_map_payload(target)
    activate_widget_surface()

    add_hud_card(
        card_id="map_fullscreen",
        title="TACTICAL MAP",
        card_type="map",
        x=0,
        y=0,
        width=1536,
        height=864,
        broadcast=False,
        data=payload,
        preferred_workspace=preferred_workspace
    )

    broadcast_state()
    return f"Map loaded for {payload.get('destination') or target}."


def open_calendar_page(preferred_workspace: str = None, use_cache: bool = False, refresh_async: bool = False) -> str:
    workspace = (
        preferred_workspace
        if preferred_workspace in {"main", "secondary"}
        else route_workshop_widget("calendar", "large")
    )

    cache_key = "calendar_page"
    payload = _cache_get(cache_key, 120) if use_cache else None

    if not payload:
        if use_cache or refresh_async:
            payload = _calendar_placeholder()
        else:
            try:
                from Sensory_Array.calendar_tools import get_calendar_widget_payload
                payload = _cache_set(cache_key, get_calendar_widget_payload())
            except Exception as error:
                payload = _calendar_placeholder(f"Calendar unavailable: {error}")

    payload["workspace"] = workspace
    add_workshop_widget(
        "calendar",
        {
            "id": "calendar_page",
            "title": "CALENDAR",
            "size_hint": "large"
        },
        preferred_workspace=workspace
    )

    activate_widget_surface()
    remove_hud_card("calendar_agenda", broadcast=False)
    broadcast_state()
    broadcast_to_hud("open_calendar_page", payload)

    if refresh_async:
        def refresh():
            try:
                from Sensory_Array.calendar_tools import get_calendar_widget_payload
                fresh_payload = _cache_set(cache_key, get_calendar_widget_payload())
            except Exception as error:
                fresh_payload = _calendar_placeholder(f"Calendar unavailable: {error}")

            fresh_payload["workspace"] = workspace
            broadcast_to_hud("open_calendar_page", fresh_payload)

        _refresh_async("calendar", refresh)

    return "Calendar opened"


def seed_context_cards(topic: str, location: str = None, replace_existing: bool = True) -> None:
    topic_lower = (topic or "").lower()
    target_location = location or current_default_location()

    if replace_existing:
        clear_hud_cards(broadcast=False)

    activate_widget_surface()

    if "note" in topic_lower or "sticky" in topic_lower:
        add_hud_card(
            card_id="sticky_notes",
            title="NOTES",
            card_type="sticky_notes",
            x=72,
            y=90,
            width=420,
            height=520,
            broadcast=False,
            data={"notes": load_notes()}
        )
        broadcast_state()
        return

    if (
        "diagnostic" in topic_lower
        or "system health" in topic_lower
        or "system status" in topic_lower
        or "hardware" in topic_lower
    ):
        add_hud_card(
            card_id="system_health",
            title="SYSTEM HEALTH",
            card_type="system_health",
            x=520,
            y=88,
            width=620,
            height=560,
            broadcast=False,
            data=get_system_health_payload()
        )
        broadcast_state()
        return

    if "calendar" in topic_lower or "agenda" in topic_lower:
        try:
            from Sensory_Array.calendar_tools import get_calendar_widget_payload
            calendar_payload = get_calendar_widget_payload()
        except Exception as error:
            calendar_payload = {
                "connected": False,
                "status": f"Calendar unavailable: {error}",
                "months": [],
                "today_events": [],
                "tomorrow_events": [],
                "upcoming_events": [],
                "priority_reminders": []
            }

        add_hud_card(
            card_id="calendar_agenda",
            title="CALENDAR",
            card_type="calendar_agenda",
            x=72,
            y=52,
            width=1180,
            height=720,
            broadcast=False,
            data=calendar_payload
        )
        broadcast_state()
        return

    if "weather" in topic_lower or "forecast" in topic_lower or "temperature" in topic_lower:
        weather_payload = get_weather_payload(target_location)
        add_hud_card(
            card_id="weather_current",
            title="LOCAL WEATHER",
            card_type="weather",
            x=64,
            y=76,
            width=520,
            height=390,
            broadcast=False,
            data=weather_payload
        )
        broadcast_state()
        return

    if "map" in topic_lower or "travel" in topic_lower or "navigate" in topic_lower:
        destination = extract_map_destination(topic) or "Planet Earth"
        map_payload = get_map_payload(destination)

        add_hud_card(
            card_id="map_fullscreen",
            title="TACTICAL MAP",
            card_type="map",
            x=0,
            y=0,
            width=1536,
            height=864,
            broadcast=False,
            data=map_payload
        )
        broadcast_state()
        return

    if (
        "news" in topic_lower
        or "headlines" in topic_lower
        or "current events" in topic_lower
        or "briefing" in topic_lower
        or "what's going on" in topic_lower
        or "whats going on" in topic_lower
        or "what is going on" in topic_lower
        or "markets" in topic_lower
        or "stocks" in topic_lower
        or "economy" in topic_lower
        or "wars" in topic_lower
        or "active wars" in topic_lower
        or "conflicts" in topic_lower
        or "kosovo" in topic_lower
        or "kosova" in topic_lower
    ):
        if "kosovo" in topic_lower or "kosova" in topic_lower or "pristina" in topic_lower or "prishtina" in topic_lower:
            active_tab = "kosovo"
        elif "finance" in topic_lower or "financial" in topic_lower or "market" in topic_lower or "markets" in topic_lower or "stock" in topic_lower or "stocks" in topic_lower or "economy" in topic_lower:
            active_tab = "markets"
        elif "war" in topic_lower or "wars" in topic_lower or "active wars" in topic_lower or "conflict" in topic_lower or "conflicts" in topic_lower or "security" in topic_lower:
            active_tab = "conflict"
        elif "tech" in topic_lower or "technology" in topic_lower or "ai" in topic_lower:
            active_tab = "technology"
        else:
            active_tab = "briefing"

        news_payload = get_news_payload(active_tab=active_tab)
        add_hud_card(
            card_id="news_briefing",
            title="INTEL BRIEFING",
            card_type="news",
            x=520,
            y=52,
            width=860,
            height=650,
            broadcast=False,
            data=news_payload
        )
        broadcast_state()
        return

    if "music" in topic_lower or "song" in topic_lower or "track" in topic_lower:
        music_payload = get_music_payload()
        add_hud_card(
            card_id="music_controls",
            title="MUSIC CONTROL",
            card_type="music",
            x=64,
            y=500,
            width=520,
            height=300,
            broadcast=False,
            data=music_payload
        )
        broadcast_state()
        return


# ==========================================
# SEARCH / FILE / TIME TOOLS
# ==========================================
def web_search(query: str) -> str:
    try:
        if any(word in query.lower() for word in ["news", "headlines", "current events"]):
            seed_context_cards("news", replace_existing=False)

        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        html = urllib.request.urlopen(req, timeout=10).read().decode("utf-8")
        snippets = re.findall(
            r'<a class="result__snippet[^>]*>(.*?)</a>',
            html,
            re.IGNORECASE | re.DOTALL
        )
        clean = [re.sub(r"<[^>]+>", "", snippet).strip() for snippet in snippets][:3]
        return "\n\n".join(clean) if clean else "No results found."

    except Exception as e:
        return f"Search failed: {e}"


def get_time() -> str:
    return datetime.datetime.now().strftime("%I:%M %p")


def read_file(filepath: str) -> str:
    try:
        path = os.path.expanduser(filepath)

        if not os.path.exists(path):
            path = os.path.expanduser(f"~/Desktop/{os.path.basename(filepath)}")

        with open(path, "r") as f:
            content = f.read()
            return f"--- FILE CONTENT: {filepath} ---\n{content}"

    except Exception as e:
        return f"SYSTEM ERROR: Unable to locate file. I checked Home and Desktop. {e}"


# ==========================================
# macOS / PYAUTOGUI AUTOMATION
# ==========================================
def execute_macos_command(category: str, target: str):
    try:
        clean_target = target.replace('"', "").strip()
        cmd_lower = clean_target.lower()

        if category == "open":
            subprocess.run(["open", os.path.expanduser(clean_target)])
            return f"SUCCESS: Launched {clean_target}."

        if category == "music":
            direct_music_scripts = {
                "next": 'tell application "Music" to next track',
                "skip": 'tell application "Music" to next track',
                "change": 'tell application "Music" to next track',
                "pause": 'tell application "Music" to pause',
                "stop": 'tell application "Music" to pause',
                "toggle": 'tell application "Music"\nif player state is playing then\npause\nelse\nplay\nend if\nend tell',
                "previous": 'tell application "Music" to back track',
                "back": 'tell application "Music" to back track',
                "last": 'tell application "Music" to back track',
                "play": 'tell application "Music" to play',
                "resume": 'tell application "Music" to play'
            }

            if cmd_lower in direct_music_scripts:
                started_at = time.time()
                subprocess.Popen(
                    ["osascript", "-e", direct_music_scripts[cmd_lower]],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )

                if (os.getenv("FRIDAY_PERF_LOG") or os.getenv("JARVIS_PERF_LOG", "1")).strip().lower() in {"1", "true", "yes", "on"}:
                    elapsed = (time.time() - started_at) * 1000
                    print(f"[PERF] python music direct: {elapsed:.0f} ms")

                return f"SUCCESS: Music protocol engaged for {clean_target}."

            if cmd_lower in {"shuffle"}:
                script = '''
                tell application "Music"
                    if not running then activate
                    set shuffle enabled to true
                end tell
                '''
            elif cmd_lower in {"unshuffle", "shuffle off"}:
                script = '''
                tell application "Music"
                    if not running then activate
                    set shuffle enabled to false
                end tell
                '''
            elif cmd_lower in {"repeat", "loop"}:
                script = '''
                tell application "Music"
                    if not running then activate
                    set song repeat to one
                end tell
                '''
            elif cmd_lower in {"repeat off", "loop off"}:
                script = '''
                tell application "Music"
                    if not running then activate
                    set song repeat to off
                end tell
                '''
            elif cmd_lower in {"repeat all", "loop all"}:
                script = '''
                tell application "Music"
                    if not running then activate
                    set song repeat to all
                end tell
                '''
            elif cmd_lower in {"status", "now playing", "current"}:
                seed_context_cards("music", replace_existing=False)
                return "SUCCESS: Music widget refreshed."
            else:
                script = f'''
                tell application "Music"
                    if not running then
                        activate
                        delay 2.0
                    end if

                    try
                        if "{cmd_lower}" is "random" or "{cmd_lower}" is "anything" or "{cmd_lower}" is "whatever" then
                            error "Force random"
                        end if

                        play playlist "{clean_target}"
                    on error
                        try
                            set searchResults to search library playlist 1 for "{clean_target}"

                            if (count of searchResults) > 0 then
                                set randomIndex to random number from 1 to (count of searchResults)
                                play item randomIndex of searchResults
                            else
                                error "No search results found"
                            end if
                        on error
                            set shuffle enabled to true
                            play playlist 1
                        end try
                    end try
                end tell
                '''

            subprocess.run(["osascript", "-e", script], check=False)
            time.sleep(1.1)
            seed_context_cards("music", replace_existing=False)
            return f"SUCCESS: Music protocol engaged for {clean_target}."

        if category == "keyboard":
            if pyautogui is None:
                _warn_pyautogui_unavailable()
                return "SYSTEM UNAVAILABLE: Keyboard automation is disabled because pyautogui is not installed."

            blocked_volume_keys = {
                "volumeup",
                "volumedown",
                "volumemute",
                "audioincreasevolume",
                "audiodecreasevolume",
                "audiomute",
                "mute"
            }
            requested_keys = {
                key.strip().lower().replace(" ", "")
                for key in clean_target.split("+")
            }

            if requested_keys & blocked_volume_keys:
                return "ERROR: System volume control is disabled."

            pyautogui.hotkey(*clean_target.split("+"))
            return f"SUCCESS: Keyboard command executed: {clean_target}."

        if category == "type":
            if pyautogui is None:
                _warn_pyautogui_unavailable()
                return "SYSTEM UNAVAILABLE: Text automation is disabled because pyautogui is not installed."

            pyautogui.write(clean_target, interval=0.01)
            return "SUCCESS: Text typed."

        if category == "click":
            if pyautogui is None:
                _warn_pyautogui_unavailable()
                return "SYSTEM UNAVAILABLE: Mouse automation is disabled because pyautogui is not installed."

            pyautogui.click()
            return "SUCCESS: Mouse click executed."

        return "ERROR: Unknown macOS command category."

    except Exception as e:
        return f"SYSTEM ERROR: {e}"


# ==========================================
# OPTICAL / CAMERA TOOLS
# ==========================================
def capture_optical_feed():
    try:
        image_path = "tactical_feed.png"
        subprocess.run(["screencapture", "-x", "-C", image_path])
        image = PIL.Image.open(image_path)

        if pyautogui is None:
            _warn_pyautogui_unavailable()
        else:
            try:
                x, y = pyautogui.position()
                draw = ImageDraw.Draw(image)
                radius = 50
                draw.ellipse((x - radius, y - radius, x + radius, y + radius), outline="red", width=8)
                draw.ellipse((x * 2 - radius, y * 2 - radius, x * 2 + radius, y * 2 + radius), outline="red", width=8)
            except Exception:
                pass

        image = image.convert("RGB")
        image.thumbnail((1024, 576), PIL.Image.Resampling.LANCZOS)
        return image

    except Exception as e:
        print(f"{RED}OPTICAL ERROR: {e}{RESET}")
        return None


def capture_webcam_feed():
    try:
        print(f"{GRAY}[Webcam Hardware Engaging...]{RESET}", end="\r")
        cap = cv2.VideoCapture(0)

        for _ in range(10):
            cap.read()

        ret, frame = cap.read()
        cap.release()

        if ret:
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image = PIL.Image.fromarray(rgb_frame)
            image.save("webcam_feed.jpg")
            return image

        return None

    except Exception as e:
        print(f"{RED}WEBCAM ERROR: {e}{RESET}")
        return None
