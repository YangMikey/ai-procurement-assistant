# -*- coding: utf-8 -*-
"""真实文件实验：汇总.xlsx · 2-工具清单（临时副本上跑全流程，不碰原文件）。

验证链路：两行表头拆分 → 列名税率识别（13%/1% 混合）→ 换算 → 合计/公式行跳过 → ai副本备份 → 按位置写回 → 幂等重跑。
运行：py tests/experiment_gongju.py
"""
import os
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from core.converter import convert_tax, parse_rate_from_name, to_number
from core.excel_io import (backup_sheet_numbered, read_table,
                           sheet_formula_rows, split_two_row_header,
                           writeback_convert)

REAL = os.path.join(_ROOT, "data", "ground_truth", "汇总.xlsx")
# 用「ai副本2-工具清单」（首次写回前的原始快照）作为实验对象：
# 真 Sheet 已被用户验收写回过（多了不含税列/合并受损），快照恒为原始 13 列布局。
SHEET = "ai副本2-工具清单"
HDR = 2
TMP = os.path.join(os.environ["TEMP"], "汇总_实验副本.xlsx")
shutil.copyfile(REAL, TMP)

ok = 0


def check(name, cond):
    global ok
    assert cond, f"[FAIL] {name}"
    ok += 1
    print(f"[PASS] {name}")


# ---- 1. 两行表头拆分 ----
with open(TMP, "rb") as f:
    file_bytes = f.read()
names, is_two = split_two_row_header(file_bytes, TMP, SHEET, HDR)
check("拆分：检测为两行式", is_two is True)
check("拆分：同国贸易", "同国贸易 税率13%" in names)
check("拆分：建菲伽", "建菲伽 税率13%" in names)
check("拆分：利源通两列去重（整块合并回填后仍去重）",
      "利源通 税率（1%)" in names and "利源通 税率（1%)(1)" in names)
check("拆分：润锋", "润锋 （13%)" in names)
check("拆分：玉兰邨", "玉兰邨 （1%)" in names)
check("拆分：左侧列保持原名（序号/数量/最小值）",
      names[0] == "序号" and names[5] == "数量" and "最小值" in names)
check("拆分：无 Unnamed 残留", not any(n.startswith("Unnamed") for n in names))

# ---- 2. 读表 + 列名税率识别（13%/1% 混合） ----
df = read_table(TMP, SHEET, header_row=HDR)
df = df.rename(columns={df.columns[j]: names[j] for j in range(min(len(names), len(df.columns)))})
check("识别：同国贸易 13%", parse_rate_from_name("同国贸易 税率13%") == 0.13)
check("识别：利源通 1%（无%号写法）", parse_rate_from_name("利源通 税率（1%)") == 0.01)
check("识别：玉兰邨 1%", parse_rate_from_name("玉兰邨 （1%)") == 0.01)

# ---- 3. 换算（选 3 列不同税率 + 合计/公式行跳过） ----
price_cols = ["同国贸易 税率13%", "建菲伽 税率13%", "利源通 税率（1%)"]
rates = [parse_rate_from_name(p) for p in price_cols]
check("识别：rates=[0.13, 0.13, 0.01]", rates == [0.13, 0.13, 0.01])

f_rows = sheet_formula_rows(file_bytes, TMP, SHEET, cols={7, 8, 9})
check("检测：G/H/I 列公式行(121,123)被识别，数据行(3)不误判",
      121 in f_rows and 123 in f_rows and 3 not in f_rows)

skip_mask = [("合计" in " ".join(str(v) for v in r.values))
             for _, r in df.iterrows()]
for i in range(len(skip_mask)):
    if (HDR + 1 + i) in f_rows:
        skip_mask[i] = True
i_sum = next(i for i, v in enumerate(df["品名"].tolist()) if "合计" in str(v))
check("跳过：合计行被标记", skip_mask[i_sum] is True)
check("跳过：f_rows 公式行均进入 skip_mask（不依赖公式缓存值）",
      all(skip_mask[p - HDR - 1] for p in f_rows if p - HDR - 1 < len(skip_mask)))
check("跳过：数据首行未被误跳", skip_mask[0] is False)

out, added = convert_tax(df, price_cols, tax_rate=0.13, direction="to_ex",
                         rates=rates, skip_mask=skip_mask)
check("换算：新增 3 列", len(added) == 3)

# 数值核对：Row3(数据首行) 同国贸易 274.62/1.13
v = to_number(out[added[0]].iloc[0])
check("换算：274.62/1.13=243.0265", v is not None and abs(v - 243.0265) < 0.001)
# 利源通 1%：185.393258426966/1.01
v = to_number(out[added[2]].iloc[0])
check("换算：185.3933/1.01=183.5577", v is not None and abs(v - 183.5577) < 0.001)
# 合计行不换算
check("跳过：合计行输出空", out[added[0]].iloc[i_sum] == "")

# ---- 4. ai副本 + 写回（按位置） ----
bk = backup_sheet_numbered(TMP, SHEET)
check("备份：ai副本前缀 + 不重名", bk and bk.startswith("ai副本") and bk != SHEET)

items = [(df.columns.get_loc(pc) + 1, added[j], out[added[j]].tolist())
         for j, pc in enumerate(price_cols)]
written = writeback_convert(TMP, SHEET, HDR, items)
check("写回：返回 3 个写入列", len(written) == 3)

from openpyxl import load_workbook
wb = load_workbook(TMP)
ws = wb[SHEET]
# 新列位置：G(7)→8, H(8)→10, I(9)→12（从右往左插入）
check("写回：G 新列表头在上一行（竖向合并复刻）",
      "同国贸易 税率13%(不含税)" in str(ws.cell(row=1, column=8).value))
check("写回：G 新列首行数值 243.0265",
      abs(float(ws.cell(row=3, column=8).value) - 243.0265) < 0.001)
check("写回：I 新列（1%税率）183.5577",
      abs(float(ws.cell(row=3, column=12).value) - 183.5577) < 0.001)
check("写回：合计行 SUM 公式原样保留", str(ws.cell(row=121, column=7).value).startswith("=SUM"))
check("写回：不含税合计公式行未被破坏", str(ws.cell(row=123, column=7).value).startswith("="))
check("写回：公式行的新列单元格保持为空（未覆写）", ws.cell(row=123, column=8).value is None)
check("写回：竖向合并已复刻（新列 Row1:Row2）",
      any(r.min_col == 8 and r.max_col == 8 and r.min_row == 1 and r.max_row == 2
          for r in ws.merged_cells.ranges))
max_col_1 = ws.max_column
wb.close()

# ---- 5. 幂等重跑（重跑=覆写纠错，不重复插列） ----
written2 = writeback_convert(TMP, SHEET, HDR, items)
wb = load_workbook(TMP)
ws = wb[SHEET]
check("幂等：重跑后列数不变", ws.max_column == max_col_1)
check("幂等：返回标注(覆写)", all("(覆写)" in w for w in written2))
wb.close()
os.remove(TMP)

print(f"\n===== 真实文件实验（2-工具清单·临时副本）全部通过：{ok} 项断言 =====")
print("原文件未做任何改动；你可在网页里对 2-工具清单 实际操作验收。")
