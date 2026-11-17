# Memecoin skener (Solana / pump.fun → Telegram)

Každých 10 minut projde pump.fun, GeckoTerminal a DexScreener a hledá **čerstvé tokeny (10 min – 2 h)** s market capem **9k–90k USD**. Nadějné kandidáty prověří přes RugCheck: počet holderů, top 10, procento deva, insidery a mint/freeze authority. Souhrn ti pošle na Telegram, včetně CA a odkazů na pump.fun, DexScreener, Solscan, RugCheck a GMGN.

Běží zdarma na **GitHub Actions**, takže počítač nemusí být zapnutý.

> Bot nic nekupuje a nic nedoporučuje. Jen odfiltruje odpad, aby ses díval na méně věcí. Před nákupem vždy projdi checklist.

---

## Instalace (asi 15 minut, stačí jednou)

### 1. Telegram bot

1. V Telegramu otevři **@BotFather** a pošli mu `/newbot`.
2. Zadej jméno (třeba `Muj skener`) a username končící na `bot`.
3. BotFather ti pošle **token**, něco jako `123456789:AAH...`. Ulož si ho.
4. Otevři svého nového bota a pošli mu jakoukoli zprávu (třeba `ahoj`). Bez toho ti nemůže psát.
5. Otevři **@userinfobot** a pošli mu cokoli. Odpoví tvým **ID**, což je číslo, třeba `987654321`. To je tvoje `TELEGRAM_CHAT_ID`.

### 2. GitHub repozitář

1. Založ si účet na [github.com](https://github.com), pokud ho nemáš.
2. Vpravo nahoře klikni na **+** → **New repository**.
   - Název: třeba `memecoin-scanner`
   - Zvol **Public**. U veřejného repozitáře je GitHub Actions zdarma bez limitu. Kód bude vidět, ale tvůj token zůstane skrytý v Secrets.
   - **Create repository**
3. Klikni na **uploading an existing file** a přetáhni tam **obsah** rozbalené složky `memecoin-scanner` (složky `bot`, `state`, `.github` a soubor `README.md`). Pak **Commit changes**.
   - ⚠️ Složka `.github` je na Macu i ve Windows skrytá. Když se nenahraje, vytvoř soubor ručně: **Add file → Create new file**, jako název napiš `.github/workflows/scan.yml` a vlož obsah souboru `scan.yml`.

### 3. Tajné údaje (Secrets)

V repozitáři jdi do **Settings → Secrets and variables → Actions → New repository secret** a přidej dva:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token od BotFathera |
| `TELEGRAM_CHAT_ID` | tvoje ID od @userinfobot |

### 4. Povolit zápis

**Settings → Actions → General → Workflow permissions** → zaškrtni **Read and write permissions** → **Save**. Bez toho si bot nezapamatuje, co už poslal, a posílal by ti pořád to samé.

### 5. Spustit

1. Záložka **Actions** → když se zeptá, potvrď **I understand my workflows, go ahead and enable them**.
2. Vlevo klikni na **memecoin-scan** → **Run workflow** → **Run workflow**.
3. Asi do minuty by ti měla na Telegram přijít první zpráva. Pak to běží samo každých 10 minut.

---

## Co znamená zpráva

```
🆕 Čerstvý · $TICKER — Název
<CA>                      ← klikni a zkopíruje se
MC $26k · stáří 2.0 h · holdeři 240
Top10 18 % · max 3 % · dev 0 % · insideři 2
1h: 100 tx (70 B / 30 S) · Ø obchod $90 · vol 1h $9k · 24h $20k · 1h +12 %
pump.fun | DexS | Solscan | RugCheck | GMGN | dev | X
```

- **🆕 Čerstvý:** token je starý 1–24 h. **🕰 Starší:** přežil víc než den a pořád se s ním obchoduje.
- **Top10 / max:** kolik drží největší holdeři. Pool a bonding curve se nepočítají.
- **insideři:** kolik propojených „insider“ peněženek našel RugCheck.
- **Ø obchod:** průměrná velikost obchodu. Velmi malá čísla znamenají boty.

## Jak bot filtruje (dá se změnit)

Všechno je nahoře v souboru `bot/scan.py` v části `CONFIG`. Na GitHubu otevři soubor, klikni na tužku, uprav hodnotu a dej **Commit changes**.

| Nastavení | Výchozí | Význam |
|---|---|---|
| `MC_MIN` / `MC_MAX` | 9 000 / 90 000 | rozsah market capu v USD |
| `FRESH_MIN_AGE_MIN` | 10 | tokeny mladší než 10 minut ignoruje |
| `FRESH_MAX_AGE_H` | 2 | tokeny starší než 2 hodiny ignoruje |
| `OLD_ENABLED` | False | `True` = posílat i tokeny starší než den |
| `FRESH_MIN_HOLDERS` | 50 | minimum holderů |
| `MAX_TOP10_PCT` | 20 | top 10 holderů max. 20 % |
| `MAX_BUNDLE_PCT` | 10 | bundleři a propojené insider peněženky max. 10 % |
| `MAX_SINGLE_PCT` | 8 | žádná peněženka nad 8 % |
| `MAX_DEV_PCT` | 5 | dev max. 5 % |
| `MIN_AVG_TRADE_USD` | 10 | průměrný obchod pod $10 = boti |
| `MAX_DROP_H1_PCT` | -40 | za hodinu spadl o víc než 40 % = přeskočit |
| `MAX_ALERTS` | 5 | kolik nejlepších tokenů poslat za běh |
| `SEND_EMPTY_SUMMARY` | True | `False` = nepiš, když nic neprošlo |

### Dev a bundle (Helius)

S bezplatným klíčem od [Helius](https://dashboard.helius.dev) uloženým v Secrets jako `HELIUS_API_KEY` bot u nejlepších tokenů navíc zjistí:

- **bundle z launche:** kolik % supply dnes drží peněženky, které koupily v prvních 2 slotech (bundleři a snipeři),
- **deva:** stáří peněženky, počet transakcí, kolik má SOL a odkud dostal první peníze. Varuje u čerstvé peněženky a u sériového launchera.

Bez klíče bere bundle jen podle propojených peněženek z RugChecku a deva neprověřuje.

### Narativ (automaticky)

- **meta:** kolik dalších aktivních tokenů sdílí stejné slovo v názvu, třeba „gta“ (7 tokenů),
- **originál vs. kopie:** jestli na Solaně existuje větší nebo starší token se stejným tickerem,
- **X odkaz:** vlastní účet projektu, cizí tweet (třeba od @elonmusk), X komunita, nebo bez X.

Jestli je meme vtipné a jestli X účet stojí za něco, musíš posoudit sám.

## Dobré vědět

- **GitHub někdy zpozdí spuštění** o pár minut. Na 15minutové souhrny to nevadí.
- **Každý token pošle jen jednou** (pamatuje si ho 3 dny).
- Kdyby pump.fun blokoval servery GitHubu, zpráva to napíše a bot pojede jen se zdroji z DexScreeneru (DEX paid, boosty, CTO).
- GitHub vypíná naplánované běhy po 60 dnech bez aktivity v repozitáři. Bot ale ukládá stav commitem, takže by měl běžet dál. Kdyby se zastavil, stačí v **Actions** kliknout na **Enable workflow**.
- Zastavení: **Actions → memecoin-scan → ⋯ → Disable workflow**.

## Test u sebe na počítači (volitelné)

S nainstalovaným Pythonem 3: `python bot/scan.py`. Bez Telegram údajů jen vypíše zprávu do konzole.
