"""KIS 종목코드 마스터파일(지수선물옵션) 다운로드·파싱 — 옵션 체인 구성·최근월물 조회의 기반.

모의투자 REST에는 전체 옵션 체인을 한 번에 조회하는 API가 없다(display-board 계열은
실전 전용). 대신 매일 갱신되는 이 마스터파일에 상장된 모든 행사가·만기의 단축코드가
정적으로 들어있으므로, 이를 내려받아 체인(행사가 목록)을 구성한 뒤 종목별로
KISRestClient.get_quote()를 반복 호출해 실시간 값을 채운다.

출처(다운로드 URL·컬럼 스키마 모두 실제 스크립트로 확인):
https://github.com/koreainvestment/open-trading-api/blob/main/stocks_info/domestic_index_future_code.py
상품종류 코드값은 2026-07-03자 실제 마스터파일을 내려받아 KOSPI200 행을 직접 확인해 확정했다.
"""

from __future__ import annotations

import re
from pathlib import Path
from zipfile import ZipFile

import httpx
import pandas as pd

_EXPIRY_FROM_NAME = re.compile(r"^[CP]\s*(\d{6})")
# 위클리는 한글종목명이 "위클리M C 2607W1 1,300.0"(상품종류 N/O, 위클리(월))이나
# "위클리C 2607W3 1,170.0"(상품종류 L/M, 위클리(목)) 형식 — C/P 앞에 "위클리" 또는 "위클리M"
# 접두어가 붙고 만기는 6자리 YYYYMM이 아니라 4자리 YYMM+주차(W1/W2/...)로 표기된다(2026-07-06
# N/O 실측, 2026-07-10 L/M 추가 확인). 이 정규식은 접두어의 "M" 유무와 무관하게 마지막
# C/P + 공백 + YYMM+주차만 잡아내므로 두 상품(N/O·L/M) 모두에 그대로 쓴다. 정확한 달력상
# 만기일(월/목 등 요일)은 이 이름만으로 확정할 수 없어 파싱하지 않는다 — main.py가 실제
# get_quote() 응답의 futs_last_tr_date로 확정한다. 여기서는 "가장 가까운 위클리부터 정렬"에만
# 쓸 수 있으면 충분하므로 YYMM+주차 문자열을 그대로 정렬키로 쓴다(같은 자릿수라 사전식 비교가
# 시간순과 일치 — W10 이상은 KOSPI200에 없어 안전).
_WEEKLY_EXPIRY_FROM_NAME = re.compile(r"[CP]\s+(\d{4}W\d)")

MASTER_FILE_URL = "https://new.real.download.dws.co.kr/common/master/fo_idx_code_mts.mst.zip"
MASTER_FILE_NAME = "fo_idx_code_mts.mst"

# 주의: KIS 공식 참고 스크립트(domestic_index_future_code.py)가 명시한 컬럼 순서는
# [..., 'ATM구분', '행사가', '월물구분코드', ...]이지만, 2026-07-03자 실제 마스터파일을
# 내려받아 raw 필드를 직접 확인한 결과 실제 순서는 [..., '월물구분코드', '행사가', 'ATM구분', ...]
# 였다(동일 만기 여러 행사가에서 5번째 필드가 '2'로 고정, 6번째 필드가 행사가별로 변함,
# 7번째 필드는 대부분 공백). 참고 스크립트가 구버전 포맷 기준이거나 오기로 보인다 — 이 파일은
# 실측값을 신뢰한다.
_COLUMNS = [
    "상품종류",
    "단축코드",
    "표준코드",
    "한글종목명",
    "월물구분코드",
    "행사가",
    "ATM구분",
    "기초자산단축코드",
    "기초자산명",
]

