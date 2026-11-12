# Unraid App Chronicle

A daily record of the Unraid app catalog: every container app and plugin that Community Applications
lists or has listed, removed ones included, with their last known links, and the history of each
container template's fields. It is built for tools that compare an installed container with the
template it came from, and for finding an app again after it has left the catalog.

## Background

[Unraid](https://unraid.net) installs most Docker containers from the
[Community Applications](https://forums.unraid.net/topic/38582-plug-in-community-applications/) plugin,
the app store of the Unraid web interface. Each app in that catalog is described by a **container
template**: an XML file, kept in its author's GitHub repository, that lists the container's image,
ports, paths, variables, devices and labels with their defaults.

The catalog itself is published as one JSON feed (`applicationFeed.json`, from `ca.unraid.net` with a
mirror on GitHub). For every app the feed gives `TemplateURL`, the address of its template file.

When an app is installed, Unraid saves its own copy of the template
(`/boot/config/plugins/dockerMan/templates-user/my-<name>.xml`) with the user's settings. That copy is
never updated. The author's template keeps changing: a new variable is added, a path or port is
renamed, a field is removed. An installed container can therefore miss settings that the current
template has, or keep ones the template dropped, and nothing shows it.

To tell which of these differences are the user's own changes and which come from the template,
a tool has to know which version of the template the saved copy was made from. That needs the git
history of every template file, which means one request to GitHub per template per server. This
repository does that work once a day for the whole catalog and publishes the result as one file.

## Who uses it

- [Chronostasis](https://github.com/luuvasara/unraid-chronostasis), an Unraid plugin that shows which
  installed containers differ from their current template and offers to apply the changes.

Any other tool can use it the same way: download the file, recompute the field set of a saved
template (the method is in [FORMAT.md](FORMAT.md)), and look it up.

## The index

- `https://luuvasara.github.io/unraid-app-chronicle/index.json.gz` (also `index.json`), rebuilt
  daily by `.github/workflows/build.yml` and published with GitHub Pages.
- `summary.json` next to it: counts from the last build.
- `changes/YYYY.jsonl` next to it (and in this repository): the catalog changelog, below.
- Format: [FORMAT.md](FORMAT.md).

It covers every template the feed lists, or has listed, at a GitHub address. For each version of
each template file it records the commit time, the short commit id, and the set of fields, each as
its type and target (`variable:PUID`, `path:/config`, `port:8080/tcp`).

Each version can also carry a list of fields that probably hold a secret: fields marked `Mask="true"`,
variables or labels whose name looks like a password, token or key, and `--password`-style flags or
`-e NAME=` variables in the default extra parameters or post arguments. A tool can use it to treat
those fields with care. Only field names are listed.

No default, value or description of any field is stored, and the templates themselves are not copied.

## Apps that leave the catalog

Apps are removed from the catalog, and templates move to new addresses. The index keeps every
container app it has seen, including removed ones, with its last known references: name, template
address, image, registry, support and project links, the GitHub repository and the catalog
repository that listed it, whether it was marked deprecated or blacklisted, and when it was first
and last seen. The template history of a removed app stays in the index too.

Plugins are kept the same way, with links only: name, plugin (`.plg`) address, its catalog template
address, author, support and project links, the supported Unraid versions, and the same flags and
dates. Nothing from the plugin files themselves is stored. With the last known
address, a removed app's template can still be found in its author's repository, or in that
repository's history, for as long as the repository exists.

Each build also appends what changed in the catalog since the previous one (apps added, removed,
listed again, moved to a new address, or with changed references) to `changes/YYYY.jsonl`, one line
per change, so the catalog's own history can be followed day by day.

## Building it yourself

`python3 build.py` (Python 3.9+ and git) builds the same index:

1. It downloads the Community Applications feed and collects the GitHub repositories its template
   addresses point at, plus those of apps it listed before.
2. It clones each repository once (bare, without files over 1 MB), walks the history of every
   template file the feed names, records each distinct field set, and deletes the clone.
3. It updates the app list in `state/catalog.json`, appends the day's changes to `changes/`, and
   writes `public/index.json`, `public/index.json.gz`, `public/summary.json` and `public/changes/`.

The per-repository results are kept in `state/`, so a later run only rebuilds repositories whose
branch has moved. `state/` and `changes/` are committed by the daily build: the app list and the
changelog cannot be rebuilt from a later feed. `python3 build.py --help` lists the options.

Every value from the feed and from the templates is treated as untrusted: it is checked for type,
length, characters and (for links) the `http`/`https` scheme before it reaches the index, and what
fails is left out and counted in the build log. The limits are in [FORMAT.md](FORMAT.md).
`python3 -I tests/test_hostile.py` runs the hostile-input tests (no network needed).
