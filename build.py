#!/usr/bin/env python3
"""Build the Unraid App Chronicle index from the Community Applications feed.

For every template file the feed points at, record each distinct field set the file has had in its
git history. Only field identity is kept (type, target, port protocol); no defaults, descriptions or
anything else. See FORMAT.md.

Also keeps a catalog of every container app and plugin the feed has listed, removed ones included, with
its last known references, and a changelog of what was added, removed or changed (state/catalog.json, changes/YYYY.jsonl).

usage: build.py [--feed FILE|URL] [--state DIR] [--out DIR] [--changes DIR] [--jobs N] [--limit N]
"""
import argparse, gzip, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

FORMAT = 3
CATALOG_VERSION = 1
PART_VERSION = 3  # saved repository parts from an older build are rebuilt
FEED_URLS = ['https://ca.unraid.net/assets/feed/applicationFeed.json',
             'https://assets.ca.unraid.net/feed/applicationFeed.json',
             'https://raw.githubusercontent.com/Squidly271/AppFeed/master/applicationFeed.json']
RAW = re.compile(r'^https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/(.+)$')
# GIT_NO_LAZY_FETCH: a blob left out by the clone filter stays missing instead of being fetched one by one.
# GIT_LITERAL_PATHSPECS: a template path from the feed is a file name, never pathspec magic such as ':(glob)'.
ENV = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='true', GIT_NO_LAZY_FETCH='1', GIT_LITERAL_PATHSPECS='1')
MAX_BLOB = 1024 * 1024  # a template larger than this is skipped
# GitHub limits anonymous git over HTTPS (HTTP 401 after a few hundred repositories). With a token in
# INDEX_GIT_TOKEN (in Actions: the job's GITHUB_TOKEN) every git call sends it as a header, passed
# through the environment so it never appears in a command line. The empty value first clears any
# header already configured for github.com (actions/checkout sets one), which GitHub would otherwise
# refuse as a duplicate Authorization header.
if os.environ.get('INDEX_GIT_TOKEN'):
    import base64
    _auth = base64.b64encode(f"x-access-token:{os.environ['INDEX_GIT_TOKEN']}".encode()).decode()
    ENV.update(GIT_CONFIG_COUNT='2',
               GIT_CONFIG_KEY_0='http.https://github.com/.extraheader', GIT_CONFIG_VALUE_0='',
               GIT_CONFIG_KEY_1='http.https://github.com/.extraheader',
               GIT_CONFIG_VALUE_1=f'Authorization: basic {_auth}')
    ENV.pop('INDEX_GIT_TOKEN', None)


def field_key(type_, target, name, mode):
    """Same key as Chronostasis cs_field_key(): type in lower case, Disabled: prefix dropped, path without a
    trailing slash, port with its protocol, and the Name when the Target is empty."""
    t = type_.strip()
    if t.lower().startswith('disabled:'):
        t = t[9:]
    t = t.lower()
    tg = target.strip()
    if t == 'path' and len(tg) > 1:
        tg = tg.rstrip('/')
    if t == 'port':
        return f"port:{tg}/{(mode.strip().lower() or 'tcp')}"
    if tg == '':
        return f"{t}:name:{name.strip()}"
    return f"{t}:{tg}"


# Secret-like field names. Names are split into upper-case words (FOO_BAR, fooBar, "Foo bar").
SECRET_WORDS = {'PASSWORD', 'PASSWD', 'PASS', 'PWD', 'PASSPHRASE', 'SECRET', 'SECRETS', 'TOKEN', 'TOKENS', 'APIKEY',
                'CREDENTIAL', 'CREDENTIALS', 'CREDS', 'OTP', 'TOTP', 'WEBHOOK', 'DSN', 'PSK', 'PRIVATE'}
# KEY counts only after one of these words (API_KEY, SECRET_KEY, LICENSE KEY, ...), never alone or after PUBLIC.
KEY_AFTER = {'API', 'ACCESS', 'SECRET', 'PRIVATE', 'LICENSE', 'LICENCE', 'AUTH', 'ENCRYPTION', 'CRYPT', 'SIGNING',
             'APP', 'CLIENT', 'MASTER', 'SESSION', 'JWT', 'HMAC', 'WG', 'WIREGUARD', 'ENC', 'SHARED', 'PRODUCT', 'SERIAL'}
# A name that also has one of these words describes the secret, it does not hold one.
NOT_SECRET = {'FILE', 'PATH', 'DIR', 'LENGTH', 'LEN', 'ENABLE', 'ENABLED', 'DISABLE', 'DISABLED', 'POLICY', 'EXPIRY',
              'EXPIRE', 'EXPIRES', 'TTL', 'TIMEOUT', 'MIN', 'MAX', 'REQUIRED', 'RESET', 'TYPE', 'MODE', 'METHOD', 'HEADER',
              'URL_PATH', 'COMPLEXITY', 'LOGIN_ATTEMPTS', 'PORT', 'HOST', 'USER', 'USERNAME', 'NAME', 'BYPASS', 'PASSTHROUGH',
              'COMPASS', 'PASSIVE', 'PUBLIC', 'PUID', 'PGID', 'THROUGH',
              'RANDOM', 'ALLOW', 'EMPTY', 'VALIDITY', 'TIME', 'GENERATE', 'SKIP'}
