#!/usr/bin/env python3
"""Steklo AirPlay bridge.

Every TV running the Steklo launcher connects here and names its room. For each linked TV the bridge runs one
shairport-sync AirPlay 2 receiver under that name (all of them share one nqptp and one Avahi) and passes the
receiver's audio, track details and cover on to the TV. Buttons pressed on the TV go back to the iPhone as
DACP remote commands. A speaker exists only while its TV is linked, so nobody plays music into a TV that is off.
"""
import asyncio
import base64
import itertools
import json
import logging
import os
import re
import struct
import subprocess
import urllib.request
from typing import Dict, List, Optional, Tuple

OPTIONS_PATH = "/data/options.json"
RUN_DIR = "/run/steklo"
SHAIRPORT = "/usr/local/bin/shairport-sync"

# Frame types of the TV link: one type byte, a big-endian length, then the payload
AUDIO, EVENT, COVER, PING = 1, 2, 3, 9
PING_SECONDS = 5
HELLO_TIMEOUT_S = 10
AUDIO_CHUNK = 16 * 1024
BYTES_PER_FRAME = 4  # 16-bit stereo
METADATA_CHUNK = 64 * 1024
MAX_METADATA_BUFFER = 8 * 1024 * 1024
MAX_NAME = 40
MAX_RECEIVERS = 8
STOP_TIMEOUT_S = 5
DACP_TIMEOUT_S = 5
# shairport-sync releases audio this far ahead of its AirPlay time; the TV's own queue absorbs it
BACKEND_BUFFER_S = 0.1
DEFAULT_OPTIONS = {"bridge_port": 7700, "first_airplay_port": 7001, "latency_offset_ms": -150}

ITEM = re.compile(
    rb"<item><type>([0-9a-f]{8})</type><code>([0-9a-f]{8})</code><length>(\d+)</length>"
    rb"(?:\s*<data encoding=\"base64\">\s*([^<]*)</data>)?\s*</item>\s*"
)
# TV button -> DACP verb understood by the sending iPhone
COMMANDS = {"previous": "previtem", "playpause": "playpause", "next": "nextitem"}
STATES = {"pbeg": "playing", "prsm": "playing", "pres": "playing", "paus": "paused", "pfls": "paused", "pend": "stopped"}
TRACK_FIELDS = {"minm": "title", "asar": "artist", "asal": "album"}
TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
})

log = logging.getLogger("steklo")


def slug(name: str) -> str:
    """An ASCII name for files and shared memory: "Спальная Юлии" -> "spalnaya-yulii"."""
    text = re.sub(r"[^a-z0-9]+", "-", name.lower().translate(TRANSLIT)).strip("-")
    return (text or "tv")[:24]


def frame(kind: int, payload: bytes = b"") -> bytes:
    return struct.pack(">BI", kind, len(payload)) + payload


def fourcc(hex_text: bytes) -> str:
    return bytes.fromhex(hex_text.decode()).decode("latin-1")


def parse_items(buffer: bytes) -> Tuple[List[Tuple[str, str, bytes]], bytes]:
    """Complete metadata items in [buffer] and the unfinished rest to keep for the next read."""
    items, end = [], 0
    for match in ITEM.finditer(buffer):
        data = base64.b64decode(match.group(4)) if match.group(4) else b""
        items.append((fourcc(match.group(1)), fourcc(match.group(2)), data))
        end = match.end()
    return items, buffer[end:]


def state_event(code: str) -> Optional[dict]:
    state = STATES.get(code)
    return {"type": "state", "state": state} if state else None


def whole_frames(buffer: bytes) -> Tuple[bytes, bytes]:
    """Splits off whole stereo frames; Android's audio output rejects a torn one."""
    cut = len(buffer) - len(buffer) % BYTES_PER_FRAME
    return buffer[:cut], buffer[cut:]


def shairport_config(name: str, port: int, metadata_pipe: str, latency_offset_ms: int) -> str:
    quoted = name.replace("\\", "\\\\").replace('"', '\\"')
    return (
        "general = {\n"
        f'  name = "{quoted}";\n'
        f"  port = {port};\n"
        '  output_backend = "stdout";\n'
        '  mdns_backend = "avahi";\n'
        f"  audio_backend_buffer_desired_length_in_seconds = {BACKEND_BUFFER_S};\n"
        f"  audio_backend_latency_offset_in_seconds = {latency_offset_ms / 1000:.3f};\n"
        "};\n"
        "stdout = {\n"
        '  output_format = "S16_LE";\n'
        "  output_rate = 44100;\n"
        "  output_channels = 2;\n"
        "};\n"
        "metadata = {\n"
        '  enabled = "yes";\n'
        '  include_cover_art = "yes";\n'
        f'  pipe_name = "{metadata_pipe}";\n'
        "};\n"
    )


