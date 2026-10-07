"""
제휴처 안내 페이지(docs/partner.html)용 가공 통계 생성.

- 원본 방문 수는 내보내지 않고, 순위·상위 %·비율·지수만 담는다.
- 입력: docs/data.json, docs/partner_store_info.json, docs/millieplace_banner_history.json, docs/place_seq_map.json
- 출력: docs/partner_stats.json
"""
import json
import os
from datetime import date, timedelta

ACTIVE_STATUSES = ("제휴 중", "10월 단기", "제휴예정")
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]
AGE_ORDER = ["10", "20", "30", "40", "50", "60"]
SIDO_NORM = {"서울특별시": "서울", "경기도": "경기", "강원도": "강원", "강원특별자치도": "강원", "경상남도": "경남", "경상북도": "경북",
             "전라남도": "전남", "전라북도": "전북", "전북특별자치도": "전북", "충청남도": "충남", "충청북도": "충북",
             "제주특별자치도": "제주", "제주도": "제주", "부산광역시": "부산", "대구광역시": "대구", "인천광역시": "인천",
             "광주광역시": "광주", "대전광역시": "대전", "울산광역시": "울산", "세종특별자치시": "세종"}


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _d(s):
    return date.fromisoformat(s)


def _rank(values: dict, name: str):
    """값이 큰 순서의 순위 (동점은 같은 순위). values: {name: value}"""
    if name not in values:
        return None
    v = values[name]
    rank = 1 + sum(1 for x in values.values() if x > v)
    total = len(values)
    return {"rank": rank, "total": total, "top_pct": round(rank / total * 100, 1)}


def _tier(top_pct):
    if top_pct is None:
        return None
    if top_pct <= 10:
        return "TOP 10%"
    if top_pct <= 25:
        return "TOP 25%"
    if top_pct <= 50:
        return "TOP 50%"
    return None


def _demo_shares(rows, metric_key):
    sex = {"F": 0.0, "M": 0.0}
    age = {a: 0.0 for a in AGE_ORDER}
    total = 0.0
    for r in rows:
        v = float(r.get(metric_key) or 0)
        if v <= 0:
            continue
        total += v
        s = r.get("sex")
        if s in sex:
            sex[s] += v
        a = str(r.get("age_band") or "")
        if a in age:
            age[a] += v
        elif a.isdigit() and int(a) >= 60:
            age["60"] += v
    if total <= 0:
        return None, 0
    return {
        "female": round(sex["F"] / total * 100, 1),
        "male": round(sex["M"] / total * 100, 1),
        "age": {a: round(age[a] / total * 100, 1) for a in AGE_ORDER},
    }, total


