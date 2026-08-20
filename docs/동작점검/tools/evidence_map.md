# 증거 지도 — 어떤 파일이 무엇의 증거인가 (마흐디)

`{D}` = `YYYY-MM-DD` (KST). 경로는 리포 루트(`C:\Users\82108\PycharmProjects\options`) 기준.

## 로그

| 파일 | 무엇을 말해주는가 | 크기 감각 | 주의 |
|---|---|---|---|
| `logs/observation_loop.log` (+ `.1`~`.9`) | 하루의 거의 전부 — 사이클·REST·WS·판단·레짐 | 10MB×10 로테이션 | **하루치가 두 파일에 걸친다.** 오래된 파일부터 읽어야 시간순이 보장된다 |
| `logs/premarket_startup.log` | 07:30 기동 / 15:45 종료 배치의 에코 | 누적 260KB | `[YYYY-MM-DD H:MM:SS.ss]` 형식. **초기 라인은 cp949로 깨져 있다** — 깨진 글자는 무시하고 시각과 영문만 읽는다 |
| `logs/watchdog.log` | 워치독 자신의 생사 | 누적 수 KB | `[시각] OK/RESTART/… — 사유`. **마지막 기록 이후 무기록**이 공백보다 중요하다 |
| `logs/cockpit.log` | COCKPIT(streamlit/uvicorn) 기동·접속 | 5MB | UI가 죽으면 사람에게 도달하는 유일한 경보가 끊긴다 |
| `logs/observation_loop_crash.log` | 프로세스 사망 트레이스백 | 2KB | **mtime이 오늘이면 오늘 죽은 것이다.** 마지막 프레임이 사인(死因) |

### 로그 형식

`2026-08-12 13:19:11,261 WARNING:mahdi.main:메시지` — 타임스탬프 + 레벨 + 로거 + 메시지.
**태그가 없다.** 사건은 문구로 식별한다. 트레이스백 본문은 타임스탬프가 없어서 레코드 줄과 구분된다
(형식이 아니라 구조가 만드는 구분).

### 상태 파일

| 파일 | 의미 |
|---|---|
| `logs/.last_successful_start.txt` | 마지막 정상 기동 시각 |
| `logs/.last_cockpit_start.txt` | COCKPIT 기동 시각 |
| `logs/.last_marketclose_stop.txt` | 장마감 종료 시각 |
| `logs/.watchdog_state.json` | `{date, restarts, last_alert_at}` — 그날 재기동 횟수 |
| `logs/.watchdog_last_check.json` | `{at, action, detail}` — 워치독의 마지막 판정. `감시 창 밖`이면 정상 대기 |

## 자동 산출물

| 파일 | 누가 만드나 | 내용 |
|---|---|---|
| `docs/동작점검/auto/{D}_지표.md` | `scripts/daily_ops_report.py` (장마감 종료 배치가 호출) | §0 가설 검정 · §1 한눈에 · §2~§17 지표. **해석 없음** |
| `docs/동작점검/auto/{D}_지표.json` | 위와 동일 | 기계 판독 — **다음날 델타의 유일한 원천** |
| `docs/동작점검/auto/{D}_증거_{국면}.md` | `tools/collect_evidence.py` | 그 국면의 뼈대와 사건. **요약이지 원본이 아니다** |
| `docs/동작점검/{D}_마흐디_일일점검.md` | 장전 세션이 **생성**, 장중·장후가 **append** | 그날의 해석 전부 — **하루 한 파일.** 장후에 종합 완성본이 된다 |
| `docs/동작점검/hypotheses.yaml` | **사람(fix 구현 시점)** | 예측치. 다음 거래일 리포트가 자동 대조 |

> **폴더 규칙 — `auto/`는 기계가 만든 재료, 루트는 사람이 읽는 보고서다.**
> 지표·증거 다이제스트는 `auto/`에 쌓이고(하루 여러 개·회전 대상), **사람 보고서는 루트에 하루 하나**다.
>
> **날짜별 규약이 다르다 — 옛 날을 찾을 때는 옛 규약으로 찾는다.**
>
> | 기간 | 사람 보고서 |
> |---|---|
> | ~2026-08-12 | `_점검_pre.md`가 **`auto/` 아래** 있었다 |
> | 2026-08-13 ~ 08-20 | 루트에 국면별 4파일 — `_점검_pre` / `_점검_intra` / `_점검_intra_1430` / `_마흐디_운영점검보고서` |
> | 2026-08-21 ~ | 루트에 **`_마흐디_일일점검.md` 하나** (append 전용) |
>
> 기계 쪽 경계는 `collect_evidence.ONE_FILE_SINCE`(2026-08-21)와 `INTRA_1430_SINCE`(2026-08-17)다.
> `latest_report_before()`는 신·구 이름을 **함께** 찾는다 — 안 그러면 §8-1이 전환일 이후 영구히 빈다.

