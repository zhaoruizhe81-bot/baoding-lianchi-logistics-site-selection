#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
在 ArcGIS Pro 的 Python 环境中自动完成“适宜区 -> 选址点”。

默认流程：
1. 从“综合评价结果”中提取高适宜区。
2. 栅格转面，并删除面积过小的碎片地块。
3. 优先使用已有“备选点”；若不存在或筛选后为空，则自动在候选面内生成点。
4. 为候选点提取综合评价值，并计算到高速、二级以上公路的距离。
5. 输出排序后的推荐点，以及最佳选址点。

运行示例（需在 Windows + ArcGIS Pro 的 arcgispro-py3 环境中执行）：
    python scripts/auto_select_sites.py
"""

from __future__ import annotations

import argparse
import math
import os
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
    "top_n": 5,
}


def message(text: str) -> None:
    arcpy.AddMessage(text)
    print(text)


def warn(text: str) -> None:
    arcpy.AddWarning(text)
    print(f"WARNING: {text}")


def dataset_path(workspace: str, name: str) -> str:
    return os.path.join(workspace, name)


def require_exists(path: str, label: str) -> None:
    if not arcpy.Exists(path):
        raise RuntimeError(f"{label}不存在: {path}")


def delete_if_exists(path: str) -> None:
    if arcpy.Exists(path):
        arcpy.management.Delete(path)


def build_output_path(workspace: str, name: str) -> str:
    return dataset_path(workspace, name)


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


def select_large_polygons(input_fc: str, output_fc: str, min_area: float) -> str:
    delete_if_exists(output_fc)
    arcpy.management.MakeFeatureLayer(input_fc, "candidate_polygons_lyr")
    arcpy.management.SelectLayerByAttribute(
        "candidate_polygons_lyr",
        "NEW_SELECTION",
        f"area_m2 >= {min_area}",
    )
    count = int(arcpy.management.GetCount("candidate_polygons_lyr").getOutput(0))
    if count == 0:
        raise RuntimeError("没有满足最小面积要求的候选地块，请降低阈值或减小最小面积。")
    arcpy.management.CopyFeatures("candidate_polygons_lyr", output_fc)
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


def copy_top_n(sorted_fc: str, output_fc: str, top_n: int) -> str:
    oid_field = arcpy.Describe(sorted_fc).OIDFieldName
    oids = []
    with arcpy.da.SearchCursor(sorted_fc, [oid_field]) as cursor:
        for row in cursor:
            oids.append(row[0])
            if len(oids) >= top_n:
                break
    if not oids:
        raise RuntimeError("排序结果为空，无法输出推荐点。")

    delete_if_exists(output_fc)
    sql = f"{arcpy.AddFieldDelimiters(sorted_fc, oid_field)} IN ({','.join(map(str, oids))})"
    arcpy.analysis.Select(sorted_fc, output_fc, sql)
    return output_fc


def copy_best_point(sorted_fc: str, output_fc: str) -> str:
    return copy_top_n(sorted_fc, output_fc, 1)


def main() -> None:
    parser = argparse.ArgumentParser(description="自动从适宜区生成推荐选址点。")
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
    parser.add_argument("--top-n", type=int, default=DEFAULTS["top_n"])
    parser.add_argument(
        "--no-existing-candidates",
        action="store_true",
        help="忽略已有的备选点，直接在候选地块内自动生成点。",
    )
    args = parser.parse_args()

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

        study_area = build_output_path(workspace, args.study_area)
        has_study_area = arcpy.Exists(study_area)
        if has_study_area:
            message(f"使用研究区约束: {args.study_area}")
        else:
            warn(f"未找到研究区 {args.study_area}，将直接使用综合评价结果。")

        threshold = pick_threshold(
            suitability_raster,
            threshold_value=args.threshold_value,
            threshold_ratio=args.threshold_ratio,
        )

        masked_raster = build_output_path(workspace, "综合评价结果_研究区裁剪")
        delete_if_exists(masked_raster)
        if has_study_area:
            ExtractByMask(suitability_raster, study_area).save(masked_raster)
        else:
            arcpy.management.CopyRaster(suitability_raster, masked_raster)

        high_suitability_raster = build_output_path(workspace, "高适宜区_栅格_自动")
        delete_if_exists(high_suitability_raster)
        Con(arcpy.Raster(masked_raster) >= threshold, 1, 0).save(high_suitability_raster)
        message("已生成高适宜区栅格。")

        raw_polygon = build_output_path(workspace, "高适宜区_面_原始")
        delete_if_exists(raw_polygon)
        arcpy.conversion.RasterToPolygon(
            in_raster=high_suitability_raster,
            out_polygon_features=raw_polygon,
            simplify="SIMPLIFY",
            raster_field="Value",
        )

        dissolved_polygon = build_output_path(workspace, "高适宜区_面_自动")
        delete_if_exists(dissolved_polygon)
        arcpy.management.MakeFeatureLayer(raw_polygon, "raw_polygon_lyr")
        arcpy.management.SelectLayerByAttribute("raw_polygon_lyr", "NEW_SELECTION", "gridcode = 1")
        arcpy.management.Dissolve("raw_polygon_lyr", dissolved_polygon)
        calculate_area(dissolved_polygon)
        message("已生成高适宜区面，并完成面积计算。")

        candidate_polygon = build_output_path(workspace, "候选地块_自动")
        select_large_polygons(dissolved_polygon, candidate_polygon, args.min_area)
        message("已完成候选地块筛选。")

        raw_points = build_output_path(workspace, "候选点_自动")
        joined_points = build_output_path(workspace, "候选点_评价")
        sorted_points = build_output_path(workspace, "推荐选址点_排序")
        top_points = build_output_path(workspace, "推荐选址点")
        best_point = build_output_path(workspace, "最佳选址点")

        for item in [raw_points, joined_points, sorted_points, top_points, best_point]:
            delete_if_exists(item)

        existing_candidate_fc = build_output_path(workspace, args.candidate_points)
        use_existing_candidates = (
            not args.no_existing_candidates and arcpy.Exists(existing_candidate_fc)
        )

        if use_existing_candidates:
            message(f"优先使用已有备选点: {args.candidate_points}")
            arcpy.management.MakeFeatureLayer(existing_candidate_fc, "existing_candidates_lyr")
            arcpy.management.SelectLayerByLocation(
                "existing_candidates_lyr",
                overlap_type="WITHIN",
                select_features=candidate_polygon,
                selection_type="NEW_SELECTION",
            )
            selected_count = int(arcpy.management.GetCount("existing_candidates_lyr").getOutput(0))
            if selected_count > 0:
                arcpy.management.CopyFeatures("existing_candidates_lyr", raw_points)
            else:
                warn("已有备选点没有落入候选地块，将自动生成点。")
                use_existing_candidates = False

        if not use_existing_candidates:
            arcpy.management.FeatureToPoint(candidate_polygon, raw_points, "INSIDE")
            message("已在候选地块内自动生成候选点。")

        spatial_join_points_with_polygons(raw_points, candidate_polygon, joined_points)

        ExtractMultiValuesToPoints(joined_points, [[suitability_raster, "suit_val"]], "NONE")
        ensure_field(joined_points, "dist_gs_m", "DOUBLE")
        ensure_field(joined_points, "dist_gl_m", "DOUBLE")

        expressway_fc = build_output_path(workspace, args.expressway)
        major_road_fc = build_output_path(workspace, args.major_road)

        if arcpy.Exists(expressway_fc):
            arcpy.analysis.Near(joined_points, expressway_fc)
            arcpy.management.CalculateField(joined_points, "dist_gs_m", "!NEAR_DIST!", "PYTHON3")
            message("已计算到高速的距离。")
        else:
            warn(f"未找到高速图层 {args.expressway}，跳过高速距离计算。")

        if arcpy.Exists(major_road_fc):
            arcpy.analysis.Near(joined_points, major_road_fc)
            arcpy.management.CalculateField(joined_points, "dist_gl_m", "!NEAR_DIST!", "PYTHON3")
            message("已计算到二级以上公路的距离。")
        else:
            warn(f"未找到二级以上公路图层 {args.major_road}，跳过公路距离计算。")

        arcpy.management.Sort(
            joined_points,
            sorted_points,
            [
                ["suit_val", "DESCENDING"],
                ["area_m2", "DESCENDING"],
                ["dist_gs_m", "ASCENDING"],
                ["dist_gl_m", "ASCENDING"],
            ],
        )
        copy_top_n(sorted_points, top_points, args.top_n)
        copy_best_point(sorted_points, best_point)

        message("")
        message("自动选址完成。")
        message(f"候选地块: {candidate_polygon}")
        message(f"推荐选址点(前{args.top_n}): {top_points}")
        message(f"最佳选址点: {best_point}")
    finally:
        arcpy.CheckInExtension("Spatial")


if __name__ == "__main__":
    main()
