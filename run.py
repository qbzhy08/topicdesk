"""启动：python run.py [--port 8000] [--auto-resume]"""
import argparse

from app.server import create_app

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--auto-resume", action="store_true", help="启动时自动恢复上次中断的任务")
    ap.add_argument("--recheck-every", type=float, default=60,
                    help="每隔多少分钟复查一次可能变化的页面，0 表示关闭")
    a = ap.parse_args()
    create_app(auto_resume=a.auto_resume, recheck_every=a.recheck_every).run(
        host=a.host, port=a.port, threaded=True)
