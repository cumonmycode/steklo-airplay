#!/usr/bin/env python3
"""Steklo AirPlay bridge.

Every TV running the Steklo launcher connects here and names its room. For each linked TV the bridge runs one
shairport-sync AirPlay 2 receiver under that name (all of them share one nqptp and one Avahi) and passes the
receiver's audio, track details, cover and volume on to the TV. Buttons pressed on the TV reach the iPhone through
shairport-sync's own remote control, which speaks AirPlay 2's event channel; each receiver gets a private D-Bus
for it, since every shairport-sync claims the same bus name. The receiver leaves the audio at full level and the
TV turns the sender's volume into its own, so the phone's volume buttons work the TV. A speaker exists only while
its TV is linked, so nobody plays music into a TV that is off.
"""
import asyncio
import base64
import binascii
import contextlib
import hashlib
import itertools
import json
import logging
import os
import re
import signal
import socket
import struct
from typing import Dict, List, Optional, Tuple

OPTIONS_PATH = "/data/options.json"
RUN_DIR = "/run/steklo"
SHAIRPORT = "/usr/local/bin/shairport-sync"

# Frame types of the TV link: one type byte, a big-endian length, then the payload
AUDIO, EVENT, COVER, PING = 1, 2, 3, 9
PING_SECONDS = 5
HELLO_TIMEOUT_S = 10
# A TV that takes nothing for this long has stopped reading; its speaker must not hold the sender up
SEND_TIMEOUT_S = 10
# An unplugged TV or one that lost Wi-Fi is noticed within half a minute instead of the kernel's 15
KEEPALIVE_IDLE_S = 10
KEEPALIVE_INTERVAL_S = 5
KEEPALIVE_COUNT = 3
USER_TIMEOUT_MS = 20_000
AUDIO_CHUNK = 16 * 1024
BYTES_PER_FRAME = 4  # 16-bit stereo
METADATA_CHUNK = 64 * 1024
MAX_METADATA_BUFFER = 8 * 1024 * 1024
# iPhone covers are a few hundred KB; a bigger picture would hold up the audio on the same link
MAX_COVER_BYTES = 2 * 1024 * 1024
MAX_NAME = 40
MAX_RECEIVERS = 8
STOP_TIMEOUT_S = 5
BUS_START_TIMEOUT_S = 5
REMOTE_TIMEOUT_S = 5
# shairport-sync quits at once when its port is taken; this long a wait catches that
START_CHECK_S = 0.5
# shairport-sync releases audio this far ahead of its AirPlay time; the TV's own queue absorbs it
BACKEND_BUFFER_S = 0.1
DEFAULT_OPTIONS = {"bridge_port": 7700, "first_airplay_port": 7001, "latency_offset_ms": -150, "room_offsets": []}
MAX_ROOM_OFFSET_MS = 500
DEFAULT_NAME = "Телевизор"

ITEM = re.compile(
    rb"<item><type>([0-9a-f]{8})</type><code>([0-9a-f]{8})</code><length>(\d+)</length>"
    rb"(?:\s*<data encoding=\"base64\">\s*([^<]*)</data>)?\s*</item>\s*"
)
ITEM_END = b"</item>"
# TV button -> shairport-sync's D-Bus remote-control method
COMMANDS = {"previous": "Previous", "playpause": "PlayPause", "next": "Next"}
VOLUME_METHOD = "SetAirplayVolume"
REMOTE_SERVICE = "org.gnome.ShairportSync"
REMOTE_PATH = "/org/gnome/ShairportSync"
REMOTE_INTERFACE = "org.gnome.ShairportSync.RemoteControl"
# An AirPlay 2 sender may end the stream while it stays paused, so only the end of the active session ("aend")
# takes the player off the TV
STATES = {
    "pbeg": "playing", "prsm": "playing", "pres": "playing",
    "paus": "paused", "pfls": "paused", "pend": "paused",
    "aend": "stopped",
}
# How long a paused session keeps its player on the TV
PAUSED_SESSION_S = 300
# AirPlay volume follows the sender's slider from -30 (quietest) to 0; -144 means muted
AIRPLAY_QUIETEST = -30.0
AIRPLAY_MUTED = -144.0
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


def receiver_key(name: str) -> str:
    """Names one speaker's files, D-Bus and nqptp clock; the hash keeps "Кухня" and "kukhnya" apart."""
    return f"{slug(name)}-{hashlib.sha1(name.encode('utf-8')).hexdigest()[:6]}"


def clean_name(raw: object) -> str:
    """The room name from a TV's hello without control characters, or the default when nothing is left."""
    text = "".join(ch for ch in str(raw) if ch.isprintable()).strip()[:MAX_NAME]
    return text or DEFAULT_NAME


