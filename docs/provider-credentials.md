# BYO provider credentials

Provider API keys are service credentials, not project data. AshatHub loads the encrypted `provider-master` credential through systemd `LoadCredentialEncrypted`; application code must use the credential path supplied by `CREDENTIALS_DIRECTORY` only to encrypt/decrypt provider payloads at runtime.

Keys must never be returned to Galileo, Alpha, Omega, Beta, Delta, browser storage, logs, Git, or API responses. AdminCP responses expose provider name, model, capabilities, status, and usage only.

The provider record stores an opaque credential reference and account ownership. Outbound provider calls resolve the reference inside AshatHub, apply account limits, and discard plaintext credentials after request completion.

Rotation: replace `/etc/ashat-ai/provider-master.cred`, restart the active provider service, and re-encrypt stored provider payloads through the administrative rotation procedure. Do not place plaintext keys in deployment files or shell history.
