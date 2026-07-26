<!-- Converted from the original Hungarian roadmap:
     "Teljes körű, handoff-kész megvalósítási útiterv egy GEX-alapú,
      teljesen automatizált futures kereskedési bothoz" (.docx).
     Text is the author's; only the structure was converted to Markdown so
     coding agents can read it. Formulas rendered as images in the original
     appear as empty lines -- see docs/specs/gex-engine.md for the formulas
     as implemented. -->

# Teljes körű, handoff-kész megvalósítási útiterv egy GEX-alapú, teljesen automatizált futures kereskedési bothoz


## Vezetői összefoglaló

Ez a riport egy olyan, mérnököknek és kódoló ügynököknek átadható megvalósítási tervet ad, amelynek központi eleme egy SPX/SPXW opciós adatokból számolt GEX-motor, és amely MES vagy ES futures instrumentumokon hajt végre ügyleteket. A javasolt első éles verzióban a jelképzés alapja SPX/SPXW, a végrehajtási instrumentum pedig MES, mert a MES kontraktusmérete az ES tizede, a multiplikátora $5 × S&P 500 index, minimális tickje 0,25 indexpont, így egy tick értéke $1,25; ezzel szemben az ES multiplikátora $50 × index, ugyanilyen 0,25 pontos tick mellett $12,50 tickértékkel. Az SPX/SPXW oldalon a Cboe szerint a konstrukció európai típusú és cash-settled, a standard SPX és a PM-settled SPXW pedig eltérő lejárati/elszámolási tulajdonságokat ad, amelyek intraday GEX-modellezésnél kifejezetten fontosak. [1]

A rendszer minimálisan négy adatpillérre épül: opciós snapshotok, open interest, Greeks/IV, valamint futures intraday ár- és likviditási adatok. A publikus OI önmagában nem elég, mert az OCC szerint az open interest értékek a megelőző napi settlementből képződnek, vagyis intraday környezetben szükségszerűen késnek. Ez különösen lényeges azért, mert a Cboe szerint 2025-ben az SPX forgalmának átlagosan 59%-át 0DTE opciók adták, vagyis a napközbeni flow erősen módosíthatja ugyanazon nap várható dealer-hedge dinamikáját. Ezért az első verzióban a GEX nem lehet önálló belépési trigger; rezsim-osztályozóként és szinttérképként kell működnie, amelyet ár-, volumen-, VWAP- és volatilitás-feature-ökkel kell megerősíteni. [2]

A preferált adatkombináció ehhez a feladathoz a következő: ThetaData az olcsóbb és fejlesztőbarát opciós snapshot/Greeks hozzáféréshez; Cboe DataShop a magasabb minőségű, auditálható történeti validációhoz és intraday Open-Close/flow jellegű adatokhoz; Databento a futures oldali kutatási és live adatokhoz; IBKR az első élő végrehajtási réteghez, papírszámla-támogatással és API-val. Ugyanakkor a licencelés kritikus: az OPRA dokumentumai szerint az automatizált, nem-kijelző jellegű OPRA-adatfelhasználás Non-Display Use kategóriába eshet, és ide tartozhat a „black box” vagy algoritmikus/trading engine használat is; a díjazás és a konkrét kötelezettségek ehhez igazodnak. Ezt még a live indulás előtt írásban kell tisztázni a konkrét vendorral. [3]

Akadémiai oldalról a GEX-alapú gondolatmenet reális, de nem bizonyított pénzautomata: több kutatás szerint a nettó gamma-pozicionálás kapcsolatban állhat a realizált volatilitással, az autokorrelációval és a piaci mikrostruktúrával; negatív dealer gamma környezetben a hedge-áramlások pro-ciklikusak lehetnek, pozitív környezetben pedig tompíthatják a mozgást. Ez azonban nem jelenti azt, hogy a publikus GEX-becslés minden napon vagy minden instrumentumban kereskedhető élre fordítható. A megvalósításnak ezért determinista szabályalapú stratégiát, pont-in-time backtestert, replay teszteket, risk engine-t, reconciliationt és működési monitoringot kell elsőbbségbe helyeznie. [4]


## Kiinduló feltételezések és pontos célállapot

A következő riport mérnöki alapfeltevéseket használ ott, ahol a felhasználó nem adott konkrét specifikációt. Ezek nem piaci tényállítások, hanem tervezési feltételezések.

| Terület | Alapfeltevés |
|---|---|
| Alapszámla méret | 50 000 USD kutatási / paper induló keret |
| Első live méret | 1 db MES kontraktus maximum |
| Késleltetési budget | Bar-alapú stratégia esetén end-to-end < 1 s célzott SLA |
| Döntési frekvencia | 1 perces adatból 5 perces döntési ciklus |
| Kereskedési ablak | Első verzióban csak U.S. regular cash session környéke |
| Overnight | Tiltott |
| Makroesemény-kezelés | High-impact események körül no-trade ablak |

A precíz célállapot nem „GEX-ből automatikusan long/short jelet adó bot”, hanem egy olyan rendszer, amely:

