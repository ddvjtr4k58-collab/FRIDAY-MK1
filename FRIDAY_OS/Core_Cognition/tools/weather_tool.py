"""Weather — current conditions and forecast."""

from __future__ import annotations

from Core_Cognition.tools import (
    KIND_DATA,
    KIND_OPEN,
    Capability,
    Slot,
    Tool,
    ToolResult,
    register,
)

# "the weather in Chicago", "forecast for Denver, CO", "the weather on Mars".
#
# Anchored to the end of the request, and blocked from starting on the words that
# begin a time phrase rather than a place — otherwise "what is the weather in the
# morning" would go looking for a town called "the morning", and "on Saturday"
# would become a place called Saturday.
_LOCATION = (
    r"\b(?:in|for|at|on|around)\s+"
    r"(?!(?:the|a|an|my|this|that|these|those|next|last|today|tomorrow|tonight|now|here|me|it"
    r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday|weekend|weekends"
    r"|morning|afternoon|evening|night|later|earlier)\b)"
    r"(?P<value>[A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z][A-Za-z.'-]*){0,3}(?:\s*,\s*[A-Za-z]{2,})?)"
    # Closing punctuation is allowed before the end. Without it a place only
    # registered when the sentence had none: "weather in Denver" found Denver and
    # "weather in Denver?" found nothing, which is exactly the wrong way round
    # for a question.
    r"\s*[.?!]*\s*$"
)

_UNAVAILABLE = "I could not reach the weather service just now."


def _configured_location() -> str:
    from Sensory_Array.system_tools import current_default_location

    return str(current_default_location() or "").strip()


def _elsewhere(slots) -> str:
    """The place this request named, if it is not the one FRIDAY can report on.

    The forecast behind this tool is a single fixed coordinate — the configured
    location and nothing else. Naming a different place used to change only the
    LABEL on the reading, so "the weather in Denver" answered with Warrenville's
    temperature and called it Denver. That is worse than not answering: it looks
    exactly like a real forecast.

    Returns the unsupported place, or "" when the request is about home. This is
    what makes the honest refusal possible, and it is also what stops a planned
    step from reporting local conditions for an event held somewhere else.
    """
    requested = str(slots.get("location") or "").strip()

    if not requested:
        return ""

    configured = _configured_location()
    known = {configured.lower(), configured.split(",")[0].strip().lower()}
    known.discard("")

    return "" if requested.lower() in known else requested


def _not_covered(place: str) -> ToolResult:
    home = _configured_location().split(",")[0].strip() or "the configured location"

    return ToolResult(
        ok=False,
        speech=f"I only have the forecast for {home}, so I cannot give you conditions for {place}.",
        data={"requested": place, "covered": home, "supported": False}
    )


def _only_home(handler):
    """Wrap a reading so it refuses a place FRIDAY does not actually cover.

    Applied at registration rather than repeated inside seven handlers, and only
    to the readings — opening the widget is unaffected.
    """
    def run(slots):
        place = _elsewhere(slots)

        return _not_covered(place) if place else handler(slots)

    run.__name__ = handler.__name__
    run.__doc__ = handler.__doc__
    return run


def _payload(slots):
    from Sensory_Array.system_tools import get_weather_payload

    return get_weather_payload(slots.get("location") or None)


def _place(payload) -> str:
    location = str(payload.get("location") or "").strip()
    # The stored default carries a state code ("Warrenville, IL") which reads as
    # an abbreviation out loud. The town on its own is what Jon would say.
    return location.split(",")[0].strip() if location else ""


def _condition(payload) -> str:
    condition = str(payload.get("condition") or "").strip().lower()
    return "" if condition in {"", "conditions", "unavailable"} else condition


def _current(slots) -> ToolResult:
    payload = _payload(slots)
    temperature = payload.get("temperature")

    if temperature is None:
        return ToolResult(ok=False, speech=_UNAVAILABLE, data=payload)

    place = _place(payload)
    condition = _condition(payload)
    feels = payload.get("feels_like")
    humidity = payload.get("humidity")

    sentence = f"It is currently {temperature} degrees"

    if condition:
        sentence += f" and {condition}"

    if place:
        sentence += f" in {place}"

    if humidity is not None:
        sentence += f", with {humidity}% humidity"

    sentence += "."

    if feels is not None and abs(int(feels) - int(temperature)) >= 4:
        sentence += f" It feels more like {feels}."

    return ToolResult(speech=sentence, action="open_weather", data=payload)


