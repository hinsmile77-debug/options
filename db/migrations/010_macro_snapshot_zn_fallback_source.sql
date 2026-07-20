-- Mahdi Phase 1 추가 (2026-07-20) — CME|CBOT 해외선물옵션 실시간시세가 KIS 유료 항목(HTS
-- [7936] 확인: 월 228.8불)임을 확인. 모의투자 개발 단계에서는 구독하지 않고 zn_front를
-- yfinance(ZN=F) 비공식 폴백으로 채우기로 함(mahdi/data/zn_fallback.py). 어느 소스에서 온
-- 값인지 구분해 COCKPIT/피처 코드가 "실제 CBOT 체결가"와 "근사치"를 혼동하지 않게 한다.
-- 값: 'kis' | 'yfinance_fallback' | NULL(둘 다 실패).
ALTER TABLE macro_snapshot_5m ADD COLUMN IF NOT EXISTS zn_front_source VARCHAR(20);
