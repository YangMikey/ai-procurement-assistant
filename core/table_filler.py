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

from .aligner import _bag_sim, _name_sim, norm_text
from .conventions import agreement as _agree_of
from .conventions import apply_rule_src as _apply_rule_src
from .conventions import apply_rule_tpl as _apply_rule_tpl
from .conventions import canon as _canon
from .conventions import learn_value_equiv as _learn_equiv
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
# 注意：「到期/截止/截至」是**日期口径**，「终止/失效」是**状态口径** —— 绝不能算一族
#（曾因此把"合同到期月份"配到源表"终止状态"，把「未终止」填进月份列）
SYNONYM_GROUPS = [
    ("截止", "截至", "结束", "到期"),
    ("终止", "失效", "作废"),
    ("金额", "总价", "总金额", "合价"),
    ("单价", "价格", "报价", "价"),
    ("编号", "单号", "号码"),              # 合同编号/单号 —— 与"编码"分开（合同编号 ≠ 项目编码）
    ("编码", "代码", "科目"),
    ("名称", "品名", "名称规格", "品名规格"),
    ("供应商", "厂商", "厂家", "供货商", "乙方"),
    ("单位", "计量单位"),
    ("数量", "需求量", "采购量"),
    ("开始", "起始", "开工"),
    ("日期", "时间"),
    ("负责人", "责任人", "联系人"),
    ("备注", "说明", "备注说明"),
    ("分类", "类别", "三级分类", "品类"),      # 采购三级分类 ↔ 类别/业务类别
]


def _level_token(name):
    """取「几级」里的级别字样（一级/二级/三级…），无则 None。"""
    m = re.search(r"([一二三四五六123456])级", norm_col(name))
    return m.group(1) if m else None


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
    """含税/不含税、总价/单价、一级分类/三级分类 这类"看起来像但含义不同"的列名，直接判为不可替代。"""
    na, nb = norm_col(a), norm_col(b)
    tax_a, tax_b = ("不含税" in na or "未税" in na or "除税" in na), ("不含税" in nb or "未税" in nb or "除税" in nb)
    if tax_a != tax_b and ("税" in na or "税" in nb):
        return True
    la, lb = _level_token(a), _level_token(b)
    if la and lb and la != lb:          # 一级分类 ≠ 三级分类
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


def _uniq_vals(series, limit=800):
    """取一列里"去重、归一化后非空"的取值（用于值域指纹比对）。"""
    out, seen = [], set()
    try:
        it = series.tolist()
    except Exception:
        it = list(series)
    for v in it:
        if not _has_value(v):
            continue
        n = norm_text(v)
        if not n or n in seen:
            continue
        seen.add(n)
        out.append(n)
        if len(out) >= limit:
            break
    return out


DOMAIN_MIN_UNIQ = 5        # 值域指纹：两边唯一值至少这么多才敢认
DOMAIN_COVER = 0.8         # 值域指纹：双向覆盖率门槛

# 日期/月份口径的列名特征 & 取值特征（类型守卫用）
_DATE_NAME_KWS = ("日期", "时间", "月份", "月度", "到期", "截止", "截至", "起始", "开始", "年", "月")
_DATE_VAL_RE = re.compile(
    r"^(\d{4}[-/.]\d{1,2}([-/.]\d{1,2})?"          # 2026-05 / 2026-05-15 / 2026.5
    r"|\d{4}\s*年\s*\d{1,2}\s*月(\s*\d{1,2}\s*日)?)?"  # 2026年5月 / 2026年5月15日
    r"$")


def _name_looks_date(name):
    n = norm_col(name)
    return any(k in n for k in _DATE_NAME_KWS)


# 编号/编码口径的列名特征 & 取值特征（软守卫：不像编号的列只降分、不硬拦）
_CODE_NAME_KWS = ("编号", "单号", "号码", "编码", "代码")


def _name_looks_code(name):
    n = norm_col(name)
    return any(k in n for k in _CODE_NAME_KWS)


def _looks_code_col(series, min_vals=5, need=0.6):
    """源列取值是否"像编号"：含数字、且多数带连字符/下划线或足够长。返回 (像不像, 比例)。

    值太少（<min_vals）时不敢判 → 放行（保持旧行为）。
    """
    try:
        it = series.tolist()
    except Exception:
        it = list(series)
    vals = [v for v in it if _has_value(v)]
    if len(vals) < min_vals:
        return True, 1.0
    good = 0
    for v in vals:
        s = str(v).strip()
        has_digit = any(ch.isdigit() for ch in s)
        if has_digit and (("-" in s) or ("_" in s) or len(s) >= 10):
            good += 1
    frac = good / len(vals)
    return frac >= need, frac


def _code_guard_block(tpl_col, how, series):
    """编号类目标列的**硬守卫**：非「同名」来源、且取值不像编号 → 不采用该列。

    典型：`合同编号` 的正确列这行为空时，别让 `甲方编号`(YC01)/`项目编码` 顶上来。
    「同名」列不受限（各家编号风格不同，同名列永远可信）。
    """
    if how == "同名" or not _name_looks_code(tpl_col):
        return False
    return not _looks_code_col(series)[0]


def _looks_date_col(series, min_vals=5, need=0.5):
    """源列取值是否"像日期/月份"：返回 (是否达标, 像日期的比例)。

    值太少（<min_vals）时不敢判 → 视为达标（放行，保持旧行为）。
    """
    import datetime as _dt
    vals = []
    try:
        it = series.tolist()
    except Exception:
        it = list(series)
    for v in it:
        if not _has_value(v):
            continue
        vals.append(v)
    if len(vals) < min_vals:
        return True, 1.0
    good = 0
    for v in vals:
        if isinstance(v, (_dt.date, _dt.datetime)):
            good += 1
            continue
        if _DATE_VAL_RE.match(str(v).strip()):
            good += 1
    frac = good / len(vals)
    return frac >= need, frac


def value_domain_sim(tpl_series, src_series, min_uniq=DOMAIN_MIN_UNIQ, cover=DOMAIN_COVER):
    """值域指纹：列名对不上，但两列取值集合双向高度重叠 → 认定"同一类列"。

    返回 (score, 双向覆盖率%)；不认则 (0.0, 0)。
    只用于**列名不达标**时兜底，且要求唯一值够多（小表/枚举列不可靠）。
    """
    a, b = _uniq_vals(tpl_series), _uniq_vals(src_series)
    if len(a) < min_uniq or len(b) < min_uniq:
        return 0.0, 0
    sa, sb = set(a), set(b)
    ca = sum(1 for x in a if x in sb) / len(a)
    cb = sum(1 for x in b if x in sa) / len(b)
    if ca >= cover and cb >= cover:
        return 90.0, int(round(min(ca, cb) * 100))
    return 0.0, 0


def pair_col(tpl_col, src_col, tpl_series=None, src_series=None,
             threshold=COL_DEFAULT_THRESHOLD, allow_domain=True):
    """列配对：先列名（同名/同义/近似）→ 不达标再试**值域指纹**（方式标「值域」）。

    类型守卫：目标列名是**日期/月份口径**时，源列取值必须"像日期/月份"，否则判冲突；
    目标列名是**编号口径**时，不像编号的源列只**降 15 分**（软守卫，避免误伤风格不同的编号）。
    """
    sc, how = col_match(tpl_col, src_col, threshold)
    if sc < threshold and how != "冲突" and _name_looks_date(tpl_col) and src_series is not None:
        ok, frac = _looks_date_col(src_series)
        if not ok:
            return 0.0, "冲突"
    if sc >= threshold:
        if _name_looks_code(tpl_col) and src_series is not None and _looks_code_col(src_series)[0] is False:
            sc = max(0.0, sc - 15.0)       # 软守卫：不像编号的列只降分（让真正的编号列排前面）
        return sc, how
    if how == "冲突":                      # 含税/级别这类语义冲突：不给兜底
        return 0.0, how
    if allow_domain and tpl_series is not None and src_series is not None:
        ds, _ = value_domain_sim(tpl_series, src_series)
        if ds >= threshold:
            return ds, "值域"
    return sc, how


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
TIE_STOP_MIN = 80.0       # 钥匙命中多行时：≥此分视为"真歧义"（留空、不回退）；<此分当没命中
MAX_ROUNDS = 4            # 级联最大轮数（提前收敛：某轮无新增即停）
EXP_NS = "多表补全"        # 经验库命名空间（与两表匹配的键区分开）
_NOT_SAME_SENTINEL = "\x00NOTSAME"   # 人工判过"不是一回事"的源值 → 归一到这里（与任何真实值都不相似）


