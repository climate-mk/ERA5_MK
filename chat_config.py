import os

DIRECT_LINE_SECRET   = os.getenv("DIRECT_LINE_SECRET", "")
DL_GENERATE_URL     = "https://europe.directline.botframework.com/v3/directline/tokens/generate"
DL_REFRESH_URL      = "https://europe.directline.botframework.com/v3/directline/tokens/refresh"
TOKEN_CACHE_BUFFER  = 300   # seconds before token expiry at which the cached token is considered stale
                             # and a new one is fetched. Direct Line tokens expire after 1800s (30 min),
                             # so with a 300s buffer the cache is valid for ~25 min per session.

# Per-IP rate limits — applied per user (by IP address) via flask-limiter.
# Controls how often a single user can request a new chat token (i.e. open/reopen chat).
# Cached sessions are reused and do not count against these limits.
TOKEN_LIMIT_MINUTE = "4 per minute"
TOKEN_LIMIT_HOUR   = "20 per hour"

# Global caps — total new chat sessions site-wide, across all users combined (0 = unlimited).
# Counted in-memory; resets at the top of each clock hour / UTC midnight.
# Use these to control Azure Bot Service costs. Cached sessions are never counted.
CHAT_GLOBAL_HOURLY_LIMIT = 100
CHAT_GLOBAL_DAILY_LIMIT  = 300

# Error messages shown in the chat modal when token fetch fails
CHAT_ERROR_RATE_LIMIT   = "Chat is temporarily unavailable — too many requests. Please try again in a few minutes."
CHAT_ERROR_GENERIC      = "Chat is temporarily unavailable. Please try again later."
CHAT_ERROR_GLOBAL_LIMIT = "The chat assistant has reached its limit for now. Please try again in a little while."
