"""`report._render_order_notices()` — 체결통보 절의 **조건 분기**를 못박는다.

2026-08-26 (08-26 §1-16 / P1-6). 그날 이 함수가 **아홉 번 구독에 성공한 통로를
「구독하지 않았다」로 인쇄했다.** 조기 반환이 `not_configured and total == 0`만 보고
`subscribed`(=9)를 읽어 놓고 안 썼기 때문이다.

08-24·08-25까지는 `subscribed = 0`이라 **분기가 우연히 옳았다.** 그래서 이 파일은
두 케이스를 **함께** 고정한다 — 고친 쪽만 고정하면 「우연히 옳던 날」의 출력이 조용히
바뀌어도 아무도 모른다. 그 옛 케이스의 문자열 불변이 이 fix의 회귀 판정선이다.
"""

from __future__ import annotations

from mahdi.ops import report


def _notices(total: int = 0, mismatched: int = 0, distribution: dict | None = None) -> dict:
    return {
        "order_notices": {
            "notices": total,
            "field_count_mismatched": mismatched,
            "field_count_distribution": distribution or {},
        }
    }


def _log(subscribed: int = 0, not_configured: int = 0, stream_down: int = 0) -> dict:
    return {
        "qualitative": {
            "order_notice_subscribed": subscribed,
            "order_notice_not_configured": not_configured,
            "order_notice_stream_down": stream_down,
        }
    }


# ===== 회귀 판정선 — 08-24·08-25의 날은 **한 글자도 안 바뀐다** =====

_LEGACY_LINES = [
    "- **체결통보를 구독하지 않았다** — `KIS_HTS_ID`가 비어 있다.",
    "",
    "> **주문이 나가도 실시간 체결 알림이 없다.** 체결 확인은 REST 조회"
    "(`get_order_fill_status`)로만 이뤄지고, v6 §13.2의 「체결통보-REST 이중 확인」은 "
    "절반만 성립한다. `.env`에 `KIS_HTS_ID`를 넣고 재기동하면 켜진다.",
    "> 경고 1건(기동당 1건이 정상 — 재연결 루프에 들어가기 전에 끝낸다).",
    "",
]


def test_not_configured_day_output_is_byte_identical_to_08_25():
    """`subscribed = 0 · not_configured = 1 · total = 0` — **08-25의 그 날**이다.

    이 fix가 건드리면 안 되는 유일한 케이스이고, 그래서 문자열 전체를 통째로 비교한다.
    한 칸이라도 바뀌면 여기서 깨진다.
    """
    out = report._render_order_notices(_notices(total=0), _log(subscribed=0, not_configured=1))
    assert out == _LEGACY_LINES


# ===== 08-26의 첫 실물 — 「둘 다 참」인 날 =====


def test_subscribed_day_does_not_claim_it_never_subscribed():
    """`not_configured = 1 · subscribed = 9 · total = 0` — 08-26 실측이다.

    **08-26에 이 함수가 낸 답이 정반대였다.** 구독이 아홉 번 성립했는데 「구독하지
    않았다」로 인쇄했고, 그 줄은 실거래 전환을 정할 때 사람이 읽는 자리다.
    """
    out = report._render_order_notices(
        _notices(total=0), _log(subscribed=9, not_configured=1, stream_down=8)
    )
    body = "\n".join(out)
    assert "체결통보를 구독하지 않았다" not in body
    assert "구독 성립 **9회**" in body
    assert "스트림 끊김 **8회**" in body


def test_subscribed_day_says_the_config_changed_mid_session():
    """경고와 구독이 **같은 날에 공존한다**는 사실 자체가 인쇄돼야 한다.

    ⚠ 경고 줄을 지우는 것이 아니다. `not_configured = 1`은 그날 **첫 기동의 사실**이고,
    지우면 「기동 중 설정이 바뀌었다」를 다음 사람이 못 읽는다.
    """
    body = "\n".join(
        report._render_order_notices(
            _notices(total=0), _log(subscribed=9, not_configured=1, stream_down=8)
        )
    )
    assert "오늘 기동 중 설정이 바뀌었다" in body
    assert "경고 1건" in body
    assert "재기동 이전의 사실" in body


def test_clean_subscribed_day_has_no_mid_session_note():
    """경고가 0건이면 그 단서 줄은 **안 나온다** — 08-27 이후의 정상적인 날이다."""
    body = "\n".join(
        report._render_order_notices(_notices(total=0), _log(subscribed=1, not_configured=0))
    )
    assert "오늘 기동 중 설정이 바뀌었다" not in body
    assert "구독 성립 **1회**" in body


def test_zero_total_still_explains_what_the_zero_means():
    """구독이 성립한 날의 통보 0건은 「체결이 없었다」다 — 그 문장이 살아 있어야 한다."""
    body = "\n".join(
        report._render_order_notices(_notices(total=0), _log(subscribed=9, not_configured=0))
    )
    assert "구독 성립 횟수가 이 0의 뜻을 가른다" in body


def test_missing_db_section_is_not_confused_with_a_silent_stream():
    """`db.order_notices` 절 자체가 없는 날 — **0건이 아니라 「못 셌다」**이다(규약 C)."""
    out = report._render_order_notices({}, _log(subscribed=9))
    assert out[0].startswith("> 체결통보 집계 불가")
