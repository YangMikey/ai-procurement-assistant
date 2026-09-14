# -*- coding: utf-8 -*-
"""AI 采购助理核心模块包。

模块一览（技能注册见 registry.py，卡片即元数据）：
- matcher    两表匹配补缺引擎（吸收自「采购数据通用匹配工具」三轮瀑布）
- excel_io   Excel/CSV 读写、备份、写回
- experience 匹配经验库（人工确认沉淀，越用越快）
- registry   技能注册表（V2 意图路由的地基）
"""
import glob
import os

_CORE_DIR = os.path.dirname(os.path.abspath(__file__))


def core_files_mtime():
    """core 目录下所有 .py 的最新修改时间（用于检测「改了 core 但没重启」）。"""
    files = glob.glob(os.path.join(_CORE_DIR, "*.py"))
    return max((os.path.getmtime(f) for f in files), default=0.0)


# 本模块被导入（=进程启动）时的 core 代码时间；app.py 每次运行与之比对，
# 不一致说明 core 已更新但内存里还是旧模块 → 提示重启（Streamlit 不热重载 core）。
CORE_LOADED_MTIME = core_files_mtime()

