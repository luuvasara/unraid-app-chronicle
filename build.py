#!/usr/bin/env python3
"""Build the template history index from the Community Applications feed.

For every template file the feed points at, record each distinct field set the file has had in its
git history. Only field identity is kept (type, target, port protocol); no defaults, descriptions or
anything else. See FORMAT.md.

Also keeps a catalog of every container app the feed has listed, removed ones included, with its last known
references, and a changelog of what was added, removed or changed (state/catalog.json, changes/YYYY.jsonl).

usage: build.py [--feed FILE|URL] [--state DIR] [--out DIR] [--changes DIR] [--jobs N] [--limit N]
"""
import argparse, gzip, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time, urllib.request
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
ENV = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='true', GIT_NO_LAZY_FETCH='1')
MAX_BLOB = 1024 * 1024  # a template larger than this is skipped
# GitHub limits anonymous git over HTTPS (HTTP 401 after a few hundred repositories). With a token in
# INDEX_GIT_TOKEN (in Actions: the job's GITHUB_TOKEN) every git call sends it as a header, passed
# through the environment so it never appears in a command line.
if os.environ.get('INDEX_GIT_TOKEN'):
    import base64
    _auth = base64.b64encode(f"x-access-token:{os.environ['INDEX_GIT_TOKEN']}".encode()).decode()
    ENV.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',
               GIT_CONFIG_VALUE_0=f'Authorization: basic {_auth}')
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
    keys, m, s = set(), set(), set()
    for c in root.findall('Config'):
        k = field_key(c.get('Type') or '', c.get('Target') or '', c.get('Name') or '', c.get('Mode') or '')
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


def load_feed(src):
    if src and os.path.exists(src):
        return json.load(open(src))
    last = None
    for u in ([src] if src else FEED_URLS):
        try:
            req = urllib.request.Request(u, headers={'User-Agent': 'template-history-index'})
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)
        except Exception as e:  # try the next mirror
            last = e
    sys.exit(f'feed download failed: {last}')


# References kept for each app, by feed field. Only addresses and names, never template content.
APP_FIELDS = {'name': 'Name', 'template_url': None, 'repository': 'Repository', 'registry': 'Registry',
              'support': 'Support', 'project': 'Project', 'ca_repo': 'Repo'}
APP_FLAGS = ('Deprecated', 'Blacklist', 'ModeratorComment')


def _s(v):
    return v.strip() if isinstance(v, str) else ''


def app_url(a):
    """The template address of a feed entry: TemplateURL, else caTemplateURL."""
    return _s(a.get('TemplateURL')) or _s(a.get('caTemplateURL'))


def app_key(a):
    """Stable id of a container app: its template address plus the name of the Community Applications
    repository that lists it (the same file can be listed by two repositories)."""
    return app_url(a) + '#' + _s(a.get('Repo'))


