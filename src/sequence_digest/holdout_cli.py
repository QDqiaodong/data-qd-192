"""sequence-digest-holdout 命令行：由摘要批次生成阅卷盲测编排。"""
import argparse
import json
import sys

from .holdout import build_plan
from .io import load, publish


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='sequence-digest-holdout',
        description='由 v1 报告数字摘要、提交编号与关联声明编排阅卷盲测集。')
    parser.add_argument('--source', required=True, help='批次 JSON')
    parser.add_argument('--output', required=True, help='新建的计划输出路径')
    args = parser.parse_args(argv)
    try:
        result = build_plan(load(args.source))
        publish(args.output, result)
    except (ValueError, OSError, UnicodeError, RecursionError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    print(json.dumps({'feasible_count': result['feasible_count']},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
