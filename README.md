# EUR/USD, USD/JPY, EUR/JPY and US Treasury alert bot

Free, read-only bot. It runs on GitHub's servers (your laptop can be off) and sends Telegram alerts. It has no broker
login and cannot trade.

## What it sends
- Reminders before high-impact (60 min, plus a 10-min heads-up) and medium-impact (15 min) EUR, USD and JPY releases,
  including US Treasury auctions. Each shows forecast, previous, what a beat usually does, current prices and past reactions.
- A quick "+5 min" reaction and a full 60-minute result after high-impact releases (pips, direction, price).
- Sudden-move alerts for EUR/USD, USD/JPY and EUR/JPY (labelled with the release that probably caused them).
- Surprise alerts (actual vs forecast) if your Finnhub plan includes the calendar.
- Headlines from central banks and forex news, scored by strength, deduplicated, with the pairs they affect.
- A daily "bot is running" message with the next 24h of high-impact events.
- A warning if any part keeps failing (bad key, rate limit), with the reason.

## Setup (about 15 minutes, no card needed)
1. Telegram: message @BotFather, send /newbot, copy the bot token. Send any message to your new bot, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy the chat id (the number after "chat":{"id":).
2. Free key from twelvedata.com (prices).
3. Optional: Finnhub key (finnhub.io) for surprise alerts. Check that your plan includes the economic calendar.
4. Create a GitHub repo, make it PUBLIC (free unlimited Actions minutes; a private repo only gets 2,000 free
   minutes a month, so use the 30-minute schedule noted in alerts.yml). Upload these files, keeping the .github folder.
   The only public data is state.json (headline IDs, stats). Never upload a .env file.
5. Repo > Settings > Secrets and variables > Actions:
   - Secrets tab: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TWELVEDATA_API_KEY, FINNHUB_API_KEY (optional)
   - Variables tab: TIMEZONE, for example Asia/Amman (otherwise times show in UTC)
6. Actions tab > eurusd-alerts > Run workflow once. It then runs every 5 minutes.

## Check that everything works (recommended)
On any computer with Python 3.10+ (`pip install requests`), set the same values as environment variables and run:
```
python eurusd_alerts.py check    # tests the calendar, every news feed, prices, Finnhub and Telegram; prints OK/FAIL
python eurusd_alerts.py demo     # sends a sample alert to your Telegram
```
`check` never prints your keys. It shows how old the newest price is, which tells you whether the free price feed is live or
delayed. Send me its output and I will fix whatever fails.
Windows (PowerShell): `$env:TELEGRAM_BOT_TOKEN="..."` then run the command. Mac/Linux: `export TELEGRAM_BOT_TOKEN=...`.

## Reading the alerts
```
🔴 USD CPI m/m · 15:30 (in 40 min)
Fcst 0.3% · Prev 0.6%
Above fcst usually: EUR/USD▼ USD/JPY▲ (EUR/JPY mixed) (below: reverse)
Now: EUR/USD 1.1201 · USD/JPY 157.00
Past reactions, pips (▲ buyers ▼ sellers):
EUR/USD last 52▼ | max 81▲ | avg 42 | up 3/8
When above fcst (3x, avg swing): EUR/USD 38▼ (up 0/3) · USD/JPY 22▲ (up 3/3) · EUR/JPY 8▲ (up 2/3)
```
- "last" = most recent reaction, "max" = biggest logged, "avg" = typical size.
- "up 3/8" = the pair's biggest swing in the hour after was upward after 3 of 8 logged releases (down after the other 5).
- The "When above/below fcst" lines split history by whether the result beat the forecast. Forex Factory's free feed has
  no actual results, so the bot tags a release one cycle later using the next release's "previous" value. Revisions can
  occasionally make a tag wrong. Add `forecast` and `actual` columns to a backfill CSV to fill history immediately.
- EUR/JPY is marked "mixed" for USD or JPY news because the moves can offset. "Above fcst usually" is a rule of thumb.

## Backfilling history
CSV columns: `datetime_utc,title` and optionally `forecast,actual`, for example
`2026-11-10 13:30,USD CPI m/m,0.3%,0.4%` (see backfill_cpi_example.csv). Titles are the currency plus Forex Factory's
event name. Then run `python eurusd_alerts.py backfill past.csv` with TWELVEDATA_API_KEY set. Without it, history builds up
automatically as releases happen.

## Spam controls
- Once per event, once per link, and the same story from different sources is sent once.
- Caps per hour: MAX_NORMAL_PER_HOUR (5) for medium alerts, MAX_CRITICAL_PER_HOUR (15) as a safety net.
- Optional QUIET_HOURS_UTC=22-6 mutes medium alerts overnight (critical ones still arrive).
- The daily message says how many alerts were held back.

## Settings (environment variables)
SHOCK_PIPS_EURUSD (12), SHOCK_PIPS_USDJPY (15), SHOCK_PIPS_EURJPY (20), SHOCK_SIGMA (3), SURPRISE_PCT (5),
PRICE_CHECK_MIN (4), CAL_REFRESH_MIN (15), MAX_NORMAL_PER_HOUR, MAX_CRITICAL_PER_HOUR, QUIET_HOURS_UTC, TIMEZONE.
News sources are the FEEDS list at the top of eurusd_alerts.py: `"Name": ("https://feed-url", min_score, needs_relevance)`.

## Limits to know about
- Free price data: Twelve Data's free plan allows 800 calls a day; the bot uses about 600 on weekdays (2 calls per
  4-minute check, weekdays only). Sources disagree on whether free forex prices are live or delayed; `check` tells you.
- GitHub's scheduler can run a few minutes late. For about 1-minute timing, run `python eurusd_alerts.py --loop` on an
  always-on server (see eurusd-alerts.service for a Linux example). If you do, disable the GitHub workflow.
- Live Treasury yield levels are not available for free; a USD/JPY spike is the closest signal. The US Treasury's own
  press feed is not included because I could not confirm its address (add it to FEEDS if you find it).
- Some feed addresses (investingLive, Fed speeches, BLS, BoJ, Forex Factory news) are unconfirmed. A failing feed is skipped and
  logged. Run `check` to see which.
- state.json changes most runs, so the repo shows frequent "update state" commits. That is expected.

## Security
- Keys live only in GitHub Secrets (or a local .env that .gitignore keeps out of the repo). The bot scrubs keys from
  error messages before printing or sending them.
- Revoke a leaked token: Telegram @BotFather > /revoke; regenerate the data keys on their sites.
- You get a daily message; if it stops arriving, something broke.
