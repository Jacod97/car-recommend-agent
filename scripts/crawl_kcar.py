"""케이카(www.kcar.com) 직영 매물 목록 수집. 목록만 받고 상세는 받지 않는다.

crawl.py와 따로 둔 이유
  - 상세 화면은 robots.txt가 금지하고, 상세 응답에 이전 소유자 개인정보(주소·나이·성별)가 들어 있다.
    그래서 목록에 있는 요약 값(차종, 연식, 주행거리, 가격, 색상, 옵션 이름, 사고 이력 요약 등)만 받는다.
  - 목록 요청 값을 사이트 스크립트가 암호화해 보내므로(setEncParam) HTTP로 직접 요청하지 않는다.
    Playwright로 목록 화면을 열고 아래로 내리면 화면이 다음 26건을 스스로 불러오고, 그 응답을 저장한다.
    Playwright 기본 headless 브라우저에서는 화면이 비어 있어 일반 Chromium(channel="chromium")을 쓴다.

저장 위치와 결과 형식은 crawl.py와 같다 (목록 한 행을 매물 파일 하나로 저장).
이어받기는 하지 않는다. 다시 실행하면 목록을 처음부터 다시 내린다.

준비: uv run playwright install chromium
실행
  uv run scripts/crawl_kcar.py                  전체 목록
  uv run scripts/crawl_kcar.py --limit 50       시험: 앞에서 50건쯤 (26건 단위로 끊김)
  uv run scripts/crawl_kcar.py --parse-only     받지 않고 저장된 원본만 다시 정리
"""

import argparse
import json
from datetime import date

from crawl import DELAY_SEC, Crawler


class Kcar(Crawler):
    name = "kcar"
    raw_ext = ".json"
    detail_url = "https://www.kcar.com/bc/detail/carInfoDtl?i_sCarCd={}"

    LIST_PAGE = "https://www.kcar.com/bc/search"
    LIST_API = "api.kcar.com/bc/search/list/drct"  # 직영 매물 목록
    IDLE_LIMIT_SEC = 30  # 내려도 이 시간 동안 새 쪽이 안 오면 끝으로 본다
    # 목록 행에 들어 있는 판매 직원 이름·전화번호. 저장하지 않는다.
    PERSONAL_KEYS = ["selerNm", "selerMpno", "selerSafeMbpno", "selerSafeTno", "sellEmpId"]

    def run(self, limit, parse_only):
        if not parse_only:
            self.download_list(limit)
        self.parse_all()

    def download_list(self, limit: int | None) -> None:
        from playwright.sync_api import sync_playwright

        (self.raw_dir / "detail").mkdir(parents=True, exist_ok=True)
        ids: list[str] = []
        seen_pages: set[int] = set()
        total_pages = None

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chromium")
            page = browser.new_page(locale="ko-KR")
            # 사진은 받지 않는다. 사이트 부담과 브라우저 메모리를 줄이려는 것이다.
            page.route("**/*.{jpg,jpeg,png,gif,webp}", lambda route: route.abort())
            responses = []
            page.on("response", lambda r: responses.append(r) if self.LIST_API in r.url else None)
            page.goto(self.LIST_PAGE, wait_until="commit", timeout=60000)

            idle_sec = 0.0
            while total_pages is None or len(seen_pages) < total_pages:
                page.wait_for_timeout(DELAY_SEC * 1000)
                got_new_page = False
                while responses:
                    data = responses.pop(0).json()["data"]
                    # 첫 쪽은 화면이 두 번 부르기도 해서 쪽 번호로 거른다.
                    if data["pageNo"] in seen_pages:
                        continue
                    seen_pages.add(data["pageNo"])
                    total_pages = data["totalPageCnt"]
                    got_new_page = True
                    for row in data["rows"]:
                        for key in self.PERSONAL_KEYS:
                            row.pop(key, None)
                        self.save_raw(row["carCd"], json.dumps(row, ensure_ascii=False).encode("utf-8"))
                        ids.append(row["carCd"])
                    self.log(f"목록 {len(seen_pages)}/{total_pages}쪽, {len(ids)}건")

                if limit and len(ids) >= limit:
                    break
                idle_sec = 0.0 if got_new_page else idle_sec + DELAY_SEC
                if idle_sec >= self.IDLE_LIMIT_SEC:
                    self.log(f"{self.IDLE_LIMIT_SEC}초 동안 새 목록이 오지 않아 멈춥니다")
                    break
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            browser.close()

        # 목록은 끝까지 다 모았을 때만 저장한다 (crawl.py의 load_ids와 같은 이유).
        if limit is None and total_pages is not None and len(seen_pages) == total_pages:
            (self.raw_dir / "ids.json").write_text(json.dumps(list(dict.fromkeys(ids))))
        else:
            self.log("목록을 끝까지 받지 못해 ids.json은 저장하지 않습니다")

    def parse(self, raw, car_cd):
        return json.loads(raw)


def main() -> None:
    parser = argparse.ArgumentParser(description="케이카 직영 매물 목록 수집")
    parser.add_argument("--limit", type=int, help="시험용: 앞에서부터 대략 N건만 받는다")
    parser.add_argument("--date", default=date.today().isoformat(), help="저장할 날짜 폴더")
    parser.add_argument("--parse-only", action="store_true", help="받지 않고 저장된 원본만 다시 정리한다")
    args = parser.parse_args()
    Kcar(args.date).run(args.limit, args.parse_only)


if __name__ == "__main__":
    main()
