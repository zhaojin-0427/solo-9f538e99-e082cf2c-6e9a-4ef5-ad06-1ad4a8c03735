# -*- coding: utf-8 -*-
"""标记重捕估算系统的端到端测试（Flask test client + 临时 SQLite）。"""

import importlib
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    sys.modules.pop("app", None)
    app_mod = importlib.import_module("app")
    app_mod.app.config["TESTING"] = True
    with app_mod.app.test_client() as c:
        yield c, app_mod


def csv_bytes(text):
    return io.BytesIO(text.encode("utf-8"))


def upload(client, r1, r2):
    return client.post(
        "/upload",
        data={
            "round1_file": (csv_bytes(r1), "round1.csv"),
            "round2_file": (csv_bytes(r2), "round2.csv"),
        },
        content_type="multipart/form-data",
    )


R1 = "tag\nA\nB\nC\nD\n"
R2 = (
    "sample_no,status,candidates\n"
    "s1,有标,A\n"
    "s2,有标,A;B\n"
    "s3,看不清,B;C\n"
    "s4,无标,\n"
    "s5,看不清,\n"
)


def get_est(app_mod):
    conn = app_mod.get_db()
    row = conn.execute("SELECT * FROM estimate WHERE id = 1").fetchone()
    conn.close()
    return dict(row) if row else None


def test_upload_feasible_and_estimate(client):
    c, app_mod = client
    resp = upload(c, R1, R2)
    assert resp.status_code == 302  # 成功 → 回首页
    est = get_est(app_mod)
    assert est["n1"] == 4 and est["n2"] == 5
    # 有标 2 条必配 → 最少重捕 2；s3 可再配 → 最多重捕 3
    assert est["min_recap"] == 2
    assert est["max_recap"] == 3
    # N = 4*5/m：上限 10.0（m=2），下限 20/3（m=3）
    assert est["est_high"] == pytest.approx(10.0)
    assert est["est_low"] == pytest.approx(20 / 3)
    min_pairs = {p["sample_no"]: p["tag"] for p in json.loads(est["min_matching"])}
    assert min_pairs == {"s1": "A", "s2": "B"}
    max_pairs = {p["sample_no"]: p["tag"] for p in json.loads(est["max_matching"])}
    assert len(max_pairs) == 3 and max_pairs["s1"] == "A" and max_pairs["s2"] == "B"


def test_infeasible_upload_keeps_old_state(client):
    c, app_mod = client
    assert upload(c, R1, R2).status_code == 302
    # 两个有标样本抢同一个候选号 → 霍尔证书：2 个样本、1 个候选号
    bad_r2 = (
        "sample_no,status,candidates\n"
        "x1,有标,A\n"
        "x2,有标,A\n"
        "x3,无标,\n"
    )
    resp = upload(c, R1, bad_r2)
    assert resp.status_code == 422
    body = resp.get_data(as_text=True)
    assert "无可行配对" in body and "x1" in body and "x2" in body
    # 旧记录与估算原样保留
    est = get_est(app_mod)
    assert est["n2"] == 5 and est["min_recap"] == 2
    conn = app_mod.get_db()
    names = [r[0] for r in conn.execute("SELECT sample_no FROM round2")]
    conn.close()
    assert names == ["s1", "s2", "s3", "s4", "s5"]


def test_correct_success_bumps_version_and_saves(client):
    c, app_mod = client
    upload(c, R1, R2)
    # s5 由无候选的"看不清"改为候选 D → 最多重捕变 4，下限变 5.0
    resp = c.post(
        "/correct",
        data={"sample_no": "s5", "version": "1",
              "status": "看不清", "candidates": "D"},
    )
    assert resp.status_code == 302
    est = get_est(app_mod)
    assert est["max_recap"] == 4
    assert est["est_low"] == pytest.approx(5.0)
    conn = app_mod.get_db()
    row = conn.execute(
        "SELECT version, candidates FROM round2 WHERE sample_no='s5'"
    ).fetchone()
    conn.close()
    assert row["version"] == 2
    assert json.loads(row["candidates"]) == ["D"]


