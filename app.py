#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""封闭池塘两轮抽样标记重捕估算系统（Flask + SQLite）。

规则：
- 首轮 CSV 登记放回鱼的唯一标记号（1~300 条）。
- 次轮 CSV 每行：唯一样本号、标记状态（有标/无标/看不清）、来自首轮的候选标记号。
- 配对：有标必配、无标不配、看不清可配可不配、每个首轮标记最多用一次。
- 求可行的最少/最多重捕数并各附一套配对（二分图匹配）。
- 鱼数估算 N = 首轮条数 × 次轮条数 ÷ 重捕数，重捕数为 0 的端点标为不可估。
- 次轮记录可按所见版号逐条更正（乐观锁）；过期、无解或写入失败时保留上次记录与估算。
"""

import csv
import io
import json
import os
import re
import sqlite3
import sys
from collections import deque
from datetime import datetime, timezone

from flask import Flask, flash, redirect, render_template, request, url_for

sys.setrecursionlimit(10000)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "markrecap.db"))
MAX_ROWS = 300
MAX_UPLOAD_BYTES = 2 * 1024 * 1024

STATUSES = ("有标", "无标", "看不清")
STATUS_ALIASES = {
    "有标": "有标", "有標": "有标", "tagged": "有标", "y": "有标", "yes": "有标",
    "无标": "无标", "無標": "无标", "untagged": "无标", "n": "无标", "no": "无标",
    "看不清": "看不清", "unclear": "看不清", "unknown": "看不清", "?": "看不清", "未知": "看不清",
}
TAG_HEADERS = ("tag", "标记号", "标记", "tag_no", "mark", "编号")
SAMPLE_HEADERS = ("sample_no", "样本号", "sample", "id", "编号")
STATUS_HEADERS = ("status", "状态", "标记状态")
CAND_HEADERS = ("candidates", "候选号", "候选标记号", "候选", "cand")

SEPARATORS = re.compile(r"[;；|/、，,·\s]+")
FORBIDDEN_IN_TAG = re.compile(r"[;；|/、，,·\s]")

SCHEMA = """
CREATE TABLE IF NOT EXISTS round1 (
    tag TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS round2 (
    sample_no  TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    candidates TEXT NOT NULL DEFAULT '[]',
    version    INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS estimate (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    n1           INTEGER NOT NULL,
    n2           INTEGER NOT NULL,
    min_recap    INTEGER NOT NULL,
    max_recap    INTEGER NOT NULL,
    est_high     REAL,
    est_low      REAL,
    min_matching TEXT NOT NULL,
    max_matching TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
"""

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")


class UploadError(Exception):
    """CSV 内容或更正内容校验失败。"""


# ---------------------------------------------------------------- 数据库

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db():
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def load_records(conn):
    return [
        {
            "sample_no": r["sample_no"],
            "status": r["status"],
            "candidates": json.loads(r["candidates"]),
            "version": r["version"],
        }
        for r in conn.execute(
            "SELECT sample_no, status, candidates, version FROM round2 ORDER BY rowid"
        )
    ]


def load_state():
    conn = get_db()
    try:
        tags = [r[0] for r in conn.execute("SELECT tag FROM round1 ORDER BY rowid")]
        records = load_records(conn)
        row = conn.execute("SELECT * FROM estimate WHERE id = 1").fetchone()
    finally:
        conn.close()
    est = None
    if row is not None:
        est = dict(row)
        est["min_matching"] = json.loads(est["min_matching"])
        est["max_matching"] = json.loads(est["max_matching"])
    return tags, records, est


def save_estimate(conn, n1, n2, sol):
    est_high = None if sol["min_recap"] == 0 else n1 * n2 / sol["min_recap"]
    est_low = None if sol["max_recap"] == 0 else n1 * n2 / sol["max_recap"]
    conn.execute(
        "INSERT OR REPLACE INTO estimate"
        " (id, n1, n2, min_recap, max_recap, est_high, est_low,"
        "  min_matching, max_matching, updated_at)"
        " VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            n1,
            n2,
            sol["min_recap"],
            sol["max_recap"],
            est_high,
            est_low,
            json.dumps(sol["min_matching"], ensure_ascii=False),
            json.dumps(sol["max_matching"], ensure_ascii=False),
            datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        ),
    )


# ---------------------------------------------------------------- 二分图匹配

def hopcroft_karp(adj, left_nodes, match_l=None, match_r=None):
    """Hopcroft-Karp 最大匹配。可传入已有匹配继续增广（用于第二阶段）。"""
    match_l = match_l if match_l is not None else {}
    match_r = match_r if match_r is not None else {}
    for u in left_nodes:
        match_l.setdefault(u, None)
    INF = float("inf")

    def bfs():
        dist = {}
        dq = deque()
        for u in left_nodes:
            if match_l[u] is None:
                dist[u] = 0
                dq.append(u)
            else:
                dist[u] = INF
        found_free = False
        while dq:
            u = dq.popleft()
            for v in adj.get(u, ()):
                w = match_r.get(v)
                if w is None:
                    found_free = True
                elif dist.get(w, INF) == INF:
                    dist[w] = dist[u] + 1
                    dq.append(w)
        return found_free, dist

    def dfs(u, dist):
        for v in adj.get(u, ()):
            w = match_r.get(v)
            if w is None or (dist.get(w, INF) == dist[u] + 1 and dfs(w, dist)):
                match_l[u] = v
                match_r[v] = u
                return True
        dist[u] = INF
        return False

    while True:
        found, dist = bfs()
        if not found:
            break
        for u in left_nodes:
            if match_l[u] is None:
                dfs(u, dist)
    return match_l, match_r


def hall_certificate(adj, left_nodes, match_l, match_r):
    """对最大匹配未覆盖全部有标样本的情形，给出霍尔定理证书：

    返回 (S, N)：S 为一组有标样本，N 为它们的候选号并集，且 |N| < |S|。
    """
    start = [u for u in left_nodes if match_l[u] is None]
    seen_l = set(start)
    seen_r = set()
    dq = deque(start)
    while dq:
        u = dq.popleft()
        for v in adj.get(u, ()):
            if match_l[u] == v or v in seen_r:
                continue
            seen_r.add(v)
            w = match_r.get(v)
            if w is not None and w not in seen_l:
                seen_l.add(w)
                dq.append(w)
    return sorted(seen_l), sorted(seen_r)


def compute_solution(records):
    """返回 dict：feasible；可行时含 min/max 重捕数与两套配对，否则含 hall 证书。"""
    tagged = [r for r in records if r["status"] == "有标"]
    unclear = [r for r in records if r["status"] == "看不清"]

    # 第一阶段：只要求覆盖全部有标样本（最少重捕 = 有标样本数）。
    adj_t = {r["sample_no"]: list(r["candidates"]) for r in tagged}
    left_t = [r["sample_no"] for r in tagged]
    match_l, match_r = hopcroft_karp(adj_t, left_t)
    if any(match_l[u] is None for u in left_t):
        s_set, n_set = hall_certificate(adj_t, left_t, match_l, match_r)
        return {
            "feasible": False,
            "hall": {
                "samples": [
                    {"sample_no": u, "candidates": adj_t[u]} for u in s_set
                ],
                "union": n_set,
                "sample_count": len(s_set),
                "union_count": len(n_set),
            },
        }

    min_pairs = {u: match_l[u] for u in left_t}

    # 第二阶段：在保留全部有标配对的前提下，让看不清样本尽量增广（最多重捕）。
    full_adj = dict(adj_t)
    left_all = list(left_t)
    for r in unclear:
        full_adj[r["sample_no"]] = list(r["candidates"])
        left_all.append(r["sample_no"])
    match_l2, _ = hopcroft_karp(full_adj, left_all, match_l, match_r)

    min_matching = [
        {"sample_no": u, "tag": min_pairs[u]} for u in sorted(min_pairs)
    ]
    max_matching = [
        {"sample_no": u, "tag": match_l2[u]}
        for u in sorted(left_all)
        if match_l2[u] is not None
    ]
    return {
        "feasible": True,
        "min_recap": len(left_t),
        "max_recap": len(max_matching),
        "min_matching": min_matching,
        "max_matching": max_matching,
    }


# ---------------------------------------------------------------- CSV 解析

def read_text_upload(fs):
    data = fs.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError("文件过大（上限 2MB）。")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise UploadError("文件编码需为 UTF-8。")


def _rows(text):
    reader = csv.reader(io.StringIO(text))
    return [row for row in reader if any(cell.strip() for cell in row)]


def _find_col(header, names):
    lowered = [h.strip().lower() for h in header]
    for name in names:
        if name.lower() in lowered:
            return lowered.index(name.lower())
    return None


def parse_round1(text):
    rows = _rows(text)
    if not rows:
        raise UploadError("首轮 CSV 为空。")
    idx = _find_col(rows[0], TAG_HEADERS)
    if idx is None:
        raise UploadError("首轮 CSV 表头需包含 tag（或：标记号 / 标记 / 编号）。")
    tags = []
    for lineno, row in enumerate(rows[1:], start=2):
        if idx >= len(row) or not row[idx].strip():
            raise UploadError(f"首轮第 {lineno} 行缺少标记号。")
        tag = row[idx].strip()
        if FORBIDDEN_IN_TAG.search(tag):
            raise UploadError(
                f"首轮第 {lineno} 行标记号 {tag!r} 含非法字符（空格或 ;；|/、，,）。"
            )
        tags.append(tag)
    if not 1 <= len(tags) <= MAX_ROWS:
        raise UploadError(f"首轮条数须为 1~{MAX_ROWS}，实际 {len(tags)} 条。")
    seen, dups = set(), set()
    for t in tags:
        if t in seen:
            dups.add(t)
        seen.add(t)
    if dups:
        sample = "、".join(sorted(dups)[:5])
        raise UploadError(f"首轮标记号不唯一：{sample}{' 等' if len(dups) > 5 else ''}。")
    return tags


def normalize_status(raw, lineno=None):
    text = raw.strip()
    if text in STATUS_ALIASES:
        return STATUS_ALIASES[text]
    if text.lower() in STATUS_ALIASES:
        return STATUS_ALIASES[text.lower()]
    where = f"第 {lineno} 行" if lineno else "该记录"
    raise UploadError(
        f"次轮{where}状态 {raw!r} 无法识别，应为：有标 / 无标 / 看不清。"
    )


def validate_candidates(raw, tagset, status, lineno=None):
    cands = []
    for tok in SEPARATORS.split(raw.strip()):
        if tok and tok not in cands:
            cands.append(tok)
    where = f"第 {lineno} 行" if lineno else "该记录"
    unknown = [c for c in cands if c not in tagset]
    if unknown:
        raise UploadError(
            f"次轮{where}候选号 {'、'.join(unknown[:5])} 不在首轮标记号中。"
        )
    if status == "无标" and cands:
        raise UploadError(f"次轮{where}状态为无标，按规则不应填写候选号。")
    return cands


def parse_round2(text, tagset):
    rows = _rows(text)
    if not rows:
        raise UploadError("次轮 CSV 为空。")
    header = rows[0]
    i_no = _find_col(header, SAMPLE_HEADERS)
    i_st = _find_col(header, STATUS_HEADERS)
    i_ca = _find_col(header, CAND_HEADERS)
    if i_no is None or i_st is None or i_ca is None:
        raise UploadError(
            "次轮 CSV 表头需包含 sample_no,status,candidates"
            "（或：样本号 / 状态 / 候选号）。"
        )
    records = []
    seen = set()
    for lineno, row in enumerate(rows[1:], start=2):
        def cell(i):
            return row[i].strip() if i < len(row) else ""

        sample_no = cell(i_no)
        if not sample_no:
            raise UploadError(f"次轮第 {lineno} 行缺少样本号。")
        if sample_no in seen:
            raise UploadError(f"次轮样本号重复：{sample_no}。")
        seen.add(sample_no)
        status = normalize_status(cell(i_st), lineno)
        cands = validate_candidates(cell(i_ca), tagset, status, lineno)
        records.append(
            {"sample_no": sample_no, "status": status, "candidates": cands}
        )
    if not 1 <= len(records) <= MAX_ROWS:
        raise UploadError(f"次轮条数须为 1~{MAX_ROWS}，实际 {len(records)} 条。")
    return records


# ---------------------------------------------------------------- 页面

def render_index(**extra):
    tags, records, est = load_state()
    return render_template(
        "index.html",
        tags=tags,
        records=records,
        est=est,
        statuses=STATUSES,
        **extra,
    )


@app.route("/")
def index():
    return render_index()


@app.route("/upload", methods=["POST"])
def upload():
    try:
        f1 = request.files.get("round1_file")
        f2 = request.files.get("round2_file")
        if f1 is None or f2 is None or not f1.filename or not f2.filename:
            raise UploadError("请同时选择首轮与次轮两个 CSV 文件。")
        tags = parse_round1(read_text_upload(f1))
        records = parse_round2(read_text_upload(f2), set(tags))
        sol = compute_solution(records)
    except UploadError as exc:
        return render_index(error=str(exc)), 400

    if not sol["feasible"]:
        return (
            render_index(
                error="次轮数据无可行配对，本次上传未保存，仍保留上次记录与估算。",
                hall=sol["hall"],
            ),
            422,
        )

    try:
        conn = get_db()
        try:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM round1")
            conn.execute("DELETE FROM round2")
            conn.executemany(
                "INSERT INTO round1(tag) VALUES (?)", [(t,) for t in tags]
            )
            conn.executemany(
                "INSERT INTO round2(sample_no, status, candidates, version)"
                " VALUES (?, ?, ?, 1)",
                [
                    (
                        r["sample_no"],
                        r["status"],
                        json.dumps(r["candidates"], ensure_ascii=False),
                    )
                    for r in records
                ],
            )
            save_estimate(conn, len(tags), len(records), sol)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except sqlite3.Error as exc:
        app.logger.exception("上传写入失败")
        return render_index(error=f"写入失败（{exc}），已保留上次记录与估算。"), 500

    flash("上传成功，已保存新估算。", "ok")
    return redirect(url_for("index"))


@app.route("/correct", methods=["POST"])
def correct():
    sample_no = (request.form.get("sample_no") or "").strip()
    version_seen = (request.form.get("version") or "").strip()
    status_raw = request.form.get("status") or ""
    cand_raw = request.form.get("candidates") or ""

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM round2 WHERE sample_no = ?", (sample_no,)
        ).fetchone()
        if row is None:
            return (
                render_index(
                    error=f"次轮记录 {sample_no!r} 不存在（可能已重新上传），"
                    "请刷新后重试；已保留上次记录与估算。"
                ),
                409,
            )
        if str(row["version"]) != version_seen:
            return (
                render_index(
                    error=f"记录 {sample_no} 已过期：所见版号为 "
                    f"{version_seen or '（空）'}，当前版号为 {row['version']}。"
                    "本次更正未保存，已保留上次记录与估算。"
                ),
                409,
            )
        try:
            status = normalize_status(status_raw)
            tags = [r[0] for r in conn.execute("SELECT tag FROM round1")]
            cands = validate_candidates(cand_raw, set(tags), status)
        except UploadError as exc:
            return render_index(error=str(exc)), 400

        records = load_records(conn)
        for r in records:
            if r["sample_no"] == sample_no:
                r["status"] = status
                r["candidates"] = cands
        sol = compute_solution(records)
        if not sol["feasible"]:
            return (
                render_index(
                    error="更正后无可行配对，本次更正未保存，仍保留上次记录与估算。",
                    hall=sol["hall"],
                ),
                422,
            )

        try:
            conn.execute("BEGIN")
            cur = conn.execute(
                "UPDATE round2 SET status = ?, candidates = ?,"
                " version = version + 1"
                " WHERE sample_no = ? AND version = ?",
                (
                    status,
                    json.dumps(cands, ensure_ascii=False),
                    sample_no,
                    row["version"],
                ),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return (
                    render_index(
                        error=f"记录 {sample_no} 已过期（并发修改），"
                        "本次更正未保存，已保留上次记录与估算。"
                    ),
                    409,
                )
            save_estimate(conn, len(tags), len(records), sol)
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            raise
    except sqlite3.Error as exc:
        app.logger.exception("更正写入失败")
        return render_index(error=f"写入失败（{exc}），已保留上次记录与估算。"), 500
    finally:
        conn.close()

    flash(
        f"记录 {sample_no} 更正成功（新版号 {row['version'] + 1}），已保存新估算。",
        "ok",
    )
    return redirect(url_for("index"))


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