def build(docs_dir: str) -> dict:
    data = _load(os.path.join(docs_dir, "data.json"), {})
    metrics = data.get("metrics", {})
    info = _load(os.path.join(docs_dir, "partner_store_info.json"), {})
    for s in info.values():
        if s.get("sido"):
            s["sido"] = SIDO_NORM.get(s["sido"], s["sido"])
    banner_hist = _load(os.path.join(docs_dir, "millieplace_banner_history.json"), {})
    seq_map = _load(os.path.join(docs_dir, "place_seq_map.json"), {})

    uv_daily = (metrics.get("daily_store_uv") or {}).get("daily_totals") or {}
    dates = sorted(uv_daily.keys())
    if not dates:
        return {}
    latest = _d(dates[-1])

    def window_sum(days_back_start, days_back_end):
        """latest 기준 [start, end] 일 전 구간 합 (0 = latest)"""
        s = (latest - timedelta(days=days_back_end)).isoformat()
        e = (latest - timedelta(days=days_back_start)).isoformat()
        out = {}
        for d in dates:
            if s <= d <= e:
                for n, v in uv_daily[d].items():
                    out[n] = out.get(n, 0) + (v or 0)
        return out

    cur30 = window_sum(0, 29)
    prev30 = window_sum(30, 59)
    first_seen = {}
    for d in dates:
        for n, v in uv_daily[d].items():
            if v and n not in first_seen:
                first_seen[n] = d

    # 순위 대상: 제휴 중인 매장 중 실제 노출(노출일 또는 실제 트래픽)이 30일 구간 시작 전부터 있던 곳
    window_start = (latest - timedelta(days=29)).isoformat()
    pool = {}
    for n, s in info.items():
        if s.get("status") not in ACTIVE_STATUSES:
            continue
        exposed = s.get("expose") or first_seen.get(n)
        if not exposed or exposed > latest.isoformat():
            continue
        start = max(exposed, window_start)
        active_days = (latest - _d(start)).days + 1
        if active_days < 3:  # 노출 직후 2일 이내는 순위 산정 제외
            continue
        total = sum((uv_daily.get(d) or {}).get(n, 0) or 0 for d in dates if start <= d)
        pool[n] = total / active_days

    def group_rank(name, key_fn):
        k = key_fn(info.get(name, {}))
        if not k:
            return None
        sub = {n: v for n, v in pool.items() if key_fn(info.get(n, {})) == k}
        if len(sub) < 3:
            return None
        r = _rank(sub, name)
        if r:
            r["group"] = k
        return r

    # 주간 지수 (최근 12개 완결 주, 월~일)
    last_sunday = latest - timedelta(days=(latest.weekday() + 1) % 7)
    weeks = []
    for i in range(11, -1, -1):
        end = last_sunday - timedelta(days=7 * i)
        start = end - timedelta(days=6)
        weeks.append((start.isoformat(), end.isoformat()))

    # 데모 (최신 완결 월)
    demo_metric = metrics.get("store_demo") or {}
    demo_rows_by_month = demo_metric.get("monthly_rows") or {}
    demo_key = demo_metric.get("metric_key") or "SUM(회원수(유니크))"
    demo_month = sorted(demo_rows_by_month.keys())[-1] if demo_rows_by_month else None
    demo_all, _ = _demo_shares(demo_rows_by_month.get(demo_month, []), demo_key) if demo_month else (None, 0)

    # 배너 노출 이력 (place_seq → 매장명)
    meta = banner_hist.get("meta") or {}
    days_map = banner_hist.get("days") or {}
    featured = {}
    for img, m in meta.items():
        name = seq_map.get(str(m.get("place_seq"))) if m.get("place_seq") else None
        if not name:
            continue
        ds = sorted(d for d, v in days_map.items() if img in (v.get("images") or []))
        if ds:
            featured.setdefault(name, []).append({"start": ds[0], "end": ds[-1], "title": m.get("title", "")})

    stores = {}
    for name, s in info.items():
        if s.get("status") not in ACTIVE_STATUSES:
            continue
        entry = {
            "sido": s.get("sido"),
            "gugun": s.get("gugun"),
            "size": s.get("size"),
            "since": s.get("expose") or first_seen.get(name),
        }
        if name in pool:
            overall = _rank(pool, name)
            entry["rank"] = {
                "overall": overall,
                "sido": group_rank(name, lambda x: x.get("sido")),
                "gugun": group_rank(name, lambda x: f'{x.get("sido")} {x.get("gugun")}' if x.get("sido") and x.get("gugun") else None),
                "size": group_rank(name, lambda x: x.get("size")),
            }
            entry["tier"] = _tier(overall["top_pct"]) if overall and pool.get(name, 0) > 0 else None

            p = prev30.get(name, 0)
            entry["trend30_pct"] = round((cur30.get(name, 0) - p) / p * 100) if p >= 10 else None

            wk = []
            for ws, we in weeks:
                wk.append(sum((uv_daily.get(d) or {}).get(name, 0) or 0 for d in dates if ws <= d <= we))
            mx = max(wk) if wk else 0
            entry["weekly_index"] = [{"week": weeks[i][0], "index": round(v / mx * 100) if mx else 0} for i, v in enumerate(wk)]

            wd = [0.0] * 7
            start90 = (latest - timedelta(days=89)).isoformat()
            for d in dates:
                if d >= start90:
                    wd[_d(d).weekday()] += (uv_daily[d].get(name, 0) or 0)
            tot = sum(wd)
            entry["weekday_share"] = {WEEKDAYS[i]: round(v / tot * 100, 1) for i, v in enumerate(wd)} if tot >= 30 else None

        if demo_month:
            rows = [r for r in demo_rows_by_month.get(demo_month, []) if r.get("place_name") == name]
            shares, n = _demo_shares(rows, demo_key)
            if shares and n >= 10:
                entry["demo"] = shares

        if name in featured:
            entry["featured"] = sorted(featured[name], key=lambda x: x["start"], reverse=True)
        stores[name] = entry

    # 전체 개요 (밀리플레이스 전체 방문 — 공개용)
    uv_series = (metrics.get("uv") or {}).get("series") or []
    monthly = {}
    for p in uv_series:
        monthly[p["date"][:7]] = monthly.get(p["date"][:7], 0) + (p.get("value") or 0)
    last7 = sum(p["value"] for p in uv_series[-7:]) if uv_series else 0
    prev7 = sum(p["value"] for p in uv_series[-14:-7]) if uv_series else 0
    # 이번 달 1일~최신일 vs 전월 같은 기간
    mtd = None
    if uv_series:
        ld = _d(uv_series[-1]["date"])
        cur_start = ld.replace(day=1)
        prev_end_month = cur_start - timedelta(days=1)
        prev_start = prev_end_month.replace(day=1)
        prev_end = prev_start + timedelta(days=min(ld.day, prev_end_month.day) - 1)
        by = {p["date"]: p.get("value") or 0 for p in uv_series}
        rng = lambda a, b: sum(by.get((a + timedelta(days=i)).isoformat(), 0) for i in range((b - a).days + 1))
        mtd = {"month": cur_start.isoformat()[:7], "days": ld.day, "cur": rng(cur_start, ld), "prev": rng(prev_start, prev_end),
               "prev_month": prev_start.isoformat()[:7]}
    overview = {
        "mtd": mtd,
        "monthly": [{"month": m, "uv": monthly[m]} for m in sorted(monthly)][-13:],
        "last7": last7,
        "prev7": prev7,
        "total": sum(p.get("value") or 0 for p in uv_series),
        "latest_date": uv_series[-1]["date"] if uv_series else None,
        "active_partners": len(pool),
        "regions": sorted({s.get("sido") for s in info.values() if s.get("sido")}),
    }

    return {
        "generated_at": data.get("updated_at"),
        "window": {"start": window_start, "end": latest.isoformat(), "days": 30},
        "demo_month": demo_month,
        "demo_all": demo_all,
        "overview": overview,
        "stores": stores,
    }


def write(docs_dir: str) -> int:
    stats = build(docs_dir)
    if not stats:
        return 0
    with open(os.path.join(docs_dir, "partner_stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, separators=(",", ":"))
    return len(stats.get("stores", {}))


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    print("partner_stats stores:", write(os.path.join(here, "docs")))
