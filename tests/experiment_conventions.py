# -*- coding: utf-8 -*-
"""口径本 / 值等价 / 85% 闸门 测试（离线）。

守住的规律：
- 写法归一：1事业部 / 一 / -一 / 第一事业部 → 同一个核心；一 ≠ 二 不许塌缩
- 值等价学习：模板 1事业部/2事业部 ↔ 源表 一/二 → 学成并让匹配到 100%
- 85% 闸门：两个候选列一致率 <85% → 判"两套口径"，拒绝互相补缺（宁可留空）
- 口径本可选（不传时行为照旧）

运行：py tests/experiment_conventions.py
"""
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import pandas as pd

from core.conventions import (ConventionStore, agreement, canon, infer_rule,
                              learn_value_equiv, verify_rule)
from core.table_filler import (_auto_key_pairs, apply_value_answers,
                               build_value_questions, fill_multi)

ok = 0
TMP = os.path.join(os.environ.get("TEMP", "."), "opencode", "conv_test.json")
os.makedirs(os.path.dirname(TMP), exist_ok=True)


def check(name, cond):
    global ok
    assert cond, f"[FAIL] {name}"
    ok += 1
    print(f"[PASS] {name}")


def store():
    if os.path.exists(TMP):
        os.remove(TMP)
    return ConventionStore(TMP)


# ---- ① 写法归一 ----
check("归一：1事业部 / 一 / -一 / 第一事业部 都是同一个核心",
      len({canon("1事业部"), canon("一"), canon("-一"), canon("第一事业部")}) == 1)
check("归一保持区分度：一 ≠ 二（不许被并成一个）", canon("一") != canon("二"))
check("归一：普通名字不动（广州科汇）", canon("广州科汇") == "广州科汇")

# ---- ② 值等价学习 ----
r = learn_value_equiv(["1事业部", "2事业部"], ["一", "二"])
check("学习：核心一一对应 → 学成", r["ok"] and r["mapping"] == {"1事业部": "一", "2事业部": "二"})
check("学习：源表唯一值太少 → 不学（宁可不学也不乱配）",
      not learn_value_equiv(["1事业部", "2事业部"], ["一", "一"])["ok"])
check("学习：核心对不上 → 不学", not learn_value_equiv(["1事业部", "2事业部"], ["甲", "乙"])["ok"])
check("一致率：换算后一致",
      agreement(["1事业部", "2事业部"], ["一", "二"], {"1事业部": "一", "2事业部": "二"})[2] == 1.0)

# ---- ③ 端到端：开口径本 → 从"留空"变成"100% 精确命中" ----
tpl = pd.DataFrame({"钥匙事业部": ["1事业部", "2事业部"], "值": ["", ""]})
src = {"name": "srcA", "df": pd.DataFrame({"钥匙事业部": ["一", "二"], "值": ["A1", "A2"]})}
r0 = fill_multi(tpl, key_cols=["钥匙事业部"], sources=[src], audit_rounds=0)
check("不开口径本：1事业部 vs 一 没有共同字符 → 宁可留空",
      str(r0["result"]["值"].iloc[0]).strip() == "")
st = store()
r1 = fill_multi(tpl, key_cols=["钥匙事业部"], sources=[src], audit_rounds=0, conventions=st)
check("开口径本：自动学到等价 → 两行都 100% 精确命中",
      list(r1["result"]["值"]) == ["A1", "A2"]
      and all("100" in str(v) for v in r1["confidence"]["值"]))
check("口径本落盘：值等价可复用",
      ConventionStore(TMP).value_mapping("钥匙事业部", "srcA") == {"1事业部": "一", "2事业部": "二"})

# ---- ④ 85% 闸门：两套口径不许互相补缺 ----
# 源 A 与源 B 都能供「值」，但两者一致率只有 50%（<85%）→ 判"两套口径"，拒绝互补
_keys = [f"k{i}" for i in range(1, 13)]
_tG = pd.DataFrame({"钥匙": _keys, "值": [""] * 12})
_sA = {"name": "源A", "df": pd.DataFrame({"钥匙": _keys,
                                          "值": [f"X{i}" for i in range(1, 13)]})}
_sB = {"name": "源B", "df": pd.DataFrame({"钥匙": _keys,
                                          "值": [f"X{i}" if i <= 6 else f"Y{i}"
                                                 for i in range(1, 13)]})}
_rG = fill_multi(_tG, key_cols=["钥匙"], sources=[_sA, _sB], audit_rounds=0, key_min=95,
                 mapping={"值": (0, "值")})       # 手动指定源A为首选（分 101）
check("闸门：一致率 50% <85%（重叠 12 行）→ 判两套口径、拒绝互补",
      _rG["stats"]["互补格数"] == 0 and len(_rG["stats"]["拒绝互补列"]) >= 1)
