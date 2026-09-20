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
import difflib
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
PROMOTE_MIN = 80.0        # 补出的值置信 ≥ 此分才允许"升级为钥匙"（级联用）
TIE_STOP_MIN = 80.0       # 钥匙命中多行时：≥此分视为"真歧义"（留空、不回退）；<此分当没命中
MAX_ROUNDS = 4            # 级联最大轮数（提前收敛：某轮无新增即停）
GATE_RATE = 0.85          # 85% 互补闸门：次选候选列与首选列一致率 ≥85%（重叠≥10行）才允许互补
GATE_MIN_OVERLAP = 10
EXP_NS = "多表补全"        # 经验库命名空间（与两表匹配的键区分开）
_NOT_SAME_SENTINEL = "\x00NOTSAME"   # 人工判过"不是一回事"的源值 → 归一到这里（与任何真实值都不相似）

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


# ---------- 结构型值（编号/日期/金额/纯数字）：格式归一后只认精确，模糊分不适用 ----------
_FW_TRANS = str.maketrans(
    "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
    "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ（）／－．，：＃％＋",
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz()/-.,:#%+")
_DATE_RE = re.compile(r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?(?:[ T].*)?$")
_HEAD_DATE_RE = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")   # 时间戳被去空格后的兜底
_YM_RE = re.compile(r"^(\d{4})[-/.年](\d{1,2})月?$")
_Y8_RE = re.compile(r"^(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])$")
_NUM_RE = re.compile(r"^[-+]?[\d,，\s]*(?:\.\d+)?\s*%?$")
_CODE_RE = re.compile(r"^(?=[A-Za-z0-9\-_/#.·+]*\d)(?=[A-Za-z0-9\-_/#.·+]*[A-Za-z])"
                      r"[A-Za-z0-9\-_/#.·+]{4,}$")


def value_kind(v):
    """值类型：date（日期/年月，含 2026-01-01、2026/1/1、2026年1月1日、2026-11、时间戳）
    / num（金额、纯数字、百分比）/ code（字母+数字编号，如 YC-…-0001、GYS055410）/ text。"""
    s = _fw2hw(str(v)).strip()
    if _DATE_RE.match(s) or _HEAD_DATE_RE.match(s) or _Y8_RE.match(s):
        return "date"
    if _YM_RE.match(s):
        return "date"
    if _NUM_RE.match(s):
        return "num"
    if _CODE_RE.match(s):
        return "code"
    return "text"


def _fw2hw(s):
    return str(s).translate(_FW_TRANS)


def _struct_norm(v):
    """结构型值的格式归一：日期→YYYY-MM-DD（丢时间部分）、金额去千分位并统一小数、
    编号全大写去空格。归一后相等 = 同一个值（100 分）。"""
    s = _fw2hw(str(v)).strip()
    k = value_kind(s)
    if k == "date":
        m0 = _HEAD_DATE_RE.match(s)          # 覆盖时间戳被去空格的情况（2026-01-0100:00:00）
        if m0:
            return "%s-%02d-%02d" % (m0.group(1), int(m0.group(2)), int(m0.group(3)))
        m = _DATE_RE.match(s)
        if m:
            return "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3)))
        m2 = _YM_RE.match(s)
        if m2:
            return "%s-%02d" % (m2.group(1), int(m2.group(2)))
        if _Y8_RE.match(s):
            return "%s-%s-%s" % (s[:4], s[4:6], s[6:8])
        return s
    if k == "num":
        s = s.replace(",", "").replace("，", "").replace(" ", "")
        try:
            f = float(s.rstrip("%"))
            s = ("%d" % int(f)) if f == int(f) else ("%g" % f)
            return s + ("%" if str(v).strip().endswith("%") else "")
        except ValueError:
            return s
    if k == "code":
        return s.upper().replace(" ", "")
    return s


# ---------- 区分位差异：差异全落在数字/序数/方位/单位词上 → 不许靠"像"猜行 ----------
_DISC_CHARS = set(
    "0123456789０１２３４５６７８９"
    "零一二两三四五六七八九十百千"
    "期栋幢座楼层单元室号＃#地块组团标段批季甲乙丙丁"
    "东南西北"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")


def _diff_discriminating(a, b):
    """两个归一值的所有差异是否**全部**落在「区分位」字符上。

    例：叠溪花园3 vs 叠溪花园4 ✓；…0001 vs …0002 ✓；楼 vs 搂（含非区分位）→ False。
    """
    if a == b or not a or not b:
        return False
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        seg = a[i1:i2] + b[j1:j2]
        for ch in seg:
            if ch not in _DISC_CHARS:
                return False
    return True


def _disc_series(vals):
    """值域里互为"区分位兄弟"的值集合：差异全落在区分位（叠溪三期↔四期、…0001↔…0002）。

    只有当兄弟值**真实存在**时，这类差异才被禁止用来"猜行"（避免过度收紧）。
    """
    out = set()
    vals = [v for v in vals if v]
    if len(vals) < 2:
        return frozenset()
    try:
        from rapidfuzz import fuzz as _fz, process as _pr
        for a in vals:
            for _s, b, _i in _pr.extract(a, vals, scorer=_fz.ratio,
                                         score_cutoff=70, limit=6):
                if b != a and _diff_discriminating(a, b):
                    out.add(a)
                    out.add(b)
                    break
    except ImportError:
        for i, a in enumerate(vals):
            for b in vals[i + 1:]:
                if _name_sim(a, b)[0] >= 70 and _diff_discriminating(a, b):
                    out.add(a)
                    out.add(b)
    return frozenset(out)


# ---------- 修饰词剥离（值域碰撞裁决：剥了会撞上另一个真值 → 不许剥） ----------
_MODIFIER_TOKENS = ("项目", "工程", "服务", "物业服务", "管理服务", "服务项目", "采购项目",
                    "委外", "外包", "采购", "合同", "协议", "标段", "包", "批次", "年度",
                    "服务费", "（）")
_STRIP_EDGES = "-—－_·.、,，()（）[]【】 "


def _strip_once(s, tok):
    """按单个修饰词剥一次（尾优先，再头）；tok=「（）」表示剥尾部括注。返回 None 表示没剥。"""
    if tok == "（）":
        for cl, op in (("）", "（"), (")", "(")):
            if s.endswith(cl):
                k = s.rfind(op)
                if k > 0:
                    return s[:k].strip(_STRIP_EDGES)
        return None
    if s.endswith(tok) and len(s) > len(tok):
        return s[:-len(tok)].strip(_STRIP_EDGES)
    if s.startswith(tok) and len(s) > len(tok):
        return s[len(tok):].strip(_STRIP_EDGES)
    return None


def _strip_tokens(s, tokens):
    """把已裁决"可剥"的修饰词从首尾剥掉（可叠层：绿化养护委外服务 → 绿化养护）。"""
    s = s.strip(_STRIP_EDGES)
    for _ in range(6):
        hit = False
        for t in tokens:
            r = _strip_once(s, t)
            if r is not None and r:
                s = r
                hit = True
        if not hit:
            break
    return s


def _strippable_tokens(tpl_dom, src_dom):
    """值域碰撞裁决：返回**可剥**的修饰词集合。

    碰撞规则（**各表内部**判）：同一张表里同时存在「X」和「X+修饰词」（如 绿化养护 和
    绿化养护委外 都在同一列）→ 该修饰词是区分性的 → 不许剥；
    跨表一边「X」一边「X+修饰词」恰恰是**同一实体的两种写法** → 允许剥（剥完精确命中）。
    只收常见无歧义词，最终由真值裁决。
    """
    ok = []
    for t in _MODIFIER_TOKENS + ("（）",):
        conflict = _affix_collides(tpl_dom, t) or _affix_collides(src_dom, t)
        if not conflict:
            ok.append(t)
    return tuple(ok)


def _affix_collides(dom, tok):
    """同一列值域里是否存在「X」与「X+tok」两种形态并存（并存 = tok 是区分性的）。"""
    domset = {d for d in dom if d}
    if len(domset) < 2:
        return False
    for v in domset:
        r = _strip_once(v, tok)
        if r is not None and r and r in domset:
            return True
    return False


def _kind_of(v):
    """值类型判定（**用原值判**，norm_text 会把 x/X→* 破坏编号形态）；
    引擎归一后的结构值带 "date:/num:/code:" 前缀，这里先剥前缀再判。"""
    s = _fw2hw(str(v)).strip()
    for p in ("date:", "num:", "code:"):
        if s.startswith(p):
            return p[:-1], s[len(p):]
    return value_kind(s), s


def _key_sim(a, b):
    """钥匙相似度（专用）：
    - 结构型（编号/日期/金额/纯数字）：**格式归一后只认精确**（相等=100，否则=0，模糊分不适用）
    - 文本型：canon 归一（中文数字↔阿拉伯+修饰词）相等 = 100（**默认启用**，1事业部=一事业部）
    - 其余：模糊相似度（"部分/字符袋"可助分但**不满分**，避免 `A公司` vs `A公司集团` 静默满分）
    """
    ka, va = _kind_of(a)
    kb, vb = _kind_of(b)
    if ka != "text" or kb != "text":
        if ka != kb:
            return 0.0
        return 100.0 if _struct_norm(va) == _struct_norm(vb) else 0.0
    na, nb = norm_text(va), norm_text(vb)
    if not na or not nb:
        return 0.0
    ca, cb = _canon(na), _canon(nb)
    if ca == cb:
        return 100.0
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


def exp_key_of(vals):
    """经验库键：归一化后 \x1f 连接（与 ExperienceStore 存库口径一致）。"""
    try:
        from .experience import norm_vals as _exp_norm
        return _exp_norm(vals)
    except Exception:
        return "\x1f".join("" if v is None else str(v) for v in vals)


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
                  norm_fn=None, norm_fn_src=None, sib=None):
    """按"钥匙层次 n→1"在源表里找最佳行；层内先精确（索引）后模糊。

    pairs: [(模板钥匙列, 源列, 列名匹配度)]（按模板钥匙列顺序）
    row_vals: {模板钥匙列: 归一化值}（本行可用且达标的钥匙）
    norm_fn / norm_fn_src: 模板侧 / 源表侧 归一化——可为 {模板列: 函数}（按列，含口径本
      规律与碰撞裁决过的修饰词剥离）或单个函数；None = norm_text。
    sib: {源列: 区分位兄弟值集合}——模糊差异全落在区分位（数字/序数…）且该值确有兄弟 → 该行**不猜**。
    返回 (row, score, k_used, tie_n, all_exact, rows) 或 None。
    """

    def _nf_of(fnmap, tk, dflt):
        if isinstance(fnmap, dict):
            return fnmap.get(tk) or dflt
        return fnmap or dflt

    nf = norm_fn if not isinstance(norm_fn, dict) else None
    nfs = norm_fn_src if not isinstance(norm_fn_src, dict) else None
    nf = nf or norm_text
    nfs = nfs or nf
    avail = [(tk, scol) for tk, scol, _ in pairs if row_vals.get(tk)]
    if not avail:
        return None
    df = src["df"]
    for k in range(len(avail), 0, -1):
        sub = avail[:k]
        nf_list = [_nf_of(norm_fn, tk, nf) for tk, _c in sub]
        nfs_list = [_nf_of(norm_fn_src, tk, nfs) for tk, _c in sub]
        want = tuple(nf_list[x](row_vals[sub[x][0]]) for x in range(k))
        ck = (id(df), tuple(scol for _, scol in sub),
              tuple(getattr(f, "_tag", "raw") for f in nf_list),
              tuple(getattr(f, "_tag", "raw") for f in nfs_list))
        idx = idx_cache.get(ck)
        if idx is None:
            cols = [df[c].tolist() for _, c in sub]
            idx = {}
            for j in range(len(df)):
                t = tuple(nfs_list[x](cols[x][j]) for x in range(k))
                if all(t):
                    idx.setdefault(t, []).append(j)
            idx_cache[ck] = idx
        rows = idx.get(want, [])
        if rows:
            return _pick_row(rows, df, target_col, 100.0, k, True, blank_on_tie)
        best_score, best_rows = 0.0, []
        for j in range(len(df)):
            vals = [nfs_list[x](df[sub[x][1]].iloc[j]) for x in range(k)]
            if not all(vals):
                continue
            if sib and _has_disc_conflict(vals, want, sub, sib):
                continue               # 差异全落在区分位且有兄弟值 → 这行不可信，不猜
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


def _has_disc_conflict(vals, want, sub, sib):
    """该候选行是否不可信：任一钥匙的模糊差异**全落在区分位**（数字/序数/方位…）
    且该源值在值域里确有"区分位兄弟"（如 三期/四期 都真实存在）→ 不许靠"像"猜行。"""
    for x in range(len(sub)):
        sv = sib.get(sub[x][1])
        if not sv or vals[x] == want[x]:
            continue
        if vals[x] in sv and _diff_discriminating(want[x], vals[x]):
            return True
    return False


@skill(
    name="多表补全",
    desc="按模板把多张源表汇总补齐：自动配钥匙列(≤4，含值域识别) + 单钥匙源表延后 + 值域/列口径确认 + 置信分档上色",
    inputs={"template_df": "模板表", "key_cols": "模板钥匙列(≤4，按优先级，可留空交给自动)",
            "sources": "[{name, df}]（源表钥匙由系统自动识别）", "mapping": "模板列→源列（可选覆盖）",
            "col_threshold": "列名匹配阈值(默认70)", "key_min": "钥匙最低分(默认20)",
            "max_rounds": "级联最大轮数(默认4)", "promote_min": "升级为钥匙的置信门槛(默认80)",
            "auto_keys": "自动补钥匙列(默认开)", "defer_single": "单钥匙源表第1轮延后(默认开)",
            "conventions": "口径本（列配对/值等价/类推规律/判定不同）"},
    outputs={"result": "补全后的表", "confidence": "逐格置信度(含轮次)", "stats": "统计",
             "supply": "列供给", "legend": "图例/说明", "review": "需人工确认清单"},
    task_modes=["多表补全"],
)
def fill_multi(template_df, key_col=None, sources=None, mapping=None,
               col_threshold=COL_DEFAULT_THRESHOLD, key_min=KEY_MIN,
               key_cols=None, max_rounds=MAX_ROUNDS, promote_min=PROMOTE_MIN,
               auto_keys=True, defer_single=True,
               allow_domain=True, key_plan=None, max_keys=4,
               blank_on_tie=True, experience=None, conventions=None):
    """多源 → 模板 单向填充（自动配钥匙 + 分层钥匙 + 级联 + 值域确认）。

    - 模板钥匙列 ≤4 个（你点的列优先；不够时自动按"歧义率低→覆盖率高→列数少"补位）
    - 源表钥匙自动配对：列名（同名/同义/近似）→ 不达标再试**值域指纹**
    - 单钥匙源表：第 1 轮整表延后，第 2 轮起能凑到 ≥2 把就用复合钥匙，仍只有 1 把就按 1 把匹配
    - 级联：最多 max_rounds 轮，某轮无新增即停；补出的值 ≥ promote_min 才可当钥匙（×0.9/跳）
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
    # ---- 口径本里"人工确认过的列配对"：强制生效（可当钥匙）----
    if conventions is not None:
        for si, s in enumerate(sources):
            nm = s["name"]
            for tk in [c for c in template_df.columns]:
                try:
                    cp = conventions.col_pair(tk, nm)
                except Exception:
                    cp = None
                if not cp:
                    continue
                col = cp.get("col")
                if col not in s["df"].columns:
                    continue
                key_pairs[si] = [(a, b, c) for a, b, c in key_pairs[si] if a != tk]
                key_pairs[si].append((tk, col, 100.0))
                if tk not in key_candidates:
                    key_candidates.append(tk)
    supply = discover_supply(target_cols, sources, col_threshold, mapping)

    result = template_df.copy()
    conf = pd.DataFrame("", index=template_df.index, columns=list(result.columns))
    filled = {}          # (i, col) -> (decayed_score, 有效轮次, k_used, tie_n)
    hit_trust = {}       # (i, col) -> (匹配分, used_tks)：链式印证用（精确命中 + 钥匙格可信 → 继承）
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

    def _src_norm(si):
        """该源表各钥匙列的归一化（**默认启用，不再等口径本学过**）：

        canon（中文数字↔阿拉伯+首尾修饰词）→ 修饰词剥离（值域碰撞裁决：剥了会撞上
        另一个真值的不剥，如 绿化养护 与 绿化养护委外 并存 → "委外"不许剥）
        → 结构型格式归一 → 该源表已学到的值等价 / 类推规律 / 判定不同。
        返回 (模板侧{列:fn}, 源表侧{列:fn}, 结论文本)。
        """
        s = sources[si]
        nm = s["name"]
        fmap_t, fmap_s, _n_mod = {}, {}, 0
        _n_alias = _n_rules = _n_ns = 0
        for tk, scol, _cs in key_pairs[si]:
            if tk not in template_df.columns or scol not in s["df"].columns:
                continue
            alias, rules, not_same = {}, [], []
            if conventions is not None:
                try:
                    mp = conventions.value_mapping(tk, nm) or {}
                except Exception:
                    mp = {}
                for k, v in mp.items():
                    ck_, cv = _canon(k), _canon(v)
                    if ck_ and cv:
                        alias[ck_] = cv
                try:
                    rules.extend(conventions.value_rules(tk, nm) or [])
                except Exception:
                    pass
                for _k, it in ((conventions.data.get("not_same") or {}).items()):
                    if it.get("scope") == f"{tk}|{nm}":
                        not_same.append((norm_text(it.get("a")), norm_text(it.get("b"))))
            dom_t, dom_s = set(), set()
            for v in template_df[tk].tolist():
                if _has_value(v):
                    dom_t.add(_canon(norm_text(v)))
            for v in s["df"][scol].tolist():
                if _has_value(v):
                    dom_s.add(_canon(norm_text(v)))
            dom_t.discard("")
            dom_s.discard("")
            strippable = (_strippable_tokens(dom_t, dom_s)
                          if 0 < len(dom_t) + len(dom_s) <= 6000 else ())
            ns_pairs = set(not_same)
            _n_alias += len(alias)
            _n_rules += len(rules)
            _n_ns += len(not_same)

            def f_t(v, _a=alias, _r=rules, _s=strippable):
                s0 = _canon(norm_text(v))
                s0 = _strip_tokens(s0, _s)
                k0 = value_kind(s0)
                if k0 != "text":
                    s0 = f"{k0}:{_struct_norm(s0)}"
                for r in _r:
                    s0 = _apply_rule_tpl(r.get("rule") or {}, v)
                return _a.get(s0, s0)

            def f_s(v, _a=alias, _r=rules, _s=strippable, _ns=ns_pairs):
                s0 = _canon(norm_text(v))
                s0 = _strip_tokens(s0, _s)
                k0 = value_kind(s0)
                if k0 != "text":
                    s0 = f"{k0}:{_struct_norm(s0)}"
                for r in _r:
                    s0 = _apply_rule_src(r.get("rule") or {}, v)
                s0 = _a.get(s0, s0)
                for a_, b_ in _ns:                # 人工判过"不是一回事"的 → 不许配上
                    if b_ and s0 == b_:
                        return _NOT_SAME_SENTINEL
                return s0

            f_t._tag = f"cT{si}.{tk}:{len(alias)}/{len(rules)}/{len(strippable)}"
            f_s._tag = f"cS{si}.{tk}:{len(alias)}/{len(rules)}/{len(strippable)}"
            fmap_t[tk] = f_t
            fmap_s[tk] = f_s
            if strippable:
                _n_mod += 1
        if _n_alias or _n_rules or _n_ns or _n_mod:
            note = f"{nm}：值等价{_n_alias}条、类推规律{_n_rules}条、判定不同{_n_ns}条"
            if _n_mod:
                note += f"、可剥修饰词列 {_n_mod}（值域碰撞裁决）"
        else:
            note = None
        return (fmap_t, fmap_s, note)

    nf_per_src, nf_notes = [], []
    for si, s in enumerate(sources):
        got = _src_norm(si)
        nf_per_src.append((got[0], got[1]))
        if got[2]:
            nf_notes.append(got[2])

    # ---- 区分位"兄弟值"检测（每张源表的钥匙列各算一次）：
    # 模糊差异全落在数字/序数/方位等区分位、且值域里确有兄弟值 → 禁止靠"像"猜行 ----
    sib_per_src = []
    for si, s in enumerate(sources):
        sib_map = {}
        fs_map = nf_per_src[si][1]
        for tk, scol, _cs in key_pairs[si]:
            if scol not in s["df"].columns:
                continue
            f_k = fs_map.get(tk) if isinstance(fs_map, dict) else fs_map
            try:
                vals = [(f_k or norm_text)(v) for v in s["df"][scol].tolist() if _has_value(v)]
            except Exception:
                vals = [norm_text(v) for v in s["df"][scol].tolist() if _has_value(v)]
            vals = sorted({v for v in vals
                           if v and v != _NOT_SAME_SENTINEL
                           and not v.startswith(("date:", "num:", "code:"))})
            if 1 < len(vals) <= 3000:
                try:
                    sib_map[scol] = _disc_series(vals)
                except Exception:
                    pass
        sib_per_src.append(sib_map)

    # ---- 口径本②：候选列"85% 闸门"（首选列空缺时，才允许用别的候选列补）----
    # 判据：两候选列在都能取到值的行上，一致率 ≥85%（重叠 ≥10 行才判）；否则判"两套口径"，不许互补
    #（同名 100 的候选列豁免闸门——互为备份；互补格会上色标"互补"）
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
            for alt in cands[1:]:
                if alt["source"] == pref["source"] and alt["col"] == pref["col"]:
                    continue
                if alt["how"] == "同名" and alt["score"] >= 100.0 - 1e-9:
                    gates[(tcol, alt["source"])] = True    # 同名100 互为备份 → 豁免 85% 闸门（互补格上色标"互补"）
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
                                           norm_fn_src=nf_per_src[cd["source"]][1],
                                           sib=sib_per_src[cd["source"]])
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
                        round_fills.append((i, tcol, 100.0, 1, 0, "经验库", False))
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
                                      norm_fn=nf_per_src[si][0], norm_fn_src=nf_per_src[si][1],
                                      sib=sib_per_src[si])
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
                            tie_info = (tie_n, k_used, cand["how"], uniq[:3], s["name"], ccol)
                        continue
                    v = s["df"][ccol].iloc[j]
                    if not _has_value(v):      # 这行该列为空 → 回退下一列
                        continue
                    if _code_guard_block(tcol, cand["how"], s["df"][ccol]):
                        continue               # 编号类列不吃"不像编号"的列（如 甲方编号/项目编码）
                    if tie_n and not blank_on_tie:
                        sc = min(sc, 39.0)     # 旧行为：并列且取第 1 条 → 降档
                    best = (v, sc, k_used, tie_n, cand["how"], pairs, s["name"], _ci > 0)
                    if _ci > 0:
                        n_complement += 1      # 记：这格是靠次选候选列补的
                    break
                if best:
                    v, sc, k_used, tie_n, how, pairs, s_name, is_comp = best
                    used_tks = [tk for tk, _c, _s in pairs][:k_used]
                    # 折减按**跳数**：只用原值钥匙 → 不折减；用了补出来的值 → 每跳 ×0.9
                    g = max((gen.get((i, tk), 0) for tk in used_tks), default=0)
                    decayed = sc * (0.9 ** g)
                    if is_comp:
                        decayed = min(decayed, 90.0)   # 互补格封顶高置信档（浅黄）+ 标"互补"，供抽查
                    result.at[i, tcol] = _norm_value(v)
                    tie_cells.pop((i, tcol), None)     # 之前判过"多候选"的格，这轮补上了 → 撤掉标记
                    filled[(i, tcol)] = (decayed, 0, k_used, tie_n)   # 轮次稍后回填
                    hit_trust[(i, tcol)] = (sc, used_tks, is_comp)   # 链式印证：匹配分 + 用了哪几把钥匙
                    gen[(i, tcol)] = g + 1
                    n_fill[_band(decayed)] += 1
                    round_fills.append((i, tcol, decayed, k_used, tie_n, s_name, is_comp))
                    progressed = True
                elif tie_info is not None:
                    # 有几条候选但取值不同 → 宁可留空，交人工选（清单里写「多候选(已留空)」）
                    tie_cells[(i, tcol)] = tie_info

        if progressed:
            eff_rounds += 1
            for i, tcol, decayed, k_used, tie_n, src_tag, is_comp in round_fills:
                _sc0, _r0, _k0, _t0 = filled[(i, tcol)]
                filled[(i, tcol)] = (decayed, eff_rounds, k_used, tie_n)
                tag = f"{_band(decayed)}:{decayed:.0f}@{eff_rounds}"
                if k_used > 1:
                    tag += f"·{k_used}钥匙"
                if tie_n:
                    tag += f"·歧义{tie_n}行"
                _g = gen.get((i, tcol), 0)
                if _g > 1:
                    tag += f"·{_g - 1}跳"          # 级联跳数：值离原始数据隔了几手
                if src_tag:
                    tag += f"·{src_tag}"
                if is_comp:
                    tag += "·互补"                 # 靠同名列互补补上（首选列该行没值）→ 抽查
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
    for (i, tcol), (tn, k_used, how, cand_vals, sname, scol2) in tie_cells.items():
        cand_vals = list(cand_vals) + [""] * (3 - len(cand_vals))
        choices.append({"行号": i + 2, "列名": tcol, "类型": "多候选",
                        "钥匙值": _row_key_text(template_df, i, key_pairs),
                        "候选1": cand_vals[0], "候选2": cand_vals[1], "候选3": cand_vals[2],
                        "备注": f"{sname} →【{scol2}】同键 {tn} 行取值不同"})
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

    # ---- 需要人工确认的"列口径 + 值域"（一次确认 → 列配对生效 / 整列类推）----
    try:
        _cq = build_col_questions(template_df, sources, key_pairs, conventions, topn=6)
    except Exception:
        _cq = []
    try:
        _vq = build_value_questions(template_df, sources, key_pairs, conventions, topn=10)
    except Exception:
        _vq = []
    value_questions = (_cq[:4] + _vq)[:10]      # 列口径最多 4 条，给值域题留位置

    # ---- 多源交叉核对（**独立复核**：两张源表都能供同一列时，比对两源给的值）----
    # 两源都有值且不同 = 矛盾。**按列自校准**：某列矛盾率 >50% → 判为"同名不同口径"，
    # 只给一行提示、不逐格列（否则 100+ 行会把人工清单淹掉）；≤50% 才逐格列。
    # **含模糊/级联命中**：不再要求两源都精确命中——命中方式不同也能互相印证/揭发。
    cross = {"可比格": 0, "矛盾": [], "口径不同": []}
    confirmed = set()          # 双源印证：两源独立命中且一致 → 该格升为完全匹配
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
                    m = _match_source(s, pairs, final_avail[i], key_min, ccol, idx_cache,
                                      blank_on_tie=True,
                                      norm_fn=nf_per_src[si][0], norm_fn_src=nf_per_src[si][1],
                                      sib=sib_per_src[si])
                    if not m or m[0] is None:
                        continue          # 模糊/级联命中也参与印证（不再要求精确命中）
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
                    elif (i, tcol) in filled and _band(filled[(i, tcol)][0]) != "ok":
                        confirmed.add((i, tcol))   # 两源独立命中且一致 → 该格已被验证
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

    # ---- 双源印证升级：两张源表**独立**命中同一格且值一致 → 已被验证，按完全匹配计 ----
    # 这是"高置信 → 100%"的自动通道（另一条是人工确认写口径本）。
    n_confirm = 0
    for (i, tcol) in sorted(confirmed):
        old = filled.get((i, tcol))
        if not old:
            continue
        b0 = _band(old[0])
        if b0 == "ok":
            continue
        n_fill[b0] = max(0, n_fill[b0] - 1)
        n_fill["ok"] += 1
        filled[(i, tcol)] = (100.0, old[1], old[2], old[3])
        conf.at[i, tcol] = f"ok:100@{old[1]}·双源印证"
        n_confirm += 1

    # ---- 链式印证（A 案）：把"已验证"沿接力链传下去 ----
    # 条件（全部满足才继承）：该格是**精确命中**（匹配分=100，折减只来自跳数）
    # + 所用每一把钥匙格都可信（模板原有值 / 已填且为 ok 档：双源印证·精确·经验库·链式）
    # + 不在两源矛盾清单里。模糊命中（匹配分<100）= 可能配错行 → **永不继承**，必须人看。
    n_chain = 0
    _bad_cells = {(int(b.get("行号", 0)) - 2, b.get("列名")) for b in cross["矛盾"]}
    _changed = True
    while _changed:
        _changed = False
        for (i, tcol) in list(filled.keys()):
            cur = filled[(i, tcol)]
            if _band(cur[0]) == "ok":
                continue
            if (i, tcol) not in hit_trust:
                continue
            sc_m, used_tks, is_comp = hit_trust[(i, tcol)]
            if is_comp:
                continue                      # 互补格保持黄色（口径隔了一道，人看；双源印证除外）
            if sc_m < 100.0 - 1e-9:
                continue                      # 模糊命中 → 永不继承
            if (i, tcol) in _bad_cells:
                continue                      # 两源矛盾过 → 不继承
            _trusted = True
            for tk in used_tks:
                if tk not in template_df.columns:
                    _trusted = False
                    break
                if _has_value(template_df.at[i, tk]):
                    continue                  # 模板原有值 → 可信
                _kcell = filled.get((i, tk))
                if _kcell is None or _band(_kcell[0]) != "ok":
                    _trusted = False          # 钥匙格不可信 → 不继承
                    break
            if not _trusted:
                continue
            b0 = _band(cur[0])
            n_fill[b0] = max(0, n_fill[b0] - 1)
            n_fill["ok"] += 1
            filled[(i, tcol)] = (100.0, cur[1], cur[2], cur[3])
            conf.at[i, tcol] = f"ok:100@{cur[1]}·链式印证"
            n_chain += 1
            _changed = True

    # ---- 需人工确认清单：只收「必看」（多候选留空 / 未补上）----
    review = []
    for (i, tcol), (tn, k_used, _how, _cv, _sn, _scol) in tie_cells.items():
        review.append({"类型": f"多候选(已留空,{tn}行取值不同)", "行号": i + 2, "列名": tcol,
                       "钥匙值": _row_key_text(template_df, i, key_pairs), "主结果": "",
                       "源表": _sn, "源列": _scol})
    for i in template_df.index:
        for tcol in target_cols:
            if (i, tcol) in tie_cells:
                continue
            if not _has_value(result.at[i, tcol]) and not _has_value(template_df.at[i, tcol]):
                review.append({"类型": "未补上", "行号": i + 2, "列名": tcol,
                               "钥匙值": _row_key_text(template_df, i, key_pairs), "主结果": "",
                               "源表": "", "源列": ""})
    _rev_cols = ["类型", "行号", "列名", "钥匙值", "主结果", "源表", "源列"]
    if review:
        review.sort(key=lambda r: (0 if str(r.get("类型", "")).startswith("多候选") else 1,
                                   r.get("行号", 0), str(r.get("列名", ""))))
        review_df = pd.DataFrame(review, columns=_rev_cols)
    else:
        review_df = pd.DataFrame(columns=_rev_cols)

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
             "双源印证格数": n_confirm,
             "链式印证格数": n_chain,
            "非100%格数": n_fill["high"] + n_fill["mid"] + n_fill["low"],
            "值等价学习(条)": n_equiv, "互补格数": n_complement,
            "拒绝互补列": list(gate_blocked), "口径说明": list(equiv_notes),
             "两源可核对格": n_cross_cmp, "两源矛盾格": n_cross_bad,
             "两源一致率": round(cross_rate * 100, 1),
             "两源口径不同列": list(cross.get("口径不同") or []),
             "需人工确认行数": len(review_df),
             "实际钥匙列": key_used_desc, "钥匙说明": plan["notes"],
             "未补全行数": int((conf[target_cols] == "miss").any(axis=1).sum()) if target_cols else 0}
    return {"result": result, "confidence": conf, "stats": stats,
            "supply": supply, "legend": legend_lines(stats), "key_pairs": key_pairs,
            "review": review_df, "choices": choices_df,
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
            # 引擎空间里的"已等价"集合：canon + 修饰词剥离（碰撞裁决）后相等的不再出题
            _st = _strippable_tokens(
                {_canon(x) for x in map(norm_text, tvals)},
                {_canon(x) for x in map(norm_text, svals)})
            skeys = {_strip_tokens(_canon(x), _st) for x in map(norm_text, svals)}
            cands = []
            for v, c in cnt.items():
                vkey = _strip_tokens(_canon(v), _st)
                if vkey in skeys or v in done:
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
                ok, cov, why, rule2 = verify_rule(rule, tvals, svals)
                if ok:
                    rule_q = {"类型": "整列", "列名": tk, "源表": nm, "源列": scol,
                              "示例模板值": v, "示例源表值": sv, "规律": rule2 or rule,
                              "覆盖率": round(cov * 100, 1), "相似度": round(sc, 1),
                              "影响格数": c, "同类数": len(cands),
                              "模板前5": [x for x, _n in cnt.most_common(5)],
                              "源前5": sorted(su)[:5]}
                    break
            if rule_q:
                qs.append(rule_q)
                continue
            # ② 没有整列规律 → **同类逐条列出来**（每条单独问，你一条条点）
            for sc, v, sv, c in cands:
                if sc < lo:
                    break
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
            if ans.get("type") == "列口径":
                _t3, _m3 = apply_col_answer(template_df, sources, tcol, sname,
                                            ans.get("src_col"), ans.get("ans"), conventions)
                if _t3:
                    applied.append((_t3, _m3))
                continue
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
            ok, cov, why, rule2 = verify_rule(rule, tvals, svals)
            if ok:
                conventions.record_value_rule([tcol, sname], rule2 or rule,
                                              evidence=f"整列类推（{why}）")
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
            ok, cov, why, r2b = verify_rule(r2, tvals, svals)
            if ok:
                conventions.record_value_rule([tcol, sname], r2b or r2,
                                              evidence=f"整列类推（{why}）")
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


def all_supply_options(tpl_col, sources, col_threshold=COL_DEFAULT_THRESHOLD, mapping=None,
                       conventions=None):
    """「列供给」下拉用：**列出所有源表的所有列**（不按阈值筛掉），附列名分数提示。

    手动映射优先排第一；其余按（列名分 → 有值数）排序。**0% 也列**，由人来判断。
    返回 [{source, col, score, how, filled}]。
    """
    mapping = mapping or {}
    out = []
    for si, s, col, score, how, nonblank in _scan_source_cols(tpl_col, sources, col_threshold):
        manual = bool(mapping.get(tpl_col)) and tuple(mapping[tpl_col]) == (si, col)
        auto = (conventions.col_pair(tpl_col, s["name"]) if conventions is not None else None)
        if auto and auto.get("col") == col:
            how = "已确认"
        out.append({"source": si, "col": col,
                    "score": 101.0 if manual else score,
                    "how": "手动" if manual else how,
                    "filled": nonblank})
    out.sort(key=lambda c: (-c["score"], -c["filled"]))
    return out


def build_col_questions(template_df, sources, key_pairs, conventions=None, topn=6):
    """「列口径」题：**钥匙列**在源表里没配上（含表头被判冲突），但**值域高度像** → 出题问一次。

    你点"是"→ 记进口径本当列配对（以后它就能当钥匙）；点"不是"→ 记住、不再问。
    """
    from .conventions import domain_overlap
    qs = []
    for si, s in enumerate(sources):
        paired_tks = {tk for tk, _c, _sc in key_pairs[si]}
        for tk in [c for c in template_df.columns]:
            if tk in paired_tks:
                continue
            if tk not in template_df.columns:
                continue
            tvals = [str(v) for v in template_df[tk].tolist() if _has_value(v)]
            if not tvals:
                continue
            for col in s["df"].columns:
                svals = [str(v) for v in s["df"][col].tolist() if _has_value(v)]
                if not svals:
                    continue
                ok, cov = domain_overlap(tvals, svals)
                if not ok:
                    continue
                sc, _how = col_match(tk, col, COL_DEFAULT_THRESHOLD)
                if sc >= COL_DEFAULT_THRESHOLD:
                    continue                       # 表头本来就配得上 → 不用问
                ex_a = next((v for v in tvals), "")
                ex_b = ""
                for v in svals:
                    if _key_sim(ex_a, v) >= 60 or _canon(v) in _canon(ex_a) or _canon(ex_a) in _canon(v):
                        ex_b = v
                        break
                qs.append({"类型": "列口径", "列名": tk, "源表": s["name"], "源列": col,
                           "覆盖率": round(cov * 100, 1), "相似度": round(cov * 100, 1),
                           "影响格数": len({str(v) for v in tvals}),
                           "同类数": len([v for v in svals]),
                           "示例模板值": ex_a, "示例源表值": ex_b or (svals[0] if svals else ""),
                           "模板前5": sorted({str(v) for v in tvals})[:5],
                           "源前5": sorted({str(v) for v in svals})[:5]})
                break
    qs.sort(key=lambda q: -q["覆盖率"])
    return qs[:topn]


def apply_col_answer(template_df, sources, tcol, sname, scol, ans, conventions):
    """「列口径」题的落地：确认 → 记录列配对（以后可当钥匙）；不是 → 记住不再问。"""
    if ans == "same":
        conventions.record_col_pair(tcol, sname, scol, src="confirmed",
                                    evidence="值域高度重合，人工确认")
        return ("列配对", f"「{tcol}」← {sname}→【{scol}】已确认，以后它可作为钥匙使用")
    if ans == "not":
        try:
            conventions.record_not_same([tcol, sname], tcol, scol)
        except Exception:
            pass
        return ("判不同", f"「{tcol}」← {sname}→【{scol}】已记为两回事，不再问")
    return (None, None)


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
    """「列供给」下拉标签：源表名〔工作表〕→【源列】(匹配方式,分数)。

    注意：源表名**必须**按 cand["source"] 去查（候选字典里没有 name 字段）。
    """
    si = cand.get("source", 0)
    src = sources[si] if isinstance(si, int) and 0 <= si < len(sources) else {}
    sname = src.get("name", "?")
    sheet = src.get("sheet")
    head = f"{sname}〔{sheet}〕" if sheet else f"{sname}"
    return f"{head} →【{cand.get('col', '')}】({cand.get('how', '')},{cand.get('score', 0):.0f})"


def _scan_source_cols(tpl_col, sources, col_threshold):
    """公共底座：枚举每张源表的**每一列** → [(源序, 源dict, 列名, 列名分, 匹配方式, 非空数)]。

    不过滤、不排序；discover_supply（阈值+守卫过滤）与 all_supply_options（全列列出）共用。
    """
    out = []
    for si, s in enumerate(sources):
        df = s["df"]
        for col in df.columns:
            score, how = col_match(tpl_col, col, col_threshold)
            out.append((si, s, col, score, how, int(df[col].notna().sum())))
    return out


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
        for si, s, col, score, how, nonblank in _scan_source_cols(tcol, sources, col_threshold):
            if col == s.get("key_col"):
                continue
            if tcol in mapping and mapping[tcol] and (si, col) == tuple(mapping[tcol]):
                continue
            if score < col_threshold or how == "冲突":
                continue
            if _name_looks_date(tcol) and not _looks_date_col(s["df"][col])[0]:
                continue                      # 类型守卫：日期类目标列不吃状态/文本列
            cands.append({"source": si, "col": col, "score": score, "how": how,
                          "filled": nonblank})
        # 匹配度高 → 有值多 → 源表顺序
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
        "浅色格的说明：鼠标悬停可看该格的置信、轮次/级联跳数与来源（Excel 批注）",
    ]
    extra = []
    if stats.get("间接补全格数"):
        extra.append(f"含 {stats['间接补全格数']} 格为多轮间接补全（置信已按跳数折减）")
    if stats.get("双源印证格数"):
        extra.append(f"双源印证 {stats['双源印证格数']} 格：两张源表**独立**命中且一致 → 已按完全匹配(100%)计")
    if stats.get("链式印证格数"):
        extra.append(f"链式印证 {stats['链式印证格数']} 格：精确命中且所用钥匙格全部可信（沿接力链继承印证）"
                     "→ 已按完全匹配(100%)计")
    if stats.get("互补格数"):
        extra.append(f"互补 {stats['互补格数']} 格：首选列该行没值 → 用**同名的第 2 候选列**补"
                     "（黄色·互补，供抽查；不同名的互补仍受 85% 闸门约束）")
    if stats.get("歧义格数"):
        extra.append(f"有 {stats['歧义格数']} 格命中多行且取值不同 → **已留空**（见「需人工确认」清单，选一条填）")
    if stats.get("延后源表数"):
        extra.append(f"有 {stats['延后源表数']} 张源表第 1 轮延后（只有 1 把钥匙，等后续轮次凑第二把）")
        extra.append(f"两源交叉核对：{stats['两源可核对格']} 格有两张源表都能供（含模糊/级联命中），"
                     f"其中矛盾 {stats.get('两源矛盾格', 0)} 格（一致率 {stats.get('两源一致率', 100)}%）"
                     + ("，见「两源矛盾」Sheet / 页面抽查区" if stats.get("两源矛盾格") else ""))
    for _line in (stats.get("两源口径不同列") or []):
        extra.append("两源" + _line)
    if stats.get("需人工确认行数"):
        extra.append(f"需人工确认：{stats['需人工确认行数']} 行（多候选留空 / 未补上，见第二个 Sheet）")
    return lines + extra


def _conf_comment(tag):
    """把置信标签翻译成人话（Excel 批注用，验收时鼠标悬停即见）。

    例："high:90@2·2钥匙·1跳·金蝶对账" → "高置信 90%｜第2轮补全｜2把钥匙｜级联1跳｜来源：金蝶对账"
    """
    t = str(tag)
    if not t or t == "miss":
        return "未匹配（留空）：源表里没有可靠的对应行"
    try:
        band, rest = t.split(":", 1)
        score, rest2 = rest.split("@", 1)
        parts = rest2.split("·")
    except Exception:
        return t
    nm = {"ok": "完全匹配", "high": "高置信", "mid": "中置信", "low": "低置信"}.get(band, band)
    bits = []
    if parts[0] not in ("", "0"):
        bits.append(f"第{parts[0]}轮补全")
    for p in parts[1:]:
        if p == "双源印证":
            bits.append("双源印证（两张源表独立命中且一致）")
        elif p == "链式印证":
            bits.append("链式印证（精确命中且所用钥匙格全部可信）")
        elif p == "互补":
            bits.append("互补格（首选列该行没值，用同名的第 2 候选列补）")
        elif p == "经验库":
            bits.append("你之前人工确认过")
        elif p.endswith("钥匙"):
            bits.append(f"{p[:-2]}把钥匙")
        elif p.startswith("歧义"):
            bits.append(f"源表有{p[2:]}行同样像")
        elif p.endswith("跳"):
            bits.append(f"级联{p}")
        else:
            bits.append(f"来源：{p}")
    return f"{nm} {score}%" + ("：" + "；".join(bits) if bits else "")


def export_filled(result_df, conf_df, out_path, stats=None, extra_notes=None, review_df=None,
                  cross_df=None):
    """写出带颜色的整合表 + 下方备注块（表头/列宽/冻结/筛选/数字格式由 theme 统一处理）。

    非 100% 的格会附 **Excel 批注**（悬停即见置信/轮次/来源），验收不用翻清单。
    """
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.styles import Font, PatternFill

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
            tag = str(conf_df.iloc[r - 2, c - 1]) if (r - 2) < len(conf_df) else ""
            band = tag.split(":")[0]
            spec = COLORS.get(band)
            if not spec or not spec["fill"]:
                if "双源印证" in tag or "链式印证" in tag:
                    ws.cell(row=r, column=c).comment = Comment(_conf_comment(tag), "AI采购助理")
                continue
            cell = ws.cell(row=r, column=c)
            cell.fill = PatternFill("solid", fgColor="FF" + spec["fill"].lstrip("#"))
            if spec["font"]:
                cell.font = Font(color="FF" + spec["font"].lstrip("#"))
            if band in ("high", "mid", "low"):
                cell.comment = Comment(_conf_comment(tag), "AI采购助理")

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
    except Exception:
        pass
    wb.save(out_path)
    return out_path
