# Exchange a one-time bootstrap token for a server-side Setup Session

On the first credential-free boot, Poppy atomically persists high-entropy bootstrap material in
its private volume and logs one complete setup URL. A valid, unexpired query token is consumed and
rotated atomically while a pending Secure, HttpOnly, SameSite session cookie is handed to the
browser. The first authenticated page request confirms the handoff, removes recoverable plaintext
tokens from durable state, and retains only the active session digest. If delivery is interrupted,
a restart logs the already-persisted recovery link while every older link remains invalid. Unsafe
setup requests additionally require the same public origin and a session-bound CSRF token. This
keeps setup state server-side, removes the presented secret from browser history, and lets partial
setup survive restarts without introducing an account system or database.
