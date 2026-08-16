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
from typing import Iterable, Sequence

from vollib.black_scholes.greeks.analytical import delta as _bs_delta
from vollib.black_scholes.greeks.analytical import gamma as _bs_gamma

logger = logging.getLogger("mahdi.features.options_intel")

_CALL_PUT_SIGN = {"c": 1.0, "p": -1.0}

# 2026-08-03(운영점검보고서 §2-1) — Gamma Flip 탐색에 쓸 수 있는 최소 레그 수. 행사가 3개 x
# 콜/풋 = 6이 하한이다(그보다 적으면 GEX(S) 곡선이 몇 개의 점으로만 결정돼 부호 전환 위치가
# 사실상 임의값이 된다). 이 아래로 떨어지면 값을 지어내지 않고 None을 돌려주되 **로그를 남긴다** —
# 지금까지 산출 실패가 조용했던 것이 §2-1 버그가 넉 달간 안 보인 직접적 원인이다.
GAMMA_FLIP_MIN_LEGS = 6

# 2026-08-05(운영점검보고서 §2-5) — 감마플립을 **수집한 행사가 범위 안**으로 가둔다.
#
# 08-05에 flip이 처음으로 0%를 벗어나 22분(4.5%) 산출됐는데 **21건이 수집 행사가 창 밖**이었고,
# 그 21건의 `flip / spot` 비율이 전부 0.95~0.99에 몰려 있었다 — 탐색 그리드의 왼쪽 끝 근처다.
# 원인은 그리드와 레그의 폭이 10배 차이난다는 것이다: 그리드는 스팟 ±5%(스팟 1040이면 104포인트)
# 인데 실제 수집 레그는 ATM±2 = 5행사가 = **폭 10포인트**다. 그리드의 90%에는 레그가 하나도 없고,
# 그 구간에서 모든 레그의 BS 감마는 0으로 수렴해 `gex_at(S)`가 부동소수 잔여만 남는다 —
# 부호가 아무 이유 없이 바뀌고, 그 첫 교차점이 그리드 왼쪽 끝 근처에서 잡힌다.
#
# **그리드를 좁히지 않는 이유**: 탐색 범위를 레그 폭으로 줄이면 "이 북엔 flip이 탐색 범위 밖에
# 있다"와 "탐색을 안 해봤다"를 구분할 수 없게 된다. 그리드는 그대로 두고 **판정만** 가둔다.
#
# 08-04에 고친 허수 flip(`v_prev == 0`이면 그리드 왼쪽 끝 반환)과 **같은 자리에서 다른 경로로
# 재발한 것**이다. 그래서 이번엔 조용히 None으로 떨어뜨리지 않고 마커 로그를 남긴다 —
# `log_metrics._QUALITATIVE_MARKERS`에 등록돼 리포트 §11에 매일 건수로 찍힌다.
#
# **경계를 [min(strike), max(strike)]로 딱 자르지 않는 이유**: 최외곽 행사가 바로 바깥에도
# 그 행사가 레그의 감마는 여전히 실재한다. 실제로 이 저장소의 기존 테스트 구성
# (행사가 340/350/360, 스팟 350, iv 0.18, 잔존 0.05년)의 진짜 flip은 **338.25**로 최저 행사가에서
# 1.75포인트 아래인데, 그 지점의 1σ 이동폭은 약 14포인트라 340 레그의 감마가 충분히 살아 있다 —
# 잡음이 아니라 정당한 flip이다. 그래서 **행사가 간격 단위의 허용치**를 둔다.
#
# 허용치를 "행사가 간격"으로 잡은 이유는 그것이 **데이터에서 자기교정되는 유일한 척도**이기
# 때문이다(포인트 상수로 두면 KOSPI200과 다른 기초자산에서 의미가 달라진다). 두 사례의 분리도:
#   - 위 테스트 구성: 간격 10, 이탈 1.75  → **0.18간격** (통과)
#   - 08-05 실측 21건: 간격 2.5, 이탈 최소 11.2 ~ 최대 86.3 → **4.5~34.5간격** (기각)
# 1.0간격은 이 둘 사이 어디에 둬도 판정이 같은 넓은 구간의 보수적인 쪽 끝이다.
GAMMA_FLIP_LEG_RANGE_TOLERANCE_INTERVALS = 1.0

