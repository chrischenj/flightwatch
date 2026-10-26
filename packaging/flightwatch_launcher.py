"""Windows 免安装版入口：双击即可启动本地网页界面。

打包方式见同目录 build_windows.bat，或手动：

    python -m PyInstaller --onefile --name flightwatch ^
        --add-data "flightwatch/web;flightwatch/web" ^
        --paths . packaging/flightwatch_launcher.py

环境变量：
    FLIGHTWATCH_NO_BROWSER=1   启动时不自动打开浏览器（方便脚本/CI 里测试）
    FLIGHTWATCH_DATA_DIR=<dir> 指定缓存与历史数据目录
"""
import os
import sys

from flightwatch.cli import main

if __name__ == "__main__":
    argv = sys.argv[1:] or ["serve"]
    if os.environ.get("FLIGHTWATCH_NO_BROWSER") and "serve" in argv:
        argv = [a for a in argv] + ["--no-browser"]
    raise SystemExit(main(argv))
