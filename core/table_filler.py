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


@skill(
    name="多表补全",
    desc="按模板把多张源表的信息汇总补齐：自动发现列供给 + 钥匙多信号匹配 + 置信度上色 + 下方备注",
    inputs={"template_df": "模板表（第1列钥匙）", "key_col": "模板钥匙列",
            "sources": "[{name, df, key_col}]", "mapping": "模板列→源列（可选，覆盖自动发现）",
            "col_threshold": "列名匹配阈值(默认70)", "key_min": "钥匙最低分(默认20)"},
    outputs={"result": "补全后的表", "confidence": "逐格置信度", "stats": "统计", "legend": "图例"},
    task_modes=["多表补全"],
)
def fill_multi(template_df, key_col, sources, mapping=None,
               col_threshold=COL_DEFAULT_THRESHOLD, key_min=KEY_MIN):
    """多源 → 模板 单向填充。返回 {result, confidence, stats, legend, supply}。"""
    mapping = mapping or {}
    tpl_cols = [c for c in template_df.columns if c != key_col]
    supply = discover_supply(tpl_cols, sources, col_threshold, mapping)

    result = template_df.copy()
    conf = pd.DataFrame("", index=template_df.index, columns=list(result.columns))
    n_fill = {"ok": 0, "high": 0, "mid": 0, "low": 0, "miss": 0}

    # 预取源表钥匙列与候选列的值（提升性能）
    def key_vals(df, kcol):
        return [norm_text(v) for v in df[kcol].tolist()] if kcol in df.columns else []

    src_cache = [{"name": s["name"], "df": s["df"], "keys": key_vals(s["df"], s["key_col"])}
                 for s in sources]

    for i, trow in template_df.iterrows():
        tk = norm_text(trow[key_col])
        for tcol in tpl_cols:
            cands = supply.get(tcol) or []
            got, score, how = None, 0.0, ""
            for cand in cands:            # 遍历候选源，取"该行钥匙匹配度最高"的值
                s = src_cache[cand["source"]]
                col = cand["col"]
                if col not in s["df"].columns or not s["keys"]:
                    continue
                best_j, best_s = None, 0.0
                for j, sk in enumerate(s["keys"]):
                    if not sk:
                        continue
                    if sk == tk:
                        best_j, best_s = j, 100.0
                        break
                    sc = _key_sim(sk, tk)
                    if sc > best_s:
                        best_j, best_s = j, sc
                if best_j is None or best_s < key_min:
                    continue
                v = s["df"][col].iloc[best_j]
                if v is None or (isinstance(v, str) and not v.strip()) or (not isinstance(v, str) and pd.isna(v)):
                    continue                  # 该源该行无值 → 看下一候选源
                if best_s > score:            # 匹配度更高者胜（并列取候选顺序，即"有值多"的源）
                    got, score, how = v, best_s, cand["how"]
            if got is None:
                conf.at[i, tcol] = "miss"
                n_fill["miss"] += 1
                continue
            result.at[i, tcol] = got
            band = _band(score)
            conf.at[i, tcol] = f"{band}:{score:.0f}"
            n_fill[band] += 1

    total_cells = len(template_df) * len(tpl_cols)
    stats = {"模板行数": len(template_df), "目标列数": len(tpl_cols), "可填格": total_cells,
             "完全匹配(100%)": n_fill["ok"], "高置信(80-99%)": n_fill["high"],
             "中置信(40-80%)": n_fill["mid"], "低置信(20-40%)": n_fill["low"],
             "未匹配(留空)": n_fill["miss"],
             "未补全行数": int((conf[[c for c in tpl_cols]] == "miss").any(axis=1).sum()) if tpl_cols else 0}
    return {"result": result, "confidence": conf, "stats": stats,
            "supply": supply, "legend": legend_lines(stats)}


def _band(score):
    if score >= 100:
        return "ok"
    if score >= 80:
        return "high"
    if score >= 40:
        return "mid"
    return "low"


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
                if col == s["key_col"]:
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
    return [
        "─" * 30 + " 说明 " + "─" * 30,
        "颜色图例：无色=完全匹配(100%)；浅黄=高置信(80–99%)；浅灰=中置信(40–80%)；"
        "浅蓝=低置信(20–40%)；浅红底空格=未匹配（留空）",
        f"统计：模板 {stats['模板行数']} 行 × 目标列 {stats['目标列数']}；"
        f"完全匹配 {stats['完全匹配(100%)']}，高置信 {stats['高置信(80-99%)']}，"
        f"中置信 {stats['中置信(40-80%)']}，低置信 {stats['低置信(20-40%)']}，"
        f"未匹配 {stats['未匹配(留空)']}",
        f"有未补全格的行数：{stats['未补全行数']}",
    ]


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