LOG_GAMMA_FLIP_OUT_OF_LEG_RANGE = (
    "감마플립 기각(레그 범위 밖) — flip %.2f, 수집 행사가 %.2f~%.2f(허용 ±%.2f), 스팟 %.2f. "
    "탐색 그리드(스팟 ±%.0f%%)가 레그 범위 밖에서 주운 부호 전환이다"
)

# ===== 2026-08-13 §5 / 고도화 1 — **만기 당일 북은 계산을 시도하지 않는다** =====
#
# 08-13 WARNING 703건 중 **383건(54.5%)**이 09:01:10~15:23:10 매분 1건씩 찍힌 같은 문장이었다:
# 「감마플립 산출 불가 — BS 계산 가능 레그 0개」. 내용은 **설계상 당연한 사실**이다 —
# 그날 먼슬리는 만기 당일이라 잔존만기가 0이고, Black-Scholes는 t=0에서 정의되지 않는다.
#
# 경고 채널의 절반이 「알고 있는 사실」로 차 있으면 **진짜 이상이 그 안에 묻힌다.**
#
# ## 레벨만 내리지 않는다
#
# 2026-08-04에 「느린 REST 호출」을 WARNING → INFO로 내렸다가 집계 정규식이 통째로 눈이 멀어
# **362건이 0건으로 보고된** 사고가 있다. 그래서 여기서는 세 가지를 함께 한다:
#
#   1. 만기 당일 북은 **계산 시도 전에** 제외한다(경고를 억누르는 것이 아니라 일을 안 하는 것).
#   2. 제외 사실을 **하루 1회 INFO**로 남긴다 — 매분이 아니라 그 북에 대해 한 번.
#   3. 제외한 북 수를 **지표로 내보낸다**(`excluded_expiring_books`) — 0건이 「계측이 없다」가
#      아니라 「제외할 북이 없었다」임을 증명하기 위해서다(규약 C).
#
# 셋 중 3번이 핵심이다. 1·2번만 하면 이 사건은 그냥 조용해지고, 조용해진 것과 사라진 것을
# 다음 사람이 구분할 수 없다.
LOG_EXPIRING_BOOK_EXCLUDED = (
    "만기 당일 북 제외 — 잔존만기 0인 레그 %d개(만기 %s), 감마플립 미산출. "
    "설계상 당연하며 이상이 아니다(BS는 t=0에서 정의되지 않는다)"
)

# 「하루 1회」의 상태. 키는 (기준일, 만기일)이라 **날이 바뀌면 자동으로 다시 한 번 찍힌다.**
# 프로세스가 하루 단위라 무한히 자라지 않는다(하루에 북 3개 = 최대 3개 항목).
_expiring_book_logged: set[tuple[date, date | None]] = set()

# 그날 제외한 만기일들. 지표(`gamma_flip.excluded_expiring_books`)의 원천이다.
_excluded_expiries: dict[date, set[date | None]] = {}


def note_expiring_book(today: date, expiry: date | None, leg_count: int) -> None:
    """만기 당일 북을 제외했다는 사실을 **하루 1회** 남기고 지표에 센다.

    호출측(`find_gamma_flip`)이 이미 「쓸 수 있는 레그가 하나도 없다」를 확인한 뒤에 부른다 —
    이 함수는 판정하지 않고 기록만 한다.
    """
    _excluded_expiries.setdefault(today, set()).add(expiry)
    key = (today, expiry)
    if key in _expiring_book_logged:
        return
    _expiring_book_logged.add(key)
    logger.info(LOG_EXPIRING_BOOK_EXCLUDED, leg_count, expiry or "미상")


def excluded_expiring_books(today: date) -> int:
    """반환: 그날 감마플립 대상에서 제외한 **북 수**. 제외가 없었으면 0.

    **0은 「계측이 없다」가 아니다** — 이 함수가 있는 한 0은 「제외할 북이 없었다」이다.
    """
    return len(_excluded_expiries.get(today, ()))


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


