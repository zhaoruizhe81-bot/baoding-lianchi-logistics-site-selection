#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
使用多目标遗传算法完成“原始选址方案 vs 优化选址方案”的建模、求解与对比。

模型目标：
1. 最小化地块开发成本代理指标（以候选地块面积作为成本代理，面积越小成本越低）。
2. 最小化道路可达距离代理指标（综合考虑到高速和到二级以上公路的距离）。
3. 最大化综合适宜性（通过最小化适宜性损失实现）。

说明：
- 当前工程没有真实地价、建设费用和订单需求数据，因此“成本最小”采用代理建模。
- 原始方案对比以“已有备选点”为基准；若已有备选点数量多于目标站点数，则按适宜性优先、
  道路距离次优的规则截取前 N 个用于公平对比。
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import math
import random
from pathlib import Path

import arcpy

from genetic_site_selection import (
    DEFAULTS as GA_DEFAULTS,
    build_candidate_polygon,
    build_candidate_pool,
    build_output_path,
    delete_if_exists,
    ensure_field,
    evaluate_candidates,
    message,
    normalize,
    require_exists,
    select_features_by_candidate_ids,
    separation_penalty,
)


DEFAULTS = {
    **GA_DEFAULTS,
    "comparison_csv": "multiobjective_scheme_comparison.csv",
    "comparison_md": "multiobjective_summary.md",
}


def safe_float(value) -> float | None:
    if value in (None, -1, -9999):
        return None
    return float(value)


def existing_field_name(feature_class: str, field_candidates: list[str]) -> str | None:
    fields = {field.name.upper(): field.name for field in arcpy.ListFields(feature_class)}
    for candidate in field_candidates:
        match = fields.get(candidate.upper())
        if match:
            return match
    return None


def load_candidate_records(evaluated_fc: str) -> list[dict]:
    name_field = existing_field_name(evaluated_fc, ["名称", "name"])
    area_field = existing_field_name(evaluated_fc, ["area_m2_1", "area_m2"])
    fields = ["ga_cand_id", "SHAPE@", "suit_val"]
    if area_field:
        fields.append(area_field)
    fields.extend(["dist_gs_m", "dist_gl_m"])
    if name_field:
        fields.append(name_field)

    records: list[dict] = []
    with arcpy.da.SearchCursor(evaluated_fc, fields) as cursor:
        for row in cursor:
            if area_field and name_field:
                ga_cand_id, geometry, suit_val, area_m2, dist_gs_m, dist_gl_m, source_name = row
            elif area_field:
                ga_cand_id, geometry, suit_val, area_m2, dist_gs_m, dist_gl_m = row
                source_name = None
            elif name_field:
                ga_cand_id, geometry, suit_val, dist_gs_m, dist_gl_m, source_name = row
                area_m2 = None
            else:
                ga_cand_id, geometry, suit_val, dist_gs_m, dist_gl_m = row
                area_m2 = None
                source_name = None
            if geometry is None:
                continue
            records.append(
                {
                    "ga_cand_id": int(ga_cand_id),
                    "geometry": geometry,
                    "suit_val": safe_float(suit_val),
                    "area_m2": safe_float(area_m2),
                    "dist_gs_m": safe_float(dist_gs_m),
                    "dist_gl_m": safe_float(dist_gl_m),
                    "source_name": source_name,
                }
            )

    if not records:
        raise RuntimeError("候选点评价结果为空，无法执行多目标优化。")

    suit_scores = normalize([r["suit_val"] for r in records])
    area_cost_scores = normalize([r["area_m2"] for r in records])
    dist_gs_scores = normalize([r["dist_gs_m"] for r in records])
    dist_gl_scores = normalize([r["dist_gl_m"] for r in records])

    for idx, record in enumerate(records):
        access_distance = 0.55 * dist_gs_scores[idx] + 0.45 * dist_gl_scores[idx]
        point_compromise = (
            GA_DEFAULTS["weight_area"] * area_cost_scores[idx]
            + (GA_DEFAULTS["weight_expressway"] + GA_DEFAULTS["weight_major_road"]) * access_distance
            + GA_DEFAULTS["weight_suitability"] * (1.0 - suit_scores[idx])
        )
        record["score_suit"] = suit_scores[idx]
        record["score_area_cost"] = area_cost_scores[idx]
        record["score_dist_gs"] = dist_gs_scores[idx]
        record["score_dist_gl"] = dist_gl_scores[idx]
        record["score_access_distance"] = access_distance
        record["point_score"] = 1.0 - point_compromise

    return records


