import asyncio
import base64
import os
import stat
import struct
import tempfile
import unittest

import bridge


def item(kind: str, code: str, data: bytes = b"") -> bytes:
    head = f"<item><type>{kind.encode().hex()}</type><code>{code.encode().hex()}</code><length>{len(data)}</length>"
    body = f"\n<data encoding=\"base64\">\n{base64.b64encode(data).decode()}</data>" if data else ""
    return (head + body + "</item>\n").encode()


class BridgeTest(unittest.TestCase):
    def test_room_names_become_ascii_slugs(self):
        self.assertEqual("spalnaya-yulii", bridge.slug("Спальная Юлии"))
        self.assertEqual("kukhnya", bridge.slug("Кухня"))
        self.assertEqual("tv", bridge.slug("!!!"))

    def test_names_that_slug_alike_still_get_their_own_speaker_files(self):
        self.assertNotEqual(bridge.receiver_key("Кухня"), bridge.receiver_key("kukhnya"))
        self.assertTrue(bridge.receiver_key("Кухня").startswith("kukhnya-"))
        self.assertEqual(bridge.receiver_key("Кухня"), bridge.receiver_key("Кухня"))

    def test_hello_names_lose_control_characters(self):
        self.assertEqual("ab", bridge.clean_name("a\u0000b"))
        self.assertEqual("Кухня", bridge.clean_name("  Кухня\n"))
        self.assertEqual(bridge.DEFAULT_NAME, bridge.clean_name("\u0007"))
        self.assertEqual(bridge.MAX_NAME, len(bridge.clean_name("я" * 100)))

    def test_frames_carry_type_and_big_endian_length(self):
        self.assertEqual(struct.pack(">BI", 2, 3) + b"abc", bridge.frame(bridge.EVENT, b"abc"))
        self.assertEqual(b"\x09\x00\x00\x00\x00", bridge.frame(bridge.PING))

    def test_metadata_items_are_decoded_and_the_unfinished_rest_kept(self):
        whole = item("core", "minm", "Песня".encode()) + item("ssnc", "pbeg")
        partial = item("core", "asar", b"Artist")[:20]

        items, rest = bridge.parse_items(whole + partial)

        self.assertEqual([("core", "minm", "Песня".encode()), ("ssnc", "pbeg", b"")], items)
        self.assertEqual(partial, rest)

    def test_a_damaged_metadata_item_is_skipped_not_fatal(self):
        damaged = b"<item><type>73736e63</type><code>70766f6c</code><length>3</length>" \
                  b"<data encoding=\"base64\">abcde</data></item>"

        items, rest = bridge.parse_items(damaged + item("ssnc", "pbeg"))

        self.assertEqual([("ssnc", "pbeg", b"")], items)
        self.assertEqual(b"", rest)

    def test_play_state_codes_become_state_events(self):
        self.assertEqual({"type": "state", "state": "playing"}, bridge.state_event("pbeg"))
        self.assertEqual({"type": "state", "state": "paused"}, bridge.state_event("paus"))
        self.assertIsNone(bridge.state_event("pvol"))

    def test_a_paused_session_keeps_its_player_until_the_session_ends(self):
        # An AirPlay 2 sender can end the stream on pause; only the end of the active session stops the player
        self.assertEqual({"type": "state", "state": "paused"}, bridge.state_event("pend"))
        self.assertEqual({"type": "state", "state": "stopped"}, bridge.state_event("aend"))
        conf = bridge.shairport_config("Кухня", 7001, "/run/steklo/kukhnya.meta", -150)
        self.assertIn(f"active_state_timeout = {bridge.PAUSED_SESSION_S:.1f};", conf)

    def test_audio_is_passed_on_in_whole_stereo_frames(self):
        aligned, rest = bridge.whole_frames(b"\x01\x02\x03\x04\x05\x06")

        self.assertEqual(b"\x01\x02\x03\x04", aligned)
        self.assertEqual(b"\x05\x06", rest)

    def test_the_receiver_config_names_the_room_and_sends_16_bit_audio_to_stdout(self):
        conf = bridge.shairport_config('Кухня "у окна"', 7001, "/run/steklo/kukhnya.meta", -150)

        self.assertIn('name = "Кухня \\"у окна\\"";', conf)
        self.assertIn("port = 7001;", conf)
        self.assertIn('output_backend = "stdout";', conf)
        self.assertIn('output_format = "S16_LE";', conf)
        self.assertIn("output_rate = 44100;", conf)
        self.assertIn("audio_backend_latency_offset_in_seconds = -0.150;", conf)
        self.assertIn('pipe_name = "/run/steklo/kukhnya.meta";', conf)
        # A fixed device id would stop shairport-sync deriving a unique one per room
        self.assertNotIn("airplay_device_id", conf)
        # Every receiver claims the same D-Bus name, so each one serves remote control on its own bus
        self.assertIn('dbus_service_bus = "session";', conf)
        self.assertIn('mpris_service_bus = "session";', conf)

    def test_the_receiver_leaves_volume_to_the_tv(self):
        conf = bridge.shairport_config("Кухня", 7001, "/run/steklo/kukhnya.meta", -150)

        self.assertIn('ignore_volume_control = "yes";', conf)

    def test_the_senders_volume_becomes_a_level_from_zero_to_one(self):
        self.assertEqual(1.0, bridge.volume_level(b"0.00,0.00,0.00,0.00"))
        self.assertEqual(0.5, bridge.volume_level(b"-15.00,0.00,0.00,0.00"))
        self.assertEqual(0.0, bridge.volume_level(b"-30.00,0.00,0.00,0.00"))
        self.assertEqual(0.0, bridge.volume_level(b"-144.00,0.00,0.00,0.00"))
        self.assertIsNone(bridge.volume_level(b"loud"))

    def test_the_tvs_volume_goes_back_to_the_sender_as_airplay_volume(self):
        self.assertEqual(0.0, bridge.airplay_volume(1.0))
        self.assertEqual(-15.0, bridge.airplay_volume(0.5))
        self.assertEqual(bridge.AIRPLAY_MUTED, bridge.airplay_volume(0.0))
        self.assertEqual(0.0, bridge.airplay_volume(1.7))

    def test_tv_buttons_call_the_receivers_remote_control_on_its_own_bus(self):
        method, args = bridge.tv_command(b'{"cmd":"next"}\n')
        command = bridge.remote_command("unix:path=/run/steklo/kukhnya.bus", method, *args)

        self.assertEqual("dbus-send", command[0])
        self.assertIn("--bus=unix:path=/run/steklo/kukhnya.bus", command)
        self.assertIn("--dest=org.gnome.ShairportSync", command)
        self.assertEqual(["/org/gnome/ShairportSync", "org.gnome.ShairportSync.RemoteControl.Next"], command[-2:])
        self.assertEqual(("PlayPause", ()), bridge.tv_command(b'{"cmd":"playpause"}'))
        self.assertEqual(("Previous", ()), bridge.tv_command(b'{"cmd":"previous"}'))

    def test_a_tv_volume_change_becomes_set_airplay_volume(self):
        self.assertEqual(("SetAirplayVolume", ("double:-15.000",)), bridge.tv_command(b'{"cmd":"volume","level":0.5}'))

    def test_lines_the_bridge_does_not_understand_are_ignored(self):
        for line in (b"{", b"[]", b'{"cmd":[]}', b'{"cmd":{}}', b'{"cmd":"reboot"}', b'{"cmd":"volume","level":true}'):
            self.assertIsNone(bridge.tv_command(line), line)


