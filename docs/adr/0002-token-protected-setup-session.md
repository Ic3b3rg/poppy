# Exchange a one-time bootstrap token for a server-side Setup Session

On the first credential-free boot, Poppy logs one high-entropy setup URL and persists only the
token digest in its private volume. A valid, unexpired query token is consumed atomically and
replaced by a Secure, HttpOnly, SameSite session cookie; unsafe setup requests additionally require
the same public origin and a session-bound CSRF token. This keeps setup state server-side, removes
the bootstrap secret from browser history after exchange, and lets partial setup survive restarts
without introducing an account system or database.
