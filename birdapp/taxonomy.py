"""名录对照的链式换算。

对照表每行 (起版 v, 旧学名 old, 下一版学名 new) 表示：
在 v -> v+1 这一次换版中，old 变为 new。
- 同一 (v, old) 出现多行  = 拆种（一个旧名对应多个下一版学名）；
- 多个不同 old 指向同一 new = 并种；
- 某版本没有列出的名称   = 沿用到下一版。
版号为连续整数，逐级换算即可把任意版本的学名追到最新版。
"""


def build_index(rows):
    """rows: 可迭代的 (from_version, old_name, next_name)，返回查询索引。"""
    idx = {}
    for from_version, old_name, next_name in rows:
        idx.setdefault((from_version, old_name), set()).add(next_name)
    return idx


def trace(names, from_version, to_version, idx):
    """把一组学名从 from_version 版换算到 to_version 版，返回可达学名集合。

    拆种时一个名字会扩成多个；并种时多个名字会收敛。
    若 from_version >= to_version，原样返回。
    """
    current = set(names)
    for version in range(from_version, to_version):
        stepped = set()
        for name in current:
            mapped = idx.get((version, name))
            if mapped:
                stepped |= mapped
            else:
                stepped.add(name)  # 未列出的名称沿用
        current = stepped
    return current
