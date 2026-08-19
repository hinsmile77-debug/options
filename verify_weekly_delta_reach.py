r"""
검증 v2: 위클리 구독 창을 넓히면 델타 0.2~0.3 밴드에 닿는가 — 몇 레그를 더 넓혀야 하는가.

v1과 무엇이 다른가 — 왜 다시 만들었는가
  v1은 "행사가가 스팟에서 몇 포인트 떨어져 있는가"만으로 델타를 버킷 묶었다. 결과가
  스텝이 커질수록 델타가 다시 튀어오르는 등 물리적으로 말이 안 되는 곡선을 냈다(R^2 4개 중
  3개가 0.7 미만). 원인은 **잔존만기를 안 통제했기 때문**이다 — 월요일 아침(잔존만기 4~5일)의
  "스팟+30p"와 만기 당일(잔존만기 0일)의 "스팟+30p"는 옵션 물리학상 전혀 다른 델타를 갖는데,
  같은 칸에 섞여 중앙값이 요동쳤다.

  v2는 포인트 거리 대신 **Black-Scholes 표준화 거리**를 쓴다:

      z = ln(행사가/스팟) / (IV * sqrt(잔존만기_년))

  이건 옵션가격모형에서 델타를 실제로 결정하는 변수(d1)에 가깝다 — 잔존만기가 다른 관측치를
  같은 척도로 정렬해준다. `t_years`는 마흐디 자체 컨벤션(`mahdi/main.py:1808`,
  `t_years=(expiry-오늘).days/365`)과 맞췄다.

방법
  1. 목표 위클리(weekly_mon/weekly_thu)의 `option_analysis_1m` 레그 + 같은 분 스팟을 모은다
     (v1과 동일한 series→expiry 매핑).
  2. 각 레그의 IV·잔존만기로 z를 계산한다. IV<=0 이거나 잔존만기<=0(만기 당일 장마감 근처
     등 t_years가 0에 가까워 분모가 폭발하는 구간)인 레그는 제외한다.
  3. z를 0.1 단위로 반올림해 버킷 묶고(콜은 z>=0, 풋은 z<=0→절대값), 버킷별 |델타| 중앙값을
     구한다.
  4. ln(|델타|) ~ z 선형회귀로 |델타|=0.30, 0.20에 닿는 z를 역산한다.
  5. **그 z를 다시 "몇 포인트"로 환산**해야 실제로 몇 레그(2.5p 스텝)를 넓혀야 하는지 답이
     된다 — z는 무차원이라 그 자체로는 "레그 수"가 안 나온다. 환산에는 그 시리즈의
     **관측된 평균 IV·평균 잔존만기**(진입이 실제로 일어나는 화~목/금~월 구간 기준)를 쓴다:

      필요 포인트 거리 = z_target * IV_평균 * sqrt(잔존만기_평균) * 스팟_평균

실행 방법
  cd C:\Users\82108\PycharmProjects\options
  python verify_weekly_delta_reach.py --start 2026-07-01 --end 2026-08-18

  결과는 화면 표 + `weekly_delta_reach_v2_{start}_{end}.csv`.

한계
  - 여전히 외삽이다 — v1보다 이론적으로 타당한 척도를 쓴다는 것이지, 확정이 아니다.
  - z→포인트 환산에 쓰는 "평균 IV·평균 잔존만기"는 하루 중에도 변한다(스마일이 있으면
    행사가별 IV가 다르다 — 이 스크립트는 평균 IV 하나로 근사한다). 정밀한 답은 실제로
    그 레그를 수집해봐야 나온다.
  - IV<=0 또는 잔존만기<=0인 레그(만기 당일 마감 직전 등)는 계산에서 제외했다 — 그 구간은
    표준화 자체가 정의되지 않는다(분모가 0에 가까워짐).
"""

from __future__ import annotations

import argparse
import csv
import math
import os
from collections import defaultdict
from datetime import date, timedelta

import psycopg

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "mahdi")
DB_USER = os.environ.get("DB_USER", "mahdi")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "mahdi")

WEEKLY_SERIES = ("weekly_mon", "weekly_thu")
STRIKE_STEP = 2.5
Z_BUCKET_SIZE = 0.1
MIN_SAMPLES_PER_BUCKET = 20
TARGET_DELTAS = (0.30, 0.20)


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


def fetch_series_expiry_by_date(conn, start: date, end: date) -> dict[date, dict[str, date]]:
    """계산: 그날 관측된 각 위클리 시리즈의 만기일."""
    sql = """
        SELECT DISTINCT timestamp::date AS d, series, expiry
        FROM expiry_liquidity_1m
        WHERE underlying = 'KOSPI200' AND series = ANY(%s)
          AND timestamp::date BETWEEN %s AND %s
    """
    out: dict[date, dict[str, date]] = defaultdict(dict)
    with conn.cursor() as cur:
        cur.execute(sql, (list(WEEKLY_SERIES), start, end))
        for d, series, expiry in cur.fetchall():
            out[d][series] = expiry
    return out


