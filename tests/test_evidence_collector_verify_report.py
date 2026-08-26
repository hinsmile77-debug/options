"""`--verify-report` — **부딪히면 소리를 낸다** (2026-08-26 제5부 고도화 5).

08-26에 이 체제가 저지른 실수 셋은 전부 「증거를 안 본 것」이 아니라 **「눈앞의 증거를 잘못
읽은 것」**이다. 그중 둘이 기계가 잡을 수 있는 형태였다:

  ② 14:30 P0-1이 **폐기 목록의 Slack 항목**을 되살렸다 — §8-2가 그 줄을 같은 파일 안에
     놓았는데도. **08-19 장후 §4 Fix#1에 이어 두 번째다.**
  ③ 14:30 P0-1이 **존재하지 않는 파일**(`mahdi/ops/alerting.py`)을 지목했다.

⚠ **이 모드는 판정하지 않는다.** 걸린 줄은 「틀렸다」가 아니라 「봐야 한다」다.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR = PROJECT_ROOT / "docs" / "동작점검" / "tools" / "collect_evidence.py"

_NEXT_TODO = """\
# 다음 할 일

## 폐기·종결된 안건 (다시 올리지 말 것)

- [x] **Slack 알림 — 2026-08-01 결정, 보류 유지.** 매 점검 보고서에서 다시 올리지 말 것.
- [x] **「GEX 광폭 체인」 — 2026-08-04 폐기.**
"""


@pytest.fixture(scope="module")
def collector():
    spec = importlib.util.spec_from_file_location("collect_evidence_verify", COLLECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def repo(tmp_path):
    (tmp_path / "docs" / "dev_memory").mkdir(parents=True)
    (tmp_path / "docs" / "dev_memory" / "NEXT_TODO.md").write_text(
        _NEXT_TODO, encoding="utf-8", newline="\n"
    )
    (tmp_path / "mahdi" / "ops").mkdir(parents=True)
    (tmp_path / "mahdi" / "notify.py").write_text("", encoding="utf-8")
    return tmp_path


def _report(repo: Path, body: str) -> Path:
    p = repo / "보고서.md"
    p.write_text(body, encoding="utf-8", newline="\n")
    return p


def test_it_catches_the_slack_revival_that_happened_twice(collector, repo):
    """**08-19와 08-26에 같은 실수가 났다.** 목록을 눈앞에 두는 것만으로는 부족했다."""
    report = _report(repo, "\n".join([
        "## 제4부 — Fix 구현계획 (P0 / P1 / P2)",
        "",
        "**P0-1. 워치독 DEGRADED 5분 초과 시 Slack 경보 1회**",
        "",
    ]))
    out = collector.verify_report(repo, report)

    assert "폐기 목록 키워드와 부딪힌다" in out
    assert "`Slack`" in out


def test_the_same_word_outside_a_fix_section_is_not_flagged(collector, repo):
    """Fix 절 밖의 서술은 안 본다 — 매일 나오는 「토글: 꺼짐」 줄까지 걸면 이 모드가 죽는다."""
    report = _report(repo, "\n".join([
        "## 제3부 — 장후",
        "",
        "지표 §16의 「Slack 경보 토글: 꺼짐」을 확인했다.",
        "",
    ]))
    assert "부딪히는 줄 없음" in collector.verify_report(repo, report)


def test_a_missing_next_todo_says_it_did_not_run(collector, tmp_path):
    """⚠ **「안 걸렸다」가 아니라 「안 돌았다」**로 인쇄한다(규약 C).

    못 읽은 것을 통과로 접으면 이 모드가 매일 조용히 성립한다 — 08-19 Fix#5가
    `discarded_items()`에서 이미 지킨 규약이다.
    """
    (tmp_path / "docs").mkdir()
    report = _report(tmp_path, "## 제4부 — Fix 구현계획\n\nSlack 경보를 켠다\n")
    out = collector.verify_report(tmp_path, report)

    assert "못 읽었다" in out
    assert "이 대조는 **안 돌았다.**" in out


def test_it_catches_a_path_that_does_not_exist(collector, repo):
    """`mahdi/ops/alerting.py` — **그 파일은 없다.** 알림 모듈은 `mahdi/notify.py`다."""
    report = _report(repo, "- 파일: `mahdi/ops/alerting.py`\n")
    out = collector.verify_report(repo, report)

    assert "리포에 없다" in out
    assert "`mahdi/ops/alerting.py`" in out


def test_an_existing_path_is_not_flagged(collector, repo):
    report = _report(repo, "- 파일: `mahdi/notify.py`\n")
    assert "전부 실재한다" in collector.verify_report(repo, report)


def test_a_bare_filename_is_not_treated_as_a_path(collector, repo):
    """⚠ 보고서는 같은 파일을 `notify.py`로도 부른다 — **그것은 약칭이지 경로가 아니다.**

    약칭까지 「없는 파일」로 세면 **진짜 하나가 그 다섯에 묻힌다**(08-26 실측: 7건 중 5건이
    약칭이었다). 「틀린 경보는 진짜 경보를 죽인다」가 여기서도 그대로다.
    """
    report = _report(repo, "`report.py`와 `collect_evidence.py`를 고친다\n")
    assert "전부 실재한다" in collector.verify_report(repo, report)


def test_a_line_number_suffix_does_not_break_the_match(collector, repo):
    """보고서는 `mahdi/ops/report.py:2232` 형태로도 쓴다."""
    report = _report(repo, "- 파일: `mahdi/ops/alerting.py:57`\n")
    assert "`mahdi/ops/alerting.py`" in collector.verify_report(repo, report)


def test_a_missing_report_says_so_instead_of_passing_silently(collector, repo):
    out = collector.verify_report(repo, repo / "없는파일.md")
    assert "파일이 없다" in out


def test_it_never_claims_the_citation_check_ran(collector, repo):
    """⛔ **(가) 인용 대조는 아직 없다** — 「검사했는데 안 걸렸다」로 읽히면 안 된다.

    못 잡는 판정기를 실으면 거짓 안심이 남는다는 것이 08-19 Fix#5의 결론이었다.
    안 실은 것은 **안 실었다고 적는다.**
    """
    out = collector.verify_report(repo, _report(repo, "아무 내용\n"))
    assert "(가) 인용 대조는 아직 없다" in out


def test_the_mode_exits_zero_even_when_it_finds_things(collector, repo, capsys):
    """오탐 하나가 회차를 막으면 **다음부터 아무도 이 모드를 안 부른다.**"""
    report = _report(repo, "## 제4부 — Fix\n\nSlack 경보 · `mahdi/ops/alerting.py`\n")
    code = collector.main(["--root", str(repo), "--verify-report", str(report)])

    assert code == 0
    assert "보고서 자기검증" in capsys.readouterr().out