def monthly_expiry(expiries: Iterable[date]) -> date | None:
    """
    입력: 후보 만기일들(중복·순서 무관).
    계산: 그중 **먼슬리(월물)** 만기를 고른다. KRX 코스피200 옵션의 월물 만기는 **그 달 두 번째
         목요일**이므로, 그 형태인 후보가 정확히 하나면 그것이 답이다. 아니면 최대 만기로 떨어진다.
    해석: 2026-08-11 — 종전 규칙은 `max(expiries)` 하나였다. 근거는 *"위클리는 늘 먼슬리보다
         가깝다"* 였는데 **이번 주에 그것이 깨졌다.** 08-13(목)이 8월물 만기이고, 08-15 광복절이
         토요일이라 08-17(월)이 대체공휴일이 되면서 위클리(월) 만기가 **08-18로 먼슬리 뒤로
         밀렸다.** 그래서 `max()`가 위클리를 먼슬리로 지목했다.

         이 함수 이전의 두 호출부는 그 위험을 알고 있었고, 완화 논리까지 적어 뒀다 —
         *"월물 만기 주간에 뒤집힐 수 있는데, 그때는 먼슬리가 이미 만기 당일이라
         `usable_for_black_scholes()`가 걸러 GEX가 0이 된다."* **그 완화는 만기 당일에만
         성립한다.** 오늘은 만기 **이틀 전**에 뒤집혔다(위클리가 먼슬리를 건너뛰었기 때문이지
         먼슬리가 가까워졌기 때문이 아니다). 공휴일이 만드는 이 형태는 매달 재현될 수 있다.

         실측 피해(2026-08-11 07:31~08:05, 개장 전에 발견): `max()`가 분 단위로 교대했다 —
         위클리는 격분에만 조회되므로 홀수분엔 먼슬리(08-13)만, 짝수분엔 위클리(08-18)가
         섞여 최대가 된다. 두 북의 ATM IV는 0.7910 vs 0.6229로 **격차 0.168**이고, 이는
         2026-08-10에 R6이 없앤 구형파(0.210)의 80% 크기다. **R6이 지운 진동이 다른 원인으로
         같은 자리에 돌아와 있었고**, 하필 HMM이 처음 라이브로 도는 날이었다.

         **왜 series 컬럼이 아니라 날짜 규칙인가**: `option_analysis_1m`에는 series가 없다
         (`signal_book_legs()` 주석). 컬럼을 추가하면 과거 행이 전부 NULL이라 오프라인 재계산
         (`fit_regime_engine.reconstruct_iv_chg`)이 라이브와 갈라진다 — 그 분기가 곧 08-10
         사고의 구조다. 날짜 규칙은 **과거 행에도 똑같이 적용된다.**
    실패 조건: 후보가 없으면 None. 두 번째 목요일이 0개거나 2개 이상이면 `max()`로 떨어진다 —
              월물 만기가 공휴일로 앞당겨지는 경우를 **지어내서 맞추려 하지 않는다**(종전 동작
              유지). 그 상황은 `db_metrics.monthly_book_expiry_with_source()`의 1순위
              (`expiry_liquidity_1m.series='regular'`)가 08:30 이후에 정정해 준다.
    """
    candidates = sorted(set(expiries))
    if not candidates:
        return None
    # 두 번째 목요일 = 목요일(weekday 3)이면서 8~14일. 달마다 정확히 하루뿐이다.
    second_thursdays = [d for d in candidates if d.weekday() == 3 and 8 <= d.day <= 14]
    if len(second_thursdays) == 1:
        return second_thursdays[0]
    return candidates[-1]


