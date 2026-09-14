# -*- coding: utf-8 -*-
"""多表补全：按「模板」把多张源表的信息汇总补齐（比两表匹配更简单：单向填充）。

用户口径（2026-09-14 确认）：
- 模板第 1 列 = 钥匙（供应商等），其余列名 = 要补的目标列；源表各含钥匙列 + 部分字段列
- 程序自动发现"哪张源表能供哪个模板列"（列名匹配：同名 > 同义词 > 近似，阈值可调，默认 70）
- 一列多源：列名匹配度优先 → 同分按"该列有值最多"的源 → 该行首选源为空则**逐行回退**下一源
- 钥匙匹配用多信号相似度；按置信度上色：
    100%        → 填，不标色
    80%~99%     → 填，浅黄
    40%~80%     → 填，浅灰
    20%~40%     → 填，浅蓝
    <20%/无候选 → **留空**，浅红底
- 结果表下方只写「颜色图例 + 统计（+ 列名近似提示）」，不列来源表
- 默认只产出新文件，不动原表
"""
import re

import pandas as pd

from .aligner import _name_sim, norm_text
from .registry import skill

# ---------- 颜色档位（统一取主题语义色：高=浅黄 / 中=浅蓝 / 低=浅紫 / 缺失=浅灰） ----------
from .theme import COLORS as _THEME_COLORS

COLORS = {
    "ok":   {"fill": None,                       "font": None},                      # 100%
    "high": {"fill": _THEME_COLORS["conf_high"][0], "font": _THEME_COLORS["conf_high"][1]},  # 80~99%
    "mid":  {"fill": _THEME_COLORS["conf_mid"][0],  "font": _THEME_COLORS["conf_mid"][1]},   # 40~80%
    "low":  {"fill": _THEME_COLORS["conf_low"][0],  "font": _THEME_COLORS["conf_low"][1]},   # 20~40%
    "miss": {"fill": _THEME_COLORS["missing"][0],   "font": None},                     # <20%/无候选
}
KEY_MIN = 20.0          # 低于此分视为"未匹配"（留空+红）
COL_DEFAULT_THRESHOLD = 70.0

# ---------- 列名同义词族（先内置几组常见；后续可由经验库扩充） ----------
SYNONYM_GROUPS = [
    ("截止", "结束", "到期", "终止"),
    ("金额", "总价", "总金额", "合价"),
    ("单价", "价格", "报价", "价"),
    ("编号", "编码", "单号", "号码"),
    ("名称", "品名", "名称规格", "品名规格"),
    ("供应商", "厂商", "厂家", "供货商", "乙方"),
    ("单位", "计量单位"),
    ("数量", "需求量", "采购量"),
    ("开始", "起始", "开工"),
    ("日期", "时间"),
    ("负责人", "责任人", "联系人"),
    ("备注", "说明", "备注说明"),
]


def norm_col(name):
    """列名归一化：全/半角、括号、空白、大小写、常见标点。"""
    s = str(name or "").strip()
    s = s.replace("（", "(").replace("）", ")").replace("，", ",")
    s = re.sub(r"\s+", "", s)
    return s.lower()


def _synonym_set(name):
    n = norm_col(name)
    for grp in SYNONYM_GROUPS:
        if any(g in n for g in grp):
            return set(grp)
    return None


def _polarity_conflict(a, b):
    """含税/不含税、总价/单价 这类"看起来像但含义不同"的列名，直接判为不可替代。"""
    na, nb = norm_col(a), norm_col(b)
    tax_a, tax_b = ("不含税" in na or "未税" in na or "除税" in na), ("不含税" in nb or "未税" in nb or "除税" in nb)
    if tax_a != tax_b and ("税" in na or "税" in nb):
        return True
    return False


