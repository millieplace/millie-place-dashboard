"""
밀리플레이스 대시보드 데이터 수집 스크립트

Superset에 로그인한 뒤, 지정된 4개 차트(UV / 혜택받기 / 직원확인 / 매장별 데이터)의
데이터를 가져와 docs/data.json 으로 저장한다.
GitHub Actions에서 하루 1회 실행되며, 결과 파일은 GitHub Pages가 서빙하는
docs/ 폴더에 저장되어 웹페이지가 바로 fetch 해서 사용할 수 있다.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

import requests

# Windows 콘솔(cp949)에서 이모지/특수문자 출력 시 죽는 문제 방지
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SUPERSET_BASE_URL = "https://superset.data.millie.co.kr"

# 트래킹할 차트 목록 (slice_id 기준)
CHARTS = {
    "uv": 3180,
    "benefit_claims": 3087,
    "staff_verifications": 3085,
    "store_breakdown": 3037,
    "daily_store": 3611,
    "store_demo": 3079,
}

# 사람이 읽을 한글 라벨 (웹페이지에서 그대로 사용)
LABELS = {
    "uv": "UV",
    "benefit_claims": "혜택받기",
    "staff_verifications": "직원확인",
    "store_breakdown": "매장별 데이터",
    "daily_store": "일별 매장 데이터",
    "store_demo": "매장별 혜택사용 구독자 Demo",
}


def get_access_token(session: requests.Session, username: str, password: str) -> str:
    """Superset에 로그인해서 access_token을 받아온다."""
    resp = session.post(
        f"{SUPERSET_BASE_URL}/api/v1/security/login",
        json={
            "username": username,
            "password": password,
            "provider": "db",
            "refresh": True,
        },
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError(f"로그인 응답에 access_token이 없습니다: {resp.text}")
    return token


def get_csrf_token(session: requests.Session, access_token: str) -> str:
    """CSRF 토큰을 받아온다 (세션 쿠키는 session 객체가 자동으로 유지)."""
    resp = session.get(
        f"{SUPERSET_BASE_URL}/api/v1/security/csrf_token/",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("result")


def guess_metric_label(query_context: dict):
    """query_context에서 실제 지표(metric)의 컬럼명을 추정한다."""
    try:
        metrics = query_context["queries"][0].get("metrics", [])
        if not metrics:
            return None
        m = metrics[0]
        if isinstance(m, str):
            return m
        if isinstance(m, dict):
            return m.get("label") or (m.get("column") or {}).get("column_name")
    except Exception:
        return None
    return None


def to_date_str(v) -> str:
    """Superset이 반환하는 시간 값(epoch ms/s 또는 문자열)을 YYYY-MM-DD로 변환."""
    if isinstance(v, (int, float)):
        try:
            ts = v / 1000 if v > 1e12 else v  # ms 단위면 초 단위로 변환
            return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
        except Exception:
            return str(v)
    if isinstance(v, str):
        return v[:10]
    return str(v)


def summarize_rows(data_rows: list, metric_label_guess) -> dict:
    """실제 반환된 row들을 보고 지표/라벨/날짜 컬럼을 판별해 합산 및 일별 시계열을 만든다."""
    if not data_rows:
        return {"total": None, "metric_key": None, "label_key": None, "time_key": None, "series": [], "rows": []}

    sample = data_rows[0]
    exclude_hints = ("date", "seq", "id", "time", "key", "month", "__")

    metric_key = None
    if metric_label_guess and isinstance(sample.get(metric_label_guess), (int, float)):
        metric_key = metric_label_guess

    if metric_key is None:
        candidates = [
            k for k, v in sample.items()
            if isinstance(v, (int, float)) and not any(h in k.lower() for h in exclude_hints)
        ]
        metric_key = candidates[0] if candidates else None

    # 날짜/시간 컬럼 추정
    time_key = None
    for k in sample.keys():
        if k == metric_key:
            continue
        if "date" in k.lower() or "time" in k.lower():
            time_key = k
            break
    if time_key is None:
        for k, v in sample.items():
            if k == metric_key:
                continue
            if isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}", v):
                time_key = k
                break

    # 라벨(매장명 등) 컬럼 추정: 'name'이 포함된 필드를 최우선으로,
    # seq/id/code 같은 코드성 필드는 최대한 피한다.
    label_candidates = [
        k for k, v in sample.items()
        if k not in (metric_key, time_key) and isinstance(v, str)
    ]
    preferred = [k for k in label_candidates if "name" in k.lower()]
    if preferred:
        label_key = preferred[0]
    else:
        non_code_candidates = [
            k for k in label_candidates
            if not any(h in k.lower() for h in ("seq", "id", "code", "key"))
        ]
        label_key = non_code_candidates[0] if non_code_candidates else (label_candidates[0] if label_candidates else None)

    total = None
    if metric_key:
        total = sum(float(r.get(metric_key) or 0) for r in data_rows)

    series = []
    if metric_key and time_key:
        agg = {}
        for r in data_rows:
            d_raw = r.get(time_key)
            if d_raw is None:
                continue
            d = to_date_str(d_raw)
            val = float(r.get(metric_key) or 0)
            agg[d] = agg.get(d, 0) + val
        series = sorted(
            [{"date": d, "value": v} for d, v in agg.items()],
            key=lambda x: x["date"],
        )

    return {
        "total": total,
        "metric_key": metric_key,
        "label_key": label_key,
        "time_key": time_key,
        "series": series,
        "rows": data_rows,
    }


def get_chart_query_context(session: requests.Session, access_token: str, chart_id: int) -> dict:
    """차트에 저장된 query_context(실제 쿼리 정의)를 가져온다."""
    resp = session.get(
        f"{SUPERSET_BASE_URL}/api/v1/chart/{chart_id}",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    result = resp.json().get("result", {})
    qc = result.get("query_context")
    if not qc:
        raise RuntimeError(
            "이 차트에는 저장된 query_context가 없습니다. "
            "Superset에서 해당 차트를 한 번 열어서 'Save' 해주면 생성됩니다."
        )
    return json.loads(qc)


def fetch_chart_data(
    session: requests.Session,
    access_token: str,
    csrf_token: str,
    chart_id: int,
    time_range_override: str = None,
):
    """저장된 query_context를 그대로(혹은 기간만 override해서) 사용해 데이터를 가져온다."""
    query_context = get_chart_query_context(session, access_token, chart_id)
    query_context.setdefault("result_format", "json")
    query_context.setdefault("result_type", "full")

    if time_range_override:
        for q in query_context.get("queries", []):
            q["time_range"] = time_range_override
            # 넓은 기간 백필 시 Superset의 기본 row_limit에 걸려 데이터가
            # 잘리는 것을 방지하기 위해 넉넉하게 override
            q["row_limit"] = max(int(q.get("row_limit") or 0), 100000)

    resp = session.post(
        f"{SUPERSET_BASE_URL}/api/v1/chart/data",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-CSRFToken": csrf_token,
            "Referer": SUPERSET_BASE_URL + "/",
        },
        json=query_context,
        timeout=60,
    )
    resp.raise_for_status()
    payload = resp.json()
    result = payload.get("result", [{}])[0]
    data_rows = result.get("data", [])
    metric_guess = guess_metric_label(query_context)
    return data_rows, metric_guess


TIME_SERIES_KEYS = ("uv", "benefit_claims", "staff_verifications")


def load_history(path: str) -> dict:
    """이전에 누적 저장된 일별 히스토리를 불러온다 (없으면 빈 dict)."""
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def merge_history(history: dict, key: str, series: list) -> None:
    """새로 가져온 일별 series를 히스토리에 upsert (같은 날짜는 최신 값으로 덮어씀)."""
    bucket = history.setdefault(key, {})
    for point in series:
        bucket[point["date"]] = point["value"]


def merge_raw_row_history(history: dict, rows: list, month_field_guess: str = "produce_month") -> None:
    """다차원(연령대/성별 등) 원본 row를 월별로 통째 누적한다.
    이번 fetch에 포함된 월은 통째로 교체하고, 포함되지 않은 과거 월은 유지한다."""
    fresh = {}
    for r in rows:
        month_raw = r.get(month_field_guess)
        if month_raw is None:
            # produce_month 필드가 없으면 날짜형으로 보이는 다른 필드를 찾아본다
            for k, v in r.items():
                if isinstance(v, (int, float)) and v > 1_000_000_000_000:
                    month_raw = v
                    break
        if month_raw is None:
            continue
        month = to_date_str(month_raw)[:7]
        fresh.setdefault(month, [])
        fresh[month].append(r)

    for month, bucket in fresh.items():
        history[month] = bucket


def merge_store_history(store_history: dict, rows: list, metric_key: str, label_key: str, division_filter: str = None) -> None:
    """매장별 데이터를 월별로 누적한다. 이번 fetch에 포함된 월은 통째로 교체하고,
    포함되지 않은 과거 월은 기존 값을 그대로 유지한다.
    division_filter가 주어지면 해당 division(예: '혜택사용_매장별')의 행만 집계한다."""
    fresh = {}
    for r in rows:
        if division_filter is not None and r.get("division") != division_filter:
            continue
        month_raw = r.get("produce_month")
        if month_raw is None:
            continue
        month = to_date_str(month_raw)[:7]
        name = r.get(label_key)
        val = float(r.get(metric_key) or 0)
        fresh.setdefault(month, {})
        fresh[month][name] = fresh[month].get(name, 0) + val

    for month, bucket in fresh.items():
        store_history[month] = bucket


def merge_daily_store_history(history: dict, rows: list, metric_key: str, label_key: str, time_key: str) -> None:
    """매장 x 날짜 데이터를 일별로 누적한다. 이번 fetch에 포함된 날짜는 통째로 교체."""
    fresh = {}
    for r in rows:
        raw_date = r.get(time_key)
        if raw_date is None:
            continue
        date = to_date_str(raw_date)
        name = r.get(label_key)
        val = float(r.get(metric_key) or 0)
        fresh.setdefault(date, {})
        fresh[date][name] = fresh[date].get(name, 0) + val

    for date, bucket in fresh.items():
        history[date] = bucket


def main():
    username = os.environ.get("SUPERSET_USERNAME")
    password = os.environ.get("SUPERSET_PASSWORD")
    backfill_range = os.environ.get("BACKFILL_TIME_RANGE", "").strip() or None

    if not username or not password:
        print("SUPERSET_USERNAME / SUPERSET_PASSWORD 환경변수가 설정되어 있지 않습니다.", file=sys.stderr)
        sys.exit(1)

    print("Superset 로그인 중...")
    session = requests.Session()
    access_token = get_access_token(session, username, password)

    print("CSRF 토큰 발급 중...")
    csrf_token = get_csrf_token(session, access_token)

    docs_dir = os.path.join(os.path.dirname(__file__), "docs")
    os.makedirs(docs_dir, exist_ok=True)
    history_path = os.path.join(docs_dir, "history.json")
    history = load_history(history_path)
    store_history_path = os.path.join(docs_dir, "store_history.json")
    store_history = load_history(store_history_path)
    benefit_store_history_path = os.path.join(docs_dir, "benefit_store_history.json")
    benefit_store_history = load_history(benefit_store_history_path)
    daily_store_history_path = os.path.join(docs_dir, "daily_store_history.json")
    daily_store_history = load_history(daily_store_history_path)
    store_demo_history_path = os.path.join(docs_dir, "store_demo_history.json")
    store_demo_history = load_history(store_demo_history_path)

    output = {
        "updated_at": datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S KST"),
        "metrics": {},
    }

    if backfill_range:
        print(f"⚠️  백필 모드: 기간을 '{backfill_range}' 로 강제 지정해서 가져옵니다.")

    for key, chart_id in CHARTS.items():
        print(f"[{LABELS[key]}] 차트({chart_id}) 데이터 가져오는 중...")
        try:
            data_rows, metric_guess = fetch_chart_data(
                session, access_token, csrf_token, chart_id, time_range_override=backfill_range
            )
            summary = summarize_rows(data_rows, metric_guess)

            if key in TIME_SERIES_KEYS and summary["series"]:
                merge_history(history, key, summary["series"])

            if key == "store_breakdown" and summary["metric_key"] and summary["label_key"]:
                merge_store_history(store_history, summary["rows"], summary["metric_key"], summary["label_key"])
                merge_store_history(
                    benefit_store_history, summary["rows"], summary["metric_key"], summary["label_key"],
                    division_filter="혜택사용_매장별",
                )

            if key == "daily_store" and summary["metric_key"] and summary["label_key"] and summary["time_key"]:
                merge_daily_store_history(
                    daily_store_history, summary["rows"], summary["metric_key"], summary["label_key"], summary["time_key"]
                )

            if key == "store_demo" and summary["rows"]:
                merge_raw_row_history(store_demo_history, summary["rows"])

            output["metrics"][key] = {
                "label": LABELS[key],
                "chart_id": chart_id,
                "total": summary["total"],
                "metric_key": summary["metric_key"],
                "label_key": summary["label_key"],
                "time_key": summary["time_key"],
                "series": summary["series"],
                "data": summary["rows"],
            }
        except Exception as e:
            body_snippet = ""
            resp_obj = getattr(e, "response", None)
            if resp_obj is not None:
                body_snippet = resp_obj.text[:500]
            print(f"  ⚠️  {LABELS[key]} 가져오기 실패: {e}", file=sys.stderr)
            output["metrics"][key] = {
                "label": LABELS[key],
                "chart_id": chart_id,
                "data": [],
                "error": str(e),
                "response_body": body_snippet,
            }

    # 누적된 전체 히스토리를 각 시계열 지표의 series로 교체 (월별/분기별 집계를 위해 필요)
    for key in TIME_SERIES_KEYS:
        if key in history and key in output["metrics"]:
            full_series = sorted(
                [{"date": d, "value": v} for d, v in history[key].items()],
                key=lambda x: x["date"],
            )
            output["metrics"][key]["series"] = full_series

    with open(history_path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

    with open(store_history_path, "w", encoding="utf-8") as f:
        json.dump(store_history, f, ensure_ascii=False, indent=2)

    with open(benefit_store_history_path, "w", encoding="utf-8") as f:
        json.dump(benefit_store_history, f, ensure_ascii=False, indent=2)

    with open(daily_store_history_path, "w", encoding="utf-8") as f:
        json.dump(daily_store_history, f, ensure_ascii=False, indent=2)

    with open(store_demo_history_path, "w", encoding="utf-8") as f:
        json.dump(store_demo_history, f, ensure_ascii=False, indent=2)

    if "store_breakdown" in output["metrics"]:
        output["metrics"]["store_breakdown"]["monthly_totals"] = store_history
        output["metrics"]["store_breakdown"]["benefit_monthly_totals"] = benefit_store_history

    if "daily_store" in output["metrics"]:
        output["metrics"]["daily_store"]["daily_totals"] = daily_store_history

    if "store_demo" in output["metrics"]:
        output["metrics"]["store_demo"]["monthly_rows"] = store_demo_history

    out_path = os.path.join(docs_dir, "data.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"완료! {out_path} 에 저장했습니다. (히스토리: {history_path})")


if __name__ == "__main__":
    main()
