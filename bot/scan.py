#!/usr/bin/env python3
"""
Memecoin skener pro Solanu / pump.fun -> Telegram.

Kazdych 15 minut (GitHub Actions) projde:
  - pump.fun: tokeny, se kterymi se prave obchoduje
  - DexScreener: nove profily ("DEX paid"), boosty a community takeovery
vyfiltruje tokeny s market capem v nastavenem rozsahu, u nadejnych dotahne
z RugChecku holdery (pocet, top 10, dev, insideri, mint/freeze) a souhrn posle
na Telegram.

Jen standardni knihovna Pythonu, zadne zavislosti.
Bez promennych TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID jen vypise zpravu do konzole.

Neni to financni poradenstvi. Bot nic nekupuje, jen filtruje.
"""
import html
import json
import os
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
    "MC_MIN": 10_000,            # minimalni market cap v USD
    "MC_MAX": 60_000,            # maximalni market cap v USD
    "PUMP_PAGES": 6,             # kolik stranek (po 50) z pump.fun projit
    "GECKO_DEXES": ["pump-fun", "pumpswap"],  # GeckoTerminal: bonding curve + po graduaci
    "GECKO_PAGES": 3,            # stranek po 20 poolech na kazdy dex
    "OLD_MIN_AGE_H": 24,         # od kolika hodin je token "starsi"

    # Cerstve tokeny (mladsi nez OLD_MIN_AGE_H)
    "FRESH_MIN_AGE_H": 1.0,      # mladsi nez 1 h ignorovat (nic "za sebou")
    "FRESH_MIN_HOLDERS": 80,
    "FRESH_MIN_TXNS_H1": 40,     # obchodu za posledni hodinu
    "FRESH_MAX_HOLDERS_PER_1K_MC": 60,  # vic = podezrele (boti, airdrop prachu)

    # Starsi tokeny (preziji aspon den)
    "OLD_MIN_HOLDERS": 150,
    "OLD_MIN_TXNS_H1": 10,
    "OLD_MIN_VOL_H24": 5_000,    # objem za 24 h v USD

    # Holdeři (bez poolu / bonding curve)
    "MAX_TOP10_PCT": 30.0,
    "MAX_SINGLE_PCT": 8.0,
    "MAX_DEV_PCT": 5.0,
    "MAX_INSIDERS": 20,          # RugCheck "graphInsidersDetected"

    # Obchodovani
    "MIN_AVG_TRADE_USD": 10,     # prumerny obchod pod $10 = boti / wash trading
    "MAX_DROP_H1_PCT": -40,      # za hodinu spadl vic nez o 40 % = pada, preskocit

    "MAX_ALERTS": 5,             # kolik nejlepsich tokenu poslat za beh
    "MAX_RUGCHECK_CALLS": 25,    # kolik tokenu za beh proverit do hloubky
    "SEND_EMPTY_SUMMARY": True,  # poslat zpravu, i kdyz nic neproslo
    "SEEN_TTL_H": 72,            # jak dlouho si pamatovat uz poslane tokeny
}

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state", "seen.json")
UA = "Mozilla/5.0 (memecoin-scanner; +github actions)"


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
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
                time.sleep(3 + attempt * 3)
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


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Stav (co uz bylo poslano)
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
def pump_candidates():
    """Tokeny z pump.fun, se kterymi se prave obchoduje, v rozsahu MC."""
    out = {}
    for page in range(CONFIG["PUMP_PAGES"]):
        url = ("https://frontend-api-v3.pump.fun/coins?offset=%d&limit=50"
               "&sort=last_trade_timestamp&order=DESC&includeNsfw=false" % (page * 50))
        data = get_json(url)
        if not isinstance(data, list) or not data:
            break
        for c in data:
            if c.get("is_banned") or c.get("nsfw") or c.get("hidden"):
                continue
            mc = c.get("usd_market_cap") or c.get("market_cap_usd") or 0
            if CONFIG["MC_MIN"] <= mc <= CONFIG["MC_MAX"] and c.get("mint"):
                out[c["mint"]] = {
                    "sources": {"pump.fun"},
                    "name": c.get("name"),
                    "symbol": c.get("symbol"),
                    "created_ms": c.get("created_timestamp"),
                    "complete": c.get("complete"),
                    "creator": c.get("creator"),
                    "twitter": c.get("twitter"),
                    "telegram": c.get("telegram"),
                    "website": c.get("website"),
                    "mc": mc,
                }
        time.sleep(0.4)
    return out