def parse_dacp_browse(output: str, dacp_id: str) -> Optional[Tuple[str, int]]:
    """Address and port of the sender's remote-control service in `avahi-browse -prt _dacp._tcp` output."""
    wanted = f"itunes_ctrl_{dacp_id}".lower()
    for line in output.splitlines():
        fields = line.split(";")
        if len(fields) >= 9 and fields[0] == "=" and fields[2] == "IPv4" and fields[3].lower() == wanted:
            return fields[7], int(fields[8])
    return None


class Receiver:
    """One AirPlay 2 speaker, alive while its TV is linked."""

    def __init__(self, name: str, port: int, latency_offset_ms: int) -> None:
        self.name = name
        self.port = port
        self.key = slug(name)
        self.meta_pipe = f"{RUN_DIR}/{self.key}.meta"
        self.conf = f"{RUN_DIR}/{self.key}.conf"
        self.latency_offset_ms = latency_offset_ms
        self.process: Optional[asyncio.subprocess.Process] = None
        self.meta_fd = -1
        self.keep_fd = -1
        self.dacp_id: Optional[str] = None
        self.active_remote: Optional[str] = None

    async def start(self) -> None:
        with open(self.conf, "w", encoding="utf-8") as conf:
            conf.write(shairport_config(self.name, self.port, self.meta_pipe, self.latency_offset_ms))
        if os.path.exists(self.meta_pipe):
            os.unlink(self.meta_pipe)
        os.mkfifo(self.meta_pipe)
        self.meta_fd = os.open(self.meta_pipe, os.O_RDONLY | os.O_NONBLOCK)
        # A writer of our own keeps the pipe from reading as finished between shairport-sync's writes
        self.keep_fd = os.open(self.meta_pipe, os.O_WRONLY | os.O_NONBLOCK)
        self.process = await asyncio.create_subprocess_exec(
            SHAIRPORT, "-c", self.conf, "-a", self.name, f"--port={self.port}",
            f"--nqptp-shared-memory-interface-name=/nqptp-{self.key}",
            stdout=asyncio.subprocess.PIPE,
        )
        log.info("AirPlay speaker «%s» up on port %d", self.name, self.port)

    async def stop(self) -> None:
        process, self.process = self.process, None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), STOP_TIMEOUT_S)
            except asyncio.TimeoutError:
                process.kill()
        for fd in (self.keep_fd, self.meta_fd):
            if fd >= 0:
                os.close(fd)
        self.meta_fd = self.keep_fd = -1
        for path in (self.meta_pipe, self.conf):
            if os.path.exists(path):
                os.unlink(path)
        log.info("AirPlay speaker «%s» down", self.name)


class Registry:
    """The running speakers by room name; a TV reconnecting under the same name replaces its old speaker."""

    def __init__(self, options: dict) -> None:
        self.options = options
        self.receivers: Dict[str, Receiver] = {}

    async def open(self, name: str) -> Receiver:
        old = self.receivers.pop(name, None)
        if old is not None:
            await old.stop()
        if len(self.receivers) >= MAX_RECEIVERS:
            raise RuntimeError("too many AirPlay speakers")
        used = {receiver.port for receiver in self.receivers.values()}
        port = next(p for p in itertools.count(self.options["first_airplay_port"]) if p not in used)
        receiver = Receiver(name, port, self.options["latency_offset_ms"])
        await receiver.start()
        self.receivers[name] = receiver
        return receiver

    async def close(self, receiver: Receiver) -> None:
        if self.receivers.get(receiver.name) is receiver:
            del self.receivers[receiver.name]
        await receiver.stop()


async def send(writer: asyncio.StreamWriter, kind: int, payload: bytes = b"") -> None:
    writer.write(frame(kind, payload))
    await writer.drain()


async def send_event(writer: asyncio.StreamWriter, event: dict) -> None:
    await send(writer, EVENT, json.dumps(event, ensure_ascii=False).encode("utf-8"))


async def pump_audio(receiver: Receiver, writer: asyncio.StreamWriter) -> None:
    assert receiver.process is not None and receiver.process.stdout is not None
    rest = b""
    while True:
        chunk = await receiver.process.stdout.read(AUDIO_CHUNK)
        if not chunk:
            log.warning("shairport-sync for «%s» stopped", receiver.name)
            return
        aligned, rest = whole_frames(rest + chunk)
        if aligned:
            await send(writer, AUDIO, aligned)