- SPX/SPXW láncból többféle GEX-variánst számol.
- A GEX-et rezsimosztályozóvá alakítja.
- Ezt kombinálja VWAP, realizált volatilitás, volumen, opening range, session context és opcionálisan intraday flow feature-ökkel.
- A jelekből csak a risk engine jóváhagyása után keletkezhet végrehajtható order.
- A végrehajtás eleinte MES-en történik, később skálázható ES-re. Az MES kisebb névértéke és kisebb tick-költsége miatt alkalmasabb az első live inkrementumhoz, míg az ES a skálázási fázisban relevánsabb. [5]
A javasolt instrumentum- és szerepkiosztás:

| Instrumentum | Elsődleges szerep | Miért ezt? | Megjegyzés | Forrás |
|---|---|---|---|---|
| SPX | Strukturális GEX és hosszabb lejáratú falak | Európai típusú, cash-settled, standard S&P 500 index opció | AM-settled standard sorozatok saját viselkedést adnak | [6] |
| SPXW | Intraday és 0DTE GEX, lejárati falak | PM-settled weekly/month-end szerkezet, expiring-day kereskedés | Intraday GEX-hez kulcsfontosságú | [7] |
| MES | Első élő execution instrumentum | $5 szorzó, 0,25 tick, $1,25/tick | Kisebb kockázati lépcső | [8] |
| ES | Későbbi skálázási execution instrumentum | $50 szorzó, nagyobb likviditás, nagyobb P&L-érzékenység | Csak robusztus live után | [9] |

Az SPXW kiválasztása intraday GEX-hez különösen indokolt, mert a Cboe szerint a PM-settled SPXW sorozatok az expiráció napján 16:00 ET-ig kereskednek, és az SPX-komplexumon belül a daily expiries/0DTE használat rohamosan nőtt. A standard SPX és a weekly SPXW tehát nem felcserélhető adatforrás egy intraday rezsimbot számára. [10]


## Adatigény, szolgáltatók és licencelési kockázatok

A rendszerhez szükséges minimum adatmodell a következő:

| Adatcsoport | Kötelező mezők | Mihez kell? | Preferált forrás |
|---|---|---|---|
| Opciós quote snapshot | timestamp, expiry, strike, call/put, bid, ask, bid/ask size, last, volume | láncfagyasztás, mid, spread, falak | ThetaData vagy Cboe Option Quotes |
| Open interest | OI, previous OI | GEX-súlyozás, struktúra | ThetaData / OCC / Cboe |
| Calcs/Greeks | IV, delta, gamma, theta, vega | GEX, zero-gamma, feature-k | ThetaData vagy Cboe Calcs |
| Intraday flow | buy/sell, open/close, participant type, interval volume | signed-GEX javítás, flow override | Cboe Open-Close |
| Futures bars | trade, bid/ask, OHLCV, contract, depth opcionálisan | execution, VWAP, vol breakout | Databento, másodlagosan IBKR |
| Index spot / reference | SPX index value, VIX opcionálisan | gamma-számítás, rezsim | ThetaData index feed / Cboe index adatok |

A szolgáltatói összehasonlítás a jelenleg publikusan elérhető dokumentáció alapján:

| Szolgáltató | Mit ad jól? | Gyenge pont | Publikus árszint | Ajánlott szerep | Forrás |
|---|---|---|---|---|---|
| ThetaData | U.S. opciós lánc, quotes, trade, Greeks, IV, index adatok; Options Standard csomagban 8 év történet, real-time, option chain snapshots, NBBO; a vendor szerint Black-Scholes alapú Greeks | A FAQ szerint jelenleg nincs CME futures adat, tehát futures execution-kutatáshoz külön vendor kell | Options Value $40/hó, Standard $80/hó, Pro $160/hó | Fejlesztés, napi kutatás, olcsóbb opciós motorréteg | [11] |
| Cboe DataShop | Option Quotes intervalok, optional Greeks/OI; Open-Close participant/action/open-close bontással; erős történeti validáció | Drágább, sok termék külön vásárolandó; egyes adatok exchange-scope-osak | All Access API publikus induló árszint $2,499/hó, magasabb tier $4,599/hó; számos histórico termék selection-based | Auditálható történeti kutatás, intraday flow, prémium validáció | [12] |
| Databento | CME futures és más venue-k live/historical, több schema, continuous symbology, usage-based + subscription modell | Az opciós GEX-hez önmagában nem elég; kész GEX-et nem ad | Használat-alapú; CME Standard publikus blog szerint $199/hó, Plus $1,750/hó, Unlimited $4,500/hó | Futures bars, order book, contract mapping, live execution research | [13] |
| IBKR | Broker API, paper account, futures execution, market data elérés | Full-chain SPX GEX-re kényelmetlenebb: subscription kell az underlyingre és a derivative-re is, market data usernévhez kötött, Web API pacing 10 req/s | Micro futures commissions publikus oldalon $0.10–$0.25/contract + exchange/reg fees tartományként jelenik meg; egyéb költségek csomagfüggők | Első execution layer és paper/live broker | [14] |

