# project-words-export

A small offline CLI for one confirmed project glossary with two deterministic exports: a CSpell word-list file and a Wispr Flow dictionary-import CSV. It reads only the exact file names and manifest fields listed in your config. Discovered words stay in `candidates`; they never enter exports until a person copies them into `terms` and sets `confirmed` to `true` in the local glossary JSON.

## Requirements and quick start

Python 3.11+ (for the standard-library TOML reader); no third-party packages.

```sh
python3 project_words.py discover --config project-words.example.json --glossary glossary.json
# Review candidates in glossary.json and explicitly confirm selected terms.
python3 project_words.py export --glossary glossary.json \
  --cspell project-words.txt --wispr-csv wispr-dictionary.csv
python3 demo.py
```

Example config:

```json
{
  "root": "sample-project",
  "files": ["src/FlowEngine.py"],
  "manifests": {
    "package.json": ["name"],
    "pyproject.toml": ["project.name"]
  }
}
```

`files` entries contribute tokens from those exact selected basenames. `manifests` maps exact selected JSON or TOML files to exact dotted string fields; no other file content is read. Source paths must be relative, exist, and contain no symlink component; resolved paths must remain under the chosen root. Manifest bytes are opened through descriptor-relative no-follow access and fail closed on platforms without that support, so a parent-path swap cannot redirect a manifest read. Each candidate keeps its source path, source kind, and manifest field when applicable. A confirmed entry keeps its original provenance; discovery updates its `active` flag and adds newly observed source records without removing an entry when its source disappears. Review the glossary before sharing it: paths and project names may reveal structure.

The glossary schema has `candidates` and `terms` arrays. To confirm a candidate, copy its `word` and `sources` into a `terms` entry, add `"confirmed": true` and an `aliases` list (possibly empty), then review the entry. Aliases are manual correction inputs; normalized aliases cannot collide with another term or alias. A confirmed source that disappears remains in the glossary with `active: false` and is still exported until you remove it yourself.

## Export formats

The CSpell export is UTF-8, sorted, one word per line, with a final newline. Single-token aliases are included; phrase aliases are kept for correction export but omitted because the CSpell word-list output is line based.

The Wispr CSV has no header and uses the documented dictionary import forms: one column for a word and two columns for `misspelling,correction`. Each confirmed canonical term produces a one-column row; each manually confirmed alias produces an alias-to-term row. CSV quoting, Unicode, commas, quotes, and embedded line breaks are serialized with Python's CSV writer. This validates file structure only; the project has not been imported into the Wispr app, so it does not claim end-to-end compatibility. The official docs list a 60-character first field and support for Unicode; exports are not a snippets JSON generator.

## Scope and limits

This project does not rewrite dictation, access a microphone, connect to an app account, or send data to a cloud service. Candidate discovery is not automatic recognition or approval. The working title was `RepoLex`, already in use, so the descriptive project name is `project-words-export`. Repositories that collect words from a project already exist; this tool's narrow value is a manually confirmed provenance-bearing JSON source shared by two portable exports. It makes no claim of absolute novelty, registry availability, trademark clearance, or name clearance.

The demo creates synthetic files in a temporary directory and applies one explicit example confirmation in its own fixture code to demonstrate the two steps. It does not read a real project.

## Development

```sh
python3 -m unittest discover -s tests -v
```

MIT licensed; see [LICENSE](LICENSE). Source references: [CSpell getting started](https://cspell.org/docs/getting-started), [Wispr Flow bulk import format](https://docs.wisprflow.ai/articles/8955301725-How-Do-I-Bulk-Import-Dictionary-Items-and-Snippets), [blacktop/Voice](https://github.com/blacktop/Voice).
