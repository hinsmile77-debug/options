"""E3 옵션 인텔리전스 — GEX/Gamma Flip/Gamma Wall/Vanna·Charm/VRP (v6 §9.2).

체인 레그는 `option_analysis_1m` DB 스키마와 1:1 대응하는 OptionLeg로 표현해, 실시간 수집·
백테스트·대시보드가 동일한 자료구조를 공유하도록 한다.
"""

from __future__ import annotations

import contextlib
import io
import logging
import math
from dataclasses import dataclass
from datetime import date, time
from typing import Sequence

from vollib.black_scholes.greeks.analytical import delta as _bs_delta
from vollib.black_scholes.greeks.analytical import gamma as _bs_gamma

logger = logging.getLogger("mahdi.features.options_intel")

_CALL_PUT_SIGN = {"c": 1.0, "p": -1.0}

# 2026-08-03(운영점검보고서 §2-1) — Gamma Flip 탐색에 쓸 수 있는 최소 레그 수. 행사가 3개 x
# 콜/풋 = 6이 하한이다(그보다 적으면 GEX(S) 곡선이 몇 개의 점으로만 결정돼 부호 전환 위치가
# 사실상 임의값이 된다). 이 아래로 떨어지면 값을 지어내지 않고 None을 돌려주되 **로그를 남긴다** —
# 지금까지 산출 실패가 조용했던 것이 §2-1 버그가 넉 달간 안 보인 직접적 원인이다.
GAMMA_FLIP_MIN_LEGS = 6


@dataclass(frozen=True, slots=True)
class OptionLeg:
    """옵션 체인의 행사가 1개 레그. vanna/charm은 Greeks 엔진에서 미리 계산해 채운다."""

    strike: float
    option_type: str  # "c" | "p"
    oi: float
    iv: float          # 내재변동성 (decimal, 예: 0.18)
    t_years: float      # 잔존만기(연 단위)
    gamma: float
    vanna: float = 0.0
    charm: float = 0.0


def legs_from_chain_rows(chain_rows: Sequence[dict], today: date) -> list[OptionLeg]:
    """
    입력: `db.latest_option_chain()`/`db.option_chain_as_of()`가 반환하는 형태의 dict 목록
         (strike/option_type/oi/iv/gamma/expiry 키), 잔존만기 계산 기준일.
    계산: `mahdi/dashboard/data_source.py`의 체인 dict -> `OptionLeg` 변환과 동일한 규칙 —
         option_type을 소문자로, t_years=max((expiry-today).days, 0)/365. vanna/charm은 이
         조회 결과에 없어 기본값(0.0)을 그대로 둔다(있는 값을 억지로 채우지 않음).
    해석: 만기가 지난(또는 없는) 레그는 제외한다 — expiry가 None인 행, 또는 today 이전에 이미
         만기가 끝난 행은 만들지 않는다.
    실패 조건: chain_rows가 비어있으면 빈 목록.

    2026-08-03(운영점검보고서 §2-1/§2-2): 위 "해석"은 원래부터 이렇게 적혀 있었는데 **코드는
    `expiry is not None`만 걸고 있었다** — 문서와 코드가 어긋나 있었고, 그 결과 라이브 체인
    246레그 중 156레그(63%)가 이미 만기가 지난 것이었다(`max(..., 0)`이 t_years를 0으로 clamp해
    조용히 통과시킨다). t_years=0 레그는 Black-Scholes 감마가 정의되지 않아 `find_gamma_flip()`의
    합계를 NaN으로 오염시킨다. 문서 쪽이 옳았으므로 코드를 문서에 맞춘다.
    """
    return [
        OptionLeg(
            strike=row["strike"],
            option_type=row["option_type"].lower(),
            oi=row["oi"],
            iv=row["iv"],
            t_years=(row["expiry"] - today).days / 365.0,
            gamma=row["gamma"],
        )
        for row in chain_rows
        if row.get("expiry") is not None and row["expiry"] >= today
    ]


