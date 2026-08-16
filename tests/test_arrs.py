"""The metadata guard on outbound arr requests. No network touched.

The point of these tests is the pair of opposite mistakes: letting a caller
aim an authenticated request at 169.254.169.254, and "fixing" that by blocking
private space, which is where every real Radarr and Sonarr actually lives.
"""

import os
import socket
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import arrs  # noqa: E402


def resolves_to(*addresses):
    return lambda host, port=None, *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0)) for addr in addresses
    ]


class LinkLocalIsRefused(unittest.TestCase):
    def assert_blocked(self, url, *addresses):
        with mock.patch.object(socket, "getaddrinfo", resolves_to(*addresses)):
            reason = arrs.blocked_reason(url)
        self.assertIsNotNone(reason, f"{url} -> {addresses} should have been refused")
        self.assertIn("metadata", reason)

    def test_the_literal_metadata_address(self):
        self.assert_blocked("http://169.254.169.254/latest/meta-data/", "169.254.169.254")

    def test_a_hostname_pointed_at_metadata(self):
        # The whole reason resolution happens before the decision.
        self.assert_blocked("http://radarr.evil.example:7878", "169.254.169.254")

    def test_any_address_in_the_range_not_just_the_famous_one(self):
        self.assert_blocked("http://169.254.170.2", "169.254.170.2")

    def test_the_ipv4_mapped_form(self):
        self.assert_blocked("http://[::ffff:169.254.169.254]", "::ffff:169.254.169.254")

    def test_the_6to4_form(self):
        self.assert_blocked("http://[2002:a9fe:a9fe::]", "2002:a9fe:a9fe::")

    def test_ipv6_link_local(self):
        self.assert_blocked("http://[fe80::1]:7878", "fe80::1%eth0")

    def test_one_bad_answer_among_good_ones_is_enough(self):
        # A rebinding-style name that answers with both.
        self.assert_blocked("http://mixed.example", "192.168.1.50", "169.254.169.254")


class RealArrsStayReachable(unittest.TestCase):
    def assert_allowed(self, url, *addresses):
        with mock.patch.object(socket, "getaddrinfo", resolves_to(*addresses)):
            self.assertIsNone(arrs.blocked_reason(url))

    def test_lan(self):
        self.assert_allowed("http://192.168.1.50:7878", "192.168.1.50")
        self.assert_allowed("http://10.0.0.5:8989", "10.0.0.5")

    def test_docker_bridge_by_container_name(self):
        self.assert_allowed("http://sonarr:8989", "172.17.0.4")

    def test_loopback_when_host_networked(self):
        self.assert_allowed("http://127.0.0.1:7878", "127.0.0.1")
        self.assert_allowed("http://localhost:7878", "127.0.0.1")

    def test_public_hosts(self):
        self.assert_allowed("https://arr.example.com", "93.184.216.34")

    def test_a_name_that_does_not_resolve_gets_the_real_dns_error_instead(self):
        def boom(*a, **k):
            raise socket.gaierror("Name or service not known")

        with mock.patch.object(socket, "getaddrinfo", boom):
            self.assertIsNone(arrs.blocked_reason("http://typo.local:7878"))


class TheGuardRunsBeforeAnySocket(unittest.TestCase):
    def test_the_api_key_never_leaves_the_process(self):
        def never(*a, **k):
            raise AssertionError("a request was made to a blocked host")

        with mock.patch.object(socket, "getaddrinfo", resolves_to("169.254.169.254")), \
             mock.patch.object(arrs._opener, "open", never):
            ok, detail = arrs.test("http://169.254.169.254", "secret-key")
        self.assertFalse(ok)
        self.assertIn("metadata", detail)

    def test_a_stored_row_is_guarded_too_not_just_the_test_button(self):
        # The rescan path reuses a saved base_url, which may have been saved
        # before this guard existed.
        def never(*a, **k):
            raise AssertionError("a request was made to a blocked host")

        client = arrs.ArrClient({
            "id": "x", "name": "evil", "kind": "radarr",
            "base_url": "http://169.254.169.254", "api_key": "secret-key",
            "arr_path": "/movies", "worker_path": "/media/Movies",
        })
        with mock.patch.object(socket, "getaddrinfo", resolves_to("169.254.169.254")), \
             mock.patch.object(arrs._opener, "open", never):
            items, error = client.items()
        self.assertEqual(items, [])
        self.assertIn("metadata", error)


if __name__ == "__main__":
    unittest.main()
