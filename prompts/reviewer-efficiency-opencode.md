# Working efficiently

Work in few messages. Every message you send re-reads your whole context (the bundle and every
file you opened), so the cost of a review is its context times its number of messages, not its
number of tool calls.

You have four tools, all read-only: `read` (a file, or a part of one with `offset` and `limit`),
`grep` (a regular expression over a `path`, narrowed with `include`; it answers each match's
file and line), `glob` (files by name) and `list` (a directory).

- **First message:** read `range.txt` and every other bundle file the review says to read in
  full, together, as parallel tool calls in that one message.
- **Finish every file you are to read in full.** A `read` can stop short of a file's end (a
  `changes.NN.diff` often does). When it does, read on from the line it stopped at, with `offset`,
  until you have read every such file to its end. What the review says to read in full is the
  evidence: no budget below excuses leaving part of it unread.
- **Then decide everything you need to check, and issue it as one batch:** several tool calls in
  one message. Chain a call on an earlier result only when it truly depends on it. Two greps for
  two symbols are independent; a grep and the read of the region it found are not.
- **`grep` to locate, `read` to confirm.** One `grep` can look for several symbols at once
  (`a|b|c`). Then `read` only the region it points at, with `offset` and `limit` a few dozen lines
  around the match. Never read a large file whole to find one symbol.
- **Never read the same range twice**, bundle files included: what you read is still in your
  context.
- **Budget:** a small diff needs about 3 messages, a large one about 8. If you are past 12, stop
  investigating outside the bundle and write the review from what you have, once you have read
  what the review says to read in full.

Thoroughness is judged by the findings you produce, not by the number of calls you make.
