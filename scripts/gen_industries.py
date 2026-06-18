# -*- coding: utf-8 -*-
"""산업 밸류체인 CSV 생성기 (건설/금융/전자부품/반도체).

실재 상장사를 밸류체인(Upstream~Downstream) + 세부분류로 큐레이션한다. 종목코드·시장은
FDR StockListing(KRX)로 검증하고, 매출액/영업이익은 FnGuide(키 불필요)로 실값을 채운다.
NICE BizLINE 엑셀은 상장사가 거의 없어 사용하지 않는다(데이터 날조 금지 — 실값만).

사용:
  python scripts/gen_industries.py verify     # 티커-기업명 검증만(파일 미작성)
  python scripts/gen_industries.py generate    # 검증 통과분으로 CSV 생성
"""
from __future__ import annotations

import csv
import os
import sys

# 컬럼: (구분, 단계, 세부분류, 기업명, 티커, 주요제품)  — 시장/매출/영익은 스크립트가 채움
# 구분 = Upstream / Midstream / Downstream
IND: dict[str, list[tuple]] = {
    # ───────────────────────────── 반도체 ─────────────────────────────
    "반도체": [
        # Upstream — 소재
        ("Upstream", "소재", "공정소재(케미칼)", "동진쎄미켐", "005290", "포토레지스트·전자재료"),
        ("Upstream", "소재", "공정소재(케미칼)", "한솔케미칼", "014680", "과산화수소·반도체 프리커서"),
        ("Upstream", "소재", "공정소재(케미칼)", "솔브레인", "357780", "식각액·전자재료"),
        ("Upstream", "소재", "공정소재(케미칼)", "이엔에프테크놀로지", "102710", "공정케미칼(식각액·신너)"),
        ("Upstream", "소재", "공정소재(케미칼)", "디엔에프", "092070", "반도체 증착 프리커서"),
        ("Upstream", "소재", "특수가스", "후성", "093370", "반도체 특수가스·냉매"),
        ("Upstream", "소재", "특수가스", "원익머트리얼즈", "104830", "반도체 특수가스"),
        ("Upstream", "소재", "소모성 파츠", "하나머티리얼즈", "166090", "실리콘 파츠(식각공정)"),
        ("Upstream", "소재", "소모성 파츠", "티씨케이", "064760", "SiC 소모성 부품"),
        ("Upstream", "소재", "소모성 파츠", "원익QnC", "074600", "쿼츠웨어·세라믹"),
        # Upstream — 장비
        ("Upstream", "장비", "전공정 장비", "원익IPS", "240810", "증착(CVD·ALD) 장비"),
        ("Upstream", "장비", "전공정 장비", "주성엔지니어링", "036930", "증착(ALD) 장비"),
        ("Upstream", "장비", "전공정 장비", "테스", "095610", "증착·식각 장비"),
        ("Upstream", "장비", "전공정 장비", "유진테크", "084370", "증착(ALD) 장비"),
        ("Upstream", "장비", "전공정 장비", "피에스케이", "319660", "PR strip·식각 장비"),
        ("Upstream", "장비", "전공정 장비", "HPSP", "403870", "고압 수소 어닐링 장비"),
        ("Upstream", "장비", "CMP·세정", "케이씨텍", "281820", "CMP·세정 장비·슬러리"),
        ("Upstream", "장비", "계측·검사", "파크시스템스", "140860", "원자현미경(AFM) 계측"),
        ("Upstream", "장비", "계측·검사", "이오테크닉스", "039030", "레이저 마커·계측"),
        ("Upstream", "장비", "EUV·기타 장비", "에프에스티", "036810", "EUV 펠리클·칠러"),
        ("Upstream", "장비", "EUV·기타 장비", "유니셈", "036200", "스크러버·칠러"),
        # Upstream — 설계(팹리스/IP)
        ("Upstream", "설계", "팹리스", "LX세미콘", "108320", "디스플레이 구동칩(DDI)"),
        ("Upstream", "설계", "팹리스", "텔레칩스", "054450", "차량용 AP·MCU"),
        ("Upstream", "설계", "팹리스", "어보브반도체", "102120", "MCU 팹리스"),
        ("Upstream", "설계", "팹리스", "제주반도체", "080220", "메모리 반도체 팹리스"),
        ("Upstream", "설계", "디자인하우스·IP", "가온칩스", "399720", "SoC 디자인하우스"),
        ("Upstream", "설계", "디자인하우스·IP", "에이디테크놀로지", "200710", "SoC 디자인하우스"),
        ("Upstream", "설계", "디자인하우스·IP", "오픈엣지테크놀로지", "394280", "반도체 설계자산(IP)"),
        # Midstream — 제조
        ("Midstream", "제조", "메모리·종합(IDM)", "삼성전자", "005930", "메모리·시스템반도체·세트"),
        ("Midstream", "제조", "메모리·종합(IDM)", "SK하이닉스", "000660", "메모리(DRAM·NAND·HBM)"),
        ("Midstream", "제조", "파운드리", "DB하이텍", "000990", "8인치 파운드리"),
        # Downstream — 후공정(OSAT)
        ("Downstream", "후공정", "패키징·테스트(OSAT)", "하나마이크론", "067310", "반도체 패키징·테스트"),
        ("Downstream", "후공정", "패키징·테스트(OSAT)", "SFA반도체", "036540", "패키징·테스트"),
        ("Downstream", "후공정", "패키징·테스트(OSAT)", "네패스", "033640", "범핑·WLP 패키징"),
        ("Downstream", "후공정", "패키징·테스트(OSAT)", "LB세미콘", "061970", "범핑·패키징"),
        ("Downstream", "후공정", "패키징·테스트(OSAT)", "시그네틱스", "033170", "패키징"),
        # Downstream — 테스트 부품
        ("Downstream", "후공정", "테스트 소켓·프로브", "리노공업", "058470", "테스트 소켓·리노핀"),
        ("Downstream", "후공정", "테스트 소켓·프로브", "ISC", "095340", "반도체 테스트 소켓"),
        ("Downstream", "후공정", "테스트 소켓·프로브", "티에스이", "131290", "프로브카드·소켓"),
        # Downstream — 패키지 기판
        ("Downstream", "기판", "패키지 기판", "대덕전자", "353200", "패키지기판(FC-BGA)"),
        ("Downstream", "기판", "패키지 기판", "심텍", "222800", "패키지기판(FC-CSP)"),
        ("Downstream", "기판", "패키지 기판", "이수페타시스", "007660", "고다층기판(MLB·AI 가속기)"),
        ("Downstream", "기판", "패키지 기판", "해성디에스", "195870", "리드프레임·패키지서브스트레이트"),
    ],
    # ───────────────────────────── 전자부품 ─────────────────────────────
    "전자부품": [
        # Upstream — PCB/기판
        ("Upstream", "기판(PCB)", "경성·HDI 기판", "코리아써키트", "007810", "PCB(HDI·패키지기판)"),
        ("Upstream", "기판(PCB)", "연성기판(FPCB)", "비에이치", "090460", "FPCB(OLED용)"),
        ("Upstream", "기판(PCB)", "연성기판(FPCB)", "인터플렉스", "051370", "FPCB"),
        ("Upstream", "기판(PCB)", "연성기판(FPCB)", "뉴프렉스", "085670", "FPCB"),
        # Midstream — 수동부품
        ("Midstream", "수동부품", "MLCC·커패시터", "삼성전기", "009150", "MLCC·카메라모듈·패키지기판"),
        ("Midstream", "수동부품", "MLCC·커패시터", "삼화콘덴서", "001820", "MLCC·필름콘덴서"),
        ("Midstream", "수동부품", "인덕터·칩부품", "아비코전자", "036010", "인덕터·칩저항"),
        ("Midstream", "수동부품", "안테나·RF부품", "아모텍", "052710", "칩바리스터·안테나"),
        ("Midstream", "수동부품", "안테나·RF부품", "와이솔", "122990", "RF SAW 필터"),
        # Midstream — 커넥터
        ("Midstream", "접속부품", "커넥터·터미널", "한국단자", "025540", "커넥터·터미널"),
        ("Midstream", "접속부품", "커넥터·터미널", "우주일렉트로", "065680", "커넥터"),
        # Downstream — 카메라모듈
        ("Downstream", "카메라·광학", "카메라모듈", "LG이노텍", "011070", "카메라모듈·광학·기판소재"),
        ("Downstream", "카메라·광학", "카메라모듈", "파트론", "091700", "카메라모듈·센서"),
        ("Downstream", "카메라·광학", "카메라모듈", "엠씨넥스", "097520", "카메라모듈"),
        ("Downstream", "카메라·광학", "카메라모듈", "캠시스", "050110", "카메라모듈"),
        ("Downstream", "카메라·광학", "카메라모듈", "나무가", "190510", "카메라모듈·3D센싱"),
        ("Downstream", "카메라·광학", "카메라모듈", "파워로직스", "047310", "카메라모듈·배터리보호회로"),
        ("Downstream", "카메라·광학", "광학부품", "옵트론텍", "082210", "광학필터·렌즈"),
        ("Downstream", "카메라·광학", "광학부품", "세코닉스", "053450", "렌즈·광학모듈"),
        # Downstream — 기타 세트부품
        ("Downstream", "세트부품", "음향·기타 부품", "이엠텍", "091120", "마이크로스피커·전자무화"),
        ("Downstream", "세트부품", "첨단소재 부품", "아모그린텍", "125210", "방열·자성 첨단소재"),
    ],
    # ───────────────────────────── 건설 ─────────────────────────────
    "건설": [
        # Upstream — 자재
        ("Upstream", "건설자재", "시멘트", "한일시멘트", "300720", "시멘트·레미콘"),
        ("Upstream", "건설자재", "시멘트", "성신양회", "004980", "시멘트"),
        ("Upstream", "건설자재", "시멘트", "아세아시멘트", "183190", "시멘트"),
        ("Upstream", "건설자재", "시멘트", "삼표시멘트", "038500", "시멘트"),
        ("Upstream", "건설자재", "건축자재", "KCC", "002380", "도료·건축자재·실리콘"),
        ("Upstream", "건설자재", "건축자재", "LX하우시스", "108670", "창호·바닥재 등 건축자재"),
        ("Upstream", "건설자재", "건축자재", "KCC글라스", "344820", "판유리·인테리어 자재"),
        ("Upstream", "건설자재", "건축자재", "벽산", "007210", "단열재·건축자재"),
        ("Upstream", "건설자재", "건축자재", "노루페인트", "090350", "건축·공업용 도료"),
        ("Upstream", "건설자재", "건축자재", "동화기업", "025900", "보드·건자재"),
        # Midstream — 종합건설(시공)
        ("Midstream", "종합건설", "대형 건설사", "현대건설", "000720", "토목·건축·플랜트"),
        ("Midstream", "종합건설", "대형 건설사", "삼성E&A", "028050", "플랜트 EPC"),
        ("Midstream", "종합건설", "대형 건설사", "DL이앤씨", "375500", "주택·플랜트·토목"),
        ("Midstream", "종합건설", "대형 건설사", "GS건설", "006360", "주택·플랜트·인프라"),
        ("Midstream", "종합건설", "대형 건설사", "대우건설", "047040", "주택·토목·플랜트"),
        ("Midstream", "종합건설", "대형 건설사", "HDC현대산업개발", "294870", "주택·도시개발"),
        ("Midstream", "종합건설", "대형 건설사", "삼성물산", "028260", "건설·상사·패션(건설부문)"),
        ("Midstream", "종합건설", "중견 건설사", "금호건설", "002990", "건축·토목"),
        ("Midstream", "종합건설", "중견 건설사", "코오롱글로벌", "003070", "건설·상사"),
        ("Midstream", "종합건설", "중견 건설사", "계룡건설", "013580", "건축·토목"),
        ("Midstream", "종합건설", "중견 건설사", "한신공영", "004960", "주택·토목"),
        ("Midstream", "종합건설", "중견 건설사", "동부건설", "005960", "건축·토목"),
        ("Midstream", "종합건설", "중견 건설사", "아이에스동서", "010780", "건설·요업·환경"),
        # Downstream — 인테리어·디벨로퍼·설계
        ("Downstream", "후방·서비스", "인테리어·가구", "한샘", "009240", "인테리어·가구"),
        ("Downstream", "후방·서비스", "인테리어·가구", "현대리바트", "079430", "가구·인테리어"),
        ("Downstream", "후방·서비스", "개발·설계·운영", "자이에스앤디", "317400", "부동산개발·시설운영"),
        ("Downstream", "후방·서비스", "개발·설계·운영", "희림", "037440", "건축설계·감리"),
    ],
    # ───────────────────────────── 금융 ─────────────────────────────
    "금융": [
        # Upstream — 은행·지주
        ("Upstream", "은행·지주", "대형 금융지주", "KB금융", "105560", "은행 금융지주"),
        ("Upstream", "은행·지주", "대형 금융지주", "신한지주", "055550", "은행 금융지주"),
        ("Upstream", "은행·지주", "대형 금융지주", "하나금융지주", "086790", "은행 금융지주"),
        ("Upstream", "은행·지주", "대형 금융지주", "우리금융지주", "316140", "은행 금융지주"),
        ("Upstream", "은행·지주", "특수·인터넷은행", "기업은행", "024110", "국책은행"),
        ("Upstream", "은행·지주", "특수·인터넷은행", "카카오뱅크", "323410", "인터넷전문은행"),
        ("Upstream", "은행·지주", "지방 금융지주", "BNK금융지주", "138930", "지방은행 금융지주"),
        ("Upstream", "은행·지주", "지방 금융지주", "iM금융지주", "139130", "지방은행 금융지주(구 DGB)"),
        ("Upstream", "은행·지주", "지방 금융지주", "JB금융지주", "175330", "지방은행 금융지주"),
        ("Upstream", "은행·지주", "지방 금융지주", "제주은행", "006220", "지방은행"),
        # Midstream — 증권
        ("Midstream", "증권", "증권사", "미래에셋증권", "006800", "증권"),
        ("Midstream", "증권", "증권사", "한국금융지주", "071050", "증권 지주(한국투자)"),
        ("Midstream", "증권", "증권사", "삼성증권", "016360", "증권"),
        ("Midstream", "증권", "증권사", "NH투자증권", "005940", "증권"),
        ("Midstream", "증권", "증권사", "키움증권", "039490", "증권(리테일)"),
        ("Midstream", "증권", "증권사", "대신증권", "003540", "증권"),
        ("Midstream", "증권", "복합 금융지주", "메리츠금융지주", "138040", "증권·보험 금융지주"),
        # Midstream — 보험
        ("Midstream", "보험", "생명보험", "삼성생명", "032830", "생명보험"),
        ("Midstream", "보험", "생명보험", "한화생명", "088350", "생명보험"),
        ("Midstream", "보험", "생명보험", "동양생명", "082640", "생명보험"),
        ("Midstream", "보험", "손해보험", "삼성화재", "000810", "손해보험"),
        ("Midstream", "보험", "손해보험", "DB손해보험", "005830", "손해보험"),
        ("Midstream", "보험", "손해보험", "현대해상", "001450", "손해보험"),
        ("Midstream", "보험", "손해보험", "한화손해보험", "000370", "손해보험"),
        ("Midstream", "보험", "재보험", "코리안리", "003690", "재보험"),
        # Downstream — 카드·핀테크·신용평가(기타 금융서비스)
        ("Downstream", "여신·소비자금융", "카드", "삼성카드", "029780", "신용카드"),
        ("Downstream", "여신·소비자금융", "핀테크 결제", "카카오페이", "377300", "핀테크 결제·금융플랫폼"),
        ("Downstream", "금융서비스", "전자지불(PG·VAN)", "KG이니시스", "035600", "전자지불(PG)"),
        ("Downstream", "금융서비스", "전자지불(PG·VAN)", "NHN KCP", "060250", "전자결제(PG)"),
        ("Downstream", "금융서비스", "전자지불(PG·VAN)", "다날", "064260", "휴대폰 결제"),
        ("Downstream", "금융서비스", "전자지불(PG·VAN)", "NICE인프라", "063570", "VAN·ATM 금융인프라(구 한국전자금융)"),
        ("Downstream", "금융서비스", "신용평가·인증", "NICE평가정보", "030190", "신용조회·평가"),
        ("Downstream", "금융서비스", "신용평가·인증", "한국기업평가", "034950", "신용평가"),
        ("Downstream", "금융서비스", "신용평가·인증", "이크레더블", "092130", "기업 신용인증"),
    ],
}

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server", "app", "data")
_HEADER = ["구분", "단계", "세부분류", "기업명", "티커", "시장", "주요제품",
           "시장점유율", "주요수출국", "고객사", "특허수", "매출액", "영업이익", "비고"]