# 상품종류 코드 (기초자산명="KOSPI200" 행 기준 실측)
# 정정(2026-07-10): 2026-07-06 당시엔 "L/M이 단 한 행도 없다"고 결론 냈으나, 그건 그 주(週)
# 우연히 N/O 풀만 살아있었을 뿐이었다. 2026-07-10 재실측(먼슬리 만기 다음날, 두 위클리 만기가
# 동시에 상장된 시점) 결과 L/M 행이 실제로 존재하며(위클리C/P, 예: "위클리C 2607W3"), N/O
# 행(위클리M C/P, 예: "위클리M C 2607W2")과 동시에 살아있었다. 처음엔 "같은 상품의 코드풀이
# 주차마다 N/O↔L/M로 교대"라고 추측했으나, COCKPIT 만기유동성비교 패널이 그날 표시한 위클리
# 만기(2026-07-13=월요일)가 N/O 쪽(그때 기준 더 가까운 2607W2)에서 나온 것으로 확인돼, eFriend가
# 이미 별도 기초자산으로 분리해 보여주던 "KOSPI200 위클리(월)"/"위클리(목)"(2026-07-06 발견,
# 상장 전이라 그땐 확인 못 함)와 정확히 일치한다 — 즉 N/O="위클리(월)", L/M="위클리(목)"인
# **서로 다른 두 상품**이지 교대 배정되는 같은 상품의 코드풀이 아니다("위클리M"의 "M"이 그때는
# 의미불명이었으나 지금 보면 "월"의 약자로 보인다). 요일 대응은 이 실측 하나로 확정한 것이라
# 만기 개편 등으로 어긋날 수 있음 — 의심되면 `get_quote()`의 `futs_last_tr_date` 요일로 재확인.
PRODUCT_TYPE_FUTURES = "1"  # 지수선물 (정규)
PRODUCT_TYPE_OPTION_CALL = "5"  # 지수옵션 콜 (정규, 월물)
PRODUCT_TYPE_OPTION_PUT = "6"  # 지수옵션 풋 (정규, 월물)
PRODUCT_TYPE_MINI_OPTION_CALL = "D"  # 미니옵션 콜
PRODUCT_TYPE_MINI_OPTION_PUT = "E"  # 미니옵션 풋
PRODUCT_TYPE_WEEKLY_MON_OPTION_CALL = "N"  # 위클리(월) 콜 — 2026-07-06 최초 실측, 2026-07-10 요일 확정
PRODUCT_TYPE_WEEKLY_MON_OPTION_PUT = "O"  # 위클리(월) 풋
PRODUCT_TYPE_WEEKLY_THU_OPTION_CALL = "L"  # 위클리(목) 콜 — 2026-07-10 실측(먼슬리 만기 주 목요일엔 상장 없음)
PRODUCT_TYPE_WEEKLY_THU_OPTION_PUT = "M"  # 위클리(목) 풋

_WEEKLY_SERIES = ("weekly_mon", "weekly_thu")

_SERIES_PRODUCT_TYPES = {
    "regular": ((PRODUCT_TYPE_OPTION_CALL,), (PRODUCT_TYPE_OPTION_PUT,)),
    "mini": ((PRODUCT_TYPE_MINI_OPTION_CALL,), (PRODUCT_TYPE_MINI_OPTION_PUT,)),
    "weekly_mon": ((PRODUCT_TYPE_WEEKLY_MON_OPTION_CALL,), (PRODUCT_TYPE_WEEKLY_MON_OPTION_PUT,)),
    "weekly_thu": ((PRODUCT_TYPE_WEEKLY_THU_OPTION_CALL,), (PRODUCT_TYPE_WEEKLY_THU_OPTION_PUT,)),
}


def download_master_zip(dest_dir: Path, client: httpx.Client | None = None) -> Path:
    """
    입력: 저장할 디렉터리.
    계산: MASTER_FILE_URL에서 zip을 내려받아 dest_dir에 저장.
    실패 조건: 4xx/5xx면 httpx.HTTPStatusError 전파.
    """
    client = client or httpx.Client(timeout=30.0, follow_redirects=True)
    response = client.get(MASTER_FILE_URL)
    response.raise_for_status()
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / "fo_idx_code_mts.mst.zip"
    zip_path.write_bytes(response.content)
    return zip_path


