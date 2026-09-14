# -*- coding: utf-8 -*-
"""M2a 报价单解析测试：合成 4 家逐项断言 + 真实件结构探针（如实标注样本量）。

- 合成件：data/samples/报价单A~D（标准/表头第2行/文本价/同品异名+缺行多行）+ ground_truth.json
- 真实件：data/raw_quotes/物资采购清单_利源通.xls（**仅 1 份，只做结构探针，不宣称准确率**）
- 报告：data/outputs/解析报告.txt
运行：py tests/experiment_parser.py
"""
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from core.parser_rule import parse_quote_file, supplier_from_filename

SAMPLES = os.path.join(_ROOT, "data", "samples")
RAW = os.path.join(_ROOT, "data", "raw_quotes")
OUT = os.path.join(_ROOT, "data", "outputs")

ok = 0
lines = []


def check(name, cond):
    global ok
    assert cond, f"[FAIL] {name}"
    ok += 1
    print(f"[PASS] {name}")
    lines.append(f"[PASS] {name}")


with open(os.path.join(SAMPLES, "ground_truth.json"), encoding="utf-8") as f:
    GT = json.load(f)
first = GT["items"][0]          # 螺钉 M4×10

# ---- A 标准格式 ----
qA = parse_quote_file(path=os.path.join(SAMPLES, "报价单A_标准格式.xlsx"))[0]
check("A：表头行=1", qA["header_row"] == 1)
check("A：字段齐全（品名/规格/单位/数量/含税单价/税率）",
      all(c in qA["mapping"] for c in ("品名", "规格", "单位", "数量", "含税单价", "税率")))
check("A：行数=6", len(qA["df"]) == 6)
check("A：首行含税单价=GT 值",
      abs(qA["df"]["含税单价"].iloc[0] - first["prices_tax_in"]["A"]) < 0.001)
check("A：首行税率解析=0.13", abs(qA["df"]["税率"].iloc[0] - 0.13) < 0.001)

# ---- B 表头第2行 + 仅不含税 + 品名及规格合并列 ----
qB = parse_quote_file(path=os.path.join(SAMPLES, "报价单B_表头第2行_仅不含税.xlsx"))[0]
check("B：表头行=2", qB["header_row"] == 2)
check("B：识别不含税单价（且无含税单价列）",
      "不含税单价" in qB["mapping"] and "含税单价" not in qB["mapping"])
check("B：首行不含税=GT含税/1.13", abs(qB["df"]["不含税单价"].iloc[0] - round(first["prices_tax_in"]["B"] / (1 + GT["tax"]), 2)) < 0.01)
check("B：合并列拆出规格", "规格" in qB["df"].columns and qB["df"]["规格"].iloc[0] != "")
check("B：品名未被合并串污染", str(qB["df"]["品名"].iloc[0]).startswith("螺钉"))

# ---- C 文本价格 ¥ + 千分位 ----
qC = parse_quote_file(path=os.path.join(SAMPLES, "报价单C_文本价格.xlsx"))[0]
check("C：表头行=1", qC["header_row"] == 1)
check("C：文本价(¥+千分位) 解析=GT 值", abs(qC["df"]["含税单价"].iloc[0] - first["prices_tax_in"]["C"]) < 0.001)

# ---- D 同品异名 + 缺1行 + 多1行 ----
qD = parse_quote_file(path=os.path.join(SAMPLES, "报价单D_同品异名_缺行多行.xlsx"))[0]
names_D = list(qD["df"]["品名"])
check("D：行数=6（6-1缺+1多）", len(qD["df"]) == 6)
check("D：同品异名写法保留（螺钉M4*10）", any("螺钉M4*10" in str(n) for n in names_D))
check("D：缺行（LED灯管不存在）", not any("LED灯管" in str(n) for n in names_D))
check("D：多行（劳保手套存在）", any("劳保手套" in str(n) for n in names_D))

# ---- 真实件探针（唯一 1 份，仅结构断言） ----
real_lines = []
raw_xls = [f for f in os.listdir(RAW) if f.lower().endswith((".xls", ".xlsx"))]
if raw_xls:
    p = os.path.join(RAW, raw_xls[0])
    qR = parse_quote_file(path=p, mode="file")[0]
    check("真实件：表头行=2", qR["header_row"] == 2)
    check("真实件：供应商名取自文件名=利源通", qR["supplier"] == "利源通")
    check("真实件：识别到关键字段（品名/单位/数量/价格/税率）",
          all(any(c in k for k in qR["mapping"]) for c in ("品名", "单位", "数量", "单价", "税率")))
    check("真实件：有效行数 > 60", len(qR["df"]) > 60)
    real_lines = [f"  真实件 {raw_xls[0]}：表头行={qR['header_row']} 供应商={qR['supplier']} "
                  f"有效行={len(qR['df'])} 字段={qR['mapping']}",
                  f"  警告：{'；'.join(qR['warnings']) if qR['warnings'] else '无'}",
                  "  ⚠️ 真实原始样本仅 1 份，仅作结构探针，不作准确率宣称"]
else:
    check("真实件：data/raw_quotes 有待测原始件", False)

check("工具：文件名提取供应商（物资采购清单（利源通）(1).xls）",
      supplier_from_filename("物资采购清单（利源通）(1).xls") == "利源通")

os.makedirs(OUT, exist_ok=True)
report = "\n".join([
    "=" * 72,
    "报价单解析报告（M2a）",
    "-" * 72,
    "【合成件】共 4 家，逐项断言全部通过（表头探测/字段映射/文本价/合并列拆分/异名/缺行多行）",
    "【真实件】以下为结构探针（样本仅 1 份）",
    *real_lines,
    "-" * 72,
    "分层说明：合成件 = 规则设计同源，仅用于回归；真实准确率待更多原始报价单（放 data/raw_quotes/）",
    "=" * 72,
])
with open(os.path.join(OUT, "解析报告.txt"), "w", encoding="utf-8") as f:
    f.write(report + "\n")

print(f"\n===== M2a 报价单解析测试通过：{ok} 项断言；报告见 data\\outputs\\解析报告.txt =====")
