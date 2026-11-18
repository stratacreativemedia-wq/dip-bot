# dip-bot (paper trading)

Large-cap dip buyer running on GitHub Actions against an **Alpaca paper account**.

- Buys up to 2 of the 30 largest US companies after a sharp short-term dip (RSI(2) < 10) while they're above their 200-day average; sells on the bounce (price above 5-day average) or after 10 trading days.
- Idle cash sits in SPY while SPY is above its 200-day average.
- Runs on weekdays around 9:35am and 3:50pm New York time. See `.github/workflows/bot.yml`.
- Needs two repository secrets: `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY`.
- Refuses to run on anything but a paper account.

Manual run: **Actions → dip-bot → Run workflow** (pick `status` or a dry run).

Educational project, not investment advice.
