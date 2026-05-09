#!/usr/bin/env python3
"""Generate Eason's daily crypto brief as static HTML.

No paid API required. It uses public RSS/pages + light rule-based grouping so it can
run inside GitHub Actions with the built-in GITHUB_TOKEN.
"""
from __future__ import annotations

import html
import json
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Asia/Shanghai")
TODAY = datetime.now(TZ).date()
DATE_SLUG = TODAY.isoformat()
DATE_CN = f"{TODAY.year}/{TODAY.month}/{TODAY.day}"

USER_AGENT = "Mozilla/5.0 (compatible; EasonDailyBrief/1.0; +https://github.com/JoedomZhang7/crypto-daily)"

RSS_SOURCES = [
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Decrypt", "https://decrypt.co/feed"),
]
ODAILY_URL = "https://www.odaily.news/zh-CN/newsflash"
COINGECKO_URL = (
    "https://api.coingecko.com/api/v3/simple/price"
    "?ids=bitcoin,ethereum,solana,xrp,near-protocol,binancecoin"
    "&vs_currencies=usd&include_24hr_change=true"
)

GROUPS = [
    ("top", "0. 今日最值得关注", []),
    ("opportunity", "1. 🔬 可关注机会", ["launch", "airdrop", "hackathon", "funding", "mainnet", "staking", "yield", "空投", "上线", "融资", "主网", "质押"]),
    ("stablecoin", "2. 🏦 稳定币 / 支付 / RWA", ["stablecoin", "usdt", "usdc", "tether", "circle", "rwa", "tokenization", "payment", "reserve", "稳定币", "支付", "代币化", "国债"]),
    ("policy", "3. ⚖️ 监管 / 政策", ["sec", "cftc", "senate", "regulation", "law", "bill", "clarity", "mifid", "mica", "监管", "法案", "参议院", "证监", "合规"]),
    ("market", "4. 📈 市场 / ETF / 机构", ["bitcoin", "btc", "ether", "ethereum", "etf", "price", "market", "coinbase", "fund", "inflow", "outflow", "比特币", "以太坊", "现货", "净流入", "净流出", "机构"]),
    ("onchain", "5. 🐳 链上 / 巨鲸 / 聪明钱", ["whale", "onchain", "wallet", "address", "liquidation", "巨鲸", "地址", "链上", "清算", "聪明钱"]),
    ("defi", "6. 🧩 DeFi / 项目动态", ["defi", "aave", "uniswap", "maker", "dao", "protocol", "layer", "solana", "base", "arbitrum", "项目", "协议"]),
    ("risk", "7. ⚠️ 风险提示 / 安全事件", ["hack", "exploit", "freeze", "security", "outage", "scam", "risk", "vulnerability", "黑客", "攻击", "冻结", "宕机", "风险", "漏洞"]),
    ("macro", "8. 🌍 宏观 / 其他", ["fed", "inflation", "jobs", "treasury", "dollar", "oil", "gold", "macro", "就业", "通胀", "美联储", "美元", "黄金", "原油"]),
]

@dataclass
class Item:
    title: str
    url: str
    source: str
    published: datetime | None = None
    summary: str = ""


def fetch(url: str, timeout: int = 18) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data.decode("utf-8", errors="replace")


