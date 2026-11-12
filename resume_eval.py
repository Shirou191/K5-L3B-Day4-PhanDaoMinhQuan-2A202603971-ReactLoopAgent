"""Chạy eval từng case một, lưu ngay từng case, chạy lại chỉ làm phần còn thiếu.

Vì sao cần: `evals/run_evals.py` chỉ ghi file ở cuối, nên khi provider hết quota ở
giữa thì toàn bộ case đã chấm xong cũng mất trắng. Trên free tier có giới hạn
token/ngày, điều đó có nghĩa không bao giờ hoàn thành được 22 case.

Script này KHÔNG thay đổi cách chạy hay cách chấm: nó gọi đúng `run_case` và
`summarize` của `evals/`, cùng cấu hình, cùng `max_steps`. Chỉ khác ở chỗ lưu
từng case rồi gộp lại, nên có thể chạy qua nhiều cửa sổ quota.
"""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'evals'))

from providers import CONFIG
from grading import summarize
from run_evals import run_case


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', choices=list(CONFIG), default=os.getenv('PROVIDER', 'groq'))
    parser.add_argument('--model', default=os.getenv('MODEL'))
    parser.add_argument('--judge-provider', choices=list(CONFIG), default=os.getenv('JUDGE_PROVIDER'))
    parser.add_argument('--judge-model', default=os.getenv('JUDGE_MODEL'))
    parser.add_argument('--max-steps', type=int, default=8)
    parser.add_argument('--store', default='.eval_cache', help='Thư mục lưu từng case')
    parser.add_argument('--output', default='eval_after.json')
    parser.add_argument('--stop-after-failures', type=int, default=2,
                        help='Dừng sau N case lỗi liên tiếp (hết quota thì chạy tiếp là vô ích)')
    args = parser.parse_args()
    args.judge_provider = args.judge_provider or args.provider
    args.judge_model = args.judge_model or args.model
    args.bonus = False
    if not args.model or not args.judge_model:
        parser.error('Thiếu --model hoặc MODEL')

    cases = json.loads((ROOT / 'evals' / 'cases.json').read_text(encoding='utf-8'))
    store = Path(args.store)
    store.mkdir(parents=True, exist_ok=True)

    streak = 0
    for case in cases:
        path = store / f"{case['id']}.json"
        if path.exists():
            cached = json.loads(path.read_text(encoding='utf-8'))
            if cached.get('grade', {}).get('score') is not None:
                print(f"{case['id']}: {cached['grade']['score']} (đã có, bỏ qua)", flush=True)
                streak = 0
                continue
        print(f"Đang chạy {case['id']}", flush=True)
        item = run_case(case, args)
        # Ghi ngay, trước khi case sau có cơ hội làm hỏng cả run.
        path.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding='utf-8')
        score = item['grade'].get('score')
        print(f"{case['id']}: {score} ({item['grade']['status']})", flush=True)
        if score is None:
            reason = item['result'].get('error') or item['grade'].get('reason', '')
            print(f"  lý do: {str(reason)[:200]}", flush=True)
            streak += 1
            if streak >= args.stop_after_failures:
                print(f'Dừng sau {streak} case lỗi liên tiếp. Chạy lại script khi có quota;'
                      f' các case đã chấm trong {store} được giữ lại.', file=sys.stderr)
                break
        else:
            streak = 0

    results = []
    missing = []
    for case in cases:
        path = store / f"{case['id']}.json"
        if path.exists():
            results.append(json.loads(path.read_text(encoding='utf-8')))
        else:
            missing.append(case['id'])
    scored = sum(1 for r in results if r['grade'].get('score') is not None)
    print(f'Đã chấm {scored}/{len(cases)} case.')
    if scored < len(cases):
        print(f"Còn thiếu: {', '.join(missing + [r['id'] for r in results if r['grade'].get('score') is None])}")
        print(f'Chưa ghi {args.output}: chỉ ghi khi đủ 22 case có điểm.', file=sys.stderr)
        return 1

    report = {'summary': summarize(results),
              'config': {'provider': args.provider, 'model': args.model,
                         'judge_provider': args.judge_provider, 'judge_model': args.judge_model,
                         'max_steps': args.max_steps, 'dataset': 'core'},
              'cases': results}
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n',
                                 encoding='utf-8')
    print(f"Điểm: {report['summary']['score_0_100']}/100. File: {args.output}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
