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


def build_version_index(rows):
    """按起版分组的 {版本: {旧学名: {下一版学名}}}，供反向推算使用。"""
    by_version = {}
    for from_version, old_name, next_name in rows:
        by_version.setdefault(from_version, {}).setdefault(old_name, set()).add(
            next_name
        )
    return by_version


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


def reaching_sets(by_version, group, latest, start_versions):
    """反向推算各起点版本中「能追到 group」与「只能落在 group 内」的学名集合。

    返回 (reach, full)，两者都是 {版本: set(学名)}：
    - reach[v]：从 v 版出发，追到最新版后与 group 有交集的学名；
    - full[v] ：从 v 版出发，追到最新版后可达学名全部落在 group 内的学名。
    full[v] 是 reach[v] 的子集。

    只遍历对照表和调用方给出的起点版本，不读取任何明细记录。
    对照表未出现的版本之间状态不变，因此只需在起点版本处取值。
    """
    group = set(group)
    reach = {latest: set(group)}
    full = {latest: set(group)}
    current_reach = set(group)
    current_full = set(group)

    targets = sorted(
        (v for v in set(start_versions) if v < latest), reverse=True
    )
    transition_versions = sorted(
        (v for v in by_version if v < latest), reverse=True
    )
    t_index = 0
    for target in targets:
        # 从高到低应用 [target, latest) 内的全部换版，得到 target 版的状态。
        while (
            t_index < len(transition_versions)
            and transition_versions[t_index] >= target
        ):
            version = transition_versions[t_index]
            entries = by_version[version]
            mapped_names = set(entries)
            next_reach = current_reach
            next_full = current_full

            # 本版未列出的名称沿用：在下一版可达/全落组内，本版同样成立。
            stepped_reach = set(next_reach) - mapped_names
            stepped_full = set(next_full) - mapped_names

            for old_name, children in entries.items():
                if children & next_reach:
                    stepped_reach.add(old_name)
                if children <= next_full:
                    stepped_full.add(old_name)  # 所有下一版学名都只落在组内

            current_reach = stepped_reach
            current_full = stepped_full
            t_index += 1

        reach[target] = set(current_reach)
        full[target] = set(current_full)

    return reach, full
