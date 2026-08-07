"""진입 판단의 **사후 평가** (2026-08-06 고도화#5).

## 왜

08-05 `p1`이 팔레트를 연 뒤 ENTER가 0 → 62건이 됐는데, **그 62건이 옳았는지 재는 지표가 하나도
없다.** ADVISORY 전용이라 손익은 없지만 진입 시점의 기초자산과 이후 궤적은 이미 DB에 있다.

실거래 전환일에 "이전보다 나아졌는가"를 물으려면 그 전의 기준선이 있어야 한다. ADVISORY라는
이유로 미루면 전환 시점에 비교할 것이 없다.

## 무엇을 재는가 — 그리고 무엇을 안 재는가

재는 것은 **방향 적중률**뿐이다: 판단이 위라고 했는데 실제로 올랐는가. 손익이 아니다 —
사이징·체결·수수료가 전부 없는 상태에서 손익을 흉내 내면 그 숫자가 곧 거짓 기준선이 된다.

**무변동(이동 0)은 적중도 실패도 아니다.** NULL로 두고 분모에서 뺀다. 0을 실패로 세면
조용한 장에서 적중률이 구조적으로 낮아지고, 성공으로 세면 반대가 된다.

## 되먹임이 아니다

이 값으로 가중치를 바꾸지 않는다. 성과 기반 배분은 Thompson Sampling(v6 §11.3, Phase 3)의
몫이고, 그 전에 "무엇을 성과로 볼 것인가"부터 며칠 쌓아 사람이 정해야 한다.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from mahdi.data.db import ConnectionLike

logger = logging.getLogger("mahdi.ops.decision_outcomes")

# 평가 지평(분). 세 개인 이유: 하나면 그 지평에 우연히 맞은 것과 구분이 안 된다.
#
# 5/15/30의 근거는 v6 §13.3의 청산 레이어 시간 스케일이다 — 대부분의 청산 판단이 이 범위 안에서
# 일어나므로, 진입 방향이 그 안에서 유효했는지가 곧 "그 판단이 쓸모 있었는가"에 가장 가깝다.
HORIZON_MINUTES = (5, 15, 30)


def compute(conn: ConnectionLike, target: date, underlying: str = "KOSPI200") -> int:
    """
    입력: DB 커넥션, 대상 날짜.
    계산: 그날 `decision='ENTER'`인 판단마다 진입 시점 스팟과 +5/+15/+30분 스팟을 붙여
         `decision_outcomes`에 upsert한다. 반환: 기록한 행 수.
    해석: 스팟은 `underlying_spot_1m`의 **정확히 그 분** 값을 쓴다(가장 가까운 값으로 보간하지
         않는다) — 없으면 NULL이다. 보간하면 결손이 숫자로 메워져 "그날 스팟이 얼마나 비었나"가
         사라지고, 그 결손 자체가 08-05·08-06에 반복해서 문제였다.
    실패 조건: 테이블이 없으면(마이그레이션 027 미적용) 경고만 남기고 0을 반환한다 —
              장마감 배치의 나머지 흐름을 막지 않는다.
    """
    horizons = ", ".join(
        f"(SELECT spot FROM underlying_spot_1m s WHERE s.underlying = %(u)s"
        f" AND s.timestamp = d.timestamp + interval '{m} minutes') AS spot_{m}"
        for m in HORIZON_MINUTES
    )
    sql = f"""
        WITH base AS (
            SELECT d.decision_id, d.timestamp,
                   (d.risk_gate_state->>'direction')::double precision AS direction,
                   (SELECT spot FROM underlying_spot_1m s
                     WHERE s.underlying = %(u)s AND s.timestamp = d.timestamp) AS entry_spot,
                   {horizons}
              FROM signal_decisions d
             WHERE d.timestamp::date = %(day)s AND d.decision = 'ENTER'
        )
        INSERT INTO decision_outcomes (
            decision_id, timestamp, underlying, direction, entry_spot,
            spot_after_5m, spot_after_15m, spot_after_30m, hit_5m, hit_15m, hit_30m, computed_at
        )
        SELECT decision_id, timestamp, %(u)s, direction, entry_spot,
               spot_5, spot_15, spot_30,
               {_hit_expr('spot_5')}, {_hit_expr('spot_15')}, {_hit_expr('spot_30')},
               now()
          FROM base
        ON CONFLICT (decision_id) DO UPDATE SET
            entry_spot = EXCLUDED.entry_spot,
            spot_after_5m = EXCLUDED.spot_after_5m,
            spot_after_15m = EXCLUDED.spot_after_15m,
            spot_after_30m = EXCLUDED.spot_after_30m,
            hit_5m = EXCLUDED.hit_5m,
            hit_15m = EXCLUDED.hit_15m,
            hit_30m = EXCLUDED.hit_30m,
            computed_at = now()
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, {"u": underlying, "day": target})
            written = cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        logger.warning(
            "판단 사후 평가 계산 실패 — 마이그레이션 027 적용 전일 수 있다", exc_info=True
        )
        return 0
    return max(written, 0)


