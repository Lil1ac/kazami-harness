"""在独立 GUI 进程中打开系统目录选择器；标准输出只返回选定目录或取消。"""

import json


def main() -> None:
    """在当前电脑显示文件夹选择框，输出一条 JSON；关闭窗口返回空路径，不操作文件。"""
    from tkinter import Tk, filedialog

    root = Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askdirectory(parent=root, title="Agent Harness · 选择项目文件夹", mustexist=True)
        print(json.dumps({"path": selected or None}, ensure_ascii=False), flush=True)
    finally:
        root.destroy()


if __name__ == "__main__":
    main()