A Cboe Option Quotes adatcsomag 1 perces vagy egyedi N-perces összesítéseket ad, NBBO-val, és opcionálisan implied volatility + Greeks + open interest mezőkkel; a Cboe Option Trades külön tradeszintű adatokat ad, hozzáadható Calcs mezőkkel. Az Open-Close különösen értékes, mert résztvevő-típus, buy/sell és open/close bontást ad intraday 1 perces vagy 10 perces aggregációban, bár fontos korlát, hogy ez a Cboe exchange-ekre épül, és az iparági OI ott value-add, best-effort jellegű mezőként szerepel. [15]

A Databento futures oldalon azért erős jelölt, mert támogat continuous contract symbologyt, különböző data schema-kat, live és historical lekérést, és a dokumentáció külön példákat ad a futures contract-expiry kezelésére, parent symbologyra és napi statisztikákra, köztük open interest és settlement lekérésére is. Egy intraday futures-botnál ez jelentősen egyszerűsíti a contract-roll és backtest/live egységességet. [16]


### Licencelés és OPRA non-display figyelmeztetés

Az OPRA dokumentáció szerint a Non-Display Use magában foglalhatja az olyan felhasználást, ahol az OPRA adatot egy adatvevő rendszer megjelenítésen túli célból dolgozza fel vagy fogyasztja; a példák között szerepel az automated trading, az algoritmikus order generation, a price referencing és a „black box” jellegű trading engine is. Magyarul: ha a robot OPRA-adatot fogyaszt és abból automatikusan döntést vagy végrehajtást generál, az nagyon könnyen non-display kategóriába eshet. Az OPRA Fee Schedule külön non-display díjstruktúrát tartalmaz; vannak korlátozott kivételek bizonyos egyfelhasználós, saját számlás esetekre, de ezek alkalmazhatóságát nem szabad feltételezni egy futures-bot esetében sem. Ezt a konkrét vendorral, és szükség esetén jogi megfelelőségi oldalról is külön meg kell erősíteni még a live előtt. [17]

A Databento live data oldal szintén jelzi, hogy sok kereskedelmi felhasználónak közvetlen venue-licenc vagy licenckérdőív szükséges lehet a valósidejű adatokhoz. Az IBKR-nél pedig a market data subscription felhasználónévenként van kezelve, és az API-n keresztüli market data ugyanúgy subscriptionhöz kötött, nem tekinthető automatikusan problémamentes, „ingyenes” feednek. [18]


## GEX-motor, feature engineering és rezsimosztályozás specifikációja

A GEX-számításban a legnagyobb gyakorlati hiba az, amikor a rendszer egyetlen „GEX számot” tekint végső igazságnak. A handoff-kész specifikáció szerint legalább öt külön GEX-nézetet kell képezni, és ezeket egységes gex_snapshot objektumba kell menteni. Erre azért van szükség, mert a dealer-hedging hatás irodalma szerint a gamma-pozicionálás és a volatilitás/momentum kapcsolata valódi lehet, de a publikus adatokból a dealer tényleges inventoryja csak becsülhető; ezért a robusztus rendszernek egyszerre kell kezelnie a struktúrát, a naiv signed proxyt és a bizonytalanságot. [4]


### GEX-változatok és képletek

A használt jelölések:

- : az -edik opció gammaértéke
- : open interest
- : kontraktus multiplikátor
- : spot/index szint
- : vizsgált spotváltozás; 1%-os konvenció esetén
A vendor-Greeks preferred megközelítés szerint a ThetaData/Cboe által adott gamma mezőt első körben át kell venni, mert ezek a szolgáltatók már számolnak IV-t és Greekset; a dokumentáció szerint a ThetaData a Greekseket Black-Scholes módszertan alapján számolja. Ezzel párhuzamosan azonban szükség van egy shadow pricing engine-re, amely legalább a zero-gamma grid újraárazásához képes saját görögöket számolni. [19]

Az első kötelező változat az unsigned gamma concentration:

Ez nem állít elő dealer-irányt, csak megmutatja, hogy hol koncentrálódik a gamma. Intraday szinttérképezéshez ez a legkevésbé modellérzékeny nézet.

A második a naiv signed GEX, egy dokumentált sign-koncepcióval:

Ez a klasszikus publikus GEX-proxy, de nem dealer inventory truth. Az Open-Close vagy trade classification adatok ezt csak javíthatják, de teljes bizonyosságot nem adnak. A Cboe Open-Close azért hasznos, mert résztvevő-bontást, buy/sell és open/close irányt ad, tehát lehetőséget teremt egy második, flow-adjusted signed modell felépítésére. [20]

A harmadik kötelező csoport az expiry-bucket GEX:

A következő bucketek legyenek kötelezően külön kezelve:

- 0DTE
- 1_2_DTE
- 3_5_DTE
- 6_30_DTE
- GT_30_DTE
Erre azért van szükség, mert a SPX 0DTE volumensúlya már olyan nagy, hogy a teljes lánc aggregátuma könnyen elrejtheti, ha a napi flow és a hosszabb lejáratú struktúra egymással ellentétes irányba mutat. [21]

A negyedik kötelező réteg a strike-level GEX:

Ebből származnak:

