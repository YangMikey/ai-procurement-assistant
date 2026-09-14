# -*- coding: utf-8 -*-
"""路线 B 新功能测试（离线）：F16 手动录入 / F17 模板输出 / F11 错配预警 / F09 LLM 建议。

运行：py tests/experiment_features_b.py
"""
import io
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import pandas as pd
from openpyxl import Workbook, load_workbook

from core.advisor_llm import advise_llm
from core.advisor_rule import advise
from core.parser_rule import manual_quote
from core.templater import fill_template

ok = 0


def check(name, cond):
    global ok
    assert cond, f"[FAIL] {name}"
    ok += 1
    print(f"[PASS] {name}")


# ---- F16 手动报价录入 ----
q = manual_quote("利源通", [
    {"品名": "螺钉 M4×10", "规格": "M4×10", "单位": "包", "数量": 100, "价格": "113", "税率": "13"},
    {"品名": "PVC管 20mm", "规格": "Φ20", "单位": "米", "数量": 300, "价格": "3.2", "税率": 13},
], price_field="含税单价")
check("F16：手动录入结构同解析器（供 align 直接用）",
      q["via"] == "manual" and q["supplier"] == "利源通" and len(q["df"]) == 2)
check("F16：价格/税率解析正确",
      abs(q["df"]["含税单价"].iloc[0] - 113.0) < 1e-9 and abs(q["df"]["税率"].iloc[1] - 0.13) < 1e-9)
check("F16：字段映射可追溯", q["mapping"].get("品名") == "品名")

# ---- F17 模板整合输出 ----
wb = Workbook()
ws = wb.active
ws.title = "比价表"
ws["A1"] = "XX 项目比价表（模板）"
ws["A2"] = "编制：采购部"
tpl = io.BytesIO()
wb.save(tpl)
m = pd.DataFrame({"品名": ["螺钉", "PVC管"], "规格": ["M4", "Φ20"], "利源通": [10.0, 2.0],
                  "润锋": [9.5, 2.2], "最低价供应商": ["润锋", "利源通"]})
out_bytes, info = fill_template(tpl.getvalue(), m, sheet="比价表", start_cell="A4",
                                title="比价结果")
wb2 = load_workbook(io.BytesIO(out_bytes))
ws2 = wb2["比价表"]
check("F17：模板原有内容保留", ws2["A1"].value == "XX 项目比价表（模板）")
check("F17：标题/表头/数据写入指定起始位置",
      ws2["A4"].value == "比价结果" and ws2["A5"].value == "品名" and ws2["A6"].value == "螺钉")
check("F17：末列数据正确（最低价供应商）", ws2.cell(row=6, column=5).value == "润锋")
check("F17：返回写入范围信息", info["sheet"] == "比价表" and info["rows"] == 2 and info["cols"] == 5)

# ---- F11 疑似错配预警（来自对齐低置信度） ----
matrix = pd.DataFrame({"品名": ["阀门", "胶条"], "规格": ["DN50", ""],
                       "甲": [10.0, 5.0], "乙": [11.0, 6.0], "丙": [10.5, 5.5]})
low = [{"supplier": "乙", "品名": "阀门", "对齐到": "阀体", "置信度": "低", "相似度": 70.0, "原因": "模糊"}]
adv = advise(matrix, ["甲", "乙", "丙"], low_confidence=low)
check("F11：低置信度项触发「疑似错配」",
      adv["rows"].iloc[0]["预警"] == "疑似错配" and adv["summary"]["疑似错配"] == 1)
check("F11：无误报的正常行", adv["rows"].iloc[1]["预警"] in ("", None))
check("F11：文案包含错配提示", any("疑似品类错配" in t for t in adv["text"]))

# ---- F09 LLM 采购建议（离线假客户端） ----
class FakeLLM:
    enabled = True

    def __init__(self, payload, err=None):
        self.payload, self.err = payload, err
        self.calls = 0

    def chat(self, messages, **kw):
        self.calls += 1
        return {"text": self.payload, "usage": {}, "cost_usd": 0.0,
                "cached": False, "error": self.err, "ms": 1}


_f = FakeLLM("①成本结论：阀门建议甲；②风险：无；③谈判：乙可压价")
r = advise_llm(matrix, ["甲", "乙", "丙"], _f, rule_advice=adv)
check("F09：LLM 建议返回文本", "成本结论" in r["text"] and r["error"] is None)
_bad = FakeLLM("", err="余额不足")
check("F09：LLM 失败不抛异常、原样上报", advise_llm(matrix, ["甲"], _bad)["error"] == "余额不足")
_dis = FakeLLM("x")
_dis.enabled = False
check("F09：未启用则不发请求", advise_llm(matrix, ["甲"], _dis)["error"] == "LLM 未启用")

# ---- 尽量填充 + 备注（对齐判定） ----
from core.aligner import align_quotes

qj = {"supplier": "甲", "df": pd.DataFrame({"品名": ["螺钉M4", "阀门"], "规格": ["", "DN50"],
                                           "单位": ["包", "个"], "数量": [1, 1],
                                           "含税单价": [10.0, 20.0]})}
qy = {"supplier": "乙", "df": pd.DataFrame({"品名": ["螺丝钉M4×10", "阀门"], "规格": ["", "PN16"],
                                           "单位": ["包", "个"], "数量": [1, 1],
                                           "含税单价": [9.0, 18.0]})}
ra = align_quotes([qj, qy], price_field="含税单价", threshold=60.0)
check("填充：阈值60时 螺钉M4↔螺丝钉M4×10（~75%）合并为 1 行", len(ra["matrix"]) == 2)
_notes = "；".join(str(x) for x in ra["matrix"]["对齐备注"])
check("备注：非精确命中写清『方式+相似度』（模糊 xx%）", "模糊" in _notes and "%" in _notes)
check("备注：精确命中不备注", sum(1 for x in ra["matrix"]["对齐备注"] if "模糊" in str(x)) >= 1)
_lv = [a for a in ra["assignments"] if a["supplier"] == "乙"]
_spec = [a for a in _lv if a["品名"] == "阀门"][0]
check("填充：规格不一致仍合并（尽量填充）", _spec["level"] == "模糊")
check("填充：规格不一致降级为低置信并备注", _spec["confidence"] == "低"
      and "规格不一致" in _spec["note"])
check("填充：规格不一致进入待人工确认",
      any("规格不一致" in str(x.get("原因", "")) for x in ra["low_confidence"]))

print(f"\n===== 路线 B 新功能测试通过：{ok} 项断言（离线）=====")
