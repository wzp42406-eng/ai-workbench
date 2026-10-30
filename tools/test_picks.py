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


def repo(name, desc='An MCP server for Claude Code', pushed=1, stars=100, created=5, **kw):
    return {'full_name': name, 'html_url': f'https://github.com/{name}', 'description': desc, 'stargazers_count': stars,
            'language': 'Go', 'topics': ['mcp'], 'created_at': iso(created), 'pushed_at': iso(pushed), 'license': {'spdx_id': 'MIT'},
            'homepage': '', **kw}


SEARCH = [repo('a/tool', stars=500), repo('b/awesome-llm'), repo('c/nodesc', desc=None), repo('d/stale', pushed=30),
          repo('e/slow', stars=100, created=50), repo('f/cooking', desc='Recipes for dinner', topics=[]), repo('g/pose', desc='NSFW pose skill')]
GOOD = {'zh': '给 Claude Code 用的 MCP 服务', 'what': '让 Claude Code 操作浏览器', 'why': '你常用 Claude Code', 'setup': '要在终端安装', 'how': '复制一行命令安装', 'fit': 4}
TRENDING_HTML = '''
<article class="Box-row"><h2 class="h3 lh-condensed">
  <a data-view-component="true" href="/old/agent-kit" class="Link">old / agent-kit</a></h2>
  <span class="d-inline-block float-sm-right">
    <svg aria-hidden="true"></svg>
    7,000 stars this week
  </span></article>
<article class="Box-row"><h2 class="h3 lh-condensed"><a href="/x/no-stars">x / no-stars</a></h2></article>
'''


def fake_gh(search=SEARCH, extra=None):
    extra = extra or {}

    def gh(path, accept=''):
        if path.startswith('/search/'):
            return {'items': search}
        if path.startswith('/repos/') and path.endswith('/readme'):
            return 'README 内容'
        return extra[path[len('/repos/'):]]
    return gh


class PicksTest(unittest.TestCase):
    def run_main(self, ask_result, prev=None, gh=None, hot=None):
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
        with mock.patch.object(picks, 'gh', gh or fake_gh()), mock.patch.object(picks, 'trending', lambda: hot or {}), \
                mock.patch.object(picks, 'ask_claude', ask), mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'x'}), \
                mock.patch.object(sys, 'argv', argv):
            picks.main()
        return json.loads((tmp / 'out.json').read_text(encoding='utf-8')), calls

    def test_filters_and_ranks_by_growth(self):
        data, calls = self.run_main(GOOD)
        # 资源合集、没简介、停更、和 AI 无关、NSFW 的去掉；a/tool 每天 100 颗星，e/slow 每天 2 颗
        self.assertEqual([x['name'] for x in data['items']], ['a/tool', 'e/slow'])
        self.assertEqual(data['items'][0]['growth'], {'per_day': 100.0, 'basis': 'age'})
        self.assertEqual(data['items'][0]['ai']['fit'], 4)
        self.assertIn('README 内容', calls[0])
        self.assertEqual(data['shown'], ['a/tool', 'e/slow'])

    def test_trending_counts_and_old_projects_allowed(self):
        old = repo('old/agent-kit', desc='Agent toolkit', stars=90000, created=900)
        data, _ = self.run_main(GOOD, gh=fake_gh(extra={'old/agent-kit': old}), hot={'old/agent-kit': 1000.0})
        self.assertEqual(data['items'][0]['name'], 'old/agent-kit')
        self.assertEqual(data['items'][0]['growth']['basis'], 'trending')

    def test_never_recommends_twice(self):
        earlier = (NOW - dt.timedelta(days=3)).isoformat(timespec='seconds')
        prev = {'generated_at': earlier, 'items': [{'name': 'a/tool', 'stars': 400, 'picked': earlier, 'ai': GOOD}],
                'shown': ['a/tool', 'z/gone'], 'seen': {'e/slow': {'stars': 40, 'at': earlier}}}
        data, calls = self.run_main(GOOD, prev)
        self.assertEqual([x['name'] for x in data['items']], ['e/slow', 'a/tool'])  # 新推荐在前，旧的留着
        self.assertEqual(data['items'][1]['picked'], earlier)
        self.assertEqual(len(calls), 1)
        self.assertEqual(data['items'][0]['growth'], {'per_day': 20.0, 'basis': 'snapshot'})  # 3 天涨了 60
        self.assertIn('z/gone', data['shown'])

    def test_old_format_items_count_as_shown(self):
        prev = {'generated_at': NOW.isoformat(timespec='seconds'), 'items': [{'name': 'a/tool', 'stars': 1}]}
        data, _ = self.run_main(GOOD, prev)
        self.assertEqual([x['name'] for x in data['items']], ['e/slow', 'a/tool'])

    def test_bad_or_useless_output(self):
        data, _ = self.run_main({**GOOD, 'fit': 9})
        self.assertNotIn('ai', data['items'][0])  # 输出不合格就不带总结，项目照样列出
        data, _ = self.run_main({**GOOD, 'fit': 1})
        self.assertEqual(data['items'], [])  # 模型觉得没用的不推荐，记下来免得明天再总结
        self.assertEqual(sorted(data['skipped']), ['a/tool', 'e/slow'])

    def test_codex_batch_one_call(self):
        """没有 ANTHROPIC_API_KEY 但有 codex：所有候选合并成一次调用，缺的、坏的、1 分的照旧处理"""
        tmp = Path(tempfile.mkdtemp())
        calls = []

        def batch(texts):
            calls.append(list(texts))
            good = {'zh': '中', 'what': '能做事', 'why': '有用', 'setup': '下载就能用', 'how': '下载', 'fit': 4}
            names = list(texts)
            return {names[0]: {**good, 'name': names[0]}, names[1]: {**good, 'name': names[1], 'fit': 1}}
        env = {k: v for k, v in os.environ.items() if k != 'ANTHROPIC_API_KEY'}
        with mock.patch.object(picks, 'gh', fake_gh()), mock.patch.object(picks, 'trending', lambda: {}), \
                mock.patch.object(picks, 'ask_codex_batch', batch), mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(picks.shutil, 'which', lambda c: '/bin/codex' if c == 'codex' else None), \
                mock.patch.object(sys, 'argv', ['picks.py', '--out', str(tmp / 'out.json')]):
            picks.main()
        out = json.loads((tmp / 'out.json').read_text(encoding='utf-8'))
        self.assertEqual(len(calls), 1)
        self.assertEqual(out['engine'], 'codex-cli')
        self.assertEqual(out['note'], '')
        names = calls[0]
        got = {x['name']: x for x in out['items']}
        self.assertEqual(got[names[0]]['ai']['fit'], 4)
        self.assertNotIn(names[1], got)            # 1 分不推荐
        self.assertIn(names[1], out['skipped'])
        self.assertTrue(all('ai' not in got[n] for n in names[2:] if n in got))  # 模型漏掉的照常推荐、没有总结

    def test_trending_parser(self):
        class Resp:
            def __init__(self, body): self.body = body
            def read(self): return self.body.encode()
            def __enter__(self): return self
            def __exit__(self, *a): pass
        with mock.patch.object(picks.urllib.request, 'urlopen', lambda req, timeout: Resp(TRENDING_HTML)):
            self.assertEqual(picks.trending(), {'old/agent-kit': 1000.0})


if __name__ == '__main__':
    unittest.main()
