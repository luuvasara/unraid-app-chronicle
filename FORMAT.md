# Index format, version 3

One JSON document, served as `index.json.gz` (gzip) and `index.json`. Rebuilt once a day.

```json
{
  "format": 3,
  "generated": 1794050000,
  "feed_updated": 1794040000,
  "apps": { "https://raw.githubusercontent.com/owner/repo/main/app.xml#Owner's Repository": {
      "name": "app", "template_url": "https://raw.githubusercontent.com/owner/repo/main/app.xml",
      "github": "owner/repo", "ca_repo": "Owner's Repository", "repository": "owner/app:latest",
      "registry": "https://hub.docker.com/r/owner/app", "support": "https://...", "project": "https://...",
      "flags": ["Deprecated"], "first_seen": 1600000000, "last_seen": 1794040000,
      "removed": true, "moved_from": ["...earlier key..."] } },
  "repos": { "owner/repo": { "branch": "full commit sha of the branch head that was indexed" } },
  "sets":  { "3f2a9c01be": ["path:/config", "port:8080/tcp", "variable:PUID"] },
  "secrets": { "9c0e5d17aa": {"m": ["variable:PASSWORD"], "s": ["variable:API_KEY", "variable:PASSWORD"], "x": ["extraparams:env:X_TOKEN"]} },
  "files": { "owner/repo/branch/path/to/template.xml": [[1700000000, "a1b2c3d", "3f2a9c01be", "9c0e5d17aa"], ...] }
}
```

- `format`: changes only when a reader written for an older value would misread the file. Readers
  must refuse a value they do not know.
- `generated`, `feed_updated`: Unix seconds. `feed_updated` is the feed's own `last_updated_timestamp`.
- `apps` (format 3): every container app the feed has listed since the index started, including apps
  that are no longer in it, with their last known references. Plugins and entries without a template
  address are not included.
  - The key is the template address (`TemplateURL`, else `caTemplateURL`), `#`, and the name of the
    Community Applications repository that lists it (`Repo`). The same file can be listed by two
    repositories, so the address alone is not unique.
  - `name`, `repository` (the image), `registry`, `support`, `project` and `ca_repo` are the feed's
    `Name`, `Repository`, `Registry`, `Support`, `Project` and `Repo`, as last seen. `github` is the
    `owner/repo` of a GitHub template address, empty for other hosts.
  - `flags`: which of `Deprecated`, `Blacklist` and `ModeratorComment` the feed sets for the app. Only
    their presence is kept, never the comment text.
  - `first_seen`: the feed's `FirstSeen` when the app was first indexed, else the build time.
    `last_seen`: the `feed_updated` of the last build that listed it.
  - `removed`: present and `true` when the last build did not list the app. Its `files` history is
    kept while its repository exists, and kept as last built when the repository is gone.
  - `moved_from`: earlier keys. When, in one build, an app disappears and another appears with the
    same image (without tag) and the same name, it is one app whose address or listing repository
    changed; it keeps its `first_seen`.
- `files`: the key is the template address with `https://raw.githubusercontent.com/` removed, exactly
  as the feed spells it. Each entry is `[commit time, short commit sha, set id, secrets id]`, oldest
  first. The secrets id is `null` when the version has no masked or secret-like field. An entry is
  written when the field set or the secrets info changes, so two entries in a row can have the same
  set id. (Format 1 entries had three elements and changed only with the set. Format 3 adds `apps` and changes nothing else.)
- `sets`: field keys, sorted. A key is `type:target`: the Config Type in lower case with any
  `Disabled:` prefix dropped, then the Target. Paths lose a trailing `/` (except `/` itself). Ports are
  `port:target/protocol`, protocol from Mode, `tcp` when empty. A field with no Target is
  `type:name:Name`. Only `Config` elements directly under the root count. The id is the first 10 hex
  digits of SHA-1 over the keys joined with `\n`, so a reader can recompute it from a saved template.
- `secrets` (format 2): field keys only, sorted, never a value.
  - `m`: fields with `Mask="true"` (shown as a password field by Unraid).
  - `s`: Variable and Label fields whose Target or Name looks like it holds a secret. The name is split
    into upper-case words (`FOO_BAR`, `fooBar`, `Foo bar`). It is secret-like when a word is one of
    PASSWORD, PASSWD, PASS, PWD, PASSPHRASE, SECRET(S), TOKEN(S), APIKEY, CREDENTIAL(S), CREDS, OTP, TOTP,
    WEBHOOK, DSN, PSK, PRIVATE; or a word ends in APIKEY, SECRETKEY, PRIVATEKEY, AUTHKEY, ACCESSKEY; or
    KEY follows API, ACCESS, SECRET, PRIVATE, LICENSE, AUTH, ENCRYPTION, CLIENT, APP, MASTER, SESSION,
    JWT, HMAC, WG, WIREGUARD, SHARED, PRODUCT, SERIAL and similar; or the whole name is a database URL
    (DATABASE_URL, CONNECTION_STRING, ...). It is not secret-like when any word describes rather than
    holds the secret: FILE, PATH, DIR, LENGTH, ENABLE(D), DISABLE(D), POLICY, EXPIRY, TTL, TIMEOUT, MIN,
    MAX, TYPE, MODE, METHOD, HEADER, PORT, HOST, USER, USERNAME, NAME, PUBLIC, PASSIVE, THROUGH, RANDOM,
    ALLOW, EMPTY, VALIDITY, TIME, GENERATE, SKIP and so on (MYSQL_RANDOM_ROOT_PASSWORD is a yes/no switch). A Redis URL
    is not counted: it rarely carries a password.
    The lists are in `build.py`; a change to them is a new build, not a new format.
  - `x`: secret-like keys in the default `ExtraParams` or `PostArgs`: `--password`-style flags
    (`flag:--password`) and `-e NAME=` / `--env NAME=` with a secret-like NAME (`env:NAME`), prefixed with
    the element name in lower case.
  - The id is the first 10 hex digits of SHA-1 over the object as compact JSON with sorted keys.
- Nothing else is kept: no Default, Description or any value.

Only templates the Community Applications feed lists or has listed are indexed. Within a repository,
history before a file was moved is followed only when the feed's address changed with it (`moved_from`).

## Changelog

`changes/YYYY.jsonl` in the repository, also published as `changes/YYYY.jsonl` next to the index: one
JSON object per line, appended by every build that sees a newer feed. The first build is the baseline
and writes none.

```json
{"date": "2026-11-08", "time": 1794040000, "event": "removed", "key": "...", "name": "app", "last_seen": 1793950000}
```

- `event`: `added`, `removed`, `returned` (a removed app is listed again), `changed`, `moved`.
- `key`, `name`: as in `apps`. `moved` also has `from`, the earlier key.
- `changes` (`changed`, `moved`): `{field: [old, new]}` for the `apps` fields that differ.
- `date` and `time` are the feed's `last_updated_timestamp` (UTC date).
