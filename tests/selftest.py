#!/usr/bin/env python3
"""关键流程自测：上传、汇总口径、拆种/并种去重、复核确认、整份换对照。

运行：python3 tests/selftest.py
退出码 0 = 全部通过。
"""

import io
import os
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

    # 15. 空选择与页面可用性
    rv = client.post("/aggregate", data={}, follow_redirects=True)
    check("空选择汇总被提示", "请至少勾选" in rv.get_data(as_text=True))
    for path in ["/", "/records", "/mapping", "/aggregate"]:
        check("GET %s 返回 200" % path, client.get(path).status_code == 200)

    print()
    if failures:
        print("失败 %d 项：%s" % (len(failures), "; ".join(failures)))
        return 1
    print("全部自测通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
