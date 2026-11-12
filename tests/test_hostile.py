#!/usr/bin/env python3
"""Hostile-input tests for build.py. Everything in the Community Applications feed and in a template is
written by third parties, so each test feeds one kind of hostile value and checks that it is left out or
made inert, and that one bad entry never stops the build.

usage: python3 -I tests/test_hostile.py [path/to/build.py]
No network: the end-to-end runs use a feed file and a stand-in git that only answers ls-remote."""
import gzip, importlib.util, json, os, subprocess, sys, tempfile

BUILD = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), '..', 'build.py'))
spec = importlib.util.spec_from_file_location('build', BUILD)
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)
FAILS = []


def t(cond, msg):
    print(('ok   ' if cond else 'FAIL ') + msg)
    if not cond:
        FAILS.append(msg)


def guard(msg, fn):
    """A test that crashes counts as failed, and the rest still run."""
    try:
        fn()
    except BaseException as e:  # SystemExit included
        t(False, f'{msg} (raised {type(e).__name__}: {str(e)[:120]})')


RAW = 'https://raw.githubusercontent.com/'
SHA40 = 'a' * 40


def app(name='app', url=RAW + 'own/repo/main/t/app.xml', repo="Own's Repository", image='own/app:latest', **kw):
    a = {'Name': name, 'TemplateURL': url, 'Repo': repo, 'Repository': image, 'Support': 'https://forums.unraid.net/x',
         'Project': 'https://example.com/', 'Registry': 'https://hub.docker.com/r/own/app', 'FirstSeen': 1600000000}
    a.update(kw)
    return a


FEED_TS = 1790000000

# --- feed_apps: one hostile value per entry --------------------------------------------------------------------
def feed_tests():
    hostile = [
        app('js', url='javascript:alert(1)//' + RAW + 'o/r/main/a.xml'),
        app('data', url='data:text/html,<script>alert(1)</script>'),
        app('vb', url='vbscript:msgbox(1)'),
        app('quote', url=RAW + 'own/repo/main/"><svg onload=alert(1)>.xml'),
        app('jslink', url=RAW + 'own/repo/main/jslink.xml', Support='javascript:alert(document.cookie)', Project=' JavaScript:alert(1)'),
        app('<script>alert(1)</script>', url=RAW + 'own/repo/main/script.xml'),
        app('ctrl\x00\x07‮name ', url=RAW + 'own/repo/main/ctrl.xml'),
        app('surrogate\ud800', url=RAW + 'own/repo/main/sur.xml'),
        app('N' * 100000, url=RAW + 'own/repo/main/huge.xml'),
        app('badimage', url=RAW + 'own/repo/main/img.xml', image='"evil image" <x>'),
        app('typename', url=RAW + 'own/repo/main/types.xml', FirstSeen='9' * 40, Registry=['x'], Project={'a': 1}),
        app({'nested': 'dict'}, url=RAW + 'own/repo/main/dict.xml'),
        app('userinfo', url='https://user:pass@raw.githubusercontent.com/own/repo/main/u.xml'),
        'not an object',
        None,
        ['a', 'list'],
    ]
    apps = B.feed_apps({'applist': hostile + [app('good', url=RAW + 'own/repo/main/good.xml')]})
    names = {r['name'] for r in apps.values()}
    keys = ' '.join(apps)
    t('good' in names, 'a good entry next to hostile ones is kept')
    t('javascript:' not in keys.lower() and 'data:' not in keys and 'vbscript:' not in keys, 'javascript:, data: and vbscript: addresses are refused')
    t('"' not in keys and '<' not in keys, 'an address with quotes or angle brackets is refused')
    j = [r for r in apps.values() if r['name'] == 'jslink']
    t(len(j) == 1 and j[0]['support'] == '' and j[0]['project'] == '', 'a javascript: support or project link is emptied, the app kept')
    c = [r for r in apps.values() if r['name'].startswith('ctrl')]
    t(len(c) == 1 and c[0]['name'] == 'ctrlname', 'control, bidi and separator characters are removed from a name')
    t(not any('\ud800' in r['name'] for r in apps.values()), 'a lone surrogate is removed from a name')
    t(not any(len(r['name']) > 200 for r in apps.values()), 'a 100000-character name is refused')
    t(not any(r['name'] == 'badimage' for r in apps.values()), 'an invalid image name is refused')
    ty = [r for r in apps.values() if r['name'] == 'typename']
    t(len(ty) == 1 and ty[0]['registry'] == '' and ty[0]['project'] == '' and ty[0].get('_first') is None,
      'wrong types (list, object, 40-digit FirstSeen) give empty references')
    t(not any('nested' in r['name'] for r in apps.values()), 'a Name that is an object is refused')
    t(not any(r['name'] == 'userinfo' for r in apps.values()), 'an address with user info is refused')
    s = [r for r in apps.values() if r['name'] == '<script>alert(1)</script>']
    t(len(s) == 1, 'a name with markup is kept as text (it is made inert in the output, tested below)')


