#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
导出遗传算法选址结果的地图、PDF 和结果表。

默认输出：
1. 推荐站点 CSV
2. 最佳站点 CSV
3. Markdown 结果摘要
4. 布局 PDF
5. 布局 PNG
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
from pathlib import Path

import arcpy


DEFAULTS = {
    "gdb_name": "MyProject8.gdb",
    "aprx_name": "MyProject8.aprx",
    "recommended_fc": "遗传算法推荐选址点",
    "best_fc": "遗传算法最佳选址点",
    "candidate_polygon_fc": "候选地块_GA",
    "map_name": "地图",
    "layouts": "布局2,布局,标题栏 A4 横向",
}

OLD_POINT_LAYER_NAMES = ("备选点",)


def message(text: str) -> None:
    arcpy.AddMessage(text)
    print(text)


def warn(text: str) -> None:
    arcpy.AddWarning(text)
    print(f"WARNING: {text}")


def dataset_path(workspace: Path, name: str) -> str:
    return str(workspace / name)


def require_exists(path: str, label: str) -> None:
    if not arcpy.Exists(path):
        raise RuntimeError(f"{label}不存在: {path}")


def slugify(value: str) -> str:
    normalized = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", value.strip(), flags=re.UNICODE)
    normalized = normalized.strip("-")
    return normalized or "layout"


def get_layouts(aprx: arcpy.mp.ArcGISProject, names: list[str]) -> list:
    found = []
    for name in names:
        matches = aprx.listLayouts(name)
        if matches:
            found.append(matches[0])
    if found:
        return found
    all_layouts = aprx.listLayouts()
    if not all_layouts:
        raise RuntimeError("工程中没有可导出的布局。")
    warn("未找到指定布局，已回退为导出全部布局。")
    return all_layouts


def existing_field_name(feature_class: str, field_candidates: list[str]) -> str | None:
    fields = {field.name.upper(): field.name for field in arcpy.ListFields(feature_class)}
    for candidate in field_candidates:
        match = fields.get(candidate.upper())
        if match:
            return match
    return None


def hide_layers(map_obj, layer_names: tuple[str, ...]) -> None:
    for layer_name in layer_names:
        for layer in map_obj.listLayers(layer_name):
            layer.visible = False


def apply_simple_renderer(
    layer,
    *,
    label: str,
    color: dict,
    size: float,
    outline_color: dict | None = None,
    outline_width: float | None = None,
) -> None:
    if not getattr(layer, "isFeatureLayer", False):
        return

    sym = layer.symbology
    if not hasattr(sym, "renderer"):
        return
    if sym.renderer.type != "SimpleRenderer":
        sym.updateRenderer("SimpleRenderer")

    sym.renderer.label = label
    sym.renderer.symbol.color = color
    sym.renderer.symbol.size = size
    if outline_color is not None:
        sym.renderer.symbol.outlineColor = outline_color
    if outline_width is not None:
        sym.renderer.symbol.outlineWidth = outline_width
    layer.symbology = sym