def initial_population(candidate_count: int, site_count: int, population_size: int) -> list[list[int]]:
    base = list(range(candidate_count))
    return [sorted(random.sample(base, site_count)) for _ in range(population_size)]


def crossover(parent_a: list[int], parent_b: list[int], candidate_count: int, site_count: int) -> list[int]:
    merged = list(dict.fromkeys(parent_a + parent_b))
    child: list[int] = []
    while merged and len(child) < site_count:
        child.append(merged.pop(random.randrange(len(merged))))

    available = [idx for idx in range(candidate_count) if idx not in child]
    while len(child) < site_count and available:
        child.append(available.pop(random.randrange(len(available))))
    return sorted(child)


def mutate(chromosome: list[int], candidate_count: int, mutation_rate: float) -> list[int]:
    updated = list(chromosome)
    if random.random() >= mutation_rate:
        return updated
    replace_pos = random.randrange(len(updated))
    available = [idx for idx in range(candidate_count) if idx not in updated]
    if not available:
        return updated
    updated[replace_pos] = random.choice(available)
    return sorted(updated)


def objective_vector(
    chromosome: list[int],
    records: list[dict],
    min_site_distance: float,
    distance_penalty_weight: float,
) -> tuple[float, float, float]:
    selected = [records[index] for index in chromosome]
    if not selected:
        return (1.0, 1.0, 1.0)

    cost_proxy = sum(item["score_area_cost"] for item in selected) / len(selected)
    distance_proxy = sum(item["score_access_distance"] for item in selected) / len(selected)
    suitability_loss = 1.0 - (sum(item["score_suit"] for item in selected) / len(selected))

    penalty = separation_penalty(chromosome, records, min_site_distance) * distance_penalty_weight
    return (
        cost_proxy + penalty,
        distance_proxy + penalty,
        suitability_loss + penalty,
    )


def dominates(left: tuple[float, float, float], right: tuple[float, float, float]) -> bool:
    return all(l <= r for l, r in zip(left, right)) and any(l < r for l, r in zip(left, right))


def non_dominated_sort(population: list[list[int]], objectives: dict[tuple[int, ...], tuple[float, float, float]]) -> list[list[list[int]]]:
    domination_count: dict[tuple[int, ...], int] = {}
    dominated: dict[tuple[int, ...], list[tuple[int, ...]]] = {}
    fronts: list[list[tuple[int, ...]]] = [[]]

    chromosomes = [tuple(chromosome) for chromosome in population]
    for chromosome in chromosomes:
        domination_count[chromosome] = 0
        dominated[chromosome] = []
        for other in chromosomes:
            if chromosome == other:
                continue
            if dominates(objectives[chromosome], objectives[other]):
                dominated[chromosome].append(other)
            elif dominates(objectives[other], objectives[chromosome]):
                domination_count[chromosome] += 1
        if domination_count[chromosome] == 0:
            fronts[0].append(chromosome)

    front_index = 0
    while front_index < len(fronts) and fronts[front_index]:
        next_front: list[tuple[int, ...]] = []
        for chromosome in fronts[front_index]:
            for other in dominated[chromosome]:
                domination_count[other] -= 1
                if domination_count[other] == 0:
                    next_front.append(other)
        if next_front:
            fronts.append(next_front)
        front_index += 1

    return [[list(chromosome) for chromosome in front] for front in fronts if front]


