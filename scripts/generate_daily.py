#!/usr/bin/env python3
"""Generate Eason's daily crypto brief as static HTML.

No paid API required. It uses public RSS/pages + light rule-based grouping so it can
run inside GitHub Actions with the built-in GITHUB_TOKEN.

The 6551 public API is used as an enhancement layer when available: it contributes
hot-score, grade, signal, coin tags, and social/X items. If 6551 is unavailable, the
brief still falls back to the base public sources.
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
API_6551_BASE = "https://ai.6551.io"
API_6551_HOT_URLS = [
    ("6551 Web3", f"{API_6551_BASE}/open/free_hot?category=web3"),
    ("6551 DeFi", f"{API_6551_BASE}/open/free_hot?category=web3&subcategory=defi"),
    ("6551 Regulation", f"{API_6551_BASE}/open/free_hot?category=web3&subcategory=regulation"),
]
CRYPTO_COIN_HINTS = {"BTC", "ETH", "SOL", "BNB", "XRP", "SUI", "TON", "AAVE", "UNI", "LINK", "USDT", "USDC", "DOGE", "PEPE", "ARB", "OP", "BASE", "NEAR", "DOT", "PROS"}
NON_CRYPTO_COIN_HINTS = {"CL", "XYZ-CL", "GC", "SI"}

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
    ("social", "9. 🐦 X / 社交热点", []),
]

@dataclass
class Item:
    title: str
    url: str
    source: str
    published: datetime | None = None
    summary: str = ""
    score: int | None = None
    grade: str = ""
    signal: str = ""
    coins: tuple[str, ...] = ()
    kind: str = "news"
    author: str = ""
    metrics: dict | None = None


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


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def normalize_signal(signal: str) -> str:
    s = (signal or "").lower().strip()
    if s in {"long", "bullish", "positive"}:
        return "bullish"
    if s in {"short", "bearish", "negative"}:
        return "bearish"
    if s in {"neutral", "mixed"}:
        return "neutral"
    return s


def is_crypto_relevant(title: str, coins: tuple[str, ...]) -> bool:
    text = title.lower()
    if has_crypto_keyword(title):
        return True
    if any(c in CRYPTO_COIN_HINTS for c in coins):
        return True
    if coins and not all(c in NON_CRYPTO_COIN_HINTS for c in coins):
        return True
    return False


def has_crypto_keyword(title: str) -> bool:
    text = re.sub(r"https?://\S+", " ", title.lower())
    short_ticker_hit = bool(re.search(r"(^|[^a-z0-9])(btc|eth|sui|ton)([^a-z0-9]|$)", text))
    return short_ticker_hit or any(k in text for k in [
        "bitcoin", "ethereum", "crypto", "web3", "defi", "stablecoin",
        "blockchain", "onchain", "solana", "airdrop", "polkadot", "chainlink",
        "bitcoin etf", "ethereum etf", "crypto etf", "spot etf",
        "加密", "比特币", "以太坊", "稳定币", "链上", "代币", "空投", "现货etf",
    ])


def has_coin_mentioned(title: str, coins: tuple[str, ...]) -> bool:
    text = re.sub(r"https?://\S+", " ", title.lower())
    for coin in coins:
        if coin in {"BTC", "ETH", "SOL"}:
            continue
        c = coin.lower()
        if coin == "LINK" and "chainlink" not in text and "$link" not in text:
            continue
        if coin in CRYPTO_COIN_HINTS and (f"${c}" in text or re.search(rf"(^|[^a-z0-9]){re.escape(c)}([^a-z0-9]|$)", text)):
            return True
    return False


def is_crypto_native_source(source: str) -> bool:
    return source.lower() in {
        "coindesk", "cointelegraph", "decrypt", "the block", "6551news", "bwenews",
        "upbit", "binance", "okx", "bybit", "kucoin", "twitter", "x",
    }


def parse_6551_hot() -> tuple[list[Item], list[Item]]:
    """Fetch 6551 hot Web3 intelligence.

    Returns (news_items, social_items). The API occasionally returns 503 while
    regenerating; in that case we warn and keep the base brief working.
    """
    news_items: list[Item] = []
    social_items: list[Item] = []
    seen_ids: set[str] = set()
    for source_name, url in API_6551_HOT_URLS:
        try:
            payload = json.loads(fetch(url, timeout=18))
        except Exception as e:
            print(f"WARN: failed 6551 {source_name}: {e}", file=sys.stderr)
            continue
        if not payload.get("success", False):
            print(f"WARN: 6551 {source_name} unavailable: {payload.get('message') or payload.get('error')}", file=sys.stderr)
            continue

        for raw in (payload.get("news") or {}).get("items", [])[:30]:
            item_id = f"news:{raw.get('id') or raw.get('link') or raw.get('title')}"
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            title = clean_text(str(raw.get("title") or ""))
            if not title:
                continue
            source = str(raw.get("source") or source_name)
            coins = tuple(str(c).upper() for c in (raw.get("coins") or []) if c)
            item = Item(
                title=title,
                url=str(raw.get("link") or ""),
                source=source,
                published=parse_iso_datetime(raw.get("published_at") or raw.get("created_at")),
                summary=clean_text(str(raw.get("summary_zh") or raw.get("summary_en") or "")),
                score=int(raw.get("score") or 0) if str(raw.get("score") or "").isdigit() else None,
                grade=str(raw.get("grade") or ""),
                signal=normalize_signal(str(raw.get("signal") or "")),
                coins=coins,
                kind="6551-news",
            )
            is_twitter_like = source.lower() in {"twitter", "x"} or "twitter.com" in item.url or "x.com" in item.url
            if is_twitter_like:
                item.kind = "6551-social"
                if has_crypto_keyword(title) or has_coin_mentioned(title, coins):
                    social_items.append(item)
            elif is_crypto_relevant(title, coins) and (is_crypto_native_source(source) or has_crypto_keyword(title) or has_coin_mentioned(title, coins)):
                news_items.append(item)

        for raw in (payload.get("tweets") or {}).get("items", [])[:20]:
            item_id = f"tweet:{raw.get('url') or raw.get('id') or raw.get('content')}"
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            content = clean_text(str(raw.get("content") or raw.get("title") or ""))
            if not content:
                continue
            author = clean_text(str(raw.get("author") or raw.get("handle") or ""))
            coins = tuple(str(c).upper() for c in (raw.get("coins") or []) if c)
            item = Item(
                title=content,
                url=str(raw.get("url") or raw.get("link") or ""),
                source="X/Twitter",
                published=parse_iso_datetime(raw.get("posted_at") or raw.get("published_at") or raw.get("created_at")),
                summary=clean_text(str(raw.get("relevance") or raw.get("summary_zh") or raw.get("summary_en") or "")),
                score=int(raw.get("score") or raw.get("relevance_score") or 0) if str(raw.get("score") or raw.get("relevance_score") or "").isdigit() else None,
                grade=str(raw.get("grade") or ""),
                signal=normalize_signal(str(raw.get("signal") or "")),
                coins=coins,
                kind="6551-social",
                author=author,
                metrics=raw.get("metrics") if isinstance(raw.get("metrics"), dict) else None,
            )
            if has_crypto_keyword(content) or has_coin_mentioned(content, coins):
                social_items.append(item)
        time.sleep(0.4)
    return news_items, social_items


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
    score = int((it.score or 0) / 10)
    for word in ["bitcoin", "btc", "ethereum", "eth", "etf", "stablecoin", "sec", "coinbase", "tether", "solana", "hack", "exploit", "regulation", "比特币", "以太坊", "稳定币", "现货", "监管", "巨鲸"]:
        if word in t:
            score += 2
    if any(x in t for x in ["million", "billion", "亿美元", "万美元"]):
        score += 1
    if it.source in {"CoinDesk", "Cointelegraph", "Odaily"}:
        score += 1
    if it.kind.startswith("6551"):
        score += 3
    if it.grade.startswith("A"):
        score += 2
    if it.coins:
        score += min(3, len(it.coins))
    return score


def classify(items: list[Item], hot6551: list[Item] | None = None, social: list[Item] | None = None) -> dict[str, list[Item]]:
    buckets = {gid: [] for gid, _, _ in GROUPS}
    hot6551 = hot6551 or []
    social = social or []
    buckets["social"] = sorted(social, key=score_item, reverse=True)[:6]
    top_pool = items + hot6551
    top = sorted(top_pool, key=score_item, reverse=True)[:3]
    buckets["top"] = top
    used = set(id(x) for x in top)
    # 6551 is an intelligence layer, not a standalone section: its high-score
    # items are mixed into the normal topical buckets and surfaced by badges.
    for it in sorted(top_pool, key=score_item, reverse=True):
        if id(it) in used:
            continue
        text = (it.title + " " + it.summary).lower()
        placed = False
        for gid, _, keywords in GROUPS[1:]:
            if gid == "social":
                continue
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
        title_prefix = f"{it.author}：" if it.author else ""
        raw_title = title_prefix + it.title
        if len(raw_title) > 420:
            raw_title = raw_title[:420].rstrip() + "…"
        title = html.escape(raw_title)
        summary = html.escape(it.summary[:180])
        source = html.escape(it.source)
        url = html.escape(it.url or "#", quote=True)
        extra = f" — {summary}" if summary and summary.lower() not in it.title.lower() else ""
        badges = []
        if it.score is not None:
            badges.append(f'<span class="mini">score {int(it.score)}</span>')
        if it.grade:
            badges.append(f'<span class="mini grade">{html.escape(it.grade)}</span>')
        if it.signal:
            badges.append(f'<span class="mini signal-{html.escape(it.signal)}">{html.escape(it.signal)}</span>')
        for coin in it.coins[:6]:
            badges.append(f'<span class="mini coin">{html.escape(coin)}</span>')
        badge_html = f'<div class="badges">{"".join(badges)}</div>' if badges else ""
        lis.append(f'<li>{badge_html}{title}{extra}<span class="source">来源：<a href="{url}">{source}</a></span></li>')
    return "<ul>\n" + "\n".join(lis) + "\n</ul>"


def render_html(buckets: dict[str, list[Item]], prices: dict) -> str:
    nav = "\n".join(f'<a href="#{gid}">{title}</a>' for gid, title, _ in GROUPS)
    sections = []
    for gid, title, _ in GROUPS:
        extra = price_cards(prices) if gid == "market" else ""
        klass = "highlight" if gid in {"top"} else ""
        sections.append(f'<section id="{gid}" class="{klass}"><h2>{html.escape(title)}</h2>{extra}{render_items(buckets.get(gid, []))}</section>')
    sections.append('''<section id="signals" class="highlight"><h2>10. 今日交易 / 叙事信号</h2><ul>
<li class="signal">6551 高分、A/A+、币种标签与社交热度已融入整体排序；不再单独拆出热点栏目。</li>
<li class="signal">稳定币监管、ETF 资金流、交易所基础设施风险仍是当前日报的一级变量。</li>
<li class="signal">宏观/地缘事件会被纳入 Web3 信号层，但需要二次筛选是否真正影响 Crypto。</li>
</ul></section>''')
    return f'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Eason's 每日早报 · {DATE_CN}</title>
  <style>
    :root{{--bg:#0b1020;--panel:rgba(255,255,255,.075);--panel-2:rgba(255,255,255,.105);--text:#eef3ff;--muted:#aab6d3;--line:rgba(255,255,255,.14);--accent:#7dd3fc;--good:#34d399;--bad:#fb7185;--shadow:0 22px 70px rgba(0,0,0,.35)}}
    *{{box-sizing:border-box}}body{{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;color:var(--text);background:radial-gradient(circle at 15% 10%,rgba(125,211,252,.22),transparent 28rem),radial-gradient(circle at 85% 8%,rgba(192,132,252,.18),transparent 26rem),var(--bg);line-height:1.65}}a{{color:var(--accent);text-decoration:none}}a:hover{{text-decoration:underline}}.wrap{{width:min(1060px,calc(100% - 32px));margin:0 auto}}header{{padding:56px 0 30px}}.badge{{display:inline-flex;color:#dbeafe;background:rgba(125,211,252,.12);border:1px solid rgba(125,211,252,.24);padding:6px 12px;border-radius:999px;font-size:13px}}h1{{font-size:clamp(34px,6vw,64px);line-height:1.04;margin:18px 0 14px;letter-spacing:-.04em}}.subtitle{{color:var(--muted);max-width:760px;font-size:17px}}.meta{{display:flex;flex-wrap:wrap;gap:10px;margin-top:22px;color:var(--muted);font-size:14px}}.pill{{border:1px solid var(--line);background:rgba(255,255,255,.06);padding:6px 10px;border-radius:999px}}.grid{{display:grid;grid-template-columns:280px 1fr;gap:22px;align-items:start;padding-bottom:60px}}nav{{position:sticky;top:18px;background:var(--panel);border:1px solid var(--line);border-radius:22px;padding:18px;box-shadow:var(--shadow);backdrop-filter:blur(18px)}}nav .toc-title{{font-weight:700;margin-bottom:10px}}nav a{{display:block;padding:8px 10px;border-radius:12px;color:var(--muted);font-size:14px}}nav a:hover{{background:rgba(255,255,255,.08);color:var(--text);text-decoration:none}}main{{display:grid;gap:18px}}section{{background:var(--panel);border:1px solid var(--line);border-radius:26px;padding:24px;box-shadow:var(--shadow);backdrop-filter:blur(18px)}}h2{{margin:0 0 14px;font-size:23px;letter-spacing:-.02em}}ul{{margin:0;padding-left:20px}}li{{margin:0 0 14px}}li:last-child{{margin-bottom:0}}.source{{display:block;margin-top:3px;font-size:13px;color:var(--muted)}}.badges{{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 6px -2px}}.mini{{display:inline-flex;align-items:center;border:1px solid var(--line);background:rgba(255,255,255,.08);border-radius:999px;padding:2px 8px;font-size:12px;color:#dbeafe}}.grade{{border-color:rgba(251,191,36,.35);background:rgba(251,191,36,.12);color:#fde68a}}.coin{{border-color:rgba(125,211,252,.35);background:rgba(125,211,252,.12);color:#bae6fd}}.signal-bullish{{border-color:rgba(52,211,153,.35);background:rgba(52,211,153,.12);color:#a7f3d0}}.signal-bearish{{border-color:rgba(251,113,133,.35);background:rgba(251,113,133,.12);color:#fecdd3}}.highlight{{background:linear-gradient(135deg,rgba(125,211,252,.14),rgba(192,132,252,.12));border-color:rgba(125,211,252,.28)}}.signal{{border-left:4px solid var(--good);padding-left:16px}}.disclaimer{{color:var(--muted);font-size:13px;text-align:center;padding:28px 0 44px}}.price{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:12px 0 18px}}.card{{background:var(--panel-2);border:1px solid var(--line);border-radius:16px;padding:14px}}.sym{{color:var(--muted);font-size:13px}}.num{{font-size:22px;font-weight:800;margin-top:4px}}.up{{color:var(--good);font-size:13px}}.down{{color:var(--bad);font-size:13px}}.empty{{color:var(--muted)}}@media(max-width:820px){{.grid{{grid-template-columns:1fr}}nav{{position:relative;top:auto}}.price{{grid-template-columns:repeat(2,1fr)}}section{{padding:20px;border-radius:22px}}}}
  </style>
</head>
<body>
  <header class="wrap"><div class="badge">🧪 Auto Brief · Crypto Daily</div><h1>Eason's 每日早报</h1><p class="subtitle">自动生成的 Crypto 新闻聚合：稳定币、ETF/机构资金、链上巨鲸、DeFi、监管；6551 热点信号已融入整体排序。</p><div class="meta"><span class="pill">日期：{DATE_CN}</span><span class="pill">自动生成</span><span class="pill">6551 增强排序</span><span class="pill">Asia/Shanghai</span></div></header>
  <div class="wrap grid"><nav><div class="toc-title">目录</div>{nav}<a href="#signals">10. 今日信号</a></nav><main>{''.join(sections)}</main></div>
  <footer class="wrap disclaimer">免责声明：以上为公开新闻聚合与信息整理，不构成投资建议。来源链接均保留用于核查。</footer>
</body></html>'''


def compact_item(it: Item, max_len: int = 92) -> str:
    title_prefix = f"{it.author}：" if it.author else ""
    text = clean_text(title_prefix + it.title)
    text = re.sub(r"\s*[-—|]\s*(CoinDesk|Cointelegraph|Decrypt)\s*$", "", text, flags=re.I)
    if len(text) > max_len:
        text = text[:max_len].rstrip() + "…"
    tags = []
    if it.signal in {"bullish", "bearish"}:
        tags.append("偏多" if it.signal == "bullish" else "偏空")
    if it.coins:
        tags.extend(it.coins[:3])
    suffix = f"（{it.source}{' · ' + '/'.join(tags) if tags else ''}）"
    return f"- {text}{suffix}"


def render_discord_markdown(buckets: dict[str, list[Item]], prices: dict) -> str:
    """Render a Discord-friendly Chinese version without tables.

    Keep it directly readable in chat. The cron job may split the file into
    multiple Discord messages if provider limits require it.
    """
    lines: list[str] = [f"**{DATE_SLUG} 加密日报 · Discord版**", ""]
    desired = [
        ("今日最值得关注", "top", 3),
        ("可关注机会", "opportunity", 3),
        ("稳定币 / 支付 / RWA", "stablecoin", 2),
        ("监管 / 政策", "policy", 2),
        ("市场 / ETF / 机构", "market", 3),
        ("链上 / 巨鲸 / 聪明钱", "onchain", 2),
        ("DeFi / 项目动态", "defi", 2),
        ("风险提示", "risk", 2),
    ]
    for title, gid, limit in desired:
        lines.append(f"**{title}**")
        section_items = buckets.get(gid, [])[:limit]
        if gid == "market" and prices:
            market_bits = []
            for key, sym in [("bitcoin", "BTC"), ("ethereum", "ETH"), ("solana", "SOL")]:
                p = prices.get(key) or {}
                if p.get("usd") is not None:
                    chg = p.get("usd_24h_change")
                    change = f"，24h {chg:+.1f}%" if isinstance(chg, (int, float)) else ""
                    market_bits.append(f"{sym} ${p['usd']:,.4g}{change}")
            if market_bits:
                lines.append("- " + "；".join(market_bits))
        if section_items:
            lines.extend(compact_item(it) for it in section_items)
        else:
            lines.append("- 暂无高置信条目。")
        lines.append("")

    lines.append("**今日信号**")
    top_coins: list[str] = []
    for group in buckets.values():
        for it in group:
            for coin in it.coins:
                if coin not in top_coins:
                    top_coins.append(coin)
    if top_coins:
        lines.append(f"- 热点标签：{' / '.join(top_coins[:8])}")
    lines.extend([
        "- 重点看稳定币监管、ETF 资金流、链上大额异动与 DeFi 安全事件是否共振。",
        "- 6551 高分、A/A+、币种标签与社交热度已融入排序；单条仍需二次核查。",
        "",
        "免责声明：公开信息聚合，不构成投资建议。",
    ])
    return "\n".join(lines).strip() + "\n"


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
    enhanced_news, social_items = parse_6551_hot()
    items = dedupe(recent_filter(items))
    enhanced_news = dedupe(recent_filter(enhanced_news))
    social_items = dedupe(recent_filter(social_items))
    items = sorted(items, key=score_item, reverse=True)[:60]
    enhanced_news = sorted(enhanced_news, key=score_item, reverse=True)[:40]
    social_items = sorted(social_items, key=score_item, reverse=True)[:20]
    buckets = classify(items, enhanced_news, social_items)
    prices = get_prices()
    outdir = ROOT / "daily"
    outdir.mkdir(exist_ok=True)
    (outdir / f"{DATE_SLUG}.html").write_text(render_html(buckets, prices), encoding="utf-8")
    (outdir / f"{DATE_SLUG}-discord.md").write_text(render_discord_markdown(buckets, prices), encoding="utf-8")
    update_index()
    print(f"Generated daily/{DATE_SLUG}.html and daily/{DATE_SLUG}-discord.md with {len(items)} base items, {len(enhanced_news)} 6551 items, {len(social_items)} social items")

if __name__ == "__main__":
    main()
