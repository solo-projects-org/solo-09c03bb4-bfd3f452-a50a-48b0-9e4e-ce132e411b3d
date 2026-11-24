from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import csvio
from .db import get_db
from .taxonomy import build_index, trace

bp = Blueprint("main", __name__)


# ---------------------------------------------------------------- 公共查询

def _latest_version(db):
    """最新版号 = 对照表最大起版+1、记录最大版号、确认最大版号 三者取最大。"""
    mapping_max = db.execute("SELECT MAX(from_version) AS m FROM mapping").fetchone()["m"]
    record_max = db.execute("SELECT MAX(version) AS m FROM records").fetchone()["m"]
    confirmed_max = db.execute(
        "SELECT MAX(confirmed_version) AS m FROM records"
    ).fetchone()["m"]
    candidates = [v for v in (record_max, confirmed_max) if v is not None]
    if mapping_max is not None:
        candidates.append(mapping_max + 1)
    return max(candidates) if candidates else 1


def _mapping_rows(db):
    return db.execute(
        "SELECT from_version, old_name, next_name FROM mapping "
        "ORDER BY from_version, old_name, next_name"
    ).fetchall()


def _mapping_index(db):
    return build_index(
        (r["from_version"], r["old_name"], r["next_name"]) for r in _mapping_rows(db)
    )


def _latest_names(db, latest):
    """最新版全部有效学名：把出现过的每个（学名, 版号）都追到最新版取并集。"""
    idx = _mapping_index(db)
    pool = set()
    for row in db.execute(
        """
        SELECT sci_name AS n, version AS v FROM records
        UNION
        SELECT confirmed_name, confirmed_version FROM records
        WHERE confirmed_name IS NOT NULL
        UNION
        SELECT old_name, from_version FROM mapping
        UNION
        SELECT next_name, from_version + 1 FROM mapping
        """
    ):
        pool.add((row["n"], row["v"]))
    names = set()
    for name, version in pool:
        if version <= latest:
            names |= trace({name}, version, latest, idx)
    return sorted(names)


def _record_start(rec):
    """记录换算起点：已确认的从确认版换算，否则从原名原版。"""
    if rec["confirmed_name"]:
        return rec["confirmed_name"], rec["confirmed_version"]
    return rec["sci_name"], rec["version"]


def _run_aggregation(db, group):
    """按年汇总一组最新版物种的最少/最多只数。

    单次汇总在一个读事务内一次性取对照表与全部记录的快照，
    保证不混用新旧对照。
    """
    db.execute("BEGIN")
    try:
        mapping_rows = db.execute(
            "SELECT from_version, old_name, next_name FROM mapping"
        ).fetchall()
        records = db.execute("SELECT * FROM records ORDER BY year, record_id").fetchall()
        latest = _latest_version(db)
    finally:
        db.execute("ROLLBACK")  # 只读事务，回滚即释放快照

    idx = build_index(
        (r["from_version"], r["old_name"], r["next_name"]) for r in mapping_rows
    )
    years = {}
    diff_records = []
    for rec in records:
        start_name, start_version = _record_start(rec)
        reachable = trace({start_name}, start_version, latest, idx)
        if not (reachable & group):
            continue  # 与本组无关
        # 每条记录只计一次：即使它沿多条路径/多个学名可达本组，也不重复计入
        year_stat = years.setdefault(rec["year"], {"min": 0, "max": 0})
        year_stat["max"] += rec["count"]
        if reachable <= group:
            year_stat["min"] += rec["count"]  # 无论分类如何取舍都落在本组
        else:
            diff_records.append(
                {
                    "record_id": rec["record_id"],
                    "year": rec["year"],
                    "sci_name": rec["sci_name"],
                    "version": rec["version"],
                    "count": rec["count"],
                    "confirmed_name": rec["confirmed_name"],
                    "confirmed_version": rec["confirmed_version"],
                    "reachable": sorted(reachable),
                }
            )
    year_rows = [
        {
            "year": year,
            "min": stat["min"],
            "max": stat["max"],
            "diff_records": [
                d["record_id"] for d in diff_records if d["year"] == year
            ],
        }
        for year, stat in sorted(years.items())
    ]
    return {
        "latest_version": latest,
        "group": sorted(group),
        "years": year_rows,
        "diff_records": diff_records,
    }


# ---------------------------------------------------------------- 页面

