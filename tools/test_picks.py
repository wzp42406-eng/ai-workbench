"""tools/picks.py 的测试：python3 -m unittest discover tools"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
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


SEARCH = [repo('a/tool', stars=500), repo('b/awesome-llm'), repo('c/nodesc', desc=None), repo('d/stale', pushed=181),
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


def fake_gh(search=SEARCH, extra=None, work=None, readmes=None):
    extra = extra or {}
    readmes = readmes or {}

    def gh(path, accept=''):
        if path.startswith('/search/'):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query).get('q', [''])[0]
            if 'pushed:>' in q:
                return {'items': work or []}
            return {'items': search}
        if path.startswith('/repos/') and path.endswith('/readme'):
            return readmes.get(path.split('/')[2] + '/' + path.split('/')[3], 'README 内容' + 'x' * 300)
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
        self.assertIn('"pushed_days":', calls[0])
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
        self.assertTrue(all(n not in got for n in names[2:]))  # 批量成功时只推荐总结有效且 fit >= 2 的项目

    def test_batch_ranks_top_five_by_fit_then_growth(self):
        repos = [repo(f'owner/ai-tool-{i}', stars=800-i*50) for i in range(8)]
        fits = [2, 4, 5, 3, 5, 1, 4, 5]
        tmp = Path(tempfile.mkdtemp())
        def batch(texts):
            return {name: {**GOOD, 'fit': fits[i]} for i, name in enumerate(texts)}
        env = {k: v for k, v in os.environ.items() if k != 'ANTHROPIC_API_KEY'}
        with mock.patch.object(picks, 'gh', fake_gh(search=repos)), mock.patch.object(picks, 'trending', lambda: {}), \
                mock.patch.object(picks, 'ask_codex_batch', batch), mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(picks.shutil, 'which', lambda c: '/bin/codex' if c == 'codex' else None), \
                mock.patch.object(sys, 'argv', ['picks.py', '--out', str(tmp / 'out.json')]):
            picks.main()
        out = json.loads((tmp / 'out.json').read_text(encoding='utf-8'))
        self.assertEqual([x['name'] for x in out['items']],
                         ['owner/ai-tool-2', 'owner/ai-tool-4', 'owner/ai-tool-7', 'owner/ai-tool-1', 'owner/ai-tool-6'])
        self.assertIn('owner/ai-tool-5', out['skipped'])

    def test_batch_failure_falls_back_to_growth_without_ai(self):
        repos = [repo(f'owner/ai-tool-{i}', stars=800-i*50) for i in range(8)]
        tmp = Path(tempfile.mkdtemp())
        env = {k: v for k, v in os.environ.items() if k != 'ANTHROPIC_API_KEY'}
        with mock.patch.object(picks, 'gh', fake_gh(search=repos)), mock.patch.object(picks, 'trending', lambda: {}), \
                mock.patch.object(picks, 'ask_codex_batch', side_effect=RuntimeError('failed')), mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(picks.shutil, 'which', lambda c: '/bin/codex' if c == 'codex' else None), \
                mock.patch.object(sys, 'argv', ['picks.py', '--out', str(tmp / 'out.json')]):
            picks.main()
        out = json.loads((tmp / 'out.json').read_text(encoding='utf-8'))
        self.assertEqual([x['name'] for x in out['items']], [f'owner/ai-tool-{i}' for i in range(5)])
        self.assertTrue(all('ai' not in x for x in out['items']))

    def test_trending_parser(self):
        class Resp:
            def __init__(self, body): self.body = body
            def read(self): return self.body.encode()
            def __enter__(self): return self
            def __exit__(self, *a): pass
        with mock.patch.object(picks.urllib.request, 'urlopen', lambda req, timeout: Resp(TRENDING_HTML)):
            self.assertEqual(picks.trending(), {'old/agent-kit': 1000.0})

    def test_180_day_cutoff_and_archived(self):
        repos = [repo('a/fresh', pushed=179), repo('b/stale', pushed=181),
                 repo('c/archived', archived=True)]
        data, _ = self.run_main(GOOD, gh=fake_gh(search=repos))
        self.assertEqual([x['name'] for x in data['items']], ['a/fresh'])
        self.assertEqual(data['items'][0]['quality']['pushed_days'], 179)

    def test_short_readme_needs_homepage(self):
        repos = [repo('a/short'), repo('b/home', homepage='https://example.com')]
        data, calls = self.run_main(GOOD, gh=fake_gh(search=repos, readmes={'a/short': 'short', 'b/home': 'short'}))
        self.assertEqual([x['name'] for x in data['items']], ['b/home'])
        self.assertEqual(len(calls), 1)

    def test_license_notes(self):
        cases = {'AGPL-3.0': 'AGPL：商用需开源', 'GPL-3.0': 'GPL：商用需开源',
                 'LGPL-2.1': 'LGPL：商用需检查链接与分发义务', 'SSPL-1.0': 'SSPL：商用需检查服务端开源义务',
                 '': '未写许可证：使用前确认授权', 'MIT': '', 'Apache-2.0': '', 'BSD-3-Clause': ''}
        for license_id, expected in cases.items():
            with self.subTest(license_id=license_id):
                self.assertEqual(picks.license_note(license_id), expected)

    def test_work_quota_with_eight_candidates(self):
        ai = [repo(f'ai/agent-{i}', stars=800-i*20) for i in range(5)]
        work = [repo(f'work/bi-{i}', desc='BI 数据分析工具', stars=900-i*10) for i in range(3)]
        data, _ = self.run_main(GOOD, gh=fake_gh(search=ai, work=work))
        self.assertEqual(len(data['items']), 5)
        self.assertEqual(sum(x.get('track') == 'work' for x in data['items']), 2)
        self.assertTrue(all('quality' in x for x in data['items']))
        low_work = [repo(f'work/bi-{i}', desc='BI 数据分析工具', stars=300-i*10) for i in range(3)]
        data, _ = self.run_main(GOOD, gh=fake_gh(search=ai, work=low_work))
        self.assertEqual(sum(x.get('track') == 'work' for x in data['items']), 1)


if __name__ == '__main__':
    unittest.main()
