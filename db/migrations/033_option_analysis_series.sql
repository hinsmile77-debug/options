-- 마흐디 추가 (2026-08-18) — 만기북 라벨(series). 종목 로테이션 규칙의 입력.
--
-- ## 이 컬럼이 없어서 막혀 있던 것
--
-- SERIES_ROTATION_RULE_v1(30거래일 검증 29/29)의 `target_series_for()`는 **series별 관측
-- 만기**를 입력으로 요구한다. 그런데 수집 루프는 레그마다 series(regular/weekly_mon/
-- weekly_thu)를 알면서(main.py 폴링 루프의 `legs` 튜플) **적재 직전에 버리고 있었다** —
-- 체인 스냅샷에는 만기만 남아 "이 만기가 어느 북인가"를 답할 곳이 없었다.
-- series↔expiry를 가진 유일한 테이블(expiry_liquidity_1m)은 08:31부터 5분 주기라 장전
-- 공백이 있고, "마흐디 자신이 같은 체인에서 관측한 값을 쓴다"는 스펙 원칙에도 어긋난다.
--
-- ## 백필하지 않는 이유
--
-- 선택기가 보는 창은 5분(CHAIN_SNAPSHOT_MAX_AGE_MINUTES)이라 배포 다음 사이클부터
-- 채워지고, 과거 행은 선택기 입력이 아니다. 과거 행을 expiry_liquidity_1m로 유추해 채우면
-- 유추가 사실처럼 굳는다 — 사후 분석에서 필요해지면 그때 별도 스크립트로, 유추임을 밝히고.
--
-- ## 값의 어휘
--
-- db.py `_VALID_EXPIRY_LIQUIDITY_SERIES`와 동일: 'regular' | 'weekly_mon' | 'weekly_thu'.
-- CHECK 제약을 걸지 않는 이유: 새 series 도입(위클리 분리 같은) 때 마이그레이션 없이
-- 코드만으로 확장돼야 하고, 잘못된 라벨은 DB가 아니라 수집 코드 리뷰가 막을 문제다
-- (expiry_liquidity_1m의 'weekly' 화석 사례처럼 — 제약이 있었어도 화석은 화석이었다).
--
-- 유니크 키는 그대로 둔다 — 같은 (expiry, strike, option_type)이 두 series에 동시에
-- 있을 수 없다(만기가 곧 북을 정한다).

ALTER TABLE option_analysis_1m ADD COLUMN IF NOT EXISTS series VARCHAR(16);

COMMENT ON COLUMN option_analysis_1m.series IS
    '만기북 라벨(regular/weekly_mon/weekly_thu) — 수집 루프가 레그를 만들 때 알던 값. '
    'NULL이면 마이그레이션 033(2026-08-18) 이전에 적재된 행. 종목 로테이션 규칙'
    '(SERIES_ROTATION_RULE_v1) target_series_for()의 입력이다.';