def frame(kind: int, payload: bytes = b"") -> bytes:
    return struct.pack(">BI", kind, len(payload)) + payload


def fourcc(hex_text: bytes) -> str:
    return bytes.fromhex(hex_text.decode()).decode("latin-1")


def parse_items(buffer: bytes) -> Tuple[List[Tuple[str, str, bytes]], bytes]:
    """Complete metadata items in [buffer] and the unfinished rest to keep for the next read."""
    items, end = [], 0
    for match in ITEM.finditer(buffer):
        end = match.end()
        try:
            data = base64.b64decode(match.group(4)) if match.group(4) else b""
        except (binascii.Error, ValueError):
            continue
        items.append((fourcc(match.group(1)), fourcc(match.group(2)), data))
    return items, buffer[end:]


def state_event(code: str) -> Optional[dict]:
    state = STATES.get(code)
    return {"type": "state", "state": state} if state else None


def volume_level(pvol: bytes) -> Optional[float]:
    """The sender's volume from 0 to 1 out of shairport-sync's "pvol" item; None when it is unreadable."""
    try:
        airplay = float(pvol.decode("ascii").split(",")[0])
    except (ValueError, UnicodeDecodeError):
        return None
    if airplay <= AIRPLAY_QUIETEST:
        return 0.0
    return round(min(1.0, 1.0 - airplay / AIRPLAY_QUIETEST), 4)


def airplay_volume(level: float) -> float:
    """The AirPlay volume for a TV level from 0 to 1; a silent TV mutes the sender."""
    level = min(1.0, max(0.0, level))
    return AIRPLAY_MUTED if level == 0 else AIRPLAY_QUIETEST * (1.0 - level)


def lead_ms(options: dict, room: str) -> int:
    """How long after the bridge lets go of a frame the TV in [room] should play it.

    shairport-sync releases audio this far ahead of its AirPlay time, so every TV playing on that schedule sounds
    together with the others and with HomePods. A room offset makes up for delay Android cannot see, such as a TV's
    own sound processing behind an HDMI stick; positive plays later.
    """
    offset = 0
    for entry in options.get("room_offsets") or []:
        if isinstance(entry, dict) and entry.get("room") == room and isinstance(entry.get("offset_ms"), int):
            offset = max(-MAX_ROOM_OFFSET_MS, min(MAX_ROOM_OFFSET_MS, entry["offset_ms"]))
    return max(0, round(BACKEND_BUFFER_S * 1000 - options["latency_offset_ms"] + offset))


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
        # The TV's own volume follows the sender instead, so the phone's buttons work the TV
        '  ignore_volume_control = "yes";\n'
        '  dbus_service_bus = "session";\n'
        '  mpris_service_bus = "session";\n'
        "};\n"
        "sessioncontrol = {\n"
        f"  active_state_timeout = {PAUSED_SESSION_S:.1f};\n"
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


def remote_command(bus: str, method: str, *args: str) -> List[str]:
    """The dbus-send call that has the receiver on [bus] skip, pause or set the volume on the sending iPhone."""
    return [
        "dbus-send", f"--bus={bus}", "--print-reply", f"--reply-timeout={REMOTE_TIMEOUT_S * 1000}",
        f"--dest={REMOTE_SERVICE}", REMOTE_PATH, f"{REMOTE_INTERFACE}.{method}", *args,
    ]


async def end_process(process: Optional[asyncio.subprocess.Process]) -> None:
    if process is None or process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), STOP_TIMEOUT_S)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()


