# Local SSE TLS fixture

The certificate/key pair in this directory is synthetic test data for the loopback HTTPS origin used by `test_core.py`. The private key is public fixture material, not a service credential. The client trusts this certificate only in that test opener, keeps hostname verification enabled, and uses the certificate for `sse-fixture.invalid`; the proxy tunnels only to the loopback origin.
