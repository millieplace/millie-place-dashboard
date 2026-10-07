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


# 밀리플레이스 앱 공식 지역 구분 (주소 도로명 기반 근사 분류)
YEONNAM_ROADS = ("연남로", "성미산로", "동교로", "월드컵북로")


def area_of(s):
    sido, gugun, addr = s.get("sido") or "", s.get("gugun") or "", s.get("address") or ""
    if sido == "부산":
        return "부산"
    if sido == "제주":
        return "제주"
    if sido == "경기":
        if "고양" in gugun or "일산" in addr:
            return "고양/일산"
        if "분당" in addr or "판교" in addr:
            return "분당/판교"
        return None
    if sido != "서울":
        return None
    if gugun == "서대문구":
        return "연남/서대문"
    if gugun == "마포구":
        return "연남/서대문" if any(r in addr for r in YEONNAM_ROADS) else "망원/합정"
    return {"종로구": "종로/광화문", "용산구": "용산/한남/이태원", "강남구": "강남/역삼", "성동구": "성수"}.get(gugun)


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

    # 순위 대상: 제휴 중 매장. 구간 내 노출 이후 일평균 방문자로 비교 (신규 매장 불이익 방지)
    def build_pool(w_start, w_end):
        out = {}
        for n, s in info.items():
            if s.get("status") not in ACTIVE_STATUSES:
                continue
            exposed = s.get("expose") or first_seen.get(n)
            if not exposed or exposed > w_end:
                continue
            start = max(exposed, w_start)
            active_days = (_d(w_end) - _d(start)).days + 1
            if active_days < 3:  # 노출 직후 2일 이내는 순위 산정 제외
                continue
            total = sum((uv_daily.get(d) or {}).get(n, 0) or 0 for d in dates if start <= d <= w_end)
            out[n] = total / active_days
        return out

    window_start = (latest - timedelta(days=29)).isoformat()
    pool = build_pool(window_start, latest.isoformat())
    prev_pool = build_pool((latest - timedelta(days=59)).isoformat(), (latest - timedelta(days=30)).isoformat())

    def group_rank(name, key_fn, min_size=3):
        k = key_fn(info.get(name, {}))
        if not k:
            return None
        sub = {n: v for n, v in pool.items() if key_fn(info.get(n, {})) == k}
        if len(sub) < min_size:
            return None
        r = _rank(sub, name)
        if r:
            r["group"] = k
            psub = {n: v for n, v in prev_pool.items() if key_fn(info.get(n, {})) == k}
            pr = _rank(psub, name) if len(psub) >= min_size else None
            r["prev_rank"] = pr["rank"] if pr else None
        return r

    # 주간 지수 (최근 12개 완결 주, 월~일)
    last_sunday = latest - timedelta(days=(latest.weekday() + 1) % 7)
    weeks = []
    for i in range(11, -1, -1):
        end = last_sunday - timedelta(days=7 * i)
        start = end - timedelta(days=6)
        weeks.append((start.isoformat(), end.isoformat()))

    flow_dates = [(latest - timedelta(days=i)).isoformat() for i in range(90, -1, -1)]

    def flow_index(getter, since=None):
        vals = [getter(d) if (not since or d >= since) else None for d in flow_dates]
        ma = []
        for i in range(len(vals)):
            win = [v for v in vals[max(0, i - 6): i + 1] if v is not None]
            ma.append(sum(win) / len(win) if len(win) >= 4 else None)
        base = [v for v in ma if v is not None]
        avg = sum(base) / len(base) if base else 0
        if avg <= 0:
            return None
        return [round(v / avg * 100) if v is not None else None for v in ma]

    pool_names = set(pool)
    overall_flow = flow_index(lambda d: sum(v or 0 for n, v in (uv_daily.get(d) or {}).items() if n in pool_names))

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
            "area": area_of(s),
            "since": s.get("expose") or first_seen.get(name),
        }
        if name in pool:
            overall = group_rank(name, lambda x: "전체")
            entry["rank"] = {
                "area": group_rank(name, area_of, min_size=2),
                "sido": group_rank(name, lambda x: x.get("sido")),
                "size": group_rank(name, lambda x: x.get("size")),
                "overall": overall,
            }
            entry["tier"] = _tier(overall["top_pct"]) if overall and pool.get(name, 0) > 0 else None

            p = prev30.get(name, 0)
            entry["trend30_pct"] = round((cur30.get(name, 0) - p) / p * 100) if p >= 10 else None

            entry["flow"] = flow_index(lambda d: (uv_daily.get(d) or {}).get(name, 0) or 0, since=entry["since"])

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
        "flow_dates": flow_dates,
        "flow_all": overall_flow,
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