def gecko_candidates():
    """Nejaktivnejsi pooly na pump.fun (bonding curve) a PumpSwap z GeckoTerminalu.
    Zaloha za pump.fun API, ktere blokuje servery GitHubu."""
    out = {}
    for dex in CONFIG["GECKO_DEXES"]:
        for page in range(1, CONFIG["GECKO_PAGES"] + 1):
            url = ("https://api.geckoterminal.com/api/v2/networks/solana/dexes/%s/pools"
                   "?page=%d&sort=h24_tx_count_desc" % (dex, page))
            data = get_json(url)
            time.sleep(2.2)  # limit 30 dotazu / min
            pools = (data or {}).get("data") if isinstance(data, dict) else None
            if not pools:
                break
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
                if not (CONFIG["MC_MIN"] <= mc <= CONFIG["MC_MAX"]):
                    continue
                created = None
                if a.get("pool_created_at"):
                    try:
                        created = datetime.fromisoformat(a["pool_created_at"].replace("Z", "+00:00")).timestamp() * 1000
                    except ValueError:
                        pass
                entry = out.setdefault(mint, {"sources": set()})
                entry["sources"].add("GeckoTerminal")
                entry.setdefault("mc", mc)
                if created and (not entry.get("created_ms") or created < entry["created_ms"]):
                    entry["created_ms"] = created
    return out


def dex_candidates():
    """Solana tokeny s novym profilem na DexScreeneru, boostem nebo CTO."""
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
    """Trzni data z DexScreeneru (po 30 adresach). Vraci mint -> nejlikvidnejsi par."""
    result = {}
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
            liq = (p.get("liquidity") or {}).get("usd") or 0
            best = result.get(mint)
            if best is None or liq > ((best.get("liquidity") or {}).get("usd") or 0):
                result[mint] = p
        time.sleep(0.3)
    return result


# --------------------------------------------------------------------------
# RugCheck - holderi a bezpecnost
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
    supply = token.get("supply") or 0
    if supply and rep.get("creatorBalance"):
        dev_pct = max(dev_pct, float(rep["creatorBalance"]) / float(supply) * 100)

    return {
        "holders": int(rep.get("totalHolders") or 0),
        "top10": sum(pcts[:10]),
        "max_single": max(pcts) if pcts else 0.0,
        "dev_pct": dev_pct,
        "insiders": int(rep.get("graphInsidersDetected") or 0),
        "insider_top": sum(1 for h in real[:10] if h.get("insider")),
        "mint_auth": token.get("mintAuthority") or rep.get("mintAuthority"),
        "freeze_auth": token.get("freezeAuthority") or rep.get("freezeAuthority"),
        "rugged": bool(rep.get("rugged")),
        "danger": [r.get("name") for r in rep.get("risks") or [] if r.get("level") == "danger"],
        "warn": [r.get("name") for r in rep.get("risks") or [] if r.get("level") == "warn"],
        "creator": rep.get("creator"),
    }


# --------------------------------------------------------------------------
# Logika filtru
# --------------------------------------------------------------------------
def market_view(mint, cand, pair):
    now_ms = time.time() * 1000
    created = cand.get("created_ms") or (pair or {}).get("pairCreatedAt")
    age_h = (now_ms - created) / 3_600_000 if created else None
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
        "age_h": age_h,
        "buys_h1": int(txns.get("buys") or 0),
        "sells_h1": int(txns.get("sells") or 0),
        "vol_h1": float(vol.get("h1") or 0),
        "vol_h24": float(vol.get("h24") or 0),
        "chg_h1": chg.get("h1"),
        "liq": ((pair or {}).get("liquidity") or {}).get("usd"),
        "dex": (pair or {}).get("dexId"),
        "sources": sorted(cand.get("sources") or []),
        "twitter": cand.get("twitter"),
        "telegram": cand.get("telegram"),
        "website": cand.get("website"),
    }


def is_old(m):
    return m["age_h"] is not None and m["age_h"] >= CONFIG["OLD_MIN_AGE_H"]


