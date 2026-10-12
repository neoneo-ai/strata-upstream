"""neo/neo_entry.py - 打包成 exe 时的入口脚本。

neo/manager.py 用的是包内相对导入（from . import state），PyInstaller 把包里的模块
当顶层脚本分析时认不出父包，那些导入会全被标成"找不到模块"。所以打包给一个
显式 import 包的脚本，pathex 指到仓库根（neo/NeoStrata.spec 里）。

源码跑不用这个文件：python -m neo.manager。
"""
import sys

from neo.manager import main

if __name__ == "__main__":
    sys.exit(main())