def target_tests():
    cat = {
        'a': {'kind': 'container', 'template_url': RAW + 'own/repo/main/../../../etc/passwd'},
        'b': {'kind': 'container', 'template_url': RAW + '-upload-pack=touch%20x/repo/main/a.xml'},
        'c': {'kind': 'container', 'template_url': RAW + 'own/../main/a.xml'},
        'd': {'kind': 'container', 'template_url': RAW + 'own/repo/main/dir\\..\\a.xml'},
        'e': {'kind': 'container', 'template_url': RAW + 'own/repo/main/:(glob)**'},
        'f': {'kind': 'container', 'template_url': RAW + 'own/repo/main/ok.xml'},
    }
    got = B.catalog_targets(cat)
    flat = {f'{o}/{r}/{p}' for (o, r), ps in got.items() for p in ps}
    t(flat <= {'own/repo/main/ok.xml', 'own/repo/main/:(glob)**'}, 'traversal paths, an owner starting with - and backslashes are never cloned or looked up')
    t('own/repo/main/ok.xml' in flat, 'a normal template address is still a target')
    t(B.ENV.get('GIT_LITERAL_PATHSPECS') == '1', 'git treats template paths literally (no pathspec magic)')


def parse_tests():
    many = '<Container><Name>x</Name>' + ''.join(f'<Config Type="Variable" Target="V{i}"/>' for i in range(3000)) + '</Container>'
    t(B.parse(many) is None, 'a template with 3000 fields is refused')
    long = f'<Container><Name>x</Name><Config Type="Path" Target="/{"a" * 10000}"/></Container>'
    t(B.parse(long) is None, 'a template with a 10000-character target is refused')
    nl = '<Container><Name>x</Name><Config Type="Variable" Target="A&#10;B"/></Container>'
    t(B.parse(nl) is None, 'a field key with a newline is refused')
    ok = '<Container><Name>x</Name><Config Type="Variable" Target="&lt;script&gt;"/></Container>'
    t(B.parse(ok) is not None, 'markup in a target stays a plain key (escaped in the output)')
    ep = '<Container><Name>x</Name><ExtraParams>-e X_' + 'A' * 600 + '_TOKEN=1</ExtraParams></Container>'
    t(B.parse(ep) is None, 'an ExtraParams secret key over the length limit is refused')


def feed_size_test():
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, 'feed.json')
        json.dump({'applist': [app('x' + str(i)) for i in range(50)], 'last_updated_timestamp': FEED_TS}, open(f, 'w'))
        old = getattr(B, 'LIMITS', None)
        if old is not None:
            B.LIMITS = dict(old, feed_bytes=1000)
        try:
            B.load_feed(f)
            t(False, 'a feed over the size limit is refused before parsing')
        except SystemExit:
            t(True, 'a feed over the size limit is refused before parsing')
        finally:
            if old is not None:
                B.LIMITS = old


# --- end to end: main() with a feed file, a saved catalog and parts, and a stand-in git --------------------------
FAKE_GIT = f'''#!/bin/sh
# stand-in git: ls-remote answers one branch; anything else fails (no network, no clone)
case "$1" in ls-remote) printf '%s\\trefs/heads/main\\n' {SHA40}; exit 0;; esac
exit 1
'''


