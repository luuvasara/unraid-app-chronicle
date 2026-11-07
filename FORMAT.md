# Index format, version 1

One JSON document, served as `index.json.gz` (gzip) and `index.json`. Rebuilt once a day.

```json
{
  "format": 1,
  "generated": 1794050000,
  "feed_updated": 1794040000,
  "repos": { "owner/repo": { "branch": "full commit sha of the branch head that was indexed" } },
  "sets":  { "3f2a9c01be": ["path:/config", "port:8080/tcp", "variable:PUID"] },
  "files": { "owner/repo/branch/path/to/template.xml": [[1700000000, "a1b2c3d", "3f2a9c01be"], ...] }
}
```

- `format`: changes only when a reader written for an older value would misread the file. Readers
  must refuse a value they do not know.
- `generated`, `feed_updated`: Unix seconds. `feed_updated` is the feed's own `last_updated_timestamp`.
- `files`: the key is the template address with `https://raw.githubusercontent.com/` removed, exactly
  as the feed spells it. Each entry is `[commit time, short commit sha, set id]`, oldest first. An entry
  is written only when the field set changes, so the time is the first commit that had that set; the
  set holds until the next entry.
- `sets`: field keys, sorted. A key is `type:target`: the Config Type in lower case with any
  `Disabled:` prefix dropped, then the Target. Paths lose a trailing `/` (except `/` itself). Ports are
  `port:target/protocol`, protocol from Mode, `tcp` when empty. A field with no Target is
  `type:name:Name`. Only `Config` elements directly under the root count. The id is the first 10 hex
  digits of SHA-1 over the keys joined with `\n`, so a reader can recompute it from a saved template.
- Nothing else is kept: no Default, Description, Mask or any value.

Only templates in the Community Applications feed are indexed, and only their current path: history
before a file was moved is not followed.