- call_wall
- put_wall
- largest_abs_gamma_strike
- positive_gamma_nodes
- negative_gamma_nodes
- gamma_voids
A falakat nem pusztán a legnagyobb call/put OI alapján kell kiválasztani, hanem a strike-aggregált gamma vagy gamma-koncentráció szerint.

Az ötödik kötelező változat a zero-gamma grid. Itt a rendszer egy $S^\*$ spot-rács mentén újraszámolja az opciók gammaértékét és az aggregált signed GEX-et:

$$

    Total\_SGEX(S^\*) = \sum_i SGEX_i(S^\*)

    $$

Majd ott keres gyököt, ahol Total_SGEX(S*) előjelet vált. Az algoritmus:

- Definiálj egy szimmetrikus spot-gridet, például  aktuális spot körül.
Minden rácsponton repricing:

vendor IV befagyasztva, vagy

- sticky-strike, vagy
- sticky-delta, vagy
- felület-rekonstruált IV.
- Számold újra $ \Gamma_i(S^\*) $-t.
- Aggregálj signed GEX-et.
- Interpoláld a legközelebbi előjelváltás környékét.

### Tesztelendő volatilitási konvenciók

A zero-gamma szint erősen modellérzékeny. A minimum tesztkészlet:

| Konvenció | Definíció | Használat |
|---|---|---|
| frozen_iv | minden kontraktus a pillanatnyi IV-jével újraárazva | baseline |
| sticky_strike | a strikehoz kötött IV változatlan marad | javasolt elsődleges kutatási kezdőpont |
| sticky_delta | delta-sík szerinti IV-konzisztencia | fejlettebb opciófelület |
| surface_refit | teljes felület újrabecslése a grid minden pontján | csak későbbi fázisban |

A kézbesíthető első verzióban a sticky_strike legyen az alapértelmezett kutatási konvenció, de a backtester kötelező kimenete legyen a másik három konvencióval kapott zero-gamma eltérés is. Az eltérés nagysága a confidence score része.


### Kötelező feature-készlet

A GEX önmagában nem elég. A feature-store minimum mezői:

- spot_to_zero_gamma_distance_pct
- spot_to_call_wall_distance_pct
- spot_to_put_wall_distance_pct
- intraday_vwap_distance
- opening_range_break_state
- realized_vol_short
- realized_vol_medium
- bar_volume_zscore
- futures_basis_proxy
- bucket_gex_ratio_0dte_vs_rest
- flow_adjusted_put_call_pressure
- gex_stability_score

### Confidence score és rezsimosztályozó

A rezsimkimenetek legyenek fix, zárt enumerációk:

```
POSITIVE_GAMMANEGATIVE_GAMMANEUTRALUNCERTAINDATA_HALTRISK_HALT
```

A confidence_score 0–100 skálán számolódjon az alábbi komponensekből:

| Komponens | Leírás | Küszöb |
|---|---|---|
| chain_completeness | mennyi strike/expiry hiányzik | UNSPECIFIED_CALIBRATE |
| quote_freshness | opciós snapshot késése másodpercben | UNSPECIFIED_CALIBRATE |
| oi_freshness | OI életkora; minimum T-1 settlement tudott | Tény: előző napi settlement-alapú [22] |
| crossed_market_penalty | crossed/locked quote-ok aránya | UNSPECIFIED_CALIBRATE |
| zero_gamma_stability | zero-gamma változása eltérő IV-konvenciók között | UNSPECIFIED_CALIBRATE |
| sign_model_agreement | naiv signed vs flow-adjusted signed eltérése | UNSPECIFIED_CALIBRATE |
| 0dte_dominance_alert | ha 0DTE bucket dominál és flow erős | UNSPECIFIED_CALIBRATE |
| vendor_lag_alert | feed-vendor timestamp drift | UNSPECIFIED_CALIBRATE |

A pontos rezsimküszöbök explicit konfigurációs változók legyenek, és ahol nincs robusztus out-of-sample alátámasztás, ott UNSPECIFIED_CALIBRATE jelölést kell kapjanak. Példa:

```
regime:  positive_gex_z_min: UNSPECIFIED_CALIBRATE  negative_gex_z_max: UNSPECIFIED_CALIBRATE  min_confidence_for_trade: UNSPECIFIED_CALIBRATE  max_quote_staleness_sec: UNSPECIFIED_CALIBRATE  max_zero_gamma_shift_pct: UNSPECIFIED_CALIBRATE  max_pre_event_window_min: UNSPECIFIED_CALIBRATE  min_reward_to_risk: UNSPECIFIED_CALIBRATE
```

Javasolt logika:

- POSITIVE_GAMMA, ha Total_SGEX_z >= positive_gex_z_min, a spot nem szakít át nagy erejűen lefelé, és a confidence magas.
- NEGATIVE_GAMMA, ha Total_SGEX_z <= negative_gex_z_max, a realized vol emelkedik, és törés/breakout igazolódik.
- NEUTRAL, ha az aggregátum a nullához közel van.
- UNCERTAIN, ha a modellek eltérnek, az adatok frissek ugyan, de a szignál nem konzisztens.
- DATA_HALT, ha feed- vagy időbélyeg-hiba van.
- RISK_HALT, ha a risk engine stopot aktivált.