def run_build(d, feed, catalog=None, parts=None, env=None):
    os.makedirs(os.path.join(d, 'state', 'repos'), exist_ok=True)
    os.makedirs(os.path.join(d, 'bin'), exist_ok=True)
    g = os.path.join(d, 'bin', 'git')
    open(g, 'w').write(FAKE_GIT)
    os.chmod(g, 0o755)
    json.dump(feed, open(os.path.join(d, 'feed.json'), 'w'))
    if catalog is not None:
        json.dump(catalog, open(os.path.join(d, 'state', 'catalog.json'), 'w'))
    for n, p in (parts or {}).items():
        json.dump(p, open(os.path.join(d, 'state', 'repos', n), 'w'))
    e = dict(os.environ, PATH=os.path.join(d, 'bin') + ':' + os.environ.get('PATH', ''), **(env or {}))
    e.pop('INDEX_GIT_TOKEN', None)
    return subprocess.run([sys.executable, '-I', BUILD, '--feed', os.path.join(d, 'feed.json'), '--state', os.path.join(d, 'state'),
                           '--out', os.path.join(d, 'out'), '--work', os.path.join(d, 'work'), '--changes', os.path.join(d, 'changes'), '--jobs', '2'],
                          capture_output=True, text=True, env=e, timeout=300)


