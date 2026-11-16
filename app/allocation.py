"""纯函数计算模块：矩形几何、覆盖校验、最大余数法分摊。

本模块不触碰数据库与 Web 层，便于单元测试。
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

# 矩形表示为 (x1, y1, x2, y2)，均为整数且 x1<x2、y1<y2。
RectTuple = tuple[int, int, int, int]


@dataclass(frozen=True)
class OldCell:
    id: str
    rect: RectTuple
    households: int


@dataclass(frozen=True)
class NewDistrict:
    id: str
    rect: RectTuple


def rect_area(rect: RectTuple) -> int:
    x1, y1, x2, y2 = rect
    return (x2 - x1) * (y2 - y1)


def overlap_area(a: RectTuple, b: RectTuple) -> int:
    """两个轴对齐矩形的重叠面积（不相交返回 0）。"""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    w = min(ax2, bx2) - max(ax1, bx1)
    h = min(ay2, by2) - max(ay1, by1)
    if w <= 0 or h <= 0:
        return 0
    return w * h


def assert_no_internal_overlap(rects: list[tuple[str, RectTuple]]) -> None:
    """同版条目内部不得重叠（边界相接允许）。

    rects: (编号, 矩形) 列表，保持提交顺序用于报错信息。
    """
    for i in range(len(rects)):
        id_a, rect_a = rects[i]
        for j in range(i + 1, len(rects)):
            id_b, rect_b = rects[j]
            if overlap_area(rect_a, rect_b) > 0:
                raise ValueError(f"条目 {id_a} 与 {id_b} 的矩形存在重叠区域")


def assert_full_coverage(old_cells: list[OldCell], districts: list[NewDistrict]) -> None:
    """任一旧格未被片区完整覆盖则拒绝。

    新片区之间互不重叠，因此对每个旧格累加与所有片区的重叠面积，
    必须恰好等于旧格自身面积。片区可以延伸到旧网格之外。
    """
    for cell in old_cells:
        covered = sum(overlap_area(cell.rect, d.rect) for d in districts)
        if covered != rect_area(cell.rect):
            raise ValueError(
                f"旧格 {cell.id} 未被新片区完整覆盖："
                f"面积 {rect_area(cell.rect)}，被覆盖 {covered}"
            )


def _id_sort_key(id_: str):
    """片区编号升序：可解析为整数的按数值升序，否则按字符串升序。"""
    try:
        return (0, int(id_), "")
    except ValueError:
        return (1, 0, id_)


def largest_remainder_share(weights: list[tuple[str, int]], total: int) -> dict[str, int]:
    """最大余数法把整数 total 按权重分摊。

    weights: (编号, 权重/重叠面积) 列表，权重均非负且至少一个为正。
    余数相同时按片区编号升序（_id_sort_key）决定优先获得 +1。
    返回 编号 -> 分摊户数，守恒于 total。
    """
    weight_sum = sum(w for _, w in weights)
    if weight_sum <= 0:
        raise ValueError("权重之和必须为正")

    floors: dict[str, int] = {}
    # 用精确分数表示余数，避免浮点误差导致“余数并列”判定不确定
    remainders: dict[str, Fraction] = {}
    for id_, w in weights:
        exact = Fraction(total * w, weight_sum)
        floors[id_] = exact.numerator // exact.denominator  # 精确向下取整
        remainders[id_] = exact - floors[id_]

    allocated = sum(floors.values())
    leftover = total - allocated
    # 余数降序；余数相同按编号升序
    order = sorted(weights, key=lambda pair: (-remainders[pair[0]], _id_sort_key(pair[0])))
    for id_, _ in order:
        if leftover == 0:
            break
        floors[id_] += 1
        leftover -= 1
    assert leftover == 0
    return floors


@dataclass(frozen=True)
class Allocation:
    district_id: str
    old_id: str
    overlap_area: int
    households: int


# ---------- 两期生成结果归因 ----------
#
# 设 A（前）与 B（后）为两次成功生成：
#   A = 分摊(old_A, new_A)                基准分摊
#   B = 分摊(old_B, new_B)                对比分摊
#   C2 = 分摊(old_B, new_A)               “先换旧网格”的中间折算
#   C3 = 分摊(old_A, new_B)               “先换新片区”的中间折算
#
# 两条折算路径对片区 d 的贡献（均为整数户）：
#   先换旧网格：旧网格 = C2-A，新片区 = B-C2
#   先换新片区：新片区 = C3-A，旧网格 = B-C3
# 两者取算术平均，得到允许半户的贡献；两项之和恒等于 B-A。
#
# 注意：C2/C3 是从两期真实输入交叉组合出来的“反事实”分摊，
# 必须同样满足旧格被片区完整覆盖，否则拒绝归因而不是猜测。

class AttributionDistrictMismatchError(ValueError):
    """两次生成的片区编号集合不一致，无法逐片区归因。"""


class AttributionCoverageError(ValueError):
    """基准或交叉折算缺少完整覆盖，拒绝归因。"""


class AttributionIntegrityError(ValueError):
    """内部对账校验失败（理论上不应发生）。"""


@dataclass(frozen=True)
class PathContribution:
    """某条 change 顺序路径下，片区变动中两项来源的整数户贡献。"""

    old_grid: int
    new_boundary: int


@dataclass(frozen=True)
class DistrictAttribution:
    district_id: str
    before_households: int
    after_households: int
    old_grid_half_units: int
    new_boundary_half_units: int
    old_grid_first: PathContribution
    new_boundary_first: PathContribution
    old_b_with_new_a: int
    old_a_with_new_b: int

    @property
    def change(self) -> int:
        return self.after_households - self.before_households


@dataclass(frozen=True)
class AttributionResult:
    district_ids: tuple[str, ...]
    districts: tuple[DistrictAttribution, ...]
    total_before: int
    total_after: int
    total_old_grid_half_units: int
    total_new_boundary_half_units: int

    @property
    def total_change(self) -> int:
        return self.total_after - self.total_before


def _district_totals(
    allocations: list[Allocation], district_ids: set[str]
) -> dict[str, int]:
    totals = {d_id: 0 for d_id in district_ids}
    for a in allocations:
        if a.district_id not in totals:  # 防御性检查：不会发生
            raise AttributionIntegrityError(
                f"分摊结果出现未知片区 {a.district_id}"
            )
        totals[a.district_id] += a.households
    return totals


def _allocate_or_raise(
    old_cells: list[OldCell], districts: list[NewDistrict], label: str
) -> list[Allocation]:
    try:
        return allocate(old_cells, districts)
    except ValueError as exc:
        raise AttributionCoverageError(f"{label}：{exc}") from exc


def attribute_periods(
    old_cells_a: list[OldCell],
    districts_a: list[NewDistrict],
    old_cells_b: list[OldCell],
    districts_b: list[NewDistrict],
) -> AttributionResult:
    """按两条折算路径的平均值，把两期片区户数差额拆成两项来源。

    - 只接受片区编号集合完全相同的两期；
    - 以传入的（任务各自固定的）旧网格 / 新片区版本为准，不读取“当前版本”；
    - 交叉折算 C2、C3 任一缺少完整覆盖即抛 AttributionCoverageError；
    - 每个片区 old_grid + new_boundary = 实测差额（半户为单位精确对账）；
    - 全部片区贡献之和 = 两期总户数变化。
    """
    ids_a = {d.id for d in districts_a}
    ids_b = {d.id for d in districts_b}
    if ids_a != ids_b:
        only_a = sorted(ids_a - ids_b, key=_id_sort_key)
        only_b = sorted(ids_b - ids_a, key=_id_sort_key)
        parts = []
        if only_a:
            parts.append(f"仅前一期有 {only_a}")
        if only_b:
            parts.append(f"仅后一期有 {only_b}")
        raise AttributionDistrictMismatchError(
            "两次生成的片区编号集合不同，无法逐片区归因：" + "；".join(parts)
        )

    district_ids = ids_a

    # 基准期 / 对比期（任务本身已成功，正常不会触发覆盖错误）
    totals_a = _district_totals(
        _allocate_or_raise(old_cells_a, districts_a, "前一期基准分摊失败"),
        district_ids,
    )
    totals_b = _district_totals(
        _allocate_or_raise(old_cells_b, districts_b, "后一期基准分摊失败"),
        district_ids,
    )

    # 交叉折算 1：先换旧网格（旧网格 B + 新片区 A）
    totals_old_first_mid = _district_totals(
        _allocate_or_raise(
            old_cells_b,
            districts_a,
            "交叉折算「先换旧网格」缺少完整覆盖（旧网格 B 配新片区 A）",
        ),
        district_ids,
    )
    # 交叉折算 2：先换新片区（旧网格 A + 新片区 B）
    totals_new_first_mid = _district_totals(
        _allocate_or_raise(
            old_cells_a,
            districts_b,
            "交叉折算「先换新片区」缺少完整覆盖（旧网格 A 配新片区 B）",
        ),
        district_ids,
    )

    rows: list[DistrictAttribution] = []
    for d_id in sorted(district_ids, key=_id_sort_key):
        before = totals_a[d_id]
        after = totals_b[d_id]
        c2 = totals_old_first_mid[d_id]
        c3 = totals_new_first_mid[d_id]

        old_grid_first = PathContribution(
            old_grid=c2 - before,
            new_boundary=after - c2,
        )
        new_boundary_first = PathContribution(
            old_grid=after - c3,
            new_boundary=c3 - before,
        )
        # 两条路径贡献的平均值；以“半户”为单位保存，避免浮点误差
        old_grid_half_units = (
            old_grid_first.old_grid + new_boundary_first.old_grid
        )
        new_boundary_half_units = (
            old_grid_first.new_boundary + new_boundary_first.new_boundary
        )
        change = after - before
        if old_grid_half_units + new_boundary_half_units != 2 * change:
            raise AttributionIntegrityError(
                f"片区 {d_id} 两项贡献之和 {old_grid_half_units / 2}"
                f" + {new_boundary_half_units / 2} 不等于实测差额 {change}"
            )

        rows.append(
            DistrictAttribution(
                district_id=d_id,
                before_households=before,
                after_households=after,
                old_grid_half_units=old_grid_half_units,
                new_boundary_half_units=new_boundary_half_units,
                old_grid_first=old_grid_first,
                new_boundary_first=new_boundary_first,
                old_b_with_new_a=c2,
                old_a_with_new_b=c3,
            )
        )

    total_before = sum(totals_a.values())
    total_after = sum(totals_b.values())
    total_old_half = sum(r.old_grid_half_units for r in rows)
    total_new_half = sum(r.new_boundary_half_units for r in rows)
    if total_old_half + total_new_half != 2 * (total_after - total_before):
        raise AttributionIntegrityError(
            "两项贡献汇总不等于两期总户数变化："
            f"{total_old_half / 2} + {total_new_half / 2}"
            f" != {total_after - total_before}"
        )
    if sum(r.change for r in rows) != total_after - total_before:
        raise AttributionIntegrityError("各片区实测差额汇总与总户数变化不一致")

    return AttributionResult(
        district_ids=tuple(sorted(district_ids, key=_id_sort_key)),
        districts=tuple(rows),
        total_before=total_before,
        total_after=total_after,
        total_old_grid_half_units=total_old_half,
        total_new_boundary_half_units=total_new_half,
    )


def allocate(
    old_cells: list[OldCell], districts: list[NewDistrict]
) -> list[Allocation]:
    """按重叠面积逐格分摊户数。

    每个旧格独立分摊其整数户数到与其相交的片区：
    先按面积比例取整，再用最大余数法分配余数，
    余数并列时按片区编号升序。所有旧格户数之和在结果中守恒。
    """
    assert_full_coverage(old_cells, districts)
    results: list[Allocation] = []

    for cell in old_cells:
        weights = [
            (d.id, area)
            for d in districts
            if (area := overlap_area(cell.rect, d.rect)) > 0
        ]
        if not weights:
            # 覆盖校验已排除这种情况，保留防御性分支
            raise ValueError(f"旧格 {cell.id} 没有任何重叠片区")
        shares = largest_remainder_share(weights, cell.households)
        # 明细按片区编号升序输出，结果稳定
        for district_id in sorted(shares, key=_id_sort_key):
            area = dict(weights)[district_id]
            results.append(
                Allocation(
                    district_id=district_id,
                    old_id=cell.id,
                    overlap_area=area,
                    households=shares[district_id],
                )
            )
    return results


def summarize(
    allocations: list[Allocation], districts: list[NewDistrict]
) -> list[dict]:
    """汇总每个片区（含零分摊片区），按编号升序。"""
    by_district: dict[str, list[Allocation]] = {d.id: [] for d in districts}
    for a in allocations:
        by_district[a.district_id].append(a)

    summary = []
    for d in sorted(districts, key=lambda x: _id_sort_key(x.id)):
        items = by_district[d.id]
        summary.append(
            {
                "district_id": d.id,
                "allocated_households": sum(a.households for a in items),
                "detail": [
                    {
                        "district_id": a.district_id,
                        "old_id": a.old_id,
                        "overlap_area": a.overlap_area,
                        "households": a.households,
                    }
                    for a in sorted(items, key=lambda a: _id_sort_key(a.old_id))
                ],
            }
        )
    return summary