def fetch_legs_with_spot(conn, start: date, end: date, expiry_list: list[date]) -> list[tuple]:
    """계산: 지정된 만기 목록에 속하는 옵션체인 레그(+ IV) + 같은 분의 기초자산 스팟.

    반환: [(timestamp, expiry, strike, option_type, delta, iv, spot), ...]
    """
    if not expiry_list:
        return []
    sql = """
        SELECT o.timestamp, o.expiry, o.strike, o.option_type, o.delta, o.iv, s.spot
        FROM option_analysis_1m o
        JOIN underlying_spot_1m s
          ON s.underlying = o.underlying AND s.timestamp = o.timestamp
        WHERE o.underlying = 'KOSPI200'
          AND o.expiry = ANY(%s)
          AND o.timestamp::date BETWEEN %s AND %s
          AND o.delta IS NOT NULL
          AND o.iv IS NOT NULL
          AND s.spot IS NOT NULL
    """
    with conn.cursor() as cur:
        cur.execute(sql, (expiry_list, start, end))
        return cur.fetchall()


def compute_z(row: tuple) -> tuple[str, float, float, float] | None:
    """계산: 표준화 거리 z = ln(strike/spot) / (iv*sqrt(t_years)).

    반환: (option_type, z, |delta|, strike-spot 포인트) 또는 계산 불가 시 None.
    """
    ts, expiry, strike, option_type, delta, iv, spot = row
    if spot is None or spot <= 0 or iv is None or iv <= 0:
        return None
    t_years = max((expiry - ts.date()).days, 0) / 365.0
    if t_years <= 0:
        return None  # 만기 당일 — 표준화 분모가 0에 가까워짐, 별도 취급 필요(0DTE)
    strike = float(strike)
    spot = float(spot)
    iv = float(iv)
    z = math.log(strike / spot) / (iv * math.sqrt(t_years))
    points = strike - spot
    return option_type, z, abs(float(delta)), points


def bucket_by_z(rows: list[tuple]) -> dict[str, dict[float, list[tuple[float, float]]]]:
    """계산: option_type별로 {z_bucket: [(|delta|, points), ...]} — OTM 방향만."""
    out: dict[str, dict[float, list[tuple[float, float]]]] = {"C": defaultdict(list), "P": defaultdict(list)}
    for row in rows:
        computed = compute_z(row)
        if computed is None:
            continue
        option_type, z, abs_delta, points = computed
        if option_type == "C" and z >= 0:
            bucket = round(z / Z_BUCKET_SIZE) * Z_BUCKET_SIZE
            out["C"][bucket].append((abs_delta, points))
        elif option_type == "P" and z <= 0:
            bucket = round(-z / Z_BUCKET_SIZE) * Z_BUCKET_SIZE
            out["P"][bucket].append((abs_delta, points))
    return out


def median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    if n == 0:
        return float("nan")
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def fit_log_linear(xs_in: list[float], ys_in: list[float]) -> tuple[float, float, float]:
    """계산: ln(y) = a + b*x 최소자승 적합. 반환: (a, b, r2)."""
    xs = list(xs_in)
    ys = [math.log(y) for y in ys_in]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    if sxx == 0:
        return mean_y, 0.0, 0.0
    b = sxy / sxx
    a = mean_y - b * mean_x
    pred = [a + b * x for x in xs]
    ss_res = sum((y - p) ** 2 for y, p in zip(ys, pred))
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return a, b, r2