def col_match(tpl_col, src_col, threshold=COL_DEFAULT_THRESHOLD):
    """列名匹配：返回 (score, how)；how ∈ 同名/同义/近似/不匹配。"""
    if _polarity_conflict(tpl_col, src_col):
        return 0.0, "冲突"
    na, nb = norm_col(tpl_col), norm_col(src_col)
    if not na or not nb:
        return 0.0, "不匹配"
    if na == nb:
        return 100.0, "同名"
    ga, gb = _synonym_set(tpl_col), _synonym_set(src_col)
    if ga and gb and ga & gb:
        return 95.0, "同义"
    s, _ = _name_sim(na, nb)
    if s >= 60:
        return float(s), "近似"
    return 0.0, "不匹配"


def _key_sim(a, b):
    """钥匙相似度（专用）：完全相同=100；否则允许"部分/字符袋"助分但**不满分**。

    避免 `A公司集团` vs `A公司` 因"包含"被判成 100% 而静默填充（应落到高置信=黄色，供复核）。
    """
    na, nb = norm_text(a), norm_text(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 100.0
    try:
        from rapidfuzz import fuzz
        r = float(fuzz.ratio(na, nb))
        p = float(fuzz.partial_ratio(na, nb))
    except ImportError:
        r = p = _name_sim(na, nb)[0]
    from .aligner import _bag_sim
    g = _bag_sim(na, nb)
    return max(r, p - 10.0, g - 5.0, 0.0)


PROMOTE_MIN = 80.0        # 补出的值置信 ≥ 此分才允许"升级为钥匙"（级联用）
MAX_ROUNDS = 4            # 级联最大轮数（提前收敛：某轮无新增即停）


def _has_value(v):
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    try:
        return not pd.isna(v)
    except Exception:
        return True


def _band(score):
    if score >= 100:
        return "ok"
    if score >= 80:
        return "high"
    if score >= 40:
        return "mid"
    return "low"


def _auto_key_pairs(template_keys, source, col_threshold, legacy_key_col=None):
    """模板钥匙列 ↔ 源表列 自动配对（按模板钥匙列顺序；同一源列不重复用）。

    legacy_key_col：兼容旧调用（源表手选单钥匙）→ 只把第一把钥匙配到该列。
    """
    df = source["df"]
    pairs = []
    if legacy_key_col:
        if legacy_key_col in df.columns and template_keys:
            pairs.append((template_keys[0], legacy_key_col, 100.0))
        return pairs
    used = set()
    for tk in template_keys:
        best = None
        for col in df.columns:
            if col in used:
                continue
            sc, how = col_match(tk, col, col_threshold)
            if sc >= col_threshold and (best is None or sc > best[1]):
                best = (col, sc)
        if best:
            pairs.append((tk, best[0], best[1]))
            used.add(best[0])
    return pairs


def _pick_row(rows, df, target_col, score, k, exact):
    """并列多行的处理：目标值相同→直接用；不同→取第1条并降档（标歧义）。"""
    if len(rows) == 1:
        return rows[0], score, k, 0, exact
    vals = [df[target_col].iloc[j] if target_col in df.columns else None for j in rows]
    first = vals[0]
    same = all((pd.isna(a) and pd.isna(first)) or (a == first) for a in vals)
    return rows[0], (score if same else min(score, 39.0)), k, len(rows), exact


def _match_source(src, pairs, row_vals, key_min, target_col, idx_cache):
    """按"钥匙层次 n→1"在源表里找最佳行；层内先精确（索引）后模糊。

    pairs: [(模板钥匙列, 源列, 列名匹配度)]（按模板钥匙列顺序）
    row_vals: {模板钥匙列: 归一化值}（本行可用且达标的钥匙）
    返回 (row, score, k_used, tie_n, all_exact) 或 None。
    """
    avail = [(tk, scol) for tk, scol, _ in pairs if row_vals.get(tk)]
    if not avail:
        return None
    df = src["df"]
    for k in range(len(avail), 0, -1):
        sub = avail[:k]
        want = tuple(row_vals[tk] for tk, _ in sub)
        ck = (id(df), tuple(scol for _, scol in sub))
        idx = idx_cache.get(ck)
        if idx is None:
            cols = [df[c].tolist() for _, c in sub]
            idx = {}
            for j in range(len(df)):
                t = tuple(norm_text(c[j]) for c in cols)
                if all(t):
                    idx.setdefault(t, []).append(j)
            idx_cache[ck] = idx
        rows = idx.get(want, [])
        if rows:
            return _pick_row(rows, df, target_col, 100.0, k, True)
        best_score, best_rows = 0.0, []
        for j in range(len(df)):
            vals = [norm_text(df[c].iloc[j]) for _, c in sub]
            if not all(vals):
                continue
            sims = [_key_sim(vals[x], want[x]) for x in range(k)]
            if min(sims) < key_min:
                continue
            sc = sum(sims) / k
            if sc > best_score + 1e-9:
                best_score, best_rows = sc, [j]
            elif abs(sc - best_score) <= 1e-9:
                best_rows.append(j)
        if best_rows and best_score >= key_min:
            return _pick_row(best_rows, df, target_col, best_score, k, False)
    return None


@skill(
    name="多表补全",
    desc="按模板把多张源表汇总补齐：源表钥匙自动识别 + 分层钥匙匹配 + 级联多轮 + 置信分档上色",
    inputs={"template_df": "模板表", "key_cols": "模板钥匙列(1~5，按优先级)",
            "sources": "[{name, df}]（源表钥匙由系统自动识别）", "mapping": "模板列→源列（可选覆盖）",
            "col_threshold": "列名匹配阈值(默认70)", "key_min": "钥匙最低分(默认20)",
            "max_rounds": "级联最大轮数(默认4)", "promote_min": "升级为钥匙的置信门槛(默认80)"},
    outputs={"result": "补全后的表", "confidence": "逐格置信度(含轮次)", "stats": "统计",
             "supply": "列供给", "legend": "图例/说明"},
    task_modes=["多表补全"],
)
def fill_multi(template_df, key_col=None, sources=None, mapping=None,
               col_threshold=COL_DEFAULT_THRESHOLD, key_min=KEY_MIN,
               key_cols=None, max_rounds=MAX_ROUNDS, promote_min=PROMOTE_MIN):
    """多源 → 模板 单向填充（分层钥匙 + 级联）。返回 {result, confidence, stats, supply, legend}。

    - 模板钥匙列 1~5 个（key_cols；兼容旧 key_col 单钥匙）
    - 源表**不用选钥匙**：按模板钥匙列自动配对（同名/同义/近似 ≥ col_threshold）
    - 分层：钥匙层 n→1，层内先精确后模糊；命中即停
    - 级联：最多 max_rounds 轮，某轮无新增即停；补出的值若 ≥ promote_min 可当钥匙（多跳置信 ×0.9/跳）
    """
    sources = list(sources or [])
    mapping = mapping or {}
    tkeys = list(key_cols) if key_cols else ([key_col] if key_col else [])
    tkeys = [c for c in tkeys if c in template_df.columns][:5]
    if not tkeys:
        raise ValueError("请至少指定 1 个模板钥匙列")
    target_cols = [c for c in template_df.columns if c not in tkeys]

    # 可作为钥匙的列 = 模板钥匙列 + "能被某张源表当钥匙对上"的目标列（级联用）
    extra_keys = [c for c in target_cols
                  if any(col_match(c, col, col_threshold)[0] >= col_threshold
                         for s in sources for col in s["df"].columns)]
    key_candidates = tkeys + extra_keys

    key_pairs = [_auto_key_pairs(key_candidates, s, col_threshold, s.get("key_col"))
                 for s in sources]
    supply = discover_supply(target_cols, sources, col_threshold, mapping)

    result = template_df.copy()
    conf = pd.DataFrame("", index=template_df.index, columns=list(result.columns))
    filled = {}          # (i, col) -> (decayed_score, round, k_used, tie_n)
    idx_cache = {}
    n_fill = {"ok": 0, "high": 0, "mid": 0, "low": 0, "miss": 0}
    rounds_used = 0

    for r in range(1, int(max_rounds) + 1):
        # 本轮开始时快照"可用钥匙"（本轮新补出的值下一轮才生效 → 级联逐轮推进、轮次可解释）
        avail_by_row = {}
        for i in template_df.index:
            rv = {}
            for kc in key_candidates:
                if kc not in result.columns or not _has_value(result.at[i, kc]):
                    continue
                sc = 100.0 if (i, kc) not in filled else filled[(i, kc)][0]
                if sc >= promote_min:          # 仅"原有值或高置信补出值"可当钥匙
                    rv[kc] = norm_text(result.at[i, kc])
            avail_by_row[i] = rv
        progressed = False
        for tcol in target_cols:
            for i in template_df.index:
                if _has_value(result.at[i, tcol]):
                    continue
                row_vals = avail_by_row.get(i) or {}
                if not row_vals:
                    continue
                best = None
                for si, s in enumerate(sources):
                    # 匹配用钥匙：本行可得 & 不是"目标列自己"（防自引用）
                    pairs = [(tk, scol, cs) for tk, scol, cs in key_pairs[si]
                             if tk in row_vals and tk != tcol]
                    if not pairs:
                        continue
                    for cand in (supply.get(tcol) or []):
                        if cand["source"] != si:
                            continue
                        ccol = cand["col"]
                        if ccol not in s["df"].columns:
                            continue
                        m = _match_source(s, pairs, row_vals, key_min, ccol, idx_cache)
                        if not m:
                            continue
                        j, sc, k_used, tie_n, exact = m
                        v = s["df"][ccol].iloc[j]
                        if not _has_value(v):
                            continue
                        if best is None or sc > best[1]:
                            best = (v, sc, k_used, tie_n, cand["how"])
                if best:
                    v, sc, k_used, tie_n, how = best
                    decayed = sc * (0.9 ** (r - 1))
                    result.at[i, tcol] = v
                    filled[(i, tcol)] = (decayed, r, k_used, tie_n)
                    band = _band(decayed)
                    tag = f"{band}:{decayed:.0f}@{r}"
                    if k_used > 1:
                        tag += f"·{k_used}钥匙"
                    if tie_n:
                        tag += f"·歧义{tie_n}行"
                    conf.at[i, tcol] = tag
                    n_fill[band] += 1
                    progressed = True
        if progressed:
            rounds_used = r
        else:
            break

    for i in template_df.index:
        for tcol in target_cols:
            if not _has_value(result.at[i, tcol]) and not _has_value(template_df.at[i, tcol]):
                conf.at[i, tcol] = "miss"
                n_fill["miss"] += 1

    indirect = sum(1 for _, (_, rr, _, _) in filled.items() if rr > 1)
    ambiguous = sum(1 for _, (_, _, _, tie) in filled.items() if tie)
    stats = {"模板行数": len(template_df), "目标列数": len(target_cols),
             "可填格": len(template_df) * len(target_cols),
             "完全匹配(100%)": n_fill["ok"], "高置信(80-99%)": n_fill["high"],
             "中置信(40-80%)": n_fill["mid"], "低置信(20-40%)": n_fill["low"],
             "未匹配(留空)": n_fill["miss"],
             "级联轮数": rounds_used, "间接补全格数": indirect, "歧义格数": ambiguous,
             "未补全行数": int((conf[target_cols] == "miss").any(axis=1).sum()) if target_cols else 0}
    return {"result": result, "confidence": conf, "stats": stats,
            "supply": supply, "legend": legend_lines(stats), "key_pairs": key_pairs}


def preview_keys(template_keys, sources, col_threshold=COL_DEFAULT_THRESHOLD):
    """界面预览：每张源表自动识别到的钥匙列 → [(源表名, [(模板钥匙列, 源列, 分)])]。"""
    return [(s["name"], _auto_key_pairs(list(template_keys), s, col_threshold, s.get("key_col")))
            for s in sources]


def discover_supply(tpl_cols, sources, col_threshold=COL_DEFAULT_THRESHOLD, mapping=None):
    """发现每个模板列的候选源列；返回 {模板列: [ {source, col, score, how, filled}, ... ]}。"""
    mapping = mapping or {}
    out = {}
    for tcol in tpl_cols:
        cands = []
        if tcol in mapping and mapping[tcol]:          # 手动指定优先（永远排第一）
            msrc, mcol = mapping[tcol]
            cands.append({"source": msrc, "col": mcol, "score": 101.0, "how": "手动",
                          "filled": 10 ** 9})
        for si, s in enumerate(sources):
            df = s["df"]
            for col in df.columns:
                if col == s.get("key_col"):
                    continue
                if tcol in mapping and mapping[tcol] and (si, col) == tuple(mapping[tcol]):
                    continue
                score, how = col_match(tcol, col, col_threshold)
                if score >= col_threshold and how != "冲突":
                    nonblank = int(df[col].notna().sum())
                    cands.append({"source": si, "col": col, "score": score, "how": how,
                                  "filled": nonblank})
        # 匹配度优先 → 有值数多者优先 → 源表顺序
        cands.sort(key=lambda c: (-c["score"], -c["filled"]))
        out[tcol] = cands
    return out


def legend_lines(stats):
    """表格下方备注（只写图例 + 统计；不列来源表）。"""
    lines = [
        "─" * 30 + " 说明 " + "─" * 30,
        "颜色图例：无色=完全匹配(100%)；浅黄=高置信(80–99%)；浅蓝=中置信(40–80%)；"
        "浅紫=低置信(20–40%)；浅灰底空格=未匹配（留空）",
        f"统计：模板 {stats['模板行数']} 行 × 目标列 {stats['目标列数']}；"
        f"完全匹配 {stats['完全匹配(100%)']}，高置信 {stats['高置信(80-99%)']}，"
        f"中置信 {stats['中置信(40-80%)']}，低置信 {stats['低置信(20-40%)']}，"
        f"未匹配 {stats['未匹配(留空)']}",
        f"有未补全格的行数：{stats['未补全行数']}",
    ]
    extra = []
    if stats.get("间接补全格数"):
        extra.append(f"含 {stats['间接补全格数']} 格为多轮间接补全（置信已按跳数折减）")
    if stats.get("歧义格数"):
        extra.append(f"有 {stats['歧义格数']} 格命中多行且取值不同，已取第 1 条，请核对")
    return lines + extra


def export_filled(result_df, conf_df, out_path, stats=None, extra_notes=None):
    """写出带颜色的整合表 + 下方备注块（表头/列宽/冻结/筛选/数字格式由 theme 统一处理）。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    from .theme import style_sheet as _style

    wb = Workbook()
    ws = wb.active
    ws.title = "模板补全结果"
    cols = list(result_df.columns)
    ws.append([str(c) for c in cols])
    for _, row in result_df.iterrows():
        ws.append(["" if (v is None or (not isinstance(v, str) and pd.isna(v))) else v for v in row])

    for r in range(2, ws.max_row + 1):
        for c in range(1, len(cols) + 1):
            band = str(conf_df.iloc[r - 2, c - 1]).split(":")[0] if (r - 2) < len(conf_df) else ""
            spec = COLORS.get(band)
            if not spec or not spec["fill"]:
                continue
            cell = ws.cell(row=r, column=c)
            cell.fill = PatternFill("solid", fgColor="FF" + spec["fill"].lstrip("#"))
            if spec["font"]:
                cell.font = Font(color="FF" + spec["font"].lstrip("#"))

    notes = list(legend_lines(stats)) if stats is not None else []
    if extra_notes:
        notes += list(extra_notes)
    _style(ws, 1, highlight_min=False, footer_lines=notes or None)
    wb.save(out_path)
    return out_path
