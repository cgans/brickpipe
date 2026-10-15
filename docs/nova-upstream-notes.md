# Notes for LDraw Nova upstream

Things found while building brickpipe against ldraw-nova v0.6.0 that belong in
Nova rather than here. Each is a candidate issue or pull request for
<https://github.com/anteloc/ldraw-nova> (or ldraw-nova-docker). None has been
filed yet.

## 1. Token usage is discarded

Every provider reply carries token counts, and `litellm.stream_chunk_builder`
returns them in `full.usage` (web/backend/agent.py, after the stream loop),
but Nova never reads them. Nothing about cost reaches the saved messages, the
event stream or the UI, so a user only learns a design was expensive when the
provider cuts them off.

Suggested change: after each step, read `full.usage`, store the input and
output counts on the saved message, emit a `usage` event, and show a running
total per turn in the chat header. About six lines in `agent.py` plus a little
UI. brickpipe would then show cost per round.

## 2. The chat page does not notice messages sent from outside

The page only learns about a message it sent itself, and only subscribes to
the chat's event stream for a turn it started. A message posted through the
API (`POST /api/chats/{id}/messages`) starts a turn that the open page never
shows until it is reloaded.

Suggested change: subscribe to the chat's stream on page open and handle a
"new message" event, so an API-driven turn appears like a typed one. Needed
by any tool that drives Nova programmatically.

## 3. The Docker repo has no `.gitattributes`

ldraw-nova has one (keeping `.mpd` files CRLF), ldraw-nova-docker does not.
A default Windows clone converts `entrypoint.sh` and the scripts to CRLF and
the container fails to start with a "no such file or directory" error.

Suggested change: add `.gitattributes` with `*.sh text eol=lf` to
ldraw-nova-docker, and mention `-c core.autocrlf=false` in the README for
Windows users.

## 4. Image build needs far more disk than the README says

The README says the first build "needs about 5 GB". On a Windows laptop with
14.5 GB free the final unpack step ran out of space and the build engine
died, because the image's layers exist twice during unpacking on top of the
build cache. About 20 GB free was needed to finish.

Suggested change: say "about 5 GB for the image; allow 20 GB free for the
build", and mention `docker builder prune` for a retry.

## 5. Models the chat produced are a dict here and a list there

`GET /api/chats/{id}` returns `models` as a dict keyed by id;
`GET /api/chats` returns each chat's `models` as a list. Harmless for the UI,
which knows both, but it caught a client written from the source.

Suggested change: return a list in both, or document the difference.
