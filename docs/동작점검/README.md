# 운영 점검 — 문서 구조와 규약

## 파일 종류

| 경로 | 누가 만드나 | 무엇인가 |
|---|---|---|
| `YYYY-MM-DD_마흐디_운영점검보고서.md` | **사람** | 그날의 해석 — 이상점 정리, fix 계획, 고도화 방안 |
| `auto/YYYY-MM-DD_지표.md` | `scripts/daily_ops_report.py` | 표와 전일 델타만. **해석 없음** |
| `auto/YYYY-MM-DD_지표.json` | 위와 동일 | 기계 판독용 — 다음날 델타 계산에 쓴다 |
| `hypotheses.yaml` | **사람(fix 구현 시점)** | 예측치 — 다음 거래일 리포트가 자동 대조 |

자동 산출은 `auto/`에만 두고 **사람 보고서가 그것을 인용한다.** 도구는 판정하지 않는다.

## 규약 — 예측치를 먼저 적는다

> **fix를 구현하는 세션은 그 자리에서 `hypotheses.yaml`에 예측치를 적는다.**
> 다음 거래일 리포트가 자동으로 대조해 §0에 낸다.
> **예측치를 못 적겠으면 그 fix는 아직 근거가 부족한 것이다.**

마지막 문장이 이 규약의 진짜 가치다. 2026-07-30 보고서는 위상 격자 스냅의 트레이드오프를
*"밀린 사이클의 데이터는 어느 쪽이든 이미 유실된 뒤다"* 라고 **추론으로 단정**했고, 07-31 실측에서
그게 틀렸음이 드러났다(밀림 83→46건인데 결손은 25→47분). 그 추론이 예측치 형태로 강제됐다면
그날 바로 걸렀을 것이다.

`hypotheses.yaml`의 `상태`는 **자동으로 바뀌지 않는다** — 사람이 보고서를 쓰면서 손으로 확정한다.
자동 판정이 틀렸을 때 조용히 덮이는 것을 막기 위함이다.

## 자동 집계 실행

장마감 자동 종료(`scripts/stop_mahdi_marketclose.bat`)가 taskkill 이후 한 줄로 호출한다 —
그 시점에 관측 루프는 이미 종료돼 로그가 완결돼 있고, DB/Redis는 의도적으로 계속 실행 중이다.

수동 실행(과거 날짜 재집계, 로그가 남아 있는 한):

```
uv run python scripts/daily_ops_report.py [--date YYYY-MM-DD] [--no-db] [--out-dir DIR]
```

`--no-db`는 Docker가 꺼진 상태에서 로그 집계만 뽑을 때 쓴다.

## 지표를 늘릴 때

로직은 `mahdi/ops/`에 두고 `scripts/`는 얇게 유지한다(`scripts/log_marketclose_stop.py`가
같은 규약을 문서로 남기고 있다 — 실제 로직은 pytest로 테스트되는 파이썬 쪽에).

- `log_metrics.py` — 순수 파서(로그 라인 → dict). 파일 I/O 없음.
- `db_metrics.py` — SQL 집계. **COCKPIT 배지와 계산 함수를 공유한다**(리포트와 배지가 다른 답을
  내면 어느 쪽을 믿을지 알 수 없다).
- `report.py` — 순수 렌더러(dict → 마크다운).
- `hypotheses.py` — 가설 로드 + 대조.

새 지표는 `tests/fixtures/observation_loop_sample.log`에 대표 줄을 추가하고
`tests/test_ops_log_metrics.py`에서 손계산 값과 대조한다.
