# YCT distributed export (experimental)

The Cloudflare D1 database `yct-distributed-jobs` and Worker script `yct-dispatcher` were created. The Worker has **no public workers.dev endpoint enabled**, and the authentication secret is not configured. The standalone executor is source code only; it is not a deployed background service.

## Required before production

1. Configure the `DISPATCH_TOKEN` Cloudflare Worker secret securely and enable a protected workers.dev endpoint or private service route.
2. Set `YCT_DISPATCH_URL` and `YCT_DISPATCH_TOKEN` on each independent Python executor service; supply existing YCT and Telegram credentials securely.
3. Integrate the website's category export UI with POST /jobs and GET /jobs/:id.
4. Add durable Telegram upload idempotency, especially for lease expiry after a successful Telegram upload but before completion acknowledgment.
5. Add rate-limit-aware global concurrency, cancellation, dead-letter handling, and integration tests.
6. Test a small book and verify exact page count and Telegram delivery before switching traffic.

Do **not** deploy several executors yet: a timed-out lease can currently cause duplicate Telegram uploads. Existing YCTDriver exports should remain on the original pipeline until these checks pass.
