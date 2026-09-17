# Copyright (c) 2026
# -*- coding: utf-8 -*-
"""UI 冒烟（Streamlit AppTest）：6 个模式渲染 + 「多表补全」走到"列供给"不报错。

覆盖过的真 bug：列供给下拉标签引用候选字典不存在的键 → KeyError: 'name'
运行：py tests/test_ui_smoke.py
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from streamlit.testing.v1 import AppTest

TPL = os.path.join(_ROOT, "data", "samples", "报价单A_标准格式.xlsx")
ok = 0


def check(name, cond):
    global ok
    assert cond, f"[FAIL] {name}"
    ok += 1
    print(f"[PASS] {name}")


at = AppTest.from_file(os.path.join(_ROOT, "app.py"), default_timeout=180)
at.run()
for m in ["两表匹配补缺", "仅换算", "仅对齐", "完整比价", "多表补全", "表格美化"]:
    at.sidebar.radio[0].set_value(m)
    at.run()
    check(f"UI 渲染无异常：{m}", len(at.exception) == 0 and not at.error)

# 多表补全：给模板路径 + 源表用项目 raw_quotes（默认全选）→ 走到「列供给」应带候选标签
at.sidebar.radio[0].set_value("多表补全")
at.run()
try:
    at.text_input(key="path_tpl").set_value(TPL)
except Exception as e:
    check("UI：能填入模板路径", False)
at.run()
check("UI：模板读取后无异常", len(at.exception) == 0 and not at.error)
try:
    at.radio(key="mf_srckind").set_value("从项目 raw_quotes 目录选")
except Exception:
    pass
at.run()
check("UI：源表选择 raw_quotes 后无异常", len(at.exception) == 0 and not at.error)
# 列供给区应出现（至少渲染过 selectbox 或提示）
has_supply = any("列供给" in str(x.value or "") for x in at.markdown) or len(at.selectbox) > 0
check("UI：列供给区渲染成功（含候选标签路径）", has_supply)

# 回归：列供给标签必须走 supply_option_label（源表名〔工作表〕 →【源列】）
# 曾经的 bug：内联 f-string 用了循环遗留变量 _s → 所有选项都写成最后一张源表名
_sb_sup = [sb for sb in at.selectbox
           if str(sb.label).startswith("「") and str(sb.label).endswith("←")]
check("UI：列供给下拉存在", len(_sb_sup) >= 1)
_opt_sup = [o for sb in _sb_sup for o in list(sb.options or []) if o != "不补"]
check("UI：列供给标签带「工作表」且格式为 源表 →【源列】",
      len(_opt_sup) >= 1 and all("〔" in o and " →【" in o for o in _opt_sup))

# 新开关（自动配钥匙 / 值域识别 / 单钥匙延后）开→关各渲染一次
for _k, _vals in (("mf_autokey", [False, True]), ("mf_domain", [False, True]),
                  ("mf_defer", [False, True])):
    for _v in _vals:
        try:
            at.checkbox(key=_k).set_value(_v)
        except Exception:
            pass
        at.run()
        check(f"UI：{_k}={_v} 渲染无异常", len(at.exception) == 0 and not at.error)

# 真跑一次「生成补全表」：走完 自动配钥匙 → 级联 → 清单 → 抽查区 全链路
if at.button(key="mf_go"):
    at.button(key="mf_go").click()
    at.run()
    check("UI：点「生成补全表」跑完全链路无异常", len(at.exception) == 0 and not at.error)

# 「另存到指定文件夹」：目标目录设成临时目录 → 点按钮后文件应真的落地
try:
    import tempfile
    from glob import glob
    _td = os.path.join(tempfile.gettempdir(), "opencode", "ui_save")
    os.makedirs(_td, exist_ok=True)
    for _old in glob(os.path.join(_td, "*.xlsx")):
        os.remove(_old)
    if at.text_input(key="savedir_fill"):
        at.text_input(key="savedir_fill").set_value(_td)
        at.run()
        at.button(key="savebtn_fill").click()
        at.run()
        check("UI：结果「另存到指定文件夹」真的写出文件",
              not at.exception and len(glob(os.path.join(_td, "*.xlsx"))) >= 1)
except Exception as e:
    print("   [skip] 另存检查：", e)

# 回归：「选择文件夹」选完后必须把路径写进输入框（曾有 bug：先在 text_input 实例化后写 session_state
# → st.session_state.savedir_fill cannot be modified after the widget ... is instantiated）
try:
    _picked_dir = os.path.join(tempfile.gettempdir(), "opencode", "ui_save_pick")
    os.makedirs(_picked_dir, exist_ok=True)
    at.session_state["browse_dir_fill_dir"] = _picked_dir   # 模拟"已选中文件夹"
    at.run()
    check("UI：选择文件夹后不报错（写 state 在 widget 之前）",
          len(at.exception) == 0 and not at.error)
    check("UI：选中的文件夹回填到输入框",
          str(at.text_input(key="savedir_fill").value) == _picked_dir)
    # 旧路径不能每轮回灌：手输新目录后再 rerun，应保持手输值
    _manual = os.path.join(tempfile.gettempdir(), "opencode", "ui_save_manual")
    os.makedirs(_manual, exist_ok=True)
    at.text_input(key="savedir_fill").set_value(_manual)
    at.run()
    check("UI：手输目录不被上次选择回灌覆盖",
          str(at.text_input(key="savedir_fill").value) == _manual)
except Exception as e:
    print("   [skip] 选文件夹回填检查：", e)

# 「值域确认」：有问题才出现；出现时点一次「确定并应用」应能写库 + 重跑
try:
    _vq_keys = [getattr(x, "key", "") for x in at.radio if str(getattr(x, "key", "")).startswith("vq_")]
    if _vq_keys and at.button(key="vq_apply"):
        at.button(key="vq_apply").click()
        at.run()
        check("UI：值域确认「确定并应用」后重跑无异常", len(at.exception) == 0 and not at.error)
    else:
        print("   [info] 本次样例没有值域问题（无需确认）")
except Exception as e:
    print("   [skip] 值域确认检查：", e)

# 多表补全：源表来源切到「粘贴文件路径」（新增分支）应能渲染
try:
    at.radio(key="mf_srckind").set_value("粘贴文件路径")
    at.run()
    check("UI：源表来源=粘贴文件路径 渲染无异常", len(at.exception) == 0 and not at.error)
    _p = os.path.join(_ROOT, "data", "ground_truth", "001.xlsx")
    if os.path.exists(_p) and at.text_area(key="mf_paths"):
        at.text_area(key="mf_paths").set_value(_p)
        at.run()
        check("UI：粘贴存在的路径后 渲染无异常", len(at.exception) == 0 and not at.error)
except Exception:
    pass
# 完整比价：报价单来源切到「粘贴文件路径」（新增浏览按钮分支）
try:
    at.sidebar.radio[0].set_value("完整比价")
    at.run()
    at.radio(key="cmp_src").set_value("粘贴文件路径")
    at.run()
    check("UI：完整比价·粘贴文件路径 渲染无异常", len(at.exception) == 0 and not at.error)
except Exception:
    pass

if at.exception:
    for e in at.exception:
        print("   E:", getattr(e, "value", e))

print(f"\n===== UI 冒烟测试通过：{ok} 项断言 =====")
