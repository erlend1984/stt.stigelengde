STT webhook backend — GitHub + Render ready
Denne mappen er klar til å pushes til GitHub og deployes på Render.
Innhold
`app/main.py` — FastAPI-backend
`Dockerfile` — Render bygger denne direkte
`render.yaml` — Render Blueprint
`.gitignore`
`GITHUB.md` — kommandoer for å opprette repo og pushe
`PLUGIN-CONNECTION.md` — eksakte WordPress-innstillinger
Slik får du den live
1. Opprett GitHub-repo
Følg `GITHUB.md`
2. Opprett Render service
Det enkleste er:
Logg inn på Render
Connect GitHub
Velg repoet
Render finner `render.yaml` i repo-roten
3. Fyll inn secret i Render
Når Render ber om `STT_WEBHOOK_SECRET`, lim inn en lang tilfeldig streng.
4. Deploy
Render bygger Docker-imaget og oppretter en web service.
5. Koble WordPress-pluginen
Følg `PLUGIN-CONNECTION.md`
Health check
Når deploy er ferdig, test:
`https://<din-service>.onrender.com/health`
Du skal få:
`{"status":"ok"}`
Merk
Denne versjonen bruker skjermdump av den offentlige Norge i bilder-løsningen for å vise flyfoto uten egen bildeleverandør.