def legs_by_expiry(chain_rows: Sequence[dict], today: date) -> dict[date, list[OptionLeg]]:
    """
    입력: `legs_from_chain_rows()`와 같은 형태의 dict 목록, 잔존만기 계산 기준일.
    계산: 만기별로 레그를 나눠 돌려준다(만기 오름차순). 각 그룹의 변환 규칙은
         `legs_from_chain_rows()`와 완전히 동일하다 — 같은 함수를 재사용한다.
    해석: 2026-08-03(운영점검보고서 §5-5). 3개 북(먼슬리 regular / 위클리 월·목)을 **합산하면
         만기별 정보가 서로를 덮는다.** 특히 만기 당일 북은 잔존만기가 0이라 Black-Scholes
         감마가 정의되지 않는 반면, v6 §A3가 말하는 **만기 Pinning은 바로 그 북에서만** 나온다 —
         합산 GEX 하나로는 그 신호를 볼 수 없다.
         용도 분리:
           - 먼슬리(최근월) → GEX/감마플립/감마월의 **주 입력**(v6 §11.4 게이트).
           - 위클리 → **핀 리스크 전용** 지표(만기일 당일 ATM 집중도).
         2026-08-03은 실제로 weekly_mon 만기일이었고, 그날 하루 GEX는 세 북 합산이었다.
    실패 조건: chain_rows가 비어있으면 빈 dict. 만기가 지난/없는 레그는
              `legs_from_chain_rows()`와 동일하게 제외된다.
    """
    grouped: dict[date, list[dict]] = {}
    for row in chain_rows:
        expiry = row.get("expiry")
        if expiry is None:
            continue
        grouped.setdefault(expiry, []).append(row)
    return {
        expiry: legs
        for expiry in sorted(grouped)
        if (legs := legs_from_chain_rows(grouped[expiry], today))
    }


def calculate_gex(legs: Sequence[OptionLeg], spot: float, multiplier: float = 250_000) -> float:
    """
    GEX = Sigma(Gamma x OI x multiplier x S^2/100), call(+) put(-) 관례.

    입력: 옵션 체인 레그(감마·미결제약정 포함), 기초자산 현재가, 계약승수
         (KOSPI200 옵션 = 250,000원/포인트).
    계산: 레그별 감마 익스포저를 부호(콜+/풋-) 규약으로 합산.
    해석: +GEX -> 딜러가 변동성을 억제(회귀장), -GEX -> 증폭(추세·급변장).
    실패 조건: legs가 비어있으면 0.0.
    """
    s_term = spot**2 / 100
    return sum(_CALL_PUT_SIGN[leg.option_type] * leg.gamma * leg.oi * multiplier * s_term for leg in legs)


def usable_for_black_scholes(leg: OptionLeg) -> bool:
    """
    입력: 옵션 체인 레그 1개.
    계산: Black-Scholes 감마를 **정의된 값으로** 계산할 수 있는 레그인지 판정한다 —
         iv/t_years/strike가 전부 양수여야 한다(셋 중 하나라도 0이면 d1의 분모가 0이 된다).
    해석: 2026-08-03(운영점검보고서 §2-1) — `find_gamma_flip()`의 `gex_at()`은 레그별 감마를
         **합산**한다. 따라서 `iv=0`인 레그가 **하나만** 섞여도 그 레그의 감마가 NaN이 되고,
         NaN이 더해진 순간 그 그리드 포인트의 합계 전체가 NaN이 된다. 그리고 NaN은
         `values[i-1] * values[i] < 0`을 **항상 False**로 만들기 때문에, 함수는 예외도 경고도
         없이 루프를 끝까지 돌고 None을 반환한다.
         라이브 DB 실측(2026-08-03 15:45): 41개 그리드 포인트가 **전부 NaN**이었고, 부분집합
         (먼슬리만/미만기만/당일만기만) 어느 쪽을 넣어도 같았다. `signal_decisions` 전 이력에서
         `available_member_count >= 3`인 행이 0건인 것이 그 결과다 — 앙상블 멤버
         `options_flow`(v6 §11.3 base_w 0.20)가 **한 번도 활성화된 적이 없다.**
         오늘 실측 결측률: `option_analysis_1m` 기준 iv가 0/NULL인 행이 먼슬리 4.4%,
         weekly_mon 9.7%. 즉 "가끔"이 아니라 매 스냅샷마다 확실히 섞인다.
    실패 조건: 없음 — 순수 판정 함수.
    """
    return leg.iv > 0 and leg.t_years > 0 and leg.strike > 0


