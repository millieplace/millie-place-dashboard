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


def get_access_token(username: str, password: str) -> str:
    """Superset에 로그인해서 access_token을 받아온다."""
    resp = requests.post(
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


def get_csrf_token(access_token: str) -> tuple[str, str]:
    """CSRF 토큰과 세션 쿠키를 받아온다 (POST 요청에 필요할 수 있음)."""
    resp = requests.get(
        f"{SUPERSET_BASE_URL}/api/v1/security/csrf_token/",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    resp.raise_for_status()
    csrf_token = resp.json().get("result")
    session_cookie = resp.cookies.get_dict()
    return csrf_token, session_cookie


def fetch_chart_data(access_token: str, csrf_token: str, chart_id: int) -> dict:
    """특정 차트의 실제 데이터를 가져온다 (Superset Explore 화면이 실제로 쓰는 방식과 동일)."""
    form_data = json.dumps({"slice_id": chart_id})
    resp = requests.post(
        f"{SUPERSET_BASE_URL}/api/v1/chart/data",
        params={"form_data": form_data},
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "X-CSRFToken": csrf_token,
            "Referer": SUPERSET_BASE_URL + "/",
        },
        json={},
        timeout=60,
    )
    resp.raise_for_status()
    payload = resp.json()
    result = payload.get("result", [{}])[0]
    return result.get("data", [])


def main():
    username = os.environ.get("SUPERSET_USERNAME")
    password = os.environ.get("SUPERSET_PASSWORD")

    if not username or not password:
        print("SUPERSET_USERNAME / SUPERSET_PASSWORD 환경변수가 설정되어 있지 않습니다.", file=sys.stderr)
        sys.exit(1)

    print("Superset 로그인 중...")
    access_token = get_access_token(username, password)

    print("CSRF 토큰 발급 중...")
    csrf_token, _ = get_csrf_token(access_token)

    output = {
        "updated_at": datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S KST"),
        "metrics": {},
    }

    for key, chart_id in CHARTS.items():
        print(f"[{LABELS[key]}] 차트({chart_id}) 데이터 가져오는 중...")
        try:
            data = fetch_chart_data(access_token, csrf_token, chart_id)
            output["metrics"][key] = {"label": LABELS[key], "chart_id": chart_id, "data": data}
        except Exception as e:
            print(f"  ⚠️  {LABELS[key]} 가져오기 실패: {e}", file=sys.stderr)
            output["metrics"][key] = {"label": LABELS[key], "chart_id": chart_id, "data": [], "error": str(e)}

    out_path = os.path.join(os.path.dirname(__file__), "docs", "data.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"완료! {out_path} 에 저장했습니다.")


if __name__ == "__main__":
    main()
