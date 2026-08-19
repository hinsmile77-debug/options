"""커밋된 트리가 **스스로 import되는가** — 테스트가 구조적으로 못 보는 자리 (2026-08-19 신설).

## 왜 필요한가

2026-08-19에 `origin/master`가 깨진 채 며칠 갈 뻔했다. `595f75d`가 `main.py`를 커밋하면서
그것이 부르는 두 함수(`rotation_snapshot`·`window_stuck_distance`)를 **작업트리에 남겼다.**
클린 체크아웃에서는 `import mahdi.main`이 ImportError로 죽는다 — 장전 기동이 통째로 실패한다.

**pytest는 이것을 볼 수 없다.** 테스트는 작업트리를 대상으로 돌고, 미커밋 파일이 거기 있으므로
1,734건이 전부 통과했다. 즉 코드 결함이 아니라 **스테이징 결함**이고, 같은 작업트리를 보는 한
어떤 테스트도 이 간극을 만들 수 없다.

그래서 **커밋된 상태를 따로 꺼내** 확인한다 — `git worktree add --detach`로 그 ref를 임시
디렉터리에 펼치고 거기서 진입 모듈을 import한다. 그것이 다른 PC가 pull한 뒤 겪을 일과 같다.

## 언제 돌리나

푸시 전. 그리고 하루 한 번(장마감 훅)이면 늦어도 하루 안에 잡힌다.

    uv run python scripts/check_head_imports.py            # HEAD 검사
    uv run python scripts/check_head_imports.py --ref abc  # 특정 커밋 검사

## 실패 조건

import이 실패하면 **종료코드 1**과 함께 어느 심볼이 없는지 그대로 인쇄한다. 워크트리를
못 만들면(git 없음·잠금 등) **종료코드 2** — 「검사를 못 했다」와 「통과했다」를 구분한다(규약 C).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 클린 체크아웃에서 반드시 import돼야 하는 진입 모듈.
# **관측 루프(`mahdi.main`)가 첫 번째다** — 그것이 죽으면 그날 하루가 통째로 없다.
ENTRY_MODULES = (
    "mahdi.main",
    "mahdi.ops.report",
    "mahdi.ops.campaign",
    "mahdi.dashboard.data_source",
)

EXIT_OK, EXIT_IMPORT_FAILED, EXIT_CANNOT_CHECK = 0, 1, 2

# 하위 프로세스 출력은 **항상 UTF-8로 읽는다.** 윈도우 기본(cp949)으로 읽으면 한글이 섞인
# 예외 메시지에서 `UnicodeDecodeError`가 나고, 그러면 「검사 실패」와 「읽기 실패」가 뭉개진다.
_SUBPROCESS_TEXT = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace"}


def import_probe(modules: tuple[str, ...] = ENTRY_MODULES) -> str:
    """
    계산: 하위 프로세스에서 돌릴 import 검사 코드를 만든다.
    해석: **줄바꿈을 문자열 리터럴 안에 넣지 않는다.** 이 코드는 셸·heredoc을 여러 겹 통과할
         수 있고, 그 과정에서 이스케이프가 풀려 생성 코드가 깨진 적이 있다(2026-08-19 실측).
         줄 목록을 만들어 `chr(10)`으로 잇는 편이 그 사고를 구조적으로 없앤다.
    실패 조건: 없음 — 순수 문자열 생성.
    """
    lines = [
        "import importlib, sys",
        "mods = %r" % (list(modules),),
        "bad = []",
        "for m in mods:",
        "    try:",
        "        importlib.import_module(m)",
        "    except Exception as exc:",
        "        bad.append('%s -> %s: %s' % (m, type(exc).__name__, exc))",
        "for line in bad:",
        "    print(line)",
        "sys.exit(1 if bad else 0)",
    ]
    return chr(10).join(lines)


def check_ref(ref: str = "HEAD", modules: tuple[str, ...] = ENTRY_MODULES) -> tuple[int, str]:
    """
    입력: 검사할 git ref, 진입 모듈 목록.
    계산: `git worktree add --detach`로 그 ref를 임시 디렉터리에 펼치고, **그 디렉터리를
         작업경로로** 파이썬을 띄워 진입 모듈을 import한다. 끝나면 워크트리를 정리한다.
    해석: 현재 작업트리의 미커밋 파일이 섞이지 않는 것이 이 함수의 전부다 — 같은 인터프리터를
         쓰되 **경로만 갈아끼운다**(가상환경 패키지는 그대로 써야 의존성 문제와 섞이지 않는다).
    실패 조건: 워크트리 생성 실패는 `EXIT_CANNOT_CHECK` — 「검사를 못 했다」와 「통과했다」를
              구분한다(규약 C). 정리는 `finally`에서 반드시 시도한다.
    """
    tmp = Path(tempfile.mkdtemp(prefix="mahdi_headcheck_"))
    worktree = tmp / "tree"
    try:
        created = subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree), ref],
            cwd=PROJECT_ROOT, **_SUBPROCESS_TEXT,
        )
        if created.returncode != 0:
            return EXIT_CANNOT_CHECK, f"워크트리 생성 실패: {(created.stderr or '').strip()}"
        probe = subprocess.run(
            [sys.executable, "-c", import_probe(modules)],
            cwd=worktree, **_SUBPROCESS_TEXT,
        )
        if probe.returncode != 0:
            return EXIT_IMPORT_FAILED, (probe.stdout or probe.stderr or "").strip()
        return EXIT_OK, ""
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)],
                       cwd=PROJECT_ROOT, **_SUBPROCESS_TEXT)
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="커밋된 트리가 스스로 import되는지 검사")
    parser.add_argument("--ref", default="HEAD", help="검사할 git ref (기본 HEAD)")
    args = parser.parse_args(argv)

    code, detail = check_ref(args.ref)
    if code == EXIT_OK:
        print(f"OK — {args.ref}에서 진입 모듈 {len(ENTRY_MODULES)}개가 전부 import된다.")
    elif code == EXIT_IMPORT_FAILED:
        print(f"실패 — {args.ref}는 클린 체크아웃에서 기동하지 못한다:", file=sys.stderr)
        print(detail, file=sys.stderr)
        print("커밋되지 않은 파일에 의존하고 있지 않은지 확인할 것(`git status`).", file=sys.stderr)
    else:
        print(f"검사 불가 — {detail}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
