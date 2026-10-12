"""neo/tests/test_tokens.py - token 的形状、按服务器分桶的缓存、局域网地址与状态。

不碰网络（lan_addresses 里那条 UDP connect 不发数据，测试直接测纯函数
is_lan_ip，需要地址列表的用例把 lan_addresses 换掉）。
状态文件指到临时目录，不写真的 neo/state。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from neo import state, tokens                                        # noqa: E402


class TestKeyShape(unittest.TestCase):
    def test_shape_and_prefix(self):
        key = tokens.new_key()
        self.assertTrue(key.startswith("sk-neo-"))
        self.assertEqual(len(key), len("sk-neo-") + 32)
        self.assertEqual(tokens.prefix_of(key), key[len("sk-neo-"):][:8])
        self.assertTrue(tokens.looks_like_key(key))

    def test_keys_are_unique(self):
        keys = {tokens.new_key() for _ in range(20)}
        self.assertEqual(len(keys), 20)

    def test_mask_does_not_leak_the_whole_key(self):
        key = tokens.new_key()
        masked = tokens.mask_of(key)
        self.assertNotIn(key, masked)
        self.assertIn(tokens.prefix_of(key), masked)

    def test_looks_like_key_rejects_junk(self):
        for bad in ("", "sk-neo-", "sk-neo-zzzzzzzzzzzz", "abc1234567890", "sk-unsloth-0123456789"):
            self.assertFalse(tokens.looks_like_key(bad), bad)


class TestKeyCache(unittest.TestCase):
    def setUp(self):
        self._was = state.STATE
        self._tmp = tempfile.TemporaryDirectory()
        state.STATE = Path(self._tmp.name)

    def tearDown(self):
        state.STATE = self._was
        self._tmp.cleanup()

    def test_saved_and_minted_are_separate_buckets(self):
        tokens.remember_key("http://127.0.0.1:8080", "sk-neo-" + "a" * 32, "saved")
        tokens.remember_key("http://127.0.0.1:8080", "sk-neo-" + "b" * 32, "minted")
        self.assertEqual(tokens.cached_keys("http://127.0.0.1:8080", "saved"),
                         ["sk-neo-" + "a" * 32])
        self.assertEqual(tokens.cached_keys("http://127.0.0.1:8080", "minted"),
                         ["sk-neo-" + "b" * 32])

    def test_keys_are_scoped_per_server(self):
        tokens.remember_key("http://127.0.0.1:8080", "sk-neo-" + "c" * 32, "saved")
        self.assertEqual(tokens.cached_keys("http://192.168.1.5:8080", "saved"), [],
                         "给这台服务器的 key 不能流到那台")

    def test_newest_first_and_no_duplicates(self):
        base = "http://127.0.0.1:8080"
        tokens.remember_key(base, "sk-neo-" + "1" * 32, "saved")
        tokens.remember_key(base, "sk-neo-" + "2" * 32, "saved")
        tokens.remember_key(base, "sk-neo-" + "1" * 32, "saved")
        self.assertEqual(tokens.cached_keys(base, "saved"),
                         ["sk-neo-" + "1" * 32, "sk-neo-" + "2" * 32])

    def test_bucket_is_capped(self):
        base = "http://127.0.0.1:8080"
        for i in range(12):
            tokens.remember_key(base, "sk-neo-" + format(i, "032x"), "saved")
        self.assertEqual(len(tokens.cached_keys(base, "saved")), 8)

    def test_forget(self):
        base = "http://127.0.0.1:8080"
        key = "sk-neo-" + "9" * 32
        tokens.remember_key(base, key, "saved")
        self.assertEqual(tokens.forget_key(base, key), 1)
        self.assertEqual(tokens.cached_keys(base, "saved"), [])

    def test_legacy_list_shape_is_read_as_minted(self):
        tokens.key_cache_path().parent.mkdir(parents=True, exist_ok=True)
        tokens.key_cache_path().write_text(
            '{"servers": {"http://127.0.0.1:8080": ["sk-neo-' + "7" * 32 + '"]}}',
            encoding="utf-8")
        self.assertEqual(tokens.cached_keys("http://127.0.0.1:8080", "minted"),
                         ["sk-neo-" + "7" * 32])
        self.assertEqual(tokens.cached_keys("http://127.0.0.1:8080", "saved"), [])


class TestLan(unittest.TestCase):
    def test_is_lan_ip_filtering(self):
        good = ("192.168.1.20", "10.0.0.5", "172.16.3.4")
        bad = ("127.0.0.1", "169.254.1.1", "0.0.0.0", "::", "224.0.0.1", "fe80::1", "")
        for ip in good:
            self.assertTrue(tokens.is_lan_ip(ip), ip)
        for ip in bad:
            self.assertFalse(tokens.is_lan_ip(ip), ip)

    def test_bases(self):
        self.assertEqual(tokens.loopback_base(8080), "http://127.0.0.1:8080")
        was = tokens.lan_addresses
        tokens.lan_addresses = lambda: ["192.168.1.20"]
        try:
            self.assertEqual(tokens.lan_bases(8080), ["http://192.168.1.20:8080"])
            self.assertEqual(tokens.base_for("0.0.0.0", 8080), "http://192.168.1.20:8080")
            self.assertEqual(tokens.base_for("127.0.0.1", 8080), "http://127.0.0.1:8080")
        finally:
            tokens.lan_addresses = was

    def test_firewall_hint_names_the_port(self):
        self.assertIn("localport=8080", tokens.firewall_hint(8080))
        self.assertIn("netsh", tokens.firewall_hint(8080))

    def test_status_says_lan_without_key_needs_one(self):
        status = tokens.token_status(8080, "0.0.0.0", "")
        self.assertTrue(status["lan_open"])
        self.assertFalse(status["has_api_key"])
        self.assertIn("必须设 token", status["note"])
        self.assertIn("localport=8080", status["firewall_hint"])

    def test_status_lan_with_key_is_quiet(self):
        status = tokens.token_status(8080, "0.0.0.0", "sk-neo-" + "3" * 32)
        self.assertTrue(status["has_api_key"])
        self.assertIn("已设 token", status["note"])

    def test_status_loopback_is_plain(self):
        status = tokens.token_status(8080, "127.0.0.1", "")
        self.assertFalse(status["lan_open"])
        self.assertEqual(status["firewall_hint"], "")
        self.assertIn("只绑本机", status["note"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
