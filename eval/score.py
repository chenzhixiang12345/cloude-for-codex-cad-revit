"""Score reviewed TuMo models in millimeters."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict, deque
from math import dist
from pathlib import Path
from time import perf_counter


def _objects(model):
    categories = defaultdict(list)
    for category in ("grid", "level"):
        categories[category].extend(model.get(category + "s", []))
    for element in model.get("elements", []):
        categories[element["category"]].append(element)
    return categories


def _geometry(item):
    if "geometry" in item:
        return item["geometry"]
    if "start" in item and "end" in item:
        return {"kind": "line", "start": item["start"], "end": item["end"]}
    elevation = item.get("elevation_arch")
    if elevation is None:
        elevation = item.get("elevation_struct")
    return {"kind": "level", "elevation": elevation}


def _distance(left, right):
    left_level, right_level = left.get("base_level"), right.get("base_level")
    if left_level and right_level and left_level != right_level:
        return float("inf")
    left_sheet = (left.get("source") or {}).get("sheet")
    right_sheet = (right.get("source") or {}).get("sheet")
    if left_sheet and right_sheet and left_sheet != right_sheet:
        return float("inf")
    a, b = _geometry(left), _geometry(right)
    if a["kind"] != b["kind"]:
        return float("inf")
    if a["kind"] == "line":
        a0, a1, b0, b1 = a["start"], a["end"], b["start"], b["end"]
        direction = sum((u - v) * (s - t) for u, v, s, t in zip(a1, a0, b1, b0))
        lengths = dist(a0, a1) * dist(b0, b1)
        if not lengths or abs(direction) / lengths < 0.98:
            return float("inf")
        if direction < 0:
            b0, b1 = b1, b0
        return max(dist(a0, b0), dist(a1, b1))
    if a["kind"] == "point":
        return dist(a["at"], b["at"])
    if a["kind"] == "level":
        if a["elevation"] is None or b["elevation"] is None:
            return float("inf")
        return abs(a["elevation"] - b["elevation"])
    if a["kind"] == "polygon":
        from shapely.geometry import Polygon

        a_center, b_center = (Polygon(g["points"]).centroid for g in (a, b))
        return a_center.distance(b_center)
    return float("inf")


def _matching(predictions, expected, tolerance):
    edges = []
    for prediction in predictions:
        candidates = [(_distance(prediction, item), index) for index, item in enumerate(expected)]
        edges.append([index for distance, index in sorted(candidates) if distance <= tolerance])

    assigned = {}
    for root in range(len(predictions)):
        pending = deque([root])
        path = {}
        visited = {root}
        while pending:
            prediction = pending.popleft()
            for target in edges[prediction]:
                if target in path:
                    continue
                path[target] = prediction
                if target not in assigned:
                    break
                previous = assigned[target]
                if previous not in visited:
                    pending.append(previous)
                    visited.add(previous)
            else:
                continue
            previous_targets = {owner: index for index, owner in assigned.items() if index in path}
            while target is not None:
                owner = path[target]
                assigned[target] = owner
                target = previous_targets.get(owner)
            break
    return assigned


def _attributes(item):
    attributes = dict(item.get("type") or {})
    for key in ("label", "name", "elevation_struct", "base_level", "top_level", "base_offset", "top_offset", "sill_height"):
        if item.get(key) is not None:
            attributes[key] = item[key]
    return {key: value for key, value in attributes.items() if value is not None}


def compare(actual, truth, tolerance=50):
    predicted, expected = _objects(actual), _objects(truth)
    result = {}
    for category in sorted((predicted.keys() | expected.keys())):
        left, right = predicted[category], expected[category]
        if not (left or right):
            continue
        matches = _matching(left, right, tolerance)
        correct = total = 0
        for target, prediction in matches.items():
            attributes = _attributes(left[prediction])
            for key, value in _attributes(right[target]).items():
                total += 1
                correct += attributes.get(key) == value
        result[category] = {
            "predicted": len(left),
            "expected": len(right),
            "matched": len(matches),
            "precision": len(matches) / len(left) if left else None,
            "recall": len(matches) / len(right) if right else None,
            "attribute_accuracy": correct / total if total else None,
        }
    return result


def recall_regressions(current, baseline):
    return [
        category
        for category, metrics in baseline.items()
        if metrics["recall"] is not None
        and metrics["recall"] - (current.get(category, {}).get("recall") or 0) > .0100000001
    ]


def _read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def main(argv=None):
    parser = argparse.ArgumentParser(description="比较识别结果和已确认的本地标准答案。")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--all", action="store_true", help="评估 samples 下所有样例，默认执行。")
    mode.add_argument("--sample", type=Path, help="评估指定样例目录。")
    mode.add_argument("--smoke", action="store_true", help="使用空模型检查评分流程。")
    parser.add_argument("--tolerance", type=float, default=50, help="匹配容差，单位毫米。")
    parser.add_argument("--output", type=Path, default=Path("eval/reports/latest.json"))
    parser.add_argument("--baseline", type=Path, help="上一份评分报告，召回率下降超过 1 个百分点时失败。")
    args = parser.parse_args(argv)
    if not 0 < args.tolerance < float("inf"):
        parser.error("匹配容差必须是有限的正数。")

    samples = [args.sample] if args.sample else sorted(p for p in Path("samples").glob("*") if p.is_dir())
    baseline = _read(args.baseline) if args.baseline else {}
    results = {}
    if args.smoke:
        results["空模型"] = {"status": "smoke", "categories": compare({}, {}, args.tolerance)}
    for sample in ([] if args.smoke else samples):
        truth_path = sample / "ground_truth" / "model.json"
        if not truth_path.is_file():
            print(f"{sample.name}：没有已确认的标准答案。")
            results[sample.name] = {"status": "no_ground_truth"}
            continue
        from tumo_engine.pipeline import run

        expected = _read(truth_path)
        started = perf_counter()
        model = run(sample)
        elapsed = perf_counter() - started
        metrics = compare(model, expected, args.tolerance)
        results[sample.name] = {"status": "scored", "seconds": elapsed, "categories": metrics}
        print(f"{sample.name}：识别耗时 {elapsed:.2f} 秒")
        print("类别 | 查准率 | 召回率 | 属性正确率")
        for category, values in metrics.items():
            formatted = ["—" if values[key] is None else f"{values[key]:.1%}"
                         for key in ("precision", "recall", "attribute_accuracy")]
            print(" | ".join([category, *formatted]))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"评分报告：{args.output}")
    if baseline:
        regressions = []
        for name, previous in baseline.items():
            current = results.get(name, {})
            regressions.extend(
                f"{name}/{category}"
                for category in recall_regressions(current.get("categories", {}), previous.get("categories", {}))
            )
        if regressions:
            print("召回率下降超过 1 个百分点：" + "、".join(regressions))
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
