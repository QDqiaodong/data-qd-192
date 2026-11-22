"""sequence-digest-ledger 命令行：apply 接入批次、snapshot 快照、changes 拉取勘误。"""
import argparse
import json
import os
from pathlib import Path
import sys

from .io import load
from .ledger import LedgerError, apply_batch, changes, snapshot

PAUSE_ENV = 'SEQUENCE_DIGEST_LEDGER_TEST_PAUSE_FILE'


def _interrupt_hook():
    """仅供测试复现中断：在临时状态 fsync 后、正式替换前暂停写者。

    设置该环境变量后，写者在提交点先写出标记文件，再阻塞读取 stdin，
    直到外部终止进程或关闭 stdin。正常使用不设置，行为完全不变。
    """
    marker = os.environ.get(PAUSE_ENV)
    if not marker:
        return None

    def pause():
        Path(marker).write_bytes(f'{os.getpid()}\n'.encode('ascii'))
        try:
            # 阻塞直到外部关闭 stdin 或终止本进程。
            sys.stdin.buffer.read(1)
        except (OSError, ValueError, AttributeError):
            pass

    return pause


def _build_parser():
    parser = argparse.ArgumentParser(
        prog='sequence-digest-ledger',
        description='人工复核工作量台账：接入数字摘要批次并对外提供快照与勘误流。')
    subparsers = parser.add_subparsers(dest='command', required=True)

    apply_parser = subparsers.add_parser('apply', help='校验并原子接纳一个批次')
    apply_parser.add_argument('--store', required=True, help='台账位置')
    apply_parser.add_argument('--source', required=True, help='批次 JSON')

    snapshot_parser = subparsers.add_parser('snapshot', help='读取当前游标与桶表')
    snapshot_parser.add_argument('--store', required=True, help='台账位置')

    changes_parser = subparsers.add_parser('changes', help='按游标拉取勘误页')
    changes_parser.add_argument('--store', required=True, help='台账位置')
    changes_parser.add_argument('--after', required=True, type=int,
                                help='只取严格大于该游标的勘误')
    changes_parser.add_argument('--limit', required=True, type=int,
                                help='本页最多条数（1..100）')
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)
    try:
        if args.command == 'apply':
            result = apply_batch(args.store, load(args.source),
                                 interrupt=_interrupt_hook())
        elif args.command == 'snapshot':
            result = snapshot(args.store)
        else:
            result = changes(args.store, args.after, args.limit)
    except (LedgerError, ValueError, OSError, UnicodeError, RecursionError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True,
                     allow_nan=False, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
