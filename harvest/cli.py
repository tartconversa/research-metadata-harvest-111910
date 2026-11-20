"""采集端命令行：初次采集、日常接续、订阅调整、引用状态查询、覆盖证明。

用法（先启动来源：python -m source.app --port 8765）：
  python -m harvest.cli --db harvest.db init --config inputs/request.json
  python -m harvest.cli --db harvest.db run [--mode auto|full|incremental]
  python -m harvest.cli --db harvest.db subscribe --team t --repo university-a --set robot
  python -m harvest.cli --db harvest.db unsubscribe --team t --repo university-a --set robot
  python -m harvest.cli --db harvest.db list --team transfer-a
  python -m harvest.cli --db harvest.db cite --team transfer-a --identifier oai:university-a:1
  python -m harvest.cli --db harvest.db citations [--team transfer-a]
  python -m harvest.cli --db harvest.db citation-status --id 1
  python -m harvest.cli --db harvest.db citation-content --id 1
  python -m harvest.cli --db harvest.db coverage
  python -m harvest.cli --db harvest.db prune [--apply]
"""
import argparse
import json
import sys

from harvest.service import Receiver

DEFAULT_BASE = 'http://127.0.0.1:8765'


def show(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(prog='harvest', description='高校成果元数据采集端')
    parser.add_argument('--db', default='harvest.db', help='接收端 SQLite 数据库路径')
    parser.add_argument('--base', default=DEFAULT_BASE, help='合成来源地址')
    commands = parser.add_subparsers(dest='command', required=True)

    cmd = commands.add_parser('init', help='初次采集：载入订阅配置并全量采集')
    cmd.add_argument('--config', required=True, help='订阅配置（如 inputs/request.json）')

    cmd = commands.add_parser('run', help='日常接续：增量为主，定期全量核对')
    cmd.add_argument('--mode', choices=['auto', 'full', 'incremental'], default='auto')

    for name in ('subscribe', 'unsubscribe'):
        cmd = commands.add_parser(name, help='订阅调整')
        cmd.add_argument('--team', required=True)
        cmd.add_argument('--repo', required=True)
        cmd.add_argument('--set', dest='set_spec', required=True)

    cmd = commands.add_parser('list', help='按团队订阅范围查询成果清单')
    cmd.add_argument('--team', required=True)

    cmd = commands.add_parser('cite', help='为团队保存一条成果引用')
    cmd.add_argument('--team', required=True)
    cmd.add_argument('--identifier', required=True)

    cmd = commands.add_parser('citations', help='列出引用及当前状态')
    cmd.add_argument('--team')

    cmd = commands.add_parser('citation-status', help='查询单条引用状态')
    cmd.add_argument('--id', type=int, required=True)

    cmd = commands.add_parser('citation-content', help='取用引用对应的原件内容')
    cmd.add_argument('--id', type=int, required=True)

    commands.add_parser('coverage', help='覆盖证明：完整覆盖与仍待核实的目录')

    cmd = commands.add_parser('prune', help='清理无任何有效订阅覆盖的原件')
    cmd.add_argument('--apply', action='store_true')

    args = parser.parse_args(argv)
    receiver = Receiver(args.base, args.db)
    try:
        if args.command == 'init':
            receiver.load_config(args.config)
            show(receiver.harvest(mode='auto'))
        elif args.command == 'run':
            show(receiver.harvest(mode=args.mode))
        elif args.command in ('subscribe', 'unsubscribe'):
            getattr(receiver, args.command)(args.team, args.repo, args.set_spec)
            show({'ok': True, 'subscriptions': receiver.store.subscriptions(team=args.team)})
        elif args.command == 'list':
            show(receiver.list_team(args.team))
        elif args.command == 'cite':
            show({'citation_id': receiver.cite(args.team, args.identifier)})
        elif args.command == 'citations':
            show([receiver.citation_status(c['id'])
                  for c in receiver.store.citations(team=args.team)])
        elif args.command == 'citation-status':
            show(receiver.citation_status(args.id))
        elif args.command == 'citation-content':
            show(receiver.citation_content(args.id))
        elif args.command == 'coverage':
            show(receiver.coverage())
        elif args.command == 'prune':
            show(receiver.prune(apply=args.apply))
    except ValueError as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        receiver.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