def exp_key_of(vals):
    """经验库键：归一化后 \x1f 连接（与 ExperienceStore 存库口径一致）。"""
    try:
        from .experience import norm_vals as _exp_norm
        return _exp_norm(vals)
    except Exception:
        return "\x1f".join("" if v is None else str(v) for v in vals)


def exp_key_values(template_df, i, key_cols):
    """界面写库用：与引擎 exp_key 完全同口径（键 = 命名空间 + 目标列 + 各行钥匙值）。

    返回 dict 供 app 直接构造键；key_cols 由结果里的 `exp_key_cols` 给出。
    """
    return {str(c): ("" if not _has_value(template_df.at[i, c]) else str(template_df.at[i, c]))
            for c in key_cols}


def exp_key_for(template_df, i, tcol, key_cols):
    """按引擎口径生成经验库左键（界面点「确认并记住」时调用）。"""
    vals = [EXP_NS, str(tcol)]
    for c in key_cols:
        if str(c) == str(tcol):
            continue
        v = template_df.at[i, c]
        vals.append("" if not _has_value(v) else str(v))
    return exp_key_of(vals)


def _has_value(v):
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    try:
        return not pd.isna(v)
    except Exception:
        return True


def _norm_value(v):
    """入库前归一化：日期时间戳若"零点"→ 只留日期（避免 2026-11-30 00:00:00 这种显示）。"""
    import datetime as _dt
    try:
        if isinstance(v, pd.Timestamp):
            v = v.to_pydatetime()
        if isinstance(v, _dt.datetime) and v.hour == 0 and v.minute == 0 and v.second == 0 \
                and v.microsecond == 0:
            return v.date()
    except Exception:
        return v
    return v


def _band(score):
    if score >= 100:
        return "ok"
    if score >= 80:
        return "high"
    if score >= 40:
        return "mid"
    return "low"


def _auto_key_pairs(template_keys, source, col_threshold, legacy_key_col=None,
                    template_df=None, allow_domain=True):
    """模板钥匙列 ↔ 源表列 自动配对（按模板钥匙列顺序；同一源列不重复用）。

    legacy_key_col：兼容旧调用（源表手选单钥匙）→ 只把第一把钥匙配到该列。
    template_df/allow_domain：给定模板表则可用**值域指纹**兜底（列名对不上也能认）。
    """
    df = source["df"]
    pairs = []
    if legacy_key_col:
        if legacy_key_col in df.columns and template_keys:
            pairs.append((template_keys[0], legacy_key_col, 100.0))
        return pairs
    used = set()
    for tk in template_keys:
        tser = template_df[tk] if (template_df is not None and tk in getattr(template_df, "columns", [])) else None
        best = None
        for col in df.columns:
            if col in used:
                continue
            sc, how = pair_col(tk, col, tser, df[col], col_threshold, allow_domain)
            if sc >= col_threshold and (best is None or sc > best[1]):
                best = (col, sc)
        if best:
            pairs.append((tk, best[0], best[1]))
            used.add(best[0])
    return pairs


def _combo_score(template_df, src, pairs, key_min):
    """自检打分：用这组钥匙去试，返回 (歧义率, 覆盖率, 列数)；越靠前越优。

    探针列取"最有区分度、且不是钥匙列"的源列（否则拿钥匙列自己当探针 → 歧义永远测不出来）。
    """
    if not pairs:
        return None
    df = src["df"]
    used_src = {scol for _tk, scol, _s in pairs}
    tgt, best_u = None, -1
    for c in df.columns:
        if c in used_src or c == src.get("key_col"):
            continue
        try:
            u = int(df[c].nunique(dropna=True))
        except Exception:
            continue
        if u > best_u:
            best_u, tgt = u, c
    if tgt is None:
        return None
    idx_cache = {}
    covered = amb = 0
    for i in template_df.index:
        rv = {}
        for tk, _scol, _cs in pairs:
            if tk in template_df.columns and _has_value(template_df.at[i, tk]):
                rv[tk] = norm_text(template_df.at[i, tk])
        pa = [(tk, scol, cs) for tk, scol, cs in pairs if rv.get(tk)]
        if not pa:
            continue
        m = _match_source(src, pa, rv, key_min, tgt, idx_cache)
        if m is None:
            continue
        covered += 1
        if m[3]:
            amb += 1
    if covered == 0:
        return (1.0, 0.0, len(pairs))
    return (amb / covered, covered / max(1, len(template_df)), len(pairs))


def _better(a, b):
    """a 是否比 b 更优：歧义率低 → 覆盖率高 → 列数少。"""
    if a[0] < b[0] - 1e-9:
        return True
    if a[0] > b[0] + 1e-9:
        return False
    if a[1] > b[1] + 1e-9:
        return True
    if a[1] < b[1] - 1e-9:
        return False
    return a[2] < b[2]


def plan_keys(template_df, key_candidates, sources, user_keys=(), mapping=None,
              col_threshold=COL_DEFAULT_THRESHOLD, key_min=KEY_MIN,
              max_keys=4, allow_domain=True):
    """给每张源表定钥匙列（≤max_keys）：你点的列优先 → 不够时按自检指标补位。

    返回 {"per_source": [pairs...], "notes": [说明...]}；pairs = [(模板列, 源列, 分)]
    """
    user_keys = list(user_keys or [])
    mapping = mapping or {}
    per_source, notes = [], []
    for si, s in enumerate(sources):
        pool = _auto_key_pairs(list(key_candidates), s, col_threshold, s.get("key_col"),
                               template_df, allow_domain)
        rank = {}
        for p in pool:
            if p[0] in user_keys:
                rank[p[0]] = (0, user_keys.index(p[0]))
            elif p[0] in mapping:
                rank[p[0]] = (1, 0)
            else:
                rank[p[0]] = (2, 0)
        pool = sorted(pool, key=lambda p: rank[p[0]])

        chosen, chosen_tks = [], set()
        for p in pool:                                  # 你点的列（该表能对上的）先拿走
            if p[0] in user_keys and len(chosen) < max_keys:
                chosen.append(p); chosen_tks.add(p[0])
        for p in pool:                                  # 你手动映射过的列，视为你确认过
            if p[0] in mapping and p[0] not in chosen_tks and len(chosen) < max_keys:
                chosen.append(p); chosen_tks.add(p[0])

        rest = [p for p in pool if p[0] not in chosen_tks]

        def _has_any(tk):
            return tk in template_df.columns and bool(template_df[tk].map(_has_value).any())

        valued = [p for p in rest if _has_any(p[0])]      # 模板里有值 → 现在就能当钥匙
        blank = [p for p in rest if not _has_any(p[0])]   # 现在没值 → 只能等级联补出来
        best = _combo_score(template_df, s, chosen, key_min) if chosen else None
        # 1) 先用"现在就能用"的列补位（按加进来后歧义率最低的顺序）
        while valued and len(chosen) < max_keys:
            trial_best = None
            for p in valued:
                sc = _combo_score(template_df, s, chosen + [p], key_min)
                if sc is None:
                    continue
                if trial_best is None or _better(sc, trial_best[1]):
                    trial_best = (p, sc)
            if trial_best is None:
                break
            chosen.append(trial_best[0])
            chosen_tks.add(trial_best[0][0])
            valued = [p for p in valued if p[0] != trial_best[0][0]]
            if best is None or _better(trial_best[1], best):
                best = trial_best[1]
        # 2) 还不够 2 把 → 补一个"结构上能对上、现在还没值"的列，留给级联当第二把钥匙
        while blank and len(chosen) < min(2, max_keys):
            chosen.append(blank.pop(0))
            chosen_tks.add(chosen[-1][0])
        auto = [p[0] for p in chosen if p[0] not in user_keys and p[0] not in mapping]
        if auto:
            notes.append(f"{s['name']}：自动补了钥匙列 {'、'.join(str(c) for c in auto)}")
        if chosen and best is not None:
            notes.append(f"{s['name']}：{len(chosen)} 把钥匙，歧义 {best[0]*100:.0f}%、覆盖 {best[1]*100:.0f}%")
        per_source.append(chosen)
    return {"per_source": per_source, "notes": notes}