## Konkrét stratégiák, risk engine és végrehajtási logika

A két kötelező első stratégia: pozitív GEX mean reversion és negatív GEX momentum. A szakirodalom és ipari gyakorlat alapján ez a leginkább védhető kettősség, mert a negatív gamma környezethez gyakrabban társítanak magasabb realized volatilitást és pro-ciklikus hedge-flowt, míg a pozitív gamma környezethez tompító hatást. Ettől még a konkrét belépési szabályok kutatási kérdések maradnak. [23]


### Pozitív GEX mean reversion stratégia

| Elem | Specifikáció |
|---|---|
| Rezsim | POSITIVE_GAMMA |
| Instrumentum | MES elsődlegesen |
| Setup | spot a zero-gamma felett vagy annak közvetlen közelében; price egy put wall / gamma node / alsó VWAP sáv felé húz |
| Belépési trigger | nem első érintésre; kell legalább egy visszazárás a szint fölé, vagy wick rejection, vagy rövidtávú momentum-forduló |
| Long entry | záróár vissza a trigger-szint fölé + bar volume nem extrém breakout jellegű + confidence ≥ küszöb |
| Short entry | call wall / felső VWAP sáv körül szimmetrikusan |
| Stop | a visszautasított szint túloldalán, UNSPECIFIED_CALIBRATE ATR/struktúra-távolsággal |
| Target 1 | session VWAP |
| Target 2 | legközelebbi domináns gamma strike |
| Time stop | ha UNSPECIFIED_CALIBRATE baron belül nincs mean reversion |
| Exit override | rezsim NEGATIVE_GAMMA-ba vált vagy confidence összeomlik |

A belépési logika konkrét, gépileg implementálható formában:

```
IF regime == POSITIVE_GAMMAAND confidence_score >= MIN_CONFAND price_location in {PUT_WALL_RETEST, LOWER_VWAP_BAND, GAMMA_NODE_LOWER}AND rejection_confirmed == TRUEAND breakout_volume_filter == FALSETHEN generate LONG_CANDIDATE
```

Ahol:

- MIN_CONF = UNSPECIFIED_CALIBRATE
- LOWER_VWAP_BAND definíciója például VWAP-tól mért standardizált eltérés
- rejection_confirmed legalább egy záródó gyertya, wick vagy mikrostruktúra-szintű visszafordulási feltétel
- breakout_volume_filter == FALSE azt jelenti, hogy nem trend-kitöréses, nem agresszív momentumkörnyezet

### Negatív GEX momentum stratégia

| Elem | Specifikáció |
|---|---|
| Rezsim | NEGATIVE_GAMMA |
| Instrumentum | MES, később ES |
| Setup | spot a zero-gamma alatt shorthoz vagy fölötte longhoz; realized vol emelkedik; opening range vagy gamma-fal törik |
| Belépési trigger | záróár szint fölé/alá kerül, majd follow-through vagy failed retest |
| Short entry | lefelé törés a zero-gamma / put-wall környezetből, VWAP alatt, forgalmi megerősítéssel |
| Long entry | felfelé törés call-wall/zero-gamma visszahódítás után |
| Stop | a megtört szint túloldalán, struktúra vagy ATR alapján |
| Primary target | következő gamma node / fal |
| Secondary exit | trailing stop |
| Exit override | price visszamegy a breakout-szint mögé vagy rezsim semlegesedik |

Algoritmikusan:

```
IF regime == NEGATIVE_GAMMAAND confidence_score >= MIN_CONFAND breakout_state == CONFIRMEDAND bar_volume_zscore >= MIN_BREAKOUT_VOL_ZAND price_vs_vwap confirms directionTHEN generate MOMENTUM_CANDIDATE
```

A MIN_BREAKOUT_VOL_Z és a trailing mechanika szintén UNSPECIFIED_CALIBRATE.


### Méretezés képlete és kockázatkezelés

MES esetén a pontérték $5/pont, ES esetén $50/pont. A pozícióméret-alapképlet:

ahol point_value = 5 MES-re és 50 ES-re. A minimum első live szabály: a fenti képlet eredményétől függetlenül cap = 1 MES. [5]

A risk engine legyen stratégiától független szolgáltatás, és soha ne engedje, hogy a stratégiamodul közvetlenül ordert küldjön a brokernek. A hard limitek:

- maximum egy nyitott pozíció
- maximum egy aktív irány
- averaging down tilos
- martingale tilos
- daily realized + unrealized loss cap
- heti drawdown cap
- max consecutive losses cap
- max entries/day
- no-trade macroablakok
- session end előtt kötelező flatten
- kill switch
A pre-trade checklist legyen explicit:

```
Data fresh?Options chain complete enough?Futures market open?Correct front contract selected?Broker connected?Local/broker positions match?No orphan order?Daily loss limit not breached?Macro no-trade window inactive?Spread acceptable?Expected reward/risk >= threshold?
```

Brokeroldali orderstruktúra első verzióban legyen bracket order, mert az IBKR dokumentációja kifejezetten támogatja a parent-child order modelleket, és a bracket természetes módon tartalmaz stopot és profit-take-et. A child orderek csak a parent teljesülése után aktiválódnak, és az egyik triggerelése esetén a másik törlődik. [24]


