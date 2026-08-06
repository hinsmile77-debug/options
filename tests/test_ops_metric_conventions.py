"""지표 규약의 **기계적 강제** — 2026-08-04(운영점검보고서 고도화#1).

## 왜 규약이 필요한가

같은 종류의 결함이 반복해서 났고, 매번 개별 수정으로 끝냈다:

  **로그 계약 파손** (규약 A)
    08-03에 `rest_client`의 로그 세 곳을 바꾸자 `log_metrics` 파서 셋이 조용히 죽었다.
    08-04 리포트는 그것을 `느린 REST 호출 0건(▼933 ✅)`이라는 **개선으로 표시**했다.

  **체인 조회에 시각 경계 누락** (규약 B) — **세 번 반복됐다**
    1) 08-03: `latest_option_chain()` — 시간 범위도 만기 필터도 없어 246레그 중 오늘
       수집분이 10개, 최고령이 4주였다.
    2) 08-04: `latest_expiry_liquidity()` — 같은 패턴. COCKPIT이 어제 만기를 표시했다.
    3) 08-04: `book_gamma_map()` — **08-03 수정과 같은 날 작성된 새 파일에서 재발.**
       "장 마지막 스냅샷"이라 적어놓고 하루치 전 행사가를 합쳐 놓았다.

개별 수정으로는 네 번째가 온다. 그래서 규약을 테스트로 못박는다.

  규약 A  로그 문구는 emit 측 모듈 상수다 → `test_ops_log_metrics_contract.py`
  규약 B  체인 스냅샷은 `db._chain_snapshot()` 하나만 만든다 → **이 파일**
  규약 C  "0건" 보고는 증명을 동반한다 → `log_metrics._parser_audit()`
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAHDI = PROJECT_ROOT / "mahdi"

# 체인 스냅샷의 지문: (만기, 행사가, 콜/풋)별 최신 1건. 이 형태를 쓰는 SQL은 곧 체인 스냅샷이다.
_CHAIN_SNAPSHOT_SQL = re.compile(
    r"DISTINCT\s+ON\s*\(\s*expiry\s*,\s*strike\s*,\s*option_type\s*\)", re.IGNORECASE
)

# 규약 B 허용목록 — 여기 없는 파일이 체인 스냅샷 SQL을 쓰면 테스트가 막는다.
_CHAIN_SNAPSHOT_ALLOWLIST = {
    # 유일한 정본. 신선도 창(CHAIN_SNAPSHOT_MAX_AGE_MINUTES)과 만기 경계가 여기 한 곳에 있다.
    "data/db.py",
    # 2026-08-04 §2-3/고도화#4 — **의도적인 예외**: "그날 방문한 전 행사가"를 보는 것이
    # 이 함수의 존재 이유다(신선도 창을 적용하면 광폭 실험 자체가 불가능하다). 그래서
    # 함수명·리포트 표기 둘 다 "광폭"임을 명시하고, 시각 경계가 없다는 사실을 docstring에 적었다.
    "ops/db_metrics.py",
}


def _python_sources() -> list[Path]:
    return [p for p in MAHDI.rglob("*.py") if "__pycache__" not in p.parts]


def _without_comments(path: Path) -> str:
    """주석 줄을 뺀 소스.

    아래 "이 문자열이 있으면 안 된다" 류의 검사는 **렌더되는 리터럴**을 겨눈다. 그런데 이
    리포지터리는 고친 결함을 주석에 그대로 인용하는 습관이 있어(그게 다음 사람을 살린다),
    주석까지 훑으면 *결함을 설명한 문장* 때문에 테스트가 깨진다 — 기록을 지우게 만드는
    테스트는 잘못된 테스트다.
    """
    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def test_chain_snapshot_sql_lives_in_one_place():
    """규약 B: 체인 스냅샷 SQL은 `db._chain_snapshot()` 하나만 만든다.

    이 결함은 2026-08-03~04에 **세 번** 재발했다(모듈 docstring 참고). 새로 SQL을 쓰는 대신
    `db.latest_option_chain()` / `db.option_chain_as_of()`를 쓰면 신선도 창과 만기 경계가
    공짜로 따라온다 — 그리고 라이브 판단과 리포트가 **같은 체인**을 보게 된다.
    """
    offenders = sorted(
        str(path.relative_to(MAHDI)).replace("\\", "/")
        for path in _python_sources()
        if _CHAIN_SNAPSHOT_SQL.search(_without_comments(path))
    )
    unexpected = [p for p in offenders if p not in _CHAIN_SNAPSHOT_ALLOWLIST]
    assert not unexpected, (
        f"체인 스냅샷 SQL이 새 위치에 생겼다: {unexpected}. "
        "db.latest_option_chain()/option_chain_as_of()를 쓰거나, 정말 예외라면 "
        "_CHAIN_SNAPSHOT_ALLOWLIST에 **이유와 함께** 추가할 것(2026-08-04 고도화#1 규약 B)."
    )


def test_chain_snapshot_has_both_freshness_and_expiry_boundaries():
    """정본 SQL이 두 경계를 실제로 걸고 있는지 — 규약이 가리키는 대상 자체를 지킨다."""
    from mahdi.data import db

    sql = db._CHAIN_SNAPSHOT_SQL
    assert "timestamp <= %s" in sql, "as_of 상한이 없다"
    assert "timestamp >= %s" in sql, "신선도 하한이 없다(2026-08-03 §2-2에서 4주치가 섞였다)"
    assert "expiry >= %s" in sql, "만기 경계가 없다(만기 지난 레그가 t_years=0으로 통과한다)"
    assert db.CHAIN_SNAPSHOT_MAX_AGE_MINUTES > 0


def test_slow_call_threshold_is_not_duplicated_as_a_literal():
    """규약 A의 부수 규칙: 임계값을 문서 문자열에 박아두지 않는다.

    08-03에 임계를 3.0 → 5.0으로 올렸는데 `report.py`의 "임계(3초) 초과 호출 없음" 문자열만
    3초로 남아 08-04 리포트 §9가 **틀린 임계를 인쇄**했다. 상수를 인용하면 갈라질 수 없다.
    """
    text = _without_comments(MAHDI / "ops" / "report.py")
    assert "임계(3초)" not in text
    assert "SLOW_CALL_LOG_THRESHOLD_SECONDS" in text


def test_theoretical_member_ceiling_is_derived_not_hardcoded():
    """규약 A의 부수 규칙: "이론 최대 N개"를 손으로 적지 않는다.

    08-04까지 `db_metrics`가 3으로 하드코딩하고 주석에 *"orderflow는 파이프라인 미구현"* 이라고
    적어뒀는데 그 전제가 사실이 아니었다(`market_raw_1m.ofi`는 선물 410분 전부 채워져 있었다).
    그 결과 죽은 멤버 하나가 **지표의 분모 안으로 숨었다.**
    """
    from mahdi.fusion.signal_layer import IMPLEMENTED_MEMBER_FIELDS
    from mahdi.ops import db_metrics

    assert db_metrics.SIGNAL_REACH_WARNINGS["member_count_max_min"] == len(IMPLEMENTED_MEMBER_FIELDS)
    report_text = _without_comments(MAHDI / "ops" / "report.py")
    assert "이론 최대 3개" not in report_text


def test_chain_age_warning_threshold_stays_below_the_snapshot_window():
    """임계는 **물리적 상한 아래**에 있어야 한다 — 위에 놓이면 구조적으로 못 울린다.

    2026-08-04 Fix#6b 때는 임계를 창에서 파생시켰다(`창 x 1.5` = 450초). 그런데 그 파생은
    임계를 **창보다 크게** 만들어, 스냅샷 창(300초)이 이미 상한인 값에 450초 임계를 건 꼴이었다.
    2026-08-06 §2-4 실측: 08-05 하루의 52%가 250초였는데 경고는 한 번도 안 울렸다.

    2026-08-06 Fix#4로 이 지표는 **먼슬리 북 하나**의 나이가 됐고(먼슬리는 매 분 폴링된다)
    건강한 값은 70~130초다. 임계 180초 = "먼슬리가 2사이클 이상 밀렸다".

    창을 바꾸든 임계를 바꾸든, 임계가 창 위로 올라가면 이 테스트가 깨진다 — 그것이 요점이다
    (2026-08-05 §2-4가 느린 호출 임계에서 겪은 것과 같은 계열의 불변식).
    """
    from mahdi.data import db
    from mahdi.ops import db_metrics

    window_seconds = db.CHAIN_SNAPSHOT_MAX_AGE_MINUTES * 60
    assert db_metrics.SIGNAL_REACH_WARNINGS["chain_age_seconds_max"] < window_seconds, (
        "체인 나이 경고 임계가 스냅샷 창 위에 있으면 그 경고는 구조적으로 울리지 않는다"
    )


def test_strike_window_design_matches_the_live_poller():
    """`db_metrics`의 설계 창 상수가 관측 루프의 실제 값과 같아야 한다(고도화#3).

    두 값이 갈라지면 "창 정합률"이 조용히 틀린 기준으로 계산된다.
    """
    from mahdi import main
    from mahdi.ops import db_metrics

    assert db_metrics.STRIKE_WINDOW_EACH_SIDE == main.STRIKES_EACH_SIDE
    assert db_metrics.KOSPI200_STRIKE_INTERVAL == main.KOSPI200_OPTION_STRIKE_INTERVAL


def test_design_leg_counts_are_derived_from_the_live_poller():
    """2026-08-05 §2-7 — 설계 레그 수 상수가 관측 루프의 실제 구독 폭과 같아야 한다.

    `ops`는 `mahdi.main`을 import하지 않는다(관측 계층이 오케스트레이터를 끌고 오면 안 된다).
    그 대가로 상수가 양쪽에 존재하게 되므로, **갈라지지 않는 것은 이 테스트가 지킨다** —
    `log_metrics`가 순수 파서로 남으면서 계약 테스트로 묶이는 것과 같은 구조다.

    갈라지면 "먼슬리 레그 완전성"(§12)이 조용히 틀린 분모로 계산된다: 예컨대 `STRIKES_EACH_SIDE`를
    2 → 3으로 넓히면 설계값은 14인데 상수가 10에 머물러 **미달 분이 0으로 보고된다.**
    """
    from mahdi import main
    from mahdi.ops import db_metrics

    strikes = 2 * main.STRIKES_EACH_SIDE + 1
    assert db_metrics.MONTHLY_LEGS_PER_CYCLE_DESIGN == strikes * 2
    # 한 사이클 = 먼슬리 1북 + 위클리 1북(격분, OPTION_CHAIN_SLOW_SERIES_PHASE로 짝/홀 분리).
    assert len(main.OPTION_CHAIN_SLOW_SERIES_PHASE) == main.OPTION_CHAIN_SLOW_SERIES_EVERY_N_MINUTES
    assert db_metrics.CHAIN_LEGS_PER_CYCLE_DESIGN == strikes * 2 * 2


def test_gamma_flip_minimum_legs_is_shared_not_copied():
    """§12의 "BS 최소 미달 분"은 `find_gamma_flip`이 실제로 쓰는 임계와 같아야 한다."""
    from mahdi.features import options_intel
    from mahdi.ops import db_metrics

    assert db_metrics.GAMMA_FLIP_MIN_LEGS is options_intel.GAMMA_FLIP_MIN_LEGS


def test_closing_auction_boundary_has_a_single_source():
    """규약 B — 종가 단일가 경계를 아는 곳은 `mahdi.session` 하나여야 한다(2026-08-05 §2-8).

    08-05까지 이 지식은 **화면에만** 있었다(`dashboard/data_source._CLOSING_AUCTION_START`).
    그래서 판단 경로는 15:36~15:44에 `orderflow_ofi_vpin`이 죽는 것을 "데이터 없음"으로만
    기록했고, 장애와 시장 구조가 구분되지 않았다.
    """
    from mahdi import main, session
    from mahdi.dashboard import data_source
    from mahdi.ops import db_metrics

    assert data_source._CLOSING_AUCTION_START is session.CLOSING_AUCTION_START
    # 사유 문자열도 한 곳에서 온다 — 갈라지면 §14-1의 "구조적" 열이 조용히 0이 된다.
    assert main.MEMBER_UNAVAILABLE_CLOSING_AUCTION == db_metrics.STRUCTURAL_UNAVAILABLE_REASON
    # 2026-08-06 Fix#5 — 장전 스팟 부재도 같은 계열의 구조적 사유다. 둘 다 집합 안에 있어야
    # §14-1이 그 분들을 가용률에서 분리해 낸다.
    assert main.MEMBER_UNAVAILABLE_CLOSING_AUCTION in db_metrics.STRUCTURAL_UNAVAILABLE_REASONS
    assert main.MEMBER_UNAVAILABLE_PREOPEN in db_metrics.STRUCTURAL_UNAVAILABLE_REASONS


def test_entry_cutoff_is_known_in_exactly_one_place(monkeypatch):
    """규약 B — 진입 컷오프(14:50)와 그 사유 문자열의 단일 출처 (2026-08-06 §2-2 / Fix#1).

    08-06에 이 지식은 **설계 문서에만** 있었다. v6 §4.2가 `14:50 이후 신규 진입 금지`를 명문으로
    적어뒀는데 코드 어디에도 없어서 그날 21건이 통과했다. 이제 세 층이 이것을 안다:

        판단(`main`) · 리스크(`RiskEngine`) · 지표(`ops.db_metrics`)

    셋이 **같은 상수와 같은 사유 문자열**을 써야 §13의 불변식이 실제로 그 분들을 센다.
    """
    from mahdi import main, session
    from mahdi.ops import db_metrics
    from mahdi.risk.engine import RiskEngine
    from mahdi.risk.limits import AccountState
    from mahdi.risk.sizing import PositionSizingInput
    from mahdi.risk.circuit_breaker import MarketConditions

    # 사유 문자열 — 갈라지면 §13이 0을 내면서 "게이트가 잘 돈다"고 말한다.
    assert main._REJECT_REASON_ENTRY_CUTOFF == db_metrics.ENTRY_CUTOFF_REJECT_REASON

    # 리스크 엔진이 내는 사유도 같은 문자열인지 — 상수 비교가 아니라 **실제 반환값**으로 본다.
    decision = RiskEngine().evaluate_entry(
        PositionSizingInput(
            base_size=1.0, regime_confidence=1.0, signal_quality=1.0, target_vol=0.01,
            realized_vol=0.01, liquidity_score=1.0, drawdown_pct=0.0,
            portfolio_capacity_remaining_pct=1.0,
        ),
        AccountState(
            daily_pnl_pct=0.0, weekly_pnl_pct=0.0, drawdown_pct=0.0, same_direction_positions=0
        ),
        "any_strategy",
        MarketConditions(),
        now=session.NEW_ENTRY_CUTOFF,
    )
    assert decision.reject_reasons == [db_metrics.ENTRY_CUTOFF_REJECT_REASON]

    # 시각 자체는 `mahdi.session`에서만 온다 — 어느 모듈도 14:50을 자기 리터럴로 들지 않는다.
    assert session.NEW_ENTRY_CUTOFF < session.FORCED_FLAT_TIME


# ===== 규약 D — 품질 지표는 감시 대상과 독립한 입력을 쓴다 (2026-08-05 고도화#1) =====
#
# 2026-08-01에 **생존 신호**에 대해 세운 원칙이 있다: *"생존 신호는 감시 대상과 독립한 타이머에서
# 나와야 한다"*(07-30 CB 하트비트에서 배웠다 — 감시 대상 이벤트에 얹으면 "이벤트가 없으면 신호도
# 멈춰" 죽은 것과 구분되지 않는다).
#
# 08-05 §2-3은 그 원칙이 **타이머만이 아니라 모든 입력에 적용된다**는 것을 보여줬다.
# §14-2 「ATM 정합률」은 08-04 고도화#3이 *"08-03의 하루치 외가격 사고를 이 지표 하나가 잡는다"*
# 며 만든 지표인데, 08-05에 **같은 종류의 사고가 90분간 재발했고 88.1%로 통과시켰다.**
# 이유는 단순하다: 그 지표가 ATM을 계산할 때 쓰는 스팟이 **감시 대상이 적재한 바로 그 값**이다.
# 앞 75분은 스팟도 행사가도 틀렸는데 둘이 서로 일치해서 정합으로 세어졌다.
#
#   규약 D — 품질 지표는 자기가 감시하는 파이프라인의 산출물을 입력으로 쓰지 않는다.
#            쓸 수밖에 없으면 **독립 소스와의 교차 검증을 지표에 함께 넣는다.**
#
# 규약 A(로그 문구는 상수) / B(체인 조회는 한 함수) / C(0건 보고는 증명 동반)에 이어 네 번째다.
# A~D는 전부 "같은 것을 두 번 쓰지 마라"의 변주이고, E(주장/대가 지표)는 "한쪽만 재지 마라"다.


def test_atm_coverage_carries_an_independent_cross_check():
    """§14-2는 감시 대상의 스팟을 쓸 수밖에 없다 — 그래서 **독립 소스 값을 함께 낸다.**

    선물 WS 1분봉은 WebSocket 체결 스트림으로 들어와 옵션체인 REST 폴러와 경로가 겹치지 않는다.
    두 값을 **같은 분 집합**에서 내야 격차가 스팟 소스 때문임이 확정된다(분모가 다르면 차이가
    소스 때문인지 분 집합 때문인지 구분되지 않는다).
    """

    from mahdi.ops import db_metrics

    source = inspect.getsource(db_metrics.strike_window_quality)
    assert "market_raw_1m" in source, "독립 소스(선물 WS)를 안 쓰면 규약 D의 교차 검증이 없는 것이다"
    # 같은 CTE 안에서 두 ATM을 함께 세는가 — 분 집합이 갈리면 격차의 의미가 사라진다.
    assert "atm_fut" in source and "atm_idx" in source


def test_quality_metrics_declare_their_input_independence():
    """**규약 D 전수 감사** — 리포트의 품질 지표마다 "입력이 감시 대상과 같은가"를 명시한다.

    이 테스트는 값을 검사하지 않는다. 감사 결과를 **코드 안에 고정**해, 새 품질 지표를 추가하는
    사람이 이 표에 한 줄을 더하면서 스스로 그 질문에 답하게 만드는 것이 목적이다.
    빠뜨리면 여기서 깨진다.
    """
    from mahdi.ops import db_metrics

    # 지표 → (감시 대상과 입력을 공유하는가, 공유한다면 무엇으로 교차 검증하는가)
    audit = {
        # 스팟을 감시 대상(옵션체인 폴러)이 적재한다 → 선물 WS로 교차 검증(08-05 고도화#1).
        "strike_window_quality": (True, "atm_covered_pct_by_futures"),
        # 스팟의 두 소스를 대조하는 것이 이 지표의 정의 자체다 — 태생적으로 독립이다.
        "spot_source_divergence": (False, None),
        # 판단 산출물(signal_decisions)을 읽지만, 감시 대상은 **체인 수집 파이프라인**이라
        # 입력이 다르다. 다만 §15(광폭 OI 지형)와의 교차 모순 검사를 crosscheck가 맡는다.
        "signal_reach": (False, None),
        # option_analysis_1m 적재 자체를 세는 지표 — 감시 대상이 곧 입력이지만 **"있는가"만**
        # 재므로 오염된 값이 통과할 여지가 없다(0행은 0행이다).
        "chain_minute_coverage": (False, None),
        "monthly_leg_completeness": (False, None),
        # 판단 행이 곧 입력이자 감시 대상이다. 점수의 "옳음"은 못 재고 **분포와 일치율**만 잰다 —
        # 그것이 이 지표가 판정을 주장하지 않는 이유다.
        "member_score_quality": (True, None),
        "member_availability": (True, None),
    }
    for name, (shares_input, cross_check_key) in audit.items():
        assert hasattr(db_metrics, name), f"{name}: 감사 표에 있는데 함수가 없다"
        if shares_input and cross_check_key:
            assert cross_check_key in inspect.getsource(getattr(db_metrics, name)), (
                f"{name}: 입력을 공유하는데 교차 검증 키({cross_check_key})가 없다"
            )