DB_URL = {'DATABASE_URL', 'DB_URL', 'CONNECTION_STRING', 'DB_CONNECTION_STRING', 'MONGO_URL', 'MONGODB_URI'}
SECRET_FLAGS = re.compile(r'(?:^|\s)(--(?:password|passwd|pass|token|api-?key|secret|auth-?key|credentials?))(?:[=\s]|$)', re.I)
ENV_ARG = re.compile(r'(?:^|\s)(?:-e|--env)(?:\s+|=)[\'"]?([A-Za-z_][A-Za-z0-9_.]*)=')


# Input limits. Everything in the feed and in a template is written by third parties, so every value is
# checked before it goes into the index: type, length, characters, and for addresses the scheme. A value that
# fails is left out (a reference) or its whole entry is (an app with no usable address, name or image; a
# template version with too many or bad fields). Each is counted in the build log; one bad entry never stops
# the build, an index that would not pass check_index() does. FORMAT.md lists the same limits.
LIMITS = {'text': 200, 'ver': 32, 'image': 255, 'url': 2048, 'path': 1024, 'key': 512, 'keys': 2000,
          'versions': 500, 'apps': 100000, 'files': 200000, 'feed_bytes': 256 << 20, 'index_bytes': 64 << 20}
# C0/C1 controls, DEL, zero-width and bidi overrides, line/paragraph separators, BOM, lone surrogates (which
# json.dumps writes as \ud800 escapes that strict readers, PHP's json_decode among them, refuse outright).
BAD_CHARS = re.compile('[\x00-\x1f\x7f-\x9f​-‏‪-‮  ⁦-⁩﻿\ud800-\udfff]')
IMAGE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/:@-]*$')
GH_OWNER = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$')
GH_REPO = re.compile(r'^[A-Za-z0-9._-]{1,100}$')
URL_CHARS = re.compile('[\\s"\'<>\\\\^`{|}]')
SET_ID = re.compile(r'^[0-9a-f]{10}$')
SHA = re.compile(r'^[0-9a-f]{7,40}$')
REJECTED = {}  # what was left out, by reason: printed in the build log and kept in summary.json


def reject(why, n=1):
    REJECTED[why] = REJECTED.get(why, 0) + n


def clean_text(v, limit):
    """A one-line text value: scalar only, bad characters removed, None when longer than limit."""
    v = _s(v)
    if BAD_CHARS.search(v):
        v = BAD_CHARS.sub('', v).strip()
    return v if len(v) <= limit else None


def clean_url(v):
    """An http or https address with a host and no user info, spaces, quotes or angle brackets, else ''."""
    v = _s(v)
    if not v or len(v) > LIMITS['url'] or BAD_CHARS.search(v) or URL_CHARS.search(v):
        return ''
    try:
        u = urllib.parse.urlsplit(v)
        host = u.hostname
    except ValueError:
        return ''
    if u.scheme.lower() not in ('http', 'https') or not host or '@' in u.netloc:
        return ''
    return v


def raw_target(u):
    """(owner, repo, rest) of a raw GitHub template address that is safe to clone and look up, else None."""
    m = RAW.match(u or '')
    if not m or not clean_url(u):
        return None
    owner, repo, rest = m[1], m[2], m[3]
    if not GH_OWNER.match(owner) or not GH_REPO.match(repo) or repo in ('.', '..'):
        return None
    if len(rest) > LIMITS['path'] or BAD_CHARS.search(rest) or '\\' in rest or any(p in ('', '.', '..') for p in rest.split('/')):
        return None
    return owner, repo, rest


def clean_key(k):
    """A field key from a template, or None when it is too long or has a control character."""
    return k if isinstance(k, str) and len(k) <= LIMITS['key'] and not BAD_CHARS.search(k) else None


def words(name):
    n = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', name.strip())
    return [w for w in re.split(r'[^A-Za-z0-9]+', n.upper()) if w]


def secret_like(name):
    """True when a variable or label name looks like it holds a secret value. See FORMAT.md."""
    if not name:
        return False
    w = words(name)
    if '_'.join(w) in DB_URL:
        return True
    if any(x in NOT_SECRET for x in w):
        return False
    if any(x in SECRET_WORDS for x in w):
        return True
    return any(x == 'KEY' and i > 0 and w[i - 1] in KEY_AFTER for i, x in enumerate(w)) or \
        any(x.endswith(('APIKEY', 'SECRETKEY', 'PRIVATEKEY', 'AUTHKEY', 'ACCESSKEY')) for x in w)


