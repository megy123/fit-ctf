# Osnova prezentácie

Prezentácia je mierená na piatok 19. 6. 2026. Cieľ je najprv ukázať, že rozumieš
existujúcemu frameworku, a potom vysvetliť návrh A&D ako prirodzené rozšírenie.

## 1. Kontext

Krátke otvorenie:

> Framework fit-ctf je dnes kontajnerovo orientovaný CTF systém s CLI pre organizátora
> a TUI pre súťažiacich. Vie spravovať projekty, používateľov, enrollmenty, scenáre,
> per-user klastre a submit statických flagov. Cieľom návrhu je doplniť Attack &
> Defense režim bez rozbitia existujúceho klasického CTF režimu.

## 2. Súčasná architektúra

Body na slide:

- `fit-ctf` CLI a `fit-rendezvous` TUI používajú spoločné jadro `CTFApp`.
- `CTFBase` inicializuje MongoDB, cesty, kontajnerový runtime a manažéry.
- Dáta sú v MongoDB, runtime súbory sú v dátových priečinkoch.
- Scenáre sú Jinja šablóny, ktoré sa kompilujú do compose súborov.
- Runtime beží cez Podman/Docker abstrakciu.

Diagram na slide: použiť diagram z časti 1 v `01-architektura-frameworku.md`.

## 3. Najdôležitejšia existujúca výhoda

Body:

- Framework už má `ProjectCluster` a `UserCluster`.
- `ProjectCluster` je vhodný pre organizátorské komponenty.
- `UserCluster` je vhodný ako základ pre tímové služby.
- `shared`, `private` a `operational` siete prirodzene mapujú A&D potreby.

Veta:

> Pre A&D teda nepotrebujeme meniť základný deployment model. Potrebujeme hlavne
> doplniť tick-based doménovú logiku, dynamické flagy, checker a scoring.

## 4. Čo dnes chýba pre A&D

Body:

- tím ako prvotriedna entita,
- tick scheduler,
- dynamická rotácia flagov,
- checker dostupnosti a korektnosti služby,
- atribúcia flagu na obeť a útočníka,
- A&D scoring a scoreboard,
- submission API pre automatizované exploit skripty.

## 5. Navrhovaný A&D model

Body:

- Projekt dostane `AttackDefenseProjectConfig`.
- Každý tím dostane vlastný tímový klaster.
- Súťažné služby sú rovnaké pre všetky tímy.
- Project cluster obsahuje checker, flag rotator, submission API a scoreboard.
- Flag je objekt: projekt, vlastník, služba, tick, platnosť, hash.
- Submission je objekt: útočník, obeť, služba, flag, tick, čas, výsledok.

Diagram na slide: architektúra z časti 2 v `02-navrh-attack-defense.md`.

## 6. Tick lifecycle

Body:

1. Scheduler otvorí nový tick.
2. Flag manager vygeneruje flagy.
3. Flag rotator ich vloží do tímových služieb.
4. Checker overí služby.
5. Submission manager prijíma a validuje flagy.
6. Score manager prepočíta skóre.

Veta:

> Tick je hranica konzistentnosti. Všetky flagy, checky a skóre sa viažu ku konkrétnemu
> ticku, vďaka čomu je systém auditovateľný.

## 7. Scoring

MVP vzorec:

```text
total = attack_score + defense_score + sla_score
```

Body:

- Attack: body za prijaté cudzie flagy.
- Defense: body za dostupné a korektné vlastné služby.
- SLA: dlhodobá dostupnosť služby.
- Neskôr sa dá pridať dynamická hodnota flagu alebo first blood bonus.

## 8. MVP implementačná stratégia

Body:

- Nezačať veľkým rewrite.
- Ako prvý prototyp použiť existujúci `UserCluster` pre technický tímový účet.
- Pridať A&D kolekcie do MongoDB.
- Implementovať file-based flag planting.
- Implementovať jeden checker kontrakt.
- Pridať `fit-ctf ad` CLI.
- Až potom riešiť plný `TeamCluster`, API, TUI a robustnejší scoring.

## 9. Rozhodnutia na diskusiu

Otázky:

- Má byť tím prvotriedna entita už v MVP?
- Stačí pre prvý prototyp file-based planting?
- Má byť submission API súčasťou prvej verzie?
- Aký scoring je očakávaný pre školský/labový use case?
- Má checker bežať ako kontajner, Python plugin alebo externý proces?

## 10. Záver

Krátke uzavretie:

> Návrh využíva existujúcu architektúru frameworku: projekty, klastre, scenáre,
> kompiláciu compose súborov a kontajnerový runtime. A&D režim pridáva hlavne novú
> doménovú vrstvu: tímy, tick, rotujúce flagy, checky, submissiony a scoring. Vďaka
> tomu sa dá začať malým prototypom a postupne ho rozširovať bez toho, aby sa rozbil
> dnešný klasický CTF workflow.