def _pick_row(rows, df, target_col, score, k, exact, blank_on_tie=False):
    """并列多行的处理。

    - 取值相同 → 直接用第 1 条（不算歧义）
    - 取值不同 → tie_n>0；blank_on_tie=True 时返回 row=None（**留空交给人工，不猜**）
    返回 (row_or_None, **原始匹配分**, k, tie_n, exact, rows)：rows 供页面给候选值。
    注意：分档降级（歧义→≤39）由调用处决定，这里保留原始分，便于判断"歧义质量"。
    """
    if len(rows) == 1:
        return rows[0], score, k, 0, exact, rows
    vals = [df[target_col].iloc[j] if target_col in df.columns else None for j in rows]
    first = vals[0]
    same = all((pd.isna(a) and pd.isna(first)) or (a == first) for a in vals)
    if same:
        return rows[0], score, k, 0, exact, rows
    return (None if blank_on_tie else rows[0]), score, k, len(rows), exact, rows


def _near_values(src, pairs, row_vals, target_col, lo=40.0, topn=3):
    """「未补上」时给最接近的候选值（模糊相似度 ≥lo 的源表行，按相似度倒序去重）。

    lo 比钥匙门槛(默认20)高、但比"能配上"的水平低 —— 只作**找线索**用，不参与自动填。
    """
    df = src["df"]
    sub = [(tk, scol) for tk, scol, _ in pairs if row_vals.get(tk)]
    if not sub or target_col not in df.columns:
        return []
    want = [row_vals[tk] for tk, _ in sub]
    scored = []
    for j in range(len(df)):
        vals = [norm_text(df[c].iloc[j]) for _, c in sub]
        if not all(vals):
            continue
        v = df[target_col].iloc[j]
        if not _has_value(v):
            continue
        sims = [_key_sim(vals[x], want[x]) for x in range(len(sub))]
        sc = sum(sims) / len(sims)
        if sc >= lo:
            scored.append((sc, str(v)))
    scored.sort(key=lambda t: -t[0])
    out, seen = [], set()
    for sc, v in scored:
        if v in seen:
            continue
        seen.add(v)
        out.append((round(sc, 1), v))
        if len(out) >= topn:
            break
    return out


def _match_source(src, pairs, row_vals, key_min, target_col, idx_cache, blank_on_tie=False,
                  norm_fn=None, norm_fn_src=None):
    """按"钥匙层次 n→1"在源表里找最佳行；层内先精确（索引）后模糊。

    pairs: [(模板钥匙列, 源列, 列名匹配度)]（按模板钥匙列顺序）
    row_vals: {模板钥匙列: 归一化值}（本行可用且达标的钥匙）
    norm_fn / norm_fn_src: 模板侧 / 源表侧 归一化（口径本的规律与值等价在这里生效）
    返回 (row, score, k_used, tie_n, all_exact, rows) 或 None。
    """
    nf = norm_fn or norm_text
    nfs = norm_fn_src or nf
    avail = [(tk, scol) for tk, scol, _ in pairs if row_vals.get(tk)]
    if not avail:
        return None
    df = src["df"]
    for k in range(len(avail), 0, -1):
        sub = avail[:k]
        want = tuple(nf(row_vals[tk]) for tk, _ in sub)
        ck = (id(df), tuple(scol for _, scol in sub),
              getattr(nf, "_tag", "raw"), getattr(nfs, "_tag", "raw"))
        idx = idx_cache.get(ck)
        if idx is None:
            cols = [df[c].tolist() for _, c in sub]
            idx = {}
            for j in range(len(df)):
                t = tuple(nfs(c[j]) for c in cols)
                if all(t):
                    idx.setdefault(t, []).append(j)
            idx_cache[ck] = idx
        rows = idx.get(want, [])
        if rows:
            return _pick_row(rows, df, target_col, 100.0, k, True, blank_on_tie)
        best_score, best_rows = 0.0, []
        for j in range(len(df)):
            vals = [nfs(df[c].iloc[j]) for _, c in sub]
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
            return _pick_row(best_rows, df, target_col, best_score, k, False, blank_on_tie)
    return None


