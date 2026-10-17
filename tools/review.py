#!/usr/bin/env python3
"""任务复盘：在本机分析 Claude Code / Codex 的会话记录，以及 ChatGPT / Claude 的导出记录，生成只给自己看的复盘数据。

用法（在仓库目录下）：
    python3 tools/review.py              # 分析并打开 http://127.0.0.1:8787/#review
    python3 tools/review.py --no-serve   # 只生成数据
    python3 tools/review.py --since 2026-09-01

隐私：
- 只读本机的 ~/.claude/projects、~/.codex/sessions，和“下载”文件夹里 ChatGPT / Claude 的官方导出（--no-exports 关掉）；除了交给 Kimi 的部分，不联网。
- 结果写到 ~/.ai-workbench/review.json（仓库外面），只通过 127.0.0.1 的本机服务给页面读，不会被推送到 GitHub。
- 同一个本机服务也负责“方法库”的读写，存在 ~/.ai-workbench/playbook.json。
- 只保存你发出的消息（截断后）和 Token 计数，不保存 AI 的回复；常见密钥格式会被替换成 [已隐藏]。
"""
import argparse
import datetime as dt
import functools
import http.server
import json
import os
import re
import sys
import webbrowser
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DEFAULT = Path.home() / '.ai-workbench' / 'review.json'
EXCERPT = 600           # 每条消息最多保留多少字
IDLE_GAP = 5 * 60       # 两次活动间隔超过 5 分钟按空闲处理，不计入耗时

# ---------- 返工原因：关键词规则 ----------
# side：model = 模型没做好；user = 表达或背景不足；scope = 需求本身变了；shared = 双方都有可能
RULES = [
    ('missed', '模型遗漏要求', 'model', r'我(之前|刚才|已经|前面)?(说过|提过|讲过)|不是说了|还是没(做|改|有|按|加)|又(没|忘)|又把.{0,10}(改|删)|没有按|你没有|并没有|漏了|忘了(加|改|做)|didn\'t|still (not|no)'),
    ('error', '结果有错误', 'model', r'报错|错误|\bbug\b|不对|失败|error|exception|traceback|不通过|没通过|不太对|不能用|没反应|跑不起来|不工作|崩了|白屏|不生效|显示不出|有问题'),
    ('format', '输出形式不符', 'shared', r'格式|太长|太短|简短点|简洁|啰嗦|详细一点|展开说|用中文|排版|样式不对'),
    ('clarify', '需求表达澄清', 'user', r'我的意思是|意思是说|不是这个意思|我是说|我指的是|准确地说|\bi mean\b'),
    ('context', '补充背景', 'user', r'补充一下|背景是|忘了说|忘记说|需要说明|对了[，,]|其实是'),
    ('added', '新增需求', 'scope', r'再加|另外|还要|还需要|顺便|增加|新增|加一个|加上|\balso\b'),
    ('changed', '目标变化', 'scope', r'改成|换成|不要了|算了|改为|去掉|删掉|删除|不用了|\binstead\b|改一下'),
]
ACK = re.compile(r'^(继续|可以|好的?|好吧|行|ok|okay|yes|是的?|对|没问题|谢谢|thanks|开始吧?|go|嗯|收到|就这样|可以了|很好|不错)[。.!！~～ ]*$', re.I)
RULES_RE = [(k, name, side, re.compile(p, re.I)) for k, name, side, p in RULES]

SECRET = re.compile(r'(sk-[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|xox[abprs]-[A-Za-z0-9\-]{10,}'
                    r'|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----|Bearer\s+[A-Za-z0-9._\-]{20,})')
# 不是人发的消息：斜杠命令输出、系统提醒、后台通知等
NOT_HUMAN = re.compile(r'^\s*(<command-|<local-command|<task-notification|<system-reminder|<bash-|<user-prompt-submit-hook|Caveat:|\[Request interrupted|This session is being continued from a previous conversation)')


def clean(text):
    return SECRET.sub('[已隐藏]', text.strip())


