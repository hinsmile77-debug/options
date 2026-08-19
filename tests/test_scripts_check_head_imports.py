"""커밋된 트리 import 검사기 — 이 검사기 자신이 옳은가.

2026-08-19에 `origin/master`가 깨진 채 푸시됐다. pytest가 그것을 못 본 이유는 테스트가
**작업트리**를 보기 때문이고, 그 간극은 어떤 테스트로도 못 메운다. 그래서 이 파일이 지키는
것은 「HEAD가 살아 있는가」가 아니라 **「검사기가 죽은 HEAD를 죽었다고 말하는가」** 다.
"""

import subprocess
import sys

import pytest

from scripts import check_head_imports as checker


def test_the_probe_is_syntactically_valid_python():
    """생성 코드가 셸·heredoc을 통과하며 깨진 적이 있다 — 그래서 컴파일까지 확인한다."""
    compile(checker.import_probe(("mahdi.main",)), "<probe>", "exec")


def test_the_probe_has_no_literal_newline_escapes():
    """줄바꿈을 문자열 리터럴 안에 넣으면 이스케이프가 풀려 코드가 깨진다(2026-08-19 실측).

    **이 검사식 자체를 문자 코드로 쓴다** — 백슬래시를 소스에 직접 넣으면 이 테스트 파일이
    셸을 통과할 때 같은 사고를 당한다(실제로 당했다).
    """
    backslash_n = chr(92) + "n"
    assert backslash_n not in checker.import_probe()


def test_the_probe_exits_zero_when_every_module_imports():
    result = subprocess.run(
        [sys.executable, "-c", checker.import_probe(("json", "pathlib"))],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_the_probe_names_the_module_that_failed():
    """어느 모듈이 죽었는지 말하지 않으면 사람이 다시 찾아야 한다."""
    result = subprocess.run(
        [sys.executable, "-c", checker.import_probe(("json", "mahdi_no_such_module"))],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 1
    assert "mahdi_no_such_module" in result.stdout
    assert "json" not in result.stdout  # 멀쩡한 모듈은 안 나온다


def test_entry_modules_start_with_the_observation_loop():
    """`mahdi.main`이 죽으면 그날 하루가 통째로 없다 — 그래서 첫 번째다."""
    assert checker.ENTRY_MODULES[0] == "mahdi.main"


def test_cannot_check_is_a_distinct_exit_code_from_failure():
    """「검사를 못 했다」와 「통과했다」를 구분한다(규약 C) — 셋이 서로 달라야 한다."""
    codes = {checker.EXIT_OK, checker.EXIT_IMPORT_FAILED, checker.EXIT_CANNOT_CHECK}
    assert len(codes) == 3


def test_an_unknown_ref_reports_cannot_check_not_pass():
    """존재하지 않는 ref를 통과로 세면 이 검사기는 아무것도 지키지 않는다."""
    code, detail = checker.check_ref("이런-ref는-없다", ("mahdi.main",))
    assert code == checker.EXIT_CANNOT_CHECK
    assert detail


def test_the_current_head_actually_imports():
    """이것이 이 검사기의 존재 이유다 — 다른 PC가 pull한 뒤 겪을 일을 여기서 먼저 겪는다.

    git이 바빠 워크트리를 못 만들면 **skip**한다. 「검사를 못 했다」를 통과로 세면 이 테스트가
    지키는 것이 사라지고, 실패로 세면 다른 세션이 커밋 중일 때마다 흔들린다(규약 C).
    """
    code, detail = checker.check_ref("HEAD")
    if code == checker.EXIT_CANNOT_CHECK:
        pytest.skip("워크트리를 못 만들어 검사하지 못했다: " + detail)
    assert code == checker.EXIT_OK, "HEAD가 클린 체크아웃에서 기동하지 못한다: " + detail
