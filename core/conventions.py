# -*- coding: utf-8 -*-
"""口径本（conventions）：把"判断"沉淀成可复用的规则，下次直接命中。

三类内容（都不含业务数据本身，只存"规则/等价/结论"）：
- value_equiv：值等价（如 1事业部 = 一、广州科汇 = 科汇），带证据与来源
- col_verdicts：列级结论（同名列对之间的一致率与判定：same / different）
- decisions：人工裁决过的一句话结论（"起始日期以合约规划为准"）

学习原则（**全部可自证，不是拍脑袋**）：
1. 归一化规则只有"通用形态"：去首尾修饰词（事业部/部/第/-）+ 中文数字↔阿拉伯
2. 必须**双向覆盖**（两边的值都能对上）且**保持区分度**（不同值不会被并成一个）
3. 只用规则能解释的部分；解释不了就不学（留空 + 问人）
"""
import json
import os
import re

from .aligner import norm_text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(ROOT, "data", "conventions.json")

# 常见"修饰词"（可从值首尾去掉再比核心）；只影响比较，不改变原值
AFFIX_TOKENS = ["事业部", "分公司", "子公司", "公司", "部门", "本部", "总部", "第", "号", "期"]
# 中文数字
_CN = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
       "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100}


def _cn_number(s):
    """把纯中文数字串转成阿拉伯数字串："一"→1、"十"→10、"十二"→12、"二十三"→23。"""
    if s in _CN_UNIT:
        return str(_CN_UNIT[s])
    if "十" in s:
        parts = s.split("十")
        tens = _CN.get(parts[0], 1) if parts[0] else 1
        ones = _CN.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
        return str(tens * 10 + ones)
    if all(ch in _CN for ch in s):
        return "".join(str(_CN[ch]) for ch in s)
    return s


def canon(value, affix_tokens=AFFIX_TOKENS, numeral=True):
    """把值压成"可比核心"：去首尾修饰词 + 中文数字转阿拉伯。

    例：`1事业部`→`1`、`一`→`1`、`-一`→`1`、`第一事业部`→`1`、`广州科汇`→`广州科汇`
    """
    s = norm_text(value)
    s = s.strip("-—－_·.、,，()（）[]【】")
    if affix_tokens:
        changed = True
        while changed and s:
            changed = False
            for t in affix_tokens:
                nt = norm_text(t)
                if nt and s.endswith(nt) and len(s) > len(nt):
                    s = s[: -len(nt)].strip("-—－_·.、,，()（）[]【】")
                    changed = True
                if nt and s.startswith(nt) and len(s) > len(nt):
                    s = s[len(nt):].strip("-—－_·.、,，()（）[]【】")
                    changed = True
        s = s.strip("-—－_·.、,，()（）[]【】")
    if numeral:
        s = re.sub(r"[零一二两三四五六七八九十百]+", lambda m: _cn_number(m.group(0)), s)
    return s


def _uniq(vals, limit=2000):
    out, seen = [], set()
    for v in vals:
        if v is None:
            continue
        s = str(v).strip()
        if not s or s.lower() in ("nan", "none", "nat"):
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= limit:
            break
    return out


def learn_value_equiv(a_vals, b_vals, min_uniq=2, max_uniq=200):
    """从两列取值学"值等价"。

    返回 dict：
      ok       是否学成（可自证）
      mapping  {a值: b值}
      reason  失败原因
    **要求**：两边唯一值都能被"核心"一一对应（覆盖 100%），且核心到值保持单射
    （不同 a 不会映到同一个核心、不同 b 也不会）。
    """
    ua, ub = _uniq(a_vals, max_uniq), _uniq(b_vals, max_uniq)
    if len(ua) < min_uniq or len(ub) < min_uniq:
        return {"ok": False, "reason": "唯一值太少，不做等价学习"}
    if len(ua) > max_uniq or len(ub) > max_uniq:
        return {"ok": False, "reason": "唯一值太多（更像自由文本），不学"}
    ca = {v: canon(v) for v in ua}
    cb = {v: canon(v) for v in ub}
    # 保持区分度：不同原值的核心必须不同（否则"一/二"会被并成一个）
    if len(set(ca.values())) != len(ca) or len(set(cb.values())) != len(cb):
        return {"ok": False, "reason": "核心不能保持区分度（会把不同值并成一个）"}
    core_b = {}
    for v, c in cb.items():
        core_b.setdefault(c, []).append(v)
    if set(ca.values()) != set(core_b.keys()):
        miss = set(ca.values()) - set(core_b.keys())
        return {"ok": False,
                "reason": f"两边核心对不上（差 {len(miss)} 个，例：{sorted(miss)[:3]}）"}
    mapping = {}
    for v, c in ca.items():
        if len(core_b[c]) != 1:
            return {"ok": False, "reason": "核心对应不唯一"}
        mapping[v] = core_b[c][0]
    return {"ok": True, "mapping": mapping, "reason": f"核心一一对应（{len(mapping)} 条）"}


