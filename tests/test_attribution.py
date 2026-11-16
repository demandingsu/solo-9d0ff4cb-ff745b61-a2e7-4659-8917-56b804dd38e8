"""两期生成结果归因：纯逻辑 + API 集成测试。"""
from __future__ import annotations

import pytest

from app import allocation as alloc
from app.allocation import (
    AttributionCoverageError,
    AttributionDistrictMismatchError,
    NewDistrict,
    OldCell,
    attribute_periods,
)
from tests.conftest import poll_generation


# ---------- 纯逻辑 ----------

def _row_grid(households: list[int]) -> list[OldCell]:
    """宽度 1 的连续旧格，拼成 [0, n) x [0, 1)。"""
    return [
        OldCell(f"g{i}", (i, 0, i + 1, 1), h) for i, h in enumerate(households)
    ]


def _row_districts(cuts: list[int], width: int) -> list[NewDistrict]:
    bounds = [0, *cuts, width]
    return [
        NewDistrict(f"d{i + 1}", (bounds[i], 0, bounds[i + 1], 1))
        for i in range(len(bounds) - 1)
    ]


def test_attribution_no_change():
    old = _row_grid([3, 5])
    districts = _row_districts([1], 2)
    result = attribute_periods(old, districts, old, districts)
    assert result.total_before == result.total_after == 8
    for row in result.districts:
        assert row.change == 0
        assert row.old_grid_half_units == 0
        assert row.new_boundary_half_units == 0


def test_attribution_half_household_values_and_conservation():
    # 前一期：d1=[0,3) 得 5+2+6=13，d2=[3,4) 得 0
    # 后一期：d1=[0,1) 得 1，d2=[1,4) 得 8+1+5=14
    old_a = _row_grid([5, 2, 6, 0])
    new_a = _row_districts([3], 4)
    old_b = _row_grid([1, 8, 1, 5])
    new_b = _row_districts([1], 4)

    result = attribute_periods(old_a, new_a, old_b, new_b)
    by_id = {r.district_id: r for r in result.districts}

    assert (by_id["d1"].before_households, by_id["d1"].after_households) == (13, 1)
    assert (by_id["d2"].before_households, by_id["d2"].after_households) == (0, 14)

    # 先换旧网格：C2 = 分摊(old_b, new_a) = (10, 5)
    #   d1: old=-3, boundary=-9；d2: old=+5, boundary=+9
    # 先换片区：C3 = 分摊(old_a, new_b) = (5, 8)
    #   d1: old=-4, boundary=-8；d2: old=+6, boundary=+8
    d1 = by_id["d1"]
    assert (d1.old_b_with_new_a, d1.old_a_with_new_b) == (10, 5)
    assert d1.old_grid_first == alloc.PathContribution(old_grid=-3, new_boundary=-9)
    assert d1.new_boundary_first == alloc.PathContribution(old_grid=-4, new_boundary=-8)
    # 平均值：(-3 + -4)/2 = -3.5，(-9 + -8)/2 = -8.5
    assert d1.old_grid_half_units == -7
    assert d1.new_boundary_half_units == -17
    assert d1.change == -12

    d2 = by_id["d2"]
    assert (d2.old_b_with_new_a, d2.old_a_with_new_b) == (5, 8)
    assert d2.old_grid_half_units == 11
    assert d2.new_boundary_half_units == 17
    assert d2.change == 14

    # 每个片区两项之和 = 实测差额；汇总 = 两期总户数变化
    for row in result.districts:
        assert row.old_grid_half_units + row.new_boundary_half_units == 2 * row.change
        assert row.change == row.after_households - row.before_households
    assert result.total_before == 13
    assert result.total_after == 15
    assert result.total_change == 2
    assert result.total_old_grid_half_units + result.total_new_boundary_half_units == 4
    assert result.total_old_grid_half_units == 4
    assert result.total_new_boundary_half_units == 0
    assert sum(r.change for r in result.districts) == result.total_change


def test_attribution_pure_old_grid_change_only():
    # 只改旧网格户数，片区不变：金额全部来自旧网格修订
    old_a = _row_grid([2, 4])
    old_b = _row_grid([5, 7])
    districts = _row_districts([1], 2)
    result = attribute_periods(old_a, districts, old_b, districts)
    assert result.total_change == 6
    for row in result.districts:
        assert row.new_boundary_half_units == 0