def _temperature(slots) -> ToolResult:
    payload = _payload(slots)
    temperature = payload.get("temperature")

    if temperature is None:
        return ToolResult(ok=False, speech=_UNAVAILABLE, data=payload)

    place = _place(payload)
    feels = payload.get("feels_like")
    sentence = f"It is {temperature} degrees" + (f" in {place}" if place else "") + "."

    if feels is not None and abs(int(feels) - int(temperature)) >= 4:
        sentence += f" Feels like {feels}."

    return ToolResult(speech=sentence, action="open_weather", data=payload)


def _humidity(slots) -> ToolResult:
    payload = _payload(slots)
    humidity = payload.get("humidity")

    if humidity is None:
        return ToolResult(ok=False, speech=_UNAVAILABLE, data=payload)

    return ToolResult(
        speech=f"Humidity is {humidity}%.",
        action="open_weather",
        data=payload
    )


def _rain(slots) -> ToolResult:
    payload = _payload(slots)
    hours = [entry for entry in payload.get("forecast") or [] if entry.get("precip") is not None][:12]

    if not hours:
        return ToolResult(ok=False, speech=_UNAVAILABLE, data=payload)

    peak = max(hours, key=lambda entry: entry.get("precip") or 0)
    chance = int(peak.get("precip") or 0)
    condition = _condition(payload)
    raining_now = condition == "rain"

    if raining_now:
        sentence = "It is raining right now."
    elif chance >= 50:
        sentence = f"Rain looks likely, up to a {chance}% chance around {peak.get('time')}."
    elif chance >= 20:
        sentence = f"There is a {chance}% chance of rain around {peak.get('time')}, but nothing heavy."
    else:
        sentence = "No rain expected in the next several hours."

    return ToolResult(speech=sentence, action="open_weather", data=payload)


def _hourly(slots) -> ToolResult:
    payload = _payload(slots)
    hours = [entry for entry in payload.get("forecast") or [] if entry.get("temp") is not None]

    if len(hours) < 2:
        return ToolResult(ok=False, speech=_UNAVAILABLE, data=payload)

    upcoming = hours[1:7]
    warmest = max(upcoming, key=lambda entry: entry["temp"])
    coolest = min(upcoming, key=lambda entry: entry["temp"])
    sentence = (
        f"Over the next few hours it runs between {coolest['temp']} and {warmest['temp']} degrees, "
        f"{_condition(payload) or 'steady'} for now."
    )

    return ToolResult(speech=sentence, action="open_weather", data=payload)


def _weekly(slots) -> ToolResult:
    payload = _payload(slots)
    days = [day for day in payload.get("daily") or [] if day.get("high") is not None]

    if not days:
        return ToolResult(ok=False, speech=_UNAVAILABLE, data=payload)

    highs = [day["high"] for day in days]
    wettest = max(days, key=lambda day: day.get("precip_chance") or 0)
    sentence = f"This week highs run from {min(highs)} to {max(highs)} degrees."

    if (wettest.get("precip_chance") or 0) >= 40:
        sentence += f" {wettest['when'].capitalize()} looks like the wet one."

    return ToolResult(speech=sentence, action="open_weather", data=payload)


def _tomorrow(slots) -> ToolResult:
    payload = _payload(slots)
    day = next(
        (entry for entry in payload.get("daily") or [] if entry.get("when") == "tomorrow"),
        None
    )

    if not day or day.get("high") is None:
        return ToolResult(ok=False, speech=_UNAVAILABLE, data=payload)

    sentence = f"Tomorrow: a high of {day['high']}, a low of {day['low']}, {str(day.get('condition') or '').lower()}."

    if (day.get("precip_chance") or 0) >= 40:
        sentence += f" {int(day['precip_chance'])}% chance of rain."

    return ToolResult(speech=sentence, data=payload)


def _open(slots) -> ToolResult:
    return ToolResult(speech="", action="open_weather", event="acknowledge")


