"""按年汇总：用分组小计算上下界，只在有差额时回查原记录。

小计按「换算起点」(start_version, start_name) 和年份分组，起点规则沿用原统计口径：
已复核的记录从确认学名/确认版起算，未复核的从原名/原版起算。
对照表只影响「哪些起点组会落入所选物种组」，不改变小计本身，
因此整份更换对照不需要重扫明细或重建小计。
"""

from .taxonomy import build_index, build_version_index, reaching_sets, trace

# 单条 SQL 中 IN 列表的上限，避免一次绑定过多参数。
_NAME_CHUNK = 400
# 某版本的命中组很多时，整版扫描一次比逐块 IN 查询更快。
_SCAN_THRESHOLD = 1000


def latest_version(db):
    """最新版号 = 对照表最大起版+1、记录最大版号、确认最大版号 三者取最大。"""
    mapping_max = db.execute("SELECT MAX(from_version) AS m FROM mapping").fetchone()["m"]
    record_max = db.execute("SELECT MAX(version) AS m FROM records").fetchone()["m"]
    # 走部分索引倒序取第一条，避免为求 MAX 而扫描整张明细表。
    confirmed_max = db.execute(
        "SELECT confirmed_version FROM records "
        "WHERE confirmed_version IS NOT NULL "
        "ORDER BY confirmed_version DESC LIMIT 1"
    ).fetchone()
    confirmed_max = confirmed_max[0] if confirmed_max else None
    candidates = [v for v in (record_max, confirmed_max) if v is not None]
    if mapping_max is not None:
        candidates.append(mapping_max + 1)
    return max(candidates) if candidates else 1


def mapping_rows(db):
    return db.execute(
        "SELECT from_version, old_name, next_name FROM mapping "
        "ORDER BY from_version, old_name, next_name"
    ).fetchall()


def start_versions(db):
    """明细中出现过的起点版号（去重），来自小的版本索引表。"""
    return [
        r["start_version"]
        for r in db.execute("SELECT start_version FROM subtotal_versions")
    ]


def latest_names(db, latest=None):
    """最新版全部有效学名：把出现过的每个（学名, 版号）都追到最新版取并集。

    起点集合取自 subtotals（按起点分组的小计）与对照表，不扫描 records 明细；
    同一版本的全部学名一次换算。
    """
    if latest is None:
        latest = latest_version(db)
    idx = build_index(
        (r["from_version"], r["old_name"], r["next_name"]) for r in mapping_rows(db)
    )
    pool = {}
    for row in db.execute(
        "SELECT start_version AS version, start_name AS name FROM subtotal_groups"
    ):
        pool.setdefault(row["version"], set()).add(row["name"])
    for row in db.execute(
        "SELECT old_name AS n, from_version AS v FROM mapping "
        "UNION SELECT next_name, from_version + 1 FROM mapping"
    ):
        if row["v"] <= latest:
            pool.setdefault(row["v"], set()).add(row["n"])
    names = set()
    for version, version_names in pool.items():
        if version <= latest:
            names |= trace(version_names, version, latest, idx)
    return sorted(names)


