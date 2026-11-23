"""엔카 공개 검색 결과를 Playwright로 저장한다.

기본 조건은 국산차·2020년식 이후다. 목록을 스크롤해 차량 상세 링크를 모으고,
각 상세 페이지의 HTML을 원본으로 저장한다. 페이지 구조가 바뀌어도 원본을 다시
파싱할 수 있도록 HTML을 남기며, 파싱 결과에는 공개 메타데이터와 표 형식의 차량
정보만 넣는다.

실행 예시
  uv run playwright install chromium
  uv run scripts/crawl_encar.py --limit 20
  uv run scripts/crawl_encar.py --parse-only

다른 목록을 수집할 때만 브라우저에서 만든 검색 결과 URL을 ``--url``로 넘긴다.

차단/인증/접근제한 화면을 만나면 우회하지 않고 즉시 중단한다.
"""

import argparse
import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from crawl import DELAY_SEC, Crawler


class Encar(Crawler):
    name = "encar"
    raw_ext = ".html"
    detail_url = "https://www.encar.com/dc/dc_cardetailview.do?carid={}"

    # ``carid``는 기존/현재 상세 링크에서 쓰이는 식별자다. URL 구조가 바뀐 경우에도
    # 저장은 가능하도록 URL 전체를 base64 대신 안전한 짧은 해시 이름으로 바꾼다.
    CAR_ID = re.compile(r"(?:carid|carId)=([A-Za-z0-9_-]+)")
    BLOCK_TEXT = ("비정상적인 트래픽", "서비스 이용 제한", "접근이 제한", "captcha")
    DEFAULT_SEARCH_URL = "https://www.encar.com/dc/dc_carsearchlist.do"

    def __init__(self, day: str, search_url: str, max_pages: int | None, min_year: int):
        super().__init__(day)
        self.search_url = search_url
        self.max_pages = max_pages
        self.min_year = min_year
        self._detail_urls: dict[str, str] = {}

    def raw_path(self, item_id: str) -> Path:
        return self.raw_dir / "detail" / f"{item_id}{self.raw_ext}.gz"

    def run(self, limit: int | None, parse_only: bool) -> None:
        if not parse_only:
            self.download_details(limit)
        self.parse_all()

    def download_details(self, limit: int | None) -> None:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright

        (self.raw_dir / "detail").mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chromium")
            context = browser.new_context(locale="ko-KR", viewport={"width": 1440, "height": 1200})
            # 사진은 차량 카드의 링크 수집과 HTML 저장에 필요 없으므로 받지 않는다.
            context.route("**/*.{jpg,jpeg,png,gif,webp,avif}", lambda route: route.abort())
            page = context.new_page()
            try:
                page.goto(self.search_url, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(int(DELAY_SEC * 1000))
                self.raise_if_blocked(page.title(), page.locator("body").inner_text(timeout=10_000))
                self.open_domestic_search(page)
                self.apply_min_year(page)
                self._collect_detail_urls(page, limit)

                ids_path = self.raw_dir / "ids.json"
                if limit is None:
                    ids_path.write_text(
                        json.dumps(self._detail_urls, ensure_ascii=False, indent=2), encoding="utf-8"
                    )

                todo = [(item_id, url) for item_id, url in self._detail_urls.items()
                        if not self.raw_path(item_id).exists()]
                self.log(f"상세: 전체 {len(self._detail_urls)}건 중 {len(todo)}건을 받습니다")
                failures = 0
                for number, (item_id, detail_url) in enumerate(todo, 1):
                    try:
                        page.goto(detail_url, wait_until="domcontentloaded", timeout=60_000)
                        page.wait_for_timeout(int(DELAY_SEC * 1000))
                        body = page.locator("body").inner_text(timeout=10_000)
                        self.raise_if_blocked(page.title(), body)
                        self.save_raw(item_id, page.content().encode("utf-8"))
                    except PlaywrightTimeoutError as exc:
                        failures += 1
                        self.log(f"[실패] {item_id}: 시간 초과 ({exc})")
                    else:
                        failures = 0

                    if failures >= 5:
                        raise RuntimeError("5번 연속 실패해서 중단합니다. 접근 제한 여부를 확인하세요.")
                    if number % 25 == 0 or number == len(todo):
                        self.log(f"상세 {number}/{len(todo)}")
            finally:
                context.close()
                browser.close()

    @staticmethod
    def open_domestic_search(page) -> None:
        """기본 URL은 '국산차 검색 시작' 랜딩 화면이므로 공개 검색 화면으로 한 번 이동한다.

        ``--url``으로 이미 검색 결과를 넘긴 경우에는 이 링크가 없어 아무 일도 하지 않는다.
        연식은 상세 페이지의 차량 정보에서 다시 검증하므로, UI의 필터 명칭·구조가
        바뀌어도 2020년식 미만 매물이 processed JSONL에 섞이지 않는다.
        """
        start = page.get_by_text("국산차 검색 시작", exact=True)
        if start.count() and start.first.is_visible():
            start.first.click()
            page.wait_for_timeout(int(DELAY_SEC * 1000))

    def apply_min_year(self, page) -> None:
        """목록 화면에 최소 연식 ``select``가 있을 때만 서버 검색 조건에도 적용한다.

        엔카는 화면 버전·응답 상태에 따라 이 컨트롤을 렌더링하지 않기도 한다.
        그 경우에도 ``parse`` 단계의 연식 검증이 결과를 걸러야 하므로, 여기서
        수집 전체를 실패시키면 안 된다.
        """
        selector = "select[title='연식 범위 최소 연도']"
        years = page.locator(selector)
        for index in range(years.count()):
            year = years.nth(index)
            if year.is_visible():
                year.select_option(str(self.min_year))
                page.wait_for_timeout(int(DELAY_SEC * 1000))
                return
        self.log("목록 연식 필터를 찾지 못해 상세 페이지 연식 검증으로 거릅니다")

    def _collect_detail_urls(self, page, limit: int | None) -> None:
        """엔카의 번호 페이지를 끝까지 넘기며 매물 상세 링크를 모은다."""
        pages_seen: set[str] = set()
        while True:
            current = page.locator("#pagination a.current")
            if not current.count():
                raise RuntimeError("엔카 목록의 페이지네이션을 찾지 못했습니다.")
            current_page = current.first.get_attribute("data-page")
            if not current_page or current_page in pages_seen:
                break
            pages_seen.add(current_page)

            hrefs = page.locator("a[href]").evaluate_all("links => links.map(link => link.href)")
            for href in hrefs:
                item_id = self.item_id_from_url(href)
                if item_id:
                    self._detail_urls.setdefault(item_id, href)
                    if limit and len(self._detail_urls) >= limit:
                        break
            self.log(f"목록 {current_page}쪽, 누적 {len(self._detail_urls)}건")

            if (limit and len(self._detail_urls) >= limit) or (
                self.max_pages is not None and len(pages_seen) >= self.max_pages
            ):
                break

            next_page = str(int(current_page) + 1)
            next_link = page.locator(f"#pagination a[data-page='{next_page}']")
            if not next_link.count():
                break
            next_link.first.click()
            page.locator(f"#pagination a.current[data-page='{next_page}']").wait_for(
                state="attached", timeout=30_000
            )
            page.wait_for_timeout(int(DELAY_SEC * 1000))

        if not self._detail_urls:
            raise RuntimeError(
                "차량 상세 링크를 찾지 못했습니다. --url이 차량 검색 결과 URL인지, "
                "사이트의 링크 구조가 바뀌지 않았는지 확인하세요."
            )

    @classmethod
    def item_id_from_url(cls, href: str) -> str | None:
        parsed = urlparse(href)
        query = parse_qs(parsed.query)
        for key in ("carid", "carId", "id"):
            if query.get(key):
                return query[key][0]
        match = cls.CAR_ID.search(href)
        if match:
            return match.group(1)
        # /cars/<매물번호> 같은 새 URL도 받는다. 일반 메뉴 링크는 제외한다.
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[-2] in {"car", "cars", "detail"} and re.fullmatch(r"[A-Za-z0-9_-]+", parts[-1]):
            return parts[-1]
        return None

    @classmethod
    def raise_if_blocked(cls, title: str, body: str) -> None:
        haystack = f"{title}\n{body}".lower()
        if any(text.lower() in haystack for text in cls.BLOCK_TEXT):
            raise RuntimeError("엔카 접근 제한 화면을 받았습니다. 우회하지 않고 중단합니다.")

    def parse(self, raw: bytes, item_id: str) -> dict | None:
        soup = BeautifulSoup(raw.decode("utf-8", errors="replace"), "html.parser")
        title = soup.select_one("h1")
        page_title = soup.title.get_text(" ", strip=True) if soup.title else None
        text = title.get_text(" ", strip=True) if title else page_title
        if not text:
            return None

        metadata = {
            meta.get("property") or meta.get("name"): meta.get("content")
            for meta in soup.select("meta[property], meta[name]")
            if (meta.get("property") or meta.get("name")) in {"og:title", "og:description", "og:image", "description"}
            and meta.get("content")
        }
        facts = {}
        for label in soup.select("dt"):
            value = label.find_next_sibling("dd")
            if value:
                facts[label.get_text(" ", strip=True)] = value.get_text(" ", strip=True)
        for label in soup.select("th"):
            value = label.find_next_sibling("td")
            if value:
                facts[label.get_text(" ", strip=True)] = value.get_text(" ", strip=True)

        model_year = self.find_model_year(facts, soup.get_text(" ", strip=True))
        if model_year is None or model_year < self.min_year:
            return None
        return {
            "name": text,
            "model_year": model_year,
            "page_title": page_title,
            "metadata": metadata,
            "facts": facts,
        }

    @staticmethod
    def find_model_year(facts: dict[str, str], page_text: str) -> int | None:
        """'연식' 표의 값을 우선 사용하고, 화면 개편 시에는 공개 본문에서 보완한다."""
        year_text = " ".join(value for key, value in facts.items() if "연식" in key)
        match = re.search(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)", year_text)
        if not match:
            # 엔카 상세는 2020년식을 '20/01식'처럼 두 자리로 표시하기도 한다.
            short_year = re.search(r"(?<!\d)(\d{2})\s*/\s*\d{1,2}\s*식", year_text)
            if short_year:
                suffix = int(short_year.group(1))
                return 2000 + suffix if suffix <= 30 else 1900 + suffix
        if not match:
            match = re.search(r"(?<!\d)(19\d{2}|20\d{2})년식", page_text)
        return int(match.group(1)) if match else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Playwright로 엔카 공개 검색 결과 수집")
    parser.add_argument(
        "--url", default=Encar.DEFAULT_SEARCH_URL,
        help="기본 국산차 목록 대신 사용할 엔카 차량 검색 결과 URL",
    )
    parser.add_argument("--limit", type=int, help="시험용: 앞에서부터 N건만 저장")
    parser.add_argument("--date", default=date.today().isoformat(), help="저장할 날짜 폴더")
    parser.add_argument("--max-pages", type=int, help="시험용: 목록 페이지를 N쪽까지만 수집")
    parser.add_argument("--min-year", type=int, default=2020, help="저장할 최소 연식 (기본: 2020)")
    parser.add_argument("--parse-only", action="store_true", help="받지 않고 저장된 원본만 다시 정리")
    args = parser.parse_args()

    Encar(args.date, args.url, args.max_pages, args.min_year).run(args.limit, args.parse_only)


if __name__ == "__main__":
    main()