def _krx_maps():
    import FinanceDataReader as fdr
    df = fdr.StockListing("KRX")
    code2 = {}
    for _, r in df.iterrows():
        code2[str(r["Code"]).zfill(6)] = (str(r["Name"]), str(r["Market"]))
    return code2


def verify():
    """반환: 차단 건수(없음·중복). 사명 불일치는 경고만(티커가 맞으면 데이터는 정상)."""
    code2 = _krx_maps()
    block, warn = 0, 0
    for ind, rows in IND.items():
        seen = set()
        for gu, stage, detail, name, tk, prod in rows:
            tk = tk.zfill(6)
            if tk in seen:
                print(f"[중복-차단] {ind}: {name} {tk}")
                block += 1
            seen.add(tk)
            info = code2.get(tk)
            if not info:
                print(f"[없음-차단] {ind}: {name} ({tk}) — KRX 상장목록에 코드 없음")
                block += 1
                continue
            kname, market = info
            kn = kname.replace(" ", "")
            nn = name.replace(" ", "")
            ok = nn in kn or kn in nn or kn[:3] == nn[:3]
            if not ok:
                warn += 1
                print(f"[사명경고] {ind:5s} {tk} CSV={name} / KRX={kname}({market}) — 티커는 정상")
    print(f"\n차단 {block}건 · 사명경고 {warn}건 (경고는 생성 진행)")
    return block