def crowding_distance(front: list[list[int]], objectives: dict[tuple[int, ...], tuple[float, float, float]]) -> dict[tuple[int, ...], float]:
    if not front:
        return {}
    if len(front) <= 2:
        return {tuple(chromosome): float("inf") for chromosome in front}

    distance = {tuple(chromosome): 0.0 for chromosome in front}
    objective_count = len(objectives[tuple(front[0])])

    for objective_index in range(objective_count):
        ordered = sorted(front, key=lambda chromosome: objectives[tuple(chromosome)][objective_index])
        low = objectives[tuple(ordered[0])][objective_index]
        high = objectives[tuple(ordered[-1])][objective_index]
        distance[tuple(ordered[0])] = float("inf")
        distance[tuple(ordered[-1])] = float("inf")
        if abs(high - low) < 1e-12:
            continue
        for idx in range(1, len(ordered) - 1):
            left = objectives[tuple(ordered[idx - 1])][objective_index]
            right = objectives[tuple(ordered[idx + 1])][objective_index]
            distance[tuple(ordered[idx])] += (right - left) / (high - low)

    return distance


def rank_population(population: list[list[int]], objectives: dict[tuple[int, ...], tuple[float, float, float]]) -> tuple[dict[tuple[int, ...], int], dict[tuple[int, ...], float], list[list[list[int]]]]:
    fronts = non_dominated_sort(population, objectives)
    rank: dict[tuple[int, ...], int] = {}
    crowding: dict[tuple[int, ...], float] = {}
    for front_index, front in enumerate(fronts):
        crowding.update(crowding_distance(front, objectives))
        for chromosome in front:
            rank[tuple(chromosome)] = front_index
    return rank, crowding, fronts


def tournament_select(
    population: list[list[int]],
    rank: dict[tuple[int, ...], int],
    crowding: dict[tuple[int, ...], float],
    tournament_size: int,
) -> list[int]:
    sampled = random.sample(population, min(tournament_size, len(population)))
    sampled.sort(
        key=lambda chromosome: (
            rank[tuple(chromosome)],
            -crowding.get(tuple(chromosome), 0.0),
        )
    )
    return list(sampled[0])


def select_compromise_solution(
    pareto_front: list[list[int]],
    objectives: dict[tuple[int, ...], tuple[float, float, float]],
    preference_weights: tuple[float, float, float],
) -> tuple[list[int], float]:
    if not pareto_front:
        raise RuntimeError("Pareto 前沿为空，无法选择折中方案。")

    objective_values = [objectives[tuple(chromosome)] for chromosome in pareto_front]
    mins = [min(values[idx] for values in objective_values) for idx in range(3)]
    maxs = [max(values[idx] for values in objective_values) for idx in range(3)]

    best_solution = pareto_front[0]
    best_score = float("inf")
    for chromosome in pareto_front:
        values = objectives[tuple(chromosome)]
        normalized = []
        for idx, value in enumerate(values):
            if abs(maxs[idx] - mins[idx]) < 1e-12:
                normalized.append(0.0)
            else:
                normalized.append((value - mins[idx]) / (maxs[idx] - mins[idx]))
        score = sum(weight * value for weight, value in zip(preference_weights, normalized))
        if score < best_score:
            best_solution = chromosome
            best_score = score
    return list(best_solution), best_score