def arg_secrets(text):
    """Secret-like keys in an ExtraParams / PostArgs default: '--password' style flags and -e NAME= with a
    secret-like NAME. Only the flag or variable name is returned, never a value."""
    out = set()
    for m in SECRET_FLAGS.finditer(text or ''):
        out.add('flag:' + m[1].lower())
    for m in ENV_ARG.finditer(text or ''):
        if secret_like(m[1]):
            out.add('env:' + m[1])
    return out


def parse(xml):
    """(sorted field keys, secret info) of a template, or None when it does not parse or is not a
    container template. Secret info: m = keys with Mask="true", s = secret-like variable/label keys,
    x = secret-like keys in ExtraParams/PostArgs. Lists are sorted; no value is ever kept."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    if root.find('Name') is None:
        return None
    configs = root.findall('Config')
    if len(configs) > LIMITS['keys']:
        reject('template version: more than %d fields' % LIMITS['keys'])
        return None
    keys, m, s = set(), set(), set()
    for c in configs:
        k = clean_key(field_key(c.get('Type') or '', c.get('Target') or '', c.get('Name') or '', c.get('Mode') or ''))
        if k is None:
            reject('template version: a field key too long or with a control character')
            return None
        keys.add(k)
        if (c.get('Mask') or '').strip().lower() == 'true':
            m.add(k)
        t = (c.get('Type') or '').strip().lower()
        if t.startswith('disabled:'):
            t = t[9:]
        if t in ('variable', 'label') and (secret_like(c.get('Target') or '') or secret_like(c.get('Name') or '')):
            s.add(k)
    x = set()
    for tag in ('ExtraParams', 'PostArgs'):
        e = root.find(tag)
        if e is not None and e.text:
            x |= {f'{tag.lower()}:{v}' for v in arg_secrets(e.text)}
    if any(clean_key(v) is None for v in x):
        reject('template version: a field key too long or with a control character')
        return None
    return sorted(keys), {'m': sorted(m), 's': sorted(s), 'x': sorted(x)}


def field_set(xml):
    """Sorted field keys of a template, or None (kept for callers of format 1)."""
    p = parse(xml)
    return p[0] if p else None


def sec_id(sec):
    """Id of a secret info object, or None when it is empty."""
    if not (sec['m'] or sec['s'] or sec['x']):
        return None
    return hashlib.sha1(json.dumps(sec, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:10]


def set_id(fields):
    return hashlib.sha1('\n'.join(fields).encode()).hexdigest()[:10]


def _read_feed(fh):
    """JSON from a feed stream, refusing one larger than LIMITS['feed_bytes'] before parsing it."""
    data = fh.read(LIMITS['feed_bytes'] + 1)
    if len(data) > LIMITS['feed_bytes']:
        raise ValueError(f"feed larger than {LIMITS['feed_bytes']} bytes")
    return json.loads(data)


def load_feed(src):
    if src and os.path.exists(src):
        try:
            with open(src, 'rb') as fh:
                return _read_feed(fh)
        except (ValueError, OSError) as e:
            sys.exit(f'feed {src} refused: {e}')
    last = None
    for u in ([src] if src else FEED_URLS):
        try:
            req = urllib.request.Request(u, headers={'User-Agent': 'unraid-app-chronicle'})
            with urllib.request.urlopen(req, timeout=180) as r:
                return _read_feed(r)
        except Exception as e:  # try the next mirror
            last = e
    sys.exit(f'feed download failed: {last}')


def feed_time(feed, now=None):
    """The feed's last_updated_timestamp as Unix seconds, the current time when it has none. A value that is
    not a whole number from 2001 to one day ahead is refused: a far-future one would freeze the catalog, since
    only a newer feed updates it."""
    now = now or time.time()
    v = feed.get('last_updated_timestamp')
    if v in (None, ''):
        return int(now)
    if isinstance(v, str) and v.isdigit():
        v = int(v)
    if isinstance(v, bool) or not isinstance(v, int) or not 1e9 <= v <= now + 86400:
        sys.exit(f'feed timestamp {str(v)[:40]!r} refused')
    return v


# References kept for each app, by feed field. Only addresses, names and versions, never template content.
APP_FIELDS = {'name': 'Name', 'template_url': None, 'repository': 'Repository', 'registry': 'Registry',
              'support': 'Support', 'project': 'Project', 'ca_repo': 'Repo'}
PLUGIN_FIELDS = {'name': 'Name', 'plugin_url': 'PluginURL', 'template_url': 'TemplateURL', 'author': None,
                 'support': 'Support', 'project': 'Project', 'ca_repo': 'Repo', 'min_ver': 'MinVer', 'max_ver': 'MaxVer'}
APP_FLAGS = ('Deprecated', 'Blacklist', 'ModeratorComment', 'Incompatible')


def _s(v):
    return v.strip() if isinstance(v, str) else (str(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else '')


def app_url(a):
    """The address that identifies a feed entry: PluginURL for a plugin, else TemplateURL, else caTemplateURL."""
    if a.get('Plugin'):
        return _s(a.get('PluginURL'))
    return _s(a.get('TemplateURL')) or _s(a.get('caTemplateURL'))


def app_key(a):
    """Stable id of an app: its address plus the name of the Community Applications repository that lists
    it (the same file can be listed by two repositories)."""
    return app_url(a) + '#' + _s(a.get('Repo'))


URL_FIELDS = ('template_url', 'plugin_url', 'registry', 'support', 'project')
TEXT_FIELDS = {'author': 'text', 'min_ver': 'ver', 'max_ver': 'ver'}


def clean_record(key, r, now=None):
    """(key, record) with every reference checked, or None when the app cannot be kept: its kind, address,
    name, catalog repository name or (containers) image is missing or fails a check. Used for the feed and
    again, at output, for every app in the saved catalog. A link that fails is emptied; both are counted."""
    now = int(now or time.time())
    if not isinstance(r, dict) or r.get('kind') not in ('container', 'plugin'):
        return reject('app: unknown kind')
    plugin = r['kind'] == 'plugin'
    out = {'kind': r['kind']}
    addr = clean_url(r.get('plugin_url' if plugin else 'template_url'))
    name = clean_text(r.get('name'), LIMITS['text'])
    ca_repo = clean_text(r.get('ca_repo'), LIMITS['text'])
    if not addr:
        return reject('app: address missing or not an http(s) address')
    if not name or ca_repo is None:
        return reject('app: name or catalog repository name missing or too long')
    if not plugin:
        img = _s(r.get('repository'))
        if not img or len(img) > LIMITS['image'] or not IMAGE.match(img):
            return reject('app: image name missing or not a valid image name')
        out['repository'] = img
    if not isinstance(key, str) or key != f'{addr}#{ca_repo}':
        return reject('app: key does not match its address and catalog repository')
    out.update(name=name, ca_repo=ca_repo)
    for f in URL_FIELDS:
        if f not in r:
            continue
        v = clean_url(r.get(f))
        if _s(r.get(f)) and not v:
            reject(f'link: {f} not an http(s) address, left out')
        out[f] = v
    for f, lim in TEXT_FIELDS.items():
        if f in r:
            v = clean_text(r.get(f), LIMITS[lim])
            if v is None:
                reject(f'reference: {f} too long, left out')
            out[f] = v or ''
    t = raw_target(addr)
    out['github'] = f'{t[0]}/{t[1]}' if t else ''
    out['flags'] = sorted(f for f in (r.get('flags') or []) if f in APP_FLAGS) if isinstance(r.get('flags'), list) else []
    for f in ('first_seen', 'last_seen'):
        v = r.get(f)
        if f in r:
            out[f] = v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= now + 86400 else now
    if '_first' in r:
        v = r['_first']
        out['_first'] = v if isinstance(v, int) and 1e9 <= v <= now + 86400 else None
    if r.get('removed') is True:
        out['removed'] = True
    if 'moved_from' in r:
        mf = r['moved_from'] if isinstance(r['moved_from'], list) else []
        out['moved_from'] = sorted({k for k in mf if isinstance(k, str) and len(k) <= LIMITS['url'] + LIMITS['text'] + 1
                                    and not BAD_CHARS.search(k)})[:50]
    return key, out


def feed_apps(feed, now=None):
    """{key: references} for every container app and plugin in the feed with an address. Every value is
    checked by clean_record(); an entry that fails is left out and counted."""
    out = {}
    applist = feed.get('applist', [])
    for a in applist if isinstance(applist, list) else []:
        if not isinstance(a, dict):
            reject('app: feed entry is not an object')
            continue
        plugin = bool(a.get('Plugin'))
        if not app_url(a) or (not plugin and not _s(a.get('Repository'))):
            continue
        if plugin:
            r = {k: (_s(a.get(f)) if f else (_s(a.get('PluginAuthor')) or _s(a.get('Author')))) for k, f in PLUGIN_FIELDS.items()}
        else:
            r = {k: (_s(a.get(f)) if f else app_url(a)) for k, f in APP_FIELDS.items()}
        r['kind'] = 'plugin' if plugin else 'container'
        r['flags'] = sorted(f for f in APP_FLAGS if a.get(f) not in (None, '', False, 'false', 'False'))
        fs = a.get('FirstSeen')
        r['_first'] = int(fs) if str(fs).isdigit() and len(str(fs)) <= 12 else None
        name = clean_text(r['name'], LIMITS['text'])
        repo = clean_text(r['ca_repo'], LIMITS['text'])
        c = clean_record(f'{app_url(a)}#{repo}', dict(r, name=name, ca_repo=repo), now) if name and repo is not None \
            else reject('app: name or catalog repository name missing or too long')
        if c is None:
            continue
        if len(out) >= LIMITS['apps']:
            reject('app: over the app limit')
            continue
        out[c[0]] = c[1]
    return out


def _fields(r):
    return sorted(k for k in r if not k.startswith('_'))


def _image(ref):
    """Image name without tag or registry default, for matching an app that moved to a new address."""
    i = ref.lower().split('@')[0]
    if ':' in i.rsplit('/', 1)[-1]:
        i = i.rsplit(':', 1)[0]
    for p in ('docker.io/', 'index.docker.io/', 'registry.hub.docker.com/'):
        if i.startswith(p):
            i = i[len(p):]
    return i


def update_catalog(catalog, apps, now):
    """Merge today's feed apps into the catalog. Returns the change events, oldest field first.
    An app that disappears and an app that appears in the same run with the same name and the same image
    (containers) or author (plugins) is one app that moved (its address or listing repository changed)."""
    events = []
    added = [k for k in apps if k not in catalog or catalog[k].get('removed')]
    gone = [k for k, c in catalog.items() if not c.get('removed') and k not in apps]
    def ident(r):
        if r.get('kind') == 'plugin':
            return ('plugin', r.get('author', '').lower(), r['name'].lower())
        return ('container', _image(r.get('repository', '')), r['name'].lower())
    gone_by = {}
    for k in gone:
        gone_by.setdefault(ident(catalog[k]), []).append(k)
    add_by = {}
    for k in added:
        if k not in catalog:
            add_by.setdefault(ident(apps[k]), []).append(k)
    for i, olds in gone_by.items():
        news = add_by.get(i, [])
        if len(olds) == 1 and len(news) == 1:
            o, n = olds[0], news[0]
            c = catalog.pop(o)
            c['moved_from'] = sorted(set(c.get('moved_from', [])) | {o})
            catalog[n] = c
            gone.remove(o)
            added.remove(n)
            ch = {f: [c.get(f), apps[n][f]] for f in _fields(apps[n]) if c.get(f) != apps[n][f]}
            events.append({'event': 'moved', 'kind': apps[n]['kind'], 'key': n, 'from': o, 'name': apps[n]['name'], 'changes': ch})
            for f in _fields(apps[n]):
                c[f] = apps[n][f]
            c['last_seen'] = now
    for k in sorted(added):
        r = apps[k]
        if k in catalog:
            c = catalog[k]
            c.pop('removed', None)
            events.append({'event': 'returned', 'kind': r['kind'], 'key': k, 'name': r['name']})
        else:
            c = catalog[k] = {'first_seen': r['_first'] or now}
            events.append({'event': 'added', 'kind': r['kind'], 'key': k, 'name': r['name']})
        for f in _fields(r):
            c[f] = r[f]
        c['last_seen'] = now
    for k in sorted(gone):
        c = catalog[k]
        c['removed'] = True
        events.append({'event': 'removed', 'kind': c.get('kind'), 'key': k, 'name': c['name'], 'last_seen': c['last_seen']})
    for k, r in sorted(apps.items()):
        c = catalog[k]
        if c.get('last_seen') == now:
            continue
        ch = {f: [c.get(f), r[f]] for f in _fields(r) if c.get(f) != r[f]}
        if ch:
            events.append({'event': 'changed', 'kind': r['kind'], 'key': k, 'name': r['name'], 'changes': ch})
            for f in ch:
                c[f] = r[f]
        c['last_seen'] = now
    return events


def catalog_targets(catalog):
    """{(owner, repo): set(rest-of-url)} for the GitHub template address of every app in the catalog,
    removed ones and earlier addresses of moved ones included, so their history stays in the index.
    Only addresses that pass raw_target() are cloned or looked up."""
    out = {}
    for c in catalog.values():
        if c.get('kind') != 'container':
            continue  # plugins: references only, no field history
        for u in [c.get('template_url') or ''] + [k.split('#', 1)[0] for k in c.get('moved_from', [])]:
            t = raw_target(u)
            if t:
                out.setdefault((t[0], t[1]), set()).add(t[2])
            elif RAW.match(u or ''):
                reject('template address: not a safe GitHub owner, repository or path')
    return out


def git(*args, cwd=None, timeout=900):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, env=ENV, timeout=timeout)


_reported = []


def _report(what, err):
    """Print the first few git failures, so a run that fails everywhere shows why."""
    if len(_reported) < 5:
        _reported.append(what)
        print(f'{what} failed: {err.strip()[-300:]}', file=sys.stderr, flush=True)


def heads(owner, repo):
    """{branch: sha}, {} for a repository that is gone, or None when GitHub would not answer."""
    for wait in (0, 5, 30):
        time.sleep(wait)
        r = git('ls-remote', '--heads', '--end-of-options', f'https://github.com/{owner}/{repo}.git', timeout=60)
        if r.returncode == 0:
            break
        err = r.stderr.decode()
        if 'not found' in err.lower() or 'error: 404' in err:
            return {}
    else:
        _report(f'ls-remote {owner}/{repo}', err)
        return None
    h = {}
    for line in r.stdout.decode().splitlines():
        sha, ref = line.split('\t')
        h[ref[len('refs/heads/'):]] = sha
    return h


def split_branch(rest, branches):
    """'main/dir/x.xml' -> ('main', 'dir/x.xml'); the longest branch name that matches wins."""
    for b in sorted(branches, key=len, reverse=True):
        if rest.startswith(b + '/'):
            return b, rest[len(b) + 1:]
    return None, None


def history(gitdir, branch, paths):
    """{path: [[time, sha7, set_id], ...]} and the sets seen, oldest first, one entry per field-set change."""
    log = git('log', '--format=#%H %ct', '--name-only', '--diff-filter=AMR', '--no-renames', '--end-of-options', f'refs/heads/{branch}', '--', *paths,
              cwd=gitdir, timeout=900)
    per, cur, want = {}, None, set(paths)
    for line in log.stdout.decode('utf8', 'replace').split('\n'):
        if line.startswith('#'):
            cur = line[1:].split()
        elif line in want and cur:
            per.setdefault(line, []).append(cur)
    cat = subprocess.Popen(['git', 'cat-file', '--batch'], cwd=gitdir, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=ENV)
    files, sets, secs = {}, {}, {}
    try:
        for p, commits in per.items():
            versions, last = [], None
            for sha, ct in reversed(commits):
                cat.stdin.write(f'{sha}:{p}\n'.encode()); cat.stdin.flush()
                head = cat.stdout.readline().split()
                if len(head) < 3:  # "<name> missing": filtered out by the clone, or gone
                    continue
                size = int(head[2])
                if head[1] != b'blob' or size > MAX_BLOB:
                    while size > 0:  # drain it, keep the stream in step
                        size -= len(cat.stdout.read(min(size, 1 << 16)))
                    cat.stdout.read(1)
                    if head[1] == b'blob':
                        print(f'skipped {p} at {sha[:7]}: {int(head[2])} bytes', file=sys.stderr)
                    continue
                data = cat.stdout.read(size); cat.stdout.read(1)
                if b'<!DOCTYPE' in data or b'<!ENTITY' in data:  # no entity expansion, ever
                    continue
                pr = parse(data.decode('utf8', 'replace'))
                if pr is None:
                    continue
                f, sec = pr
                i = set_id(f)
                sets[i] = f
                j = sec_id(sec)
                if j:
                    secs[j] = sec
                if (i, j) != last:
                    versions.append([int(ct), sha[:7], i, j])
                    last = (i, j)
            if versions:
                files[p] = versions
    finally:
        cat.stdin.close(); cat.wait()
    return files, sets, secs


def build_repo(owner, repo, rests, state_dir, work):
    """Rebuild one repository's part when its branch heads changed; otherwise reuse the saved part."""
    name = f'{owner}/{repo}'
    part_file = os.path.join(state_dir, 'repos', f'{owner}__{repo}.json'.lower())
    old = json.load(open(part_file)) if os.path.exists(part_file) else None
    h = heads(owner, repo)
    if h is None:
        return name, old, 'unreachable' + (' (kept old part)' if old else '')
    if not h:
        return name, old, 'gone' + (' (kept old part)' if old else '')
    wanted = {}
    for rest in rests:
        b, p = split_branch(rest, h)
        if b:
            wanted.setdefault(b, set()).add(p)
    if not wanted:
        return name, None, 'no branch matches'
    want_heads = {b: h[b] for b in wanted}
    want_paths = {b: sorted(v) for b, v in wanted.items()}
    if old and old.get('v') == PART_VERSION and old.get('heads') == want_heads and old.get('paths') == want_paths:
        return name, old, 'unchanged'
    d = tempfile.mkdtemp(dir=work)
    try:
        url = f'https://github.com/{owner}/{repo}.git'
        r = git('clone', '--bare', '--quiet', '--filter=blob:limit=1m', '--no-tags', url, d, timeout=1800)
        if r.returncode:
            _report(f'clone {name}', r.stderr.decode())
            return name, old, 'clone failed: ' + r.stderr.decode()[-200:].strip()
        if any(b not in h for b in wanted):
            return name, old, 'branch vanished'
        git('fetch', '--quiet', '--end-of-options', 'origin', *[f'+refs/heads/{b}:refs/heads/{b}' for b in wanted], cwd=d, timeout=900)
        files, sets, secs = {}, {}, {}
        for b, paths in want_paths.items():
            f, s, x = history(d, b, paths)
            sets.update(s)
            secs.update(x)
            for p, v in f.items():
                files[f'{b}/{p}'] = v
        part = {'v': PART_VERSION, 'heads': want_heads, 'paths': want_paths, 'files': files, 'sets': sets, 'secrets': secs}
        os.makedirs(os.path.dirname(part_file), exist_ok=True)
        with open(part_file + '.tmp', 'w') as fh:
            json.dump(part, fh, separators=(',', ':'), sort_keys=True)
        os.replace(part_file + '.tmp', part_file)
        return name, part, 'built'
    except subprocess.TimeoutExpired:
        return name, old, 'timeout'
    finally:
        shutil.rmtree(d, ignore_errors=True)


