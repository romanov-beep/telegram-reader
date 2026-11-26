# TDLib adapter

`telegram_reader.tdlib` uses Python's standard library and the official shared
`libtdjson` library. Importing it does not load native code or access Telegram.
Install/build TDLib separately; this project does not fetch or compile it on the
small VPS. Use the source revision recorded by deployment tooling, never an
unreviewed third-party binary.

Official references inspected during implementation:

- [JSON C API](https://github.com/tdlib/td/blob/master/td/telegram/td_json_client.h).
- [TDLib API schema](https://github.com/tdlib/td/blob/master/td/generate/scheme/td_api.tl),
  verified at pinned commit `9d92b49bc5d0d2e29fe518239af647a88f8680e6`
  (TDLib 1.8.69).

The binding requires the modern `td_create_client_id`, `td_send`, `td_receive`,
and `td_execute` interface. TDLib's receiver is process-global and cannot run
simultaneously in two threads. This adapter permits one client per process, with
one receiver thread. Responses use `@client_id` and `@extra` correlation. Native
pointers are copied by ctypes' `c_char_p` before the next receive call.

Idle receive waits up to one second; responses wake it immediately. Runtime 429
starts a fail-fast cooldown without retries or sleeps; local close bypasses it.
No application chat/message cache is added. Folder metadata is bounded to 100
records, 256 title characters and 64 icon characters, with scalar whitelisting.
It is cleared on authorization loss, receiver failure and close.

## Contract

Construct `TdClient(config)` with `tdjson_path`, `api_id`, `api_hash`,
`database_directory`, `files_directory`, `database_encryption_key`, and optionally
`request_timeout` (20 seconds by default). The encryption key is a plaintext Python
string supplied securely by the caller; it is UTF-8/base64 encoded for TDLib's
`bytes` field. Base64 is serialization, not encryption. The adapter does not write
configuration files or log secrets. Native logging is disabled before startup.

Directories have mode 0700. The exclusive lock
`database_directory/.telegram-reader.lock` has mode 0600 and lasts until
`authorizationStateClosed`. The CLI and daemon must use the same database
directory. Do not manually remove that lock while either process is running.

- `request(method, *, _timeout=None, **params) -> dict`: synchronous, at most eight native in-flight calls, including
  timed-out callers until TDLib replies or closes,
  exact read method allowlist. `_timeout` is local, never serialized to TDLib;
  it caps the whole authorization-wait/response wait to the smaller of the
  requested timeout and configured request timeout. No arbitrary TDLib transport
  is exposed to MCP.
- `wait_ready(timeout=None) -> True`: existing sessions only; fails with code 401
  when local login is required, without prompting or sending login data.
- `list_folders(*, _timeout=None)`: latest ordered `updateChatFolders` metadata snapshot; an empty
  snapshot can also mean no update received yet. Query live dynamic membership
  with `getChats(chat_list={"@type":"chatListFolder","chat_folder_id":id}, ...)`;
  `loadChats` is also allowlisted for bounded incremental list loading.
- `authorization_state`: current TDLib authorization type string, or `None`.
- `login() -> True`: explicit local terminal flow for phone/code/2FA, including
  email address/email code when requested by Telegram. Inputs use `getpass`.
  Unsupported registration, purchase, QR, and other states fail explicitly.
- `close()`: sends close and waits for `authorizationStateClosed`. A timeout or
  receiver failure retains locks because native code could still own the session;
  terminate/restart the process before retrying. Context manager exit calls close.
- `TdError`: `.code`, `.message`, and optional integer `.retry_after` (1..1209600 seconds). Arbitrary native/server error text is hidden;
  safe uppercase error identifiers may be preserved. Error 406 text is hidden.

On `authorizationStateWaitTdlibParameters`, the adapter sends the current flat
`setTdlibParameters` schema. Test DC, file/chat/message databases, and secret chats
are disabled. TDLib's own necessary authorization data may still be persisted.
Optional `message_unload_delay`, `ignore_inline_thumbnails`,
`disable_persistent_network_statistics`, and `disable_top_chats` settings are not
applied: no `setOption` path is exposed, and the adapter avoids additional
initialization requests whose failures could prevent startup. This reduces storage but does
not guarantee the native library's memory use on a 768 MB VPS.

Runtime calls do not open chats, mark messages read, download files, send messages,
join chats, or alter Telegram account settings. Reading through the official API
still involves Telegram network requests and server-side session activity.

The runtime allowlist is limited to `getChats`, `getChat`, `searchChats`,
`searchChatMessages`, `getChatHistory`, `getMessage`, `getOption`,
`getChatFolder`, and `loadChats`. Global message search, public lookup, and
identity lookup methods are excluded.

The deployment revision must match tool request schemas: the inspected schema's
`searchChatMessages` uses nullable `topic_id:MessageTopic`; `searchChats` supports
nullable `type_filter`; `getMessageLink` also includes `checklist_task_id` and
`poll_option_id`. Never mix undocumented request fields from another revision.

Run offline adapter tests with:

```sh
python -m unittest discover -s tests -p test_tdlib.py -v
```