## Pont-in-time backtester, replay és kutatási kapuk

A validációs réteg célja nem pusztán az, hogy „visszatesztelje” a stratégiát, hanem hogy bizonyítsa: a rendszer pont-in-time konzisztens, nincs look-ahead bias, és a live-ban elvárható működési hibamódokat is kezeli. Erre azért van szükség, mert az opciós open interest T-1 settlementből származik, az intraday SPX/0DTE flow pedig ugyanazon a napon érdemben átírhatja a dealer hedge-környezetet. [2]


### Kötelező pont-in-time tervezési elvek

A backtester csak olyan adatot láthat, amely az adott időpillanatban valóban ismert volt:

- T napon 10:00-kor nem használható a T napi záró OI.
- 15:45-ös snapshot nem generálhat 10:00-s szignált.
- Később korrigált vendor-record nem írhatja felül a történeti truth setet az audit trailben.
- A front contract roll nem történhet utólagos „leglikvidebb kontraktus” tudással; a Databento continuous vagy saját roll-logika csak akkori információból dönthet. [25]
A futures execution szimuláció minimálisan modellezze:

- bid/ask spread
- legalább 1 tick adverse slippage stressz esetben
- broker + exchange + regulatory fees
- order queue késleltetés egyszerűsített modellje
- partial fill lehetősége
- stop gap
- contract roll
- holiday / early close
- időzóna- és DST-konverzió
A CME roll oldala szerint az equity index futures piacokon a „lead month” váltásának szokásos dátuma a lejárati hónap harmadik péntekét megelőző hétfő, de a kutatási motorban ezt nem szabad vakon beégetni; a tényleges volume/likviditás-váltást továbbra is ellenőrizni kell. [26]


### Kötelező replay és tesztkimenetek

A replay framework az egyik legfontosabb handoff-elem. Ugyanazon nap ugyanazon nyers inputjából bitre reprodukálható outputot kell adnia.

Kötelező replay tesztek:

- teljes session message-by-message újrajátszás
- feed-drop szimuláció
- opciós snapshot fagyás
- futures feed késés
- broker reconnect
- orphan/duplicate order
- restart nyitott pozíció mellett
- end-of-day flatten kényszer
- macro no-trade ablak aktiváció
Kötelező riportmetrikák:

| Metrika | Kötelező |
|---|---|
| Net P&L after costs | igen |
| Expectancy / trade | igen |
| Profit factor | igen |
| Win rate | igen |
| Avg win / avg loss | igen |
| Max drawdown | igen |
| Time under water | igen |
| Sharpe / Sortino | igen |
| Tail loss / worst day | igen |
| MAE / MFE | igen |
| Slippage sensitivity | igen |
| Paraméter-szenzitivitás | igen |
| Regime-by-regime bontás | igen |
| Time-of-day bontás | igen |
| Long/short bontás | igen |


### Walk-forward és minimum kutatási kapuk

A walk-forward kapuk explicit módon legyenek implementálva:

```
DEVELOPMENT -> VALIDATION -> OOS -> PAPER -> LIVE_STAGE_1 -> LIVE_STAGE_2
```

Kötelező minimum research gate-ek:

- pozitív out-of-sample expectancy költségek után
- pozitív vagy legalább nem összeomló eredmény megemelt slippage mellett
- nincs egyetlen hónap vagy egyetlen nap dominanciája
- nincs egyetlen mágikus threshold függés
- szomszédos paraméterértékek mellett is fennmaradó viselkedés
- eltérő volatilitási rezsimekben sem degradál nullára
- replay és backtest újrafuttatható ugyanarra a raw datasetre
- működési hibák nélkül legalább több tucat paper session
A makroesemény-no-trade ablakokhoz a legjobb eljárás hivatalos naptárakból dolgozni, például a Federal Reserve FOMC-kalendáriumából és a BLS CPI/Employment Situation publikációs naptáraiból. Ezek legalább azt biztosítják, hogy a bot az ismert, nagy hatású U.S. makroesemények előtt és körül determinisztikusan tiltson. [27]


## Infrastruktúra, szolgáltatások, repo-szerkezet és kézbesíthető mérföldkövek

Az ajánlott stack:

- Python az összes core szolgáltatáshoz
- Docker fejlesztési és deployment egységesítéshez
- PostgreSQL mint truth store és audit trail
- FastAPI admin, health, config és report endpointokhoz
- Grafana monitoringhoz
- Polars/pandas + NumPy/SciPy kutatási és számítási réteghez
- Alembic migrációkhoz
- pytest unit/integration/replay tesztekhez
Ez a választás technológiailag konzervatív, de erősen automatizálható és jól átadható kódoló ügynököknek.


### Ajánlott szolgáltatás-szétválasztás

*[A diagram appeared here in the original .docx as an embedded image and
did not survive text extraction. The surrounding prose describes its
content.]*

A szolgáltatások javasolt bontása:

