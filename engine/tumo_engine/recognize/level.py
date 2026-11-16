import re
from unicodedata import normalize

from tumo_engine.preprocess import layer_key

NUMBERS = "零一二三四五六七八九"
FLOOR = r"(?:B\d+F?|\d+F?|[一二三四五六七八九十]+)"
RANGE = re.compile(rf"(?:^|[^\w]|楼)\s*({FLOOR})\s*[-~～—–至]\s*({FLOOR})\s*层")
SINGLE = re.compile(rf"({FLOOR})\s*层\s*(?:建筑|结构)?平面图")


def _number(text):
    text = text.upper().removesuffix("F")
    if text.startswith("B") and text[1:].isdigit():
        return -int(text[1:])
    if text.isdigit():
        return int(text)
    if text in NUMBERS:
        return NUMBERS.index(text)
    if "十" in text:
        tens, ones = text.split("十", 1)
        return (NUMBERS.index(tens) if tens else 1) * 10 + (NUMBERS.index(ones) if ones else 0)
    return None


def floor_from_title(title):
    if "首层" in title:
        return 1
    match = RANGE.search(title) or SINGLE.search(title)
    return _number(match[1]) if match else None


def _elevation(text):
    value = text.strip().upper()
    if not re.fullmatch(r"[±+-]?(?:\d+(?:\.\d+)?|\.\d+)\s*(?:MM|M)?", value):
        return None
    unit = 1 if value.endswith("MM") else 1000 if "." in value or value.endswith("M") else 1
    return round(float(re.sub(r"MM?$", "", value).lstrip("±")) * unit, 3)


def recognize_levels(items, title, discipline, rules):
    table_layer = rules["layers"]["level_table"]
    cfg = rules["level"]
    table = [
        (item.points[0], normalize("NFKC", item.text).strip())
        for item in items if item.kind == "text" and re.search(table_layer, layer_key(item.layer))
    ]
    if not table:
        table = [(item.points[0], normalize("NFKC", item.text).strip()) for item in items if item.kind == "text"]
    headers = [
        (at, "elevation_struct" if "结构" in text or ("标高" in text and "建筑" not in text and discipline == "struct") else "elevation_arch")
        for at, text in table if "标高" in text and len(text) <= 12
    ]
    levels_by_number = {}
    for at, text in table:
        if not re.fullmatch(r"B?\d{1,2}F|[一二三四五六七八九十]+层", text, re.I):
            continue
        number = _number(text.removesuffix("层"))
        if number is None:
            continue
        row = [
            (pos, elevation)
            for pos, cell in table
            if pos[0] > at[0] and abs(pos[1] - at[1]) <= cfg["row_tolerance"]
            and (elevation := _elevation(cell)) is not None
        ]
        values = {}
        for header_pos, key in headers:
            if abs(header_pos[0] - at[0]) > 12000:
                continue
            options = [(abs(pos[0] - header_pos[0]), val) for pos, val in row if abs(pos[0] - header_pos[0]) < 650]
            if options:
                values[key] = min(options)[1]
        if values:
            levels_by_number[number] = {
                "id": _id(number), "name": text, **values, "source": "楼层表", "confidence": 0.90,
            }

    number = floor_from_title(title)
    if number is None:
        positive = [key for key in levels_by_number if key > 0]
        number = min(positive) if positive else 1
    warnings = []
    if number not in levels_by_number:
        levels_by_number[number] = {
            "id": _id(number), "name": _name(number),
            "elevation_arch": (number - 1) * cfg["default_height"], "source": "默认楼层",
            "confidence": 0.5,
        }
        warnings.append("没有找到当前层标高，暂用默认楼层标高。请核对楼层表。")
    if number + 1 not in levels_by_number:
        base = levels_by_number[number]
        elevations = {
            key: base[key] + cfg["default_height"]
            for key in ("elevation_arch", "elevation_struct") if key in base
        }
        levels_by_number[number + 1] = {
            "id": _id(number + 1), "name": _name(number + 1), **elevations,
            "source": "默认层高", "confidence": 0.5,
        }
        warnings.append(f"没有找到上一层标高，暂按 {cfg['default_height']} mm 层高生成墙和柱。")
    used = [levels_by_number[value] for value in (number, number + 1)]
    return used, warnings


def _id(number):
    return f"L{number}" if number >= 0 else f"LB{-number}"


def _name(number):
    return f"{number}F" if number >= 0 else f"B{-number}F"
