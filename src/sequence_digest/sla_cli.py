"""sequence-digest-sla 命令行：核算本地复核工单的 SLA 首次违约。"""
import argparse
import json
import sys

from .io import load, publish
from .sla import build_digest


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='sequence-digest-sla',
        description='按助教队服务时段、暂停/交接与策略时限核算工单首次违约。')
    parser.add_argument('--source', required=True, help='工单 SLA 输入 JSON')
    parser.add_argument('--output', required=True, help='新建的核算输出路径')
    args = parser.parse_args(argv)
    try:
        result = build_digest(load(args.source))
        publish(args.output, result)
    except (ValueError, OSError, UnicodeError, RecursionError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(
        {'tickets': len(result['tickets']),
         'breached_teams': len(result['breaches_by_team'])},
        ensure_ascii=False, sort_keys=True, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
