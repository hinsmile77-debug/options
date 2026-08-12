"""레버 상태 집계 + 규약 H — **레버가 꺼진 날의 숫자로 그 레버의 가설을 판정하지 않는다.**

2026-08-12 §1-1 / Fix#6. 그날 「켤 것 — 오늘 단 하나만」으로 지정된 확신도 분모 레버
(`use_effective_member_count`)가 **안 켜졌는데**, 자동 지표 §0은 그것을 켜진 전제로 판정해
「HIGH_CONVICTION이 34건보다 줄어야 한다」 옆에 실측 91건을 찍었다. 표만 보면 분모 전환이
**반대로 작동한 것처럼** 보인다 — 실제로는 그 코드가 한 번도 실행되지 않았다.
"""

from __future__ import annotations

from datetime import date

from mahdi.ops import hypotheses, levers, report

_TARGET = date(2026, 8, 12)


def _levers(**overrides):
    """`collect()` 결과 모양의 최소 픽스처."""
    base = {
        "use_effective_member_count": False,
        "SIGNAL_FUSION_PHASE_OFFSET_SECONDS": 10.0,
    }
    base.update(overrides)
    defaults = {"use_effective_member_count": False, "SIGNAL_FUSION_PHASE_OFFSET_SECONDS": 10.0}
    return {
        "levers": [
            {"key": k, "위치": "테스트", "value": v, "default": defaults[k], "on": v != defaults[k]}
            for k, v in base.items()
        ],
        "git_head": "abc1234",
    }


def test_collect_reads_the_real_lever_values():
    """문서가 아니라 **코드/설정에서** 읽는다 — 08-12에 필요했던 답은 저장소 안에 있었다."""
    state = levers.collect()
    keys = {lever["key"] for lever in state["levers"]}
    assert "use_effective_member_count" in keys
    assert "SIGNAL_FUSION_PHASE_OFFSET_SECONDS" in keys
    assert "OPTION_CHAIN_SLOW_SERIES_CONGESTED_HOURS" in keys
    assert "REGIME_RESTORE_SESSION_WINDOW" in keys
    # 08-12 현재 상태 — 전부 꺼져 있다. **그 사실이 표에 찍히는 것**이 이 fix의 전부다.
    for lever in state["levers"]:
        assert lever["on"] is not None, f"{lever['key']}를 못 읽었다 — levers.py를 고쳐야 한다"


def test_on_is_the_difference_from_the_off_value_not_truthiness():
    """`on`은 「좋다/나쁘다」가 아니라 **「그 코드가 오늘 실행됐는가」** 다."""
    state = _levers(SIGNAL_FUSION_PHASE_OFFSET_SECONDS=25.0)
    assert levers.lever_state(state, "SIGNAL_FUSION_PHASE_OFFSET_SECONDS") is True
    assert levers.lever_state(state, "use_effective_member_count") is False


def test_an_unregistered_lever_reports_unknown_not_off():
    """**「꺼져 있었다」와 「모른다」를 섞지 않는다.** 조치가 다르다(08-06의 「경로 없음」과 같은 구분)."""
    assert levers.lever_state(_levers(), "없는레버") is None
    assert levers.lever_state(None, "use_effective_member_count") is None


# ===== 규약 H — 판정 게이트 =====


_ENTRY = {
    "id": "2026-08-11-eF",
    "가설": "분모를 실질 멤버 수로 바꾸면 확신도 부풀림이 사라진다",
    "검증예정일": "2026-08-12",
    "상태": "pending",
    "전제레버": "use_effective_member_count",
    "예측": [{"metric": "overrun.count", "expect": "<= 5", "역할": hypotheses.ROLE_CLAIM}],
}


def test_a_hypothesis_whose_lever_was_off_is_not_judged():
    """**08-12 재현.** 레버가 꺼져 있으면 실측이 무엇이든 「미실행」이다.

    이 게이트가 없으면 그날처럼 멀쩡한 fix가 반증으로 찍히고, 그 반증을 믿으면
    **고칠 필요 없는 것을 고치게 된다.**
    """
    results = hypotheses.evaluate(
        [_ENTRY], _TARGET, {"overrun": {"count": 99}}, levers=_levers()
    )
    assert [r["verdict"] for r in results] == [hypotheses.VERDICT_LEVER_OFF]
    assert results[0]["lever_off"] == ["use_effective_member_count"]
    # 실측/예측은 **사실이므로 그대로 둔다** — 판정만 무효화한다(주장 지표 없음과 같은 처리).
    assert results[0]["actual"] == 99


def test_the_same_hypothesis_is_judged_once_the_lever_is_on():
    results = hypotheses.evaluate(
        [_ENTRY], _TARGET, {"overrun": {"count": 3}},
        levers=_levers(use_effective_member_count=True),
    )
    assert [r["verdict"] for r in results] == [hypotheses.VERDICT_CONFIRMED]


def test_a_hypothesis_without_a_lever_is_unaffected():
    """레버와 무관한 가설이 대다수다 — 규약 H가 그것들을 건드리면 안 된다."""
    entry = {k: v for k, v in _ENTRY.items() if k != "전제레버"}
    results = hypotheses.evaluate([entry], _TARGET, {"overrun": {"count": 3}}, levers=_levers())
    assert [r["verdict"] for r in results] == [hypotheses.VERDICT_CONFIRMED]


def test_an_unknown_lever_name_does_not_silently_close_the_hypothesis():
    """오타를 「미실행」으로 덮으면 영영 안 고쳐진다 — 판정은 그대로 하고 경고만 낸다."""
    entry = dict(_ENTRY, 전제레버="use_effective_membercount")  # 오타
    results = hypotheses.evaluate([entry], _TARGET, {"overrun": {"count": 3}}, levers=_levers())
    assert results[0]["verdict"] == hypotheses.VERDICT_CONFIRMED
    assert results[0]["lever_unknown"] == ["use_effective_membercount"]


def test_missing_lever_collection_does_not_block_every_judgement():
    """레버를 못 읽은 날에 전 가설이 「미실행」이 되면 08-06 「경로 없음」 사고의 재현이다."""
    results = hypotheses.evaluate([_ENTRY], _TARGET, {"overrun": {"count": 3}}, levers=None)
    assert results[0]["verdict"] == hypotheses.VERDICT_CONFIRMED


# ===== 리포트 =====


def test_report_prints_the_lever_table_and_flags_the_unjudged_entries():
    out = report.render(
        {"date": "2026-08-12"},
        hypotheses=hypotheses.evaluate([_ENTRY], _TARGET, {"overrun": {"count": 99}}, levers=_levers()),
        levers=_levers(),
    )
    assert "## 0. 오늘의 레버 상태" in out
    assert "use_effective_member_count" in out
    assert "abc1234" in out  # 오늘 돌던 코드 — 레버 값만으로는 "어제와 같은 코드였나"에 못 답한다
    assert "미실행 1건" in out
    assert "규약 H" in out