def dumps_safe(obj, **kw):
    """Compact JSON with <, > and & written as \\u escapes, so a third-party string stays inert even when a
    reader puts the JSON inside an HTML page or a script block. Non-ASCII is escaped too (ensure_ascii)."""
    t = json.dumps(obj, separators=(',', ':'), sort_keys=True, **kw)
    return t.replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')


def _keys_ok(v, limit=None):
    return isinstance(v, list) and len(v) <= (limit or LIMITS['keys']) and all(clean_key(k) is not None for k in v)


def _sec_ok(v):
    return isinstance(v, dict) and set(v) == {'m', 's', 'x'} and all(_keys_ok(v[k]) for k in 'msx')


def _entry_ok(e, sets, secs):
    return (isinstance(e, list) and len(e) == 4 and isinstance(e[0], int) and not isinstance(e[0], bool) and e[0] >= 0
            and isinstance(e[1], str) and SHA.match(e[1]) is not None and e[2] in sets
            and (e[3] is None or e[3] in secs))


def merge_part(index, name, part):
    """Adds one repository's part to the index, checking every set, secret info and file entry again (parts
    saved by an older build were made without these checks). What fails is left out and counted."""
    if not isinstance(part, dict):
        return reject('part: not an object')
    sets = {}
    for i, v in (part.get('sets') or {}).items():
        if SET_ID.match(str(i)) and _keys_ok(v):
            sets[i] = v
        else:
            reject('part: field set with a bad id or key')
    secs = {}
    for i, v in (part.get('secrets') or {}).items():
        if SET_ID.match(str(i)) and _sec_ok(v):
            secs[i] = v
        else:
            reject('part: secret info with a bad id or key')
    files = {}
    for k, v in (part.get('files') or {}).items():
        key = f'{name}/{k}'
        if not isinstance(k, str) or len(key) > LIMITS['path'] + 200 or BAD_CHARS.search(key) or not isinstance(v, list):
            reject('part: file with a bad path')
            continue
        good = [e for e in v if _entry_ok(e, sets, secs)]
        if len(good) != len(v):
            reject('part: file version entry with a bad shape or unknown id', len(v) - len(good))
        if len(good) > LIMITS['versions']:
            reject('part: versions over the limit, oldest left out', len(good) - LIMITS['versions'])
            good = good[-LIMITS['versions']:]
        if good:
            files[key] = good
    heads = part.get('heads')
    if not isinstance(heads, dict) or not all(isinstance(b, str) and 0 < len(b) <= 255 and not BAD_CHARS.search(b)
                                              and isinstance(h, str) and re.match(r'^[0-9a-f]{40}$', h) for b, h in heads.items()):
        return reject('part: branch heads with a bad shape')
    if len(index['files']) + len(files) > LIMITS['files']:
        return reject('part: over the file limit')
    index['repos'][name] = heads
    index['sets'].update(sets)
    index['secrets'].update(secs)
    index['files'].update(files)


