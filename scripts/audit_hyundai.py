"""현대 파싱 결과의 누락·단위·중복·해석 주의 항목을 집계한다.

uv run scripts/audit_hyundai.py --input data/processed/hyundai/2026-11-21.jsonl \
    --output data/analysis/hyundai/2026-11-21-quality.json

빈 옵션/사고 목록은 미장착/무사고로 판정하지 않는다.
가격은 원, 주행거리는 km로 변환한다. 원본은 수정하지 않는다.
"""

import argparse
from collections import Counter
from datetime import date
from decimal import Decimal
import hashlib
import gzip
import json
from pathlib import Path
import re
from statistics import median


FIELDS = {
    "매물 ID": "goods_no", "차량명": "name", "가격": "price",
    "연식": "basic_info.연식", "최초등록일": "basic_info.최초등록",
    "주행거리": "basic_info.주행거리", "제조사": "model_info.brand_type",
    "모델": "model_info.vehicle_model", "트림": "model_info.vehicle_trim",
    "차종": "basic_info.차종", "연료": "basic_info.연료",
    "변속기": "basic_info.변속기", "옵션 목록": "options",
    "선택옵션 목록": "selected_options", "사고진단 표시": "highlights.사고진단",
    "보험 요약": "insurance_summary", "보험 정보제공 제한 안내": "insurance_notes",
    "판매상태 코드": "sale_status_code",
}
NUMBER = r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?"


def present(value):
    """0·없음·0건은 정보가 존재하는 값이며, 빈 자료와 구별한다."""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in {"", "-", "—", "n/a", "null", "미확인"}
    if isinstance(value, dict):
        return any(present(v) for v in value.values())
    if isinstance(value, list):
        return any(present(v) for v in value)
    return True


def get(row, path):
    value = row
    for key in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def quantity(value, units):
    """명시된 단위와 정상 숫자 형식만 변환한다. 음수·소수 결과는 거부한다."""
    if not isinstance(value, str):
        return None
    for unit, multiplier in units.items():
        match = re.fullmatch(rf"({NUMBER})\s*{re.escape(unit)}", value.strip())
        if match:
            result = Decimal(match[1].replace(",", "")) * multiplier
            return int(result) if result == result.to_integral_value() else None
    return None


def integer_year(value):
    match = re.fullmatch(r"([0-9]{4})년", str(value).strip())
    return int(match[1]) if match else None


def registration_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}\.[0-9]{2}\.[0-9]{2}", value):
        return None
    try:
        return date.fromisoformat(value.replace(".", "-")).isoformat()
    except ValueError:
        return None


def distribution(rows, path):
    return dict(sorted(Counter(str(get(r, path)) if present(get(r, path)) else "미확인" for r in rows).items()))