FAKE_DBUS = "#!/bin/sh\necho unix:path=fake,guid=x\nexec sleep 1000\n"
FAKE_SHAIRPORT = "#!/bin/sh\nexec sleep 1000\n"
FAILING_SHAIRPORT = "#!/bin/sh\nexit 3\n"


class RegistryTest(unittest.IsolatedAsyncioTestCase):
    """Speakers come and go with fake shairport-sync and dbus-daemon programs in a scratch folder."""

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        root = self.scratch.name
        self.bin = os.path.join(root, "bin")
        os.makedirs(self.bin)
        self.write_program("dbus-daemon", FAKE_DBUS)
        self.shairport = self.write_program("shairport-sync", FAKE_SHAIRPORT)
        self.saved = (bridge.RUN_DIR, bridge.SHAIRPORT, bridge.START_CHECK_S, os.environ["PATH"])
        bridge.RUN_DIR = os.path.join(root, "run")
        os.makedirs(bridge.RUN_DIR)
        bridge.SHAIRPORT = self.shairport
        os.environ["PATH"] = self.bin + os.pathsep + os.environ["PATH"]
        self.registry = bridge.Registry(dict(bridge.DEFAULT_OPTIONS))

    async def asyncTearDown(self):
        # A fake left running would hold the test runner's output open
        await self.registry.close_all()

    def tearDown(self):
        bridge.RUN_DIR, bridge.SHAIRPORT, bridge.START_CHECK_S, os.environ["PATH"] = self.saved
        self.scratch.cleanup()

    def write_program(self, name: str, text: str) -> str:
        path = os.path.join(self.bin, name)
        with open(path, "w") as program:
            program.write(text)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return path

    async def test_tvs_linking_at_once_get_their_own_ports(self):
        registry = self.registry

        one, two = await asyncio.gather(registry.open("Один"), registry.open("Два"))

        self.assertNotEqual(one.port, two.port)
        await registry.close_all()

    async def test_an_old_session_ending_late_leaves_the_new_speaker_alone(self):
        registry = self.registry
        old = await registry.open("Кухня")
        new = await registry.open("Кухня")

        await registry.close(old)

        self.assertTrue(os.path.exists(new.meta_pipe))
        self.assertTrue(os.path.exists(new.conf))
        self.assertIs(new, registry.receivers["Кухня"])
        await registry.close_all()
        self.assertEqual([], os.listdir(bridge.RUN_DIR))

    async def test_names_that_slug_alike_do_not_remove_each_others_files(self):
        registry = self.registry
        cyrillic = await registry.open("Кухня")
        latin = await registry.open("kukhnya")

        await registry.close(cyrillic)

        self.assertTrue(os.path.exists(latin.meta_pipe))
        await registry.close_all()

    async def test_a_speaker_that_fails_to_start_leaves_nothing_behind(self):
        self.write_program("shairport-sync", FAILING_SHAIRPORT)
        registry = self.registry

        with self.assertRaises(RuntimeError):
            await registry.open("Кухня")

        self.assertEqual({}, registry.receivers)
        self.assertEqual([], os.listdir(bridge.RUN_DIR))


if __name__ == "__main__":
    unittest.main()
