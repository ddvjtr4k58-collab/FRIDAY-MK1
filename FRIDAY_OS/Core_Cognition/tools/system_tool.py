"""System Status — what this machine is doing right now.

Reads each measurement on its own rather than through the full diagnostics sweep.
That sweep also polls presence, both cameras, the calendar and the weather API,
which costs the better part of a second — far too long to spend answering "how
much battery do I have", which is the question this tool gets asked most.

Nothing here opens the diagnostics widget to answer a question. A number that was
asked for out loud gets said out loud.
"""

from __future__ import annotations

import datetime
import re

from Core_Cognition.tools import (
    KIND_DATA,
    KIND_OPEN,
    Capability,
    Tool,
    ToolResult,
    register,
)


def _battery_reading() -> dict:
    from Sensory_Array.system_tools import _pmset_battery_status

    raw = str(_pmset_battery_status() or "").strip()
    match = re.search(r"(\d+)\s*percent", raw)

    return {
        "percent": int(match.group(1)) if match else None,
        "charging": "charging" in raw.lower(),
        "summary": raw or "unavailable",
    }


def _battery(_slots) -> ToolResult:
    reading = _battery_reading()
    percent = reading.get("percent")

    if percent is None:
        return ToolResult(ok=False, speech="I cannot read the battery right now.", data=reading)

    if reading.get("charging"):
        sentence = f"Battery is at {percent}% and charging."
    elif percent <= 20:
        sentence = f"Battery is down to {percent}%. Worth plugging in."
    else:
        sentence = f"Battery is at {percent}%."

    return ToolResult(speech=sentence, data=reading)


def _cpu(_slots) -> ToolResult:
    import psutil

    load = psutil.cpu_percent(interval=0.2)
    descriptor = "busy" if load >= 70 else "steady" if load >= 25 else "idle"

    return ToolResult(
        speech=f"CPU is at {load:.0f}%, mostly {descriptor}.",
        data={"cpu_percent": load}
    )


def _memory(_slots) -> ToolResult:
    import psutil

    memory = psutil.virtual_memory()
    used = memory.percent
    free_gb = round(memory.available / (1024 ** 3), 1)

    return ToolResult(
        speech=f"Memory is {used:.0f}% used, with {free_gb} gigabytes free.",
        data={"memory_percent": used, "available_gb": free_gb}
    )


def _disk(_slots) -> ToolResult:
    import shutil
    from pathlib import Path

    usage = shutil.disk_usage(Path.home())
    free_gb = round(usage.free / (1024 ** 3), 1)
    total_gb = round(usage.total / (1024 ** 3), 1)

    return ToolResult(
        speech=f"{free_gb} gigabytes free out of {total_gb}.",
        data={"free_gb": free_gb, "total_gb": total_gb}
    )


def _clock(_slots) -> ToolResult:
    now = datetime.datetime.now()
    # %-I / %-d are the no-leading-zero forms, which is how a time is spoken.
    return ToolResult(
        speech=f"It is {now.strftime('%-I:%M %p').lower()} on {now.strftime('%A, %B %-d')}.",
        data={"iso": now.isoformat(timespec="seconds")}
    )


def _overview(_slots) -> ToolResult:
    import psutil

    battery = _battery_reading()
    load = psutil.cpu_percent(interval=0.2)
    memory = psutil.virtual_memory().percent
    parts = [f"CPU at {load:.0f}%", f"memory at {memory:.0f}%"]

    if battery.get("percent") is not None:
        parts.append(f"battery at {battery['percent']}%")

    return ToolResult(
        speech="Everything looks healthy: " + ", ".join(parts) + ".",
        data={"cpu_percent": load, "memory_percent": memory, "battery": battery}
    )


def _open(_slots) -> ToolResult:
    return ToolResult(speech="", action="open_system_health", event="acknowledge")