# 2026-08-04(운영점검보고서 §2-4 / Fix#4) — Charm 계산의 시간 스텝(1영업일 ≈ 1/365년).
_CHARM_TIME_STEP_YEARS = 1.0 / 365.0


def with_computed_charm(
    legs: Sequence[OptionLeg], spot: float, risk_free_rate: float = 0.035
) -> list[OptionLeg]:
    """
    입력: 옵션 체인 레그, 현재 스팟, 무위험이자율.
    계산: 레그마다 **Charm(= 하루 경과당 델타 변화량)** 을 Black-Scholes 델타의 수치 미분으로
         채워 새 레그 목록을 돌려준다. `charm = delta(T - 1일) - delta(T)`.
    해석: 2026-08-04 §2-4 — `_options_flow_score()`의 두 진입로 중 Charm 경로(v6 §13.2
         "14:00 이후 Charm 드리프트 방향 우선")가 **배선이 끊겨 있었다.** `SignalInputs`에
         `total_charm`/`charm_active` 필드가 있고 `vanna_charm_drift()`도 구현돼 있는데,
         `main._build_signal_inputs()`가 둘 다 채우지 않았다. 그리고 채우더라도 값이 0이었을
         것이다 — `legs_from_chain_rows()`가 `charm=0.0` 기본값을 두고(체인 조회에 그 컬럼이
         없다), `option_analysis_1m.charm` 컬럼은 존재하지만 **적재된 적이 없다**
         (08-04 실측: 9,288행 전부 NULL).

         **부호 규약(여기서 처음 정한다)**: 시간이 **흐르는** 방향(잔존만기 감소)의 델타 변화다.
         양수면 시간이 갈수록 델타가 커지고(딜러가 매수 방향으로 재헤지), 음수면 그 반대다.
         `_options_flow_score()`는 이 부호만 쓴다.

         쓰기 경로(`_parse_option_quote`)가 아니라 **읽기 시점에** 계산하는 이유: KIS는 Charm을
         주지 않으므로 어차피 우리가 계산해야 하고, 읽기 시점에 하면 (a) 과거 데이터 백필이
         필요 없고 (b) 백테스트 리플레이가 라이브와 **같은 함수**로 같은 값을 얻는다.
         비용은 레그당 vollib 호출 2회다(먼슬리 10레그면 분당 20회 — `find_gamma_flip`의
         41 x 레그수에 비하면 무시할 수준).
    실패 조건: 계산 불가 레그(`usable_for_black_scholes()` 탈락)는 charm=0.0으로 남겨 그대로
              돌려준다 — 배제하면 호출측이 레그 수가 줄어든 이유를 알 수 없다. 0은 부호가
              없으므로 합계에 기여하지 않는다.
    """
    out: list[OptionLeg] = []
    with contextlib.redirect_stdout(io.StringIO()):  # vollib.ref_python의 디버그 print 흡수
        for leg in legs:
            if not usable_for_black_scholes(leg) or leg.t_years <= _CHARM_TIME_STEP_YEARS:
                out.append(leg)
                continue
            now_delta = _bs_delta(leg.option_type, spot, leg.strike, leg.t_years, risk_free_rate, leg.iv)
            next_delta = _bs_delta(
                leg.option_type, spot, leg.strike,
                leg.t_years - _CHARM_TIME_STEP_YEARS, risk_free_rate, leg.iv,
            )
            charm = next_delta - now_delta
            if not math.isfinite(charm):
                out.append(leg)
                continue
            out.append(
                OptionLeg(
                    strike=leg.strike, option_type=leg.option_type, oi=leg.oi, iv=leg.iv,
                    t_years=leg.t_years, gamma=leg.gamma, vanna=leg.vanna, charm=charm,
                )
            )
    return out


