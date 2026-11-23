import argparse
import gzip
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup, Tag

ROOT = Path(__file__).resolve().parent.parent

DELAY_SEC = 1.0
MAX_CONSECUTIVE_FAILURES = 5
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0 Safari/537.36"
)


class Crawler:
    name: str
    raw_ext: str
    detail_url: str
    first_page: object
    headers: dict = {}

    def __init__(self, day: str):
        self.day = day
        self.raw_dir = ROOT / "data" / "raw" / self.name / day
        self.out_path = ROOT / "data" / "processed" / self.name / f"{day}.jsonl"
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, **self.headers})

    def fetch_page(self, position) -> tuple[list[str], object]:
        raise NotImplementedError

    def fetch_raw(self, item_id: str) -> bytes:
        raise NotImplementedError

    def parse(self, raw: bytes, item_id: str) -> dict | None:
        raise NotImplementedError

    def log(self, msg: str) -> None:
        print(f"[{self.name}] {msg}", flush=True)

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        time.sleep(DELAY_SEC)
        resp = self.session.request(method, url, timeout=30, **kwargs)
        resp.raise_for_status()
        return resp

    def run(self, limit: int | None, parse_only: bool) -> None:
        if not parse_only:
            self.download(self.load_ids(limit))
        self.parse_all()

    def load_ids(self, limit: int | None) -> list[str]:
        path = self.raw_dir / "ids.json"
        if path.exists():
            ids = json.loads(path.read_text())
            self.log(f"저장된 목록 {len(ids)}건을 씁니다")
        else:
            ids = self.collect_ids(limit)
            if limit is None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(ids))
        return ids[:limit] if limit else ids

    def collect_ids(self, limit: int | None) -> list[str]:
        ids: list[str] = []
        position = self.first_page
        while position is not None and (limit is None or len(ids) < limit):
            page, position = self.fetch_page(position)
            if not set(page) - set(ids):
                break
            ids.extend(page)
            self.log(f"목록 {len(ids)}건")
        return list(dict.fromkeys(ids))

    def raw_path(self, item_id: str) -> Path:
        return self.raw_dir / "detail" / f"{item_id}{self.raw_ext}.gz"

    def download(self, ids: list[str]) -> None:
        (self.raw_dir / "detail").mkdir(parents=True, exist_ok=True)
        todo = [i for i in ids if not self.raw_path(i).exists()]
        self.log(f"상세: 전체 {len(ids)}건 중 {len(todo)}건을 받습니다")

        failures = 0
        for n, item_id in enumerate(todo, 1):
            try:
                raw = self.fetch_raw(item_id)
            except requests.RequestException as e:
                failures += 1
                self.log(f"[실패] {item_id}: {e}")
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    raise RuntimeError(f"{failures}번 연달아 실패해서 멈춥니다. 다시 실행하면 이어서 받습니다.")
            else:
                failures = 0
                self.save_raw(item_id, raw)

            if n % 50 == 0 or n == len(todo):
                self.log(f"상세 {n}/{len(todo)}")

    def save_raw(self, item_id: str, raw: bytes) -> None:
        path = self.raw_path(item_id)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(gzip.compress(raw))
        tmp.replace(path)

    def parse_all(self) -> None:
        paths = sorted((self.raw_dir / "detail").glob(f"*{self.raw_ext}.gz"))
        if not paths:
            self.log(f"{self.raw_dir / 'detail'} 에 받은 원본이 없습니다")
            return

        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        skipped = []
        with self.out_path.open("w", encoding="utf-8") as f:
            for path in paths:
                item_id = path.name.removesuffix(f"{self.raw_ext}.gz")
                data = self.parse(gzip.decompress(path.read_bytes()), item_id)
                if data is None:
                    skipped.append(item_id)
                    continue
                item = {"source": self.name, "id": item_id, "url": self.detail_url.format(item_id),
                        **data, "crawled_date": self.day}
                f.write(json.dumps(item, ensure_ascii=False) + "\n")

        self.log(f"{len(paths) - len(skipped)}건 저장 → {self.out_path}")
        if skipped:
            self.log(f"데이터가 없어 건너뜀 {len(skipped)}건: {', '.join(skipped)}")


