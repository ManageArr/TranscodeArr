# Contributing

Issues and pull requests are both welcome. This file is short on purpose; it is
the whole set of rules.

## Running the tests

```sh
python -m unittest discover tests -v
```

Python 3.11 or newer, from the repo root. No virtualenv, no `pip install`, no
`make` step, because there is nothing to install. That is the point of the next
section.

CI runs exactly that, then checks every module still imports, then builds the
image. Run the tests before you open a pull request and you will have seen what
CI will say.

## Pure standard library, no dependencies

No third-party packages. No build step, no bundler, no CDN. The bundled UI is
HTML, CSS and JavaScript served as strings out of `app/web.py`, not a frontend
project.

This is not minimalism for its own sake. The image runs unattended on someone's
NAS for years at a time and takes an unreviewed update from nobody. Every
dependency added here is a package that has to keep building for both amd64 and
arm64, keep being maintained, and keep not being compromised, forever, for a
program the standard library already covers. `sqlite3`, `http.server`,
`urllib`, `subprocess`, `hashlib` and `hmac` are doing all of the work today.

If something genuinely needs a dependency, open an issue and make the case
before you write the code.

## House rules for text

- **American English.** behavior, color, canceling, initialize, normalized,
  licensed. The one exception is the job state literal `cancelled`, which is a
  frozen database value and API enum. Leave that spelling alone.
- **No em-dashes or en-dashes**, anywhere: code, comments, docs, commit
  messages, UI strings. Use a hyphen.
- **UTF-8, LF line endings**, every file. This repo gets edited on Windows, so
  check your editor is not writing CRLF.
- **Comments say why, and name the failure.** Not what the line does, which the
  line already says. The comments in this codebase read like small incident
  reports because most of them are.

## The one thing to understand

This program replaces files in someone's media library. Not a cache, not a
generated artifact: the only copy of a film somebody owns. The safety rules are
therefore not a feature area, they are the product:

- a source is only touched after its size has held still (`is_stable`)
- output is written to a partial name that never becomes visible until it is
  verified against the source (`verify_output`)
- the replacement is an `os.replace` on the same filesystem, and the original
  goes to a trash directory with a retention window, never to `unlink`
- every path is resolved and confirmed inside `MEDIA_ROOTS` first
  (`validate_path`)

Those live in `app/core.py` as pure functions precisely so they can be tested
without an ffmpeg or a disk.

**A change anywhere on the write path needs a test, and the test's name should
say which failure it prevents.** `test_a_short_output_never_replaces_the_source`
tells the next reader what went wrong once. `test_replace_2` tells them nothing,
and the day it goes red they will delete it.

Small, focused pull requests. The maintainer reads every line, so a diff that
does one thing gets read sooner than one that does five.
