# Remote MCP attachments

An MCP client does not need access to the server filesystem. Existing local-path
`send_email.attachments` and `download_attachment` calls remain available for local
integrations; new remote clients should use the transfer tools below.

## Upload and attach

1. Read `attachment_limits`.
2. Calculate the file's exact byte length and SHA-256 digest on the client.
3. Call `begin_attachment_upload(name, mime_type, size, sha256)`.
4. Send base64 chunks to `upload_attachment_chunk(upload_id, offset, content_base64)`.
   Each decoded chunk is at most 262,144 bytes. Use the returned `received_bytes` as
   the next offset. Identical chunks can be retried after a lost acknowledgement.
5. Once `ready` is true, pass `attachment_upload_ids: [upload_id]` to
   `create_draft` or, after the owner's explicit approval, `send_email`.

`create_draft` saves a multipart MIME message with the actual attachment bytes
inside the mailbox's Drafts folder. A mail client can open and send that draft.
It does not send SMTP mail. Bcc and reply/thread headers remain in the draft;
SMTP sending puts Bcc recipients only in the delivery envelope.
IMAP drafts and Sent copies serialize with standard SMTP CRLF line endings; the
encoded-message limit counts those actual wire bytes. For older LF-only messages,
Gmail can report an LF-based BODYSTRUCTURE size and return the part with CRLF.
Retrieval accepts that exact reversible size difference only for valid base64
whose line endings are uniformly CRLF. Truncation, mixed/bare-CR lines and other
size mismatches still fail closed. Decoded file bytes and SHA-256 are unchanged.
An attached `.eml` with MIME type `message/rfc822` is composed as a nested message
under RFC 2046, preserving its message structure rather than using invalid
base64 message encoding. Mail serialization can normalize its line endings.

Uploads are consumed when the provider operation starts, including a failed or
uncertain attempt. A missing SMTP/APPEND acknowledgement must be reconciled in
Sent/Drafts before another attempt. The server never automatically repeats an
uncertain APPEND in another folder. A consumed handle cannot send the file again.

## Discover and receive

`list_emails_metadata` and `get_emails_content` keep the existing `attachments`
filename list and add `attachment_info` with:

- `attachment_id`: the MIME section, unique within the selected message;
- `name`, `mime_type`, `size`, `size_kind`, `disposition`.

Metadata uses IMAP BODYSTRUCTURE without downloading file contents. Its size is
the exact **transfer-encoded** octet count (`size_kind: transfer_encoded`), not
the decoded file length. Full message reads expose decoded sizes. A server that
cannot supply BODYSTRUCTURE sets `attachment_info_available: false`; an empty
list in that case is not proof that the message has no files. Duplicate filenames
and named inline images have separate MIME section IDs.

Call `get_attachment(account_name, email_id, attachment_id, mailbox)` using the
message UID, folder and section from the card. The response contains exact decoded
size, SHA-256 and the first base64 chunk. Small files also return a standard MCP
embedded binary resource. For larger files, call
`read_attachment_chunk(download_id, next_offset)` until `complete` is true, then
decode/concatenate the chunks on the client and verify SHA-256. The last chunk
immediately removes the temporary copy. After a lost final response, fetching the
original attachment again is a safe read.

These reads use a fresh IMAP selection and BODY.PEEK, with no STORE, EXPUNGE or
RFC822/BODY[] fallback, and preserve unread flags. The pinned aioimaplib version
does not update its internal selected state after EXAMINE, so this path uses
SELECT plus PEEK rather than changing library internals.

Unexpected read failures report the operation phase and exception class (for
example `Attachment read failed at mime_headers (TimeoutError)`). Logs contain
only those fixed phase labels and class names; provider exception text, mailbox
identities, payloads and credentials are excluded. Existing explicit validation
errors remain specific. No provider retry is added. This diagnostic does not by
itself fix or establish the cause of a hosted read failure.

## Explicitly copy original attachments

To reply or forward with original files, supply `attachment_sources` to
`send_email` or `create_draft`:

```json
[
  {
    "account_name": "example-account",
    "email_id": "42",
    "mailbox": "INBOX",
    "attachment_id": "2"
  }
]
```

Only the explicitly selected files are fetched. Nothing is attached just because
a message is a reply. Sources and fresh uploads can be combined.

## Limits and custody

- 16 MiB decoded per file and for the combined remote attachment payload.
- 25,000,000 bytes for the entire encoded MIME message, including headers/body.
  A provider can impose a smaller limit; there is no automatic split or resend.
- At most 20 attachments and 256 KiB decoded per transfer chunk.
- 10-minute absolute expiry, 32 transfers and 64 MiB of reserved transfer memory
  per process, with at most 32 MiB reserved per caller. Inbound attachment reads
  are serialized to bound temporary MIME decoding memory.
- No transfer files on disk, database or object storage. Unused transfers can be
  removed with `discard_attachment_transfer`. Restarting the process discards them.
- Expiry runs even without another request. Completion, discard and consumed
  uploads release the retained buffers. This is memory lifetime control, not a
  claim of cryptographic erasure of allocator memory.

The existing authenticated proxy must overwrite `X-Email-MCP-Caller` with a
SHA-256 fingerprint of the validated bearer credential, then strip Authorization.
The private backend must not be exposed directly. HTTP transfers fail closed if
the trusted header is absent. Transfers cannot be shared between credentials;
after token refresh/reconnection, restart an unfinished upload if necessary.
Stdio transfers are scoped to the MCP session. There are no new OAuth grants,
mailbox credentials, public download routes or persistent secrets.

## Validation and release

The test suite covers remote MCP HTTP calls, caller isolation, actual MIME
serialization, loopback IMAP metadata/PEEK/APPEND, Unicode and duplicate filenames,
inline files, chunk retries, digest failures, limits, automatic expiry and unknown
provider outcomes. It never sends mail to a real recipient.

Release the Python fork and its exact container pin together with the proxy
fingerprint change. A source commit and local tests do not establish that the
hosted MCP has been updated, or that a particular client UI can upload its local
files automatically. A hosted client draft/attachment acceptance remains part
of deployment verification.

## Sent and safe removal

`move_to_trash` replaces the destructive MCP deletion tool. It resolves the
mailbox's special-use Trash folder and uses atomic UID MOVE only. Unsupported
servers fail without a STORE/EXPUNGE fallback; removing something already in
Trash never empties it. Lost acknowledgements are not replayed automatically.

Gmail SMTP manages its Sent copy; the handler never adds another IMAP copy.
Spacemail installations enable `save_to_sent`. Folder discovery prefers the
server's special-use Sent flag, including localized or unquoted LIST names.
Before APPEND, Message-ID lookup detects an existing copy. Only one APPEND is
attempted: an uncertain result is reported after SMTP success with instructions
to reconcile Sent rather than resend the message.
