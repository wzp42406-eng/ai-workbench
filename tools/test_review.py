"""tools/review.py 的测试：python3 -m unittest discover tools"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review  # noqa: E402


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows) + '\n', encoding='utf-8')


def user(sid, ts, content, **kw):
    return {'type': 'user', 'sessionId': sid, 'timestamp': ts, 'cwd': '/u/p/app', 'message': {'role': 'user', 'content': content}, **kw}


def assistant(sid, ts, mid, out):
    return {'type': 'assistant', 'sessionId': sid, 'timestamp': ts,
            'message': {'id': mid, 'model': 'claude-x', 'usage': {'input_tokens': 10, 'output_tokens': out, 'cache_creation_input_tokens': 100, 'cache_read_input_tokens': 1000}}}


class LabelTest(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(review.label_turn('帮我做一个网站', 0), ([], []))
        self.assertEqual(review.label_turn('好的', 1)[0], ['ack'])
        self.assertIn('error', review.label_turn('测试还是不通过', 1)[0])
        self.assertIn('missed', review.label_turn('我之前说过要用中文，你还是没改', 1)[0])
        self.assertIn('clarify', review.label_turn('我的意思是只统计工作日', 1)[0])
        self.assertEqual(review.label_turn('这个地方我想想', 1)[0], ['other'])

    def test_secrets_hidden(self):
        self.assertNotIn('sk-', review.clean('key 是 sk-abcdefghijklmnopqrstuvwx'))


class ParseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        sid = 's1'
        write(self.tmp / 'claude' / 'p' / 's1.jsonl', [
            user(sid, '2026-10-15T10:00:00Z', '帮我做一个记账小程序'),
            assistant(sid, '2026-10-15T10:01:00Z', 'm1', 500),
            assistant(sid, '2026-10-15T10:01:00Z', 'm1', 500),  # 同一条回复拆成多行，不能重复计
            {'type': 'user', 'sessionId': sid, 'timestamp': '2026-10-15T10:01:30Z', 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'x', 'content': 'ok'}]}},
            user(sid, '2026-10-15T10:02:00Z', '<command-name>/clear</command-name>'),
            user(sid, '2026-10-15T10:03:00Z', '页面报错了'),
            user(sid, '2026-10-15T10:03:10Z', '系统提示', isMeta=True),
            user(sid, '2026-10-15T10:03:20Z', 'This session is being continued from a previous conversation. Summary: ...', isCompactSummary=True),
            user(sid, '2026-10-15T10:03:30Z', '   '),
            user(sid, '2026-10-15T11:00:00Z', '好的'),
        ])
        write(self.tmp / 'claude' / 'p' / 's2.jsonl', [user('s2', '2026-10-15T09:00:00Z', [{'type': 'text', 'text': '  '}])])
        write(self.tmp / 'codex' / '2026' / '10' / '15' / 'rollout-x.jsonl', [
            {'timestamp': '2026-10-15T12:00:00Z', 'type': 'session_meta', 'payload': {'id': 'abc', 'cwd': '/u/p/wb'}},
            {'timestamp': '2026-10-15T12:00:01Z', 'type': 'event_msg', 'payload': {'type': 'user_message', 'message': '修复趋势图'}},
            {'timestamp': '2026-10-15T12:03:00Z', 'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'total_token_usage': {'input_tokens': 500, 'cached_input_tokens': 300, 'output_tokens': 40}}}},
        ])
        self.args = SimpleNamespace(claude_dir=str(self.tmp / 'claude'), codex_dir=str(self.tmp / 'codex'), since=None, kimi=False, export_dirs=[])

    def test_build(self):
        data = review.build(self.args)
        tasks = {t['id']: t for t in data['tasks']}
        c = tasks['claude:s1']
        self.assertEqual([t['text'] for t in c['turns']], ['帮我做一个记账小程序', '页面报错了', '好的'])
        self.assertEqual(c['revisions'], 1)
        self.assertEqual(c['tokens']['output'], 500)
        self.assertEqual(c['tokens']['total'], 10 + 500 + 100)
        self.assertNotIn('claude:s2', tasks)  # 只有空白的会话不算任务，也不报错
        self.assertEqual(c['active_minutes'], 3.5)  # 10:00 到 10:03:10 计入，之后到 11:00 的空闲不算
        self.assertNotIn('_full', c['turns'][0])
        x = tasks['codex:abc']
        self.assertEqual(x['tokens']['total'], 200 + 40)
        self.assertEqual(x['tokens']['cache_read'], 300)

    def test_since(self):
        self.args.since = '2026-10-16'
        self.assertEqual(review.build(self.args)['tasks'], [])


def chatgpt_conv():
    def node(i, parent, role, text, t, **meta):
        return {'id': i, 'parent': parent, 'message': {'author': {'role': role}, 'create_time': t, 'metadata': meta,
                                                      'content': {'content_type': 'text', 'parts': [text]}}}
    nodes = [
        {'id': 'root', 'parent': None, 'message': None},
        node('sys', 'root', 'user', '我的自定义指令', 1760518800, is_visually_hidden_from_conversation=True),
        node('u1', 'sys', 'user', '帮我写一封请假邮件', 1760518800),
        node('a1', 'u1', 'assistant', 'AI 的回复不应该被读', 1760518860, model_slug='gpt-x'),
        node('old', 'a1', 'user', '编辑前的旧问题', 1760518900),
        node('u2', 'a1', 'user', '语气再正式一点', 1760518920),
    ]
    return {'id': 'c1', 'title': '请假邮件', 'create_time': 1760518800, 'current_node': 'u2', 'mapping': {n['id']: n for n in nodes}}


def claude_conv():
    return {'uuid': 'k1', 'name': '读书笔记', 'created_at': '2026-10-15T08:00:00Z', 'chat_messages': [
        {'sender': 'human', 'created_at': '2026-10-15T08:00:00Z', 'text': '总结这本书', 'content': []},
        {'sender': 'assistant', 'created_at': '2026-10-15T08:00:30Z', 'text': 'AI 回复'},
        {'sender': 'human', 'created_at': '2026-10-15T08:02:00Z', 'text': '', 'content': [{'type': 'text', 'text': '我的意思是只要三点'}]},
    ]}


class ExportTest(unittest.TestCase):
    def setUp(self):
        import zipfile
        self.tmp = Path(tempfile.mkdtemp())
        dl = self.tmp / 'Downloads'
        dl.mkdir()
        with zipfile.ZipFile(dl / 'chatgpt-export.zip', 'w') as z:
            z.writestr('conversations.json', json.dumps([chatgpt_conv()]))
            z.writestr('chat.html', '<html>')
        (dl / 'data-2026-10-15').mkdir()  # 解压后的 Claude 导出
        (dl / 'data-2026-10-15' / 'conversations.json').write_text(json.dumps([claude_conv()]), encoding='utf-8')
        (dl / 'other.zip').write_bytes(b'not a zip')
        self.args = SimpleNamespace(claude_dir=str(self.tmp / 'none'), codex_dir=str(self.tmp / 'none'), since=None, kimi=False,
                                    export_dirs=[str(dl)])

    def test_reads_only_user_messages_on_current_branch(self):
        data = review.build(self.args)
        tasks = {t['id']: t for t in data['tasks']}
        g = tasks['chatgpt:c1']
        self.assertEqual([t['text'] for t in g['turns']], ['帮我写一封请假邮件', '语气再正式一点'])
        self.assertEqual(g['title'], '请假邮件')
        self.assertEqual(g['tokens']['measurement'], 'unavailable')
        c = tasks['claude_ai:k1']
        self.assertEqual([t['text'] for t in c['turns']], ['总结这本书', '我的意思是只要三点'])
        self.assertIn('clarify', c['turns'][1]['labels'])
        self.assertEqual(data['sources']['chatgpt']['tasks'], 1)
        self.assertEqual(data['sources']['claude_ai']['file'], 'data-2026-10-15')
        dumped = json.dumps(data, ensure_ascii=False)
        for text in ('AI 的回复不应该被读', 'AI 回复', '编辑前的旧问题', '我的自定义指令'):  # AI 回复、旧分支、自定义指令都不读
            self.assertNotIn(text, dumped)

    def test_no_exports(self):
        self.args.export_dirs = []
        data = review.build(self.args)
        self.assertEqual(data['tasks'], [])
        self.assertIsNone(data['sources']['chatgpt'])


class KimiParseTest(unittest.TestCase):
    def test_drops_made_up_quotes_and_unknown_labels(self):
        t = {'turns': [{'_full': '做个网站'}, {'_full': '页面报错了'}, {'_full': '再加个按钮'}]}
        raw = '说明\n{"summary": "s", "lesson": "l", "turns": [{"i": 1, "labels": ["error"], "quote": "报错"}, {"i": 2, "labels": ["added", "bogus"], "quote": "编的"}, {"i": 9, "labels": ["error"]}]}'
        r = review.kimi_parse(raw, t, {'error', 'added'})
        self.assertEqual(r['turns'], {'1': {'labels': ['error'], 'evidence': ['报错']}})  # 引用对不上原文的整条丢掉


if __name__ == '__main__':
    unittest.main()