@skill(
    name="多表补全",
    desc="按模板把多张源表汇总补齐：自动配钥匙列(≤4，含值域识别) + 单钥匙源表延后 + 随机换组合复验 + 置信分档上色",
    inputs={"template_df": "模板表", "key_cols": "模板钥匙列(≤4，按优先级，可留空交给自动)",
            "sources": "[{name, df}]（源表钥匙由系统自动识别）", "mapping": "模板列→源列（可选覆盖）",
            "col_threshold": "列名匹配阈值(默认70)", "key_min": "钥匙最低分(默认20)",
            "max_rounds": "级联最大轮数(默认4)", "promote_min": "升级为钥匙的置信门槛(默认80)",
            "auto_keys": "自动补钥匙列(默认开)", "defer_single": "单钥匙源表第1轮延后(默认开)",
            "audit_rounds": "随机换组合复验组数(默认2)", "audit_seed": "复验随机种子(默认42)"},
    outputs={"result": "补全后的表", "confidence": "逐格置信度(含轮次)", "stats": "统计",
             "supply": "列供给", "legend": "图例/说明", "review": "需人工确认清单"},
    task_modes=["多表补全"],
)
def fill_multi(template_df, key_col=None, sources=None, mapping=None,
               col_threshold=COL_DEFAULT_THRESHOLD, key_min=KEY_MIN,
               key_cols=None, max_rounds=MAX_ROUNDS, promote_min=PROMOTE_MIN,
               auto_keys=True, defer_single=True, audit_rounds=0, audit_seed=42,
               allow_domain=True, key_plan=None, _audit=False, max_keys=4,
               blank_on_tie=True, experience=None, conventions=None):
    """多源 → 模板 单向填充（自动配钥匙 + 分层钥匙 + 级联 + 复验）。

    - 模板钥匙列 ≤4 个（你点的列优先；不够时自动按"歧义率低→覆盖率高→列数少"补位）
    - 源表钥匙自动配对：列名（同名/同义/近似）→ 不达标再试**值域指纹**
    - 单钥匙源表：第 1 轮整表延后，第 2 轮起能凑到 ≥2 把就用复合钥匙，仍只有 1 把就按 1 把匹配
    - 级联：最多 max_rounds 轮，某轮无新增即停；补出的值 ≥ promote_min 才可当钥匙（×0.9/跳）
    - 复验：另外用「少一把钥匙」的降级对照再跑一遍，定不出来/取值不同的格进「需人工确认」清单
    - blank_on_tie=True（默认）：命中多行且取值不同 → **留空**交人工（不猜第 1 条）
    - experience：经验库实例（传了就启用）——人工确认过的格**下次自动填**、标 `·经验库`、不再问
    - 返回 `choices`：多候选的**候选值**与"未补上"的**最接近 3 个候选**（供页面一键确认 → 写经验库）
    """
    sources = list(sources or [])
    mapping = mapping or {}
    tkeys = list(key_cols) if key_cols else ([key_col] if key_col else [])
    tkeys = [c for c in tkeys if c in template_df.columns][:max_keys]
    if not tkeys and not auto_keys:
        raise ValueError("请至少指定 1 个模板钥匙列")
    target_cols = [c for c in template_df.columns if c not in tkeys]

    def _pairable(tpl_col, s):
        return any(pair_col(tpl_col, c, template_df[tpl_col], s["df"][c],
                            col_threshold, allow_domain)[0] >= col_threshold
                   for c in s["df"].columns)

    # 可作为钥匙的列 = 模板钥匙列 + "能被某张源表当钥匙对上"的目标列（级联/自动补位用）
    extra_keys = [c for c in target_cols if any(_pairable(c, s) for s in sources)]
    key_candidates = tkeys + extra_keys
    if not key_candidates:
        raise ValueError("没有可用于匹配的钥匙列：请指定模板钥匙列")

    # ---- 定钥匙列（每张源表各自 ≤max_keys 把） ----
    if key_plan is not None:
        plan = {"per_source": key_plan, "notes": []}
    elif auto_keys:
        plan = plan_keys(template_df, key_candidates, sources, user_keys=tkeys,
                         mapping=mapping, col_threshold=col_threshold,
                         key_min=key_min, max_keys=max_keys, allow_domain=allow_domain)
    else:
        plan = {"per_source": [_auto_key_pairs(tkeys or key_candidates, s, col_threshold,
                                               s.get("key_col"), template_df, allow_domain)
                               for s in sources], "notes": []}
    key_pairs = plan["per_source"]
    supply = discover_supply(target_cols, sources, col_threshold, mapping)

    result = template_df.copy()
    conf = pd.DataFrame("", index=template_df.index, columns=list(result.columns))
    filled = {}          # (i, col) -> (decayed_score, 有效轮次, k_used, tie_n)
    gen = {}             # (i, col) -> 生成代：原值=0；用原值补出=1；用补出值再补=2…
    idx_cache = {}
    n_fill = {"ok": 0, "high": 0, "mid": 0, "low": 0, "miss": 0}
    eff_rounds = 0       # 有效轮数 = 真正补出东西的轮数（"延后"不占轮）
    deferred_sources = set()
    tie_cells = {}       # (i, col) -> (tie_n, k_used, how, [候选值], 源表名)
    n_exp = 0            # 经验库命中格数

    # ---- 经验库：键 = ("多表补全", 目标列, 该行各钥匙列的模板值) ----
    exp_lut = {}
    if experience is not None:
        try:
            exp_lut = experience.build_lookup()
        except Exception:
            exp_lut = {}
    exp_cols = []
    for pr in key_pairs:
        for tk, _sc, _cs in pr:
            tk = str(tk)
            if tk in template_df.columns and tk not in exp_cols \
                    and bool(template_df[tk].map(_has_value).any()):
                exp_cols.append(tk)

    def exp_key(i, tcol):
        vals = [EXP_NS, str(tcol)]
        for c in exp_cols:
            if str(c) == str(tcol):          # 目标列自己不当钥匙
                continue
            v = template_df.at[i, c]
            vals.append("" if not _has_value(v) else str(v))
        return exp_key_of(vals)

    # ---- 口径本①：学"值等价"（1事业部 = 一 这类）→ 让钥匙能精确命中，而不是"75 分像" ----
    n_equiv = 0
    equiv_notes = []
    if conventions is not None:
        try:
            for s in sources:
                nm = s["name"]
                for tk, scol, _cs in key_pairs[sources.index(s)]:
                    if tk not in template_df.columns or scol not in s["df"].columns:
                        continue
                    r = _learn_equiv(template_df[tk].tolist(), s["df"][scol].tolist())
                    if r.get("ok"):
                        conventions.record_value_equiv([tk, nm], r["mapping"], src="auto",
                                                       evidence=r.get("reason", ""))
                        n_equiv += 1
                        equiv_notes.append(f"{nm}「{scol}」↔ 模板「{tk}」：{r['reason']}")
        except Exception:
            pass

    def _src_norm(src_name, tpl_cols):
        """该源表的钥匙归一化：canon（去修饰词+数字互转）+ 该源表已学到的值等价 + 类推规律。

        返回 (模板侧函数, 源表侧函数, 结论文本)；没有任何口径信息时返回 None（保持旧行为）。
        """
        alias, rules, not_same = {}, [], []
        if conventions is not None:
            for tc in tpl_cols:
                try:
                    mp = conventions.value_mapping(tc, src_name) or {}
                except Exception:
                    mp = {}
                for k, v in mp.items():
                    ck_, cv = _canon(k), _canon(v)
                    if ck_ and cv:
                        alias[ck_] = cv
                try:
                    rules.extend(conventions.value_rules(tc, src_name) or [])
                except Exception:
                    pass
                for _k, it in ((conventions.data.get("not_same") or {}).items()):
                    if it.get("scope") == f"{tc}|{src_name}":
                        not_same.append((norm_text(it.get("a")), norm_text(it.get("b"))))
        if not alias and not rules and not not_same:
            return None
        ns_pairs = set(not_same)

        def f_tpl(v):
            s = _canon(v)
            for r in rules:
                s = _apply_rule_tpl(r.get("rule") or {}, v)
            return alias.get(s, s)

        def f_src(v):
            s = _canon(v)
            for r in rules:
                s = _apply_rule_src(r.get("rule") or {}, v)
            s = alias.get(s, s)
            for a, b in ns_pairs:                 # 人工判过"不是一回事"的 → 不许配上
                if b and s == b:
                    return _NOT_SAME_SENTINEL
            return s

        f_tpl._tag = f"convT:{src_name}:{len(alias)}/{len(rules)}/{len(not_same)}"
        f_src._tag = f"convS:{src_name}:{len(alias)}/{len(rules)}/{len(not_same)}"
        return (f_tpl, f_src, f"{src_name}：值等价{len(alias)}条、类推规律{len(rules)}条、判定不同{len(not_same)}条")

    nf_per_src, nf_notes = [], []
    for si, s in enumerate(sources):
        tpl_cols = [tk for tk, _sc, _cs in key_pairs[si]]
        got = _src_norm(s["name"], tpl_cols)
        if got:
            nf_per_src.append((got[0], got[1]))
            nf_notes.append(got[2])
        else:
            nf_per_src.append((None, None))

    # ---- 口径本②：候选列"85% 闸门"（首选列空缺时，才允许用别的候选列补）----
    # 判据：两候选列在都能取到值的行上，一致率 ≥85%（重叠 ≥10 行才判）；否则判"两套口径"，不许互补
    GATE_RATE, GATE_MIN_OVERLAP = 0.85, 10
    gates = {}            # (tcol, 候选列所属源) -> allow
    gate_blocked = []     # 被拒的（列, 原因）
    if len(sources) >= 2:
        base_avail = {}
        for i in template_df.index:
            rv = {}
            for kc in key_candidates:
                if kc in template_df.columns and _has_value(template_df.at[i, kc]):
                    rv[kc] = norm_text(template_df.at[i, kc])
            base_avail[i] = rv
        for tcol in target_cols:
            cands = sorted(list(supply.get(tcol) or []), key=lambda c: -c["score"])
            if len(cands) < 2:
                continue
            pref = cands[0]
            pref_col = {pref["source"]: pref["col"]}
            for alt in cands[1:]:
                if alt["source"] == pref["source"] and alt["col"] == pref["col"]:
                    continue
                va, vb = [], []
                for i in template_df.index:
                    rv = base_avail.get(i) or {}
                    if not rv:
                        continue
                    vals = {}
                    for cd in (pref, alt):
                        s2 = sources[cd["source"]]
                        ccol = cd["col"]
                        if ccol not in s2["df"].columns:
                            continue
                        pa = [(tk, scol, cs) for tk, scol, cs in key_pairs[cd["source"]]
                              if tk in rv and tk != tcol]
                        if not pa:
                            continue
                        m2 = _match_source(s2, pa, rv, key_min, ccol, idx_cache,
                                           norm_fn=nf_per_src[cd["source"]][0],
                                           norm_fn_src=nf_per_src[cd["source"]][1])
                        if m2 and m2[0] is not None:
                            v2 = s2["df"][ccol].iloc[m2[0]]
                            if _has_value(v2):
                                vals[cd["source"]] = v2
                    if len(vals) >= 2:
                        va.append(vals[pref["source"]])
                        vb.append(vals[alt["source"]])
                same, tot, rate = _agree_of(va, vb)
                allow = (tot < GATE_MIN_OVERLAP) or (rate >= GATE_RATE)
                gates[(tcol, alt["source"])] = allow
                if conventions is not None:
                    try:
                        conventions.record_col_verdict(tcol, sources[pref["source"]]["name"],
                                                       sources[alt["source"]]["name"],
                                                       rate, tot)
                    except Exception:
                        pass
                if not allow:
                    gate_blocked.append(
                        f"「{tcol}」：{sources[pref['source']]['name']} 与 "
                        f"{sources[alt['source']]['name']} 一致率仅 {rate * 100:.0f}%"
                        f"（{same}/{tot}）→ 判定两套口径，**拒绝互相补缺**，请定以哪张表为准")
    n_complement = 0      # 靠"次选候选列"补上的格数

    for r in range(1, int(max_rounds) + 1):
        # 本轮开始快照"可用钥匙"（本轮新补出的值下一轮才生效 → 级联逐轮推进、轮次可解释）
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

        # 表级可用钥匙数：该源表本轮真正能用上的钥匙列（模板里有值的）
        usable_cols = []
        for si in range(len(sources)):
            cols = {tk for tk, _sc, _cs in key_pairs[si]
                    if any(tk in (avail_by_row.get(i) or {}) for i in template_df.index)}
            usable_cols.append(cols)

        # 单钥匙源表：第 1 轮整表延后（等后续轮次看能不能凑到第二把）
        active = []
        for si in range(len(sources)):
            if not usable_cols[si]:
                continue
            if defer_single and r == 1 and len(usable_cols[si]) == 1:
                deferred_sources.add(si)
                continue
            active.append(si)

        progressed = False
        round_fills = []                 # 本轮补出的格，等"有效轮次"定稿后再写备注
        for tcol in target_cols:
            for i in template_df.index:
                if _has_value(result.at[i, tcol]):
                    continue
                row_vals = avail_by_row.get(i) or {}
                if not row_vals:
                    continue
                # ---- 经验库优先：人工确认过的格 → 直接填，不再猜（标 ·经验库）----
                if exp_lut:
                    ek = exp_key(i, tcol)
                    hit = exp_lut.get(ek) if ek else None
                    if hit:
                        result.at[i, tcol] = _norm_value(hit)
                        tie_cells.pop((i, tcol), None)
                        filled[(i, tcol)] = (100.0, 0, 1, 0)
                        gen[(i, tcol)] = 0
                        n_fill["ok"] += 1
                        n_exp += 1
                        round_fills.append((i, tcol, 100.0, 1, 0, "经验库"))
                        progressed = True
                        continue
                best = None
                tie_info = None
                # 候选列按**列名匹配度**降序（列名更对的先用）；该列这行取不到值/无命中才回退下一列。
                # 回退要过"85% 闸门"：两个候选列一致率 <85% → 判两套口径，**不许互相补缺**
                _cands = sorted(list(supply.get(tcol) or []), key=lambda c: -c["score"])
                for _ci, cand in enumerate(_cands):
                    si = cand["source"]
                    if si not in active:
                        continue
                    if _ci > 0 and not gates.get((tcol, si), True):
                        continue               # 口径不同 → 不互补
                    s = sources[si]
                    ccol = cand["col"]
                    if ccol not in s["df"].columns:
                        continue
                    # 匹配用钥匙：本行可得 & 不是"目标列自己"（防自引用）
                    pairs = [(tk, scol, cs) for tk, scol, cs in key_pairs[si]
                             if tk in row_vals and tk != tcol]
                    if not pairs:
                        continue
                    m = _match_source(s, pairs, row_vals, key_min, ccol, idx_cache,
                                      blank_on_tie=blank_on_tie,
                                      norm_fn=nf_per_src[si][0], norm_fn_src=nf_per_src[si][1])
                    if not m:
                        continue
                    j, sc, k_used, tie_n, exact, hit_rows = m
                    if j is None:              # 这列命中多行且取值不同 → 记候选（按列名分高者优先），继续回退
                        vals = [str(s["df"][ccol].iloc[r]) for r in hit_rows
                                if _has_value(s["df"][ccol].iloc[r])]
                        uniq = []
                        for v in vals:
                            if v not in uniq:
                                uniq.append(v)
                        if tie_info is None:
                            tie_info = (tie_n, k_used, cand["how"], uniq[:3], s["name"])
                        continue
                    v = s["df"][ccol].iloc[j]
                    if not _has_value(v):      # 这行该列为空 → 回退下一列
                        continue
                    if _code_guard_block(tcol, cand["how"], s["df"][ccol]):
                        continue               # 编号类列不吃"不像编号"的列（如 甲方编号/项目编码）
                    if tie_n and not blank_on_tie:
                        sc = min(sc, 39.0)     # 旧行为：并列且取第 1 条 → 降档
                    best = (v, sc, k_used, tie_n, cand["how"], pairs)
                    if _ci > 0:
                        n_complement += 1      # 记：这格是靠次选候选列补的
                    break
                if best:
                    v, sc, k_used, tie_n, how, pairs = best
                    used_tks = [tk for tk, _c, _s in pairs][:k_used]
                    # 折减按**跳数**：只用原值钥匙 → 不折减；用了补出来的值 → 每跳 ×0.9
                    g = max((gen.get((i, tk), 0) for tk in used_tks), default=0)
                    decayed = sc * (0.9 ** g)
                    result.at[i, tcol] = _norm_value(v)
                    tie_cells.pop((i, tcol), None)     # 之前判过"多候选"的格，这轮补上了 → 撤掉标记
                    filled[(i, tcol)] = (decayed, 0, k_used, tie_n)   # 轮次稍后回填
                    gen[(i, tcol)] = g + 1
                    n_fill[_band(decayed)] += 1
                    round_fills.append((i, tcol, decayed, k_used, tie_n, ""))
                    progressed = True
                elif tie_info is not None:
                    # 有几条候选但取值不同 → 宁可留空，交人工选（清单里写「多候选(已留空)」）
                    tie_cells[(i, tcol)] = tie_info

        if progressed:
            eff_rounds += 1
            for i, tcol, decayed, k_used, tie_n, src_tag in round_fills:
                _sc0, _r0, _k0, _t0 = filled[(i, tcol)]
                filled[(i, tcol)] = (decayed, eff_rounds, k_used, tie_n)
                tag = f"{_band(decayed)}:{decayed:.0f}@{eff_rounds}"
                if k_used > 1:
                    tag += f"·{k_used}钥匙"
                if tie_n:
                    tag += f"·歧义{tie_n}行"
                if src_tag:
                    tag += f"·{src_tag}"
                conf.at[i, tcol] = tag
        elif r == 1 and deferred_sources:
            continue                      # 第 1 轮只是"延后"，不算收敛
        else:
            break
    rounds_used = eff_rounds

    for i in template_df.index:
        for tcol in target_cols:
            if not _has_value(result.at[i, tcol]) and not _has_value(template_df.at[i, tcol]):
                conf.at[i, tcol] = "miss"
                n_fill["miss"] += 1

    indirect = sum(1 for _, (_, rr, _, _) in filled.items() if rr > 1)
    ambiguous = len(tie_cells)                  # 命中多行取值不同、最终仍留空待人工选
    n_blank = max(0, n_fill["miss"] - ambiguous)   # 纯"源表没有"的留空（护栏：不得为负）

    # ---- 候选表（供页面一键确认 → 写进经验库，下次自动填）----
    _ch_cols = ["行号", "列名", "类型", "钥匙值", "候选1", "候选2", "候选3", "备注"]
    choices = []
    for (i, tcol), (tn, k_used, how, cand_vals, sname) in tie_cells.items():
        cand_vals = list(cand_vals) + [""] * (3 - len(cand_vals))
        choices.append({"行号": i + 2, "列名": tcol, "类型": "多候选",
                        "钥匙值": _row_key_text(template_df, i, key_pairs),
                        "候选1": cand_vals[0], "候选2": cand_vals[1], "候选3": cand_vals[2],
                        "备注": f"{sname} 同键 {tn} 行取值不同"})
    for i in template_df.index:                 # 未补上 → 给最接近的 3 个候选
        for tcol in target_cols:
            if (i, tcol) in tie_cells:
                continue
            if _has_value(result.at[i, tcol]) or _has_value(template_df.at[i, tcol]):
                continue
            row_vals = {}
            for c in exp_cols:
                v = template_df.at[i, c]
                if _has_value(v):
                    row_vals[c] = norm_text(v)
            near = []
            for si, s in enumerate(sources):
                pairs = [(tk, scol, cs) for tk, scol, cs in key_pairs[si]]
                for cand in (supply.get(tcol) or []):
                    if cand["source"] != si:
                        continue
                    nv = _near_values(s, pairs, row_vals, cand["col"])
                    if nv:
                        near = [(sc, v, s["name"]) for sc, v in nv]
                        break
                if near:
                    break
            if near:
                vals = [v for _sc, v, _nm in near] + [""] * (3 - len(near))
                choices.append({"行号": i + 2, "列名": tcol, "类型": "最接近候选",
                                "钥匙值": _row_key_text(template_df, i, key_pairs),
                                "候选1": vals[0], "候选2": vals[1], "候选3": vals[2],
                                "备注": "；".join(f"{v}≈{sc:.0f}%" for sc, v, _nm in near)
                                + f"（来源：{near[0][2]}）"})
    choices_df = pd.DataFrame(choices, columns=_ch_cols) if choices \
        else pd.DataFrame(columns=_ch_cols)

    # ---- 复验：只用"同一语义的降级对照"（少用一把 / 退到单钥匙），不抽无关列 ----
    audit = {"组数": 0, "可比格": 0, "不一致": []}
    if audit_rounds and not _audit:
        alt_plans, seen_alt = [], set()
        for kind in ("drop_last", "single", "drop_first"):
            per, changed = [], False
            for si in range(len(sources)):
                main = key_pairs[si]
                if kind == "drop_last" and len(main) >= 2:
                    per.append(main[:-1]); changed = True
                elif kind == "drop_first" and len(main) >= 3:
                    per.append(main[1:]); changed = True
                elif kind == "single" and len(main) >= 2:
                    per.append(main[:1]); changed = True
                else:
                    per.append(main)
            key = tuple(tuple((p[0], p[1]) for p in pr) for pr in per)
            if changed and key not in seen_alt:
                seen_alt.add(key)
                alt_plans.append(per)
        for alt in alt_plans[:max(0, int(audit_rounds))]:
            try:
                r2 = fill_multi(template_df, key_cols=tkeys, sources=sources, mapping=mapping,
                                col_threshold=col_threshold, key_min=key_min,
                                max_rounds=max_rounds, promote_min=promote_min,
                                auto_keys=False, defer_single=defer_single,
                                audit_rounds=0, allow_domain=allow_domain,
                                key_plan=alt, _audit=True, max_keys=max_keys)
            except Exception:
                continue
            audit["组数"] += 1
            res2 = r2["result"]
            for tcol in target_cols:
                for i in template_df.index:
                    a1, a2 = result.at[i, tcol], res2.at[i, tcol]
                    if not _has_value(a1) or not _has_value(a2):
                        continue          # 降级后"定不出来"不是问题（说明第二把钥匙在起作用）
                    audit["可比格"] += 1
                    if norm_text(a1) != norm_text(a2):
                        audit["不一致"].append({"行号": i + 2, "列名": tcol,
                                                "钥匙值": _row_key_text(template_df, i, key_pairs),
                                                "主结果": a1, "复验结果": a2})
    # ---- 需要人工确认的"值域"（只问值域；一次确认 → 整列类推）----
    try:
        value_questions = build_value_questions(template_df, sources, key_pairs,
                                                conventions, topn=10)
    except Exception:
        value_questions = []

    n_cmp = audit["可比格"]
    n_diff = len(audit["不一致"])
    consistency = (1.0 - n_diff / n_cmp) if n_cmp else 1.0

    # ---- 多源交叉核对（**独立复核**：两张源表都能供同一列时，比对两源给的值）----
    # 两源都有值且不同 = 矛盾。**按列自校准**：某列矛盾率 >50% → 判为"同名不同口径"，
    # 只给一行提示、不逐格列（否则 100+ 行会把人工清单淹掉）；≤50% 才逐格列。
    cross = {"可比格": 0, "矛盾": [], "口径不同": []}
    _multi_cols = [c for c in target_cols
                   if len({cd["source"] for cd in (supply.get(c) or [])}) >= 2]
    if _multi_cols:
        final_avail = {}
        for i in template_df.index:
            rv = {}
            for kc in key_candidates:
                if kc in result.columns and _has_value(result.at[i, kc]):
                    sc0 = 100.0 if (i, kc) not in filled else filled[(i, kc)][0]
                    if sc0 >= promote_min:
                        rv[kc] = norm_text(result.at[i, kc])
            final_avail[i] = rv
        for tcol in _multi_cols:
            best_col_of = {}
            for cd in sorted(list(supply.get(tcol) or []), key=lambda c: -c["score"]):
                best_col_of.setdefault(cd["source"], cd["col"])
            cmp_n = 0
            bad = []
            for i in template_df.index:
                if not final_avail.get(i):
                    continue
                got = {}
                for si, ccol in best_col_of.items():
                    s = sources[si]
                    if ccol not in s["df"].columns:
                        continue
                    pairs = [(tk, scol, cs) for tk, scol, cs in key_pairs[si]
                             if tk in final_avail[i] and tk != tcol]
                    if not pairs:
                        continue
                    m = _match_source(s, pairs, final_avail[i], key_min, ccol, idx_cache)
                    if not m or m[0] is None or not m[4]:
                        continue          # 只在"钥匙精确命中"时才做两源比对（否则是拿苹果比橘子）
                    v = s["df"][ccol].iloc[m[0]]
                    if _has_value(v):
                        got[si] = v
                if len(got) >= 2:
                    cmp_n += 1
                    items = list(got.items())
                    if any(norm_text(items[0][1]) != norm_text(v) for _si, v in items[1:]):
                        bad.append({"行号": i + 2, "列名": tcol,
                                    "钥匙值": _row_key_text(template_df, i, key_pairs),
                                    "源A（值）": f"{sources[items[0][0]]['name']}：{items[0][1]}",
                                    "源B（值）": "；".join(f"{sources[si]['name']}：{v}"
                                                          for si, v in items[1:])})
            cross["可比格"] += cmp_n
            if cmp_n >= 10 and len(bad) / cmp_n > 0.3:
                # 大面积不一致 → 疑似"同名不同口径"，只提示、不逐格
                _ex = bad[0]
                cross["口径不同"].append(
                    f"「{tcol}」：{len(bad)}/{cmp_n} 格两源不一致 → 疑似两张表**口径不同**"
                    f"（例：{_ex['源A（值）']} vs {_ex['源B（值）']}）；已按列名匹配度取一家，"
                    f"你若确认口径，可在源表里改列名区分")
            else:
                cross["矛盾"].extend(bad)
    _cross_cols = ["行号", "列名", "钥匙值", "源A（值）", "源B（值）"]
    cross_df = pd.DataFrame(cross["矛盾"], columns=_cross_cols) if cross["矛盾"] \
        else pd.DataFrame(columns=_cross_cols)
    n_cross_cmp = cross["可比格"]
    n_cross_bad = len(cross["矛盾"])
    cross_rate = (1.0 - n_cross_bad / n_cross_cmp) if n_cross_cmp else 1.0

    # ---- 需人工确认清单：只收「必看」（多候选留空 / 未补上）----
    # 「复验不一致」本质是"少一把钥匙→配到别的行"的预期差异，对用户没有可执行动作 →
    # 不进主清单，只留质量分 + 明细（audit_detail，页面折叠/导出第三个 Sheet）
    review = []
    for (i, tcol), (tn, k_used, _how, _cv, _sn) in tie_cells.items():
        review.append({"类型": f"多候选(已留空,{tn}行取值不同)", "行号": i + 2, "列名": tcol,
                       "钥匙值": _row_key_text(template_df, i, key_pairs), "主结果": ""})
    for i in template_df.index:
        for tcol in target_cols:
            if (i, tcol) in tie_cells:
                continue
            if not _has_value(result.at[i, tcol]) and not _has_value(template_df.at[i, tcol]):
                review.append({"类型": "未补上", "行号": i + 2, "列名": tcol,
                               "钥匙值": _row_key_text(template_df, i, key_pairs), "主结果": ""})
    _rev_cols = ["类型", "行号", "列名", "钥匙值", "主结果"]
    if review:
        review.sort(key=lambda r: (0 if str(r.get("类型", "")).startswith("多候选") else 1,
                                   r.get("行号", 0), str(r.get("列名", ""))))
        review_df = pd.DataFrame(review, columns=_rev_cols)
    else:
        review_df = pd.DataFrame(columns=_rev_cols)
    _aud_cols = ["行号", "列名", "钥匙值", "主结果", "复验结果"]
    audit_df = pd.DataFrame(audit["不一致"], columns=_aud_cols) if audit["不一致"] \
        else pd.DataFrame(columns=_aud_cols)

    # ---- 抽查清单：所有**非 100%** 的填充格（模糊/多钥匙/级联 → 都要让人能看到）----
    _pt_cols = ["行号", "列名", "值", "置信", "钥匙值"]
    partial = []
    for (i, tcol), (sc0, _rr, _k, _t) in filled.items():
        if _band(sc0) == "ok":
            continue
        partial.append({"行号": i + 2, "列名": tcol, "值": result.at[i, tcol],
                        "置信": conf.at[i, tcol],
                        "钥匙值": _row_key_text(template_df, i, key_pairs)})
    partial.sort(key=lambda r: (r["行号"], str(r["列名"])))
    partial_df = pd.DataFrame(partial, columns=_pt_cols) if partial \
        else pd.DataFrame(columns=_pt_cols)

    # 每张源表实际用了哪几把钥匙（给界面显示）
    key_used_desc = [(sources[si]["name"],
                      "、".join(str(tk) for tk, _c, _s in key_pairs[si]) or "—")
                     for si in range(len(sources))]

    stats = {"模板行数": len(template_df), "目标列数": len(target_cols),
             "可填格": len(template_df) * len(target_cols),
             "完全匹配(100%)": n_fill["ok"], "高置信(80-99%)": n_fill["high"],
             "中置信(40-80%)": n_fill["mid"], "低置信(20-40%)": n_fill["low"],
             "未匹配(留空)": n_blank,
             "级联轮数": rounds_used, "间接补全格数": indirect, "歧义格数": ambiguous,
             "留空合计": n_fill["miss"],
             "延后源表数": len(deferred_sources),
             "经验库命中": n_exp,
            "非100%格数": n_fill["high"] + n_fill["mid"] + n_fill["low"],
            "值等价学习(条)": n_equiv, "互补格数": n_complement,
            "拒绝互补列": list(gate_blocked), "口径说明": list(equiv_notes),
             "复验组数": audit["组数"], "复验可比格": n_cmp, "复验不一致格": n_diff,
             "复验一致率": round(consistency * 100, 1),
             "两源可核对格": n_cross_cmp, "两源矛盾格": n_cross_bad,
             "两源一致率": round(cross_rate * 100, 1),
             "两源口径不同列": list(cross.get("口径不同") or []),
             "需人工确认行数": len(review_df),
             "实际钥匙列": key_used_desc, "钥匙说明": plan["notes"],
             "未补全行数": int((conf[target_cols] == "miss").any(axis=1).sum()) if target_cols else 0}
    return {"result": result, "confidence": conf, "stats": stats,
            "supply": supply, "legend": legend_lines(stats), "key_pairs": key_pairs,
            "review": review_df, "audit": audit_df, "choices": choices_df,
            "partial": partial_df, "cross": cross_df, "exp_key_cols": exp_cols,
            "value_questions": value_questions}