def extract_master_file(zip_path: Path, dest_dir: Path) -> Path:
    """zip을 dest_dir에 풀고 .mst 파일 경로를 반환."""
    with ZipFile(zip_path) as zf:
        zf.extractall(dest_dir)
    return dest_dir / MASTER_FILE_NAME


def parse_master_file(mst_path: Path) -> pd.DataFrame:
    """
    입력: 압축 해제된 .mst 파일 경로.
    계산: pipe(|) 구분·cp949 인코딩으로 읽어 9개 컬럼 DataFrame으로 변환하고, 월물구분코드를
         숫자로 강제 변환(원본이 문자열이라 두 자릿수 랭크가 섞이면 사전식 비교로 틀릴 수 있음).
    실패 조건: 파일이 없으면 FileNotFoundError(pandas 기본 동작).
    """
    df = pd.read_table(mst_path, sep="|", encoding="cp949", header=None)
    df.columns = _COLUMNS
    df["월물구분코드"] = pd.to_numeric(df["월물구분코드"], errors="coerce")
    # 상품종류는 범주형 코드지만 값이 전부 숫자인 파일에서는 pandas가 int로 추론해버려
    # PRODUCT_TYPE_* 문자열 상수와의 비교가 조용히 실패한다 — 항상 문자열로 고정한다.
    df["상품종류"] = df["상품종류"].astype(str)
    return df


def load_index_derivatives_master(cache_dir: Path, client: httpx.Client | None = None) -> "IndexDerivativesMaster":
    """다운로드→압축해제→파싱을 한 번에 수행하는 편의 함수 (main.py 기동 시 1일 1회 호출 용도)."""
    zip_path = download_master_zip(cache_dir, client=client)
    mst_path = extract_master_file(zip_path, cache_dir)
    return IndexDerivativesMaster(parse_master_file(mst_path))


