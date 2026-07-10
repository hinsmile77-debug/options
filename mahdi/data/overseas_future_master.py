"""KIS 해외선물옵션 종목코드 마스터파일(ffcode.mst) 다운로드·파싱 — CME/CBOE/HKEx 등 상장
선물의 근월물/차근월물 코드를 알아내는 데 쓴다(Cross-asset stress 피처, v6 §7.3).

symbol_master.py(국내 지수선물옵션)와 같은 패턴: 매일 갱신되는 KIS 정적 마스터파일을 내려받아
계약월별 단축코드를 구성한다. 다만 이 파일은 pipe(|) 구분이 아니라 고정폭(fixed-width) 포맷이라
파싱 방식이 다르다(출처: stocks_info/overseas_future_code.py 컬럼 슬라이스, 2026-07-10 실제
마스터파일로 재검증 — VXN26=최근월물여부 1과 CNHN26=최근월물여부 1이 실제 현재가 조회에서도
정상 응답해 슬라이스 오프셋이 맞음을 확인했다).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile

import httpx

MASTER_FILE_URL = "https://new.real.download.dws.co.kr/common/master/ffcode.mst.zip"
MASTER_FILE_NAME = "ffcode.mst"

# 계약월 라벨은 종목한글명 끝에 "-YYYYMM" 형태로 항상 붙는다(예: "VIX Index-202607",
# "USDCNH(HKEX)-202608", "10year U.S T-Notes-202609") — 계약코드의 월물 알파벳(F/G/H/...)을
# 별도 테이블로 해석할 필요 없이 이 라벨만으로 만기 오름차순 정렬이 가능하다.
_EXPIRY_FROM_LABEL = re.compile(r"-(\d{6})$")


@dataclass(frozen=True, slots=True)
class OverseasFutureContract:
    symbol: str  # 종목코드(예: VXN26)
    label: str  # 종목한글명(예: "VIX Index-202607")
    exchange_code: str  # 거래소코드(CBOE/CME/HKEx 등)
    product_code: str  # 품목코드(VX/CNH/ZN 등)
    expiry_yyyymm: str  # 라벨에서 뽑은 계약월(YYYYMM), 정렬키
    is_front_month: bool  # 최근월물여부


def download_master_zip(dest_dir: Path, client: httpx.Client | None = None) -> Path:
    """입력: 저장할 디렉터리. 계산: MASTER_FILE_URL에서 zip을 내려받아 dest_dir에 저장."""
    client = client or httpx.Client(timeout=30.0, follow_redirects=True)
    response = client.get(MASTER_FILE_URL)
    response.raise_for_status()
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / "ffcode.mst.zip"
    zip_path.write_bytes(response.content)
    return zip_path


def extract_master_file(zip_path: Path, dest_dir: Path) -> Path:
    """zip을 dest_dir에 풀고 .mst 파일 경로를 반환."""
    with ZipFile(zip_path) as zf:
        zf.extractall(dest_dir)
    return dest_dir / MASTER_FILE_NAME


def parse_master_file(mst_path: Path) -> list[OverseasFutureContract]:
    """
    입력: 압축 해제된 .mst 파일 경로.
    계산: 고정폭 슬라이스로 종목코드/한글명/거래소코드/품목코드/최근월물여부를 뽑는다(오프셋
         출처: stocks_info/overseas_future_code.py, 2026-07-10 실제 파일로 재검증).
         스프레드 콤보 행(종목코드에 "-" 포함, 예: "VXMN26-Q26")은 근월물 계산에 방해가 되므로
         제외한다 — 단일 계약월 행만 남긴다.
    실패 조건: 라벨에 계약월(YYYYMM)이 없는 행(스프레드 콤보 등)은 이미 "-" 필터로 걸러지지만,
              혹시 남아 있으면 expiry_yyyymm 정규식이 매치되지 않아 건너뛴다.
    """
    contracts: list[OverseasFutureContract] = []
    with open(mst_path, mode="r", encoding="cp949", errors="replace") as f:
        for row in f:
            symbol = row[:32].rstrip()
            if "-" in symbol:  # 스프레드 콤보 종목코드(예: VXMN26-Q26) 제외
                continue
            label = row[82:107].rstrip()
            exchange_code = row[-92:-82].rstrip()
            product_code = row[-82:-72].rstrip()
            is_front_month = row[-6:-5].rstrip() == "1"

            match = _EXPIRY_FROM_LABEL.search(label)
            if match is None:
                continue

            contracts.append(
                OverseasFutureContract(
                    symbol=symbol,
                    label=label,
                    exchange_code=exchange_code,
                    product_code=product_code,
                    expiry_yyyymm=match.group(1),
                    is_front_month=is_front_month,
                )
            )
    return contracts


def load_overseas_future_master(cache_dir: Path, client: httpx.Client | None = None) -> "OverseasFutureMaster":
    """다운로드→압축해제→파싱을 한 번에 수행하는 편의 함수 (main.py 기동/일 1회 갱신 용도)."""
    zip_path = download_master_zip(cache_dir, client=client)
    mst_path = extract_master_file(zip_path, cache_dir)
    return OverseasFutureMaster(parse_master_file(mst_path))


class OverseasFutureMaster:
    """해외선물 종목코드 마스터 조회 헬퍼 — 품목코드별 근월물/차근월물 단축코드를 찾는다."""

    def __init__(self, contracts: list[OverseasFutureContract]) -> None:
        self._contracts = contracts

    @classmethod
    def from_file(cls, mst_path: Path) -> "OverseasFutureMaster":
        return cls(parse_master_file(mst_path))

    def contracts_by_expiry(self, product_code: str) -> list[OverseasFutureContract]:
        """품목코드(VX/CNH/ZN 등)로 필터링해 계약월 오름차순으로 정렬한 목록."""
        matched = [c for c in self._contracts if c.product_code == product_code]
        return sorted(matched, key=lambda c: c.expiry_yyyymm)

    def front_month_code(self, product_code: str) -> str | None:
        """
        계산: 최근월물여부='1'인 행의 단축코드. 실측상 이 플래그가 붙은 행은 품목코드당 정확히
             하나이므로 그대로 신뢰한다.
        실패 조건: 해당 품목코드에 최근월물 플래그가 붙은 행이 없으면(마스터 갱신 지연 등)
                  contracts_by_expiry의 첫 번째 행으로 폴백. 그마저 없으면 None.
        """
        matched = self.contracts_by_expiry(product_code)
        if not matched:
            return None
        for contract in matched:
            if contract.is_front_month:
                return contract.symbol
        return matched[0].symbol

    def front_two_codes(self, product_code: str) -> tuple[str | None, str | None]:
        """
        계산: 계약월 오름차순 정렬 목록에서 앞의 두 건(근월물, 차근월물) 단축코드를 반환한다 —
             VIX 기간구조(콘탱고/백워데이션)는 이 두 값의 스프레드로 계산한다.
        실패 조건: 계약이 1건 이하면 두 번째 값은 None.
        """
        matched = self.contracts_by_expiry(product_code)
        front = matched[0].symbol if matched else None
        next_ = matched[1].symbol if len(matched) > 1 else None
        return front, next_
