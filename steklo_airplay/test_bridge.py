import base64
import struct
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

    def test_frames_carry_type_and_big_endian_length(self):
        self.assertEqual(struct.pack(">BI", 2, 3) + b"abc", bridge.frame(bridge.EVENT, b"abc"))
        self.assertEqual(b"\x09\x00\x00\x00\x00", bridge.frame(bridge.PING))

    def test_metadata_items_are_decoded_and_the_unfinished_rest_kept(self):
        whole = item("core", "minm", "Песня".encode()) + item("ssnc", "pbeg")
        partial = item("core", "asar", b"Artist")[:20]

        items, rest = bridge.parse_items(whole + partial)

        self.assertEqual([("core", "minm", "Песня".encode()), ("ssnc", "pbeg", b"")], items)
        self.assertEqual(partial, rest)

    def test_play_state_codes_become_state_events(self):
        self.assertEqual({"type": "state", "state": "playing"}, bridge.state_event("pbeg"))
        self.assertEqual({"type": "state", "state": "paused"}, bridge.state_event("paus"))
        self.assertEqual({"type": "state", "state": "stopped"}, bridge.state_event("pend"))
        self.assertIsNone(bridge.state_event("pvol"))

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

    def test_the_senders_remote_control_service_is_found_by_its_dacp_id(self):
        output = "\n".join([
            "+;end0;IPv4;iTunes_Ctrl_ABCD1234;_dacp._tcp;local",
            "=;end0;IPv6;iTunes_Ctrl_ABCD1234;_dacp._tcp;local;iPhone.local;fe80::1;50122;",
            "=;end0;IPv4;iTunes_Ctrl_FFFF0000;_dacp._tcp;local;iPad.local;192.168.1.40;50123;",
            "=;end0;IPv4;iTunes_Ctrl_ABCD1234;_dacp._tcp;local;iPhone.local;192.168.1.33;50122;",
        ])

        self.assertEqual(("192.168.1.33", 50122), bridge.parse_dacp_browse(output, "abcd1234"))
        self.assertIsNone(bridge.parse_dacp_browse(output, "00000000"))

    def test_tv_buttons_map_to_dacp_verbs(self):
        self.assertEqual("playpause", bridge.COMMANDS["playpause"])
        self.assertEqual("nextitem", bridge.COMMANDS["next"])
        self.assertEqual("previtem", bridge.COMMANDS["previous"])


if __name__ == "__main__":
    unittest.main()