class IndexDerivativesMaster:
    """지수선물옵션 종목코드 마스터 조회 헬퍼. 기본 기초자산은 KOSPI200(정규 계약)."""

    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df

    @classmethod
    def from_file(cls, mst_path: Path) -> "IndexDerivativesMaster":
        return cls(parse_master_file(mst_path))

    def futures(self, underlying: str = "KOSPI200") -> pd.DataFrame:
        df = self._df
        return df[(df["상품종류"] == PRODUCT_TYPE_FUTURES) & (df["기초자산명"] == underlying)].sort_values(
            "월물구분코드"
        )

    def front_month_future_code(self, underlying: str = "KOSPI200") -> str | None:
        """
        계산: 정규 지수선물(상품종류='1') 중 월물구분코드가 가장 작은(최근월) 행의 단축코드.
        실패 조건: 해당 기초자산의 선물이 없으면 None.
        """
        rows = self.futures(underlying)
        if rows.empty:
            return None
        return str(rows.iloc[0]["단축코드"])

    def options(self, option_type: str, underlying: str = "KOSPI200", series: str = "regular") -> pd.DataFrame:
        """
        option_type: "C" 또는 "P". series: "regular"(정규 월물, 기본값) | "mini"(미니, 승수
        50,000원) | "weekly_mon"(위클리 월요일 만기, 상품종류 N/O) | "weekly_thu"(위클리 목요일
        만기, 상품종류 L/M) — 2026-07-06 N/O 추가, 2026-07-10 L/M을 별도 요일(목)로 분리
        (eFriend가 "위클리(월)"/"위클리(목)"을 애초에 별도 상품으로 구분해온 것과 일치, 근거는
        PRODUCT_TYPE_WEEKLY_*_OPTION_* 주석 참고).

        해석: 반환 DataFrame에 "만기YYYYMM" 컬럼을 추가해서 준다 — regular/mini는 월물구분코드가
             실제로는 만기 순번을 뜻하지 않는다(실측 결과 11개 서로 다른 만기월에 걸쳐 단
             3개 값(1/2/3)만 나타남 — 아마 유동성/분류 코드). 대신 한글종목명에 항상 박혀
             있는 "C 202607   545.0" 형식에서 만기(YYYYMM)를 정규식으로 뽑아 신뢰한다.
             weekly_mon/weekly_thu는 이름 형식이 달라(_WEEKLY_EXPIRY_FROM_NAME 주석 참고) 별도
             정규식을 쓴다.
        실패 조건: series가 알 수 없는 값이면 ValueError.
        """
        if option_type not in ("C", "P"):
            raise ValueError("option_type은 'C' 또는 'P'여야 합니다")
        if series not in _SERIES_PRODUCT_TYPES:
            raise ValueError(f"series는 {sorted(_SERIES_PRODUCT_TYPES)} 중 하나여야 합니다")
        call_types, put_types = _SERIES_PRODUCT_TYPES[series]
        product_types = call_types if option_type == "C" else put_types
        df = self._df
        rows = df[(df["상품종류"].isin(product_types)) & (df["기초자산명"] == underlying)].copy()
        expiry_pattern = _WEEKLY_EXPIRY_FROM_NAME if series in _WEEKLY_SERIES else _EXPIRY_FROM_NAME
        rows["만기YYYYMM"] = rows["한글종목명"].str.extract(expiry_pattern)[0]
        return rows.sort_values(["만기YYYYMM", "행사가"])

    def nearest_expiry_chain(self, underlying: str = "KOSPI200", series: str = "regular") -> list[dict]:
        """
        계산: 콜/풋 각각에서 만기(한글종목명에서 추출한 정렬키)가 가장 빠른 값의 전 행사가 목록을 반환.
        해석: 반환된 각 dict({option_type, strike, symbol, month_label})가 옵션 체인 스냅샷의
             기초 재료다 — 실시간 값(IV/Greeks/OI)은 각 symbol로 KISRestClient.get_quote()를
             호출해 채워야 한다(REST에는 체인 전체를 한 번에 주는 호출이 없음).
        실패 조건: 콜/풋 데이터가 없으면 빈 리스트.
        """
        result: list[dict] = []
        for opt_type in ("C", "P"):
            rows = self.options(opt_type, underlying, series=series).dropna(subset=["만기YYYYMM"])
            if rows.empty:
                continue
            nearest_expiry = rows["만기YYYYMM"].min()
            nearest = rows[rows["만기YYYYMM"] == nearest_expiry]
            for _, row in nearest.iterrows():
                result.append(
                    {
                        "option_type": opt_type,
                        "strike": float(row["행사가"]),
                        "symbol": str(row["단축코드"]),
                        "month_label": str(row["한글종목명"]).strip(),
                    }
                )
        return sorted(result, key=lambda r: (r["option_type"], r["strike"]))

    def option_symbol(
        self, option_type: str, strike: float, underlying: str = "KOSPI200", series: str = "regular"
    ) -> str | None:
        """
        계산: 만기가 가장 빠른 값(정렬키 기준) 중 option_type/strike가 일치하는 단축코드를 찾는다.
        해석: 요청한 strike가 실제 상장된 행사가 격자에 없으면(예: 임의 그리드 근사) None —
             호출측(RollingSubscriptionManager)이 해당 strike 구독을 건너뛰어야 한다.
        실패 조건: 일치하는 행이 없으면 None.
        """
        rows = self.options(option_type, underlying, series=series).dropna(subset=["만기YYYYMM"])
        if rows.empty:
            return None
        nearest_expiry = rows["만기YYYYMM"].min()
        match = rows[(rows["만기YYYYMM"] == nearest_expiry) & (rows["행사가"] == strike)]
        if match.empty:
            return None
        return str(match.iloc[0]["단축코드"])