def run_multiobjective_genetic_algorithm(
    records: list[dict],
    site_count: int,
    population_size: int,
    generations: int,
    mutation_rate: float,
    tournament_size: int,
    min_site_distance: float,
    distance_penalty_weight: float,
    preference_weights: tuple[float, float, float],
) -> tuple[list[int], float, list[list[int]], dict[tuple[int, ...], tuple[float, float, float]]]:
    candidate_count = len(records)
    population = initial_population(candidate_count, site_count, population_size)
    objective_cache = {
        tuple(chromosome): objective_vector(chromosome, records, min_site_distance, distance_penalty_weight)
        for chromosome in population
    }

    best_solution: list[int] | None = None
    best_compromise = float("inf")
    last_fronts: list[list[list[int]]] = []

    for generation in range(1, generations + 1):
        objective_cache.update(
            {
                tuple(chromosome): objective_vector(chromosome, records, min_site_distance, distance_penalty_weight)
                for chromosome in population
                if tuple(chromosome) not in objective_cache
            }
        )
        rank, crowding, fronts = rank_population(population, objective_cache)
        for chromosome in population:
            key = tuple(chromosome)
            rank.setdefault(key, len(fronts))
            crowding.setdefault(key, 0.0)
        last_fronts = fronts

        if fronts:
            generation_best, generation_score = select_compromise_solution(
                fronts[0],
                objective_cache,
                preference_weights,
            )
            if generation_score < best_compromise:
                best_solution = list(generation_best)
                best_compromise = generation_score

        next_population: list[list[int]] = []
        for front in fronts:
            if len(next_population) + len(front) <= population_size:
                next_population.extend([list(chromosome) for chromosome in front])
                continue

            front_crowding = crowding_distance(front, objective_cache)
            ordered_front = sorted(
                front,
                key=lambda chromosome: front_crowding.get(tuple(chromosome), 0.0),
                reverse=True,
            )
            remaining = population_size - len(next_population)
            next_population.extend([list(chromosome) for chromosome in ordered_front[:remaining]])
            break

        while len(next_population) < population_size:
            parent_a = tournament_select(population, rank, crowding, tournament_size)
            parent_b = tournament_select(population, rank, crowding, tournament_size)
            child = crossover(parent_a, parent_b, candidate_count, site_count)
            child = mutate(child, candidate_count, mutation_rate)
            next_population.append(child)

        population = next_population

        if generation == 1 or generation == generations or generation % 10 == 0:
            message(
                f"第 {generation} 代完成，Pareto 前沿解数量: {len(fronts[0]) if fronts else 0}，当前折中得分: {best_compromise:.4f}"
            )

    if best_solution is None:
        raise RuntimeError("多目标优化未找到有效解。")

    return best_solution, best_compromise, last_fronts[0], objective_cache


def choose_original_scheme(records: list[dict], site_count: int) -> list[dict]:
    existing = [record for record in records if record.get("source_name")]
    if not existing:
        return []

    existing.sort(
        key=lambda item: (
            -(item["score_suit"]),
            item["score_access_distance"],
            item["score_area_cost"],
        )
    )
    if len(existing) <= site_count:
        return existing
    return existing[:site_count]


def annotate_selected_features(output_fc: str, selected_records: list[dict], scheme_score: float) -> None:
    ensure_field(output_fc, "ga_rank", "LONG")
    ensure_field(output_fc, "ga_score", "DOUBLE")
    ensure_field(output_fc, "ga_fit", "DOUBLE")

    rank_map = {
        record["ga_cand_id"]: (rank, record["point_score"])
        for rank, record in enumerate(selected_records, start=1)
    }
    with arcpy.da.UpdateCursor(output_fc, ["ga_cand_id", "ga_rank", "ga_score", "ga_fit"]) as cursor:
        for ga_cand_id, _, _, _ in cursor:
            rank, point_score = rank_map[int(ga_cand_id)]
            cursor.updateRow([ga_cand_id, rank, point_score, 1.0 - scheme_score])