class Receiver:
    """One AirPlay 2 speaker, alive while its TV is linked."""

    def __init__(self, name: str, port: int, latency_offset_ms: int) -> None:
        self.name = name
        self.port = port
        self.key = receiver_key(name)
        self.meta_pipe = f"{RUN_DIR}/{self.key}.meta"
        self.conf = f"{RUN_DIR}/{self.key}.conf"
        self.bus_socket = f"{RUN_DIR}/{self.key}.bus"
        self.bus = f"unix:path={self.bus_socket}"
        self.latency_offset_ms = latency_offset_ms
        self.process: Optional[asyncio.subprocess.Process] = None
        self.bus_daemon: Optional[asyncio.subprocess.Process] = None
        self.meta_fd = -1
        self.keep_fd = -1
        self.stopped = False

    async def start(self) -> None:
        try:
            with open(self.conf, "w", encoding="utf-8") as conf:
                conf.write(shairport_config(self.name, self.port, self.meta_pipe, self.latency_offset_ms))
            for path in (self.meta_pipe, self.bus_socket):
                if os.path.exists(path):
                    os.unlink(path)
            os.mkfifo(self.meta_pipe)
            self.meta_fd = os.open(self.meta_pipe, os.O_RDONLY | os.O_NONBLOCK)
            # A writer of our own keeps the pipe from reading as finished between shairport-sync's writes
            self.keep_fd = os.open(self.meta_pipe, os.O_WRONLY | os.O_NONBLOCK)
            await self.start_bus()
            self.process = await asyncio.create_subprocess_exec(
                SHAIRPORT, "-c", self.conf, "-a", self.name, f"--port={self.port}",
                f"--nqptp-shared-memory-interface-name=/nqptp-{self.key}",
                stdout=asyncio.subprocess.PIPE,
                env={**os.environ, "DBUS_SESSION_BUS_ADDRESS": self.bus},
            )
            await asyncio.sleep(START_CHECK_S)
            if self.process.returncode is not None:
                raise RuntimeError(f"shairport-sync quit with code {self.process.returncode}")
        except BaseException:
            # Whatever got started so far must not outlive a TV that was turned away
            await self.stop()
            raise
        log.info("AirPlay speaker «%s» up on port %d", self.name, self.port)

    async def start_bus(self) -> None:
        """A D-Bus of this speaker's own for its remote control; it prints its address once it listens."""
        self.bus_daemon = await asyncio.create_subprocess_exec(
            "dbus-daemon", "--session", f"--address={self.bus}", "--nofork", "--nopidfile", "--print-address",
            stdout=asyncio.subprocess.PIPE,
        )
        assert self.bus_daemon.stdout is not None
        try:
            address = await asyncio.wait_for(self.bus_daemon.stdout.readline(), BUS_START_TIMEOUT_S)
        except asyncio.TimeoutError:
            address = b""
        if not address:
            # The speaker still plays; only the TV's buttons and volume sync go quiet
            log.warning("D-Bus for «%s» did not start; the TV's buttons will not reach the sender", self.name)

    async def stop(self) -> None:
        # A replaced speaker is stopped again when its old session ends; by then its paths belong to the new one
        if self.stopped:
            return
        self.stopped = True
        for process in (self.process, self.bus_daemon):
            await end_process(process)
        for fd in (self.keep_fd, self.meta_fd):
            if fd >= 0:
                os.close(fd)
        self.meta_fd = self.keep_fd = -1
        for path in (self.meta_pipe, self.conf, self.bus_socket):
            if os.path.exists(path):
                os.unlink(path)
        log.info("AirPlay speaker «%s» down", self.name)


class Registry:
    """The running speakers by room name; a TV reconnecting under the same name replaces its old speaker."""

    def __init__(self, options: dict) -> None:
        self.options = options
        self.receivers: Dict[str, Receiver] = {}
        # TVs reconnect together after a restart; one at a time keeps two of them off the same port
        self.lock = asyncio.Lock()

    async def open(self, name: str) -> Receiver:
        async with self.lock:
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

    async def close_all(self) -> None:
        receivers, self.receivers = list(self.receivers.values()), {}
        await asyncio.gather(*(receiver.stop() for receiver in receivers), return_exceptions=True)


async def send(writer: asyncio.StreamWriter, kind: int, payload: bytes = b"") -> None:
    writer.write(frame(kind, payload))
    await asyncio.wait_for(writer.drain(), SEND_TIMEOUT_S)


async def send_event(writer: asyncio.StreamWriter, event: dict) -> None:
    await send(writer, EVENT, json.dumps(event, ensure_ascii=False).encode("utf-8"))


def keep_alive(writer: asyncio.StreamWriter) -> None:
    """Has the kernel notice a TV that vanished without closing the link."""
    sock = writer.get_extra_info("socket")
    if sock is None:
        return
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    options = {
        "TCP_KEEPIDLE": KEEPALIVE_IDLE_S,
        "TCP_KEEPINTVL": KEEPALIVE_INTERVAL_S,
        "TCP_KEEPCNT": KEEPALIVE_COUNT,
        "TCP_USER_TIMEOUT": USER_TIMEOUT_MS,
    }
    for option, value in options.items():
        if hasattr(socket, option):
            sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, option), value)


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
    transport, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(os.dup(receiver.meta_fd), "rb", 0),
    )
    try:
        buffer = b""
        track: Dict[str, str] = {}
        sent_track: Dict[str, str] = {}
        while True:
            chunk = await reader.read(METADATA_CHUNK)
            if not chunk:
                return
            buffer += chunk
            # A cover arrives in many reads; scanning only once an item has closed keeps that linear
            if buffer.find(ITEM_END, max(0, len(buffer) - len(chunk) - len(ITEM_END))) < 0:
                if len(buffer) > MAX_METADATA_BUFFER:
                    buffer = b""
                continue
            items, buffer = parse_items(buffer)
            if len(buffer) > MAX_METADATA_BUFFER:
                buffer = b""
            for kind, code, data in items:
                if kind == "core" and code in TRACK_FIELDS:
                    track[TRACK_FIELDS[code]] = data.decode("utf-8", "replace")
                elif code == "PICT":
                    await send(writer, COVER, data if len(data) <= MAX_COVER_BYTES else b"")
                elif code == "snam":
                    await send_event(writer, {"type": "session", "client": data.decode("utf-8", "replace")})
                elif code == "pvol":
                    level = volume_level(data)
                    if level is not None:
                        await send_event(writer, {"type": "volume", "level": level})
                elif state_event(code) is not None:
                    log.info("«%s»: %s", receiver.name, code)
                    if code == "aend":
                        track, sent_track = {}, {}
                    await send_event(writer, state_event(code))
            # Title, artist and album arrive as separate items; one event per change keeps the TV calm
            if track and track != sent_track:
                sent_track = dict(track)
                await send_event(writer, {"type": "track", **track})
    finally:
        transport.close()


