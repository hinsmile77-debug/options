"""§7 레버 경고를 「**지남 AND 꺼짐**」으로 좁힌다 (2026-08-26 §1-3 / P2-1).

08-26에 **다섯 회차가 연속으로** 같은 오탐을 봤다: `use_effective_member_count`는 이미
`true`로 켜져 있는데(수집기가 그 값을 파싱해 화면에 인쇄까지 하고 있었다) 무조건발동일만
보고 「발동 안 됨 ⚠」을 냈다. **읽어 놓고 판정에 안 썼다.**

08-26 고도화 3이 정리한 네 사례 중 **2번**이 이 자리다.
⚠ **경고를 없애는 것이 아니라 등급을 내리는 것이다** — 날짜가 안 옮겨진 것은 사실이다.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR = PROJECT_ROOT / "docs" / "동작점검" / "tools" / "collect_evidence.py"


@pytest.fixture(scope="module")
def collector():
    spec = importlib.util.spec_from_file_location("collect_evidence_lever", COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "key,line,expected",
    [
        # yaml 불리언
        ("use_effective_member_count", "use_effective_member_count: true", True),
        ("use_effective_member_count", "use_effective_member_count: false", False),
        # yaml 숫자 — 0 = 재진입 쿨다운 미설정
        ("reentry_cooldown_minutes", "  reentry_cooldown_minutes: 0", False),
        ("reentry_cooldown_minutes", "  reentry_cooldown_minutes: 30", True),
        # 파이썬 None = 전역값 사용 = OFF (주석이 붙어 있다)
        ("OPTION_CHAIN_READ_TIMEOUT_SECONDS",
         "OPTION_CHAIN_READ_TIMEOUT_SECONDS: float | None = None  # None = 전역값(4.0초) 사용 = 레버 OFF",
         False),
        ("OPTION_CHAIN_READ_TIMEOUT_SECONDS",
         "OPTION_CHAIN_READ_TIMEOUT_SECONDS: float | None = 6.0", True),
        # 파이썬 빈 dict — 타입 주석과 대입이 한 줄에 같이 있다
        ("OPTION_CHAIN_SLOW_SERIES_CONGESTED_HOURS",
         "OPTION_CHAIN_SLOW_SERIES_CONGESTED_HOURS: dict[int, int] = {}", False),
        ("OPTION_CHAIN_SLOW_SERIES_CONGESTED_HOURS",
         "OPTION_CHAIN_SLOW_SERIES_CONGESTED_HOURS: dict[int, int] = {10: 2}", True),
        # 파이썬 불리언
        ("REGIME_RESTORE_SESSION_WINDOW", "REGIME_RESTORE_SESSION_WINDOW = False", False),
        ("REGIME_RESTORE_SESSION_WINDOW", "REGIME_RESTORE_SESSION_WINDOW = True", True),
    ],
)
def test_each_lever_family_is_read_with_its_own_notion_of_off(collector, key, line, expected):
    """⚠ **표기가 계열별로 갈린다.**

    yaml 불리언 · yaml 숫자 · 파이썬 `None` · 빈 dict · 파이썬 `False`가 전부 섞여 있다.
    하나의 「거짓처럼 보이는 값」 규칙으로 뭉치면 `0`이 유효한 설정인 레버에서 틀린다.
    """
    assert collector.lever_is_on(key, line) is expected


def test_the_live_repo_lever_that_caused_five_false_alarms_reads_as_on(collector):
    """08-26에 다섯 회차가 본 그 레버 — **실물 파일에서** 켜짐으로 읽혀야 한다."""
    line = next(
        ln.strip() for ln in
        (PROJECT_ROOT / "mahdi" / "config" / "strategy_params.yaml").read_text(
            encoding="utf-8"
        ).splitlines()
        if "use_effective_member_count" in ln and not ln.strip().startswith("#")
    )
    assert collector.lever_is_on("use_effective_member_count", line) is True


def test_an_unreadable_line_is_none_not_off(collector):
    """⚠ **`None`과 `False`는 다른 값이다**(규약 C).

    전자는 「이 도구가 못 읽었다」이고 후자는 「레버가 꺼져 있다」다. 못 읽은 것을 「꺼짐」으로
    접으면 이 fix가 만들려던 등급 구분이 도로 사라진다 — 그러면 오탐이 다시 시작된다.
    """
    assert collector.lever_is_on("use_effective_member_count", "값 없는 줄") is None
    assert collector.lever_is_on("use_effective_member_count", "use_effective_member_count") is None
    assert collector.lever_is_on("use_effective_member_count", "use_effective_member_count: ") is None


def test_an_unknown_lever_is_never_judged(collector):
    """`LEVER_OFF_LITERALS`에 없는 레버는 **판정하지 않는다** — 추측이 곧 오탐이다."""
    assert collector.lever_is_on("SOME_NEW_LEVER", "SOME_NEW_LEVER = 0") is None


def test_every_registered_lever_has_an_off_literal(collector):
    """§7이 인쇄하는 레버 전부가 「꺼짐」 정의를 가져야 한다.

    없으면 그 레버는 영영 「판정 못 함」으로 남고, 그 칸이 있는 줄 모르는 채 지나간다.
    """
    for key, _rel in collector.LEVER_KEYS:
        assert key in collector.LEVER_OFF_LITERALS, f"{key}: 꺼짐 정의가 없다"
