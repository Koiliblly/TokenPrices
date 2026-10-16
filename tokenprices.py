#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TokenPrices — the LLM API price board for your terminal.

One command shows the live per-million-token price of 400+ LLM models
(OpenAI, Anthropic, Google, DeepSeek, xAI, Meta, Alibaba, ByteDance, …),
finds the cheapest model for your workload, compares models side by side,
and computes how much a prompt will cost. Terminal, JSON, or a share-ready
HTML report. EN / 中文.

Data sources (all keyless, all official):
  - OpenRouter models API  https://openrouter.ai/api/v1/models  (465+ models, live)
  - LiteLLM pricing DB     https://github.com/BerriAI/litellm    (4,000+ entries)
  - Built-in domestic reference table (DeepSeek / Zhipu GLM / Moonshot Kimi,
    plus current OpenAI / Google flagship list prices) with source links.

Requires Python 3.8+. Set HTTPS_PROXY if needed.
"""

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TokenPrices/1.0"
TIMEOUT = 25
CACHE_DIR = Path.home() / ".tokenprices"
CACHE_TTL = 12 * 3600  # seconds; prices change often, keep it fresh
USD_CNY = 7.1  # reference exchange rate for the domestic table

OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
LITELLM_URL = ("https://raw.githubusercontent.com/BerriAI/litellm/main/"
               "model_prices_and_context_window.json")

# Domestic / direct-official reference prices (CNY converted to USD at ~7.1).
# Sources & dates are kept per row so numbers are never anonymous.
DOMESTIC = [
    {"id": "deepseek/deepseek-v4-flash", "provider": "DeepSeek", "in": 0.30, "out": None,
     "ctx": None, "note": "peak input $0.30/M (official docs, 2026-10)",
     "src": "https://api-docs.deepseek.com/quick_start/pricing/"},
    {"id": "deepseek/deepseek-v4-pro", "provider": "DeepSeek", "in": 4.5 / USD_CNY, "out": 27.0 / USD_CNY,
     "ctx": None, "note": "off-peak in ¥4.5, peak out ¥27 (official docs, 2026-10)",
     "src": "https://api-docs.deepseek.com/quick_start/pricing/"},
    {"id": "zhipu/glm-5.3", "provider": "Zhipu", "in": 8.0 / USD_CNY, "out": 28.0 / USD_CNY,
     "ctx": 1_000_000, "note": "¥8 in / ¥28 out per M, 1M ctx (bigmodel.cn, 2026-09)",
     "src": "https://bigmodel.cn/pricing"},
    {"id": "zhipu/glm-5.3-flash", "provider": "Zhipu", "in": 0.8 / USD_CNY, "out": 1.4 / USD_CNY,
     "ctx": 1_000_000, "note": "standard ¥0.8 in / ¥1.4 out per M (bigmodel.cn, 2026-09)",
     "src": "https://bigmodel.cn/pricing"},
    {"id": "moonshot/kimi-k3", "provider": "Moonshot", "in": 3.00, "out": 15.00,
     "ctx": 1_048_576, "note": "cache-hit in $0.30/M (platform.kimi.com, 2026-09)",
     "src": "https://platform.kimi.com/docs/pricing/chat"},
    {"id": "openai/gpt-6.1-sol", "provider": "OpenAI", "in": 2.00, "out": 10.00,
     "ctx": None, "note": "list price per official launch (2026-10)",
     "src": "https://openai.com"},
    {"id": "google/gemini-3.8-flash", "provider": "Google", "in": 0.75, "out": 3.75,
     "ctx": None, "note": "promo price valid until 2026-12-31 (2026-10)",
     "src": "https://ai.google.dev"},
]

# --------------------------------------------------------------------------
# I18N
# --------------------------------------------------------------------------
I18N = {
    "en": {
        "model": "MODEL", "provider": "PROVIDER", "in": "INPUT $/1M", "out": "OUTPUT $/1M",
        "ctx": "CONTEXT", "src": "SOURCE", "note": "NOTE", "price": "PRICE",
        "free": "free", "n_a": "—",
        "models": "models", "updated": "updated", "cached": "cached",
        "cheapest_in": "Cheapest by input price", "cheapest_out": "Cheapest by output price",
        "cost_of": "Estimated cost", "compare": "Comparison", "vs": "vs",
        "no_match": "No models matched", "error": "Error", "done": "done",
        "hint": "Tip: try `tokenprices search <name>`, `tokenprices compare a b c`, "
                "`tokenprices cost --model X --in 1000 --out 500`, `tokenprices top 10`.",
    },
    "zh": {
        "model": "模型", "provider": "供应商", "in": "输入 $/百万", "out": "输出 $/百万",
        "ctx": "上下文", "src": "来源", "note": "备注", "price": "价格",
        "free": "免费", "n_a": "—",
        "models": "款模型", "updated": "更新于", "cached": "缓存",
        "cheapest_in": "按输入价格最便宜", "cheapest_out": "按输出价格最便宜",
        "cost_of": "预计成本", "compare": "对比", "vs": "对比",
        "no_match": "没有匹配的模型", "error": "错误", "done": "完成",
        "hint": "试试：`tokenprices search deepseek`、`tokenprices compare gpt-6.1-sol kimi-k3`、"
                "`tokenprices cost --model X --in 1000 --out 500`、`tokenprices top 10`。",
    },
}


def tr(key, lang):
    return I18N.get(lang, I18N["en"]).get(key, I18N["en"][key])


# --------------------------------------------------------------------------
# Fetching (keyless official sources)
# --------------------------------------------------------------------------
def http_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = r.read()
    for enc in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _num(v):
    """'0.0000003' or 3e-07 -> float; None/'' -> None; negative -> None
    (OpenRouter uses -1 for route-only models with no posted price)."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return None if v < 0 else float(v)
    s = str(v).strip()
    if not s or s.lower() in ("none", "null"):
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return None if f < 0 else f


