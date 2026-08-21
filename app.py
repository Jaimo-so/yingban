from __future__ import annotations

import argparse
import json
import logging
import sys

from agent import AgentRuntime
from catalog import MovieCatalog
from integrations import InternetRuntime
from server import AppContext, YingbanHTTPServer
from settings import Settings
from storage import Store
from voice import VoiceRuntime


def build_context(settings: Settings | None = None) -> AppContext:
    settings = settings or Settings()
    store = Store(settings.database_path, settings.invite_pepper, settings.session_secret)
    catalog = MovieCatalog(store, settings.movie_seed_path)
    catalog.load_seed()
    internet = InternetRuntime(settings, store)
    voice = VoiceRuntime(settings, store)
    agent = AgentRuntime(settings, store, catalog, internet)
    return AppContext(
        settings=settings,
        store=store,
        catalog=catalog,
        agent=agent,
        internet=internet,
        voice=voice,
    )


def serve(app: AppContext) -> None:
    server = YingbanHTTPServer((app.settings.host, app.settings.port), app)
    model_config = app.agent.model_config()
    mode = "演示 Agent" if model_config.demo_mode else f"模型 {model_config.model_id}"
    print(f"影伴已启动：http://{app.settings.host}:{app.settings.port}")
    print(f"管理员后台：http://{app.settings.host}:{app.settings.port}/admin")
    print(f"当前模式：{mode}")
    print(f"联网电影：{'已接入' if app.internet and app.internet.config().movie_enabled else '未配置'}")
    print(f"语音交互：{'已接入' if app.voice and app.voice.config().enabled else '未配置'}")
    for warning in app.settings.security_warnings():
        print(f"[安全提醒] {warning}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n影伴已停止。")
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="影伴 AI 电影伙伴")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("serve", help="启动 Web 服务")
    generate = subparsers.add_parser("generate-invites", help="在本机批量生成邀请码")
    generate.add_argument("count", type=int, nargs="?", default=1)
    subparsers.add_parser("list-invites", help="查看邀请码状态（不显示完整邀请码）")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = build_context()
    command = args.command or "serve"
    if command == "serve":
        serve(app)
        return 0
    if command == "generate-invites":
        codes = app.store.generate_invites(args.count)
        print("以下邀请码只在本次生成时完整显示，请通过安全渠道发放：")
        for code in codes:
            print(code)
        return 0
    if command == "list-invites":
        print(json.dumps(app.store.list_invites(), ensure_ascii=False, indent=2))
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
