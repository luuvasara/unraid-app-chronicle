# Unraid template history index

A daily list of how every Community Applications container template has changed over time, by field
names only. A plugin can download one small file and tell which version of a template a saved copy
was based on, without asking GitHub for the history of each template itself.

- Format: [FORMAT.md](FORMAT.md)
- Build: `python3 build.py` (Python 3.9+ and git). It reads the Community Applications feed, clones
  each template repository once without large files, records the field sets, and deletes the clone.
  The next run rebuilds only repositories whose branch moved; parts are kept in `state/`.
- Schedule: `.github/workflows/build.yml`, daily, published with GitHub Pages.

The index holds field identities only. It does not change or redistribute the templates.