def ensure_result_layers(
    aprx: arcpy.mp.ArcGISProject,
    map_name: str,
    candidate_polygon_path: str,
    recommended_path: str,
    best_path: str,
) -> None:
    map_matches = aprx.listMaps(map_name)
    map_obj = map_matches[0] if map_matches else aprx.listMaps()[0]

    hide_layers(map_obj, OLD_POINT_LAYER_NAMES)

    for layer_name in ["候选地块_GA", "遗传算法推荐选址点", "遗传算法最佳选址点"]:
        for layer in map_obj.listLayers(layer_name):
            map_obj.removeLayer(layer)

    map_obj.addDataFromPath(candidate_polygon_path)
    map_obj.addDataFromPath(recommended_path)
    map_obj.addDataFromPath(best_path)

    candidate_layer = map_obj.listLayers("候选地块_GA")[0]
    recommended_layer = map_obj.listLayers("遗传算法推荐选址点")[0]
    best_layer = map_obj.listLayers("遗传算法最佳选址点")[0]

    apply_simple_renderer(
        candidate_layer,
        label="候选地块",
        color={"RGB": [255, 230, 140, 25]},
        outline_color={"RGB": [255, 140, 0, 100]},
        outline_width=1.5,
        size=1.5,
    )
    apply_simple_renderer(
        recommended_layer,
        label="遗传算法推荐点",
        color={"RGB": [255, 170, 0, 100]},
        outline_color={"RGB": [90, 45, 0, 100]},
        outline_width=1.8,
        size=14,
    )
    apply_simple_renderer(
        best_layer,
        label="遗传算法最佳点",
        color={"RGB": [220, 20, 60, 100]},
        outline_color={"RGB": [255, 255, 255, 100]},
        outline_width=2.2,
        size=18,
    )


def export_layouts(aprx: arcpy.mp.ArcGISProject, output_dir: Path, layout_names: list[str]) -> list[Path]:
    exported: list[Path] = []
    for layout in get_layouts(aprx, layout_names):
        base_name = slugify(layout.name)
        pdf_path = output_dir / f"{base_name}.pdf"
        png_path = output_dir / f"{base_name}.png"
        layout.exportToPDF(str(pdf_path), resolution=200)
        layout.exportToPNG(str(png_path), resolution=180)
        exported.extend([pdf_path, png_path])
        message(f"已导出布局: {layout.name}")
    return exported


def feature_class_to_rows(feature_class: str) -> list[dict]:
    area_field = existing_field_name(feature_class, ["area_m2_1", "area_m2"])
    fields = ["ga_rank", "ga_cand_id", "ga_score", "ga_fit", "suit_val"]
    if area_field:
        fields.append(area_field)
    fields.extend(["dist_gs_m", "dist_gl_m", "SHAPE@XY"])
    rows: list[dict] = []
    with arcpy.da.SearchCursor(feature_class, fields) as cursor:
        for row in cursor:
            if area_field:
                ga_rank, ga_cand_id, ga_score, ga_fit, suit_val, area_m2, dist_gs_m, dist_gl_m, xy = row
            else:
                ga_rank, ga_cand_id, ga_score, ga_fit, suit_val, dist_gs_m, dist_gl_m, xy = row
                area_m2 = None
            x_value, y_value = xy if xy else (None, None)
            rows.append(
                {
                    "ga_rank": ga_rank,
                    "ga_cand_id": ga_cand_id,
                    "ga_score": ga_score,
                    "ga_fit": ga_fit,
                    "suit_val": suit_val,
                    "area_m2": area_m2,
                    "dist_gs_m": dist_gs_m,
                    "dist_gl_m": dist_gl_m,
                    "x": x_value,
                    "y": y_value,
                }
            )
    rows.sort(key=lambda item: (item["ga_rank"] is None, item["ga_rank"]))
    return rows