def market_ok(m):
    if not (CONFIG["MC_MIN"] <= m["mc"] <= CONFIG["MC_MAX"]):
        return False, "MC mimo rozsah"
    if m["age_h"] is None or m["age_h"] < CONFIG["FRESH_MIN_AGE_H"]:
        return False, "mladsi nez %.0f h" % CONFIG["FRESH_MIN_AGE_H"]
    tx = m["buys_h1"] + m["sells_h1"]
    if tx and m["vol_h1"] / tx < CONFIG["MIN_AVG_TRADE_USD"]:
        return False, "boti/wash"
    if m["chg_h1"] is not None and float(m["chg_h1"]) < CONFIG["MAX_DROP_H1_PCT"]:
        return False, "prudce pada"
    if is_old(m):
        if tx < CONFIG["OLD_MIN_TXNS_H1"]:
            return False, "malo obchodu"
        if m["vol_h24"] < CONFIG["OLD_MIN_VOL_H24"]:
            return False, "maly objem 24h"
    elif tx < CONFIG["FRESH_MIN_TXNS_H1"]:
        return False, "malo obchodu"
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
        return False, "malo holderu"
    if not is_old(m) and m["mc"] and rc["holders"] / (m["mc"] / 1000) > CONFIG["FRESH_MAX_HOLDERS_PER_1K_MC"]:
        return False, "nafouknuti holderi"
    if rc["top10"] > CONFIG["MAX_TOP10_PCT"]:
        return False, "top10 moc vysoko"
    if rc["max_single"] > CONFIG["MAX_SINGLE_PCT"]:
        return False, "velryba"
    if rc["dev_pct"] > CONFIG["MAX_DEV_PCT"]:
        return False, "dev drzi moc"
    if rc["insiders"] > CONFIG["MAX_INSIDERS"]:
        return False, "insideri"
    return True, ""


def score(m, rc):
    """Jednoduche poradi: prevaha nakupu, rozlozeni holderu, objem, maly pocet insideru."""
    tx = m["buys_h1"] + m["sells_h1"]
    buy_ratio = m["buys_h1"] / tx if tx else 0.5
    spread = max(0.0, 1 - rc["top10"] / CONFIG["MAX_TOP10_PCT"])
    vol = min(1.0, m["vol_h1"] / 20_000)
    insiders = max(0.0, 1 - rc["insiders"] / max(CONFIG["MAX_INSIDERS"], 1))
    return 0.35 * buy_ratio + 0.25 * spread + 0.25 * vol + 0.15 * insiders


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


def token_block(m, rc):
    e = html.escape
    kind = "🕰 Starší" if is_old(m) else "🆕 Čerstvý"
    chg = m["chg_h1"]
    chg_txt = (" · 1h %+.0f %%" % float(chg)) if chg is not None else ""
    lines = [
        "<b>%s · $%s</b> — %s" % (kind, e(str(m["symbol"])), e(str(m["name"]))),
        "<code>%s</code>" % m["mint"],
        "MC <b>%s</b> · stáří %s · holdeři <b>%d</b>" % (fmt_usd(m["mc"]), fmt_age(m["age_h"]), rc["holders"]),
        "Top10 %.1f %% · max %.1f %% · dev %.1f %% · insideři %d"
        % (rc["top10"], rc["max_single"], rc["dev_pct"], rc["insiders"]),
        "1h: %d tx (%d B / %d S) · Ø obchod %s · vol 1h %s · 24h %s%s"
        % (m["buys_h1"] + m["sells_h1"], m["buys_h1"], m["sells_h1"],
           fmt_usd(m["vol_h1"] / max(m["buys_h1"] + m["sells_h1"], 1)),
           fmt_usd(m["vol_h1"]), fmt_usd(m["vol_h24"]), chg_txt),
    ]
    extra = []
    if m["sources"]:
        extra.append("zdroj: " + ", ".join(m["sources"]))
    if m["dex"]:
        extra.append("dex: " + m["dex"])
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
    ]
    if rc.get("creator"):
        links.append('<a href="https://solscan.io/account/%s">dev</a>' % rc["creator"])
    for key, label in (("twitter", "X"), ("telegram", "TG"), ("website", "web")):
        url = m.get(key)
        if url and url.startswith("http"):
            links.append('<a href="%s">%s</a>' % (e(url, quote=True), label))
    lines.append(" | ".join(links))
    return "\n".join(lines)


def now_label():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Prague")).strftime("%d.%m. %H:%M")
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc).strftime("%d.%m. %H:%M UTC")


