# Security policy

TranscodeArr holds an API key, can be told to serve TLS, and replaces files in
someone's media library. A bug in any of those is worth reporting privately
first, so a fix exists before the details do.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting:
**[Report a vulnerability](https://github.com/ManageArr/TranscodeArr/security/advisories/new)**
(also reachable from the repo's Security tab). It opens a private thread with
the maintainer, and it is the preferred channel.

Please do not open a public issue for a security bug. If the form is not
available to you, open an issue that says only that you have a report and how
to reach you privately, with no details in it.

Useful in a report: the version from `/healthz`, whether the port is published
beyond localhost, whether TLS is on, and the smallest request or file layout
that reproduces it.

## What to expect

This is a single-maintainer project with no company behind it, so there is no
response-time commitment to make. What is promised instead: reports are read,
you get an acknowledgment when the report is picked up, you are told plainly if
something is not going to be fixed and why, and a fix ships as a release with a
published advisory. Credit in the advisory if you want it, none if you do not.

## In scope

- Authentication and sessions: login, API keys, cookies, the logout paths.
- Anything that reads, writes, replaces or trashes a file outside `MEDIA_ROOTS`,
  or replaces a file the configured rules say is protected.
- Anything that gets shell or argv control into the `ffmpeg`, `ffprobe` or
  `openssl` invocations.
- Escaping the drop to `PUID:PGID`, or keeping capabilities the entrypoint is
  supposed to give up.
- The TLS path: certificate or key handling, or a configuration that ends up
  serving the login form in clear text when it was told not to.
- Secrets leaking into an API response, the config backup, or the log.

## Out of scope

- Publishing the port to the internet, or binding `0.0.0.0` on an untrusted
  network. The documentation says not to; an API that queues encodes and
  replaces media does not belong there at any authentication strength.
- Vulnerabilities in `ffmpeg` itself. Those go to the
  [jellyfin-ffmpeg](https://github.com/jellyfin/jellyfin-ffmpeg) or upstream
  FFmpeg projects. What is in scope here is anything TranscodeArr hands it that
  it should not.
- Losing a source file that documented behavior deleted: converting a folder you
  pointed `watch_roots` at and letting `trash_keep_days` expire does what it says.
  Data loss that contradicts the documentation is a bug report, and a welcome
  one, but it is not this form.
- Resource exhaustion from your own settings, such as pointing the watcher at a
  library larger than the disk can hold output for.
- **`TRANSCODEARR_RESET_ADMIN` deleting the admin account.** That is what it is
  for, and it is documented in the README. It is read only from the environment,
  so using it means being able to set an environment variable on this container -
  which is already being able to read `TRANSCODEARR_TOKEN` out of that same
  environment and to write the config volume the database lives in. Someone
  holding that could take the account by hand with `sqlite3`; the flag removes
  the hand-editing, not an obstacle. It grants nothing over the network: no route
  reads it, no request can set it, and it is acted on only at boot. It deletes
  the account and every session rather than setting a temporary password,
  because a temporary password would have to be written to a log, an environment
  or a response, all of which outlive the recovery. Minted API keys are
  deliberately kept, so a password recovery does not become an outage for
  whatever authenticates with them.
  - **In scope, and worth reporting:** any way to trigger that reset without
    already having write access to the container's environment - a route, a
    request, a stored setting or a restore payload that reaches it.

## Supported versions

The newest release. This project has one maintainer and no capacity for
backport branches, so a fix ships forward and you upgrade.