def fetch_openrouter():
    """465+ models with live per-token USD pricing."""
    data = json.loads(http_text(OPENROUTER_URL))
    models = []
    for m in data.get("data", []):
        p = m.get("pricing") or {}
        inp = _num(p.get("prompt"))
        out = _num(p.get("completion"))
        if inp is None and out is None:
            continue
        tp = m.get("top_provider") or {}
        provider = (tp.get("provider") or str(m.get("id", "")).split("/")[0] or "unknown")
        models.append({
            "id": m.get("id", ""),
            "provider": str(provider),
            "in": None if inp is None else inp * 1e6,   # per token -> per 1M
            "out": None if out is None else out * 1e6,
            "ctx": m.get("context_length"),
            "src": "openrouter",
        })
    return models


def fetch_litellm():
    """LiteLLM pricing database (complementary source)."""
    data = json.loads(http_text(LITELLM_URL))
    models = []
    for mid, v in data.items():
        if not isinstance(v, dict):
            continue
        inp = _num(v.get("input_cost_per_token"))
        out = _num(v.get("output_cost_per_token"))
        if inp is None and out is None:
            continue
        models.append({
            "id": mid,
            "provider": str(v.get("litellm_provider") or mid.split("/")[0]),
            "in": None if inp is None else inp * 1e6,
            "out": None if out is None else out * 1e6,
            "ctx": v.get("max_tokens"),
            "src": "litellm",
        })
    return models