def clean_text(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def parse_rss(source: str, url: str) -> list[Item]:
    try:
        text = fetch(url)
        root = ET.fromstring(text)
    except Exception as e:
        print(f"WARN: failed RSS {source}: {e}", file=sys.stderr)
        return []

    items: list[Item] = []
    for node in root.findall(".//item")[:40]:
        title = clean_text(node.findtext("title"))
        link = clean_text(node.findtext("link"))
        desc = clean_text(node.findtext("description"))
        pub = node.findtext("pubDate") or node.findtext("published")
        dt = None
        if pub:
            try:
                dt = parsedate_to_datetime(pub)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except Exception:
                dt = None
        if title and link:
            items.append(Item(title=title, url=link, source=source, published=dt, summary=desc))
    return items


def parse_odaily() -> list[Item]:
    try:
        text = fetch(ODAILY_URL)
    except Exception as e:
        print(f"WARN: failed Odaily: {e}", file=sys.stderr)
        return []
    # Good enough for Odaily newsflash SSR snippets.
    found = re.findall(r'\[([^\]\n]{8,120})\]\((/zh-CN/newsflash/\d+)\)([^\n]{0,220})', text)
    items = []
    for title, path, tail in found[:35]:
        title = clean_text(title)
        if title in {"星球早讯"}:
            continue
        items.append(Item(title=title, url="https://www.odaily.news" + path, source="Odaily", summary=clean_text(tail)))
    return items


def recent_filter(items: list[Item]) -> list[Item]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=36)
    out = []
    for it in items:
        if it.published is None or it.published.astimezone(timezone.utc) >= cutoff:
            out.append(it)
    return out


def dedupe(items: list[Item]) -> list[Item]:
    seen = set()
    out = []
    for it in items:
        key = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", it.title.lower())[:80]
        if key and key not in seen:
            seen.add(key)
            out.append(it)
    return out


def score_item(it: Item) -> int:
    t = (it.title + " " + it.summary).lower()
    score = 0
    for word in ["bitcoin", "btc", "ethereum", "eth", "etf", "stablecoin", "sec", "coinbase", "tether", "solana", "hack", "exploit", "regulation", "比特币", "以太坊", "稳定币", "现货", "监管", "巨鲸"]:
        if word in t:
            score += 2
    if any(x in t for x in ["million", "billion", "亿美元", "万美元"]):
        score += 1
    if it.source in {"CoinDesk", "Cointelegraph", "Odaily"}:
        score += 1
    return score


def classify(items: list[Item]) -> dict[str, list[Item]]:
    buckets = {gid: [] for gid, _, _ in GROUPS}
    top = sorted(items, key=score_item, reverse=True)[:3]
    buckets["top"] = top
    used = set(id(x) for x in top)
    for it in items:
        if id(it) in used:
            continue
        text = (it.title + " " + it.summary).lower()
        placed = False
        for gid, _, keywords in GROUPS[1:]:
            if any(k.lower() in text for k in keywords):
                if len(buckets[gid]) < 5:
                    buckets[gid].append(it)
                    placed = True
                    break
        if not placed and len(buckets["macro"]) < 5:
            buckets["macro"].append(it)
    return buckets


def get_prices() -> dict:
    try:
        return json.loads(fetch(COINGECKO_URL, timeout=12))
    except Exception as e:
        print(f"WARN: failed prices: {e}", file=sys.stderr)
        return {}


def price_cards(prices: dict) -> str:
    names = [("bitcoin", "BTC"), ("ethereum", "ETH"), ("solana", "SOL"), ("near-protocol", "NEAR")]
    cards = []
    for key, sym in names:
        p = prices.get(key) or {}
        usd = p.get("usd")
        chg = p.get("usd_24h_change")
        if usd is None:
            continue
        cls = "up" if (chg or 0) >= 0 else "down"
        cards.append(f'<div class="card"><div class="sym">{sym}</div><div class="num">${usd:,.4g}</div><div class="{cls}">{chg:+.1f}% 24h</div></div>')
    return '<div class="price">' + "\n".join(cards) + '</div>' if cards else ""


def render_items(items: list[Item]) -> str:
    if not items:
        return "<p class=\"empty\">暂无高置信条目。</p>"
    lis = []
    for it in items:
        title = html.escape(it.title)
        summary = html.escape(it.summary[:180])
        source = html.escape(it.source)
        url = html.escape(it.url, quote=True)
        extra = f" — {summary}" if summary and summary.lower() not in it.title.lower() else ""
        lis.append(f'<li>{title}{extra}<span class="source">来源：<a href="{url}">{source}</a></span></li>')
    return "<ul>\n" + "\n".join(lis) + "\n</ul>"