def check_index(index):
    """The problems that make an index invalid, [] when there are none. Run on the whole index before it is
    written: a build that would publish an invalid index stops instead."""
    p = []
    if index.get('format') != FORMAT or not isinstance(index.get('generated'), int) or not isinstance(index.get('feed_updated'), int):
        p.append('format, generated or feed_updated')
    for k in ('apps', 'repos', 'sets', 'secrets', 'files'):
        if not isinstance(index.get(k), dict):
            return p + [f'{k} is not an object']
    for k, c in index['apps'].items():
        if clean_record(k, c) != (k, c):
            p.append(f'app {k[:80]!r}')
    for i, v in index['sets'].items():
        if not SET_ID.match(i) or not _keys_ok(v):
            p.append(f'set {i[:20]!r}')
    for i, v in index['secrets'].items():
        if not SET_ID.match(i) or not _sec_ok(v):
            p.append(f'secrets {i[:20]!r}')
    for k, v in index['files'].items():
        if BAD_CHARS.search(k) or not isinstance(v, list) or not v or len(v) > LIMITS['versions'] \
                or not all(_entry_ok(e, index['sets'], index['secrets']) for e in v):
            p.append(f'file {k[:80]!r}')
    if len(index['apps']) > LIMITS['apps'] or len(index['files']) > LIMITS['files']:
        p.append('over the app or file limit')
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--feed')
    ap.add_argument('--state', default='state')
    ap.add_argument('--out', default='public')
    ap.add_argument('--work', default='work')
    ap.add_argument('--changes', default='changes')
    ap.add_argument('--jobs', type=int, default=8)
    ap.add_argument('--limit', type=int, default=0)
    a = ap.parse_args()
    t0 = time.time()
    feed = load_feed(a.feed)
    for d in (a.work, a.out, a.state, a.changes):
        os.makedirs(d, exist_ok=True)
    if not isinstance(feed, dict) or not isinstance(feed.get('applist'), list):
        sys.exit('feed refused: no app list')
    now = feed_time(feed)
    cat_file = os.path.join(a.state, 'catalog.json')
    cat = json.load(open(cat_file)) if os.path.exists(cat_file) else {'v': CATALOG_VERSION, 'updated': 0, 'apps': {}}
    if cat.get('v') != CATALOG_VERSION:
        sys.exit(f'state catalog version {cat.get("v")} is not {CATALOG_VERSION}')
    events = []
    if now > cat['updated']:
        apps = feed_apps(feed, now)
        first = not cat['apps']
        listed = sum(1 for c in cat['apps'].values() if not c.get('removed'))
        # a truncated or emptied feed would mark most of the catalog removed in one go
        if listed >= 500 and len(apps) < listed // 2 and not os.environ.get('CHRONICLE_ALLOW_SHRINK'):
            sys.exit(f'feed refused: {len(apps)} usable apps, the catalog lists {listed} (set CHRONICLE_ALLOW_SHRINK=1 if that is right)')
        events = update_catalog(cat['apps'], apps, now)
        if first:  # the first build is the baseline: every app would be "added"
            events = []
        cat['updated'] = now
        day = time.strftime('%Y-%m-%d', time.gmtime(now))
        if events:
            with open(os.path.join(a.changes, day[:4] + '.jsonl'), 'a') as fh:
                for e in events:
                    fh.write(dumps_safe({'date': day, 'time': now, **e}) + '\n')
        with open(cat_file + '.tmp', 'w') as fh:
            json.dump(cat, fh, indent=0, sort_keys=True)
        os.replace(cat_file + '.tmp', cat_file)
    else:
        print(f'feed not newer than the catalog ({now} <= {cat["updated"]}), catalog unchanged', file=sys.stderr)
    targets = sorted(catalog_targets(cat['apps']).items())
    if a.limit:
        targets = targets[:a.limit]
    apps_out = {}
    for k, c in cat['apps'].items():
        r = clean_record(k, c)
        if r is None:
            reject('app: saved catalog entry left out of the index')
        else:
            apps_out[k] = r[1]
    index = {'format': FORMAT, 'generated': int(time.time()), 'feed_updated': now,
             'apps': apps_out, 'repos': {}, 'sets': {}, 'secrets': {}, 'files': {}}
    status = {}
    with ThreadPoolExecutor(a.jobs) as ex:
        futs = [ex.submit(build_repo, o, r, rests, a.state, a.work) for (o, r), rests in targets]
        for n, fut in enumerate(futs, 1):
            name, part, how = fut.result()
            status[name] = how
            if n % 100 == 0:
                bad = sum(1 for v in status.values() if v.startswith(('unreachable', 'clone failed', 'timeout')))
                print(f'{n}/{len(futs)} {time.time() - t0:.0f}s, {bad} failed', flush=True)
                if bad == n:
                    ex.shutdown(wait=False, cancel_futures=True)
                    sys.exit(f'all {n} repositories failed so far; stopping (see the errors above)')
            if not part:
                continue
            merge_part(index, name, part)
    problems = check_index(index)
    if problems:
        sys.exit(f'index refused, {len(problems)} problem(s): ' + '; '.join(problems[:10]))
    raw = dumps_safe(index).encode()
    if len(raw) > LIMITS['index_bytes']:
        sys.exit(f"index refused: {len(raw)} bytes, the limit is {LIMITS['index_bytes']}")
    for why, n in sorted(REJECTED.items()):
        print(f'left out: {n} x {why}', file=sys.stderr)
    open(os.path.join(a.out, 'index.json'), 'wb').write(raw)
    gz = gzip.compress(raw, 9, mtime=0)
    open(os.path.join(a.out, 'index.json.gz'), 'wb').write(gz)
    json.dump(status, open(os.path.join(a.state, 'status.json'), 'w'), indent=0, sort_keys=True)
    pub = os.path.join(a.out, 'changes')
    os.makedirs(pub, exist_ok=True)
    for f in sorted(os.listdir(a.changes)):
        if f.endswith('.jsonl'):
            shutil.copyfile(os.path.join(a.changes, f), os.path.join(pub, f))
    counts = {}
    for v in status.values():
        k = v.split(':')[0].split(' (')[0]
        counts[k] = counts.get(k, 0) + 1
    ev = {}
    for e in events:
        ev[e['event']] = ev.get(e['event'], 0) + 1
    summary = {'apps': len(cat['apps']), 'plugins': sum(1 for c in cat['apps'].values() if c.get('kind') == 'plugin'),
               'apps_removed': sum(1 for c in cat['apps'].values() if c.get('removed')),
               'changes_this_run': ev, 'repos': len(targets), 'repos_indexed': len(index['repos']), 'files': len(index['files']),
               'versions': sum(len(v) for v in index['files'].values()), 'distinct_sets': len(index['sets']), 'distinct_secret_infos': len(index['secrets']),
               'raw_bytes': len(raw), 'gzip_bytes': len(gz), 'seconds': round(time.time() - t0, 1), 'status': counts,
               'rejected': dict(sorted(REJECTED.items()))}
    json.dump(summary, open(os.path.join(a.out, 'summary.json'), 'w'), indent=1)
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
