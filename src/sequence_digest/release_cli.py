"""sequence-digest-release-check 命令行：周报整套附件的联发审查。"""
import argparse
import json
import sys

from .io import load, publish
from .release import review


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='sequence-digest-release-check',
        description='审查整套周报附件联合刊登后是否仍可反推受保护格。')
    parser.add_argument('--snapshot', required=True,
                        help='sequence-digest-ledger snapshot 的 stdout JSON')
    parser.add_argument('--layout', required=True, help='版面与保护清单 JSON')
    parser.add_argument('--output', required=True, help='新建的整包输出路径')
    args = parser.parse_args(argv)
    try:
        result = review(load(args.snapshot), load(args.layout))
    except (ValueError, OSError, UnicodeError, RecursionError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    if result['forced']:
        # 可推出的受保护格（按课程、日期升序），不输出推得的数值。
        print(json.dumps({'forced': result['forced']},
                         ensure_ascii=False, separators=(',', ':')),
              file=sys.stderr)
        return 3
    try:
        publish(args.output, result['package'])
    except (ValueError, OSError, UnicodeError, RecursionError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    print(json.dumps({'pages': len(result['package']['pages'])},
                     ensure_ascii=False, sort_keys=True, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