| Szolgáltatás | Felelősség |
|---|---|
| options_ingest_service | vendor snapshotok, chain-ek ingestje |
| futures_ingest_service | futures bars, quotes, contract metadata |
| reference_service | SPX, VIX, holiday, macro calendar |
| gex_engine_service | GEX-változatok, zero-gamma grid, walls |
| feature_service | VWAP, realized vol, volume-z, opening range |
| regime_service | rezsim- és confidence-kimenetek |
| strategy_service | candidate trade generálás |
| risk_service | jóváhagyás / elutasítás / méretezés |
| broker_service | order placement, cancel, query |
| reconciliation_service | broker-local állapot szinkron |
| monitoring_service | heartbeat, alerts, SLA |
| backtest_replay_service | point-in-time research pipeline |


### Állapotgép

*[A diagram appeared here in the original .docx as an embedded image and
did not survive text extraction. The surrounding prose describes its
content.]*

A monitoringnak legalább az alábbiakat kell figyelnie:

- ingest heartbeat hiány
- stale options feed
- stale futures feed
- chain completeness esés
- broker disconnect
- rejected order
- orphan order
- local/broker position mismatch
- daily loss cap trigger
- end-of-day flatten failure
- exception spike
- latency budget megsértése
Az IBKR oldalán a paper account használható API-val is, és a funkcionalitás főleg hasonló a live-hoz, de a végrehajtás szimulátorban történik, ezért a paper eredmény nem elegendő a valódi slippage és fill-minőség bizonyítására. Az API réteghez a TWS API és a Web API is elérhető; a Web API jelenlegi dokumentációja felhasználónként 10 req/s globális korlátot emel ki. Ezért a live GEX-chain tömeges feldolgozására továbbra is külön market-data vendor javasolt, míg az IBKR fő szerepe a végrehajtás. [28]


### Repository layout

```
gex-futures-bot/├── config/│   ├── research.yaml│   ├── paper.yaml│   └── live.yaml├── infra/│   ├── docker/│   ├── grafana/│   └── postgres/├── migrations/├── src/│   ├── adapters/│   │   ├── thetadata/│   │   ├── cboe/│   │   ├── databento/│   │   └── ibkr/│   ├── domain/│   │   ├── contracts.py│   │   ├── orders.py│   │   ├── positions.py│   │   └── states.py│   ├── ingest/│   ├── gex/│   │   ├── formulas.py│   │   ├── zero_gamma.py│   │   ├── walls.py│   │   └── confidence.py│   ├── features/│   ├── regime/│   ├── strategy/│   ├── risk/│   ├── broker/│   ├── reconcile/│   ├── backtest/│   ├── replay/│   ├── api/│   └── app.py├── tests/│   ├── unit/│   ├── integration/│   ├── replay/│   └── regression/└── docs/    ├── specs/    ├── runbooks/    └── handoff/
```


### Mérföldkövek, deliverable-ök és idővonal

Az időtartamok ott TBD / unspecified, ahol a csapatméret, adatelérés, licencjóváhagyás és a kutatási iterációk hossza külsőleg nem ismert.

*[A diagram appeared here in the original .docx as an embedded image and
did not survive text extraction. The surrounding prose describes its
content.]*

Részletes deliverable-lista:

| Mérföldkő | Kötelező deliverable |
|---|---|
| Licenc és vendor setup | OPRA/non-display státusz tisztázása, vendor szerződések, API access |
| Adat-normalizálás | teljes SPX/SPXW lánc snapshot save + futures bars + reference feed |
| GEX motor v1 | unsigned, naive signed, bucketed, strike-level, zero-gamma grid |
| Feature store | VWAP, volume-z, realized vol, opening range, distance metrics |
| Regime v1 | enum kimenetek + confidence + audit log |
| Strategy v1 | positive-GEX MR + negative-GEX momentum |
| Risk/Execution v1 | sizing, bracket order, kill switch, reconciliation |
| Backtest/Replay | point-in-time engine, no-lookahead tests |
| Paper | legalább több tucat stabil session, incident-free operation |
| Live v1 | 1 MES max, supervised rollout |


### Tesztelési checklist

| Teszttípus | Minimális lefedettség |
|---|---|
| Unit | GEX-képletek, strike-bucket, zero-gamma interpoláció, sizing |
| Integration | vendor ingest → DB → GEX → strategy → risk → broker |
| Replay | teljes nap re-run, ugyanaz az input ugyanazt az outputot adja |
| Chaos | feed freeze, delayed timestamps, broker disconnect, restart |
| Reconciliation | open orders, fills, positions, cancel logic |
| Regression | korábbi kutatási eredmények nem romlanak váratlanul |
| Ops | end-of-day flatten, holiday handling, roll date handling |


### Költségbecslési sávok

A publikus, ma látható árszintek alapján:

