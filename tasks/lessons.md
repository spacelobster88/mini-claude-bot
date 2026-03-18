# Lessons Learned

## Dashboard Data Architecture

### Push via API, not Vercel Edge Functions (2026-03-18)
- **Problem**: Mac mini dashboard was broken because the data flow relied on an approach that hit Vercel rate limits.
- **Root cause**: Vercel edge functions get rate-limited when called frequently (e.g., every minute for system metrics).
- **Solution**: The mac mini machine must **push data to the dashboard via API** (POST to a backend endpoint) on a cron schedule (every minute), rather than having the Vercel-hosted dashboard pull/compute data via edge functions.
- **Rule**: For any recurring data collection (metrics, health checks, system stats), always use a push model from the mac mini to the API. Never rely on Vercel edge solutions for high-frequency polling — they will get rate-limited.