register(Tool(
    name="weather",
    description="Current conditions and forecast for a location.",
    category="environment",
    anchors=(
        "weather",
        "forecast",
        "temperature",
        "temp",
        "humidity",
        "humid",
        "rain",
        "raining",
        "snow",
        "snowing",
        "sunny",
        "degrees",
        "outside",
        "umbrella",
    ),
    inputs=("location (optional, defaults to the configured location)",),
    outputs=("spoken conditions summary", "weather widget on the Workstation"),
    # Open-Meteo over the network.
    timeout=8.0,
    capabilities=(
        Capability(
            name="current_conditions",
            description="Temperature, condition and humidity right now.",
            handler=_only_home(_current),
            kind=KIND_DATA,
            default=True,
            priority=1,
            phrases=(
                "what is the weather",
                "what is the weather today",
                "what is the weather like",
                "what is the weather like today",
                "how is the weather",
                "hows the weather outside",
                "what is it like outside",
                "whats it doing outside",
            ),
            patterns=(
                r"^(?:what|how) (?:is|was) (?:the |it )?(?:weather|conditions)",
                r"\bweather (?:today|right now|now|outside|out there)\b",
                r"^is it (?:nice|warm|hot|cold|sunny|cloudy) (?:out|outside|today)",
                # Naming a place is asking what it is like there, not asking for a
                # widget — "show me the weather in Denver" wants the reading.
                r"\b(?:weather|temperature|conditions) (?:in|for|at) \w+",
            ),
            cues=("today", "now", "right now", "currently", "outside", "out there", "at the moment"),
            slots=(Slot("location", _LOCATION, description="Place to report on"),),
            inputs=("location",),
            outputs=("temperature", "condition", "humidity", "feels like"),
        ),
        Capability(
            name="temperature",
            description="Just the temperature, and what it feels like.",
            handler=_only_home(_temperature),
            kind=KIND_DATA,
            phrases=(
                "what is the temperature",
                "how hot is it",
                "how cold is it",
                "how warm is it",
                "what is the temp",
            ),
            patterns=(r"^how (?:hot|cold|warm) is it", r"\bwhat is the (?:temperature|temp)\b"),
            cues=("temperature", "temp", "degrees", "how hot", "how cold", "how warm"),
            slots=(Slot("location", _LOCATION),),
            outputs=("temperature", "feels like"),
        ),
        Capability(
            name="humidity",
            description="Relative humidity right now.",
            handler=_only_home(_humidity),
            kind=KIND_DATA,
            phrases=("what is the humidity", "how humid is it"),
            patterns=(r"\bhumid(?:ity)?\b",),
            cues=("humidity", "humid"),
            slots=(Slot("location", _LOCATION),),
            outputs=("humidity",),
        ),
        Capability(
            name="rain_chance",
            description="Whether it is going to rain, and how likely.",
            handler=_only_home(_rain),
            kind=KIND_DATA,
            phrases=(
                "is it going to rain",
                "will it rain",
                "will it rain today",
                "do i need an umbrella",
                "is it raining",
            ),
            patterns=(
                r"^(?:is|will) it (?:going to )?(?:rain|snow)",
                r"\bneed (?:an )?umbrella\b",
                r"\bchance of (?:rain|precipitation)\b",
            ),
            cues=("rain", "raining", "snow", "snowing", "umbrella", "precipitation", "wet"),
            slots=(Slot("location", _LOCATION),),
            outputs=("rain chance", "peak hour"),
        ),
        Capability(
            name="hourly_forecast",
            description="How conditions move over the next few hours.",
            handler=_only_home(_hourly),
            kind=KIND_DATA,
            phrases=("what is the hourly forecast", "what is the weather later"),
            patterns=(r"\bhourly forecast\b", r"\bweather (?:later|this afternoon|this evening|tonight)\b"),
            cues=("hourly", "later", "later today", "this afternoon", "this evening", "tonight", "next few hours"),
            slots=(Slot("location", _LOCATION),),
            outputs=("hourly range", "condition"),
        ),
        Capability(
            name="tomorrow_forecast",
            description="Tomorrow's high, low and conditions.",
            handler=_only_home(_tomorrow),
            kind=KIND_DATA,
            phrases=("what is the weather tomorrow", "what will the weather be tomorrow"),
            patterns=(r"\b(?:weather|forecast|temperature|rain|hot|cold) .*\btomorrow\b", r"\btomorrow.*\bweather\b"),
            cues=("tomorrow",),
            slots=(Slot("location", _LOCATION),),
            outputs=("high", "low", "condition", "rain chance"),
        ),
        Capability(
            name="weekly_forecast",
            description="The week ahead in one line.",
            handler=_only_home(_weekly),
            kind=KIND_DATA,
            phrases=("what is the forecast this week", "what is the weekly forecast"),
            patterns=(r"\b(?:weather|forecast) .*\b(?:this week|the week|weekend|next few days)\b",),
            cues=("this week", "the week", "weekly", "weekend", "next few days", "coming days", "seven day"),
            slots=(Slot("location", _LOCATION),),
            outputs=("weekly high range", "wettest day"),
        ),
        Capability(
            name="open_weather",
            description="Put the weather widget on the Workstation.",
            handler=_open,
            kind=KIND_OPEN,
            phrases=(
                "open weather",
                "show weather",
                "open the weather",
                "show me the weather",
                "pull up the weather",
                "bring up the weather",
                "open weather widget",
                "weather widget",
            ),
            patterns=(r"^(?:open|show|pull up|bring up|launch) (?:the |my )?weather\b",),
            cues=("open", "show", "pull up", "bring up", "launch", "widget"),
            outputs=("weather widget",),
        ),
    ),
))