def load_models(source, refresh, lang):
    cache = CACHE_DIR / ("cache_" + source + ".json")
    if not refresh and cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if time.time() - data.get("fetched_at", 0) < CACHE_TTL:
                return data["models"], data["fetched_at"], True
        except Exception:
            pass
    if source == "openrouter":
        models = fetch_openrouter()
    elif source == "litellm":
        models = fetch_litellm()
    else:
        models = [{"id": d["id"], "provider": d["provider"], "in": d["in"], "out": d["out"],
                   "ctx": d["ctx"], "src": "domestic", "note": d["note"], "link": d["src"]}
                  for d in DOMESTIC]
        return models, None, False
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        cache.write_text(json.dumps({"fetched_at": time.time(), "models": models},
                                    ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return models, time.time(), False


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------
def fmt(v, lang):
    if v is None:
        return tr("n_a", lang)
    if v == 0:
        return tr("free", lang)
    if v >= 100:
        return "{0:,.0f}".format(v)
    if v >= 10:
        return "{0:,.2f}".format(v)
    return "{0:,.3f}".format(v)


def width(s, lang):
    """Display width (CJK chars count as 2)."""
    if lang == "zh":
        w = 0
        for ch in s:
            w += 2 if ord(ch) > 0x2E7F else 1
        return w
    return len(s)


def pad(s, w, lang):
    return s + " " * max(1, w - width(s, lang))


def print_table(rows, cols, lang):
    widths = {}
    for c in cols:
        widths[c] = max(width(tr(c, lang), lang),
                        max((width(str(r.get(c, "")), lang) for r in rows), default=1))
    header = "  " + "  ".join(pad(tr(c, lang), widths[c], lang) for c in cols)
    print(header)
    print("  " + "-" * (sum(widths.values()) + 2 * (len(cols) - 1)))
    for r in rows:
        line = "  " + "  ".join(pad(str(r.get(c, "")), widths[c], lang) for c in cols)
        print(line)


def print_hint(lang):
    print("\n  " + tr("hint", lang))


# --------------------------------------------------------------------------
# HTML report (dark, SVG, zero external dependencies)
# --------------------------------------------------------------------------
def _ctx(v):
    """Context length as short display string; truncate odd data."""
    if v is None:
        return ""
    s = "{0:,}".format(v) if isinstance(v, (int, float)) else str(v)
    return s[:14] + "…" if len(s) > 15 else s


def _esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def shorten(s, n):
    return s if len(s) <= n else s[: n - 1] + "…"


def render_html(models, title, lang, limit=24):
    """Price board + cost bar chart for the top-N models by input price."""
    t = I18N[lang]
    picked = [m for m in models if m.get("in") is not None or m.get("out") is not None]
    # a price board should show priced models: drop all-free models
    picked = [m for m in picked if not (m.get("in") == 0 and m.get("out") == 0)]
    picked.sort(key=lambda m: (m.get("in") if m.get("in") is not None else 1e12))
    picked = picked[:limit]
    max_p = max([m.get("in") or 0 for m in picked] + [m.get("out") or 0 for m in picked] + [1])
    w, bar_h, gap = 640, 26, 12
    h = len(picked) * (bar_h + gap) + 12
    id_w, bar_w = 300, w - 300 - 140
    parts = []
    for i, m in enumerate(picked):
        y = 10 + i * (bar_h + gap)
        inp = m.get("in")
        outp = m.get("out")
        parts.append('<text x="{0}" y="{1}" class="nm" text-anchor="end">{2}</text>'
                     .format(292, y + 10, _esc(shorten(m["id"], 34))))
        # input bar
        bw = int(bar_w * (inp or 0) / max_p)
        parts.append('<text x="{0}" y="{1}" class="ax">IN</text>'.format(300, y + 10))
        parts.append('<rect x="{0}" y="{1}" width="{2}" height="9" rx="4" class="bar-in"/>'
                     .format(326, y + 2, max(2, bw)))
        parts.append('<text x="{0}" y="{1}" class="val">{2}</text>'
                     .format(326 + max(2, bw) + 6, y + 10, fmt(inp, lang)))
        # output bar
        bw2 = int(bar_w * (outp or 0) / max_p)
        parts.append('<text x="{0}" y="{1}" class="ax">OUT</text>'.format(300, y + bar_h - 2))
        parts.append('<rect x="{0}" y="{1}" width="{2}" height="9" rx="4" class="bar-out"/>'
                     .format(326, y + bar_h - 8, max(2, bw2)))
        parts.append('<text x="{0}" y="{1}" class="val">{2}</text>'
                     .format(326 + max(2, bw2) + 6, y + bar_h - 2, fmt(outp, lang)))
    rows = "".join(
        "<tr><td class='id'>{0}</td><td>{1}</td><td class='mono'>{2}</td><td class='mono'>{3}</td>"
        "<td class='mono'>{4}</td></tr>".format(
            _esc(m["id"]), _esc(str(m.get("provider", ""))),
            fmt(m.get("in"), lang), fmt(m.get("out"), lang),
            (("{0:,}".format(m["ctx"])) if m.get("ctx") else tr("n_a", lang)))
        for m in picked)
    return """<!DOCTYPE html>
<html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ --bg:#0b0e1a; --card:#131731; --line:#222945; --tx:#e6e9f5; --mut:#7c83a6; --acc:#8b5cf6; }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font:13.5px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif; background:var(--bg); color:var(--tx); padding:28px 16px; }}
  .wrap {{ max-width:1020px; margin:auto; }}
  h1 {{ font-size:24px; letter-spacing:-.3px; }}
  .sub {{ color:var(--mut); margin:4px 0 20px; font-size:13px; }}
  .grid {{ display:grid; grid-template-columns:1.05fr .95fr; gap:14px; }}
  @media (max-width:820px) {{ .grid {{ grid-template-columns:1fr; }} }}
  .panel {{ background:var(--card); border:1px solid var(--line); border-radius:16px; padding:18px; }}
  .panel h2 {{ font-size:14px; margin-bottom:10px; }}
  svg text {{ fill:var(--tx); font-size:11px; }}
  svg text.nm {{ font-size:11.5px; }}
  svg text.val {{ font-weight:700; fill:var(--acc); font-size:10.5px; }}
  svg text.ax {{ font-size:8.5px; fill:var(--mut); }}
  svg .bar-in {{ fill:url(#gin); }}
  svg .bar-out {{ fill:url(#gout); }}
  table {{ width:100%; min-width:560px; border-collapse:collapse; margin-top:10px; font-size:12px; }}
  th {{ text-align:left; color:var(--mut); font-size:10px; text-transform:uppercase; letter-spacing:.06em; padding:5px 8px; border-bottom:1px solid var(--line); }}
  td {{ padding:5px 8px; border-bottom:1px solid #1a2040; }}
  td.id {{ font-family:ui-monospace,Consolas,monospace; font-size:11px; max-width:240px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  td.mono {{ font-variant-numeric:tabular-nums; }}
  .foot {{ color:var(--mut); font-size:11px; margin-top:16px; }}
  a {{ color:var(--acc); }}
</style></head>
<body><div class="wrap">
<svg width="0" height="0"><defs>
<linearGradient id="gin" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#8b5cf6"/><stop offset="1" stop-color="#6366f1"/></linearGradient>
<linearGradient id="gout" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#f59e0b"/><stop offset="1" stop-color="#ef4444"/></linearGradient>
</defs></svg>
<h1>⚡ {title}</h1>
<div class="sub">{updated} · {count} {models} · <a href="https://github.com/Koiliblly/TokenPrices">TokenPrices</a></div>
<div class="grid">
  <div class="panel"><h2>📊 {compare}</h2>{bars}</div>
  <div class="panel" style="max-height:640px; overflow:auto"><h2>📋 {model}</h2>{table_html}</div>
</div>
<div class="foot">{hint}</div>
</div></body></html>""".format(
        lang=lang, title=_esc(title), updated=tr("updated", lang), count=len(picked),
        models=tr("models", lang), compare=tr("compare", lang), model=tr("model", lang),
        bars='<svg viewBox="0 0 {0} {1}" xmlns="http://www.w3.org/2000/svg">{2}</svg>'.format(w, h, "".join(parts)),
        table_html="<table><tr><th>{0}</th><th>{1}</th><th>{2}</th><th>{3}</th><th>{4}</th></tr>{5}</table>"
            .format(tr("model", lang), tr("provider", lang), tr("in", lang),
                    tr("out", lang), tr("ctx", lang), rows),
        hint=_esc(tr("hint", lang)))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def resolve(models, query):
    """Exact id first, then fuzzy: match any model whose id or provider
    contains the query; if still empty, try provider name."""
    q = query.lower()
    idx = {m["id"].lower(): m for m in models}
    if q in idx:
        return [idx[q]]
    hits = [m for m in models if q in m["id"].lower()]
    if hits:
        return hits
    hits = [m for m in models if q in str(m.get("provider", "")).lower()]
    return hits


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="tokenprices",
        description="Live LLM API price board: 400+ models, compare, cost calculator, HTML report. EN / 中文.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("mode", nargs="?", default="list",
                    choices=["list", "search", "compare", "cost", "top"],
                    help="command: list | search | compare | cost | top")
    ap.add_argument("query", nargs="*", help="model name / list of model ids")
    ap.add_argument("--source", choices=["openrouter", "litellm", "domestic"], default="openrouter",
                    help="data source (domestic = direct official reference prices)")
    ap.add_argument("--lang", choices=["en", "zh"], default="en")
    ap.add_argument("--refresh", action="store_true", help="ignore cache and refetch")
    ap.add_argument("--offline", action="store_true", help="use cache only")
    ap.add_argument("--limit", type=int, default=20, help="rows to show (list/search/top)")
    ap.add_argument("--free", action="store_true", help="only free models (pricing = 0)")
    ap.add_argument("--in", dest="in_tokens", type=int, default=1000, help="input tokens for cost")
    ap.add_argument("--out", dest="out_tokens", type=int, default=500, help="output tokens for cost")
    ap.add_argument("--calls", type=int, default=1, help="number of calls for cost")
    ap.add_argument("--json", metavar="FILE", help="export matched models as JSON")
    ap.add_argument("--html", metavar="FILE", help="render a share-ready HTML price report")
    args = ap.parse_args(argv)
    lang = args.lang
    t = I18N[lang]

    try:
        if args.offline:
            cache = CACHE_DIR / ("cache_" + args.source + ".json")
            if not cache.exists():
                print("{0}: no cache for source '{1}'".format(t["error"], args.source), file=sys.stderr)
                return 1
            models = json.loads(cache.read_text(encoding="utf-8"))["models"]
            fetched_at, cached = None, True
        else:
            models, fetched_at, cached = load_models(args.source, args.refresh, lang)

        q = " ".join(args.query).lower() if args.query else ""
        mode = args.mode

        if mode == "search":
            if not q and not args.free:
                ap.error("search needs a query, e.g. `tokenprices search deepseek` (or --free)")
            matched = models
            if q:
                matched = [m for m in matched if q in m["id"].lower() or q in m.get("provider", "").lower()]
            if args.free:
                matched = [m for m in matched
                           if (m.get("in") is not None and m["in"] == 0)
                           or (m.get("out") is not None and m["out"] == 0)]
            rows = [{"model": m["id"], "provider": m.get("provider", ""), "in": fmt(m.get("in"), lang),
                     "out": fmt(m.get("out"), lang), "ctx": _ctx(m.get("ctx"))} for m in matched[:args.limit]]
            if not rows:
                print(t["no_match"] + ": " + (q or "--free"))
            else:
                print_table(rows, ["model", "provider", "in", "out", "ctx"], lang)
        elif mode == "compare":
            if len(args.query) < 1:
                ap.error("compare needs at least one model id")
            picked = []
            missing = []
            for i in args.query:
                hits = resolve(models, i)
                if hits:
                    picked.append(hits[0])
                else:
                    missing.append(i)
            if missing:
                print("{0}: {1}".format(t["no_match"], ", ".join(missing)), file=sys.stderr)
            rows = [{"model": m["id"], "provider": m.get("provider", ""),
                     "in": fmt(m.get("in"), lang), "out": fmt(m.get("out"), lang),
                     "ctx": _ctx(m.get("ctx"))} for m in picked]
            print_table(rows, ["model", "provider", "in", "out", "ctx"], lang)
            if len(picked) >= 2:
                ins = [m for m in picked if m.get("in") is not None]
                if ins:
                    cheapest = min(ins, key=lambda m: m["in"])
                    print("\n  {0}: {1} ({2} $/1M)".format(t["cheapest_in"], cheapest["id"],
                                                           fmt(cheapest["in"], lang)))
        elif mode == "cost":
            if len(args.query) < 1:
                ap.error("cost needs a model id, e.g. `tokenprices cost gpt-4o --in 1000 --out 500`")
            hits = resolve(models, args.query[0])
            if not hits:
                print("{0}: {1}".format(t["no_match"], args.query[0]), file=sys.stderr)
                return 1
            m = hits[0]
            mid = m["id"]
            inp, outp = m.get("in"), m.get("out")
            cost = (inp or 0) * args.in_tokens / 1e6 + (outp or 0) * args.out_tokens / 1e6
            total = cost * args.calls
            rows = [{"model": mid, "provider": m.get("provider", ""),
                     "in": "{0:,}".format(args.in_tokens), "out": "{0:,}".format(args.out_tokens),
                     "price": "${0}/1M".format(fmt(inp, lang)) if inp is not None else tr("n_a", lang)}]
            print_table(rows, ["model", "provider", "in", "out", "price"], lang)
            print("\n  {0} (1 call): ${1:.6f}".format(t["cost_of"], cost))
            if args.calls > 1:
                print("  {0} x {1} calls: ${2:.6f}".format(t["cost_of"], args.calls, total))
        elif mode == "top":
            n = int(args.query[0]) if args.query and args.query[0].isdigit() else args.limit
            known = [m for m in models if m.get("in") is not None]
            known.sort(key=lambda m: m["in"])
            rows = [{"model": m["id"], "provider": m.get("provider", ""),
                     "in": fmt(m.get("in"), lang), "out": fmt(m.get("out"), lang),
                     "ctx": _ctx(m.get("ctx"))} for m in known[:n]]
            print(t["cheapest_in"] + ":")
            print_table(rows, ["model", "provider", "in", "out", "ctx"], lang)
        else:  # list
            if q:
                matched = [m for m in models if q in m["id"].lower() or q in m.get("provider", "").lower()]
            else:
                matched = models
            rows = [{"model": m["id"], "provider": m.get("provider", ""), "in": fmt(m.get("in"), lang),
                     "out": fmt(m.get("out"), lang), "ctx": _ctx(m.get("ctx"))}
                    for m in matched[:args.limit]]
            print_table(rows, ["model", "provider", "in", "out", "ctx"], lang)

        if fetched_at:
            print("\n  {0}: {1} · {2}: {3}".format(tr("updated", lang),
                  time.strftime("%Y-%m-%d %H:%M", time.localtime(fetched_at)),
                  tr("cached", lang), "yes" if cached else "no"))
        print_hint(lang)

        if args.json:
            Path(args.json).write_text(json.dumps(
                [{"id": m["id"], "provider": m.get("provider", ""), "input_usd_per_1m": m.get("in"),
                  "output_usd_per_1m": m.get("out"), "context": m.get("ctx")}
                 for m in models[:args.limit]], ensure_ascii=False, indent=2), encoding="utf-8")
            print("\n{0}: {1}".format(t["done"], args.json))
        if args.html:
            title = "Token Prices" if lang == "en" else "大模型 API 价格速览"
            html = render_html(models, title, lang, limit=args.limit)
            Path(args.html).write_text(html, encoding="utf-8")
            print("{0}: {1}".format(t["done"], args.html))
            try:
                import webbrowser
                webbrowser.open(Path(args.html).resolve().as_uri())
            except Exception:
                pass
        return 0
    except Exception as e:
        print("{0}: {1}".format(t["error"], e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
