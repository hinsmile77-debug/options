"""줄바꿈 규약을 **파일이 아니라 테스트가 지킨다** (2026-08-19, 08-17 Fix#4 / 08-18 Fix#9).

## 왜 `.gitattributes`만으로는 부족한가

`.gitattributes`는 **git이 파일을 만질 때만** 작동한다. 커밋 시 인덱스를 LF로 정규화하고
체크아웃 시 워킹트리를 규칙대로 쓴다. 그런데 이 저장소를 실제로 망가뜨린 경로는 그 둘이
아니라 **워킹트리에 직접 append하는 것**이었다:

    cat >> file        heredoc이 LF로 붙인다
    파이썬 write       LF로 붙인다

원본이 CRLF면 그 파일은 그 자리에서 반반이 된다. 08-19에 `start_mahdi_premarket.bat`이
그렇게 됐고(추가한 11줄만 LF), 같은 날 `hypotheses.yaml`·테스트 3종도 mixed였다.
**git은 그것을 커밋 시 조용히 고쳐 주므로 인덱스만 보면 영원히 안 보인다.**

그래서 여기서 **워킹트리를 직접** 본다.

## 무엇을 지키는가

    mixed 금지     한 파일 안에 CRLF 줄과 LF 줄이 섞이면 실패
    .bat은 CRLF    cmd.exe에 넘기는 유일한 파일이라 보수적으로 못 박는다

`.bat`의 LF 줄이 실제로 실행된다는 것은 08-19에 격리 환경에서 확인했다(단순 `echo`는 돈다).
그래도 강제하는 이유는 `goto` 레이블·괄호 블록이 나중에 들어가면 그때 처음 깨지고,
**그 실패가 아침 기동 전체**이기 때문이다. 여기서 아끼는 것은 없다.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _tracked_files() -> list[Path]:
    """git이 추적하는 파일 — 워킹트리에 실재하는 것만."""
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=PROJECT_ROOT, capture_output=True, timeout=60,
    )
    if result.returncode != 0:
        pytest.skip("git ls-files 실패 — 저장소 밖에서 도는 실행이다")
    names = result.stdout.decode("utf-8").split("\0")
    return [PROJECT_ROOT / n for n in names if n and (PROJECT_ROOT / n).is_file()]


def _line_endings(path: Path) -> tuple[int, int]:
    """반환: (CRLF 줄 수, LF 단독 줄 수). 마지막 줄에 개행이 없으면 세지 않는다."""
    raw = path.read_bytes()
    if b"\x00" in raw[:8000]:
        return (0, 0)  # 바이너리 — 판정 대상이 아니다
    lines = raw.split(b"\n")[:-1]
    crlf = sum(1 for line in lines if line.endswith(b"\r"))
    return (crlf, len(lines) - crlf)


def test_no_tracked_file_mixes_line_endings():
    """**한 파일 안에 CRLF와 LF가 섞이면 안 된다.**

    08-19 실측으로 6건이 있었다 — `hypotheses.yaml` · `start_mahdi_premarket.bat` ·
    테스트 3종. 전부 heredoc/스크립트 append가 남긴 것이고, 그중 하나는 **아침 기동
    스크립트**였다. git은 커밋 시 조용히 고쳐 주므로 인덱스만 보면 안 보인다.
    """
    offenders = []
    for path in _tracked_files():
        crlf, lf = _line_endings(path)
        if crlf and lf:
            offenders.append(f"{path.relative_to(PROJECT_ROOT)} (CRLF {crlf} / LF {lf})")
    assert not offenders, (
        "워킹트리에 줄바꿈이 섞인 파일이 있다 — heredoc(`cat >>`)이나 스크립트 append가 "
        f"CRLF 파일에 LF 줄을 붙인 것이다. `git add --renormalize .` 후 재체크아웃할 것: {offenders}"
    )


def test_every_text_file_in_the_working_tree_is_lf():
    """**`.gitattributes`가 정한 것은 워킹트리도 LF라는 것이다**(`* text=auto eol=lf`).

    mixed만 막으면 절반이다 — 파일 **전체**가 CRLF로 되돌아가는 회귀는 그 검사를 통과한다.
    그리고 그 회귀는 실제로 일어난다: 2026-08-19에 정규화를 마친 직후, 문서를 편집한
    파이썬 한 줄이 두 파일을 통째로 CRLF로 되돌렸다.

        pathlib.Path(p).write_text(s)      # Windows에서 \\n -> \\r\\n (os.linesep)

    텍스트 모드의 기본 `newline=None`이 **쓸 때** `os.linesep`으로 변환하기 때문이다.
    고치는 법은 `newline="\\n"`을 넘기거나(`open(p, "w", newline="\\n")`),
    이 저장소처럼 **테스트가 잡게 하는 것**이다. git은 커밋 시 조용히 정규화하므로
    인덱스만 보면 이 회귀는 영원히 안 보인다.

    위반을 고치는 법: `rm <파일> && git checkout -- <파일>`(커밋된 내용으로 재체크아웃).
    """
    offenders = []
    for path in _tracked_files():
        if path.suffix.lower() in (".bat", ".cmd"):
            continue  # 아래 테스트가 따로 지킨다 — 이쪽은 CRLF가 규칙이다
        crlf, lf = _line_endings(path)
        if crlf:
            offenders.append(f"{path.relative_to(PROJECT_ROOT)} (CRLF {crlf}줄)")
    assert not offenders, (
        "워킹트리에 CRLF 텍스트 파일이 있다 — `.gitattributes`는 `eol=lf`를 정했고, "
        "이것을 되돌리는 흔한 원인은 파이썬 `write_text()`(Windows에서 os.linesep 변환)다. "
        f"`rm <파일> && git checkout -- <파일>`로 되돌릴 것: {offenders}"
    )


def test_windows_batch_files_stay_crlf():
    """`.bat`은 **우리가 안 짜는 파서(cmd.exe)** 에 넘기는 유일한 파일이다.

    LF도 단순 명령에서는 돈다(08-19 격리 실험으로 확인). 그래도 강제하는 이유는
    `goto` 레이블·괄호 블록이 들어가면 그때 처음 깨지고, 그 실패가 **아침 기동 전체**라서다.
    """
    offenders = []
    for path in _tracked_files():
        if path.suffix.lower() not in (".bat", ".cmd"):
            continue
        crlf, lf = _line_endings(path)
        if lf:
            offenders.append(f"{path.relative_to(PROJECT_ROOT)} (LF 단독 {lf}줄)")
    assert not offenders, f"배치 파일에 LF 줄이 있다 — cmd.exe에 넘기기 전에 CRLF로 되돌릴 것: {offenders}"


def test_the_repo_carries_its_own_line_ending_rule():
    """**규칙이 저장소 밖에 있으면 PC마다 다르게 동작한다.**

    이 PC의 `core.autocrlf=true`는 시스템 gitconfig에서 온다 — 저장소 것도 사용자 것도
    아니다. `.gitattributes`가 없으면 다른 PC가 그 값을 다르게 들고 있을 때 같은 커밋이
    다른 바이트로 체크아웃된다(「어느 PC서 pull해도 동작해야 한다」는 이 저장소의 규약).
    """
    attributes = PROJECT_ROOT / ".gitattributes"
    assert attributes.exists(), ".gitattributes가 사라졌다 — 줄바꿈 규칙이 다시 PC 설정에 얹힌다"
    text = attributes.read_text(encoding="utf-8")
    assert "* text=auto eol=lf" in text, "기본 규칙(LF)이 없다"
    assert "*.bat text eol=crlf" in text, "배치 예외가 없다 — 아침 기동 스크립트가 LF가 된다"
