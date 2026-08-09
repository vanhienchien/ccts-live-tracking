"""
build_region_boundaries.py — gộp ranh giới tỉnh (trước sáp nhập 2025) thành
5 khu vực quản lý, xuất 1 file GeoJSON tĩnh để backend/frontend dùng cắt
bản đồ nhiệt theo đúng khu vực (KHÔNG tính lại lúc runtime, vì union polygon
chi tiết khá nặng — chạy 1 lần, commit file kết quả).

Nguồn ranh giới tỉnh: repo nguyenduy1133/Free-GIS-Data (bản trước sáp nhập
2025, 63 tỉnh/thành, có kèm 2 bản ghi trùng tên "Da Nang city" và
"Khanh Hoa" vì file gốc tách riêng phần đất liền và phần quần đảo Hoàng
Sa/Trường Sa — script này CHỈ lấy phần đất liền, bỏ phần quần đảo, để
không kéo bounds/mask ra giữa Biển Đông.

Việc gán tỉnh -> khu vực dựa theo mô tả nghiệp vụ, ranh giới KHÔNG cần
tuyệt đối chính xác ở bước này (theo yêu cầu) — chỉnh sửa REGION_PROVINCES
bên dưới nếu thiếu/sai tỉnh nào.
"""

import json
from shapely.geometry import shape, mapping
from shapely.ops import unary_union

SRC_GEOJSON = "provinces_pre2025.geojson"
OUT_GEOJSON = "region_boundaries.geojson"

# Đơn giản hoá polygon (độ) — ~300-500m, đủ mịn để mask/hiển thị bản đồ,
# nhẹ hơn nhiều so với ranh giới gốc (giảm dung lượng file ~90%).
SIMPLIFY_TOLERANCE = 0.004

# Tên tỉnh dùng ĐÚNG như trong file gốc (không dấu). Sửa danh sách này nếu
# thiếu/thừa tỉnh so với thực tế phân vùng của bạn.
REGION_PROVINCES: dict[str, list[str]] = {
    "Mtay": [
        "An Giang", "Vinh Long", "Dong Thap", "Can Tho city", "Hau Giang",
        "Kien Giang",  # gồm cả Phú Quốc (đảo thuộc Kiên Giang trong polygon gốc)
        "Soc Trang", "Bac Lieu", "Ca Mau", "Tra Vinh",
    ],
    "DNI-BPH": [
        "Dong Nai", "Binh Phuoc",
    ],
    "LDO-BTH": [
        "Lam Dong", "Binh Thuan", "Ninh Thuan",
    ],
    "Tây Nguyên": [
        "Dak Nong", "Dak Lak", "Gia Lai", "Kon Tum",
        "Khanh Hoa", "Phu Yen", "Binh Dinh", "Quang Ngai",
    ],
    "DNA-QNA": [
        "Da Nang city", "Quang Nam",
    ],
}

# index 0 = phần đất liền, index 1 = phần quần đảo Hoàng Sa/Trường Sa —
# loại các polygon có bbox nằm hẳn ngoài khơi (kinh độ > 111 hoặc dải toạ độ
# quá rộng bất thường so với 1 tỉnh) để không kéo mask ra giữa biển.
def _is_offshore_archipelago(geom) -> bool:
    minx, miny, maxx, maxy = geom.bounds
    return minx > 109.0 or (maxx - minx) > 3.0


def main() -> None:
    with open(SRC_GEOJSON, encoding="utf-8") as f:
        src = json.load(f)

    by_name: dict[str, list] = {}
    for feat in src["features"]:
        name = feat["properties"]["Name"]
        geom = shape(feat["geometry"])
        if _is_offshore_archipelago(geom):
            continue
        by_name.setdefault(name, []).append(geom)

    missing_all: list[str] = []
    features_out = []
    for region, provinces in REGION_PROVINCES.items():
        geoms = []
        for p in provinces:
            found = by_name.get(p)
            if not found:
                missing_all.append(f"{region}: '{p}' không tìm thấy trong file gốc")
                continue
            geoms.extend(found)
        if not geoms:
            print(f"[WARN] Khu vực {region} không có tỉnh nào hợp lệ, bỏ qua.")
            continue
        merged = unary_union(geoms).simplify(SIMPLIFY_TOLERANCE, preserve_topology=True)
        features_out.append({
            "type": "Feature",
            "properties": {"region": region, "province_count": len(provinces)},
            "geometry": mapping(merged),
        })
        minx, miny, maxx, maxy = merged.bounds
        print(f"[OK] {region}: {len(provinces)} tỉnh, bbox=({minx:.3f},{miny:.3f})-({maxx:.3f},{maxy:.3f})")

    if missing_all:
        print("\n[CHÚ Ý] Tên tỉnh không khớp (kiểm tra lại chính tả so với file gốc):")
        for m in missing_all:
            print("  -", m)

    out = {"type": "FeatureCollection", "features": features_out}
    with open(OUT_GEOJSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"\nĐã ghi {OUT_GEOJSON} ({len(features_out)} khu vực).")


if __name__ == "__main__":
    main()
