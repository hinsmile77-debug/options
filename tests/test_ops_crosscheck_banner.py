"""§17 교차 점검 요약을 리포트 맨 위(§0-3)로 — 2026-09-10 제5부 고도화 2 (09-10 §3-6).

## 이 파일이 존재하는 이유

09-10 §3-6이 그날 절벽(15:00~15:02, rows=0 3분)의 원인을 **KIS 귀속**으로 가르면서
§6(백오프)과 §9-1(KIS 응답시간) 두 표를 눈으로 맞춰봤다. 그런데 **그 대조는 §17이 이미
자동으로 해 놓은 것**이었다 — 문서 맨 아래에 있어서 위에서부터 읽는 사람이 못 만났고,
그래서 매 회차가 같은 대조를 손으로 반복했다.

## 이 절이 하지 않는 것

**새로 판정하지 않는다.** 고도화 2의 원안은 *"「오늘 지연은 [KIS/우리] 귀속」이라는 한 줄
배너"*였는데, 그대로 만들면 `crosscheck.py`가 자기 docstring에 적어 둔 규약(*"판정하지 않고
모순을 지적만 한다 — 어느 쪽이 틀렸는지는 사람이 정한다"*)을 정면으로 어긴다. 그래서 §17이
**이미 낸 finding의 `summary`를 그대로 옮겨 인쇄만** 한다.
"""

from __future__ import annotations

from mahdi.ops import report

# 09-10 실측 조합 — 우리 쪽 압력은 낮고(백오프 1.64배) KIS 지연 경고·ReadTimeout은 높다.
_KIS_ATTRIBUTION_DAY = {
    "rest_latency": {"warnings": [{"hour": 9}, {"hour": 15}]},
    "backoff": {"max_multiplier": 1.64},
    "qualitative": {"read_timeout": 281},
}


def _summary(metrics: dict) -> str:
    return "\n".join(report._render_crosscheck_summary(metrics, None))


def test_the_banner_repeats_section_17_verbatim():
    """위와 아래가 **같은 소스**에서 나온다. 두 자리가 갈리면 사람이 어느 쪽을 믿을지
    정해야 하고, 그 순간 이 절은 도움이 아니라 부담이 된다."""
    top = _summary(_KIS_ATTRIBUTION_DAY)
    bottom = "\n".join(report._render_crosschecks(_KIS_ATTRIBUTION_DAY, None))

    assert "백오프 최대 1.64배" in top
    for line in top.splitlines():
        if line.startswith("- §"):
            assert line[len("- "):] in bottom


def test_the_banner_points_down_to_the_evidence():
    """요약은 위, 근거는 아래. 지우고 옮기면 detail(왜 그런가)이 사라진다."""
    assert "아래 §17" in _summary(_KIS_ATTRIBUTION_DAY)


def test_a_quiet_day_still_prints_the_section():
    """규약 C — findings가 0건인 날도 절이 실린다. 「어긋난 것 없음」과 「§17을 안 돌렸다」가
    같은 칸이 되면 안 된다."""
    out = _summary({})
    assert "어긋난 것 없음" in out
    assert "「전부 정상」이 아니다" in out


def test_the_tool_never_invents_the_other_verdict():
    """⛔ 규칙이 안 걸리는 날 도구가 「우리 귀속」이라고 지어내지 않는다 —
    `crosscheck.py`의 규약(*"도구는 판정하지 않는다"*)이 이 자리에서 깨지기 쉽다."""
    quiet = _summary({"backoff": {"max_multiplier": 3.0}, "qualitative": {"read_timeout": 0}})
    assert "우리 귀속" not in quiet
    assert "KIS 귀속" not in quiet


def test_the_section_sits_above_the_headline_table():
    """§0-3은 §1보다 **먼저** 온다 — 표를 다 읽고 나서 만나면 이 절이 존재할 이유가 없다."""
    rendered = report.render(_KIS_ATTRIBUTION_DAY)
    assert rendered.index("## 0-3.") < rendered.index("## 1. ")
    assert rendered.index("## 1. ") < rendered.index("## 17. ")


def test_section_17_body_is_left_intact():
    """본문은 그대로 둔다 — 요약만 위로 올리고 detail은 아래에 남는다."""
    rendered = report.render(_KIS_ATTRIBUTION_DAY)
    assert "08-04 §2-6이 미리 적어둔 판정표" in rendered
    assert "모순을 지적할 뿐 판정하지 않는다" in rendered