def signal_book_legs(chain_rows: Sequence[dict], today: date) -> tuple[list[OptionLeg], date | None]:
    """
    입력: 체인 스냅샷 행, 잔존만기 계산 기준일.
    계산: **먼슬리(최근월) 북 하나**의 레그만 골라 (레그, 그 북의 만기)를 돌려준다.
    해석: 2026-08-04 §2-8 / Fix#5 — 종전에는 `legs_from_chain_rows()`로 세 만기를 평탄화해
         한 덩어리로 GEX/감마플립을 계산했다. 그런데 v6 §11.4와 자동 리포트 §15 각주는 둘 다
         *"먼슬리(최근월)가 GEX/감마플립의 주 입력이고 위클리는 핀 리스크 전용"* 이라고 적고
         있었다 — **코드가 문서와 다르게 동작했다.** 08-04 실측 피해: 08-06 위클리 GEX +90.8B
         (콜 편중)와 08-13 먼슬리 −51.0B(풋 편중)가 상쇄돼 라이브 GEX가 하루 동안
         −33.5B ~ +99.1B를 오갔고(양수 233분 / 음수 259분), `_options_flow_score()`가 GEX
         부호로 회귀/증폭을 가르므로 **신호 부호가 그 상쇄에 좌우됐다.**

         **어느 북이 먼슬리인가는 `monthly_expiry()`가 정한다** — 규칙과 그 근거는 그 함수
         docstring에 있다. `option_analysis_1m`에 series 컬럼이 없어 날짜로 판별한다.

         2026-08-11 이전에는 여기가 `max(by_expiry)`였고, 근거는 *"위클리는 늘 먼슬리보다
         가깝다"* 였다. **그 전제가 2026-08-11에 깨졌다**(공휴일로 위클리(월)가 먼슬리 뒤로
         밀렸다). 당시 이 자리에 적혀 있던 완화 논리 — *"뒤집혀도 그때는 먼슬리가 만기
         당일이라 `usable_for_black_scholes()`가 걸러 GEX가 0이 된다"* — 는 **만기 당일에만
         성립**하고, 실제 사고는 만기 이틀 전에 났다. 위험을 알면서 적어 둔 완화가 실제 발생
         형태를 못 덮은 사례라 지우지 않고 남긴다.

         `signal_decisions.gex_expiry`(마이그레이션 023)에 실제 사용 만기를 남기는 이유가
         여기 있다 — 08-11의 오선택도 그 컬럼으로 사후 확인된다.

         2026-08-05(COCKPIT 육안 점검 P0-2): `mahdi/main.py`의 private 헬퍼였던 것을 여기로
         옮긴다. **COCKPIT(`dashboard/data_source.py`)이 같은 규칙을 쓰지 않아 화면의 Gamma
         Map은 위 Fix#5 이전 상태 — 세 북 평탄화 — 그대로였다.** 판단 근거와 화면이 다른 체인을
         보고 있으면 화면으로 판단을 검증할 수 없다(`_signal_reach_check`가 배지와 리포트에
         같은 함수를 강제하는 것과 같은 이유). 관측 루프와 대시보드가 공유할 수 있는 유일한
         자리가 이 모듈이라 여기에 둔다.
    실패 조건: 체인이 비었으면 ([], None).
    """
    by_expiry = legs_by_expiry(chain_rows, today)
    if not by_expiry:
        return [], None
    # 2026-08-11 — `max()`에서 `monthly_expiry()`로. 근거는 그 함수 docstring.
    # 후보는 **usable 레그가 남은 만기**뿐이다(`legs_by_expiry`가 이미 걸렀다) — 먼슬리가
    # 만기 당일이라 통째로 걸러졌으면 여기 없고, 그때는 종전대로 남은 것 중 최대로 떨어진다.
    expiry = monthly_expiry(by_expiry)
    return by_expiry[expiry], expiry


