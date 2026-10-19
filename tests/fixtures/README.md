# Local SSE TLS fixture

The certificate/key pair in this directory is synthetic test data for the loopback HTTPS origin used by `test_core.py`. The private key is public fixture material, not a service credential. The client trusts this certificate only in that test opener, keeps hostname verification enabled, and uses the certificate for `sse-fixture.invalid`; the proxy tunnels only to the loopback origin.

`test_profile_runner/` contains static synthetic Python test projects copied into temporary workspaces by `test_process_runner.py`. They cover successful and failing results, output bounds/redaction, ordinary child-process timeout/cancellation, and Windows Job Object membership. The fixtures make no network requests and use no model API.
