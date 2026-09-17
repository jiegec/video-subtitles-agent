"""Web 服务入口：`uv run subtitles-agent`（或 `python -m subtitles_agent`）启动。"""

from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="启动视频字幕 Agent 的 Web 界面")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8765, help="监听端口")
    parser.add_argument("--reload", action="store_true", help="开发模式自动重载")
    args = parser.parse_args()

    print(f"\n  字幕 Agent 已启动 →  http://{args.host}:{args.port}\n")
    uvicorn.run(
        "subtitles_agent.web.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