def scheme_metrics(selected_records: list[dict]) -> dict[str, float | int | str]:
    if not selected_records:
        return {
            "site_count": 0,
            "avg_suit_val": 0.0,
            "avg_area_m2": 0.0,
            "avg_dist_gs_m": 0.0,
            "avg_dist_gl_m": 0.0,
            "avg_cost_proxy": 0.0,
            "avg_distance_proxy": 0.0,
            "avg_suitability_score": 0.0,
            "site_names": "",
        }

    count = len(selected_records)
    return {
        "site_count": count,
        "avg_suit_val": sum(record["suit_val"] or 0.0 for record in selected_records) / count,
        "avg_area_m2": sum(record["area_m2"] or 0.0 for record in selected_records) / count,
        "avg_dist_gs_m": sum(record["dist_gs_m"] or 0.0 for record in selected_records) / count,
        "avg_dist_gl_m": sum(record["dist_gl_m"] or 0.0 for record in selected_records) / count,
        "avg_cost_proxy": sum(record["score_area_cost"] for record in selected_records) / count,
        "avg_distance_proxy": sum(record["score_access_distance"] for record in selected_records) / count,
        "avg_suitability_score": sum(record["score_suit"] for record in selected_records) / count,
        "site_names": "、".join(
            str(record["source_name"] or f"候选点{record['ga_cand_id']}") for record in selected_records
        ),
    }


def percent_change(new_value: float, old_value: float) -> str:
    if abs(old_value) < 1e-12:
        return "N/A"
    delta = ((new_value - old_value) / old_value) * 100.0
    return f"{delta:+.2f}%"


