#!/usr/bin/env python3
"""
Memecoin skener pro Solanu / pump.fun -> Telegram.

Kazdych 15 minut (GitHub Actions):
  1. stahne aktivni tokeny (pump.fun, GeckoTerminal, DexScreener),
  2. nechá jen ty v rozsahu MC a stari (default 10k-60k USD, 10 min - 2 h),
  3. prověří holdery a bezpecnost (RugCheck),
  4. bundly a deva (Helius, pokud je nastaveny HELIUS_API_KEY),
  5. narativ (meta, original vs. kopie, odkud je X odkaz),
  6. nejlepsi posle na Telegram.

Jen standardni knihovna Pythonu, zadne zavislosti.
Bez TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID jen vypise zpravu do konzole.

Neni to financni poradenstvi. Bot nic nekupuje, jen filtruje.
"""
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# NASTAVENI - tady si muzes upravit filtry
# --------------------------------------------------------------------------
CONFIG = {
    "MC_MIN": 9_000,             # minimalni market cap v USD
    "MC_MAX": 90_000,            # maximalni market cap v USD

    # Stari tokenu
    "FRESH_MIN_AGE_MIN": 10,     # mladsi nez 10 min ignorovat
    "FRESH_MAX_AGE_H": 2.0,      # starsi nez 2 h uz neni "cerstvy"
    "OLD_ENABLED": False,        # True = posilat i starsi tokeny (prezily aspon OLD_MIN_AGE_H)
    "OLD_MIN_AGE_H": 24,

    # Cerstve tokeny
    "FRESH_MIN_HOLDERS": 50,
    "FRESH_MIN_TXNS_H1": 40,     # obchodu za posledni hodinu
    "FRESH_MAX_HOLDERS_PER_1K_MC": 60,  # vic = podezrele (boti, airdrop prachu)

    # Starsi tokeny (jen kdyz OLD_ENABLED)
    "OLD_MIN_HOLDERS": 150,
    "OLD_MIN_TXNS_H1": 10,
    "OLD_MIN_VOL_H24": 5_000,

    # Holdeři (bez poolu / bonding curve)
    "MAX_TOP10_PCT": 20.0,
    "MAX_SINGLE_PCT": 8.0,
    "MAX_DEV_PCT": 5.0,
    "MAX_BUNDLE_PCT": 10.0,      # bundleri + propojene insider peněženky dohromady
    "MAX_INSIDERS": 20,          # RugCheck "graphInsidersDetected"

    # Obchodovani
    "MIN_AVG_TRADE_USD": 10,     # prumerny obchod pod $10 = boti / wash trading
    "MAX_DROP_H1_PCT": -40,      # za hodinu spadl vic nez o 40 % = pada, preskocit

    # Dev (Helius)
    "DEV_FRESH_WALLET_H": 24,    # peněženka deva mladsi nez tohle = varovani
    "DEV_BUSY_TXS": 3000,        # vic transakci = mozny seriovy launcher

    # Narativ
    "META_MIN_TOKENS": 3,        # kolik aktivnich tokenu musi sdilet slovo, aby to byla "meta"

    # Zdroje
    "PUMP_PAGES": 6,
    "GECKO_DEX_PAGES": {"pump-fun": 5, "pumpswap": 2},  # stranek po 20 poolech (podle obchodu za 24 h)
    "GECKO_TRENDING_PAGES": 3,   # stranek trending poolu za 5 min / 1 h
    "GECKO_NEW_PAGES": 3,        # stranek nejnovejsich poolu na Solane
    "GECKO_DELAY_S": 6,          # pauza mezi dotazy (GeckoTerminal ma prisny limit)
    "GECKO_TRENDING": ["5m", "1h"],

    "MAX_ALERTS": 5,             # kolik nejlepsich tokenu poslat za beh
    "MAX_RUGCHECK_CALLS": 25,    # kolik tokenu za beh proverit pres RugCheck
    "MAX_HELIUS_TOKENS": 6,      # kolik tokenu za beh proverit pres Helius (setri kredity)
    "SEND_EMPTY_SUMMARY": True,  # poslat zpravu, i kdyz nic neproslo
    "SEEN_TTL_H": 72,
}

