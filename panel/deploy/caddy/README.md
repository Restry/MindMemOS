# Public HTTPS access for the MindMemOS Panel

The LAN Panel continues to bind only to its configured LAN address/port. Public access is provided by the existing infrastructure:

```text
Internet
  -> public FRP server
  -> host 235 frpc HTTP/HTTPS virtual-host routes
  -> host 235 Caddy
  -> 192.168.1.235:8666
```

Production URL:

```text
https://mindmemos.nexora.restry.cn
```

Security controls:

- HTTPS certificate managed by Caddy/Let's Encrypt;
- HTTP Basic Authentication at Caddy, before any Panel page or API;
- HSTS, `nosniff`, `no-referrer`, and `noindex` response headers;
- the plaintext credential is not stored in Git;
- production credential file: `/home/claw/.hermes/mindmemos_panel_access.json`, mode `0600`;
- Caddy stores only a bcrypt password hash.

Install the Caddy snippet from `mindmemos-panel.caddy.example`, replacing
`<BCRYPT_HASH>` with the output of `caddy hash-password`. Add the domain to both
the HTTP and HTTPS `customDomains` arrays in `/etc/frp/frpc.toml`, validate both
configurations, restart frpc, then reload Caddy.

Important: production Caddy currently uses a single-file Docker bind mount. Do
not atomically replace the host Caddyfile and assume the running container sees
the new inode. Either restart/recreate the Caddy container after replacing the
host file, or update the mounted inode and then call `caddy reload`. Always
verify that the host and in-container Caddyfiles have identical hashes.

Verification contract:

```text
unauthenticated /                           -> 401
wrong credentials /                        -> 401
unauthenticated /api/recall-evaluations    -> 401
authenticated /                            -> 200
authenticated /api/recall-evaluations      -> 200 JSON with ok=true
certificate SAN                            -> mindmemos.nexora.restry.cn
```

Never commit the credential JSON, plaintext password, bcrypt production hash,
FRP token, or complete production Caddyfile.
