#!/usr/bin/env python3
"""关键流程自测：上传、汇总口径、拆种/并种去重、复核确认、整份换对照、
分组小计维护、升级重建、写入失败回滚。

运行：python3 tests/selftest.py
退出码 0 = 全部通过。
"""

import io
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from birdapp import create_app  # noqa: E402

MAPPING = """起版,旧学名,下一版学名
1,Larus argentatus,Larus argentatus
1,Larus argentatus,Larus michahellis
2,Larus michahellis,Larus argentatus
1,Netta rufina,Netta rufina
1,Netta rufina,Netta erythrophthalma
"""

CENSUS = """记录号,年份,名录版,学名,只数
R1,2020,1,Netta rufina,10
R2,2020,2,Netta rufina,5
R3,2021,1,Larus argentatus,7
R4,2021,2,Larus michahellis,3
R5,2021,3,Larus argentatus,4
R6,2022,1,Anas crecca,20
R7,2020,2,Netta erythrophthalma,8
"""

# 只改只数（R2: 5->6, R5: 4->9），原名/原版不变，确认应保留
CENSUS_V2 = CENSUS.replace("R2,2020,2,Netta rufina,5", "R2,2020,2,Netta rufina,6").replace(
    "R5,2021,3,Larus argentatus,4", "R5,2021,3,Larus argentatus,9"
)

# 缺少 (1, Netta rufina, Netta rufina)：已确认的 R1 将追不到确认名
MAPPING_BAD = """起版,旧学名,下一版学名
1,Larus argentatus,Larus argentatus
1,Larus argentatus,Larus michahellis
2,Larus michahellis,Larus argentatus
1,Netta rufina,Netta erythrophthalma
"""

# 保留原有追溯链，并新增 v3->v4 并种与 Anas crecca 拆种
MAPPING_GOOD = """起版,旧学名,下一版学名
1,Larus argentatus,Larus argentatus
1,Larus argentatus,Larus michahellis
2,Larus michahellis,Larus argentatus
1,Netta rufina,Netta rufina
1,Netta rufina,Netta erythrophthalma
1,Anas crecca,Anas crecca
1,Anas crecca,Anas carolinensis
3,Netta erythrophthalma,Netta rufina
"""

failures = []


def check(label, cond, detail=""):
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", label,
                         "" if cond else "  -- " + str(detail)))
    if not cond:
        failures.append(label)


