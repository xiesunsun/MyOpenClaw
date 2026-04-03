import subprocess
import argparse
import datetime

def capture_screen(output_path):
    # 使用 macOS 自带的 screencapture 命令
    try:
        subprocess.run(["screencapture", output_path], check=True)
        print(f"截图已保存至: {output_path}")
    except Exception as e:
        print(f"截图失败: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=f"screenshot_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.png")
    args = parser.parse_args()
    
    capture_screen(args.output)