def build_value_questions(template_df, sources, key_pairs, conventions=None,
                          topn=10, lo=40.0):
    """生成"值域待确认清单"（只问值域，且**优先问"能类推整列"的**）。

    每组 (模板列 ↔ 源表.源列) 只出一题：
    - 类型"整列"：找到一对能推出**可整列自证**的规律（如 去后缀「事业部」+ 数字互换）
      → 你点"是"，这一列以后全部 100%，同类不再问
    - 类型"特例"：没有能通用自证的规律，但有一对相似度 ≥60 的
      → 你点"是"，只记这一对；点"不是"，记住"这两回事"
    - 相似度太低（<40）→ 当"源表没有"，直接留空、不打扰
    """
    from collections import Counter
    from .conventions import infer_rule, verify_rule
    qs = []
    for si, s in enumerate(sources):
        nm = s["name"]
        for tk, scol, _cs in key_pairs[si]:
            if tk not in template_df.columns or scol not in s["df"].columns:
                continue
            if conventions is not None:
                try:
                    if conventions.value_rules(tk, nm):
                        continue                      # 已有类推规律 → 整列自动，不再问
                except Exception:
                    pass
            tvals = [str(v) for v in template_df[tk].tolist() if _has_value(v)]
            svals = [str(v) for v in s["df"][scol].tolist() if _has_value(v)]
            if not tvals or not svals:
                continue
            done = set()
            if conventions is not None:
                try:
                    done = {norm_text(k) for k in (conventions.value_mapping(tk, nm) or {})}
                except Exception:
                    done = set()
            su = {norm_text(x) for x in svals}
            cnt = Counter(norm_text(x) for x in tvals)
            cands = []
            for v, c in cnt.items():
                if v in su or v in done:
                    continue
                sc, sv = max(((_key_sim(v, x), x) for x in su), default=(0.0, ""))
                if sc >= lo:
                    cands.append((sc, v, sv, c))
            if not cands:
                continue
            cands.sort(reverse=True)
            # ① 先找"能整列自证"的规律（这才是值得问的）
            rule_q = None
            for sc, v, sv, c in cands[:8]:
                rule = infer_rule(v, sv)
                if not rule:
                    continue
                ok, cov, why = verify_rule(rule, tvals, svals)
                if ok:
                    rule_q = {"类型": "整列", "列名": tk, "源表": nm, "源列": scol,
                              "示例模板值": v, "示例源表值": sv, "规律": rule,
                              "覆盖率": round(cov * 100, 1), "相似度": round(sc, 1),
                              "影响格数": c, "同类数": len(cands),
                              "模板前5": [x for x, _n in cnt.most_common(5)],
                              "源前5": sorted(su)[:5]}
                    break
            if rule_q:
                qs.append(rule_q)
                continue
            # ② 退而求其次：单值特例（相似度 ≥lo 就问 —— "看着像但不是"的也要让你能判"不是"）
            sc, v, sv, c = cands[0]
            if sc >= lo:
                qs.append({"类型": "特例", "列名": tk, "源表": nm, "源列": scol,
                           "示例模板值": v, "示例源表值": sv, "规律": None,
                           "覆盖率": 0.0, "相似度": round(sc, 1),
                           "影响格数": c, "同类数": len(cands),
                           "模板前5": [x for x, _n in cnt.most_common(5)],
                           "源前5": sorted(su)[:5]})
    qs.sort(key=lambda q: (0 if q["类型"] == "整列" else 1, -q["相似度"], -q["影响格数"]))
    return qs[:topn]


