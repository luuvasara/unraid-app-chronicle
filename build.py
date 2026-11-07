#!/usr/bin/env python3
"""Build the template history index from the Community Applications feed.

For every template file the feed points at, record each distinct field set the file has had in its
git history. Only field identity is kept (type, target, port protocol); no defaults, descriptions or
anything else. See FORMAT.md.

usage: build.py [--feed FILE|URL] [--state DIR] [--out DIR] [--jobs N] [--limit N]
"""
import argparse, gzip, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time, urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

FORMAT = 1
FEED_URLS = ['https://ca.unraid.net/assets/feed/applicationFeed.json',
             'https://assets.ca.unraid.net/feed/applicationFeed.json',
             'https://raw.githubusercontent.com/Squidly271/AppFeed/master/applicationFeed.json']
RAW = re.compile(r'^https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/(.+)$')
ENV = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='true')


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


def field_set(xml):
    """Sorted field keys of a template, or None when it does not parse or is not a container template."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    if root.find('Name') is None:
        return None
    return sorted({field_key(c.get('Type') or '', c.get('Target') or '', c.get('Name') or '', c.get('Mode') or '')
                   for c in root.findall('Config')})


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


def feed_targets(feed):
    """{(owner, repo): set(rest-of-url)} for every GitHub raw template address in the feed."""
    out = {}
    for a in feed.get('applist', []):
        m = RAW.match(a.get('TemplateURL') or '')
        if m:
            out.setdefault((m[1], m[2]), set()).add(m[3])
    return out


def git(*args, cwd=None, timeout=900):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, env=ENV, timeout=timeout)


def heads(owner, repo):
    r = git('ls-remote', '--heads', f'https://github.com/{owner}/{repo}.git', timeout=60)
    if r.returncode:
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
    log = git('log', '--format=#%H %ct', '--name-only', '--diff-filter=AMR', '--no-renames', branch, '--', *paths,
              cwd=gitdir, timeout=900)
    per, cur, want = {}, None, set(paths)
    for line in log.stdout.decode('utf8', 'replace').split('\n'):
        if line.startswith('#'):
            cur = line[1:].split()
        elif line in want and cur:
            per.setdefault(line, []).append(cur)
    cat = subprocess.Popen(['git', 'cat-file', '--batch'], cwd=gitdir, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=ENV)
    files, sets = {}, {}
    try:
        for p, commits in per.items():
            versions, last = [], None
            for sha, ct in reversed(commits):
                cat.stdin.write(f'{sha}:{p}\n'.encode()); cat.stdin.flush()
                head = cat.stdout.readline().split()
                if len(head) < 3 or head[1] != b'blob':
                    continue
                data = cat.stdout.read(int(head[2])); cat.stdout.read(1)
                f = field_set(data.decode('utf8', 'replace'))
                if f is None:
                    continue
                i = set_id(f)
                sets[i] = f
                if i != last:
                    versions.append([int(ct), sha[:7], i])
                    last = i
            if versions:
                files[p] = versions
    finally:
        cat.stdin.close(); cat.wait()
    return files, sets


def build_repo(owner, repo, rests, state_dir, work):
    """Rebuild one repository's part when its branch heads changed; otherwise reuse the saved part."""
    name = f'{owner}/{repo}'
    part_file = os.path.join(state_dir, 'repos', f'{owner}__{repo}.json'.lower())
    old = json.load(open(part_file)) if os.path.exists(part_file) else None
    h = heads(owner, repo)
    if h is None:
        return name, old, 'unreachable' + (' (kept old part)' if old else '')
    wanted = {}
    for rest in rests:
        b, p = split_branch(rest, h)
        if b:
            wanted.setdefault(b, set()).add(p)
    if not wanted:
        return name, None, 'no branch matches'
    want_heads = {b: h[b] for b in wanted}
    want_paths = {b: sorted(v) for b, v in wanted.items()}
    if old and old.get('heads') == want_heads and old.get('paths') == want_paths:
        return name, old, 'unchanged'
    d = tempfile.mkdtemp(dir=work)
    try:
        url = f'https://github.com/{owner}/{repo}.git'
        r = git('clone', '--bare', '--quiet', '--filter=blob:limit=1m', '--no-tags', url, d, timeout=1800)
        if r.returncode:
            return name, old, 'clone failed: ' + r.stderr.decode()[-200:].strip()
        if any(b not in h for b in wanted):
            return name, old, 'branch vanished'
        git('fetch', '--quiet', 'origin', *[f'+refs/heads/{b}:refs/heads/{b}' for b in wanted], cwd=d, timeout=900)
        files, sets = {}, {}
        for b, paths in want_paths.items():
            f, s = history(d, b, paths)
            sets.update(s)
            for p, v in f.items():
                files[f'{b}/{p}'] = v
        part = {'heads': want_heads, 'paths': want_paths, 'files': files, 'sets': sets}
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
    ap.add_argument('--jobs', type=int, default=8)
    ap.add_argument('--limit', type=int, default=0)
    a = ap.parse_args()
    t0 = time.time()
    feed = load_feed(a.feed)
    targets = sorted(feed_targets(feed).items())
    if a.limit:
        targets = targets[:a.limit]
    os.makedirs(a.work, exist_ok=True); os.makedirs(a.out, exist_ok=True)
    index = {'format': FORMAT, 'generated': int(time.time()), 'feed_updated': feed.get('last_updated_timestamp'),
             'repos': {}, 'sets': {}, 'files': {}}
    status = {}
    with ThreadPoolExecutor(a.jobs) as ex:
        futs = [ex.submit(build_repo, o, r, rests, a.state, a.work) for (o, r), rests in targets]
        for n, fut in enumerate(futs, 1):
            name, part, how = fut.result()
            status[name] = how
            if n % 100 == 0:
                print(f'{n}/{len(futs)} {time.time() - t0:.0f}s', flush=True)
            if not part:
                continue
            index['repos'][name] = part['heads']
            index['sets'].update(part['sets'])
            for k, v in part['files'].items():
                index['files'][f'{name}/{k}'] = v
    raw = json.dumps(index, separators=(',', ':'), sort_keys=True).encode()
    open(os.path.join(a.out, 'index.json'), 'wb').write(raw)
    gz = gzip.compress(raw, 9, mtime=0)
    open(os.path.join(a.out, 'index.json.gz'), 'wb').write(gz)
    json.dump(status, open(os.path.join(a.state, 'status.json'), 'w'), indent=0, sort_keys=True)
    counts = {}
    for v in status.values():
        k = v.split(':')[0].split(' (')[0]
        counts[k] = counts.get(k, 0) + 1
    summary = {'repos_in_feed': len(targets), 'repos_indexed': len(index['repos']), 'files': len(index['files']),
               'versions': sum(len(v) for v in index['files'].values()), 'distinct_sets': len(index['sets']),
               'raw_bytes': len(raw), 'gzip_bytes': len(gz), 'seconds': round(time.time() - t0, 1), 'status': counts}
    json.dump(summary, open(os.path.join(a.out, 'summary.json'), 'w'), indent=1)
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
