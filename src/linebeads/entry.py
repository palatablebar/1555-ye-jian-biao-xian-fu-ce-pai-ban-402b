from argparse import ArgumentParser
from functools import lru_cache
import json
from pathlib import Path
import re

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge, UnsupportedMediaType

from .pipeline import DISPLAY, MODULES, execute, open_fixture, preview
from .retrocheck_slots.followup import schedule_followups

app = Flask(__name__, static_folder='web/static', template_folder='web/templates')
app.config['MAX_CONTENT_LENGTH'] = 1_500_000
app.json.sort_keys = False


def validate_parameters(capacity, strategy):
    if type(capacity) is not int or not 1 <= capacity <= 60:
        raise ValueError('额度应是 1 到 60 的整数')
    if not isinstance(strategy, str) or strategy not in {'balanced', 'focus'}:
        raise ValueError('策略应是 balanced 或 focus')
    return capacity, strategy


@lru_cache(maxsize=8)
def sample(capacity=12, strategy='balanced'):
    return preview(capacity, strategy)


@app.get('/')
def home():
    return render_template('desk.html', modules=MODULES, display=DISPLAY)


@app.get('/health')
def health():
    return {'status': 'ok', 'service': 'linebeads'}


@app.get('/api/linebeads/overview')
def overview():
    return jsonify(sample())


@app.post('/api/linebeads/run')
def run_case():
    try:
        posted = request.get_json()
        allowed = {'capacity', 'strategy', 'records', 'as_of', 'orders', 'future_days',
                   'horizon', 'daily_capacity', 'replay', 'baseline', 'replay_end', 'coverage_end'}
        if not isinstance(posted, dict) or posted.keys() - allowed:
            raise ValueError('请求字段无效')
        if 'orders' in posted and 'records' not in posted:
            return schedule_case()
        mode = posted.get('strategy', 'balanced')
        capacity, strategy = validate_parameters(posted.get('capacity', 12), mode)
        if 'records' not in posted:
            if 'as_of' in posted:
                raise ValueError('导入截止日需要同时提交 records')
            return jsonify(sample(capacity, strategy))
        as_of = posted.get('as_of', 96)
        if type(as_of) is not int or not 91 <= as_of <= 3650:
            raise ValueError('截止日须在第 91 日后')
        future_days = posted.get('future_days', posted.get('horizon', 5))
        if type(future_days) is not int or not 1 <= future_days <= 31:
            raise ValueError('未来排班天数须为 1 至 31 的整数')
        return jsonify(execute(
            posted['records'], capacity, strategy, as_of=as_of,
            future_days=future_days, daily_capacity=posted.get('daily_capacity'),
            replay=posted.get('replay'), baseline=posted.get('baseline'),
            replay_end=posted.get('replay_end'), coverage_end=posted.get('coverage_end'),
        ))
    except (BadRequest, UnsupportedMediaType):
        return jsonify(code='INVALID_JSON', message='请提交 JSON 对象'), 400
    except ValueError as issue:
        return jsonify(code='INVALID_BATCH', message=str(issue)), 422


@app.post('/api/linebeads/schedule')
@app.post('/api/linebeads/future-schedule')
@app.post('/api/linebeads/followups')
def schedule_case():
    try:
        payload = request.get_json(silent=True)
        if isinstance(payload, list):
            payload = {'orders': payload}
        if not isinstance(payload, dict):
            raise ValueError('排班请求应为对象或单据数组')
        records = payload.get('records')
        first_record = records[0] if isinstance(records, list) and records and isinstance(records[0], dict) else {}
        if 'records' in payload and 'orders' not in payload and any(
            key in first_record for key in ('retro_cd', 'form')
        ):
            capacity = payload.get('capacity', payload.get('daily_capacity', 12))
            strategy = payload.get('strategy', 'balanced')
            as_of = payload.get('as_of', 96)
            result = execute(payload['records'], capacity, strategy, as_of=as_of)
            return jsonify(result.get('followup_schedule', result))
        plan = schedule_followups(**payload)
        return jsonify(plan.to_dict())
    except ValueError as issue:
        return jsonify(code='INVALID_SCHEDULE', message=str(issue)), 422


