"""중고차 플랫폼 매물 수집: 현대·제네시스 인증중고차, 기아 인증중고차, 헤이딜러.
케이카 목록은 브라우저 자동화가 필요해 crawl_kcar.py로 따로 뒀다.

사이트마다 다른 것은 세 가지이고, 사이트 class가 하나씩 구현한다.
  fetch_page  목록 한 쪽에서 매물 번호를 꺼내고 다음 쪽 위치를 돌려준다
  fetch_raw   매물 하나의 원본(HTML 또는 JSON)을 받는다
  parse       원본에서 매물 데이터를 꺼낸다
요청 간격, 연속 실패 시 중단, 이어받기, 저장은 Crawler가 공통으로 맡는다.

저장 위치
  data/raw/<사이트>/<날짜>/ids.json                 목록에서 모은 매물 번호 (끝까지 모았을 때만 저장)
  data/raw/<사이트>/<날짜>/detail/<번호>.html.gz    매물 원본 (기아는 .json.gz)
  data/processed/<사이트>/<날짜>.jsonl              매물 데이터, 한 줄에 하나

값은 사이트가 준 그대로 둔다. 단위 변환과 키 이름 통일은 플랫폼을 하나의 형식으로 합치는 단계에서 한다.
엔카는 robots.txt 금지와 crawler 차단 때문에 넣지 않았다 (docs/crawler.md).

실행
  uv run scripts/crawl.py all                     세 사이트를 동시에 받고 정리
  uv run scripts/crawl.py kia --limit 3           시험: 앞에서 3건만
  uv run scripts/crawl.py hyundai --parse-only    받지 않고 저장된 원본만 다시 정리
  uv run scripts/crawl.py heydealer --date 2026-11-22   그 날짜 폴더로 이어받기
"""

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

DELAY_SEC = 1.0  # 한 사이트에 보내는 요청 사이 대기 시간
MAX_CONSECUTIVE_FAILURES = 5  # 연달아 이만큼 실패하면 막힌 것으로 보고 그 사이트를 멈춘다
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# 공통
# ---------------------------------------------------------------------------

class Crawler:
    name: str  # 저장 폴더 이름
    raw_ext: str  # 원본 형식: ".html" 또는 ".json"
    detail_url: str  # 매물 화면 주소. {}에 매물 번호가 들어간다
    first_page: object  # 목록 첫 쪽의 위치 (쪽 번호, 시작 위치, cursor 등 사이트마다 다름)
    headers: dict = {}

    def __init__(self, day: str):
        self.day = day
        self.raw_dir = ROOT / "data" / "raw" / self.name / day
        self.out_path = ROOT / "data" / "processed" / self.name / f"{day}.jsonl"
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, **self.headers})

    # 사이트마다 구현 --------------------------------------------------------
    def fetch_page(self, position) -> tuple[list[str], object]:
        """목록 한 쪽의 매물 번호와 다음 쪽 위치를 돌려준다. 다음 쪽이 없으면 위치는 None."""
        raise NotImplementedError

    def fetch_raw(self, item_id: str) -> bytes:
        raise NotImplementedError

    def parse(self, raw: bytes, item_id: str) -> dict | None:
        """원본에서 매물 데이터를 꺼낸다. 오류 화면 등으로 데이터가 없으면 None."""
        raise NotImplementedError

    # 공통 ------------------------------------------------------------------
    def log(self, msg: str) -> None:
        print(f"[{self.name}] {msg}", flush=True)

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        """모든 요청이 여기를 지난다. 요청 전에 DELAY_SEC 쉬고, 4xx·5xx 응답은 예외로 올린다."""
        time.sleep(DELAY_SEC)
        resp = self.session.request(method, url, timeout=30, **kwargs)
        resp.raise_for_status()
        return resp

    def run(self, limit: int | None, parse_only: bool) -> None:
        if not parse_only:
            self.download(self.load_ids(limit))
        self.parse_all()

    def load_ids(self, limit: int | None) -> list[str]:
        # 목록은 끝까지 다 모았을 때만 저장한다. --limit으로 일부만 모은 목록이 저장되면,
        # 같은 날 전체 실행 때 그 일부 목록을 그대로 다시 쓰게 되기 때문이다.
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
            # 빈 쪽이거나, 서버가 같은 쪽을 계속 돌려주면 끝으로 본다.
            if not set(page) - set(ids):
                break
            ids.extend(page)
            self.log(f"목록 {len(ids)}건")
        # 수집 도중 새 매물이 올라오면 순서가 밀려 같은 번호가 다시 나올 수 있다.
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
                # 목록을 모은 뒤 팔린 매물은 오류가 올 수 있어, 연달아 실패할 때만 멈춘다.
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
        # 쓰는 도중 끊기면 반쪽 파일이 '받은 매물'로 남으므로, 임시 파일에 쓴 뒤 이름을 바꾼다.
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