register(Tool(
    name="system_status",
    description="Battery, processor, memory, disk and the clock for this machine.",
    category="system",
    anchors=(
        "battery",
        "charge",
        "charging",
        "power",
        "cpu",
        "processor",
        "memory",
        "ram",
        "disk",
        "storage",
        "space",
        "system",
        "diagnostics",
        "machine",
        "laptop",
        "computer",
        "mac",
        "hardware",
    ),
    inputs=("none",),
    outputs=("spoken measurement", "diagnostics widget when explicitly asked for"),
    capabilities=(
        Capability(
            name="battery",
            description="Battery percentage and whether it is charging.",
            handler=_battery,
            kind=KIND_DATA,
            priority=2,
            phrases=(
                "how much battery do i have",
                "how much battery is left",
                "what is my battery",
                "what is my battery level",
                "battery level",
                "battery status",
                "how is my battery",
                "am i charging",
            ),
            patterns=(
                r"\bbattery\b",
                r"\bhow much (?:charge|power) (?:do i have|is left)\b",
                r"\bam i (?:charging|plugged in)\b",
            ),
            cues=("battery", "charge", "charging", "power", "plugged in"),
            outputs=("percent", "charging"),
        ),
        Capability(
            name="cpu_load",
            description="Current processor load.",
            handler=_cpu,
            kind=KIND_DATA,
            phrases=("what is my cpu usage", "how is the cpu", "cpu load"),
            patterns=(r"\bcpu\b", r"\bprocessor (?:load|usage)\b"),
            cues=("cpu", "processor"),
            outputs=("cpu percent",),
        ),
        Capability(
            name="memory_usage",
            description="How much RAM is in use.",
            handler=_memory,
            kind=KIND_DATA,
            phrases=("how much memory am i using", "what is my memory usage", "how much ram is free"),
            patterns=(r"\b(?:ram|memory) (?:usage|used|free|left)\b", r"\bhow much (?:ram|memory)\b"),
            cues=("ram", "memory"),
            outputs=("memory percent", "available gigabytes"),
        ),
        Capability(
            name="disk_space",
            description="Free disk space.",
            handler=_disk,
            kind=KIND_DATA,
            phrases=("how much disk space do i have", "how much storage is left", "how much space is left"),
            patterns=(r"\b(?:disk|storage|drive) space\b", r"\bhow much (?:disk|storage|space)\b"),
            cues=("disk", "storage", "space", "drive"),
            outputs=("free gigabytes", "total gigabytes"),
        ),
        Capability(
            name="current_time",
            description="Local time and date.",
            handler=_clock,
            kind=KIND_DATA,
            phrases=(
                "what time is it",
                "what is the time",
                "what day is it",
                "what is the date",
                "what is todays date",
                "what is the time right now",
            ),
            # Patterns only. "time" is far too common a word to use as an anchor —
            # "what time is my next meeting" belongs to the calendar, not here.
            patterns=(
                r"^what time is it\b",
                r"^what is the (?:time|date)\b",
                r"^what (?:day|date) is it\b",
                r"^what is today(?:s)? date\b",
                r"^do you have the time\b",
            ),
            outputs=("local time", "date"),
        ),
        Capability(
            name="overview",
            description="A one-line health summary of the machine.",
            handler=_overview,
            kind=KIND_DATA,
            default=True,
            phrases=("how is the system", "how is my machine", "how is my mac", "system overview"),
            patterns=(r"^how (?:is|are) (?:the |my )?(?:system|machine|mac|computer|laptop)\b",),
            cues=("system", "machine", "computer", "laptop", "mac", "hardware", "health"),
            outputs=("cpu percent", "memory percent", "battery percent"),
        ),
        Capability(
            name="open_diagnostics",
            description="Put the diagnostics widget on the Workstation.",
            handler=_open,
            kind=KIND_OPEN,
            phrases=(
                "open diagnostics",
                "show diagnostics",
                "open system health",
                "show system health",
                "open system status",
                "run diagnostics",
            ),
            patterns=(r"^(?:open|show|run|pull up|bring up) (?:the )?(?:diagnostics|system health|system status)\b",),
            cues=("diagnostics", "open", "show", "run", "widget"),
            outputs=("diagnostics widget",),
        ),
    ),
))