def find_gamma_flip(
    legs: Sequence[OptionLeg],
    spot: float,
    multiplier: float = 250_000,
    risk_free_rate: float = 0.035,
    search_pct: float = 0.05,
    steps: int = 41,
) -> float | None:
    """
    GEX 부호가 바뀌는 기초자산 레벨(Gamma Flip) — 이탈 시 urgency 모드.

    입력: 옵션 체인 레그(행사가·IV·잔존만기 포함), 현재 스팟, 계약승수.
    계산: **먼저 `usable_for_black_scholes()`로 계산 불가 레그를 걸러낸 뒤**, 스팟 ±search_pct
         구간을 steps개 그리드로 나눠 각 지점에서 Black-Scholes 감마를 재계산(행사가·IV·
         잔존만기는 고정, 스팟만 이동)해 GEX(S)를 구성하고, 부호가 바뀌는 구간을 선형보간해
         flip 레벨을 추정한다. steps x legs번 vollib.gamma()를 호출하는데, vollib.ref_python
         (C 확장 미설치 시 폴백되는 순수 파이썬 구현)의 d1()에 디버그용 print('')이 남아 있어
         (2026-07-08 실측: COCKPIT 하루 로그의 99%가 이 빈 줄이었음) stdout을 로컬로 흡수한다.
    해석: 이 레벨을 이탈하면 딜러 헤지가 안정화<->증폭으로 전환 — 변동성 폭발 준비 신호.
    실패 조건: 사용 가능한 레그가 GAMMA_FLIP_MIN_LEGS 미만이면 None + WARNING. 그리드 전 구간에서
              부호가 바뀌지 않으면 None(flip 레벨이 탐색 범위 밖 — 정상적인 결과이므로 로그 없음).
              그리드 값에 NaN이 남아 있으면(방어) 그 구간은 건너뛰고, 전 구간이 NaN이면 None + WARNING.

    2026-08-03(§2-1) 이전 버전은 RuntimeWarning을 억제하면서 주석에 *"값 계산 자체는 정상,
    numpy가 nan/inf를 반환할 뿐이고 그 지점은 그대로 GEX 부호 비교에 들어가 flip 계산에 영향
    없음"* 이라고 적어 두었는데 **이것이 사실과 정반대였다** — NaN은 "그 지점"이 아니라 합계를
    거쳐 전 구간을 오염시키고, 부호 비교를 조용히 무력화한다. 경고 억제가 그 사실을 덮었다.
    이제 억제하지 않고 **입력 단계에서 배제한다**(억제는 계산 결과를 못 믿게 만든다).
    """
    usable = [leg for leg in legs if usable_for_black_scholes(leg)]
    if len(usable) < GAMMA_FLIP_MIN_LEGS:
        logger.warning(
            "감마플립 산출 불가 — BS 계산 가능 레그 %d개(전체 %d개, 최소 %d 필요). "
            "iv=0 %d개 / 잔존만기<=0 %d개",
            len(usable), len(legs), GAMMA_FLIP_MIN_LEGS,
            sum(1 for leg in legs if leg.iv <= 0),
            sum(1 for leg in legs if leg.t_years <= 0),
        )
        return None

    step_size = (spot * 2 * search_pct) / (steps - 1)
    grid = [spot * (1 - search_pct) + i * step_size for i in range(steps)]

    def gex_at(s: float) -> float:
        s_term = s**2 / 100
        total = 0.0
        for leg in usable:
            g = _bs_gamma(leg.option_type, s, leg.strike, leg.t_years, risk_free_rate, leg.iv)
            total += _CALL_PUT_SIGN[leg.option_type] * g * leg.oi * multiplier * s_term
        return total

    with contextlib.redirect_stdout(io.StringIO()):
        values = [gex_at(s) for s in grid]

    finite = [(i, v) for i, v in enumerate(values) if math.isfinite(v)]
    if not finite:
        logger.warning(
            "감마플립 산출 불가 — 그리드 %d개 전 구간이 NaN/inf다(레그 %d개). "
            "usable_for_black_scholes()를 통과한 입력에서 이 경로가 나오면 vollib 쪽 문제다.",
            len(grid), len(usable),
        )
        return None

    # 2026-08-04(운영점검보고서 §2-9 후속) — 종전 코드는 `if v_prev == 0: return grid[i_prev]`로
    # **정확히 0인 첫 그리드 점을 그대로 flip 레벨로 돌려줬다.** 그런데 OI가 전부 0인 북
    # (08-04의 weekly_mon 2026-08-10: 2,233레그 중 OI≠0이 128개, 평균 0)은 GEX(S)가 **모든 구간에서
    # 0**이라, 이 분기가 `grid[0]` = spot x 0.95를 flip으로 반환했다. 실측: 스팟 1000.03에서
    # `감마플립 950.03` — 사람이 읽으면 "5% 아래에 딜러 전환선이 있다"로 읽는 완전한 허수다.
    # 0은 "여기서 부호가 바뀐다"와 "이 북엔 포지션이 없다"를 구분하지 못한다. **비영(非零) 값의
    # 부호 전환만** flip으로 인정하고, 중간의 정확한 0은 건너뛴다(접점은 교차가 아니다).
    if all(v == 0 for _i, v in finite):
        return None

    last_nonzero: tuple[int, float] | None = None
    for i_cur, v_cur in finite:
        if v_cur == 0:
            continue
        if last_nonzero is not None and last_nonzero[1] * v_cur < 0:
            i_prev, v_prev = last_nonzero
            frac = v_prev / (v_prev - v_cur)
            return grid[i_prev] + frac * (grid[i_cur] - grid[i_prev])
        last_nonzero = (i_cur, v_cur)
    return None


