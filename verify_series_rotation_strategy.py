r"""
검증: 요일에 따라 목표 종목(만기북)을 바꾸는 전략의 전제 조건이 실제 데이터로 성립하는가.

가설
  - 화~목요일 : 위클리 목(weekly_thu) 거래량이 다른 두 북보다 우위
  - 금·월요일 : 위클리 월(weekly_mon) 거래량이 다른 두 북보다 우위
  - 먼슬리 마지막주(화~목) : 먼슬리(regular) 거래량이 다른 두 북보다 우위
  - 어느 북이든 "오늘이 그 북의 만기일"이면, 요일 규칙과 무관하게 그 북이 최우선

2026-07-04~08-18 1차 실행(45일 인자 없음 기준 30거래일) 결과 28/30(93.3%) 일치, 놓친 2일:
  - 08-10(월, 먼슬리 만기주 첫날): 아직 weekly_mon이 우세 — 먼슬리 쏠림은 화요일부터 시작됨
    을 확인. "마지막주" 판정을 월요일 포함에서 화~목만으로 좁힘.
  - 08-18(화): 위클리 월 만기가 08-17(대체공휴일)에서 화요일로 밀려, 만기 당일 거래량이
    요일 규칙을 눌렀다. "만기 당일 최우선" 규칙을 신설해 반영.

데이터 원천
  expiry_liquidity_1m 테이블. `series`(regular|weekly_mon|weekly_thu)와 `volume`이
  이미 함께 적재돼 있다(마흐디가 10분마다 ATM±2 구간을 스캔해 그 분의 체결거래량 합을 남김).
  하루 중 표본 수가 적을 수 있으므로(장중 유동성 스캔 슬롯) "그날의 대표 거래량"은
  일자별 volume의 **합계**로 잡는다(평균보다 그날 전체 활동량을 더 잘 반영).

실행 방법 (사용자 컴퓨터, 마흐디가 도는 그 환경에서)
  cd C:\Users\82108\PycharmProjects\options
  python verify_series_rotation_strategy.py --start 2026-07-01 --end 2026-08-18

  결과는 화면에 표로 찍히고, 같은 폴더에 `series_rotation_check_{start}_{end}.csv`로도 남는다.
  그 CSV(또는 화면 출력 전체)를 다시 대화에 붙여주면 해석을 이어간다.

주의
  - `expiry_liquidity_1m`은 10분 슬롯(홀수분 1/3/5, 세 북이 돌아가며)이라 하루 표본이
    수십 건 수준이다. 통계적으로 확정하기엔 얇을 수 있다 — 이 스크립트는 "며칠간 그런
    경향이 보이는가"를 먼저 확인하는 1차 스크리닝이다.
  - `regular`(먼슬리)의 만기가 언제인지는 DB에 있는 그 시리즈의 `expiry` 값을 그대로 쓴다
    (달력 규칙을 별도로 가정하지 않는다 — 마흐디 자신이 관측한 값이 정답이다).
"""

from __future__ import annotations

import argparse
import csv
import os
from collections import defaultdict
from datetime import date, timedelta

import psycopg

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "mahdi")
DB_USER = os.environ.get("DB_USER", "mahdi")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "mahdi")

SERIES = ("regular", "weekly_mon", "weekly_thu")
WEEKDAY_NAME = ["월", "화", "수", "목", "금", "토", "일"]


