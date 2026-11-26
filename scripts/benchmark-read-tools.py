"""Reproducible synthetic read-tool CPU/payload benchmark; never opens Telegram.

Run from the integration directory. --baseline-ref reads only tools.py from Git;
results include local CPU/serialization time, not Telegram or network latency.
No timing threshold is used as a correctness test.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time
import types
import tracemalloc
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_tools(ref=None, filename=None):
    if filename:
        source = Path(filename).read_text(encoding="utf-8")
    elif ref:
        source = subprocess.check_output(
            ['git', 'show', f'{ref}:telegram-plugin-integration/telegram_reader/tools.py'],
            cwd=ROOT, encoding='utf-8')
    else:
        source = (ROOT / 'telegram_reader/tools.py').read_text(encoding='utf-8')
    module = types.ModuleType('benchmark_tools')
    exec(compile(source, str(ROOT / 'telegram_reader/tools.py'), 'exec'), module.__dict__)
    return module


class FakeClient:
    authorization_state = 'authorizationStateReady'

    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def request(self, method, **params):
        self.calls += 1
        if method == 'getChat':
            return {'id': params['chat_id'], 'title': 'Synthetic chat',
                    'type': {'@type': 'chatTypePrivate'}}
        if method == 'getMessage':
            return self.rows[0]
        if method in ('getChatHistory', 'searchChatMessages'):
            return {'messages': self.rows[:params['limit']]}
        raise AssertionError(method)


def fixture(text, count):
    now = datetime.now(timezone.utc)
    config = {'allowed_chats': {'42': {
        'not_before': (now - timedelta(days=1)).isoformat(),
        'expires_at': (now + timedelta(days=1)).isoformat(),
        'consent_note': 'Synthetic benchmark only'}}}
    rows = [{'id': 10000 - i, 'chat_id': 42, 'date': int(now.timestamp()),
             'sender_id': {'@type': 'messageSenderUser', 'user_id': 7},
             'content': {'@type': 'messageText', 'text': {'text': text}}}
            for i in range(count)]
    return config, rows


def measure(module, text, count, iterations, preview=None):
    config, rows = fixture(text, count)
    client = FakeClient(rows)
    tools = module.ReadTools(client, config)
    args = {'chat_id': '42', 'limit': count}
    if preview is not None:
        args['max_text_chars'] = preview
    for _ in range(10):
        tools.call('telegram_read_history', args)
    timings = []
    client.calls = 0
    for _ in range(iterations):
        started = time.perf_counter_ns()
        result = tools.call('telegram_read_history', args)
        timings.append((time.perf_counter_ns() - started) / 1000000)
    assert not result.get('isError'), result
    timed_calls = client.calls
    tracemalloc.start()
    tools.call('telegram_read_history', args)
    _, peak_python = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    original_dumps = json.dumps
    serialized_characters = 0
    def counted_dumps(value, *positional, **kwargs):
        nonlocal serialized_characters
        encoded = original_dumps(value, *positional, **kwargs)
        serialized_characters += len(encoded)
        return encoded
    with patch.object(json, 'dumps', side_effect=counted_dumps):
        tools.call('telegram_read_history', args)
    encoded = json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode()
    structured = result['structuredContent']
    timings.sort()
    return {
        'median_ms': round(statistics.median(timings), 4),
        'p95_ms': round(timings[min(len(timings)-1, int(len(timings)*0.95))], 4),
        'tdlib_calls_per_tool': timed_calls / iterations,
        'peak_python_bytes': peak_python,
        'serialized_characters': serialized_characters,
        'returned_messages': len(structured['messages']),
        'tool_result_utf8_bytes': len(encoded),
        'ascii_budget_bytes': len(json.dumps(result, ensure_ascii=True).encode()),
        'text_characters': sum(len(m.get('text', '')) for m in structured['messages']),
        'descriptor_utf8_bytes': len(json.dumps(tools.descriptors(), ensure_ascii=False,
                                               separators=(',', ':')).encode()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    baseline = parser.add_mutually_exclusive_group()
    baseline.add_argument('--baseline-ref')
    baseline.add_argument('--baseline-file', type=Path)
    parser.add_argument('--iterations', type=int, default=300)
    args = parser.parse_args()
    if not 10 <= args.iterations <= 10000:
        parser.error('--iterations must be between 10 and 10000')
    cases = {'short_ascii_50': ('A short synthetic message.', 50),
             'cyrillic_20': ('Синтетическое сообщение. ' * 12, 20),
             'emoji_budget_50': ('😀' * 6000, 50),
             'long_cyrillic_3': ('Синтетическое сообщение. ' * 40, 3)}
    modules = {'candidate': load_tools()}
    if args.baseline_ref or args.baseline_file:
        modules = {'baseline': load_tools(args.baseline_ref, args.baseline_file), **modules}
    report = {'python': platform.python_version(), 'platform': platform.system(),
              'iterations': args.iterations,
              'scope': 'synthetic local CPU and payload; no Telegram/network/tokenizer',
              'baseline_ref': args.baseline_ref, 'results': {}}
    for label, module in modules.items():
        report['results'][label] = {case: measure(module, text, count, args.iterations)
                                   for case, (text, count) in cases.items()}
    report['results']['candidate']['preview_240_long_cyrillic_3'] = measure(
        modules['candidate'], cases['long_cyrillic_3'][0], 3, args.iterations, preview=240)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