async def pump_metadata(receiver: Receiver, writer: asyncio.StreamWriter) -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(os.dup(receiver.meta_fd), "rb", 0))
    buffer = b""
    track: Dict[str, str] = {}
    sent_track: Dict[str, str] = {}
    while True:
        chunk = await reader.read(METADATA_CHUNK)
        if not chunk:
            return
        items, buffer = parse_items(buffer + chunk)
        if len(buffer) > MAX_METADATA_BUFFER:
            buffer = b""
        for kind, code, data in items:
            if kind == "core" and code in TRACK_FIELDS:
                track[TRACK_FIELDS[code]] = data.decode("utf-8", "replace")
            elif code == "PICT":
                await send(writer, COVER, data)
            elif code == "snam":
                await send_event(writer, {"type": "session", "client": data.decode("utf-8", "replace")})
            elif code == "daid":
                receiver.dacp_id = data.decode("ascii", "replace")
            elif code == "acre":
                receiver.active_remote = data.decode("ascii", "replace")
            elif state_event(code) is not None:
                if code == "pend":
                    track, sent_track = {}, {}
                await send_event(writer, state_event(code))
        # Title, artist and album arrive as separate items; one event per change keeps the TV calm
        if track and track != sent_track:
            sent_track = dict(track)
            await send_event(writer, {"type": "track", **track})


async def pings(writer: asyncio.StreamWriter) -> None:
    while True:
        await asyncio.sleep(PING_SECONDS)
        await send(writer, PING)


def dacp_send(receiver: Receiver, verb: str) -> None:
    """Asks the sending iPhone to skip or pause, as a DACP remote would. Runs off the event loop."""
    if not receiver.dacp_id or not receiver.active_remote:
        log.info("No remote-control details from the sender yet")
        return
    browse = subprocess.run(["avahi-browse", "-prt", "_dacp._tcp"], capture_output=True, text=True, timeout=DACP_TIMEOUT_S)
    target = parse_dacp_browse(browse.stdout, receiver.dacp_id)
    if target is None:
        log.info("The sender does not announce remote control")
        return
    host, port = target
    request = urllib.request.Request(
        f"http://{host}:{port}/ctrl-int/1/{verb}",
        headers={"Active-Remote": receiver.active_remote},
    )
    try:
        with urllib.request.urlopen(request, timeout=DACP_TIMEOUT_S):
            pass
    except OSError as error:
        log.warning("Remote command %s failed: %s", verb, error)


async def tv_commands(receiver: Receiver, reader: asyncio.StreamReader) -> None:
    """Button presses from the TV; the TV hanging up ends the session."""
    loop = asyncio.get_running_loop()
    while True:
        line = await reader.readline()
        if not line:
            return
        try:
            verb = COMMANDS.get(json.loads(line.decode("utf-8")).get("cmd", ""))
        except (ValueError, AttributeError):
            continue
        if verb:
            await loop.run_in_executor(None, dacp_send, receiver, verb)


async def serve_tv(registry: Registry, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    try:
        hello = json.loads((await asyncio.wait_for(reader.readline(), HELLO_TIMEOUT_S)).decode("utf-8"))
        name = str(hello.get("name", "")).strip()[:MAX_NAME] or "Телевизор"
        receiver = await registry.open(name)
    except (asyncio.TimeoutError, ValueError, AttributeError, RuntimeError, OSError) as error:
        log.warning("Rejected TV %s: %s", peer, error)
        writer.close()
        return
    log.info("TV %s linked as «%s»", peer, name)
    tasks = [
        asyncio.ensure_future(pump_audio(receiver, writer)),
        asyncio.ensure_future(pump_metadata(receiver, writer)),
        asyncio.ensure_future(pings(writer)),
        asyncio.ensure_future(tv_commands(receiver, reader)),
    ]
    try:
        await send_event(writer, {"type": "receiver", "name": name})
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except (ConnectionError, OSError) as error:
        log.info("TV «%s» link broke: %s", name, error)
    finally:
        for task in tasks:
            task.cancel()
        await registry.close(receiver)
        writer.close()
        log.info("TV «%s» unlinked", name)


def load_options() -> dict:
    options = dict(DEFAULT_OPTIONS)
    try:
        with open(OPTIONS_PATH, encoding="utf-8") as file:
            options.update(json.load(file))
    except (OSError, ValueError) as error:
        log.warning("Using default options: %s", error)
    return options


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    options = load_options()
    os.makedirs(RUN_DIR, exist_ok=True)
    registry = Registry(options)
    server = await asyncio.start_server(
        lambda reader, writer: serve_tv(registry, reader, writer), host="0.0.0.0", port=options["bridge_port"],
    )
    log.info("Waiting for Steklo TVs on port %d", options["bridge_port"])
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