def test_attribution_rejects_when_cross_allocation_uncovered():
    # 后一期旧网格更宽（多出 x=4..5 的格子），而前一期片区只覆盖 [0,4)
    old_a = _row_grid([2, 3, 4])
    new_a = _row_districts([1, 2], 3)
    old_b = _row_grid([2, 3, 4, 5])
    new_b = _row_districts([1, 3], 4)
    with pytest.raises(AttributionCoverageError, match="先换旧网格"):
        attribute_periods(old_a, new_a, old_b, new_b)


def test_attribution_rejects_when_new_boundary_first_cross_uncovered():
    # 前一期旧网格更宽，只有前一期片区覆盖到 x=2..3；
    # 后一期旧网格较窄，配前一期片区可覆盖，但前一期旧网格配后一期片区会露底。
    old_a = _row_grid([2, 3, 4])
    new_a = [NewDistrict("d1", (0, 0, 2, 1)), NewDistrict("d2", (2, 0, 3, 1))]
    old_b = _row_grid([1, 1])
    new_b = [NewDistrict("d1", (0, 0, 1, 1)), NewDistrict("d2", (1, 0, 2, 1))]
    with pytest.raises(AttributionCoverageError, match="先换新片区"):
        attribute_periods(old_a, new_a, old_b, new_b)


def test_attribution_rejects_district_id_set_mismatch():
    old = _row_grid([2, 3])
    new_a = _row_districts([1], 2)
    new_b = [NewDistrict("e1", (0, 0, 1, 1)), NewDistrict("e2", (1, 0, 2, 1))]
    with pytest.raises(AttributionDistrictMismatchError, match="d1|e1"):
        attribute_periods(old, new_a, old, new_b)


def test_attribution_uses_half_units_without_float_drift():
    # 多个片区同时出现半户贡献时，以整数半户精确相加
    old_a = _row_grid([5, 2, 6, 0])
    new_a = _row_districts([3], 4)
    old_b = _row_grid([1, 8, 1, 5])
    new_b = _row_districts([1], 4)
    result = attribute_periods(old_a, new_a, old_b, new_b)
    total_half = sum(
        r.old_grid_half_units + r.new_boundary_half_units for r in result.districts
    )
    assert total_half == 2 * result.total_change


def test_attribution_is_order_antisymmetric():
    """交换两期顺序后，每项贡献与前后户数都取反。"""
    old = _row_grid([5, 2, 6, 0])
    districts = _row_districts([3], 4)
    old_b = _row_grid([1, 8, 1, 5])
    districts_b = _row_districts([1], 4)
    forward = attribute_periods(old, districts, old_b, districts_b)
    backward = attribute_periods(old_b, districts_b, old, districts)
    fwd = {r.district_id: r for r in forward.districts}
    bwd = {r.district_id: r for r in backward.districts}
    assert forward.total_before == backward.total_after
    assert forward.total_after == backward.total_before
    for d_id in fwd:
        assert fwd[d_id].before_households == bwd[d_id].after_households
        assert fwd[d_id].after_households == bwd[d_id].before_households
        assert fwd[d_id].old_grid_half_units == -bwd[d_id].old_grid_half_units
        assert fwd[d_id].new_boundary_half_units == -bwd[d_id].new_boundary_half_units


# ---------- API ----------

OLD_A = {
    "items": [
        {"id": "g1", "rect": [0, 0, 10, 10], "households": 7},
        {"id": "g2", "rect": [0, 10, 10, 20], "households": 11},
    ]
}
NEW_A = {
    "items": [
        {"id": "d1", "rect": [0, 0, 5, 20]},
        {"id": "d2", "rect": [5, 0, 10, 20]},
    ]
}
OLD_B = {
    "items": [
        {"id": "g1", "rect": [0, 0, 10, 10], "households": 9},
        {"id": "g2", "rect": [0, 10, 10, 20], "households": 13},
    ]
}
NEW_B = {
    "items": [
        {"id": "d1", "rect": [0, 0, 4, 20]},
        {"id": "d2", "rect": [4, 0, 10, 20]},
    ]
}


