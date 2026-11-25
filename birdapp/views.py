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
from .aggregation import latest_names, latest_version, mapping_rows, run_aggregation
from .db import get_db
from .taxonomy import build_index, trace

bp = Blueprint("main", __name__)


# ---------------------------------------------------------------- 公共查询

def _mapping_index(db):
    return build_index(
        (r["from_version"], r["old_name"], r["next_name"]) for r in mapping_rows(db)
    )


def _record_start(rec):
    """记录换算起点：已确认的从确认版换算，否则从原名原版。"""
    if rec["confirmed_name"]:
        return rec["confirmed_name"], rec["confirmed_version"]
    return rec["sci_name"], rec["version"]


def _run_aggregation(db, group):
    return run_aggregation(db, group)


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
        "latest": latest_version(db),
    }
    return render_template("index.html", stats=stats)


@bp.get("/aggregate")
def aggregate_form():
    db = get_db()
    latest = latest_version(db)
    names = latest_names(db, latest)
    return render_template(
        "aggregate.html", latest=latest, names=names, result=None
    )


@bp.post("/aggregate")
def aggregate_run():
    db = get_db()
    group = set(request.form.getlist("species"))
    latest = latest_version(db)
    if not group:
        flash("请至少勾选一个最新版物种", "error")
        return redirect(url_for("main.aggregate_form"))
    result = _run_aggregation(db, group)
    if request.form.get("format") == "json":
        return jsonify(result)
    names = latest_names(db, latest)
    return render_template(
        "aggregate.html", latest=latest, names=names, result=result
    )


@bp.get("/records")
def records_page():
    db = get_db()
    latest = latest_version(db)
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
        "mapping.html", rows=mapping_rows(db), latest=latest_version(db)
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
            "latest_version": latest_version(db),
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
    db.execute("BEGIN IMMEDIATE")  # 先取写锁，避免读事务升级写锁时直接报 database is locked
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
    except Exception as exc:  # 明细与小计同一事务，失败时一起回滚
        db.rollback()
        flash(
            "普查记录写入失败，已回滚，原有明细和汇总结果保持不变：%s" % exc,
            "error",
        )
        return redirect(url_for("main.index"))
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
    # 整份更换时只扫描已复核（已确认）的记录，未复核的明细不参与校验；
    # 只需确认相关列，配合部分索引避免读取明细的年份/只数等列。
    confirmed = db.execute(
        "SELECT record_id, version, sci_name, confirmed_name, confirmed_version "
        "FROM records WHERE confirmed_name IS NOT NULL ORDER BY record_id"
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

    db.execute("BEGIN IMMEDIATE")  # 先取写锁，避免读事务升级写锁时直接报 database is locked
    try:
        db.execute("DELETE FROM mapping")
        db.executemany(
            "INSERT INTO mapping (from_version, old_name, next_name) "
            "VALUES (?, ?, ?)",
            entries,
        )
        db.commit()
    except Exception as exc:  # 换表整表替换是原子的，失败时旧表保留
        db.rollback()
        flash(
            "名录对照更换失败，已回滚，旧表保持不变：%s" % exc,
            "error",
        )
        return redirect(url_for("main.mapping_page"))
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
    latest = latest_version(db)

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
