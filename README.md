MuDaSiR VIP Allocator Bot
=========================
Set env vars in Heroku (Settings -> Config Vars):

- BOT_TOKEN         = <telegram bot token>
- BOT_OWNER         = MuDaSiR
- LOG_CHAT_ID       = <optional admin chat id>
- UPSTREAM_BASE     = http://mysmsportal.com
- LOGIN_FORM_RAW    = user=7944&password=10-16-2025%40Swi    (urlencoded)
- ALL_PATH          = /index.php?opt=shw_all_v2
- TODAY_PATH        = /index.php?opt=shw_sts_today
- LOGIN_PATH        = /index.php?login=1

Deploy:
- git init / add / commit / push to GitHub
- Connect repo to Heroku, enable worker dyno (scale worker to 1)