def render_html(buckets: dict[str, list[Item]], prices: dict) -> str:
    nav = "\n".join(f'<a href="#{gid}">{title}</a>' for gid, title, _ in GROUPS)
    sections = []
    for gid, title, _ in GROUPS:
        extra = price_cards(prices) if gid == "market" else ""
        klass = "highlight" if gid in {"top"} else ""
        sections.append(f'<section id="{gid}" class="{klass}"><h2>{html.escape(title)}</h2>{extra}{render_items(buckets.get(gid, []))}</section>')
    sections.append('''<section id="signals" class="highlight"><h2>9. 今日交易 / 叙事信号</h2><ul>
<li class="signal">优先跟踪稳定币监管、ETF 资金流与交易所基础设施风险。</li>
<li class="signal">若 SOL/ETH ETF 与链上资金流同步走强，说明风险偏好可能继续修复。</li>
<li class="signal">宏观数据、美元流动性与地缘风险仍是 BTC 短线波动的主因。</li>
</ul></section>''')
    return f'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Eason's 每日早报 · {DATE_CN}</title>
  <style>
    :root{{--bg:#0b1020;--panel:rgba(255,255,255,.075);--panel-2:rgba(255,255,255,.105);--text:#eef3ff;--muted:#aab6d3;--line:rgba(255,255,255,.14);--accent:#7dd3fc;--good:#34d399;--bad:#fb7185;--shadow:0 22px 70px rgba(0,0,0,.35)}}
    *{{box-sizing:border-box}}body{{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;color:var(--text);background:radial-gradient(circle at 15% 10%,rgba(125,211,252,.22),transparent 28rem),radial-gradient(circle at 85% 8%,rgba(192,132,252,.18),transparent 26rem),var(--bg);line-height:1.65}}a{{color:var(--accent);text-decoration:none}}a:hover{{text-decoration:underline}}.wrap{{width:min(1060px,calc(100% - 32px));margin:0 auto}}header{{padding:56px 0 30px}}.badge{{display:inline-flex;color:#dbeafe;background:rgba(125,211,252,.12);border:1px solid rgba(125,211,252,.24);padding:6px 12px;border-radius:999px;font-size:13px}}h1{{font-size:clamp(34px,6vw,64px);line-height:1.04;margin:18px 0 14px;letter-spacing:-.04em}}.subtitle{{color:var(--muted);max-width:760px;font-size:17px}}.meta{{display:flex;flex-wrap:wrap;gap:10px;margin-top:22px;color:var(--muted);font-size:14px}}.pill{{border:1px solid var(--line);background:rgba(255,255,255,.06);padding:6px 10px;border-radius:999px}}.grid{{display:grid;grid-template-columns:280px 1fr;gap:22px;align-items:start;padding-bottom:60px}}nav{{position:sticky;top:18px;background:var(--panel);border:1px solid var(--line);border-radius:22px;padding:18px;box-shadow:var(--shadow);backdrop-filter:blur(18px)}}nav .toc-title{{font-weight:700;margin-bottom:10px}}nav a{{display:block;padding:8px 10px;border-radius:12px;color:var(--muted);font-size:14px}}nav a:hover{{background:rgba(255,255,255,.08);color:var(--text);text-decoration:none}}main{{display:grid;gap:18px}}section{{background:var(--panel);border:1px solid var(--line);border-radius:26px;padding:24px;box-shadow:var(--shadow);backdrop-filter:blur(18px)}}h2{{margin:0 0 14px;font-size:23px;letter-spacing:-.02em}}ul{{margin:0;padding-left:20px}}li{{margin:0 0 14px}}li:last-child{{margin-bottom:0}}.source{{display:block;margin-top:3px;font-size:13px;color:var(--muted)}}.highlight{{background:linear-gradient(135deg,rgba(125,211,252,.14),rgba(192,132,252,.12));border-color:rgba(125,211,252,.28)}}.signal{{border-left:4px solid var(--good);padding-left:16px}}.disclaimer{{color:var(--muted);font-size:13px;text-align:center;padding:28px 0 44px}}.price{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:12px 0 18px}}.card{{background:var(--panel-2);border:1px solid var(--line);border-radius:16px;padding:14px}}.sym{{color:var(--muted);font-size:13px}}.num{{font-size:22px;font-weight:800;margin-top:4px}}.up{{color:var(--good);font-size:13px}}.down{{color:var(--bad);font-size:13px}}.empty{{color:var(--muted)}}@media(max-width:820px){{.grid{{grid-template-columns:1fr}}nav{{position:relative;top:auto}}.price{{grid-template-columns:repeat(2,1fr)}}section{{padding:20px;border-radius:22px}}}}
  </style>
</head>
<body>
  <header class="wrap"><div class="badge">🧪 Auto Brief · Crypto Daily</div><h1>Eason's 每日早报</h1><p class="subtitle">自动生成的 Crypto 新闻聚合：稳定币、ETF/机构资金、链上巨鲸、DeFi、监管与宏观信号。</p><div class="meta"><span class="pill">日期：{DATE_CN}</span><span class="pill">自动生成</span><span class="pill">Asia/Shanghai</span></div></header>
  <div class="wrap grid"><nav><div class="toc-title">目录</div>{nav}<a href="#signals">9. 今日信号</a></nav><main>{''.join(sections)}</main></div>
  <footer class="wrap disclaimer">免责声明：以上为公开新闻聚合与信息整理，不构成投资建议。来源链接均保留用于核查。</footer>
</body></html>'''


def update_index() -> None:
    daily = sorted((ROOT / "daily").glob("*.html"), reverse=True)
    items = []
    for p in daily[:120]:
        slug = p.stem
        try:
            d = datetime.strptime(slug, "%Y-%m-%d").date()
            label = f"{d.year}/{d.month}/{d.day}"
        except Exception:
            label = slug
        items.append(f'<a class="item" href="./daily/{p.name}">{label} · 每日早报</a>')
    index = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" /><title>Eason's 每日早报</title><style>body{{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;background:#0b1020;color:#eef3ff;line-height:1.7}}.wrap{{width:min(860px,calc(100% - 32px));margin:0 auto;padding:56px 0}}.card{{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.14);border-radius:24px;padding:24px;margin-top:22px;box-shadow:0 22px 70px rgba(0,0,0,.35)}}h1{{font-size:clamp(36px,6vw,64px);line-height:1.05;margin:0 0 12px;letter-spacing:-.04em}}p{{color:#aab6d3}}.item{{display:block;padding:16px 0;border-top:1px solid rgba(255,255,255,.12);color:#7dd3fc;text-decoration:none}}.item:first-child{{border-top:0}}.item:hover{{text-decoration:underline}}.tag{{display:inline-block;border:1px solid rgba(125,211,252,.25);background:rgba(125,211,252,.12);border-radius:999px;padding:5px 10px;color:#dbeafe;font-size:13px}}</style></head><body><main class="wrap"><span class="tag">Crypto Daily Archive</span><h1>Eason's 每日早报</h1><p>每日 Crypto 新闻聚合、稳定币/ETF/链上/监管信号整理。</p><section class="card"><h2>日报归档</h2>{''.join(items)}</section></main></body></html>'''
    (ROOT / "index.html").write_text(index, encoding="utf-8")


def main() -> None:
    items: list[Item] = []
    for source, url in RSS_SOURCES:
        items.extend(parse_rss(source, url))
        time.sleep(0.5)
    items.extend(parse_odaily())
    items = dedupe(recent_filter(items))
    items = sorted(items, key=score_item, reverse=True)[:60]
    buckets = classify(items)
    prices = get_prices()
    outdir = ROOT / "daily"
    outdir.mkdir(exist_ok=True)
    (outdir / f"{DATE_SLUG}.html").write_text(render_html(buckets, prices), encoding="utf-8")
    update_index()
    print(f"Generated daily/{DATE_SLUG}.html with {len(items)} source items")

if __name__ == "__main__":
    main()