def apply_value_answers(template_df, sources, answers, conventions):
    """把"值域确认"的回答落进口径本（**一条回答解决一类**）。

    answers: {(模板列, 源表名): "same"|"not"|"skip"}（问题里带的"规律"会被直接用）
    返回已应用清单 [(类型, 说明)]。
    """
    from .conventions import infer_rule, verify_rule
    by_name = {s["name"]: s for s in sources}
    applied = []
    for key, ans in (answers or {}).items():
        if isinstance(key, (list, tuple)) and len(key) == 2:
            tcol, sname = key
        else:
            continue
        if isinstance(ans, dict):
            ans_val, rule = ans.get("ans"), ans.get("rule")
        else:
            ans_val, rule = ans, None
        if ans_val in (None, "", "skip"):
            continue
        s = by_name.get(sname)
        if s is None:
            continue
        tvals = [str(v) for v in template_df[tcol].tolist() if _has_value(v)] \
            if tcol in template_df.columns else []
        cand = None
        for c in s["df"].columns:
            sc, _how = col_match(tcol, c, 50.0)
            if cand is None or sc > cand[1]:
                cand = (c, sc)
        if not cand or not tvals:
            continue
        scol = cand[0]
        svals = [str(v) for v in s["df"][scol].tolist() if _has_value(v)]
        if ans_val == "not":
            t0 = norm_text(tvals[0])
            for sv in {norm_text(x) for x in svals}:
                if _key_sim(t0, sv) >= 40:
                    conventions.record_not_same([tcol, sname], t0, sv)
            applied.append(("判不同", f"「{tcol}」← {sname}：已记住「这是两回事」，不再问、也不许模糊配上"))
            continue
        # same
        if rule:                                   # 问题里已验证过的规律 → 直接整列类推
            ok, cov, why = verify_rule(rule, tvals, svals)
            if ok:
                conventions.record_value_rule([tcol, sname], rule,
                                              evidence=f"整列类推（{why}，覆盖 {cov:.0%}）")
                applied.append(("类推整列",
                                f"「{tcol}」← {sname}：规律={rule.get('kind')}，"
                                f"整列通用（{cov:.0%}），以后 100%"))
                continue
        pair = None                                # 特例：找一对最像的
        for tv in {norm_text(x) for x in tvals}:
            sc, sv = max(((_key_sim(tv, x), x) for x in {norm_text(y) for y in svals}),
                         default=(0.0, ""))
            if pair is None or sc > pair[0]:
                pair = (sc, tv, sv)
        if not pair or not pair[2]:
            continue
        r2 = infer_rule(pair[1], pair[2])
        if r2:
            ok, cov, why = verify_rule(r2, tvals, svals)
            if ok:
                conventions.record_value_rule([tcol, sname], r2,
                                              evidence=f"整列类推（{why}，覆盖 {cov:.0%}）")
                applied.append(("类推整列",
                                f"「{tcol}」← {sname}：规律={r2.get('kind')}，整列通用（{cov:.0%}）"))
                continue
            why = f"规律不通用（{why}）"
        else:
            why = "推不出通用规律"
        conventions.record_value_equiv([tcol, sname], {pair[1]: pair[2]},
                                       evidence=f"特例：{why}")
        applied.append(("特例", f"「{tcol}」← {sname}：{pair[1]} = {pair[2]}（{why}）"))
    return applied


