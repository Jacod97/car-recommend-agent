import argparse
import json
import os
import time
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent

API_URL = "https://apis.data.go.kr/B553530/CAREFF/CAREFF_LIST"
PAGE_SIZE = 1000
DELAY_SEC = 1.0


def load_key() -> str:
    if key := os.environ.get("DATA_GO_KR_KEY"):
        return key
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "DATA_GO_KR_KEY":
                return value.strip()
    raise SystemExit(".env에 DATA_GO_KR_KEY가 없습니다")


def fetch_page(session: requests.Session, key: str, page_no: int) -> dict:
    resp = session.get(API_URL, params={
        "serviceKey": key, "pageNo": page_no, "numOfRows": PAGE_SIZE, "apiType": "JSON",
    }, timeout=30)
    try:
        data = resp.json()
    except ValueError:
        resp.raise_for_status()
        raise SystemExit(f"JSON이 아닌 응답: {resp.text[:300]}")
    if "OpenAPI_ServiceResponse" in data:
        header = data["OpenAPI_ServiceResponse"]["cmmMsgHeader"]
        raise SystemExit(f"API 오류: {header.get('errMsg')} ({header.get('returnAuthMsg')})")
    body = data.get("response", data).get("body", {})
    header = data.get("response", data).get("header", {})
    if header.get("resultCode") not in (None, "00"):
        raise SystemExit(f"API 오류: {header.get('resultCode')} {header.get('resultMsg')}")
    return body


def page_items(body: dict) -> list[dict]:
    items = (body.get("items") or {}).get("item") or []
    return [items] if isinstance(items, dict) else items


def main() -> None:
    parser = argparse.ArgumentParser(description="한국에너지공단 자동차 표시연비 목록 수집")
    parser.add_argument("--limit-pages", type=int, help="시험용: 앞에서부터 N쪽만 받는다")
    parser.add_argument("--date", default=date.today().isoformat(), help="저장할 날짜 폴더")
    args = parser.parse_args()

    key = load_key()
    raw_dir = ROOT / "data" / "raw" / "fueleff" / args.date
    out_path = ROOT / "data" / "processed" / "fueleff" / f"{args.date}.jsonl"
    raw_dir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    items: list[dict] = []
    page_no = 1
    total = None
    while total is None or len(items) < total:
        body = fetch_page(session, key, page_no)
        (raw_dir / f"page_{page_no:04d}.json").write_text(
            json.dumps(body, ensure_ascii=False), encoding="utf-8")
        page = page_items(body)
        total = int(body.get("totalCount") or 0)
        if not page:
            break
        items.extend(page)
        print(f"[fueleff] {page_no}쪽, {len(items)}/{total}건", flush=True)
        if args.limit_pages and page_no >= args.limit_pages:
            break
        page_no += 1
        time.sleep(DELAY_SEC)

    with out_path.open("w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps({"source": "fueleff", **item, "crawled_date": args.date},
                               ensure_ascii=False) + "\n")
    print(f"[fueleff] {len(items)}건 저장 → {out_path}")


if __name__ == "__main__":
    main()