def audit(rows):
    if not rows:
        raise ValueError("분석 대상이 0건이다.")
    if any(not isinstance(r, dict) or r.get("source") != "hyundai" for r in rows):
        raise ValueError("source=hyundai인 JSON 객체만 지원한다.")
    numeric = {"price_krw": [], "mileage_km": [], "year": []}
    details = []
    for index, row in enumerate(rows, 1):
        price = quantity(row.get("price"), {"만원": 10000, "원": 1})
        mileage = quantity(get(row, "basic_info.주행거리"), {"km": 1})
        year = integer_year(get(row, "basic_info.연식"))
        registered = registration_date(get(row, "basic_info.최초등록"))
        missing = [label for label, path in FIELDS.items() if not present(get(row, path))]
        invalid = []
        for label, value, path in [("가격", price, "price"), ("주행거리", mileage, "basic_info.주행거리"),
                                   ("연식", year, "basic_info.연식"), ("최초등록일", registered, "basic_info.최초등록")]:
            if present(get(row, path)) and (value is None or (label == "가격" and value <= 0)):
                invalid.append(label)
        title_year = re.match(r"^([0-9]{4})\s", row.get("name") or "")
        title_differs = bool(title_year and year and int(title_year[1]) != year)
        accident_list = row.get("accidents")
        accident_state = ("records_present" if accident_list else "empty_list_unknown") if isinstance(accident_list, list) else "missing_or_invalid"
        option_state = "labels_present" if present(row.get("options")) else "unknown"
        unavailable = get(row, "insurance_notes.정보제공 불가능 기간")
        gap = present(unavailable) and str(unavailable).strip() not in {"없음", "해당없음", "해당 없음"}
        for key, value in [("price_krw", price), ("mileage_km", mileage), ("year", year)]:
            if value is not None and not (key == "price_krw" and value <= 0):
                numeric[key].append(value)
        details.append({"row": index, "goods_no": row.get("goods_no"), "missing_or_empty": missing,
                        "invalid_format_or_value": invalid, "price_krw": price, "mileage_km": mileage,
                        "year": year, "registration_date": registered, "title_year_differs": title_differs,
                        "options_state": option_state, "accidents_state": accident_state,
                        "insurance_unavailable_period": bool(gap)})
    ids = Counter(str(r["goods_no"]) for r in rows if present(r.get("goods_no")))
    # 차량번호는 중복 건수 계산에만 사용하며 값을 출력하지 않는다.
    plates = Counter(str(get(r, "basic_info.차량번호")) for r in rows if present(get(r, "basic_info.차량번호")))
    return {
        "audit_version": 1, "records": len(rows),
        "coverage": {label: {"present": sum(present(get(r, path)) for r in rows),
                              "missing_or_empty": sum(not present(get(r, path)) for r in rows)}
                     for label, path in FIELDS.items()},
        "invalid_counts": dict(Counter(x for r in details for x in r["invalid_format_or_value"])),
        "duplicate_ids": {k: n for k, n in sorted(ids.items()) if n > 1},
        "duplicate_plate_groups": sum(n > 1 for n in plates.values()),
        "plate_missing": sum(not present(get(r, "basic_info.차량번호")) for r in rows),
        "numeric": {key: {"valid": len(values), "min": min(values) if values else None,
                          "median": median(values) if values else None, "max": max(values) if values else None}
                    for key, values in numeric.items()},
        "distributions": {path: distribution(rows, path) for path in
                          ["model_info.brand_type", "model_info.vehicle_model", "basic_info.차종",
                           "basic_info.연료", "highlights.사고진단", "sale_status_code"]},
        "title_year_differences": sum(r["title_year_differs"] for r in details),
        "insurance_unavailable_periods": sum(r["insurance_unavailable_period"] for r in details),
        "accident_states": dict(Counter(r["accidents_state"] for r in details)),
        "option_states": dict(Counter(r["options_state"] for r in details)),
        "records_with_accidents_and_no_accident_label": sum(
            bool(isinstance(r.get("accidents"), list) and r["accidents"])
            and "무사고" in str(get(r, "highlights.사고진단") or "") for r in rows),
        "rows": details,
    }


def audit_raw_options(rows, raw_dir):
    """저장된 HTML의 off 표시가 파싱 결과에 포함되는지 대조한다."""
    from bs4 import BeautifulSoup

    evidence = []
    missing = []
    for row in rows:
        goods_no = row.get("goods_no")
        if not isinstance(goods_no, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", goods_no):
            raise ValueError("원본 대조에 사용할 매물 ID 형식이 올바르지 않다.")
        path = raw_dir / f"{goods_no}.html.gz"
        if not path.exists():
            missing.append(goods_no)
            continue
        content = path.read_bytes()
        soup = BeautifulSoup(gzip.decompress(content).decode("utf-8"), "html.parser")
        inactive = [node.get_text(" ", strip=True) for node in soup.select(".option_01 > li.off > span")]
        parsed = row.get("options") or []
        evidence.append({"goods_no": goods_no, "raw_sha256": hashlib.sha256(content).hexdigest(),
                         "option_section_present": soup.select_one(".option_01") is not None,
                         "inactive_labels": inactive,
                         "inactive_labels_in_parsed_options": [label for label in inactive if label in parsed]})
    return {"checked": len(evidence), "missing_raw_ids": missing,
            "missing_option_sections": sum(not r["option_section_present"] for r in evidence),
            "affected_records": sum(bool(r["inactive_labels_in_parsed_options"]) for r in evidence),
            "inactive_label_occurrences": sum(len(r["inactive_labels_in_parsed_options"]) for r in evidence),
            "evidence": evidence}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--raw-dir", type=Path, help="원본 detail/*.html.gz 폴더; 생략 시 옵션 원본 대조 미수행")
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error("입력과 출력 경로가 같다.")
    raw = args.input.read_bytes()
    rows = []
    for line_no, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as error:
                parser.error(f"입력 {line_no}행 JSON 오류: {error.msg}")
    try:
        report = audit(rows)
        report["raw_option_check"] = audit_raw_options(rows, args.raw_dir) if args.raw_dir else None
    except ValueError as error:
        parser.error(str(error))
    report["input"] = {"file": args.input.as_posix(), "sha256": hashlib.sha256(raw).hexdigest(),
                       "crawled_dates": sorted({str(r.get("crawled_date")) for r in rows})}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(rows)}건 분석 → {args.output}")


if __name__ == "__main__":
    main()