| Költségszint | Jellemző összetétel | Publikus árszint / megjegyzés | Forrás |
|---|---|---|---|
| Belépő kutatási stack | ThetaData Standard + Databento Standard CME + IBKR | körülbelül $80/hó + $199/hó + broker/adatcsomagok, plusz esetleges OPRA/market data díjak | [29] |
| Fejlettebb live stack | ThetaData Pro + Databento Plus + IBKR + monitoring/VPS | $160/hó + $1,750/hó felett, vendor- és brokerfüggő ráépülő költségekkel | [30] |
| Prémium validáció | Cboe DataShop Open-Close / Quotes / All Access | All Access API induló publikus árszint $2,499/hó, magasabb tier $4,599/hó, sok történeti dataset selection-based | [31] |
| IBKR futures execution | futures commission + exchange/reg fees | publikus oldalakon a micro futures díj $0.10–$0.25/contract + exchange/reg fees tartományként is megjelenik; a pontos teljes költség termék- és csomagfüggő | [32] |
| OPRA / non-display kockázat | automatizált OPRA-felhasználás esetén | az OPRA fee schedule külön non-display díjakat tartalmaz; a pontos kötelezettség use case-függő | [33] |


### Végső mérnöki ajánlás

A leghatékonyabb első implementáció:

- Signal layer: SPX/SPXW opciós lánc
- Execution layer: MES
- Data stack: ThetaData + Databento
- Validation augmentation: Cboe DataShop
- Broker: IBKR
- Core principle: GEX mint rezsim- és szintmodell, nem mint önálló trade trigger
A legfontosabb átadandó elv a kódoló ügynököknek ez legyen:

> A bot nem azt kérdezi, hogy „pozitív vagy negatív a GEX?”.A bot azt kérdezi, hogy „milyen hedge-rezsim valószínű, mennyire megbízható ez a becslés, és az ár/volumen/volatilitás most valóban ad-e végrehajtható setupot ezen a rezsimen belül?”

Ez a megközelítés jobban illeszkedik ahhoz, amit a jelenlegi exchange-, vendor- és akadémiai források alátámasztanak: a gamma-pozicionálás fontos mikrostruktúra-változó lehet, de a publikus adatokból számolt GEX önmagában becslés, amelyet adatminőségi, flow- és végrehajtási kontrollokkal kell körbebástyázni. [34]

[1] [5] [8] Micro E-mini S&P 500 Index Futures Quotes

https://www.cmegroup.com/markets/equities/sp/micro-e-mini-sandp-500.html?utm_source=chatgpt.com

[2] [22] OCC - Series Search

https://www.theocc.com/Market-Data/Market-Data-Reports/Series-and-Trading-Data/Series-Search?symbol=A&symbolType=U&utm_source=chatgpt.com

[3] [11] [29] [30] Theta Data | Pricing

https://www.thetadata.net/pricing?utm_source=chatgpt.com

[4] [23] [34] Option gamma and stock returns

https://www.sciencedirect.com/science/article/pii/S0927539823001093?utm_source=chatgpt.com

[6] SPX® Index Options

https://cdn.cboe.com/resources/spx/spx-fact-sheet.pdf?utm_source=chatgpt.com

[7] [10] S&P 500 Index Options Product Specifications

https://www.cboe.com/en/tradable-products/sp-500/spx-options/spx-specifications/?utm_source=chatgpt.com

[9] E-mini S&P 500 Futures Contract Specs

https://www.cmegroup.com/markets/equities/sp/e-mini-sandp500.contractSpecs.html?utm_source=chatgpt.com

[12] [15] Option Quotes

https://datashop.cboe.com/option-quote-intervals?utm_source=chatgpt.com

[13] [16] [25] How to get continuous contracts

https://databento.com/docs/examples/symbology/continuous?utm_source=chatgpt.com

[14] Market Data Subscriptions | IBKR API | IBKR Campus

https://www.interactivebrokers.com/campus/ibkr-api-page/market-data-subscriptions/?utm_source=chatgpt.com

[17] [33] OPRA Non-Display Use Declaration

https://cdn.opraplan.com/documents/OPRA_Non_Display_Declaration.pdf?utm_source=chatgpt.com

[18] Live data guide | Databento portal

https://databento.com/docs/portal/live-data?utm_source=chatgpt.com

[19] Options Data | ThetaData

https://www.thetadata.net/options-data?utm_source=chatgpt.com

[20] Cboe Open-Close Volume Summary - Cboe DataShop

https://datashop.cboe.com/cboe-options-open-close-volume-summary?utm_source=chatgpt.com

[21] The State of the Options Industry: 2025

https://www.cboe.com/insights/posts/the-state-of-the-options-industry-2025/?utm_source=chatgpt.com

[24] TWS Python API Placing Complex Orders | Trading Lesson

https://www.interactivebrokers.com/campus/trading-lessons/python-complex-orders/?utm_source=chatgpt.com

[26] Equity Index Roll Dates

https://www.cmegroup.com/trading/equity-index/rolldates.html?utm_source=chatgpt.com

[27] The Fed - Meeting calendars and information

https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm?utm_source=chatgpt.com

[28] Paper Trading Account | IBKR Glossary

https://www.interactivebrokers.com/campus/glossary-terms/paper-trading-account/?utm_source=chatgpt.com

[31] Cboe All Access API

https://datashop.cboe.com/cboe-all-access-api?utm_source=chatgpt.com

[32] Micro Futures Comparison

https://www.interactivebrokers.com/en/trading/micro-futures-comparison.php?utm_source=chatgpt.com
