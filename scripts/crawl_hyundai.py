"""현대·제네시스 인증중고차(certified.hyundai.com) 매물 상세 페이지를 받아 저장한다.

1. 검색 목록을 끝까지 넘기며 매물 번호(goodsNo)를 모은다.
2. 매물마다 상세 페이지 HTML을 받아 gzip으로 저장한다.

저장 위치: data/raw/hyundai/<날짜>/
    goods_nos.json            목록에서 모은 매물 번호
    detail/<goodsNo>.html.gz  상세 페이지 원본

이미 받은 매물은 건너뛰므로, 중간에 끊겨도 같은 날짜로 다시 실행하면 이어서 받는다.
HTML에서 값을 뽑는 일은 parse_hyundai.py가 맡는다.

실행: uv run scripts/crawl_hyundai.py [--limit N] [--date YYYY-MM-DD]
"""

import argparse
import gzip
import json
import re
import time
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw" / "hyundai"

BASE_URL = "https://certified.hyundai.com"
# 검색 화면의 '더보기' 버튼이 쓰는 주소.
# 첫 화면용 주소(/p/search/vehicle/list)는 매물 사이에 배너가 한 칸 끼어 있어 위치 계산이 어긋난다.
LIST_URL = f"{BASE_URL}/p/search/vehicle/element"
DETAIL_URL = f"{BASE_URL}/p/goods/goodsDetail.do"

PAGE_SIZE = 15  # 사이트 기본값. 100으로 요청하면 400 오류가 난다.
DELAY_SEC = 1.0  # 요청 사이 대기 시간
MAX_CONSECUTIVE_FAILURES = 5  # 연달아 이만큼 실패하면 막힌 것으로 보고 멈춘다

# 목록 HTML의 링크가 goodsDeatil('GJX260827031478') 형태다. Deatil은 사이트 원문 철자.
GOODS_NO_PATTERN = re.compile(r"goodsDeatil\(&#39;([A-Z0-9]+)&#39;\)")


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0 Safari/537.36"
        ),
        "Referer": f"{BASE_URL}/p/search/vehicle",
    })
    return session


def fetch_goods_nos(session: requests.Session, limit: int | None) -> list[str]:
    """목록을 PAGE_SIZE씩 넘기며 매물 번호를 모은다. 새 번호가 안 나오면 끝으로 본다."""
    goods_nos: list[str] = []
    start_no = 0
    while limit is None or len(goods_nos) < limit:
        resp = session.post(
            LIST_URL,
            data={
                "type": "PLP",
                "startNo": start_no,
                "rowsPerPage": PAGE_SIZE,
                "listCnt": PAGE_SIZE,
                "sortType": "basic",
                "srchType": "srchWord",
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=30,
        )
        resp.raise_for_status()

        page = GOODS_NO_PATTERN.findall(resp.text)
        if not set(page) - set(goods_nos):
            break
        goods_nos.extend(page)
        print(f"목록 {len(goods_nos)}건", flush=True)

        start_no += PAGE_SIZE
        time.sleep(DELAY_SEC)

    # 수집 도중 새 매물이 올라오면 순서가 밀려 같은 번호가 두 번 나올 수 있다.
    unique = list(dict.fromkeys(goods_nos))
    return unique[:limit] if limit else unique


def fetch_details(session: requests.Session, goods_nos: list[str], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    todo = [no for no in goods_nos if not (out_dir / f"{no}.html.gz").exists()]
    print(f"상세 페이지: 전체 {len(goods_nos)}건 중 {len(todo)}건을 받습니다", flush=True)

    failures = 0
    for i, goods_no in enumerate(todo, 1):
        try:
            resp = session.get(DETAIL_URL, params={"goodsNo": goods_no}, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            # 목록을 모은 뒤 팔린 매물은 500이 온다.
            failures += 1
            print(f"[실패] {goods_no}: {e}", flush=True)
            if failures >= MAX_CONSECUTIVE_FAILURES:
                raise SystemExit(f"{failures}번 연달아 실패해서 멈춥니다. 다시 실행하면 이어서 받습니다.")
        else:
            failures = 0
            # 쓰는 도중 끊기면 반쪽 파일이 '받은 매물'로 남으므로, 임시 파일에 쓴 뒤 이름을 바꾼다.
            path = out_dir / f"{goods_no}.html.gz"
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(gzip.compress(resp.content))
            tmp.replace(path)

        if i % 50 == 0 or i == len(todo):
            print(f"상세 {i}/{len(todo)}", flush=True)
        time.sleep(DELAY_SEC)


def main() -> None:
    parser = argparse.ArgumentParser(description="현대·제네시스 인증중고차 매물 수집")
    parser.add_argument("--limit", type=int, help="시험용: 앞에서부터 N건만 받는다")
    parser.add_argument("--date", default=date.today().isoformat(), help="저장할 날짜 폴더 (이어받기용)")
    args = parser.parse_args()

    out_dir = RAW_DIR / args.date
    session = make_session()

    # 목록은 끝까지 다 모았을 때만 저장한다. --limit으로 일부만 모은 목록이
    # 저장되면, 같은 날 전체 실행할 때 그 일부 목록을 그대로 쓰게 되기 때문이다.
    list_path = out_dir / "goods_nos.json"
    if list_path.exists():
        goods_nos = json.loads(list_path.read_text())
        print(f"저장된 목록 {len(goods_nos)}건을 씁니다", flush=True)
    else:
        goods_nos = fetch_goods_nos(session, args.limit)
        if args.limit is None:
            out_dir.mkdir(parents=True, exist_ok=True)
            list_path.write_text(json.dumps(goods_nos, indent=0))

    if args.limit:
        goods_nos = goods_nos[: args.limit]
    fetch_details(session, goods_nos, out_dir / "detail")


if __name__ == "__main__":
    main()