# 一致率 100% 的两列 → 允许互补（首选列某行为空时，用另一列补上）
_keys2 = [f"m{i}" for i in range(1, 13)]
_tH = pd.DataFrame({"钥匙": _keys2, "值": [""] * 12})
_sC = {"name": "源C", "df": pd.DataFrame({"钥匙": _keys2[:11],
                                          "值": [f"V{i}" for i in range(1, 12)]})}
_sD = {"name": "源D", "df": pd.DataFrame({"钥匙": _keys2,
                                          "值": [f"V{i}" for i in range(1, 13)]})}
_rH = fill_multi(_tH, key_cols=["钥匙"], sources=[_sC, _sD], audit_rounds=0, key_min=95,
                 mapping={"值": (0, "值")})       # 首选源C（它缺 m12）
check("闸门：一致率 100% → 允许互补（m12 由次选列补上）",
      str(_rH["result"]["值"].iloc[11]) == "V12" and _rH["stats"]["互补格数"] >= 1)

# ---- ⑤ 值域问题清单 + 一条回答解决一类（**必须类推**）----
_AA = ["1事业部", "2事业部", "3事业部"]
_BB = ["一事业部", "二事业部", "三事业部"]
check("规律：从 2事业部↔二事业部 推出 numeral 规律", infer_rule("2事业部", "二事业部") ==
      {"kind": "numeral", "prefix": "", "suffix": "事业部"})
check("规律自证：整列通用（覆盖 100%）", verify_rule(infer_rule("2事业部", "二事业部"), _AA, _BB)[0])
check("规律守卫：会把不同值并成一个 → 拒绝",
      not verify_rule({"kind": "drop_head", "n": 3}, ["广州科汇", "深圳科汇"], ["科汇", "科汇"])[0])

tplQ = pd.DataFrame({"钥匙事业部": _AA, "值": ["", "", ""]})
srcQ = {"name": "srcQ", "df": pd.DataFrame({"钥匙事业部": _BB, "值": ["A1", "A2", "A3"]})}
stQ = store()
kpQ = [_auto_key_pairs(["钥匙事业部"], srcQ, 70.0, None, tplQ, True)]
qs = build_value_questions(tplQ, [srcQ], kpQ, stQ)
check("问题清单：只出一题、类型=整列、示例对正确（不是 3↔二 那种乱问）",
      len(qs) == 1 and qs[0]["类型"] == "整列"
      and qs[0]["示例模板值"] == "2事业部" and qs[0]["示例源表值"] == "二事业部"
      and qs[0]["覆盖率"] == 100.0 and qs[0]["同类数"] == 3)
_ap = apply_value_answers(tplQ, [srcQ], {("钥匙事业部", "srcQ"): {"ans": "same", "rule": qs[0]["规律"]}},
                          stQ)
check("应用：整列类推（一条回答解决一类）", _ap and _ap[0][0] == "类推整列")
_rQ = fill_multi(tplQ, key_cols=["钥匙事业部"], sources=[srcQ], audit_rounds=0, conventions=stQ)
check("类推后：三行全部 100% 精确命中（不再有非100%）",
      list(_rQ["result"]["值"]) == ["A1", "A2", "A3"]
      and all("ok:100" in str(x) for x in _rQ["confidence"]["值"]))
check("类推后：同一列不再出问题（同类不再问）",
      build_value_questions(tplQ, [srcQ], kpQ, stQ) == [])
check("口径本统计含 类推规律 1 条", stQ.stats()["类推规律"] == 1)

# ---- ⑥ 判"不是"：记住、不再问、不许模糊配上 ----
tplN = pd.DataFrame({"项目": ["石碑街道综合事务中心"], "值": [""]})
srcN = {"name": "srcN", "df": pd.DataFrame({"项目": ["石碑大院"], "值": ["Z1"]})}
stN = store()
kpN = [_auto_key_pairs(["项目"], srcN, 70.0, None, tplN, True)]
qN = build_value_questions(tplN, [srcN], kpN, stN)
check("问题清单：看着像但不是的（57 分）也给出来让你判",
      len(qN) == 1 and qN[0]["类型"] == "特例")
apply_value_answers(tplN, [srcN], {("项目", "srcN"): "not"}, stN)
check("判不同：已记住", stN.stats()["判定不同"] >= 1)
check("判不同后：该格不再模糊填（留空）",
      str(fill_multi(tplN, key_cols=["项目"], sources=[srcN], audit_rounds=0,
                     conventions=stN)["result"]["值"].iloc[0]).strip() == "")
check("判不同：已记住", stN.stats()["判定不同"] >= 1)
check("判不同后：该格不再模糊填（留空）",
      str(fill_multi(tplN, key_cols=["项目"], sources=[srcN], audit_rounds=0,
                     conventions=stN)["result"]["值"].iloc[0]).strip() == "")

if os.path.exists(TMP):
    os.remove(TMP)
print(f"\n===== 口径本/值等价/85%闸门 通过：{ok} 项断言 =====")
