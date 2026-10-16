# -*- coding: utf-8 -*-
"""Unit tests for TokenPrices. All network I/O is mocked with a small
OpenRouter-style fixture, so the suite runs offline and fast."""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tokenprices as tp  # noqa: E402

FIXTURE = {
    "data": [
        {"id": "openai/gpt-6.1-sol", "name": "GPT-6.1 Sol",
         "context_length": 1050000,
         "pricing": {"prompt": "0.000002", "completion": "0.00001"},
         "top_provider": {"provider": "openai", "context_length": 1050000}},
        {"id": "deepseek/deepseek-v4-flash", "name": "DeepSeek V4 Flash",
         "context_length": 1048576,
         "pricing": {"prompt": "0.000000091", "completion": "0.000000182"},
         "top_provider": {"provider": "deepseek", "context_length": 1048576}},
        {"id": "moonshotai/kimi-k3", "name": "Kimi K3",
         "context_length": 1048576,
         "pricing": {"prompt": "0.000000304", "completion": "0.00001194"},
         "top_provider": {"provider": "moonshotai", "context_length": 1048576}},
        {"id": "nvidia/nemotron-3.5-super-vl:free", "name": "Nemotron Free",
         "context_length": 1000000,
         "pricing": {"prompt": "0", "completion": "0"},
         "top_provider": {"provider": "nvidia", "context_length": 1000000}},
        {"id": "openrouter/auto-beta", "name": "Auto Beta (route)",
         "context_length": 2000000,
         "pricing": {"prompt": "-1", "completion": "-1"},
         "top_provider": {"provider": "openrouter", "context_length": 2000000}},
    ]
}


def fake_fetch(url):
    return json.dumps(FIXTURE)


def run_cli(*argv):
    buf = io.StringIO()
    with mock.patch.object(sys, "stdout", buf):
        code = tp.main(list(argv))
    return code, buf.getvalue()


class TestNumFmt(unittest.TestCase):
    def test_num_parses_strings_and_numbers(self):
        self.assertEqual(tp._num("0.0000003"), 3e-7)
        self.assertEqual(tp._num(3e-07), 3e-07)
        self.assertIsNone(tp._num(None))
        self.assertIsNone(tp._num(""))
        self.assertIsNone(tp._num("none"))

    def test_num_rejects_negative(self):
        # OpenRouter uses -1 for route-only models with no posted price
        self.assertIsNone(tp._num("-1"))
        self.assertIsNone(tp._num(-1))

    def test_fmt(self):
        self.assertEqual(tp.fmt(None, "en"), "—")
        self.assertEqual(tp.fmt(0, "en"), "free")
        self.assertEqual(tp.fmt(0.091, "en"), "0.091")
        self.assertEqual(tp.fmt(10.0, "en"), "10.00")
        self.assertEqual(tp.fmt(120.5, "en"), "120")


class TestFetch(unittest.TestCase):
    @mock.patch("tokenprices.http_text", fake_fetch)
    def test_openrouter_normalization(self):
        models = tp.fetch_openrouter()
        by_id = {m["id"]: m for m in models}
        # per-token -> per-1M conversion
        self.assertAlmostEqual(by_id["openai/gpt-6.1-sol"]["in"], 2.0, places=4)
        self.assertAlmostEqual(by_id["openai/gpt-6.1-sol"]["out"], 10.0, places=4)
        self.assertAlmostEqual(by_id["deepseek/deepseek-v4-flash"]["in"], 0.091, places=6)
        self.assertEqual(by_id["deepseek/deepseek-v4-flash"]["ctx"], 1048576)
        self.assertEqual(by_id["deepseek/deepseek-v4-flash"]["provider"], "deepseek")
        # free and route-only models
        self.assertEqual(by_id["nvidia/nemotron-3.5-super-vl:free"]["in"], 0.0)
        self.assertNotIn("openrouter/auto-beta", by_id)  # negative pricing dropped


class TestResolve(unittest.TestCase):
    def setUp(self):
        with mock.patch("tokenprices.http_text", fake_fetch):
            self.models = tp.fetch_openrouter()

    def test_exact_id(self):
        hits = tp.resolve(self.models, "openai/gpt-6.1-sol")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["id"], "openai/gpt-6.1-sol")

    def test_fuzzy_id(self):
        hits = tp.resolve(self.models, "kimi-k3")
        self.assertEqual(hits[0]["id"], "moonshotai/kimi-k3")

    def test_provider_match(self):
        hits = tp.resolve(self.models, "deepseek")
        self.assertTrue(any("deepseek" in h["id"] for h in hits))

    def test_no_match(self):
        self.assertEqual(tp.resolve(self.models, "does-not-exist"), [])


class TestCost(unittest.TestCase):
    def test_cost_math(self):
        # gpt-6.1-sol: $2/M in, $10/M out
        inp, outp = 2.0, 10.0
        cost = inp * 1000 / 1e6 + outp * 500 / 1e6
        self.assertAlmostEqual(cost, 0.007, places=9)


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tp.CACHE_DIR = Path(self.tmp.name)
        self.patchers = [
            mock.patch("tokenprices.http_text", fake_fetch),
            mock.patch("tokenprices.CACHE_DIR", tp.CACHE_DIR),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in self.patchers:
            p.stop()
        self.tmp.cleanup()

    def test_list(self):
        code, out = run_cli("list", "--limit", "3")
        self.assertEqual(code, 0)
        self.assertIn("gpt-6.1-sol", out)

    def test_search(self):
        code, out = run_cli("search", "deepseek")
        self.assertEqual(code, 0)
        self.assertIn("deepseek-v4-flash", out)

    def test_search_free(self):
        code, out = run_cli("search", "--free", "--limit", "2")
        self.assertEqual(code, 0)
        self.assertIn("free", out.lower())

    def test_compare(self):
        code, out = run_cli("compare", "gpt-6.1-sol", "kimi-k3")
        self.assertEqual(code, 0)
        self.assertIn("gpt-6.1-sol", out)
        self.assertIn("kimi-k3", out)

    def test_top_skips_negatives(self):
        code, out = run_cli("top", "3")
        self.assertEqual(code, 0)
        self.assertNotIn("auto-beta", out)  # -1 pricing dropped

    def test_cost(self):
        code, out = run_cli("cost", "gpt-6.1-sol", "--in", "1000", "--out", "500")
        self.assertEqual(code, 0)
        self.assertIn("0.007", out)

    def test_html_report(self):
        path = Path(self.tmp.name) / "rep.html"
        code, out = run_cli("list", "--limit", "3", "--html", str(path))
        self.assertEqual(code, 0)
        html = path.read_text(encoding="utf-8")
        self.assertIn("<html", html)
        self.assertIn("deepseek-v4-flash", html)  # cheapest models appear in report
        self.assertIn("svg", html)

    def test_domestic_table_has_sources(self):
        for d in tp.DOMESTIC:
            self.assertTrue(d["src"].startswith("http"), d["id"])
            self.assertTrue(d["provider"], d["id"])
            self.assertIsNotNone(d.get("in") or d.get("out"), d["id"])


if __name__ == "__main__":
    unittest.main()