def write_csv(rows: list[dict], output_path: Path) -> None:
    fieldnames = ["ga_rank", "ga_cand_id", "ga_score", "ga_fit", "suit_val", "area_m2", "dist_gs_m", "dist_gl_m", "x", "y"]
    with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_summary(
    output_path: Path,
    recommended_rows: list[dict],
    best_rows: list[dict],
    exported_files: list[Path],
) -> None:
    lines = [
        "# 选址结果摘要",
        "",
        f"- 导出时间：{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 推荐站点数量：{len(recommended_rows)}",
        f"- 最佳站点数量：{len(best_rows)}",
    ]

    if best_rows:
        best = best_rows[0]
        lines.extend(
            [
                f"- 最佳适应度：{best.get('ga_fit')}",
                f"- 最佳站点编号：{best.get('ga_cand_id')}",
                f"- 最佳站点评分：{best.get('ga_score')}",
            ]
        )

    lines.extend(["", "## 导出文件", ""])
    for exported_file in exported_files:
        lines.append(f"- {exported_file.name}")

    lines.extend(["", "## 推荐站点", ""])
    for row in recommended_rows:
        lines.append(
            "- 排名 {ga_rank} | 候选点 {ga_cand_id} | 评分 {ga_score:.4f} | 适应度 {ga_fit:.4f} | 综合评价 {suit_val} | 面积 {area_m2} | 高速距离 {dist_gs_m} | 主干路距离 {dist_gl_m}".format(
                ga_rank=row.get("ga_rank"),
                ga_cand_id=row.get("ga_cand_id"),
                ga_score=float(row.get("ga_score") or 0.0),
                ga_fit=float(row.get("ga_fit") or 0.0),
                suit_val=row.get("suit_val"),
                area_m2=row.get("area_m2"),
                dist_gs_m=row.get("dist_gs_m"),
                dist_gl_m=row.get("dist_gl_m"),
            )
        )

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="导出遗传算法选址结果的地图、PDF 和结果表。")
    parser.add_argument(
        "--project-dir",
        default=str(Path(__file__).resolve().parents[1]),
        help="ArcGIS 工程目录，默认是脚本上一级目录。",
    )
    parser.add_argument("--gdb-name", default=DEFAULTS["gdb_name"])
    parser.add_argument("--aprx-name", default=DEFAULTS["aprx_name"])
    parser.add_argument("--recommended-fc", default=DEFAULTS["recommended_fc"])
    parser.add_argument("--best-fc", default=DEFAULTS["best_fc"])
    parser.add_argument("--candidate-polygon-fc", default=DEFAULTS["candidate_polygon_fc"])
    parser.add_argument("--map-name", default=DEFAULTS["map_name"])
    parser.add_argument("--layouts", default=DEFAULTS["layouts"])
    parser.add_argument("--output-dir", default=None, help="导出目录，默认写入仓库根目录下的 artifacts/latest。")
    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()
    repo_dir = project_dir.parent
    output_dir = Path(args.output_dir).resolve() if args.output_dir else repo_dir / "artifacts" / "latest"
    output_dir.mkdir(parents=True, exist_ok=True)

    workspace = project_dir / args.gdb_name
    aprx_path = project_dir / args.aprx_name

    recommended_path = dataset_path(workspace, args.recommended_fc)
    best_path = dataset_path(workspace, args.best_fc)
    candidate_polygon_path = dataset_path(workspace, args.candidate_polygon_fc)

    require_exists(recommended_path, "遗传算法推荐选址点")
    require_exists(best_path, "遗传算法最佳选址点")
    require_exists(candidate_polygon_path, "候选地块")
    if not aprx_path.exists():
        raise RuntimeError(f"ArcGIS 工程不存在: {aprx_path}")

    recommended_rows = feature_class_to_rows(recommended_path)
    best_rows = feature_class_to_rows(best_path)

    write_csv(recommended_rows, output_dir / "recommended_sites.csv")
    write_csv(best_rows, output_dir / "best_site.csv")
    message("已导出结果表 CSV。")

    aprx = arcpy.mp.ArcGISProject(str(aprx_path))
    ensure_result_layers(
        aprx=aprx,
        map_name=args.map_name,
        candidate_polygon_path=candidate_polygon_path,
        recommended_path=recommended_path,
        best_path=best_path,
    )

    exported_files = [
        output_dir / "recommended_sites.csv",
        output_dir / "best_site.csv",
    ]
    exported_files.extend(export_layouts(aprx, output_dir, [item.strip() for item in args.layouts.split(",") if item.strip()]))

    summary_path = output_dir / "summary.md"
    write_summary(summary_path, recommended_rows, best_rows, exported_files)
    exported_files.append(summary_path)
    message(f"已导出结果摘要: {summary_path}")


if __name__ == "__main__":
    main()
