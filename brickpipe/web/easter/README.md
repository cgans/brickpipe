# Easter egg clip

When a visitor presses "Use the shared keys" in the setup dialog without having
added any keys of their own, brickpipe tries to play `nedry.mp3` from this
folder and shows an "Ah, ah, ah!" toast.

The clip is deliberately **not** included in the repository. To enable it, drop
your own copy at:

    brickpipe/web/easter/nedry.mp3

`easter/*.mp3` is gitignored, so your file is never committed. Without it, the
page plays a short original three-blip sound instead.
