# Unraid template history index

A daily list of how every Community Applications container template has changed over time, by field
names only. A plugin can download one small file and tell which version of a template a saved copy
was based on, without asking GitHub for the history of each template itself.

- Index: `https://luuvasara.github.io/unraid-template-history/index.json.gz` (also `index.json`)
- Format: [FORMAT.md](FORMAT.md)
- Build: `python3 build.py` (Python 3.9+ and git). It reads the Community Applications feed, clones
  each template repository once without large files, records the field sets, and deletes the clone.
  The next run rebuilds only repositories whose branch moved; parts are kept in `state/`.
- Schedule: `.github/workflows/build.yml`, daily, published with GitHub Pages.

## What is recorded

For each version of a template: the commit time, the short commit id, and the set of fields, each as
its type and target (`variable:PUID`, `path:/config`, `port:8080/tcp`).

Each version can also carry secret info: which of its fields are masked (`Mask="true"`), which
variables or labels have a name that looks like a password, token or key, and which `--password`-style
flags or `-e NAME=` variables appear in the default extra parameters or post arguments. This is a list
of field names only, so a plugin can warn that a field probably holds a secret. No default, value or
description of any field is ever stored.

The index does not change or redistribute the templates.
