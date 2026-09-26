# Broken Feeds — Follow-up Investigation

These sources were dropped from `sources.yaml` on 2026-04-28 because their RSS
URLs returned errors. Each is worth revisiting in a future session.

## Anthropic News
- Tried: `/news/rss.xml`, `/rss`, `/news/rss`, `/index.xml` — all 404
- Hypothesis: RSS may have been removed or moved to a different path
- Next step: web_fetch the news page, look for `<link rel="alternate" type="application/rss+xml">`
- Priority: HIGH — this is a key AI source

## NHK World English
- Tried: `/news/feeds/`, `/news/feeds/news.xml`, `/news/all.rss`
- Saw 403 Forbidden on the .rss URL — user-agent block likely
- Next step: try setting `feedparser.USER_AGENT = "Mozilla/5.0 ..."` before parse
- Priority: HIGH — your Japan signal source

## Nikkei Asia
- Tried: `/rss/feed/nikkei-news`, `/rss`, `/feed` — all 404
- Hypothesis: RSS may be paywall-gated or discontinued
- Next step: look for working RSS aggregators that cover Nikkei Asia
- Priority: LOW — Japan Times covers similar ground

## Speedhunters
- status=None on `/feed/` — likely network/SSL or Cloudflare block
- Next step: test with curl directly to see actual error
- Priority: LOW — The Drive covers cars sufficiently

## Resolved 2026-09-26 — Willamette Week, OPB News (HTTP 404 since 2026-05-04)

Both publishers moved to Arc XP; the old `/feed/` and `/news/feed/` paths return 404, which the old fetch logged as "malformed feed, no entries parsed" for 144 days. New URLs: `https://www.wweek.com/arc/outboundfeeds/rss/?outputType=xml` and `https://www.opb.org/arc/outboundfeeds/rss/category/news/?outputType=xml`. Fetch now logs the HTTP status, and a feed failing 3 runs in a row puts a "Dead feeds" banner on the brief.