def text(el: Tag | None, sep: str = " ") -> str | None:
    if el is None:
        return None
    return " ".join(el.get_text(sep, strip=True).split())


class Hyundai(Crawler):
    name = "hyundai"
    raw_ext = ".html"
    detail_url = "https://certified.hyundai.com/p/goods/goodsDetail.do?goodsNo={}"
    headers = {"Referer": "https://certified.hyundai.com/p/search/vehicle"}
    first_page = 0

    LIST_URL = "https://certified.hyundai.com/p/search/vehicle/element"
    PAGE_SIZE = 15
    GOODS_NO = re.compile(r"goodsDeatil\(&#39;([A-Z0-9]+)&#39;\)")

    GA4_BLOCK = re.compile(r"const ga4Obj = \{(.*?)items\s*:", re.S)
    GA4_FIELD = re.compile(r'(\w+)\s*:\s*"((?:[^"\\]|\\.)*)"')
    GA4_KEYS = ["brand_type", "vehicle_modelgroup", "vehicle_model", "vehicle_grade", "vehicle_trim"]

    def fetch_page(self, start_no):
        resp = self.request(
            "POST", self.LIST_URL,
            data={"type": "PLP", "startNo": start_no, "rowsPerPage": self.PAGE_SIZE,
                  "listCnt": self.PAGE_SIZE, "sortType": "basic", "srchType": "srchWord"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        return self.GOODS_NO.findall(resp.text), start_no + self.PAGE_SIZE

    def fetch_raw(self, goods_no):
        return self.request("GET", self.detail_url.format(goods_no)).content

    def parse(self, raw, goods_no):
        html = raw.decode("utf-8")
        soup = BeautifulSoup(html, "html.parser")
        name = text(soup.select_one("h1.name"))
        if name is None:
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
            "name": name,
            "sale_status_code": status_input.get("value") if status_input else None,
            "model_info": self.parse_model_info(html),
            "flags": [text(s) for s in soup.select(".pdp02_price .flag span")],
            "price": text(price_box.select_one(".txt.pay"), sep="") if price_box else None,
            "price_before_discount": text(price_box.select_one(".txt.del"), sep="") if price_box else None,
            "new_car_price": text(soup.select_one(".car_price .txt_con"), sep=""),
            "basic_info": {
                text(li.select_one(".tit")): text(li.select_one(".txt"))
                for li in soup.select(".pdp03_tabs.first ol.base_01 > li")
            },
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
            "insurance_notes": {
                text(dl.select_one("dt")): text(dl.select_one("dd"))
                for dl in accident_layer.select("dl.cont_history")
            } if accident_layer else {},
            "accidents": self.parse_accidents(accident_layer) if accident_layer else [],
            "center": center,
        }

    def parse_model_info(self, html: str) -> dict:
        m = self.GA4_BLOCK.search(html)
        if not m:
            return {}
        fields = {k: json.loads(f'"{v}"') for k, v in self.GA4_FIELD.findall(m.group(1))}
        return {k: fields[k] for k in self.GA4_KEYS if k in fields}

    @staticmethod
    def parse_accidents(layer: Tag) -> list[dict]:
        accidents = []
        for li in layer.select("ol.list_history > li"):
            date_text = text(li.select_one("p.date")) or ""
            records = []
            for box in li.select("div.box"):
                spans = box.select("p.txt > span")
                records.append({
                    "type": text(spans[0]) if spans else None,
                    "handling": text(spans[1], sep="") if len(spans) > 1 else None,
                    "costs": {
                        text(item.select_one("span:nth-of-type(1)")): text(item.select_one("span:nth-of-type(2)"), sep="")
                        for item in box.select("div.cont li")
                    },
                    "repair_detail": (text(box.select_one("p.info")) or "").removeprefix("수리내역 -").strip() or None,
                })
            accidents.append({"date": date_text.split(":", 1)[-1].strip(), "records": records})
        return accidents


class Kia(Crawler):
    name = "kia"
    raw_ext = ".json"
    detail_url = "https://cpo.kia.com/products/detail/?id={}"
    headers = {"Referer": "https://cpo.kia.com/products"}
    first_page = []

    API_URL = "https://cpo.kia.com/api"
    PAGE_SIZE = 100
    DETAIL_ENDPOINTS = ["detail", "car", "options", "insurance-history", "cost"]

    def fetch_page(self, cursors):
        params = {"size": self.PAGE_SIZE, "sort": "DISPLAYED_AT_DESC"}
        if cursors:
            params["cursors"] = cursors
        content = self.request("GET", f"{self.API_URL}/search/", params=params).json()["content"]
        next_cursors = content[-1]["cursors"] if len(content) == self.PAGE_SIZE else None
        return [str(item["id"]) for item in content], next_cursors

    def fetch_raw(self, item_id):
        record = {
            name: self.request("GET", f"{self.API_URL}/product/{name}/{item_id}/").json()
            for name in self.DETAIL_ENDPOINTS
        }
        seller = record["detail"].get("productSeller") or {}
        seller.pop("name", None)
        seller.pop("number", None)
        return json.dumps(record, ensure_ascii=False).encode("utf-8")

    def parse(self, raw, item_id):
        return json.loads(raw)


class Heydealer(Crawler):

    name = "heydealer"
    raw_ext = ".html"
    detail_url = "https://www.heydealer.com/market/cars/{}"
    first_page = 1

    LIST_URL = "https://www.heydealer.com/market/cars"
    HASH_ID = re.compile(r'href="/market/cars/([A-Za-z0-9]{8})"')
    FLIGHT_CHUNK = re.compile(r"<script>self\.__next_f\.push\((\[.*?\])\)</script>", re.S)
    DROP_KEYS = ["top_car_list_group", "bottom_car_list_group"]

    def fetch_page(self, page_no):
        html = self.request("GET", self.LIST_URL, params={"page": page_no}).text
        return list(dict.fromkeys(self.HASH_ID.findall(html))), page_no + 1

    def fetch_raw(self, hash_id):
        return self.request("GET", self.detail_url.format(hash_id)).content

    def parse(self, raw, hash_id):
        chunks = []
        for m in self.FLIGHT_CHUNK.finditer(raw.decode("utf-8")):
            try:
                arr = json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
            if len(arr) > 1 and isinstance(arr[1], str):
                chunks.append(arr[1])
        flight = "".join(chunks)

        start = flight.find(f'{{"car":{{"hash_id":"{hash_id}"')
        if start < 0:
            return None
        car = json.JSONDecoder().raw_decode(flight, start)[0]["car"]
        for key in self.DROP_KEYS:
            car.pop(key, None)
        return car


SITES = {cls.name: cls for cls in (Hyundai, Kia, Heydealer)}


def main() -> None:
    parser = argparse.ArgumentParser(description="중고차 플랫폼 매물 수집")
    parser.add_argument("site", choices=[*SITES, "all"])
    parser.add_argument("--limit", type=int, help="시험용: 앞에서부터 N건만 받는다")
    parser.add_argument("--date", default=date.today().isoformat(), help="저장할 날짜 폴더 (이어받기용)")
    parser.add_argument("--parse-only", action="store_true", help="받지 않고 저장된 원본만 다시 정리한다")
    args = parser.parse_args()

    crawlers = [SITES[name](args.date) for name in (SITES if args.site == "all" else [args.site])]

    with ThreadPoolExecutor(len(crawlers)) as pool:
        futures = {c.name: pool.submit(c.run, args.limit, args.parse_only) for c in crawlers}

    failed = []
    for name, future in futures.items():
        try:
            future.result()
        except Exception as e:
            failed.append(name)
            print(f"[{name}] 중단: {e}", flush=True)
    if failed:
        raise SystemExit(f"중단된 사이트: {', '.join(failed)}")


if __name__ == "__main__":
    main()