def end_to_end():
    with tempfile.TemporaryDirectory() as d:
        good = ['path:/config', 'variable:PUID']
        sid = B.set_id(good)
        bad_set = ['variable:<img src=x onerror=alert(1)>', 'x' * 5000]
        part = {'v': B.PART_VERSION, 'heads': {'main': SHA40}, 'paths': {'main': ['t/app.xml', 't/two.xml']},
                'sets': {sid: good, 'NOT-AN-ID': good, 'bbbbbbbbbb': bad_set},
                'secrets': {'cccccccccc': {'m': ['variable:\x00evil'], 's': [], 'x': []}},
                'files': {'main/t/app.xml': [[1600000000, 'abcdef1', sid, None], ['when', 'zz', sid, None], [1, 'abcdef2', 'bbbbbbbbbb', None],
                                             [2, 'abcdef3', sid, 'cccccccccc']],
                          'main/t/two\nxml': [[1600000000, 'abcdef1', sid, None]]}}
        # first run: baseline catalog from a feed with hostile entries next to good ones
        feed = {'applist': [app('good', url=RAW + 'own/repo/main/t/app.xml'), app('<script>alert(1)</script>', url=RAW + 'own/repo/main/t/two.xml'),
                            app('surrogate\ud800', url=RAW + 'own/repo/main/t/sur.xml'), 'junk', app('js', url='javascript:alert(1)')],
                'last_updated_timestamp': FEED_TS}
        r = run_build(d, feed, parts={'own__repo.json': part})
        t(r.returncode == 0, 'one bad entry never stops the build' + ('' if r.returncode == 0 else ': ' + r.stderr.strip()[-300:]))
        raw = open(os.path.join(d, 'out', 'index.json'), 'rb').read() if os.path.exists(os.path.join(d, 'out', 'index.json')) else b''
        t(raw != b'' and b'<' not in raw and b'>' not in raw, 'index.json has no raw < or > (written as \\u003c / \\u003e)')
        t(raw != b'' and b'\\ud800' not in raw.lower(), 'index.json has no lone surrogate escape (PHP json_decode refuses the whole file on one)')
        ix = json.loads(raw or b'{}')
        names = sorted(c.get('name') for c in ix.get('apps', {}).values())
        t('<script>alert(1)</script>' in names and 'good' in names, 'the markup name round-trips as text')
        t(not any('javascript' in k for k in ix.get('apps', {})), 'the javascript: app is not in the index')
        t(ix.get('feed_updated') == FEED_TS, 'feed_updated is the checked feed time')
        t('NOT-AN-ID' not in ix.get('sets', {}) and 'bbbbbbbbbb' not in ix.get('sets', {}) and 'cccccccccc' not in ix.get('secrets', {}),
          'a saved part\'s bad set ids, over-long keys and control characters are left out')
        f = ix.get('files', {}).get('own/repo/main/t/app.xml', [])
        t(f == [[1600000000, 'abcdef1', sid, None]], 'only well-formed version entries that point at known sets remain')
        t(not any('\n' in k for k in ix.get('files', {})), 'a file key with a newline is left out')
        sm = json.load(open(os.path.join(d, 'out', 'summary.json'))) if os.path.exists(os.path.join(d, 'out', 'summary.json')) else {}
        t(bool(sm.get('rejected')), 'what was left out is counted in summary.json and the build log')

        # second run: a newer feed where the markup app changed: the changelog line stays inert too
        feed2 = dict(feed, last_updated_timestamp=FEED_TS + 86400)
        feed2['applist'] = [app('good', url=RAW + 'own/repo/main/t/app.xml', Support='https://x/<b>'),
                            app('<script>alert(2)</script>', url=RAW + 'own/repo/main/t/two.xml')]
        r = run_build(d, feed2)
        ch = os.path.join(d, 'changes', '2026.jsonl')
        text = open(ch, 'rb').read() if os.path.exists(ch) else b''
        t(r.returncode == 0 and text != b'' and b'<' not in text and b'>' not in text, 'changes/*.jsonl has no raw < or >')

    with tempfile.TemporaryDirectory() as d:
        r = run_build(d, {'applist': [app('good')], 'last_updated_timestamp': 4102444800})
        t(r.returncode != 0 and not os.path.exists(os.path.join(d, 'state', 'catalog.json')),
          'a feed timestamp in the future is refused (it would freeze the catalog)')
    with tempfile.TemporaryDirectory() as d:
        r = run_build(d, {'applist': {'not': 'a list'}, 'last_updated_timestamp': FEED_TS})
        t(r.returncode != 0 and not os.path.exists(os.path.join(d, 'out', 'index.json')), 'a feed with no app list is refused, no index written')
    with tempfile.TemporaryDirectory() as d:
        cat = {'v': B.CATALOG_VERSION, 'updated': FEED_TS - 1,
               'apps': {f'{RAW}own/repo/main/a{i}.xml#R': {'kind': 'container', 'name': f'a{i}', 'template_url': f'{RAW}own/repo/main/a{i}.xml',
                                                           'repository': 'own/app', 'ca_repo': 'R', 'github': 'own/repo', 'flags': [],
                                                           'first_seen': 1, 'last_seen': 1} for i in range(600)}}
        r = run_build(d, {'applist': [app('only')], 'last_updated_timestamp': FEED_TS}, catalog=cat)
        after = json.load(open(os.path.join(d, 'state', 'catalog.json')))
        t(r.returncode != 0 and not any(c.get('removed') for c in after['apps'].values()),
          'a truncated feed (1 app against 600 listed) is refused instead of marking the catalog removed')
    with tempfile.TemporaryDirectory() as d:
        cat = {'v': B.CATALOG_VERSION, 'updated': FEED_TS + 10,
               'apps': {'javascript:alert(1)#R': {'kind': 'container', 'name': 'old', 'template_url': 'javascript:alert(1)', 'repository': 'x',
                                                   'ca_repo': 'R', 'github': '', 'flags': [], 'first_seen': 1, 'last_seen': 1},
                        f'{RAW}own/repo/main/a.xml#R': {'kind': 'container', 'name': 'fine', 'template_url': f'{RAW}own/repo/main/a.xml',
                                                         'repository': 'own/app', 'ca_repo': 'R', 'github': 'own/repo', 'flags': [],
                                                         'support': 'javascript:alert(1)', 'first_seen': 1, 'last_seen': 1}}}
        r = run_build(d, {'applist': [app('x')], 'last_updated_timestamp': FEED_TS}, catalog=cat)
        ix = json.load(open(os.path.join(d, 'out', 'index.json'))) if os.path.exists(os.path.join(d, 'out', 'index.json')) else {'apps': {'?': 1}}
        t(r.returncode == 0 and list(ix['apps']) == [f'{RAW}own/repo/main/a.xml#R'] and ix['apps'][f'{RAW}own/repo/main/a.xml#R'].get('support') == '',
          'a bad entry saved by an older build is checked again at output: left out, or its bad link emptied')


for name, fn in [('feed', feed_tests), ('targets', target_tests), ('parse', parse_tests), ('feed size', feed_size_test), ('end to end', end_to_end)]:
    guard(name, fn)
print(f'{len(FAILS)} failed' if FAILS else 'all passed')
sys.exit(1 if FAILS else 0)
