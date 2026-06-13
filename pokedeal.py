#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import asyncio
import os
import random
import statistics
import time
from datetime import datetime
from pathlib import Path

import yaml
import httpx

from mercapi import Mercapi
from mercapi.requests.search import SearchRequestData as MP

BASE = Path(__file__).resolve().parent
COND_NAMES = {1: "新品", 2: "未使用に近い", 3: "目立った傷なし",
              4: "やや傷あり", 5: "傷あり", 6: "状態が悪い"}

_cfg_notify: dict = {}


def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def trimmed_median(prices, trim_ratio=0.1):
    prices = sorted(p for p in prices if p and p > 0)
    if not prices:
        return None
    if len(prices) >= 10:
        k = int(len(prices) * trim_ratio)
        prices = prices[k: len(prices) - k] or prices
    return int(statistics.median(prices))


def title_ok(title, cfg, target):
    t = title or ""
    excl = list(cfg.get("exclude_keywords", [])) + list(target.get("exclude_keywords") or [])
    for kw in excl:
        if kw and kw in t:
            return False
    req = []
    if cfg.get("strict_keyword", True):
        req += target["keyword"].split()
    req += list(cfg.get("must_include", [])) + list(target.get("must_include") or [])
    for kw in req:
        if kw and kw not in t:
            return False
    return True


def cond_excluded(cond_id, cfg):
    return cond_id in (cfg.get("mercari_exclude_condition_ids") or [])


def age_minutes(created):
    if not created:
        return 1e9
    return (datetime.now() - created).total_seconds() / 60.0


async def mercari_search(merc, keyword, status, price_max=None, limit=60):
    res = await merc.search(
        keyword,
        sort_by=MP.SortBy.SORT_CREATED_TIME,
        sort_order=MP.SortOrder.ORDER_DESC,
        status=[status],
        price_max=price_max,
    )
    return res.items[:limit]


async def sold_prices(merc, cfg, target, price_max=None):
    items = await mercari_search(merc, target["keyword"], MP.Status.STATUS_SOLD_OUT, price_max)
    out = []
    for it in items:
        if it.real_price is None:
            continue
        if not title_ok(it.name, cfg, target):
            continue
        if cond_excluded(it.item_condition_id, cfg):
            continue
        out.append(it.real_price)
    return out


async def live_listings(merc, cfg, target, price_max=None):
    items = await mercari_search(merc, target["keyword"], MP.Status.STATUS_ON_SALE, price_max)
    out = []
    for it in items:
        if it.real_price is None:
            continue
        out.append({
            "title": it.name,
            "price": it.real_price,
            "url": f"https://jp.mercari.com/item/{it.id_}",
            "condition_id": it.item_condition_id,
            "age_min": age_minutes(it.created),
        })
    return out


def evaluate(ls, reference, cfg, target):
    if not title_ok(ls["title"], cfg, target):
        return None
    if cond_excluded(ls["condition_id"], cfg):
        return None
    if not reference:
        return None
    pct = round((reference - ls["price"]) / reference * 100)
    threshold = target.get("discount_pct") or cfg.get("default_discount_pct", 15)
    return pct if pct >= threshold else None


def notify(ls, reference, pct):
    cond = f"｜{COND_NAMES.get(ls['condition_id'], '')}" if ls["condition_id"] else ""
    body = (f"{ls['title']}{cond}\n"
            f"¥{ls['price']:,}（相場 ¥{reference:,} / 約{pct}%安い）\n"
            f"{ls['url']}\n"
            f"※偽物・状態難の可能性あり。説明と出品者評価を必ず確認。")
    print("\n" + "=" * 56 + f"\n🔥 相場より約{pct}%安い\n{body}\n" + "=" * 56)

    topic = os.environ.get("NTFY_TOPIC") or _cfg_notify.get("ntfy_topic")
    if topic:
        try:
            httpx.post(f"https://ntfy.sh/{topic}",
                       data=body.encode("utf-8"),
                       headers={"Title": f"Pokeca deal -{pct}%",
                                "Click": ls["url"], "Tags": "fire"},
                       timeout=15)
        except Exception as e:
            print(f"  (ntfy通知失敗: {e})")

    hook = os.environ.get("DISCORD_WEBHOOK") or _cfg_notify.get("discord_webhook_url")
    if hook:
        try:
            httpx.post(hook, json={"content": f"🔥 相場より約{pct}%安い\n{body}"[:1900]},
                       timeout=15)
        except Exception as e:
            print(f"  (Discord通知失敗: {e})")


async def run_once(merc, cfg):
    recent = cfg.get("recent_minutes", 30)
    for target in cfg["targets"]:
        kw = target["keyword"]
        price_max = target.get("price_max")

        ref = target.get("reference_price")
        if not ref:
            try:
                sold = await sold_prices(merc, cfg, target, price_max)
            except Exception as e:
                print(f"[{kw}] 売却取得エラー: {e}")
                continue
            if len(sold) < cfg.get("reference_min_samples", 5):
                print(f"[{kw}] 一致する売却データ不足({len(sold)}件)→相場の手動指定推奨。スキップ")
                continue
            ref = trimmed_median(sold)

        thr = target.get("discount_pct") or cfg.get("default_discount_pct", 15)
        print(f"[{kw}] 相場 ¥{ref:,}（{thr}%以上安い & 直近{recent}分の出品を通知）")

        try:
            live = await live_listings(merc, cfg, target, price_max)
        except Exception as e:
            print(f"[{kw}] 出品取得エラー: {e}")
            continue

        for ls in live:
            if ls["age_min"] > recent:
                continue
            pct = evaluate(ls, ref, cfg, target)
            if pct is not None:
                notify(ls, ref, pct)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(BASE / "config.yaml"))
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    global _cfg_notify
    _cfg_notify = cfg
    merc = Mercapi()

    interval = cfg.get("poll_interval_sec", 120)
    jitter = cfg.get("jitter_sec", 60)

    while True:
        print(f"\n──── 巡回 {datetime.now():%H:%M:%S} ────")
        try:
            await run_once(merc, cfg)
        except Exception as e:
            print(f"巡回エラー: {e}")
        if args.once:
            break
        wait = interval + random.uniform(0, jitter)
        print(f"次の巡回まで {wait:.0f} 秒…")
        time.sleep(wait)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n終了しました。")
