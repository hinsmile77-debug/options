"""거래 세션 경계 — v6 §4.2 운영 헌법의 시각들 (2026-08-06 §2-2 / Fix#1).

여기서 지키는 것은 값 자체가 아니라 **값들 사이의 관계**다. 컷오프가 평탄화보다 뒤에 있으면
청산할 수 없는 포지션이 생긴다 — 08-06에 15:30 진입 판단이 나왔고 그것을 15:10이 청산할
방법은 없었다.
"""

from __future__ import annotations

from datetime import datetime, time as dtime

from mahdi import session


# ===== 관계 불변식 =====


def test_entry_cutoff_is_before_forced_flat():
    """**이것이 이 파일의 존재 이유다.** 두 값이 뒤집히면 청산 불가능한 진입이 생긴다.

    08-06까지 두 값은 서로 다른 층에 있었다 — 15:10은 `execution/exit_stack.py`에,
    14:50은 **설계 문서에만**. 서로를 볼 수 없으면 이 부등식은 누구도 검사할 수 없다.
    """
    assert session.NEW_ENTRY_CUTOFF < session.FORCED_FLAT_TIME


def test_forced_flat_is_before_closing_auction_and_day_end():
    """강제 평탄화는 연속거래 중에 끝나야 한다 — 단일가 구간에서는 원하는 가격에 못 나간다."""
    assert session.FORCED_FLAT_TIME < session.CLOSING_AUCTION_START < session.TRADING_DAY_END


def test_entry_cutoff_is_after_market_open():
    """컷오프가 개장 전이면 하루 종일 진입이 막힌다(게이트가 아니라 차단기가 된다)."""
    assert session.TRADING_DAY_START < session.NEW_ENTRY_CUTOFF


def test_v6_constitution_values():
    """v6 §4.2 표의 값 그대로 — 바꾸려면 설계 문서를 먼저 고쳐야 한다는 뜻이다."""
    assert session.NEW_ENTRY_CUTOFF == dtime(14, 50)
    assert session.FORCED_FLAT_TIME == dtime(15, 10)


# ===== is_after_entry_cutoff =====


def test_entry_cutoff_boundary_is_inclusive():
    """"14:50 이후 금지"의 자연스러운 독해 — 14:50:00 정각도 금지다.

    초과(`>`)로 두면 그 한 건만 규칙 밖에 놓인다.
    """
    assert session.is_after_entry_cutoff(dtime(14, 49, 59)) is False
    assert session.is_after_entry_cutoff(dtime(14, 50)) is True
    assert session.is_after_entry_cutoff(dtime(14, 50, 1)) is True


def test_entry_cutoff_accepts_datetime_and_time():
    """`is_closing_auction()`과 같은 계약 — datetime도 time도 받는다."""
    assert session.is_after_entry_cutoff(datetime(2026, 8, 6, 15, 30)) is True
    assert session.is_after_entry_cutoff(datetime(2026, 8, 6, 10, 0)) is False


def test_entry_cutoff_has_no_upper_bound():
    """15:45 이후(장외)도 True — 종료가 늦어진 사이클이 '진입 가능'으로 분류되면 안 된다."""
    assert session.is_after_entry_cutoff(dtime(16, 30)) is True


def test_preopen_is_not_after_cutoff():
    """장전은 컷오프 전이다 — 여기서 True가 나오면 하루가 통째로 막힌다."""
    assert session.is_after_entry_cutoff(dtime(7, 31)) is False


# ===== is_forced_flat_time =====


def test_forced_flat_time_boundary_is_inclusive():
    assert session.is_forced_flat_time(dtime(15, 9, 59)) is False
    assert session.is_forced_flat_time(dtime(15, 10)) is True


def test_after_forced_flat_is_also_after_entry_cutoff():
    """부등식의 실제 귀결 — 평탄화 시각을 넘겼는데 진입이 열려 있으면 안 된다.

    08-06에 정확히 이것이 깨져 있었다(15:11~15:30 ENTER 18건).
    """
    for moment in (dtime(15, 10), dtime(15, 30), dtime(15, 44)):
        assert session.is_forced_flat_time(moment)
        assert session.is_after_entry_cutoff(moment)


# ===== 기존 경계와의 정합 =====


def test_closing_auction_and_continuous_trading_are_complementary_in_session():
    assert session.is_continuous_trading(dtime(15, 34)) is True
    assert session.is_closing_auction(dtime(15, 34)) is False
    assert session.is_continuous_trading(dtime(15, 35)) is False
    assert session.is_closing_auction(dtime(15, 35)) is True


def test_preopen_boundary():
    assert session.is_preopen(dtime(8, 59, 59)) is True
    assert session.is_preopen(dtime(9, 0)) is False


# ==========================================================================================
# 2026-08-07(§3-1 / Fix#1·#2) — 현물(유가증권시장) 연속거래 종료 15:20
# ==========================================================================================


def test_equity_spot_is_live_only_during_equity_continuous_trading():
    """지수 스팟은 09:00~15:20에만 실시간이다 — 그 뒤는 단일가/종가의 잔상이다.

    08-04~08-07 나흘 내내 15:21~15:29(9분)가 「지수 정지」로 잡혀 원인 규명 대기 목록에
    올라 있었다. 08-07 실측: 15:20에 975.03으로 멈춰 15:29까지 고정, 같은 시각 선물은
    978.40~980.25로 계속 움직였다. 유가증권시장 장 마감 동시호가(15:20~15:30)다.
    """
    assert session.is_equity_spot_live(dtime(9, 0)) is True
    assert session.is_equity_spot_live(dtime(15, 19, 59)) is True
    assert session.is_equity_spot_live(dtime(15, 20)) is False       # 동시호가 시작
    assert session.is_equity_spot_live(dtime(15, 29)) is False       # 나흘 내내 잡히던 그 구간
    assert session.is_equity_spot_live(dtime(15, 30)) is False       # 이후는 종가 고정
    assert session.is_equity_spot_live(dtime(15, 44)) is False
    assert session.is_equity_spot_live(dtime(8, 59)) is False        # 장전은 별도(is_preopen)


def test_equity_and_derivative_session_ends_are_different_markets():
    """두 경계를 하나로 합치면 15:20~15:35의 25분이 어느 쪽으로든 틀리게 분류된다.

    `CLOSING_AUCTION_START`(15:35)는 **파생상품시장**, `EQUITY_CONTINUOUS_TRADING_END`(15:20)는
    **현물시장**이다. 옵션은 15:45까지 거래되므로 그 25분은 「체인은 살아 있고 스팟만 죽은」
    구간이며, 그것이 Fix#2가 다루는 대상이다.
    """
    assert session.EQUITY_CONTINUOUS_TRADING_END < session.CLOSING_AUCTION_START
    assert session.CLOSING_AUCTION_START < session.TRADING_DAY_END
    # 파생 단일가에는 연속 체결이 없고(OFI 없음), 현물 마감에는 스팟이 없다 — 다른 결핍이다.
    at_1525 = dtime(15, 25)
    assert session.is_continuous_trading(at_1525) is True      # 파생은 아직 연속거래
    assert session.is_equity_spot_live(at_1525) is False       # 현물은 이미 끝났다


def test_equity_spot_window_leaves_the_entry_cutoff_untouched():
    """이 구간은 전부 진입 컷오프(14:50) 이후다 — 신규 진입에는 1비트도 영향이 없다."""
    assert session.NEW_ENTRY_CUTOFF < session.EQUITY_CONTINUOUS_TRADING_END
    assert session.is_after_entry_cutoff(session.EQUITY_CONTINUOUS_TRADING_END) is True