@bp.get("/")
def index():
    db = get_db()
    stats = {
        "records": db.execute("SELECT COUNT(*) AS c FROM records").fetchone()["c"],
        "confirmed": db.execute(
            "SELECT COUNT(*) AS c FROM records WHERE confirmed_name IS NOT NULL"
        ).fetchone()["c"],
        "mapping": db.execute("SELECT COUNT(*) AS c FROM mapping").fetchone()["c"],
        "latest": _latest_version(db),
    }
    return render_template("index.html", stats=stats)


@bp.get("/aggregate")
def aggregate_form():
    db = get_db()
    latest = _latest_version(db)
    names = _latest_names(db, latest)
    return render_template(
        "aggregate.html", latest=latest, names=names, result=None
    )


@bp.post("/aggregate")
def aggregate_run():
    db = get_db()
    group = set(request.form.getlist("species"))
    latest = _latest_version(db)
    if not group:
        flash("请至少勾选一个最新版物种", "error")
        return redirect(url_for("main.aggregate_form"))
    result = _run_aggregation(db, group)
    if request.form.get("format") == "json":
        return jsonify(result)
    names = _latest_names(db, latest)
    return render_template(
        "aggregate.html", latest=latest, names=names, result=result
    )


@bp.get("/records")
def records_page():
    db = get_db()
    latest = _latest_version(db)
    idx = _mapping_index(db)
    records = db.execute("SELECT * FROM records ORDER BY year, record_id").fetchall()
    rows = []
    for rec in records:
        start_name, start_version = _record_start(rec)
        reachable = sorted(trace({start_name}, start_version, latest, idx))
        rows.append({"rec": rec, "reachable": reachable})
    all_names = [
        r["n"]
        for r in db.execute(
            "SELECT sci_name AS n FROM records UNION SELECT old_name FROM mapping "
            "UNION SELECT next_name FROM mapping ORDER BY n"
        )
    ]
    return render_template(
        "records.html", rows=rows, latest=latest, all_names=all_names
    )


@bp.get("/mapping")
def mapping_page():
    db = get_db()
    return render_template(
        "mapping.html", rows=_mapping_rows(db), latest=_latest_version(db)
    )


@bp.get("/api/status")
def api_status():
    db = get_db()
    return jsonify(
        {
            "records": db.execute("SELECT COUNT(*) AS c FROM records").fetchone()["c"],
            "confirmed": db.execute(
                "SELECT COUNT(*) AS c FROM records WHERE confirmed_name IS NOT NULL"
            ).fetchone()["c"],
            "mapping_entries": db.execute("SELECT COUNT(*) AS c FROM mapping").fetchone()[
                "c"
            ],
            "latest_version": _latest_version(db),
        }
    )


# ---------------------------------------------------------------- 上传

@bp.post("/upload/records")
def upload_records():
    file = request.files.get("file")
    if file is None or file.filename == "":
        flash("请选择普查记录 CSV 文件", "error")
        return redirect(url_for("main.index"))
    rows, errors = csvio.parse_records(file)
    if errors:
        for err in errors[:20]:
            flash(err, "error")
        flash("普查记录上传被拒绝，未写入任何数据", "error")
        return redirect(url_for("main.index"))

    db = get_db()
    inserted = updated = confirmations_cleared = 0
    db.execute("BEGIN")
    try:
        for row in rows:
            cur = db.execute(
                "SELECT version, sci_name, confirmed_name FROM records "
                "WHERE record_id = ?",
                (row["record_id"],),
            ).fetchone()
            if cur is None:
                db.execute(
                    "INSERT INTO records (record_id, year, version, sci_name, count) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        row["record_id"],
                        row["year"],
                        row["version"],
                        row["sci_name"],
                        row["count"],
                    ),
                )
                inserted += 1
            elif (
                cur["version"] == row["version"] and cur["sci_name"] == row["sci_name"]
            ):
                db.execute(
                    "UPDATE records SET year = ?, count = ? WHERE record_id = ?",
                    (row["year"], row["count"], row["record_id"]),
                )
                updated += 1
            else:
                # 原名/原版变了，原确认不再有效，一并清除
                if cur["confirmed_name"]:
                    confirmations_cleared += 1
                db.execute(
                    "UPDATE records SET year = ?, version = ?, sci_name = ?, "
                    "count = ?, confirmed_name = NULL, confirmed_version = NULL "
                    "WHERE record_id = ?",
                    (
                        row["year"],
                        row["version"],
                        row["sci_name"],
                        row["count"],
                        row["record_id"],
                    ),
                )
                updated += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    flash(
        "普查记录已导入：新增 %d 条，更新 %d 条，因原名/原版变更清除确认 %d 条"
        % (inserted, updated, confirmations_cleared),
        "ok",
    )
    return redirect(url_for("main.index"))


