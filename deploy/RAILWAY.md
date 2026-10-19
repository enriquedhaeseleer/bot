# FundedBot op Railway zetten

Op Railway draait het **dashboard** als website (met wachtwoord) en dat dashboard houdt de **bot** draaiende: het start hem vanzelf, ook na een crash of een herstart. De status en het logboek staan op een **Volume**, zodat ze een herstart overleven.

> **Nooit twee bots op hetzelfde account.** Stop eerst de bot op je pc (Ctrl+C in dat venster, of sluit het) voordat je hem op Railway aanzet.

## Via de website (zonder terminal)

Railway bouwt de bot dan vanuit een privé GitHub-repo. Alles kan in je browser.

**A. Code op GitHub zetten**
1. Maak een account op https://github.com (als je er nog geen hebt).
2. Klik rechtsboven op **+** → **New repository**. Naam: `fundedbot`. Kies **Private**. Klik **Create repository**.
3. Klik op de pagina die verschijnt op **uploading an existing file**.
4. Open in Verkenner de map **`C:\Users\enriq\fundedbot_upload`**, selecteer **alles wat erin staat** (Ctrl+A) en sleep het naar GitHub. Wacht tot alles geüpload is en klik **Commit changes**.
   In die map zit géén `.env`: je API-key komt nooit op GitHub.

**B. Op Railway zetten**
1. Ga naar https://railway.com, log in met GitHub en kies het **Hobby**-plan.
2. **New Project** → **Deploy from GitHub repo**. Geef Railway toegang tot de repo `fundedbot` en kies hem.
3. Railway ziet de `Dockerfile` en `railway.json` en begint te bouwen. De eerste keer mag het mislukken: de variabelen staan er nog niet in.
4. Ga verder bij **3. Instellingen in Railway** hieronder (variabelen, Volume op `/data`, domein), en klik daarna op **Deploy**.

**Bijwerken**: upload de gewijzigde bestanden opnieuw op GitHub (zelfde manier). Railway bouwt en herstart dan vanzelf.

---

Liever met de terminal? Dan kan het ook zo:

## 1. Account en tool

1. Maak een account op https://railway.com (inloggen met GitHub kan).
2. Kies het **Hobby**-plan (~$5 per maand, daar zit $5 verbruik in; deze bot gebruikt weinig).
3. Installeer de Railway-tool op je pc. Je hebt daarvoor Node.js nodig (https://nodejs.org). Daarna in PowerShell:
   ```
   npm i -g @railway/cli
   ```

## 2. Project aanmaken en uploaden

In PowerShell, in de map `C:\Users\enriq\fundedbot`:
```
railway login
railway init
railway up
```
- `railway init`: kies "Empty project" en geef het een naam, bijvoorbeeld `fundedbot`.
- `railway up` uploadt de code en bouwt hem met de `Dockerfile`. Je `.env` en de onderzoeksmap gaan **niet** mee (zie `.railwayignore`).
- De eerste keer mag het mislukken: de variabelen staan er nog niet in.

## 3. Instellingen in Railway (op de website)

Open je project op railway.com en klik op de service.

**Variables**: voeg toe:

| Naam | Waarde |
|---|---|
| `FP_API_KEY` | je key (`fp_test_...` voor de sandbox) |
| `FP_ACCOUNT_ID` | het ID van het account (zie `python -m fundedbot accounts`) |
| `DASHBOARD_PASSWORD` | een sterk wachtwoord voor het dashboard |

`FP_DATA_DIR=/data` en `FP_AUTOSTART=1` staan al in de Dockerfile.

**Volume**: klik met rechts op de service (of gebruik "+ New" → Volume), koppel het aan de service met mount path **`/data`**.

**Networking**: Settings → Networking → **Generate Domain**. Je krijgt een adres zoals `https://fundedbot-production.up.railway.app`.

Laat **Replicas op 1** staan (dat staat ook in `railway.json`).

## 4. Starten en controleren

1. Klik op **Deploy** (of draai `railway up` opnieuw).
2. Open je Railway-adres. Je browser vraagt een gebruikersnaam en wachtwoord: de naam maakt niet uit, het wachtwoord is `DASHBOARD_PASSWORD`.
3. Bovenaan moet na ongeveer een minuut staan: **bot draait (sandbox)**.
4. Elk uur verschijnt in het logboek een regel "Hartslag". Zo zie je dat hij leeft, ook als er geen trades zijn.

## Dagelijks gebruik

- **Stoppen**: knop *Stoppen* in het dashboard. Hij blijft dan gestopt, ook na een herstart van Railway.
- **Weer starten**: knop *Bot starten*.
- **Noodknop**: typ `SLUIT` en klik *Alles sluiten*: bot stopt, alle orders en posities gaan dicht.
- **Code bijgewerkt?** Draai `railway up` opnieuw in de map `fundedbot`.
- **Logboek in Railway**: de service → Deployments → View logs (daar zie je ook de meldingen van de watchdog).
- **Ander account** (bijvoorbeeld je funded account): pas `FP_ACCOUNT_ID` aan. De bot begint dan zelf met een schone status.
- **Later live**: zet `FP_API_KEY` op je `fp_live_...` key én voeg de variabele `FP_LIVE=1` toe. Test eerst lang genoeg in de sandbox.

## Veiligheid

- Je API-key staat alleen in de Railway-variabelen, niet in de code.
- Iedereen met het dashboardwachtwoord kan de bot stoppen en posities sluiten: deel het niet en kies een sterk wachtwoord.
- Gebruik een eigen account: bij een echte challenge of funded account mag meestal niemand anders dan de eigenaar handelen.