STATE_FILE = os.environ.get("SCANNER_STATE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "state", "seen.json")
UA = "Mozilla/5.0 (memecoin-scanner; +github actions)"
STOPWORDS = {
    "the", "coin", "sol", "solana", "token", "of", "and", "on", "in", "to", "for", "a", "is", "my",
    "pump", "fun", "official", "real", "first", "by", "with", "it", "this", "that", "you", "me",
}


def env(name):
    return (os.environ.get(name) or "").strip()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def log(msg):
    print(msg, file=sys.stderr, flush=True)


def get_json(url, retries=2, timeout=20):
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            if e.code == 429:
                time.sleep(8 + attempt * 8)
                continue
            if e.code in (400, 401, 403, 404):
                break
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        time.sleep(1 + attempt)
    log(f"  ! {url[:90]} -> {last_err}")
    return None


def post_json(url, payload, timeout=20):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# --------------------------------------------------------------------------
# Helius (Solana RPC)
# --------------------------------------------------------------------------
class Helius:
    def __init__(self, key):
        self.url = "https://mainnet.helius-rpc.com/?api-key=" + urllib.parse.quote(key)
        self.calls = 0
        self.failed = False

    def rpc(self, method, params):
        if self.failed:
            return None
        for attempt in range(3):
            try:
                self.calls += 1
                res = post_json(self.url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=25)
                if "error" in res:
                    log("  ! Helius %s: %s" % (method, res["error"]))
                    return None
                return res.get("result")
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if e.code in (401, 403):
                    log("!! Helius odmita klic (HTTP %s). Zkontroluj HELIUS_API_KEY." % e.code)
                    self.failed = True
                    return None
                log("  ! Helius %s HTTP %s" % (method, e.code))
                return None
            except Exception as e:  # noqa: BLE001
                log("  ! Helius %s: %s" % (method, e))
                time.sleep(1)
        return None

    def signatures(self, address, max_pages):
        """Vraci (seznam podpisu od nejnovejsiho, dosahli_jsme_konce)."""
        out, before = [], None
        for _ in range(max_pages):
            params = {"limit": 1000}
            if before:
                params["before"] = before
            page = self.rpc("getSignaturesForAddress", [address, params])
            if page is None:
                return out, False
            out.extend(page)
            if len(page) < 1000:
                return out, True
            before = page[-1]["signature"]
        return out, False

    def tx(self, sig):
        return self.rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])


