# Local SSE TLS fixture

The certificate/key pair in this directory is synthetic test data for the loopback HTTPS origin used by `test_core.py`. The private key is public fixture material, not a service credential. The client trusts this certificate only in that test opener, keeps hostname verification enabled, and uses the certificate for `sse-fixture.invalid`; the proxy tunnels only to the loopback origin.

`test_profile_runner/` contains static synthetic Python projects and a dependency-free Node built-in test copied into temporary workspaces by the test-runner tests and self-check. They cover successful and failing results, fixed-command argv/profile approval, output bounds/redaction, ordinary child-process timeout/cancellation, and Windows Job Object membership. The fixtures make no network requests and use no model API; Node tests require only the Node runtime, not npm or installed packages.

For fixtures whose expected value is raw bytes, a SHA-256, or an exact newline sequence, create the file with `write_bytes(text.encode("utf-8"))` or open it with `newline="\n"`. `Path.write_text()` uses platform text newline translation on Windows, so a source string containing `\n` can become CRLF bytes. Use ordinary `write_text()` only when the test intentionally relies on text-mode newline behavior; do not alter product newline handling to compensate for fixture setup.