def test_correct_stale_version_rejected(client):
    c, app_mod = client
    upload(c, R1, R2)
    c.post("/correct", data={"sample_no": "s5", "version": "1",
                             "status": "看不清", "candidates": "D"})
    # 用旧版号再次提交 → 过期，估算不变
    resp = c.post(
        "/correct",
        data={"sample_no": "s5", "version": "1",
              "status": "无标", "candidates": ""},
    )
    assert resp.status_code == 409
    assert "已过期" in resp.get_data(as_text=True)
    est = get_est(app_mod)
    assert est["max_recap"] == 4  # 仍是第一次更正后的结果


def test_correct_to_infeasible_rejected(client):
    c, app_mod = client
    upload(c, R1, R2)
    # 把 s2 的候选改成只有 A（与 s1 冲突）→ 无解，保留旧估算
    resp = c.post(
        "/correct",
        data={"sample_no": "s2", "version": "1",
              "status": "有标", "candidates": "A"},
    )
    assert resp.status_code == 422
    body = resp.get_data(as_text=True)
    assert "无可行配对" in body and "s1" in body and "s2" in body
    est = get_est(app_mod)
    assert est["min_recap"] == 2 and est["max_recap"] == 3


def test_zero_recapture_marked_unestimable(client):
    c, app_mod = client
    r2 = "sample_no,status,candidates\ns1,无标,\ns2,看不清,\n"
    assert upload(c, R1, r2).status_code == 302
    est = get_est(app_mod)
    assert est["min_recap"] == 0 and est["max_recap"] == 0
    assert est["est_high"] is None and est["est_low"] is None
    resp = c.get("/")
    assert "不可估" in resp.get_data(as_text=True)


def test_persistence_across_restart(client, tmp_path, monkeypatch):
    c, app_mod = client
    upload(c, R1, R2)
    c.post("/correct", data={"sample_no": "s5", "version": "1",
                             "status": "看不清", "candidates": "D"})
    # 模拟重启：同一 DB 路径重新导入应用
    sys.modules.pop("app", None)
    app_mod2 = importlib.import_module("app")
    app_mod2.app.config["TESTING"] = True
    with app_mod2.app.test_client() as c2:
        body = c2.get("/").get_data(as_text=True)
        assert "s5" in body and "当前估算" in body
    est = get_est(app_mod2)
    assert est["max_recap"] == 4 and est["est_low"] == pytest.approx(5.0)


def test_validation_errors(client):
    c, app_mod = client
    # 无标却带候选号
    resp = upload(c, R1, "sample_no,status,candidates\ns1,无标,A\n")
    assert resp.status_code == 400
    assert "无标" in resp.get_data(as_text=True)
    # 候选号不在首轮
    resp = upload(c, R1, "sample_no,status,candidates\ns1,有标,Z\n")
    assert resp.status_code == 400
    assert "不在首轮" in resp.get_data(as_text=True)
    # 超过 300 条
    big = "tag\n" + "".join(f"T{i}\n" for i in range(301))
    resp = upload(c, big, "sample_no,status,candidates\ns1,无标,\n")
    assert resp.status_code == 400
    assert "1~300" in resp.get_data(as_text=True)
    # 首轮标记号重复
    resp = upload(c, "tag\nA\nA\n", "sample_no,status,candidates\ns1,无标,\n")
    assert resp.status_code == 400
    assert "不唯一" in resp.get_data(as_text=True)
    # 全部失败 → 数据库仍为空
    assert get_est(app_mod) is None


def test_max_rows_boundary(client):
    c, app_mod = client
    r1 = "tag\n" + "".join(f"T{i}\n" for i in range(300))
    r2 = "sample_no,status,candidates\n" + "".join(
        f"s{i},有标,T{i}\n" for i in range(300)
    )
    assert upload(c, r1, r2).status_code == 302
    est = get_est(app_mod)
    assert est["min_recap"] == 300 and est["max_recap"] == 300
    assert est["est_high"] == pytest.approx(300.0)