def send(text_parts):
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    # sloucit bloky do zprav do ~3800 znaku (limit Telegramu je 4096)
    messages, cur = [], ""
    for part in text_parts:
        if cur and len(cur) + len(part) + 2 > 3800:
            messages.append(cur)
            cur = ""
        cur = (cur + "\n\n" + part) if cur else part
    if cur:
        messages.append(cur)
    if not token or not chat:
        log("!! Chybi TELEGRAM_BOT_TOKEN nebo TELEGRAM_CHAT_ID (GitHub Settings -> Secrets). "
            "Zprava jen vypsana do logu.")
        for msg in messages:
            print(msg + "\n" + "-" * 40)
        return False
    all_ok = True
    for msg in messages:
        try:
            post_json("https://api.telegram.org/bot%s/sendMessage" % token, {
                "chat_id": chat, "text": msg, "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
        except urllib.error.HTTPError as ex:
            all_ok = False
            detail = ex.read().decode("utf-8", "replace")[:300]
            log("!! Telegram chyba HTTP %s: %s" % (ex.code, detail))
            if ex.code in (400, 403):
                log("   Zkontroluj TELEGRAM_CHAT_ID a jestli jsi botovi v Telegramu napsal aspon jednu zpravu.")
        except Exception as ex:  # noqa: BLE001
            all_ok = False
            log("!! Telegram chyba: %s" % ex)
        time.sleep(0.5)
    return all_ok


# --------------------------------------------------------------------------
# Hlavni beh
# --------------------------------------------------------------------------
def main():
    seen = load_seen()
    log("Stahuji kandidaty...")
    cands = pump_candidates()
    pump_ok = bool(cands)
    gecko = gecko_candidates()
    for extra_src in (gecko, dex_candidates()):
        for mint, d in extra_src.items():
            if mint in cands:
                cands[mint]["sources"] |= d["sources"]
                for k in ("twitter", "telegram", "website", "created_ms", "mc"):
                    if d.get(k):
                        cands[mint].setdefault(k, d[k])
            else:
                cands[mint] = d
    log("  kandidatu: %d (pump.fun %s, GeckoTerminal %d)"
        % (len(cands), "OK" if pump_ok else "nedostupny", len(gecko)))

    pairs = dex_pairs(cands.keys())
    stats = {}
    shortlist = []
    for mint, cand in cands.items():
        if mint in seen:
            stats["uz poslano"] = stats.get("uz poslano", 0) + 1
            continue
        m = market_view(mint, cand, pairs.get(mint))
        ok, why = market_ok(m)
        if not ok:
            stats[why] = stats.get(why, 0) + 1
            continue
        shortlist.append(m)
    in_range = sum(1 for mint, c in cands.items()
                   if CONFIG["MC_MIN"] <= (((pairs.get(mint) or {}).get("marketCap")) or c.get("mc") or 0) <= CONFIG["MC_MAX"])

    shortlist.sort(key=lambda m: m["buys_h1"] + m["sells_h1"], reverse=True)
    shortlist = shortlist[:CONFIG["MAX_RUGCHECK_CALLS"]]
    log("  do hloubky proveruji: %d" % len(shortlist))

    passed = []
    for m in shortlist:
        rc = rugcheck(m["mint"])
        time.sleep(0.8)
        if rc is None:
            stats["RugCheck nedostupny"] = stats.get("RugCheck nedostupny", 0) + 1
            continue
        ok, why = holders_ok(m, rc)
        if not ok:
            stats[why] = stats.get(why, 0) + 1
            continue
        passed.append((m, rc))

    passed.sort(key=lambda x: score(*x), reverse=True)
    extra = passed[CONFIG["MAX_ALERTS"]:]
    passed = passed[:CONFIG["MAX_ALERTS"]]

    header = "🔎 <b>Skener %s</b>\nMC %s–%s · v rozsahu %d · prověřeno %d · <b>prošlo %d</b>" % (
        now_label(), fmt_usd(CONFIG["MC_MIN"]), fmt_usd(CONFIG["MC_MAX"]),
        in_range, len(shortlist), len(passed) + len(extra))
    if extra:
        header += "\nnejlepších %d níže, další: %s" % (
            len(passed), html.escape(", ".join("$" + str(m["symbol"]) for m, _ in extra)))
    if not pump_ok and not gecko:
        header += "\n⚠️ pump.fun ani GeckoTerminal teď neodpovídají, jen DexScreener zdroje."
    if stats:
        top_reasons = sorted(stats.items(), key=lambda kv: -kv[1])[:4]
        header += "\n<i>vyřazeno: " + html.escape(", ".join("%s %d" % kv for kv in top_reasons)) + "</i>"

    if passed or CONFIG["SEND_EMPTY_SUMMARY"]:
        parts = [header] + [token_block(m, rc) for m, rc in passed]
        if passed:
            parts.append("<i>Jen filtr, ne doporučení. Před nákupem projdi checklist (X, narativ, dev).</i>")
        if send(parts):
            # zapamatovat si jen to, co opravdu odeslo
            for m, _ in passed:
                seen[m["mint"]] = time.time()
    save_seen(seen)
    log("Hotovo: proslo %d" % len(passed))


if __name__ == "__main__":
    main()