@bp.post("/upload/mapping")
def upload_mapping():
    file = request.files.get("file")
    if file is None or file.filename == "":
        flash("请选择名录对照 CSV 文件", "error")
        return redirect(url_for("main.mapping_page"))
    entries, errors = csvio.parse_mapping(file)
    if errors:
        for err in errors[:20]:
            flash(err, "error")
        flash("名录对照上传被拒绝，旧表保持不变", "error")
        return redirect(url_for("main.mapping_page"))

    db = get_db()
    new_idx = build_index(entries)
    # 整份更换时只扫描已复核（已确认）的记录，不扫描未复核的明细
    confirmed = db.execute(
        "SELECT * FROM records WHERE confirmed_name IS NOT NULL ORDER BY record_id"
    ).fetchall()
    conflicts = []
    for rec in confirmed:
        reachable = trace(
            {rec["sci_name"]}, rec["version"], rec["confirmed_version"], new_idx
        )
        if rec["confirmed_name"] not in reachable:
            conflicts.append(rec)
    if conflicts:
        ids = "、".join(rec["record_id"] for rec in conflicts)
        flash(
            "更换被拒绝：按新对照，以下 %d 条已确认记录无法从原名追到其确认的"
            "版号和学名，已保留旧表。记录号：%s" % (len(conflicts), ids),
            "error",
        )
        return redirect(url_for("main.mapping_page"))

    db.execute("BEGIN")
    try:
        db.execute("DELETE FROM mapping")
        db.executemany(
            "INSERT INTO mapping (from_version, old_name, next_name) "
            "VALUES (?, ?, ?)",
            entries,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    flash(
        "名录对照已整份更换：%d 条（已核验 %d 条已确认记录均可从原名追溯）"
        % (len(entries), len(confirmed)),
        "ok",
    )
    return redirect(url_for("main.mapping_page"))


# ---------------------------------------------------------------- 复核确认

@bp.post("/records/<record_id>/confirm")
def confirm(record_id):
    db = get_db()
    rec = db.execute(
        "SELECT * FROM records WHERE record_id = ?", (record_id,)
    ).fetchone()
    if rec is None:
        abort(404)

    if request.form.get("action") == "clear":
        db.execute(
            "UPDATE records SET confirmed_name = NULL, confirmed_version = NULL "
            "WHERE record_id = ?",
            (record_id,),
        )
        db.commit()
        flash("%s：已清除确认" % record_id, "ok")
        return redirect(url_for("main.records_page"))

    name = (request.form.get("confirmed_name") or "").strip()
    version_raw = (request.form.get("confirmed_version") or "").strip()
    try:
        version = int(version_raw)
    except ValueError:
        version = None
    latest = _latest_version(db)

    if not name or version is None:
        flash("%s：请填写确认学名和整数版号" % record_id, "error")
        return redirect(url_for("main.records_page"))
    if version < rec["version"] or version > latest:
        flash(
            "%s：确认版号须在原版 v%d 与最新版 v%d 之间"
            % (record_id, rec["version"], latest),
            "error",
        )
        return redirect(url_for("main.records_page"))

    idx = _mapping_index(db)
    reachable = trace({rec["sci_name"]}, rec["version"], version, idx)
    if name not in reachable:
        flash(
            "%s：按当前对照，无法从原名 %s（v%d）追到 %s（v%d）"
            % (record_id, rec["sci_name"], rec["version"], name, version),
            "error",
        )
        return redirect(url_for("main.records_page"))

    db.execute(
        "UPDATE records SET confirmed_name = ?, confirmed_version = ? "
        "WHERE record_id = ?",
        (name, version, record_id),
    )
    db.commit()
    flash(
        "%s：已确认为 %s（v%d），后续汇总从确认版换算" % (record_id, name, version),
        "ok",
    )
    return redirect(url_for("main.records_page"))