def _hit_expr(column: str) -> str:
    """방향 x 이동의 부호. **무변동은 NULL**이다 — 상세 근거는 모듈 docstring."""
    return (
        f"CASE WHEN direction IS NULL OR entry_spot IS NULL OR {column} IS NULL THEN NULL"
        f"     WHEN direction = 0 THEN NULL"
        f"     WHEN {column} = entry_spot THEN NULL"
        f"     ELSE (direction * ({column} - entry_spot)) > 0 END"
    )


def summarize(conn: ConnectionLike, target: date) -> dict:
    """
    반환: 지평별 적중률과 표본 수.
    해석: **표본 수를 반드시 함께 읽는다.** 진입이 3건인 날의 적중률 100%는 아무 뜻이 없다.
         마지막 30분에 난 진입은 +30분 지평이 장 마감을 넘겨 구조적으로 NULL이다 —
         지평이 길수록 표본이 줄어드는 것이 정상이다.
    실패 조건: 테이블이 없으면 `{"available": False}`.
    """
    selects = ", ".join(
        f"count(hit_{m}m) AS n_{m}, count(*) FILTER (WHERE hit_{m}m) AS hit_{m}"
        for m in HORIZON_MINUTES
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT count(*), {selects} FROM decision_outcomes WHERE timestamp::date = %s",
                (target,),
            )
            row = cur.fetchone()
    except Exception:
        conn.rollback()
        logger.warning("판단 사후 평가 집계 실패", exc_info=True)
        return {"available": False}
    if not row or not row[0]:
        return {"available": False, "reason": "그날 ENTER 판단 없음 또는 미계산"}

    out: dict = {"available": True, "entries": int(row[0]), "horizons": {}}
    for i, minutes in enumerate(HORIZON_MINUTES):
        sample, hits = int(row[1 + i * 2]), int(row[2 + i * 2])
        out["horizons"][f"{minutes}m"] = {
            "sample": sample,
            "hits": hits,
            "hit_pct": round(hits / sample * 100, 1) if sample else None,
        }
    out["control"] = _reject_control_group(conn, target)
    return out