def launch_bundle_pct(h, mint, supply_ui, exclude_owners):
    """Kolik % supply dnes drzi peněženky, ktere koupily v prvnich 2 slotech (bundle/sniper)."""
    sigs, complete = h.signatures(mint, max_pages=5)
    if not sigs or not complete:
        return None  # moc transakci, zacatek jsme nenasli
    first_slot = min(s["slot"] for s in sigs)
    launch = [s for s in sigs if s["slot"] <= first_slot + 1 and not s.get("err")]
    launch.sort(key=lambda s: s["slot"])
    token_accounts = {}
    for s in launch[:15]:
        t = h.tx(s["signature"])
        if not t:
            continue
        keys = [k.get("pubkey") if isinstance(k, dict) else k
                for k in ((t.get("transaction") or {}).get("message") or {}).get("accountKeys") or []]
        for b in (t.get("meta") or {}).get("postTokenBalances") or []:
            if b.get("mint") != mint or b.get("owner") in exclude_owners:
                continue
            idx = b.get("accountIndex")
            if idx is not None and idx < len(keys):
                token_accounts[keys[idx]] = b.get("owner")
    if not token_accounts:
        return 0.0
    accs = list(token_accounts)[:100]
    res = h.rpc("getMultipleAccounts", [accs, {"encoding": "jsonParsed"}])
    total = 0.0
    for a in (res or {}).get("value") or []:
        try:
            total += float(a["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
        except (TypeError, KeyError):
            continue
    return total / supply_ui * 100 if supply_ui else None


def dev_profile(h, creator):
    """Stari peněženky deva, pocet transakci, SOL a odkud dostal prvni penize."""
    out = {"wallet": creator}
    bal = h.rpc("getBalance", [creator])
    if isinstance(bal, dict):
        out["sol"] = (bal.get("value") or 0) / 1e9
    sigs, complete = h.signatures(creator, max_pages=3)
    if not sigs:
        return out
    out["txs"] = len(sigs)
    out["txs_plus"] = not complete
    oldest = sigs[-1]
    if complete and oldest.get("blockTime"):
        out["age_h"] = (time.time() - oldest["blockTime"]) / 3600
        t = h.tx(oldest["signature"])
        for ins in (((t or {}).get("transaction") or {}).get("message") or {}).get("instructions") or []:
            p = ins.get("parsed") if isinstance(ins, dict) else None
            if isinstance(p, dict) and p.get("type") in ("transfer", "transferWithSeed"):
                info = p.get("info") or {}
                if info.get("destination") == creator and info.get("source"):
                    out["funded_by"] = info["source"]
                    break
    return out


# --------------------------------------------------------------------------
# Stav
# --------------------------------------------------------------------------
def load_seen():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def save_seen(seen):
    now = time.time()
    ttl = CONFIG["SEEN_TTL_H"] * 3600
    seen = {k: v for k, v in seen.items() if now - v < ttl}
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=0, sort_keys=True)


# --------------------------------------------------------------------------
# Zdroje kandidatu
# --------------------------------------------------------------------------
def in_mc(mc):
    return CONFIG["MC_MIN"] <= (mc or 0) <= CONFIG["MC_MAX"]


def pump_candidates():
    out = {}
    for page in range(CONFIG["PUMP_PAGES"]):
        url = ("https://frontend-api-v3.pump.fun/coins?offset=%d&limit=50"
               "&sort=last_trade_timestamp&order=DESC&includeNsfw=false" % (page * 50))
        data = get_json(url, retries=0)
        if not isinstance(data, list) or not data:
            break
        for c in data:
            if c.get("is_banned") or c.get("nsfw") or c.get("hidden"):
                continue
            mc = c.get("usd_market_cap") or c.get("market_cap_usd") or 0
            if in_mc(mc) and c.get("mint"):
                out[c["mint"]] = {
                    "sources": {"pump.fun"}, "name": c.get("name"), "symbol": c.get("symbol"),
                    "created_ms": c.get("created_timestamp"), "creator": c.get("creator"),
                    "twitter": c.get("twitter"), "telegram": c.get("telegram"),
                    "website": c.get("website"), "mc": mc,
                }
        time.sleep(0.4)
    return out


def _gecko_pools(url, out, label):
    data = get_json(url, retries=1)
    time.sleep(CONFIG["GECKO_DELAY_S"])
    pools = data.get("data") if isinstance(data, dict) else None
    if not pools:
        return False
    for p in pools:
        a = p.get("attributes") or {}
        base = (((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
        mint = base.split("_", 1)[1] if base.startswith("solana_") else None
        if not mint or mint.startswith("So1111"):
            continue
        try:
            mc = float(a.get("market_cap_usd") or a.get("fdv_usd") or 0)
        except (TypeError, ValueError):
            mc = 0
        if not in_mc(mc):
            continue
        entry = out.setdefault(mint, {"sources": set()})
        entry["sources"].add(label)
        entry.setdefault("mc", mc)
    return True


def gecko_candidates():
    """GeckoTerminal: nejaktivnejsi pooly na pump.fun / PumpSwap a trending na Solane."""
    out = {}
    for dex, pages in CONFIG["GECKO_DEX_PAGES"].items():
        for page in range(1, pages + 1):
            url = ("https://api.geckoterminal.com/api/v2/networks/solana/dexes/%s/pools"
                   "?page=%d&sort=h24_tx_count_desc" % (dex, page))
            if not _gecko_pools(url, out, "GeckoTerminal"):
                break
    for dur in CONFIG["GECKO_TRENDING"]:
        for page in range(1, CONFIG["GECKO_TRENDING_PAGES"] + 1):
            if not _gecko_pools("https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"
                                "?duration=%s&page=%d" % (dur, page), out, "Gecko trending"):
                break
    for page in range(1, CONFIG["GECKO_NEW_PAGES"] + 1):
        if not _gecko_pools("https://api.geckoterminal.com/api/v2/networks/solana/new_pools?page=%d" % page,
                            out, "Gecko new"):
            break
    return out


def dex_candidates():
    feeds = {
        "https://api.dexscreener.com/token-profiles/latest/v1": "DEX paid",
        "https://api.dexscreener.com/token-boosts/latest/v1": "DEX boost",
        "https://api.dexscreener.com/community-takeovers/latest/v1": "CTO",
    }
    out = {}
    for url, label in feeds.items():
        data = get_json(url)
        if not isinstance(data, list):
            continue
        for t in data:
            if t.get("chainId") != "solana" or not t.get("tokenAddress"):
                continue
            entry = out.setdefault(t["tokenAddress"], {"sources": set()})
            entry["sources"].add(label)
            for link in t.get("links") or []:
                if link.get("type") == "twitter":
                    entry.setdefault("twitter", link.get("url"))
                elif link.get("type") == "telegram":
                    entry.setdefault("telegram", link.get("url"))
                elif link.get("label", "").lower() == "website" or not link.get("type"):
                    entry.setdefault("website", link.get("url"))
        time.sleep(0.3)
    return out


def dex_pairs(mints):
    """DexScreener: mint -> (nejlikvidnejsi par, nejstarsi pairCreatedAt)."""
    best, earliest = {}, {}
    mints = list(mints)
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        data = get_json("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(chunk))
        if not isinstance(data, list):
            continue
        for p in data:
            mint = (p.get("baseToken") or {}).get("address")
            if mint not in chunk:
                continue
            created = p.get("pairCreatedAt")
            if created and (mint not in earliest or created < earliest[mint]):
                earliest[mint] = created
            liq = (p.get("liquidity") or {}).get("usd") or 0
            b = best.get(mint)
            if b is None or liq > ((b.get("liquidity") or {}).get("usd") or 0):
                best[mint] = p
        time.sleep(0.3)
    return best, earliest


# --------------------------------------------------------------------------
# RugCheck
# --------------------------------------------------------------------------
def rugcheck(mint):
    rep = get_json("https://api.rugcheck.xyz/v1/tokens/%s/report" % mint, retries=1)
    if not isinstance(rep, dict):
        return None
    token = rep.get("token") or {}
    known = rep.get("knownAccounts") or {}
    pool_owners = {k for k, v in known.items() if (v or {}).get("type") in ("AMM", "LOCKER")}
    creator_owners = {k for k, v in known.items() if (v or {}).get("type") == "CREATOR"}
    if rep.get("creator"):
        creator_owners.add(rep["creator"])

    top = rep.get("topHolders") or []
    real = [h for h in top if h.get("owner") not in pool_owners and h.get("address") not in pool_owners]
    pcts = [float(h.get("pct") or 0) for h in real]
    dev_pct = sum(float(h.get("pct") or 0) for h in top if h.get("owner") in creator_owners)
    supply = float(token.get("supply") or 0)
    decimals = int(token.get("decimals") or 6)
    if supply and rep.get("creatorBalance"):
        dev_pct = max(dev_pct, float(rep["creatorBalance"]) / supply * 100)
    insider_pct = 0.0
    if supply:
        insider_pct = sum(float(n.get("currentHolding") or 0) for n in rep.get("insiderNetworks") or []) / supply * 100

    return {
        "holders": int(rep.get("totalHolders") or 0),
        "top10": sum(pcts[:10]),
        "max_single": max(pcts) if pcts else 0.0,
        "dev_pct": dev_pct,
        "insiders": int(rep.get("graphInsidersDetected") or 0),
        "insider_pct": insider_pct,
        "bundle_pct": None,
        "mint_auth": token.get("mintAuthority") or rep.get("mintAuthority"),
        "freeze_auth": token.get("freezeAuthority") or rep.get("freezeAuthority"),
        "rugged": bool(rep.get("rugged")),
        "danger": [r.get("name") for r in rep.get("risks") or [] if r.get("level") == "danger"],
        "warn": [r.get("name") for r in rep.get("risks") or [] if r.get("level") == "warn"],
        "creator": rep.get("creator"),
        "supply_ui": supply / (10 ** decimals) if supply else 0,
        "pool_owners": pool_owners,
        "creator_owners": creator_owners,
    }


# --------------------------------------------------------------------------
# Narativ
# --------------------------------------------------------------------------
def words(text):
    return {w for w in re.split(r"[^a-z0-9]+", (text or "").lower().replace("$", ""))
            if len(w) >= 2 and w not in STOPWORDS and not w.isdigit()}


def build_meta(pairs, cands):
    """slovo -> pocet ruznych aktivnich tokenu, ktere ho maji v nazvu/tickeru."""
    counts = {}
    for mint, cand in cands.items():
        base = ((pairs.get(mint) or {}).get("baseToken") or {})
        for w in words((cand.get("name") or base.get("name") or "") + " " + (cand.get("symbol") or base.get("symbol") or "")):
            counts[w] = counts.get(w, 0) + 1
    return counts


def x_link_kind(url):
    if not url:
        return "bez X"
    u = urllib.parse.urlparse(url if "://" in url else "https://" + url)
    if "x.com" not in u.netloc and "twitter.com" not in u.netloc:
        return "bez X"
    parts = [p for p in u.path.split("/") if p]
    if parts[:2] == ["i", "communities"]:
        return "X komunita"
    if len(parts) >= 3 and parts[1] == "status":
        return "tweet od @%s" % parts[0] if parts[0] != "i" else "odkaz na tweet"
    if len(parts) == 1:
        return "vlastní X @%s" % parts[0]
    return "X odkaz"


def x_info(url):
    """Co jde o X odkazu zjistit zdarma: u tweetu autora, text a datum (oficialni oEmbed)."""
    kind = x_link_kind(url)
    if not kind.startswith("tweet") and kind != "odkaz na tweet":
        return kind
    clean = url.split("?")[0]
    data = get_json("https://publish.twitter.com/oembed?omit_script=1&dnt=1&url=" + urllib.parse.quote(clean, safe=""),
                    retries=0, timeout=10)
    if not isinstance(data, dict) or not data.get("html"):
        return kind + " · ⚠️ tweet nedostupný/smazaný"
    raw = data["html"]
    text = re.sub(r"<[^>]+>", " ", raw.split("&mdash;")[0])
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    text = re.sub(r"pic\.twitter\.com/\S+|https?://t\.co/\S+", "", text).strip()
    if len(text) > 90:
        text = text[:87].rstrip() + "…"
    author = (data.get("author_url") or "").rstrip("/").split("/")[-1]
    date = re.findall(r">([A-Z][a-z]+ \d{1,2}, \d{4})<", raw)
    out = "tweet od @%s" % author if author else kind
    if date:
        out += " (%s)" % date[-1]
    if text:
        out += ": „%s“" % text
    return out


def copy_check(m):
    """Existuje na Solane jiny token se stejnym tickerem, ktery je starsi nebo vetsi?"""
    sym = (m["symbol"] or "").lstrip("$").strip()
    if not sym or sym == "?":
        return None, None
    data = get_json("https://api.dexscreener.com/latest/dex/search?q=" + urllib.parse.quote(sym), retries=1)
    time.sleep(0.3)
    others = {}
    for p in (data or {}).get("pairs") or []:
        b = p.get("baseToken") or {}
        if p.get("chainId") != "solana" or b.get("address") == m["mint"]:
            continue
        if (b.get("symbol") or "").lstrip("$").lower() != sym.lower():
            continue
        prev = others.get(b["address"])
        if prev is None or (p.get("marketCap") or 0) > (prev.get("marketCap") or 0):
            others[b["address"]] = p
    if not others:
        return "originál", None
    my_created = m.get("created_ms") or 0
    bigger = [p for p in others.values() if (p.get("marketCap") or 0) > m["mc"]]
    older = [p for p in others.values() if p.get("pairCreatedAt") and my_created and p["pairCreatedAt"] < my_created]
    if bigger:
        top = max(bigger, key=lambda p: p.get("marketCap") or 0)
        return "kopie? větší $%s má MC %s" % (sym, fmt_usd(top.get("marketCap"))), top["baseToken"]["address"]
    if older:
        top = min(older, key=lambda p: p["pairCreatedAt"])
        return "kopie? existuje starší $%s (%d×)" % (sym, len(others)), top["baseToken"]["address"]
    return "originál (%d mladších kopií)" % len(others), None


# --------------------------------------------------------------------------
# Filtry
# --------------------------------------------------------------------------
def market_view(mint, cand, pair, earliest_ms):
    created = [c for c in (cand.get("created_ms"), earliest_ms) if c]
    created_ms = min(created) if created else None
    age_h = (time.time() * 1000 - created_ms) / 3_600_000 if created_ms else None
    txns = ((pair or {}).get("txns") or {}).get("h1") or {}
    vol = (pair or {}).get("volume") or {}
    chg = (pair or {}).get("priceChange") or {}
    info = (pair or {}).get("info") or {}
    for s in info.get("socials") or []:
        if s.get("type") == "twitter":
            cand.setdefault("twitter", s.get("url"))
        if s.get("type") == "telegram":
            cand.setdefault("telegram", s.get("url"))
    for w in info.get("websites") or []:
        cand.setdefault("website", w.get("url"))
    base = (pair or {}).get("baseToken") or {}
    return {
        "mint": mint,
        "name": cand.get("name") or base.get("name") or "?",
        "symbol": cand.get("symbol") or base.get("symbol") or "?",
        "mc": (pair or {}).get("marketCap") or cand.get("mc") or 0,
        "age_h": age_h, "created_ms": created_ms,
        "buys_h1": int(txns.get("buys") or 0), "sells_h1": int(txns.get("sells") or 0),
        "vol_h1": float(vol.get("h1") or 0), "vol_h24": float(vol.get("h24") or 0),
        "chg_h1": chg.get("h1"), "dex": (pair or {}).get("dexId"),
        "sources": sorted(cand.get("sources") or []),
        "twitter": cand.get("twitter"), "telegram": cand.get("telegram"), "website": cand.get("website"),
    }


def is_old(m):
    return m["age_h"] is not None and m["age_h"] >= CONFIG["OLD_MIN_AGE_H"]


def market_ok(m):
    if not in_mc(m["mc"]):
        return False, "MC mimo rozsah"
    age = m["age_h"]
    if age is None:
        return False, "neznamé stáří"
    if age * 60 < CONFIG["FRESH_MIN_AGE_MIN"]:
        return False, "mladší 10 min"
    if age > CONFIG["FRESH_MAX_AGE_H"] and not (CONFIG["OLD_ENABLED"] and is_old(m)):
        return False, "starší 2 h"
    tx = m["buys_h1"] + m["sells_h1"]
    if tx and m["vol_h1"] / tx < CONFIG["MIN_AVG_TRADE_USD"]:
        return False, "boti/wash"
    if m["chg_h1"] is not None and float(m["chg_h1"]) < CONFIG["MAX_DROP_H1_PCT"]:
        return False, "prudce padá"
    if is_old(m):
        if tx < CONFIG["OLD_MIN_TXNS_H1"]:
            return False, "málo obchodů"
        if m["vol_h24"] < CONFIG["OLD_MIN_VOL_H24"]:
            return False, "malý objem 24h"
    elif tx < CONFIG["FRESH_MIN_TXNS_H1"]:
        return False, "málo obchodů"
    return True, ""


def holders_ok(m, rc):
    if rc["rugged"]:
        return False, "rugged"
    if rc["mint_auth"]:
        return False, "mint authority"
    if rc["freeze_auth"]:
        return False, "freeze authority"
    min_h = CONFIG["OLD_MIN_HOLDERS"] if is_old(m) else CONFIG["FRESH_MIN_HOLDERS"]
    if rc["holders"] < min_h:
        return False, "málo holderů"
    if not is_old(m) and m["mc"] and rc["holders"] / (m["mc"] / 1000) > CONFIG["FRESH_MAX_HOLDERS_PER_1K_MC"]:
        return False, "nafouknutí holdeři"
    if rc["top10"] > CONFIG["MAX_TOP10_PCT"]:
        return False, "top10 > %d %%" % CONFIG["MAX_TOP10_PCT"]
    if rc["max_single"] > CONFIG["MAX_SINGLE_PCT"]:
        return False, "velryba"
    if rc["dev_pct"] > CONFIG["MAX_DEV_PCT"]:
        return False, "dev drží moc"
    if rc["insiders"] > CONFIG["MAX_INSIDERS"]:
        return False, "insideři"
    if rc["insider_pct"] > CONFIG["MAX_BUNDLE_PCT"]:
        return False, "bundle > %d %%" % CONFIG["MAX_BUNDLE_PCT"]
    return True, ""


def bundle_ok(rc):
    b = max(rc["insider_pct"], rc["bundle_pct"] or 0)
    if b > CONFIG["MAX_BUNDLE_PCT"]:
        return False, "bundle > %d %%" % CONFIG["MAX_BUNDLE_PCT"]
    return True, ""


def score(m, rc):
    tx = m["buys_h1"] + m["sells_h1"]
    buy_ratio = m["buys_h1"] / tx if tx else 0.5
    spread = max(0.0, 1 - rc["top10"] / CONFIG["MAX_TOP10_PCT"])
    vol = min(1.0, m["vol_h1"] / 20_000)
    clean = max(0.0, 1 - max(rc["insider_pct"], rc["bundle_pct"] or 0) / CONFIG["MAX_BUNDLE_PCT"])
    return 0.35 * buy_ratio + 0.25 * spread + 0.2 * vol + 0.2 * clean


# --------------------------------------------------------------------------
# Zprava
# --------------------------------------------------------------------------
def fmt_usd(v):
    if v is None:
        return "?"
    v = float(v)
    if v >= 1_000_000:
        return "$%.2fM" % (v / 1_000_000)
    if v >= 1_000:
        return "$%.1fk" % (v / 1_000)
    return "$%.0f" % v


def fmt_age(h):
    if h is None:
        return "?"
    if h < 1:
        return "%d min" % round(h * 60)
    if h < 48:
        return "%.1f h" % h
    return "%d dní" % round(h / 24)


def short(addr):
    return addr[:4] + "…" + addr[-4:] if addr and len(addr) > 10 else (addr or "?")


def dev_line(dev, rc):
    e = html.escape
    parts = ["drží %.1f %%" % rc["dev_pct"]]
    flags = []
    if dev:
        if "age_h" in dev:
            parts.append("peněženka " + fmt_age(dev["age_h"]))
            if dev["age_h"] < CONFIG["DEV_FRESH_WALLET_H"]:
                flags.append("čerstvá peněženka")
        if "txs" in dev:
            parts.append("%d%s tx" % (dev["txs"], "+" if dev.get("txs_plus") else ""))
            if dev.get("txs_plus") or dev["txs"] >= CONFIG["DEV_BUSY_TXS"]:
                flags.append("hodně aktivní (sériový launcher?)")
        if "sol" in dev:
            parts.append("%.2f SOL" % dev["sol"])
        if dev.get("funded_by"):
            parts.append('financoval <a href="https://solscan.io/account/%s">%s</a>' % (dev["funded_by"], short(dev["funded_by"])))
    line = "👤 Dev: " + " · ".join(parts)
    if flags:
        line += " · ⚠️ " + e(", ".join(flags))
    return line


def narrative_line(m, meta_word, meta_n, copy_txt, copy_addr=None):
    bits = []
    if meta_word:
        bits.append("🔥 meta „%s“ (%d tokenů)" % (html.escape(meta_word), meta_n))
    if copy_txt:
        t = html.escape(copy_txt)
        if copy_addr:
            t += ' → <a href="https://dexscreener.com/solana/%s">DexS</a> · <a href="https://pump.fun/coin/%s">pump.fun</a>' % (copy_addr, copy_addr)
        bits.append(t)
    line = "🧠 Narativ: " + (" · ".join(bits) if bits else "—")
    line += "\n🐦 X: " + html.escape(x_info(m.get("twitter")))
    return line


def token_block(m, rc, dev, meta_word, meta_n, copy_txt, copy_addr=None):
    e = html.escape
    kind = "🕰 Starší" if is_old(m) else "🆕 Čerstvý"
    chg = m["chg_h1"]
    chg_txt = (" · 1h %+.0f %%" % float(chg)) if chg is not None else ""
    tx = m["buys_h1"] + m["sells_h1"]
    bundle = max(rc["insider_pct"], rc["bundle_pct"] or 0)
    bundle_src = "launch+insideři" if rc["bundle_pct"] is not None else "insideři"
    lines = [
        "<b>%s · $%s</b> — %s" % (kind, e(str(m["symbol"])), e(str(m["name"]))),
        "<code>%s</code>" % m["mint"],
        "MC <b>%s</b> · holdeři <b>%d</b>" % (fmt_usd(m["mc"]), rc["holders"]),
        "⏱ stáří <b>%s</b> · vytvořen %s" % (fmt_age(m["age_h"]), prague_time(m["created_ms"]) if m.get("created_ms") else "?"),
        "Top10 %.1f %% · max %.1f %% · bundle %.1f %% (%s)" % (rc["top10"], rc["max_single"], bundle, bundle_src),
        "1h: %d tx (%d B / %d S) · Ø %s · vol %s%s"
        % (tx, m["buys_h1"], m["sells_h1"], fmt_usd(m["vol_h1"] / max(tx, 1)), fmt_usd(m["vol_h1"]), chg_txt),
        dev_line(dev, rc),
        narrative_line(m, meta_word, meta_n, copy_txt, copy_addr),
    ]
    extra = []
    if rc["warn"]:
        extra.append("⚠️ " + ", ".join(rc["warn"][:3]))
    if rc["danger"]:
        extra.append("🛑 " + ", ".join(rc["danger"][:3]))
    if extra:
        lines.append(e(" · ".join(extra)))
    mint = m["mint"]
    links = [
        '<a href="https://pump.fun/coin/%s">pump.fun</a>' % mint,
        '<a href="https://dexscreener.com/solana/%s">DexS</a>' % mint,
        '<a href="https://solscan.io/token/%s">Solscan</a>' % mint,
        '<a href="https://rugcheck.xyz/tokens/%s">RugCheck</a>' % mint,
        '<a href="https://gmgn.ai/sol/token/%s">GMGN</a>' % mint,
        '<a href="https://v2.bubblemaps.io/map?address=%s&amp;chain=solana">Bubblemaps</a>' % mint,
    ]
    if rc.get("creator"):
        links.append('<a href="https://solscan.io/account/%s">dev</a>' % rc["creator"])
    grok_q = ("Prověř memecoin $%s na Solaně, CA: %s. Hledej na X: kdo o něm postuje (KOLové, boti, "
              "placený shill), kdy se začal objevovat, X účet projektu (stáří, followeři, přejmenování), "
              "sentiment a red flagy. Odpověz stručně česky." % (m["symbol"], mint))
    links.append('<a href="https://grok.com/?q=%s">🤖 Grok</a>' % urllib.parse.quote(grok_q))
    for key, label in (("twitter", "X"), ("telegram", "TG"), ("website", "web")):
        url = m.get(key)
        if url and url.startswith("http"):
            links.append('<a href="%s">%s</a>' % (e(url, quote=True), label))
    lines.append(" | ".join(links))
    return "\n".join(lines)


def prague_time(ms):
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(ms / 1000, ZoneInfo("Europe/Prague")).strftime("%H:%M")
    except Exception:  # noqa: BLE001
        return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%H:%M UTC")


def now_label():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Prague")).strftime("%d.%m.%Y · %H:%M")
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc).strftime("%d.%m. %H:%M UTC")


def send(text_parts):
    token, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    messages, cur = [], ""
    for part in text_parts:
        if cur and len(cur) + len(part) + 2 > 3800:
            messages.append(cur)
            cur = ""
        cur = (cur + "\n\n" + part) if cur else part
    if cur:
        messages.append(cur)
    if not token or not chat:
        log("!! Chybi TELEGRAM_BOT_TOKEN nebo TELEGRAM_CHAT_ID (GitHub Settings -> Secrets). Zprava jen v logu.")
        for msg in messages:
            print(msg + "\n" + "-" * 40)
        return False
    all_ok = True
    for msg in messages:
        try:
            post_json("https://api.telegram.org/bot%s/sendMessage" % token, {
                "chat_id": chat, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True,
            })
        except urllib.error.HTTPError as ex:
            all_ok = False
            log("!! Telegram chyba HTTP %s: %s" % (ex.code, ex.read().decode("utf-8", "replace")[:300]))
        except Exception as ex:  # noqa: BLE001
            all_ok = False
            log("!! Telegram chyba: %s" % ex)
        time.sleep(0.5)
    return all_ok


# --------------------------------------------------------------------------
# Hlavni beh
# --------------------------------------------------------------------------
def bump(stats, why):
    stats[why] = stats.get(why, 0) + 1


def main():
    seen = load_seen()
    hkey = env("HELIUS_API_KEY")
    helius = Helius(hkey) if hkey else None
    if helius:
        slot = helius.rpc("getSlot", [])
        log("  Helius test: %s" % ("OK (slot %s)" % slot if slot else "NEFUNGUJE"))

    xt = get_json("https://publish.twitter.com/oembed?omit_script=1&url=https%3A%2F%2Fx.com%2Felonmusk%2Fstatus%2F2122589089702813944", retries=0, timeout=10)
    log("  X oEmbed test: %s" % ("OK" if isinstance(xt, dict) else "nedostupny"))
    log("Stahuji kandidaty...")
    cands = pump_candidates()
    pump_ok = bool(cands)
    gecko = gecko_candidates()
    for src in (gecko, dex_candidates()):
        for mint, d in src.items():
            if mint in cands:
                cands[mint]["sources"] |= d["sources"]
                for k in ("twitter", "telegram", "website", "mc"):
                    if d.get(k):
                        cands[mint].setdefault(k, d[k])
            else:
                cands[mint] = d
    log("  kandidatu: %d (pump.fun %s, GeckoTerminal %d, Helius %s)"
        % (len(cands), "OK" if pump_ok else "nedostupny", len(gecko), "ano" if helius else "ne"))

    pairs, earliest = dex_pairs(cands.keys())
    meta = build_meta(pairs, cands)

    stats, shortlist = {}, []
    for mint, cand in cands.items():
        if mint in seen:
            bump(stats, "už posláno")
            continue
        m = market_view(mint, cand, pairs.get(mint), earliest.get(mint))
        ok, why = market_ok(m)
        if not ok:
            bump(stats, why)
            continue
        shortlist.append(m)
    shortlist.sort(key=lambda m: m["buys_h1"] + m["sells_h1"], reverse=True)
    shortlist = shortlist[:CONFIG["MAX_RUGCHECK_CALLS"]]
    log("  vyrazeno pred RugCheckem: %s" % json.dumps(stats, ensure_ascii=False))
    log("  RugCheck: %d" % len(shortlist))

    stage2 = []
    for m in shortlist:
        rc = rugcheck(m["mint"])
        time.sleep(0.8)
        if rc is None:
            bump(stats, "RugCheck nedostupný")
            continue
        ok, why = holders_ok(m, rc)
        if not ok:
            bump(stats, why)
            continue
        stage2.append((m, rc))
    stage2.sort(key=lambda x: score(*x), reverse=True)

    # Helius: bundly z launche + dev, jen u nejlepsich (setri kredity)
    passed = []
    for i, (m, rc) in enumerate(stage2):
        dev = None
        if helius and i < CONFIG["MAX_HELIUS_TOKENS"] and not helius.failed:
            exclude = rc["pool_owners"] | rc["creator_owners"]
            rc["bundle_pct"] = launch_bundle_pct(helius, m["mint"], rc["supply_ui"], exclude)
            ok, why = bundle_ok(rc)
            if not ok:
                bump(stats, why)
                continue
            if rc.get("creator"):
                dev = dev_profile(helius, rc["creator"])
        passed.append((m, rc, dev))
    if helius:
        log("  Helius volani: %d" % helius.calls)

    passed.sort(key=lambda x: score(x[0], x[1]), reverse=True)
    extra = passed[CONFIG["MAX_ALERTS"]:]
    passed = passed[:CONFIG["MAX_ALERTS"]]

    blocks = []
    for m, rc, dev in passed:
        tw = sorted(words(m["name"] + " " + m["symbol"]), key=lambda w: -meta.get(w, 0))
        meta_word = tw[0] if tw and meta.get(tw[0], 0) >= CONFIG["META_MIN_TOKENS"] else None
        copy_txt, copy_addr = copy_check(m)
        blocks.append(token_block(m, rc, dev, meta_word, meta.get(meta_word, 0) if meta_word else 0, copy_txt, copy_addr))

    header = "━━━━━━━━━━━━━━━━━━\n🔎 <b><u>SKENER · %s</u></b>\n━━━━━━━━━━━━━━━━━━\nMC %s–%s · %d min–%g h · prověřeno %d · <b>prošlo %d</b>" % (
        now_label(), fmt_usd(CONFIG["MC_MIN"]), fmt_usd(CONFIG["MC_MAX"]),
        CONFIG["FRESH_MIN_AGE_MIN"], CONFIG["FRESH_MAX_AGE_H"], len(shortlist), len(passed) + len(extra))
    if extra:
        header += "\nnejlepších %d níže, další: %s" % (
            len(passed), html.escape(", ".join("$" + str(m["symbol"]) for m, _, _ in extra)))
    if not helius:
        header += "\nℹ️ Bez HELIUS_API_KEY: bundle jen podle RugChecku, bez kontroly deva."
    elif helius.failed:
        header += "\n⚠️ Helius klíč nefunguje, bundle/dev jen částečně."
    if not pump_ok and not gecko:
        header += "\n⚠️ pump.fun ani GeckoTerminal neodpovídají, jen DexScreener."
    if stats:
        top_reasons = sorted(stats.items(), key=lambda kv: -kv[1])[:5]
        header += "\n<i>vyřazeno: " + html.escape(", ".join("%s %d" % kv for kv in top_reasons)) + "</i>"

    if passed or CONFIG["SEND_EMPTY_SUMMARY"]:
        parts = [header] + blocks
        if passed:
            parts.append("<i>Jen filtr, ne doporučení. Narativ a X ještě projdi očima.</i>")
        if send(parts):
            for m, _, _ in passed:
                seen[m["mint"]] = time.time()
    save_seen(seen)
    log("Hotovo: proslo %d" % len(passed))


if __name__ == "__main__":
    main()