# ---------- 规律推断与类推（让"确认一条 = 解决一类"） ----------

def _lcp(a, b):
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return a[:n]


def _lcs(a, b):
    n = 0
    while n < min(len(a), len(b)) and a[-1 - n] == b[-1 - n]:
        n += 1
    return a[len(a) - n:] if n else ""


def infer_rule(a, b):
    """从"你确认是同一个"的一对值里，提取**可类推的规律**。

    - numeral：公共前后缀去掉后，剩下的是数字（1 ↔ 一、2 ↔ 二…）→ 整列可类推
    - drop_head / drop_tail：源值 = 模板值去掉头/尾 n 个字符（如 广州科汇 → 科汇）
    返回 dict 或 None（推不出规律 → 只能当"特例"记一条）
    """
    a2, b2 = norm_text(a), norm_text(b)
    if not a2 or not b2 or a2 == b2:
        return None
    pre, suf = _lcp(a2, b2), _lcs(a2, b2)
    ra = a2[len(pre): len(a2) - len(suf)] if len(a2) > len(pre) + len(suf) else ""
    rb = b2[len(pre): len(b2) - len(suf)] if len(b2) > len(pre) + len(suf) else ""
    if ra and rb and _cn_number(ra) == _cn_number(rb):
        return {"kind": "numeral", "prefix": pre, "suffix": suf}
    if a2.endswith(b2) and len(a2) > len(b2):
        return {"kind": "drop_head", "n": len(a2) - len(b2)}
    if a2.startswith(b2) and len(a2) > len(b2):
        return {"kind": "drop_tail", "n": len(a2) - len(b2)}
    return None


def apply_rule_tpl(rule, v):
    """模板侧 → 可比核心。"""
    s = norm_text(v)
    k = (rule or {}).get("kind")
    if k == "numeral":
        pre, suf = rule.get("prefix") or "", rule.get("suffix") or ""
        if pre and s.startswith(pre):
            s = s[len(pre):]
        if suf and s.endswith(suf):
            s = s[: len(s) - len(suf)]
        return _cn_number(s)
    if k == "drop_head":
        return s[rule.get("n", 0):]
    if k == "drop_tail":
        n = rule.get("n", 0)
        return s[: len(s) - n] if len(s) > n else s
    return s


def apply_rule_src(rule, v):
    """源表侧 → 可比核心（numeral 规律与模板侧同样处理；drop_* 规律源侧保持原样）。"""
    s = norm_text(v)
    k = (rule or {}).get("kind")
    if k == "numeral":
        pre, suf = rule.get("prefix") or "", rule.get("suffix") or ""
        if pre and s.startswith(pre):
            s = s[len(pre):]
        if suf and s.endswith(suf):
            s = s[: len(s) - len(suf)]
        return _cn_number(s)
    return s


def verify_rule(rule, a_vals, b_vals, cover=0.9):
    """规律能否解释**整列**：双向覆盖 ≥cover 且保持区分度（不同值不塌缩）。

    返回 (ok, 覆盖率, 说明)。**这是"类推"能不能成立的唯一依据**。
    """
    A, B = _uniq(a_vals), _uniq(b_vals)
    if not A or not B:
        return False, 0.0, "空列"
    ta = {v: apply_rule_tpl(rule, v) for v in A}
    tb = {v: apply_rule_src(rule, v) for v in B}
    if len(set(ta.values())) != len(ta) or len(set(tb.values())) != len(tb):
        return False, 0.0, "会把不同值并成一个（禁止）"
    sa, sb = set(ta.values()), set(tb.values())
    ca, cb = len(sa & sb) / len(sa), len(sa & sb) / len(sb)
    ok = ca >= cover and cb >= cover
    return ok, min(ca, cb), ("通过" if ok else f"覆盖率不足（{ca:.0%} / {cb:.0%}）")


def agreement(vals_a, vals_b, mapping=None):
    """逐格一致率：返回 (一致格数, 可比格数, 一致率)。

    比之前先过 canon；若给了 mapping（学到/人工确认的值等价），先按它换算。
    """
    same = tot = 0
    for x, y in zip(vals_a, vals_b):
        sx = "" if x is None else str(x).strip()
        sy = "" if y is None else str(y).strip()
        if not sx or not sy or sx.lower() in ("nan", "none") or sy.lower() in ("nan", "none"):
            continue
        tot += 1
        vx = mapping.get(sx, sx) if mapping else sx
        if canon(vx) == canon(sy):
            same += 1
    return same, tot, (same / tot if tot else 0.0)


