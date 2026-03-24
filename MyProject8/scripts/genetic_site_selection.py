#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
在 ArcGIS Pro 的 Python 环境中通过遗传算法完成“适宜区 -> 选址组合”。

默认流程：
1. 从“综合评价结果”中提取高适宜区并生成候选地块。
2. 汇总已有备选点、候选地块质心与随机候选点，形成候选点池。
3. 为候选点提取综合评价值、候选地块面积、到高速和主干路的距离。
4. 使用遗传算法搜索最优站点组合，并输出推荐结果。

运行示例（需在 Windows + ArcGIS Pro 的 arcgispro-py3 环境中执行）：
    python scripts/genetic_site_selection.py
"""

from __future__ import annotations

import argparse
import math
import os
import random
from pathlib import Path

import arcpy
from arcpy.sa import Con, ExtractByMask, ExtractMultiValuesToPoints


DEFAULTS = {
    "gdb_name": "MyProject8.gdb",
    "suitability_raster": "综合评价结果",
    "study_area": "莲池区_1",
    "candidate_points": "备选点",
    "expressway": "高速_1",
    "major_road": "二级以上的公路_1",
    "threshold_ratio": 0.8,
    "min_area": 50000.0,
    "num_sites": 3,
    "random_points": 80,
    "population_size": 60,
    "generations": 80,
    "mutation_rate": 0.18,
    "elite_size": 6,
    "tournament_size": 4,
    "min_site_distance": 1200.0,
    "distance_penalty": 1.25,
    "weight_suitability": 0.45,
    "weight_area": 0.20,
    "weight_expressway": 0.20,
    "weight_major_road": 0.15,
    "seed": 42,
}


def message(text: str) -> None:
    arcpy.AddMessage(text)
    print(text)


def warn(text: str) -> None:
    arcpy.AddWarning(text)
    print(f"WARNING: {text}")


def dataset_path(workspace: str, name: str) -> str:
    return os.path.join(workspace, name)


def build_output_path(workspace: str, name: str) -> str:
    return dataset_path(workspace, name)


def require_exists(path: str, label: str) -> None:
    if not arcpy.Exists(path):
        raise RuntimeError(f"{label}不存在: {path}")


def delete_if_exists(path: str) -> None:
    if arcpy.Exists(path):
        arcpy.management.Delete(path)


def ensure_field(fc: str, field_name: str, field_type: str) -> None:
    field_names = {field.name.upper() for field in arcpy.ListFields(fc)}
    if field_name.upper() not in field_names:
        arcpy.management.AddField(fc, field_name, field_type)


def calculate_area(fc: str, field_name: str = "area_m2") -> None:
    ensure_field(fc, field_name, "DOUBLE")
    arcpy.management.CalculateGeometryAttributes(
        fc,
        [[field_name, "AREA_GEODESIC"]],
        area_unit="SQUARE_METERS",
    )


def get_raster_min_max(raster: str) -> tuple[float, float]:
    min_value = float(arcpy.management.GetRasterProperties(raster, "MINIMUM").getOutput(0))
    max_value = float(arcpy.management.GetRasterProperties(raster, "MAXIMUM").getOutput(0))
    return min_value, max_value


def pick_threshold(raster: str, threshold_value: float | None, threshold_ratio: float) -> float:
    min_value, max_value = get_raster_min_max(raster)
    if threshold_value is not None:
        value = threshold_value
    else:
        value = max(min_value, max_value * threshold_ratio)
        if abs(round(value) - value) < 1e-6:
            value = round(value)
        elif max_value <= 20:
            value = math.ceil(value)
    message(f"综合评价结果范围: min={min_value}, max={max_value}")
    message(f"高适宜区阈值: {value}")
    return float(value)


def select_large_polygons(input_fc: str, output_fc: str, min_area: float) -> str:
    delete_if_exists(output_fc)
    arcpy.management.MakeFeatureLayer(input_fc, "ga_candidate_polygons_lyr")
    arcpy.management.SelectLayerByAttribute(
        "ga_candidate_polygons_lyr",
        "NEW_SELECTION",
        f"area_m2 >= {min_area}",
    )
    count = int(arcpy.management.GetCount("ga_candidate_polygons_lyr").getOutput(0))
    if count == 0:
        raise RuntimeError("没有满足最小面积要求的候选地块，请降低阈值或减小最小面积。")
    arcpy.management.CopyFeatures("ga_candidate_polygons_lyr", output_fc)
    return output_fc


def spatial_join_points_with_polygons(points_fc: str, polygons_fc: str, output_fc: str) -> str:
    delete_if_exists(output_fc)
    arcpy.analysis.SpatialJoin(
        target_features=points_fc,
        join_features=polygons_fc,
        out_feature_class=output_fc,
        join_operation="JOIN_ONE_TO_ONE",
        join_type="KEEP_COMMON",
        match_option="WITHIN",
    )
    return output_fc


def normalize(values: list[float | None], reverse: bool = False) -> list[float]:
    valid = [v for v in values if v is not None]
    if not valid:
        return [0.5] * len(values)
    min_value = min(valid)
    max_value = max(valid)
    if abs(max_value - min_value) < 1e-9:
        return [0.5] * len(values)

    normalized = []
    for value in values:
        if value is None:
            score = 0.5
        else:
            score = (value - min_value) / (max_value - min_value)
            if reverse:
                score = 1.0 - score
        normalized.append(max(0.0, min(1.0, score)))
    return normalized


def build_candidate_polygon(
    workspace: str,
    suitability_raster: str,
    study_area: str,
    threshold_value: float | None,
    threshold_ratio: float,
    min_area: float,
) -> str:
    study_area_path = build_output_path(workspace, study_area)
    has_study_area = arcpy.Exists(study_area_path)
    if has_study_area:
        message(f"使用研究区约束: {study_area}")
    else:
        warn(f"未找到研究区 {study_area}，将直接使用综合评价结果。")

    threshold = pick_threshold(
        suitability_raster,
        threshold_value=threshold_value,
        threshold_ratio=threshold_ratio,
    )

    masked_raster = build_output_path(workspace, "综合评价结果_GA_研究区裁剪")
    delete_if_exists(masked_raster)
    if has_study_area:
        ExtractByMask(suitability_raster, study_area_path).save(masked_raster)
    else:
        arcpy.management.CopyRaster(suitability_raster, masked_raster)

    high_suitability_raster = build_output_path(workspace, "高适宜区_GA_栅格")
    delete_if_exists(high_suitability_raster)
    Con(arcpy.Raster(masked_raster) >= threshold, 1, 0).save(high_suitability_raster)
    message("已生成遗传算法高适宜区栅格。")

    raw_polygon = build_output_path(workspace, "高适宜区_GA_面_原始")
    delete_if_exists(raw_polygon)
    arcpy.conversion.RasterToPolygon(
        in_raster=high_suitability_raster,
        out_polygon_features=raw_polygon,
        simplify="SIMPLIFY",
        raster_field="Value",
    )

    dissolved_polygon = build_output_path(workspace, "高适宜区_GA_面")
    delete_if_exists(dissolved_polygon)
    arcpy.management.MakeFeatureLayer(raw_polygon, "ga_raw_polygon_lyr")
    arcpy.management.SelectLayerByAttribute("ga_raw_polygon_lyr", "NEW_SELECTION", "gridcode = 1")
    arcpy.management.Dissolve("ga_raw_polygon_lyr", dissolved_polygon)
    calculate_area(dissolved_polygon)
    message("已生成遗传算法候选面并完成面积计算。")

    candidate_polygon = build_output_path(workspace, "候选地块_GA")
    select_large_polygons(dissolved_polygon, candidate_polygon, min_area)
    message("已完成遗传算法候选地块筛选。")
    return candidate_polygon


def build_candidate_pool(
    workspace: str,
    candidate_polygon: str,
    existing_candidate_name: str,
    random_points: int,
    min_site_distance: float,
    no_existing_candidates: bool,
) -> str:
    pool_parts: list[str] = []

    centroid_fc = build_output_path(workspace, "候选点_GA_质心")
    delete_if_exists(centroid_fc)
    arcpy.management.FeatureToPoint(candidate_polygon, centroid_fc, "INSIDE")
    pool_parts.append(centroid_fc)
    message("已生成候选地块质心。")

    if random_points > 0:
        random_fc = build_output_path(workspace, "候选点_GA_随机")
        delete_if_exists(random_fc)
        arcpy.management.CreateRandomPoints(
            workspace,
            os.path.basename(random_fc),
            candidate_polygon,
            None,
            random_points,
            max(min_site_distance / 5.0, 0.0),
            "POINT",
        )
        pool_parts.append(random_fc)
        message(f"已在候选地块内生成随机候选点: {random_points}")

    existing_candidate_fc = build_output_path(workspace, existing_candidate_name)
    if not no_existing_candidates and arcpy.Exists(existing_candidate_fc):
        selected_existing = build_output_path(workspace, "候选点_GA_已有")
        delete_if_exists(selected_existing)
        arcpy.management.MakeFeatureLayer(existing_candidate_fc, "ga_existing_candidates_lyr")
        arcpy.management.SelectLayerByLocation(
            "ga_existing_candidates_lyr",
            overlap_type="WITHIN",
            select_features=candidate_polygon,
            selection_type="NEW_SELECTION",
        )
        count = int(arcpy.management.GetCount("ga_existing_candidates_lyr").getOutput(0))
        if count > 0:
            arcpy.management.CopyFeatures("ga_existing_candidates_lyr", selected_existing)
            pool_parts.append(selected_existing)
            message(f"已纳入已有备选点: {count}")
        else:
            warn("已有备选点没有落入候选地块，已忽略。")

    if not pool_parts:
        raise RuntimeError("未能生成任何候选点。")

    merged_pool = build_output_path(workspace, "候选点_GA_池")
    delete_if_exists(merged_pool)
    if len(pool_parts) == 1:
        arcpy.management.CopyFeatures(pool_parts[0], merged_pool)
    else:
        arcpy.management.Merge(pool_parts, merged_pool)

    try:
        arcpy.management.DeleteIdentical(merged_pool, ["Shape"])
    except arcpy.ExecuteError:
        warn("DeleteIdentical 执行失败，保留原始候选点池。")

    count = int(arcpy.management.GetCount(merged_pool).getOutput(0))
    if count == 0:
        raise RuntimeError("候选点池为空，无法执行遗传算法。")
    message(f"候选点池构建完成，共 {count} 个候选点。")
    return merged_pool


def evaluate_candidates(
    workspace: str,
    candidate_pool: str,
    candidate_polygon: str,
    suitability_raster: str,
    expressway_name: str,
    major_road_name: str,
) -> str:
    evaluated_fc = build_output_path(workspace, "候选点_GA_评价")
    spatial_join_points_with_polygons(candidate_pool, candidate_polygon, evaluated_fc)

    ensure_field(evaluated_fc, "ga_cand_id", "LONG")
    arcpy.management.CalculateField(evaluated_fc, "ga_cand_id", "!OBJECTID!", "PYTHON3")

    ExtractMultiValuesToPoints(evaluated_fc, [[suitability_raster, "suit_val"]], "NONE")
    ensure_field(evaluated_fc, "dist_gs_m", "DOUBLE")
    ensure_field(evaluated_fc, "dist_gl_m", "DOUBLE")

    expressway_fc = build_output_path(workspace, expressway_name)
    major_road_fc = build_output_path(workspace, major_road_name)

    if arcpy.Exists(expressway_fc):
        arcpy.analysis.Near(evaluated_fc, expressway_fc)
        arcpy.management.CalculateField(evaluated_fc, "dist_gs_m", "!NEAR_DIST!", "PYTHON3")
        message("已计算候选点到高速的距离。")
    else:
        warn(f"未找到高速图层 {expressway_name}，高速距离将按中性值处理。")

    if arcpy.Exists(major_road_fc):
        arcpy.analysis.Near(evaluated_fc, major_road_fc)
        arcpy.management.CalculateField(evaluated_fc, "dist_gl_m", "!NEAR_DIST!", "PYTHON3")
        message("已计算候选点到二级以上公路的距离。")
    else:
        warn(f"未找到二级以上公路图层 {major_road_name}，公路距离将按中性值处理。")

    return evaluated_fc


def load_candidate_records(
    evaluated_fc: str,
    weight_suitability: float,
    weight_area: float,
    weight_expressway: float,
    weight_major_road: float,
) -> list[dict]:
    fields = [
        "ga_cand_id",
        "SHAPE@",
        "suit_val",
        "area_m2",
        "dist_gs_m",
        "dist_gl_m",
    ]
    records: list[dict] = []
    with arcpy.da.SearchCursor(evaluated_fc, fields) as cursor:
        for ga_cand_id, geometry, suit_val, area_m2, dist_gs_m, dist_gl_m in cursor:
            if geometry is None:
                continue
            records.append(
                {
                    "ga_cand_id": int(ga_cand_id),
                    "geometry": geometry,
                    "suit_val": None if suit_val in (None, -9999) else float(suit_val),
                    "area_m2": None if area_m2 is None else float(area_m2),
                    "dist_gs_m": None if dist_gs_m in (None, -1) else float(dist_gs_m),
                    "dist_gl_m": None if dist_gl_m in (None, -1) else float(dist_gl_m),
                }
            )

    if not records:
        raise RuntimeError("候选点评价结果为空，无法执行遗传算法。")

    suit_scores = normalize([r["suit_val"] for r in records])
    area_scores = normalize([r["area_m2"] for r in records])
    expressway_scores = normalize([r["dist_gs_m"] for r in records], reverse=True)
    major_road_scores = normalize([r["dist_gl_m"] for r in records], reverse=True)

    for idx, record in enumerate(records):
        record["score_suit"] = suit_scores[idx]
        record["score_area"] = area_scores[idx]
        record["score_gs"] = expressway_scores[idx]
        record["score_gl"] = major_road_scores[idx]
        record["site_score"] = (
            weight_suitability * suit_scores[idx]
            + weight_area * area_scores[idx]
            + weight_expressway * expressway_scores[idx]
            + weight_major_road * major_road_scores[idx]
        )

    records.sort(key=lambda item: item["site_score"], reverse=True)
    return records


def initial_population(candidate_count: int, site_count: int, population_size: int) -> list[list[int]]:
    population: list[list[int]] = []
    base = list(range(candidate_count))
    for _ in range(population_size):
        chromosome = sorted(random.sample(base, site_count))
        population.append(chromosome)
    return population


def separation_penalty(chromosome: list[int], records: list[dict], min_site_distance: float) -> float:
    if min_site_distance <= 0:
        return 0.0
    penalty = 0.0
    for left in range(len(chromosome)):
        for right in range(left + 1, len(chromosome)):
            distance = records[chromosome[left]]["geometry"].distanceTo(
                records[chromosome[right]]["geometry"]
            )
            if distance < min_site_distance:
                penalty += (min_site_distance - distance) / min_site_distance
    return penalty


def fitness(
    chromosome: list[int],
    records: list[dict],
    min_site_distance: float,
    distance_penalty_weight: float,
) -> float:
    score = sum(records[index]["site_score"] for index in chromosome)
    score /= max(len(chromosome), 1)
    penalty = separation_penalty(chromosome, records, min_site_distance)
    return score - penalty * distance_penalty_weight


def tournament_select(
    population: list[list[int]],
    records: list[dict],
    min_site_distance: float,
    distance_penalty_weight: float,
    tournament_size: int,
) -> list[int]:
    sampled = random.sample(population, min(tournament_size, len(population)))
    sampled.sort(
        key=lambda chromosome: fitness(
            chromosome,
            records,
            min_site_distance,
            distance_penalty_weight,
        ),
        reverse=True,
    )
    return list(sampled[0])


def crossover(parent_a: list[int], parent_b: list[int], candidate_count: int, site_count: int) -> list[int]:
    merged = list(dict.fromkeys(parent_a + parent_b))
    child: list[int] = []
    while merged and len(child) < site_count:
        index = random.randrange(len(merged))
        child.append(merged.pop(index))

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


def run_genetic_algorithm(
    records: list[dict],
    site_count: int,
    population_size: int,
    generations: int,
    mutation_rate: float,
    elite_size: int,
    tournament_size: int,
    min_site_distance: float,
    distance_penalty_weight: float,
) -> tuple[list[int], float]:
    candidate_count = len(records)
    population = initial_population(candidate_count, site_count, population_size)

    best_chromosome = max(
        population,
        key=lambda chromosome: fitness(
            chromosome,
            records,
            min_site_distance,
            distance_penalty_weight,
        ),
    )
    best_score = fitness(best_chromosome, records, min_site_distance, distance_penalty_weight)

    for generation in range(1, generations + 1):
        ranked = sorted(
            population,
            key=lambda chromosome: fitness(
                chromosome,
                records,
                min_site_distance,
                distance_penalty_weight,
            ),
            reverse=True,
        )
        current_best = ranked[0]
        current_score = fitness(current_best, records, min_site_distance, distance_penalty_weight)
        if current_score > best_score:
            best_chromosome = list(current_best)
            best_score = current_score

        next_population = [list(chromosome) for chromosome in ranked[:elite_size]]
        while len(next_population) < population_size:
            parent_a = tournament_select(
                ranked,
                records,
                min_site_distance,
                distance_penalty_weight,
                tournament_size,
            )
            parent_b = tournament_select(
                ranked,
                records,
                min_site_distance,
                distance_penalty_weight,
                tournament_size,
            )
            child = crossover(parent_a, parent_b, candidate_count, site_count)
            child = mutate(child, candidate_count, mutation_rate)
            next_population.append(child)
        population = next_population

        if generation == 1 or generation == generations or generation % 10 == 0:
            message(f"第 {generation} 代完成，当前最优适应度: {best_score:.4f}")

    return sorted(best_chromosome), best_score


def select_features_by_candidate_ids(
    input_fc: str,
    output_fc: str,
    candidate_ids: list[int],
    id_field: str = "ga_cand_id",
) -> str:
    if not candidate_ids:
        raise RuntimeError("候选站点结果为空，无法输出要素。")
    delete_if_exists(output_fc)
    sql = f"{arcpy.AddFieldDelimiters(input_fc, id_field)} IN ({','.join(map(str, candidate_ids))})"
    arcpy.analysis.Select(input_fc, output_fc, sql)
    return output_fc


def annotate_selected_features(
    output_fc: str,
    selected_records: list[dict],
    best_fitness: float,
) -> None:
    ensure_field(output_fc, "ga_rank", "LONG")
    ensure_field(output_fc, "ga_score", "DOUBLE")
    ensure_field(output_fc, "ga_fit", "DOUBLE")

    rank_map = {
        record["ga_cand_id"]: (rank, record["site_score"])
        for rank, record in enumerate(selected_records, start=1)
    }
    with arcpy.da.UpdateCursor(output_fc, ["ga_cand_id", "ga_rank", "ga_score", "ga_fit"]) as cursor:
        for ga_cand_id, _, _, _ in cursor:
            rank, site_score = rank_map[int(ga_cand_id)]
            cursor.updateRow([ga_cand_id, rank, site_score, best_fitness])


def main() -> None:
    parser = argparse.ArgumentParser(description="使用遗传算法从适宜区选择最优站点组合。")
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
    parser.add_argument("--num-sites", type=int, default=DEFAULTS["num_sites"])
    parser.add_argument("--random-points", type=int, default=DEFAULTS["random_points"])
    parser.add_argument("--population-size", type=int, default=DEFAULTS["population_size"])
    parser.add_argument("--generations", type=int, default=DEFAULTS["generations"])
    parser.add_argument("--mutation-rate", type=float, default=DEFAULTS["mutation_rate"])
    parser.add_argument("--elite-size", type=int, default=DEFAULTS["elite_size"])
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
    args = parser.parse_args()

    if args.num_sites <= 0:
        raise RuntimeError("--num-sites 必须大于 0。")
    if args.population_size < 4:
        raise RuntimeError("--population-size 至少为 4。")
    if args.elite_size <= 0 or args.elite_size >= args.population_size:
        raise RuntimeError("--elite-size 必须大于 0 且小于 population-size。")

    random.seed(args.seed)

    project_dir = Path(args.project_dir).resolve()
    workspace = str(project_dir / args.gdb_name)
    require_exists(workspace, "地理数据库")

    arcpy.env.workspace = workspace
    arcpy.env.overwriteOutput = True
    arcpy.env.addOutputsToMap = True

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

        records = load_candidate_records(
            evaluated_fc=evaluated_fc,
            weight_suitability=args.weight_suitability,
            weight_area=args.weight_area,
            weight_expressway=args.weight_expressway,
            weight_major_road=args.weight_major_road,
        )

        if len(records) < args.num_sites:
            warn(f"候选点数量不足 {args.num_sites} 个，已自动调整为 {len(records)} 个。")
        site_count = min(args.num_sites, len(records))

        best_indices, best_fitness = run_genetic_algorithm(
            records=records,
            site_count=site_count,
            population_size=args.population_size,
            generations=args.generations,
            mutation_rate=args.mutation_rate,
            elite_size=args.elite_size,
            tournament_size=args.tournament_size,
            min_site_distance=args.min_site_distance,
            distance_penalty_weight=args.distance_penalty,
        )

        selected_records = sorted(
            [records[index] for index in best_indices],
            key=lambda item: item["site_score"],
            reverse=True,
        )
        selected_ids = [record["ga_cand_id"] for record in selected_records]

        recommended_fc = build_output_path(workspace, "遗传算法推荐选址点")
        best_fc = build_output_path(workspace, "遗传算法最佳选址点")
        select_features_by_candidate_ids(evaluated_fc, recommended_fc, selected_ids)
        annotate_selected_features(recommended_fc, selected_records, best_fitness)
        select_features_by_candidate_ids(evaluated_fc, best_fc, [selected_records[0]["ga_cand_id"]])
        annotate_selected_features(best_fc, [selected_records[0]], best_fitness)

        message("")
        message("遗传算法选址完成。")
        message(f"候选地块: {candidate_polygon}")
        message(f"候选点池: {candidate_pool}")
        message(f"遗传算法推荐选址点: {recommended_fc}")
        message(f"遗传算法最佳选址点: {best_fc}")
        message(f"最优适应度: {best_fitness:.4f}")
    finally:
        arcpy.CheckInExtension("Spatial")


if __name__ == "__main__":
    main()
