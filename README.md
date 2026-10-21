# fundedbot

Trading bot voor **MyFundedPerps** 1-Step **Signature** challenges, **alleen BTC**, via hun officiële API.

## Wat de bot doet

De bot werkt anders tijdens de challenge dan wanneer je funded bent. Hij leest de fase zelf uit de API.

| | Challenge (snel halen) | Funded (uitbetaling halen) |
|---|---|---|
| Strategie | Keltner 4h met trendfilter + Keltner 2h + Donchian 4h met trendfilter | Wide-range candle 4h (brede candle die dicht bij top/bodem sluit) |
| Risico per leg | 1,2% van de startbalans (de eerste leg wordt door de 40%-buffer begrensd op ~0,9%) | 0,8% |
| Stops | trailing (ATR) | trailing (ATR) |
| Einde | target (+10%) gehaald: alles sluiten, bot stopt | **+5%**: alles sluiten, jij vraagt de uitbetaling aan |

- **Meerdere "legs" in één BTC-positie.** Elke leg heeft een eigen stop en target. De exchange houdt per market maar één netto positie, dus:
  - een leg sluit alleen zijn eigen deel van de positie;
  - een leg opent nooit tegen de bestaande positie in;
  - op de server staat altijd één stop-loss voor de hele positie, op het gewogen gemiddelde van de leg-stops. Die stop gaat mee met elke instap-order (`stop_loss_price`, met `replace_overlapping_exits` als er al een positie staat). Trailing en het sluiten van één leg passen hem aan via `PUT /v1/positions/{id}/exit-orders`. Een losse reduce-only stop-order weigert MFP. Valt je pc uit, dan verlies je nooit meer dan alle leg-stops samen.
  - Lukt het niet om die stop te plaatsen, dan sluit de bot alles meteen. Een positie staat nooit onbeschermd open.
- **Risk-manager** (`fundedbot/risk.py`). Alle limieten worden afgeleid van de floors die de server zelf rapporteert:
  - Als 60% van de daily-loss-ruimte op is: alles sluiten en pauze tot middernacht New Yorkse tijd. **Er blijft dus altijd 40% van je daily loss vrij.**
  - Als 90% van de max-loss-ruimte op is: alles sluiten.
  - Elke nieuwe leg riskeert hooguit de helft van de ruimte die tot die grens over is. Alle stops samen kunnen die 60%-grens dus niet passeren.
  - Maximaal 5x totale leverage (BTC staat 10x toe) en maximaal 12 trades per dag.
  - Geen nieuwe trades bij een payout in behandeling, trading halt, reduce-only-restrictie, onvolledige marks, of een BTC-positie die niet van de bot is.
- **Gemiste signalen.** Sloot een candle terwijl de bot niet draaide (herstart, nieuwe deploy), dan neemt hij dat signaal alsnog zolang de volgende candle van die timeframe nog loopt, en alleen als de prijs minder dan één stopafstand verder staat. Elk signaal wordt maar één keer genomen.
- **Inactiviteitsregel.** Een account vervalt zonder trade binnen 30 dagen (challenge) of 7 dagen (funded). Is de strategie zo lang stil, dan doet de bot de kleinste mogelijke trade en sluit die meteen: na 25 dagen in de challenge, na 6 dagen funded.
- **Na de uitbetaling** ligt de max loss op je startbalans en is er geen ruimte meer. De bot handelt dan niet meer op dat account. Je koopt een nieuwe challenge.

Wat de bot **niet** kan: challenges kopen, uitbetalingen aanvragen en KYC. Dat kan alleen op de website.

## Wat je mag verwachten (simulatie)

Gesimuleerd met één netto positie per market, ~$10-20 slippage op BTC, 0,03% fee per fill en 0,03% swap per dag. Binnen een candle telt steeds het slechtste scenario. Train: 2021 tot juni 2024. Test: juli 2024 tot oktober 2026 (niet gebruikt om te kiezen).

