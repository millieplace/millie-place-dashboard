"""
구독권 발급 리스트(관리자 엑셀 다운로드, HTML 형식 .xls) → docs/coupon_stats.json (집계만)

보안: 구독권 코드·회원번호 등 원본 행은 저장하지 않고, 날짜별 건수·비율만 내보낸다.
사용: python3 build_coupon_stats.py <export.xls> [<export2.xls> ...]
      (같은 구독권이 여러 파일에 있으면 코드 기준으로 중복 제거)
"""
import json, re, sys, os
from io import StringIO
from datetime import datetime
import pandas as pd

COLS = ['구독권번호', '구독권명', '쿠폰유효기간', '코드', '회원번호', '필명', '사용여부', '사용일', '등록일', '발급일']


def load_export(path):
    html = open(path, encoding='utf-8-sig').read()
    df = pd.read_html(StringIO(html))[0]
    if len(df.columns) == len(COLS):  # 헤더에 회원번호가 빠져 한 칸씩 밀려 있음
        df.columns = COLS
    for c in ['사용일', '등록일', '발급일']:
        df[c] = pd.to_datetime(df[c].astype(str).replace('0000-00-00 00:00:00', None), errors='coerce')
    df['회원번호'] = pd.to_numeric(df['회원번호'], errors='coerce').fillna(0).astype(int)
    return df


def short_label(name):
    m = re.search(r'\(([^)]*)\)', name)
    return m.group(1) if m else name


def build(paths, out_path, as_of=None):
    df = pd.concat([load_export(p) for p in paths]).drop_duplicates(subset=['코드'], keep='last')
    if not as_of:  # 파일명의 다운로드 시각(…_YYYYMMDDHHMMSS.xls) 우선
        stamps = [m.group(1) for p in paths for m in [re.search(r'(\d{14})', os.path.basename(p))] if m]
        as_of = datetime.strptime(max(stamps), '%Y%m%d%H%M%S').strftime('%Y-%m-%d %H:%M') if stamps else datetime.now().strftime('%Y-%m-%d %H:%M')
    coupons = []
    for (cid, name, valid), g in df.groupby(['구독권번호', '구독권명', '쿠폰유효기간']):
        reg = g[g['등록일'].notna()]
        used = g[g['사용여부'] == 'Y']
        lag = (used['사용일'] - used['등록일']).dt.total_seconds() / 86400
        buckets = {
            '당일': int((lag < 1).sum()),
            '1~3일': int(((lag >= 1) & (lag < 4)).sum()),
            '4~7일': int(((lag >= 4) & (lag < 8)).sum()),
            '8일 이상': int((lag >= 8).sum()),
        }
        members = reg[reg['회원번호'] > 0].groupby('회원번호').size()
        daily = {}
        for col, key in [('등록일', 'registered'), ('사용일', 'used')]:
            s = g[g[col].notna()][col].dt.strftime('%Y-%m-%d').value_counts()
            for d, n in s.items():
                daily.setdefault(d, {'registered': 0, 'used': 0})[key] = int(n)
        vs, ve = [x.strip()[:10] for x in valid.split('~')]
        coupons.append({
            'id': str(cid),
            'name': name,
            'label': short_label(name),
            'valid_from': vs, 'valid_to': ve,
            'issued_at': g['발급일'].min().strftime('%Y-%m-%d') if g['발급일'].notna().any() else None,
            'issued': int(len(g)),
            'registered': int(len(reg)),
            'used': int(len(used)),
            'registered_unused': int(len(reg) - len(used)),
            'members': int(len(members)),
            'multi_members': int((members > 1).sum()),
            'max_per_member': int(members.max()) if len(members) else 0,
            'first_registered': reg['등록일'].min().strftime('%Y-%m-%d') if len(reg) else None,
            'last_registered': reg['등록일'].max().strftime('%Y-%m-%d') if len(reg) else None,
            'lag_buckets': buckets,
            'lag_median_hours': round(float(lag.median() * 24), 1) if len(lag) else None,
            'daily': dict(sorted(daily.items())),
            'paid_converted': None,  # 유료 구독 전환 데이터 연동 예정
        })
    out = {'as_of': as_of, 'source_rows': int(len(df)), 'coupons': sorted(coupons, key=lambda c: c['issued_at'] or '', reverse=True)}
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    return out


if __name__ == '__main__':
    here = os.path.dirname(os.path.abspath(__file__))
    o = build(sys.argv[1:], os.path.join(here, 'docs', 'coupon_stats.json'))
    for c in o['coupons']:
        print(c['label'], '| 발급', c['issued'], '등록', c['registered'], '사용', c['used'], '| 회원', c['members'], '(2장+', c['multi_members'], ')')