class ConventionStore:
    """口径本：列级结论 + 值等价 + 人工裁决（JSON，随时可看/可改/可删）。"""

    def __init__(self, path=None):
        self.path = path or DEFAULT_PATH
        self.data = {"version": 1, "col_verdicts": {}, "value_equiv": [], "decisions": []}
        self._load()

    def _load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    self.data.update(d)
        except Exception:
            pass

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ---------- 列级 ----------
    def col_verdict(self, tpl_col, src_name_a, src_name_b):
        key = f"{tpl_col}|{src_name_a}|{src_name_b}"
        return (self.data.get("col_verdicts") or {}).get(key)

    def record_col_verdict(self, tpl_col, src_name_a, src_name_b, agree, overlap,
                           verdict=None, src="auto"):
        if verdict is None:
            verdict = "same" if agree >= 0.85 else "different"
        key = f"{tpl_col}|{src_name_a}|{src_name_b}"
        self.data.setdefault("col_verdicts", {})[key] = {
            "tpl": tpl_col, "a": src_name_a, "b": src_name_b,
            "agree": round(float(agree), 3), "overlap": int(overlap),
            "verdict": verdict, "src": src}
        self._save()
        return self.data["col_verdicts"][key]

    # ---------- 值级 ----------
    def value_rules(self, *scope):
        """取某作用域下的"规律"列表（用于整列类推）。"""
        tag = "|".join(str(x) for x in scope if x)
        return [it for it in (self.data.get("value_rules") or []) if it.get("scope") == tag]

    def record_value_rule(self, scope, rule, src="auto", evidence=""):
        tag = "|".join(str(x) for x in (scope if isinstance(scope, (list, tuple)) else [scope]) if x)
        items = self.data.setdefault("value_rules", [])
        for it in items:
            if it.get("scope") == tag:
                it.update({"rule": rule, "src": src, "evidence": evidence})
                self._save()
                return it
        it = {"scope": tag, "rule": rule, "src": src, "evidence": evidence}
        items.append(it)
        self._save()
        return it

    def is_not_same(self, scope, a, b):
        """这对值被人工判过"不是一回事"（下次不再问、也不许模糊配上）。"""
        tag = "|".join(str(x) for x in (scope if isinstance(scope, (list, tuple)) else [scope]) if x)
        key = f"{tag}\x1f{norm_text(a)}\x1f{norm_text(b)}"
        return key in (self.data.get("not_same") or {})

    def record_not_same(self, scope, a, b, src="manual"):
        tag = "|".join(str(x) for x in (scope if isinstance(scope, (list, tuple)) else [scope]) if x)
        key = f"{tag}\x1f{norm_text(a)}\x1f{norm_text(b)}"
        self.data.setdefault("not_same", {})[key] = {"scope": tag, "a": str(a), "b": str(b), "src": src}
        self._save()
        return key

    def value_mapping(self, *scope):
        """取某作用域下的值等价映射 {a值: b值}（多条合并，后写覆盖）。"""
        tag = "|".join(str(x) for x in scope if x)
        out = {}
        for it in (self.data.get("value_equiv") or []):
            if it.get("scope") == tag:
                out.update(it.get("mapping") or {})
        return out

    def record_value_equiv(self, scope, mapping, src="auto", evidence=""):
        tag = "|".join(str(x) for x in (scope if isinstance(scope, (list, tuple)) else [scope]) if x)
        items = self.data.setdefault("value_equiv", [])
        for it in items:
            if it.get("scope") == tag:
                it["mapping"] = {**(it.get("mapping") or {}), **mapping}
                it["src"] = src
                it["evidence"] = evidence
                self._save()
                return it
        it = {"scope": tag, "mapping": dict(mapping), "src": src, "evidence": evidence}
        items.append(it)
        self._save()
        return it

    # ---------- 人工裁决 ----------
    def record_decision(self, question, answer, scope=""):
        self.data.setdefault("decisions", []).append(
            {"q": question, "a": answer, "scope": scope, "src": "manual"})
        self._save()

    def stats(self):
        return {"列级结论": len(self.data.get("col_verdicts") or {}),
                "值等价组": len(self.data.get("value_equiv") or []),
                "类推规律": len(self.data.get("value_rules") or []),
                "判定不同": len(self.data.get("not_same") or {}),
                "人工裁决": len(self.data.get("decisions") or [])}

    def list_items(self):
        return (self.data.get("value_equiv") or []) + (self.data.get("decisions") or [])

    def clear(self):
        self.data = {"version": 1, "col_verdicts": {}, "value_equiv": [], "decisions": []}
        self._save()