def _submit(client, path, payload):
    r = client.post(path, json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _generate(client) -> dict:
    r = client.post("/generations")
    assert r.status_code == 202, r.text
    gen = poll_generation(client, r.json()["id"])
    assert gen["status"] == "succeeded", gen
    return gen


def _setup_two_generations(client):
    _submit(client, "/old-grids", OLD_A)
    _submit(client, "/new-districts", NEW_A)
    gen_a = _generate(client)
    _submit(client, "/old-grids", OLD_B)
    _submit(client, "/new-districts", NEW_B)
    gen_b = _generate(client)
    return gen_a, gen_b


def test_attribution_api_happy_path(client):
    gen_a, gen_b = _setup_two_generations(client)
    before_published = client.get("/result").json()

    r = client.get(
        "/attribution", params={"from": gen_a["id"], "to": gen_b["id"]}
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["generation_a"] == gen_a["id"]
    assert body["generation_b"] == gen_b["id"]
    assert (body["old_version_a"], body["new_version_a"]) == (
        gen_a["old_version"],
        gen_a["new_version"],
    )
    assert (body["old_version_b"], body["new_version_b"]) == (
        gen_b["old_version"],
        gen_b["new_version"],
    )
    assert body["total_before"] == 18
    assert body["total_after"] == 22
    assert body["total_change"] == 4

    by_id = {d["district_id"]: d for d in body["districts"]}
    assert set(by_id) == {"d1", "d2"}
    for row in body["districts"]:
        # 前后户数 + 实测差额
        assert row["change"] == row["after_households"] - row["before_households"]
        # 两项之和 = 实测差额（允许半户）
        assert row["old_grid_change"] + row["new_boundary_change"] == row["change"]
        # 平均值恰为两条路径贡献的中点
        assert (
            row["old_grid_change"]
            == (row["old_grid_first"]["old_grid_change"]
                + row["new_boundary_first"]["old_grid_change"]) / 2
        )
        assert (
            row["new_boundary_change"]
            == (row["new_boundary_first"]["new_boundary_change"]
                + row["old_grid_first"]["new_boundary_change"]) / 2
        )
    assert by_id["d1"]["before_households"] == 10
    assert by_id["d1"]["after_households"] == 9
    assert by_id["d2"]["before_households"] == 8
    assert by_id["d2"]["after_households"] == 13
    assert (by_id["d1"]["old_grid_change"], by_id["d1"]["new_boundary_change"]) == (
        2.0,
        -3.0,
    )
    assert (by_id["d2"]["old_grid_change"], by_id["d2"]["new_boundary_change"]) == (
        2.0,
        3.0,
    )

    # 汇总与两期总户数变化一致
    assert sum(d["change"] for d in body["districts"]) == body["total_change"]
    assert (
        body["total_old_grid_change"] + body["total_new_boundary_change"]
        == body["total_change"]
    )

    # 归因是只读查询：不改变已发布结果
    assert client.get("/result").json() == before_published


def test_attribution_api_swapped_order_negates_contributions(client):
    gen_a, gen_b = _setup_two_generations(client)
    forward = client.get(
        "/attribution", params={"from": gen_a["id"], "to": gen_b["id"]}
    ).json()
    backward = client.get(
        "/attribution", params={"from": gen_b["id"], "to": gen_a["id"]}
    ).json()
    assert backward["total_before"] == forward["total_after"]
    assert backward["total_after"] == forward["total_before"]
    fwd = {d["district_id"]: d for d in forward["districts"]}
    bwd = {d["district_id"]: d for d in backward["districts"]}
    for d_id in fwd:
        assert bwd[d_id]["before_households"] == fwd[d_id]["after_households"]
        assert bwd[d_id]["after_households"] == fwd[d_id]["before_households"]
        assert bwd[d_id]["old_grid_change"] == -fwd[d_id]["old_grid_change"]
        assert bwd[d_id]["new_boundary_change"] == -fwd[d_id]["new_boundary_change"]


def test_attribution_pins_task_versions_after_newer_submissions(client):
    gen_a, gen_b = _setup_two_generations(client)
    # 归因之后又发布一套新输入与结果；旧任务对比应保持不变
    _submit(client, "/old-grids", {"items": [
        {"id": "z", "rect": [0, 0, 10, 20], "households": 100}
    ]})
    _submit(client, "/new-districts", {"items": [
        {"id": "d1", "rect": [0, 0, 5, 20]},
        {"id": "d2", "rect": [5, 0, 10, 20]},
    ]})
    _generate(client)

    first = client.get(
        "/attribution", params={"from": gen_a["id"], "to": gen_b["id"]}
    ).json()
    assert (first["old_version_a"], first["old_version_b"]) == (
        gen_a["old_version"],
        gen_b["old_version"],
    )
    assert first["total_before"] == 18 and first["total_after"] == 22
    # 新发布的版本号更大，但未进入本次对比
    assert first["old_version_b"] < max(
        v["version"] for v in client.get("/versions", params={"kind": "old"}).json()
    )


def test_attribution_rejects_failed_and_missing_generations(client):
    gen_a, _ = _setup_two_generations(client)
    # 提交一个无法完整覆盖的片区版本，生成失败
    _submit(client, "/new-districts", {"items": [
        {"id": "d1", "rect": [0, 0, 10, 10]}
    ]})
    r = client.post("/generations")
    failed = poll_generation(client, r.json()["id"])
    assert failed["status"] == "failed"

    published = client.get("/result").json()

    # 失败任务不参与归因（409），且失败不影响当前结果
    r = client.get(
        "/attribution", params={"from": gen_a["id"], "to": failed["id"]}
    )
    assert r.status_code == 409
    assert "只有成功" in r.json()["detail"]
    assert client.get("/result").json() == published

    # 不存在的任务 -> 404
    assert client.get(
        "/attribution", params={"from": gen_a["id"], "to": 9999}
    ).status_code == 404
    # 同一任务 -> 422
    assert client.get(
        "/attribution", params={"from": gen_a["id"], "to": gen_a["id"]}
    ).status_code == 422
    # 参数缺失 -> 422
    assert client.get("/attribution").status_code == 422


def test_attribution_rejects_mismatched_district_sets(client):
    gen_a, _ = _setup_two_generations(client)
    _submit(client, "/new-districts", {"items": [
        {"id": "x1", "rect": [0, 0, 5, 20]},
        {"id": "x2", "rect": [5, 0, 10, 20]},
    ]})
    gen_b = _generate(client)
    r = client.get(
        "/attribution", params={"from": gen_a["id"], "to": gen_b["id"]}
    )
    assert r.status_code == 422
    assert "编号集合不同" in r.json()["detail"]


def test_attribution_rejects_incomplete_cross_coverage(client):
    gen_a, _ = _setup_two_generations(client)
    # 新片区保持不变（d1/d2 覆盖 [0,10]x[0,20]），但旧网格向右扩出一列
    _submit(client, "/old-grids", {"items": [
        {"id": "g1", "rect": [0, 0, 10, 10], "households": 9},
        {"id": "g2", "rect": [0, 10, 10, 20], "households": 13},
        {"id": "g3", "rect": [10, 0, 12, 20], "households": 4},
    ]})
    _submit(client, "/new-districts", {"items": [
        {"id": "d1", "rect": [0, 0, 4, 20]},
        {"id": "d2", "rect": [4, 0, 12, 20]},
    ]})
    gen_b = _generate(client)

    # C2 = 旧网格 B + 新片区 A：g3 未被覆盖 -> 拒绝
    r = client.get(
        "/attribution", params={"from": gen_a["id"], "to": gen_b["id"]}
    )
    assert r.status_code == 422
    assert "完整覆盖" in r.json()["detail"]
    # 当前发布结果不受影响
    assert client.get("/result").json()["generation_id"] == gen_b["id"]


def test_attribution_query_is_stable_during_new_publish(client, monkeypatch):
    """对比读取进行中发生新发布，本次结果仍按任务固定版本计算。"""
    gen_a, gen_b = _setup_two_generations(client)
    before_published = client.get("/result").json()

    import app.db as dbmod

    original = dbmod._load_comparable_snapshot
    calls = {"n": 0}

    def snapshot_with_publish(conn, gen_id):
        snap = original(conn, gen_id)
        calls["n"] += 1
        if calls["n"] == 1:
            # 模拟“查询期间有新发布”：随后再生成并发布一套完全不同的结果
            _submit(client, "/old-grids", {"items": [
                {"id": "q", "rect": [0, 0, 10, 20], "households": 1000}
            ]})
            _submit(client, "/new-districts", {"items": [
                {"id": "d1", "rect": [0, 0, 5, 20]},
                {"id": "d2", "rect": [5, 0, 10, 20]},
            ]})
            _generate(client)
        return snap

    monkeypatch.setattr(dbmod, "_load_comparable_snapshot", snapshot_with_publish)
    body = client.get(
        "/attribution", params={"from": gen_a["id"], "to": gen_b["id"]}
    ).json()

    assert body["total_before"] == 18
    assert body["total_after"] == 22
    assert body["old_version_a"] == gen_a["old_version"]
    assert body["old_version_b"] == gen_b["old_version"]
    # 新发布确实已经切换，但没有污染本次归因
    assert client.get("/result").json() != before_published
