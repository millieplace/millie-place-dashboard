"""
밀리플레이스 대시보드 데이터 수집 스크립트

Superset에 로그인한 뒤, 지정된 4개 차트(UV / 혜택받기 / 직원확인 / 매장별 데이터)의
데이터를 가져와 docs/data.json 으로 저장한다.
GitHub Actions에서 하루 1회 실행되며, 결과 파일은 GitHub Pages가 서빙하는
docs/ 폴더에 저장되어 웹페이지가 바로 fetch 해서 사용할 수 있다.
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta

import requests

SUPERSET_BASE_URL = "https://superset.data.millie.co.kr"

# 트래킹할 차트 목록 (slice_id 기준)
CHARTS = {
    "uv": 3180,
    "benefit_claims": 3087,
    "staff_verifications": 3085,
    "store_breakdown": 3037,
}

# 사람이 읽을 한글 라벨 (웹페이지에서 그대로 사용)
LABELS = {
    "uv": "UV",
    "benefit_claims": "혜택받기",
    "staff_verifications": "직원확인",
    "store_breakdown": "매장별 데이터",
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


def summarize_rows(data_rows: list, metric_label_guess) -> dict:
    """실제 반환된 row들을 보고 어떤 컬럼이 진짜 지표인지, 어떤 컬럼이 라벨(매장명 등)인지 판별해 합산한다."""
    if not data_rows:
        return {"total": None, "metric_key": None, "label_key": None, "rows": []}

    sample = data_rows[0]
    exclude_hints = ("date", "seq", "id", "time", "key", "__")

    metric_key = None
    if metric_label_guess and isinstance(sample.get(metric_label_guess), (int, float)):
        metric_key = metric_label_guess

    if metric_key is None:
        candidates = [
            k for k, v in sample.items()
            if isinstance(v, (int, float)) and not any(h in k.lower() for h in exclude_hints)
        ]
        metric_key = candidates[0] if candidates else None

    label_key = None
    for k, v in sample.items():
        if k != metric_key and isinstance(v, str):
            label_key = k
            break

    total = None
    if metric_key:
        total = sum(float(r.get(metric_key) or 0) for r in data_rows)

    return {"total": total, "metric_key": metric_key, "label_key": label_key, "rows": data_rows}


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


def fetch_chart_data(session: requests.Session, access_token: str, csrf_token: str, chart_id: int):
    """저장된 query_context를 그대로 사용해 차트의 실제 데이터를 가져온다."""
    query_context = get_chart_query_context(session, access_token, chart_id)
    query_context.setdefault("result_format", "json")
    query_context.setdefault("result_type", "full")

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


def main():
    username = os.environ.get("SUPERSET_USERNAME")
    password = os.environ.get("SUPERSET_PASSWORD")

    if not username or not password:
        print("SUPERSET_USERNAME / SUPERSET_PASSWORD 환경변수가 설정되어 있지 않습니다.", file=sys.stderr)
        sys.exit(1)

    print("Superset 로그인 중...")
    session = requests.Session()
    access_token = get_access_token(session, username, password)

    print("CSRF 토큰 발급 중...")
    csrf_token = get_csrf_token(session, access_token)

    output = {
        "updated_at": datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S KST"),
        "metrics": {},
    }

    for key, chart_id in CHARTS.items():
        print(f"[{LABELS[key]}] 차트({chart_id}) 데이터 가져오는 중...")
        try:
            data_rows, metric_guess = fetch_chart_data(session, access_token, csrf_token, chart_id)
            summary = summarize_rows(data_rows, metric_guess)
            output["metrics"][key] = {
                "label": LABELS[key],
                "chart_id": chart_id,
                "total": summary["total"],
                "metric_key": summary["metric_key"],
                "label_key": summary["label_key"],
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

    out_path = os.path.join(os.path.dirname(__file__), "docs", "data.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"완료! {out_path} 에 저장했습니다.")


if __name__ == "__main__":
    main()