def z_for_target_delta(a: float, b: float, target: float) -> float | None:
    if b >= 0:
        return None
    return (math.log(target) - a) / b


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default=None, help="YYYY-MM-DD (생략 시 --end 기준 45일 전)")
    parser.add_argument("--end", default=None, help="YYYY-MM-DD (생략 시 오늘)")
    args = parser.parse_args()
    if args.end is None:
        args.end = date.today().isoformat()
    if args.start is None:
        args.start = (date.fromisoformat(args.end) - timedelta(days=45)).isoformat()
    print(f"(인자 생략분 기본값 적용) start={args.start} end={args.end}")

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
    csv_rows = []

    with psycopg.connect(conninfo) as conn:
        expiry_by_date = fetch_series_expiry_by_date(conn, start, end)

        for series in WEEKLY_SERIES:
            expiries = sorted({d_exp[series] for d_exp in expiry_by_date.values() if series in d_exp})
            if not expiries:
                print(f"\n=== {series}: 관측된 만기가 없습니다 ===")
                continue
            rows = fetch_legs_with_spot(conn, start, end, expiries)
            buckets = bucket_by_z(rows)

            # z->포인트 환산용 평균 IV·평균 잔존만기·평균 스팟(유효 레그 전체 기준 근사)
            valid = [compute_z(r) for r in rows]
            valid = [v for v in valid if v is not None]
            avg_iv_by_type: dict[str, float] = {}
            avg_t_by_type: dict[str, float] = {}
            for option_type in ("C", "P"):
                ivs = []
                ts_years = []
                for row in rows:
                    ts, expiry, strike, o_type, delta, iv, spot = row
                    if o_type != option_type or iv is None or iv <= 0 or spot is None or spot <= 0:
                        continue
                    t_years = max((expiry - ts.date()).days, 0) / 365.0
                    if t_years <= 0:
                        continue
                    ivs.append(float(iv))
                    ts_years.append(t_years)
                avg_iv_by_type[option_type] = sum(ivs) / len(ivs) if ivs else float("nan")
                avg_t_by_type[option_type] = sum(ts_years) / len(ts_years) if ts_years else float("nan")
            avg_spot = sum(float(r[6]) for r in rows if r[6]) / len(rows) if rows else float("nan")

            print(f"\n=== {series} — 관측 만기 {len(expiries)}개, 레그 표본 {len(rows)}건 "
                  f"(t_years<=0 또는 IV<=0 제외 후 유효 {len(valid)}건) ===")

            for option_type, label in (("C", "콜(스팟 위쪽)"), ("P", "풋(스팟 아래쪽)")):
                bucket_data = buckets[option_type]
                bucket_medians = {
                    z: median([d for d, _p in items])
                    for z, items in bucket_data.items()
                    if len(items) >= MIN_SAMPLES_PER_BUCKET
                }
                print(f"  -- {label} (평균 IV={avg_iv_by_type[option_type]:.4f}, "
                      f"평균 잔존만기={avg_t_by_type[option_type]*365:.1f}일) --")
                if not bucket_medians:
                    print("     표본부족 — 회귀 불가")
                    continue
                for z in sorted(bucket_medians):
                    n = len(bucket_data[z])
                    print(f"     z={z:+.1f}  |델타| 중앙값 {bucket_medians[z]:.3f}  (표본 {n}건)")

                zs = sorted(bucket_medians)
                if len(zs) < 3:
                    print("     회귀 불가 — 버킷이 3개 미만")
                    continue
                medians_sorted = [bucket_medians[z] for z in zs]
                a, b, r2 = fit_log_linear(zs, medians_sorted)
                current_max_z = max(zs)
                print(f"     회귀: ln(|delta|) = {a:.3f} + {b:.4f}*z  (R^2={r2:.3f})")
                if r2 < 0.7:
                    print("     ⚠ R^2가 낮다 — 외삽 신뢰도가 낮으니 참고만 할 것")

                avg_iv = avg_iv_by_type[option_type]
                avg_t = avg_t_by_type[option_type]
                for target in TARGET_DELTAS:
                    needed_z = z_for_target_delta(a, b, target)
                    if needed_z is None or math.isnan(avg_iv) or math.isnan(avg_t):
                        print(f"     델타 {target:.2f} 도달 불가(회귀 기울기 문제 또는 IV/만기 결측)")
                        continue
                    # z -> 포인트 환산: strike = spot * exp(z * iv * sqrt(t))
                    needed_points = avg_spot * (math.exp(needed_z * avg_iv * math.sqrt(avg_t)) - 1)
                    current_max_points = avg_spot * (
                        math.exp(current_max_z * avg_iv * math.sqrt(avg_t)) - 1
                    )
                    extra_points = max(0.0, needed_points - current_max_points)
                    extra_legs = math.ceil(extra_points / STRIKE_STEP)
                    print(
                        f"     델타 {target:.2f} 도달 예상 z={needed_z:.2f} "
                        f"(≈스팟+{needed_points:.1f}p) → 현재 최대 z({current_max_z:.1f}, "
                        f"≈스팟+{current_max_points:.1f}p) 대비 "
                        f"**레그 {extra_legs}개 추가 필요(추정, 평균 IV·잔존만기 기준)**"
                    )
                    csv_rows.append(
                        {
                            "series": series,
                            "option_type": option_type,
                            "target_delta": target,
                            "avg_iv": round(avg_iv, 4),
                            "avg_t_days": round(avg_t * 365, 1),
                            "current_max_z": round(current_max_z, 2),
                            "current_max_points_est": round(current_max_points, 1),
                            "regression_a": round(a, 4),
                            "regression_b": round(b, 5),
                            "r_squared": round(r2, 4),
                            "needed_z": round(needed_z, 2),
                            "needed_points_est": round(needed_points, 1),
                            "extra_legs_estimate": extra_legs,
                            "sample_total": len(rows),
                        }
                    )

    if csv_rows:
        out_path = f"weekly_delta_reach_v2_{start}_{end}.csv"
        with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
            writer.writeheader()
            writer.writerows(csv_rows)
        print(f"\nCSV 저장: {out_path}")
    else:
        print("\n산출 가능한 결과가 없습니다.")


if __name__ == "__main__":
    main()
