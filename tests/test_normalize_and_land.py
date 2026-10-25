import json
from datetime import date

from ops_agent.normalize import be_to_ce, clean_thai, hhmm, parse_ddmon, parse_dowddmon, parse_th_date
from ops_agent.parsers.land_confirmation import from_llm_json


def test_thai_dates():
    assert parse_th_date("นัดหมาย วันเสาร์ที่24 ตุลาคม 2569") == date(2026, 10, 24)
    assert parse_th_date("27 ต.ค. 69") == date(2026, 10, 27)
    assert be_to_ce(69) == 2026 and be_to_ce(2569) == 2026 and be_to_ce(2026) == 2026


def test_airline_dates():
    assert parse_ddmon("28OCT", date(2026, 10, 23)) == date(2026, 10, 28)
    assert parse_ddmon("02JAN", date(2026, 12, 20)) == date(2027, 1, 2)
    assert parse_dowddmon("WE28OCT", date(2026, 1, 1)) == date(2026, 10, 28)


def test_times():
    assert hhmm("0805") == "08:05"
    assert hhmm("19.35 น.") == "19:35"
    assert hhmm("9:5") is None


def test_clean_thai():
    assert clean_thai("ท่านสามารถ น ากระเป๋า") == "ท่านสามารถ นำกระเป๋า"
    assert clean_thai("เช\n็\nกอิน") == "เช็กอิน"


def test_land_confirmation_from_llm_json():
    raw = json.dumps({
        "land_operator": {"name": "Zhangjiajie Tian Ma", "contact_person": "เจนภพ", "phone": "+86 158-0744-4898",
                          "wechat": "38778418", "quote": "张家界天马"},
        "guides": [{"name": "ส้ม", "phone": "+86 190-7741-6205", "wechat": None, "quote": "导游"}],
        "hotels": [{"date": "2026-10-27", "hotel_name": "Rezen Kingtown", "nights": 1, "quote": "丽呈"}],
        "optional_tours": [{"name": "两江游", "price": 300, "currency": "CNY", "quote": "300"},
                           {"name": "两江游", "price": 380, "currency": "CNY", "quote": "380"}],
        "pax_total": 13,
    })
    doc = from_llm_json("Here is the JSON:\n" + raw, "land.docx")
    assert doc.land_operator.wechat == "38778418"
    assert doc.guides[0].phone == "+86 190-7741-6205"
    assert [o.price for o in doc.optional_tours] == [300, 380]
    assert doc.hotels[0].date == date(2026, 10, 27)
    assert doc.optional_tours[1].source.raw == "380"
