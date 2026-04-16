# Procfile — Railway Service Definitions
#
# Deploy each line as a separate Railway service (Cron Job type).
# Set the schedule in Railway dashboard for each service.
#
# Service 1: FII/DII Daily Collector
#   Schedule: 0 10 * * 1-5   (4:00 PM IST = 10:30 UTC weekdays)
#   Command:  python collector.py
#
# Service 2: News Sentiment Scanner
#   Schedule: 30 2 * * 1-5   (8:00 AM IST = 2:30 UTC weekdays)
#   Command:  python news_sentiment.py
#
# Service 3: Policy Scraper (RBI + PIB)
#   Schedule: 0 1 * * 1       (7:00 AM IST = 1:00 UTC Mondays)
#   Command:  python policy_scraper.py
#
# Service 4: LLM Synthesiser
#   Schedule: 30 2 * * 1      (8:00 AM IST = 2:30 UTC Mondays)
#   Command:  python llm_synthesiser.py
#
# Service 5: Alert Tracker (runs every 30 min during market hours)
#   Schedule: */30 3-10 * * 1-5  (9:00 AM–4:00 PM IST = 3:30–10:00 UTC)
#   Command:  python tracker.py
#
# Service 6: Monthly Screener (1st of each month)
#   Schedule: 30 2 1 * *      (8:00 AM IST = 2:30 UTC, 1st of month)
#   Command:  python screener.py

collector: python collector.py
news: python news_sentiment.py
policy: python policy_scraper.py
synthesiser: python llm_synthesiser.py
tracker: python tracker.py
screener: python screener.py