# ---------------------------------------------------------------------------
# 현대·제네시스 인증중고차 (certified.hyundai.com): 목록·상세 모두 HTML
# ---------------------------------------------------------------------------

def text(el: Tag | None, sep: str = " ") -> str | None:
    """요소 안의 글자를 공백 하나로 이어 붙인다. 가격처럼 <em>4,590</em><i>만원</i>으로
    쪼개진 값은 sep=""로 붙여야 "4,590만원"이 된다."""
    if el is None:
        return None
    return " ".join(el.get_text(sep, strip=True).split())


class Hyundai(Crawler):
    name = "hyundai"
    raw_ext = ".html"
    detail_url = "https://certified.hyundai.com/p/goods/goodsDetail.do?goodsNo={}"
    headers = {"Referer": "https://certified.hyundai.com/p/search/vehicle"}
    first_page = 0  # 목록 시작 위치(startNo)

    # 검색 화면의 '더보기' 버튼이 쓰는 주소. 첫 화면용 /p/search/vehicle/list는
    # 매물 사이에 배너가 한 칸 끼어 있어 위치 계산이 어긋난다.
    LIST_URL = "https://certified.hyundai.com/p/search/vehicle/element"
    PAGE_SIZE = 15  # 사이트 기본값. 100으로 요청하면 400 오류가 난다.
    # 목록 HTML의 링크가 goodsDeatil('GJX260827031478') 형태다. Deatil은 사이트 원문 철자.
    GOODS_NO = re.compile(r"goodsDeatil\(&#39;([A-Z0-9]+)&#39;\)")

    # 페이지 스크립트의 광고 추적용 객체(ga4Obj)에만 모델·트림이 나뉘어 들어 있다.
    GA4_BLOCK = re.compile(r"const ga4Obj = \{(.*?)items\s*:", re.S)
    GA4_FIELD = re.compile(r'(\w+)\s*:\s*"((?:[^"\\]|\\.)*)"')
    GA4_KEYS = ["brand_type", "vehicle_modelgroup", "vehicle_model", "vehicle_grade", "vehicle_trim"]

    def fetch_page(self, start_no):
        resp = self.request(
            "POST", self.LIST_URL,
            # 검색 화면 스크립트(search-vehicle.js)가 보내는 값 그대로. filter 값은 비워 전체 매물을 받는다.
            data={"type": "PLP", "startNo": start_no, "rowsPerPage": self.PAGE_SIZE,
                  "listCnt": self.PAGE_SIZE, "sortType": "basic", "srchType": "srchWord"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        return self.GOODS_NO.findall(resp.text), start_no + self.PAGE_SIZE

    def fetch_raw(self, goods_no):
        return self.request("GET", self.detail_url.format(goods_no)).content

    def parse(self, raw, goods_no):
        """최상위 키는 이 스크립트가 정한 이름이고, 그 아래 dict의 키와 값은 화면 글자 그대로다.
        판매 직원 이름·사원증 번호는 개인정보라 뽑지 않는다."""
        html = raw.decode("utf-8")
        soup = BeautifulSoup(html, "html.parser")
        name = text(soup.select_one("h1.name"))
        if name is None:  # 오류 화면
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
            # "20"이면 구매 가능. 검색 화면의 '구매 가능한 차량' 필터가 이 값으로 거른다.
            # 다른 값(계약 중, 일시 품절 등으로 추정)의 정확한 뜻은 아직 확인하지 못했다.
            "sale_status_code": status_input.get("value") if status_input else None,
            "model_info": self.parse_model_info(html),
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
            # 예: {"정보제공 불가능 기간": "2023년06월~2026년08월"}. 이 기간의 사고는 accidents에 안 나온다.
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
        # 값이 JS 문자열이라 사, \/ 같은 이스케이프가 섞여 있다. JSON 문자열 규칙과 같아서 json.loads로 푼다.
        fields = {k: json.loads(f'"{v}"') for k, v in self.GA4_FIELD.findall(m.group(1))}
        return {k: fields[k] for k in self.GA4_KEYS if k in fields}

    @staticmethod
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


# ---------------------------------------------------------------------------
# 기아 인증중고차 (cpo.kia.com): 목록·상세 모두 JSON API
# ---------------------------------------------------------------------------

class Kia(Crawler):
    name = "kia"
    raw_ext = ".json"
    detail_url = "https://cpo.kia.com/products/detail/?id={}"
    headers = {"Referer": "https://cpo.kia.com/products"}
    first_page = []  # 첫 쪽은 cursors 없이 요청한다

    API_URL = "https://cpo.kia.com/api"
    PAGE_SIZE = 100  # 화면은 16건씩 부르지만 100도 받아 준다. 요청 수를 줄이려고 100.
    # 상세 화면이 부르는 API. performance-report는 신고번호 하나뿐이라 뺐다.
    DETAIL_ENDPOINTS = ["detail", "car", "options", "insurance-history", "cost"]

    def fetch_page(self, cursors):
        params = {"size": self.PAGE_SIZE, "sort": "DISPLAYED_AT_DESC"}
        if cursors:
            params["cursors"] = cursors  # 리스트를 넘기면 cursors=a&cursors=b 형태로 보낸다
        content = self.request("GET", f"{self.API_URL}/search/", params=params).json()["content"]
        # 다음 쪽은 이 쪽 마지막 매물의 cursors(노출 시각, id)로 요청한다. 정렬 기준이 고정이라
        # 수집 도중 새 매물이 올라와도 이미 받은 쪽과 겹치지 않는다. 덜 찬 쪽이 마지막 쪽이다.
        next_cursors = content[-1]["cursors"] if len(content) == self.PAGE_SIZE else None
        return [str(item["id"]) for item in content], next_cursors

    def fetch_raw(self, item_id):
        record = {
            name: self.request("GET", f"{self.API_URL}/product/{name}/{item_id}/").json()
            for name in self.DETAIL_ENDPOINTS
        }
        # 판매 직원 이름·사원번호는 저장하지 않는다. 주소와 080 대표번호는 남긴다.
        seller = record["detail"].get("productSeller") or {}
        seller.pop("name", None)
        seller.pop("number", None)
        return json.dumps(record, ensure_ascii=False).encode("utf-8")

    def parse(self, raw, item_id):
        # API가 이미 정리된 JSON을 주므로 그대로 쓴다.
        return json.loads(raw)


# ---------------------------------------------------------------------------
# 헤이딜러 (www.heydealer.com): 목록·상세 모두 HTML, 상세 HTML 안에 JSON이 들어 있음
# ---------------------------------------------------------------------------

class Heydealer(Crawler):
    """데이터 API(market-api.heydealer.com, api.heydealer.com)는 robots.txt가 금지하므로
    쓰지 않고, 허용된 www 화면 HTML만 받는다."""

    name = "heydealer"
    raw_ext = ".html"
    detail_url = "https://www.heydealer.com/market/cars/{}"
    first_page = 1  # 쪽 번호

    LIST_URL = "https://www.heydealer.com/market/cars"  # 한 쪽에 10건. 마지막 쪽을 넘기면 빈 목록이 온다.
    HASH_ID = re.compile(r'href="/market/cars/([A-Za-z0-9]{8})"')
    # 상세 화면은 Next.js가 그린 화면이라, 매물 데이터가 self.__next_f.push([1, "..."]) 조각들에
    # JSON 문자열로 나뉘어 들어 있다.
    FLIGHT_CHUNK = re.compile(r"<script>self\.__next_f\.push\((\[.*?\])\)</script>", re.S)
    # 같은 화면 아래쪽의 '비슷한 차' 목록이다. 이 매물의 데이터가 아니라서 뺀다.
    DROP_KEYS = ["top_car_list_group", "bottom_car_list_group"]

    def fetch_page(self, page_no):
        # 목록이 '추천순'이라 수집 도중 순서가 바뀌면 같은 매물이 두 번 나오거나 빠질 수 있다.
        # 정렬 기준을 바꾸는 값은 찾지 못했다.
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

        # 조각을 이어 붙인 글에서 {"car":{"hash_id":"<매물 번호>" 로 시작하는 객체를 꺼낸다.
        start = flight.find(f'{{"car":{{"hash_id":"{hash_id}"')
        if start < 0:
            return None
        car = json.JSONDecoder().raw_decode(flight, start)[0]["car"]
        for key in self.DROP_KEYS:
            car.pop(key, None)
        return car


# ---------------------------------------------------------------------------

SITES = {cls.name: cls for cls in (Hyundai, Kia, Heydealer)}


def main() -> None:
    parser = argparse.ArgumentParser(description="중고차 플랫폼 매물 수집")
    parser.add_argument("site", choices=[*SITES, "all"])
    parser.add_argument("--limit", type=int, help="시험용: 앞에서부터 N건만 받는다")
    # 날짜를 처음에 한 번 정해 받기와 정리에 같이 쓴다. 자정을 넘겨도 같은 폴더를 쓴다.
    parser.add_argument("--date", default=date.today().isoformat(), help="저장할 날짜 폴더 (이어받기용)")
    parser.add_argument("--parse-only", action="store_true", help="받지 않고 저장된 원본만 다시 정리한다")
    args = parser.parse_args()

    crawlers = [SITES[name](args.date) for name in (SITES if args.site == "all" else [args.site])]

    # 사이트마다 서버가 달라서 동시에 돌린다. 한 사이트 안에서는 요청을 하나씩 DELAY_SEC 간격으로 보낸다.
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
