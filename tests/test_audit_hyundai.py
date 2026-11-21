import copy
import gzip
import importlib.util
from pathlib import Path
import unittest
from tempfile import TemporaryDirectory

spec = importlib.util.spec_from_file_location("audit_hyundai", Path(__file__).resolve().parents[1] / "scripts/audit_hyundai.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class AuditTest(unittest.TestCase):
    def row(self):
        return {"source": "hyundai", "goods_no": "TEST-1", "name": "2022 테스트 차량",
                "price": "2,610만원", "basic_info": {"연식": "2023년", "주행거리": "0km", "최초등록": "2023.02.28"},
                "options": [], "accidents": [], "insurance_notes": {}}

    def test_units_and_invalid_numbers(self):
        self.assertEqual(module.quantity("2,610만원", {"만원": 10000, "원": 1}), 26100000)
        self.assertEqual(module.quantity("26,100,000원", {"만원": 10000, "원": 1}), 26100000)
        self.assertEqual(module.quantity("0km", {"km": 1}), 0)
        for value in ["26,10만원", "-10만원", "2610", 2610, "알 수 없음"]:
            self.assertIsNone(module.quantity(value, {"만원": 10000, "원": 1}))

    def test_unknown_is_not_absent_or_accident_free(self):
        report = module.audit([self.row()])
        self.assertEqual(report["option_states"], {"unknown": 1})
        self.assertEqual(report["accident_states"], {"empty_list_unknown": 1})
        self.assertTrue(module.present("0건"))
        self.assertTrue(module.present("없음"))
        self.assertFalse(module.present({"field": "-"}))

    def test_year_difference_and_real_calendar(self):
        row = self.row()
        row["basic_info"]["최초등록"] = "2023.02.29"
        result = module.audit([row])
        self.assertEqual(result["title_year_differences"], 1)
        self.assertEqual(result["invalid_counts"], {"최초등록일": 1})
        self.assertEqual(module.registration_date("2024.02.29"), "2024-02-29")

    def test_accidents_and_label_are_independent(self):
        row = self.row()
        row.update(accidents=[{"date": "2024.01.01"}], highlights={"사고진단": "단순교환무사고"},
                   insurance_notes={"정보제공 불가능 기간": "2023년01월~2023년12월"})
        report = module.audit([row])
        self.assertEqual(report["records_with_accidents_and_no_accident_label"], 1)
        self.assertEqual(report["insurance_unavailable_periods"], 1)
        row["insurance_notes"]["정보제공 불가능 기간"] = "없음"
        self.assertEqual(module.audit([row])["insurance_unavailable_periods"], 0)

    def test_duplicates_missing_ids_and_no_mutation(self):
        row = self.row()
        row["basic_info"]["차량번호"] = "SYNTHETIC-PLATE"
        missing = self.row()
        missing["goods_no"] = None
        rows = [row, copy.deepcopy(row), missing]
        before = copy.deepcopy(rows)
        report = module.audit(rows)
        self.assertEqual(report["duplicate_ids"], {"TEST-1": 2})
        self.assertEqual(report["duplicate_plate_groups"], 1)
        self.assertEqual(report["coverage"]["매물 ID"]["missing_or_empty"], 1)
        self.assertNotIn("SYNTHETIC-PLATE", str(report))
        self.assertEqual(rows, before)

    def test_empty_input_wrong_source_and_zero_price(self):
        for rows in [[], [{"source": "kia"}], [None]]:
            with self.assertRaises(ValueError):
                module.audit(rows)
        row = self.row()
        row["price"] = "0만원"
        report = module.audit([row])
        self.assertEqual(report["invalid_counts"], {"가격": 1})
        self.assertEqual(report["numeric"]["price_krw"]["valid"], 0)

    def test_raw_disabled_options_and_missing_raw(self):
        row = self.row()
        row["options"] = ["통풍시트", "선루프"]
        html = '<ol class="option_01"><li><span>통풍시트</span></li><li class="off"><span>선루프</span></li></ol>'
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "TEST-1.html.gz").write_bytes(gzip.compress(html.encode()))
            result = module.audit_raw_options([row], root)
            self.assertEqual(result["affected_records"], 1)
            self.assertEqual(result["evidence"][0]["inactive_labels_in_parsed_options"], ["선루프"])
            row["goods_no"] = "NOT-DOWNLOADED"
            result = module.audit_raw_options([row], root)
            self.assertEqual(result["checked"], 0)
            self.assertEqual(result["missing_raw_ids"], ["NOT-DOWNLOADED"])


if __name__ == "__main__":
    unittest.main()
