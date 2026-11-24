"""CSV 上传解析与校验。任何一行出错则整份文件拒绝，保证不落半截数据。"""

import csv
import io

RECORDS_HEADER = ["记录号", "年份", "名录版", "学名", "只数"]
MAPPING_HEADER = ["起版", "旧学名", "下一版学名"]


def _read_rows(file_storage):
    raw = file_storage.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, ["文件不是有效的 UTF-8 编码"]
    rows = [[cell.strip() for cell in row] for row in csv.reader(io.StringIO(text))]
    rows = [row for row in rows if any(row)]  # 去掉空行
    return rows, None


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_records(file_storage):
    """返回 (记录列表, 错误列表)。记录为 dict。"""
    rows, err = _read_rows(file_storage)
    if err:
        return [], err
    errors = []
    if not rows:
        return [], ["文件为空"]
    if rows[0] != RECORDS_HEADER:
        return [], [
            "表头应为：%s；实际为：%s" % (",".join(RECORDS_HEADER), ",".join(rows[0]))
        ]
    out, seen = [], set()
    for lineno, row in enumerate(rows[1:], start=2):
        if len(row) != 5:
            errors.append("第%d行：应有5列，实际%d列" % (lineno, len(row)))
            continue
        record_id, year, version, sci_name, count = row
        if not record_id:
            errors.append("第%d行：记录号为空" % lineno)
            continue
        if record_id in seen:
            errors.append("第%d行：记录号 %s 在文件内重复" % (lineno, record_id))
            continue
        year_i, version_i, count_i = _to_int(year), _to_int(version), _to_int(count)
        if year_i is None:
            errors.append("第%d行：年份不是整数：%s" % (lineno, year))
            continue
        if version_i is None or version_i < 1:
            errors.append("第%d行：名录版应为正整数：%s" % (lineno, version))
            continue
        if count_i is None or count_i < 0:
            errors.append("第%d行：只数应为非负整数：%s" % (lineno, count))
            continue
        if not sci_name:
            errors.append("第%d行：学名为空" % lineno)
            continue
        seen.add(record_id)
        out.append(
            {
                "record_id": record_id,
                "year": year_i,
                "version": version_i,
                "sci_name": sci_name,
                "count": count_i,
            }
        )
    return out, errors


def parse_mapping(file_storage):
    """返回 (对照条目列表, 错误列表)。条目为 (起版, 旧学名, 下一版学名)。"""
    rows, err = _read_rows(file_storage)
    if err:
        return [], err
    errors = []
    if not rows:
        return [], ["文件为空"]
    if rows[0] != MAPPING_HEADER:
        return [], [
            "表头应为：%s；实际为：%s" % (",".join(MAPPING_HEADER), ",".join(rows[0]))
        ]
    out, seen = [], set()
    for lineno, row in enumerate(rows[1:], start=2):
        if len(row) != 3:
            errors.append("第%d行：应有3列，实际%d列" % (lineno, len(row)))
            continue
        from_version, old_name, next_name = row
        from_i = _to_int(from_version)
        if from_i is None or from_i < 1:
            errors.append("第%d行：起版应为正整数：%s" % (lineno, from_version))
            continue
        if not old_name or not next_name:
            errors.append("第%d行：旧学名/下一版学名不能为空" % lineno)
            continue
        key = (from_i, old_name, next_name)
        if key in seen:
            continue  # 完全重复的行去重
        seen.add(key)
        out.append(key)
    return out, errors
