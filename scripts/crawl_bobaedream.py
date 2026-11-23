import argparse
import re
from datetime import date
from urllib.parse import quote

from bs4 import BeautifulSoup, Tag

from crawl import Crawler

BASE = "https://www.bobaedream.co.kr"
BOARDS = ["national", "import"]
KEYWORDS = ["고민", "추천", "첫차", "중고차", "구매", "구입", "살까", "조언", "골라", "어떤차", "뭐살"]


def text(el: Tag | None, sep: str = " ") -> str | None:
    if el is None:
        return None
    return " ".join(el.get_text(sep, strip=True).split())


def body_text(el: Tag | None) -> str | None:
    if el is None:
        return None
    for br in el.select("br"):
        br.replace_with("\n")
    lines = [" ".join(line.split()) for line in el.get_text("\n").splitlines()]
    return "\n".join(line for line in lines if line) or None


def to_int(s: str | None) -> int | None:
    digits = re.sub(r"[^\d]", "", s or "")
    return int(digits) if digits else None


class Bobaedream(Crawler):
    name = "bobaedream"
    raw_ext = ".html"
    detail_url = "{}"
    first_page = None

    LIST_URL = BASE + "/board/bulletin/list.php"
    PAGE_SIZE = 30
    MAX_PAGES = 100

    def __init__(self, day: str, since_year: int):
        super().__init__(day)
        self.since_year = since_year

    def collect_ids(self, limit):
        ids: list[str] = []
        for board in BOARDS:
            for keyword in KEYWORDS:
                found = self.search(board, keyword)
                ids.extend(found)
                self.log(f"{board} '{keyword}': {len(found)}건, 누적 {len(dict.fromkeys(ids))}건")
                if limit and len(dict.fromkeys(ids)) >= limit:
                    return list(dict.fromkeys(ids))
        return list(dict.fromkeys(ids))

    def search(self, board: str, keyword: str) -> list[str]:
        today = date.today()
        year, prev = today.year, (today.month, today.day)
        ids: list[str] = []
        for page_no in range(1, self.MAX_PAGES + 1):
            url = (f"{self.LIST_URL}?code={board}&s_select=Subject&s_key={quote(keyword.encode('cp949'))}"
                   f"&pagescale={self.PAGE_SIZE}&page={page_no}")
            soup = BeautifulSoup(self.request("GET", url).content.decode("utf-8", "replace"), "html.parser")
            rows = [r for r in soup.select("table tbody tr")
                    if (text(r.select_one("td.num01")) or "").isdigit() and r.select_one('a[href*="/view?"]')]
            if not rows:
                break
            for row in rows:
                m = re.match(r"(\d{2})/(\d{2})", text(row.select_one("td.date")) or "")
                md = (int(m.group(1)), int(m.group(2))) if m else (today.month, today.day)
                if md > prev:
                    year -= 1
                prev = md
                if year < self.since_year:
                    return ids
                no = re.search(r"No=(\d+)", row.select_one('a[href*="/view?"]')["href"]).group(1)
                ids.append(f"{board}-{no}")
        return ids

    def fetch_raw(self, item_id):
        board, no = item_id.split("-")
        return self.request("GET", f"{BASE}/view?code={board}&No={no}").content

    def parse(self, raw, item_id):
        board, no = item_id.split("-")
        soup = BeautifulSoup(raw.decode("utf-8", "replace"), "html.parser")
        head = soup.select_one(".writerProfile")
        if head is None:
            return None
        title_el = head.select_one("dt strong")
        for em in title_el.select("em"):
            em.extract()
        counts = head.select(".countGroup em.txtType")
        posted = re.search(r"(\d{4})\.(\d{2})\.(\d{2})\s*\(.\)\s*(\d{2}:\d{2})", text(head.select_one(".countGroup")) or "")
        author = text(head.select_one("a.nickName"))

        comments = []
        for li in soup.select("#cmt_reply > li"):
            cmt = li.select_one("dd[id^=small_cmt_]")
            if cmt is None:
                continue
            votes = [to_int(text(a)) for a in li.select(".updownbox a")]
            comments.append({
                "id": cmt["id"].removeprefix("small_cmt_"),
                "date": text(li.select_one(".date")),
                "is_op": author is not None and text(li.select_one(".author")) == author,
                "is_reply": "re" in li.get("class", []),
                "text": body_text(cmt),
                "up": votes[0] if votes else None,
                "down": votes[1] if len(votes) > 1 else None,
            })

        return {
            "url": f"{BASE}/view?code={board}&No={no}",
            "board": board,
            "title": text(title_el),
            "posted_at": f"{posted.group(1)}-{posted.group(2)}-{posted.group(3)} {posted.group(4)}" if posted else None,
            "views": to_int(text(counts[0])) if counts else None,
            "recommends": to_int(text(counts[1])) if len(counts) > 1 else None,
            "comment_count": to_int(text(soup.select_one("#cmt_list .comm2"))),
            "body": body_text(soup.select_one(".bodyCont")),
            "comments": comments,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="보배드림 구매 고민 글 수집")
    parser.add_argument("--limit", type=int, help="시험용: 앞에서부터 N건만 받는다")
    parser.add_argument("--since-year", type=int, default=2024, help="이 해 이후 글만 모은다")
    parser.add_argument("--date", default=date.today().isoformat(), help="저장할 날짜 폴더 (이어받기용)")
    parser.add_argument("--parse-only", action="store_true", help="받지 않고 저장된 원본만 다시 정리한다")
    args = parser.parse_args()
    Bobaedream(args.date, args.since_year).run(args.limit, args.parse_only)


if __name__ == "__main__":
    main()
