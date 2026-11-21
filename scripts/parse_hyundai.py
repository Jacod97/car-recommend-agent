"""crawl_hyundai.py가 받은 상세 페이지 HTML에서 매물 정보를 뽑아 JSONL로 저장한다.

입력: data/raw/hyundai/<날짜>/detail/*.html.gz
출력: data/processed/hyundai/<날짜>.jsonl  (한 줄에 매물 하나)

최상위 키는 이 스크립트가 정한 이름이고, 그 아래 dict의 키와 값은 페이지에 적힌 글자 그대로다.
단위 변환(예: "4,590만원" → 45900000)은 여러 플랫폼을 하나의 스키마로 합치는 단계에서 한다.
판매 직원 이름·사원증 번호는 개인정보라 뽑지 않는다.

실행: uv run scripts/parse_hyundai.py [--date YYYY-MM-DD]
"""

import argparse
import gzip
import json
import re
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup, Tag

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw" / "hyundai"
OUT_DIR = ROOT / "data" / "processed" / "hyundai"

DETAIL_URL = "https://certified.hyundai.com/p/goods/goodsDetail.do?goodsNo={}"

# 페이지 스크립트의 광고 추적용 객체(ga4Obj)에만 모델·트림이 나뉘어 들어 있다.
GA4_BLOCK = re.compile(r"const ga4Obj = \{(.*?)items\s*:", re.S)
GA4_FIELD = re.compile(r'(\w+)\s*:\s*"((?:[^"\\]|\\.)*)"')
GA4_KEYS = ["brand_type", "vehicle_modelgroup", "vehicle_model", "vehicle_grade", "vehicle_trim"]


def text(el: Tag | None, sep: str = " ") -> str | None:
    """요소 안의 글자를 공백 하나로 이어 붙인다. 가격처럼 <em>4,590</em><i>만원</i>으로
    쪼개진 값은 sep=""로 붙여야 "4,590만원"이 된다."""
    if el is None:
        return None
    return " ".join(el.get_text(sep, strip=True).split())


def parse_model_info(html: str) -> dict:
    m = GA4_BLOCK.search(html)
    if not m:
        return {}
    # 값이 JS 문자열이라 사, \/ 같은 이스케이프가 섞여 있다. JSON 문자열 규칙과 같아서 json.loads로 푼다.
    fields = {k: json.loads(f'"{v}"') for k, v in GA4_FIELD.findall(m.group(1))}
    return {k: fields[k] for k in GA4_KEYS if k in fields}


def parse_accidents(layer: Tag) -> list[dict]:
    """보험이력 보고서의 사고 목록. 사고 하나에 '내차 피해'와 '타차 가해' 칸이 하나씩 있다."""
    accidents = []
    for li in layer.select("ol.list_history > li"):
        date_text = text(li.select_one("p.date")) or ""  # "사고일 : 2024.09.10"
        records = []
        for box in li.select("div.box"):
            spans = box.select("p.txt > span")
            records.append({
                "type": text(spans[0]) if spans else None,  # 내차 피해 / 타차 가해
                "handling": text(spans[1], sep="") if len(spans) > 1 else None,  # 상대보험으로 처리 / 없음
                "costs": {
                    text(item.select_one("span:nth-of-type(1)")): text(item.select_one("span:nth-of-type(2)"), sep="")
                    for item in box.select("div.cont li")
                },
                # "수리내역 - 리어범퍼 표면보수(도장),..." 에서 앞의 제목을 뗀다. 내역이 없으면 "수리내역 -"만 있다.
                "repair_detail": (text(box.select_one("p.info")) or "").removeprefix("수리내역 -").strip() or None,
            })
        accidents.append({"date": date_text.split(":", 1)[-1].strip(), "records": records})
    return accidents


def parse_detail(html: str, goods_no: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    name = text(soup.select_one("h1.name"))
    if name is None:  # 오류 페이지
        return None

    price_box = soup.select_one(".pdp02_price .pricetxt")
    status_input = soup.select_one("input#sdStatCd")
    accident_layer = soup.select_one("#accidentLayer")
    center = next(
        (text(li.select_one(".text")) for li in soup.select(".seller_info li")
         if (text(li.select_one(".tit")) or "").startswith("소속")),
        None,
    )

    return {
        "source": "hyundai",
        "goods_no": goods_no,
        "url": DETAIL_URL.format(goods_no),
        "name": name,
        # "20"이면 구매 가능. 검색 화면의 '구매 가능한 차량' 필터가 이 값으로 거른다.
        # 다른 값(계약 중, 일시 품절 등으로 추정)의 정확한 뜻은 아직 확인하지 못했다.
        "sale_status_code": status_input.get("value") if status_input else None,
        "model_info": parse_model_info(html),
        "flags": [text(s) for s in soup.select(".pdp02_price .flag span")],
        "price": text(price_box.select_one(".txt.pay"), sep="") if price_box else None,
        "price_before_discount": text(price_box.select_one(".txt.del"), sep="") if price_box else None,
        "new_car_price": text(soup.select_one(".car_price .txt_con"), sep=""),
        # 기본 정보 · 압류/저당 · 보험 이력 요약이 모두 같은 "제목: 값" 목록 형태다.
        "basic_info": {
            text(li.select_one(".tit")): text(li.select_one(".txt"))
            for li in soup.select(".pdp03_tabs.first ol.base_01 > li")
        },
        # 가격 아래 아이콘 줄: 내차피해이력, 사고진단, 정밀점검, 보증정보 등
        "highlights": {
            text(li.select_one("span")): text(li.select_one("strong.state"))
            for li in soup.select(".usp_item li")
        },
        "options": [text(s) for s in soup.select(".option_01 > li > span")],
        "selected_options": {
            text(li.select_one(".name")): text(li.select_one(".price"), sep="")
            for li in soup.select(".sel_option li")
        },
        "insurance_summary": {
            text(th): text(th.find_next_sibling("td"), sep="")
            for th in accident_layer.select(".head_box th")
        } if accident_layer else {},
        # 예: {"정보제공 불가능 기간": "2023년06월~2026년08월"}. 이 기간의 사고는 위 목록에 안 나온다.
        "insurance_notes": {
            text(dl.select_one("dt")): text(dl.select_one("dd"))
            for dl in accident_layer.select("dl.cont_history")
        } if accident_layer else {},
        "accidents": parse_accidents(accident_layer) if accident_layer else [],
        "center": center,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="현대·제네시스 인증중고차 상세 페이지 파싱")
    parser.add_argument("--date", default=date.today().isoformat(), help="읽을 날짜 폴더")
    args = parser.parse_args()

    in_dir = RAW_DIR / args.date / "detail"
    paths = sorted(in_dir.glob("*.html.gz"))
    if not paths:
        raise SystemExit(f"{in_dir} 에 받은 파일이 없습니다.")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"{args.date}.jsonl"
    skipped = []
    with out_path.open("w", encoding="utf-8") as f:
        for path in paths:
            goods_no = path.name.removesuffix(".html.gz")
            html = gzip.decompress(path.read_bytes()).decode("utf-8")
            item = parse_detail(html, goods_no)
            if item is None:
                skipped.append(goods_no)
                continue
            item["crawled_date"] = args.date
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    print(f"{len(paths) - len(skipped)}건 저장 → {out_path}")
    if skipped:
        print(f"내용이 없어 건너뜀 {len(skipped)}건: {', '.join(skipped)}")


if __name__ == "__main__":
    main()