def _row_key_text(template_df, i, key_pairs):
    """清单里显示该行的钥匙值（各源表第一把钥匙的模板列）。"""
    names = []
    for pr in key_pairs:
        if pr and str(pr[0][0]) not in names:
            names.append(str(pr[0][0]))
    vals = []
    for nm in names[:3]:
        if nm in template_df.columns and _has_value(template_df.at[i, nm]):
            vals.append(f"{nm}={template_df.at[i, nm]}")
    return " | ".join(vals)


def preview_keys(template_keys, sources, col_threshold=COL_DEFAULT_THRESHOLD,
                 template_df=None, allow_domain=True):
    """界面预览：每张源表自动识别到的钥匙列 → [(源表名, [(模板钥匙列, 源列, 分)])]。"""
    return [(s["name"], _auto_key_pairs(list(template_keys), s, col_threshold, s.get("key_col"),
                                        template_df, allow_domain))
            for s in sources]


def supply_option_label(cand, sources):
    """「列供给」下拉标签：源表名【源列】(匹配方式,分数)。（候选字典无 name 字段，需查 sources）"""
    si = cand.get("source", 0)
    sname = sources[si]["name"] if isinstance(si, int) and 0 <= si < len(sources) else "?"
    return f"{sname}【{cand.get('col', '')}】({cand.get('how', '')},{cand.get('score', 0):.0f})"


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
                    if _name_looks_date(tcol) and not _looks_date_col(df[col])[0]:
                        continue                      # 类型守卫：日期类目标列不吃状态/文本列
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
        "颜色图例：无色=完全匹配(100%) 或 模板本来就有的值（工具没动）；浅黄=高置信(80–99%)；"
        "浅蓝=中置信(40–80%)；浅紫=低置信(20–40%)；浅灰底空格=未匹配（留空）",
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
        extra.append(f"有 {stats['歧义格数']} 格命中多行且取值不同 → **已留空**（见「需人工确认」清单，选一条填）")
    if stats.get("延后源表数"):
        extra.append(f"有 {stats['延后源表数']} 张源表第 1 轮延后（只有 1 把钥匙，等后续轮次凑第二把）")
    if stats.get("复验组数"):
        extra.append(f"复验（仅开发回测）：另用「少一把钥匙」的对照跑了 {stats['复验组数']} 遍，"
                     f"可比 {stats.get('复验可比格', 0)} 格、一致率 {stats.get('复验一致率', 100)}%"
                     f"（不一致 {stats.get('复验不一致格', 0)} 格属于「少钥匙→配到别的行」的预期差异）")
    if stats.get("两源可核对格"):
        extra.append(f"两源交叉核对：{stats['两源可核对格']} 格有两张源表都能供（都精确命中才比），"
                     f"其中矛盾 {stats.get('两源矛盾格', 0)} 格（一致率 {stats.get('两源一致率', 100)}%）"
                     + ("，见「两源矛盾」Sheet / 页面抽查区" if stats.get("两源矛盾格") else ""))
    for _line in (stats.get("两源口径不同列") or []):
        extra.append("两源" + _line)
    if stats.get("需人工确认行数"):
        extra.append(f"需人工确认：{stats['需人工确认行数']} 行（多候选留空 / 未补上，见第二个 Sheet）")
    return lines + extra


