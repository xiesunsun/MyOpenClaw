---
name: screen-capturer
description: 获取当前桌面或指定窗口的截图。当用户要求截屏、查看当前界面、或需要通过截图分析视觉内容时使用。
---

# Screen Capturer

用于通过程序化方式获取屏幕截图。

## 使用方法

直接调用 `scripts/capture.py` 即可完成截图。

### 常用命令

```bash
# 截取全屏并保存至指定位置
python3 scripts/capture.py --output screenshot.png
```

## 注意事项

- 此技能依赖于系统截图工具或相关库（如 `pyautogui` 或 `screencapture`）。
- 确保执行环境具有操作屏幕的系统权限。