def label_turn(text, index):
    """给一条用户消息打标签；第一条是任务开头，不算返工。"""
    if index == 0:
        return [], []
    t = text.strip()
    if len(t) <= 12 and ACK.match(t):
        return ['ack'], []
    labels, evidence = [], []
    for key, _, _, rx in RULES_RE:
        m = rx.search(t)
        if m:
            labels.append(key)
            evidence.append(m.group(0))
    return labels or ['other'], evidence


def parse_time(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except ValueError:
        return None


def active_minutes(times):
    times = sorted(t for t in times if t)
    total = 0.0
    for a, b in zip(times, times[1:]):
        gap = (b - a).total_seconds()
        if 0 < gap <= IDLE_GAP:
            total += gap
    return round(total / 60, 1)


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in content):
            return None
        parts = [b.get('text', '') for b in content if isinstance(b, dict) and b.get('type') in ('text', 'input_text')]
        return '\n'.join(p for p in parts if p) or None
    return None


# ---------- Claude Code ----------
def read_claude(base, since):
    sessions = {}
    files = list(base.rglob('*.jsonl')) if base.is_dir() else []
    for f in files:
        try:
            lines = f.open(encoding='utf-8', errors='replace')
        except OSError:
            continue
        with lines:
            for line in lines:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                sid = d.get('sessionId')
                if not sid:
                    continue
                s = sessions.setdefault(sid, {'id': f'claude:{sid}', 'provider': 'claude_code', 'cwd': None, 'title': None,
                                              'turns': [], 'times': [], 'usage': {}, 'models': set()})
                if d.get('type') == 'ai-title' and d.get('aiTitle'):
                    s['title'] = d['aiTitle']
                ts = parse_time(d.get('timestamp'))
                if ts:
                    s['times'].append(ts)
                if d.get('cwd') and not s['cwd']:
                    s['cwd'] = d['cwd']
                msg = d.get('message') or {}
                if d.get('type') == 'assistant':
                    # 同一条回复会拆成多行，usage 重复，按 message.id 去重
                    if msg.get('id') and isinstance(msg.get('usage'), dict):
                        s['usage'][msg['id']] = msg['usage']
                    if msg.get('model') and not str(msg['model']).startswith('<'):
                        s['models'].add(msg['model'])
                # 压缩摘要（isCompactSummary）是 AI 写的，不算用户消息
                elif d.get('type') == 'user' and not any(d.get(k) for k in ('isMeta', 'isSidechain', 'isCompactSummary', 'isVisibleInTranscriptOnly')):
                    text = text_of(msg.get('content'))
                    if text and not NOT_HUMAN.match(text):
                        s['turns'].append((ts, text))
    out = []
    for s in sessions.values():
        if not s['turns']:
            continue
        u = list(s['usage'].values())
        tokens = {
            'input': sum(x.get('input_tokens') or 0 for x in u),
            'output': sum(x.get('output_tokens') or 0 for x in u),
            'cache_write': sum(x.get('cache_creation_input_tokens') or 0 for x in u),
            'cache_read': sum(x.get('cache_read_input_tokens') or 0 for x in u),
        }
        tokens['total'] = tokens['input'] + tokens['output'] + tokens['cache_write']
        tokens['measurement'] = 'real' if u else 'unavailable'
        out.append(finish(s, tokens, since))
    return [t for t in out if t], len(files)


