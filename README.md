# LuxAlgo PRO Railway deployment starter

This is a **read-only deployment shell**, NOT a completed live-trading bot. No Binance keys, no order handling, and no secret credentials. It is safe to put in a private GitHub repository as an initial deployment check.

Root files: `app.py`, `requirements.txt`, `Procfile`.

Railway deployment: upload all three root files to the private GitHub repository, commit, select the repository in Railway and deploy. The service should respond at `/health` with `orders_enabled: false`. Configure Railway networking/domain after deployment. Some environments may require explicitly setting the start command to `waitress-serve --host=0.0.0.0 --port=$PORT app:app`.

Do not switch on live execution until a separately reviewed and tested authenticated live engine is ready. Preserve the Windows paper bot and Telegram alerts.
