# Unraid template history index

A daily index of how the Docker container templates in the Unraid app catalog have changed over
time. It is built for tools that compare an installed container with the template it came from.

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

- `https://luuvasara.github.io/unraid-template-history/index.json.gz` (also `index.json`), rebuilt
  daily by `.github/workflows/build.yml` and published with GitHub Pages.
- `summary.json` next to it: counts from the last build.
- Format: [FORMAT.md](FORMAT.md).

It covers every template in the feed whose `TemplateURL` points at a GitHub repository. For each version
of each template file it records the commit time, the short commit id, and the set of fields, each as its type and target (`variable:PUID`, `path:/config`,
`port:8080/tcp`).

Each version can also carry a list of fields that probably hold a secret: fields marked `Mask="true"`,
variables or labels whose name looks like a password, token or key, and `--password`-style flags or
`-e NAME=` variables in the default extra parameters or post arguments. A tool can use it to treat
those fields with care. Only field names are listed.

No default, value or description of any field is stored, and the templates themselves are not copied.

## Building it yourself

`python3 build.py` (Python 3.9+ and git) builds the same index:

1. It downloads the Community Applications feed and collects the GitHub repositories its
   `TemplateURL`s point at.
2. It clones each repository once (bare, without files over 1 MB), walks the history of every
   template file the feed names, records each distinct field set, and deletes the clone.
3. It writes `public/index.json`, `public/index.json.gz` and `public/summary.json`.

The per-repository results are kept in `state/`, so a later run only rebuilds repositories whose
branch has moved. `python3 build.py --help` lists the options.
