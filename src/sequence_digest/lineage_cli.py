"""sequence-digest-lineage 命令行：seal 封存、export 重发。"""
import argparse
import json
import sys

from .lineage import LineageError, export, seal


def _build_parser():
    parser = argparse.ArgumentParser(
        prog='sequence-digest-lineage',
        description='坐标血缘发布包：封存切分坐标发布，并凭独立回执重发。')
    subparsers = parser.add_subparsers(dest='command', required=True)

    seal_parser = subparsers.add_parser('seal', help='校验并封存一次发布')
    seal_parser.add_argument('--source', required=True, help='v1 切分输入 JSON')
    seal_parser.add_argument('--annotations', required=True, help='标注 JSON')
    seal_parser.add_argument('--archive', required=True, help='归档目录')
    seal_parser.add_argument('--release-id', required=True, help='发布 ID（仓库 ID 规则）')

    export_parser = subparsers.add_parser('export', help='仅凭归档与回执重发发布文件')
    export_parser.add_argument('--archive', required=True, help='归档目录（可只读）')
    export_parser.add_argument('--receipt', required=True, help='使用方独立留存的回执 JSON')
    export_parser.add_argument('--output', required=True, help='新建的发布输出路径')
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)
    try:
        if args.command == 'seal':
            _, receipt = seal(args.source, args.annotations, args.archive, args.release_id)
            print(json.dumps(receipt, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':')))
        else:
            export(args.archive, args.receipt, args.output)
    except (LineageError, ValueError, OSError, UnicodeError, RecursionError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
