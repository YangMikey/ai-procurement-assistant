# -*- coding: utf-8 -*-
"""跨供应商比价对齐：把多份解析后的报价 → 一张比价矩阵（行=品类，列=供应商价格）。

设计（PRD F03 / 评估纪律）：
- 组合键：默认「品名 + 规格」（页面可自选）；规范化后再比（全/半角、空格、×/x/*、括号、大小写）
- 同品异名：① 规范化精确 → ② 模糊(rapidfuzz 多信号，规格作一致性下限) → ③ **经验库兜底** → ④ 新品类
  （经验库为兜底：规则都没结果时才用；单位/数量不一致 → 置信度降级）
- 结果「对齐备注」标注模糊/经验库来源（供人工复核）
- 置信度分级：高 / 中 / 低；低置信度与模糊命中的分配回传界面做人工确认（回写经验库）
- 口径：按 price_field 取价（默认"不含税单价"）；只有另一口径时用 税率 现算（缺税率用全局兜底）
- 最低价：复用 highlighter.find_min
"""
import re

import pandas as pd

from .converter import to_number
from .highlighter import find_min
from .registry import skill

SEP = "\x1f"
_DEFAULT_KEY_FIELDS = ("品名", "规格")


def norm_text(s):
    """规范化：全角→半角、去空白、统一乘号/括号、小写。"""
    t = str(s or "").strip()
    t = t.replace("（", "(").replace("）", ")").replace("，", ",")
    t = t.replace("×", "*").replace("＊", "*").replace("✕", "*").replace("x", "*").replace("X", "*")
    t = t.replace("Φ", "φ").replace("ø", "φ")
    t = t.replace("毫米", "mm").replace("米", "m")
    t = re.sub(r"\s+", "", t)
    return t.lower()


def _item_key(row, key_fields):
    return SEP.join(norm_text(row.get(f, "")) for f in key_fields)


def _sim(a, b):
    """单信号相似度（仅供外部/测试用）。"""
    return _name_sim(a, b)[0]


def _bag_sim(a, b):
    """字符袋相似度：对乱序（如「触摸开关86型」vs「86型触摸开关」）敏感度低。"""
    from collections import Counter
    ca, cb = Counter(a), Counter(b)
    inter = sum((ca & cb).values())
    total = len(a) + len(b)
    return (2.0 * inter / total * 100.0) if total else 0.0


def _name_sim(a, b):
    """多信号名称相似度：取 常规/部分/字符袋 的最大值；返回 (score, scorer)。

    中文物料名常见「乱序」「子串」「含型号混排」，单一 ratio 会漏配，
    故综合三种信号；其中仅靠字符袋命中（顺序不同）的降级为待确认。
    """
    try:
        from rapidfuzz import fuzz
        r = float(fuzz.ratio(a, b))
        p = float(fuzz.partial_ratio(a, b))
    except ImportError:
        from difflib import SequenceMatcher
        r = p = SequenceMatcher(None, a, b).ratio() * 100.0
    bag = _bag_sim(a, b)
    best = max(r, p, bag)
    scorer = "ratio" if best == r else ("partial" if best == p else "bag")
    return best, scorer


def price_series(df, price_field, tax_rate=0.13):
    """取指定口径价格；缺该口径时用 税率 从另一口径现算。返回 (list, derived_bool)。"""
    if price_field in df.columns:
        return [to_number(v) for v in df[price_field].tolist()], False
    other = "不含税单价" if price_field == "含税单价" else "含税单价"
    if other not in df.columns:
        return [None] * len(df), False
    rates = df["税率"].tolist() if "税率" in df.columns else [None] * len(df)
    out = []
    for i, v in enumerate(df[other].tolist()):
        r = rates[i] if i < len(rates) and rates[i] is not None else tax_rate
        n = to_number(v)
        if n is None:
            out.append(None)
        elif price_field == "不含税单价":
            out.append(round(n / (1 + r), 6))
        else:
            out.append(round(n * (1 + r), 6))
    return out, True


