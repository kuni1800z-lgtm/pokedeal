#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import asyncio
import json
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
STATE_FILE = BASE / "seen.json"
COND_NAMES = {1: "新品", 2: "未使用に近い", 3: "目立った傷なし",
              4: "やや傷あり", 5: "傷あり", 6: "状態が悪い"}

_cfg_notify: dict = {}


def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    cutoff = time.time() - 14 * 86400
    pruned = {k: v for k, v in state.items() if v > cutoff}
    STATE_FILE.write_text(json.dumps(pruned, ensure_ascii=False), encoding="utf-8")


def trimmed_median(prices, trim_ratio=0.1):
    prices = sorted(p for p in prices if p and p > 0)
    if not prices:
        return None
    if len(prices) >= 10:
        k = int(len(prices) * trim_ratio)
        prices = prices[k: len(prices) - k] or prices
    return int(statistics.median(prices))


async def mercari_search(merc, keyword, status, price_max=None, limit=60):
    res = await merc.search(
        keyword,
        sort_by=MP.SortBy.SORT_CREATED_TIME,
        sort_order=MP.SortOrder.ORDER_DESC,
        status=[status],
        price_max=price_max,
    )
    return res.items[:limit]


async def sold_prices(merc, keyword, price_max=None):
    items = await mercari_search(merc, keyword, MP.Status.STATUS_SOLD_OUT, price_max)
    return [it.real_price for it in items if it.real_price]


async def live_listings(merc, keyword, price_max=None):
    items = await mercari_search(merc, keyword, MP.Status.STATUS_ON_SALE, price_max)
    out = []
    for it in items:
        if it.real_price is None:
            continue
        out.append({
            "id": it.id_,
            "title": it.name,
            "price": it.real_price,
            "url": f"https://jp.mercari.com/item/{it.id_}",
            "condition_id": it.item_condition_id,
        })
    return out


def is_noise(title, cond_id, cfg, target):
    t = title or ""
    words = list(cfg.get("exclude_keywords", [])) + list(target.get("exclude_keywords") or [])
    for kw in words:
        if kw in t:
            return f"除外語『{kw}』"
    if cond_id in (cfg.get("mercari_exclude_condition_ids") or []):
        return f"状態『{COND_NAMES.get(cond_id, cond_id)}』"
    return None


def evaluate(ls, reference, cfg, target):
    if is_noise(ls["title"], ls["condition_id"], cfg, target):
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
            httpx.post(
                f"https://ntfy.sh/{topic}",
                data=body.encode("utf-8"),
                headers={"Title": f"Pokeca deal -{pct}%",
                         "Click": ls["url"], "Tags": "fire"},
                timeout=15,
            )
        except Exception as e:
            print(f"  (ntfy通知失敗: {e})")

    hook = os.environ.get("DISCORD_WEBHOOK") or _cfg_notify.get("discord_webhook_url")
    if hook:
        try:
            httpx.post(hook, json={"content": f"🔥 相場より約{pct}%安い\n{body}"[:1900]},
                       timeout=15)
        except Exception as e:
            print(f"  (Discord通知失敗: {e})")


async def run_once(merc, cfg, state):
    for target in cfg["targets"]:
        kw = target["keyword"]
        price_max = target.get("price_max")

        ref = target.get("reference_price")
        if not ref:
            try:
                sold = await sold_prices(merc, kw, price_max)
            except Exception as e:
                print(f"[{kw}] 売却取得エラー: {e}")
                continue
            if len(sold) < cfg.get("reference_min_samples", 5):
                print(f"[{kw}] 売却データ不足({len(sold)}件)→相場の手動指定推奨。スキップ")
                continue
            ref = trimmed_median(sold)

        thr = target.get("discount_pct") or cfg.get("default_discount_pct", 15)
        print(f"[{kw}] 相場 ¥{ref:,}（{thr}%以上安いと通知）")

        try:
            live = await live_listings(merc, kw, price_max)
        except Exception as e:
            print(f"[{kw}] 出品取得エラー: {e}")
            continue

        for ls in live:
            if ls["id"] in state:
                continue
            pct = evaluate(ls, ref, cfg, target)
            if pct is not None:
                notify(ls, ref, pct)
            state[ls["id"]] = time.time()
    save_state(state)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(BASE / "config.yaml"))
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    global _cfg_notify
    _cfg_notify = cfg
    state = load_state()
    merc = Mercapi()

    interval = cfg.get("poll_interval_sec", 120)
    jitter = cfg.get("jitter_sec", 60)

    while True:
        print(f"\n──── 巡回 {datetime.now():%H:%M:%S} ────")
        try:
            await run_once(merc, cfg, state)
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