def monthly_atm_iv(chain_rows: Sequence[dict], spot: float | None) -> float | None:
    """
    입력: 체인 스냅샷 행(여러 북이 섞여 있어도 된다), 기준 스팟.
    계산: **먼슬리 북 한 개**(`monthly_expiry()`)로 좁힌 뒤 스팟에 가장 가까운 행사가를 ATM으로
         보고, 그 행사가의 콜/풋 IV 평균을 돌려준다.
    해석: 2026-08-10 — §7.3 `iv_chg`(ATM IV 변화율)의 유일한 입력이다. **북을 좁히는 것이 이
         함수의 존재 이유다.** 종전에는 `main._update_atm_iv()`가 전 북에서 ATM 행사가를 고르고
         그 행사가를 가진 **모든 북의 IV를 평균**했는데, 위클리는 `main._books_due_this_cycle()`로
         격분에만 조회되므로 ATM IV가 **분 단위 구형파**가 됐다(08-10 실측: 짝수분 0.5285 /
         홀수분 0.7387, 차분 자기상관 −0.900). 그 진동을 학습한 HMM이 "짝수분/홀수분"을 레짐으로
         94% 재현했고, 재생에서 분당 99.2%가 레짐 전이였다.

         **여기에 있는 이유**: 라이브(`main._update_atm_iv`)와 오프라인 재계산
         (`scripts/fit_regime_engine.reconstruct_iv_chg`)이 **반드시 같은 규칙**을 써야 한다.
         두 곳에 같은 로직을 적으면 한쪽만 바뀌어도 학습과 라이브가 다른 피처를 보게 되고,
         그것이 곧 08-10 사고의 구조다(`replay_live_predictions`를 `regime_pipeline`에 둔 것과
         같은 원칙).

         북 선택은 `signal_book_legs()`와 **같은 함수**(`monthly_expiry()`)를 쓴다 —
         판단(GEX/감마플립)이 보는 북과 레짐 피처가 보는 북이 갈리면 안 된다.
         2026-08-11에 그 공유가 값을 했다: 규칙을 한 곳에서 고치니 두 경로가 함께 나았다.
         다만 **Black-Scholes 사용 가능 여부로 거르지 않는다** —
         만기 당일이라 GEX가 0이 되는 북이라도 IV 자체는 관측값이라 레짐 피처로는 유효하다.
    실패 조건: rows/spot이 없거나, 만기가 있는 행이 없거나, ATM 행사가에 IV가 하나도 없으면 None.
    """
    if not chain_rows or spot is None:
        return None
    expiries = [row["expiry"] for row in chain_rows if row.get("expiry") is not None]
    if not expiries:
        return None
    # 2026-08-11 — `max()`에서 `monthly_expiry()`로. 아래 "먼슬리 = 최대 만기" 서술은 그 함수가
    # 대체했다. `signal_book_legs()`와 **같은 함수**를 부르는 것이 요점이다 — 판단이 보는 북과
    # 레짐 피처가 보는 북은 갈리면 안 되고, 규칙이 두 벌이면 한쪽만 고쳐진다.
    monthly = monthly_expiry(expiries)
    monthly_rows = [row for row in chain_rows if row.get("expiry") == monthly]
    if not monthly_rows:
        return None
    atm_strike = min(monthly_rows, key=lambda row: abs(row["strike"] - spot))["strike"]
    ivs = [row["iv"] for row in monthly_rows if row["strike"] == atm_strike and row.get("iv") is not None]
    return sum(ivs) / len(ivs) if ivs else None


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
    *,
    today: date | None = None,
    expiry: date | None = None,
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
              **부호 전환을 찾았더라도 그 레벨이 수집 행사가 범위 [min(strike), max(strike)] 밖이면
              None + WARNING**(2026-08-05 §2-5) — 레그가 없는 구간의 GEX(S)는 외삽이고, 거기서
              주운 부호 전환은 수치 잡음이다. 상세 근거는 `LOG_GAMMA_FLIP_OUT_OF_LEG_RANGE` 주석.

    2026-08-03(§2-1) 이전 버전은 RuntimeWarning을 억제하면서 주석에 *"값 계산 자체는 정상,
    numpy가 nan/inf를 반환할 뿐이고 그 지점은 그대로 GEX 부호 비교에 들어가 flip 계산에 영향
    없음"* 이라고 적어 두었는데 **이것이 사실과 정반대였다** — NaN은 "그 지점"이 아니라 합계를
    거쳐 전 구간을 오염시키고, 부호 비교를 조용히 무력화한다. 경고 억제가 그 사실을 덮었다.
    이제 억제하지 않고 **입력 단계에서 배제한다**(억제는 계산 결과를 못 믿게 만든다).
    """
    usable = [leg for leg in legs if usable_for_black_scholes(leg)]
    if len(usable) < GAMMA_FLIP_MIN_LEGS:
        expiring = sum(1 for leg in legs if leg.t_years <= 0)
        # 2026-08-13 고도화 1 — **북 전체가 만기 당일이면 그것은 이상이 아니다.**
        # 상세 근거는 `LOG_EXPIRING_BOOK_EXCLUDED` 주석. 조건을 「전부」로 좁게 잡는 이유:
        # 일부만 t=0인 상황(북이 섞였다)은 여전히 진짜 이상이고, 그건 아래 WARNING이 잡아야 한다.
        # `today`가 없으면 종전 경로를 그대로 탄다 — 「하루 1회」의 키를 만들 수 없는데
        # 조용히 넘기면 그 호출측은 영원히 아무 기록도 안 남기게 된다(백테스트·단위 테스트가
        # 그 경우다). **모르면 시끄러운 쪽으로 넘어진다.**
        if today is not None and legs and expiring == len(legs):
            note_expiring_book(today, expiry, expiring)
            return None
        logger.warning(
            "감마플립 산출 불가 — BS 계산 가능 레그 %d개(전체 %d개, 최소 %d 필요). "
            "iv=0 %d개 / 잔존만기<=0 %d개",
            len(usable), len(legs), GAMMA_FLIP_MIN_LEGS,
            sum(1 for leg in legs if leg.iv <= 0),
            expiring,
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

    # 2026-08-05(§2-5) — 판정 경계는 그리드가 아니라 **수집한 행사가 범위 ± 1행사가 간격**이다.
    # `usable`(BS 계산 가능 레그)에서 뽑는다: 계산에서 배제된 레그는 GEX(S) 곡선에 기여하지
    # 않으므로 그 행사가까지 "관측했다"고 볼 수 없다.
    # 간격은 상수로 두지 않고 **그 스냅샷의 실제 행사가에서 추론한다**(최소 양의 간격) —
    # 위클리/먼슬리·기초자산마다 다르고, 절단된 사이클에서는 수집 행사가가 듬성해질 수 있다.
    strikes = sorted({leg.strike for leg in usable})
    leg_lo, leg_hi = strikes[0], strikes[-1]
    interval = min((b - a for a, b in zip(strikes, strikes[1:])), default=0.0)
    tolerance = interval * GAMMA_FLIP_LEG_RANGE_TOLERANCE_INTERVALS

    # 범위 밖 교차가 **범위 안 교차를 가리지 않도록** 그리드를 끝까지 훑는다. 왼쪽 끝의 수치
    # 잡음에서 교차가 하나 잡혔다고 거기서 멈추면, 레그 범위 안의 진짜 전환을 놓친다.
    # 로그는 호출당 최대 1줄 — 기각이 여러 번이어도 첫 건만 대표로 남긴다(레그가 없는 구간은
    # 부호가 여러 번 튈 수 있고, 그것을 전부 남기면 §2-4에서 본 로그 폭증을 새로 만든다).
    rejected: tuple[float, ...] | None = None
    last_nonzero: tuple[int, float] | None = None
    for i_cur, v_cur in finite:
        if v_cur == 0:
            continue
        if last_nonzero is not None and last_nonzero[1] * v_cur < 0:
            i_prev, v_prev = last_nonzero
            frac = v_prev / (v_prev - v_cur)
            flip = grid[i_prev] + frac * (grid[i_cur] - grid[i_prev])
            if leg_lo - tolerance <= flip <= leg_hi + tolerance:
                return flip
            if rejected is None:
                rejected = (flip, leg_lo, leg_hi, tolerance, spot, search_pct * 100)
        last_nonzero = (i_cur, v_cur)

    if rejected is not None:
        logger.warning(LOG_GAMMA_FLIP_OUT_OF_LEG_RANGE, *rejected)
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


def atm_straddle_vrp(chain_rows: Sequence[dict], spot: float | None) -> float | None:
    """
    입력: **한 북(만기)** 의 체인 스냅샷 행(`db.latest_option_chain()` 형태 — strike/option_type/
         iv/rv_5d 키), 기초자산 스팟.
    계산: 스팟에 가장 가까운 행사가를 ATM으로 잡고, 그 행사가의 **콜과 풋 IV 평균**(ATM 스트래들
         IV)에서 실현변동성을 뺀다. 즉 `calculate_vrp(mean(iv_call, iv_put), rv)`.
    해석: 2026-08-05(운영점검보고서 2026-08-05 §2 이상점 1 / Fix#1) — v6 §11.4 매트릭스의 열
         (저평가/적정/고평가)을 정하는 입력이다. 08-05까지 `SignalFusionEngine.evaluate()`가
         `vrp` 인자를 받지 못해 **기본값 0.0 = 항상 "적정" 열**이었고, 그 결과 매트릭스 3열 중
         2열이 전 이력 도달 불가였다.

         **왜 단일 레그가 아니라 콜·풋 평균인가**: 08-05 실측에서 KIS `hts_ints_vltl`이
         행사가 격자에 따라 계통적으로 튄다 — 먼슬리 북에서 2.5의 홀수배 행사가(1042.5/1047.5/
         1052.5) 콜 IV가 0.57~0.63인데 5의 배수 행사가(1045/1050) 콜은 0.87~0.89였다(같은 분,
         같은 북). 이 상태에서 ATM 콜 하나만 쓰면 **행사가가 한 칸 옮겨가는 것만으로 VRP 부호가
         뒤집힌다.** 실제로 09:18에 콜 단독 IV 0.5423으로 "저평가"(VRP −0.24)가 나왔는데, 그
         분의 풋은 iv가 없어 제외된 것이었다. 콜·풋 평균은 이 격자 아티팩트와 풋 스큐를 동시에
         상쇄하는 교과서적 ATM IV 추정이다.

         **왜 콜·풋이 둘 다 있어야만 산출하는가**: 한쪽만 있으면 위의 상쇄가 일어나지 않아
         평균이 그 한쪽 값 그대로다 — "평균을 냈다"는 형식만 남고 아티팩트는 그대로 통과한다.
         `find_gamma_flip()`이 `iv=0` 레그 하나에 합계 전체를 NaN으로 오염당하던 것(2026-08-03)과
         같은 종류의 방어다: **입력이 규약을 만족하지 않으면 값을 지어내지 말고 None을 낸다.**
    실패 조건: 스팟이 없거나, 체인이 비었거나, ATM 행사가에 유효한(iv>0) 콜·풋이 둘 다 있지
              않거나, 실현변동성이 없거나 0 이하면 None. rv<=0은 KIS `hist_vltl` 미제공을
              뜻하며(08-05 위클리 두 북이 전부 0이었다) 그대로 빼면 VRP가 곧 IV가 돼
              **항상 극단적 고평가**로 나온다.
    """
    if spot is None or not chain_rows:
        return None
    usable = [
        row for row in chain_rows
        if row.get("iv") is not None and float(row["iv"]) > 0.0 and row.get("strike") is not None
    ]
    if not usable:
        return None
    # 동점(스팟이 두 행사가의 정확한 중점)이면 낮은 행사가를 택한다 — 결정론적 선택.
    atm_strike = min(usable, key=lambda row: (abs(float(row["strike"]) - spot), float(row["strike"])))["strike"]
    at_atm = [row for row in usable if float(row["strike"]) == float(atm_strike)]
    by_type = {str(row["option_type"]).lower(): row for row in at_atm}
    if "c" not in by_type or "p" not in by_type:
        return None
    rvs = [
        float(row["rv_5d"]) for row in at_atm
        if row.get("rv_5d") is not None and float(row["rv_5d"]) > 0.0
    ]
    if not rvs:
        return None
    straddle_iv = (float(by_type["c"]["iv"]) + float(by_type["p"]["iv"])) / 2.0
    return calculate_vrp(straddle_iv, sum(rvs) / len(rvs))