def export_filled(result_df, conf_df, out_path, stats=None, extra_notes=None, review_df=None,
                  audit_df=None, cross_df=None):
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

    # 需人工确认清单（只在有内容时加这个 Sheet）
    try:
        if review_df is not None and len(review_df):
            ws2 = wb.create_sheet("需人工确认")
            ws2.append([str(c) for c in review_df.columns])
            for _, row in review_df.iterrows():
                ws2.append(["" if (v is None or (not isinstance(v, str) and pd.isna(v))) else v
                            for v in row])
            _style(ws2, 1, highlight_min=False)
        if cross_df is not None and len(cross_df):
            ws4 = wb.create_sheet("两源矛盾")
            ws4.append([str(c) for c in cross_df.columns])
            for _, row in cross_df.iterrows():
                ws4.append(["" if (v is None or (not isinstance(v, str) and pd.isna(v))) else v
                            for v in row])
            _style(ws4, 1, highlight_min=False)
        if audit_df is not None and len(audit_df):
            ws3 = wb.create_sheet("复验存疑")
            ws3.append([str(c) for c in audit_df.columns])
            for _, row in audit_df.iterrows():
                ws3.append(["" if (v is None or (not isinstance(v, str) and pd.isna(v))) else v
                            for v in row])
            _style(ws3, 1, highlight_min=False)
    except Exception:
        pass
    wb.save(out_path)
    return out_path
