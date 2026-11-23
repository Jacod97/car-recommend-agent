import argparse
import json
from datetime import date

from crawl import DELAY_SEC, Crawler


class Kcar(Crawler):
    name = "kcar"
    raw_ext = ".json"
    detail_url = "https://www.kcar.com/bc/detail/carInfoDtl?i_sCarCd={}"

    LIST_PAGE = "https://www.kcar.com/bc/search"
    LIST_API = "api.kcar.com/bc/search/list/drct"
    IDLE_LIMIT_SEC = 30
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