def gamma_walls(
    legs: Sequence[OptionLeg], spot: float, multiplier: float = 250_000, top_n: int = 3
) -> list[tuple[float, float]]:
    """
    감마 집중 상위 행사가 — Pinning 후보, 부분청산 기준선.

    계산: 행사가별 |Gamma x OI x multiplier x S^2/100| 합산 후 내림차순 top_n.
    해석: 값이 큰 행사가일수록 만기 근접 시 가격이 붙들리는 자석(Pinning) 후보.
    실패 조건: legs가 비어있으면 빈 리스트.
    """
    s_term = spot**2 / 100
    by_strike: dict[float, float] = {}
    for leg in legs:
        exposure = abs(leg.gamma * leg.oi * multiplier * s_term)
        by_strike[leg.strike] = by_strike.get(leg.strike, 0.0) + exposure
    return sorted(by_strike.items(), key=lambda kv: kv[1], reverse=True)[:top_n]


def pin_risk(legs: Sequence[OptionLeg], spot: float, multiplier: float = 250_000) -> dict | None:
    """
    입력: **하나의 만기**에 속한 레그들(`legs_by_expiry()`의 한 그룹), 현재 스팟, 계약승수.
    계산: 감마 노출이 가장 큰 행사가(자석 후보)와, 그 행사가가 전체 노출에서 차지하는 비중,
         그리고 스팟이 그 행사가에서 얼마나 떨어져 있는지를 낸다.
    해석: 2026-08-03 §5-5 — v6 §A3 "만기 Pinning". 만기 당일에는 잔존만기가 0이라 Black-Scholes
         감마·감마플립이 정의되지 않지만, **핀 리스크는 바로 그 북에서만 의미가 있다.** 그래서
         이 지표는 저장된 감마(`OptionLeg.gamma`)만 쓰고 BS를 재계산하지 않는다 — 만기 당일에도
         계산된다는 것이 이 함수의 존재 이유다.
         `concentration`이 높고(한 행사가에 노출이 몰림) `distance_pct`가 작으면(스팟이 그 위)
         만기 근접 시 가격이 그 행사가에 붙들릴 가능성이 크다.
    실패 조건: legs가 비어있거나 전체 노출이 0이면(OI가 전부 0 등) None — 지어내지 않는다.
    """
    walls = gamma_walls(legs, spot, multiplier, top_n=1)
    if not walls:
        return None
    total = sum(abs(leg.gamma * leg.oi * multiplier * (spot**2 / 100)) for leg in legs)
    if total <= 0:
        return None
    strike, exposure = walls[0]
    return {
        "strike": strike,
        "concentration": exposure / total,
        "distance_pct": (spot - strike) / spot * 100 if spot else None,
    }