def _subtotal_totals(db, reach, full):
    """按年汇总各起点组的小计，返回 ({年份: [最少, 最多]}, 部分命中组集合)。

    reach / full：{版本: 学名集合}。只在起始版号与学名的组合上取小计，
    不读取明细。部分命中组（有记录落在组内、也有记录落在组外）会同时
    收集起来，供差额明细回查使用。
    """
    years = {}
    partial_keys = set()
    for version in sorted(reach):
        reach_names = reach[version]
        if not reach_names:
            continue
        full_names = full.get(version, set())
        names = sorted(reach_names)
        partial_names = [n for n in names if n not in full_names]
        if len(partial_names) > _SCAN_THRESHOLD:
            # 差额组太多时整版扫描一次（走覆盖索引），比几十次 IN 更快。
            rows = db.execute(
                "SELECT start_name, year, total FROM subtotals "
                "WHERE start_version = ?",
                (version,),
            ).fetchall()
            for row in rows:
                if row["start_name"] not in reach_names:
                    continue
                stat = years.setdefault(row["year"], [0, 0])
                stat[1] += row["total"]
                if row["start_name"] in full_names:
                    stat[0] += row["total"]
                else:
                    partial_keys.add((version, row["start_name"]))
            continue

        # 全落组内的起点一次查询累加「最少」和「最多」。
        if full_names:
            full_list = sorted(full_names)
            for offset in range(0, len(full_list), _NAME_CHUNK):
                chunk = full_list[offset : offset + _NAME_CHUNK]
                placeholders = ",".join(["?"] * len(chunk))
                rows = db.execute(
                    "SELECT year, SUM(total) AS total FROM subtotals "
                    "WHERE start_version = ? AND start_name IN (%s) "
                    "GROUP BY year" % placeholders,
                    [version] + chunk,
                ).fetchall()
                for row in rows:
                    stat = years.setdefault(row["year"], [0, 0])
                    stat[0] += row["total"]
                    stat[1] += row["total"]

        # 部分命中的起点只累加「最多」，并记下供回查差额明细。
        for offset in range(0, len(partial_names), _NAME_CHUNK):
            chunk = partial_names[offset : offset + _NAME_CHUNK]
            placeholders = ",".join(["?"] * len(chunk))
            rows = db.execute(
                "SELECT start_name, year, total FROM subtotals "
                "WHERE start_version = ? AND start_name IN (%s)" % placeholders,
                [version] + chunk,
            ).fetchall()
            for row in rows:
                stat = years.setdefault(row["year"], [0, 0])
                stat[1] += row["total"]
                partial_keys.add((version, row["start_name"]))
    return years, partial_keys


def _diff_records(db, partial_keys, latest, idx):
    """逐条读取造成差额的原记录（只有存在部分命中组时才执行）。"""
    diff_records = []
    reach_cache = {}
    for version, name in sorted(partial_keys):
        start_key = (version, name)
        if start_key not in reach_cache:
            reach_cache[start_key] = sorted(trace({name}, version, latest, idx))
        reachable = reach_cache[start_key]
        rows = db.execute(
            "SELECT * FROM records "
            "WHERE COALESCE(confirmed_version, version) = ? "
            "  AND COALESCE(confirmed_name, sci_name) = ? "
            "ORDER BY year, record_id",
            (version, name),
        ).fetchall()
        for rec in rows:
            diff_records.append(
                {
                    "record_id": rec["record_id"],
                    "year": rec["year"],
                    "sci_name": rec["sci_name"],
                    "version": rec["version"],
                    "count": rec["count"],
                    "confirmed_name": rec["confirmed_name"],
                    "confirmed_version": rec["confirmed_version"],
                    "reachable": reachable,
                }
            )
    diff_records.sort(key=lambda d: (d["year"], d["record_id"]))
    return diff_records


def run_aggregation(db, group):
    """按年汇总一组最新版物种的最少/最多只数。

    单次汇总在一个读事务内一次性取对照表、起点集合、小计与差额记录的快照，
    保证上下界与差额记录取自同一快照，不混用新旧对照。
    """
    group = set(group)
    db.execute("BEGIN")
    try:
        latest = latest_version(db)
        rows = mapping_rows(db)
        versions = sorted(set(start_versions(db)))
        if versions:
            by_version = build_version_index(
                (r["from_version"], r["old_name"], r["next_name"]) for r in rows
            )
            reach, full = reaching_sets(by_version, group, latest, versions)
        else:
            reach, full = {}, {}

        reach = {v: names for v, names in reach.items() if names}
        years, partial_keys = _subtotal_totals(db, reach, full)

        idx = build_index(
            (r["from_version"], r["old_name"], r["next_name"]) for r in rows
        )
        diff_records = _diff_records(db, partial_keys, latest, idx)
    finally:
        db.execute("ROLLBACK")  # 只读事务，回滚即释放快照

    year_rows = []
    diff_by_year = {}
    for d in diff_records:
        diff_by_year.setdefault(d["year"], []).append(d["record_id"])
    for year in sorted(years):
        low, high = years[year]
        year_rows.append(
            {
                "year": year,
                "min": low,
                "max": high,
                "diff_records": diff_by_year.get(year, []),
            }
        )
    return {
        "latest_version": latest,
        "group": sorted(group),
        "years": year_rows,
        "diff_records": diff_records,
    }