async def pings(writer: asyncio.StreamWriter) -> None:
    while True:
        await asyncio.sleep(PING_SECONDS)
        await send(writer, PING)


async def remote_send(receiver: Receiver, method: str, *args: str) -> None:
    """Asks the sending iPhone to skip, pause or change volume through the receiver's own remote control."""
    try:
        process = await asyncio.create_subprocess_exec(
            *remote_command(receiver.bus, method, *args),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
    except OSError as error:
        log.warning("Remote %s for «%s» could not run: %s", method, receiver.name, error)
        return
    try:
        _, error = await asyncio.wait_for(process.communicate(), REMOTE_TIMEOUT_S + 1)
    except asyncio.TimeoutError:
        await end_process(process)
        log.warning("Remote %s for «%s» timed out", method, receiver.name)
        return
    if process.returncode != 0:
        log.warning("Remote %s for «%s» failed: %s", method, receiver.name, error.decode("utf-8", "replace").strip())


def tv_command(line: bytes) -> Optional[Tuple[str, Tuple[str, ...]]]:
    """The remote-control call for one line from the TV, or None for anything it does not understand."""
    try:
        command = json.loads(line.decode("utf-8"))
    except ValueError:
        return None
    if not isinstance(command, dict):
        return None
    cmd, level = command.get("cmd"), command.get("level")
    if cmd == "volume" and isinstance(level, (int, float)) and not isinstance(level, bool):
        return VOLUME_METHOD, (f"double:{airplay_volume(float(level)):.3f}",)
    if isinstance(cmd, str) and cmd in COMMANDS:
        return COMMANDS[cmd], ()
    return None


async def tv_commands(receiver: Receiver, reader: asyncio.StreamReader) -> None:
    """Button presses and volume changes from the TV; the TV hanging up ends the session."""
    while True:
        try:
            line = await reader.readline()
        except ValueError:
            # A line longer than the stream allows is not from a Steklo TV
            return
        if not line:
            return
        call = tv_command(line)
        if call is not None:
            await remote_send(receiver, call[0], *call[1])


async def serve_tv(registry: Registry, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    try:
        hello = json.loads((await asyncio.wait_for(reader.readline(), HELLO_TIMEOUT_S)).decode("utf-8"))
        name = clean_name(hello.get("name", ""))
        receiver = await registry.open(name)
    except (asyncio.TimeoutError, ValueError, AttributeError, RuntimeError, OSError) as error:
        log.warning("Rejected TV %s: %s", peer, error)
        writer.close()
        return
    keep_alive(writer)
    log.info("TV %s linked as «%s»", peer, name)
    tasks = [
        asyncio.ensure_future(pump_audio(receiver, writer)),
        asyncio.ensure_future(pump_metadata(receiver, writer)),
        asyncio.ensure_future(pings(writer)),
        asyncio.ensure_future(tv_commands(receiver, reader)),
    ]
    try:
        await send_event(writer, {"type": "receiver", "name": name, "lead_ms": lead_ms(registry.options, name)})
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                log.info("TV «%s» link ended: %r", name, task.exception())
    except (ConnectionError, OSError, asyncio.TimeoutError) as error:
        log.info("TV «%s» link broke: %r", name, error)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await registry.close(receiver)
        finally:
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
        # No host: listen on IPv6 as well, since TVs may resolve the bridge to its IPv6 address first
        lambda reader, writer: serve_tv(registry, reader, writer), host=None, port=options["bridge_port"],
    )
    log.info("Waiting for Steklo TVs on port %d", options["bridge_port"])
    # Home Assistant stops the add-on with SIGTERM; the speakers say goodbye on mDNS instead of lingering on iPhones
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)
    await stopping.wait()
    log.info("Stopping: taking every AirPlay speaker down")
    server.close()
    await registry.close_all()


if __name__ == "__main__":
    asyncio.run(main())