def load_dotenv_if_present() -> None:
    """리포 루트의 .env가 있으면 읽어 DB_* 환경변수를 채운다(이미 os.environ에 있으면 덮지 않음)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("DB_") and key not in os.environ:
                os.environ[key] = value.strip()


def fetch_daily_series_volume(conn, start: date, end: date) -> dict[date, dict[str, float]]:
    """계산: 날짜별 · series별 그날 volume 합계. 반환: {날짜: {series: 합계볼륨}}."""
    sql = """
        SELECT timestamp::date AS d, series, COALESCE(SUM(volume), 0) AS total_volume,
               COUNT(*) AS samples
        FROM expiry_liquidity_1m
        WHERE underlying = 'KOSPI200'
          AND timestamp::date BETWEEN %s AND %s
          AND series = ANY(%s)
        GROUP BY d, series
        ORDER BY d, series
    """
    out: dict[date, dict[str, float]] = defaultdict(dict)
    samples: dict[date, dict[str, int]] = defaultdict(dict)
    with conn.cursor() as cur:
        cur.execute(sql, (start, end, list(SERIES)))
        for d, series, total_volume, cnt in cur.fetchall():
            out[d][series] = float(total_volume)
            samples[d][series] = int(cnt)
    return out, samples


def fetch_expiry_by_date(conn, start: date, end: date) -> dict[date, dict[str, date]]:
    """계산: 그날 관측된 각 시리즈(regular/weekly_mon/weekly_thu)의 만기일.

    한 북이 관측 안 된 날은 그 북의 키가 빠진다(휴장·이월 등으로 그날 그 북 자체가 없었다는 뜻).
    """
    sql = """
        SELECT DISTINCT timestamp::date AS d, series, expiry
        FROM expiry_liquidity_1m
        WHERE underlying = 'KOSPI200' AND series = ANY(%s)
          AND timestamp::date BETWEEN %s AND %s
        ORDER BY d
    """
    out: dict[date, dict[str, date]] = defaultdict(dict)
    with conn.cursor() as cur:
        cur.execute(sql, (list(SERIES), start, end))
        for d, series, expiry in cur.fetchall():
            out[d][series] = expiry
    return out


def target_series_for(d: date, expiries: dict[str, date]) -> str:
    """계산: 사용자 가설의 요일 규칙에 따라 그날의 "목표 종목"을 정한다.

    우선순위(위가 이긴다):
      1. 만기 당일 최우선 — 오늘이 그 북의 만기일이면 요일과 무관하게 그 북.
         (08-18 미스 원인: 위클리 월 만기가 대체공휴일로 화요일로 밀렸는데 그날 거래량이
          다른 두 북을 압도했다 — 만기 당일은 요일 규칙보다 강한 별개의 힘이다.)
      2. 먼슬리 마지막주, 단 **화~목만** — regular 만기가 속한 주라도 월요일은 아직
         weekly_mon이 우세하다(08-10 실측: regular 21,800 vs weekly_mon 768,915).
         쏠림은 화요일부터 시작해 만기일(주로 목)에 정점을 찍는다.
      3. 기본 요일 규칙 — 화수목=weekly_thu, 금·월=weekly_mon.
    """
    weekday = d.weekday()  # 0=월 ... 4=금 5=토 6=일

    for series in SERIES:
        if expiries.get(series) == d:
            return series  # 만기 당일 최우선

    regular_expiry = expiries.get("regular")
    if (
        regular_expiry is not None
        and d.isocalendar()[:2] == regular_expiry.isocalendar()[:2]
        and weekday in (1, 2, 3)  # 화수목만 — 월요일 제외
    ):
        return "regular"

    if weekday in (1, 2, 3):  # 화수목
        return "weekly_thu"
    if weekday in (4, 0):  # 금월
        return "weekly_mon"
    return "n/a"  # 주말 등 — 거래일이 아니면 애초에 데이터가 없다


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--start", default=None,
        help="YYYY-MM-DD (생략 시 --end 기준 45일 전)",
    )
    parser.add_argument(
        "--end", default=None,
        help="YYYY-MM-DD (생략 시 오늘)",
    )
    args = parser.parse_args()
    if args.end is None:
        args.end = date.today().isoformat()
    if args.start is None:
        args.start = (date.fromisoformat(args.end) - timedelta(days=45)).isoformat()
    print(f"(인자 생략분 기본값 적용 — 필요하면 --start/--end로 직접 지정) start={args.start} end={args.end}")

    load_dotenv_if_present()
    global DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
    DB_HOST = os.environ.get("DB_HOST", DB_HOST)
    DB_PORT = os.environ.get("DB_PORT", DB_PORT)
    DB_NAME = os.environ.get("DB_NAME", DB_NAME)
    DB_USER = os.environ.get("DB_USER", DB_USER)
    DB_PASSWORD = os.environ.get("DB_PASSWORD", DB_PASSWORD)

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    conninfo = f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} user={DB_USER} password={DB_PASSWORD}"
    with psycopg.connect(conninfo) as conn:
        daily_volume, daily_samples = fetch_daily_series_volume(conn, start, end)
        expiry_by_date = fetch_expiry_by_date(conn, start, end)

    rows = []
    hit = 0
    total = 0
    for d in sorted(daily_volume.keys()):
        vols = daily_volume[d]
        if not any(vols.get(s, 0) > 0 for s in SERIES):
            continue  # 그날 세 북 다 볼륨 0 — 비교 불가(휴장/데이터 없음)
        target = target_series_for(d, expiry_by_date.get(d, {}))
        if target == "n/a":
            continue
        ranked = sorted(SERIES, key=lambda s: vols.get(s, 0.0), reverse=True)
        actual_top = ranked[0]
        is_hit = actual_top == target
        total += 1
        hit += int(is_hit)
        rows.append(
            {
                "date": d.isoformat(),
                "weekday": WEEKDAY_NAME[d.weekday()],
                "target_series": target,
                "actual_top_series": actual_top,
                "match": "O" if is_hit else "X",
                "regular_volume": round(vols.get("regular", 0.0), 1),
                "weekly_mon_volume": round(vols.get("weekly_mon", 0.0), 1),
                "weekly_thu_volume": round(vols.get("weekly_thu", 0.0), 1),
                "samples_regular": daily_samples[d].get("regular", 0),
                "samples_weekly_mon": daily_samples[d].get("weekly_mon", 0),
                "samples_weekly_thu": daily_samples[d].get("weekly_thu", 0),
                "regular_expiry": expiry_by_date.get(d, {}).get("regular", ""),
                "weekly_mon_expiry": expiry_by_date.get(d, {}).get("weekly_mon", ""),
                "weekly_thu_expiry": expiry_by_date.get(d, {}).get("weekly_thu", ""),
                "is_expiry_day_override": (
                    "O" if any(expiry_by_date.get(d, {}).get(s) == d for s in SERIES) else ""
                ),
            }
        )

    header = list(rows[0].keys()) if rows else []
    print(f"조사 구간: {start} ~ {end} · 비교 가능 거래일 {total}일")
    print("-" * 100)
    print(
        f"{'날짜':10} {'요일':4} {'목표':10} {'실제1위':10} {'일치':4} "
        f"{'regular':>10} {'weekly_mon':>10} {'weekly_thu':>10}"
    )
    for r in rows:
        print(
            f"{r['date']:10} {r['weekday']:4} {r['target_series']:10} {r['actual_top_series']:10} "
            f"{r['match']:4} {r['regular_volume']:>10} {r['weekly_mon_volume']:>10} {r['weekly_thu_volume']:>10}"
        )
    print("-" * 100)
    if total:
        print(f"일치율: {hit}/{total} = {hit / total * 100:.1f}%")
    else:
        print("비교 가능한 거래일이 없습니다 — 구간을 넓혀 다시 실행해 주세요.")

    if rows:
        out_path = f"series_rotation_check_{start}_{end}.csv"
        with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=header)
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nCSV 저장: {out_path}")


if __name__ == "__main__":
    main()