@app.get('/api/linebeads/schedule')
def schedule_sample():
    result = sample()
    return jsonify(result.get('followup_schedule', {'orders': result['rows']}))


@app.get('/api/linebeads/orders/<key>')
def order_detail(key):
    result = sample()
    row = next((item for item in result['rows'] if item['key'] == key), None)
    if row is None:
        return jsonify(code='UNKNOWN_ORDER', message='未找到单据'), 404
    return jsonify(row)


@app.get('/api/linebeads/stages/<stage>')
def stage_detail(stage):
    mapping = {slug: index for index, (slug, _) in enumerate(MODULES)}
    if stage not in mapping:
        return jsonify(code='UNKNOWN_STAGE', message='未找到业务环节'), 404
    try:
        raw = request.args.get('capacity', '12')
        if not re.fullmatch(r'[0-9]{1,2}', raw):
            raise ValueError('额度应是 1 到 60 的整数')
        capacity, strategy = validate_parameters(int(raw), request.args.get('strategy', 'balanced'))
    except ValueError as issue:
        return jsonify(code='INVALID_BATCH', message=str(issue)), 422
    result = sample(capacity, strategy)
    return jsonify(run_id=result['run_id'], **result['sections'][mapping[stage]])


@app.errorhandler(RequestEntityTooLarge)
def rejected_size(_issue):
    return jsonify(code='PAYLOAD_TOO_LARGE', message='单据批次超过 1.5 MB'), 413


def main():
    parser = ArgumentParser(description=DISPLAY['title'])
    commands = parser.add_subparsers(dest='command', required=True)
    runner = commands.add_parser('evaluate', help='复算一个班次，并输出处置单与审计证据')
    runner.add_argument('--capacity', type=int, default=12)
    runner.add_argument('--strategy', choices=('balanced', 'focus'), default='balanced')
    runner.add_argument('--input', type=Path)
    runner.add_argument('--output', type=Path)
    scheduler = commands.add_parser('schedule', help='排未来数日巡检并核对最迟/实排日期')
    scheduler.add_argument('--input', type=Path, help='排班 JSON：orders 数组或包含 orders 的对象')
    scheduler.add_argument('--capacity', type=int, default=4)
    scheduler.add_argument('--start-day', type=int, default=0)
    scheduler.add_argument('--days', type=int, default=5)
    scheduler.add_argument('--output', type=Path)
    server = commands.add_parser('serve', help='打开本地工作台')
    server.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    if args.command == 'serve':
        from waitress import serve
        sample()
        serve(app, host='0.0.0.0', port=args.port, threads=4)
        return
    if args.command == 'schedule':
        try:
            if args.input:
                document = json.loads(args.input.read_text(encoding='utf-8'))
            else:
                document = {'orders': []}
            if isinstance(document, list):
                document = {'orders': document}
            if 'records' in document and 'orders' not in document:
                result = execute(document['records'], args.capacity, as_of=document.get('as_of', 96),
                                 future_days=args.days)['followup_schedule']
            else:
                payload = dict(document)
                payload.setdefault('daily_capacity', args.capacity)
                payload.setdefault('start_day', args.start_day)
                payload.setdefault('horizon', args.days)
                result = schedule_followups(**payload).to_dict()
            output = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
            if args.output:
                args.output.write_text(output, encoding='utf-8')
            else:
                print(output, end='')
        except (ValueError, OSError) as issue:
            parser.error(str(issue))
        return
    try:
        capacity, strategy = validate_parameters(args.capacity, args.strategy)
        if args.input:
            custom = open_fixture(args.input)
            result = execute(custom['records'], capacity, strategy, as_of=custom.get('as_of', 96))
        else:
            result = sample(capacity, strategy)
    except (ValueError, OSError) as issue:
        parser.error(str(issue))
    output = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if args.output:
        args.output.write_text(output, encoding='utf-8')
    else:
        print(output, end='')


if __name__ == '__main__':
    main()
