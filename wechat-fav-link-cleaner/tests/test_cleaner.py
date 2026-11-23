# -*- coding: utf-8 -*-
"""离线单元测试：不发起真实网络请求。"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

import wechat_fav_link_cleaner as cleaner


class FakeResponse:
    def __init__(self, status_code, url):
        self.status_code = status_code
        self.url = url

    def close(self):
        pass


class ExtractTests(unittest.TestCase):
    def test_extract_urls_basic(self):
        text = (
            "看看这个 https://www.example.com/a?x=1&y=2 还有 "
            "<a href=\"http://foo.com/b\">链接</a>，"
            "以及 https://bad.com/path）。"
        )
        urls = cleaner.extract_urls(text)
        self.assertIn("https://www.example.com/a?x=1&y=2", urls)
        self.assertIn("http://foo.com/b", urls)
        self.assertIn("https://bad.com/path", urls)
        self.assertEqual(len(urls), 3)

    def test_extract_urls_ignores_non_http(self):
        self.assertEqual(cleaner.extract_urls("ftp://a.com x://b.com 文本"), [])

    def test_extract_from_csv(self):
        sample = Path(__file__).parents[1] / "examples" / "sample_favorites.csv"
        items = cleaner.extract_from_file(sample)
        self.assertEqual(len(items), 3)
        self.assertTrue(all(i.source == sample.name for i in items))
        self.assertTrue(any("不存在的路径" in i.title for i in items))


class ClassifyTests(unittest.TestCase):
    def test_dead_status(self):
        item = cleaner.LinkItem(url="https://a.com")
        cleaner.classify(item, 404, "")
        self.assertEqual(item.status, "dead")
        self.assertEqual(item.confidence(), "高")

    def test_suspect_status(self):
        item = cleaner.LinkItem(url="https://a.com")
        cleaner.classify(item, 403, "")
        self.assertEqual(item.status, "suspect")
        self.assertEqual(item.confidence(), "低")

    def test_timeout_is_suspect(self):
        item = cleaner.LinkItem(url="https://a.com")
        cleaner.classify(item, None, "timeout")
        self.assertEqual(item.status, "suspect")

    def test_dns_is_dead(self):
        item = cleaner.LinkItem(url="https://a.com")
        cleaner.classify(item, None, "dns")
        self.assertEqual(item.status, "dead")

    def test_ok(self):
        item = cleaner.LinkItem(url="https://a.com")
        cleaner.classify(item, 200, "")
        self.assertEqual(item.status, "ok")


class CheckOneTests(unittest.TestCase):
    def test_404(self):
        session = Mock()
        session.head.return_value = FakeResponse(404, "https://a.com/gone")
        item = cleaner.check_one(cleaner.LinkItem(url="https://a.com/gone"), session, 2, 0)
        self.assertEqual(item.status, "dead")
        self.assertEqual(item.http_code, 404)

    def test_head_403_get_fallback(self):
        session = Mock()
        session.head.return_value = FakeResponse(403, "https://a.com")
        session.get.return_value = FakeResponse(200, "https://a.com")
        item = cleaner.check_one(cleaner.LinkItem(url="https://a.com"), session, 2, 0)
        self.assertEqual(item.status, "ok")
        session.get.assert_called_once()

    def test_dns_failure(self):
        session = Mock()
        session.head.side_effect = requests.exceptions.ConnectionError(
            "[Errno 11001] getaddrinfo failed"
        )
        item = cleaner.check_one(cleaner.LinkItem(url="https://no-such.invalid"), session, 2, 1)
        self.assertEqual(item.status, "dead")
        self.assertIn("域名", item.reason)

    def test_timeout_retry_then_suspect(self):
        session = Mock()
        session.head.side_effect = requests.exceptions.Timeout("timed out")
        item = cleaner.check_one(cleaner.LinkItem(url="https://slow.com"), session, 2, 1)
        self.assertEqual(item.status, "suspect")
        self.assertEqual(session.head.call_count, 2)


class OutputTests(unittest.TestCase):
    def test_write_outputs_and_confirm(self):
        items = [
            cleaner.LinkItem(url="https://a.com/gone", title="失效A", status="dead",
                             reason="HTTP 404", http_code=404),
            cleaner.LinkItem(url="https://b.com/403", title="疑似B", status="suspect",
                             reason="HTTP 403", http_code=403),
            cleaner.LinkItem(url="https://c.com", title="正常C", status="ok", reason="HTTP 200"),
        ]
        candidates = [x for x in items if x.status != "ok"]
        with patch("builtins.input", return_value="y"):
            picked = cleaner.ask_confirmation(candidates, auto_yes=False)
        self.assertEqual(len(picked), 2)

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            cleaner.write_csv(out / "delete_list.csv", picked)
            cleaner.write_markdown(out / "delete_list.md", picked, "2026-01-01 00:00:00")
            self.assertTrue((out / "delete_list.csv").exists())
            md = (out / "delete_list.md").read_text(encoding="utf-8")
            self.assertIn("失效A", md)
            self.assertIn("疑似B", md)
            self.assertNotIn("正常C", md)

    def test_confirm_cancel(self):
        items = [cleaner.LinkItem(url="https://a.com", status="dead", reason="HTTP 404")]
        with patch("builtins.input", return_value="n"):
            self.assertEqual(cleaner.ask_confirmation(items, auto_yes=False), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