# ---------- Codex ----------
def read_codex(base, since):
    out, files = [], (sorted(base.rglob('rollout-*.jsonl')) if base.is_dir() else [])
    for f in files:
        s = {'id': f'codex:{f.stem}', 'provider': 'codex', 'cwd': None, 'title': None, 'turns': [], 'times': [], 'models': set()}
        last_total = None
        fallback_turns = []
        try:
            with f.open(encoding='utf-8', errors='replace') as lines:
                for line in lines:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    ts = parse_time(d.get('timestamp'))
                    if ts:
                        s['times'].append(ts)
                    p = d.get('payload') or {}
                    kind = d.get('type')
                    if kind == 'session_meta':
                        s['cwd'] = p.get('cwd') or s['cwd']
                        if p.get('id'):
                            s['id'] = f"codex:{p['id']}"
                    elif kind == 'turn_context' and p.get('model'):
                        s['models'].add(p['model'])
                    elif kind == 'event_msg' and p.get('type') == 'user_message':
                        text = p.get('message')
                        if isinstance(text, str) and text.strip() and not NOT_HUMAN.match(text):
                            s['turns'].append((ts, text))
                    elif kind == 'event_msg' and p.get('type') == 'token_count':
                        info = p.get('info') or {}
                        if isinstance(info.get('total_token_usage'), dict):
                            last_total = info['total_token_usage']
                    elif kind == 'response_item' and p.get('type') == 'message' and p.get('role') == 'user':
                        # 老版本没有 user_message 事件时退回读这里；跳过注入的环境说明
                        text = text_of(p.get('content'))
                        if text and not text.lstrip().startswith(('<environment_context', '<user_instructions', '# AGENTS.md')):
                            fallback_turns.append((ts, text))
        except OSError:
            continue
        if not s['turns']:
            s['turns'] = fallback_turns
        if not s['turns']:
            continue
        if last_total:
            cached = last_total.get('cached_input_tokens') or 0
            tokens = {'input': max(0, (last_total.get('input_tokens') or 0) - cached), 'output': last_total.get('output_tokens') or 0,
                      'cache_write': 0, 'cache_read': cached}
            tokens['total'] = tokens['input'] + tokens['output']
            tokens['measurement'] = 'real'
        else:
            tokens = {'input': 0, 'output': 0, 'cache_write': 0, 'cache_read': 0, 'total': 0, 'measurement': 'unavailable'}
        t = finish(s, tokens, since)
        if t:
            out.append(t)
    return out, len(files)


# ---------- ChatGPT / Claude 网页和桌面 App：官方“导出数据” ----------
# 两家都没有公开的读取接口，桌面 App 的本地数据也不能用（ChatGPT 加密，Claude 不全），
# 所以读你在设置里导出的压缩包。默认在“下载”文件夹里找，压缩包和解压后的文件夹都认；
# 导出是全部历史，同一家只用最新的那一份。只取你发出的消息，不读 AI 回复，也不复制压缩包。
EXPORT_MAX = 500 * 1024 * 1024
NO_TOKENS = {'input': 0, 'output': 0, 'cache_write': 0, 'cache_read': 0, 'total': 0, 'measurement': 'unavailable'}


def export_kind(conversations):
    first = next((c for c in conversations if isinstance(c, dict)), None) if isinstance(conversations, list) else None
    if not first:
        return None
    if 'mapping' in first:
        return 'chatgpt'
    if 'chat_messages' in first:
        return 'claude_ai'
    return None


def load_export(path):
    """返回 (kind, 对话列表)；不是导出文件就返回 None。只读 conversations*.json，不解压其他文件。"""
    try:
        if path.is_dir():
            files = sorted(path.glob('conversations*.json'))
            raw = [f.read_bytes() for f in files if f.stat().st_size <= EXPORT_MAX]
        else:
            with zipfile.ZipFile(path) as z:
                names = sorted(n for n in z.namelist() if re.fullmatch(r'(?:[^/]+/)?conversations[^/]*\.json', n))
                raw = [z.read(n) for n in names if z.getinfo(n).file_size <= EXPORT_MAX]
        convs = []
        for b in raw:
            d = json.loads(b)
            convs.extend(d if isinstance(d, list) else [])
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
        return None
    kind = export_kind(convs)
    return (kind, convs) if kind else None


def find_exports(dirs):
    """在这些文件夹（和下一层子文件夹）里找导出，每家返回最新的一份 {kind: path}"""
    found = {}
    for base in dirs:
        base = Path(base).expanduser()
        if not base.is_dir():
            continue
        cands = [p for p in base.iterdir() if p.suffix.lower() == '.zip' or (p.is_dir() and any(p.glob('conversations*.json')))]
        for p in sorted(cands, key=lambda p: p.stat().st_mtime, reverse=True):
            if p.suffix.lower() == '.zip' and p.stat().st_size > EXPORT_MAX:
                continue
            r = load_export(p)
            if r and r[0] not in found:
                found[r[0]] = (p, r[1])
    return found


