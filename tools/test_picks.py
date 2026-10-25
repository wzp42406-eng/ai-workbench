"""tools/picks.py 的测试：python3 -m unittest discover tools"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import picks  # noqa: E402

NOW = dt.datetime.now(dt.timezone.utc)


def iso(days):
    return (NOW - dt.timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')


def repo(name, desc='An MCP server for Claude Code', pushed=1, **kw):
    return {'full_name': name, 'html_url': f'https://github.com/{name}', 'description': desc, 'stargazers_count': 100,
            'language': 'Go', 'topics': ['mcp'], 'created_at': iso(5), 'pushed_at': iso(pushed), 'license': {'spdx_id': 'MIT'}, 'homepage': '', **kw}


SEARCH = {'items': [repo('a/tool'), repo('b/awesome-llm'), repo('c/nodesc', desc=None), repo('d/stale', pushed=30), repo('e/old')]}
GOOD = {'zh': '给 Claude Code 用的 MCP 服务', 'what': '让 Claude Code 操作浏览器', 'why': '你常用 Claude Code', 'setup': '要在终端安装', 'how': '复制一行命令安装', 'fit': 4}


def fake_gh(path, accept=''):
    if path.startswith('/search/'):
        return SEARCH
    return 'README 内容'


class PicksTest(unittest.TestCase):
    def run_main(self, ask_result, prev=None):
        tmp = Path(tempfile.mkdtemp())
        argv = ['picks.py', '--out', str(tmp / 'out.json')]
        if prev is not None:
            (tmp / 'prev.json').write_text(json.dumps(prev), encoding='utf-8')
            argv += ['--prev', str(tmp / 'prev.json')]
        calls = []

        def ask(text):
            calls.append(text)
            if isinstance(ask_result, Exception):
                raise ask_result
            return ask_result
        with mock.patch.object(picks, 'gh', fake_gh), mock.patch.object(picks, 'ask_claude', ask), \
                mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'x'}), mock.patch.object(sys, 'argv', argv):
            picks.main()
        return json.loads((tmp / 'out.json').read_text(encoding='utf-8')), calls

    def test_filters_and_summarizes(self):
        data, calls = self.run_main(GOOD)
        self.assertEqual([x['name'] for x in data['items']], ['a/tool', 'e/old'])  # 资源合集、没简介、停更的去掉
        self.assertEqual(data['items'][0]['ai']['fit'], 4)
        self.assertIn('README 内容', calls[0])

    def test_reuses_recent_summary(self):
        prev = {'items': [{'name': 'a/tool', 'ai': {**GOOD, 'zh': '旧的', 'at': NOW.isoformat()}}]}
        data, calls = self.run_main(GOOD, prev)
        self.assertEqual(data['items'][0]['ai']['zh'], '旧的')
        self.assertEqual(len(calls), 1)  # 只总结 e/old

    def test_week_projects_not_crowded_out(self):
        week = [repo('w/new')]
        month = [repo(f'm/big{i}', stargazers_count=9000 - i, created_at=iso(20)) for i in range(40)] + [repo('x/pose', desc='NSFW pose skill')]

        def gh(path, accept=''):
            if path.startswith('/search/'):
                return {'items': week if f'created%3A%3E%3D{(NOW - dt.timedelta(days=7)).date()}' in path else month}
            return ''
        with mock.patch.object(picks, 'gh', gh):
            names = [x['name'] for x in picks.search(NOW)]
        self.assertEqual(names[0], 'w/new')
        self.assertEqual(len(names), picks.KEEP)
        self.assertNotIn('x/pose', names)

    def test_bad_output_is_dropped(self):
        data, _ = self.run_main({**GOOD, 'fit': 9})
        self.assertNotIn('ai', data['items'][0])
        data, _ = self.run_main(RuntimeError('boom'))
        self.assertEqual(len(data['items']), 2)


if __name__ == '__main__':
    unittest.main()