def write_comparison_csv(output_path: Path, original_metrics: dict, optimized_metrics: dict) -> None:
    fieldnames = [
        "scheme",
        "site_count",
        "avg_suit_val",
        "avg_area_m2",
        "avg_dist_gs_m",
        "avg_dist_gl_m",
        "avg_cost_proxy",
        "avg_distance_proxy",
        "avg_suitability_score",
        "site_names",
    ]
    rows = [
        {"scheme": "原始方案", **original_metrics},
        {"scheme": "多目标优化方案", **optimized_metrics},
    ]
    with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_comparison_summary(
    output_path: Path,
    original_metrics: dict,
    optimized_metrics: dict,
    compromise_score: float,
    pareto_count: int,
    assumption_note: str,
) -> None:
    lines = [
        "# 多目标优化选址结果摘要",
        "",
        f"- 导出时间：{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Pareto 前沿解数量：{pareto_count}",
        f"- 最终折中方案得分：{1.0 - compromise_score:.6f}",
        f"- 成本建模说明：{assumption_note}",
        "",
        "## 原始方案",
        "",
        f"- 站点数量：{original_metrics['site_count']}",
        f"- 站点组成：{original_metrics['site_names']}",
        f"- 平均综合评价值：{original_metrics['avg_suit_val']}",
        f"- 平均面积：{original_metrics['avg_area_m2']}",
        f"- 平均高速距离：{original_metrics['avg_dist_gs_m']}",
        f"- 平均主干路距离：{original_metrics['avg_dist_gl_m']}",
        "",
        "## 多目标优化方案",
        "",
        f"- 站点数量：{optimized_metrics['site_count']}",
        f"- 站点组成：{optimized_metrics['site_names']}",
        f"- 平均综合评价值：{optimized_metrics['avg_suit_val']}",
        f"- 平均面积：{optimized_metrics['avg_area_m2']}",
        f"- 平均高速距离：{optimized_metrics['avg_dist_gs_m']}",
        f"- 平均主干路距离：{optimized_metrics['avg_dist_gl_m']}",
        "",
        "## 对比结论",
        "",
        f"- 平均综合评价值变化：{percent_change(float(optimized_metrics['avg_suit_val']), float(original_metrics['avg_suit_val']))}",
        f"- 平均面积变化：{percent_change(float(optimized_metrics['avg_area_m2']), float(original_metrics['avg_area_m2']))}",
        f"- 平均高速距离变化：{percent_change(float(optimized_metrics['avg_dist_gs_m']), float(original_metrics['avg_dist_gs_m']))}",
        f"- 平均主干路距离变化：{percent_change(float(optimized_metrics['avg_dist_gl_m']), float(original_metrics['avg_dist_gl_m']))}",
    ]

    if float(optimized_metrics["avg_distance_proxy"]) <= float(original_metrics["avg_distance_proxy"]) and float(
        optimized_metrics["avg_cost_proxy"]
    ) <= float(original_metrics["avg_cost_proxy"]):
        lines.append("- 综合判断：多目标优化方案在成本代理和道路距离代理上整体优于原始方案，可作为当前最合适的选址结论。")
    else:
        lines.append("- 综合判断：多目标优化方案与原始方案存在权衡关系，应结合业务偏好进一步确定最终站点。")

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="使用多目标遗传算法完成选址优化与原始方案对比。")
    parser.add_argument(
        "--project-dir",
        default=str(Path(__file__).resolve().parents[1]),
        help="项目根目录，默认使用脚本上一级目录。",
    )
    parser.add_argument("--gdb-name", default=DEFAULTS["gdb_name"])
    parser.add_argument("--suitability-raster", default=DEFAULTS["suitability_raster"])
    parser.add_argument("--study-area", default=DEFAULTS["study_area"])
    parser.add_argument("--candidate-points", default=DEFAULTS["candidate_points"])
    parser.add_argument("--expressway", default=DEFAULTS["expressway"])
    parser.add_argument("--major-road", default=DEFAULTS["major_road"])
    parser.add_argument("--threshold-value", type=float, default=None)
    parser.add_argument("--threshold-ratio", type=float, default=DEFAULTS["threshold_ratio"])
    parser.add_argument("--min-area", type=float, default=DEFAULTS["min_area"])
    parser.add_argument("--num-sites", type=int, default=None)
    parser.add_argument("--random-points", type=int, default=DEFAULTS["random_points"])
    parser.add_argument("--population-size", type=int, default=DEFAULTS["population_size"])
    parser.add_argument("--generations", type=int, default=DEFAULTS["generations"])
    parser.add_argument("--mutation-rate", type=float, default=DEFAULTS["mutation_rate"])
    parser.add_argument("--tournament-size", type=int, default=DEFAULTS["tournament_size"])
    parser.add_argument("--min-site-distance", type=float, default=DEFAULTS["min_site_distance"])
    parser.add_argument("--distance-penalty", type=float, default=DEFAULTS["distance_penalty"])
    parser.add_argument("--weight-suitability", type=float, default=DEFAULTS["weight_suitability"])
    parser.add_argument("--weight-area", type=float, default=DEFAULTS["weight_area"])
    parser.add_argument("--weight-expressway", type=float, default=DEFAULTS["weight_expressway"])
    parser.add_argument("--weight-major-road", type=float, default=DEFAULTS["weight_major_road"])
    parser.add_argument("--seed", type=int, default=DEFAULTS["seed"])
    parser.add_argument(
        "--no-existing-candidates",
        action="store_true",
        help="忽略已有备选点，仅使用自动生成的候选点池。",
    )
    parser.add_argument("--comparison-csv", default=DEFAULTS["comparison_csv"])
    parser.add_argument("--comparison-md", default=DEFAULTS["comparison_md"])
    args = parser.parse_args()

    random.seed(args.seed)

    project_dir = Path(args.project_dir).resolve()
    workspace = str(project_dir / args.gdb_name)
    require_exists(workspace, "地理数据库")

    output_dir = project_dir.parent / "artifacts" / "latest"
    output_dir.mkdir(parents=True, exist_ok=True)

    arcpy.env.workspace = workspace
    arcpy.env.overwriteOutput = True
    arcpy.env.addOutputsToMap = True

    preference_weights = (
        args.weight_area,
        args.weight_expressway + args.weight_major_road,
        args.weight_suitability,
    )
    weight_total = sum(preference_weights)
    preference_weights = tuple(weight / weight_total for weight in preference_weights)

    arcpy.CheckOutExtension("Spatial")
    try:
        suitability_raster = build_output_path(workspace, args.suitability_raster)
        require_exists(suitability_raster, "综合评价结果栅格")

        candidate_polygon = build_candidate_polygon(
            workspace=workspace,
            suitability_raster=suitability_raster,
            study_area=args.study_area,
            threshold_value=args.threshold_value,
            threshold_ratio=args.threshold_ratio,
            min_area=args.min_area,
        )
        candidate_pool = build_candidate_pool(
            workspace=workspace,
            candidate_polygon=candidate_polygon,
            existing_candidate_name=args.candidate_points,
            random_points=args.random_points,
            min_site_distance=args.min_site_distance,
            no_existing_candidates=args.no_existing_candidates,
        )
        evaluated_fc = evaluate_candidates(
            workspace=workspace,
            candidate_pool=candidate_pool,
            candidate_polygon=candidate_polygon,
            suitability_raster=suitability_raster,
            expressway_name=args.expressway,
            major_road_name=args.major_road,
        )

        records = load_candidate_records(evaluated_fc)
        original_candidates = [record for record in records if record.get("source_name")]
        requested_site_count = args.num_sites if args.num_sites is not None else (
            len(original_candidates) if original_candidates else DEFAULTS["num_sites"]
        )
        if len(records) < requested_site_count:
            message(f"候选点数量不足 {requested_site_count} 个，已自动调整为 {len(records)} 个。")
        site_count = min(requested_site_count, len(records))

        best_indices, compromise_score, pareto_front, objective_cache = run_multiobjective_genetic_algorithm(
            records=records,
            site_count=site_count,
            population_size=args.population_size,
            generations=args.generations,
            mutation_rate=args.mutation_rate,
            tournament_size=args.tournament_size,
            min_site_distance=args.min_site_distance,
            distance_penalty_weight=args.distance_penalty,
            preference_weights=preference_weights,
        )

        optimized_records = sorted(
            [records[index] for index in best_indices],
            key=lambda item: item["point_score"],
            reverse=True,
        )
        original_records = choose_original_scheme(records, site_count)

        optimized_ids = [record["ga_cand_id"] for record in optimized_records]
        recommended_fc = build_output_path(workspace, "遗传算法推荐选址点")
        best_fc = build_output_path(workspace, "遗传算法最佳选址点")
        original_fc = build_output_path(workspace, "原始选址对比点")

        select_features_by_candidate_ids(evaluated_fc, recommended_fc, optimized_ids)
        annotate_selected_features(recommended_fc, optimized_records, compromise_score)
        select_features_by_candidate_ids(evaluated_fc, best_fc, [optimized_records[0]["ga_cand_id"]])
        annotate_selected_features(best_fc, [optimized_records[0]], compromise_score)

        if original_records:
            select_features_by_candidate_ids(
                evaluated_fc,
                original_fc,
                [record["ga_cand_id"] for record in original_records],
            )
        else:
            delete_if_exists(original_fc)

        original_metrics = scheme_metrics(original_records)
        optimized_metrics = scheme_metrics(optimized_records)

        assumption_note = "使用候选地块面积作为开发成本代理指标，面积越小视为成本越低。"
        write_comparison_csv(output_dir / args.comparison_csv, original_metrics, optimized_metrics)
        write_comparison_summary(
            output_dir / args.comparison_md,
            original_metrics,
            optimized_metrics,
            compromise_score,
            len(pareto_front),
            assumption_note,
        )

        message("")
        message("多目标优化选址完成。")
        message(f"候选地块: {candidate_polygon}")
        message(f"候选点池: {candidate_pool}")
        message(f"Pareto 前沿解数量: {len(pareto_front)}")
        message(f"折中方案得分: {1.0 - compromise_score:.4f}")
        message(f"推荐选址点: {recommended_fc}")
        message(f"最佳选址点: {best_fc}")
        message(f"原始方案对比点: {original_fc if original_records else '无可用原始方案'}")
        message(f"对比表: {output_dir / args.comparison_csv}")
        message(f"对比摘要: {output_dir / args.comparison_md}")
    finally:
        arcpy.CheckInExtension("Spatial")


if __name__ == "__main__":
    main()
