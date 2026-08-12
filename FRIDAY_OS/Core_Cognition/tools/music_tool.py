"""Music — transport control and what is playing."""

from __future__ import annotations

from Core_Cognition.tools import (
    KIND_ACTION,
    KIND_DATA,
    KIND_OPEN,
    Capability,
    Tool,
    ToolResult,
    register,
)


def _transport(action: str, speech: str = "") -> ToolResult:
    return ToolResult(speech=speech, action=action, event="acknowledge")


def _play(_slots) -> ToolResult:
    return _transport("music_play")


def _pause(_slots) -> ToolResult:
    return _transport("music_pause")


def _resume(_slots) -> ToolResult:
    return _transport("music_play")


def _next(_slots) -> ToolResult:
    return _transport("music_next")


def _previous(_slots) -> ToolResult:
    return _transport("music_previous")


def _open(_slots) -> ToolResult:
    return _transport("open_music")


def _now_playing(_slots) -> ToolResult:
    from Sensory_Array.system_tools import get_music_payload

    payload = get_music_payload()
    title = str(payload.get("title") or "").strip()
    artist = str(payload.get("artist") or "").strip()

    if not payload.get("is_playing") or not title or title == "No track loaded":
        return ToolResult(speech="Nothing is playing right now.", data=payload)

    sentence = f"{title} by {artist}." if artist else f"{title}."
    return ToolResult(speech=sentence, data=payload)


def _volume(_slots) -> ToolResult:
    # Never reached: registered as unsupported so resolve() skips it. It is here
    # so the registry stays an honest description of the Music tool rather than
    # quietly omitting a capability FRIDAY is under a standing rule not to use.
    return ToolResult(ok=False, speech="I do not change the system audio level.")


register(Tool(
    name="music",
    description="Playback control for Apple Music, and what is currently playing.",
    category="media",
    anchors=(
        "music",
        "song",
        "songs",
        "track",
        "tracks",
        "playlist",
        "album",
        "tune",
        "tunes",
        "playing",
    ),
    inputs=("none",),
    outputs=("playback state change", "spoken now-playing summary", "music widget"),
    capabilities=(
        Capability(
            name="play",
            description="Start playback.",
            handler=_play,
            kind=KIND_ACTION,
            priority=2,
            phrases=(
                "play some music",
                "play music",
                "put some music on",
                "put on some music",
                "start the music",
                "play a song",
                "play something",
            ),
            patterns=(
                r"^(?:play|start|put on|put) (?:some |a |the )?(?:music|song|track|tunes)\b",
                r"^put (?:some )?music on\b",
                r"^play something\b",
            ),
            cues=("play", "start", "put on", "put some"),
            outputs=("playback started",),
        ),
        Capability(
            name="pause",
            description="Pause playback.",
            handler=_pause,
            kind=KIND_ACTION,
            priority=2,
            phrases=("pause the music", "stop the music", "pause the song", "kill the music"),
            patterns=(r"^(?:pause|stop|kill|silence) (?:the |this )?(?:music|song|track|playback)\b",),
            cues=("pause", "stop", "kill"),
            outputs=("playback paused",),
        ),
        Capability(
            name="resume",
            description="Resume playback.",
            handler=_resume,
            kind=KIND_ACTION,
            priority=2,
            phrases=("resume the music", "keep playing", "carry on playing", "unpause the music"),
            patterns=(r"^(?:resume|unpause|keep playing|carry on)\b.*\b(?:music|song|track|playing)\b",),
            cues=("resume", "unpause", "keep playing"),
            outputs=("playback resumed",),
        ),
        Capability(
            name="next",
            description="Skip to the next track.",
            handler=_next,
            kind=KIND_ACTION,
            priority=3,
            phrases=("skip this song", "next song please", "skip this track", "play the next song"),
            patterns=(
                r"^(?:skip|next)\b.*\b(?:song|track|this|music)\b",
                r"^(?:skip|change) (?:this|it)\b",
            ),
            cues=("skip", "next", "change"),
            outputs=("track advanced",),
        ),
        Capability(
            name="previous",
            description="Go back a track.",
            handler=_previous,
            kind=KIND_ACTION,
            priority=3,
            phrases=("go back a song", "play the previous song", "previous track please", "replay that song"),
            patterns=(r"^(?:go back|previous|replay|last)\b.*\b(?:song|track)\b",),
            cues=("previous", "go back", "replay", "last one"),
            outputs=("track rewound",),
        ),
        Capability(
            name="now_playing",
            description="What track is playing, and who by.",
            handler=_now_playing,
            kind=KIND_DATA,
            priority=1,
            phrases=(
                "what is playing",
                "what song is this",
                "what is this song",
                "who sings this",
                "what am i listening to",
                "what is currently playing",
            ),
            patterns=(
                r"^what (?:song|track) is (?:this|playing)\b",
                r"^what is (?:currently )?playing\b",
                r"^who (?:sings|is singing) this\b",
                r"^what am i listening to\b",
            ),
            cues=("what is playing", "what song", "who sings", "listening to", "playing"),
            outputs=("title", "artist", "album"),
        ),
        Capability(
            name="open_music",
            description="Put the music widget on the Workstation.",
            handler=_open,
            kind=KIND_OPEN,
            phrases=("open music", "show music", "open the music player", "pull up music", "bring up music"),
            patterns=(r"^(?:open|show|pull up|bring up|launch) (?:the |my )?music\b",),
            cues=("open", "show", "pull up", "bring up", "launch", "widget"),
            outputs=("music widget",),
        ),
        Capability(
            name="volume",
            description="Playback volume.",
            handler=_volume,
            kind=KIND_ACTION,
            supported=False,
            note=(
                "Declared, deliberately inactive. FRIDAY is under a standing rule never to "
                "change macOS audio levels or mute state, so volume requests stay conversation."
            ),
            cues=("volume", "louder", "quieter", "turn it up", "turn it down"),
            outputs=("none",),
        ),
    ),
))