def _conf_level(level, score, scorer, row, canon):
    if level in ("经验库", "精确"):
        base = "高"
    elif level == "模糊":
        if scorer == "bag":                 # 仅乱序字符袋命中 → 保守，交人工确认
            base = "中"
        else:
            base = "高" if score >= 92 else ("中" if score >= 85 else "低")
    else:
        base = "高"
    notes = []
    for f in ("单位", "数量"):
        a, b = str(row.get(f, "") or "").strip(), str(canon.get(f, "") or "").strip()
        if a and b and norm_text(a) != norm_text(b):
            notes.append(f"{f}不一致({a} vs {b})")
    if notes and base == "高":
        base = "中"
    elif notes and base == "中":
        base = "低"
    return base, "；".join(notes)


def record_alias(experience, item_row, canonical_key, key_fields=_DEFAULT_KEY_FIELDS, src="manual"):
    """把「供商品名组合键 → 标准品组合键」写入经验库（raw，不做二次清洗）。"""
    key = SEP.join(norm_text(item_row.get(f, "")) for f in key_fields)
    return experience.record(key, canonical_key, src=src, raw=True)


@skill(
    name="跨供应商对齐",
    desc="多份报价 → 比价矩阵；组合键(品名+规格)+规范化+经验库置顶+模糊+单位/数量校验，输出置信度分级",
    inputs={"quotes": "解析后的报价列表", "price_field": "比价口径（默认不含税单价）",
            "key_fields": "对齐键字段（默认 品名+规格）", "threshold": "模糊阈值",
            "experience": "经验库实例（可选）"},
    outputs={"matrix": "比价矩阵", "confidence": "置信度矩阵", "assignments": "每条匹配明细",
             "low_confidence": "需人工确认项"},
    task_modes=["完整比价"],
)
def align_quotes(quotes, price_field="不含税单价", key_fields=_DEFAULT_KEY_FIELDS,
                 threshold=60.0, experience=None, tax_rate=0.13):
    """把多份 ParsedQuote 对齐成比价矩阵。

    threshold 默认 60（**尽量填充**）：非"规范化后完全相同"的命中都会写进「对齐备注」
    （含方式与相似度百分比），供人工复查；精确命中的不备注。
    """
    key_fields = tuple(key_fields)
    suppliers = [q["supplier"] for q in quotes]
    lut = {}
    if experience is not None:
        try:
            lut = experience.build_lookup()
        except Exception:
            lut = {}

    canon = []            # [{品名, 规格, 单位, 数量, key}]
    canon_by_key = {}     # norm key -> canonical index
    key_to_canon = {}     # 组合键（规范串）-> canonical index（经验库用）
    # 内部：supplier_idx -> {canon_idx: (price, conf, level, score, note)}
    cell = {}
    assignments = []
    low = []
    row_notes = {}          # canonical idx -> [来源说明]（只记 模糊/经验库）

    def _way(level, score, scorer):
        if level == "精确":
            return "精确"                      # 去格式后完全相同 → 无需备注
        if level == "模糊":
            return f"模糊 {score:.0f}%"         # 备注写清"怎么填的 + 相似度"
        if level == "经验库":
            return "经验库兜底"
        return "新品类"

    for qi, q in enumerate(quotes):
        df = q["df"]
        if df is None or df.empty:
            continue
        prices, _ = price_series(df, price_field, tax_rate)
        for i in range(len(df)):
            row = df.iloc[i]
            name = str(row.get("品名", "") or "").strip()
            if not name:
                continue
            key = _item_key(row, key_fields)
            if not key.strip(SEP):
                continue
            cidx, level, score, note = None, "新增", 100.0, ""
            scorer = "ratio"
            # ① 规范化精确（最优先）
            if key in canon_by_key:
                cidx, level = canon_by_key[key], "精确"
            # ② 模糊（尽量填充：规格不一致不直接否掉，但降级并备注）
            if cidx is None and canon:
                best, bs, bscorer, bspec = None, 0.0, "ratio", None
                _sp = str(row.get("规格", "") or "").strip()
                for ci, c in enumerate(canon):
                    s, sc = _name_sim(norm_text(name), norm_text(c["品名"]))
                    if _sp and c["规格"].strip():
                        ss, _ = _name_sim(norm_text(_sp), norm_text(c["规格"]))
                        bspec_tmp = ss
                        if ss >= threshold:
                            if ss < s:
                                s, sc = ss, "spec"
                        else:
                            sc = "spec-low"        # 规格对不上：仍需品名达标
                    else:
                        bspec_tmp = None
                    if s > bs:
                        bs, best, bscorer, bspec = s, ci, sc, bspec_tmp
                if best is not None and bs >= threshold:
                    cidx, level, score, scorer = best, "模糊", bs, bscorer
            # ③ 经验库兜底（规则都没结果时才用）
            if cidx is None:
                tgt = lut.get(key)
                if tgt is not None:
                    c2 = canon_by_key.get(tgt)
                    if c2 is not None:
                        cidx, level = c2, "经验库"
            # ④ 新品类
            if cidx is None:
                canon.append({"品名": name, "规格": str(row.get("规格", "") or "").strip(),
                              "单位": str(row.get("单位", "") or "").strip(),
                              "数量": row.get("数量", ""), "key": key})
                cidx = len(canon) - 1
                canon_by_key[key] = cidx
            else:
                c = canon[cidx]
                if not c["单位"]:
                    c["单位"] = str(row.get("单位", "") or "").strip()
                if c["数量"] in ("", None):
                    c["数量"] = row.get("数量", "")
            conf, cnote = _conf_level(level, score, scorer, row, canon[cidx])
            if scorer == "spec-low":
                conf = "低"
                cnote = (cnote + "；" if cnote else "") + f"规格不一致({(bspec or 0):.0f}%)"
            key_to_canon[key] = canon[cidx]["key"]
            way = _way(level, score, scorer)
            if level in ("模糊", "经验库"):
                row_notes.setdefault(cidx, []).append(f"{q['supplier']}:{way}")
            pv = prices[i] if i < len(prices) else None
            prev = cell.setdefault(qi, {}).get(cidx)
            if prev is None or (pv is not None and (prev[0] is None or pv < prev[0])):
                cell[qi][cidx] = (pv, conf, level, score, cnote)
            assignments.append({"supplier": q["supplier"], "品名": name,
                                "规格": str(row.get("规格", "") or ""),
                                "canonical": canon[cidx]["品名"], "level": level,
                                "way": way, "score": round(score, 1),
                                "confidence": conf, "note": cnote})
            if conf != "高":
                low.append({"supplier": q["supplier"], "品名": name, "规格": str(row.get("规格", "") or ""),
                            "对齐到": canon[cidx]["品名"], "相似度": round(score, 1),
                            "置信度": conf, "原因": cnote or level,
                            "alias_key": key, "canon_key": canon[cidx]["key"]})

    # ---- 组装矩阵 ----
    rows = []
    for ci, c in enumerate(canon):
        r = {"品名": c["品名"], "规格": c["规格"], "单位": c["单位"], "数量": c["数量"]}
        for qi, sup in enumerate(suppliers):
            v = cell.get(qi, {}).get(ci)
            r[sup] = v[0] if v else None
        r["对齐备注"] = "；".join(row_notes.get(ci, []))
        rows.append(r)
    mdf = pd.DataFrame(rows)
    if mdf.empty:
        mdf = pd.DataFrame(columns=["品名", "规格", "单位", "数量"] + suppliers + ["对齐备注"])
    min_vals, min_sups = find_min(mdf, suppliers) if suppliers else ([], [])
    mdf["最低价"] = min_vals
    mdf["最低价供应商"] = min_sups

    # 置信度矩阵
    crow = []
    for ci, c in enumerate(canon):
        r = {"品名": c["品名"], "规格": c["规格"]}
        for qi, sup in enumerate(suppliers):
            v = cell.get(qi, {}).get(ci)
            r[sup] = v[1] if v else ""
        crow.append(r)
    cdf = pd.DataFrame(crow)

    return {"matrix": mdf, "confidence": cdf, "assignments": assignments,
            "low_confidence": low, "suppliers": suppliers,
            "key_to_canon": key_to_canon}