def vanna_charm_drift(legs: Sequence[OptionLeg], now: time, charm_active_after: time = time(14, 0)) -> dict:
    """
    Vanna: dDelta/dVol -> IV 변화 방향과 결합해 딜러 재헤지 방향 추정.
    Charm: dDelta/dTime -> 14:00 이후 Charm 방향 드리프트 가중치 활성화.

    입력: 레그별 vanna/charm(Greeks 엔진에서 미리 계산해 채운 값), 현재 시각.
    계산: 전체 vanna/charm 익스포저 합산, 마감 임박 여부(charm_active) 플래그.
    해석: charm_active=True일 때만 Charm 드리프트 방향을 신호에 반영해야 한다.
    실패 조건: OI 데이터 지연·이벤트 당일에는 신뢰도 하향 — 호출측(Fusion)에서 처리한다.
    """
    total_vanna = sum(leg.vanna * leg.oi for leg in legs)
    total_charm = sum(leg.charm * leg.oi for leg in legs)
    return {
        "total_vanna": total_vanna,
        "total_charm": total_charm,
        "charm_active": now >= charm_active_after,
    }


class GammaMapEngine:
    """v6 §9.2 GammaMapEngine — 체인 스냅샷을 받아 GEX/Flip/Wall/Vanna·Charm을 계산한다."""

    def __init__(self, multiplier: float = 250_000, risk_free_rate: float = 0.035) -> None:
        self.multiplier = multiplier
        self.risk_free_rate = risk_free_rate

    def calculate_gex(self, legs: Sequence[OptionLeg], spot: float) -> float:
        return calculate_gex(legs, spot, self.multiplier)

    def find_gamma_flip(self, legs: Sequence[OptionLeg], spot: float) -> float | None:
        return find_gamma_flip(legs, spot, self.multiplier, self.risk_free_rate)

    def gamma_walls(self, legs: Sequence[OptionLeg], spot: float, top_n: int = 3) -> list[tuple[float, float]]:
        return gamma_walls(legs, spot, self.multiplier, top_n)

    def vanna_charm_drift(self, legs: Sequence[OptionLeg], now: time) -> dict:
        return vanna_charm_drift(legs, now)


def calculate_vrp(iv: float, realized_vol: float) -> float:
    """
    IV-RV Spread (변동성 리스크 프리미엄, VRP).

    계산: iv - realized_vol.
    해석: VRP>0 -> 옵션이 비쌈(프리미엄 매도 후보, 안정 레짐+positive GEX 한정),
         VRP<0 -> 옵션이 저평가(이벤트 전 눌린 IV일 가능성 -> Long Vol 후보).
    실패 조건: 없음(단순 차분) — realized_vol 추정 윈도우가 짧으면 노이즈에 취약함에 유의.
    """
    return iv - realized_vol