def upload(client, url, text):
    return client.post(
        url,
        data={"file": (io.BytesIO(text.encode("utf-8")), "upload.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def agg(client, *species):
    data = {"species": list(species), "format": "json"}
    rv = client.post("/aggregate", data=data)
    assert rv.status_code == 200, rv.status_code
    return rv.get_json()


def year(result, y):
    for row in result["years"]:
        if row["year"] == y:
            return row
    return None


def status(client):
    return client.get("/api/status").get_json()


def confirm(client, rid, name, version, action="save"):
    return client.post(
        "/records/%s/confirm" % rid,
        data={"confirmed_name": name, "confirmed_version": version, "action": action},
        follow_redirects=True,
    )


def main():
    tmp = tempfile.mkdtemp(prefix="birdapp-test-")
    app = create_app(
        {"DATABASE": os.path.join(tmp, "test.db"), "SECRET_KEY": "test", "TESTING": True}
    )
    client = app.test_client()

    # 1. 空库状态
    st = status(client)
    check("空库：记录0/对照0/最新版v1",
          st["records"] == 0 and st["mapping_entries"] == 0 and st["latest_version"] == 1, st)

    # 2. 上传名录对照与普查记录
    rv = upload(client, "/upload/mapping", MAPPING)
    check("上传名录对照成功", "名录对照已整份更换" in rv.get_data(as_text=True))
    rv = upload(client, "/upload/records", CENSUS)
    check("上传普查记录成功", "新增 7 条" in rv.get_data(as_text=True))
    st = status(client)
    check("状态：7条记录/5条对照/最新版v3",
          st["records"] == 7 and st["mapping_entries"] == 5 and st["latest_version"] == 3, st)

    # 3. 最新版物种勾选列表
    page = client.get("/aggregate").get_data(as_text=True)
    check("勾选列表含最新版学名",
          all(n in page for n in ["Netta rufina", "Netta erythrophthalma",
                                  "Larus argentatus", "Anas crecca"]))
    check("勾选列表不含已被并掉的旧名 Larus michahellis",
          "Larus michahellis" not in page)

    # 4. 汇总口径：最少/最多与差额记录
    res = agg(client, "Netta rufina")
    y = year(res, 2020)
    check("{Netta rufina} 2020 最少5 最多15", y and y["min"] == 5 and y["max"] == 15, y)
    check("{Netta rufina} 差额记录为 R1", y and y["diff_records"] == ["R1"], y)

    # 5. 拆种后再并种：同一笔计数不沿多条路径重复计入
    res = agg(client, "Larus argentatus")
    y = year(res, 2021)
    check("{Larus argentatus} 2021 最少=最多=14（R3 沿两条路径到达但不重复计）",
          y and y["min"] == 14 and y["max"] == 14, y)

    # 6. 一条记录可达两个组内学名时仍只计一次
    res = agg(client, "Netta rufina", "Netta erythrophthalma")
    y = year(res, 2020)
    check("双种组 2020 最少=最多=23（R1 只计一次）",
          y and y["min"] == 23 and y["max"] == 23, y)

    res = agg(client, "Anas crecca")
    y = year(res, 2022)
    check("{Anas crecca} 2022 最少=最多=20（未列名沿用）",
          y and y["min"] == 20 and y["max"] == 20, y)

    # 7. 复核确认：非法确认被拒绝
    rv = confirm(client, "R2", "Anas crecca", "2")
    check("确认不可达学名被拒绝", "无法从原名" in rv.get_data(as_text=True))
    rv = confirm(client, "R3", "Larus argentatus", "9")
    check("确认版号超出最新版被拒绝", "确认版号须在原版" in rv.get_data(as_text=True))
    rv = confirm(client, "R1", "Netta rufina", "0")
    check("确认版号小于原版被拒绝", "确认版号须在原版" in rv.get_data(as_text=True))
    rv = confirm(client, "R1", "Netta rufina", "abc")
    check("确认版号非整数被拒绝", "整数版号" in rv.get_data(as_text=True))
    check("非法确认后仍无已确认记录", status(client)["confirmed"] == 0)

    # 8. 合法确认：从原名可追到，后续从确认版换算
    rv = confirm(client, "R1", "Netta rufina", "2")
    check("确认 R1=Netta rufina(v2) 成功", "已确认为 Netta rufina" in rv.get_data(as_text=True))
    res = agg(client, "Netta rufina")
    y = year(res, 2020)
    check("确认后 {Netta rufina} 2020 最少=最多=15（差额消除）",
          y and y["min"] == 15 and y["max"] == 15 and not y["diff_records"], y)

    # 9. 普查记录重传（同名同版只更新只数，确认保留）
    rv = upload(client, "/upload/records", CENSUS_V2)
    check("重传普查记录：更新7条", "更新 7 条" in rv.get_data(as_text=True))
    st = status(client)
    check("重传后记录仍7条、确认保留1条",
          st["records"] == 7 and st["confirmed"] == 1, st)
    res = agg(client, "Netta rufina")
    y = year(res, 2020)
    check("重传后 {Netta rufina} 2020 最少=最多=16", y and y["min"] == 16 and y["max"] == 16, y)
    res = agg(client, "Larus argentatus")
    y = year(res, 2021)
    check("重传后 {Larus argentatus} 2021 = 19", y and y["min"] == 19 and y["max"] == 19, y)

    # 10. 整份更换对照：已确认记录追不到 -> 列出记录号并保留旧表
    rv = upload(client, "/upload/mapping", MAPPING_BAD)
    text = rv.get_data(as_text=True)
    check("坏对照被拒绝并列出记录号 R1", "更换被拒绝" in text and "R1" in text)
    st = status(client)
    check("坏对照被拒后旧表保留（仍5条，最新版v3）",
          st["mapping_entries"] == 5 and st["latest_version"] == 3, st)
    res = agg(client, "Netta rufina")
    y = year(res, 2020)
    check("坏对照被拒后汇总仍按旧表（16）", y and y["min"] == 16 and y["max"] == 16, y)

    # 11. 整份更换对照：合法新表被接受（未复核明细不参与校验）
    rv = upload(client, "/upload/mapping", MAPPING_GOOD)
    check("好对照整份更换成功", "名录对照已整份更换" in rv.get_data(as_text=True))
    st = status(client)
    check("更换后8条对照、最新版v4",
          st["mapping_entries"] == 8 and st["latest_version"] == 4, st)

    # 12. 更换后单次汇总只用新对照
    res = agg(client, "Netta rufina")
    y = year(res, 2020)
    check("新对照下 {Netta rufina} 2020 最少=最多=24（R7 经 v3->v4 并种入组）",
          y and y["min"] == 24 and y["max"] == 24, y)
    res = agg(client, "Anas crecca")
    y = year(res, 2022)
    check("新对照下 {Anas crecca} 2022 最少0 最多20（未复核的 R6 按新表拆种）",
          y and y["min"] == 0 and y["max"] == 20 and y["diff_records"] == ["R6"], y)
    page = client.get("/aggregate").get_data(as_text=True)
    check("更换后勾选列表出现新学名 Anas carolinensis", "Anas carolinensis" in page)

    # 13. 清除确认后按原名原版重新换算
    rv = confirm(client, "R1", "", "", action="clear")
    check("清除 R1 确认成功", "已清除确认" in rv.get_data(as_text=True))
    check("清除后无已确认记录", status(client)["confirmed"] == 0)
    res = agg(client, "Netta rufina")
    y = year(res, 2020)
    check("清除确认后 {Netta rufina} 2020 仍 24（v4 下原名可达组内）",
          y and y["min"] == 24 and y["max"] == 24, y)

    # 14. CSV 校验：坏文件整份拒绝
    rv = upload(client, "/upload/records", "a,b,c\n1,2,3\n")
    check("普查记录表头错误被拒绝", "表头应为" in rv.get_data(as_text=True))
    rv = upload(client, "/upload/records",
                "记录号,年份,名录版,学名,只数\nR8,2023,1,X,1\nR8,2023,1,X,2\n")
    check("记录号文件内重复被拒绝", "重复" in rv.get_data(as_text=True))
    rv = upload(client, "/upload/records",
                "记录号,年份,名录版,学名,只数\nR9,2023,1,X,-3\n")
    check("只数为负被拒绝", "非负整数" in rv.get_data(as_text=True))
    rv = upload(client, "/upload/mapping", "起版,旧学名,下一版学名\nx,A,B\n")
    check("对照起版非整数被拒绝", "正整数" in rv.get_data(as_text=True))
    st = status(client)
    check("坏文件均未落库（仍7条记录/8条对照）",
          st["records"] == 7 and st["mapping_entries"] == 8, st)

    # 16. 分组小计：与明细全量计算一致，汇总只读小计、不逐条读明细
    with app.app_context():
        from birdapp.aggregation import run_aggregation
        from birdapp.db import get_db

        db = get_db()
        rows = db.execute(
            "SELECT start_version, start_name, year, total, record_count "
            "FROM subtotals ORDER BY 1, 2, 3"
        ).fetchall()
        expected = db.execute(
            "SELECT COALESCE(confirmed_version, version) AS v, "
            "COALESCE(confirmed_name, sci_name) AS n, year, "
            "SUM(count) AS total, COUNT(*) AS c "
            "FROM records GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
        ).fetchall()
        check("小计与按明细全量计算的结果一致",
              [tuple(r) for r in rows] == [tuple(r) for r in expected],
              (len(rows), len(expected)))
        groups = db.execute(
            "SELECT COUNT(*) AS c FROM subtotal_groups"
        ).fetchone()["c"]
        check("起点集合与明细起点数一致（7 个）", groups == 7, groups)

        read_columns = set()
        db.set_authorizer(
            lambda action, a1, a2, dbn, trg: (
                read_columns.add((a1, a2))
                if action == sqlite3.SQLITE_READ
                else None
            )
            or sqlite3.SQLITE_OK
        )
        try:
            run_aggregation(db, {"Netta rufina"})
        finally:
            db.set_authorizer(None)
        detail_columns = {
            col
            for table, col in read_columns
            if table == "records"
        }
        check("汇总只读版号索引，不读只数/年份/学名等明细列",
              detail_columns <= {"version", "confirmed_version"}, detail_columns)

    # 17. 复核确认 + 记录改年：移出组与移入组的小计都更新
    before = agg(client, "Anas crecca")
    check("确认前 R6 为差额记录（2022 最少0 最多20）",
          year(before, 2022) and year(before, 2022)["min"] == 0
          and year(before, 2022)["max"] == 20
          and year(before, 2022)["diff_records"] == ["R6"], before)
    rv = confirm(client, "R6", "Anas crecca", "2")
    check("确认 R6=Anas crecca(v2)",
          "已确认为 Anas crecca" in rv.get_data(as_text=True))
    confirmed_res = agg(client, "Anas crecca")
    a2022 = year(confirmed_res, 2022)
    check("确认后 R6 从部分命中转为全命中（最少0->20，最多不变）",
          a2022 and a2022["min"] == 20 and a2022["max"] == 20
          and not a2022["diff_records"], a2022)

    census_moved = CENSUS.replace(
        "R6,2022,1,Anas crecca,20", "R6,2023,1,Anas crecca,20"
    )
    rv = upload(client, "/upload/records", census_moved)
    check("记录改年后重传成功", "更新" in rv.get_data(as_text=True))
    after = agg(client, "Anas crecca")
    check("改年后 2022 不再有 R6", year(after, 2022) is None, after)
    check("改年后 R6 计入 2023（最少/最多均 20）",
          year(after, 2023) and year(after, 2023)["min"] == 20
          and year(after, 2023)["max"] == 20, after)
    with app.app_context():
        from birdapp.db import get_db

        db = get_db()
        groups = [tuple(r) for r in db.execute(
            "SELECT start_version, start_name, ref_count FROM subtotal_groups "
            "ORDER BY 1, 2"
        )]
    check("改年后移出的旧起点组已清理",
          all(not (g[0] == 2 and g[1] == "Anas crecca" and g[2] <= 0)
              for g in groups), groups)

    # 18. 写入失败：明细与小计同一事务回滚，原结果仍可查
    before = agg(client, "Netta rufina")
    with app.app_context():
        from birdapp.db import get_db

        db = get_db()
        db.execute(
            "CREATE TRIGGER t_fail BEFORE INSERT ON records "
            "BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
        db.commit()
    rv = upload(client, "/upload/records",
                "记录号,年份,名录版,学名,只数\nR99,2024,1,Netta rufina,50\n")
    check("写入失败被提示并回滚", "已回滚" in rv.get_data(as_text=True))
    after = agg(client, "Netta rufina")
    check("写入失败后汇总与失败前一致", before == after, (before, after))
    st = status(client)
    check("写入失败后未多出记录", st["records"] == 7, st)
    with app.app_context():
        from birdapp.db import get_db

        db = get_db()
        db.execute("DROP TRIGGER t_fail")
        db.commit()

    # 19. 换表：最新版号没变也按新对照重算年表；维护小计不增加明细扫描
    # 先清除 R6 确认，再确认 R1，便于用坏表验证"已确认记录校验"。
    confirm(client, "R6", "", "", action="clear")
    rv = confirm(client, "R1", "Netta rufina", "2")
    check("换表前确认 R1=Netta rufina(v2)",
          "已确认为 Netta rufina" in rv.get_data(as_text=True))

    # 坏表：去掉 v1 的 Netta rufina 自映射，R1 无法追溯 -> 拒绝并保留旧表
    broken = MAPPING_GOOD.replace("1,Netta rufina,Netta rufina\n", "")
    rv = upload(client, "/upload/mapping", broken)
    check("坏表被拒绝并列出记录号 R1",
          "更换被拒绝" in rv.get_data(as_text=True)
          and "R1" in rv.get_data(as_text=True))
    st = status(client)
    check("坏表被拒后旧表保留（8条/v4）",
          st["mapping_entries"] == 8 and st["latest_version"] == 4, st)
    res = agg(client, "Netta rufina")
    check("坏表被拒后年表仍按旧表", year(res, 2020)["max"] == 23, res)

    # 新表：最新版号仍为 v4，但去掉 v3->v4 的并种，R7 不再并入 Netta rufina
    new_table = MAPPING_GOOD.replace(
        "3,Netta erythrophthalma,Netta rufina\n", "3,Anas crecca,Anas crecca\n"
    )
    before = agg(client, "Netta rufina")
    check("换表前 {Netta rufina} 2020 最多 23（含 R7）",
          year(before, 2020)["max"] == 23, before)
    rv = upload(client, "/upload/mapping", new_table)
    check("新表（最新版号不变）被接受",
          "名录对照已整份更换" in rv.get_data(as_text=True))
    st = status(client)
    check("换表后最新版号仍为 v4", st["latest_version"] == 4, st)
    after = agg(client, "Netta rufina")
    check("换表后年表按新对照重算（2020 最多 15，R7 移出）",
          year(after, 2020)["max"] == 15, after)

    # 维护小计不得扫描明细：换表后汇总仍不读 records 明细列
    with app.app_context():
        from birdapp.aggregation import run_aggregation
        from birdapp.db import get_db

        db = get_db()
        read_columns = set()
        db.set_authorizer(
            lambda action, a1, a2, dbn, trg: (
                read_columns.add((a1, a2))
                if action == sqlite3.SQLITE_READ
                else None
            )
            or sqlite3.SQLITE_OK
        )
        try:
            run_aggregation(db, {"Netta rufina"})
        finally:
            db.set_authorizer(None)
        detail_columns = {col for table, col in read_columns if table == "records"}
    check("换表后汇总仍只读版号索引，不扫明细列",
          detail_columns <= {"version", "confirmed_version"}, detail_columns)

    with app.app_context():
        from birdapp.db import get_db

        db = get_db()
        captured = []
        db.set_trace_callback(captured.append)
        try:
            upload(client, "/upload/mapping", new_table)
        finally:
            db.set_trace_callback(None)
        swap_sql = " ".join(captured)
    check("换表未重建/扫描小计",
          "DELETE FROM subtotals" not in swap_sql
          and "GROUP BY 1, 2, 3" not in swap_sql, swap_sql[:300])
    check("换表只查已确认记录（走部分索引）",
          any("confirmed_name IS NOT NULL" in s for s in captured),
          captured[:5])

    # 20. 写入失败时换表同样回滚，旧表与旧年表仍可用
    stable = agg(client, "Netta rufina")
    with app.app_context():
        from birdapp.db import get_db

        db = get_db()
        db.execute(
            "CREATE TRIGGER t_map_fail BEFORE INSERT ON mapping "
            "BEGIN SELECT RAISE(ABORT, 'boom-map'); END"
        )
        db.commit()
    rv = upload(client, "/upload/mapping", MAPPING)
    check("换表写入失败被提示并回滚", "已回滚" in rv.get_data(as_text=True))
    st = status(client)
    check("换表失败后对照表不变",
          st["mapping_entries"] == 8 and st["latest_version"] == 4, st)
    check("换表失败后年表不变", agg(client, "Netta rufina") == stable)
    with app.app_context():
        from birdapp.db import get_db

        db = get_db()
        db.execute("DROP TRIGGER t_map_fail")
        db.commit()

    # 21. 删除记录（小计与起点集合同步减少）——用直接 SQL 模拟
    with app.app_context():
        from birdapp.db import get_db

        db = get_db()
        before_groups = db.execute(
            "SELECT COUNT(*) FROM subtotal_groups"
        ).fetchone()[0]
        db.execute("DELETE FROM records WHERE record_id = 'R4'")
        db.commit()
        after_groups = db.execute(
            "SELECT COUNT(*) FROM subtotal_groups"
        ).fetchone()[0]
        sub = db.execute(
            "SELECT COUNT(*) FROM subtotals"
        ).fetchone()[0]
    check("删除记录后小计仍在且起点组不增加",
          after_groups <= before_groups and sub > 0, (before_groups, after_groups))

    # 22. 已有数据库首次升级：自动全量建立小计，重启不重复重建
    legacy_dir = tempfile.mkdtemp(prefix="birdapp-legacy-")
    legacy_path = os.path.join(legacy_dir, "legacy.db")
    conn = sqlite3.connect(legacy_path)
    conn.executescript(
        """
        CREATE TABLE records (record_id TEXT PRIMARY KEY, year INTEGER NOT NULL,
            version INTEGER NOT NULL, sci_name TEXT NOT NULL, count INTEGER NOT NULL,
            confirmed_name TEXT, confirmed_version INTEGER);
        CREATE TABLE mapping (from_version INTEGER NOT NULL, old_name TEXT NOT NULL,
            next_name TEXT NOT NULL, PRIMARY KEY (from_version, old_name, next_name));
        """
    )
    conn.executemany(
        "INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("L1", 2020, 1, "A", 10, None, None),
            ("L2", 2020, 1, "A", 5, None, None),
            ("L3", 2021, 2, "B", 7, "C", 3),
        ],
    )
    conn.execute("INSERT INTO mapping VALUES (1, 'A', 'B')")
    conn.commit()
    conn.close()

    legacy_app = create_app({"DATABASE": legacy_path, "SECRET_KEY": "t", "TESTING": True})
    legacy_client = legacy_app.test_client()
    st = status(legacy_client)
    check("旧库升级后记录保持 3 条", st["records"] == 3, st)
    with legacy_app.app_context():
        from birdapp.db import get_db

        db = get_db()
        subs = [tuple(r) for r in db.execute(
            "SELECT start_version, start_name, year, total, record_count "
            "FROM subtotals ORDER BY 1, 2, 3"
        )]
        check("旧库升级后小计已建立",
              subs == [(1, "A", 2020, 15, 2), (3, "C", 2021, 7, 1)], subs)
        check("升级版本号已记录",
              db.execute("SELECT value FROM meta WHERE key = 'subtotals_version'")
              .fetchone()[0] == "1")
    legacy_app2 = create_app({"DATABASE": legacy_path, "SECRET_KEY": "t", "TESTING": True})
    with legacy_app2.app_context():
        from birdapp.db import get_db

        count = get_db().execute("SELECT COUNT(*) FROM subtotals").fetchone()[0]
        check("重启不重复重建小计（仍 2 行）", count == 2, count)

    # 23. 汇总页候选学名与 JSON 字段沿用原口径
    page = client.get("/aggregate").get_data(as_text=True)
    check("汇总页仍可勾选物种（含当前最新版学名）",
          "Netta rufina" in page and "Anas crecca" in page)
    result = agg(client, "Netta rufina")
    check("JSON 字段保持 latest_version/group/years/diff_records",
          set(result) == {"latest_version", "group", "years", "diff_records"},
          set(result))
    check("年度行字段保持 year/min/max/diff_records",
          all(set(y) == {"year", "min", "max", "diff_records"}
              for y in result["years"]))

    print()
    if failures:
        print("失败 %d 项：%s" % (len(failures), "; ".join(failures)))
        return 1
    print("全部自测通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