def epoch(x):
    try:
        return dt.datetime.fromtimestamp(float(x), dt.timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def chatgpt_branch(c):
    """沿 current_node 往回走，只取你最后看到的那条分支（重新生成、编辑过的旧分支不算）"""
    mapping = c.get('mapping') or {}
    node, chain, seen = c.get('current_node'), [], set()
    while node and node in mapping and node not in seen:
        seen.add(node)
        chain.append(mapping[node])
        node = mapping[node].get('parent')
    if not chain:  # 没有 current_node 时按时间排序
        chain = sorted(mapping.values(), key=lambda n: ((n.get('message') or {}).get('create_time') or 0), reverse=True)
    return [n.get('message') for n in reversed(chain) if n.get('message')]


def read_chatgpt(convs, since):
    out = []
    for c in convs:
        if not isinstance(c, dict):
            continue
        s = {'id': f"chatgpt:{c.get('conversation_id') or c.get('id') or c.get('create_time')}", 'provider': 'chatgpt',
             'cwd': None, 'title': c.get('title'), 'turns': [], 'times': [], 'models': set()}
        for m in chatgpt_branch(c):
            ts = epoch(m.get('create_time'))
            meta = m.get('metadata') or {}
            if ts:
                s['times'].append(ts)
            if meta.get('model_slug'):
                s['models'].add(meta['model_slug'])
            content = m.get('content') or {}
            # 自定义指令、隐藏的系统消息不算你发的
            if (m.get('author') or {}).get('role') != 'user' or meta.get('is_visually_hidden_from_conversation') \
                    or content.get('content_type') not in ('text', 'multimodal_text'):
                continue
            text = '\n'.join(p for p in content.get('parts') or [] if isinstance(p, str)).strip()
            if text:
                s['turns'].append((ts, text))
        if not s['times']:
            s['times'] = [t for t in (epoch(c.get('create_time')), epoch(c.get('update_time'))) if t]
        t = finish(s, dict(NO_TOKENS), since) if s['turns'] else None
        if t:
            out.append(t)
    return out


def read_claude_ai(convs, since):
    out = []
    for c in convs:
        if not isinstance(c, dict):
            continue
        s = {'id': f"claude_ai:{c.get('uuid')}", 'provider': 'claude_ai', 'cwd': None, 'title': c.get('name') or None,
             'turns': [], 'times': [], 'models': set()}
        for m in c.get('chat_messages') or []:
            ts = parse_time(m.get('created_at'))
            if ts:
                s['times'].append(ts)
            if m.get('sender') != 'human':
                continue
            text = m.get('text') or '\n'.join(b.get('text', '') for b in m.get('content') or [] if isinstance(b, dict) and b.get('type') == 'text')
            if text and text.strip():
                s['turns'].append((ts, text))
        if not s['times']:
            s['times'] = [t for t in (parse_time(c.get('created_at')), parse_time(c.get('updated_at'))) if t]
        t = finish(s, dict(NO_TOKENS), since) if s['turns'] else None
        if t:
            out.append(t)
    return out


def finish(s, tokens, since):
    times = [t for t in s['times'] if t]
    if not times:
        return None
    start, end = min(times), max(times)
    if since and end.date() < since:
        return None
    turns = []
    texts = [(ts, clean(text)) for ts, text in sorted(s['turns'], key=lambda x: x[0] or start)]
    texts = [(ts, text) for ts, text in texts if text]  # 只有空白（比如只贴了图）的消息跳过
    if not texts:
        return None
    for i, (ts, text) in enumerate(texts):
        labels, evidence = label_turn(text, i)
        turns.append({'at': (ts or start).isoformat(), 'text': text[:EXCERPT], 'length': len(text), 'labels': labels, 'evidence': evidence, '_full': text})
    revisions = sum(1 for t in turns[1:] if any(l not in ('ack', 'other') for l in t['labels']))
    first = turns[0]['text']
    return {
        'id': s['id'], 'provider': s['provider'],
        'project': Path(s['cwd']).name if s['cwd'] else '',
        'title': s['title'] or first.splitlines()[0][:60],
        'started_at': start.isoformat(), 'ended_at': end.isoformat(),
        'active_minutes': active_minutes(times),
        'models': sorted(s['models']),
        'turns': turns, 'revisions': revisions, 'tokens': tokens,
    }


# ---------- Kimi CLI：长文本交给 Kimi 归类 ----------
# 默认运行（加 --no-kimi 关掉；没装 kimi 命令时自动跳过）。用 tools/kimi/agent.yaml（不带任何工具）在一个空目录里跑 kimi --quiet，
# 输入只有你发出的消息；结果按“任务 + 内容”缓存，同样的内容不会重复调用。
KIMI_DIR = Path(__file__).resolve().parent / 'kimi'
KIMI_CACHE = Path.home() / '.ai-workbench' / 'kimi-cache.json'
KIMI_LONG = 200          # 有一条消息超过这么多字，或者有规则没认出来的追问，才交给 Kimi
KIMI_TASK_CHARS = 30000  # 每个任务最多发这么多字


def kimi_wanted(t):
    follow = t['turns'][1:]
    return bool(follow) and (any(x['length'] > KIMI_LONG for x in t['turns']) or any('other' in x['labels'] for x in follow))


def kimi_prompt(t, rules):
    keys = '\n'.join(f'- {k}：{n}' for k, n, _, _ in rules) + '\n- ack：只是确认或让模型继续\n- other：以上都不是'
    body, used = [], 0
    for i, turn in enumerate(t['turns']):
        text = turn['_full'][:max(0, KIMI_TASK_CHARS - used)]
        used += len(text)
        body.append(f'[{i}] {text}')
    return (f'下面是一次 AI 协作任务里人发出的消息，按时间排序，[0] 是最初需求。\n\n' + '\n\n'.join(body) +
            f'\n\n请判断 [1] 起每条消息出现的原因，可以有多个，从这些里选：\n{keys}\n\n'
            '只输出 JSON：{"summary": "一句话说明这个任务要做什么", '
            '"lesson": "如果最初需求怎么写可以少返工，一句话；看不出来就留空", '
            '"turns": [{"i": 1, "labels": ["error"], "quote": "这条消息里能说明原因的原文片段"}]}')


def kimi_call(cmd, prompt):
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as empty:
        for attempt in range(2):
            r = subprocess.run([cmd, '--quiet', '--agent-file', str(KIMI_DIR / 'agent.yaml'), '-w', empty],
                               input=prompt, capture_output=True, text=True, timeout=300)
            if r.returncode == 0:
                return r.stdout
            if r.returncode != 75 or attempt:  # 75 = 可重试（限流、服务端错误）
                raise RuntimeError((r.stderr or r.stdout).strip()[-200:] or f'退出码 {r.returncode}')
    return ''


def write_private(path, text):
    """只有自己能读：目录 700，文件一创建就是 600，写完再替换，不会出现半个文件"""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def kimi_parse(raw, t, keys):
    a, b = raw.find('{'), raw.rfind('}')
    d = json.loads(raw[a:b + 1])
    turns = {}
    for x in d.get('turns') or []:
        try:
            i = int(x.get('i'))
        except (TypeError, ValueError):
            continue
        if not 1 <= i < len(t['turns']):
            continue
        labels = [l for l in x.get('labels') or [] if l in keys]
        quote = str(x.get('quote') or '').strip()
        # 没有引用或引用对不上原文，整条归类都不要，回到关键词规则，防止编造
        if labels and quote and quote in t['turns'][i]['_full']:
            turns[str(i)] = {'labels': labels, 'evidence': [quote]}
    return {'source': 'kimi', 'summary': str(d.get('summary') or '')[:200], 'lesson': str(d.get('lesson') or '')[:300], 'turns': turns}


def run_kimi(tasks, args):
    import hashlib
    import shutil
    note = {'analyzed': 0, 'cached': 0, 'failed': 0, 'skipped': 0}
    if not shutil.which(args.kimi_cmd):
        note['error'] = f'找不到 {args.kimi_cmd} 命令，没有调用 Kimi'
        return note
    keys = {k for k, _, _, _ in RULES} | {'ack', 'other'}
    try:
        cache = json.loads(KIMI_CACHE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        cache = {}
    for t in tasks:
        if not kimi_wanted(t):
            continue
        h = hashlib.sha256(json.dumps([x['_full'] for x in t['turns']], ensure_ascii=False).encode()).hexdigest()[:16]
        key = f"{t['id']}:{h}"
        if key in cache:
            t['ai'] = cache[key]
            note['cached'] += 1
            continue
        if note['analyzed'] + note['failed'] >= args.kimi_max:
            note['skipped'] += 1
            continue
        try:
            t['ai'] = cache[key] = kimi_parse(kimi_call(args.kimi_cmd, kimi_prompt(t, RULES)), t, keys)
            note['analyzed'] += 1
            print(f"  Kimi 已分析：{t['title'][:40]}")
        except Exception as e:  # 单个任务失败不影响其他任务
            note['failed'] += 1
            note['error'] = str(e)[:200]
    write_private(KIMI_CACHE, json.dumps(cache, ensure_ascii=False))
    return note


def build(args):
    since = dt.date.fromisoformat(args.since) if args.since else None
    claude, cf = read_claude(Path(args.claude_dir).expanduser(), since)
    codex, xf = read_codex(Path(args.codex_dir).expanduser(), since)
    exports = find_exports(args.export_dirs) if args.export_dirs else {}
    chatgpt = read_chatgpt(exports['chatgpt'][1], since) if 'chatgpt' in exports else []
    claude_ai = read_claude_ai(exports['claude_ai'][1], since) if 'claude_ai' in exports else []
    tasks = sorted(claude + codex + chatgpt + claude_ai, key=lambda t: t['started_at'], reverse=True)
    kimi_note = run_kimi(tasks, args) if args.kimi else None
    for t in tasks:
        for turn in t['turns']:
            turn.pop('_full', None)
    return {
        'generated_at': dt.datetime.now().astimezone().isoformat(timespec='seconds'),
        'since': args.since,
        'rules': [{'key': k, 'name': n, 'side': s} for k, n, s, _ in RULES],
        'sources': {
            'claude_code': {'dir': args.claude_dir, 'files': cf, 'tasks': len(claude)},
            'codex': {'dir': args.codex_dir, 'files': xf, 'tasks': len(codex)},
            **{k: {'file': exports[k][0].name, 'exported_at': dt.datetime.fromtimestamp(exports[k][0].stat().st_mtime).astimezone().isoformat(timespec='seconds'),
                   'tasks': n} if k in exports else None
               for k, n in (('chatgpt', len(chatgpt)), ('claude_ai', len(claude_ai)))},
        },
        'export_dirs': args.export_dirs,
        'kimi': kimi_note,
        'tasks': tasks,
    }


PLAYBOOK = Path.home() / '.ai-workbench' / 'playbook.json'
PLAYBOOK_MAX = 2 * 1024 * 1024
# 只提供页面需要的文件，不把整个仓库（包括 .git）暴露出去
PUBLIC_FILES = {'/', '/index.html', '/data.json'}


class Handler(http.server.SimpleHTTPRequestHandler):
    """静态页面 + 两个只在本机的数据地址：复盘结果（只读）和方法库（可读写）。"""
    review_path = OUT_DEFAULT
    playbook_path = PLAYBOOK

    def send_json(self, body, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def local_host(self):
        # 防 DNS 重绑定：只接受用本机地址访问
        host = (self.headers.get('Host') or '').rsplit(':', 1)[0].strip('[]')
        return host in ('127.0.0.1', 'localhost', '::1')

    def allowed(self):
        if not self.local_host():
            self.send_error(403)
            return False
        if self.path.split('?')[0] not in PUBLIC_FILES | {'/review.local.json', '/playbook.local.json'}:
            self.send_error(404)
            return False
        return True

    def do_HEAD(self):
        if self.allowed():
            super().do_HEAD()

    def do_GET(self):
        if not self.allowed():
            return
        path = self.path.split('?')[0]
        if path in ('/review.local.json', '/playbook.local.json'):
            if path == '/playbook.local.json' and not self.playbook_path.exists():
                self.send_json(b'{"items": []}')
                return
            try:
                body = (self.review_path if path == '/review.local.json' else self.playbook_path).read_bytes()
            except OSError:
                self.send_error(404)
                return
            self.send_json(body)
            return
        super().do_GET()

    def do_PUT(self):
        # 只有页面自己能写：要求自定义请求头和 JSON 类型，别的网站跨域发不过来（预检不会通过）
        if (self.path.split('?')[0] != '/playbook.local.json' or not self.local_host()
                or self.headers.get('X-Workbench') != '1' or 'application/json' not in (self.headers.get('Content-Type') or '')):
            self.send_error(403)
            return
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            self.send_error(400)
            return
        if not 0 < n <= PLAYBOOK_MAX:
            self.send_error(413)
            return
        try:
            data = json.loads(self.rfile.read(n))
            if not isinstance(data, dict) or not isinstance(data.get('items'), list):
                raise ValueError
        except ValueError:
            self.send_error(400)
            return
        write_private(self.playbook_path, json.dumps(data, ensure_ascii=False, indent=1))
        self.send_json(b'{"ok": true}')

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser(description='在本机生成任务复盘数据')
    ap.add_argument('--claude-dir', default='~/.claude/projects')
    ap.add_argument('--codex-dir', default='~/.codex/sessions')
    ap.add_argument('--export-dir', dest='export_dirs', action='append',
                    help='到哪里找 ChatGPT / Claude 的导出压缩包，可以写多次；默认是“下载”文件夹')
    ap.add_argument('--no-exports', action='store_true', help='不读 ChatGPT / Claude 的导出')
    ap.add_argument('--since', help='只分析这天之后的会话，格式 YYYY-MM-DD')
    ap.add_argument('--out', default=str(OUT_DEFAULT))
    ap.add_argument('--port', type=int, default=8787)
    ap.add_argument('--no-serve', action='store_true')
    ap.add_argument('--serve-only', action='store_true', help='不重新分析，只打开页面（用方法库时）')
    ap.add_argument('--no-open', action='store_true')
    ap.add_argument('--no-kimi', dest='kimi', action='store_false', help='不调用 Kimi CLI。默认会把长对话交给本机 Kimi 归类和总结（只发你发出的消息，不发 AI 回复）')
    ap.add_argument('--kimi-max', type=int, default=20, help='这次最多新分析多少个任务（已分析过的走缓存，不重复调用）')
    ap.add_argument('--kimi-cmd', default='kimi', help='Kimi CLI 的命令名或路径')
    args = ap.parse_args()

    args.export_dirs = [] if args.no_exports else (args.export_dirs or ['~/Downloads'])
    out = Path(args.out).expanduser()
    if ROOT in out.resolve().parents:
        sys.exit('为了不把对话记录推送到 GitHub，--out 不能放在仓库目录里。')
    if not args.serve_only:
        data = build(args)
        write_private(out, json.dumps(data, ensure_ascii=False))
        src = data['sources']
        print(f"Claude Code：{src['claude_code']['files']} 个文件，{src['claude_code']['tasks']} 个任务")
        print(f"Codex：{src['codex']['files']} 个文件，{src['codex']['tasks']} 个任务")
        for k, name, where in (('chatgpt', 'ChatGPT', 'ChatGPT 设置 → 数据管控 → 导出数据'), ('claude_ai', 'Claude 对话', 'Claude 设置 → 隐私 → 导出数据')):
            e = src[k]
            print(f"{name}：{e['file']}（{e['exported_at'][:10]}），{e['tasks']} 个对话" if e
                  else f"{name}：在 {'、'.join(args.export_dirs) or '（已关闭）'} 里没找到导出，可以在 {where} 下载后放进去")
        if data['kimi']:
            k = data['kimi']
            print(f"Kimi：新分析 {k['analyzed']} 个任务，缓存 {k['cached']} 个，失败 {k['failed']} 个，跳过 {k['skipped']} 个{('（' + k['error'] + '）') if k.get('error') else ''}")
        print(f'已写入 {out}（只在本机）')
    if args.no_serve:
        return
    Handler.review_path = out
    handler = functools.partial(Handler, directory=str(ROOT))
    url = f"http://127.0.0.1:{args.port}/#{'playbook' if args.serve_only else 'review'}"
    with http.server.ThreadingHTTPServer(('127.0.0.1', args.port), handler) as httpd:
        print(f'打开 {url} 查看，按 Ctrl+C 结束')
        if not args.no_open:
            webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