def _fin(tk: str):
    """FnGuide 최신 연간 매출액·영업이익(원). 기존 financials 모듈 재사용."""
    import sys as _s
    _s.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
    from app import financials as F
    try:
        d = F.financials(tk)
        pl = (d.get("annual") or {}).get("PL") or {}
        rows = pl.get("rows") or []
        def last(pred):
            for r in rows:
                if pred(r["account"].replace(" ", "")):
                    vals = [v for v in r["values"] if v is not None]
                    return vals[-1] if vals else None
            return None
        # 금융권은 '매출액'이 없어 순영업수익(은행·증권·카드)·영업수익(보험)으로 폴백
        rev = (last(lambda a: a == "매출액") or last(lambda a: a == "순영업수익")
               or last(lambda a: a == "영업수익"))
        op = last(lambda a: a == "영업이익")
        # FnGuide 단위 억원 → 원
        return (rev * 1e8 if rev is not None else None,
                op * 1e8 if op is not None else None)
    except Exception as e:
        print(f"  매출 조회 실패 {tk}: {e}")
        return (None, None)


def generate():
    if verify() > 0:
        print("검증 실패 — 생성 중단(티커/이름 먼저 수정).")
        return
    from concurrent.futures import ThreadPoolExecutor
    code2 = _krx_maps()
    for ind, rows in IND.items():
        tickers = [r[4].zfill(6) for r in rows]
        with ThreadPoolExecutor(max_workers=8) as ex:
            fins = list(ex.map(_fin, tickers))
        out = os.path.join(_DIR, f"industry_{ind}.csv")
        with open(out, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(_HEADER)
            for (gu, stage, detail, name, tk, prod), (rev, op) in zip(rows, fins):
                tk = tk.zfill(6)
                market = code2.get(tk, ("", ""))[1]
                w.writerow([gu, stage, detail, name, tk, market, prod, "", "", "", "",
                            int(rev) if rev else "", int(op) if op else ""])
        ok = sum(1 for r in fins if r[0])
        print(f"생성: industry_{ind}.csv  ({len(rows)}종목, 매출 {ok}건)")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "verify"
    (generate if mode == "generate" else verify)()