수동 재집계(로그가 남아 있는 한):

```
uv run python scripts/daily_ops_report.py --date {D} [--no-db]
```

`--no-db` 는 Docker가 꺼진 상태에서 로그 집계만 뽑을 때.

## 기준 문서

| 파일 | 역할 |
|---|---|
| `docs/Dev_md/MAHDI_ULTIMATE_SYSTEM_v6.md` | 현행 마스터 설계도. 공리·알파 원장·엔진별 실패 조건 |
| `docs/Dev_md/MAHDI_ULTIMATE_SYSTEM_v3~v5.md` | 이력 — 설계 근거 추적용 |
| `docs/Dev_md/RESEARCH_EXPIRY_SELECTION_v1.md` | 만기·종목 선발 체계 |
| `docs/동작점검/README.md` | **점검 규약의 헌법.** 예측치 규약과 그것이 생긴 사고 이력(규약 A~H) |
| `docs/동작흐름과상태/` | 진입 흐름과 개발 상태 스냅샷 |
| `docs/dev_memory/KIS_RAW_FIELD_RANGES.md` | 외부 API 필드 실측 범위 |
| `docs/CyBos ref/`, `docs/efriend/` | 브로커 API 원문 |

## dev_memory (`docs/dev_memory/`)

| 파일 | 크기 | 읽는 법 |
|---|---|---|
| `DECISION_LOG.md` | ~220KB | **통째로 읽지 않는다.** 헤딩 목록 + 꼬리 몇 KB. append만, 덮어쓰기 금지 |
| `NEXT_TODO.md` | ~140KB | 미완료 체크박스 `- [ ]` 만 뽑아 본다 |
| `CURRENT_STATE.md` | ~60KB | 현재 상태 요약 |
| `SESSION_LOG.md` | ~155KB | 세션 이력 |

## 코드 — 어디를 고치는가

| 경로 | 무엇 |
|---|---|
| `mahdi/main.py` | 관측 루프 본체. 폴러·예산·사이클·로그 문구의 원본 |
| `mahdi/broker/rest_client.py` · `ws_client.py` | KIS REST/WS. 타임아웃·백오프·재연결 |
| `mahdi/ops/log_metrics.py` | 순수 파서(로그 → dict). 파일 I/O 없음 |
| `mahdi/ops/db_metrics.py` | SQL 집계. **COCKPIT 배지와 계산 함수를 공유한다** |
| `mahdi/ops/report.py` | 순수 렌더러(dict → 마크다운) |
| `mahdi/ops/hypotheses.py` | 가설 로드 + 대조 |
| `mahdi/ops/levers.py` | 레버 상태 수집 (규약 H) |
| `mahdi/ops/watchdog_metrics.py` | 워치독 자신의 로그 |
| `scripts/watchdog_observation_loop.py` | 워치독 본체 |
| `scripts/start_mahdi_premarket.bat` · `stop_mahdi_marketclose.bat` | 기동·종료 |
| `tests/test_ops_*.py` | 지표·가설 규약의 기계적 강제. **새 지표는 여기 대표 줄을 추가한다** |

## 자주 쓰는 원본 조회

```bash
# 하루치를 파일 하나로 (로테이션 걸침 주의 — 오래된 것부터)
grep -h '^2026-08-12' logs/observation_loop.log.1 logs/observation_loop.log > /tmp/day.log

# ERROR 이상만
grep -E '^2026-08-12.*(ERROR|CRITICAL):' logs/observation_loop.log

# 특정 사건 전량
grep '^2026-08-12' logs/observation_loop.log | grep 'WS 연결 끊김'

# 특정 시간대
grep -E '^2026-08-12 10:1[0-9]:' logs/observation_loop.log | grep -v httpx

# 경고 분포 (숫자를 지워 같은 사건을 묶는다)
grep -E '^2026-08-12.*WARNING:' logs/observation_loop.log | sed -E 's/[0-9]+/N/g' | cut -c1-120 | sort | uniq -c | sort -rn | head -20
```

## git

```bash
git log --oneline --since="{D} 00:00" --until="<익일> 00:00"   # 당일 커밋
git status --porcelain                                          # 미커밋
git log --oneline -10
```

커밋 메시지 첫 단어는 PC 식별자 `[MW0601]`.
**커밋 시각과 관측 루프 기동 시각의 선후**가 그 fix의 검증 가능 여부를 정한다.
