# NoventaCommsApp (MVP)

## Start locally
```
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

## Run on Render (Native Python Web Service)
- Build Command: `pip install -r requirements.txt`
- Start Command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
- Add env vars: `OPENAI_API_KEY`, `BASE_PUBLIC_URL` (the Render URL), and social tokens if needed.
