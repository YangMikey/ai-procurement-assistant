# -*- coding: utf-8 -*-
"""合成报价单生成器：4 家供应商 × 4 种格式变体 + 同品异名/脏数据 + ground truth。

防自嗨原则（PRD §12）：
- 故意掺脏：¥/千分位文本价、同品异名、缺行、多行、表头不在第一行、税率两种写法
- ground truth 随文件一起生成，tests 可自动核对解析/换算/比价结果
- 运行：py tests/synth_quotes.py → data/samples/ 下生成 4 个 xlsx + ground_truth.json
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openpyxl import Workbook

SAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "samples")

# 品类标准名, 规格, 单位, 数量, 基准含税单价
ITEMS = [
    ("螺钉 M4×10", "M4×10", "包", 100, 12.50),
    ("电线电缆 3×2.5mm²", "BV-3×2.5", "卷", 20, 386.00),
    ("86型触摸开关", "86型", "个", 50, 22.80),
    ("PVC线管 20mm", "Φ20", "米", 300, 3.20),
    ("LED灯管 T8 1.2m", "T8/1.2m", "支", 80, 9.90),
    ("水泥钉 2寸", "2寸", "盒", 40, 15.60),
]
TAX = 0.13
# 各供应商价格浮动（刻意让不同供应商在不同品类上最低，比价才有内容）
DELTA = {"A": 0.00, "B": -0.05, "C": 0.03, "D": -0.02}
# 供应商 D 的同品异名写法
D_ALIAS = {
    "螺钉 M4×10": "螺钉M4*10",
    "电线电缆 3×2.5mm²": "电线电缆BV3x2.5",
    "86型触摸开关": "触摸开关86型",
}
D_DROP = {"LED灯管 T8 1.2m"}          # D 缺这个品
D_EXTRA = ("劳保手套", "均码", "双", 200, 2.50)  # D 多这个品


def _price(base, delta):
    return round(base * (1 + delta), 2)


def gen_A(path):
    """标准格式：表头第 1 行，含税单价 + 税率'13%'。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "报价"
    ws.append(["品类", "规格型号", "单位", "数量", "含税单价", "税率"])
    for name, spec, unit, qty, base in ITEMS:
        ws.append([name, spec, unit, qty, _price(base, DELTA["A"]), "13%"])
    wb.save(path)


def gen_B(path):
    """变体1：第 1 行是标题、表头在第 2 行；无含税价只有不含税价；税率为数字 13。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "供应商B"
    ws.append(["供应商B-报价单（2026.09）"])
    ws.append(["品名及规格", "单位", "数量", "不含税单价", "税率(%)"])
    for name, spec, unit, qty, base in ITEMS:
        ws.append([f"{name}({spec})", unit, qty,
                   round(_price(base, DELTA["B"]) / (1 + TAX), 2), 13])
    wb.save(path)


def gen_C(path):
    """变体2：含税单价为文本（¥ + 千分位）；列名是'含税单价(元)'与'型号'。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "C供应商报价"
    ws.append(["品类", "型号", "单位", "含税单价(元)"])
    for name, spec, unit, qty, base in ITEMS:
        p = _price(base, DELTA["C"])
        ws.append([name, spec, unit, f"¥{p:,.2f}"])
    wb.save(path)


def gen_D(path):
    """变体3：同品异名 + 缺 1 个品 + 多 1 个品（脏数据）。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "报价"
    ws.append(["品类", "规格", "单位", "含税单价"])
    for name, spec, unit, qty, base in ITEMS:
        if name in D_DROP:
            continue
        ws.append([D_ALIAS.get(name, name), spec, unit, _price(base, DELTA["D"])])
    ws.append(list(D_EXTRA))
    wb.save(path)


def main():
    os.makedirs(SAMPLES, exist_ok=True)
    truth = {"tax": TAX, "items": []}
    for name, spec, unit, qty, base in ITEMS:
        truth["items"].append({
            "canonical": name,
            "spec": spec,
            "qty": qty,
            "prices_tax_in": {s: _price(base, d) for s, d in DELTA.items()},
            "alias_D": D_ALIAS.get(name),
            "missing_in": ["D"] if name in D_DROP else [],
        })
    truth["extra_only_in_D"] = D_EXTRA[0]

    paths = {
        "A": os.path.join(SAMPLES, "报价单A_标准格式.xlsx"),
        "B": os.path.join(SAMPLES, "报价单B_表头第2行_仅不含税.xlsx"),
        "C": os.path.join(SAMPLES, "报价单C_文本价格.xlsx"),
        "D": os.path.join(SAMPLES, "报价单D_同品异名_缺行多行.xlsx"),
    }
    gen_A(paths["A"])
    gen_B(paths["B"])
    gen_C(paths["C"])
    gen_D(paths["D"])
    with open(os.path.join(SAMPLES, "ground_truth.json"), "w", encoding="utf-8") as f:
        json.dump(truth, f, ensure_ascii=False, indent=2)
    for s, p in paths.items():
        print(f"[OK] {s} -> {p}")
    print("[OK] ground_truth.json")


if __name__ == "__main__":
    main()