def _reject_control_group(conn: ConnectionLike, target: date, underlying: str = "KOSPI200") -> dict:
    """
    입력: DB 커넥션, 대상 날짜.
    계산: 같은 날 **REJECT** 판단에 같은 지평·같은 규칙으로 적중률을 매긴다(적재하지 않는다).
         그리고 **시간대를 맞춘** 비교를 함께 낸다 — 두 그룹이 모두 표본을 가진 시(hour)로만
         제한한 적중률이다.
    해석: 2026-08-07 고도화#C. 08-07 첫 실측에서 ENTER 적중률이 5m 52.3% / 15m 54.4% /
         30m 48.9%로 나왔는데, **그 숫자만으로는 "진입 신호가 무작위보다 나은가"에 답할 수
         없다.** 그날 시장이 한 방향으로 흘렀으면 아무 방향이나 찍어도 그 정도가 나온다.
         답은 **같은 분에 우리가 거른 판단이 어땠는가**와 비교해야 나온다.

         **왜 시간대를 맞추는가**: 08-07 실측에서 두 그룹의 분포가 심하게 달랐다 —
         ENTER는 09~14시(12~13시에 119건 집중), REJECT는 08시 14건·15시 45건으로 장전/장후에
         몰렸다. 시장이 시간대마다 다르게 움직이므로 그대로 비교하면 **신호 품질이 아니라
         시간대를 재게 된다.** 08-07 원시 비교는 REJECT가 30m에서 10pt 앞섰는데, 그 결론을
         그대로 믿는 것과 교란을 걷어내고 보는 것은 다른 일이다.

         **적재하지 않는 이유**: `decision_outcomes`는 "우리가 낸 진입의 성적표"라는 뜻을
         가진 테이블이다. 거른 판단을 같은 테이블에 넣으면 그 뜻이 흐려진다. 대조군은
         **읽을 때 만든다.**

         **이 값으로 가중치를 바꾸지 않는다** — 평가이지 되먹임이 아니다(§13 주석과 같다).
    실패 조건: 조회 실패는 `{"available": False}`. 겹치는 시간대가 없으면 `time_matched`가 비고,
              그 자체가 "비교 불가"라는 결론이다(억지로 숫자를 만들지 않는다).
    """
    horizons = ", ".join(
        f"(SELECT s.spot FROM underlying_spot_1m s WHERE s.underlying = %(u)s"
        f" AND s.timestamp = d.timestamp + interval '{m} minutes') AS spot_{m}"
        for m in HORIZON_MINUTES
    )
    hits = ", ".join(f"{_hit_expr(f'spot_{m}')} AS hit_{m}" for m in HORIZON_MINUTES)
    counts = ", ".join(
        f"count(hit_{m}) AS n_{m}, count(*) FILTER (WHERE hit_{m}) AS h_{m}" for m in HORIZON_MINUTES
    )
    sql = f"""
        WITH base AS (
            SELECT d.decision, date_trunc('hour', d.timestamp) AS hour,
                   (d.risk_gate_state->>'direction')::double precision AS direction,
                   (SELECT s.spot FROM underlying_spot_1m s
                     WHERE s.underlying = %(u)s AND s.timestamp = d.timestamp) AS entry_spot,
                   {horizons}
              FROM signal_decisions d
             WHERE d.timestamp::date = %(day)s
        ), scored AS (SELECT decision, hour, {hits} FROM base)
        SELECT decision, hour, count(*), {counts} FROM scored GROUP BY decision, hour
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, {"u": underlying, "day": target})
            rows = cur.fetchall()
    except Exception:
        conn.rollback()
        logger.warning("REJECT 대조군 집계 실패 — 진입 적중률만 낸다", exc_info=True)
        return {"available": False}
    if not rows:
        return {"available": False, "reason": "그날 판단 없음"}
    return _fold_control_group(rows)


def _fold_control_group(rows: list[tuple]) -> dict:
    """
    입력: `(decision, hour, total, n_5, h_5, n_15, h_15, ...)` 행 목록.
    계산: 그룹별 전체 적중률과, **두 그룹이 모두 표본을 가진 시(hour)로 제한한** 적중률.
    해석: 순수 함수로 뺀 이유는 시간대 매칭 규칙이 이 절의 핵심 주장이기 때문이다 —
         SQL 안에 숨기면 테스트가 DB를 필요로 하고, 그러면 아무도 안 고친다.
    실패 조건: 없다.
    """
    per: dict[str, dict] = {}
    hours: dict[str, set] = {}
    for row in rows:
        decision, hour, total = row[0], row[1], int(row[2])
        bucket = per.setdefault(decision, {"total": 0, "by_hour": {}})
        bucket["total"] += total
        cells = {}
        for i, minutes in enumerate(HORIZON_MINUTES):
            cells[minutes] = (int(row[3 + i * 2]), int(row[4 + i * 2]))
        bucket["by_hour"][hour] = cells
        if any(sample for sample, _ in cells.values()):
            hours.setdefault(decision, set()).add(hour)

    shared = hours.get("ENTER", set()) & hours.get("REJECT", set())

    def _rates(decision: str, only: set | None) -> dict:
        out = {}
        for minutes in HORIZON_MINUTES:
            sample = hit = 0
            for hour, cells in per.get(decision, {}).get("by_hour", {}).items():
                if only is not None and hour not in only:
                    continue
                s, h = cells[minutes]
                sample += s
                hit += h
            out[f"{minutes}m"] = {
                "sample": sample, "hits": hit,
                "hit_pct": round(hit / sample * 100, 1) if sample else None,
            }
        return out

    return {
        "available": "REJECT" in per,
        "rejects": per.get("REJECT", {}).get("total", 0),
        "horizons": _rates("REJECT", None),
        # 시간대를 맞춘 비교 — 두 그룹이 모두 표본을 가진 시로만 제한한다.
        "shared_hours": sorted(h.strftime("%H") for h in shared),
        "time_matched": {
            "enter": _rates("ENTER", shared),
            "reject": _rates("REJECT", shared),
        },
    }


def horizon_end(day: date) -> timedelta:
    """가장 긴 지평 — 리포트가 "표본이 왜 줄었나"를 설명할 때 쓴다."""
    return timedelta(minutes=max(HORIZON_MINUTES))