- **Alleen legs met echte voorsprong.** Elke challenge-leg wint per trade na kosten, en doet dat in beide periodes beter dan 95% van dezelfde strategie met willekeurige instapmomenten (`research/edge_scan.py`). LuxAlgo RSI, SFP's en mean reversion haalden dat niet (`research/edge_scan2.py`).
- **Eén challenge** slaagt in ~51-54% van de keren, meestal in ~3-4 weken. Met willekeurige instapmomenten zou het ~34-35% zijn (`research/edge_combo.py`).
- **Funded** (+5% = $400 voor jou op $10K): wide-range candle 4h op 0,8%, met echte voorsprong per trade in beide periodes (+0,36R tegen +0,10R willekeurig, `research/edge_scan3.py`). Direct na het halen van de challenge betaalt hij in 78% (train) / 91% (test) van de funded accounts uit, na ~3-4 maanden (`research/funded_like_cycles.py`). SMC 4h en Keltner 4h trend vielen af. Elite (vaker kleine uitbetalingen) was sinds 2024 gemiddeld verliesgevend (`research/elite_sim.py`).
- **Met $200 budget** (`research/funded_search.py`):

| | Train | Test |
|---|---|---|
| Kans op een uitbetaling | 92% | 97% |
| Dagen tot de uitbetaling (mediaan, inclusief opnieuw kopen) | ~175 | ~140 |

- Simulatie per ronde sinds juli 2024 (`research/cycle_trades.py`): 93 van 101 afgeronde rondes uitbetaald, gemiddeld +$275 netto, mediaan ~4 maanden van start tot uitbetaling.

- Resultaten uit het verleden garanderen niets.

Alle onderzoeksstappen staan in `research/` en in `research/results/research_log.jsonl`.

## Starten (sandbox)

1. Ga naar https://myfundedperpetuals.com/settings?section=api-keys en maak een key aan: **Test (Sandbox)**, toegang *Read And Trade*.
2. `copy .env.example .env` en vul `FP_API_KEY=fp_test_...` in.
   Zoek het juiste account-ID met `python -m fundedbot accounts` (toont het nummer van de website, `FP-...`, naast het ID) en zet dat in `FP_ACCOUNT_ID`.
3. `pip install -r requirements.txt`
4. Eerst droog draaien, zonder orders: `python -m fundedbot --dry-run`
5. Daarna echt in de sandbox: `python -m fundedbot`
6. Je trades bekijken: `python -m fundedbot trades`

**Dashboard:** `python -m fundedbot dashboard` en open http://127.0.0.1:8787. Je ziet het account, hoeveel van je daily loss en max loss gebruikt is, de positie en de legs van de bot, de stop op de server, de signalen, de recente trades en het logboek. Met de knoppen start en stop je de bot (ook als dry-run), en de noodknop sluit alles. Het dashboard draait alleen op je eigen pc; de API-key gaat niet naar de browser. Sluit je het dashboard, dan stopt een bot die je daar gestart hebt ook.

Logs komen in `bot.log`, de status in `state.json` (bij een dry-run vanuit het dashboard: `state_dryrun.json`). Trade niet handmatig BTC op het account dat de bot gebruikt.

**Controleer in de sandbox eerst deze punten:**
- dat na een instap in `bot.log` staat: "gevuld ... stop op de server @ ...", en niet "KAN POSITIE NIET BESCHERMEN";
- dat het verschuiven van de stop (trailing) werkt: "beschermende stop op de server: ...";
- of fills en fees overeenkomen met de simulatie.

Na het halen van de challenge stopt de bot. Start hem opnieuw zodra je funded account actief is. Pas eventueel `FP_ACCOUNT_ID` aan.

Live gaat alleen met een `fp_live_`-key **én** de vlag `--live`. Laat de bot eerst in de sandbox draaien.

## Online (Railway)

De bot kan 24/7 op Railway draaien: het dashboard is dan een website met wachtwoord en houdt de bot zelf draaiende, ook na een crash of herstart. Stappenplan: [`deploy/RAILWAY.md`](deploy/RAILWAY.md). Een eigen Linux-server kan ook: `deploy/setup.sh` en `deploy/upload.ps1`.

De bot schrijft elk uur een "Hartslag" in het logboek, houdt een Windows-pc wakker zolang hij draait, en weigert te starten als er al een bot draait. Status, logboek en `bot.pid` staan in `FP_DATA_DIR` (standaard de huidige map).

## Tests

```
python -m pytest tests -q
```

De tests controleren onder meer:
- dat elke leg in de bot bar voor bar dezelfde signalen geeft als de backtest;
- dat de snelle simulator gelijk is aan de referentieversie, ook met netting en slippage;
- hoe de legs, de beschermende stop, de guards, de uitbetaling en de inactiviteitsregel werken tegen een nep-exchange.
