"""sequence-digest-staff 命令行：由台账快照与花名册生成复核产能排班。"""
import argparse
import json
import sys

from .io import load, publish
from .staff import build_plan


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='sequence-digest-staff',
        description='把复核点需求排给助教队产能班次，保留已确认并尽量少动旧草案。')
    parser.add_argument('--snapshot', required=True,
                        help='sequence-digest-ledger snapshot 的 stdout JSON')
    parser.add_argument('--roster', required=True, help='班次与旧草案预约')
    parser.add_argument('--output', required=True, help='新建的计划输出路径')
    args = parser.parse_args(argv)
    try:
        result = build_plan(load(args.snapshot), load(args.roster))
        publish(args.output, result)
    except (ValueError, OSError, UnicodeError, RecursionError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(
        {'covered_points': result['covered_points'],
         'retained_provisional_points': result['retained_provisional_points']},
        ensure_ascii=False, sort_keys=True, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