def feed_apps(feed):
    """{key: references} for every container app in the feed (not plugins, not entries without an address)."""
    out = {}
    for a in feed.get('applist', []):
        if a.get('Plugin') or not _s(a.get('Repository')) or not app_url(a):
            continue
        r = {k: (_s(a.get(f)) if f else app_url(a)) for k, f in APP_FIELDS.items()}
        m = RAW.match(r['template_url'])
        r['github'] = f'{m[1]}/{m[2]}' if m else ''
        r['flags'] = sorted(f for f in APP_FLAGS if a.get(f) not in (None, '', False, 'false', 'False'))
        fs = a.get('FirstSeen')
        r['_first'] = int(fs) if str(fs).isdigit() else None
        out[app_key(a)] = r
    return out


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
    An app that disappears and an app that appears in the same run with the same image and name is
    one app that moved (its address or listing repository changed)."""
    events = []
    added = [k for k in apps if k not in catalog or catalog[k].get('removed')]
    gone = [k for k, c in catalog.items() if not c.get('removed') and k not in apps]
    def ident(r):
        return (_image(r['repository']), r['name'].lower())
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
            ch = {f: [c.get(f), apps[n][f]] for f in list(APP_FIELDS) + ['github', 'flags'] if c.get(f) != apps[n][f]}
            events.append({'event': 'moved', 'key': n, 'from': o, 'name': apps[n]['name'], 'changes': ch})
            for f in list(APP_FIELDS) + ['github', 'flags']:
                c[f] = apps[n][f]
            c['last_seen'] = now
    for k in sorted(added):
        r = apps[k]
        if k in catalog:
            c = catalog[k]
            c.pop('removed', None)
            events.append({'event': 'returned', 'key': k, 'name': r['name']})
        else:
            c = catalog[k] = {'first_seen': r['_first'] or now}
            events.append({'event': 'added', 'key': k, 'name': r['name']})
        for f in list(APP_FIELDS) + ['github', 'flags']:
            c[f] = r[f]
        c['last_seen'] = now
    for k in sorted(gone):
        c = catalog[k]
        c['removed'] = True
        events.append({'event': 'removed', 'key': k, 'name': c['name'], 'last_seen': c['last_seen']})
    for k, r in sorted(apps.items()):
        c = catalog[k]
        if c.get('last_seen') == now:
            continue
        ch = {f: [c.get(f), r[f]] for f in list(APP_FIELDS) + ['github', 'flags'] if c.get(f) != r[f]}
        if ch:
            events.append({'event': 'changed', 'key': k, 'name': r['name'], 'changes': ch})
            for f in ch:
                c[f] = r[f]
        c['last_seen'] = now
    return events


def catalog_targets(catalog):
    """{(owner, repo): set(rest-of-url)} for the GitHub template address of every app in the catalog,
    removed ones and earlier addresses of moved ones included, so their history stays in the index."""
    out = {}
    for c in catalog.values():
        for u in [c.get('template_url') or ''] + [k.split('#', 1)[0] for k in c.get('moved_from', [])]:
            m = RAW.match(u)
            if m:
                out.setdefault((m[1], m[2]), set()).add(m[3])
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
    now = int(feed.get('last_updated_timestamp') or time.time())
    cat_file = os.path.join(a.state, 'catalog.json')
    cat = json.load(open(cat_file)) if os.path.exists(cat_file) else {'v': CATALOG_VERSION, 'updated': 0, 'apps': {}}
    if cat.get('v') != CATALOG_VERSION:
        sys.exit(f'state catalog version {cat.get("v")} is not {CATALOG_VERSION}')
    events = []
    if now > cat['updated']:
        apps = feed_apps(feed)
        first = not cat['apps']
        events = update_catalog(cat['apps'], apps, now)
        if first:  # the first build is the baseline: every app would be "added"
            events = []
        cat['updated'] = now
        day = time.strftime('%Y-%m-%d', time.gmtime(now))
        if events:
            with open(os.path.join(a.changes, day[:4] + '.jsonl'), 'a') as fh:
                for e in events:
                    fh.write(json.dumps({'date': day, 'time': now, **e}, separators=(',', ':'), sort_keys=True) + '\n')
        with open(cat_file + '.tmp', 'w') as fh:
            json.dump(cat, fh, indent=0, sort_keys=True)
        os.replace(cat_file + '.tmp', cat_file)
    else:
        print(f'feed not newer than the catalog ({now} <= {cat["updated"]}), catalog unchanged', file=sys.stderr)
    targets = sorted(catalog_targets(cat['apps']).items())
    if a.limit:
        targets = targets[:a.limit]
    index = {'format': FORMAT, 'generated': int(time.time()), 'feed_updated': feed.get('last_updated_timestamp'),
             'apps': cat['apps'], 'repos': {}, 'sets': {}, 'secrets': {}, 'files': {}}
    status = {}
    with ThreadPoolExecutor(a.jobs) as ex:
        futs = [ex.submit(build_repo, o, r, rests, a.state, a.work) for (o, r), rests in targets]
        for n, fut in enumerate(futs, 1):
            name, part, how = fut.result()
            status[name] = how
            if n % 100 == 0:
                bad = sum(1 for v in status.values() if v.startswith(('unreachable', 'clone failed', 'timeout')))
                print(f'{n}/{len(futs)} {time.time() - t0:.0f}s, {bad} failed', flush=True)
            if not part:
                continue
            index['repos'][name] = part['heads']
            index['sets'].update(part['sets'])
            index['secrets'].update(part.get('secrets', {}))
            for k, v in part['files'].items():
                index['files'][f'{name}/{k}'] = v
    raw = json.dumps(index, separators=(',', ':'), sort_keys=True).encode()
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
    summary = {'apps': len(cat['apps']), 'apps_removed': sum(1 for c in cat['apps'].values() if c.get('removed')),
               'changes_this_run': ev, 'repos': len(targets), 'repos_indexed': len(index['repos']), 'files': len(index['files']),
               'versions': sum(len(v) for v in index['files'].values()), 'distinct_sets': len(index['sets']), 'distinct_secret_infos': len(index['secrets']),
               'raw_bytes': len(raw), 'gzip_bytes': len(gz), 'seconds': round(time.time() - t0, 1), 'status': counts}
    json.dump(summary, open(os.path.join(a.out, 'summary.json'), 'w'), indent=1)
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
